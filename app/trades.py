"""订单与已实现盈亏排行。

- 订单：同一账户下 ibOrderID 相同的逐笔成交合并为一个订单（数量求和、按数量加权的成交均价）
- 已实现盈亏：IBKR 的 fifoPnlRealized（FIFO 口径，已扣除佣金），按合约（conid）汇总
金额先用 fxRateToBase 换算到账户基础货币，再换算到展示币种。
"""
from collections import defaultdict
from datetime import date

from .calc import FxConverter

MARKETS = {"USD": "US", "HKD": "HK", "CNH": "CN", "CNY": "CN", "JPY": "JP", "GBP": "UK", "EUR": "EU",
           "CAD": "CA", "AUD": "AU", "SGD": "SG"}


def instrument(t: dict) -> dict:
    """展示用的名称：股票为「公司名 / US TSLA」，期权为「TSLA Call / US 270617 400」。"""
    market = MARKETS.get(t["currency"], t["currency"])
    if t["asset"] in ("OPT", "FOP") and t["put_call"]:
        strike = f"{t['strike']:g}"
        expiry = t["expiry"].replace("-", "")[2:] if t["expiry"] else ""
        name = f"{t['underlying'] or t['symbol'].split()[0]} {'Call' if t['put_call'] == 'C' else 'Put'}"
        code = f"{expiry} {strike}".strip()
    else:
        name = t["description"] or t["symbol"]
        code = t["symbol"]
    return {"name": name, "code": code, "market": market, "asset": t["asset"]}


def _to_display(t: dict, amount: float, fx: FxConverter, base_ccy: dict[str, str]) -> float:
    return amount * (t["fx"] or 1.0) * fx.factor(base_ccy.get(t["account_id"]), date.fromisoformat(t["trade_date"]))


def build_orders(trades: list[dict], fx: FxConverter, base_ccy: dict[str, str]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["account_id"], t["order_id"], t["conid"], t["side"])].append(t)

    orders = []
    for (acct, order_id, _, side), fills in groups.items():
        fills.sort(key=lambda t: (t["trade_date"], t["time"] or ""))
        first = fills[0]
        qty = sum(t["quantity"] for t in fills)
        avg = sum(t["quantity"] * t["price"] for t in fills) / qty if qty else 0.0
        orders.append({
            "id": f"{acct}:{order_id}:{first['conid']}:{side}",
            "account_id": acct,
            **instrument(first),
            "side": side,
            "open_close": first["open_close"],
            "quantity": qty,
            "avg_price": avg,
            "currency": first["currency"],
            "amount": sum(abs(t["proceeds"]) for t in fills),
            "commission": sum(t["commission"] for t in fills),
            "realized": sum(_to_display(t, t["realized"], fx, base_ccy) for t in fills),
            "multiplier": first["multiplier"],
            "date": first["trade_date"],
            "time": first["time"],
            "last_date": fills[-1]["trade_date"],
            "fills": len(fills),
            "order_type": first["order_type"],
            "exchange": first["exchange"],
        })
    orders.sort(key=lambda o: (o["date"], o["time"] or ""), reverse=True)
    return orders


def build_ranking(trades: list[dict], fx: FxConverter, base_ccy: dict[str, str]) -> list[dict]:
    groups: dict[str, dict] = {}
    for t in trades:
        if not t["realized"]:
            continue
        g = groups.get(t["conid"])
        if g is None:
            g = groups[t["conid"]] = {"key": t["conid"], **instrument(t), "pnl": 0.0, "trades": 0}
        g["pnl"] += _to_display(t, t["realized"], fx, base_ccy)
        g["trades"] += 1
    return sorted(groups.values(), key=lambda g: g["pnl"], reverse=True)
