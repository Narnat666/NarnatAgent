"""Grep工具 —— 按内容搜索代码，定位关键行"""

import fnmatch
import os
import re


_DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": "Search file content by regex to locate key lines. Results truncated to 100 by default (controlled by head_limit), truncation notice returned when exceeded, increase head_limit for full results",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Search path. Suggest specifying a specific directory or file. Use glob to limit file types to avoid excessive output"},
                "glob": {"type": "string", "description": "Limit file types, e.g. *.py"},
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Output format, default files_with_matches",
                },
                "i": {"type": "boolean", "description": "Case insensitive"},
                "n": {"type": "boolean", "description": "Show line numbers (content mode)"},
                "multiline": {"type": "boolean", "description": "Multiline matching mode"},
                "A": {"type": "integer", "description": "Show N lines of context after match"},
                "B": {"type": "integer", "description": "Show N lines of context before match"},
                "C": {"type": "integer", "description": "Show N lines of context before and after match"},
                "head_limit": {"type": "integer", "description": "Limit output to first N matches, default 100. Must be a positive integer"},
            },
            "required": ["pattern"],
        },
    },
}


def execute(
    pattern: str,
    path: str = "",
    glob: str = "",
    output_mode: str = "files_with_matches",
    i: bool = False,
    n: bool = False,
    multiline: bool = False,
    A: int = 0,
    B: int = 0,
    C: int = 0,
    head_limit: int = 100,
    _tool_context=None,
) -> str:
    """
    按正则搜索文件内容。

    Args:
        pattern: 正则表达式
        path: 搜索路径，可以是目录或文件
        glob: 限定文件类型，如 *.py
        output_mode: files_with_matches / content / count
        i: 忽略大小写
        n: 显示行号(content模式)
        multiline: 多行匹配
        A/B/C: 上下文行数
        head_limit: 限制输出前N条

    Returns:
        搜索结果字符串
    """
    # 编译正则
    flags = 0
    if i:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"错误: 非法正则: {e}"

    # 上下文：C覆盖A和B
    if C > 0:
        A = C
        B = C

    root = path or os.getcwd()
    if os.path.isfile(root):
        # path是文件 → 直接在该文件内搜索
        return _search_single_file(root, regex, output_mode, n, A, B, head_limit)
    if not os.path.isdir(root):
        return f"错误: 路径不存在: {root}"

    ignore_dirs = _DEFAULT_IGNORE_DIRS
    if _tool_context and _tool_context.ignore_dirs:
        ignore_dirs = set(_tool_context.ignore_dirs)

    results = []
    file_matches = _search_files(root, regex, glob, output_mode, n, A, B, head_limit, ignore_dirs)
    truncated = False

    if output_mode == "files_with_matches":
        for rel_path in file_matches:
            results.append(rel_path)
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
        output = "\n".join(results) if results else "(无匹配)"
    elif output_mode == "count":
        for rel_path, count in file_matches:
            results.append(f"{rel_path}:{count}")
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
        output = "\n".join(results) if results else "(无匹配)"
    else:  # content
        for item in file_matches:
            results.append(item)
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
        output = "\n".join(results) if results else "(无匹配)"

    if truncated:
        output += f"\n...[已截断: 超出head_limit({head_limit})限制, 增大head_limit获取完整结果]"

    return output


def _search_single_file(file_path, regex, output_mode, show_n, A, B, head_limit):
    """在单个文件内执行搜索"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="strict") as f:
            content = f.read()
    except (UnicodeDecodeError, PermissionError, OSError):
        return "(无匹配)"

    if output_mode == "files_with_matches":
        return file_path if regex.search(content) else "(无匹配)"
    elif output_mode == "count":
        matches = regex.findall(content)
        return f"{file_path}:{len(matches)}" if matches else "(无匹配)"
    else:  # content
        lines = content.split("\n")
        results, count = _match_lines(file_path, lines, regex, A, B, head_limit)
        output = "\n".join(results) if results else "(无匹配)"
        if head_limit and count >= head_limit:
            output += f"\n...[已截断: 超出head_limit({head_limit})限制, 增大head_limit获取完整结果]"
        return output


def _match_lines(path_label, lines, regex, A, B, head_limit):
    """逐行匹配并收集结果（content模式），供单文件和目录搜索共用。

    Returns:
        (results, match_count) — results含上下文行，match_count仅计匹配行数
    """
    results = []
    count = 0
    for line_idx, line in enumerate(lines):
        if not regex.search(line):
            continue
        line_num = line_idx + 1
        if B > 0 or A > 0:
            start = max(0, line_idx - B)
            end = min(len(lines), line_idx + A + 1)
            context_lines = []
            for ci in range(start, end):
                prefix = ">" if ci == line_idx else " "
                context_lines.append(f"  {prefix} {ci+1}:{lines[ci]}")
            results.append(f"{path_label}:{line_num}:{line}")
            results.extend(context_lines)
        else:
            results.append(f"{path_label}:{line_num}:{line}")
        count += 1
        if head_limit and count >= head_limit:
            break
    return results, count


def _search_files(root, regex, glob_filter, output_mode, show_n, A, B, head_limit, ignore_dirs):
    """遍历文件执行搜索"""

    results = []
    count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fname in filenames:
            if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)

            # 跳过二进制文件
            try:
                with open(full, "r", encoding="utf-8", errors="strict") as f:
                    content = f.read()
            except (UnicodeDecodeError, PermissionError, OSError):
                continue

            if output_mode == "files_with_matches":
                if regex.search(content):
                    results.append(rel)
                    count += 1
            elif output_mode == "count":
                matches = regex.findall(content)
                if matches:
                    results.append((rel, len(matches)))
                    count += 1
            else:  # content
                lines = content.split("\n")
                remaining = head_limit - count if head_limit else 0
                matched, match_count = _match_lines(rel, lines, regex, A, B, remaining)
                results.extend(matched)
                count += match_count

            if head_limit and count >= head_limit:
                break
        if head_limit and count >= head_limit:
            break

    return results
