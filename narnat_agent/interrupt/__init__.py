"""interrupt 积木 —— 中断信号总线（实例）与按键采集器。

- `InterruptBus`：实现 `contracts.interrupt.InterruptSignal`——中断标志、广播订阅、
  运行/输入双模式状态机（design D5：替换现状模块级 `_interrupt_ctrl` 单例与
  `_abort_callback` 全局，实例化、可注入、可单测）；
- `KeyListener` / `KeySource` / `poll_keys` / `scan_escape`：运行模式期间的 Esc 按键
  采集——现状 Windows msvcrt、Windows ReadConsoleInput、POSIX termios 三套轮询
  收敛为同一采集器，平台差异只剩字符源适配与两个判定超时参数。

依赖规则：本积木只依赖标准库（L1，见 `tests/check_layering.py`）；中断的消费方经
`contracts` 的 `InterruptSignal` 协议与本积木交互，订阅自各自积木在构造时接线。
"""
from __future__ import annotations

from .bus import InterruptBus
from .keys import KeyListener, KeySource, poll_keys, scan_escape

__all__ = [
    "InterruptBus",
    "KeyListener",
    "KeySource",
    "poll_keys",
    "scan_escape",
]
