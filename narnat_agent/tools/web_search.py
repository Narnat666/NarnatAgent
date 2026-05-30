"""WebSearch工具 —— Bing(HTTP) + 百度(Playwright) 双引擎

降级策略:
1. Bing HTML — 纯HTTP，速度快（~1s），国内直连cn.bing.com
2. Baidu    — Playwright无头浏览器，速度慢（~3s），中文搜索质量更高

Bing 先行快速返回，百度补充更多中文结果。两者合并去重后按相关性排序。

百度必须用浏览器：纯HTTP请求无法绕过验证码（百度JS设置关键cookie）。
Playwright用系统Edge（channel=msedge），非headless+反自动化检测。
"""

import re
import time
from html import unescape as _html_unescape
from typing import List, Dict
from urllib.parse import quote_plus, urlparse


# ── 常量 ──

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_TIMEOUT = 5           # HTTP请求超时秒数
_MAX_RETRIES = 2       # 仅5xx重试
_SNIPPET_LEN = 200     # 摘要最大长度
_BAIDU_TIMEOUT = 15000 # 百度Playwright超时ms

# 预编译正则
_RE_STRIP_TAG = re.compile(r"<[^>]+>")

# ── Bing 正则 ──
_RE_BING_H2 = re.compile(
    r'<h2[^>]*><a[^>]*?href="(https?[^"]+)"[^>]*>(.*?)</a>\s*</h2>',
    re.DOTALL,
)
_RE_BING_CAPTION = re.compile(
    r'<div[^>]+class="b_caption[^"]*"[^>]*>(.*?)</div>', re.DOTALL,
)
_RE_P_TAG = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL)
_BING_OWN_DOMAINS = {"bing.com", "microsoft.com", "msn.com"}

# ── 百度 正则 ──
_RE_BAIDU_H3 = re.compile(
    r'<h3[^>]*>.*?href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL,
)
_RE_BAIDU_C_ABSTRACT = re.compile(
    r'class="c-abstract"[^>]*>(.*?)</(?:span|div)>', re.DOTALL,
)
_RE_BAIDU_CONTENT_RIGHT = re.compile(
    r'<span[^>]+class="content-right_[^"]*"[^>]*>(.*?)</span>', re.DOTALL,
)
_BAIDU_OWN_DOMAINS = {"baidu.com", "baidu.php"}


# ── 工具函数 ──

def _strip_tags(text: str) -> str:
    """去除HTML标签 → 解码HTML实体 → 清理空白"""
    return _html_unescape(_RE_STRIP_TAG.sub("", text)).strip()


def _url_key(url: str) -> str:
    """URL去重键：域名+路径，忽略查询参数和片段"""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def _relevance_score(result: Dict, query: str) -> float:
    """结果与查询的相关性：标题命中查询词越多分越高"""
    title = result.get("title", "").lower()
    snippet = result.get("snippet", "").lower()
    q_words = _extract_query_words(query)
    if not q_words:
        return 0.5
    title_hits = sum(1 for w in q_words if w in title)
    snippet_hits = sum(1 for w in q_words if w in snippet)
    return (title_hits * 3 + snippet_hits) / (len(q_words) * 4)


def _extract_query_words(query: str) -> set:
    """
    提取查询词用于相关性判断。
    英文按空格拆分，中文按2字滑窗拆分（"红米K80" → "红米","米k","k8","80"）。
    """
    words = set()
    # 英文词
    for w in re.findall(r"[a-zA-Z0-9]{2,}", query):
        words.add(w.lower())
    # 中文：连续中文字符按2字滑窗
    cn_chars = re.findall(r"[\u4e00-\u9fff]+", query)
    for seg in cn_chars:
        for i in range(len(seg) - 1):
            words.add(seg[i:i + 2])
        if len(seg) == 1:
            words.add(seg)
    return words


def _format_results(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本"""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        snippet = r.get("snippet", "")
        if snippet:
            if len(snippet) > _SNIPPET_LEN:
                snippet = snippet[:_SNIPPET_LEN - 3] + "..."
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ── 主入口 ──

def execute(query: str, num: int = 5, lr: str = "") -> str:
    """
    联网搜索（Bing + 百度）。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认5
        lr: 语言限制（如lang_en/lang_zh-CN）

    Returns:
        格式化的搜索结果，每条含标题、URL、摘要
    """
    try:
        import requests  # noqa: F401
    except ImportError:
        return "错误: 需要requests库，请 pip install requests"

    seen: set = set()
    results: List[Dict] = []

    # 1. Bing — 纯HTTP，速度快
    for r in _search_bing(query, num, lr):
        key = _url_key(r.get("url", ""))
        if key and key not in seen:
            seen.add(key)
            results.append(r)

    # 2. 百度 — Playwright，中文搜索更强
    # 百度跳转链接(/link?url=xxx)按完整URL去重，因为路径都是/link
    baidu_seen: set = set()
    for r in _search_baidu(query, num):
        url = r.get("url", "")
        key = url if "/link?" in url else _url_key(url)
        if key and key not in seen and key not in baidu_seen:
            baidu_seen.add(key)
            results.append(r)

    if not results:
        return "(无搜索结果)"

    # 过滤掉明显不相关的结果（标题与查询无任何词重叠）
    q_words = _extract_query_words(query)
    if q_words:
        filtered = []
        for r in results:
            title = r.get("title", "").lower()
            # 标题至少命中1个查询词才算相关
            if any(w in title for w in q_words):
                filtered.append(r)
        # 如果过滤后还有结果就用过滤后的，否则保留原始（避免过度过滤）
        if filtered:
            results = filtered

    # 按相关性排序
    results.sort(key=lambda r: _relevance_score(r, query), reverse=True)
    return _format_results(results[:num])


# ═══════════════════════════════════════════════════════════════
# 1. Bing — 纯HTTP，国内可用（cn.bing.com）
# ═══════════════════════════════════════════════════════════════

def _request(method: str, url: str, **kwargs):
    """安全HTTP请求。仅5xx重试，连接/超时错误直接抛出。"""
    import requests

    kwargs.setdefault("headers", {"User-Agent": _UA})
    kwargs.setdefault("timeout", _TIMEOUT)

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, **kwargs) if method == "GET" else requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            if isinstance(e, requests.exceptions.HTTPError) and resp.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    continue
            raise
    raise


def _search_bing(query: str, num: int, lr: str) -> List[Dict]:
    """Bing搜索 — 国内直连可用（自动重定向到cn.bing.com）"""
    try:
        fetch_count = min(num * 2, 20)
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={fetch_count}"
        if lr:
            url += f"&setlang={lr}"
        resp = _request("GET", url)
        return _parse_bing(resp.text, num)
    except Exception:
        return []


def _parse_bing(html: str, num: int) -> List[Dict]:
    """解析Bing搜索结果：h2>a 定位标题+URL，b_caption>p 定位摘要"""
    results = []
    for m in _RE_BING_H2.finditer(html):
        url = m.group(1)
        title = _strip_tags(m.group(2))
        if not title or any(d in url for d in _BING_OWN_DOMAINS):
            continue
        snippet = _extract_bing_snippet(html, m.end())
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= num:
            break
    return results


def _extract_bing_snippet(html: str, start: int) -> str:
    """从h2位置向后提取Bing摘要"""
    window = html[start:start + 800]
    cap = _RE_BING_CAPTION.search(window)
    if cap:
        pm = _RE_P_TAG.search(cap.group(1))
        if pm:
            return _strip_tags(pm.group(1))[:_SNIPPET_LEN]
    pm = _RE_P_TAG.search(window[:400])
    if pm:
        return _strip_tags(pm.group(1))[:_SNIPPET_LEN]
    return ""


# ═══════════════════════════════════════════════════════════════
# 2. 百度 — Playwright浏览器，中文搜索强
# ═══════════════════════════════════════════════════════════════

# 模块级浏览器缓存，避免每次搜索都启动浏览器
_browser_cache = {"pw": None, "browser": None, "context": None, "page": None}


def _get_baidu_page():
    """获取或创建百度搜索用的浏览器页面（复用实例）"""
    cache = _browser_cache

    if cache["page"] is not None:
        try:
            # 检查页面是否还活着
            cache["page"].evaluate("1+1")
            return cache["page"]
        except Exception:
            _close_baidu_browser()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            channel="msedge",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        # 反自动化检测：隐藏 webdriver、伪造浏览器特征
        context.add_init_script(
            'Object.defineProperty(navigator,"webdriver",{get:()=>undefined});'
            'Object.defineProperty(navigator,"languages",{get:()=>["zh-CN","zh","en"]});'
            'Object.defineProperty(navigator,"plugins",{get:()=>[1,2,3,4,5]});'
            'window.chrome={runtime:{}};'
        )
        page = context.new_page()

        # 先访问首页获取cookie（等待JS执行设置关键cookie）
        page.goto("https://www.baidu.com/", timeout=_BAIDU_TIMEOUT)
        page.wait_for_load_state("networkidle", timeout=_BAIDU_TIMEOUT)
        time.sleep(2)  # 等待JS设置BAIDUID_BFESS/BA_HECTOR/ZFY等cookie

        cache["pw"] = pw
        cache["browser"] = browser
        cache["context"] = context
        cache["page"] = page
        return page
    except Exception:
        _close_baidu_browser()
        return None


def _close_baidu_browser():
    """关闭缓存的浏览器"""
    cache = _browser_cache
    for key in ("page", "context", "browser", "pw"):
        try:
            if cache[key] is not None:
                if key == "pw":
                    cache[key].stop()
                else:
                    cache[key].close()
        except Exception:
            pass
        cache[key] = None


def _search_baidu(query: str, num: int) -> List[Dict]:
    """百度搜索 — Playwright + 系统Edge，复用浏览器实例"""
    page = _get_baidu_page()
    if page is None:
        return []

    try:
        # 百度对长查询容易触发验证码，截断到30字符
        baidu_query = query[:30] if len(query) > 30 else query
        fetch_rn = min(num * 2, 20)
        page.goto(
            f"https://www.baidu.com/s?wd={quote_plus(baidu_query)}&rn={fetch_rn}",
            timeout=_BAIDU_TIMEOUT,
        )
        page.wait_for_load_state("networkidle", timeout=_BAIDU_TIMEOUT)
        html = page.content()
        return _parse_baidu(html, num)
    except Exception:
        # 页面可能已失效，清除缓存下次重建
        _close_baidu_browser()
        return []


def _parse_baidu(html: str, num: int) -> List[Dict]:
    """解析百度搜索结果：h3>a 定位标题+URL，c-abstract 定位摘要"""
    if len(html) < 5000 or "安全验证" in html:
        return []

    seen: set = set()
    results: List[Dict] = []

    for m in _RE_BAIDU_H3.finditer(html):
        url = m.group(1)
        title = _strip_tags(m.group(2))
        if not title or len(title) < 2:
            continue

        # 跳过百度自身非结果链接和广告
        if "/baidu.php" in url:
            continue
        if "/link?" not in url and any(d in url for d in _BAIDU_OWN_DOMAINS):
            continue

        # 去重：跳转链接按完整URL去重（路径都是/link）
        key = url if "/link?" in url else _url_key(url)
        if key in seen:
            continue
        seen.add(key)

        snippet = _extract_baidu_snippet(html, m.end())
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= num:
            break

    return results


def _extract_baidu_snippet(html: str, start: int) -> str:
    """从h3位置向后提取百度摘要"""
    window = html[start:start + 1200]
    m = _RE_BAIDU_C_ABSTRACT.search(window)
    if m:
        return _strip_tags(m.group(1))[:_SNIPPET_LEN]
    m = _RE_BAIDU_CONTENT_RIGHT.search(window)
    if m:
        return _strip_tags(m.group(1))[:_SNIPPET_LEN]
    return ""
