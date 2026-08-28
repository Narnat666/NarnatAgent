"""TodoWrite工具 —— 创建和管理结构化任务列表"""

import json
from typing import List, Dict, Any, Optional, Callable

DEFINITION = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": "创建并管理任务列表（同时刻最多1个in_progress，多余会自动调整为待处理）。做任务前，优先使用此工具与用户同步计划。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "任务描述（祈使句，如'运行测试'）"},
                            "activeForm": {"type": "string", "description": "执行时的进行时描述（如'正在运行测试'；可选，缺省用content）"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态",
                            },
                        },
                        "required": ["content", "status"],
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
        for field in ("content", "status"):
            if field not in todo:
                return f"[错误: 第{i+1}项缺少必填字段: {field}]"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"[错误: 第{i+1}项status非法: {todo['status']}]"
        # activeForm可选：缺失时回退content（消费端tool_callbacks已按此兼容）
        if not todo.get("activeForm"):
            todo["activeForm"] = todo["content"]

    # in_progress 自动容错：保留第一个，其余降级为 pending。
    # （AI 偶发传多个 in_progress，硬报错会导致计划整体丢失、AI 不再同步；
    #   框架语义是"同时刻最多1个"，自动降级与之一致，且返回值附提示兜底）
    demoted = 0
    first_active_seen = False
    for todo in todos:
        if todo["status"] == "in_progress":
            if first_active_seen:
                todo["status"] = "pending"
                demoted += 1
            else:
                first_active_seen = True

    # 通知UI更新
    if _tool_context and _tool_context.ui_callback:
        _tool_context.ui_callback(todos)

    # 同步todo状态到上下文（供计划优先拦截使用）
    if _tool_context is not None:
        _tool_context.current_todos = list(todos)

    # ── 构建返回给 LLM 的状态提示 ──
    fix_note = ""
    if demoted:
        fix_note = f"[已自动修正: 检测到多个in_progress，保留第一个，其余{demoted}项调整为待处理]\n"

    unfinished = [t for t in todos if t["status"] != "completed"]

    if not unfinished:
        return fix_note + "[任务全部完成]"

    lines = ["[你有未完成的任务，请继续:]", ""]
    for i, t in enumerate(unfinished, 1):
        status_label = "进行中" if t["status"] == "in_progress" else "待处理"
        lines.append(f"{i}. [{status_label}] {t['content']}")

    return fix_note + "\n".join(lines)
