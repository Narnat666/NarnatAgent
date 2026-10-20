"""stdio 传输层 —— RPC 模式下 stdout 的唯一协议出口

RPC 模式下 stdout 是 JSON-RPC 通道（一行一条消息，NDJSON 分帧）。
终端形态的渲染、命令输出、调试打印一律改道 stderr；协议消息走安装时
捕获的真实 stdout，由一把写锁保证「一次写出一行、无多余换行」。

另外提供线程本地的输出捕获：command/exec 用它收集命令输出成 lines，
复核流程用它把终端格式的结果行解析成 verify/result 的裁决。
"""

import sys
import threading
from typing import List


_local = threading.local()


class Capture:
    """线程本地输出捕获（同一线程内的嵌套捕获按栈恢复）"""

    def __init__(self) -> None:
        self._buf: List[str] = []
        self._prev = None
        self.active = False

    def __enter__(self) -> "Capture":
        self._prev = getattr(_local, "capture", None)
        _local.capture = self
        self.active = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _local.capture = self._prev
        self.active = False
        return False

    @property
    def text(self) -> str:
        return "".join(self._buf)

    def append(self, text: str) -> None:
        self._buf.append(text)


class _StdoutProxy:
    """sys.stdout 替身：捕获线程写入缓冲，其余线程改道 stderr

    协议消息不走这里（server 直接写传输层捕获的真实 stdout），
    因此任何误写到 stdout 的内容都不会污染分帧。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, transport: "StdioTransport") -> None:
        self._transport = transport

    def write(self, text) -> int:
        if not text:
            return 0
        cap = getattr(_local, "capture", None)
        if cap is not None and cap.active:
            cap.append(text)
            return len(text)
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        try:
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kwargs):
        return None

    def fileno(self) -> int:
        return sys.stderr.fileno()


def _ensure_encodable(text: str) -> str:
    """把孤立代理字符（\\ud800-\\udfff）替换成占位符，保证能编码成 UTF-8。

    为什么需要：用户可以把孤立代理字符放进会话名（从某些工具粘贴、或程序拼
    出来的半个 emoji）。这种字符串无法 UTF-8 编码，写 stdout 会抛
    UnicodeEncodeError —— 而它是 ValueError 的子类，正好被原来的
    `except (OSError, ValueError): pass` 吞掉。后果是这条响应**永远发不出去**，
    前端一直转圈（实测），且磁盘上留下一个删不掉的怪文件。
    """
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8", "replace")


class StdioTransport:
    """协议通道：写 stdout 加锁，一行一条"""

    def __init__(self) -> None:
        self._real = None
        self._lock = threading.RLock()
        self._installed = False

    def install(self) -> None:
        """保存真实 stdout 并把 sys.stdout 换成代理（幂等）"""
        if self._installed:
            return
        self._real = sys.stdout
        sys.stdout = _StdoutProxy(self)
        self._installed = True

    def write_line(self, line: str) -> None:
        out = self._real if self._real is not None else sys.__stdout__
        if out is None:
            return
        safe = _ensure_encodable(line)
        with self._lock:
            try:
                out.write(safe + "\n")
                out.flush()
            except (OSError, ValueError) as e:
                # 不能完全静默：这条响应发不出去，前端就只能一直等。
                # 至少留个痕迹，别让问题变成「界面永久转圈、日志里啥也没有」。
                try:
                    sys.__stderr__.write(f"[narnat] 输出失败: {type(e).__name__}\n")
                    sys.__stderr__.flush()
                except Exception:
                    pass

    def capture(self) -> Capture:
        return Capture()


_transport = StdioTransport()


def get_transport() -> StdioTransport:
    return _transport


def _disable_keyboard_polling() -> None:
    """禁用 ESC 键盘轮询

    RPC 模式下 stdin 是协议通道：终端形态的 ESC 轮询线程会去抢读控制台
    输入（并在 Unix 上把 stdin 切成 cbreak），压缩等流程内部还会调用
    enter_run_mode，必须整体失效。中断改由 chat/cancel 驱动。
    """
    try:
        from ..ui.interrupt import _interrupt_ctrl
    except Exception:
        return

    def _enter_input_mode():
        _interrupt_ctrl._stop_poll.set()
        _interrupt_ctrl._interrupt.clear()

    def _enter_run_mode():
        _interrupt_ctrl._interrupt.clear()
        _interrupt_ctrl._stop_poll.set()

    _interrupt_ctrl.enter_input_mode = _enter_input_mode
    _interrupt_ctrl.enter_run_mode = _enter_run_mode


def install_stdio_transport() -> StdioTransport:
    """安装传输层（main.py --rpc 入口调用）"""
    _transport.install()
    _disable_keyboard_polling()
    return _transport
