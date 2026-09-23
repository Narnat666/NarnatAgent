"""目标模式 —— 续跑判定、轮计数与强制收尾轮指令（TurnOutcome 驱动）。

契约来源：`openspec/changes/recast-v2/specs/conversation/spec.md`
「目标模式自动续跑与强制收尾轮」「兼容性怪癖保持」：

每轮结束后按固定顺序判定——非目标模式 → 结束；本轮被中断 → 结束；本轮非正常
完成 → 结束；AI 已声明完成（`GoalState.consume()`）→ 复位并结束；达到轮数上限
（"达到即收尾"，上限为负值时首轮结束即触发）→ 注入收尾指令并执行一轮强制收尾；
否则注入续跑提示并继续下一轮。每轮结束轮计数加一；轮数上限取临时覆盖值
（大于 0 时）或配置默认值。

判定结果以 `GoalDecision` 返回，由主循环（app）执行注入、建流与保存等动作；
文案为已发布字面量，精确搬运旧实现 `narnat_agent/core/agent.py`（139-168 行）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.tool import GoalState
from .loop import TurnOutcome

__all__ = [
    "CONTINUE_TEMPLATE",
    "FINAL_ROUND_TEMPLATE",
    "GOAL_CONTINUE",
    "GOAL_END",
    "GOAL_FINAL",
    "GoalDecision",
    "GoalMode",
]

GOAL_END = "end"
"""结束续跑（不注入任何消息）。"""

GOAL_CONTINUE = "continue"
"""注入续跑提示并继续下一轮。"""

GOAL_FINAL = "final"
"""注入收尾指令并执行一轮强制收尾（该轮显示统计栏）。"""

FINAL_ROUND_TEMPLATE = (
    "【系统提示】目标模式已达到轮数上限（{limit}轮），任务尚未完成。"
    "请向用户总结当前进度、已完成工作和未完成原因，无需继续执行新任务。"
)

CONTINUE_TEMPLATE = (
    "【自动续跑】已完成{rounds}轮，任务：{task}\n"
    "请继续推进任务。若任务已完成，请调用GoalComplete工具声明完成。"
)


@dataclass(frozen=True)
class GoalDecision:
    """一轮结束后的续跑判定结果。

    - `action`：`end` / `continue` / `final` 三选一；
    - `text`：需要注入历史的用户消息（`continue` 与 `final` 时非空，均为 `【…】`
      开头的系统文案；`end` 时为空串）。
    """

    action: str
    text: str = ""

    @property
    def ends(self) -> bool:
        """是否结束续跑。"""
        return self.action == GOAL_END

    @property
    def continues(self) -> bool:
        """是否注入续跑提示并自动开始下一轮。"""
        return self.action == GOAL_CONTINUE

    @property
    def needs_final_round(self) -> bool:
        """是否需要执行一轮强制收尾（达到轮数上限）。"""
        return self.action == GOAL_FINAL


class GoalMode:
    """目标模式状态机（轮计数 / 轮数上限 / 任务内容 / 完成标记消费）。

    生命周期：用户输入解析出目标模式开启时 `start(任务, 临时上限)`；此后每轮结束
    调用 `after_round(outcome)` 取判定；强制收尾轮执行完毕后由主循环结束整轮调度。
    非目标模式期间 `after_round` 恒返回结束（不计数）。
    """

    def __init__(self, goal_state: GoalState, default_max_rounds: int) -> None:
        """构造目标模式状态机。

        - `goal_state`：目标完成标记（GoalComplete 工具置位；本类消费并复位）；
        - `default_max_rounds`：配置默认的轮数上限（临时覆盖值缺失/为 0 时使用）。
        """
        self._goal = goal_state
        self._default_max_rounds = default_max_rounds
        self._active = False
        self._task = ""
        self._limit = 0
        self._rounds = 0

    # ── 状态查询 ──

    @property
    def active(self) -> bool:
        """目标模式是否开启（未开启时判定恒为结束）。"""
        return self._active

    @property
    def rounds(self) -> int:
        """已完成的轮数（每轮结束后加一）。"""
        return self._rounds

    @property
    def limit(self) -> int:
        """当前轮数上限（临时覆盖值大于 0 时优先，否则配置默认值）。"""
        return self._limit

    @property
    def task(self) -> str:
        """本次目标任务内容（进入目标模式时的用户输入）。"""
        return self._task

    # ── 生命周期 ──

    def start(self, task: str, max_rounds: int = 0) -> None:
        """进入目标模式：记录任务内容、解析轮数上限并清零轮计数。

        `max_rounds > 0` 时作为临时覆盖值（headless `-g`），否则用配置默认值。
        """
        self._task = task
        self._limit = max_rounds or self._default_max_rounds
        self._rounds = 0
        self._active = True

    def stop(self) -> None:
        """退出目标模式（不再续跑；轮计数与任务内容保留供查询）。"""
        self._active = False

    # ── 轮后判定 ──

    def after_round(self, outcome: TurnOutcome) -> GoalDecision:
        """一轮结束后的固定判定（判定顺序即契约，不得重排）。

        每轮结束轮计数加一；中断、错误、空回复（非正常完成）与非目标模式一律结束；
        声明完成时复位标记并结束；达到轮数上限时给出收尾指令，否则给出续跑提示。
        """
        if not self._active:
            return GoalDecision(GOAL_END)
        self._rounds += 1
        if outcome.interrupted:
            return GoalDecision(GOAL_END)
        if not outcome.completed:
            return GoalDecision(GOAL_END)
        if self._goal.consume():
            return GoalDecision(GOAL_END)
        if self._rounds >= self._limit:
            return GoalDecision(
                GOAL_FINAL, FINAL_ROUND_TEMPLATE.format(limit=self._limit)
            )
        return GoalDecision(
            GOAL_CONTINUE,
            CONTINUE_TEMPLATE.format(rounds=self._rounds, task=self._task),
        )
