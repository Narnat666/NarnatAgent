"""工具运行时环境 —— `ToolEnv` 协议实现与各状态 Tracker（状态归主）。

契约来源：`contracts/tool.py`（ToolSettings / PlanTracker / GoalState / ReminderState /
DeleteGate / ToolEnv 六个协议）+ specs/tools-file「工具运行时上下文数据语义」、
specs/tools-todo「计划状态」「目标完成标记的主循环消费」「收尾软提醒」、
specs/tools-shell「非 Windows 挂起确认」、design D6（状态归主：每项状态只有一个修改入口）。

设计要点：
- 本模块的实现均为「单一属主 + 显式 API」：写入路径见各方法 docstring，
  消灭旧 `ToolContext` 的跨模块私有互摸（`_delete_confirmed` 等）；
- 无模块级状态：装配点构造实例、经 `ToolEnvImpl` 聚合注入，各工具只读；
- `ToolSettingsImpl` 提供全部默认值的零参构造；配置驱动的实现由 config 积木提供并可替换；
- 契约中的 `ReadFileTracker` 不予实现（specs/tools-file 明确规定不追踪已读文件）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts.tool import (
    DeleteGate,
    GoalState,
    McpPort,
    PlanTracker,
    ReminderState,
    TodoItem,
    ToolSettings,
)

__all__ = [
    "DeleteGateImpl",
    "GoalStateImpl",
    "PlanTrackerImpl",
    "ReminderStateImpl",
    "ToolEnvImpl",
    "ToolSettingsImpl",
]


@dataclass
class ToolSettingsImpl:
    """`ToolSettings` 协议的默认实现 —— 工具只读配置面（零参构造，全部默认值）。

    默认值对齐 specs/tools-file「零参构造」场景：输出上限 65536、超时上限 1800、
    忽略目录空、计划优先关闭、阈值 2、传输上限 100。可变默认（列表/字典）使用
    `default_factory`，逐实例独立、不共享。
    """

    ignore_dirs: list[str] = field(default_factory=list)
    max_tool_output_chars: int = 65536
    max_timeout_seconds: int = 1800
    max_transfer_mb: int = 100
    git_skip_confirm: bool = False
    rm_skip_confirm: bool = False
    require_plan: bool = False
    min_tools: int = 2
    api_keys: dict[str, str] = field(default_factory=dict)

    def get_api_key(self, name: str) -> str:
        """查询 API 密钥；未配置的键返回空串，不抛键错误。"""
        return self.api_keys.get(name, "")


class PlanTrackerImpl:
    """`PlanTracker` 协议的实现 —— 当前计划的唯一属主（TodoWrite 写，拦截/提醒读）。"""

    def __init__(self) -> None:
        self._todos: list[TodoItem] = []

    def current(self) -> list[TodoItem]:
        """当前计划列表（无计划时为空列表）；返回本体，保持引用语义。"""
        return self._todos

    def replace(self, todos: list[TodoItem]) -> None:
        """整体替换当前计划（TodoWrite 每次提交完整列表）。

        直接持有提交列表的引用：与提交列表共享元素对象（specs/tools-todo 兼容怪癖
        「就地修正」——多项 in_progress 的就地降级须同时反映到计划状态）。
        """
        self._todos = todos


class GoalStateImpl:
    """`GoalState` 协议的实现 —— 目标完成标记（GoalComplete 置位、主循环消费并复位）。"""

    def __init__(self) -> None:
        self._is_set = False

    @property
    def is_set(self) -> bool:
        """标记是否已置位（统计栏结案信号等只读场景）。"""
        return self._is_set

    def mark(self) -> None:
        """置位（GoalComplete 通过收尾校验后调用）。"""
        self._is_set = True

    def consume(self) -> bool:
        """消费即复位：返回消费前是否已置位，并把标记复位。"""
        was_set = self._is_set
        self._is_set = False
        return was_set

    def reset(self) -> None:
        """复位（用户提交新输入时调用，防止普通模式残留误判）。"""
        self._is_set = False


class ReminderStateImpl:
    """`ReminderState` 协议的实现 —— 收尾软提醒标志（计划/后台各一，触发一次语义）。

    两种提醒各自独立计数、互不影响；标志由新任务输入复位。
    """

    def __init__(self) -> None:
        self._plan_triggered = False
        self._bg_triggered = False

    def try_trigger_plan(self) -> bool:
        """尝试触发计划提醒：未触发过则置位并返回 True；已触发过返回 False。"""
        if self._plan_triggered:
            return False
        self._plan_triggered = True
        return True

    def try_trigger_bg(self) -> bool:
        """尝试触发后台任务提醒：未触发过则置位并返回 True；已触发过返回 False。"""
        if self._bg_triggered:
            return False
        self._bg_triggered = True
        return True

    def reset(self) -> None:
        """复位两个标志（用户提交新输入时调用）。"""
        self._plan_triggered = False
        self._bg_triggered = False


class DeleteGateImpl:
    """`DeleteGate` 协议的实现 —— 删除确认挂起门（非 Windows；工具写、内循环消费）。

    取代旧 `ToolContext.pending_delete` + `_delete_confirmed` 私有字段：
    「暂存待确认调用」与「已确认一次性标记」各有显式 API，跨模块不再互摸私有成员。
    """

    def __init__(self) -> None:
        self._pending: tuple[str, dict[str, object]] | None = None
        self._confirmed = False

    def pend(self, tool_name: str, arguments: dict[str, object]) -> None:
        """暂存待确认的调用（工具名与完整参数），命令未执行。"""
        self._pending = (tool_name, arguments)

    def take(self) -> tuple[str, dict[str, object]] | None:
        """取出暂存调用（取出后清空暂存）；无暂存时返回 None。"""
        pending = self._pending
        self._pending = None
        return pending

    def mark_confirmed(self) -> None:
        """置「已确认」一次性标记（用户在提示符下确认后调用）。"""
        self._confirmed = True

    def consume_confirmed(self) -> bool:
        """消费「已确认」标记：返回消费前是否已确认，并把标记复位。

        工具执行入口调用：True=本次调用已确认，跳过挂起直接执行。
        """
        was_confirmed = self._confirmed
        self._confirmed = False
        return was_confirmed


class ToolEnvImpl:
    """`ToolEnv` 协议的实现 —— 聚合注入的运行时门面（装配点唯一构造，各工具只读）。

    未注入的组件按契约语义缺省：`delete_gate` 等 tracker 缺省时自动构造；
    `confirm` 为 None 表示未注入（headless：命中删除类/git 的命令不拦截、直接执行）；
    `mcp` 为 None 表示未初始化（MCP 工具返回「MCP 管理器未初始化」形态错误行）。
    """

    def __init__(
        self,
        settings: ToolSettings | None = None,
        plan: PlanTracker | None = None,
        goal: GoalState | None = None,
        reminders: ReminderState | None = None,
        delete_gate: DeleteGate | None = None,
        confirm: Callable[[str], bool] | None = None,
        mcp: McpPort | None = None,
    ) -> None:
        self._settings = settings if settings is not None else ToolSettingsImpl()
        self._plan = plan if plan is not None else PlanTrackerImpl()
        self._goal = goal if goal is not None else GoalStateImpl()
        self._reminders = reminders if reminders is not None else ReminderStateImpl()
        self._delete_gate = delete_gate if delete_gate is not None else DeleteGateImpl()
        self._confirm = confirm
        self._mcp = mcp

    @property
    def settings(self) -> ToolSettings:
        """只读配置面。"""
        return self._settings

    @property
    def plan(self) -> PlanTracker:
        """当前计划 tracker。"""
        return self._plan

    @property
    def goal(self) -> GoalState:
        """目标完成标记。"""
        return self._goal

    @property
    def reminders(self) -> ReminderState:
        """收尾软提醒标志。"""
        return self._reminders

    @property
    def delete_gate(self) -> DeleteGate:
        """删除确认挂起门。"""
        return self._delete_gate

    @property
    def confirm(self) -> Callable[[str], bool] | None:
        """同步确认回调（仅 Windows 使用；None 表示未注入）。"""
        return self._confirm

    @property
    def mcp(self) -> McpPort | None:
        """MCP 连接管理端口引用（None 表示未初始化）。"""
        return self._mcp
