"""目标模式状态 —— 开关、临时轮数上限、配置默认轮数与完成标记工具的注入。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`（「目标模式命令
（/goal）」）与 `specs/conversation`（「目标模式自动续跑与强制收尾轮」）。

- 开启：向 LLM 动态注入目标完成标记工具（GoalComplete），可带临时轮数上限
  （正整数，本轮有效）；
- 关闭：清除临时轮数上限并移除目标完成标记工具；
- 生效轮数：临时覆盖（大于 0 时优先）> 配置默认值；两者均为 0 时语义为「默认」；
- 消费方：`/goal` 命令（本积木命令层）与 conversation / app 的续跑判定
  （读 `enabled` / `max_rounds`，design D7：替代对会话管理器私有字段的互摸）。
"""
from __future__ import annotations

from typing import Callable, Optional

__all__ = ["GoalMode"]


class GoalMode:
    """目标模式状态（开关 + 临时轮数上限 + 配置默认上限 + 工具注入端口）。

    构造注入 `default_max_rounds`（配置默认轮数上限，0 = 未配置）与可选的
    `tool_gate`（目标完成标记工具的注入/移除开关，如 `llm.set_goal_tool`）。
    """

    def __init__(self, default_max_rounds: int = 0,
                 tool_gate: Optional[Callable[[bool], None]] = None) -> None:
        self._enabled = False
        self._override = 0
        self._default_max_rounds = int(default_max_rounds or 0)
        self._tool_gate = tool_gate

    # ── 读 ──

    @property
    def enabled(self) -> bool:
        """目标模式是否开启。"""
        return self._enabled

    @property
    def override_max_rounds(self) -> int:
        """临时轮数上限（0 = 未设置）。"""
        return self._override

    @property
    def default_max_rounds(self) -> int:
        """配置默认轮数上限（0 = 未配置）。"""
        return self._default_max_rounds

    @property
    def max_rounds(self) -> int:
        """生效轮数上限：临时覆盖（大于 0 时优先）> 配置默认值（0 = 均为空）。"""
        return self._override if self._override > 0 else self._default_max_rounds

    # ── 写 ──

    def turn_on(self, override_max_rounds: int = 0) -> None:
        """开启目标模式并注入完成标记工具；`override_max_rounds` 大于 0 时设为临时上限。"""
        self._enabled = True
        self._override = max(0, int(override_max_rounds or 0))
        self._set_tool(True)

    def turn_off(self) -> bool:
        """关闭目标模式、清除临时轮数上限并移除完成标记工具；返回关闭前是否开启。"""
        was_enabled = self._enabled
        self._enabled = False
        self._override = 0
        self._set_tool(False)
        return was_enabled

    # ── 内部 ──

    def _set_tool(self, enabled: bool) -> None:
        """经注入端口注入/移除目标完成标记工具（未注入端口时为 no-op）。"""
        if self._tool_gate is not None:
            self._tool_gate(enabled)
