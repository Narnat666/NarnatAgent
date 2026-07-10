"""Write工具 —— 创建新文件或完整覆写文件"""

import os
import difflib

from ..diff_utils import colorize_diff

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "创建新文件或全量覆盖文件。本地或远程写入文件时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "content": {"type": "string", "description": "完整文件内容"},
                "remote": {"type": "boolean", "description": "是否写入远程文件（默认否，启用前需先Terminal连接）"},
                "host": {"type": "string", "description": "远程主机IP或域名（默认空，需启用remote）"},
            },
            "required": ["file_path", "content"],
        },
    },
}



def execute(file_path: str, content: str,
            remote: bool = False, host: str = "",
            _tool_context=None) -> tuple:
    """
    创建或覆写文件。

    Args:
        file_path: 文件路径
        content: 完整文件内容
        remote: 通过SFTP写入远程文件（需先Terminal connect）
        host: 远程主机（仅remote=True时使用）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示新建文件无需diff
    """
    remote = bool(remote)
    if remote:
        from ..terminal.remote import remote_write
        return remote_write(file_path, content, host, _tool_context=_tool_context)
    abs_path = os.path.abspath(file_path)

    # 覆写已有文件前检查是否Read过
    if os.path.isfile(abs_path):
        if _tool_context and not _tool_context.is_read(abs_path):
            return ((f"错误: 覆写已有文件前必须先Read确认当前内容。"
                     f"请先Read {file_path}，再决定用Edit还是Write。"), "")

    # 自动创建父目录
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # 覆写已有文件时生成diff
    color_diff = ""
    old_content = ""
    diff = ""
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8-sig", newline='') as f:
                old_content = f.read()
            diff = _make_diff(old_content, content, file_path)
            color_diff = colorize_diff(diff)
        except Exception:
            pass

    try:
        with open(abs_path, "w", encoding="utf-8", newline='') as f:
            f.write(content)
    except OSError as e:
        return (f"错误: 写入失败: {e}", "")

    byte_count = len(content.encode("utf-8"))
    if _tool_context:
        _tool_context.mark_read(abs_path)

    if diff:
        return (f"已写入: {file_path} ({byte_count}字节)\n{diff}", color_diff)
    return (f"已写入: {file_path} ({byte_count}字节)", color_diff)


def _make_diff(old_content: str, new_content: str, file_path: str) -> str:
    """生成unified diff"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{os.path.basename(file_path)}",
        tofile=f"b/{os.path.basename(file_path)}",
        lineterm="",
    )
    result = "\n".join(diff)
    return result if result else "(无差异)"
