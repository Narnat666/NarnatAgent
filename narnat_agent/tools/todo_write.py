"""TodoWrite工具 —— 创建和管理结构化任务列表"""

import json
from typing import List, Dict, Any, Optional, Callable


def execute(todos: List[Dict[str, Any]], _tool_context=None) -> str:
    """
    创建/更新任务列表。

    Args:
        todos: 任务列表，每项含content/activeForm/status
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        空串（UI侧更新显示）
    """
    # 校验非空
    if not todos:
        return "错误: todos不能为空"

    # 校验每个todo的字段
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return f"错误: 第{i+1}项不是对象"
        for field in ("content", "activeForm", "status"):
            if field not in todo:
                return f"错误: 第{i+1}项缺少必填字段: {field}"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"错误: 第{i+1}项status非法: {todo['status']}"

    # 校验in_progress数量：允许0个（初始状态）或1个，禁止多个
    in_progress_count = sum(1 for t in todos if t["status"] == "in_progress")
    if in_progress_count > 1:
        return f"错误: 最多1个in_progress任务，当前有{in_progress_count}个"

    # 通知UI更新
    if _tool_context and _tool_context.ui_callback:
        _tool_context.ui_callback(todos)

    return f"任务列表已更新({len(todos)}项)"
