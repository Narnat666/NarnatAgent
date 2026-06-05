"""LLM仿真测试 —— 基于MockLLMServer的闭环测试

验证MockLLMServer能正确模拟OpenAI兼容API，
Agent连上去后按序收到预设tool_call，形成完整闭环。
"""

import json
import pytest
import urllib.request

from tests.simulators.mock_llm_server import MockLLMServer


class TestMockLLMServerBasic:
    """MockLLMServer基础功能"""

    def test_server_starts(self):
        """服务器能启动和停止"""
        server = MockLLMServer()
        server.start()
        assert server.port > 0
        assert server.base_url.startswith("http://")
        server.stop()

    def test_context_manager(self):
        """上下文管理器模式"""
        with MockLLMServer() as server:
            assert server.port > 0

    def test_enqueue_text(self):
        """预设纯文本响应"""
        with MockLLMServer(stream=False) as server:
            server.enqueue_text("Hello from mock LLM!")

            # 发送请求
            data = json.dumps({
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert result["choices"][0]["message"]["content"] == "Hello from mock LLM!"

    def test_enqueue_tool_calls(self):
        """预设tool_call响应"""
        with MockLLMServer(stream=False) as server:
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
            ])

            data = json.dumps({
                "model": "mock-model",
                "messages": [{"role": "user", "content": "read file"}],
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                tc = result["choices"][0]["message"]["tool_calls"]
                assert len(tc) == 1
                assert tc[0]["function"]["name"] == "Read"
                args = json.loads(tc[0]["function"]["arguments"])
                assert args["file_path"] == "/tmp/test.txt"

    def test_sequential_responses(self):
        """按序消费多个响应"""
        with MockLLMServer(stream=False) as server:
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/a.txt"}},
            ])
            server.enqueue_text("文件内容已读取。")
            server.enqueue_tool_calls([
                {"name": "Edit", "arguments": {"file_path": "/tmp/a.txt", "old_string": "old", "new_string": "new"}},
            ])
            server.enqueue_text("修改完成。")

            assert server.remaining == 4

            # 第1次请求: tool_call Read
            data = json.dumps({"model": "mock", "messages": []}).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Read"

            assert server.remaining == 3

            # 第2次请求: 纯文本
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert "已读取" in result["choices"][0]["message"]["content"]

            # 第3次请求: tool_call Edit
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Edit"

            # 第4次请求: 纯文本
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert "完成" in result["choices"][0]["message"]["content"]

    def test_queue_exhaustion(self):
        """队列耗尽返回默认结束消息"""
        with MockLLMServer(stream=False) as server:
            # 不enqueue任何响应
            data = json.dumps({"model": "mock", "messages": []}).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                assert "任务完成" in result["choices"][0]["message"]["content"]

    def test_clear_queue(self):
        """清空响应队列"""
        server = MockLLMServer()
        server.start()
        server.enqueue_text("a")
        server.enqueue_text("b")
        assert server.remaining == 2
        server.clear()
        assert server.remaining == 0
        server.stop()


class TestMockLLMServerStream:
    """MockLLMServer流式响应"""

    def test_stream_text(self):
        """流式文本响应"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("Hello!")

            data = json.dumps({
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                content = resp.read().decode("utf-8")
                # SSE格式: data: {...}\n\n
                assert "data:" in content
                assert "[DONE]" in content

    def test_stream_tool_calls(self):
        """流式tool_call响应"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_tool_calls([
                {"name": "Bash", "arguments": {"command": "ls"}},
            ])

            data = json.dumps({
                "model": "mock-model",
                "messages": [{"role": "user", "content": "list files"}],
                "stream": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{server.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                content = resp.read().decode("utf-8")
                assert "Bash" in content
                assert "[DONE]" in content
