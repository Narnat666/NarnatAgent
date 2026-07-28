"""持久化 cmd.exe 会话 —— 命令走 stdin，输出走 stdout

消除 subprocess.Popen + list2cmdline 导致的转义损耗。
AI 写什么，cmd 就收到什么——和真实 cmd 窗口行为一致。

核心机制：
- cmd.exe 作为持久子进程，stdin/stdout 全走管道
- 单后台 reader 线程逐字节 read(1) 收集 stdout 到共享 buffer
- 每条命令后追加 echo MARKER%errorlevel% 哨兵
- 主线程轮询 buffer 直到哨兵出现（Condition.wait 唤醒）
- ESC 打断：往 stdin 写 \x03 (Ctrl+C)
"""

import os
import re
import subprocess
import threading
import time
from typing import Optional


def _truncate_output(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return "[错误: max_output_chars需为正整数]"
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n...[已截断: 输出共{len(text)}字符, 当前显示前{max_chars}字符。增大max_output_chars可获取完整输出]"
    )


class CmdSession:
    """持久化的 cmd.exe 进程。

    单后台 reader 线程 + 共享 buffer + Condition 通知。
    主线程发送命令后轮询 buffer 直到哨兵。
    """

    _ANSI_RE = re.compile(
        r"\x1b\[[0-9;]*[a-zA-Z]"
        r"|\x1b\].*?(?:\x07|\x1b\\)"
        r"|\x1b[()][A-Za-z0-9]"
        r"|\x1b[0-9:;<=>?@[A-Z\[\]^_`]"
    )

    # 哨兵环境变量名（通过 env 注入，避免出现在回显命令中）
    _ENV_MARKER = "__NARNAT_M"

    def __init__(self):
        # 哨兵值：固定字符串，所有命令共用（每次 execute 前清空 buffer）
        self._marker = f"__NARNAT_BASE_{time.time_ns()}__"

        # 通过进程环境变量注入：回显中只显示 %__NARNAT_M%，不显示明文
        self._env = os.environ.copy()
        self._env[self._ENV_MARKER] = self._marker

        self._proc = subprocess.Popen(
            "cmd.exe",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=self._env,
        )

        self._busy = False
        self._interrupt = threading.Event()
        self._dead = False  # 超时后标记会话死亡，下次调用时重建

        # 共享 buffer + Condition
        self._buffer = b""
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._reader_alive = True

        # 后台 reader 线程（唯一，全程存活）
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

        # 初始化：消化启动横幅
        self._init_drain()

    def _reader_loop(self):
        """逐字节读取 stdout，追加到 buffer，通知等待者。"""
        while self._reader_alive:
            try:
                ch = self._proc.stdout.read(1)
                if not ch:  # EOF
                    break
                with self._cond:
                    self._buffer += ch
                    self._cond.notify_all()
            except Exception:
                break


    def _init_drain(self):
        """发送 @echo off + 哨兵，消化启动横幅。"""
        marker_bytes = self._marker.encode("utf-8")
        try:
            self._proc.stdin.write(b"@echo off\r\n")
            self._proc.stdin.write(
                f"@echo %{self._ENV_MARKER}%\r\n".encode("utf-8")
            )
            self._proc.stdin.flush()
        except Exception:
            return

        # 二进制搜索哨兵（逐字节读取的中文可能被切碎，不可中途解码）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with self._cond:
                if marker_bytes in self._buffer:
                    self._buffer = b""
                    remaining = deadline - time.time()
                    if remaining > 0.2:
                        self._cond.wait(timeout=0.2)
                    self._buffer = b""
                    return
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.1, remaining))

        with self._lock:
            self._buffer = b""

    # ── 公开接口 ──

    def execute(
        self, command: str, timeout: int = 120, max_output_chars: int = 4000
    ) -> str:
        if self._dead:
            return "错误: cmd.exe 会话已超时死亡，正在重建..."
        if self._busy:
            return "[上一个命令尚未完成，此终端暂不可用]"
        if self._proc.poll() is not None:
            return "错误: cmd.exe 进程已退出，请重启会话"

        self._busy = True
        try:
            return self._do_execute(command, timeout, max_output_chars)
        finally:
            self._busy = False

    def kill_active(self):
        self._interrupt.set()  # 只设标志，pipe 下 \x03 是字面量不触发中断

    def close(self):
        self._reader_alive = False
        self._interrupt.set()
        try:
            self._proc.stdin.write(b"exit\r\n")
            self._proc.stdin.flush()
        except Exception:
            pass
        with self._cond:
            self._cond.notify_all()
        try:
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()

    # ── 内部实现 ──

    def _do_execute(
        self, command: str, timeout: int, max_output_chars: int
    ) -> str:
        self._interrupt.clear()

        # 清空上次残留
        with self._lock:
            self._buffer = b""

        full_cmd = f"@{command}\r\n@echo %{self._ENV_MARKER}%%errorlevel%\r\n"
        try:
            self._proc.stdin.write(full_cmd.encode("gbk", errors="replace"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            return f"错误: cmd.exe 进程通信失败: {e}"

        raw, found = self._wait_for_marker(timeout)

        if not found:
            interrupted = self._interrupt.is_set()
            # 标记死亡，不尝试恢复——pipe 下无法可靠中断正在运行的命令
            self._dead = True
            self._proc.kill()
            try:
                self._proc.wait(timeout=3)
            except Exception:
                pass

            tag = "[ESC中断]" if interrupted else f"[超时中断: {timeout}秒]"
            cmd_out, exit_code = self._parse(raw, command)
            ec = f"[exit code: {exit_code}]\n" if exit_code is not None else ""
            body = f"{ec}{cmd_out}" if cmd_out else ec.rstrip()
            return _truncate_output(
                f"{body}\n{tag}" if body else tag, max_output_chars
            )

        cmd_out, exit_code = self._parse(raw, command)
        ec = f"[exit code: {exit_code}]\n" if exit_code is not None else ""
        result = f"{ec}{cmd_out}" if cmd_out else ec.strip()
        return _truncate_output(result if result else "", max_output_chars)

    def _wait_for_marker(self, timeout: int, existing: bytes = b""):
        """等待 buffer 中出现 self._marker 字节，返回 (raw_bytes, found)。

        全程二进制比较，不中途解码——避免逐字节读取切碎 GBK 中文字符。
        """
        marker_bytes = self._marker.encode("utf-8")
        buf = existing
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._interrupt.is_set():
                break

            with self._cond:
                buf += self._buffer
                self._buffer = b""

                if marker_bytes in buf:
                    return buf, True

                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.1, remaining))

        return buf, False

    def _parse(self, raw: bytes, command: str = ""):
        """解析输出：哨兵前行 = 命令输出，最后含哨兵行 = 退出码。"""
        # 优先 UTF-8（eza/git/python 等工具），失败回退 GBK（cmd 系统错误信息）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        exit_code: Optional[int] = None
        lines = text.split("\n")
        marker = self._marker

        # 找到最后一个含 marker 的行
        marker_idx = None
        for i, line in enumerate(lines):
            if marker in line:
                marker_idx = i
                try:
                    idx = line.index(marker) + len(marker)
                    val = line[idx:].strip().strip("\r")
                    if val:
                        exit_code = int(val)
                except (ValueError, IndexError):
                    pass

        if marker_idx is not None:
            lines_before = lines[:marker_idx]
            # PIPE 模式：命令回显始终在首行，无条件剥离。
            # 用户输出以 @ 开头时，其回显行是 `@echo @xxx`，不是首字符 @，
            # 且 cmd 在 PIPE 模式回显首行以 @ 开头是固定行为。
            if lines_before:
                first = lines_before[0]
                # 只要首行以@开头且包含 echo/set/cd/dir/for/if/ping 等命令特征，
                # 就是回显行而非用户输出。安全兜底：不是纯 `@文字` 格式就剥。
                if first.lstrip().startswith("@"):
                    lines_before = lines_before[1:]
            cmd_out = "\n".join(lines_before)
        else:
            cmd_out = text

        return self._clean(cmd_out), exit_code

    def _clean(self, raw: str) -> str:
        """清洗 ANSI 转义码、\r 覆盖、哨兵行、版权横幅、回显命令行。"""
        cleaned = self._ANSI_RE.sub("", raw)

        # 移除版权横幅行
        cleaned = re.sub(
            r"^Microsoft Windows.*$", "", cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(
            r"^\(c\) Microsoft Corporation.*$", "", cleaned, flags=re.MULTILINE
        )

        # 只移除哨兵 echo 命令行（@echo %__NARNAT_M%...），保留其他 @ 开头的用户输出
        cleaned = re.sub(r"^@echo\s+%__NARNAT_M%.*$", "", cleaned, flags=re.MULTILINE)

        # \r 覆盖合并
        merged = []
        for line in cleaned.split("\n"):
            if "\r" not in line:
                merged.append(line)
                continue
            segs = line.split("\r")
            result = ""
            for seg in segs:
                if len(seg) >= len(result):
                    result = seg
                else:
                    result = seg + result[len(seg) :]
            merged.append(result)
        cleaned = "\n".join(merged)

        # 移除哨兵行和 base marker 行
        marker = self._marker
        cleaned = re.sub(
            rf"^.*{re.escape(marker)}\d*.*$", "", cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()
