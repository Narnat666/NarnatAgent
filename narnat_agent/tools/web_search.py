"""WebSearch工具 —— AnySearch 为主，Open WebSearch 为备

搜索链:
  1. AnySearch API → GitHub/技术文档/官方文档 (AI意图路由)
  2. 失败则降级到 Open WebSearch (Bing + Baidu 并行)

依赖:
  - AnySearch API: https://api.anysearch.com/mcp (免费，无需Key)
  - open-websearch (npm包)，需先启动 daemon:
    $env:MODE="http"; open-websearch serve

纯标准库调用(urllib)，零Python依赖。
"""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
from urllib.parse import urlparse


# ── 常量 ──

_DAEMON_URL = "http://127.0.0.1:3210"
_ANYSEARCH_URL = "https://api.anysearch.com/mcp"
_ANYSEARCH_API_KEY = ""
_TIMEOUT = 10
_ANYSEARCH_TIMEOUT = 15


# ── 工具函数 ──

def _url_key(url: str) -> str:
    """URL去重键：域名+路径，忽略查询参数和片段"""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def _extract_query_words(query: str) -> set:
    """提取查询词用于相关性排序"""
    words = set()
    for w in re.findall(r"[a-zA-Z0-9]{2,}", query):
        words.add(w.lower())
    cn_chars = re.findall(r"[\u4e00-\u9fff]+", query)
    for seg in cn_chars:
        for i in range(len(seg) - 1):
            words.add(seg[i:i + 2])
        if len(seg) == 1:
            words.add(seg)
    return words


def _relevance_score(result: Dict, query: str) -> float:
    """按标题和摘要中的查询词命中数打分"""
    title = result.get("title", "").lower()
    desc = result.get("description", "").lower()
    q_words = _extract_query_words(query)
    if not q_words:
        return 0.5
    title_hits = sum(1 for w in q_words if w in title)
    desc_hits = sum(1 for w in q_words if w in desc)
    return (title_hits * 3 + desc_hits) / (len(q_words) * 4)


def _format_results(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本"""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        desc = r.get("description", "")
        if desc:
            lines.append(f"   {desc}")
    return "\n".join(lines)


# ── AnySearch 调用 ──

def _parse_anysearch_markdown(text: str) -> List[Dict]:
    """解析 AnySearch 返回的 markdown 文本为结构化结果"""
    results = []
    # 格式: ### N. Title\n- **URL**: url\n- description\n...
    blocks = re.split(r"\n(?=### \d+\.)", text)
    for block in blocks:
        title_m = re.search(r"^### \d+\.\s*(.+)$", block, re.MULTILINE)
        url_m = re.search(r"-\s*\*\*URL\*\*:\s*(https?://[^\s\n]+)", block)
        if title_m and url_m:
            title = title_m.group(1).strip()
            url = url_m.group(1).strip()
            # 提取描述：URL之后的文本，去掉前缀标记
            desc_start = url_m.end()
            rest = block[desc_start:].strip()
            # 去掉可能的 "**Description**: " 等前缀
            desc = re.sub(r"^[-*]+\s*(\*\*Description\*\*:\s*)?", "", rest).strip()
            results.append({"title": title, "url": url, "description": desc})
    return results


def _search_anysearch(query: str, max_results: int) -> List[Dict]:
    """调用 AnySearch API"""
    data = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {
            "query": query, "max_results": max_results, "zone": "cn"
        }}
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _ANYSEARCH_API_KEY:
        headers["X-API-Key"] = _ANYSEARCH_API_KEY
    req = urllib.request.Request(_ANYSEARCH_URL, data=data, headers=headers)
    resp = urllib.request.urlopen(req, timeout=_ANYSEARCH_TIMEOUT)
    raw = json.loads(resp.read())
    # 提取所有 text 内容块
    texts = []
    for item in raw.get("result", {}).get("content", []):
        if isinstance(item, dict) and "text" in item:
            texts.append(item["text"])
    combined = "\n".join(texts)
    return _parse_anysearch_markdown(combined)


# ── Open WebSearch 调用（备用） ──

def _search_ows(query: str, engine: str, max_results: int) -> List[Dict]:
    """调用 Open WebSearch daemon 搜索单个引擎"""
    data = json.dumps({
        "query": query,
        "maxResults": max_results,
        "engine": engine,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_DAEMON_URL}/search", data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    result = json.loads(resp.read())
    raw = result.get("data", {}).get("results", [])
    # 统一字段名：OWS用description
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "description": r.get("description", "")} for r in raw]


def _check_daemon() -> bool:
    """检查 Open WebSearch daemon 是否可用"""
    try:
        req = urllib.request.Request(f"{_DAEMON_URL}/health")
        resp = urllib.request.urlopen(req, timeout=3)
        return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


# ── 主入口 ──

def execute(query: str, num: int = 5, _tool_context=None) -> str:
    """
    联网搜索。主引擎 AnySearch，失败则降级到 Open WebSearch。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认5
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        格式化的搜索结果，每条含标题、URL、描述
    """
    # 从tool_context注入API Key
    global _ANYSEARCH_API_KEY
    if _tool_context and _tool_context.api_keys:
        _ANYSEARCH_API_KEY = _tool_context.api_keys.get("anysearch", _ANYSEARCH_API_KEY)

    results: List[Dict] = []

    # ── Tier 1: AnySearch ──
    if _ANYSEARCH_API_KEY:
        try:
            results = _search_anysearch(query, num)
            if results:
                results.sort(key=lambda r: _relevance_score(r, query), reverse=True)
                return _format_results(results[:num])
        except Exception:
            pass

    # ── Tier 2: Open WebSearch (Bing + Baidu 并行) ──
    if not _check_daemon():
        return ("搜索失败: AnySearch 不可用，且 Open WebSearch daemon 未运行。\n"
                "请先启动: $env:MODE='http'; open-websearch serve")

    all_results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_search_ows, query, "bing", num): "bing",
            pool.submit(_search_ows, query, "baidu", num): "baidu",
        }
        for fut in as_completed(futures):
            try:
                all_results.extend(fut.result())
            except Exception:
                pass

    if not all_results:
        return "(无搜索结果)"

    # 去重
    seen: set = set()
    unique: List[Dict] = []
    for r in all_results:
        key = _url_key(r.get("url", ""))
        if key and key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda r: _relevance_score(r, query), reverse=True)
    return _format_results(unique[:num])
