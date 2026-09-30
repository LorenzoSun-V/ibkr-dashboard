"""盈亏与收益率计算。

单账户、单日：
    pnl_t   = NAV_t − NAV_{t−1} − 净流入_t          （净流入 = 入金/转入 − 出金/转出）
    denom_t = NAV_{t−1} + max(净流入_t, 0)          （入金视为当日开盘前到账）
    r_t     = pnl_t / denom_t
多账户合并：先把 pnl、denom 换算成展示币种后分别相加，再相除。
区间收益率：时间加权 TWR = Π(1 + r_t) − 1
"""
import bisect
from collections import defaultdict
from datetime import date


class FxConverter:
    def __init__(self, fx_rows, display: str):
        self.display = display
        self.table: dict[tuple[str, str], tuple[list[date], list[float]]] = {}
        grouped = defaultdict(list)
        for d, f, t, r in fx_rows:
            grouped[(f, t)].append((d, r))
        for k, rows in grouped.items():
            rows.sort()
            self.table[k] = ([d for d, _ in rows], [r for _, r in rows])
        self.missing: set[str] = set()

    def _lookup(self, key, d):
        if key not in self.table:
            return None
        dates, rates = self.table[key]
        i = bisect.bisect_right(dates, d) - 1
        return rates[max(i, 0)]

    def factor(self, ccy: str | None, d: date) -> float:
        """ccy -> 展示币种 的乘数。"""
        if not ccy or ccy == self.display:
            return 1.0
        r = self._lookup((ccy, self.display), d)
        if r:
            return r
        r = self._lookup((self.display, ccy), d)
        if r:
            return 1.0 / r
        self.missing.add(ccy)
        return 1.0


def account_daily(nav: dict[date, float], flows: list[tuple[date, float]]):
    """返回单账户每日明细 [{date, nav, flow, pnl, denom}]（基础货币）。"""
    dates = sorted(nav)
    flow_dates = [f[0] for f in flows]
    rows = []
    for prev, cur in zip(dates, dates[1:]):
        lo = bisect.bisect_right(flow_dates, prev)
        hi = bisect.bisect_right(flow_dates, cur)
        flow = sum(a for _, a in flows[lo:hi])
        prev_nav, cur_nav = nav[prev], nav[cur]
        rows.append({
            "date": cur,
            "nav": cur_nav,
            "flow": flow,
            "pnl": cur_nav - prev_nav - flow,
            "denom": prev_nav + max(flow, 0.0),
        })
    return rows


def combined_daily(accounts, nav, flows, fx: FxConverter, selected: list[str] | None):
    """多账户合并后的每日数据，按日期升序。"""
    ids = [a for a in (selected or accounts.keys()) if a in nav]
    by_date: dict[date, dict] = {}
    for acct in ids:
        ccy = accounts.get(acct)
        for row in account_daily(nav[acct], flows.get(acct, [])):
            k = fx.factor(ccy, row["date"])
            agg = by_date.setdefault(row["date"], {"pnl": 0.0, "denom": 0.0, "flow": 0.0, "accounts": {}})
            pnl, denom = row["pnl"] * k, row["denom"] * k
            agg["pnl"] += pnl
            agg["denom"] += denom
            agg["flow"] += row["flow"] * k
            agg["accounts"][acct] = {"pnl": pnl, "ret": pnl / denom if denom > 0 else None,
                                     "nav": row["nav"] * k}

    # 合并 NAV：对缺失日期的账户使用最近一次 NAV（前向填充）
    nav_sorted = {a: (sorted(nav[a]), [nav[a][d] for d in sorted(nav[a])]) for a in ids}
    result = []
    for d in sorted(by_date):
        agg = by_date[d]
        total_nav = 0.0
        for a in ids:
            ds, vs = nav_sorted[a]
            i = bisect.bisect_right(ds, d) - 1
            if i >= 0:
                total_nav += vs[i] * fx.factor(accounts.get(a), d)
        result.append({
            "date": d.isoformat(),
            "pnl": agg["pnl"],
            "ret": agg["pnl"] / agg["denom"] if agg["denom"] > 0 else None,
            "flow": agg["flow"],
            "nav": total_nav,
            "accounts": agg["accounts"],
        })
    return result


def summarize(rows):
    """对一段区间的每日数据做汇总。"""
    if not rows:
        return None
    growth = 1.0
    for r in rows:
        if r["ret"] is not None:
            growth *= 1 + r["ret"]
    pnls = [r["pnl"] for r in rows]
    best = max(rows, key=lambda r: r["pnl"])
    worst = min(rows, key=lambda r: r["pnl"])
    return {
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "pnl": sum(pnls),
        "twr": growth - 1,
        "days": len(rows),
        "win_days": sum(1 for p in pnls if p > 0),
        "loss_days": sum(1 for p in pnls if p < 0),
        "best": {"date": best["date"], "pnl": best["pnl"]},
        "worst": {"date": worst["date"], "pnl": worst["pnl"]},
        "net_flow": sum(r["flow"] for r in rows),
        "end_nav": rows[-1]["nav"],
    }


def group_by(rows, key_len: int):
    """按月(key_len=7, YYYY-MM)或按年(key_len=4)聚合。"""
    groups = defaultdict(list)
    for r in rows:
        groups[r["date"][:key_len]].append(r)
    out = []
    for k in sorted(groups):
        s = summarize(groups[k])
        out.append({"period": k, "pnl": s["pnl"], "ret": s["twr"], "days": s["days"]})
    return out
