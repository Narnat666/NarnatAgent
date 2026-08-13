"""Write工具 —— 创建新文件或完整覆写文件

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import os
import difflib

from ..diff_utils import colorize_diff
from ..terminal import _normalize_device_for_tools, _file_tool_device_hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "创建新文件或全量覆盖文件。支持本地或远程写入文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "content": {"type": "string", "description": "完整文件内容"},
                "device": {"type": "string", "description": "设备dev编号：默认dev0（可省略）写入本机文件，设置dev1..devn则写入被控设备文件（需先Terminal connect被控设备获取dev编号）"},
            },
            "required": ["file_path", "content"],
        },
    },
}



def execute(file_path: str, content: str,
            device: str = "",
            _tool_context=None) -> tuple:
    """
    创建或覆写文件。

    Args:
        file_path: 文件路径
        content: 完整文件内容
        device: 设备dev编号：dev0=本机（默认），dev1..devn=被控设备（需先Terminal connect）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示新建文件无需diff
    """
    device = _normalize_device_for_tools(device)
    if device is None:
        return (f"[错误: {_file_tool_device_hint()}]", "")

    if device:
        from ..terminal.remote import remote_write
        return remote_write(file_path, content, device, _tool_context=_tool_context)
    abs_path = os.path.abspath(file_path)

    # 目录路径：open()会报Permission denied，误导AI去查权限而非换路径，提前拦截给出真实原因
    if os.path.isdir(abs_path):
        return (f"[错误: {file_path} 是目录，请使用正确的文件路径]", "")

    # 覆写已有文件前检查是否Read过
    if os.path.isfile(abs_path):
        if _tool_context and not _tool_context.is_read(abs_path):
            return ((f"[错误: 覆写已有文件前必须先Read确认当前内容。"
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
            # 旧内容编码探测（与Read一致）：GBK文件按utf-8-sig读会抛异常导致diff静默丢失，
            # AI 覆写后看不到差异。此处仅用于展示diff，errors=replace 不影响写入内容
            from ..read import _detect_text_encoding
            with open(abs_path, "rb") as fb:
                head = fb.read(8192)
            if not (head and b"\x00" in head):
                encoding = _detect_text_encoding(head)
                with open(abs_path, "r", encoding=encoding, errors="replace", newline='') as f:
                    old_content = f.read()
                diff = _make_diff(old_content, content, file_path)
                color_diff = colorize_diff(diff)
        except Exception:
            pass

    try:
        with open(abs_path, "w", encoding="utf-8", newline='') as f:
            f.write(content)
    except OSError as e:
        return (f"[错误: 写入失败: {e}]", "")

    byte_count = len(content.encode("utf-8"))
    if _tool_context:
        _tool_context.mark_read(abs_path)

    if diff:
        return (f"[已写入: {file_path} ({byte_count}字节)]\n{diff}", color_diff)
    return (f"[已写入: {file_path} ({byte_count}字节)]", color_diff)


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
    return result if result else "[无差异]"
