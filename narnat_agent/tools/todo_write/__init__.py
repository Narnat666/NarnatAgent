"""TodoWrite工具 —— 创建和管理结构化任务列表"""

import json
from typing import List, Dict, Any, Optional, Callable

DEFINITION = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": "创建并管理任务列表（同时刻最多1个in_progress）。做任务前，优先使用此工具与用户同步计划。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "任务描述（祈使句，如'运行测试'）"},
                            "activeForm": {"type": "string", "description": "执行时的进行时描述（如'正在运行测试'）"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态",
                            },
                        },
                        "required": ["content", "status", "activeForm"],
                    },
                    "description": "任务列表",
                },
            },
            "required": ["todos"],
        },
    },
}



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
        return "[错误: todos不能为空]"

    # 校验每个todo的字段
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return f"[错误: 第{i+1}项不是对象]"
        for field in ("content", "activeForm", "status"):
            if field not in todo:
                return f"[错误: 第{i+1}项缺少必填字段: {field}]"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"[错误: 第{i+1}项status非法: {todo['status']}]"

    # 校验in_progress数量：允许0个（初始状态）或1个，禁止多个
    in_progress_count = sum(1 for t in todos if t["status"] == "in_progress")
    if in_progress_count > 1:
        return f"[错误: 最多1个in_progress任务，当前有{in_progress_count}个]"

    # 通知UI更新
    if _tool_context and _tool_context.ui_callback:
        _tool_context.ui_callback(todos)

    # 同步todo状态到上下文（供计划优先拦截使用）
    if _tool_context is not None:
        _tool_context.current_todos = list(todos)

    # ── 构建返回给 LLM 的状态提示 ──
    unfinished = [t for t in todos if t["status"] != "completed"]

    if not unfinished:
        return "[任务全部完成]"

    lines = ["[你有未完成的任务，请继续:]", ""]
    for i, t in enumerate(unfinished, 1):
        status_label = "进行中" if t["status"] == "in_progress" else "待处理"
        lines.append(f"{i}. [{status_label}] {t['content']}")
    lines.append("")
    lines.append("[完成后请使用 TodoWrite 更新任务状态。]")

    return "\n".join(lines)
