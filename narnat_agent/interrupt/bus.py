"""中断总线 —— 中断标志、广播订阅与运行/输入双模式状态机（实例）。

契约来源：

- `openspec/changes/recast-v2/specs/interrupt/spec.md`：中断来源与按键语义（Esc 主来源、
  Ctrl+C 信号按打包形态忽略 / 源码形态默认处理）、运行·输入双模式状态机（进入任一
  模式即清除既有标志；切换先停旧采集再启新采集）、中断广播与订阅者语义（触发即置
  标志并广播；订阅动作失败静默容忍）、兼容性怪癖保持（无「保存并恢复原处理」路径、
  广播整体吞异常）；
- design D5：现状模块级 `_interrupt_ctrl` 单例（`narnat_agent/ui/interrupt.py`）与
  `_abort_callback` 全局（`narnat_agent/core/interrupt.py`）合并为本积木的 `InterruptBus`
  实例——UI 采集按键 → `bus.raise_()`；订阅方（LLM 断连、前台进程杀树、远程/串口
  中断、后台等待唤醒、压缩放弃）在构造时 `subscribe` 接线。

双模式语义与轮询时机保持现状等价（见 `keys.py`）。本模块无模块级可变状态：
中断标志、订阅表、采集器全部在实例内。
"""
from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from .keys import KeyListener

__all__ = ["InterruptBus"]


class _ListenerPort(Protocol):
    """模式机所需的采集器面（注入替身只需实现 start/stop）。"""

    def start(self) -> None:
        """启动采集（实现须先停旧采集，保证同一时刻至多一个采集线程）。"""
        ...

    def stop(self) -> None:
        """停止采集（仅置停止信号并最多等待 0.2 秒）。"""
        ...


class InterruptBus:
    """中断信号总线（实现 `contracts.interrupt.InterruptSignal`；实例化、可注入）。

    - 状态：`is_set` 查询 / `clear()` 清除 / `raise_()` 触发（先置标志，再同步广播）；
    - 订阅：`subscribe(handler)`——无参回调，触发时按注册顺序同步调用；单个订阅者
      异常被静默吞掉，不影响中断标志置位与其余订阅（收敛由主流程检查点兜底）；
    - 双模式：`enter_run_mode()`（清除标志并启动 Esc 采集）/ `enter_input_mode()`
      （停止采集——最多等 0.2 秒——并清除标志）；进入任一模都重设 Ctrl+C 处理。
    """

    def __init__(self, listener: _ListenerPort | None = None) -> None:
        self._signal = threading.Event()
        self._handlers: list[Callable[[], None]] = []
        self._listener: _ListenerPort = (
            listener if listener is not None else KeyListener(self.raise_))

    @property
    def is_set(self) -> bool:
        """中断标志是否已置位（消费方检查点与取消检查读取此值）。"""
        return self._signal.is_set()

    def clear(self) -> None:
        """清除中断标志（每次读取用户输入前等入口调用）。"""
        self._signal.clear()

    def subscribe(self, handler: Callable[[], None]) -> None:
        """注册无参订阅回调（订阅方在构造时接线；触发时按注册顺序同步调用）。"""
        self._handlers.append(handler)

    def raise_(self) -> None:
        """触发中断：先置标志，再同步广播给全部订阅者（逐个吞异常）。

        典型订阅者（specs/interrupt「中断广播与订阅者语义」）：关闭 LLM 活动连接、
        终止前台命令进程树、远程会话发送中断字符、串口会话发送中断字符、后台等待
        唤醒、压缩放弃。兼容怪癖保持：任一订阅动作失败静默容忍——不影响中断标志
        置位，也不阻断其余订阅。
        """
        self._signal.set()
        for handler in tuple(self._handlers):
            try:
                handler()
            except Exception:
                pass

    def enter_run_mode(self) -> None:
        """进入运行模式：清除标志并启动 Esc 采集（创建流式输出会话、手动压缩期间）。

        现状等价：先清除既有中断标志（使上一局残留中断不影响新一局），再停旧采集
        线程、以独立停止信号启动新采集线程，最后重设 Ctrl+C 处理。
        """
        self.clear()
        self._listener.start()
        _set_sigint_handler()

    def enter_input_mode(self) -> None:
        """进入输入模式：停止 Esc 采集（最多等 0.2 秒）并清除中断标志。

        进入时机（调用方）：启动时、每次读取用户输入前、流式输出正常结束时、
        流式输出中止时、中断收敛回调时。等待用户输入期间不启动采集线程，从而
        不消费、不吞掉输入会话的任何按键。
        """
        self._listener.stop()
        self.clear()
        _set_sigint_handler()


def _set_sigint_handler() -> None:
    """设置 Ctrl+C 处理：打包形态忽略（防杀进程，Esc 为唯一中断手段），源码形态交默认处理。

    兼容怪癖保持：现状 `_saved_sigint` 恒为 None（从不赋值），「保存并恢复原处理」
    分支不可达——实际行为恒为上二者之一，故此处只保留恒定分支。
    `signal.signal` 只能在主线程调用，非主线程调用静默跳过。
    """
    if threading.current_thread() is not threading.main_thread():
        return
    if getattr(sys, "frozen", False):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, signal.default_int_handler)
