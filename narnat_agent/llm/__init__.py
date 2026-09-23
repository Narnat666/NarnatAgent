"""`llm` 积木 —— LLM 双协议客户端（OpenAI 兼容 / Anthropic 兼容）。

对外契约：
- 统一事件流形态由 `contracts.llm_events` 定义（dict 事件，键集与取值不得更改）；
- 本积木对外提供 `LLMClient`（请求入口、重试上限同步、工具定义动态管理、原始 SSE
  取证）与可独立复用的纯函数（`retry_sleep` / `retry_notice` / `classify_stream_error`
  等，供上层流中断退避复用）。

装配约定（T4.3 接线）：thinking 参数与回传格式的唯一权威表在 `config.defaults`
（`llm` 与 `config` 同层不得互相 import），构造时必须注入——
`LLMClient(config, thinking_rules=config.defaults)`；未注入即 `TypeError`
（本积木不内置映射表副本）。
"""
from __future__ import annotations

from .anthropic_backend import SYNTHETIC_THINKING, AnthropicBackend, user_has_tool_result
from .client import DEFAULT_MAX_OUTPUT_TOKENS, LLMClient, strip_surrogates
from .openai_backend import OpenAIBackend
from .retry import (
    CONTEXT_OVERFLOW_HINTS,
    DEFAULT_MAX_RETRIES,
    MAX_RETRY_LIMIT,
    RETRY_BACKOFF_BASE,
    RETRY_POLL_SECONDS,
    RETRY_REASON_NETWORK,
    RETRY_REASON_RATE,
    backoff_base,
    clamp_retry_count,
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

__all__ = [
    "CONTEXT_OVERFLOW_HINTS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_RETRIES",
    "LLMClient",
    "LLMConfig",
    "LLMRuntime",
    "MAX_RETRY_LIMIT",
    "RETRY_BACKOFF_BASE",
    "RETRY_POLL_SECONDS",
    "RETRY_REASON_NETWORK",
    "RETRY_REASON_RATE",
    "STREAM_END",
    "STREAM_POLL_SECONDS",
    "STREAM_STALL_SECONDS",
    "SYNTHETIC_THINKING",
    "AnthropicBackend",
    "OpenAIBackend",
    "backoff_base",
    "clamp_retry_count",
    "classify_stream_error",
    "is_context_overflow",
    "is_retryable_http",
    "iter_to_queue",
    "retry_notice",
    "retry_sleep",
    "server_error_reason",
    "strip_surrogates",
    "user_has_tool_result",
]
