"""Edit工具 —— 精确修改文件内容

支持两种模式:
1. 行号范围替换: Edit(file_path, line_start, line_end, new_string)
   - 替换 [line_start, line_end] 行（含两端）为 new_string
   - 省略 line_end 则只替换 line_start 一行
   - new_string 为空字符串则删除指定行

2. 字符串精确替换: Edit(file_path, old_string, new_string)
   - old_string 必须精确匹配文件内容
   - replace_all=True 替换所有匹配

行号模式更高效：Read → Edit(file, line_start, line_end, new_string)
省掉中间的 old_string 拷贝步骤。
"""

import os
import difflib

from ..ui.ui_design import colorize_diff


def execute(file_path: str, old_string: str = "", new_string: str = "",
            replace_all: bool = False,
            line_start: int = 0, line_end: int = 0,
            remote: bool = False, host: str = "") -> tuple:
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
        remote: 通过SFTP修改远程文件（需先Terminal connect）
        host: 远程主机（仅remote=True时使用）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示
    """
    # AI可能传字符串类型的数值参数，确保类型正确
    line_start = int(line_start) if line_start else 0
    line_end = int(line_end) if line_end else 0
    replace_all = bool(replace_all)
    remote = bool(remote)
    if remote:
        from .remote import remote_edit
        return remote_edit(file_path, old_string, new_string, replace_all,
                          line_start, line_end, host)
    if not os.path.isfile(file_path):
        return (f"错误: 文件不存在: {file_path}，如需创建请用Write工具", "")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except PermissionError:
        return (f"错误: 权限不足: {file_path}", "")
    except OSError as e:
        return (f"错误: 读取失败: {e}", "")

    # ── 行号模式 ──
    if line_start > 0:
        return _edit_by_lines(content, file_path, line_start, line_end, new_string)

    # ── 字符串模式 ──
    if not old_string:
        return ("错误: old_string不能为空（或使用line_start行号模式）", "")

    count = content.count(old_string)
    if count == 0:
        hint = _find_similar(content, old_string)
        return (f"错误: 未找到匹配文本。请先Read确认文件内容。\n{hint}", "")

    if count > 1 and not replace_all:
        return ((f"错误: 找到{count}处匹配，old_string不唯一。"
                 f"请扩大上下文使其唯一，或设置replace_all=True"), "")

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    return _write_and_diff(content, new_content, file_path,
                           count if replace_all else 1)


def _edit_by_lines(content: str, file_path: str,
                   line_start: int, line_end: int, new_string: str) -> tuple:
    """行号范围替换"""
    lines = content.splitlines(keepends=True)
    total = len(lines)

    # line_end 默认等于 line_start（替换单行）
    if line_end <= 0:
        line_end = line_start

    # 边界检查
    if line_start < 1 or line_start > total:
        return (f"错误: line_start={line_start} 超出范围（1-{total}）", "")
    if line_end < line_start:
        return (f"错误: line_end={line_end} < line_start={line_start}", "")
    if line_end > total:
        return (f"错误: line_end={line_end} 超出范围（1-{total}）", "")

    # 构造新内容
    new_lines = new_string.splitlines(keepends=True)
    if new_string and not new_string.endswith("\n"):
        new_lines[-1] = new_lines[-1] + _detect_line_ending(content)

    # 替换 [line_start-1, line_end) 范围的行
    new_content_lines = lines[:line_start - 1] + new_lines + lines[line_end:]
    new_content = "".join(new_content_lines)

    replaced_count = line_end - line_start + 1
    return _write_and_diff(content, new_content, file_path, replaced_count,
                           f"行{line_start}-{line_end}")


def _detect_line_ending(content: str) -> str:
    """检测文件的行尾格式"""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def _write_and_diff(old_content: str, new_content: str, file_path: str,
                    count: int, range_desc: str = "") -> tuple:
    """写回文件并生成diff。

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示无差异
    """
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return (f"错误: 写入失败: {e}", "")

    diff = _make_diff(old_content, new_content, file_path)
    if range_desc:
        llm_result = f"已替换{range_desc}（{count}行）\n{diff}"
    else:
        llm_result = f"已替换{count}处\n{diff}"

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
    return result if result else "(无差异)"


def _make_color_diff(diff_text: str) -> str:
    """对着色diff调用ui层着色函数，返回ANSI着色文本"""
    return colorize_diff(diff_text)
