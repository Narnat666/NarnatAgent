"""Glob工具 —— 按模式匹配文件和目录"""

import os
import glob
from pathlib import Path

_DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": '按模式匹配文件和目录。例："*.h"、"src/**/*.cpp"。返回匹配路径，按修改时间排序。',
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob模式（支持 * ? [] ** 通配符）"},
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

    recursive = "**" in pattern
    matches = glob.glob(pattern, root_dir=root, recursive=recursive)

    result = []
    for rel in matches:
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
