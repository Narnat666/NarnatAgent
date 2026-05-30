"""Bash工具 —— 执行shell命令"""

import os
import re
import subprocess
import sys
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


# 权限确认回调，由agent层注入
_confirm_callback: Optional[Callable[[str], bool]] = None


def set_confirm_callback(cb: Callable[[str], bool]):
    """设置删除确认回调。cb返回True表示允许执行。"""
    global _confirm_callback
    _confirm_callback = cb


def execute(
    command: str,
    description: str = "",
    timeout: int = 120000,
    run_in_background: bool = False,       # 暂未实现
    dangerouslyDisableSandbox: bool = False,  # 暂未实现
) -> str:
    """
    执行shell命令。

    Args:
        command: shell命令
        description: 命令描述
        timeout: 超时毫秒数
        run_in_background: 后台运行
        dangerouslyDisableSandbox: 禁用沙箱

    Returns:
        stdout + stderr + 退出码
    """
    # 拦截交互式命令
    if _RE_INTERACTIVE.search(command):
        return "错误: 禁止交互式命令，请使用非交互式替代方案"

    # 删除命令需确认
    if _RE_DELETE.search(command):
        if _confirm_callback and not _confirm_callback(command):
            return "操作已取消: 删除命令需用户确认"

    # 选择shell
    if sys.platform == "win32":
        shell_cmd = ["powershell", "-Command", command]
    else:
        shell_cmd = ["bash", "-c", command]

    timeout_sec = min(timeout / 1000, 600)

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
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        return f"{out}\n[超时: 命令执行超过{timeout_sec:.0f}秒]"

    # 解码输出
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""

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
