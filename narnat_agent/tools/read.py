"""Read工具 —— 读取文件内容，带行号

不截断输出，完整返回文件内容。AI自行决定是否用offset/limit分段读取大文件。
"""

import os


def execute(file_path: str, offset: int = 0, limit: int = 0,
            remote: bool = False, host: str = "") -> str:
    """
    读取文件内容。

    Args:
        file_path: 文件绝对路径
        offset: 起始行号(1-based)，0表示从头读
        limit: 最大行数，0表示读全文
        remote: 通过SFTP读取远程文件（需先Terminal connect）
        host: 远程主机IP（仅remote=True时使用）

    Returns:
        带行号的文件内容字符串，格式 "  行号→内容"
    """
    # AI可能传字符串类型的数值参数，确保类型正确
    offset = int(offset) if offset else 0
    limit = int(limit) if limit else 0
    remote = bool(remote)
    if remote:
        from .remote import remote_read, mark_remote_read
        result = remote_read(file_path, offset, limit, host)
        if "错误" not in result:
            mark_remote_read(file_path, host)
        return result
    if not os.path.isfile(file_path):
        return f"错误: 文件不存在: {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return f"错误: 权限不足: {file_path}"
    except OSError as e:
        return f"错误: 读取失败: {e}"

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
        result.append(f"  {line_num}→{content}")

    return "\n".join(result)
