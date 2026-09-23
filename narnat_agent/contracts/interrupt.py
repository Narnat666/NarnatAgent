"""中断信号协议 —— 中断标志、广播订阅与运行/输入双模式状态机的接口形态。

契约来源：
- `openspec/changes/recast-v2/specs/interrupt/spec.md`「中断广播与订阅者语义」
  （触发即置标志并广播；订阅动作失败静默容忍）、「运行/输入双模式状态机」
  （进入任一模即清除既有标志；切换时先停旧采集再启新采集）；
- design D5（模块级全局 `_interrupt_ctrl` / `_abort_callback` 合并为
  `interrupt` 积木的 `InterruptBus` 实例，本协议是其注入面）。

具体实现（InterruptBus 与按键采集器）归 `interrupt` 积木，不在契约层。

本模块为零逻辑纯定义层：只依赖标准库（typing / collections.abc）；
不 import 新包其他积木。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

__all__ = ["InterruptSignal"]


@runtime_checkable
class InterruptSignal(Protocol):
    """中断信号总线（实例化、可注入；构造时由订阅方 `subscribe` 接线）。

    - 状态：`is_set` 查询 / `clear()` 清除 / `raise_()` 触发；
    - 订阅：`subscribe(handler)`——handler 为无参回调，触发时同步调用；
      （实现须静默容忍单个订阅者异常：不影响中断标志置位与其余订阅，
      收敛由主流程检查点兜底）；
    - 双模式：`enter_run_mode()` 进入运行模式（清除标志并开始采集）、
      `enter_input_mode()` 进入输入模式（停止采集并清除标志）。
    """

    @property
    def is_set(self) -> bool:
        """中断标志是否已置位（消费方检查点与取消检查读取此值）。"""
        ...

    def clear(self) -> None:
        """清除中断标志（每次读取用户输入前等入口调用）。"""
        ...

    def raise_(self) -> None:
        """触发中断：置中断标志并同步广播给全部订阅者。

        典型订阅者（specs/interrupt「中断广播与订阅者语义」）：LLM 活动连接关闭、
        前台命令进程树终止、远程会话发送中断字符、串口会话发送中断字符、后台
        等待唤醒、压缩放弃。
        """
        ...

    def subscribe(self, handler: Callable[[], None]) -> None:
        """注册无参订阅回调（订阅方在构造时接线；触发时同步调用）。"""
        ...

    def enter_input_mode(self) -> None:
        """进入输入模式：停止按键采集（仅置停止信号并最多等待 0.2 秒）并清除
        中断标志；等待用户输入期间不消费、不吞掉输入会话的任何按键。"""
        ...

    def enter_run_mode(self) -> None:
        """进入运行模式：清除中断标志并启动按键采集（运行模式期间响应 Esc）。

        进入时机（specs/interrupt「运行/输入双模式状态机」）：创建流式输出会话时、
        手动压缩执行期间（临时）。
        """
        ...
