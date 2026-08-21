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
        _tool_context.goal_complete = True
    return "[GOAL_COMPLETE] 目标任务已声明完成，自动续跑将停止。请向用户总结完成情况。"
