"""Read工具 —— 读取文件内容，带行号

默认上限2000行，超出自动截断并提示。limit需为正整数。最终输出由系统全局上限控制。
"""

import os

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "读取纯文本文件内容，返还内容带行号（由1开始）。不可读取二进制文件。本地或远程读取文件时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "offset": {"type": "integer", "description": "起始行（默认1，含本行）"},
                "limit": {"type": "integer", "description": "读取行数（正整数，默认2000）"},
                "remote": {"type": "boolean", "description": "是否读取远程（默认否，启用前需先Terminal连接）"},
                "host": {"type": "string", "description": "远程主机IP或域名（默认空，需启用remote）"},
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
        limit: 最大行数，正整数，默认2000
        remote: 通过SFTP读取远程文件（需先Terminal connect）
        host: 远程主机IP（仅remote=True时使用）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        带行号的文件内容字符串，格式 "  行号→内容"
    """
    # AI可能传字符串类型的数值参数，确保类型正确并处理None
    offset = int(offset) if offset is not None else 0
    limit = int(limit) if limit is not None else 2000
    if limit <= 0:
        return "[错误: limit需为正整数]"
    
    remote = bool(remote)
    if remote:
        from ..terminal.remote import remote_read
        result = remote_read(file_path, offset, limit, host)
        if "错误" not in result and _tool_context:
            _tool_context.mark_remote_read(file_path, host)
        return result
        
    if not os.path.isfile(file_path):
        return f"[错误: 文件不存在: {file_path}]"

    # 统一转为绝对路径，供 _tool_context 标记和校验
    abs_path = os.path.abspath(file_path)

    # 标记文件已被Read（供Write检查）
    if _tool_context:
        _tool_context.mark_read(abs_path)

    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            start = max(offset - 1, 0) if offset > 0 else 0
            
            # 流式跳过 offset 行，避免大文件内存溢出
            for _ in range(start):
                if not f.readline():
                    break
            
            # 按需读取 limit 行
            result = []
            truncated_by_limit = False
            
            for i in range(limit):
                line = f.readline()
                if not line:
                    break  # 文件结束
                
                line_num = start + i + 1
                content = line.rstrip("\n\r")
                formatted = f"  {line_num}→{content}"
                
                result.append(formatted)
            else:
                # for...else: 循环正常结束（没有 break），说明读完了 limit 行
                # 此时再尝试读一行，如果非空，说明文件还有内容，被 limit 截断了
                if f.readline():
                    truncated_by_limit = True

    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # 截断提示
    if truncated_by_limit:
        result.append(f"  ... [截断: 已显示 {limit} 行。使用 offset={start + limit + 1} 参数可读取其余部分]")

    return "\n".join(result)
