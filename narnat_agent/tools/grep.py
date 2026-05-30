"""Grep工具 —— 按内容搜索代码，定位关键行"""

import fnmatch
import os
import re
from typing import Optional


_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}


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
    head_limit: int = 0,
) -> str:
    """
    按正则搜索文件内容。

    Args:
        pattern: 正则表达式
        path: 搜索目录
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

    root = path or os.getcwd()
    if not os.path.isdir(root):
        return f"错误: 目录不存在: {root}"

    # 上下文：C覆盖A和B
    if C > 0:
        A = C
        B = C

    results = []
    file_matches = _search_files(root, regex, glob, output_mode, n, A, B, head_limit)

    if output_mode == "files_with_matches":
        for rel_path in file_matches:
            results.append(rel_path)
            if head_limit and len(results) >= head_limit:
                break
        return "\n".join(results) if results else "(无匹配)"
    elif output_mode == "count":
        for rel_path, count in file_matches:
            results.append(f"{rel_path}:{count}")
            if head_limit and len(results) >= head_limit:
                break
        return "\n".join(results) if results else "(无匹配)"
    else:  # content
        for item in file_matches:
            results.append(item)
            if head_limit and len(results) >= head_limit:
                break
        return "\n".join(results) if results else "(无匹配)"


def _search_files(root, regex, glob_filter, output_mode, show_n, A, B, head_limit):
    """遍历文件执行搜索"""
    fnm = fnmatch

    results = []
    count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            if glob_filter and not fnm.fnmatch(fname, glob_filter):
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
                for line_idx, line in enumerate(lines):
                    m = regex.search(line)
                    if m:
                        line_num = line_idx + 1
                        # 上下文
                        context_lines = []
                        if B > 0 or A > 0:
                            start = max(0, line_idx - B)
                            end = min(len(lines), line_idx + A + 1)
                            for ci in range(start, end):
                                prefix = ">" if ci == line_idx else " "
                                context_lines.append(f"  {prefix} {ci+1}:{lines[ci]}")
                            results.append(f"{rel}:{line_num}:{line}")
                            results.extend(context_lines)
                        else:
                            results.append(f"{rel}:{line_num}:{line}")
                        count += 1

            if head_limit and count >= head_limit:
                break
        if head_limit and count >= head_limit:
            break

    return results
