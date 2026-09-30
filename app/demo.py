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
            f'<CashTransactions>{"".join(cash)}</CashTransactions></FlexStatement>')


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
