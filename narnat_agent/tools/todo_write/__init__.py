"""TodoWrite工具 —— 创建和管理结构化任务列表"""

from typing import List, Dict, Any

DEFINITION = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": (
            "创建并管理当前任务的计划清单。多步任务开始前先建计划："
            "每步一条、用祈使句、可验证；同时只保留一个进行中的项；"
            "完成一步立即勾选。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述（祈使句，如'运行测试'）",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态（同时刻最多1个in_progress，多余的自动调整为待处理）",
                            },
                        },
                        "required": ["content", "status"],
                    },
                    "description": "任务列表（每次提交完整列表，整体替换）",
                },
            },
            "required": ["todos"],
        },
    },
}


CAPABILITY = {
    "label": "更新计划",
    "dispatch": "serial",
    "summary": "todos",
    "trusted_output": True,
}


def execute(todos: List[Dict[str, Any]], _tool_context=None) -> str:
    """
    创建/更新任务列表。

    Args:
        todos: 任务列表，每项含content/status
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        计数状态提示（全部完成时返回"[任务全部完成]"）
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
        if not str(todo["content"]).strip():
            return f"[错误: 第{i+1}项content为空，请补全后重新提交]"

    # 校验重复content（strip后比较）
    seen = set()
    for i, todo in enumerate(todos):
        key = str(todo["content"]).strip()
        if key in seen:
            return f"[错误: 重复的content \"{key}\"，请合并或改写]"
        seen.add(key)

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

    # 同步todo状态到上下文（供收尾软提醒/压缩重注入/完成校验使用）
    if _tool_context is not None:
        _tool_context.current_todos = list(todos)

    # ── 构建返回给 LLM 的状态提示（中性计数，不催跑）──
    fix_note = ""
    if demoted:
        fix_note = f"[已自动修正: 检测到多个in_progress，保留第一个，其余{demoted}项调整为待处理]\n"

    pending = sum(1 for t in todos if t["status"] == "pending")
    in_progress = sum(1 for t in todos if t["status"] == "in_progress")
    completed = sum(1 for t in todos if t["status"] == "completed")

    if completed == len(todos):
        return fix_note + "[任务全部完成]"

    return fix_note + f"[计划已更新] {pending} 待处理 / {in_progress} 进行中 / {completed} 已完成"
