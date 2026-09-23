"""按键采集 —— 运行模式期间的 Esc 监听（平台差异收敛为「字符源」）。

契约来源：`openspec/changes/recast-v2/specs/interrupt/spec.md`「键盘监听生命周期」
与「平台按键采集差异」（含兼容性怪癖：转义序列消费上限 5 字节、停止采集最多等待
0.2 秒且不同步确认线程退出、msvcrt 降级路径不清空输入缓冲）。**本模块的 Windows
主路径选择与采集循环退出时机为有意变更（待 spec 同步）**：

- Windows 主路径 = ReadConsoleInput 事件源（精确按键语义：只认「Esc 键按下」事件，
  交错按键不影响判定；构造后由 `self_check()` 验证句柄可等待，失败才降级）；
- msvcrt 字节流为降级路径（保留 20 毫秒窗口 / 5 字节上限判定与不清缓冲的兼容怪癖）；
- 判中断触发 `on_escape` 一次后，采集线程继续消费并丢弃按键直到停止：运行模式期间
  按键不再积压，回到输入态不出现「幽灵输入」。

结构（对齐 R6 报告发现「三套 ESC 轮询同构不共享」）：

- 平台差异只留在三个字符源适配器里（msvcrt / ReadConsoleInput / POSIX termios），
  各自只提供「读一个字节」原语与两个平台超时参数；
- 「20 毫秒判定窗口、转义序列最多消费 5 字节、消费窗口内第二个 Esc 判为连按」
  与轮询骨架（30 毫秒周期、停止检查、异常静默、触发一次后继续消费）为单一共享
  实现（`scan_escape` / `poll_keys`）——现状 native（旧 interrupt.py 139-163）、
  coninput（165-215）、unix（217-274）三处循环在此收敛；
- 采集线程为守护线程：停止信号置位后最迟一个轮询周期自行退出；模式切换先停旧
  线程再启新线程，且每个线程用独立停止信号（避免旧线程被新线程的状态清除唤醒）；
- 无法取得控制台（管道、重定向输入）或无法设置终端模式时静默降级为「无 Esc
  中断能力」：不报错、不影响其余功能。

本模块无模块级可变状态：源、线程、停止信号全部在实例内。
"""
from __future__ import annotations

import atexit
import os
import select
import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

__all__ = ["KeyListener", "KeySource", "poll_keys", "scan_escape"]

ESC_BYTE = b"\x1b"
# Esc 字节（所有平台的判定基准）

POLL_INTERVAL_SECONDS = 0.03
# 按键轮询周期（spec：采集线程以 30 毫秒级周期检查输入）

ESC_SETTLE_SECONDS = 0.02
# 单次 Esc 判定窗口：Esc 之后静默这么久仍无后续字节即判为中断

ESCAPE_SEQUENCE_LIMIT = 5
# 转义序列消费上限（字节）；连按 Esc 可能吞掉紧随其后的少量输入（兼容怪癖保持）

STOP_JOIN_TIMEOUT_SECONDS = 0.2
# 停止采集时最多等待线程退出的秒数（不同步确认，避免阻塞输入界面回归）

STD_INPUT_HANDLE = -10
# Windows 标准输入句柄编号

WAIT_FAILED = 0xFFFFFFFF
# `WaitForSingleObject` 的失败返回值（句柄无效/不可等待）；其余返回值为可用


class KeySource(Protocol):
    """字符源：把平台按键读取收敛为「读一个字节 + 平台判定参数」。

    实现须在无数据时消耗掉 timeout（或退化为更短的空闲等待），不得忙等。
    """

    escape_immediate: bool
    # True = 「Esc 键按下事件」即判定中断（Windows 非原生控制台事件源专用）

    probe_timeout: float
    # 判定窗口内探测后续字节的读取超时（秒）

    consume_timeout: float
    # 转义序列消费每一步的读取超时（秒）

    def read(self, timeout: float) -> bytes | None:
        """等待至多 timeout 秒读取一个字节；无数据返回 None。"""
        ...

    def close(self) -> None:
        """释放资源（POSIX 源恢复终端设置；其余源为空实现）。"""
        ...


class _MsvcrtSource:
    """Windows 原生控制台的 msvcrt 字符源。

    不清空输入缓冲：「回车后立即按 Esc」的 Esc 若被清掉将无法打断（兼容怪癖保持）；
    prompt_toolkit 退出后的残留转义序列由判定逻辑自然消费（单 Esc → 中断，
    序列 → 吞掉）。
    """

    escape_immediate = False
    probe_timeout = 0.0
    # 探测：静默 20 毫秒后立即查询（对齐旧实现 sleep(0.02) + kbhit）
    consume_timeout = 0.0
    # 消费：逐步立即查询（旧实现消费循环无等待）

    def __init__(self, msvcrt_module) -> None:
        self._msvcrt = msvcrt_module

    def read(self, timeout: float) -> bytes | None:
        if self._msvcrt.kbhit():
            return self._msvcrt.getch()
        if timeout <= 0:
            return None
        time.sleep(timeout)
        if self._msvcrt.kbhit():
            return self._msvcrt.getch()
        return None

    def close(self) -> None:
        """msvcrt 无资源需释放。"""


class _ConsoleInputSource:
    """Windows ReadConsoleInput 事件源（主路径；自检失败才降级 msvcrt）。

    事件级语义（spec「平台按键采集差异」）：仅「键按下且为 Esc 键」的事件触发中断，
    普通字符键不触发——Esc 键按下事件产出 Esc 字节并即刻判定（`escape_immediate`），
    方向键/功能键等其余事件不产出字节也不触发中断。
    """

    escape_immediate = True
    probe_timeout = 0.0
    consume_timeout = 0.0

    INPUT_RECORD_SIZE = 20
    KEY_EVENT = 0x0001
    VK_ESCAPE = 0x1B
    BATCH_SIZE = 8
    # INPUT_RECORD 内存布局（与旧实现逐字节一致）：
    # EventType(偏移0/2) + 填充(2) + bKeyDown(4/4) + wRepeatCount(8/2)
    # + wVirtualKeyCode(10/2) + wVirtualScanCode(12/2) + UnicodeChar(14/2)
    # + dwControlKeyState(16/4)

    def __init__(self, kernel32, ctypes_module) -> None:
        self._kernel32 = kernel32
        self._ctypes = ctypes_module
        self._handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        self._buffer = (ctypes_module.c_char
                        * (self.INPUT_RECORD_SIZE * self.BATCH_SIZE))()
        self._records_read = ctypes_module.c_ulong()

    def self_check(self) -> None:
        """自检控制台句柄可用：不可等待（无效句柄、管道输入）即抛异常（走降级）。

        `WaitForSingleObject` 返回 `WAIT_FAILED` 表示句柄无效；`WAIT_OBJECT_0`（有
        事件）与 `WAIT_TIMEOUT`（无事件）都表示句柄可用。
        """
        if self._kernel32.WaitForSingleObject(self._handle, 0) == WAIT_FAILED:
            raise OSError("控制台标准输入句柄不可等待")

    def read(self, timeout: float) -> bytes | None:
        milliseconds = int(max(timeout, 0.0) * 1000)
        if self._kernel32.WaitForSingleObject(self._handle, milliseconds) != 0:
            return None
        ok = self._kernel32.ReadConsoleInputW(
            self._handle, self._buffer, self.BATCH_SIZE,
            self._ctypes.byref(self._records_read))
        if not ok:
            raise OSError("ReadConsoleInputW 失败")
        return self._scan_escape_event()

    def _scan_escape_event(self) -> bytes | None:
        """在本批输入记录中查找「键按下且为 Esc 键」的事件。"""
        for index in range(self._records_read.value):
            offset = index * self.INPUT_RECORD_SIZE
            event_type = int.from_bytes(self._buffer[offset:offset + 2], "little")
            if event_type != self.KEY_EVENT:
                continue
            key_down = int.from_bytes(self._buffer[offset + 4:offset + 8], "little")
            if not key_down:
                continue
            key_code = int.from_bytes(self._buffer[offset + 10:offset + 12], "little")
            if key_code == self.VK_ESCAPE:
                return ESC_BYTE
        return None

    def close(self) -> None:
        """控制台句柄由进程持有，无需释放。"""


class _PosixSource:
    """POSIX（Linux/macOS）字符源：termios 单字符模式 + select 超时轮询。

    - 构造即把标准输入切到 cbreak 单字符模式；无法取得终端（管道输入、非 tty）
      时抛异常，由采集器静默降级；
    - `close()` 恢复原终端设置；进程退出兜底恢复由采集器注册 atexit（仅一次）。
    """

    escape_immediate = False
    probe_timeout = 0.01
    # 探测：判定窗口后再探 10 毫秒（对齐旧实现 sleep(0.02) + select(..., 0.01)）
    consume_timeout = 0.005
    # 消费：每一步探 5 毫秒（对齐旧实现转义序列消费循环）

    def __init__(self, fd: int, termios_module, tty_module, select_module) -> None:
        self.fd = fd
        self._termios = termios_module
        self._select = select_module
        self.old_settings = termios_module.tcgetattr(fd)
        tty_module.setcbreak(fd)

    def read(self, timeout: float) -> bytes | None:
        ready, _, _ = self._select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, 1)

    def close(self) -> None:
        """恢复终端原设置（进程退出路径，忽略一切异常）。"""
        try:
            self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self.old_settings)
        except Exception:
            pass


def scan_escape(source: KeySource) -> bool:
    """判定刚读到的 Esc 是「中断」还是「转义序列前缀」（平台无关的共享判定）。

    返回 True 表示中断（单次 Esc，或消费窗口内出现第二个 Esc 的连按）。
    平台差异只剩源上的两个超时参数：20 毫秒判定窗口、最多消费 5 字节为共享语义。
    """
    if source.escape_immediate:
        return True
    time.sleep(ESC_SETTLE_SECONDS)
    following = source.read(source.probe_timeout)
    if following is None:
        return True
    consumed = [following]
    for _ in range(ESCAPE_SEQUENCE_LIMIT - 1):
        more = source.read(source.consume_timeout)
        if more is None:
            break
        consumed.append(more)
    return ESC_BYTE in consumed


def poll_keys(source: KeySource, stop: threading.Event,
              on_escape: Callable[[], None]) -> None:
    """统一采集循环：以 30 毫秒周期读取按键，判定中断后回调一次并继续消费按键。

    覆盖旧实现三套循环（native / coninput / unix）的共享骨架：停止检查、异常静默
    退出、非 Esc 按键丢弃。**有意变更**：判中断触发一次后不结束采集线程，继续读取并
    丢弃按键直到停止（运行模式期间按键不积压，回输入态不出现「幽灵输入」）。
    """
    fired = False
    while not stop.is_set():
        try:
            key = source.read(POLL_INTERVAL_SECONDS)
            interrupted = (not fired) and key == ESC_BYTE and scan_escape(source)
        except (OSError, ValueError):
            return
        if not interrupted:
            continue
        fired = True
        try:
            on_escape()
        except Exception:
            pass
        # 继续消费按键（丢弃、不重复触发）直到停止：消灭积压与幽灵输入


class KeyListener:
    """运行模式期间的 Esc 按键采集器（守护线程 + 平台字符源）。

    - `start()`：先停旧线程，再为新线程新建独立停止信号并启动（切换不残留）；
    - `stop()`：仅置停止信号并最多等待 0.2 秒（不同步确认线程退出）；
    - 字符源在采集线程内构建：失败即静默降级（不报错、无 Esc 能力）。
    """

    def __init__(self, on_escape: Callable[[], None],
                 source_factory: Callable[[], KeySource | None] | None = None) -> None:
        self._on_escape = on_escape
        self._source_factory = source_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._atexit_registered = False

    @property
    def running(self) -> bool:
        """采集线程是否在运行（诊断与测试用）。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动采集线程（先停旧线程；为新线程分配独立停止信号）。"""
        self.stop()
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(target=self._run, args=(stop,),
                                        name="narnat-key-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止采集：仅置停止信号并最多等待 0.2 秒（不同步确认线程退出）。"""
        stop, self._stop = self._stop, threading.Event()
        stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)

    def _run(self, stop: threading.Event) -> None:
        factory = self._source_factory or self._open_source
        try:
            source = factory()
        except Exception:
            return
        if source is None:
            return
        try:
            poll_keys(source, stop, self._on_escape)
        finally:
            try:
                source.close()
            except Exception:
                pass

    def _open_source(self) -> KeySource | None:
        """按平台构建字符源；无法取得控制台时返回 None（静默降级）。"""
        if sys.platform == "win32":
            try:
                return _open_windows_source()
            except Exception:
                return None
        return self._open_posix_source()

    def _open_posix_source(self) -> KeySource | None:
        """构建 POSIX 字符源；失败返回 None。atexit 兜底恢复仅注册一次。"""
        try:
            import termios
            import tty

            source = _PosixSource(sys.stdin.fileno(), termios, tty, select)
        except Exception:
            return None
        if not self._atexit_registered:
            atexit.register(_restore_term, source.fd, source.old_settings)
            self._atexit_registered = True
        return source


def _open_windows_source() -> KeySource:
    """Windows 平台字符源：优先 ReadConsoleInput 事件源（精确按键语义）。

    事件源构造后自检句柄可等待；失败（无效句柄、管道输入）才降级 msvcrt 字节流源，
    降级路径保持不清空输入缓冲的兼容怪癖（见 `_MsvcrtSource`）。
    """
    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    try:
        source = _ConsoleInputSource(kernel32, ctypes)
        source.self_check()
        return source
    except Exception:
        return _MsvcrtSource(msvcrt)


def _restore_term(fd: int, settings) -> None:
    """atexit 兜底：恢复终端原始设置（进程退出中，忽略一切异常）。"""
    try:
        import termios

        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
    except Exception:
        pass
