"""LLM 事件协议 —— 事件流的键集、取值与消费谓词集中定义。

契约来源：
- `openspec/changes/recast-v2/specs/llm/spec.md`（事件协议事实来源：文本与思考
  事件流、工具调用流式聚合、用量事件归一、完成原因取值与协议映射、重试通知、
  流中断上报、上下文超限识别、错误事件与文案、取消与中断）；
- `docs/recast/reports/R1_report_core_dialog.md` 第 870-879 行「chunk 事件协议」
  清单（逐字对照，保持 dict 形态契约）。

设计决策（design D2）：`llm` 积木产出的仍是 dict 事件（已发布的快照对照契约，
零转换成本），本模块以 TypedDict 集中定义全部事件形态、以 `LLM_EVENT` 联合全部
形态；向产出方（llm）提供 `KEY_*` 键名常量（禁止键名字面量散落），向消费方
（conversation 等）提供纯函数谓词（逐条对齐 specs/llm 的消费规则）。

本模块为零逻辑纯定义层：只依赖标准库（typing / collections.abc）；
不 import 新包其他积木。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict, cast

__all__ = [
    # 事件形态（TypedDict / 联合类型）
    "Completion",
    "ContextOverflow",
    "ErrorCompletion",
    "LLM_EVENT",
    "RetryNotice",
    "StreamInterrupted",
    "StreamInterruptedInfo",
    "TextDelta",
    "ToolCall",
    "ToolCallFunction",
    "ToolCallsReady",
    "Usage",
    "UsageEvent",
    # 取值常量
    "FINISH_REASONS",
    "INTERRUPT_KINDS",
    # 键名常量
    "KEY_ARGUMENTS",
    "KEY_CACHED_TOKENS",
    "KEY_COMPLETION_TOKENS",
    "KEY_CONTENT",
    "KEY_DETAIL",
    "KEY_FINISH_REASON",
    "KEY_FUNCTION",
    "KEY_ID",
    "KEY_KIND",
    "KEY_NAME",
    "KEY_PROMPT_TOKENS",
    "KEY_RETRY_NOTICE",
    "KEY_STREAM_INTERRUPTED",
    "KEY_THINKING",
    "KEY_THINKING_SIGNATURE",
    "KEY_TOOL_CALLS",
    "KEY_TYPE",
    "KEY_USAGE",
    # 消费谓词
    "get_finish_reason",
    "get_stream_interrupted",
    "get_thinking",
    "get_thinking_signature",
    "get_tool_calls",
    "get_usage",
    "is_completion",
    "is_retry_notice",
    "is_stream_interrupted",
    "is_text_delta",
]

# ═══════════════════════════════════════════════════════════════
# 取值常量
# ═══════════════════════════════════════════════════════════════

FINISH_REASONS: tuple[str, ...] = (
    "stop",
    "tool_calls",
    "max_tokens",
    "content_filter",
    "server_busy",
    "error",
    "context_overflow",
)
"""已知完成原因取值集合（specs/llm「完成原因取值与协议映射」）。

Anthropic 兼容协议映射表外的结束原因 SHALL 原样透传——因此本集合是
「已知取值清单」而非封闭枚举，消费方的分支判定不得假定它是全集。
"""

INTERRUPT_KINDS: tuple[str, ...] = ("timeout", "network", "unknown")
"""流中断类别取值（specs/llm「流中断上报与静默挂死检测」）。

- `timeout`：超时类异常（含挂死检测伪造的超时）；
- `network`：传输类异常；
- `unknown`：其他。
"""

# ═══════════════════════════════════════════════════════════════
# 键名常量（llm 产出方与 conversation 消费方共用，禁止字面量散落）
# ═══════════════════════════════════════════════════════════════

KEY_CONTENT = "content"
KEY_TOOL_CALLS = "tool_calls"
KEY_FINISH_REASON = "finish_reason"
KEY_THINKING = "thinking"
KEY_THINKING_SIGNATURE = "thinking_signature"
KEY_USAGE = "usage"
KEY_RETRY_NOTICE = "retry_notice"
KEY_STREAM_INTERRUPTED = "stream_interrupted"
KEY_PROMPT_TOKENS = "prompt_tokens"
KEY_COMPLETION_TOKENS = "completion_tokens"
KEY_CACHED_TOKENS = "cached_tokens"
KEY_KIND = "kind"
KEY_DETAIL = "detail"
KEY_ID = "id"
KEY_TYPE = "type"
KEY_FUNCTION = "function"
KEY_NAME = "name"
KEY_ARGUMENTS = "arguments"

# ═══════════════════════════════════════════════════════════════
# 事件形态（TypedDict）
# ═══════════════════════════════════════════════════════════════


class Usage(TypedDict):
    """用量载荷：`{"prompt_tokens": int, "completion_tokens": int, "cached_tokens": int}`。

    归一语义见 specs/llm「用量事件归一」：Anthropic 兼容协议的输入用量为
    `input_tokens + 缓存命中数`；OpenAI 兼容协议的缓存命中数经三级回退取值。
    """

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


class ToolCallFunction(TypedDict):
    """工具调用的函数体：名称与参数按到达顺序字符串拼接（参数不做解析）。"""

    name: str
    arguments: str


class ToolCall(TypedDict):
    """工具调用条目（完成事件 `tool_calls` 列表的元素形态）。

    形状为 `{"id": str, "type": "function", "function": {"name": str, "arguments": str}}`；
    无依据时 id 为 `_tc_<序号>` 占位形式（specs/llm「工具调用流式聚合」）。
    """

    id: str
    type: Literal["function"]
    function: ToolCallFunction


class TextDelta(TypedDict):
    """文本增量事件 `{"content": str}`。

    对每个文本增量即时下发（同一轮流中可出现多次）；Anthropic 兼容协议的
    thinking-only 兜底路径也会以本形态下发（specs/llm「文本与思考事件流」）。
    """

    content: str


class ToolCallsReady(TypedDict):
    """工具调用就绪事件（带缓冲完成标记）。

    形状：`{"tool_calls": [ToolCall, ...], "finish_reason": str, "thinking": str | None}`
    加可选的 `"thinking_signature"`（仅 Anthropic 兼容协议输出，恰一个思考块且
    签名非空时取签名值，否则为空值）。
    """

    tool_calls: list[ToolCall]
    finish_reason: str
    thinking: str | None
    thinking_signature: NotRequired[str | None]


class Completion(TypedDict):
    """无工具轮的完成事件 `{"finish_reason": str, "thinking": ...}`。

    无工具调用缓冲时下发（不带 `tool_calls` 键）；思考内容（Anthropic 兼容协议的
    完成事件还附 `thinking_signature`）按回传规则给出，思考作为正式文本兜底输出时
    `thinking` 为空串。
    """

    finish_reason: str
    thinking: NotRequired[str | None]
    thinking_signature: NotRequired[str | None]


class ContextOverflow(TypedDict):
    """上下文超限事件 `{"finish_reason": "context_overflow"}`（不带 content）。

    状态 400 且响应文本命中超限特征时只下发本事件且不重试
    （specs/llm「上下文超限识别」）。
    """

    finish_reason: Literal["context_overflow"]


class ErrorCompletion(TypedDict):
    """错误完成事件 `{"content": "[错误: …]", "finish_reason": "error"}`。

    各类失败文案的最终形态（specs/llm「错误事件与文案」）；用户取消 SHALL NOT
    产生本事件。
    """

    content: str
    finish_reason: Literal["error"]


class UsageEvent(TypedDict):
    """用量事件 `{"usage": Usage}`（同一轮流中可出现多次）。"""

    usage: Usage


class RetryNotice(TypedDict):
    """重试通知事件 `{"retry_notice": str}`（仅供渲染，不进入对话历史）。

    文案固定：`\\n⚠ <原因>，约<基数>s后自动重试（第<次数>/<上限>次）…\\n`
    （specs/llm「重试、退避与重试通知」）。
    """

    retry_notice: str


class StreamInterruptedInfo(TypedDict):
    """流中断信息：`{"kind": "timeout" | "network" | "unknown", "detail": str}`。

    `detail` 为异常文本前 200 字符；同一轮流只保留并上报第一个异常。
    """

    kind: Literal["timeout", "network", "unknown"]
    detail: str


class StreamInterrupted(TypedDict):
    """流中断事件（未收到完成标记时上报，不伪造完成原因）。"""

    stream_interrupted: StreamInterruptedInfo


LLM_EVENT = (
    TextDelta
    | ToolCallsReady
    | Completion
    | ContextOverflow
    | ErrorCompletion
    | UsageEvent
    | RetryNotice
    | StreamInterrupted
)
"""全部 LLM 事件形态的联合类型（消费方类型标注的锚点）。

注：按 Python 惯例该别名宜写作 `LLMEvent`，此处遵循仓库分层检查的模块级命名
规则（顶层赋值名须全大写）取 `LLM_EVENT`，语义不变。
"""

# ═══════════════════════════════════════════════════════════════
# 纯函数消费谓词（无状态、deterministic；对齐 specs/llm 消费规则）
# ═══════════════════════════════════════════════════════════════


def is_text_delta(event: Mapping[str, object]) -> bool:
    """判定「纯文本增量」事件：含 `content` 且不含 `tool_calls`。

    对应 conversation 消费规则中的固定判定（specs/conversation「内循环轮转与
    事件流消费」）：仅当该事件不含工具调用时才输出到终端并计入本轮文本缓冲。
    注意该判定同样命中 `ErrorCompletion` 携带的错误文案（与现状消费顺序一致，
    错误出口不写入历史）。
    """
    return KEY_CONTENT in event and KEY_TOOL_CALLS not in event


def is_completion(event: Mapping[str, object]) -> bool:
    """判定事件是否携带结束标记（`finish_reason` 键存在即为「完成」出口）。

    涵盖无工具完成、工具轮完成、上下文超限与错误完成四种形态；取值语义见
    `get_finish_reason`。
    """
    return KEY_FINISH_REASON in event


def get_finish_reason(event: Mapping[str, object]) -> str | None:
    """取完成原因（`finish_reason`）；无该键或值非文本时返回 None。

    取值可能超出 `FINISH_REASONS`（Anthropic 兼容协议未知结束原因原样透传）。
    """
    value = event.get(KEY_FINISH_REASON)
    return value if isinstance(value, str) else None


def get_tool_calls(event: Mapping[str, object]) -> list[ToolCall] | None:
    """取工具调用列表（`tool_calls`）；无该键或值非列表时返回 None。

    空列表表示「有缓冲但为空」（工具调用聚合键存在），消费方按自身规则判空。
    """
    value = event.get(KEY_TOOL_CALLS)
    if isinstance(value, list):
        return cast("list[ToolCall]", value)
    return None


def get_usage(event: Mapping[str, object]) -> Usage | None:
    """取用量载荷（`usage`）；无该键或值非字典时返回 None。"""
    value = event.get(KEY_USAGE)
    if isinstance(value, dict):
        return cast("Usage", value)
    return None


def get_thinking(event: Mapping[str, object]) -> str | None:
    """取思考内容（`thinking`）；无该键或值非文本时返回 None。

    工具轮之外的回传判定与思考输出规则见 specs/llm「思考回传格式与开关」。
    """
    value = event.get(KEY_THINKING)
    return value if isinstance(value, str) else None


def get_thinking_signature(event: Mapping[str, object]) -> str | None:
    """取思考签名（`thinking_signature`）；无该键或值非文本时返回 None。

    仅 Anthropic 兼容协议输出；多个思考块并存时为空值（宁缺勿错）。
    """
    value = event.get(KEY_THINKING_SIGNATURE)
    return value if isinstance(value, str) else None


def is_stream_interrupted(event: Mapping[str, object]) -> bool:
    """判定事件是否上报流中断（`stream_interrupted` 键存在）。"""
    return KEY_STREAM_INTERRUPTED in event


def get_stream_interrupted(
    event: Mapping[str, object],
) -> StreamInterruptedInfo | None:
    """取流中断信息（`stream_interrupted`）；无该键或值非字典时返回 None。"""
    value = event.get(KEY_STREAM_INTERRUPTED)
    if isinstance(value, dict):
        return cast("StreamInterruptedInfo", value)
    return None


def is_retry_notice(event: Mapping[str, object]) -> bool:
    """判定事件是否为重试通知（`retry_notice` 键存在）。

    重试提示直通终端、不写入对话历史（specs/conversation「内循环轮转与事件流消费」）。
    """
    return KEY_RETRY_NOTICE in event
