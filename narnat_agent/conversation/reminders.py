"""收尾软提醒 —— 计划未勾选与后台任务运行中的注入文案（各自最多触发一次）。

契约来源：`openspec/changes/recast-v2/specs/conversation/spec.md`「收尾软提醒」：
一轮正常结束（无工具调用且有文本、无错误）后，两种情况各注入一次仅面向 AI 的提醒
（终端不展示），随后继续内循环；标志由新任务输入复位（`ReminderState`，见 app 能力）。

文案为已发布字面量，精确搬运旧实现 `narnat_agent/core/agent_loop.py`（345-384 行）；
触发一次的记账由 `contracts.tool.ReminderState` 承担，本模块只负责判定输入与拼文案。
"""
from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "BG_REMINDER_TEMPLATE",
    "PLAN_REMINDER_MAX_ITEMS",
    "PLAN_REMINDER_TEMPLATE",
    "bg_reminder_text",
    "plan_reminder_text",
    "unfinished_items",
]

PLAN_REMINDER_MAX_ITEMS = 5
"""计划提醒最多列出的未完成项数（超出时附「等共 N 项」）。"""

PLAN_REMINDER_TEMPLATE = (
    "[系统提醒] 你的计划仍有未勾选完成的项: {names}{tail}。"
    "若这些任务实际已完成，请先调用TodoWrite勾选它们（并把仍需继续的项标记为进行中）"
    "再输出最终总结；若确实未完成或刻意跳过，请在最终总结中向用户说明原因；"
    "若你有需要用户澄清的困惑或依赖用户决策，可直接向用户提问，无需强行收尾。"
)

BG_REMINDER_TEMPLATE = (
    "[系统提醒] 后台仍有任务在运行: {summary}。"
    "若需等待其完成，请调用 Shell(bg=\"wait\")（任一任务完成会立即返回）；"
    "若不再需要这些任务，请调用 Shell(bg=\"cancel\", id=N) 清理后结束回复。"
)


def unfinished_items(todos: Sequence[dict]) -> list[dict]:
    """计划中未勾选完成的项（`status != "completed"`，保持原顺序）。"""
    return [todo for todo in todos if todo.get("status") != "completed"]


def plan_reminder_text(unfinished: Sequence[dict]) -> str:
    """计划未勾选提醒文案：列出最多前 5 项名称，多于 5 项时附「等共 N 项」。"""
    names = "、".join(todo.get("content", "") for todo in unfinished[:PLAN_REMINDER_MAX_ITEMS])
    tail = f"等共{len(unfinished)}项" if len(unfinished) > PLAN_REMINDER_MAX_ITEMS else ""
    return PLAN_REMINDER_TEMPLATE.format(names=names, tail=tail)


def bg_reminder_text(summary: str) -> str:
    """后台任务运行中提醒文案（附等待与清理的操作方式）。"""
    return BG_REMINDER_TEMPLATE.format(summary=summary)
