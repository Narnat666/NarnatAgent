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

class GlobLimits:
    """Glob 工具边界参数（原模块级常量收敛为类成员）"""
    MAX_BRACE_EXPANSIONS = 100
    MAX_HARD_LIMIT = 50_000


# 用户中断（ESC）导致遍历/扫描提前结束时的返回文本（Grep/Glob 共用同一标记）
_CANCELLED_TEXT = "[已取消: 用户中断，扫描提前结束]"


# ── 花括号展开 ────────────────────────────────────────────────

def _expand_range(body: str) -> list[str] | None:
    """展开 {N..M} / {c..c} 范围（bash 语义子集），无效返回 None。

    支持：整数（可负、可补零、可步进）与 ASCII 单字符；显式步进只表达
    步长，方向由首尾决定；0 步进或解析失败视为无效（调用方保持字面，
    对齐 bash 对无效范围的处理）。生成项数受 MAX_BRACE_EXPANSIONS 截断
    （与逗号展开共用上限）。此前仅检测 ".." 需要展开、却按逗号切分原样
    保留（花括号被剥离为字面），r{1..3}.txt 静默无匹配。
    """
    parts = body.split("..")
    if len(parts) not in (2, 3):
        return None
    step = None
    if len(parts) == 3:
        try:
            step = int(parts[2])
        except ValueError:
            return None
        if step == 0:
            return None
    a, b = parts[0], parts[1]
    limit = GlobLimits.MAX_BRACE_EXPANSIONS

    def _int_like(s: str) -> bool:
        t = s[1:] if s[:1] in ("-", "+") else s
        return bool(t) and t.isdigit()

    if _int_like(a) and _int_like(b):
        start, end = int(a), int(b)
        pad = 0
        for s in (a, b):
            t = s.lstrip("+-")
            if len(t) > 1 and t.startswith("0"):
                pad = max(pad, len(t))
        direction = 1 if start <= end else -1
        step_val = abs(step) * direction if step is not None else direction
        count = min(abs(end - start) // abs(step_val) + 1, limit)
        return [
            str(start + i * step_val).zfill(pad)
            if pad and start + i * step_val >= 0
            else str(start + i * step_val)
            for i in range(count)
        ]

    def _is_ascii_char(s: str) -> bool:
        return len(s) == 1 and s.isalpha() and ord(s) < 128

    if _is_ascii_char(a) and _is_ascii_char(b):
        start, end = ord(a), ord(b)
        direction = 1 if start <= end else -1
        step_val = abs(step) * direction if step is not None else direction
        count = min(abs(end - start) // abs(step_val) + 1, limit)
        return [chr(start + i * step_val) for i in range(count)]

    return None


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

                # 检查 body 中是否有顶层逗号 / ..（需要展开）
                has_comma = False
                has_dots = False
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
                        has_comma = True
                    elif ch == "." and b_depth == 0:
                        # 检测 .. （范围序列，如 {1..5}）
                        if j + 1 < len(body) and body[j + 1] == ".":
                            has_dots = True
                            j += 1
                    j += 1

                if not has_comma and not has_dots:
                    # 无逗号/..，不展开，保留字面花括号
                    return [pattern]

                if has_comma:
                    # 按顶层逗号切分（bash 语义：逗号优先于范围）
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
                else:
                    # 仅 .. → 范围展开；无效范围（如 {1..x}）保持字面（bash 语义）
                    options = _expand_range(body)
                    if options is None:
                        return [pattern]

                results: list[str] = []
                for opt in options:
                    for exp in _expand_braces(head + opt + tail):
                        if len(results) >= GlobLimits.MAX_BRACE_EXPANSIONS:
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

    结尾锚定 \\Z（fd 语义：起点不锚定、结尾锚定）：
    - "*.c" 可命中任意层的 x.c（配合 search），但不会误匹配 x.c.bak
    - "**/xxx"、"src/**/*.c" 行为不变（本就是结尾对齐）
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

    # Windows 文件系统不区分大小写，pattern 大小写差异不应导致静默无匹配
    # （AI 从历史输出/记忆里复述路径时大小写常不一致）。Linux 保持 fd smart case。
    if os.name == "nt":
        flags = re.IGNORECASE
    else:
        flags = 0 if _pattern_has_uppercase(pattern) else re.IGNORECASE

    # 点开头+通配（.*、.?、.[abc] 等）→ 起点锚定：
    # search 语义下 \.[^/]* 会命中 main.py 的扩展名点，导致 .* 返回全集。
    # AI 传 .* 的意图是"点开头的隐藏项"（fd 语义），加 ^ 锚定起点。
    anchor_start = len(p) >= 2 and p[0] == "." and p[1] in "*?["
    prefix = "^" if anchor_start else ""
    # 结尾锚定：fd 语义（起点不锚定、结尾锚定），配合 search 使用
    return re.compile(prefix + "".join(parts) + "\\Z", flags)


# ── 目录遍历 & 匹配 ────────────────────────────────────────────

class _CancelGate:
    """取消检查节流门：首个检查点立即查询，其后每 CHECK_INTERVAL 次调用真查一次。

    长耗时遍历（Grep/Glob）在目录/文件/chunk 循环里按调用点粒度检查取消；
    节流避免每步都付一次回调开销。cancel_check 为 None/不可调用时
    check() 恒为 False —— 工具行为与无取消完全一致。

    并行扫描下多个线程共享计数（自增非原子，最坏只是真查时机略偏），
    仅作节流用，不影响取消判定的收敛性。
    """

    CHECK_INTERVAL = 32

    def __init__(self, cancel_check=None):
        self._fn = cancel_check if callable(cancel_check) else None
        self._calls = 0
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> bool:
        if self._cancelled or self._fn is None:
            return self._cancelled
        self._calls += 1
        if self._calls == 1 or self._calls % self.CHECK_INTERVAL == 0:
            self._cancelled = bool(self._fn())
        return self._cancelled


def _is_nt_reparse(entry) -> bool:
    """目录项是否 Windows 重解析点（junction 等）。

    DirEntry.is_symlink() 对 junction 返回 False，防环仅靠它无效，
    需查 st_reparse_tag 才能识别。非 Windows 无此属性，getattr 兜底 0。
    """
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0))


def _collect(
    root: str,
    regexes: list[re.Pattern[str]],
    ignore_dirs: set[str],
    max_results: int,
    skip_hidden_files: bool,
    gate: "_CancelGate | None" = None,
) -> tuple[list[tuple[str, float]], int]:
    """scandir 遍历目录树，返回 (按 mtime 降序的结果, 总匹配数)。

    gate 命中取消时立即停止遍历（已收集部分不回滚，由调用方按取消语义处理）。
    """
    root = os.path.abspath(root)
    heap: list[tuple[float, str]] = []  # (mtime, rel_path) — 堆中统一用 / 分隔
    total = 0
    stack: list[tuple[str, str]] = [(root, "")]
    single_regex = regexes[0] if len(regexes) == 1 else None
    _is_nt = (os.name == "nt")  # 缓存，避免循环内重复判断

    if gate is not None and gate.check():
        return [], 0

    while stack:
        cur_dir, rel_prefix = stack.pop()

        if gate is not None and gate.check():
            break

        try:
            with os.scandir(cur_dir) as entries:
                subdirs: list[tuple[str, str]] = []
                for entry in entries:
                    if gate is not None and gate.check():
                        break

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

                    # ── junction/重解析点目录：跳过（与符号链接目录同语义，防自反环无限递归）。
                    #    仅目录形态需拦（文件形态的链接/占位文件照常处理） ──
                    if is_dir and _is_nt_reparse(entry):
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
                            dir_matched = single_regex.search(dir_rel_norm) is not None
                        else:
                            dir_matched = any(rx.search(dir_rel_norm) for rx in regexes)

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
                        matched = single_regex.search(rel_match) is not None
                    else:
                        for rx in regexes:
                            if rx.search(rel_match):
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


def _pattern_has_dot_component(pattern: str) -> bool:
    """pattern 任意路径组件以 . 开头（如 .narnat/**、**/.narnat/*）→ 不过滤隐藏文件。

    对齐 fd 语义：pattern 中显式出现隐藏组件时启用隐藏文件搜索。
    仅剥离前导 ./ 与 /（与 _compile_pattern 一致），不剥离组件内部的 .。
    """
    p = pattern.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return any(comp.startswith(".") for comp in p.split("/") if comp)


def _hidden_files_hint(skip_hidden_files: bool) -> str:
    """无匹配时的隐藏文件提示：把默认跳过规则变成可见路标，AI 无需背规则。"""
    if not skip_hidden_files:
        return ""
    return "（注: 隐藏文件默认跳过；pattern中含以.开头的路径组件可匹配隐藏文件）"


def _ignored_dirs_hint(ignore_dirs) -> str:
    """无匹配时的忽略目录提示：把静默收缩的搜索范围变成可见路标。

    忽略目录来自配置（Glob/Grep 默认跳过），无匹配时 AI 无从知道范围被收缩，
    会误判"文件不存在/写入没生效"；此处对齐隐藏文件提示，附上清单与出路。
    排序：非默认（用户自定义）条目优先，默认项补充在后；否则按字母序取前 5
    时用户的 output 等自定义目录会被裁掉，AI 需多花试错回合。
    """
    if not ignore_dirs:
        return ""
    from ...config.defaults import DEFAULT_IGNORE_DIRS
    defaults = set(DEFAULT_IGNORE_DIRS)
    custom = sorted(n for n in ignore_dirs if n not in defaults)
    builtin = sorted(n for n in ignore_dirs if n in defaults)
    names = custom + builtin
    head = "，".join(names[:5]) + ("等" if len(names) > 5 else "")
    return f"（注: 已跳过忽略目录 {head}；如需搜索请显式传路径）"


# ── 静态前缀拆分 ─────────────────────────────────────────────

def _split_static_prefix(pattern: str) -> tuple:
    """pattern 拆分: (静态目录, 剩余pattern)。相对/绝对 pattern 统一适用。

    - 无通配符 → (pattern, "")，调用方做存在性检查
    - 有通配符 → 首个通配符之前最后一个路径分隔符处切分
    - 无法切分 → ("", pattern)，调用方按普通pattern处理
    """
    wild_pos = -1
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            wild_pos = i
            break
    if wild_pos < 0:
        return pattern, ""
    sep = max(pattern.rfind("/", 0, wild_pos), pattern.rfind("\\", 0, wild_pos))
    if sep < 0:
        return "", pattern
    static_dir = pattern[:sep]
    # 盘符单独作为前缀（如 D:\\*.py）时补分隔符：D: 是"盘符相对路径"而非盘根
    if os.name == "nt" and len(static_dir) == 2 and static_dir[1] == ":":
        static_dir += "\\"
    return static_dir, pattern[sep + 1:]


def _pattern_syntax_error(pattern: str) -> str:
    """探测 glob pattern 的语法错误，返回错误描述（合法时为 ""）。

    当前仅检测"未闭合的字符类"（如 "["、"a[bc"）：该形态此前被 _compile_pattern
    静默降级为字面匹配，最终返回 [无匹配]，误导 AI 判定"文件不存在/内容没写入"。
    转义 "\\[" 视为字面字符不参与配对；紧邻 "[" 的 "]" 可为字面量
    （与 _compile_pattern 的解析一致）。
    """
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            end = pattern.find("]", j)
            if end == -1:
                return f'未闭合的字符类 "{pattern[i:]}"'
            i = end + 1
            continue
        i += 1
    return ""


# ── 公共接口 ────────────────────────────────────────────────────

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": (
            "按模式匹配文件和目录。返回匹配路径，按修改时间倒序。"
            "（仅支持本机文件，不支持远程设备文件）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        '匹配路径任意位置，如 "*.py" 递归匹配所有层的.py文件，'
                        '"dir/*.ext" 递归匹配 dir 下所有层的.ext（含子目录）；'
                        '例："**/*.h"、"src/**/*.cpp"、"*.{docx,pdf}"。'
                        '支持绝对路径（如 win:"D:\\work\\**\\*.py"、'
                        'linux/macOS:"/home/user/**/*.py"），绝对与相对写法语义一致。'
                        '默认跳过隐藏文件；含以.开头的路径组件时匹配隐藏文件（".*"匹配点开头的项）'
                    ),
                },
                "path": {"type": "string", "description": "搜索目录（默认当前目录）"},
                "max_results": {"type": "integer", "description": "最大结果数（正整数，默认50）"},
            },
            "required": ["pattern"],
        },
    },
}


CAPABILITY = {
    "label": "搜索文件",
    "dispatch": "readonly",
    "summary": "pattern_path",
    "trusted_output": True,
}


def execute(pattern: str, path: str = "", max_results: int = 50, _tool_context=None) -> str:
    # ── 空 pattern 拒绝：返回全集会淹没 AI（大型项目海量结果无意义）──
    if pattern is None or not str(pattern).strip():
        return "[错误: pattern不能为空]"
    pattern = str(pattern).strip()

    # path 类型防御：非字符串（float/list 等）会让 os.path.isdir 抛 TypeError
    # 逃出工具层（registry 兜底不应是常规路径）
    if path is not None and not isinstance(path, str):
        return "[错误: path需为字符串]"

    # 含 NUL（JSON \u0000）：win32 stat/open 抛未捕获 ValueError 逃出工具层
    if "\x00" in pattern or "\x00" in (path or ""):
        return "[错误: pattern/path 含 NUL 字符（\\0），非法路径]"

    root = path or os.getcwd()
    if not os.path.isdir(root):
        # 相对路径解析依赖当前目录（Shell cd会改变它），报错时带上cwd帮AI一次定位
        return f"[错误: 目录不存在: {root}（当前目录: {os.getcwd()}）]"

    # AI可能传字符串类型的数值参数，统一转int（与Grep容错风格一致）
    # OverflowError：JSON 里的 1e999 → float('inf')，int(inf) 抛 OverflowError
    try:
        max_results = int(max_results) if max_results is not None else 50
    except (TypeError, ValueError, OverflowError):
        return "[错误: max_results需为正整数]"

    if max_results <= 0:
        return "[错误: max_results需为正整数]"

    # 硬上限防止 OOM（对齐 fd 设计）
    max_results = min(max_results, GlobLimits.MAX_HARD_LIMIT)

    # 忽略目录统一来自 narnat.json 配置（经 ToolContext 注入），工具内不再写死
    # str() 兜底：绕过 loader 直接构造 ToolContext 时元素可能是数字/对象，
    # set() 对不可哈希元素抛 TypeError（配置侧已在 loader 过滤，此处仅防御）
    extra = getattr(_tool_context, "ignore_dirs", None) if _tool_context is not None else None
    ignore_dirs = {str(d) for d in extra} if extra else set()

    # 取消门（用户中断时遍历/扫描提前结束；cancel_check 缺省为 None，行为不变）
    gate = _CancelGate(getattr(_tool_context, "cancel_check", None)
                       if _tool_context is not None else None)
    if gate.check():
        return _CANCELLED_TEXT

    # 1. 展开花括号 + 去转义
    raw_patterns = _expand_braces(pattern)
    patterns = [_unescape_braces(p) for p in raw_patterns]

    # 1b. 非法 pattern 明确报错（如未闭合的字符类 "["）：
    #     静默降级为字面匹配会返回 [无匹配]，误导 AI 判定"文件不存在"
    #     （对齐 Grep 的"非法正则"处理风格）
    for p in patterns:
        syntax_err = _pattern_syntax_error(p)
        if syntax_err:
            return f"[错误: 非法 pattern: {syntax_err}]"

    # 2. 隐藏文件过滤（对齐 fd：pattern 任意路径组件以 . 开头则不过滤隐藏文件）
    #    注: 不能用 p.lstrip("./")——会把 ".narnat" 开头的 . 误剥离导致判断失效
    skip_hidden_files = not any(_pattern_has_dot_component(p) for p in patterns)

    # 3. 统一匹配（相对/绝对 pattern 语义一致，对齐 fd：起点自由、结尾锚定）
    #    有静态前缀（如 src/*.c）→ 拆出静态目录，在其内用剩余 pattern 匹配：
    #    src 下所有层的 .c 都能命中（AI 历史期望：LinksFPGA/*.{c,h} 想要 src 下文件）
    #    无静态前缀（如 *.py、**/*.c）→ 在 root 内匹配
    merged: dict[str, float] = {}   # 归一化路径(/分隔) → mtime
    total = 0
    missing_dirs: list[str] = []
    root_patterns: list[str] = []

    for p in patterns:
        static_dir, rest = _split_static_prefix(p)
        if not rest:
            # 无通配的路径：存在性检查（相对 root 解析），不必遍历全树
            target = p if os.path.isabs(p) else os.path.join(root, p)
            target = os.path.normpath(target)
            try:
                st = os.stat(target)
            except OSError:
                continue
            if os.path.isdir(target) or os.path.isfile(target):
                label = p if os.path.isabs(p) else os.path.relpath(target, root)
                merged.setdefault(label.replace("\\", "/"), st.st_mtime)
                total += 1
            continue
        if not static_dir:
            # 无静态前缀 → 在 root 内匹配（合并为一次遍历）
            root_patterns.append(p)
            continue
        # 静态目录解析：相对目录一律相对 root（path 参数）解析，绝对目录直接使用。
        # 修复：此前相对目录先按 cwd 判断存在性，cwd 中存在同名目录（如 .git、..）
        # 时会无视 path 参数去搜 cwd 的内容（搜索范围逃逸）。
        if os.path.isabs(static_dir):
            if not os.path.isdir(static_dir):
                missing_dirs.append(static_dir)
                continue
            search_dir = static_dir
        else:
            joined = os.path.join(root, static_dir)
            if not os.path.isdir(joined):
                # 静态目录不存在 → 记录，最后报错帮 AI 一次定位（优于静默无匹配）
                missing_dirs.append(static_dir)
                continue
            search_dir = joined
        res, t = _collect(search_dir, [_compile_pattern(rest)], ignore_dirs,
                          max_results, skip_hidden_files, gate)
        # 输出前缀保持 AI 写法（相对 path 的用相对、绝对用绝对；
        # Windows 下大小写跟随 pattern 字面量，文件系统不区分大小写，路径仍有效）
        prefix = static_dir.replace("\\", "/").rstrip("/") + "/"
        for rel, mtime in res:
            merged.setdefault(prefix + rel.replace("\\", "/"), mtime)
        total += t

    if root_patterns:
        res, t = _collect(root, [_compile_pattern(p) for p in root_patterns],
                          ignore_dirs, max_results, skip_hidden_files, gate)
        for rel, mtime in res:
            merged.setdefault(rel.replace("\\", "/"), mtime)
        total += t

    # 取消优先于结果输出：中断后不再拼装/截断提示（结果不会被消费）
    if gate.cancelled:
        return _CANCELLED_TEXT

    if not merged:
        hint = _hidden_files_hint(skip_hidden_files)
        if missing_dirs:
            return f"[错误: 目录不存在: {missing_dirs[0]}（当前目录: {os.getcwd()}）]"
        # 带上实际搜索目录，帮 AI 一次定位（Shell cd 会改变当前目录，无匹配常因目录不对）
        # 附隐藏文件/忽略目录提示：让静默的坑变成可见路标，AI 无需背规则
        return f"[无匹配（搜索目录: {root}）]{hint}{_ignored_dirs_hint(ignore_dirs)}"

    results = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = [
        p.replace("/", os.sep) if os.sep != "/" else p
        for p, _ in results[:max_results]
    ]
    output = "\n".join(shown)

    if total > max_results:
        output += (
            f"\n...[已截断: 共{total}个匹配项, "
            f"当前显示按修改时间最近的{max_results}个。增大max_results可获取完整列表]"
        )

    return output
