"""百度搜索 Worker — 长连接模式，复用浏览器实例

被 web_search.py 的 _search_baidu() 通过 subprocess 调用。
协议:
  - 启动后等待 stdin 输入 JSON: {"query": "...", "num": 5}
  - 每次输入后输出 JSON 结果到 stdout: [{"title":..., "url":..., "snippet":...}, ...]
  - 输入空行或 EOF 时退出
"""

import json
import re
import sys
import time
from html import unescape
from urllib.parse import quote_plus


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_TIMEOUT_MS = 15000
_SNIPPET_LEN = 200

_RE_STRIP_TAG = re.compile(r"<[^>]+>")
_RE_H3 = re.compile(r'<h3[^>]*>.*?href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_RE_C_ABSTRACT = re.compile(r'class="c-abstract"[^>]*>(.*?)</(?:span|div)>', re.DOTALL)
_RE_CONTENT_RIGHT = re.compile(
    r'<span[^>]+class="content-right_[^"]*"[^>]*>(.*?)</span>', re.DOTALL,
)
_BAIDU_OWN = {"baidu.com", "baidu.php"}


def _strip_tags(text: str) -> str:
    return unescape(_RE_STRIP_TAG.sub("", text)).strip()


def _extract_snippet(html: str, start: int) -> str:
    window = html[start:start + 1200]
    m = _RE_C_ABSTRACT.search(window)
    if m:
        return _strip_tags(m.group(1))[:_SNIPPET_LEN]
    m = _RE_CONTENT_RIGHT.search(window)
    if m:
        return _strip_tags(m.group(1))[:_SNIPPET_LEN]
    return ""


def _parse(html: str, num: int) -> list:
    if len(html) < 5000 or "安全验证" in html:
        return []

    seen: set = set()
    results: list = []

    for m in _RE_H3.finditer(html):
        url = m.group(1)
        title = _strip_tags(m.group(2))
        if not title or len(title) < 2:
            continue
        if "/baidu.php" in url:
            continue
        if "/link?" not in url and any(d in url for d in _BAIDU_OWN):
            continue
        key = url if "/link?" in url else url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)

        snippet = _extract_snippet(html, m.end())
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= num:
            break

    return results


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[]", flush=True)
        return

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
        context.add_init_script(
            'Object.defineProperty(navigator,"webdriver",{get:()=>undefined});'
            'Object.defineProperty(navigator,"languages",{get:()=>["zh-CN","zh","en"]});'
            'Object.defineProperty(navigator,"plugins",{get:()=>[1,2,3,4,5]});'
            'window.chrome={runtime:{}};'
        )
        page = context.new_page()

        # 先访问首页获取cookie
        page.goto("https://www.baidu.com/", timeout=_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
        time.sleep(2)

        # 通知主进程：worker已就绪
        print('{"status":"ready"}', flush=True)

        # 长连接：循环读取stdin
        for line in sys.stdin:
            line = line.strip()
            if not line:
                break
            try:
                cmd = json.loads(line)
                query = cmd.get("query", "")
                num = cmd.get("num", 5)
            except json.JSONDecodeError:
                print("[]", flush=True)
                continue

            try:
                fetch_rn = min(num * 2, 20)
                page.goto(
                    f"https://www.baidu.com/s?wd={quote_plus(query)}&rn={fetch_rn}",
                    timeout=_TIMEOUT_MS,
                )
                page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
                html = page.content()
                results = _parse(html, num)
                print(json.dumps(results, ensure_ascii=False), flush=True)
            except Exception:
                print("[]", flush=True)

        browser.close()
        pw.stop()

    except Exception:
        print("[]", flush=True)


if __name__ == "__main__":
    main()
