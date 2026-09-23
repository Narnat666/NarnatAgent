"""GoalComplete工具 —— 声明目标任务已完成

仅在目标模式（/goal on）下有意义：AI 判定任务真正完成时调用，
系统将停止自动续跑。调用后 AI 应向用户总结完成情况。
"""

DEFINITION = {
    "type": "function",
    "function": {
        "name": "GoalComplete",
        "description": (
            "声明当前目标任务已完成——任务执行的最后一步调用。\n"
            "调用前必须全部满足：\n"
            "1. 任务目标已实际达成，关键结果已经真实验证，而非仅凭推理判断；\n"
            "2. 最终答复已完整写出。\n"
            "调用方式：在输出最终答复后调用本工具。\n"
            "收到工具结果后只用一两句话简短收尾，不要重复完整答复内容。"
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
        # 硬兜底：声明完成 = 会话结案，清理全部受管后台任务（杀 running + 清目录）。
        # 延迟导入避免 tools 包加载顺序依赖。
        try:
            from ..background import cleanup_all
            cleanup_all()
        except Exception:
            pass
    return "[GOAL_COMPLETE] 目标任务已声明完成，自动续跑将停止。请向用户总结完成情况。"
