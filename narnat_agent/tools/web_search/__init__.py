"""WebSearch工具 —— MCP搜索API

依赖:
  - MCP搜索API (默认AnySearch): 需配置api_keys

纯标准库调用(urllib)，零Python依赖。
"""

import json
import re
import urllib.request
from typing import List, Dict


# ── 常量 ──

_DEFAULT_SEARCH_URL = "https://api.anysearch.com/mcp"
_ANYSEARCH_API_KEY = ""
_ANYSEARCH_URL = ""
_ANYSEARCH_TIMEOUT = 15

DEFINITION = {
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "Web search for API docs, solutions, tech articles. Must include Sources links at end of response after searching",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num": {"type": "integer", "description": "Number of results, default 5"},
            },
            "required": ["query"],
        },
    },
}


# ── 工具函数 ──

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
    """调用MCP搜索API"""
    url = _ANYSEARCH_URL or _DEFAULT_SEARCH_URL
    data = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {
            "query": query, "max_results": max_results, "zone": "cn"
        }}
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _ANYSEARCH_API_KEY:
        headers["X-API-Key"] = _ANYSEARCH_API_KEY
    req = urllib.request.Request(url, data=data, headers=headers)
    resp = urllib.request.urlopen(req, timeout=_ANYSEARCH_TIMEOUT)
    raw = json.loads(resp.read())
    # 提取所有 text 内容块
    texts = []
    for item in raw.get("result", {}).get("content", []):
        if isinstance(item, dict) and "text" in item:
            texts.append(item["text"])
    combined = "\n".join(texts)
    return _parse_anysearch_markdown(combined)


# ── 主入口 ──

def execute(query: str, num: int = 5, _tool_context=None) -> str:
    """
    联网搜索。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认5
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        格式化的搜索结果，每条含标题、URL、描述
    """
    # 从tool_context注入API Key和URL
    global _ANYSEARCH_API_KEY, _ANYSEARCH_URL
    if _tool_context and _tool_context.api_keys:
        _ANYSEARCH_API_KEY = _tool_context.api_keys.get("websearch", _ANYSEARCH_API_KEY)
        _ANYSEARCH_URL = _tool_context.api_keys.get("websearch_url", _ANYSEARCH_URL)

    if not _ANYSEARCH_API_KEY:
        return "搜索失败: 未配置 WebSearch API Key"

    try:
        results = _search_anysearch(query, num)
    except Exception as e:
        return f"搜索失败: {e}"

    if not results:
        return "(无搜索结果)"

    results.sort(key=lambda r: _relevance_score(r, query), reverse=True)
    return _format_results(results[:num])
