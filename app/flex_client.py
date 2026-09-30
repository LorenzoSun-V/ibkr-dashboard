"""IBKR Flex Web Service (v3) 客户端。

流程：SendRequest(token, queryId) -> ReferenceCode -> GetStatement(token, ReferenceCode) -> XML
"""
import time
import xml.etree.ElementTree as ET

import requests

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
# IBKR 会拒绝没有 User-Agent 的请求
HEADERS = {"User-Agent": "ibkr-dashboard/1.0 (Python)"}

# 报表还在生成中，稍后重试
RETRYABLE_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}
# 请求太频繁
THROTTLED_CODES = {"1018"}


class FlexError(Exception):
    pass


def _get(url: str, params: dict, timeout: int, log, attempts: int = 5) -> str:
    """带重试的 GET：网络抖动 / 代理断开 / SSL EOF 时自动重试。错误信息中不包含 token。"""
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            reason = type(e.__context__ or e).__name__ if isinstance(e, requests.ConnectionError) else type(e).__name__
            if i == attempts - 1:
                raise FlexError(f"连接 IBKR 失败（{reason}），已重试 {attempts} 次。请检查网络或代理后重试") from None
            wait = 5 * 2 ** i
            log(f"连接 IBKR 出错（{reason}），{wait}s 后重试 ({i + 1}/{attempts - 1})")
            time.sleep(wait)


def _parse_status(text: str) -> tuple[str, dict]:
    root = ET.fromstring(text)
    info = {child.tag: (child.text or "").strip() for child in root}
    return info.get("Status", ""), info


def _describe(info: dict) -> str:
    return f"[{info.get('ErrorCode', '?')}] {info.get('ErrorMessage', '未知错误')}"


def fetch_statement(token: str, query_id: str, max_wait: int = 300, log=print) -> str:
    """执行一次 Flex Query，返回完整 XML 文本。"""
    if not token or not query_id:
        raise FlexError("IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID 未配置，请检查 .env")

    # 1) SendRequest
    for attempt in range(5):
        text = _get(SEND_URL, {"t": token, "q": query_id, "v": "3"}, 60, log)
        status, info = _parse_status(text)
        if status == "Success":
            break
        if info.get("ErrorCode") in THROTTLED_CODES | RETRYABLE_CODES:
            wait = 10 * (attempt + 1)
            log(f"SendRequest 暂不可用 {_describe(info)}，{wait}s 后重试")
            time.sleep(wait)
            continue
        raise FlexError(f"SendRequest 失败 {_describe(info)}")
    else:
        raise FlexError(f"SendRequest 多次重试仍失败 {_describe(info)}")

    ref_code = info["ReferenceCode"]
    get_url = info.get("Url") or SEND_URL.replace("SendRequest", "GetStatement")
    log(f"Query {query_id} 已提交，ReferenceCode={ref_code}，等待报表生成…")

    # 2) GetStatement（轮询直到报表生成完毕）
    deadline = time.time() + max_wait
    delay = 5
    while True:
        time.sleep(delay)
        text = _get(get_url, {"t": token, "q": ref_code, "v": "3"}, 120, log)
        if "<FlexQueryResponse" in text[:500]:
            return text
        status, info = _parse_status(text)
        code = info.get("ErrorCode")
        if code in RETRYABLE_CODES | THROTTLED_CODES and time.time() < deadline:
            log(f"报表生成中 {_describe(info)}…")
            delay = min(delay + 5, 30)
            continue
        raise FlexError(f"GetStatement 失败 {_describe(info)}")
