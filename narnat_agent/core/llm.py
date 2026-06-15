"""
LLM调用层 —— 双协议支持（OpenAI兼容 + Anthropic兼容），流式输出，token计数

通过 base_url 中是否包含 "anthropic" 自动选择协议：
- 含 "anthropic" → AnthropicClient（/v1/messages 端点，x-api-key 认证）
- 其他 → OpenAI SDK（/chat/completions 端点，Bearer 认证）

上层统一使用 OpenAI 格式的 messages/tool_calls，AnthropicClient 内部做双向转换。
"""

import json
import queue
import random
import re
import threading
import time
import httpx
from typing import List, Dict, Any, Iterator, Optional

from ..config.loader import AIConfig
from ..tools.registry import get_tool_definitions

# ESC中断：持有当前活跃的LLM HTTP连接，供ESC轮询线程关闭
_active_llm_response = None

# 队列哨兵值，标记流结束
_STREAM_END = object()

# 重试参数
_MAX_NETWORK_RETRIES = 3   # 网络/服务端错误
_MAX_RATE_RETRIES = 5      # 429 速率限制
_RETRY_BACKOFF_BASE = [1, 2, 4, 8, 8]  # 1s起步，8s封顶


def _retry_sleep(attempt: int, cancel_check=None) -> bool:
    """指数退避休眠，带 jitter 和中断检查。返回 False 表示用户取消。"""
    base = _RETRY_BACKOFF_BASE[min(attempt, len(_RETRY_BACKOFF_BASE) - 1)]
    jitter = base * 0.25 * (random.random() * 2 - 1)  # ±25%
    sleep_time = max(0, base + jitter)
    deadline = time.time() + sleep_time
    while time.time() < deadline:
        if cancel_check and cancel_check():
            return False
        time.sleep(min(0.2, deadline - time.time()))
    return True


def _is_retryable_http(status: int) -> bool:
    """判断 HTTP 状态码是否可重试（不含 429，429 单独处理）"""
    return status in (408, 409) or status >= 500


def abort_active_llm_request():
    """关闭当前LLM请求的HTTP连接。"""
    global _active_llm_response
    resp = _active_llm_response
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass


def _iter_to_queue(iterator, q):
    """后台线程：将迭代器的元素逐个放入队列，最后放入_STREAM_END。"""
    try:
        for item in iterator:
            q.put(item)
    except Exception:
        pass  # 连接关闭等异常，主线程通过取消检查来处理
    finally:
        q.put(_STREAM_END)


class LLMClient:
    """
    LLM客户端，自动选择 OpenAI 或 Anthropic 协议。

    对外接口统一：chat_stream() yield OpenAI 格式的 chunk。
    """

    def __init__(self, config: AIConfig, logger=None):
        self._config = config
        self._logger = logger
        self._tool_defs = get_tool_definitions()

        if "anthropic" in config.base_url.lower():
            self._backend = _AnthropicBackend(config, self._tool_defs, logger)
        else:
            self._backend = _OpenAIBackend(config, self._tool_defs, logger)

    def chat_stream(self, messages: List[Dict[str, Any]], no_tools: bool = False, cancel_check=None) -> Iterator:
        """流式调用LLM，yield OpenAI格式chunk。no_tools=True时不传工具定义（压缩请求用）。
        cancel_check: 可选的 callable，返回 True 表示用户请求中断。"""
        return self._backend.chat_stream(messages, no_tools=no_tools, cancel_check=cancel_check)

    @property
    def raw_sse(self):
        """空回复调试：返回上轮 Anthropic 原始 SSE 数据。若非 Anthropic 后端返回 None。"""
        if hasattr(self._backend, '_last_raw_sse'):
            return list(self._backend._last_raw_sse)
        return None



# ═══════════════════════════════════════════════════════════════
# OpenAI 兼容后端
# ═══════════════════════════════════════════════════════════════

class _OpenAIBackend:
    """OpenAI SDK 后端"""

    def __init__(self, config, tool_defs, logger):
        from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError
        self._APIStatusError = APIStatusError
        self._APIConnectionError = APIConnectionError
        self._APITimeoutError = APITimeoutError
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=0,  # 禁用SDK层重试（我们自己控制）
        )

    def chat_stream(self, messages, no_tools=False, cancel_check=None):
        """OpenAI 流式调用。cancel_check 为 callable，返回 True 表示应中断。"""
        if self._logger:
            self._logger.info("core.llm", f"发送请求(OpenAI), messages={len(messages)}条")

        global _active_llm_response

        network_retries = 0
        rate_retries = 0
        stream = None

        while True:
            _active_llm_response = self._client
            try:
                kwargs = dict(
                    model=self._config.model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    timeout=httpx.Timeout(connect=5.0, read=0.2, write=30.0, pool=30.0),
                )
                if not no_tools:
                    kwargs["tools"] = self._tool_defs
                stream = self._client.chat.completions.create(**kwargs)
                break
            except self._APIStatusError as e:
                _active_llm_response = None
                status = e.status_code

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {e}")
                    yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                    return

                if cancel_check and cancel_check():
                    return

                # 429 速率限制
                if status == 429 and rate_retries < _MAX_RATE_RETRIES:
                    rate_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                    if not _retry_sleep(rate_retries - 1, cancel_check):
                        return
                    continue

                # 可重试服务端错误
                if _is_retryable_http(status) and network_retries < _MAX_NETWORK_RETRIES:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                    if not _retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue

                if self._logger:
                    self._logger.error("core.llm", f"API调用失败(重试耗尽): {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

            except (self._APIConnectionError, self._APITimeoutError) as e:
                _active_llm_response = None
                if cancel_check and cancel_check():
                    return
                if network_retries < _MAX_NETWORK_RETRIES:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"网络错误(第{network_retries}次重试): {e}")
                    if not _retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"网络错误(重试耗尽): {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

            except Exception as e:
                _active_llm_response = None
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

        _active_llm_response = stream  # 切换到stream对象

        try:
            tool_calls_buffer = {}      # tc_id → {id, name, arguments}
            _index_to_id = {}           # index → tc_id（用于增量chunk匹配）
            content_buffer = []
            _tc_idx = 0

            # 后台线程读取stream，主线程通过Queue消费——主线程永不阻塞在C级别recv()
            chunk_queue = queue.Queue()
            reader = threading.Thread(
                target=_iter_to_queue, args=(iter(stream), chunk_queue), daemon=True)
            reader.start()

            while True:
                try:
                    chunk = chunk_queue.get(timeout=0.1)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    continue
                if chunk is _STREAM_END:
                    break

                # 捕获usage（OpenAI stream_options={"include_usage": True} 的最后chunk）
                usage = getattr(chunk, 'usage', None)
                if usage:
                    cached = 0
                    details = getattr(usage, 'prompt_tokens_details', None)
                    if details:
                        cached = getattr(details, 'cached_tokens', 0)
                    yield {
                        "usage": {
                            "prompt_tokens": usage.prompt_tokens,
                            "completion_tokens": usage.completion_tokens,
                            "cached_tokens": cached,
                        }
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if delta.content:
                    content_buffer.append(delta.content)
                    yield {"content": delta.content}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        tc_index = getattr(tc, 'index', None)
                        if tc.id:
                            tc_id = tc.id
                            if tc_index is not None:
                                _index_to_id[tc_index] = tc_id
                        elif tc_index is not None and tc_index in _index_to_id:
                            tc_id = _index_to_id[tc_index]
                        else:
                            tc_id = f"_tc_{_tc_idx}"
                            _tc_idx += 1
                        buf = tool_calls_buffer.setdefault(
                            tc_id, {"id": tc_id, "name": "", "arguments": ""},
                        )
                        if tc.function.name:
                            buf["name"] += tc.function.name
                        if tc.function.arguments:
                            buf["arguments"] += tc.function.arguments

                if finish_reason:
                    if tool_calls_buffer:
                        completed_calls = []
                        for tc_id, buf in tool_calls_buffer.items():
                            completed_calls.append({
                                "id": buf["id"],
                                "type": "function",
                                "function": {"name": buf["name"], "arguments": buf["arguments"]},
                            })
                        yield {"tool_calls": completed_calls, "finish_reason": finish_reason}
                    else:
                        yield {"finish_reason": finish_reason}

                    if self._logger:
                        total_out = len("".join(content_buffer))
                        self._logger.info("core.llm", f"响应完成, content_len={total_out}")
        finally:
            _active_llm_response = None


# ═══════════════════════════════════════════════════════════════
# Anthropic 兼容后端
# ═══════════════════════════════════════════════════════════════

class _AnthropicBackend:
    """
    Anthropic 兼容后端。

    端点: {base_url}/v1/messages
    认证: x-api-key + anthropic-version
    流式: content_block_start/delta 事件

    内部做 OpenAI ↔ Anthropic 消息格式双向转换，
    对外统一 yield OpenAI 格式的 chunk。
    """

    def __init__(self, config, tool_defs, logger):
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._url = f"{config.base_url.rstrip('/')}/v1/messages"
        self._last_raw_sse = []  # 空回复调试：缓存本轮原始SSE数据
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _get_max_tokens():
        try:
            from ..ui.ui_design import MAX_TOKENS
            return MAX_TOKENS
        except Exception:
            return 128000

    def chat_stream(self, messages, no_tools=False, cancel_check=None):
        self._last_raw_sse.clear()
        if self._logger:
            self._logger.info("core.llm", f"发送请求(Anthropic), messages={len(messages)}条")

        # 转换消息格式：OpenAI → Anthropic
        try:
            system, anthropic_msgs = self._convert_messages(messages)
            anthropic_tools = self._convert_tools(self._tool_defs)
        except Exception as e:
            yield {"content": f"错误: 消息格式转换失败: {e}", "finish_reason": "error"}
            return

        body = {
            "model": self._config.model,
            "messages": anthropic_msgs,
            "max_tokens": self._get_max_tokens(),
            "stream": True,
        }
        if system:
            body["system"] = system
        if anthropic_tools and not no_tools:
            body["tools"] = anthropic_tools

        # 发送请求（带重试）
        import requests
        session = requests.Session()
        global _active_llm_response

        network_retries = 0
        rate_retries = 0

        while True:
            _active_llm_response = session
            try:
                resp = session.post(
                    self._url, headers=self._headers, json=body,
                    stream=True, timeout=120.0,
                )
                status = resp.status_code

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    resp.raise_for_status()

                # 429 速率限制
                if status == 429:
                    if rate_retries < _MAX_RATE_RETRIES:
                        session.close()
                        rate_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                        if not _retry_sleep(rate_retries - 1, cancel_check):
                            _active_llm_response = None
                            return
                        session = requests.Session()
                        continue
                    _active_llm_response = None
                    session.close()
                    yield {"content": f"错误: API返回429速率限制(已重试{_MAX_RATE_RETRIES}次)", "finish_reason": "error"}
                    return

                # 可重试服务端错误
                if _is_retryable_http(status):
                    if network_retries < _MAX_NETWORK_RETRIES:
                        session.close()
                        network_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                        if not _retry_sleep(network_retries - 1, cancel_check):
                            _active_llm_response = None
                            return
                        session = requests.Session()
                        continue
                    _active_llm_response = None
                    session.close()
                    yield {"content": f"错误: API返回{status}错误(已重试{_MAX_NETWORK_RETRIES}次)", "finish_reason": "error"}
                    return

                # 成功或其他码
                resp.raise_for_status()
                break

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.SSLError) as e:
                _active_llm_response = None
                session.close()
                if cancel_check and cancel_check():
                    return
                if network_retries < _MAX_NETWORK_RETRIES:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"网络错误(第{network_retries}次重试): {e}")
                    if not _retry_sleep(network_retries - 1, cancel_check):
                        return
                    session = requests.Session()
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"网络错误(重试耗尽): {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

            except requests.exceptions.HTTPError as e:
                _active_llm_response = None
                session.close()
                status = e.response.status_code if e.response is not None else 0

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {e}")
                    yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                    return

                if cancel_check and cancel_check():
                    return

                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

            except Exception as e:
                _active_llm_response = None
                session.close()
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
                return

        # 解析 Anthropic SSE 流（后台线程读，主线程Queue消费）
        _active_llm_response = resp  # 切换到response对象

        try:
            content_buffer = []
            tool_use_blocks = {}  # index → {id, name, input_json}

            line_queue = queue.Queue()
            reader = threading.Thread(
                target=_iter_to_queue,
                args=(resp.iter_lines(decode_unicode=True), line_queue),
                daemon=True,
            )
            reader.start()

            while True:
                try:
                    raw_line = line_queue.get(timeout=0.1)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    continue
                if raw_line is _STREAM_END:
                    break

                line = (raw_line or "").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue

                self._last_raw_sse.append(data_str)

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                dtype = data.get("type", "")

                if dtype == "content_block_start":
                    cb = data.get("content_block", {})
                    idx = data.get("index", 0)
                    ctype = cb.get("type", "")
                    if ctype == "tool_use":
                        tool_use_blocks[idx] = {
                            "id": cb.get("id", ""),
                            "name": cb.get("name", ""),
                            "input_json": "",
                        }

                elif dtype == "content_block_delta":
                    delta = data.get("delta", {})
                    idx = data.get("index", 0)
                    d_type = delta.get("type", "")

                    if d_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            content_buffer.append(text)
                            yield {"content": text}

                    elif d_type == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if idx in tool_use_blocks:
                            tool_use_blocks[idx]["input_json"] += pj

                elif dtype == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason") or "end_turn"
                    if stop_reason:
                        if stop_reason == "end_turn":
                            finish_reason = "stop"
                        elif stop_reason == "tool_use":
                            finish_reason = "tool_calls"
                        elif stop_reason in ("max_tokens", "length"):
                            finish_reason = "max_tokens"
                        elif stop_reason == "content_filter":
                            finish_reason = "content_filter"
                        elif stop_reason == "insufficient_system_resource":
                            finish_reason = "server_busy"
                        else:
                            finish_reason = stop_reason

                        if tool_use_blocks:
                            completed_calls = []
                            for idx in sorted(tool_use_blocks.keys()):
                                tu = tool_use_blocks[idx]
                                completed_calls.append({
                                    "id": tu["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tu["name"],
                                        "arguments": tu["input_json"],
                                    },
                                })
                            yield {"tool_calls": completed_calls, "finish_reason": finish_reason}
                        else:
                            yield {"finish_reason": finish_reason}

                        if self._logger:
                            total_out = len("".join(content_buffer))
                            self._logger.info("core.llm", f"响应完成, content_len={total_out}, stop_reason={stop_reason}")

                    # 捕获usage（Anthropic message_delta 中的 usage）
                    usage = data.get("usage", {})
                    if usage:
                        prompt = usage.get("input_tokens", 0)
                        cached = usage.get("cache_read_input_tokens", 0)
                        yield {"usage": {"prompt_tokens": prompt + cached, "completion_tokens": usage.get("output_tokens", 0), "cached_tokens": cached}}

                elif dtype == "error":
                    err_msg = data.get("error", {}).get("message", "未知错误")
                    yield {"content": f"错误: {err_msg}", "finish_reason": "error"}
                    return
        finally:
            _active_llm_response = None
            session.close()

    # ── 消息格式转换：OpenAI → Anthropic ──

    def _convert_messages(self, messages):
        """
        将 OpenAI 格式 messages 转换为 Anthropic 格式。
        返回 (system_text, anthropic_messages)。
        """
        system_parts = []
        anthropic_msgs = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "user":
                anthropic_msgs.append({"role": "user", "content": content or ""})

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    # assistant 带 tool_calls → 多个 content block
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        try:
                            inp = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": inp,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                else:
                    anthropic_msgs.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                # tool 结果 → 转为 user 消息中的 tool_result block
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                }
                # 合并到相邻的 user 消息
                if anthropic_msgs and anthropic_msgs[-1]["role"] == "user" \
                        and isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].append(tool_result)
                else:
                    anthropic_msgs.append({"role": "user", "content": [tool_result]})

        system_text = "\n\n".join(system_parts) if system_parts else ""
        return system_text, anthropic_msgs

    # ── 工具定义转换：OpenAI → Anthropic ──

    def _convert_tools(self, tool_defs):
        """将 OpenAI 格式工具定义转换为 Anthropic 格式"""
        if not tool_defs:
            return []
        anthropic_tools = []
        for t in tool_defs:
            func = t.get("function", t)
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {
                    "type": "object", "properties": {},
                }),
            })
        return anthropic_tools
