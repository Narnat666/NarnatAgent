"""Shell工具 —— 纯管道，AI写什么就执行什么

Windows用PowerShell，Linux/macOS用bash。
AI自己负责写正确语法，我们只管送达和返回。
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Callable


# 删除命令正则（仅保留删除确认，其他全部放行）
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# 后台进程注册表 {pid: (proc, start_time)}
_background_procs: dict = {}

# 权限确认回调，由agent层注入
_confirm_callback: Optional[Callable[[str], bool]] = None

# 中断检查回调，由agent层注入（返回True表示用户按了ESC）
_interrupt_check: Optional[Callable[[], bool]] = None


def set_confirm_callback(cb: Callable[[str], bool]):
    """设置删除确认回调。cb返回True表示允许执行。"""
    global _confirm_callback
    _confirm_callback = cb


def set_interrupt_check(cb: Callable[[], bool]):
    """设置中断检查回调。cb返回True表示用户请求中断。"""
    global _interrupt_check
    _interrupt_check = cb


def _find_executable(*names: str) -> Optional[str]:
    """按优先级查找可执行文件，返回第一个找到的名称或路径。"""
    import shutil
    for name in names:
        if shutil.which(name):
            return name
    return None


def _decode_output(raw: bytes) -> str:
    """安全解码子进程输出。Windows下回退GBK，Unix下仅UTF-8。"""
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


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """杀掉整个进程树，确保&&链式命令的所有子进程都被终止。"""
    try:
        if sys.platform == "win32":
            # Windows: taskkill /F /T /PID 杀整棵进程树
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # Unix: 杀整个进程组
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def execute(
    command: str,
    timeout: int = 120000,
    run_in_background: bool = False,
) -> str:
    """
    执行shell命令。AI写什么就执行什么，不做翻译。

    Args:
        command: shell命令
        timeout: 超时毫秒数
        run_in_background: 后台运行，立即返回

    Returns:
        stdout + stderr + 退出码
    """
    # 安全检查：仅删除命令需确认
    if _RE_DELETE.search(command):
        if _confirm_callback and not _confirm_callback(command):
            return "操作已取消: 删除命令需用户确认"

    # Windows: 统一用PowerShell，AI输入什么就执行什么
    # 优先pwsh(PowerShell 7+，原生支持&&/||)，回退powershell 5.x
    if sys.platform == "win32":
        ps = _find_executable("pwsh", "powershell")
        if ps is None:
            return "错误: 未找到PowerShell，请安装后重试"
        shell_cmd = [ps, "-NoProfile", "-Command",
                     "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command]
    else:
        shell = _find_executable("bash", "sh")
        if shell is None:
            return "错误: 未找到shell，请安装bash或sh后重试"
        shell_cmd = [shell, "-c", command]

    timeout_sec = min(timeout / 1000, 600)

    # 后台运行模式
    if run_in_background:
        return _run_background(shell_cmd, command)

    # 前台运行模式
    # Unix: 用新进程组，确保能killpg杀整棵树
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": os.getcwd(),
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(shell_cmd, **popen_kwargs)
    except FileNotFoundError as e:
        return f"错误: Shell未找到: {e}"
    except OSError as e:
        return f"错误: 启动失败: {e}"

    # 非阻塞读取: 用线程读stdout/stderr，主线程轮询中断+超时
    stdout_chunks = []
    stderr_chunks = []

    def _reader(stream, chunks):
        try:
            while True:
                data = stream.read(4096)
                if not data:
                    break
                chunks.append(data)
        except Exception:
            pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.time() + timeout_sec
    interrupted = False
    timed_out = False

    # 主循环: 等待进程结束，同时轮询中断
    while proc.poll() is None:
        if _interrupt_check and _interrupt_check():
            _kill_proc_tree(proc)
            interrupted = True
            break
        if time.time() >= deadline:
            # 超时不杀进程（纯管道原则），但给读取线程一点时间收尾
            timed_out = True
            break
        time.sleep(0.05)

    # 等待读取线程结束（最多3秒，中断场景下不无限等）
    wait_time = 3.0 if interrupted else 5.0
    t_out.join(timeout=wait_time)
    t_err.join(timeout=wait_time)

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)

    if interrupted:
        return "[用户中断]"

    if timed_out:
        out = _decode_output(stdout)
        err = _decode_output(stderr)
        parts = []
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        parts.append(f"[超时: 命令执行超过{timeout_sec:.0f}秒，进程仍在运行]")
        return "\n".join(parts)

    out = _decode_output(stdout)
    err = _decode_output(stderr)

    parts = []
    if out.strip():
        parts.append(out.strip())
    if err.strip():
        parts.append(f"[stderr]\n{err.strip()}")
    parts.append(f"[exit code: {proc.returncode}]")

    return "\n".join(parts)


def _run_background(shell_cmd: list, original_command: str) -> str:
    """后台运行命令，立即返回进程信息"""
    try:
        proc = subprocess.Popen(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
        )
    except FileNotFoundError as e:
        return f"错误: Shell未找到: {e}"
    except OSError as e:
        return f"错误: 启动失败: {e}"

    pid = proc.pid
    start_time = time.time()
    _background_procs[pid] = (proc, start_time)

    def _wait_and_cleanup():
        proc.wait()
        time.sleep(60)
        _background_procs.pop(pid, None)

    t = threading.Thread(target=_wait_and_cleanup, daemon=True)
    t.start()

    return (
        f"[后台进程已启动]\n"
        f"PID: {pid}\n"
        f"命令: {original_command}\n"
        f"提示: 进程在后台运行，输出可通过重定向到文件后用Read查看"
    )


def get_background_status() -> str:
    """查询所有后台进程状态（内部接口）"""
    if not _background_procs:
        return "(无后台进程)"

    lines = []
    now = time.time()
    for pid, (proc, start) in list(_background_procs.items()):
        elapsed = now - start
        status = "运行中" if proc.poll() is None else f"已结束(退出码:{proc.returncode})"
        lines.append(f"PID {pid}: {status}, 运行{elapsed:.0f}秒")

    return "\n".join(lines)
