"""Glob工具 —— 按文件名模式搜索文件"""

import os
import fnmatch
from pathlib import PurePath


# 忽略的目录
_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}


def execute(pattern: str, path: str = "", max_results: int = 500) -> str:
    """
    按glob模式搜索文件。

    Args:
        pattern: glob模式，如 **/*.py
        path: 搜索根目录，空串为当前工作目录
        max_results: 最大返回结果数，默认500。必须为正整数

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

    if max_results <= 0:
        return "(max_results必须为正整数)"

    total = len(matches)
    # 按修改时间倒序
    matches.sort(key=lambda x: x[1], reverse=True)

    if total > max_results:
        shown = matches[:max_results]
        result = "\n".join(m[0] for m in shown)
        return f"{result}\n...[已截断: 共{total}个匹配文件, 当前显示前{max_results}个。增大max_results可获取完整列表]"

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
