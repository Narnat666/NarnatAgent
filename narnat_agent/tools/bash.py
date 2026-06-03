"""Shell工具 —— 执行shell命令

AI写什么就执行什么，不做命令翻译。
Windows用PowerShell执行，Linux/macOS用bash执行。

安全策略:
- 仅拦截rm/del等删除命令，需用户确认
- 其他命令直接执行，信任AI
"""

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional, Callable

from ..config.defaults import MAX_BASH_OUTPUT


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


def _needs_powershell(command: str) -> bool:
    """
    检测命令是否需要PowerShell而非cmd。

    cmd /c 对嵌套引号处理有缺陷，如:
      python -c "import sys; sys.exit(1)"
    cmd会把引号吃掉导致语法错误。
    """
    if re.search(r'\bpython\d?\s+-c\s+"', command):
        return True
    if re.search(r'\bnode\s+-e\s+"', command):
        return True
    if re.search(r'\b(Get-|Set-|New-|Remove-|Write-|Select-|Where-|ForEach-|Invoke-|Start-|Stop-|Out-)\w+', command):
        return True
    if '$env:' in command:
        return True
    return False


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


def execute(
    command: str,
    description: str = "",
    timeout: int = 120000,
    run_in_background: bool = False,
    dangerouslyDisableSandbox: bool = False,
) -> str:
    """
    执行shell命令。AI写什么就执行什么，不做翻译。

    Args:
        command: shell命令
        description: 命令描述
        timeout: 超时毫秒数
        run_in_background: 后台运行，立即返回
        dangerouslyDisableSandbox: 禁用安全检查

    Returns:
        stdout + stderr + 退出码
    """
    # 安全检查：仅删除命令需确认
    if not dangerouslyDisableSandbox:
        if _RE_DELETE.search(command):
            if _confirm_callback and not _confirm_callback(command):
                return "操作已取消: 删除命令需用户确认"

    # 选择shell，不做命令翻译
    if sys.platform == "win32":
        if _needs_powershell(command):
            ps = _find_executable("powershell", "pwsh")
            if ps is None:
                return "错误: 未找到PowerShell，请安装后重试"
            shell_cmd = [ps, "-Command", command]
        else:
            shell_cmd = ["cmd", "/c", command]
    else:
        shell = _find_executable("bash", "sh")
        if shell is None:
            return "错误: 未找到shell，请安装bash或sh后重试"
        shell_cmd = [shell, "-c", command]

    timeout_sec = min(timeout / 1000, 600)

    # ── 后台运行模式 ──
    if run_in_background:
        return _run_background(shell_cmd, command)

    # ── 前台运行模式 ──
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

    interrupted = False
    try:
        def _interrupt_watcher():
            while proc.poll() is None:
                if _interrupt_check and _interrupt_check():
                    proc.kill()
                    return
                time.sleep(0.05)

        watcher = threading.Thread(target=_interrupt_watcher, daemon=True)
        watcher.start()

        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        out = _decode_output(stdout)
        return f"{out}\n[超时: 命令执行超过{timeout_sec:.0f}秒]"
    except Exception:
        proc.kill()
        stdout, stderr = proc.communicate()

    if _interrupt_check and _interrupt_check():
        interrupted = True

    if interrupted:
        return "[用户中断]"

    out = _decode_output(stdout)
    err = _decode_output(stderr)

    if len(out) > MAX_BASH_OUTPUT:
        out = out[:MAX_BASH_OUTPUT] + f"\n... (输出超过{MAX_BASH_OUTPUT}字符，已截断)"

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
