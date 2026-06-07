"""WebSearch工具 —— Bing(HTTP) + 百度(Playwright子进程) 双引擎

降级策略:
1. Bing HTML — 纯HTTP，速度快（~1s），国内直连cn.bing.com
2. Baidu    — Playwright子进程，速度慢（~3s），中文搜索质量更高

Bing 先行快速返回，百度补充更多中文结果。两者合并去重后按相关性排序。

百度必须用浏览器：纯HTTP请求无法绕过验证码（百度JS设置关键cookie）。
Playwright在子进程中运行，避免事件循环污染主进程（prompt_toolkit依赖asyncio）。
"""

import json
import os
import re
import subprocess
import sys
import time
import platform
from html import unescape as _html_unescape
from typing import List, Dict, Optional, Callable
from urllib.parse import quote_plus, urlparse


# ── 常量 ──

# 中断检查回调，由agent层注入（返回True表示用户按了ESC）
_interrupt_check: Optional[Callable[[], bool]] = None


def set_interrupt_check(cb: Callable[[], bool]):
    """设置中断检查回调。cb返回True表示用户请求中断。"""
    global _interrupt_check
    _interrupt_check = cb

def _make_ua() -> str:
    """根据当前平台生成 User-Agent 字符串。"""
    os_name = platform.system()
    if os_name == "Windows":
        os_part = "Windows NT 10.0; Win64; x64"
    elif os_name == "Darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_part = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({os_part}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

_UA = _make_ua()

_TIMEOUT = 5           # HTTP请求超时秒数
_MAX_RETRIES = 50      # 仅5xx重试
_BAIDU_TIMEOUT = 20    # 百度子进程超时秒数

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


def _extract_query_words(query: str) -> set:
    """
    提取查询词用于相关性判断。
    英文按空格拆分，中文按2字滑窗拆分（"红米K80" → "红米","米k","k8","80"）。
    """
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
    """结果与查询的相关性：标题命中查询词越多分越高"""
    title = result.get("title", "").lower()
    snippet = result.get("snippet", "").lower()
    q_words = _extract_query_words(query)
    if not q_words:
        return 0.5
    title_hits = sum(1 for w in q_words if w in title)
    snippet_hits = sum(1 for w in q_words if w in snippet)
    return (title_hits * 3 + snippet_hits) / (len(q_words) * 4)


def _format_results(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本，完整返回摘要不截断"""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        snippet = r.get("snippet", "")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ── 主入口 ──

def execute(query: str, num: int = 10, lr: str = "") -> str:
    """
    联网搜索（Bing + 百度）。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认10
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

    # ESC铁律: Bing完成后检查中断，跳过百度
    if _interrupt_check and _interrupt_check():
        if not results:
            return "[用户中断]"
        return _format_results(results[:num]) + "\n[用户中断]"

    # 2. 百度 — Playwright子进程，中文搜索更强
    baidu_seen: set = set()
    for r in _search_baidu(query, num):
        url = r.get("url", "")
        key = url if "/link?" in url else _url_key(url)
        if key and key not in seen and key not in baidu_seen:
            baidu_seen.add(key)
            results.append(r)

    if not results:
        return "(无搜索结果)"

    # ESC铁律: 百度完成后也检查中断
    if _interrupt_check and _interrupt_check():
        return _format_results(results[:num]) + "\n[用户中断]"

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
    """从h2位置向后提取Bing摘要，完整返回不截断"""
    window = html[start:start + 800]
    cap = _RE_BING_CAPTION.search(window)
    if cap:
        pm = _RE_P_TAG.search(cap.group(1))
        if pm:
            return _strip_tags(pm.group(1))
    pm = _RE_P_TAG.search(window[:400])
    if pm:
        return _strip_tags(pm.group(1))
    return ""


# ═══════════════════════════════════════════════════════════════
# 2. 百度 — Playwright子进程，隔离事件循环
# ═══════════════════════════════════════════════════════════════

# 子进程脚本路径
_BAIDU_WORKER = os.path.join(os.path.dirname(__file__), "_baidu_worker.py")

# 长连接worker进程缓存
_worker_proc = None
_worker_ready = False


def _get_worker():
    """获取或启动百度搜索worker子进程（长连接复用浏览器）"""
    global _worker_proc, _worker_ready

    # 检查现有进程是否还活着
    if _worker_proc is not None and _worker_ready:
        if _worker_proc.poll() is None:
            return _worker_proc
        # 进程已死，清理
        _cleanup_worker()

    if not os.path.exists(_BAIDU_WORKER):
        return None

    try:
        proc = subprocess.Popen(
            [sys.executable, _BAIDU_WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        # 等待ready信号
        line = proc.stdout.readline()
        if not line:
            _cleanup_worker()
            return None

        text = _decode_output(line)
        if '"ready"' not in text:
            _cleanup_worker()
            return None

        _worker_proc = proc
        _worker_ready = True
        return proc
    except Exception:
        _cleanup_worker()
        return None


def _cleanup_worker():
    """清理worker子进程"""
    global _worker_proc, _worker_ready
    if _worker_proc is not None:
        try:
            _worker_proc.stdin.close()
        except Exception:
            pass
        try:
            _worker_proc.terminate()
            _worker_proc.wait(timeout=3)
        except Exception:
            try:
                _worker_proc.kill()
            except Exception:
                pass
    _worker_proc = None
    _worker_ready = False


def _decode_output(data: bytes) -> str:
    """解码子进程输出。Windows默认GBK，Unix默认UTF-8。"""
    fallback_encs = ("gbk",) if sys.platform == "win32" else ()
    for enc in ("utf-8",) + fallback_encs + ("latin-1",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _search_baidu(query: str, num: int) -> List[Dict]:
    """百度搜索 — 通过长连接worker子进程，复用浏览器实例"""
    proc = _get_worker()
    if proc is None:
        return []

    # 百度搜索 — 纯管道: AI给什么搜什么，不截断
    baidu_query = query

    try:
        # 发送搜索请求
        cmd = json.dumps({"query": baidu_query, "num": num}, ensure_ascii=False) + "\n"
        proc.stdin.write(cmd.encode("utf-8"))
        proc.stdin.flush()

        # 读取结果
        line = proc.stdout.readline()
        if not line:
            _cleanup_worker()
            return []

        text = _decode_output(line)
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        _cleanup_worker()
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
    """从h3位置向后提取百度摘要，完整返回不截断"""
    window = html[start:start + 1200]
    m = _RE_BAIDU_C_ABSTRACT.search(window)
    if m:
        return _strip_tags(m.group(1))
    m = _RE_BAIDU_CONTENT_RIGHT.search(window)
    if m:
        return _strip_tags(m.group(1))
    return ""
