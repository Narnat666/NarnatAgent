"""仿真HTTP服务器 —— 本地闭环测试WebSearch工具

模拟 Open WebSearch daemon 的 API 接口。
WebSearch工具连上去，搜到预设结果，无需真实网络。

用法:
    with MockHTTPServer() as server:
        server.add_search_result("python教程", [
            {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/", "snippet": "Welcome to Python"},
        ])

        # WebSearch使用server.base_url作为daemon地址
"""

import json
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs


class _MockHTTPHandler(BaseHTTPRequestHandler):
    """处理HTTP请求 — 模拟 Open WebSearch daemon"""

    def do_GET(self):
        server = self.server.mock_server
        parsed = urlparse(self.path)
        path = parsed.path

        # 健康检查端点
        if path == "/health":
            self._send_json({"status": "ok", "message": "open-websearch daemon running"})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        server = self.server.mock_server
        parsed = urlparse(self.path)
        path = parsed.path

        # 搜索API端点 — 模拟 Open WebSearch daemon 的 /search
        if path == "/search":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, 400)
                return

            query = data.get("query", "")
            engine = data.get("engine", "bing")
            max_results = data.get("maxResults", 5)

            # 查找预设结果
            results = server._search_results.get(query, [])
            # 限制返回数量
            results = results[:max_results]

            self._send_json({
                "data": {
                    "results": results,
                    "query": query,
                    "engine": engine,
                }
            })
            return

        self._send_json({"error": "not found"}, 404)

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class MockHTTPServer:
    """仿真HTTP服务器 — 模拟 Open WebSearch daemon

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
