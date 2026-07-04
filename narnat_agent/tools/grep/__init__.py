"""Grep工具 —— 按内容搜索代码，定位关键行"""

import fnmatch
import os
import re

_DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": "正则搜索文件内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索路径（默认当前）"},
                "glob": {"type": "string", "description": "文件类型过滤，如*.py（默认空）"},
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "输出格式（默认files_with_matches）",
                },
                "i": {"type": "boolean", "description": "是否忽略大小写（默认否）"},
                "n": {"type": "boolean", "description": "是否显示行号（content模式专用，默认否）"},
                "multiline": {"type": "boolean", "description": "是否多行匹配（默认否）"},
                "A": {"type": "integer", "description": "额外带后续N行（默认0）"},
                "B": {"type": "integer", "description": "额外带前面N行（默认0）"},
                "C": {"type": "integer", "description": "额外带前后各N行（默认0）"},
                "head_limit": {"type": "integer", "description": "最大返回结果数（默认100）"},
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
    flags = 0
    if i:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"错误: 非法正则: {e}"

    if C > 0:
        A = C
        B = C

    root = path or os.getcwd()
    if os.path.isfile(root):
        return _search_single_file(root, regex, output_mode, n, A, B, head_limit)
    if not os.path.isdir(root):
        return f"错误: 路径不存在: {root}"

    ignore_dirs = _DEFAULT_IGNORE_DIRS
    if _tool_context and _tool_context.ignore_dirs:
        ignore_dirs = set(_tool_context.ignore_dirs)

    file_matches = _search_files(root, regex, glob, output_mode, n, A, B, head_limit, ignore_dirs)
    return _format_results(file_matches, output_mode, head_limit)


def _format_results(file_matches, output_mode, head_limit):
    """格式化输出结果"""
    results = []
    truncated = False
    
    if output_mode == "files_with_matches":
        for rel_path in file_matches:
            results.append(rel_path)
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
    elif output_mode == "count":
        for rel_path, count in file_matches:
            results.append(f"{rel_path}:{count}")
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
    else:
        for item in file_matches:
            results.append(item)
            if head_limit and len(results) >= head_limit:
                truncated = True
                break
    
    output = "\n".join(results) if results else "(无匹配)"
    if truncated:
        output += f"\n...[已截断: 超出head_limit({head_limit})限制]"
    return output


def _search_single_file(file_path, regex, output_mode, show_n, A, B, head_limit):
    """在单个文件内执行搜索，保持输入路径完整"""
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="strict") as f:
            content = f.read()
    except (UnicodeDecodeError, PermissionError, OSError):
        return "(无匹配)"

    if output_mode == "files_with_matches":
        return file_path if regex.search(content) else "(无匹配)"
    elif output_mode == "count":
        matches = regex.findall(content)
        return f"{file_path}:{len(matches)}" if matches else "(无匹配)"
    else:
        lines = content.split("\n")
        results, count = _match_lines(file_path, lines, regex, A, B, head_limit, show_n)
        output = "\n".join(results) if results else "(无匹配)"
        if head_limit and count >= head_limit:
            output += f"\n...[已截断: 超出head_limit({head_limit})限制]"
        return output


def _match_lines(path_label, lines, regex, A, B, head_limit, show_n=False):
    """逐行匹配并收集结果（content模式），统一使用 : 作为分隔符"""
    results = []
    count = 0
    for line_idx, line in enumerate(lines):
        if not regex.search(line):
            continue
        line_num = line_idx + 1
        
        if B > 0 or A > 0:
            start = max(0, line_idx - B)
            end = min(len(lines), line_idx + A + 1)
            for ci in range(start, end):
                is_match_line = (ci == line_idx)
                marker = ">" if is_match_line else " "
                if show_n:
                    results.append(f"{path_label}:{marker}{ci+1}:{lines[ci]}")
                else:
                    results.append(f"{path_label}:{marker}{lines[ci]}")
        else:
            if show_n:
                results.append(f"{path_label}:{line_num}:{line}")
            else:
                results.append(f"{path_label}:{line}")
        
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

            try:
                with open(full, "r", encoding="utf-8-sig", errors="strict") as f:
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
            else:
                lines = content.split("\n")
                remaining = head_limit - count if head_limit else 0
                matched, match_count = _match_lines(rel, lines, regex, A, B, remaining, show_n)
                results.extend(matched)
                count += match_count

            if head_limit and count >= head_limit:
                break
        if head_limit and count >= head_limit:
            break

    return results
