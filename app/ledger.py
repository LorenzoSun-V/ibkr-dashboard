"""给记账软件（算盘）用的只读数据：每日 NAV 和资金流水，原样保存、按版本取用。

和网页用的 nav / flows 表分开：
- 金额、汇率保存 Flex 里的原始十进制文本，计算用 Decimal，不经过浮点；
- 每份报表（一个 Query 里的一个账户）是一个版本，带覆盖区间、包含的 section 和核对结果，不覆盖旧版本；
- 资金流水带 IBKR 的 transactionID、原币金额、币种、汇率、dateTime、reportDate。

当前数据 = 按报表生成时间（whenGenerated）从旧到新叠加完整的报表：较新的报表在它覆盖的日期范围内取代较旧的。
所以 IBKR 修订历史数据后，算盘拿到的是修订后的值和提供它的报表编号；导入顺序不影响结果。
核对不通过的报表照样保存，但不参与叠加，接口会报告它的问题。
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .parser import parse_date

ET_ZONE = ZoneInfo("America/New_York")  # Flex 的 dateTime、whenGenerated 是美东时间
REQUIRED_SECTIONS = ("EquitySummaryInBase", "CashTransactions", "Transfers", "ChangeInNAV")
CHANGE_IN_NAV_FIELDS = ("startingValue", "endingValue", "linkingAdjustments", "depositsWithdrawals",
                        "internalCashTransfers", "assetTransfers")
FLOW_TOLERANCE = Decimal("0.01")  # Change in NAV 的汇总值和明细合计允许的差（汇总值本身按分舍入）
SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_statements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL,
    source         TEXT NOT NULL,
    imported_at    TEXT NOT NULL,
    when_generated TEXT NOT NULL,
    from_date      TEXT,
    to_date        TEXT,
    period         TEXT,
    base_currency  TEXT,
    sections       TEXT NOT NULL,
    change_in_nav  TEXT,
    problems       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_statements ON ledger_statements (account_id, when_generated);
CREATE TABLE IF NOT EXISTS ledger_nav (
    statement_id INTEGER NOT NULL,
    date         TEXT NOT NULL,
    total        TEXT NOT NULL,
    PRIMARY KEY (statement_id, date)
);
CREATE TABLE IF NOT EXISTS ledger_flows (
    statement_id   INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    kind           TEXT NOT NULL,
    type           TEXT,
    direction      TEXT NOT NULL,
    asset_category TEXT,
    symbol         TEXT,
    quantity       TEXT,
    currency       TEXT NOT NULL,
    amount         TEXT NOT NULL,
    fx_rate        TEXT NOT NULL,
    base_amount    TEXT NOT NULL,
    date_time      TEXT,
    report_date    TEXT NOT NULL,
    settle_date    TEXT,
    description    TEXT,
    counterparty   TEXT,
    PRIMARY KEY (statement_id, transaction_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    name       TEXT PRIMARY KEY,
    key_hash   TEXT NOT NULL UNIQUE,
    accounts   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
"""


def dec(raw: str | None) -> Decimal | None:
    """Flex 里的数字原样转成 Decimal；空值返回 None。"""
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    if s in ("", "--"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"无法识别的数字: {raw!r}") from None


def _plain(raw: str | None) -> str | None:
    """保存用的文本：去掉千分位，其余保持原样（不改变小数位）。"""
    return None if dec(raw) is None else raw.strip().replace(",", "")


def et_datetime(raw: str | None) -> str | None:
    """dateTime "20251210;200801"（美东）→ "2025-12-10T20:08:01-05:00"；只有日期时 → "2025-12-10"。"""
    if not raw:
        return None
    s = raw.strip()
    for sep in (";", ",", " ", "T"):
        if sep in s:
            d_part, t_part = s.split(sep, 1)
            digits = "".join(c for c in t_part if c.isdigit())
            if len(digits) >= 4:
                d = parse_date(d_part)
                t = digits.ljust(6, "0")
                dt = datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), int(t[4:6]), tzinfo=ET_ZONE)
                return dt.isoformat()
            break
    d = parse_date(s)
    return d.isoformat() if d else None


@dataclass
class RawStatement:
    account_id: str
    when_generated: str
    from_date: date | None
    to_date: date | None
    period: str | None
    base_currency: str | None
    sections: list[str]
    change_in_nav: dict[str, str] | None
    nav: dict[date, str] = field(default_factory=dict)
    flows: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _detail(el: ET.Element) -> bool:
    lod = (el.get("levelOfDetail") or "DETAIL").upper()
    return "SUMMARY" not in lod and el.get("currency") != "BASE_SUMMARY"


def _signed(amount: Decimal, direction: str | None) -> Decimal:
    direction = (direction or "").upper()
    if direction == "IN":
        return abs(amount)
    if direction == "OUT":
        return -abs(amount)
    return amount


def _flow(row: ET.Element, kind: str, amount: Decimal, base: Decimal, fx: Decimal) -> dict:
    report = parse_date(row.get("reportDate"))  # 归属日只认 reportDate，不用 date / dateTime 推测
    settle = parse_date(row.get("settleDate"))
    return {
        "transaction_id": row.get("transactionID") or "",
        "kind": kind,
        "type": row.get("type") or "",
        "direction": "IN" if amount > 0 else "OUT",
        "asset_category": (row.get("assetCategory") or "").upper(),
        "symbol": row.get("symbol") or "",
        "quantity": _plain(row.get("quantity")),
        "currency": row.get("currency") or "",
        "amount": str(amount),
        "fx_rate": str(fx),
        "base_amount": str(base),
        "date_time": et_datetime(row.get("dateTime")),
        "report_date": report.isoformat() if report else "",
        "settle_date": settle.isoformat() if settle else None,
        "description": row.get("description") or "",
        "counterparty": row.get("account") or "",
    }


def _when_generated(raw: str | None) -> str:
    """"20261015;091852" → "2026-10-15T09:18:52"（美东，可直接按字符串排序）。"""
    if not raw:
        return ""
    s = et_datetime(raw) or ""
    return s[:19]


def _parse_statement(st: ET.Element) -> RawStatement:
    sections = [child.tag for child in st]
    cin_el = st.find("ChangeInNAV")
    raw = RawStatement(
        account_id=st.get("accountId", ""),
        when_generated=_when_generated(st.get("whenGenerated")),
        from_date=parse_date(st.get("fromDate")),
        to_date=parse_date(st.get("toDate")),
        period=st.get("period"),
        base_currency=None,
        sections=sections,
        change_in_nav=dict(cin_el.attrib) if cin_el is not None else None,
    )
    problems = raw.problems

    for row in st.iter("EquitySummaryByReportDateInBase"):
        d = parse_date(row.get("reportDate"))
        total = _need(row, "total", problems)
        if d is None:
            problems.append("每日 NAV 有一行缺少 reportDate")
        if d is None or total is None:
            continue
        raw.nav[d] = _plain(row.get("total"))
        raw.base_currency = raw.base_currency or row.get("currency")
    if raw.base_currency is None and cin_el is not None:
        raw.base_currency = cin_el.get("currency")

    for row in st.iter("CashTransaction"):
        if not _detail(row) or (row.get("type") or "") != "Deposits/Withdrawals":
            continue
        amount = _need(row, "amount", problems)
        fx = _fx(row, problems)
        if amount is None or fx is None or not _has_report_date(row, problems):
            continue  # 报表已标为不完整：不拿 0、1 或推测的日期顶替
        if amount:
            raw.flows.append(_flow(row, "cash", amount, amount * fx, fx))

    blank = 0
    for row in st.iter("Transfer"):
        if not _detail(row):
            continue
        if row.get("direction") is None and row.get("positionAmount") is None and row.get("cashTransfer") is None:
            blank += 1
            continue
        if not _has_report_date(row, problems):
            continue
        if (row.get("assetCategory") or "").upper() == "CASH":
            cash, fx = _need(row, "cashTransfer", problems), _fx(row, problems)
            if cash is None or fx is None:
                continue
            amount = _signed(cash, row.get("direction"))
            base = amount * fx
            kind = "internal" if (row.get("type") or "").upper() == "INTERNAL" else "cash_transfer"
        else:
            in_base = _number(row, "positionAmountInBase", problems)
            if in_base is not None:
                amount = _signed(_number(row, "positionAmount", problems) or Decimal(0), row.get("direction"))
                base = _signed(in_base, row.get("direction"))
                fx = _number(row, "fxRateToBase", problems) or Decimal(1)
            else:
                position, fx = _need(row, "positionAmount", problems), _fx(row, problems)
                if position is None or fx is None:
                    continue
                amount = _signed(position, row.get("direction"))
                base = amount * fx
            kind = "position"
        if amount or base:
            raw.flows.append(_flow(row, kind, amount, base, fx))

    if not raw.when_generated:
        problems.append("报表缺少生成时间（whenGenerated）")
    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing:
        problems.append("Flex Query 缺少 section：" + "、".join(missing))
    if raw.base_currency and raw.base_currency != "USD":
        problems.append(f"账户本位币是 {raw.base_currency}，目前只支持 USD")
    if not raw.nav:
        problems.append("没有每日 NAV")
    if blank:
        problems.append(f"Transfers 有 {blank} 条记录没有字段，请在该 section 中 Select All")
    seen = set()
    for f in raw.flows:
        if not f["transaction_id"]:
            problems.append(f"{f['report_date']} 有一笔资金流水没有 transactionID")
        elif f["transaction_id"] in seen:
            problems.append(f"transactionID {f['transaction_id']} 在同一份报表里出现了两次")
        seen.add(f["transaction_id"])
        if not f["report_date"]:
            problems.append(f"资金流水 {f['transaction_id']} 没有日期")
    if "ChangeInNAV" in sections and raw.change_in_nav is None:
        problems.append("Change in NAV 没有数据")
    if raw.change_in_nav is not None and raw.nav:
        problems.extend(_check_change_in_nav(raw))
    return raw


def _label(row: ET.Element) -> str:
    return f"{row.tag} {row.get('transactionID') or '（无编号）'}"


def _number(row: ET.Element, attr: str, problems: list[str]) -> Decimal | None:
    """A number that may be absent; one that is there but unreadable makes the statement incomplete."""
    try:
        return dec(row.get(attr))
    except ValueError:
        problems.append(f"{_label(row)} 的 {attr} 不是数字：{row.get(attr)!r}")
        return None


def _need(row: ET.Element, attr: str, problems: list[str]) -> Decimal | None:
    """A number the ledger books from: missing or unreadable makes the statement incomplete."""
    if row.get(attr) is None or not row.get(attr).strip():
        problems.append(f"{_label(row)} 缺少 {attr}")
        return None
    return _number(row, attr, problems)


def _fx(row: ET.Element, problems: list[str]) -> Decimal | None:
    """Rate to USD. Required for any other currency; a USD row may leave it out (it is 1)."""
    if (row.get("currency") or "") == "USD" and not (row.get("fxRateToBase") or "").strip():
        return Decimal(1)
    if not row.get("currency"):
        problems.append(f"{_label(row)} 缺少 currency")
        return None
    fx = _need(row, "fxRateToBase", problems)
    if fx is not None and fx <= 0:
        problems.append(f"{_label(row)} 的 fxRateToBase 是 {fx}")
        return None
    return fx


def _has_report_date(row: ET.Element, problems: list[str]) -> bool:
    if parse_date(row.get("reportDate")) is None:
        problems.append(f"{_label(row)} 缺少 reportDate")
        return False
    return True


def _check_change_in_nav(raw: RawStatement) -> list[str]:
    cin = raw.change_in_nav
    out = []
    values = {}
    for k in CHANGE_IN_NAV_FIELDS:  # 核对要用的字段缺一个，这份报表就不能用
        try:
            values[k] = dec(cin.get(k))
        except ValueError:
            values[k] = None
        if values[k] is None:
            out.append(f"Change in NAV 缺少或无法识别 {k}")
    if out:
        return out
    if values["linkingAdjustments"]:
        out.append(f"Change in NAV 有 linkingAdjustments {values['linkingAdjustments']}，需要人工处理")
    first, last = min(raw.nav), max(raw.nav)
    start, end = values["startingValue"], values["endingValue"]
    if start != Decimal(raw.nav[first]):
        out.append(f"Change in NAV 期初值 {start} 和 {first} 的 NAV {raw.nav[first]} 不一致")
    if end != Decimal(raw.nav[last]):
        out.append(f"Change in NAV 期末值 {end} 和 {last} 的 NAV {raw.nav[last]} 不一致")
    expected = sum(values[k] for k in ("depositsWithdrawals", "internalCashTransfers", "assetTransfers"))
    lo, hi = parse_date(cin.get("fromDate")) or raw.from_date, parse_date(cin.get("toDate")) or raw.to_date
    got = sum((Decimal(f["base_amount"]) for f in raw.flows
               if (lo is None or f["report_date"] >= lo.isoformat()) and (hi is None or f["report_date"] <= hi.isoformat())),
              Decimal(0))
    if abs(got - expected) > FLOW_TOLERANCE:
        out.append(f"资金流水明细合计 {got} 和 Change in NAV 的出入金合计 {expected} 不一致")
    return out


def parse_records(text: str) -> list[RawStatement]:
    root = ET.fromstring(text)
    return [_parse_statement(st) for st in root.findall(".//FlexStatement")]


def save(conn, statements: list[RawStatement], source: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for st in statements:
        if not st.account_id:
            continue
        cur = conn.execute(
            "INSERT INTO ledger_statements (account_id, source, imported_at, when_generated, from_date, to_date, period,"
            " base_currency, sections, change_in_nav, problems) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (st.account_id, source, now, st.when_generated,
             st.from_date.isoformat() if st.from_date else None, st.to_date.isoformat() if st.to_date else None,
             st.period, st.base_currency, json.dumps(st.sections),
             json.dumps(st.change_in_nav) if st.change_in_nav is not None else None,
             json.dumps(st.problems, ensure_ascii=False)))
        sid = cur.lastrowid
        conn.executemany("INSERT INTO ledger_nav (statement_id, date, total) VALUES (?, ?, ?)",
                         [(sid, d.isoformat(), v) for d, v in st.nav.items()])
        cols = ("transaction_id", "kind", "type", "direction", "asset_category", "symbol", "quantity", "currency",
                "amount", "fx_rate", "base_amount", "date_time", "report_date", "settle_date", "description",
                "counterparty")
        conn.executemany(
            f"INSERT OR IGNORE INTO ledger_flows (statement_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            [(sid, *(f[c] for c in cols)) for f in st.flows])


def _ranges(st) -> tuple[str, str, str]:
    """(NAV 覆盖起点, 流水覆盖起点, 终点)。NAV 的第一行是 fromDate 的前一天（期初值）。"""
    lo = st["from_date"] or st["nav_lo"]
    hi = st["to_date"] or st["nav_hi"]
    nav_lo = min(st["nav_lo"], lo) if st["nav_lo"] and lo else lo
    return nav_lo, lo, hi


def current(conn, account_id: str) -> dict:
    """叠加该账户所有完整的报表，得到当前的 NAV、资金流水和覆盖区间。"""
    rows = conn.execute(
        "SELECT s.*, (SELECT MIN(date) FROM ledger_nav n WHERE n.statement_id = s.id) nav_lo,"
        " (SELECT MAX(date) FROM ledger_nav n WHERE n.statement_id = s.id) nav_hi"
        " FROM ledger_statements s WHERE account_id = ? ORDER BY when_generated, id", (account_id,)).fetchall()
    nav: dict[str, tuple[str, int]] = {}
    flows: dict[str, dict] = {}
    spans: list[list[str]] = []
    base_currency = None
    for st in rows:
        if json.loads(st["problems"]):
            continue
        nav_lo, lo, hi = _ranges(st)
        if not lo or not hi:
            continue
        base_currency = st["base_currency"] or base_currency
        for d in [d for d in nav if nav_lo <= d <= hi]:
            del nav[d]
        for d, total in conn.execute("SELECT date, total FROM ledger_nav WHERE statement_id = ?", (st["id"],)):
            nav[d] = (total, st["id"])
        for k in [k for k, f in flows.items() if lo <= f["report_date"] <= hi]:
            del flows[k]
        for f in conn.execute("SELECT * FROM ledger_flows WHERE statement_id = ?", (st["id"],)):
            flows[f["transaction_id"]] = dict(f)
        spans.append([lo, hi])
    return {"nav": nav, "flows": flows, "coverage": _merge(spans), "base_currency": base_currency,
            "statements": rows}


def _merge(spans: list[list[str]]) -> list[list[str]]:
    out: list[list[str]] = []
    for lo, hi in sorted(spans):
        if out and date.fromisoformat(lo) <= date.fromisoformat(out[-1][1]) + timedelta(days=1):
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _statement_info(st) -> dict:
    return {"id": st["id"], "when_generated": st["when_generated"], "from": st["from_date"], "to": st["to_date"],
            "imported_at": st["imported_at"], "problems": json.loads(st["problems"])}


def statement(conn, account_id: str, since: str) -> dict:
    """接口 /api/ledger/statement 的内容：since（含）之后的 NAV 和资金流水，以及完整性信息。"""
    cur = current(conn, account_id)
    nav = cur["nav"]
    complete = [st for st in cur["statements"] if not json.loads(st["problems"])]
    latest = cur["statements"][-1] if cur["statements"] else None
    flows = sorted((f for f in cur["flows"].values() if f["report_date"] >= since),
                   key=lambda f: (f["report_date"], f["transaction_id"]))
    return {
        "account": account_id,
        "base_currency": cur["base_currency"],
        "latest_report_date": max(nav) if nav else None,
        "coverage": cur["coverage"],
        "latest_statement": _statement_info(latest) if latest else None,
        "latest_complete_statement": _statement_info(complete[-1]) if complete else None,
        "nav": [{"date": d, "total": v, "statement_id": sid} for d, (v, sid) in sorted(nav.items()) if d >= since],
        "flows": [{k: f[k] for k in f if k != "statement_id"} | {"statement_id": f["statement_id"]} for f in flows],
    }
