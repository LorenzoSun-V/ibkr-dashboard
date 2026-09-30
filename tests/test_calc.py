from datetime import date

from app import calc
from app.parser import parse_flex_xml

XML = """<FlexQueryResponse><FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="20260801" toDate="20260805">
<EquitySummaryInBase>
  <EquitySummaryByReportDateInBase accountId="U1" currency="USD" reportDate="20260803" total="1000" />
  <EquitySummaryByReportDateInBase accountId="U1" currency="USD" reportDate="20260804" total="1100" />
  <EquitySummaryByReportDateInBase accountId="U1" currency="USD" reportDate="20260805" total="1650" />
</EquitySummaryInBase>
<CashTransactions>
  <CashTransaction accountId="U1" currency="USD" fxRateToBase="1" type="Deposits/Withdrawals" amount="500" reportDate="20260805" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1" currency="USD" fxRateToBase="1" type="Dividends" amount="3" reportDate="20260805" levelOfDetail="DETAIL" />
  <CashTransaction accountId="U1" currency="BASE_SUMMARY" type="Deposits/Withdrawals" amount="500" reportDate="20260805" levelOfDetail="SUMMARY" />
</CashTransactions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_daily_pnl_excludes_deposits():
    (st,) = parse_flex_xml(XML)
    assert st.flows == [(date(2026, 8, 5), 500.0, "cash", "")]
    rows = calc.account_daily(st.nav, [(d, a) for d, a, *_ in st.flows])
    assert [r["pnl"] for r in rows] == [100.0, 50.0]
    # 8/05：入金 500 视为开盘前到账，分母 = 1100 + 500
    assert rows[1]["denom"] == 1600.0

    fx = calc.FxConverter([], "USD")
    combined = calc.combined_daily({"U1": "USD"}, {"U1": st.nav}, {"U1": [(d, a) for d, a, *_ in st.flows]}, fx, None)
    s = calc.summarize(combined)
    assert s["pnl"] == 150.0
    assert abs(s["twr"] - ((1 + 0.1) * (1 + 50 / 1600) - 1)) < 1e-12


def test_transfers_and_reconciliation():
    xml = """<FlexQueryResponse><FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="20260803" toDate="20260805">
<ChangeInNAV accountId="U1" depositsWithdrawals="500" internalCashTransfers="-200" assetTransfers="1000" />
<EquitySummaryInBase>
  <EquitySummaryByReportDateInBase accountId="U1" currency="USD" reportDate="20260803" total="1000" />
</EquitySummaryInBase>
<CashTransactions />
<Transfers>
  <Transfer accountId="U1" assetCategory="CASH" currency="USD" fxRateToBase="1" type="INTERNAL" direction="OUT" cashTransfer="-200" reportDate="20260804" levelOfDetail="TRANSFER" />
  <Transfer accountId="U1" assetCategory="STK" symbol="AAPL" currency="USD" fxRateToBase="1" type="ACATS" direction="IN" positionAmount="1000" positionAmountInBase="1000" reportDate="20260805" levelOfDetail="TRANSFER" />
  <Transfer levelOfDetail="TRANSFER" />
</Transfers>
</FlexStatement></FlexStatements></FlexQueryResponse>"""
    (st,) = parse_flex_xml(xml)
    assert [(d.day, a) for d, a, *_ in st.flows] == [(4, -200.0), (5, 1000.0)]
    assert st.expected_flow == 1300.0
    assert any("Select All" in w for w in st.warnings)
