"""Read工具 —— 读取文件内容，带行号"""

import os
from ..config.defaults import MAX_FILE_LINES, MAX_LINE_CHARS


def execute(file_path: str, offset: int = 0, limit: int = 0) -> str:
    """
    读取文件内容。

    Args:
        file_path: 文件绝对路径
        offset: 起始行号(1-based)，0表示从头读
        limit: 最大行数，0表示读全文

    Returns:
        带行号的文件内容字符串，格式 "  行号→内容"
    """
    if not os.path.isfile(file_path):
        return f"错误: 文件不存在: {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return f"错误: 权限不足: {file_path}"
    except OSError as e:
        return f"错误: 读取失败: {e}"

    total = len(lines)

    # 超大文件且无offset/limit，截断并提示
    if total > MAX_FILE_LINES and offset == 0 and limit == 0:
        lines = lines[:MAX_FILE_LINES]
        hint = f"\n... 文件共{total}行，仅显示前{MAX_FILE_LINES}行，可用offset/limit分段读取 ..."
    else:
        hint = ""

    # 应用offset/limit
    start = max(offset - 1, 0) if offset > 0 else 0
    if limit > 0:
        lines = lines[start:start + limit]
    else:
        lines = lines[start:]

    # 格式化输出
    result = []
    for i, line in enumerate(lines):
        line_num = start + i + 1
        content = line.rstrip("\n\r")
        if len(content) > MAX_LINE_CHARS:
            content = content[:MAX_LINE_CHARS] + "...(截断)"
        result.append(f"  {line_num}→{content}")

    return "\n".join(result) + hint
