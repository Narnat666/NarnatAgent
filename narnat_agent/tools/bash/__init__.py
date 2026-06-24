"""Shell工具 —— 纯管道，AI写什么就执行什么

Windows用PowerShell，Linux/macOS用bash。
AI自己负责写正确语法，我们只管送达和返回。
"""

import base64
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional, Callable


# 删除命令正则
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# git 只读子命令白名单（不在白名单中的 git 命令需确认）
# 只匹配第一个子命令词，如 git log --oneline 匹配 log
_GIT_READONLY = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "blame", "shortlog", "rev-parse", "ls-tree", "describe",
    "reflog", "name-rev", "ls-remote", "show-ref", "merge-base",
    "cherry", "var",
})

# 匹配 git 子命令的正则（只捕获第一个子命令词）
_RE_GIT_CMD = re.compile(r"\bgit\s+(\S+)", re.IGNORECASE)

# PowerShell CLIXML 噪音正则（模块加载进度记录，对AI无意义）
_RE_CLIXML = re.compile(
    r'#<\s*CLIXML\s*\n.*?</Objs>',
    re.DOTALL,
)

# 后台进程注册表 {pid: (proc, start_time)}
_background_procs: dict = {}

# 当前运行的前台进程（agent层ESC打断后可调用kill_active杀掉）
_active_proc: Optional[subprocess.Popen] = None
_active_proc_lock = threading.Lock()

# ESC打断标记，kill_active()设置，execute()检查后清除
_interrupted = False

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Shell",
        "description": f"Execute shell commands. Syntax: {__import__('sys').platform}. Forbidden for file operations. Output truncated to 2000 chars by default, truncation notice returned when exceeded, increase max_output_chars for full output",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": f"Shell command (syntax: {__import__('sys').platform})"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default 120, max 600"},
                "run_in_background": {"type": "boolean", "description": "Run in background"},
                "max_output_chars": {"type": "integer", "description": "Max output chars, default 2000. Must be a positive integer"},
            },
            "required": ["command"],
        },
    },
}


def kill_active():
    """杀掉当前正在运行的前台子进程（ESC打断时由agent调用）"""
    global _interrupted
    with _active_proc_lock:
        proc = _active_proc
    if proc is not None and proc.poll() is None:
        _kill_proc_tree(proc)
        _interrupted = True


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


def _strip_clixml(text: str) -> str:
    """清洗PowerShell CLIXML噪音（模块加载进度记录，对AI无意义，仅输出优化）"""
    text = _RE_CLIXML.sub("", text)
    text = re.sub(r'\n{3,}', '\n\n', text)  # 压缩多余空行
    return text.strip()


def _kill_proc_tree(proc: subprocess.Popen):
    """杀掉进程树（Unix用killpg，Windows用taskkill）"""
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
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出到指定字符数，超出部分附加提示"""
    if max_chars <= 0:
        return "(max_output_chars必须为正整数)"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[已截断: 输出共{len(text)}字符, 当前显示前{max_chars}字符。增大max_output_chars可获取完整输出]"


def execute(
    command: str,
    timeout: int = 120,
    run_in_background: bool = False,
    max_output_chars: int = 2000,
    _tool_context=None,
) -> str:
    """
    执行shell命令。AI写什么就执行什么，不做翻译。

    Args:
        command: shell命令
        timeout: 超时秒数
        run_in_background: 后台运行，立即返回
        max_output_chars: 返回内容最大字符数，默认2000。设为0或负数表示不限制
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        stdout + stderr + 退出码
    """
    # 安全检查：删除命令和非只读git命令需确认
    need_confirm = False
    if _RE_DELETE.search(command):
        need_confirm = True
    else:
        m = _RE_GIT_CMD.search(command)
        if m:
            subcmd = m.group(1).lower()
            if subcmd not in _GIT_READONLY:
                need_confirm = True
            # branch/tag 虽然是只读子命令，但 -d/-D 是删除操作
            elif subcmd in ("branch", "tag") and re.search(r"\s-[dD]\b", command):
                need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            # Windows: prompt_toolkit和input()用不同的输入系统，直接用input()确认
            if _tool_context and _tool_context.confirm_callback and not _tool_context.confirm_callback(command):
                return "操作已取消: 此命令需用户确认"
        else:
            # Linux/macOS: 终端被prompt_toolkit占用，无法在子线程中读取输入
            # 用户已确认过（_delete_confirmed=True），直接执行
            if _tool_context and _tool_context._delete_confirmed:
                _tool_context._delete_confirmed = False
            else:
                # 暂存命令，返回AWAIT_CONFIRM标记，由agent主循环在#提示符下等待用户确认
                if _tool_context is not None:
                    _tool_context.pending_delete = ("Shell", {
                        "command": command,
                        "timeout": timeout,
                        "run_in_background": run_in_background,
                        "max_output_chars": max_output_chars,
                    })
                return "__AWAIT_CONFIRM__"

    # Windows: 统一用PowerShell，AI输入什么就执行什么
    # 优先pwsh(PowerShell 7+，原生支持&&/||)，回退powershell 5.x
    # 用-EncodedCommand传Base64，避免-Command对$_等特殊字符的二次解析
    if sys.platform == "win32":
        ps = _find_executable("pwsh", "powershell")
        if ps is None:
            return "错误: 未找到PowerShell，请安装后重试"
        full_cmd = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
        encoded = base64.b64encode(full_cmd.encode("utf-16-le")).decode("ascii")
        shell_cmd = [ps, "-NoProfile", "-EncodedCommand", encoded]
    else:
        shell = _find_executable("bash", "sh")
        if shell is None:
            return "错误: 未找到shell，请安装bash或sh后重试"
        shell_cmd = [shell, "-c", command]

    timeout_sec = min(timeout, 600)

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

    # 注册到_active_proc，agent层ESC打断后可调用kill_active杀掉
    with _active_proc_lock:
        global _active_proc
        _active_proc = proc

    try:
        # 非阻塞读取: 用线程读stdout/stderr，主线程轮询超时
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
        timed_out = False

        while proc.poll() is None:
            if time.time() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        # 等待读取线程结束
        t_out.join(timeout=5.0)
        t_err.join(timeout=5.0)

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)

        # ESC打断检查（优先级最高，kill_active已在外部调用）
        global _interrupted
        if _interrupted:
            _interrupted = False
            out = _decode_output(stdout)
            err = _strip_clixml(_decode_output(stderr))
            parts = []
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            parts.append("[用户中断]")
            return _truncate_output("\n".join(parts), max_output_chars)

        if timed_out:
            out = _decode_output(stdout)
            err = _strip_clixml(_decode_output(stderr))
            parts = []
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            parts.append(f"[超时: 命令执行超过{timeout_sec:.0f}秒，进程仍在运行]")
            return _truncate_output("\n".join(parts), max_output_chars)

        out = _decode_output(stdout)
        err = _strip_clixml(_decode_output(stderr))

        parts = []
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        parts.append(f"[exit code: {proc.returncode}]")

        return _truncate_output("\n".join(parts), max_output_chars)
    finally:
        with _active_proc_lock:
            _active_proc = None


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
