"""HTTP仿真测试 —— 基于MockHTTPServer的闭环测试

验证MockHTTPServer能正确模拟搜索API和网页。
"""

import json
import pytest
import urllib.request

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

    def test_search_api(self):
        """搜索API返回预设结果"""
        with MockHTTPServer() as server:
            server.add_search_result("python教程", [
                {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/", "snippet": "Welcome to Python"},
                {"title": "菜鸟教程", "url": "https://www.runoob.com/python3/", "snippet": "Python3基础教程"},
            ])

            url = f"{server.base_url}/search?q=python%E6%95%99%E7%A8%8B"
            with urllib.request.urlopen(url) as resp:
                result = json.loads(resp.read())
                assert len(result["results"]) == 2
                assert result["results"][0]["title"] == "Python官方教程"

    def test_preset_page(self):
        """预设页面"""
        with MockHTTPServer() as server:
            server.add_page("https://example.com/test.html", "<html><body>Test Content</body></html>")

            url = f"{server.base_url}/test.html"
            with urllib.request.urlopen(url) as resp:
                content = resp.read().decode("utf-8")
                assert "Test Content" in content

    def test_bing_search_html(self):
        """Bing搜索HTML"""
        with MockHTTPServer() as server:
            server.add_search_result("cmake", [
                {"title": "CMake官网", "url": "https://cmake.org/", "snippet": "Build with CMake"},
            ])

            url = f"{server.base_url}/bing/search?q=cmake"
            with urllib.request.urlopen(url) as resp:
                content = resp.read().decode("utf-8")
                assert "CMake官网" in content
                assert "cmake.org" in content

    def test_baidu_search_html(self):
        """百度搜索HTML"""
        with MockHTTPServer() as server:
            server.add_search_result("gcc安装", [
                {"title": "GCC安装教程", "url": "https://gcc.gnu.org/", "snippet": "安装GCC编译器"},
            ])

            url = f"{server.base_url}/baidu/s?wd=gcc%E5%AE%89%E8%A3%85"
            with urllib.request.urlopen(url) as resp:
                content = resp.read().decode("utf-8")
                assert "GCC安装教程" in content

    def test_no_result(self):
        """无搜索结果"""
        with MockHTTPServer() as server:
            url = f"{server.base_url}/search?q=nonexistent"
            with urllib.request.urlopen(url) as resp:
                result = json.loads(resp.read())
                assert result["results"] == []

    def test_multiple_search_results(self):
        """多个搜索关键词"""
        with MockHTTPServer() as server:
            server.add_search_result("python", [
                {"title": "Python", "url": "https://python.org/", "snippet": "Python"},
            ])
            server.add_search_result("rust", [
                {"title": "Rust", "url": "https://rust-lang.org/", "snippet": "Rust"},
            ])

            url1 = f"{server.base_url}/search?q=python"
            with urllib.request.urlopen(url1) as resp:
                assert "Python" in json.loads(resp.read())["results"][0]["title"]

            url2 = f"{server.base_url}/search?q=rust"
            with urllib.request.urlopen(url2) as resp:
                assert "Rust" in json.loads(resp.read())["results"][0]["title"]
