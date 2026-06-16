"""
中断控制器 ── 封装 ESC/SIGINT 中断管理（线程安全）

状态机: INPUT_MODE ↔ RUN_MODE
"""

import os
import sys
import signal
import threading
import time
from typing import Optional


class InterruptController:
    """
    线程安全的 ESC/SIGINT 中断管理。
    状态机: INPUT_MODE ↔ RUN_MODE
    """

    def __init__(self) -> None:
        self._interrupt = threading.Event()
        self._stop_poll = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._saved_sigint = None

    @property
    def is_set(self) -> bool:
        return self._interrupt.is_set()

    def clear(self) -> None:
        self._interrupt.clear()

    def enter_input_mode(self) -> None:
        self._stop_poll.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        self._interrupt.clear()
        # signal.signal只能在主线程调用
        if threading.current_thread() is threading.main_thread():
            if self._saved_sigint is not None:
                signal.signal(signal.SIGINT, self._saved_sigint)
                self._saved_sigint = None
            else:
                # 编译为exe后Ctrl+C会直接杀进程导致崩溃，忽略SIGINT，用ESC中断
                if getattr(sys, 'frozen', False):
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                else:
                    signal.signal(signal.SIGINT, signal.default_int_handler)

    def enter_run_mode(self) -> None:
        self._interrupt.clear()
        # 每个轮询线程用自己独立的stop event，避免旧线程被新线程的clear唤醒
        self._stop_poll = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_esc,
                                             args=(self._stop_poll,), daemon=True)
        self._poll_thread.start()
        # signal.signal只能在主线程调用
        if threading.current_thread() is threading.main_thread():
            if self._saved_sigint is not None:
                signal.signal(signal.SIGINT, self._saved_sigint)
                self._saved_sigint = None
            else:
                # 编译为exe后Ctrl+C会直接杀进程导致崩溃，忽略SIGINT，用ESC中断
                if getattr(sys, 'frozen', False):
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                else:
                    signal.signal(signal.SIGINT, signal.default_int_handler)

    def _poll_esc(self, stop: threading.Event) -> None:
        """轮询ESC键。Windows使用msvcrt，Unix使用select+termios。"""
        if sys.platform == "win32":
            self._poll_esc_windows(stop)
        else:
            self._poll_esc_unix(stop)

    def _poll_esc_windows(self, stop: threading.Event) -> None:
        """Windows下检测ESC键。优先msvcrt，非原生控制台回退ReadConsoleInput。"""
        try:
            import msvcrt
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # 原生控制台: 使用msvcrt
                # 清空残留输入：prompt_toolkit退出后msvcrt缓冲区可能有残留的转义序列字节
                time.sleep(0.05)
                while msvcrt.kbhit():
                    msvcrt.getch()
                self._poll_esc_windows_native(stop, msvcrt)
            else:
                # 非原生控制台(Windows Terminal等): 使用ReadConsoleInput
                self._poll_esc_windows_coninput(stop, kernel32)
        except (ImportError, OSError, AttributeError):
            pass

    def _poll_esc_windows_native(self, stop: threading.Event, msvcrt) -> None:
        """原生CMD下使用msvcrt检测ESC键。"""
        while not stop.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b'\x1b':
                        time.sleep(0.02)
                        if not msvcrt.kbhit():
                            self._interrupt.set()
                            from ..core.interrupt import abort_request
                            abort_request()
                            break
                        # 转义序列，消费掉后续字符
                        while msvcrt.kbhit():
                            msvcrt.getch()
            except OSError:
                break
            stop.wait(0.03)

    def _poll_esc_windows_coninput(self, stop: threading.Event, kernel32) -> None:
        """非原生控制台(Windows Terminal等)下使用ReadConsoleInput检测ESC键。"""
        import ctypes

        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE

        # INPUT_RECORD 结构体: 20 bytes (EventType 2bytes + padding 2bytes + Event 16bytes)
        # KEY_EVENT_RECORD: bKeyDown(4) + wRepeatCount(2) + wVirtualKeyCode(2) + wVirtualScanCode(2) + UnicodeChar(2) + dwControlKeyState(4)
        INPUT_RECORD_SIZE = 20
        KEY_EVENT = 0x0001
        VK_ESCAPE = 0x1B

        buf = (ctypes.c_char * (INPUT_RECORD_SIZE * 8))()  # 一次读8条
        records_read = ctypes.c_ulong()

        while not stop.is_set():
            try:
                # WaitForSingleObject 等待控制台输入，超时50ms
                result = kernel32.WaitForSingleObject(handle, 50)
                if result != 0:  # WAIT_TIMEOUT=0x102, WAIT_FAILED=0xFFFFFFFF
                    continue

                # 读取输入记录
                if not kernel32.ReadConsoleInputW(
                    handle, buf, 8, ctypes.byref(records_read)
                ):
                    break

                for i in range(records_read.value):
                    offset = i * INPUT_RECORD_SIZE
                    event_type = int.from_bytes(
                        buf[offset:offset+2], byteorder='little', signed=False
                    )
                    if event_type != KEY_EVENT:
                        continue
                    # KEY_EVENT_RECORD 偏移4字节处是 bKeyDown (BOOL, 4 bytes)
                    key_down = int.from_bytes(
                        buf[offset+4:offset+8], byteorder='little', signed=False
                    )
                    if not key_down:
                        continue
                    # wVirtualKeyCode 偏移10字节处 (2 bytes)
                    vk_code = int.from_bytes(
                        buf[offset+10:offset+12], byteorder='little', signed=False
                    )
                    if vk_code == VK_ESCAPE:
                        self._interrupt.set()
                        from ..core.interrupt import abort_request
                        abort_request()
                        return
            except (OSError, ValueError):
                break
            stop.wait(0.02)

    def _poll_esc_unix(self, stop: threading.Event) -> None:
        """Unix/Linux/macOS下使用select+termios检测ESC键。"""
        try:
            import select
            import termios
            import tty
        except ImportError:
            return

        fd = sys.stdin.fileno()
        old_settings = None
        try:
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)  # 设置为cbreak模式，允许单字符读取
        except (termios.error, OSError):
            return  # 无法设置终端模式（如管道输入）

        try:
            while not stop.is_set():
                try:
                    # 使用select检测stdin是否有数据，超时30ms
                    ready, _, _ = select.select([sys.stdin], [], [], 0.03)
                    if ready:
                        ch = os.read(fd, 1)
                        if ch == b'\x1b':
                            # 等待短暂时间判断是否为转义序列
                            time.sleep(0.02)
                            ready2, _, _ = select.select([sys.stdin], [], [], 0.01)
                            if not ready2:
                                self._interrupt.set()
                                from ..core.interrupt import abort_request
                                abort_request()
                                break
                            # 转义序列，消费掉后续字符
                            while True:
                                ready3, _, _ = select.select(
                                    [sys.stdin], [], [], 0.005)
                                if not ready3:
                                    break
                                os.read(fd, 1)
                except (OSError, ValueError):
                    break
        finally:
            # 恢复终端原始设置
            if old_settings is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except (termios.error, OSError):
                    pass


# 全局单例
_interrupt_ctrl = InterruptController()
