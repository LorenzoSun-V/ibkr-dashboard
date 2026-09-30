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


def test_trades_orders_and_ranking():
    from app import trades

    xml = """<FlexQueryResponse><FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="20260701" toDate="20260930">
<Trades>
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="TSLA" description="TESLA INC" conid="76792991"
    tradeDate="20260730" dateTime="20260730;110150" buySell="BUY" quantity="300" tradePrice="303.00" proceeds="-90900"
    ibCommission="-1.5" fifoPnlRealized="0" openCloseIndicator="O" ibOrderID="1001" transactionID="1" levelOfDetail="EXECUTION" />
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="TSLA" description="TESLA INC" conid="76792991"
    tradeDate="20260730" dateTime="20260730;110152" buySell="BUY" quantity="200" tradePrice="304.75" proceeds="-60950"
    ibCommission="-1" fifoPnlRealized="0" openCloseIndicator="O" ibOrderID="1001" transactionID="2" levelOfDetail="EXECUTION" />
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="OPT" symbol="TSLA  270617C00400000" underlyingSymbol="TSLA"
    description="TSLA 17JUN27 400 C" conid="999" putCall="C" strike="400" expiry="20270617" multiplier="100"
    tradeDate="20260922" dateTime="20260922;100249" buySell="SELL" quantity="-5" tradePrice="55.04" proceeds="27520"
    ibCommission="-3.5" fifoPnlRealized="12138.5" openCloseIndicator="C" ibOrderID="2002" transactionID="3" levelOfDetail="EXECUTION" />
  <Trade accountId="U1" symbol="TSLA" levelOfDetail="ORDER" quantity="500" tradeDate="20260730" />
</Trades>
</FlexStatement></FlexStatements></FlexQueryResponse>"""
    (st,) = parse_flex_xml(xml)
    assert len(st.trades) == 3  # ORDER 汇总行被忽略
    rows = [dict(t, account_id="U1") for t in st.trades]
    fx = calc.FxConverter([], "USD")

    orders = trades.build_orders(rows, fx, {"U1": "USD"})
    assert len(orders) == 2
    opt, stk = orders  # 按时间倒序
    assert (opt["name"], opt["code"], opt["side"], opt["quantity"]) == ("TSLA Call", "270617 400", "SELL", 5)
    assert (stk["quantity"], stk["fills"], stk["time"]) == (500, 2, "11:01:50")
    assert abs(stk["avg_price"] - (300 * 303 + 200 * 304.75) / 500) < 1e-9

    ranking = trades.build_ranking(rows, fx, {"U1": "USD"})
    assert [(r["name"], r["pnl"]) for r in ranking] == [("TSLA Call", 12138.5)]
