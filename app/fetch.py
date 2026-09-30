"""拉取 / 导入数据。

    python -m app.fetch                  # 通过 Flex Web Service 拉取最新数据
    python -m app.fetch --file a.xml ... # 导入手动下载的 Flex XML（用于补录更早的历史）
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from . import db
from .config import settings
from .flex_client import fetch_statement
from .parser import parse_flex_xml


def import_xml(text: str, source: str, log=print) -> str:
    statements = parse_flex_xml(text)
    with db.connect() as conn:
        for st in statements:
            db.save_statement(conn, st)
    parts = []
    for st in statements:
        rng = f"{min(st.nav)} ~ {max(st.nav)}" if st.nav else "无 NAV 数据"
        trades = f", {len(st.trades)} 笔成交" if st.trades is not None else ""
        parts.append(f"{st.account_id}: {len(st.nav)} 天 NAV, {len(st.flows)} 笔出入金/转仓{trades} ({rng})")
        for w in st.warnings:
            log(f"⚠️  {st.account_id}: {w}")
        if st.expected_flow is not None and st.nav:
            lo, hi = st.from_date or min(st.nav), st.to_date or max(st.nav)
            got = sum(a for d, a, *_ in st.flows if lo <= d <= hi)
            if abs(got - st.expected_flow) > max(1.0, abs(st.expected_flow) * 0.01):
                log(f"⚠️  {st.account_id}: 出入金对账不一致 —— IBKR Change in NAV 显示 {st.expected_flow:,.2f}，"
                    f"明细合计 {got:,.2f}。请检查 Cash Transactions / Transfers 的配置")
            else:
                log(f"✅ {st.account_id}: 出入金对账一致（{got:,.2f}）")
        if not st.nav:
            log(f"⚠️  {st.account_id} 没有 NAV 数据：该 Query 的期间为 {st.period or '?'}，"
                f"包含的 section 为 {', '.join(st.sections) or '无'}。\n"
                f"    请在 Flex Query 中勾选 'Net Asset Value (NAV) in Base'，并把 Period 设为 'Last 365 Calendar Days'")
    msg = f"{source} -> " + "; ".join(parts)
    log(msg)
    return msg


def run_fetch(log=print) -> tuple[bool, str]:
    messages = []
    ok = True
    if not settings.query_ids:
        log("❌ IBKR_FLEX_QUERY_ID 未配置，请检查 .env")
        return False, "IBKR_FLEX_QUERY_ID 未配置"
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    for i, qid in enumerate(settings.query_ids):
        if i:
            time.sleep(5)  # 多个 Query 之间稍作间隔，避免触发 IBKR 限流
        try:
            xml = fetch_statement(settings.token, qid, log=log)
            raw = settings.raw_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{qid}.xml"
            raw.write_text(xml, encoding="utf-8")
            messages.append(import_xml(xml, f"query {qid}", log=log))
        except Exception as e:  # noqa: BLE001
            e = str(e).replace(settings.token, "***") if settings.token else e
            ok = False
            messages.append(f"query {qid} 失败: {e}")
            log(f"❌ query {qid} 失败: {e}")
    msg = " | ".join(messages)
    with db.connect() as conn:
        db.log_fetch(conn, ok, msg)
    return ok, msg


def main():
    ap = argparse.ArgumentParser(description="拉取/导入 IBKR Flex 数据")
    ap.add_argument("--file", nargs="+", type=Path, help="导入本地 Flex XML 文件")
    args = ap.parse_args()

    if args.file:
        for p in args.file:
            import_xml(p.read_text(encoding="utf-8"), p.name)
        return
    ok, _ = run_fetch()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
