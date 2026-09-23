"""Anthropic 兼容后端 —— httpx 流式请求、消息/工具双向转换与 SSE 解析。

搬运来源：旧包 `narnat_agent/core/llm.py` 541-1107 行（`_AnthropicBackend`）——请求体
组装、重试矩阵、SSE 解析、消息与工具转换逐行对照搬运，算法零改动；事件键集改用
`contracts.llm_events` 常量、合成占位文本改用 `contracts.messages` 常量，共享状态
与重试参数改从 `LLMRuntime` 读取
（对齐 specs/llm「Anthropic 兼容请求体组装」「消息格式转换（OpenAI → Anthropic）」
「工具定义传递与转换」「原始响应取证」「兼容性怪癖保持」等）。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Iterator

import httpx

from ..contracts.llm_events import (
    KEY_ARGUMENTS,
    KEY_CACHED_TOKENS,
    KEY_COMPLETION_TOKENS,
    KEY_CONTENT,
    KEY_FINISH_REASON,
    KEY_FUNCTION,
    KEY_ID,
    KEY_NAME,
    KEY_PROMPT_TOKENS,
    KEY_STREAM_INTERRUPTED,
    KEY_THINKING,
    KEY_THINKING_SIGNATURE,
    KEY_TOOL_CALLS,
    KEY_TYPE,
    KEY_USAGE,
)
from ..contracts.messages import SYNTHETIC_THINKING
from .retry import (
    RETRY_REASON_NETWORK,
    RETRY_REASON_RATE,
    classify_stream_error,
    is_context_overflow,
    is_retryable_http,
    retry_notice,
    retry_sleep,
    server_error_reason,
)
from .runtime import (
    STREAM_END,
    STREAM_POLL_SECONDS,
    STREAM_STALL_SECONDS,
    LLMConfig,
    LLMRuntime,
)

__all__ = ["SYNTHETIC_THINKING", "AnthropicBackend", "user_has_tool_result"]


def user_has_tool_result(user_msg: dict) -> bool:
    """转换后的 user 消息内容块里是否含有 tool_result 块。"""
    content = user_msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


class AnthropicBackend:
    """Anthropic 兼容后端。

    端点: `{base_url}/v1/messages`；认证: `x-api-key` + `anthropic-version`；
    流式: `content_block_start/delta` 事件。内部做 OpenAI ↔ Anthropic 消息格式双向
    转换，对外统一产出 OpenAI 形态事件。
    """

    def __init__(self, config: LLMConfig, runtime: LLMRuntime, logger=None,
                 max_output_tokens: int = 128000,
                 client_factory=None, stall_seconds: float = STREAM_STALL_SECONDS) -> None:
        self._config = config
        self._runtime = runtime
        self._logger = logger
        self._max_output_tokens = max_output_tokens
        self._stall_seconds = stall_seconds
        self._client_factory = client_factory or self._new_client
        self._url = f"{config.base_url.rstrip('/')}/v1/messages"
        self._last_raw_sse: list[str] = []
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _new_client() -> httpx.Client:
        """默认 HTTP 客户端（连接 10s、读 300s、写 60s、连接池 30s）。"""
        return httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=30.0))

    @property
    def raw_sse(self) -> list[str]:
        """本轮请求按到达顺序记录的 `data:` 数据段（读取返回副本）。"""
        return list(self._last_raw_sse)

    def chat_stream(self, messages, no_tools: bool = False, no_thinking: bool = False,
                    cancel_check=None) -> Iterator[dict]:
        """流式请求并产出统一事件流（生成器；首次迭代才真正发请求）。"""
        self._last_raw_sse.clear()
        if self._logger:
            self._logger.info("llm", f"发送请求(Anthropic), messages={len(messages)}条")

        try:
            system, anthropic_msgs = self.convert_messages(messages)
            anthropic_tools = self.convert_tools(self._runtime.tool_defs)
        except Exception as e:
            yield {KEY_CONTENT: f"[错误: 消息格式转换失败: {e}]", KEY_FINISH_REASON: "error"}
            return

        think_body_top, think_extra = self._runtime.thinking_rules.resolve_thinking_params(
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
        if self._config.temperature is not None:
            body["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens

        # 发送请求（带重试，使用 httpx 流式）
        network_retries = 0
        rate_retries = 0

        while True:
            # read 放宽到 300s：思考期可能长时间无字节输出，大上下文 prefill 也会挤占
            # 读超时；流中静默挂死由看门狗（stall_seconds）主动断连触发上层重试
            client = self._client_factory()
            self._runtime.attach_handle(client)
            try:
                # 使用 stream 模式发送请求，先拿到 status_code 再决定是否读取流
                req = client.build_request("POST", self._url, headers=self._headers, json=body)
                resp = client.send(req, stream=True)
                status = resp.status_code

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    # stream 模式需先 read() 才能访问 text（否则抛 ResponseNotRead 掩盖真实错误）
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    self._runtime.detach_handle()
                    # 上下文超限单独归类：溢出恢复路径（压缩→重发）而非普通错误
                    if status == 400 and is_context_overflow(err_text):
                        if self._logger:
                            self._logger.warning("llm", "API返回400: 上下文超限")
                        yield {KEY_FINISH_REASON: "context_overflow"}
                        return
                    if self._logger:
                        self._logger.error("llm", f"API调用失败(不可重试): {status} {err_text[:200]}")
                    yield {KEY_CONTENT: f"[错误: API调用失败({status}): {err_text[:200]}]",
                           KEY_FINISH_REASON: "error"}
                    return

                # 429 速率限制
                if status == 429:
                    resp.close()
                    client.close()
                    if rate_retries < self._runtime.max_retries:
                        rate_retries += 1
                        if self._logger:
                            self._logger.warning("llm", f"API返回429(第{rate_retries}次重试)...")
                        yield retry_notice(rate_retries, self._runtime.max_retries,
                                           RETRY_REASON_RATE)
                        if not retry_sleep(rate_retries - 1, cancel_check):
                            self._runtime.detach_handle()
                            return
                        continue
                    self._runtime.detach_handle()
                    yield {KEY_CONTENT: f"[错误: API返回429速率限制"
                                        f"(已重试{self._runtime.max_retries}次)]",
                           KEY_FINISH_REASON: "error"}
                    return

                # 可重试服务端错误
                if is_retryable_http(status):
                    resp.close()
                    client.close()
                    if network_retries < self._runtime.max_retries:
                        network_retries += 1
                        if self._logger:
                            self._logger.warning(
                                "llm", f"API返回{status}(第{network_retries}次重试)...")
                        yield retry_notice(network_retries, self._runtime.max_retries,
                                           server_error_reason(status))
                        if not retry_sleep(network_retries - 1, cancel_check):
                            self._runtime.detach_handle()
                            return
                        continue
                    self._runtime.detach_handle()
                    yield {KEY_CONTENT: f"[错误: API返回{status}错误"
                                        f"(已重试{self._runtime.max_retries}次)]",
                           KEY_FINISH_REASON: "error"}
                    return

                # 成功
                if status != 200:
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    self._runtime.detach_handle()
                    if self._logger:
                        self._logger.error("llm", f"API调用失败: {status} {err_text[:200]}")
                    yield {KEY_CONTENT: f"[错误: API调用失败({status}): {err_text[:200]}]",
                           KEY_FINISH_REASON: "error"}
                    return
                break

            # TransportError 覆盖 Connect/Read/Write/RemoteProtocol 全部连接类异常，
            # 包括 "Server disconnected without sending a response"（RemoteProtocolError）
            except (httpx.TransportError, httpx.TimeoutException) as e:
                client.close()
                self._runtime.detach_handle()
                if cancel_check and cancel_check():
                    return
                if network_retries < self._runtime.max_retries:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("llm", f"网络错误(第{network_retries}次重试): {e}")
                    yield retry_notice(network_retries, self._runtime.max_retries,
                                       RETRY_REASON_NETWORK)
                    if not retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("llm", f"网络错误(重试耗尽): {e}")
                yield {KEY_CONTENT: f"[错误: API调用失败({type(e).__name__}，"
                                    f"重试{network_retries}次): {e}]",
                       KEY_FINISH_REASON: "error"}
                return

            except Exception as e:
                client.close()
                self._runtime.detach_handle()
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("llm", f"API调用失败: {e}")
                yield {KEY_CONTENT: f"[错误: API调用失败({type(e).__name__}): {e}]",
                       KEY_FINISH_REASON: "error"}
                return

        # 解析 Anthropic SSE 流（resp 已是 stream=True 模式）
        self._runtime.attach_handle(resp)

        try:
            content_buffer = []
            thinking_buffer = []  # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text
            thinking_blocks = {}  # index → 思考文本（思考模式下需回传 API）
            thinking_sigs = {}    # index → 思考块签名（Claude 要求回传时携带，经 signature_delta 送达）
            tool_use_blocks = {}
            _msg_delta_seen = False  # 守卫：防止 message_delta 正常到达后兜底重复 yield
            _start_usage = None      # message_start 中的初始 usage，兜底时补用

            line_queue = queue.Queue()
            stream_err = []          # 流读取异常（服务端中途断开）
            last_data_ts = [None]    # 最近收到数据的时间戳（看门狗用；收到首个事件后才有值）

            def _read_lines():
                """后台线程：从 httpx 流式响应中逐行读取 SSE"""
                try:
                    for line in resp.iter_lines():
                        last_data_ts[0] = time.time()
                        line_queue.put(line)
                except Exception as e:
                    # 看门狗已记录错误时不再重复追加
                    if not stream_err:
                        stream_err.append(e)
                finally:
                    line_queue.put(STREAM_END)

            reader = threading.Thread(target=_read_lines, daemon=True)
            reader.start()

            while True:
                try:
                    line = line_queue.get(timeout=STREAM_POLL_SECONDS)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    # 看门狗：已开始输出后静默超过阈值视为挂死，主动断连触发上层重试；
                    # 首字节前（prefill/思考期）静默由 read 超时兜底，不在此误断
                    if (not stream_err and last_data_ts[0] is not None
                            and time.time() - last_data_ts[0] > self._stall_seconds):
                        stream_err.append(httpx.ReadTimeout(
                            f"流式输出静默超过{self._stall_seconds:.0f}s"))
                        try:
                            resp.close()
                        except Exception:
                            pass
                    continue
                if line is STREAM_END:
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
                        # 兼容层可能返回 cache_read_input_tokens 或 prompt_cache_hit_tokens，双兼容
                        cached = usage.get("cache_read_input_tokens", 0) or usage.get(
                            "prompt_cache_hit_tokens", 0) or 0
                        _start_usage = {
                            KEY_PROMPT_TOKENS: usage.get("input_tokens", 0) + cached,
                            KEY_COMPLETION_TOKENS: 0,
                            KEY_CACHED_TOKENS: cached,
                        }
                    continue

                if dtype == "content_block_start":
                    cb = data.get("content_block", {})
                    idx = data.get("index", 0)
                    ctype = cb.get("type", "")
                    if ctype == "tool_use":
                        tool_use_blocks[idx] = {
                            KEY_ID: cb.get("id", ""),
                            KEY_NAME: cb.get("name", ""),
                            "input_json": "",
                        }
                    elif ctype == "thinking":
                        # 思考块开始：DeepSeek 流式下 signature 为空串且不回填，回传时无需携带
                        thinking_blocks[idx] = ""

                elif dtype == "content_block_delta":
                    delta = data.get("delta", {})
                    idx = data.get("index", 0)
                    d_type = delta.get("type", "")

                    if d_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            content_buffer.append(text)
                            yield {KEY_CONTENT: text}

                    elif d_type == "thinking_delta":
                        # 收集 thinking 内容，用于 thinking-only 回复兜底 + 思考模式回传 API
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            thinking_buffer.append(thinking_text)
                            thinking_blocks[idx] = thinking_blocks.get(idx, "") + thinking_text

                    elif d_type == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if idx in tool_use_blocks:
                            tool_use_blocks[idx]["input_json"] += pj

                    elif d_type == "signature_delta":
                        # Claude：思考块签名经 signature_delta 事件送达（回传思考块必需）
                        sig = delta.get("signature", "")
                        if sig:
                            thinking_sigs[idx] = sig

                elif dtype == "message_delta":
                    _msg_delta_seen = True
                    stop_reason = data.get("delta", {}).get("stop_reason") or "end_turn"
                    thinking_combined = "".join(thinking_blocks[i] for i in sorted(thinking_blocks))
                    # 回传开关关闭：真实思考内容不下发（上层不会存入消息历史）
                    passback_on = getattr(self._config, "thinking_passback", True)
                    thinking_out = thinking_combined if passback_on else None
                    # Claude 签名：仅当恰好一个思考块且签名非空时上报（多块边界无法可靠
                    # 对齐，宁缺勿错）
                    thinking_sig_out = None
                    if len(thinking_blocks) == 1:
                        sig = thinking_sigs.get(next(iter(thinking_blocks)), "")
                        if sig:
                            thinking_sig_out = sig
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
                                    KEY_ID: tu[KEY_ID],
                                    KEY_TYPE: "function",
                                    KEY_FUNCTION: {
                                        KEY_NAME: tu[KEY_NAME],
                                        KEY_ARGUMENTS: tu["input_json"],
                                    },
                                })
                            yield {KEY_TOOL_CALLS: completed_calls,
                                   KEY_FINISH_REASON: finish_reason,
                                   KEY_THINKING: thinking_out,
                                   KEY_THINKING_SIGNATURE: thinking_sig_out}
                        else:
                            # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text，
                            # 此时将 thinking 内容作为正式输出
                            if not content_buffer and thinking_buffer:
                                fallback_text = "".join(thinking_buffer)
                                content_buffer.append(fallback_text)
                                yield {KEY_CONTENT: fallback_text}
                                if self._logger:
                                    self._logger.info(
                                        "llm",
                                        f"thinking-only兜底: 将thinking内容"
                                        f"({len(fallback_text)}字符)作为text输出")
                                # thinking 已作为 text 输出，避免下一轮回传时重复占用上下文
                                yield {KEY_FINISH_REASON: finish_reason,
                                       KEY_THINKING: "",
                                       KEY_THINKING_SIGNATURE: None}
                            else:
                                yield {KEY_FINISH_REASON: finish_reason,
                                       KEY_THINKING: thinking_out,
                                       KEY_THINKING_SIGNATURE: thinking_sig_out}

                        if self._logger:
                            total_out = len("".join(content_buffer))
                            self._logger.info(
                                "llm",
                                f"响应完成, content_len={total_out}, stop_reason={stop_reason}")

                    # 捕获 usage
                    usage = data.get("usage", {})
                    if usage:
                        prompt = usage.get("input_tokens", 0)
                        cached = usage.get("cache_read_input_tokens", 0) or usage.get(
                            "prompt_cache_hit_tokens", 0) or 0
                        yield {KEY_USAGE: {
                            KEY_PROMPT_TOKENS: prompt + cached,
                            KEY_COMPLETION_TOKENS: usage.get("output_tokens", 0),
                            KEY_CACHED_TOKENS: cached,
                        }}

                elif dtype == "error":
                    err_msg = data.get("error", {}).get("message", "未知错误")
                    yield {KEY_CONTENT: f"[错误: {err_msg}]", KEY_FINISH_REASON: "error"}
                    return

            # ── 流中断：服务端中途断开且未收到完成标记 ──
            # 不 yield 虚假完成标记；上报中断信息，由上层决定整轮重试
            if stream_err and not _msg_delta_seen:
                if self._logger:
                    self._logger.warning("llm", f"响应流中断: {stream_err[0]}")
                yield {KEY_STREAM_INTERRUPTED: classify_stream_error(stream_err[0])}

            # ── 兜底：流正常结束但未收到 message_delta（DeepSeek 偶发漏发）──
            # 此时缓冲区可能已有完整内容，直接使用
            elif not _msg_delta_seen:
                if _start_usage:
                    yield {KEY_USAGE: _start_usage}
                thinking_combined = "".join(thinking_blocks[i] for i in sorted(thinking_blocks))
                # 回传开关关闭：真实思考内容不下发
                passback_on = getattr(self._config, "thinking_passback", True)
                thinking_out = thinking_combined if passback_on else None
                thinking_sig_out = None
                if len(thinking_blocks) == 1:
                    sig = thinking_sigs.get(next(iter(thinking_blocks)), "")
                    if sig:
                        thinking_sig_out = sig
                if tool_use_blocks:
                    completed_calls = []
                    for idx in sorted(tool_use_blocks.keys()):
                        tu = tool_use_blocks[idx]
                        if tu[KEY_ID] and tu[KEY_NAME]:
                            completed_calls.append({
                                KEY_ID: tu[KEY_ID],
                                KEY_TYPE: "function",
                                KEY_FUNCTION: {
                                    KEY_NAME: tu[KEY_NAME],
                                    KEY_ARGUMENTS: tu["input_json"],
                                },
                            })
                    if completed_calls:
                        yield {KEY_TOOL_CALLS: completed_calls,
                               KEY_FINISH_REASON: "tool_calls",
                               KEY_THINKING: thinking_out,
                               KEY_THINKING_SIGNATURE: thinking_sig_out}
                        if self._logger:
                            self._logger.info(
                                "llm",
                                f"兜底: 未收到message_delta，从缓冲区提取"
                                f"{len(completed_calls)}个工具调用",
                            )
                elif not content_buffer and thinking_buffer:
                    fallback_text = "".join(thinking_buffer)
                    content_buffer.append(fallback_text)
                    yield {KEY_CONTENT: fallback_text}
                    yield {KEY_FINISH_REASON: "stop", KEY_THINKING: "",
                           KEY_THINKING_SIGNATURE: None}
                    if self._logger:
                        self._logger.info(
                            "llm",
                            f"兜底: 未收到message_delta，将thinking({len(fallback_text)}字符)"
                            f"作为text输出",
                        )
                elif content_buffer:
                    yield {KEY_FINISH_REASON: "stop",
                           KEY_THINKING: thinking_out,
                           KEY_THINKING_SIGNATURE: thinking_sig_out}
                    if self._logger:
                        self._logger.info(
                            "llm",
                            f"兜底: 未收到message_delta，但已有文字内容"
                            f"({len(''.join(content_buffer))}字符)",
                        )

        finally:
            self._runtime.detach_handle()
            resp.close()
            client.close()

    # ── 消息格式转换：OpenAI → Anthropic ──

    def convert_messages(self, messages):
        """内部消息数组 → `(system_text, anthropic_msgs)`（对齐 specs/llm 转换规则）。

        - `system` 消息内容抽为顶层系统文本（多条以空行连接、空内容跳过）；
        - 连续纯文本 `user` 合并为一条（前一条含 tool_result 时不合并）；
        - `assistant` 转为内容块序列 `[思考块, 文本块, 工具调用块…]`（thinking 回传按
          `协议×模型` 查表判定：`thinking_block_signed` 缺签名则整块省略）；
        - `tool` 消息转为 tool_result 块并追加到前一条 `user` 消息的内容列表。
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
                content = content or ""
                prev = anthropic_msgs[-1] if anthropic_msgs else None
                if (content and prev is not None and prev["role"] == "user"
                        and not user_has_tool_result(prev)):
                    # 合并纯文本连续 user（Anthropic 强制角色交替）。
                    # 含 tool_result 的 user 消息不合并：校验要求 tool_result 必须紧邻
                    # tool_use（同消息插入 text 会破坏校验）
                    if isinstance(prev["content"], list):
                        prev["content"].append({"type": "text", "text": content})
                    else:
                        prev["content"] = [
                            {"type": "text", "text": prev["content"]},
                            {"type": "text", "text": content},
                        ]
                else:
                    anthropic_msgs.append({"role": "user", "content": content})

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                # 思考块回传按"协议+模型"查表：
                # - thinking_block（DeepSeek/MiMo anthropic）：回传 thinking 块，API 强制否则 400
                # - thinking_block_signed（Claude）：回传块且必须携带签名；未捕获签名
                #   （多思考块/无签名）→ 省略该块的思考回传（安全，Claude 不报错）
                # 思考回传开关关闭：彻底删除 thinking 段，一刀切。
                thinking = None
                sig = None
                if self._config.thinking_enabled and getattr(
                        self._config, "thinking_passback", True):
                    mode = self._runtime.thinking_rules.resolve_thinking_passback(
                        "anthropic", self._config.model)
                    if mode in ("thinking_block", "thinking_block_signed"):
                        t = msg.get(KEY_THINKING)
                        if t is not None:
                            # 例外：repair 合成占位（SYNTHETIC_THINKING）无 tool_calls 但必须
                            # 回传——DeepSeek 校验请求尾部 assistant 携带非空 thinking 块。
                            if tool_calls or t == SYNTHETIC_THINKING:
                                if mode == "thinking_block_signed":
                                    s = msg.get(KEY_THINKING_SIGNATURE)
                                    if s:
                                        thinking, sig = t, s
                                else:
                                    thinking = t  # DeepSeek：无需签名（流式下签名恒空）
                if tool_calls:
                    blocks = []
                    if thinking is not None:
                        blk = {"type": "thinking", "thinking": thinking}
                        if sig:
                            blk["signature"] = sig
                        blocks.append(blk)
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        func = tc.get(KEY_FUNCTION, {})
                        try:
                            inp = json.loads(func.get(KEY_ARGUMENTS, "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            KEY_ID: tc.get(KEY_ID, ""),
                            KEY_NAME: func.get(KEY_NAME, ""),
                            "input": inp,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                elif thinking is not None:
                    blocks = [{"type": "thinking", "thinking": thinking}]
                    if sig:
                        blocks[0]["signature"] = sig
                    if content:
                        blocks.append({"type": "text", "text": content})
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

    def convert_tools(self, tool_defs):
        """工具定义列表 → Anthropic 形态（`input_schema` 取 `parameters`，缺省空对象模式）。"""
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
