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


def set_confirm_callback(cb: Callable[[str], bool]):
    """设置删除确认回调。cb返回True表示允许执行。"""
    global _confirm_callback
    _confirm_callback = cb


def _adapt_windows_command(command: str) -> str:
    """
    将常见bash语法自动适配为PowerShell语法。

    仅做最小必要转换，不改变用户意图。
    """
    adapted = command

    # mkdir -p → New-Item -ItemType Directory -Force
    adapted = re.sub(
        r'\bmkdir\s+-p\s+',
        lambda m: 'New-Item -ItemType Directory -Force -Path ',
        adapted,
    )

    # && → ; (PowerShell中&&需要PS7+，用;保证兼容)
    # 但保留PowerShell原生的&&（PS7+用户）
    if "&&" in adapted and "$env:" not in adapted:
        # 仅在明显是bash风格命令时转换
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
    """安全解码子进程输出，处理GBK/UTF-8等编码"""
    if not raw:
        return ""
    # 优先UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Windows下GBK
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        pass
    # 最终降级
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
        effective_cmd = _adapt_windows_command(command)
        shell_cmd = ["powershell", "-Command", effective_cmd]
    else:
        shell_cmd = ["bash", "-c", command]

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

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        out = _decode_output(stdout)
        return f"{out}\n[超时: 命令执行超过{timeout_sec:.0f}秒]"

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
