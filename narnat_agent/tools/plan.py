"""计划工具族 —— TodoWrite（任务列表）与 GoalComplete（目标完成声明）。

契约来源：`openspec/changes/recast-v2/specs/tools-todo/spec.md`（工具名与参数契约、
校验与错误文案、多项 in_progress 自动修正、返回形态、状态同步与终端显示、
GoalComplete 可见性与行为、目标完成标记的主循环消费、兼容性怪癖保持）。行为搬运自
旧实现 `narnat_agent/tools/todo_write/__init__.py` 与 `tools/goal_complete/__init__.py`。

结构要点（design D6 状态归主 / D7 无私有互摸）：
- 计划写 `ToolEnv.plan`（PlanTracker）、完成标记写 `ToolEnv.goal`（GoalState）、
  收尾软提醒标志走 `ToolEnv.reminders`（ReminderState）——不再有跨模块直改的私有字段；
- 终端显示改走 `ToolResult.ui_text`（旧 `ui_callback` 的等价通道）：工具产出带色状态行，
  由 UI 层按着色 diff 同款规则渲染（逐行前置两个空格缩进、静默工具模式跳过渲染）；
- 颜色经构造注入（结构上兼容 `output.Theme`），工具积木不 import 其它积木；
- 工具定义为模块级常量（与旧实现 DEFINITION 逐字节等价，与同积木其它工具族同形）。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from ..contracts.tool import UI_TEXT_LINES, ToolDefinition, ToolEnv, ToolResult
from .registry import ToolRegistry

__all__ = [
    "GOAL_COMPLETE_DEFINITION",
    "GOAL_COMPLETE_TEXT",
    "STATUS_LABELS",
    "STATUSES",
    "TODOWRITE_DEFINITION",
    "BackgroundTasks",
    "GoalCompleteTool",
    "PlanTheme",
    "TodoWriteTool",
    "register_plan_tools",
    "reminder_text",
]

STATUSES = ("pending", "in_progress", "completed")
"""计划项状态枚举（待处理 / 进行中 / 已完成）。"""

STATUS_LABELS = {"in_progress": "进行中", "pending": "待处理"}
"""未完成清单的状态标签（已完成项不进清单）。"""

GOAL_COMPLETE_TEXT = "[GOAL_COMPLETE] 目标任务已声明完成，自动续跑将停止。请向用户总结完成情况。"
"""声明完成的固定返回文本。"""

ACTIVE_PREFIX = "正在"
"""进行中项的显示前缀（内容已以「正在」开头时不再叠加）。"""


class PlanTheme(Protocol):
    """计划状态行所需的颜色来源（`output.Theme` 实例在结构上满足本协议）。

    六项取值均按 `str()` 转成 ANSI 文本：成功色、警告色、次要色、暗淡、加粗、复位。
    """

    c_success: object
    c_warning: object
    c_secondary: object
    d: object
    b: object
    r: object


# ═══════════════════════════════════════════════════════════════
# 工具定义
# ═══════════════════════════════════════════════════════════════


TODOWRITE_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": (
            "创建并管理任务列表。"
            "多步任务开始前先与用户同步计划。"
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


GOAL_COMPLETE_DEFINITION: ToolDefinition = {
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


# ═══════════════════════════════════════════════════════════════
# TodoWrite
# ═══════════════════════════════════════════════════════════════


def validate_todos(todos: object) -> str | None:
    """全量校验提交的任务列表，返回首个错误的固定文案（通过时返回 None）。

    校验逐项进行、先于任何副作用（终端显示、上下文同步）：空/None/缺失 →
    `[错误: todos不能为空]`；第 i+1 项不是对象 → `[错误: 第{i+1}项不是对象]`；
    缺 `content`/`status` → `[错误: 第{i+1}项缺少必填字段: {字段名}]`；
    `status` 不在三值枚举 → `[错误: 第{i+1}项status非法: {传入值}]`。
    """
    if not todos:
        return "[错误: todos不能为空]"
    for index, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return f"[错误: 第{index + 1}项不是对象]"
        for field in ("content", "status"):
            if field not in todo:
                return f"[错误: 第{index + 1}项缺少必填字段: {field}]"
        if todo["status"] not in STATUSES:
            return f"[错误: 第{index + 1}项status非法: {todo['status']}]"
    return None


def demote_extra_in_progress(todos: list[dict]) -> int:
    """就地修正多项进行中：保留第一个，其余降级为待处理；返回被降级项数。

    修正是对提交列表的就地修改（兼容性怪癖保持：元素对象与计划状态共享）。
    """
    demoted = 0
    first_active_seen = False
    for todo in todos:
        if todo["status"] == "in_progress":
            if first_active_seen:
                todo["status"] = "pending"
                demoted += 1
            else:
                first_active_seen = True
    return demoted


def build_unfinished_list(todos: Sequence[dict], fix_note: str) -> str:
    """未完成任务清单文本：全部完成返回 `[任务全部完成]`。

    否则返回以 `[你有未完成的任务，请继续:]` 起头、随后一个空行、再逐项一行
    `{序号}. [{状态标签}] {内容}` 的清单（序号自 1 起、仅对未完成项重新编号）。
    自动修正前缀置于清单最前。
    """
    unfinished = [todo for todo in todos if todo["status"] != "completed"]
    if not unfinished:
        return fix_note + "[任务全部完成]"
    lines = ["[你有未完成的任务，请继续:]", ""]
    for index, todo in enumerate(unfinished, 1):
        label = STATUS_LABELS.get(todo["status"], "待处理")
        lines.append(f"{index}. [{label}] {todo['content']}")
    return fix_note + "\n".join(lines)


def build_display_lines(todos: Sequence[dict], theme: PlanTheme | None) -> str | None:
    """计划状态行的显示文本（每项一行，不带缩进——缩进由 UI 渲染层施加）。

    图标与颜色角色：已完成 `✓`（成功色；内容暗淡）、进行中 `●`（警告色；内容加粗，
    内容不以「正在」开头时前置「正在」）、待处理 `○`（次要色；内容暗淡）。
    未注入主题时无色（直接调用场景）。
    """
    if theme is None:
        return None
    ok, warn = str(theme.c_success), str(theme.c_warning)
    pending_color = str(theme.c_secondary)
    dim, bold, reset = str(theme.d), str(theme.b), str(theme.r)
    lines: list[str] = []
    for todo in todos:
        status = todo["status"]
        content = todo.get("content", "")
        if status == "completed":
            lines.append(f"{ok}✓{reset} {dim}{content}{reset}")
        elif status == "in_progress":
            prefix = "" if content.startswith(ACTIVE_PREFIX) else ACTIVE_PREFIX
            lines.append(f"{warn}●{reset} {bold}{prefix}{content}{reset}")
        else:
            lines.append(f"{pending_color}○{reset} {dim}{content}{reset}")
    return "\n".join(lines)


class TodoWriteTool:
    """TodoWrite 工具实现（`contracts.tool.Tool` 协议）。

    `theme` 为计划状态行的颜色来源（结构上兼容 `output.Theme`）；由装配注入，
    未注入时终端显示为无色文本。
    """

    name = "TodoWrite"

    def __init__(self, theme: PlanTheme | None = None) -> None:
        self._theme = theme

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（与旧实现 DEFINITION 逐字节等价）。"""
        return TODOWRITE_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """提交完整任务列表：校验 → 自动修正 → 终端显示 → 计划同步 → 返回清单。

        校验不通过时在任何状态变更之前返回固定错误文案；未携带工具上下文
        （直接调用场景）静默跳过上下文同步与终端显示，仅返回结果文本。
        """
        todos = args.get("todos")
        error = validate_todos(todos)
        if error is not None:
            return ToolResult(llm_text=error)

        demoted = demote_extra_in_progress(todos)
        # 终端显示（旧 ui_callback 通道）：先产出显示文本，再做计划同步（顺序与旧实现一致）
        ui_text = build_display_lines(todos, self._theme) if env is not None else None
        if env is not None:
            env.plan.replace(todos)

        fix_note = ""
        if demoted:
            fix_note = (f"[已自动修正: 检测到多个in_progress，保留第一个，"
                        f"其余{demoted}项调整为待处理]\n")
        return ToolResult(llm_text=build_unfinished_list(todos, fix_note),
                          ui_text=ui_text, ui_text_kind=UI_TEXT_LINES)


# ═══════════════════════════════════════════════════════════════
# GoalComplete
# ═══════════════════════════════════════════════════════════════


def reminder_text(unfinished: Sequence[dict]) -> str:
    """收尾软提醒文本：列出最多前 5 项内容，多于 5 项时追加 `等共{N}项`。"""
    names = "、".join(todo.get("content", "") for todo in unfinished[:5])
    tail = f"等共{len(unfinished)}项" if len(unfinished) > 5 else ""
    return (
        f"[提醒] 计划仍有未勾选完成的项: {names}{tail}。"
        "若这些任务实际已完成，请先调用TodoWrite勾选它们（并把仍需继续的项标记为进行中）"
        "，然后再次调用GoalComplete；若确实未完成或刻意跳过，"
        "请再次调用GoalComplete并在最终总结中向用户说明原因。"
    )


class BackgroundTasks(Protocol):
    """受管后台任务端口（结构上兼容 `tools.shell.background.BackgroundManager`）。

    只在「声明完成」路径上使用：清理全部受管后台任务（终止运行中任务进程并清空
    任务目录）。也可直接传入同签名的可调用对象（如绑定方法）。
    """

    def cleanup_all(self) -> None: ...


class GoalCompleteTool:
    """GoalComplete 工具实现（`contracts.tool.Tool` 协议）。

    `background` 为受管后台任务端口（装配注入 `tools.shell` 的后台管理器；未注入
    表示无后台任务来源，跳过清理；清理失败静默忽略）。执行能力始终注册（兼容历史
    残留调用），定义仅在目标模式开启时经
    `ToolRegistry.get_tool_definitions(include_goal=True)` 注入
    （旧实现 `LLMClient.set_goal_tool` 的等价机制）。
    """

    name = "GoalComplete"

    def __init__(self, background: BackgroundTasks | Callable[[], None] | None = None) -> None:
        self._background = background

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（默认不进默认工具表）。"""
        return GOAL_COMPLETE_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """按固定顺序执行：收尾软提醒（计划未勾选，仅一次）→ 完成标记 + 后台清理。

        未携带工具上下文（直接调用场景）跳过全部状态写入，仅返回固定文本。
        """
        if env is None:
            return ToolResult(llm_text=GOAL_COMPLETE_TEXT)

        unfinished = [
            todo for todo in env.plan.current()
            if todo.get("status") != "completed"
        ]
        if unfinished and env.reminders.try_trigger_plan():
            return ToolResult(llm_text=reminder_text(unfinished))

        env.goal.mark()
        self._cleanup_background_tasks()
        return ToolResult(llm_text=GOAL_COMPLETE_TEXT)

    def _cleanup_background_tasks(self) -> None:
        """清理全部受管后台任务（终止运行中任务进程并清空任务目录）；失败静默忽略。"""
        target = self._background
        if target is None:
            return
        cleanup = getattr(target, "cleanup_all", target)
        try:
            cleanup()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════


def register_plan_tools(registry: ToolRegistry, *, theme: PlanTheme | None = None,
                        background: BackgroundTasks | Callable[[], None] | None = None) -> None:
    """注册计划工具族：TodoWrite（定义进默认工具表）与 GoalComplete（定义不进）。

    GoalComplete 的定义经 `get_tool_definitions(include_goal=True)` 在目标模式开启时
    注入、关闭时不再传入即为移除（幂等由开关取用保证）。
    """
    registry.register(TodoWriteTool(theme))
    registry.register(GoalCompleteTool(background), expose_definition=False)
