"""FastAPI 服务：提供数据接口 + 托管前端页面。

    python -m app.server
"""
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import calc, db, keys, ledger, trades
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


def _fetched_today(now: datetime) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM fetch_log WHERE ok = 1 AND at >= ? LIMIT 1",
                           (now.strftime("%Y-%m-%d"),)).fetchone()
    return row is not None


def _scheduler():
    """服务运行期间，每天在 AUTO_FETCH_TIME 自动拉取一次。

    启动时（或电脑睡眠醒来后）已经过了当天的时间、当天还没成功拉取过，就立即补拉一次；
    每个进程每天最多自动尝试一次，失败了可以在网页上手动刷新。"""
    last_run: date | None = None
    while True:
        now = datetime.now()
        if (now.strftime("%H:%M") >= settings.auto_fetch_time and last_run != now.date()
                and not _fetched_today(now)):
            last_run = now.date()
            _do_fetch()
        time.sleep(20)


@asynccontextmanager
async def lifespan(_app):
    if settings.auto_fetch_time:
        threading.Thread(target=_scheduler, daemon=True).start()
    yield


app = FastAPI(title="IBKR 收益日历", lifespan=lifespan)


@app.middleware("http")
async def ledger_keys_only_reach_ledger(request, call_next):
    """带记账密钥的请求只能访问 /api/ledger/*。网页接口不验证身份（只给本机浏览器用），
    记账软件的地址如果被填成网页接口，不能借这把密钥读到别的账户。浏览器访问网页不带这个头。"""
    path = request.url.path
    if request.headers.get("authorization") and not path.startswith("/api/ledger/"):
        return JSONResponse({"detail": "记账密钥只能访问 /api/ledger/ 接口"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    """页面文件每次都向服务器校验，更新代码后刷新即可生效（未修改时返回 304，开销很小）。"""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


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
        trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return {
        "display_currency": settings.display_currency,
        "accounts": accounts,
        "first_date": rng["lo"],
        "last_date": rng["hi"],
        "last_fetch": dict(last) if last else None,
        "fetching": _fetch_state["running"],
        "configured": bool(settings.token and settings.query_ids),
        "demo": settings.demo_mode,
        "has_trades": trade_count > 0,
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


def _load_trades(accounts_param: str | None, start: str | None, end: str | None):
    selected = [a for a in (accounts_param or "").split(",") if a] or None
    with db.connect() as conn:
        base_ccy = {r["account_id"]: r["base_currency"] for r in conn.execute("SELECT * FROM accounts")}
        fx_rows = db.load_all(conn)[3]
        rows = db.load_trades(conn, selected, start, end)
    return rows, calc.FxConverter(fx_rows, settings.display_currency), base_ccy


@app.get("/api/orders")
def orders(accounts: str | None = None, start: str | None = None, end: str | None = None,
           q: str = "", side: str = "", asset: str = "", offset: int = 0, limit: int = 50):
    rows, fx, base_ccy = _load_trades(accounts, start, end)
    result = trades.build_orders(rows, fx, base_ccy)
    if q:
        ql = q.lower()
        result = [o for o in result if ql in f"{o['name']} {o['code']}".lower()]
    if side in ("BUY", "SELL"):
        result = [o for o in result if o["side"] == side]
    if asset == "STK":
        result = [o for o in result if o["asset"] == "STK"]
    elif asset == "OPT":
        result = [o for o in result if o["asset"] in ("OPT", "FOP")]
    elif asset == "OTHER":
        result = [o for o in result if o["asset"] not in ("STK", "OPT", "FOP")]
    return {"total": len(result), "orders": result[offset:offset + limit]}


@app.get("/api/ranking")
def ranking(accounts: str | None = None, start: str | None = None, end: str | None = None):
    rows, fx, base_ccy = _load_trades(accounts, start, end)
    items = trades.build_ranking(rows, fx, base_ccy)
    return {
        "items": items,
        "total": sum(i["pnl"] for i in items),
        "first_trade": rows[0]["trade_date"] if rows else None,
        "last_trade": rows[-1]["trade_date"] if rows else None,
    }


def _allowed(authorization: str | None) -> list[str]:
    """记账接口的鉴权：Authorization: Bearer <密钥>。没有或无效返回 401。"""
    key = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else None
    with db.connect() as conn:
        allowed = keys.accounts_for(conn, key)
    if allowed is None:
        raise HTTPException(401, "需要有效的密钥", headers={"WWW-Authenticate": "Bearer"})
    return allowed


def _last_fetch() -> dict | None:
    # 只给时间和成功与否：拉取日志的文字里有其他账户的信息
    with db.connect() as conn:
        last = conn.execute("SELECT at, ok FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    return {"at": last["at"], "ok": bool(last["ok"])} if last else None


@app.get("/api/ledger/accounts")
def ledger_accounts(authorization: str | None = Header(None)):
    allowed = _allowed(authorization)
    out = []
    with db.connect() as conn:
        for acct in allowed:
            cur = ledger.current(conn, acct)
            out.append({"id": acct, "base_currency": cur["base_currency"],
                        "latest_report_date": max(cur["nav"]) if cur["nav"] else None})
    return {"accounts": out, "last_fetch": _last_fetch(), "fetching": _fetch_state["running"]}


@app.get("/api/ledger/statement")
def ledger_statement(account: str, since: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                     authorization: str | None = Header(None)):
    """since（含）之后的每日 NAV 和资金流水，金额都是原始十进制文本。"""
    if account not in _allowed(authorization):
        raise HTTPException(403, "这把密钥不能读这个账户")
    with db.connect() as conn:
        result = ledger.statement(conn, account, since)
    return result | {"last_fetch": _last_fetch(), "fetching": _fetch_state["running"]}


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
