"""仿真HTTP服务器 —— 本地闭环测试WebSearch工具

启动本地HTTP服务器，返回预设的搜索结果页面。
WebSearch工具连上去，搜到预设结果，无需真实网络。

用法:
    with MockHTTPServer() as server:
        server.add_search_result("python教程", [
            {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/", "snippet": "Welcome to Python"},
        ])
        server.add_page("https://example.com/test.html", "<html>test content</html>")

        # WebSearch使用server.host:server.port作为代理
"""

import json
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs


class _MockHTTPHandler(BaseHTTPRequestHandler):
    """处理HTTP请求"""

    def do_GET(self):
        server = self.server.mock_server
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # 搜索API端点
        if path == "/search":
            query = params.get("q", [""])[0]
            results = server._search_results.get(query, [])
            self._send_json({"results": results})
            return

        # 预设页面
        if path in server._pages:
            self._send_html(server._pages[path])
            return

        # Bing搜索页面模拟
        if "bing.com" in self.headers.get("Host", "") or path == "/bing/search":
            query = params.get("q", [""])[0]
            html = server._generate_bing_html(query)
            self._send_html(html)
            return

        # 百度搜索页面模拟
        if "baidu.com" in self.headers.get("Host", "") or path == "/baidu/s":
            query = params.get("wd", params.get("q", [""])[0])[0] if "wd" in params else params.get("q", [""])[0]
            html = server._generate_baidu_html(query)
            self._send_html(html)
            return

        self._send_json({"error": "not found"}, 404)

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body = html.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class MockHTTPServer:
    """仿真HTTP服务器

    用法:
        with MockHTTPServer() as server:
            server.add_search_result("python教程", [
                {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/", "snippet": "Welcome"},
            ])
    """

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 0
        self._search_results = {}
        self._pages = {}
        self._server = None
        self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        self._server = HTTPServer((self.host, 0), _MockHTTPHandler)
        self._server.mock_server = self
        self.port = self._server.server_address[1]

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def stop(self):
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=3)

    def add_search_result(self, query: str, results: list):
        """添加搜索结果

        Args:
            query: 搜索关键词
            results: [{"title": "...", "url": "...", "snippet": "..."}, ...]
        """
        self._search_results[query] = results

    def add_page(self, url: str, html: str):
        """添加预设页面"""
        parsed = urlparse(url)
        self._pages[parsed.path] = html

    def _generate_bing_html(self, query: str) -> str:
        """生成Bing搜索结果HTML"""
        results = self._search_results.get(query, [])
        items = ""
        for r in results:
            items += f'''
            <li class="b_algo">
                <h2><a href="{r['url']}">{r['title']}</a></h2>
                <p>{r.get('snippet', '')}</p>
            </li>'''
        return f'''<html><body>
            <div id="b_results">
                <ol>{items}</ol>
            </div>
        </body></html>'''

    def _generate_baidu_html(self, query: str) -> str:
        """生成百度搜索结果HTML"""
        results = self._search_results.get(query, [])
        items = ""
        for r in results:
            items += f'''
            <div class="result c-container">
                <h3 class="t"><a href="{r['url']}">{r['title']}</a></h3>
                <span class="content-right_8Zs40">{r.get('snippet', '')}</span>
            </div>'''
        return f'''<html><body>
            <div id="content_left">{items}</div>
        </body></html>'''
