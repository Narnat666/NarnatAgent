"""Read工具 —— 读取文件内容，带行号

默认上限2000行、总输出128KB，超出自动截断并提示。limit必须>0。
"""

import os

MAX_OUTPUT_CHARS = 128 * 1024  # 128KB 总输出上限

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read file content with line numbers. Default max 2000 lines and 128KB total output, truncated with notice if exceeded. limit must be > 0. Use offset+limit to read in chunks. When remote=True, read remote file via SFTP",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path (absolute or relative to working directory)"},
                "offset": {"type": "integer", "description": "Starting line (1-based). Omit to read from beginning"},
                "limit": {"type": "integer", "description": "Max lines, must be > 0, default 2000"},
                "remote": {"type": "boolean", "description": "Read remote file via SFTP (requires prior Terminal connect)"},
                "host": {"type": "string", "description": "Remote host IP (only used when remote=True)"},
            },
            "required": ["file_path"],
        },
    },
}


def execute(file_path: str, offset: int = 0, limit: int = 2000,
            remote: bool = False, host: str = "",
            _tool_context=None) -> str:
    """
    读取文件内容。

    Args:
        file_path: 文件绝对路径
        offset: 起始行号(1-based)，0表示从头读
        limit: 最大行数，必须>0，默认2000
        remote: 通过SFTP读取远程文件（需先Terminal connect）
        host: 远程主机IP（仅remote=True时使用）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        带行号的文件内容字符串，格式 "  行号→内容"
    """
    # AI可能传字符串类型的数值参数，确保类型正确
    offset = int(offset) if offset else 0
    limit = int(limit) if limit else 0
    if limit <= 0:
        return "错误: limit必须>0"
    remote = bool(remote)
    if remote:
        from ..terminal.remote import remote_read
        result = remote_read(file_path, offset, limit, host)
        if "错误" not in result and _tool_context:
            _tool_context.mark_remote_read(file_path, host)
        return result
    if not os.path.isfile(file_path):
        return f"错误: 文件不存在: {file_path}"

    # 标记文件已被Read（供Write检查）
    if _tool_context:
        _tool_context.mark_read(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return f"错误: 权限不足: {file_path}"
    except OSError as e:
        return f"错误: 读取失败: {e}"

    # 应用offset/limit
    total_lines = len(lines)
    start = max(offset - 1, 0) if offset > 0 else 0
    lines = lines[start:start + limit]

    # 格式化输出，同时检查总字符数
    result = []
    char_count = 0
    truncated_by_size = False
    for i, line in enumerate(lines):
        line_num = start + i + 1
        content = line.rstrip("\n\r")
        formatted = f"  {line_num}→{content}"
        char_count += len(formatted) + 1  # +1 for \n
        if char_count > MAX_OUTPUT_CHARS:
            truncated_by_size = True
            break
        result.append(formatted)

    # 截断提示（优先报字符数截断）
    if truncated_by_size:
        result.append(f"  ... [输出截断: 已达 {MAX_OUTPUT_CHARS // 1024}KB 上限。使用 offset/limit 参数可读取其余部分]")
    elif len(lines) > 0 and start + len(lines) < total_lines:
        result.append(f"  ... [截断: 文件共 {total_lines} 行，已显示 {len(lines)} 行。使用 offset/limit 参数可读取其余部分]")

    return "\n".join(result)
