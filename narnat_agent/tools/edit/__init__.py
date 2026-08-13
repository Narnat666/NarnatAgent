"""Edit工具 —— 精确修改文件内容

支持两种模式（互斥）:
1. 行号范围替换: Edit(file_path, line_start, line_end, new_string)
   - 替换 [line_start, line_end] 行（含两端）为 new_string
   - 省略 line_end 则只替换 line_start 一行
   - new_string 为空字符串则删除指定行

2. 字符串精确替换: Edit(file_path, old_string, new_string)
   - old_string 必须精确匹配文件内容
   - replace_all=True 替换所有匹配
   - 自动兼容 \r\n 和 \n 换行符

注意: line_start 和 old_string 不能同时使用，需选择一种模式。

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import os
import difflib

from ..diff_utils import colorize_diff
from ..param_utils import to_bool
from ..terminal import _normalize_device_for_tools, _file_tool_device_hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": (
            "编辑文件（字符串替换或行替换，两种模式互斥）。支持本地或远程编辑文件。"
            "首次编辑某文件前必须先Read该文件（未Read直接报错）。"
            "自动识别并保持原编码（UTF-8/GBK），自动兼容CRLF/LF换行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "old_string": {"type": "string", "description": "待替换文本（字符串模式）"},
                "new_string": {"type": "string", "description": "替换文本"},
                "replace_all": {"type": "boolean", "description": "是否替换全部匹配（字符串模式，默认否）"},
                "line_start": {"type": "integer", "description": "起始行（行号模式，默认不启用，传值≥1时启用行替换模式，含本行）"},
                "line_end": {"type": "integer", "description": "结束行（含本行，默认等于line_start）"},
                "device": {"type": "string", "description": "设备dev编号：默认dev0（可省略）编辑本机文件，设置dev1..devn则编辑被控设备文件（需先Terminal connect被控设备获取dev编号）"},
            },
            "required": ["file_path"],
        },
    },
}


def _read_for_edit(file_path: str) -> tuple:
    """读取文件内容用于编辑，返回 (content, 写回编码)。

    编码策略与 Read 一致（首块探测 utf-8-sig / gbk），但必须严格解码：
    Edit 会把内容写回文件，errors="replace" 会以 U+FFFD 永久替换原字节，
    造成静默数据损坏。探测结果无法严格解码时抛 UnicodeDecodeError，
    由调用方拒绝编辑（对齐远程 Edit 行为）。

    Raises:
        ValueError: 二进制文件（含NUL字节）
        UnicodeDecodeError: 无法按 UTF-8/GBK 严格解码
    """
    from ..read import _detect_text_encoding
    with open(file_path, "rb") as fb:
        head = fb.read(8192)
    if b"\x00" in head:
        raise ValueError("binary")
    encoding = _detect_text_encoding(head)
    with open(file_path, "r", encoding=encoding, newline="") as f:
        content = f.read()
    # 写回编码保持原文件形态：UTF-8 有 BOM 保留 BOM、无 BOM 不添加，GBK 写回 GBK
    if encoding == "utf-8-sig":
        write_encoding = "utf-8-sig" if head.startswith(b"\xef\xbb\xbf") else "utf-8"
    else:
        write_encoding = "gbk"
    return content, write_encoding


def execute(file_path: str, old_string: str = "", new_string: str = "",
            replace_all: bool = False,
            line_start: int = 0, line_end: int = 0,
            device: str = "",
            _tool_context=None) -> tuple:
    """
    修改文件内容。

    行号模式: Edit(file_path, line_start=10, line_end=15, new_string="...")
    字符串模式: Edit(file_path, old_string="...", new_string="...")

    Args:
        file_path: 文件路径
        old_string: 要替换的原文（字符串模式，必须精确匹配）
        new_string: 替换后的新文
        replace_all: 替换所有匹配（字符串模式，默认只替换第一个）
        line_start: 起始行号（行号模式，从1开始）
        line_end: 结束行号（行号模式，含此行；0或省略则等于line_start）
        device: 设备dev编号：dev0=本机（默认），dev1..devn=被控设备（需先Terminal connect）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示
    """
    # AI可能传字符串类型的数值参数，确保类型正确
    line_start = int(line_start) if line_start else 0
    line_end = int(line_end) if line_end else 0
    # 归一化布尔参数: LLM 偶发传 "false" 字符串，bool("false") 恒为 True
    # 会导致全量替换（与意图相反的危险行为），必须按字符串语义解析
    replace_all = to_bool(replace_all)

    device = _normalize_device_for_tools(device)
    if device is None:
        return (f"[错误: {_file_tool_device_hint()}]", "")

    if device:
        from ..terminal.remote import remote_edit
        return remote_edit(file_path, old_string, new_string, replace_all,
                          line_start, line_end, device, _tool_context=_tool_context)

    if not os.path.isfile(file_path):
        return (f"[错误: 文件不存在: {file_path}，如需创建请用Write工具]", "")

    abs_path = os.path.abspath(file_path)
    if _tool_context and not _tool_context.is_read(abs_path):
        return (f"[错误: 编辑前必须先Read该文件: {file_path}。"
                f"若Read已报错（如二进制文件），说明该文件无法用Edit编辑，请改用Shell工具处理]", "")

    try:
        content, write_encoding = _read_for_edit(file_path)
    except PermissionError:
        return (f"[错误: 权限不足: {file_path}]", "")
    except OSError as e:
        return (f"[错误: 读取失败: {e}]", "")
    except ValueError:
        return ("[错误: 检测到二进制文件（含NUL字节），Edit仅支持文本文件。请使用Shell工具处理]", "")
    except UnicodeDecodeError:
        return ((f"[错误: 文件非UTF-8/GBK编码，为防止内容损坏已拒绝编辑: {file_path}。"
                 f"请用Shell工具处理（如iconv转码后再编辑）]"), "")

    # ── 参数互斥检查 ──
    has_line_mode = line_start > 0
    has_string_mode = bool(old_string)

    if has_line_mode and has_string_mode:
        return ("[错误: line_start 和 old_string 不能同时使用，请选择一种模式:\n"
                "  - 行号模式: line_start + new_string\n"
                "  - 字符串模式: old_string + new_string", "")

    if has_line_mode and replace_all:
        return ("[错误: replace_all 仅用于字符串模式，行号模式不支持]", "")

    # ── 行号模式 ──
    if has_line_mode:
        return _edit_by_lines(content, file_path, line_start, line_end, new_string,
                              _tool_context, write_encoding)

    return _edit_by_string(content, old_string, new_string, replace_all, file_path,
                           _tool_context, write_encoding)


def _edit_by_string(content: str, old_string: str, new_string: str,
                    replace_all: bool, file_path: str,
                    _tool_context=None, write_encoding: str = "utf-8") -> tuple:
    """字符串精确替换，自动兼容换行符"""
    if not old_string:
        return ("[错误: old_string不能为空（或使用line_start行号模式）]", "")

    # 检测文件换行符风格，转换 old_string/new_string 以匹配
    has_crlf = '\r\n' in content
    if has_crlf:
        _normalize = lambda s: s.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
    else:
        _normalize = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')

    old_string_normalized = _normalize(old_string)
    new_string_normalized = _normalize(new_string)

    count = content.count(old_string_normalized)
    if count == 0:
        hint = _find_similar(content, old_string)
        return (f"[错误: 未找到匹配文本。请先Read确认文件内容。]\n{hint}", "")

    if count > 1 and not replace_all:
        return (f"[错误: 找到{count}处匹配，old_string不唯一。请扩大上下文使其唯一，或设置replace_all=True]", "")

    if replace_all:
        new_content = content.replace(old_string_normalized, new_string_normalized)
    else:
        new_content = content.replace(old_string_normalized, new_string_normalized, 1)

    return _write_and_diff(content, new_content, file_path, count if replace_all else 1,
                           _tool_context=_tool_context, write_encoding=write_encoding)


def _edit_by_lines(content: str, file_path: str,
                   line_start: int, line_end: int, new_string: str,
                   _tool_context=None, write_encoding: str = "utf-8") -> tuple:
    """行号范围替换"""
    lines = content.splitlines(keepends=True)
    total = len(lines)

    # line_end 默认等于 line_start（替换单行）
    if line_end <= 0:
        line_end = line_start

    # 边界检查
    if total == 0:
        return ("[错误: 文件为空，无法按行号编辑]", "")
    if line_start < 1 or line_start > total:
        return (f"[错误: line_start={line_start} 超出范围（1-{total}）]", "")
    if line_end < line_start:
        return (f"[错误: line_end={line_end} < line_start={line_start}]", "")
    if line_end > total:
        return (f"[错误: line_end={line_end} 超出范围（1-{total}）]", "")

    # 构造新内容：按文件换行符风格归一化 new_string
    line_ending = _detect_line_ending(content)
    if line_ending == "\r\n":
        new_string_normalized = new_string.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
    else:
        new_string_normalized = new_string.replace('\r\n', '\n').replace('\r', '\n')

    new_lines = new_string_normalized.splitlines(keepends=True)

    if new_lines and not new_string_normalized.endswith("\n"):
        new_lines[-1] = new_lines[-1] + line_ending

    # 替换 [line_start-1, line_end) 范围的行
    new_content_lines = lines[:line_start - 1] + new_lines + lines[line_end:]
    new_content = "".join(new_content_lines)

    replaced_count = line_end - line_start + 1
    return _write_and_diff(content, new_content, file_path, replaced_count,
                           f"行{line_start}-{line_end}", _tool_context=_tool_context,
                           write_encoding=write_encoding)


def _detect_line_ending(content: str) -> str:
    """检测文件的行尾格式"""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def _write_and_diff(old_content: str, new_content: str, file_path: str,
                    count: int, range_desc: str = "",
                    _tool_context=None, write_encoding: str = "utf-8") -> tuple:
    """写回文件并生成diff。

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示无差异
    """
    try:
        with open(file_path, "w", encoding=write_encoding, newline='') as f:
            f.write(new_content)
    except OSError as e:
        return (f"[错误: 写入失败: {e}]", "")
    except UnicodeEncodeError:
        # 新内容含文件编码无法表示的字符（如 GBK 文件中写入 emoji）
        return (f"[错误: 新内容包含文件编码({write_encoding})无法表示的字符，写入失败: {file_path}]", "")

    if _tool_context:
        _tool_context.mark_read(file_path)

    diff = _make_diff(old_content, new_content, file_path)
    if range_desc:
        llm_result = f"[已替换{range_desc}（{count}行）]\n{diff}"
    else:
        llm_result = f"[已替换{count}处]\n{diff}"

    color_diff = _make_color_diff(diff)
    return (llm_result, color_diff)


def _find_similar(content: str, old_string: str) -> str:
    """查找相似行，帮助LLM定位"""
    content_lines = content.splitlines()
    old_lines = old_string.strip().splitlines()
    if not old_lines:
        return ""

    target = old_lines[0].strip()
    similarities = []
    for i, line in enumerate(content_lines):
        ratio = difflib.SequenceMatcher(None, target, line.strip()).ratio()
        if ratio > 0.5:
            similarities.append((ratio, i + 1, line))

    if not similarities:
        return ""

    similarities.sort(reverse=True)
    hints = ["相似行（供参考）:"]
    for ratio, line_num, line in similarities[:3]:
        hints.append(f"  行{line_num}: {line.strip()} (相似度{ratio:.0%})")
    return "\n".join(hints)


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


def _make_color_diff(diff_text: str) -> str:
    """对着色diff调用ui层着色函数，返回ANSI着色文本"""
    return colorize_diff(diff_text)
