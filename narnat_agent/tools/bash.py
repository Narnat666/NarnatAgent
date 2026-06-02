"""Bash工具 —— 执行shell命令

跨平台适配:
- Windows: PowerShell (自动转换常见bash语法)
- Linux/macOS: bash

高级功能:
- run_in_background: 后台运行，立即返回进程信息
- dangerouslyDisableSandbox: 禁用安全检查（交互式命令拦截等）
"""

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional, Callable

from ..config.defaults import MAX_BASH_OUTPUT


# 删除命令正则
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# 交互式命令正则
_RE_INTERACTIVE = re.compile(
    r"\b(vim|vi|nano|emacs|top|htop|less|more|man|ssh|telnet)\b",
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

    检测规则：命令中存在 -c/-m 后跟引号包裹的代码片段。
    """
    # python -c "code" / python3 -c "code" 模式
    if re.search(r'\bpython\d?\s+-c\s+"', command):
        return True
    # node -e "code" 模式
    if re.search(r'\bnode\s+-e\s+"', command):
        return True
    # PowerShell cmdlet 特征
    if re.search(r'\b(Get-|Set-|New-|Remove-|Write-|Select-|Where-|ForEach-|Invoke-|Start-|Stop-|Out-)\w+', command):
        return True
    # $env: 变量
    if '$env:' in command:
        return True
    return False


def _adapt_windows_command(command: str) -> str:
    """
    将常见bash语法自动适配为Windows cmd语法。

    仅做最小必要转换，不改变用户意图。
    """
    adapted = command

    # mkdir -p → mkdir（cmd的mkdir自动创建中间目录）
    adapted = re.sub(
        r'\bmkdir\s+-p\s+',
        lambda m: 'mkdir ',
        adapted,
    )

    # && 保持不变（cmd支持&&）

    # export VAR=val → set VAR=val
    adapted = re.sub(
        r'\bexport\s+(\w+)=(\S+)',
        r'set \1=\2',
        adapted,
    )

    # echo 保持不变（cmd原生支持echo）

    # cat file → type file
    adapted = re.sub(
        r'\bcat\s+',
        'type ',
        adapted,
    )

    # ls → dir
    adapted = re.sub(
        r'\bls\b',
        'dir',
        adapted,
    )

    # which → where
    adapted = re.sub(
        r'\bwhich\s+',
        'where ',
        adapted,
    )

    # touch file → type nul > file
    adapted = re.sub(
        r'\btouch\s+',
        'type nul > ',
        adapted,
    )

    return adapted


def _adapt_powershell_command(command: str) -> str:
    """
    将常见bash语法自动适配为PowerShell语法。
    仅在需要回退到PowerShell时使用。
    """
    adapted = command

    # mkdir -p → New-Item -ItemType Directory -Force
    adapted = re.sub(
        r'\bmkdir\s+-p\s+',
        lambda m: 'New-Item -ItemType Directory -Force -Path ',
        adapted,
    )

    # && → ; (PowerShell中&&需要PS7+，用;保证兼容)
    if "&&" in adapted and "$env:" not in adapted:
        adapted = adapted.replace("&&", "; ")

    # export VAR=val → $env:VAR = "val"
    adapted = re.sub(
        r'\bexport\s+(\w+)=(\S+)',
        r'$env:\1 = "\2"',
        adapted,
    )

    # echo "text" → Write-Host "text" (仅简单echo)
    adapted = re.sub(
        r'\becho\s+',
        'Write-Host ',
        adapted,
        count=1,
    )

    # cat file → Get-Content file
    adapted = re.sub(
        r'\bcat\s+',
        'Get-Content ',
        adapted,
    )

    # ls → Get-ChildItem (仅独立ls)
    adapted = re.sub(
        r'\bls\b',
        'Get-ChildItem',
        adapted,
    )

    # which → Get-Command
    adapted = re.sub(
        r'\bwhich\s+',
        'Get-Command ',
        adapted,
    )

    # touch file → New-Item -ItemType File -Path file
    adapted = re.sub(
        r'\btouch\s+',
        'New-Item -ItemType File -Path ',
        adapted,
    )

    return adapted


def _decode_output(raw: bytes) -> str:
    """安全解码子进程输出。Windows下回退GBK，Unix下仅UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Windows下cmd默认GBK编码
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
    执行shell命令。

    Args:
        command: shell命令
        description: 命令描述
        timeout: 超时毫秒数
        run_in_background: 后台运行，立即返回
        dangerouslyDisableSandbox: 禁用安全检查

    Returns:
        stdout + stderr + 退出码
    """
    # 安全检查（可被dangerouslyDisableSandbox跳过）
    if not dangerouslyDisableSandbox:
        # 拦截交互式命令
        if _RE_INTERACTIVE.search(command):
            return "错误: 禁止交互式命令，请使用非交互式替代方案"

        # 删除命令需确认
        if _RE_DELETE.search(command):
            if _confirm_callback and not _confirm_callback(command):
                return "操作已取消: 删除命令需用户确认"

    # 选择shell + 命令适配
    if sys.platform == "win32":
        # cmd /c 对嵌套引号处理有缺陷（如 python -c "code"），
        # 检测到引号嵌套时回退到PowerShell
        if _needs_powershell(command):
            effective_cmd = _adapt_powershell_command(command)
            # 优先 powershell，回退 pwsh（PowerShell Core）
            ps = _find_executable("powershell", "pwsh")
            if ps is None:
                return "错误: 未找到PowerShell，请安装后重试"
            shell_cmd = [ps, "-Command", effective_cmd]
        else:
            effective_cmd = _adapt_windows_command(command)
            shell_cmd = ["cmd", "/c", effective_cmd]
    else:
        # 优先bash，回退sh（某些最小Unix环境可能只有sh）
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

    # 用communicate(timeout)等待，同时在线程中检查中断
    # 比poll()轮询更高效，避免管道大量输出时死锁
    interrupted = False
    try:
        # 中断检查线程：如果用户按ESC，kill进程
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

    # 检查是否被中断
    if _interrupt_check and _interrupt_check():
        interrupted = True

    if interrupted:
        return "[用户中断]"

    # 解码输出
    out = _decode_output(stdout)
    err = _decode_output(stderr)

    # 截断长输出
    if len(out) > MAX_BASH_OUTPUT:
        out = out[:MAX_BASH_OUTPUT] + f"\n... (输出超过{MAX_BASH_OUTPUT}字符，已截断)"

    # 格式化结果
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

    # 启动后台线程等待进程完成，自动清理
    def _wait_and_cleanup():
        proc.wait()
        # 进程完成后保留一段时间再清理，允许查询结果
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
