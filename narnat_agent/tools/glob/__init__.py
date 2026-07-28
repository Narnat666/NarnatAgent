"""Glob工具 —— 按模式匹配文件和目录（fd 启发的高性能版本）

核心改进（借鉴 fd）：
- os.scandir() 替代 glob.glob() —— 单次系统调用拿 name + type，避免额外 stat
- 遍历时即时跳过忽略目录 —— 不再进入 .git/node_modules 等子树
- glob → 预编译正则 —— 匹配在遍历循环内完成，零额外开销
- 堆维护 top-K（按 mtime） —— O(N log K) 时间 + O(K) 内存
- 迭代式 DFS —— 百万级文件无递归栈溢出风险
"""

from __future__ import annotations

import heapq
import os
import re
from functools import lru_cache

_DEFAULT_IGNORE_DIRS: set[str] = {
    ".git", "__pycache__", "node_modules", ".svn", ".hg",
    "venv", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".idea", ".vscode", ".tox", ".nox",
}
_MAX_BRACE_EXPANSIONS = 100
_MAX_HARD_LIMIT = 50_000


# ── 花括号展开 ────────────────────────────────────────────────

def _expand_braces(pattern: str) -> list[str]:
    """展开花括号。支持 \\{ \\} \\, 转义，无逗号/..时不展开（对齐 bash 语义）。"""
    if "{" not in pattern:
        return [pattern]

    depth = 0
    start = -1
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        # 反斜杠后紧跟 { } , → 跳过，不参与深度 / 切割
        if c == "\\" and i + 1 < n and pattern[i + 1] in ("{", "}", ","):
            i += 2
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)  # 防止负深度（模式如 }abc{）
            if depth == 0 and start != -1:
                head = pattern[:start]
                body = pattern[start + 1:i]
                tail = pattern[i + 1:]

                # 检查 body 中是否有顶层的逗号或 ..（需要展开）
                has_expand = False
                b_depth = 0
                j = 0
                while j < len(body):
                    ch = body[j]
                    if ch == "\\" and j + 1 < len(body) and body[j + 1] in ("{", "}", ","):
                        j += 2
                        continue
                    if ch == "{":
                        b_depth += 1
                    elif ch == "}":
                        b_depth = max(b_depth - 1, 0)
                    elif ch == "," and b_depth == 0:
                        has_expand = True
                        break
                    elif ch == "." and b_depth == 0:
                        # 检测 .. （范围序列，如 {1..5}）
                        if j + 1 < len(body) and body[j + 1] == ".":
                            has_expand = True
                            break
                    j += 1

                if not has_expand:
                    # 无逗号/..，不展开，保留字面花括号
                    return [pattern]

                # 按顶层逗号切分
                options: list[str] = []
                b_depth = 0
                last = 0
                j = 0
                while j < len(body):
                    ch = body[j]
                    if ch == "\\" and j + 1 < len(body) and body[j + 1] in ("{", "}", ","):
                        j += 2
                        continue
                    if ch == "{":
                        b_depth += 1
                    elif ch == "}":
                        b_depth = max(b_depth - 1, 0)
                    elif ch == "," and b_depth == 0:
                        options.append(body[last:j])
                        last = j + 1
                    j += 1
                options.append(body[last:])

                results: list[str] = []
                for opt in options:
                    for exp in _expand_braces(head + opt + tail):
                        if len(results) >= _MAX_BRACE_EXPANSIONS:
                            return results
                        results.append(exp)
                return results
        i += 1
    # 花括号不匹配，原样返回
    return [pattern]


def _unescape_braces(pattern: str) -> str:
    """去除花括号相关转义：\\{ → {, \\} → }, \\, → ,。"""
    result: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i] == "\\" and i + 1 < n and pattern[i + 1] in ("{", "}", ","):
            result.append(pattern[i + 1])
            i += 2
        else:
            result.append(pattern[i])
            i += 1
    return "".join(result)


# ── glob → 正则编译 ────────────────────────────────────────────

def _pattern_has_uppercase(pattern: str) -> bool:
    """检测 glob pattern 是否含字面上需要区分的大小写字符（smart case）。"""
    return any(c.isupper() for c in pattern)


@lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """将 glob pattern 编译为正则（带缓存，对齐 fd build_regex 语义）。

    规则（对齐 fd GlobBuilder 语义）：
      ** 仅当独立路径组件时才跨目录（首 / 尾 / 紧邻 /），否则等价于 *
      **/ → (?:[^/]+/)*   零回溯跨任意层目录
      *   → [^/]*         不跨路径分隔符
      ?   → [^/]
      [abc] 保持，[!abc] → [^abc]
      其余 → re.escape
    """
    # Windows 上 \ 是路径分隔符；Linux 上 \ 是合法文件名字符，不替换
    if os.name == "nt":
        p = pattern.replace("\\", "/")
    else:
        p = pattern

    # 去掉前导 ./ 和 /
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/").rstrip("/")
    if not p:
        p = "*"

    parts: list[str] = []
    i = 0
    n = len(p)
    pending_slash = False  # 前一个字符是 / 且尚未输出

    while i < n:
        c = p[i]

        if pending_slash:
            # / 之后紧跟 ** 且到末尾 → (?:/.*)?
            if c == "*" and i + 1 < n and p[i + 1] == "*" and i + 2 >= n:
                parts.append("(?:/.*)?")
                pending_slash = False
                i += 2
                continue
            # / 之后紧跟 **/  → /(?:[^/]+/)*
            if c == "*" and i + 1 < n and p[i + 1] == "*" and i + 2 < n and p[i + 2] == "/":
                parts.append("/(?:[^/]+/)*")
                pending_slash = False
                i += 3
                continue
            # 否则：/ 是普通分隔符，输出后再处理当前字符
            parts.append("/")
            pending_slash = False
            # fall through 继续处理 c

        if c == "/":
            pending_slash = True
            i += 1
        elif c == "*":
            if i + 1 < n and p[i + 1] == "*":
                i += 2
                # ** 仅在独立路径组件时跨目录
                at_boundary_start = (i - 2 == 0) or (i - 3 >= 0 and p[i - 3] == "/")
                at_boundary_end = (i == n) or (i < n and p[i] == "/")

                if i < n and p[i] == "/":
                    parts.append("(?:[^/]+/)*")
                    i += 1
                elif at_boundary_start and at_boundary_end:
                    # ** 独立存在（如 "a/**" 或 "**"）
                    parts.append(".*")
                else:
                    # a**b → a[^/]*b
                    parts.append("[^/]*")
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            negate = False
            if j < n and p[j] in ("!", "^"):
                negate = True
                j += 1
            had_literal_close = False
            if j < n and p[j] == "]":
                j += 1
                had_literal_close = True
            end = p.find("]", j)
            if end == -1:
                parts.append(re.escape(c))
                i += 1
                continue
            inner = ("]" if had_literal_close else "") + p[j:end]
            # 验证字符类合法性；无效范围（如 [z-a]）降级为逐字匹配
            try:
                re.compile("[" + inner + "]")
            except re.error:
                inner = "".join(re.escape(ch) for ch in inner)
            if negate:
                parts.append("[^" + inner + "]")
            else:
                parts.append("[" + inner + "]")
            i = end + 1
        else:
            parts.append(re.escape(c))
            i += 1

    # 末尾残留待定 /（如 pattern 原样以 / 结尾）
    if pending_slash:
        parts.append("/")

    flags = 0 if _pattern_has_uppercase(pattern) else re.IGNORECASE
    return re.compile("".join(parts), flags)


# ── 目录遍历 & 匹配 ────────────────────────────────────────────

def _collect(
    root: str,
    regexes: list[re.Pattern[str]],
    ignore_dirs: set[str],
    max_results: int,
    skip_hidden_files: bool,
) -> tuple[list[tuple[str, float]], int]:
    """scandir 遍历目录树，返回 (按 mtime 降序的结果, 总匹配数)。"""
    root = os.path.abspath(root)
    heap: list[tuple[float, str]] = []  # (mtime, rel_path) — 堆中统一用 / 分隔
    total = 0
    stack: list[tuple[str, str]] = [(root, "")]
    single_regex = regexes[0] if len(regexes) == 1 else None
    _is_nt = (os.name == "nt")  # 缓存，避免循环内重复判断

    while stack:
        cur_dir, rel_prefix = stack.pop()

        try:
            with os.scandir(cur_dir) as entries:
                subdirs: list[tuple[str, str]] = []
                for entry in entries:
                    name = entry.name

                    # ── 符号链接：仅跳过指向目录的符号链接（防死循环），文件符号链接正常匹配 ──
                    if entry.is_symlink():
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                continue
                        except OSError:
                            continue
                        # 符号链接文件：作为普通文件继续处理
                        is_dir = False
                    else:
                        try:
                            is_dir = entry.is_dir()
                        except OSError:
                            continue

                    # ── 隐藏文件过滤（对齐 fd：pattern 以 . 开头则不过滤） ──
                    if skip_hidden_files and name.startswith("."):
                        if is_dir:
                            # 隐藏目录不遍历但也不匹配
                            continue
                        else:
                            continue

                    if is_dir:
                        if name in ignore_dirs:
                            continue

                        # ── 目录也参与匹配（对齐 DEFINITION"匹配文件和目录"） ──
                        dir_rel = rel_prefix + name  # 无尾部斜杠
                        dir_rel_norm = _norm_path(dir_rel, _is_nt)
                        if single_regex is not None:
                            dir_matched = single_regex.fullmatch(dir_rel_norm) is not None
                        else:
                            dir_matched = any(rx.fullmatch(dir_rel_norm) for rx in regexes)

                        if dir_matched:
                            try:
                                dir_mtime = entry.stat().st_mtime
                            except OSError:
                                dir_mtime = 0.0
                            total += 1
                            if len(heap) >= max_results:
                                heapq.heappushpop(heap, (dir_mtime, dir_rel_norm))
                            else:
                                heapq.heappush(heap, (dir_mtime, dir_rel_norm))

                        subdirs.append((entry.path, dir_rel + "/"))
                        continue

                    # ── 匹配文件 ──
                    rel = rel_prefix + name if rel_prefix else name
                    rel_match = _norm_path(rel, _is_nt)

                    matched = False
                    if single_regex is not None:
                        matched = single_regex.fullmatch(rel_match) is not None
                    else:
                        for rx in regexes:
                            if rx.fullmatch(rel_match):
                                matched = True
                                break

                    if matched:
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        total += 1
                        if len(heap) >= max_results:
                            heapq.heappushpop(heap, (mtime, rel_match))
                        else:
                            heapq.heappush(heap, (mtime, rel_match))

                # 子目录入栈（顺序不影响最终排序结果，直接 extend）
                stack.extend(subdirs)

        except (PermissionError, OSError):
            continue

    results: list[tuple[str, float]] = [
        (rel.replace("/", os.sep) if os.sep != "/" else rel, mtime)
        for mtime, rel in sorted(heap, key=lambda x: (-x[0], x[1]))
    ]
    return results, total


def _norm_path(path: str, is_nt: bool) -> str:
    """归一化路径分隔符：Windows 上 \\ → /，Linux 原样返回。"""
    if is_nt and "\\" in path:
        return path.replace("\\", "/")
    return path


# ── 公共接口 ────────────────────────────────────────────────────

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": (
            '按模式匹配文件和目录。"*" 只匹配当前目录；"**" 递归所有子目录。'
            '例："**/*.h"、"src/**/*.cpp"、"*.{docx,pdf}"。返回匹配路径，按修改时间倒序。'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob模式"},
                "path": {"type": "string", "description": "搜索目录（默认当前目录）"},
                "max_results": {"type": "integer", "description": "最大结果数（正整数，默认500）"},
            },
            "required": ["pattern"],
        },
    },
}


def execute(pattern: str, path: str = "", max_results: int = 500, _tool_context=None) -> str:
    root = path or os.getcwd()
    if not os.path.isdir(root):
        return f"错误: 目录不存在: {root}"

    if max_results <= 0:
        return "[错误: max_results需为正整数]"

    # 硬上限防止 OOM（对齐 fd 设计）
    max_results = min(max_results, _MAX_HARD_LIMIT)

    # 合并 ignore_dirs
    ignore_dirs = _DEFAULT_IGNORE_DIRS.copy()
    if _tool_context is not None:
        extra = getattr(_tool_context, "ignore_dirs", None)
        if extra:
            ignore_dirs |= set(extra)

    # 1. 展开花括号 + 去转义
    raw_patterns = _expand_braces(pattern)
    patterns = [_unescape_braces(p) for p in raw_patterns]

    # 2. 预编译所有 pattern 为正则
    regexes = [_compile_pattern(p) for p in patterns]

    # 3. 隐藏文件过滤（对齐 fd：pattern 以 . 开头则不过滤隐藏文件）
    skip_hidden_files = not any(
        p.lstrip("./").startswith(".") for p in patterns
    )

    # 4. scandir 遍历 + 堆 top-K
    results, total = _collect(root, regexes, ignore_dirs, max_results, skip_hidden_files)

    if not results:
        return "(无匹配)"

    output = "\n".join(m[0] for m in results)

    if total > max_results:
        output += (
            f"\n...[已截断: 共{total}个匹配项, "
            f"当前显示按修改时间最近的{max_results}个。增大max_results可获取完整列表]"
        )

    return output
