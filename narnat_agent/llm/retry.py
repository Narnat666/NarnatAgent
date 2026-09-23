"""重试、退避与错误分类 —— llm 积木的自管重试决策单点。

搬运来源：旧包 `narnat_agent/core/llm.py` 43-120 行（`retry_sleep` / `_retry_notice` /
`_classify_stream_error` / `_is_retryable_http` / `_is_context_overflow`）——算法零改动，
只把旧实现的类级常量收敛为模块级常量（对齐 specs/llm「重试、退避与重试通知」
「错误事件与文案」「上下文超限识别」「流中断上报与静默挂死检测」）。

重试分类矩阵（spec 逐条）：
- `400/401/403/404/422` 不重试（400 命中超限特征除外，见 `is_context_overflow`）；
- `429` 限流类重试；
- `408`、`409` 与全部 ≥500 状态按服务端类重试（`is_retryable_http`）；
- 连接与超时异常按网络类重试；
- 其他异常不重试。
"""
from __future__ import annotations

import random
import time

import httpx

from ..contracts.llm_events import KEY_DETAIL, KEY_KIND, KEY_RETRY_NOTICE

__all__ = [
    "CONTEXT_OVERFLOW_HINTS",
    "DEFAULT_MAX_RETRIES",
    "MAX_RETRY_LIMIT",
    "RETRY_BACKOFF_BASE",
    "RETRY_POLL_SECONDS",
    "RETRY_REASON_NETWORK",
    "RETRY_REASON_RATE",
    "backoff_base",
    "clamp_retry_count",
    "classify_stream_error",
    "is_context_overflow",
    "is_retryable_http",
    "retry_notice",
    "retry_sleep",
    "server_error_reason",
]

RETRY_BACKOFF_BASE: tuple[int, ...] = (1, 2, 4, 8, 8)
"""指数退避基数序列（秒）：第 n 次重试取第 `n-1` 项，超出末项后固定 8 秒。"""

RETRY_POLL_SECONDS = 0.2
"""退避等待的取消轮询分片上限（秒）：等待期间以不大于该值的分片检查取消标记。"""

DEFAULT_MAX_RETRIES = 3
"""重试上限缺省值（配置 `智能体.LLM重试次数` 的默认值）。"""

MAX_RETRY_LIMIT = 10
"""重试上限钳制上界（配置值无论大小都被钳制到 1–10）。"""

RETRY_REASON_NETWORK = "网络连接失败"
"""网络类重试通知原因文案。"""

RETRY_REASON_RATE = "请求被限流(429)"
"""限流类重试通知原因文案。"""


def clamp_retry_count(n: int) -> int:
    """把配置的重试次数钳制到 1–10 区间（限流类与服务端/网络类上限同值）。"""
    return max(1, min(n, MAX_RETRY_LIMIT))


def backoff_base(attempt: int) -> float:
    """取第 `attempt` 项退避基数（`attempt` 自 0 起；超出末项后固定末项）。"""
    return RETRY_BACKOFF_BASE[min(attempt, len(RETRY_BACKOFF_BASE) - 1)]


def retry_sleep(attempt: int, cancel_check=None) -> bool:
    """指数退避休眠，带 ±25% 抖动与取消检查。返回 False 表示用户取消。

    等待以不大于 `RETRY_POLL_SECONDS` 的分片轮询取消标记（specs/llm
    「重试、退避与重试通知」）；退避期间取消时不重发请求，由调用方静默结束本轮。
    """
    base = backoff_base(attempt)
    jitter = base * 0.25 * (random.random() * 2 - 1)
    sleep_time = max(0, base + jitter)
    deadline = time.time() + sleep_time
    while time.time() < deadline:
        if cancel_check and cancel_check():
            return False
        time.sleep(min(RETRY_POLL_SECONDS, deadline - time.time()))
    return True


def retry_notice(attempt: int, max_retries: int, reason: str = RETRY_REASON_NETWORK) -> dict:
    """重试的用户提示事件（`{"retry_notice": …}`；仅供渲染，不进入对话历史）。

    文案固定为 `\\n⚠ <原因>，约<基数>s后自动重试（第<次数>/<上限>次）…\\n`，秒数与
    该次真实退避基数一致（取第 `attempt-1` 项）。
    """
    base = backoff_base(attempt - 1)
    return {
        KEY_RETRY_NOTICE: (
            f"\n⚠ {reason}，约{base}s后自动重试"
            f"（第{attempt}/{max_retries}次）…\n"
        )
    }


def server_error_reason(status: int) -> str:
    """服务端类重试通知原因文案（`服务端错误(<状态码>)`）。"""
    return f"服务端错误({status})"


def is_retryable_http(status: int) -> bool:
    """可重试的服务端状态：`408`、`409` 与全部 ≥500。"""
    return status in (408, 409) or status >= 500


CONTEXT_OVERFLOW_HINTS: tuple[str, ...] = (
    "context length",
    "context window",
    "context_length_exceeded",
    "context is too long",
    "prompt is too long",
    "too long",
    "exceeds the maximum",
    "maximum context",
    "input length",
    "超出上下文",
    "上下文长度",
    "超出限制",
    "超限",
)
"""上下文超限特征集合（中英措辞；匹配宽松，宽泛子串可能把非超限 400 判为超限）。"""


def is_context_overflow(text: str) -> bool:
    """识别 400 响应文本中的上下文超限特征（不区分大小写，任一命中即为超限）。

    命中后由上层归类为 `context_overflow` 并走压缩恢复，而非普通错误。
    """
    lowered = text.lower()
    return any(hint in lowered for hint in CONTEXT_OVERFLOW_HINTS)


def classify_stream_error(e: Exception) -> dict:
    """把流读取异常归类为上层可识别的中断信息 `{"kind": …, "detail": …}`。

    `timeout`：超时类异常（含挂死检测伪造的超时）；`network`：传输类异常；
    `unknown`：其他。`detail` 为异常文本前 200 字符。
    """
    if isinstance(e, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(e, httpx.TransportError):
        kind = "network"
    else:
        # OpenAI SDK 用自身异常类型包装底层连接错误（APITimeoutError 是 APIConnectionError 子类）
        try:
            from openai import APIConnectionError, APITimeoutError
        except ImportError:
            APIConnectionError = APITimeoutError = ()
        if isinstance(e, APITimeoutError):
            kind = "timeout"
        elif isinstance(e, APIConnectionError):
            kind = "network"
        else:
            kind = "unknown"
    return {KEY_KIND: kind, KEY_DETAIL: str(e)[:200]}
