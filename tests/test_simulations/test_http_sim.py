"""HTTP仿真测试 —— 基于MockHTTPServer的闭环测试

验证MockHTTPServer能正确模拟Open WebSearch daemon的搜索API。
"""

import json
import urllib.request
import pytest

from tests.simulators.mock_http_server import MockHTTPServer


class TestMockHTTPServerBasic:
    """MockHTTPServer基础功能"""

    def test_server_starts(self):
        server = MockHTTPServer()
        server.start()
        assert server.port > 0
        server.stop()

    def test_context_manager(self):
        with MockHTTPServer() as server:
            assert server.port > 0

    def test_health_endpoint(self):
        """健康检查端点"""
        with MockHTTPServer() as server:
            url = f"{server.base_url}/health"
            with urllib.request.urlopen(url) as resp:
                result = json.loads(resp.read())
                assert result["status"] == "ok"

    def test_search_api(self):
        """搜索API返回预设结果（POST JSON）"""
        with MockHTTPServer() as server:
            server.add_search_result("python教程", [
                {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/", "snippet": "Welcome to Python"},
                {"title": "菜鸟教程", "url": "https://www.runoob.com/python3/", "snippet": "Python3基础教程"},
            ])

            data = json.dumps({"query": "python教程", "maxResults": 5, "engine": "bing"}).encode()
            req = urllib.request.Request(
                f"{server.base_url}/search",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                results = result["data"]["results"]
                assert len(results) == 2
                assert results[0]["title"] == "Python官方教程"

    def test_no_result(self):
        """无搜索结果"""
        with MockHTTPServer() as server:
            data = json.dumps({"query": "nonexistent", "maxResults": 5, "engine": "bing"}).encode()
            req = urllib.request.Request(
                f"{server.base_url}/search",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert result["data"]["results"] == []

    def test_max_results_limit(self):
        """maxResults限制返回数量"""
        with MockHTTPServer() as server:
            server.add_search_result("python", [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": f"Snippet {i}"}
                for i in range(10)
            ])

            data = json.dumps({"query": "python", "maxResults": 3, "engine": "bing"}).encode()
            req = urllib.request.Request(
                f"{server.base_url}/search",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert len(result["data"]["results"]) == 3

    def test_multiple_search_results(self):
        """多个搜索关键词"""
        with MockHTTPServer() as server:
            server.add_search_result("python", [
                {"title": "Python", "url": "https://python.org/", "snippet": "Python"},
            ])
            server.add_search_result("rust", [
                {"title": "Rust", "url": "https://rust-lang.org/", "snippet": "Rust"},
            ])

            for query, expected in [("python", "Python"), ("rust", "Rust")]:
                data = json.dumps({"query": query, "maxResults": 5, "engine": "bing"}).encode()
                req = urllib.request.Request(
                    f"{server.base_url}/search",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req) as resp:
                    result = json.loads(resp.read())
                    assert expected in result["data"]["results"][0]["title"]
