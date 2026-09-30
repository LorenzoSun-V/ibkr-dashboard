"""SQLite 存储。每次导入按 (账户, 日期区间) 先删后写，IBKR 修正历史数据时会被自动覆盖。"""
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from .config import settings
from .parser import AccountStatement

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    base_currency TEXT
);
CREATE TABLE IF NOT EXISTS nav (
    account_id TEXT NOT NULL,
    date       TEXT NOT NULL,
    nav        REAL NOT NULL,
    PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS flows (
    account_id  TEXT NOT NULL,
    date        TEXT NOT NULL,
    amount      REAL NOT NULL,
    kind        TEXT NOT NULL,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_flows ON flows (account_id, date);
CREATE TABLE IF NOT EXISTS fx (
    date     TEXT NOT NULL,
    from_ccy TEXT NOT NULL,
    to_ccy   TEXT NOT NULL,
    rate     REAL NOT NULL,
    PRIMARY KEY (date, from_ccy, to_ccy)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    message TEXT
);
"""


@contextmanager
def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_statement(conn: sqlite3.Connection, st: AccountStatement) -> None:
    if not st.account_id:
        return
    conn.execute(
        "INSERT INTO accounts (account_id, base_currency) VALUES (?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET base_currency = COALESCE(excluded.base_currency, base_currency)",
        (st.account_id, st.base_currency),
    )

    dates = [d for d in [st.from_date, st.to_date, *st.nav, *(f[0] for f in st.flows)] if d]
    if not dates:
        return
    lo, hi = min(dates).isoformat(), max(dates).isoformat()

    conn.execute("DELETE FROM nav WHERE account_id = ? AND date BETWEEN ? AND ?", (st.account_id, lo, hi))
    conn.execute("DELETE FROM flows WHERE account_id = ? AND date BETWEEN ? AND ?", (st.account_id, lo, hi))
    conn.executemany(
        "INSERT INTO nav (account_id, date, nav) VALUES (?, ?, ?)",
        [(st.account_id, d.isoformat(), v) for d, v in st.nav.items()],
    )
    conn.executemany(
        "INSERT INTO flows (account_id, date, amount, kind, description) VALUES (?, ?, ?, ?, ?)",
        [(st.account_id, d.isoformat(), amt, kind, desc) for d, amt, kind, desc in st.flows if d],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO fx (date, from_ccy, to_ccy, rate) VALUES (?, ?, ?, ?)",
        [(d.isoformat(), f, t, r) for d, f, t, r in st.fx if r],
    )


def log_fetch(conn: sqlite3.Connection, ok: bool, message: str) -> None:
    conn.execute("INSERT INTO fetch_log (at, ok, message) VALUES (?, ?, ?)",
                 (datetime.now().isoformat(timespec="seconds"), int(ok), message))


def load_all(conn: sqlite3.Connection):
    accounts = {r["account_id"]: r["base_currency"] for r in conn.execute("SELECT * FROM accounts")}
    nav: dict[str, dict[date, float]] = {}
    for r in conn.execute("SELECT account_id, date, nav FROM nav ORDER BY date"):
        nav.setdefault(r["account_id"], {})[date.fromisoformat(r["date"])] = r["nav"]
    flows: dict[str, list[tuple[date, float]]] = {}
    for r in conn.execute("SELECT account_id, date, amount FROM flows ORDER BY date"):
        flows.setdefault(r["account_id"], []).append((date.fromisoformat(r["date"]), r["amount"]))
    fx = [(date.fromisoformat(r["date"]), r["from_ccy"], r["to_ccy"], r["rate"])
          for r in conn.execute("SELECT * FROM fx ORDER BY date")]
    return accounts, nav, flows, fx
