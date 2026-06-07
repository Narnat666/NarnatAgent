"""Glob工具 —— 按文件名模式搜索文件"""

import os
import fnmatch
from pathlib import PurePath
from typing import Optional, Callable


# 忽略的目录
_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}

# 中断检查回调，由agent层注入（返回True表示用户按了ESC）
_interrupt_check: Optional[Callable[[], bool]] = None


def set_interrupt_check(cb: Callable[[], bool]):
    """设置中断检查回调。cb返回True表示用户请求中断。"""
    global _interrupt_check
    _interrupt_check = cb


def execute(pattern: str, path: str = "") -> str:
    """
    按glob模式搜索文件。

    Args:
        pattern: glob模式，如 **/*.py
        path: 搜索根目录，空串为当前工作目录

    Returns:
        匹配的文件路径列表，每行一个，按修改时间倒序
    """
    root = path or os.getcwd()
    if not os.path.isdir(root):
        return f"错误: 目录不存在: {root}"

    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地修改dirnames跳过忽略目录
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        # ESC铁律: 每遍历一个目录检查一次中断
        if _interrupt_check and _interrupt_check():
            return "[用户中断]"
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            # 用PurePath.match匹配**模式
            if _match_pattern(rel, pattern):
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    mtime = 0
                matches.append((rel, mtime))

    if not matches:
        return "(无匹配文件)"

    # 按修改时间倒序
    matches.sort(key=lambda x: x[1], reverse=True)
    return "\n".join(m[0] for m in matches)


def _match_pattern(rel_path: str, pattern: str) -> bool:
    """匹配glob模式，支持**递归"""
    # **/*.py 应同时匹配根目录下的 *.py 和子目录下的
    if pattern.startswith("**/"):
        suffix = pattern[3:]  # 去掉前缀**/
        # 匹配任意深度子目录，或根目录
        if fnmatch.fnmatch(os.path.basename(rel_path), suffix):
            return True
        # 也尝试匹配带路径的
        if fnmatch.fnmatch(rel_path, suffix):
            return True

    # PurePath.match在Python 3.12+支持**模式
    try:
        if PurePath(rel_path).match(pattern):
            return True
    except (ValueError, TypeError):
        pass

    # 降级：用fnmatch逐段匹配
    if "**" not in pattern:
        return fnmatch.fnmatch(rel_path, pattern)

    # 简单的**处理
    parts = pattern.split("**")
    if len(parts) == 2:
        prefix, suffix = parts
        if prefix:
            prefix = prefix.rstrip("/\\")
        if suffix:
            suffix = suffix.lstrip("/\\")
        if prefix and not rel_path.startswith(prefix):
            return False
        if suffix:
            return fnmatch.fnmatch(os.path.basename(rel_path), suffix) or \
                   fnmatch.fnmatch(rel_path, suffix)
        return True
    # 多个**的情况：用递归walk匹配（已由外层os.walk保证递归）
    # 只需检查basename是否匹配pattern中最后一个**后的suffix
    if len(parts) > 2:
        last_suffix = parts[-1].lstrip("/\\")
        first_prefix = parts[0].rstrip("/\\")
        if first_prefix and not rel_path.startswith(first_prefix):
            return False
        if last_suffix:
            return fnmatch.fnmatch(os.path.basename(rel_path), last_suffix)
        return True
    return fnmatch.fnmatch(rel_path, pattern)
