"""conversation 积木 —— 对话内循环、工具调度、目标模式与收尾软提醒。

对外面：
- `ConversationLoop` / `TurnOutcome`：单轮对话的内循环与结局返回值（`loop.py`）；
- `ToolDispatcher` / `group_tool_calls`：工具调度的三阶段分组执行与终端调度行
  （`dispatch.py`，装配点构造注入）；
- `GoalMode` / `GoalDecision`：目标模式续跑判定与收尾轮指令（`goal.py`，
  主循环每轮结束后调用）；
- 软提醒文案（`reminders.py`）。
"""
from __future__ import annotations

from .dispatch import (
    CancelProbe,
    ConsolePort,
    DispatchTheme,
    LogPort,
    ToolDispatcher,
    ToolExecutor,
    group_tool_calls,
    parse_tool_call,
)
from .goal import (
    CONTINUE_TEMPLATE,
    FINAL_ROUND_TEMPLATE,
    GOAL_CONTINUE,
    GOAL_END,
    GOAL_FINAL,
    GoalDecision,
    GoalMode,
)
from .loop import (
    BG_RUNNING_NOTICE,
    DELETE_CANCELLED_TEXT,
    DELETE_CONFIRM_PROMPT,
    EMPTY_REPLY_TEXTS,
    TURN_COMPLETED,
    TURN_EMPTY,
    TURN_ERROR,
    TURN_INTERRUPTED,
    AIOptions,
    BackgroundPort,
    CompressionPort,
    ConversationLoop,
    HistoryPort,
    LLMStreamSource,
    RatioPort,
    StatsPort,
    TurnOutcome,
)
from .reminders import (
    BG_REMINDER_TEMPLATE,
    PLAN_REMINDER_MAX_ITEMS,
    PLAN_REMINDER_TEMPLATE,
    bg_reminder_text,
    plan_reminder_text,
    unfinished_items,
)

__all__ = [
    # dispatch
    "CancelProbe",
    "ConsolePort",
    "DispatchTheme",
    "LogPort",
    "ToolDispatcher",
    "ToolExecutor",
    "group_tool_calls",
    "parse_tool_call",
    # goal
    "CONTINUE_TEMPLATE",
    "FINAL_ROUND_TEMPLATE",
    "GOAL_CONTINUE",
    "GOAL_END",
    "GOAL_FINAL",
    "GoalDecision",
    "GoalMode",
    # loop
    "BG_RUNNING_NOTICE",
    "DELETE_CANCELLED_TEXT",
    "DELETE_CONFIRM_PROMPT",
    "EMPTY_REPLY_TEXTS",
    "TURN_COMPLETED",
    "TURN_EMPTY",
    "TURN_ERROR",
    "TURN_INTERRUPTED",
    "AIOptions",
    "BackgroundPort",
    "CompressionPort",
    "ConversationLoop",
    "HistoryPort",
    "LLMStreamSource",
    "RatioPort",
    "StatsPort",
    "TurnOutcome",
    # reminders
    "BG_REMINDER_TEMPLATE",
    "PLAN_REMINDER_MAX_ITEMS",
    "PLAN_REMINDER_TEMPLATE",
    "bg_reminder_text",
    "plan_reminder_text",
    "unfinished_items",
]
