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

# 匹配 git 命令的简单正则（出现 git 即命中）
_RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

_EXITCODE_MARKER = "__EXIT_b3f7__"
_RE_EXITCODE = re.compile(rf"(?:^|\n){_EXITCODE_MARKER}:(\d+)\s*$")

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
        "description": f"在{__import__('sys').platform}执行命令",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令"},
                "timeout": {"type": "integer", "description": "超时秒数（默认120，上限600）"},
                "run_in_background": {"type": "boolean", "description": "是否后台运行（默认否）"},
                "max_output_chars": {"type": "integer", "description": "最大输出字符数（默认2000）"},
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


def _extract_exitcode(text: str) -> tuple[str, int | None]:
    """从输出末尾提取__EXIT_b3f7__:N，返回(清洗后文本, 退出码)"""
    m = _RE_EXITCODE.search(text)
    if m:
        code = int(m.group(1))
        cleaned = text[:m.start()].rstrip("\n")
        return cleaned, code
    return text, None


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
    # 安全检查：删除命令和git命令根据配置决定是否需要确认
    need_confirm = False
    tc = _tool_context
    if tc and not tc.rm_skip_confirm and _RE_DELETE.search(command):
        need_confirm = True
    elif tc and not tc.git_skip_confirm and _RE_GIT.search(command):
        need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            # Windows: prompt_toolkit和input()用不同的输入系统，直接用input()确认
            if tc and tc.confirm_callback and not tc.confirm_callback(command):
                return "操作已取消: 此命令需用户确认"
        else:
            # Linux/macOS: 终端被prompt_toolkit占用，无法在子线程中读取输入
            # 用户已确认过（_delete_confirmed=True），直接执行
            if tc and tc._delete_confirmed:
                tc._delete_confirmed = False
            else:
                # 暂存命令，返回AWAIT_CONFIRM标记，由agent主循环在#提示符下等待用户确认
                if tc is not None:
                    tc.pending_delete = ("Shell", {
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
        full_cmd = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"$__ec = 0; "
            f"try {{ & {{ {command}\n}} 2>&1 | ForEach-Object {{ if($_ -is [System.Management.Automation.ErrorRecord]){{ $_.ToString() }}else{{ $_ }} }} }} "
            f"catch {{ Write-Output $_.ToString(); $__ec = 1 }} "
            f"finally {{ if($LASTEXITCODE){{ $__ec = $LASTEXITCODE }}elseif(!$?){{ $__ec = 1 }} ; Write-Output \"{_EXITCODE_MARKER}:$__ec\" }}"
        )
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
    is_win = sys.platform == "win32"
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE if not is_win else subprocess.DEVNULL,
        "cwd": os.getcwd(),
    }
    if not is_win:
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
        threads = [t_out]
        if not is_win:
            t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
            threads.append(t_err)
        for t in threads:
            t.start()

        deadline = time.time() + timeout_sec
        timed_out = False

        while proc.poll() is None:
            if time.time() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        # 等待读取线程结束
        for t in threads:
            t.join(timeout=5.0)

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks) if not is_win else b""

        # ESC打断检查（优先级最高，kill_active已在外部调用）
        global _interrupted
        if _interrupted:
            _interrupted = False
            if is_win:
                out, exitcode = _extract_exitcode(_decode_output(stdout))
                final_code = exitcode if exitcode else proc.returncode
                parts = []
                if out.strip():
                    parts.append(out.strip())
                parts.append("[用户中断]")
                parts.append(f"[exit code: {final_code}]")
            else:
                out = _decode_output(stdout)
                err = _decode_output(stderr)
                parts = []
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                parts.append("[用户中断]")
            return _truncate_output("\n".join(parts), max_output_chars)

        if timed_out:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)
            if is_win:
                out, exitcode = _extract_exitcode(_decode_output(stdout))
                final_code = exitcode if exitcode else proc.returncode
                parts = []
                if out.strip():
                    parts.append(out.strip())
                parts.append(f"[超时: 命令执行超过{timeout_sec:.0f}秒，已终止]")
                parts.append(f"[exit code: {final_code}]")
            else:
                out = _decode_output(stdout)
                err = _decode_output(stderr)
                parts = []
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                parts.append(f"[超时: 命令执行超过{timeout_sec:.0f}秒，已终止]")
            return _truncate_output("\n".join(parts), max_output_chars)

        if is_win:
            out, exitcode = _extract_exitcode(_decode_output(stdout))
            final_code = exitcode if exitcode else proc.returncode
            parts = []
            if out.strip():
                parts.append(out.strip())
            parts.append(f"[exit code: {final_code}]")
        else:
            out = _decode_output(stdout)
            err = _decode_output(stderr)
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
