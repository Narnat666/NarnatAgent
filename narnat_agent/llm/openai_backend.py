"""OpenAI 兼容后端 —— openai SDK 流式请求与事件解析。

搬运来源：旧包 `narnat_agent/core/llm.py` 258-538 行（`_OpenAIBackend`）——请求构建、
重试矩阵、流解析、思考回传与完成事件逐行对照搬运，算法零改动；事件键集改用
`contracts.llm_events` 常量，共享状态与重试参数改从 `LLMRuntime` 读取
（对齐 specs/llm「协议选择与请求构建」「文本与思考事件流」「工具调用流式聚合」
「用量事件归一」「完成原因取值与协议映射」「重试、退避与重试通知」「错误事件与文案」
「上下文超限识别」「流中断上报与静默挂死检测」「取消与中断」「兼容性怪癖保持」）。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Iterator

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
    iter_to_queue,
)

__all__ = ["OpenAIBackend"]


class OpenAIBackend:
    """OpenAI 兼容后端（`openai` SDK 客户端；底层自动重试关闭，重试自管）。"""

    def __init__(self, config: LLMConfig, runtime: LLMRuntime, logger=None,
                 client: Any = None, stall_seconds: float = STREAM_STALL_SECONDS) -> None:
        self._config = config
        self._runtime = runtime
        self._logger = logger
        self._stall_seconds = stall_seconds
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                max_retries=0,
            )

    def prepare_messages(self, messages):
        """内部消息 → OpenAI 协议请求消息。

        内部 assistant 消息可携带 thinking 字段（思考回传的通用内部表示）：
        - `reasoning_content`（DeepSeek/Kimi/GLM/MiMo 查表命中）：assistant 含
          tool_calls 且思考非空时才把 thinking 写为顶层 `reasoning_content`；
        - 其余（`none` / 纯文本轮 / 开关关闭）：剥离 `thinking` 与
          `thinking_signature` 字段（省 token，对齐 specs/llm「思考回传格式与开关」）。
        """
        use_rc = (
            self._config.thinking_enabled
            and getattr(self._config, "thinking_passback", True)
            and self._runtime.thinking_rules.resolve_thinking_passback(
                "openai", self._config.model) == "reasoning_content"
        )
        out = []
        for m in messages:
            m = dict(m)
            thinking = m.pop(KEY_THINKING, None)
            m.pop(KEY_THINKING_SIGNATURE, None)
            if (use_rc and m.get("role") == "assistant"
                    and m.get("tool_calls") and thinking is not None):
                m["reasoning_content"] = thinking
            out.append(m)
        return out

    def chat_stream(self, messages, no_tools: bool = False, no_thinking: bool = False,
                    cancel_check=None) -> Iterator[dict]:
        """流式请求并产出统一事件流（生成器；首次迭代才真正发请求）。

        `no_tools=True` 不带工具定义、`no_thinking=True` 走 thinking 禁用分支；
        两者均不影响思考回传开关的判定与完成事件中的思考输出（specs/llm
        「请求裁剪选项」）。
        """
        if self._logger:
            self._logger.info("llm", f"发送请求(OpenAI), messages={len(messages)}条")

        messages = self.prepare_messages(messages)

        from openai import APIConnectionError, APIStatusError, APITimeoutError

        network_retries = 0
        rate_retries = 0
        stream = None

        while True:
            self._runtime.attach_handle(self._client)
            try:
                think_body_top, think_extra = self._runtime.thinking_rules.resolve_thinking_params(
                    "openai", self._config.model,
                    self._config.thinking_enabled and not no_thinking,
                    self._config.thinking_effort,
                )
                kwargs = dict(
                    model=self._config.model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    # read 放宽到 300s：思考期可能长时间无字节输出，大上下文 prefill
                    # 也会挤占读超时；已开始输出后的流中静默由看门狗负责检测
                    timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=30.0),
                )
                kwargs.update(think_body_top)
                if think_extra:
                    kwargs["extra_body"] = think_extra
                if not no_tools:
                    kwargs["tools"] = self._runtime.tool_defs
                if self._config.temperature is not None:
                    kwargs["temperature"] = self._config.temperature
                if self._config.max_tokens is not None:
                    kwargs["max_tokens"] = self._config.max_tokens
                stream = self._client.chat.completions.create(**kwargs)
                break

            except APIStatusError as e:
                self._runtime.detach_handle()
                status = e.status_code
                if status in (400, 401, 403, 404, 422):
                    # 上下文超限单独归类：溢出恢复路径（压缩→重发）而非普通错误
                    if status == 400 and is_context_overflow(
                            f"{e}{getattr(e, 'body', '')}"):
                        if self._logger:
                            self._logger.warning("llm", "API返回400: 上下文超限")
                        yield {KEY_FINISH_REASON: "context_overflow"}
                        return
                    if self._logger:
                        self._logger.error("llm", f"API调用失败(不可重试): {e}")
                    yield {KEY_CONTENT: f"[错误: API调用失败({type(e).__name__}): {e}]",
                           KEY_FINISH_REASON: "error"}
                    return
                if cancel_check and cancel_check():
                    return
                if status == 429 and rate_retries < self._runtime.max_retries:
                    rate_retries += 1
                    if self._logger:
                        self._logger.warning("llm", f"API返回429(第{rate_retries}次重试)...")
                    yield retry_notice(rate_retries, self._runtime.max_retries, RETRY_REASON_RATE)
                    if not retry_sleep(rate_retries - 1, cancel_check):
                        return
                    continue
                if is_retryable_http(status) and network_retries < self._runtime.max_retries:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("llm", f"API返回{status}(第{network_retries}次重试)...")
                    yield retry_notice(network_retries, self._runtime.max_retries,
                                       server_error_reason(status))
                    if not retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("llm", f"API调用失败(重试耗尽): {e}")
                yield {KEY_CONTENT: f"[错误: API调用失败({type(e).__name__}，"
                                    f"重试{network_retries}次): {e}]",
                       KEY_FINISH_REASON: "error"}
                return

            except (APIConnectionError, APITimeoutError) as e:
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
                self._runtime.detach_handle()
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("llm", f"API调用失败: {e}")
                yield {KEY_CONTENT: f"[错误: API调用失败({type(e).__name__}): {e}]",
                       KEY_FINISH_REASON: "error"}
                return

        self._runtime.attach_handle(stream)

        try:
            tool_calls_buffer = {}
            _index_to_id = {}
            content_buffer = []
            reasoning_buffer = []
            _tc_idx = 0

            chunk_queue = queue.Queue()
            stream_err = []   # 流迭代异常（服务端中途断开）
            received_finish = False
            last_data_ts = [None]  # 最近收到数据的时间戳（看门狗用；收到首个chunk后才有值）
            reader = threading.Thread(
                target=iter_to_queue,
                args=(iter(stream), chunk_queue, stream_err, last_data_ts),
                daemon=True)
            reader.start()

            while True:
                try:
                    chunk = chunk_queue.get(timeout=STREAM_POLL_SECONDS)
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
                            stream.close()
                        except Exception:
                            pass
                    continue
                if chunk is STREAM_END:
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
                            cached = (getattr(usage, 'model_extra', None) or {}).get(
                                'prompt_cache_hit_tokens', 0) or 0
                    yield {
                        KEY_USAGE: {
                            KEY_PROMPT_TOKENS: usage.prompt_tokens,
                            KEY_COMPLETION_TOKENS: usage.completion_tokens,
                            KEY_CACHED_TOKENS: cached,
                        }
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if delta.content:
                    content_buffer.append(delta.content)
                    yield {KEY_CONTENT: delta.content}

                # 捕获 reasoning_content（DeepSeek/Kimi 思考模式：多轮对话需回传）
                # SDK 对非原生字段可能放进 model_extra，双路兼容
                rc = getattr(delta, 'reasoning_content', None)
                if rc is None:
                    rc = (getattr(delta, 'model_extra', None) or {}).get('reasoning_content')
                if rc:
                    reasoning_buffer.append(rc)

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
                            tc_id, {KEY_ID: tc_id, KEY_NAME: "", KEY_ARGUMENTS: ""},
                        )
                        if tc.function.name:
                            buf[KEY_NAME] += tc.function.name
                        if tc.function.arguments:
                            buf[KEY_ARGUMENTS] += tc.function.arguments

                if finish_reason:
                    received_finish = True
                    # 思考回传开关 + 查表：reasoning_content 格式时随完成标记上报思考
                    rc_on = (
                        self._config.thinking_enabled
                        and getattr(self._config, "thinking_passback", True)
                        and self._runtime.thinking_rules.resolve_thinking_passback(
                            "openai", self._config.model) == "reasoning_content"
                    )
                    thinking_out = "".join(reasoning_buffer) if rc_on else None
                    if tool_calls_buffer:
                        completed_calls = []
                        for tc_id, buf in tool_calls_buffer.items():
                            completed_calls.append({
                                KEY_ID: buf[KEY_ID],
                                KEY_TYPE: "function",
                                KEY_FUNCTION: {
                                    KEY_NAME: buf[KEY_NAME],
                                    KEY_ARGUMENTS: buf[KEY_ARGUMENTS],
                                },
                            })
                        yield {KEY_TOOL_CALLS: completed_calls,
                               KEY_FINISH_REASON: finish_reason,
                               KEY_THINKING: thinking_out}
                    else:
                        yield {KEY_FINISH_REASON: finish_reason, KEY_THINKING: thinking_out}

                    if self._logger:
                        total_out = len("".join(content_buffer))
                        self._logger.info("llm", f"响应完成, content_len={total_out}")

            # ── 流中断检测：迭代器异常退出且未收到完成标记 ──
            # 不 yield 虚假完成标记；上报中断信息，由上层决定整轮重试
            if stream_err and not received_finish:
                if self._logger:
                    self._logger.warning("llm", f"响应流中断: {stream_err[0]}")
                yield {KEY_STREAM_INTERRUPTED: classify_stream_error(stream_err[0])}
        finally:
            self._runtime.detach_handle()
