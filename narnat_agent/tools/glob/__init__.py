"""Glob工具 —— 按模式匹配文件和目录"""

import os
import re
import glob
from pathlib import Path

_DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}
_MAX_BRACE_EXPANSIONS = 100


def _expand_braces(pattern: str) -> list:
    """递归展开花括号 {a,b,c}，返回 pattern 列表。不含花括号则返回单元素列表。"""
    if "{" not in pattern or "}" not in pattern:
        return [pattern]

    results = []
    # 匹配最内层花括号（从右往左找第一个}，再往前找最近的{）
    end = pattern.rfind("}")
    if end == -1:
        return [pattern]
    start = pattern.rfind("{", 0, end)
    if start == -1:
        return [pattern]

    options = [opt.strip() for opt in pattern[start + 1:end].split(",")]
    head = pattern[:start]
    tail = pattern[end + 1:]

    for opt in options:
        # 递归展开剩余部分（处理多组花括号）
        for expanded in _expand_braces(head + opt + tail):
            if len(results) >= _MAX_BRACE_EXPANSIONS:
                break
            results.append(expanded)

    return results[:_MAX_BRACE_EXPANSIONS]

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": '按模式匹配文件和目录。例："*.h"、"src/**/*.cpp"、"file?.txt"、"test_[0-9][0-9].py"、"*.{docx,pdf}"。返回匹配路径，按修改时间排序。',
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob模式"},
                "path": {"type": "string", "description": "搜索目录（默认当前目录）"},
                "max_results": {"type": "integer", "description": "最大结果数（默认500）"},
            },
            "required": ["pattern"],
        },
    },
}


def execute(pattern: str, path: str = "", max_results: int = 500, _tool_context=None) -> str:
    root = path or os.getcwd()
    if not os.path.isdir(root):
        return f"错误: 目录不存在: {root}"

    ignore_dirs = _DEFAULT_IGNORE_DIRS
    if _tool_context and _tool_context.ignore_dirs:
        ignore_dirs = set(_tool_context.ignore_dirs)

    # 展开花括号
    patterns = _expand_braces(pattern)

    seen = set()
    raw_matches = []
    for p in patterns:
        recursive = "**" in p
        for rel in glob.glob(p, root_dir=root, recursive=recursive):
            if rel not in seen:
                seen.add(rel)
                raw_matches.append(rel)

    result = []
    for rel in raw_matches:
        parts = Path(rel).parts
        if any(part in ignore_dirs for part in parts):
            continue
        full = os.path.join(root, rel)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            mtime = 0
        result.append((rel, mtime))

    if not result:
        return "(无匹配)"

    if max_results <= 0:
        return "(max_results必须为正整数)"

    total = len(result)
    result.sort(key=lambda x: x[1], reverse=True)

    if total > max_results:
        shown = result[:max_results]
        output = "\n".join(m[0] for m in shown)
        return f"{output}\n...[已截断: 共{total}个匹配项, 当前显示前{max_results}个。增大max_results可获取完整列表]"

    return "\n".join(m[0] for m in result)
