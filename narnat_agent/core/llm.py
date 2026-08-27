"""
LLM调用层 —— 双协议支持（OpenAI兼容 + Anthropic兼容），流式输出，token计数

通过 AIConfig.protocol 显式选择协议：
- "anthropic" → AnthropicBackend（/v1/messages 端点，x-api-key 认证）
- "openai"    → OpenAIBackend（/chat/completions 端点，Bearer 认证）

上层统一使用 OpenAI 格式的 messages/tool_calls，AnthropicBackend 内部做双向转换。
Thinking 参数通过 THINKING_PARAM_MAP 映射表动态构造，不再硬编码。
两个后端统一使用 httpx，无 requests 依赖。
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
from ..config.defaults import resolve_thinking_params
from .interrupt import register_abort


def _strip_surrogates(obj):
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_surrogates(v) for v in obj]
    return obj

# ESC中断：持有当前活跃的LLM HTTP连接
_active_llm_response = None

# 队列哨兵值
_STREAM_END = object()

# 重试参数
_MAX_NETWORK_RETRIES = 3   # 网络/服务端错误（可通过set_retry_count修改）
_MAX_RATE_RETRIES = 5      # 429 速率限制
_RETRY_BACKOFF_BASE = [1, 2, 4, 8, 8]


def set_retry_count(n: int) -> None:
    """设置LLM网络重试次数（由Agent初始化时从配置读取）"""
    global _MAX_NETWORK_RETRIES
    _MAX_NETWORK_RETRIES = max(1, min(n, 10))  # 限制1-10


def _retry_sleep(attempt: int, cancel_check=None) -> bool:
    """指数退避休眠，带 jitter 和中断检查。返回 False 表示用户取消。"""
    base = _RETRY_BACKOFF_BASE[min(attempt, len(_RETRY_BACKOFF_BASE) - 1)]
    jitter = base * 0.25 * (random.random() * 2 - 1)
    sleep_time = max(0, base + jitter)
    deadline = time.time() + sleep_time
    while time.time() < deadline:
        if cancel_check and cancel_check():
            return False
        time.sleep(min(0.2, deadline - time.time()))
    return True


def _is_retryable_http(status: int) -> bool:
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


register_abort(abort_active_llm_request)


def _iter_to_queue(iterator, q, err_box=None):
    """后台线程：将迭代器的元素逐个放入队列，最后放入_STREAM_END。

    迭代异常（如服务端中途断开）记录到 err_box，供主线程判断流是否被中断；
    不再静默吞掉，避免把截断响应当作正常完成。
    """
    try:
        for item in iterator:
            q.put(item)
    except Exception as e:
        if err_box is not None:
            err_box.append(e)
    finally:
        q.put(_STREAM_END)


# ═══════════════════════════════════════════════════════════════
# LLM 客户端
# ═══════════════════════════════════════════════════════════════

class LLMClient:
    """LLM客户端，通过 config.protocol 选择 OpenAI 或 Anthropic 协议。"""

    def __init__(self, config: AIConfig, logger=None, max_output_tokens: int = 128000,
                 tool_definitions: list = None):
        self._config = config
        self._logger = logger
        # 复制一份：set_goal_tool 动态增删 GoalComplete 时不影响调用方持有的原列表
        self._tool_defs = list(tool_definitions or [])
        self._max_output_tokens = max_output_tokens

        # 协议由 config.protocol 显式指定
        protocol = config.protocol
        self._protocol = protocol

        if protocol == "anthropic":
            self._backend = _AnthropicBackend(config, self._tool_defs, logger, max_output_tokens)
        else:
            self._backend = _OpenAIBackend(config, self._tool_defs, logger)

    def chat_stream(self, messages: List[Dict[str, Any]], no_tools: bool = False,
                    no_thinking: bool = False, cancel_check=None) -> Iterator:
        return self._backend.chat_stream(_strip_surrogates(messages), no_tools=no_tools,
                                         no_thinking=no_thinking, cancel_check=cancel_check)

    def set_goal_tool(self, enabled: bool) -> None:
        """动态注入/移除 GoalComplete 工具定义。

        目标模式（/goal on）开启时注入，关闭时移除；普通模式不向 LLM 暴露该工具。
        幂等：重复开启/关闭不会重复添加或报错。
        """
        # 局部导入避免模块顶层循环依赖
        from ..tools.goal_complete import DEFINITION as _GOAL_DEF
        name = _GOAL_DEF["function"]["name"]
        exists = any(
            d.get("function", {}).get("name") == name
            for d in self._tool_defs
        )
        if enabled and not exists:
            self._tool_defs.append(_GOAL_DEF)
        elif not enabled and exists:
            self._tool_defs[:] = [
                d for d in self._tool_defs
                if d.get("function", {}).get("name") != name
            ]

    @property
    def raw_sse(self):
        if hasattr(self._backend, '_last_raw_sse'):
            return list(self._backend._last_raw_sse)
        return None


# ═══════════════════════════════════════════════════════════════
# OpenAI 兼容后端
# ═══════════════════════════════════════════════════════════════

class _OpenAIBackend:
    """OpenAI SDK 后端"""

    def __init__(self, config, tool_defs, logger):
        from openai import OpenAI
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=0,
        )

    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None):
        if self._logger:
            self._logger.info("core.llm", f"发送请求(OpenAI), messages={len(messages)}条")

        global _active_llm_response
        from openai import APIStatusError, APIConnectionError, APITimeoutError

        network_retries = 0
        rate_retries = 0
        stream = None

        while True:
            _active_llm_response = self._client
            try:
                # 动态构造 thinking 参数（不再硬编码）
                think_body_top, think_extra = resolve_thinking_params(
                    "openai", self._config.model,
                    self._config.thinking_enabled and not no_thinking,
                    self._config.thinking_effort,
                )
                kwargs = dict(
                    model=self._config.model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    # read 放宽到 120s：thinking 模式下思考期可能长时间无字节输出
                    timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=30.0),
                )
                kwargs.update(think_body_top)
                if think_extra:
                    kwargs["extra_body"] = think_extra
                if not no_tools:
                    kwargs["tools"] = self._tool_defs
                # thinking 模式下 temperature 不生效，传入会误导用户
                if self._config.temperature is not None:
                    kwargs["temperature"] = self._config.temperature
                if self._config.max_tokens is not None:
                    kwargs["max_tokens"] = self._config.max_tokens
                stream = self._client.chat.completions.create(**kwargs)
                break

            except APIStatusError as e:
                _active_llm_response = None
                status = e.status_code
                if status in (400, 401, 403, 404, 422):
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {e}")
                    yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                    return
                if cancel_check and cancel_check():
                    return
                if status == 429 and rate_retries < _MAX_RATE_RETRIES:
                    rate_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                    if not _retry_sleep(rate_retries - 1, cancel_check):
                        return
                    continue
                if _is_retryable_http(status) and network_retries < _MAX_NETWORK_RETRIES:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                    if not _retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败(重试耗尽): {e}")
                yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                return

            except (APIConnectionError, APITimeoutError) as e:
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
                yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                return

            except Exception as e:
                _active_llm_response = None
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                return

        _active_llm_response = stream

        try:
            tool_calls_buffer = {}
            _index_to_id = {}
            content_buffer = []
            _tc_idx = 0

            chunk_queue = queue.Queue()
            stream_err = []   # 流迭代异常（服务端中途断开）
            received_finish = False
            reader = threading.Thread(
                target=_iter_to_queue, args=(iter(stream), chunk_queue, stream_err), daemon=True)
            reader.start()

            while True:
                try:
                    chunk = chunk_queue.get(timeout=0.05)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    continue
                if chunk is _STREAM_END:
                    break

                usage = getattr(chunk, 'usage', None)
                if usage:
                    cached = 0
                    details = getattr(usage, 'prompt_tokens_details', None)
                    if details:
                        cached = getattr(details, 'cached_tokens', 0)
                    if not cached:
                        # DeepSeek: 缓存命中数在顶层 prompt_cache_hit_tokens（OpenAI 原生无此字段）
                        cached = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
                        if not cached:
                            cached = (getattr(usage, 'model_extra', None) or {}).get('prompt_cache_hit_tokens', 0) or 0
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
                    received_finish = True
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

            # ── 流中断检测：迭代器异常退出且未收到完成标记 ──
            # 不 yield 虚假完成标记，由上层（agent_loop）决定整轮重试
            if stream_err and not received_finish:
                if self._logger:
                    self._logger.warning("core.llm", f"响应流中断: {stream_err[0]}")
        finally:
            _active_llm_response = None


# ═══════════════════════════════════════════════════════════════
# Anthropic 兼容后端（httpx 实现，无 requests 依赖）
# ═══════════════════════════════════════════════════════════════

class _AnthropicBackend:
    """
    Anthropic 兼容后端。

    端点: {base_url}/v1/messages
    认证: x-api-key + anthropic-version
    流式: content_block_start/delta 事件

    内部做 OpenAI ↔ Anthropic 消息格式双向转换，
    对外统一 yield OpenAI 格式的 chunk。
    使用 httpx 替代 requests，统一HTTP客户端依赖。
    """

    def __init__(self, config, tool_defs, logger, max_output_tokens=128000):
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._max_output_tokens = max_output_tokens
        self._url = f"{config.base_url.rstrip('/')}/v1/messages"
        self._last_raw_sse = []
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None):
        self._last_raw_sse.clear()
        if self._logger:
            self._logger.info("core.llm", f"发送请求(Anthropic), messages={len(messages)}条")

        # 转换消息格式
        try:
            system, anthropic_msgs = self._convert_messages(messages)
            anthropic_tools = self._convert_tools(self._tool_defs)
        except Exception as e:
            yield {"content": f"[错误: 消息格式转换失败: {e}]", "finish_reason": "error"}
            return

        # 动态构造 thinking 参数
        think_body_top, think_extra = resolve_thinking_params(
            "anthropic", self._config.model,
            self._config.thinking_enabled and not no_thinking,
            self._config.thinking_effort,
        )
        body = {
            "model": self._config.model,
            "messages": anthropic_msgs,
            "max_tokens": self._max_output_tokens,
            "stream": True,
        }
        # Anthropic 协议下 body_top 和 extra_body 都合并到 body 顶层
        body.update(think_body_top)
        body.update(think_extra)
        if system:
            body["system"] = system
        if anthropic_tools and not no_tools:
            body["tools"] = anthropic_tools
        # thinking 模式下 temperature 不生效，传入会误导用户
        if self._config.temperature is not None:
            body["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens

        # 发送请求（带重试，使用 httpx 流式）
        global _active_llm_response
        network_retries = 0
        rate_retries = 0

        while True:
            client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0))
            _active_llm_response = client
            try:
                # 使用 stream 模式发送请求，先拿到 status_code 再决定是否读取流
                req = client.build_request("POST", self._url, headers=self._headers, json=body)
                resp = client.send(req, stream=True)
                status = resp.status_code

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    err_text = resp.text
                    resp.close()
                    client.close()
                    _active_llm_response = None
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {status} {err_text[:200]}")
                    yield {"content": f"[错误: API调用失败({status}): {err_text[:200]}]", "finish_reason": "error"}
                    return

                # 429 速率限制
                if status == 429:
                    resp.close()
                    client.close()
                    if rate_retries < _MAX_RATE_RETRIES:
                        rate_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                        if not _retry_sleep(rate_retries - 1, cancel_check):
                            _active_llm_response = None
                            return
                        continue
                    _active_llm_response = None
                    yield {"content": f"[错误: API返回429速率限制(已重试{_MAX_RATE_RETRIES}次)]", "finish_reason": "error"}
                    return

                # 可重试服务端错误
                if _is_retryable_http(status):
                    resp.close()
                    client.close()
                    if network_retries < _MAX_NETWORK_RETRIES:
                        network_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                        if not _retry_sleep(network_retries - 1, cancel_check):
                            _active_llm_response = None
                            return
                        continue
                    _active_llm_response = None
                    yield {"content": f"[错误: API返回{status}错误(已重试{_MAX_NETWORK_RETRIES}次)]", "finish_reason": "error"}
                    return

                # 成功
                if status != 200:
                    err_text = resp.text
                    resp.close()
                    client.close()
                    _active_llm_response = None
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败: {status} {err_text[:200]}")
                    yield {"content": f"[错误: API调用失败({status}): {err_text[:200]}]", "finish_reason": "error"}
                    return
                break

            # TransportError 覆盖 Connect/Read/Write/RemoteProtocol 全部连接类异常，
            # 包括 "Server disconnected without sending a response"（RemoteProtocolError）
            except (httpx.TransportError, httpx.TimeoutException) as e:
                client.close()
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
                yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                return

            except Exception as e:
                client.close()
                _active_llm_response = None
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": f"[错误: API调用失败: {e}]", "finish_reason": "error"}
                return

        # 解析 Anthropic SSE 流（resp 已是 stream=True 模式）
        _active_llm_response = resp

        try:
            content_buffer = []
            thinking_buffer = []  # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text
            tool_use_blocks = {}
            _msg_delta_seen = False  # 守卫：防止 message_delta 正常到达后兜底重复 yield
            _start_usage = None     # message_start 中的初始 usage，兜底时补用

            line_queue = queue.Queue()
            stream_err = []  # 流读取异常（服务端中途断开）

            def _read_lines():
                """后台线程：从 httpx 流式响应中逐行读取 SSE"""
                try:
                    for line in resp.iter_lines():
                        line_queue.put(line)
                except Exception as e:
                    stream_err.append(e)
                finally:
                    line_queue.put(_STREAM_END)

            reader = threading.Thread(target=_read_lines, daemon=True)
            reader.start()

            while True:
                try:
                    line = line_queue.get(timeout=0.05)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    continue
                if line is _STREAM_END:
                    break

                line = (line or "").strip()
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

                if dtype == "message_start":
                    # 捕获初始 usage，供 message_delta 缺失时兜底
                    msg = data.get("message", {})
                    usage = msg.get("usage", {})
                    if usage:
                        # DeepSeek 兼容层可能返回 cache_read_input_tokens 或 prompt_cache_hit_tokens，双兼容
                        cached = usage.get("cache_read_input_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0
                        _start_usage = {
                            "prompt_tokens": usage.get("input_tokens", 0) + cached,
                            "completion_tokens": 0,
                            "cached_tokens": cached,
                        }
                    continue

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

                    elif d_type == "thinking_delta":
                        # 收集 thinking 内容，用于 thinking-only 回复兜底
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            thinking_buffer.append(thinking_text)

                    elif d_type == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if idx in tool_use_blocks:
                            tool_use_blocks[idx]["input_json"] += pj

                elif dtype == "message_delta":
                    _msg_delta_seen = True
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
                            # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text
                            # 此时将 thinking 内容作为正式输出
                            if not content_buffer and thinking_buffer:
                                fallback_text = "".join(thinking_buffer)
                                content_buffer.append(fallback_text)
                                yield {"content": fallback_text}
                                if self._logger:
                                    self._logger.info("core.llm", f"thinking-only兜底: 将thinking内容({len(fallback_text)}字符)作为text输出")
                            yield {"finish_reason": finish_reason}

                        if self._logger:
                            total_out = len("".join(content_buffer))
                            self._logger.info("core.llm", f"响应完成, content_len={total_out}, stop_reason={stop_reason}")

                    # 捕获usage
                    usage = data.get("usage", {})
                    if usage:
                        prompt = usage.get("input_tokens", 0)
                        # DeepSeek 兼容层可能返回 cache_read_input_tokens 或 prompt_cache_hit_tokens，双兼容
                        cached = usage.get("cache_read_input_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0
                        yield {"usage": {"prompt_tokens": prompt + cached, "completion_tokens": usage.get("output_tokens", 0), "cached_tokens": cached}}

                elif dtype == "error":
                    err_msg = data.get("error", {}).get("message", "未知错误")
                    yield {"content": f"[错误: {err_msg}]", "finish_reason": "error"}
                    return

            # ── 流中断：服务端中途断开且未收到完成标记 ──
            # 不 yield 虚假完成标记，由上层（agent_loop）决定整轮重试
            if stream_err and not _msg_delta_seen:
                if self._logger:
                    self._logger.warning("core.llm", f"响应流中断: {stream_err[0]}")

            # ── 兜底：流正常结束但未收到 message_delta（DeepSeek 偶发漏发）──
            # 此时缓冲区可能已有完整内容，直接使用
            elif not _msg_delta_seen:
                if _start_usage:
                    yield {"usage": _start_usage}
                if tool_use_blocks:
                    completed_calls = []
                    for idx in sorted(tool_use_blocks.keys()):
                        tu = tool_use_blocks[idx]
                        if tu["id"] and tu["name"]:
                            completed_calls.append({
                                "id": tu["id"],
                                "type": "function",
                                "function": {
                                    "name": tu["name"],
                                    "arguments": tu["input_json"],
                                },
                            })
                    if completed_calls:
                        yield {"tool_calls": completed_calls, "finish_reason": "tool_calls"}
                        if self._logger:
                            self._logger.info(
                                "core.llm",
                                f"兜底: 未收到message_delta，从缓冲区提取{len(completed_calls)}个工具调用",
                            )
                elif not content_buffer and thinking_buffer:
                    fallback_text = "".join(thinking_buffer)
                    content_buffer.append(fallback_text)
                    yield {"content": fallback_text}
                    yield {"finish_reason": "stop"}
                    if self._logger:
                        self._logger.info(
                            "core.llm",
                            f"兜底: 未收到message_delta，将thinking({len(fallback_text)}字符)作为text输出",
                        )
                elif content_buffer:
                    yield {"finish_reason": "stop"}
                    if self._logger:
                        self._logger.info(
                            "core.llm",
                            f"兜底: 未收到message_delta，但已有文字内容({len(''.join(content_buffer))}字符)",
                        )

        finally:
            _active_llm_response = None
            resp.close()
            client.close()

    # ── 消息格式转换：OpenAI → Anthropic ──

    def _convert_messages(self, messages):
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
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                }
                if anthropic_msgs and anthropic_msgs[-1]["role"] == "user" \
                        and isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].append(tool_result)
                else:
                    anthropic_msgs.append({"role": "user", "content": [tool_result]})

        system_text = "\n\n".join(system_parts) if system_parts else ""
        return system_text, anthropic_msgs

    # ── 工具定义转换：OpenAI → Anthropic ──

    def _convert_tools(self, tool_defs):
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
