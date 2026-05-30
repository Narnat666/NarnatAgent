"""Edit工具 —— 精确修改文件内容"""

import os
import difflib


def execute(file_path: str, old_string: str, new_string: str,
            replace_all: bool = False) -> str:
    """
    精确替换文件中的字符串。

    Args:
        file_path: 文件路径
        old_string: 要替换的原文（必须精确匹配）
        new_string: 替换后的新文
        replace_all: 替换所有匹配（默认只替换第一个）

    Returns:
        确认信息 + unified diff
    """
    if not old_string:
        return "错误: old_string不能为空"

    if not os.path.isfile(file_path):
        return f"错误: 文件不存在: {file_path}，如需创建请用Write工具"

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except PermissionError:
        return f"错误: 权限不足: {file_path}"
    except OSError as e:
        return f"错误: 读取失败: {e}"

    # 查找old_string
    count = content.count(old_string)
    if count == 0:
        # 计算相似行提示
        hint = _find_similar(content, old_string)
        return f"错误: 未找到匹配文本。请先Read确认文件内容。\n{hint}"

    if count > 1 and not replace_all:
        return (f"错误: 找到{count}处匹配，old_string不唯一。"
                f"请扩大上下文使其唯一，或设置replace_all=True")

    # 执行替换
    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    # 写回文件
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return f"错误: 写入失败: {e}"

    # 生成unified diff
    diff = _make_diff(content, new_content, file_path)
    replaced_count = count if replace_all else 1
    return f"已替换{replaced_count}处\n{diff}"


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
