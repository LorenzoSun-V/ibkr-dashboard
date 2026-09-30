"""解析 Activity Flex Query 的 XML。

用到的 section：
- EquitySummaryInBase / EquitySummaryByReportDateInBase：每日 NAV（账户基础货币）
- CashTransactions / CashTransaction：出入金（type 为 Deposits/Withdrawals）
- Transfers / Transfer：持仓转入转出（只取持仓市值部分，现金部分已在 CashTransactions 中）
- ConversionRates / ConversionRate：汇率（多币种账户合并时使用）
- Trades / Trade（levelOfDetail=EXECUTION）：逐笔成交，用于订单查询与已实现盈亏排行
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%y", "%m/%d/%y")


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    # dateTime 形如 "20250102;101500" / "2025-01-02, 10:15:00" / "20250102 101500"
    s = raw.strip()
    for sep in (";", ",", " ", "T"):
        s = s.split(sep)[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的日期格式: {raw!r}（建议在 Flex Query 中把 Date Format 设为 yyyyMMdd）")


def _num(raw: str | None) -> float:
    if raw in (None, "", "--"):
        return 0.0
    return float(raw.replace(",", ""))


def _time(raw: str | None) -> str:
    """从 dateTime（如 "20260922;100249"）中取出 "10:02:49"。"""
    if not raw:
        return ""
    for sep in (";", ",", " ", "T"):
        if sep in raw:
            digits = "".join(c for c in raw.split(sep, 1)[1] if c.isdigit())
            if len(digits) >= 4:
                digits = digits.ljust(6, "0")
                return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    return ""


def _parse_trade(row: ET.Element) -> dict | None:
    d = parse_date(row.get("tradeDate") or row.get("dateTime") or row.get("reportDate"))
    qty = _num(row.get("quantity"))
    if d is None or not qty:
        return None
    side = (row.get("buySell") or ("BUY" if qty > 0 else "SELL")).upper()
    expiry = parse_date(row.get("expiry"))
    trade_id = row.get("transactionID") or row.get("tradeID") or row.get("ibExecID")
    if not trade_id:
        trade_id = f"{row.get('conid')}-{row.get('dateTime')}-{qty}-{row.get('tradePrice')}"
    return {
        "trade_id": trade_id,
        "order_id": row.get("ibOrderID") or row.get("orderID") or trade_id,
        "trade_date": d.isoformat(),
        "time": _time(row.get("dateTime")),
        "asset": (row.get("assetCategory") or "").upper(),
        "conid": row.get("conid") or row.get("symbol") or "",
        "symbol": row.get("symbol") or "",
        "underlying": row.get("underlyingSymbol") or "",
        "description": row.get("description") or "",
        "put_call": (row.get("putCall") or "").upper(),
        "strike": _num(row.get("strike")),
        "expiry": expiry.isoformat() if expiry else "",
        "multiplier": _num(row.get("multiplier")) or 1.0,
        "currency": row.get("currency") or "",
        "fx": _num(row.get("fxRateToBase")) or 1.0,
        "side": "SELL" if side.startswith("SELL") else "BUY",
        "cancelled": "CA." in side,
        "quantity": abs(qty),
        "price": _num(row.get("tradePrice")),
        "proceeds": _num(row.get("proceeds")),
        "commission": _num(row.get("ibCommission")),
        "realized": _num(row.get("fifoPnlRealized")),
        "open_close": (row.get("openCloseIndicator") or "").upper(),
        "order_type": row.get("orderType") or "",
        "exchange": row.get("exchange") or row.get("listingExchange") or "",
    }


def _is_detail(el: ET.Element) -> bool:
    """同时勾选了 Summary 时会多出汇总行，这里只保留明细（Transfers 的明细行 levelOfDetail 为 TRANSFER）。"""
    lod = (el.get("levelOfDetail") or "DETAIL").upper()
    return "SUMMARY" not in lod and el.get("currency") != "BASE_SUMMARY"


@dataclass
class AccountStatement:
    account_id: str
    from_date: date | None
    to_date: date | None
    base_currency: str | None = None
    period: str | None = None
    sections: list[str] = field(default_factory=list)
    nav: dict[date, float] = field(default_factory=dict)
    # (date, amount_in_base, kind, description)
    flows: list[tuple[date, float, str, str]] = field(default_factory=list)
    # (date, from_ccy, to_ccy, rate)
    fx: list[tuple[date, str, str, float]] = field(default_factory=list)
    # IBKR Change in NAV 汇总里的外部资金流（入金+内部划转+转仓），用于对账；未勾选该 section 时为 None
    expected_flow: float | None = None
    warnings: list[str] = field(default_factory=list)
    # 逐笔成交；None 表示该 Query 没有勾选 Trades section（此时不应覆盖已有成交数据）
    trades: list[dict] | None = None


def parse_flex_xml(text: str) -> list[AccountStatement]:
    root = ET.fromstring(text)
    statements = root.findall(".//FlexStatement")
    if not statements:
        raise ValueError("XML 中没有 FlexStatement 节点，确认下载的是 Activity Flex Query 的 XML")

    result = []
    for st in statements:
        acct = AccountStatement(
            account_id=st.get("accountId", ""),
            from_date=parse_date(st.get("fromDate")),
            to_date=parse_date(st.get("toDate")),
            period=st.get("period"),
            sections=[child.tag for child in st],
        )

        for row in st.iter("EquitySummaryByReportDateInBase"):
            d = parse_date(row.get("reportDate"))
            if d is None or row.get("total") is None:
                continue
            acct.nav[d] = _num(row.get("total"))
            acct.base_currency = acct.base_currency or row.get("currency")

        for row in st.iter("CashTransaction"):
            if not _is_detail(row):
                continue
            if "deposit" not in (row.get("type") or "").lower():
                continue
            d = parse_date(row.get("reportDate") or row.get("settleDate") or row.get("dateTime"))
            fx = _num(row.get("fxRateToBase")) or 1.0
            acct.flows.append((d, _num(row.get("amount")) * fx, "cash", row.get("description") or ""))

        blank_transfers = 0
        for row in st.iter("Transfer"):
            if not _is_detail(row):
                continue
            if row.get("direction") is None and row.get("positionAmount") is None and row.get("cashTransfer") is None:
                blank_transfers += 1
                continue
            fx = _num(row.get("fxRateToBase")) or 1.0
            if (row.get("assetCategory") or "").upper() == "CASH":
                # 账户间现金划转：不在 CashTransactions 的 Deposits/Withdrawals 里，需要单独计入
                value = (_num(row.get("cashTransfer")) or _num(row.get("positionAmount")) or _num(row.get("quantity"))) * fx
            else:
                value = _num(row.get("positionAmountInBase")) or _num(row.get("positionAmount")) * fx
            if not value:
                continue
            direction = (row.get("direction") or "").upper()
            if direction in ("IN", "OUT"):
                value = abs(value) if direction == "IN" else -abs(value)
            d = parse_date(row.get("reportDate") or row.get("date") or row.get("dateTime"))
            desc = f"{row.get('type', '')} {row.get('symbol', '')} {direction}".strip()
            acct.flows.append((d, value, "transfer", desc))
        if blank_transfers:
            acct.warnings.append(f"Transfers 有 {blank_transfers} 条记录但没有字段，请在该 section 中 Select All 字段")
        if "CashTransactions" not in acct.sections:
            acct.warnings.append("未包含 Cash Transactions section，银行出入金无法剔除")

        if "Trades" in acct.sections:
            acct.trades = []
            for row in st.iter("Trade"):
                # 同时勾选了 Order / Closed Lots / Symbol Summary 等选项时，只取逐笔成交
                if (row.get("levelOfDetail") or "EXECUTION").upper() != "EXECUTION":
                    continue
                t = _parse_trade(row)
                if t:
                    acct.trades.append(t)
            if acct.trades is not None and st.find("Trades/Trade") is not None and not acct.trades:
                acct.warnings.append("Trades 中没有逐笔成交（Execution），请在 Trades 选项中勾选 Execution 并 Select All 字段")

        for row in st.iter("ChangeInNAV"):
            acct.expected_flow = sum(_num(row.get(k)) for k in (
                "depositsWithdrawals", "internalCashTransfers", "assetTransfers"))

        for row in st.iter("ConversionRate"):
            d = parse_date(row.get("reportDate"))
            if d and row.get("fromCurrency") and row.get("toCurrency"):
                acct.fx.append((d, row.get("fromCurrency"), row.get("toCurrency"), _num(row.get("rate"))))

        if acct.nav:
            dates = sorted(acct.nav)
            acct.from_date = acct.from_date or dates[0]
            acct.to_date = acct.to_date or dates[-1]
        result.append(acct)
    return result
