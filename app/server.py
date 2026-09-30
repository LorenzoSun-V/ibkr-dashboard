"""FastAPI 服务：提供数据接口 + 托管前端页面。

    python -m app.server
"""
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import calc, db
from .config import ROOT, settings
from .fetch import run_fetch

_fetch_lock = threading.Lock()
_fetch_state = {"running": False}


def _do_fetch():
    if not _fetch_lock.acquire(blocking=False):
        return
    _fetch_state["running"] = True
    try:
        run_fetch()
    finally:
        _fetch_state["running"] = False
        _fetch_lock.release()


def _scheduler():
    """服务运行期间，每天在 AUTO_FETCH_TIME 自动拉取一次。"""
    last_run: date | None = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == settings.auto_fetch_time and last_run != now.date():
            last_run = now.date()
            _do_fetch()
        time.sleep(20)


@asynccontextmanager
async def lifespan(_app):
    if settings.auto_fetch_time:
        threading.Thread(target=_scheduler, daemon=True).start()
    yield


app = FastAPI(title="IBKR 收益日历", lifespan=lifespan)


def _load(accounts_param: str | None):
    with db.connect() as conn:
        accounts, nav, flows, fx_rows = db.load_all(conn)
    selected = [a for a in (accounts_param or "").split(",") if a] or None
    fx = calc.FxConverter(fx_rows, settings.display_currency)
    rows = calc.combined_daily(accounts, nav, flows, fx, selected)
    return rows, fx


def _filter(rows, start: str | None, end: str | None):
    return [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]


@app.get("/api/meta")
def meta():
    with db.connect() as conn:
        accounts = [
            {"id": r["account_id"], "alias": settings.aliases.get(r["account_id"], ""),
             "currency": r["base_currency"]}
            for r in conn.execute("SELECT * FROM accounts ORDER BY account_id")
        ]
        rng = conn.execute("SELECT MIN(date) lo, MAX(date) hi FROM nav").fetchone()
        last = conn.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "display_currency": settings.display_currency,
        "accounts": accounts,
        "first_date": rng["lo"],
        "last_date": rng["hi"],
        "last_fetch": dict(last) if last else None,
        "fetching": _fetch_state["running"],
        "configured": bool(settings.token and settings.query_ids),
        "demo": settings.demo_mode,
    }


@app.get("/api/daily")
def daily(accounts: str | None = None, start: str | None = None, end: str | None = None):
    rows, fx = _load(accounts)
    return {"rows": _filter(rows, start, end), "fx_missing": sorted(fx.missing)}


@app.get("/api/summary")
def summary(accounts: str | None = None, start: str | None = None, end: str | None = None):
    rows, _ = _load(accounts)
    return calc.summarize(_filter(rows, start, end))


@app.get("/api/periods")
def periods(by: str = Query("month", pattern="^(month|year)$"), accounts: str | None = None,
            start: str | None = None, end: str | None = None):
    rows, _ = _load(accounts)
    return calc.group_by(_filter(rows, start, end), 7 if by == "month" else 4)


@app.post("/api/refresh")
def refresh():
    if not (settings.token and settings.query_ids):
        raise HTTPException(400, "未配置 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID")
    if _fetch_state["running"]:
        return {"started": False, "message": "已经在拉取中"}
    threading.Thread(target=_do_fetch, daemon=True).start()
    return {"started": True}


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


def main():
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
