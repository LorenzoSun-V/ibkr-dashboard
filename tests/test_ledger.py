"""给记账软件用的数据：原样保存、核对、按版本叠加、带密钥的接口。"""
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, keys, ledger
from app.config import settings
from app.fetch import import_xml

FIXTURES = Path(__file__).parent / "fixtures"
A = (FIXTURES / "ledger_a.xml").read_text(encoding="utf-8")
B = (FIXTURES / "ledger_b.xml").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "ibkr.db")


def _import(text, source="test"):
    import_xml(text, source, log=lambda *_: None)


def test_parse_keeps_original_decimals_and_fields():
    (st,) = ledger.parse_records(A)
    assert st.problems == []
    assert st.when_generated == "2026-10-10T09:18:52"
    assert st.nav[min(st.nav)] == "10283.892372948"  # 第一行是 fromDate 前一天的期初值
    assert str(min(st.nav)) == "2026-09-30"
    flows = {f["transaction_id"]: f for f in st.flows}
    assert "9000000005" not in flows  # 利息不是资金流水
    cnh = flows["9000000001"]
    assert (cnh["kind"], cnh["currency"], cnh["amount"], cnh["fx_rate"], cnh["direction"]) == (
        "cash", "CNH", "70000", "0.14781", "IN")
    assert Decimal(cnh["base_amount"]) == Decimal("10346.70")
    internal = flows["9000000002"]
    assert (internal["kind"], internal["direction"], internal["amount"], internal["counterparty"]) == (
        "internal", "OUT", "-70000", "U0000098")


def test_date_time_is_eastern_and_report_date_is_kept():
    (st,) = ledger.parse_records(A)
    flows = {f["transaction_id"]: f for f in st.flows}
    # 美东周日晚上入金，归到周一的报表
    assert flows["9000000003"]["date_time"] == "2026-10-04T22:39:02-04:00"
    assert flows["9000000003"]["report_date"] == "2026-10-05"
    # 美东晚上 8 点（北京第二天早上），仍是当天的报表
    assert flows["9000000004"]["date_time"] == "2026-10-06T20:08:01-04:00"
    assert flows["9000000004"]["report_date"] == "2026-10-06"
    # 只有日期没有时间
    assert flows["9000000001"]["date_time"] == "2026-10-01"
    assert ledger.et_datetime("20251210;200801") == "2025-12-10T20:08:01-05:00"  # 冬令时


def test_empty_cash_section_is_fine_but_missing_is_not():
    (st,) = ledger.parse_records(B)
    assert st.problems == []
    assert {f["transaction_id"] for f in st.flows} == {"9000000002", "9000000006"}
    (st,) = ledger.parse_records(B.replace("<CashTransactions />", ""))
    assert any("CashTransactions" in p for p in st.problems)


def test_change_in_nav_compares_numbers_not_strings():
    # NAV 写 28301.0，Change in NAV 写 28301.00：数值相同
    (st,) = ledger.parse_records(A)
    assert st.problems == []
    (st,) = ledger.parse_records(A.replace('endingValue="28301.00"', 'endingValue="28301.01"'))
    assert any("期末值" in p for p in st.problems)
    (st,) = ledger.parse_records(A.replace('startingValue="10283.892372948"', 'startingValue="10283.892372949"'))
    assert any("期初值" in p for p in st.problems)


@pytest.mark.parametrize("change, expected", [
    (('linkingAdjustments="0"', 'linkingAdjustments="5"'), "linkingAdjustments"),
    (('depositsWithdrawals="18010"', 'depositsWithdrawals="18010.02"'), "不一致"),
    (('whenGenerated="20261010;091852"', ''), "whenGenerated"),
    (('currency="USD" reportDate="20260930"', 'currency="HKD" reportDate="20260930"'), "本位币"),
    (('transactionID="9000000003"', 'transactionID=""'), "transactionID"),
    (("</Transfers>", '<Transfer levelOfDetail="TRANSFER" /></Transfers>'), "Select All"),
])
def test_incomplete_statements_are_flagged(change, expected):
    (st,) = ledger.parse_records(A.replace(*change))
    assert any(expected in p for p in st.problems), st.problems


def test_flow_total_within_a_cent_passes():
    (st,) = ledger.parse_records(A.replace('depositsWithdrawals="18010"', 'depositsWithdrawals="18010.01"'))
    assert st.problems == []


def test_position_transfer_uses_market_value_not_quantity():
    xml = A.replace("</Transfers>", (
        '<Transfer accountId="U0000038" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="AAPL" '
        'reportDate="20261009" dateTime="20261009" type="ACATS" direction="IN" quantity="10" positionAmount="2300" '
        'positionAmountInBase="2300" cashTransfer="0" transactionID="9000000007" levelOfDetail="TRANSFER" /></Transfers>'))
    xml = xml.replace('assetTransfers="0"', 'assetTransfers="2300"')
    (st,) = ledger.parse_records(xml)
    assert st.problems == []
    f = next(f for f in st.flows if f["transaction_id"] == "9000000007")
    assert (f["kind"], f["base_amount"], f["quantity"], f["symbol"]) == ("position", "2300", "10", "AAPL")


def test_cash_transfer_never_falls_back_to_quantity():
    from app.parser import parse_flex_xml
    xml = A.replace('cashTransfer="-12000"', 'cashTransfer=""').replace(
        'quantity="0" transferPrice="0" positionAmount="0" positionAmountInBase="0" pnlAmount="0" pnlAmountInBase="0" cashTransfer=""',
        'quantity="500" transferPrice="0" positionAmount="0" positionAmountInBase="0" pnlAmount="0" pnlAmountInBase="0" cashTransfer=""')
    (st,) = parse_flex_xml(xml)
    assert all(abs(a) != 500 for _, a, *_ in st.flows)


def _revise(text, generated, **subs):
    text = text.replace('whenGenerated="20261010;091852"', f'whenGenerated="{generated}"')
    for old, new in subs.values():
        text = text.replace(old, new)
    return text


def test_newer_statement_replaces_older_within_its_range():
    _import(A)
    # 第二天的报表：10-08 的 NAV 被修订；30000 的入金改为 30100；10 美元那笔被删除
    newer = _revise(A, "20261011;091852",
                    nav=('reportDate="20261008" cash="28290.676252135" stock="0" total="28290.676252135"',
                         'reportDate="20261008" cash="28290.676252135" stock="0" total="28390.676252135"'),
                    amount=('amount="30000"', 'amount="30100"'),
                    cin=('depositsWithdrawals="18010"', 'depositsWithdrawals="18100"'))
    newer = newer.replace(newer[newer.index('<CashTransaction accountId="U0000038" acctAlias="" model="" currency="USD" fxRateToBase="1" assetCategory="" symbol="" description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" dateTime="20261006;200801"'):].split("\n", 1)[0], "")
    _import(newer)
    with db.connect() as conn:
        result = ledger.statement(conn, "U0000038", "2026-09-30")
    nav = {r["date"]: r for r in result["nav"]}
    assert nav["2026-10-08"]["total"] == "28390.676252135"
    flows = {f["transaction_id"]: f for f in result["flows"]}
    assert flows["9000000003"]["amount"] == "30100"
    assert "9000000004" not in flows
    assert nav["2026-10-08"]["statement_id"] == flows["9000000003"]["statement_id"] == result["latest_complete_statement"]["id"]


def test_import_order_does_not_matter():
    newer = _revise(A, "20261011;091852", nav=('total="28301.0"', 'total="28301.5"'),
                    cin=('endingValue="28301.00"', 'endingValue="28301.5"'))
    _import(newer)
    _import(A)  # 旧文件后导入（例如补录历史），不能盖掉新的
    with db.connect() as conn:
        result = ledger.statement(conn, "U0000038", "2026-10-09")
    assert result["nav"] == [{"date": "2026-10-09", "total": "28301.5", "statement_id": 1}]


def test_incomplete_statement_is_skipped_and_reported():
    _import(A)
    broken = _revise(A, "20261011;091852", link=('linkingAdjustments="0"', 'linkingAdjustments="3"'),
                     nav=('total="28301.0"', 'total="99999"'))
    _import(broken)
    with db.connect() as conn:
        result = ledger.statement(conn, "U0000038", "2026-10-01")
    assert result["latest_report_date"] == "2026-10-09"
    assert result["nav"][-1]["total"] == "28301.0"  # 沿用上一个完整版本
    assert result["latest_statement"]["problems"]
    assert result["latest_complete_statement"]["when_generated"] == "2026-10-10T09:18:52"


def test_coverage_merges_adjacent_ranges_and_shows_gaps():
    assert ledger._merge([["2026-10-10", "2026-10-12"], ["2026-10-01", "2026-10-09"], ["2026-10-20", "2026-10-22"]]) == [
        ["2026-10-01", "2026-10-12"], ["2026-10-20", "2026-10-22"]]
    _import(A)
    with db.connect() as conn:
        assert ledger.current(conn, "U0000038")["coverage"] == [["2026-10-01", "2026-10-09"]]


def test_flows_since_includes_history_for_revision_checks():
    _import(A)
    with db.connect() as conn:
        all_flows = ledger.statement(conn, "U0000038", "2026-09-01")["flows"]
        recent = ledger.statement(conn, "U0000038", "2026-10-06")["flows"]
    assert [f["transaction_id"] for f in all_flows] == ["9000000001", "9000000002", "9000000003", "9000000004", "9000000006"]
    assert [f["transaction_id"] for f in recent] == ["9000000004", "9000000006"]


# ---------- 接口和密钥 ----------

@pytest.fixture
def client():
    from app.server import app
    _import(A)
    _import(B)
    with db.connect() as conn:
        lorenzo = keys.add(conn, "lorenzo", ["U0000038", "U0000098"])
        zoey = keys.add(conn, "zoey", ["U0000098"])
    c = TestClient(app)
    c.lorenzo, c.zoey = lorenzo, zoey
    return c


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_api_requires_a_valid_key(client):
    assert client.get("/api/ledger/accounts").status_code == 401
    assert client.get("/api/ledger/accounts", headers=_auth("wrong")).status_code == 401
    r = client.get("/api/ledger/statement", params={"account": "U0000098", "since": "2026-10-01"})
    assert r.status_code == 401


def test_key_only_reads_its_accounts(client):
    r = client.get("/api/ledger/accounts", headers=_auth(client.zoey))
    assert [a["id"] for a in r.json()["accounts"]] == ["U0000098"]
    assert r.json()["accounts"][0]["latest_report_date"] == "2026-10-09"
    r = client.get("/api/ledger/statement", params={"account": "U0000038", "since": "2026-10-01"},
                   headers=_auth(client.zoey))
    assert r.status_code == 403
    r = client.get("/api/ledger/statement", params={"account": "U0000098", "since": "2026-10-01"},
                   headers=_auth(client.zoey))
    assert r.status_code == 200
    body = r.json()
    assert body["latest_report_date"] == "2026-10-09"
    assert body["nav"][-1] == {"date": "2026-10-09", "total": "22360.03456131", "statement_id": 2}
    assert [f["transaction_id"] for f in body["flows"]] == ["9000000002", "9000000006"]
    assert set(body["last_fetch"] or {}) <= {"at", "ok"}  # 不带拉取日志的文字（里面有其他账户）


def test_lorenzo_reads_both(client):
    r = client.get("/api/ledger/accounts", headers=_auth(client.lorenzo))
    assert [a["id"] for a in r.json()["accounts"]] == ["U0000038", "U0000098"]
    r = client.get("/api/ledger/statement", params={"account": "U0000038", "since": "2026-10-01"},
                   headers=_auth(client.lorenzo))
    assert r.json()["coverage"] == [["2026-10-01", "2026-10-09"]]


def test_revoked_key_stops_working(client):
    with db.connect() as conn:
        assert keys.revoke(conn, "zoey")
    assert client.get("/api/ledger/accounts", headers=_auth(client.zoey)).status_code == 401
    with db.connect() as conn:
        new = keys.add(conn, "zoey", ["U0000098"])
    assert client.get("/api/ledger/accounts", headers=_auth(new)).status_code == 200


def test_since_must_be_a_date(client):
    r = client.get("/api/ledger/statement", params={"account": "U0000098", "since": "yesterday"},
                   headers=_auth(client.zoey))
    assert r.status_code == 422


def test_unknown_account_returns_no_data(client):
    with db.connect() as conn:
        keys.revoke(conn, "zoey")
        key = keys.add(conn, "zoey", ["U0000098", "U0000077"])
    r = client.get("/api/ledger/statement", params={"account": "U0000077", "since": "2026-10-01"}, headers=_auth(key))
    assert r.status_code == 200
    assert r.json()["latest_report_date"] is None and r.json()["nav"] == []


def _drop(text, attr, txids=None):
    """Remove an attribute from the rows with these transactionIDs (all rows when None)."""
    import re
    out = []
    for line in text.split("\n"):
        if txids is None or any(f'transactionID="{t}"' in line for t in txids):
            line = re.sub(rf'\s{attr}="[^"]*"', "", line)
        out.append(line)
    return "\n".join(out)


CNH = ("9000000001", "9000000002")  # 70000 CNH 入金，当天转去另一个账户：两笔方向相反


@pytest.mark.parametrize("attr, txids, expected", [
    ("fxRateToBase", CNH, "fxRateToBase"),
    ("amount", ("9000000001",), "amount"),
    ("cashTransfer", ("9000000002",), "cashTransfer"),
    ("reportDate", CNH, "reportDate"),
    ("currency", ("9000000001",), "currency"),
])
def test_flow_missing_a_booked_field_makes_the_statement_incomplete(attr, txids, expected):
    broken = _drop(A, attr, txids)
    (st,) = ledger.parse_records(broken)
    assert any(expected in p for p in st.problems), st.problems
    # 作为新版导入：不被选为完整报表，接口沿用旧版的金额和日期
    _import(A)
    _import(_revise(broken, "20261011;091852"))
    with db.connect() as conn:
        result = ledger.statement(conn, "U0000038", "2026-09-30")
    flows = {f["transaction_id"]: f for f in result["flows"]}
    assert Decimal(flows["9000000001"]["base_amount"]) == Decimal("10346.70")
    assert flows["9000000001"]["report_date"] == "2026-10-01"
    assert "9000000002" in flows
    assert result["latest_statement"]["problems"]
    assert result["latest_complete_statement"]["when_generated"] == "2026-10-10T09:18:52"


def test_usd_row_without_rate_is_one():
    (st,) = ledger.parse_records(_drop(A, "fxRateToBase", ("9000000003",)))
    assert st.problems == []
    assert next(f for f in st.flows if f["transaction_id"] == "9000000003")["fx_rate"] == "1"


def test_unreadable_number_is_reported_not_raised():
    (st,) = ledger.parse_records(A.replace('amount="30000"', 'amount="abc"'))
    assert any("不是数字" in p for p in st.problems)


@pytest.mark.parametrize("attr", ["startingValue", "endingValue", "linkingAdjustments", "depositsWithdrawals"])
def test_change_in_nav_missing_a_check_field_is_refused(attr):
    (st,) = ledger.parse_records(_drop(A, attr))
    assert any(attr in p for p in st.problems), st.problems


def test_change_in_nav_without_check_fields_cannot_replace_a_good_version():
    _import(A)
    broken = A
    for attr in ("startingValue", "endingValue", "linkingAdjustments"):
        broken = _drop(broken, attr)
    broken = _revise(broken.replace('total="28301.0"', 'total="999999999.99"'), "20261011;091852")
    _import(broken)
    with db.connect() as conn:
        result = ledger.statement(conn, "U0000038", "2026-10-09")
    assert result["nav"][-1]["total"] == "28301.0"
    assert any("endingValue" in p for p in result["latest_statement"]["problems"])
    assert result["latest_complete_statement"]["when_generated"] == "2026-10-10T09:18:52"
