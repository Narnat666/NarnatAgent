"""GoalComplete工具 —— 声明目标任务已完成

仅在目标模式（/goal on）下有意义：AI 判定任务真正完成时调用，
系统将停止自动续跑。调用后 AI 应向用户总结完成情况。
"""

DEFINITION = {
    "type": "function",
    "function": {
        "name": "GoalComplete",
        "description": (
            "声明当前目标任务已完成。仅在任务真正完成时调用；"
            "调用后系统将停止自动续跑，请随后向用户总结完成情况、验证方式与具体结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def execute(_tool_context=None) -> str:
    """
    标记目标任务完成。

    Args:
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        提示文本，回传给LLM
    """
    if _tool_context is not None:
        # 收尾软提醒：计划未全部勾选时提醒一次（仅一次），提醒后不置完成标记，
        # 让AI补勾选后再调用GoalComplete；已提醒过或全部勾选则正常放行。
        unfinished = [
            t for t in _tool_context.current_todos
            if t.get("status") != "completed"
        ]
        if unfinished and not _tool_context.todo_reminded:
            _tool_context.todo_reminded = True
            names = "、".join(t.get("content", "") for t in unfinished[:5])
            tail = f"等共{len(unfinished)}项" if len(unfinished) > 5 else ""
            return (
                f"[提醒] 计划仍有未勾选完成的项: {names}{tail}。"
                "若这些任务实际已完成，请先调用TodoWrite勾选它们（并把仍需继续的项标记为进行中）"
                "，然后再次调用GoalComplete；若确实未完成或刻意跳过，"
                "请再次调用GoalComplete并在最终总结中向用户说明原因。"
            )
        _tool_context.goal_complete = True
    return "[GOAL_COMPLETE] 目标任务已声明完成，自动续跑将停止。请向用户总结完成情况。"
