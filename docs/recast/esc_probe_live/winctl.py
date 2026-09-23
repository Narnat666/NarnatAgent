"""E2 实验：Win32 控制台注入 / 读屏原语。

用途：把按键事件（KEY_EVENT）写进目标控制台的输入缓冲（无需窗口焦点），
并从目标控制台的屏幕缓冲区读回渲染后的文本（判定"思考中 / 已打断"）。

约束：仅服务 docs/recast/esc_probe_live 实验，不进入主线代码。
注意：AttachConsole 之后调用进程自身的标准句柄被接管，调用方不得再依赖
本进程 stdout（实验脚本一律写文件）。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
KEY_EVENT = 0x0001
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_OEM_3 = 0xC0  # 反引号 ` 键


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", ctypes.c_ushort), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    # 与 Win32 KEY_EVENT_RECORD 对齐：BOOL(4) + WORD*3(6) + WCHAR(2) + DWORD(4) = 16
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", ctypes.c_wchar), ("dwControlKeyState", wintypes.DWORD)]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_Pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]


assert ctypes.sizeof(KEY_EVENT_RECORD) == 16
assert ctypes.sizeof(INPUT_RECORD) == 20

kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WriteConsoleInputW.restype = wintypes.BOOL
kernel32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
kernel32.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                 wintypes.DWORD, COORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
kernel32.GetConsoleMode.restype = wintypes.BOOL
kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


def open_device(name: str):
    """打开当前附加控制台的设备（CONIN$ / CONOUT$）。"""
    handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                  OPEN_EXISTING, 0, None)
    if handle in (None, 0) or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFileW({name}) 失败: {ctypes.get_last_error()}")
    return handle


def _key_record(vk: int, ch: str, down: bool) -> INPUT_RECORD:
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    ev = rec.Event.KeyEvent
    ev.bKeyDown = 1 if down else 0
    ev.wRepeatCount = 1
    ev.wVirtualKeyCode = vk
    ev.wVirtualScanCode = 0
    ev.UnicodeChar = ch
    ev.dwControlKeyState = 0
    return rec


class ConsoleIO:
    """附加到目标控制台的注入 / 读屏通道。"""

    def __init__(self, pid: int, attach_timeout: float = 10.0):
        kernel32.FreeConsole()
        deadline = time.time() + attach_timeout
        while True:
            if kernel32.AttachConsole(pid):
                break
            if time.time() > deadline:
                raise OSError(f"AttachConsole({pid}) 失败: {ctypes.get_last_error()}")
            time.sleep(0.2)
        self.pid = pid
        self.conin = open_device("CONIN$")
        self.conout = open_device("CONOUT$")

    # ── 注入 ──

    def press(self, key: str, hold: bool = True) -> None:
        """注入一次按键（key: esc / backtick / enter / 单字符）。

        每个按键发 keydown + keyup 两条记录（与真实键盘事件形态一致）。
        """
        if key == "esc":
            vk, ch = VK_ESCAPE, "\x1b"
        elif key == "backtick":
            vk, ch = VK_OEM_3, "`"
        elif key == "enter":
            vk, ch = VK_RETURN, "\r"
        elif key == "ctrl-c":
            vk, ch = 0x43, "\x03"
        elif len(key) == 1:
            vk, ch = 0, key
        else:
            raise ValueError(f"未知键: {key}")
        self._write([(vk, ch, True), (vk, ch, False)])

    def type_text(self, text: str) -> None:
        """注入一段文本（每字符一个 keydown+keyup）。"""
        for ch in text:
            self._write([(0, ch, True), (0, ch, False)])

    def _write(self, events) -> None:
        n = len(events)
        arr = (INPUT_RECORD * n)()
        for i, (vk, ch, down) in enumerate(events):
            arr[i] = _key_record(vk, ch, down)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteConsoleInputW(self.conin, arr, n, ctypes.byref(written))
        if not ok or written.value != n:
            raise OSError(f"WriteConsoleInputW 失败: err={ctypes.get_last_error()} "
                          f"written={written.value}/{n}")

    # ── 读屏 ──

    def info(self) -> CONSOLE_SCREEN_BUFFER_INFO:
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(self.conout, ctypes.byref(info)):
            raise OSError(f"GetConsoleScreenBufferInfo 失败: err={ctypes.get_last_error()}")
        return info

    def screen_text(self) -> str:
        """读可见窗口文本（每行去尾空格；尾部空行去掉）。"""
        info = self.info()
        win = info.srWindow
        width = win.Right - win.Left + 1
        lines = []
        buf = ctypes.create_unicode_buffer(width + 1)
        n = wintypes.DWORD(0)
        for y in range(win.Top, win.Bottom + 1):
            ok = kernel32.ReadConsoleOutputCharacterW(
                self.conout, buf, width, COORD(win.Left, y), ctypes.byref(n))
            if not ok:
                break
            lines.append(buf[:n.value].rstrip())
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def cursor(self) -> tuple:
        info = self.info()
        return info.dwCursorPosition.X, info.dwCursorPosition.Y

    def close(self) -> None:
        for h in (self.conin, self.conout):
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass
        self.conin = self.conout = None
