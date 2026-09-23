"""
中断控制器 ── 封装 ESC/SIGINT 中断管理（线程安全）

状态机: INPUT_MODE ↔ RUN_MODE
"""

import atexit
import os
import sys
import signal
import threading
import time
from typing import Optional


def _try_restore_term(fd: int, settings) -> None:
    """atexit 回调：尝试恢复终端原始设置。忽略所有异常（进程退出中）。"""
    try:
        import termios
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
    except Exception:
        pass


def _on_esc_detected(ctrl: "InterruptController") -> None:
    """ESC按下后的立即动作（轮询线程内执行，全部非阻塞，像硬件中断一样）。

    切断大动脉三步：set中断事件 + 关LLM HTTP连接 + 杀全部活跃子进程。
    之后主线程无论走到哪个检查点都会立刻发现中断并返还输入界面。

    三个kill函数均幂等（后台杀树/发Ctrl+C/设标志），且各自在
    新一局execute入口清零标志，重复调用不会误伤下一局。
    延迟导入避免 ui↔tools 模块循环依赖。
    """
    ctrl._interrupt.set()
    try:
        from ..core.interrupt import abort_request
        from ..tools.bash import kill_active
        from ..tools.terminal import kill_active_exec as _kill_term
        from ..tools.serial import kill_active_exec as _kill_serial
        abort_request()
        kill_active()
        _kill_term()
        _kill_serial()
    except Exception:
        # 中断路径不允许异常打断流程：kill失败由主线程检查点的kill兜底
        pass


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
        self._atexit_registered = False  # atexit 去重：仅注册一次

    @property
    def is_set(self) -> bool:
        return self._interrupt.is_set()

    def clear(self) -> None:
        self._interrupt.clear()

    def enter_input_mode(self) -> None:
        self._stop_poll.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            # 轮询线程最慢30ms一轮；不等它同步退出，避免阻塞输入界面回归
            self._poll_thread.join(timeout=0.2)
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
        # 先停旧轮询线程，防止多个并行轮询线程竞争同一 _interrupt Event
        self._stop_poll.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=0.2)
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
                # 不清空输入缓冲："回车后立即按ESC"的ESC若被清掉将无法打断。
                # prompt_toolkit退出后的残留转义序列由轮询线程按
                # "ESC vs 转义序列"识别逻辑自然消费（单ESC→打断，序列→吞掉）。
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
                            _on_esc_detected(self)
                            break
                        # 转义序列，消费掉后续字符（最长CSI序列约5字节，
                        # 限制消费上限，防止连按ESC后用户新输入被当作序列尾巴吞掉）
                        consumed = []
                        for _ in range(5):
                            if not msvcrt.kbhit():
                                break
                            consumed.append(msvcrt.getch())
                        if b'\x1b' in consumed:
                            # 窗口内出现第二个ESC：用户连按ESC，立即打断
                            _on_esc_detected(self)
                            break
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
                        _on_esc_detected(self)
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
            # atexit 兜底: daemon 线程被强制终止时 finally 可能不执行，注册退出回调确保终端恢复
            # 使用 _atexit_registered 去重，避免多次启停累积回调
            if not self._atexit_registered:
                atexit.register(lambda _fd=fd, _old=old_settings: _try_restore_term(_fd, _old))
                self._atexit_registered = True
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
                                _on_esc_detected(self)
                                break
                            # 转义序列，消费掉后续字符（最长CSI序列约5字节，
                            # 限制消费上限，防止用户新输入被当作序列尾巴吞掉）
                            consumed = []
                            for _ in range(5):
                                ready3, _, _ = select.select(
                                    [sys.stdin], [], [], 0.005)
                                if not ready3:
                                    break
                                consumed.append(os.read(fd, 1))
                            if b'\x1b' in consumed:
                                # 窗口内出现第二个ESC：用户连按ESC，立即打断
                                _on_esc_detected(self)
                                break
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
