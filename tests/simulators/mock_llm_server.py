"""仿真LLM API服务器 —— 本地闭环测试Agent调度循环

模拟OpenAI兼容API服务器，返回预设的tool_call序列。
Agent连上去，收到预设tool_call→执行工具→回传结果→收到下一个tool_call，
形成完整的"AI决策→工具执行→结果回传→AI再决策"闭环。

用法:
    with MockLLMServer() as server:
        server.enqueue_tool_calls([
            {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
        ])
        server.enqueue_text("我已经读取了文件内容。")

        # Agent使用server.base_url作为API端点
        agent = Agent(ai_config=AIConfig(
            api_key="test",
            base_url=server.base_url,
            model="mock-model",
        ))
        agent.run("读取/tmp/test.txt")
"""

import json
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional


class _ResponseQueue:
    """线程安全的响应队列"""

    def __init__(self):
        self._queue = []
        self._lock = threading.Lock()
        self._index = 0

    def enqueue(self, response: dict):
        with self._lock:
            self._queue.append(response)

    def dequeue(self) -> Optional[dict]:
        with self._lock:
            if self._index < len(self._queue):
                resp = self._queue[self._index]
                self._index += 1
                return resp
        return None

    def clear(self):
        with self._lock:
            self._queue.clear()
            self._index = 0

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._queue) - self._index


class _MockAPIHandler(BaseHTTPRequestHandler):
    """处理OpenAI兼容API请求"""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        if self.path.endswith("/chat/completions"):
            self._handle_chat_completions(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_chat_completions(self, body: str):
        """处理 /v1/chat/completions 请求"""
        server = self.server.mock_server
        response = server._responses.dequeue()

        if response is None:
            # 队列耗尽，返回一个结束消息
            response = {
                "role": "assistant",
                "content": "任务完成。",
                "tool_calls": [],
            }

        # 构建SSE流式响应
        if server.stream:
            self._send_stream_response(response)
        else:
            self._send_non_stream_response(response)

    def _send_non_stream_response(self, response: dict):
        """非流式响应"""
        message = {
            "role": "assistant",
            "content": response.get("content", ""),
        }
        if response.get("tool_calls"):
            message["tool_calls"] = []
            for i, tc in enumerate(response["tool_calls"]):
                message["tool_calls"].append({
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"],
                    },
                })

        result = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "stop" if not response.get("tool_calls") else "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        self._send_json(result)

    def _send_stream_response(self, response: dict):
        """流式SSE响应（模拟OpenAI streaming格式）"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        # 先发送content（逐字符流式）
        if content:
            for char in content:
                chunk = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "model": "mock-model",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": char},
                        "finish_reason": None,
                    }],
                }
                self._send_sse_chunk(chunk)

        # 发送tool_calls
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                # 第一个chunk: function name
                chunk = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "model": "mock-model",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": i,
                                "id": f"call_{i}",
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": "",
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }
                self._send_sse_chunk(chunk)

                # 第二个chunk: arguments
                args_str = json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"]
                chunk["choices"][0]["delta"] = {
                    "tool_calls": [{
                        "index": i,
                        "function": {"arguments": args_str},
                    }],
                }
                self._send_sse_chunk(chunk)

        # 结束chunk
        finish_chunk = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
        }
        self._send_sse_chunk(finish_chunk)

        # SSE结束
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def _send_sse_chunk(self, data: dict):
        try:
            line = f"data: {json.dumps(data)}\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默日志


class MockLLMServer:
    """仿真LLM API服务器

    启动本地HTTP服务器，模拟OpenAI兼容API。
    预设tool_call序列，Agent连上去后按序收到响应。

    用法:
        with MockLLMServer() as server:
            # 预设AI的决策序列
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
            ])
            server.enqueue_text("文件内容已读取。")
            server.enqueue_tool_calls([
                {"name": "Edit", "arguments": {"file_path": "/tmp/test.txt", "old_string": "old", "new_string": "new"}},
            ])
            server.enqueue_text("修改完成。")

            # Agent使用server.base_url
            # agent = Agent(ai_config=AIConfig(api_key="test", base_url=server.base_url, model="mock"))
    """

    def __init__(self, stream: bool = True):
        self.stream = stream
        self.host = "127.0.0.1"
        self.port = 0
        self._responses = _ResponseQueue()
        self._server = None
        self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        """启动API服务器"""
        self._server = HTTPServer((self.host, 0), _MockAPIHandler)
        self._server.mock_server = self
        self.port = self._server.server_address[1]

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def stop(self):
        """停止API服务器"""
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=3)

    def enqueue_tool_calls(self, tool_calls: list):
        """预设AI返回tool_call

        Args:
            tool_calls: [{"name": "Read", "arguments": {"file_path": "..."}}, ...]
        """
        self._responses.enqueue({
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        })

    def enqueue_text(self, text: str):
        """预设AI返回纯文本（无tool_call）"""
        self._responses.enqueue({
            "role": "assistant",
            "content": text,
            "tool_calls": [],
        })

    def enqueue_response(self, content: str = "", tool_calls: list = None):
        """预设AI返回混合响应（文本+tool_call）"""
        self._responses.enqueue({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls or [],
        })

    def clear(self):
        """清空响应队列"""
        self._responses.clear()

    @property
    def remaining(self) -> int:
        """剩余未消费的响应数"""
        return self._responses.remaining
