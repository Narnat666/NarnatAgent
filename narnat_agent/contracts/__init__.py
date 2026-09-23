"""contracts 积木 —— 跨积木共享契约层（L0：最底层，其余全部积木的依赖锚点）。

全部跨积木共享的**类型与协议定义**（零业务逻辑、零实现、零状态）：
- `contracts.llm_events`：LLM 事件协议（契约来源：specs/llm；R1 报告 870-879 清单）；
- `contracts.tool`：工具协议与运行时状态协议（契约来源：specs/tools-*、specs/mcp、
  specs/conversation、design D3/D6）；
- `contracts.output`：输出/交互端口（契约来源：specs/ui、specs/stats、design D4/D8）；
- `contracts.interrupt`：中断信号协议（契约来源：specs/interrupt、design D5）。

依赖规则（design D1，机械化检查见 `tests/check_layering.py`）：本积木禁止 import
新包其他积木；其余积木经本层符号交互。
"""
from __future__ import annotations

from . import interrupt, llm_events, output, tool
from .interrupt import InterruptSignal
from .llm_events import (
    FINISH_REASONS,
    INTERRUPT_KINDS,
    KEY_ARGUMENTS,
    KEY_CACHED_TOKENS,
    KEY_COMPLETION_TOKENS,
    KEY_CONTENT,
    KEY_DETAIL,
    KEY_FINISH_REASON,
    KEY_FUNCTION,
    KEY_ID,
    KEY_KIND,
    KEY_NAME,
    KEY_PROMPT_TOKENS,
    KEY_RETRY_NOTICE,
    KEY_STREAM_INTERRUPTED,
    KEY_THINKING,
    KEY_THINKING_SIGNATURE,
    KEY_TOOL_CALLS,
    KEY_TYPE,
    KEY_USAGE,
    LLM_EVENT,
    Completion,
    ContextOverflow,
    ErrorCompletion,
    RetryNotice,
    StreamInterrupted,
    StreamInterruptedInfo,
    TextDelta,
    ToolCall,
    ToolCallFunction,
    ToolCallsReady,
    Usage,
    UsageEvent,
    get_finish_reason,
    get_stream_interrupted,
    get_thinking,
    get_thinking_signature,
    get_tool_calls,
    get_usage,
    is_completion,
    is_retry_notice,
    is_stream_interrupted,
    is_text_delta,
)
from .output import Animator, InteractionPort, OutputSink, TurnStats
from .tool import (
    AWAIT_CONFIRM,
    ConnectedServer,
    DeleteGate,
    GoalState,
    McpPort,
    PlanTracker,
    ReminderState,
    TodoItem,
    Tool,
    ToolCatalog,
    ToolDefinition,
    ToolEnv,
    ToolFunctionDef,
    ToolResult,
    ToolSettings,
)

__all__ = [
    # 子模块（`from narnat_agent.contracts import llm_events` 等四条路径为验收锚点）
    "interrupt",
    "llm_events",
    "output",
    "tool",
    # llm_events —— 事件形态
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
    # llm_events —— 取值常量
    "FINISH_REASONS",
    "INTERRUPT_KINDS",
    # llm_events —— 键名常量
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
    # llm_events —— 消费谓词
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
    # tool
    "AWAIT_CONFIRM",
    "ConnectedServer",
    "DeleteGate",
    "GoalState",
    "McpPort",
    "PlanTracker",
    "ReminderState",
    "TodoItem",
    "Tool",
    "ToolCatalog",
    "ToolDefinition",
    "ToolEnv",
    "ToolFunctionDef",
    "ToolResult",
    "ToolSettings",
    # output
    "Animator",
    "InteractionPort",
    "OutputSink",
    "TurnStats",
    # interrupt
    "InterruptSignal",
]
