"""WebSearch工具 —— 联网搜索，搜索引擎降级链"""

import re
import json
from typing import List, Dict
from urllib.parse import quote_plus


def execute(query: str, num: int = 5, lr: str = "") -> str:
    """
    联网搜索。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认5
        lr: 语言限制，如lang_en/lang_zh-CN

    Returns:
        搜索结果列表，每条含标题、URL、摘要
    """
    results = _search_baidu(query, num, lr)
    if not results:
        results = _search_bing(query, num, lr)
    if not results:
        results = _search_duckduckgo(query, num)

    if not results:
        return "(无搜索结果)"

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


def _search_baidu(query: str, num: int, lr: str) -> List[Dict]:
    """百度搜索"""
    try:
        import requests
    except ImportError:
        return []

    try:
        url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn={num}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return _parse_baidu_html(resp.text, num)
    except Exception:
        return []


def _parse_baidu_html(html: str, num: int) -> List[Dict]:
    """解析百度搜索结果HTML"""
    results = []
    # 简单正则提取
    pattern = re.compile(
        r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?href="([^"]+)".*?>(.*?)</a>.*?'
        r'<span[^>]*class="content-right_[^"]*"[^>]*>(.*?)</span>',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if title:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= num:
            break
    return results


def _search_bing(query: str, num: int, lr: str) -> List[Dict]:
    """Bing搜索"""
    try:
        import requests
    except ImportError:
        return []

    try:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={num}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return _parse_bing_html(resp.text, num)
    except Exception:
        return []


def _parse_bing_html(html: str, num: int) -> List[Dict]:
    """解析Bing搜索结果HTML"""
    results = []
    pattern = re.compile(
        r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>.*?'
        r'<p[^>]*>(.*?)</p>',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if title:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= num:
            break
    return results


def _search_duckduckgo(query: str, num: int) -> List[Dict]:
    """DuckDuckGo搜索（无需API key）"""
    try:
        import requests
    except ImportError:
        return []

    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        # RelatedTopics
        for topic in data.get("RelatedTopics", [])[:num]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                })
        return results
    except Exception:
        return []
