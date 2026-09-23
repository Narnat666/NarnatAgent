"""终端输出原语与终端能力适配。

契约来源：`openspec/changes/recast-v2/specs/output/spec.md`
- 「全局输出开关」：纯文本模式（颜色求值为空、无行首回车、不重申终端能力、
  运行期实时查询）与静默工具日志（仅提供开关查询，输出抑制由消费方执行）；
- 「终端能力适配」：Windows 虚拟终端启用与写出前重申、真彩能力启动时检测并固定、
  无控制台句柄（管道/重定向）时跳过重申；
- 「输出原语与行控制」：阻塞写出（互斥锁串行 + 立即刷新 + 非 plain 时前置
  行首回车）、非阻塞写出（拿不到锁立即放弃、不补回车）。

结构（design D7）：所有状态（plain/quiet/锁/VT 句柄/真彩能力）由 `Console`
实例承载，无模块级全局；消费方（headless 装配、渲染层、工具调度日志等）
构造注入同一实例。
"""
from __future__ import annotations

import os
import sys
import threading
from typing import IO

__all__ = ["Console", "detect_truecolor"]

# Windows 控制台 mode 位：启用虚拟终端处理（ANSI 序列被解析而非按字符占位）
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# GetStdHandle 的标准输出句柄编号
STD_OUTPUT_HANDLE = -11

# 真彩声明的环境变量取值
TRUECOLOR_ENV_VALUES = ("truecolor", "24bit")


def detect_truecolor(platform: str | None = None) -> bool:
    """真彩能力检测（启动时一次，结果固定）。

    `COLORTERM` 为 `truecolor`/`24bit`、存在 `WT_SESSION`、`ConEmuANSI` 为
    `ON`、或 Windows 控制台已启用虚拟终端处理——任一成立即真彩；否则降级。
    检测异常（无 ctypes 控制台等）静默为 False。
    """
    plat = sys.platform if platform is None else platform
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in TRUECOLOR_ENV_VALUES:
        return True
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("ConEmuANSI") == "ON":
        return True
    if plat == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
                    return True
        except Exception:
            pass
    return False


class Console:
    """终端输出原语（旧实现 output 模块单例语义的实例化承载）。

    生命周期（装配顺序）：构造（启用 Windows VT + 固定真彩能力）→
    headless 启动时 `set_plain(True)` / `set_quiet_tools(True)` →
    运行期 `write` / `try_write`（写出前重申 VT）。
    """

    def __init__(
        self,
        stdout: IO[str] | None = None,
        *,
        truecolor: bool | None = None,
        platform: str | None = None,
    ):
        """
        - `stdout`：输出目标（缺省动态跟随 `sys.stdout`；测试可注入 StringIO）；
        - `truecolor`：真彩能力（默认按 `detect_truecolor` 检测并固定）；
        - `platform`：平台标识（默认 `sys.platform`；测试可注入）。
        """
        self._stdout = stdout
        self._platform = sys.platform if platform is None else platform
        self._plain = False
        self._quiet_tools = False
        self._lock = threading.Lock()
        self._vt_handle = None
        # 先启用 VT 再检测真彩：让能力检测看到 VT 已开启（Win 控制台场景）
        self._enable_vt()
        self._truecolor = (
            detect_truecolor(self._platform) if truecolor is None else bool(truecolor)
        )

    def _target(self) -> IO[str]:
        """写出目标（未注入时动态跟随 `sys.stdout`，与旧实现行为一致）。"""
        return sys.stdout if self._stdout is None else self._stdout

    # ── 输出开关（纯文本模式 / 静默工具日志）──

    def set_plain(self, plain: bool = True) -> None:
        """切换纯文本模式（headless 一次性任务启动时置位）。"""
        self._plain = bool(plain)

    def is_plain(self) -> bool:
        """查询纯文本模式（渲染器等按当前值实时判断，不做启动期快照）。"""
        return self._plain

    def set_quiet_tools(self, quiet: bool = True) -> None:
        """切换工具调度日志静默（headless 默认开启，`-l` 关闭）。"""
        self._quiet_tools = bool(quiet)

    def is_quiet_tools(self) -> bool:
        """查询工具日志静默开关（工具调度/回调按当前值判断）。"""
        return self._quiet_tools

    # ── 终端能力 ──

    @property
    def truecolor(self) -> bool:
        """真彩能力（构造时固定）。"""
        return self._truecolor

    @property
    def lock(self) -> threading.Lock:
        """写出互斥锁（供需要整块原子写出的消费方，如动画暂停/恢复）。"""
        return self._lock

    # ── 写出原语 ──

    def write(self, text: str) -> None:
        """阻塞写出：串行化 + 立即刷新；非 plain 时文本前补行首回车。

        行首回车用于 prompt_toolkit 退出后交还界面的行首对齐；纯文本模式
        （重定向到文件）不补回车、不重申终端能力，输出干净。
        """
        with self._lock:
            stream = self._target()
            if self._plain:
                stream.write(text)
            else:
                self._assert_vt()
                stream.write("\r" + text)
            stream.flush()

    def try_write(self, text: str) -> bool:
        """非阻塞写出：拿不到互斥锁立即放弃并返回 False（动画帧掉帧容错）。

        不补行首回车（原地刷新由调用方自行实现回车 + 擦除序列）；写出前
        重申终端能力（与阻塞写出同）。
        """
        if self._lock.acquire(blocking=False):
            try:
                stream = self._target()
                self._assert_vt()
                stream.write(text)
                stream.flush()
                return True
            finally:
                self._lock.release()
        return False

    # ── Windows 虚拟终端（内部）──

    def _enable_vt(self) -> None:
        """在 Windows 控制台上启用 VT 处理，并缓存句柄供写出前重申。

        仅在构造时设置一次不够：Shell/Terminal 工具派生的子进程（cmd.exe、
        ssh 等使用 legacy console API 的程序）共享同一控制台，可能把
        console mode 重置回无 VT 状态，故 `write`/`try_write` 每次写出前
        重申（见 `_assert_vt`）。非 Windows / 无控制台（管道、重定向）时
        句柄保持 None，重申被跳过，ANSI 序列原样输出。
        """
        if self._platform != "win32":
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            if not handle or handle in (-1, 0):
                return
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
                self._vt_handle = handle
        except Exception:
            pass

    def _assert_vt(self) -> None:
        """写出前重申 VT 已开启（防子进程把 console mode 重置回 legacy）。"""
        if self._vt_handle is None:
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(self._vt_handle, ctypes.byref(mode)):
                if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                    kernel32.SetConsoleMode(
                        self._vt_handle,
                        mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                    )
        except Exception:
            pass
