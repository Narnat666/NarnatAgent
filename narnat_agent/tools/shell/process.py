"""进程与运行态原语 —— 前台执行器与后台槽位共用的底层能力。

契约来源：specs/tools-shell「前台执行、超时与中断」（进程树终止、收尾宽限、
输出解码回退、shell 查找）与「后台任务提交与槽位」（UTF-8 编码环境、进程隔离）。

结构决策（对齐任务书「跨模块私有依赖改为显式共享」）：旧实现把杀树原语
`_kill_proc_tree` 与子进程环境 `BashRuntime.utf8_env` 放在 bash 模块内，被
`tools/background` 私有 import；本模块把杀树、收尾宽限、解码、可执行查找与
UTF-8 环境收敛为公开接口，前台执行器（`executor.py`）与后台槽位
（`background.py`）共同 import，不再互摸私有实现。

前台运行态（当前前台进程 + 中断标志）的属主是 `ShellRuntime`：旧实现是
`BashRuntime` 的类属性（后台模块直接读写 `BashRuntime.interrupted`），新结构
改为实例 + 显式动作（中断 / 清零 / 消费 / 挂载 / 摘除），语义等价。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

__all__ = [
    "DRAIN_GRACE",
    "UTF8_ENV",
    "ShellRuntime",
    "decode_output",
    "drain_readers",
    "find_executable",
    "kill_proc_tree",
]

DRAIN_GRACE = 0.3
"""读线程收尾宽限（秒）：收尾时为全部读线程共享一个截止（非逐个 join）。"""

# 子进程环境变量：强制 UTF-8 编码，解决 Windows 下 Python print emoji 等
# Unicode 字符在 GBK 代码页下报 UnicodeEncodeError 的问题
UTF8_ENV = os.environ.copy()
UTF8_ENV["PYTHONIOENCODING"] = "utf-8"
UTF8_ENV["PYTHONUTF8"] = "1"


def kill_proc_tree(proc: subprocess.Popen) -> None:
    """终止整棵进程树（Windows 走 `taskkill /F /T`、Unix 走进程组 SIGKILL）。

    已退出的进程直接短路；杀树失败静默容忍（收敛由调用方的收尾逻辑兜底）。
    """
    if proc.poll() is not None:
        return
    pid = proc.pid
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def drain_readers(*threads: threading.Thread) -> None:
    """读线程收尾：共享一个宽限，而非逐个 join 各自的超时。

    `start /b` 分离出的孙进程会继承管道写端，cmd.exe 退出后管道仍不 EOF，
    读线程一直阻塞在 read()；逐个 join(timeout=5) 会串成 10 秒天花板，
    命令早已结束却仍要等满。管道背压使 cmd 退出时缓冲留存有界（毫秒级可
    读完），故共享宽限只对"永不 EOF"的分离场景生效，正常命令零影响。
    """
    deadline = time.time() + DRAIN_GRACE
    for t in threads:
        remain = deadline - time.time()
        if remain > 0:
            t.join(timeout=remain)


def decode_output(raw: bytes) -> str:
    """安全解码子进程输出：UTF-8 优先；Windows 下失败回退 GBK；仍失败按替换字符兜底。

    Unix 无 GBK 回退（与旧实现一致）。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if sys.platform == "win32":
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def find_executable(*names: str) -> Optional[str]:
    """按优先级查找可执行文件，返回第一个找到的名称或路径（皆无返回 None）。"""
    for name in names:
        if shutil.which(name):
            return name
    return None


class ShellRuntime:
    """前台运行态属主 —— 当前前台进程与中断标志（跨调用共享、跨模块显式 API）。

    - `interrupt()`：中断入口（ESC 打断、流取消），置标志并无阻塞终止当前前台
      进程树——杀树在后台线程执行，中断方立即返回；
    - `clear_interrupt()`：入口清零（每次执行/等待入口调用，上轮残留不污染本轮）；
    - `consume_interrupt()`：消费中断标志（读后清零，前台执行循环与后台等待循环
      的检查点）；
    - `attach()` / `detach()`：挂载与摘除当前前台进程引用。

    旧实现中这两项状态住在 `BashRuntime` 的类属性里、被后台模块直接读写；
    新结构收进实例并只暴露上述显式动作，语义等价（含"入口清零"惯例）。
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._interrupted = False

    def interrupt(self) -> None:
        """中断入口：置中断标志，并异步终止当前前台进程树（无阻塞）。

        后台线程持有旧 proc 引用；新命令是新 Popen，挂载时会覆盖当前引用，
        故杀树期间用户输入新命令不受影响。
        """
        self._interrupted = True
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            threading.Thread(target=kill_proc_tree, args=(proc,), daemon=True).start()

    def clear_interrupt(self) -> None:
        """清除中断标志（执行/等待入口清零惯例）。"""
        self._interrupted = False

    def consume_interrupt(self) -> bool:
        """消费中断标志：返回消费前是否已中断，并把标志复位。"""
        was_interrupted = self._interrupted
        self._interrupted = False
        return was_interrupted

    def attach(self, proc: subprocess.Popen) -> None:
        """挂载当前前台进程（供中断路径杀树；收尾时须 `detach`）。"""
        with self._lock:
            self._proc = proc

    def detach(self) -> None:
        """摘除当前前台进程引用。"""
        with self._lock:
            self._proc = None
