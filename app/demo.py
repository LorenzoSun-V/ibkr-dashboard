"""演示模式：生成模拟数据（两个账户、含出入金）并启动网页，不需要 IBKR 账户，也不会影响正式数据。

    python -m app.demo
"""
import os
import random
from datetime import date, timedelta

# 必须在导入 config 之前设置，.env 里的值不会覆盖这些
os.environ.update({
    "DB_PATH": "data/demo.db",
    "IBKR_FLEX_TOKEN": "",
    "IBKR_FLEX_QUERY_ID": "",
    "ACCOUNT_ALIASES": "U1000001:主账户,U1000002:IRA",
    "AUTO_FETCH_TIME": "",
    "DEMO_MODE": "1",
})
os.environ.setdefault("PORT", "8001")


def _statement(rng: random.Random, acct: str, start_nav: float, deposits: dict[date, float], end: date) -> str:
    d = end - timedelta(days=600)
    nav = start_nav
    navs, cash = [], []
    while d <= end:
        if d.weekday() < 5:
            flow = deposits.get(d, 0.0)
            nav = nav * (1 + rng.gauss(0.0006, 0.012)) + flow
            navs.append(f'<EquitySummaryByReportDateInBase accountId="{acct}" currency="USD" '
                        f'reportDate="{d:%Y%m%d}" total="{nav:.2f}" />')
            if flow:
                cash.append(f'<CashTransaction accountId="{acct}" currency="USD" fxRateToBase="1" '
                            f'type="Deposits/Withdrawals" amount="{flow:.2f}" reportDate="{d:%Y%m%d}" '
                            f'description="CASH RECEIPTS" levelOfDetail="DETAIL" />')
        d += timedelta(days=1)
    return (f'<FlexStatement accountId="{acct}" period="Custom">'
            f'<EquitySummaryInBase>{"".join(navs)}</EquitySummaryInBase>'
            f'<CashTransactions>{"".join(cash)}</CashTransactions>'
            f'{_trades(rng, acct, end, 40 if start_nav > 500_000 else 15)}</FlexStatement>')


STOCKS = [("TSLA", "TESLA INC", 300), ("NVDA", "NVIDIA CORP", 150), ("TSM", "TAIWAN SEMICONDUCTOR-SP ADR", 220),
          ("AAPL", "APPLE INC", 230), ("CRCL", "CIRCLE INTERNET GROUP INC", 120), ("SOXL", "DIREXION DAILY SEMICOND BULL 3X", 30),
          ("MSTR", "STRATEGY INC", 350), ("PLTR", "PALANTIR TECHNOLOGIES INC-A", 140), ("IREN", "IREN LTD", 25)]


def _trade_xml(acct, d, t, side, qty, price, order_id, trade_id, realized, open_close, *, opt=None):
    sym, desc, _ = t
    commission = -round(max(1.0, qty * (0.65 if opt else 0.005)), 2)
    mult = 100 if opt else 1
    proceeds = (-1 if side == "BUY" else 1) * qty * price * mult
    attrs = {
        "accountId": acct, "currency": "USD", "fxRateToBase": "1", "assetCategory": "OPT" if opt else "STK",
        "symbol": sym, "description": desc, "conid": "-".join(map(str, (sym, *opt))) if opt else sym, "tradeDate": f"{d:%Y%m%d}",
        "dateTime": f"{d:%Y%m%d};{rng_time(trade_id)}", "buySell": side, "quantity": f"{qty if side == 'BUY' else -qty}",
        "tradePrice": f"{price:.2f}", "proceeds": f"{proceeds:.2f}", "ibCommission": f"{commission}",
        "fifoPnlRealized": f"{realized:.2f}", "openCloseIndicator": open_close, "multiplier": str(mult),
        "ibOrderID": str(order_id), "transactionID": str(trade_id), "orderType": "LMT", "exchange": "SMART",
        "levelOfDetail": "EXECUTION",
    }
    if opt:
        expiry, strike, pc = opt
        attrs.update({"underlyingSymbol": sym, "putCall": pc, "strike": str(strike), "expiry": expiry,
                      "symbol": f"{sym} {expiry[2:]}{pc}{strike * 1000:08.0f}",
                      "description": f"{sym} {expiry} {strike} {pc}"})
    return "<Trade " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + " />"


def rng_time(seed) -> str:
    r = random.Random(seed)
    return f"{r.randint(9, 15):02d}{r.randint(0, 59):02d}{r.randint(0, 59):02d}"


def _trades(rng: random.Random, acct: str, end: date, n: int) -> str:
    rows, order_id, trade_id = [], rng.randint(10**8, 9 * 10**8), rng.randint(10**9, 9 * 10**9)
    for _ in range(n):
        t = rng.choice(STOCKS)
        opt = None
        if rng.random() < 0.3:
            opt = ((end + timedelta(days=rng.randint(90, 400))).strftime("%Y%m%d"),
                   int(t[2] * rng.choice([1.0, 1.2, 1.5]) / 10) * 10, rng.choice("CCP"))
        qty = rng.choice([5, 10, 20]) if opt else rng.choice([10, 20, 50, 100, 200, 500])
        buy_px = (t[2] * rng.uniform(0.08, 0.2)) if opt else t[2] * rng.uniform(0.8, 1.2)
        buy_d = end - timedelta(days=rng.randint(20, 560))
        while buy_d.weekday() >= 5:
            buy_d -= timedelta(days=1)
        order_id += 1
        # 部分订单拆成两笔成交
        parts = [qty] if qty < 50 or rng.random() < 0.6 else [qty // 2, qty - qty // 2]
        for q in parts:
            trade_id += 1
            rows.append(_trade_xml(acct, buy_d, t, "BUY", q, buy_px, order_id, trade_id, 0, "O", opt=opt))
        if rng.random() < 0.8:  # 80% 已平仓
            sell_d = buy_d + timedelta(days=rng.randint(1, 60))
            while sell_d.weekday() >= 5:
                sell_d += timedelta(days=1)
            if sell_d > end:
                continue
            sell_px = buy_px * rng.uniform(0.5, 1.9) if opt else buy_px * rng.uniform(0.8, 1.3)
            mult = 100 if opt else 1
            realized = (sell_px - buy_px) * qty * mult - 2 * max(1.0, qty * (0.65 if opt else 0.005))
            order_id += 1
            trade_id += 1
            rows.append(_trade_xml(acct, sell_d, t, "SELL", qty, sell_px, order_id, trade_id, realized, "C", opt=opt))
    return f"<Trades>{''.join(rows)}</Trades>"


def build_sample_xml(end: date | None = None, seed: int = 7) -> str:
    end = end or date.today() - timedelta(days=1)
    rng = random.Random(seed)
    return ('<FlexQueryResponse queryName="Demo" type="AF"><FlexStatements count="2">'
            + _statement(rng, "U1000001", 800_000, {end - timedelta(days=400): 50_000}, end)
            + _statement(rng, "U1000002", 150_000, {end - timedelta(days=200): 20_000}, end)
            + "</FlexStatements></FlexQueryResponse>")


def main():
    from .config import settings
    from .fetch import import_xml
    from .server import main as serve

    settings.db_path.unlink(missing_ok=True)
    import_xml(build_sample_xml(), "demo")
    print(f"演示页面：http://{settings.host}:{settings.port}")
    serve()


if __name__ == "__main__":
    main()
