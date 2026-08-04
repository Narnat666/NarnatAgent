"""Grep工具 —— 按内容搜索代码，定位关键行"""

import fnmatch
import io
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

_DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"}

# ── 滚动缓冲区大小（64KB，与 ripgrep 的 DEFAULT_BUFFER_CAPACITY 一致）──
_BUFFER_SIZE = 64 * 1024

# ── ReDoS 防护：正则最大长度 ──
_MAX_PATTERN_LENGTH = 4096

# ── 单文件最大大小（100MB），超出跳过 ──
_MAX_FILE_SIZE = 100 * 1024 * 1024

# ── 超长行上限（1MB），leftover 超过此值视为二进制跳过 ──
_MAX_LINE_LENGTH = 1 * 1024 * 1024

# ── 正则元字符集，用于判断 pattern 是否为纯文本 ──
_RE_META_CHARS = set(r".*+?[]{}()\|^$")

# ── 二进制检测：首块中 NUL 字节阈值 ──
_BINARY_NUL_THRESHOLD = 1

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": "正则搜索文件内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索路径（默认当前）",
                },
                "glob": {
                    "type": "string",
                    "description": "文件过滤，如*.py、src/*.c、**/*.c（默认空）",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "输出格式（默认files_with_matches）",
                },
                "i": {
                    "type": "boolean",
                    "description": "是否忽略大小写（默认否）",
                },
                "n": {
                    "type": "boolean",
                    "description": "是否显示行号（content模式专用，默认否）",
                },
                "A": {
                    "type": "integer",
                    "description": "额外带后续N行（默认0）",
                },
                "B": {
                    "type": "integer",
                    "description": "额外带前面N行（默认0）",
                },
                "C": {
                    "type": "integer",
                    "description": "额外带前后各N行（默认0）",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "最大返回结果数（正整数，默认100）；files_with_matches/count按文件计数，content按行计数",
                },
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
    A: int = 0,
    B: int = 0,
    C: int = 0,
    head_limit: int = 100,
    _tool_context=None,
) -> str:
    # ── ReDoS 防护 ──
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return f"[错误: 正则表达式过长（>{_MAX_PATTERN_LENGTH}字符），拒绝执行以防ReDoS]"

    # ── output_mode 运行时校验 ──
    if output_mode not in ("files_with_matches", "content", "count"):
        return f"[错误: 无效的 output_mode: {output_mode}]"

    flags = 0
    if i:
        flags |= re.IGNORECASE

    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[错误: 非法正则: {e}]"

    if C > 0:
        A = C
        B = C

    if head_limit is not None and head_limit <= 0:
        return "[错误: head_limit需为正整数]"

    root = path or os.getcwd()
    if os.path.isfile(root):
        return _search_single_file(root, regex, output_mode, n, A, B, head_limit)
    if not os.path.isdir(root):
        return f"[错误: 路径不存在: {root}]"

    # ignore_dirs 合并而非覆盖
    ignore_dirs = _DEFAULT_IGNORE_DIRS.copy()
    if _tool_context and _tool_context.ignore_dirs:
        ignore_dirs |= set(_tool_context.ignore_dirs)

    file_matches = _search_files(root, regex, glob, output_mode, n, A, B, head_limit, ignore_dirs)
    return _format_results(file_matches, output_mode, head_limit)


def _format_results(file_matches, output_mode, head_limit):
    """格式化输出结果"""
    results = []
    truncated = False

    if output_mode == "files_with_matches":
        for rel_path in file_matches:
            results.append(rel_path)
            if head_limit is not None and len(results) >= head_limit:
                truncated = True
                break
    elif output_mode == "count":
        for rel_path, count in file_matches:
            results.append(f"{rel_path}:{count}")
            if head_limit is not None and len(results) >= head_limit:
                truncated = True
                break
    else:
        # content 模式：streaming 引擎保证匹配行数 ≤ head_limit
        return "\n".join(file_matches) if file_matches else "[无匹配]"

    output = "\n".join(results) if results else "[无匹配]"
    if truncated:
        output += f"\n...[已截断: 超出head_limit({head_limit})限制]"
    return output


# ═══════════════════════════════════════════════════════════════
# 快慢双路径 — 纯文本快速预筛选
# ═══════════════════════════════════════════════════════════════

def _has_re_meta(pattern: str) -> bool:
    """判断 pattern 是否包含正则元字符。

    任何反斜杠都视为正则语法（\\( 、\\)、\\d、\\n 等均非纯文本）。
    """
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\':
            return True   # 任何转义 = 正则语法，不走纯文本快路径
        if ch in _RE_META_CHARS:
            return True
        i += 1
    return False


def _make_fast_searcher(pattern: str, ignore_case: bool):
    """
    如果 pattern 是纯文本，返回快速搜索函数 (line: str) -> bool。
    - 大小写敏感：用 str.find（最快）
    - 大小写不敏感：用 re.compile(re.escape(...), IGNORECASE) 保证语义等价于正则路径
    含正则元字符时返回 None。
    """
    if _has_re_meta(pattern):
        return None
    if ignore_case:
        compiled = re.compile(re.escape(pattern), re.IGNORECASE)
        return lambda line: bool(compiled.search(line))
    else:
        return lambda line: pattern in line


# ═══════════════════════════════════════════════════════════════
# 二进制检测（内联在 streaming 中，避免双重 I/O）
# ═══════════════════════════════════════════════════════════════

def _check_binary_first_chunk(first_chunk: bytes) -> bool:
    """检查首块中是否含 NUL 字节。"""
    return first_chunk.count(0) >= _BINARY_NUL_THRESHOLD


# ═══════════════════════════════════════════════════════════════
# 滚动缓冲流式搜索（核心引擎）
# ═══════════════════════════════════════════════════════════════

def _search_file_streaming(
    file_path: str,
    path_label: str,
    regex,
    fast_searcher,
    output_mode: str,
    show_n: bool,
    A: int,
    B: int,
    head_limit,
    counter: dict,
    lock,
):
    """
    使用 64KB 滚动缓冲区搜索单个文件，内存 O(1)。

    二进制检测、文件大小检查均整合在此函数中，避免双重 I/O。

    参数:
        file_path:   文件绝对路径
        path_label:  输出用的路径标签
        regex:       编译后的正则对象
        fast_searcher: 纯文本快速搜索函数，或 None
        output_mode: "files_with_matches" | "count" | "content"
        show_n:      是否显示行号
        A:           after_context 行数
        B:           before_context 行数
        head_limit:  最大结果数（None=无限制，0=无结果）
        counter:     共享计数器 {"count": int}
        lock:        线程锁（并行模式），单线程时为 None

    返回:
        (results, match_count)
    """
    # ── 入口截断检查 ──
    if head_limit is not None:
        if lock:
            with lock:
                if counter["count"] >= head_limit:
                    return [], 0
        elif counter["count"] >= head_limit:
            return [], 0

    # ── 单次 I/O：rb 打开，检查二进制，文件大小 ──
    try:
        raw_f = open(file_path, "rb")
    except (PermissionError, OSError):
        return [], 0

    try:
        # 文件大小检查
        raw_f.seek(0, 2)  # SEEK_END
        file_size = raw_f.tell()
        if file_size > _MAX_FILE_SIZE:
            return [], 0
        raw_f.seek(0)

        # 二进制检测（首块）
        first_chunk = raw_f.read(_BUFFER_SIZE)
        if _check_binary_first_chunk(first_chunk):
            return [], 0

        # 包装为文本流（从已读取的首块继续）
        f = io.TextIOWrapper(
            raw_f,
            encoding="utf-8-sig",
            errors="replace",
            newline="",          # 不自动转换行尾，由我们手动处理 CRLF
        )
        # TextIOWrapper 的缓冲区需要手动喂入首块
        # 简化方案：直接用 raw_f 的剩余数据，首块用文本方式重新解析
        # 最稳妥的做法：关闭，重新以文本打开（首块开销可接受）
        f.close()
        raw_f = None
    except (PermissionError, OSError):
        if raw_f:
            raw_f.close()
        return [], 0

    # 重新以文本模式打开（确保 TextIOWrapper 状态干净）
    # 二进制首块已判定非二进制，第二次打开 I/O 可接受
    try:
        f = open(file_path, "r", encoding="utf-8-sig", errors="replace", newline="")
    except (PermissionError, OSError):
        return [], 0

    results = []
    count = 0
    leftover = ""                       # 跨缓冲区边界的不完整行
    line_num = 0
    before_window = deque()             # (line_text, line_num)，用于 before_context
    pending_after = 0                   # 还需输出的 after_context 行数
    last_output_line = 0                # 最后输出行号（用于 -- 分组分隔符）

    try:
        while True:
            chunk = f.read(_BUFFER_SIZE)
            if not chunk:
                break

            # ── CRLF 归一化（防止 \r\n 在缓冲区边界分裂）──
            chunk = chunk.replace("\r\n", "\n")

            data = leftover + chunk
            lines = data.split("\n")
            leftover = lines.pop()

            # ── 超长行防护 ──
            if len(leftover) > _MAX_LINE_LENGTH:
                return results, count

            for line in lines:
                # 剥离行尾残留 \r（缓冲区恰好在 \r|\n 分裂时）
                line = line.rstrip("\r")
                line_num += 1

                # ── 快路径（纯文本）或慢路径（正则）──
                if fast_searcher:
                    matched = fast_searcher(line)
                else:
                    matched = bool(regex.search(line))

                # ── files_with_matches: 找到即返回 ──
                if output_mode == "files_with_matches":
                    if matched:
                        return [path_label], 1

                # ── count: 仅计数 ──
                elif output_mode == "count":
                    if matched:
                        count += 1

                # ── content: 完整输出（含上下文）──
                else:
                    if matched:
                        # ── head_limit 先检查再追加（防竞态）──
                        if head_limit is not None:
                            if lock:
                                with lock:
                                    if counter["count"] >= head_limit:
                                        return results, count
                                    counter["count"] += 1
                            else:
                                if counter["count"] >= head_limit:
                                    return results, count
                                counter["count"] += 1

                        # ── 组分隔符 ──
                        if last_output_line > 0:
                            first_line = (
                                before_window[0][1]
                                if before_window
                                else line_num
                            )
                            if last_output_line + 1 < first_line:
                                results.append("--")

                        # 输出 before_context
                        for bline, bline_num in before_window:
                            if show_n:
                                results.append(
                                    f"{path_label}-{bline_num}-{bline}"
                                )
                            else:
                                results.append(f"{path_label}-{bline}")
                            last_output_line = bline_num
                        before_window.clear()

                        # 输出匹配行
                        if show_n:
                            results.append(
                                f"{path_label}:{line_num}:{line}"
                            )
                        else:
                            results.append(f"{path_label}:{line}")

                        count += 1
                        last_output_line = line_num
                        pending_after = A

                    elif pending_after > 0:
                        # after_context 行
                        if show_n:
                            results.append(
                                f"{path_label}-{line_num}-{line}"
                            )
                        else:
                            results.append(f"{path_label}-{line}")
                        last_output_line = line_num
                        pending_after -= 1
                    else:
                        # 维护 before_context 滑动窗口
                        if B > 0:
                            before_window.append((line, line_num))
                            if len(before_window) > B:
                                before_window.popleft()

        # ── 处理文件末尾的不完整行 ──
        if leftover and len(leftover) < _MAX_LINE_LENGTH:
            leftover = leftover.rstrip("\r")
            line_num += 1
            if fast_searcher:
                matched = fast_searcher(leftover)
            else:
                matched = bool(regex.search(leftover))
            if matched:
                if output_mode == "files_with_matches":
                    return [path_label], 1
                elif output_mode == "count":
                    count += 1
                else:  # content
                    if head_limit is not None:
                        if lock:
                            with lock:
                                if counter["count"] >= head_limit:
                                    return results, count
                                counter["count"] += 1
                        else:
                            if counter["count"] >= head_limit:
                                return results, count
                            counter["count"] += 1

                    if last_output_line > 0:
                        first_line = (
                            before_window[0][1]
                            if before_window
                            else line_num
                        )
                        if last_output_line + 1 < first_line:
                            results.append("--")

                    for bline, bline_num in before_window:
                        if show_n:
                            results.append(
                                f"{path_label}-{bline_num}-{bline}"
                            )
                        else:
                            results.append(f"{path_label}-{bline}")
                    if show_n:
                        results.append(
                            f"{path_label}:{line_num}:{leftover}"
                        )
                    else:
                        results.append(f"{path_label}:{leftover}")
                    count += 1

    finally:
        f.close()

    return results, count


# ═══════════════════════════════════════════════════════════════
# 单文件搜索（委托给流式引擎）
# ═══════════════════════════════════════════════════════════════

def _search_single_file(file_path, regex, output_mode, show_n, A, B, head_limit):
    """在单个文件内执行搜索（流式读取，内存 O(1)）。"""
    fast_searcher = _make_fast_searcher(
        regex.pattern, bool(regex.flags & re.IGNORECASE)
    )
    counter = {"count": 0}
    results, count = _search_file_streaming(
        file_path, file_path, regex, fast_searcher,
        output_mode, show_n, A, B, head_limit,
        counter, None,
    )

    if output_mode == "files_with_matches":
        return results[0] if results else "[无匹配]"
    elif output_mode == "count":
        return f"{file_path}:{count}" if count else "[无匹配]"
    else:
        return "\n".join(results) if results else "[无匹配]"


# ═══════════════════════════════════════════════════════════════
# 并行文件遍历 + 流式搜索
# ═══════════════════════════════════════════════════════════════

def _match_glob(fname: str, rel: str, glob_filter: str) -> bool:
    """glob 过滤：支持纯文件名或相对路径（含通配符），与 Glob 工具语义对齐。

    - 匹配对象为相对路径或纯文件名，二者任一命中即通过；
    - Windows 下正反斜杠等价（glob 与 rel 均归一化为 /）；
    - **/ 前缀可匹配零层目录（即根目录下的文件）。
    """
    if os.name == "nt":
        glob_filter = glob_filter.replace("\\", "/")
        rel = rel.replace("\\", "/")
    patterns = [glob_filter]
    if glob_filter.startswith("**/"):
        patterns.append(glob_filter[3:])  # **/ 可匹配零层目录
    return any(
        fnmatch.fnmatchcase(fname, p) or fnmatch.fnmatchcase(rel, p)
        for p in patterns
    )


def _search_files(root, regex, glob_filter, output_mode, show_n, A, B, head_limit, ignore_dirs):
    """遍历文件执行搜索（并行 + 流式读取）。"""
    # ── 收集 + 排序（保证遍历顺序确定）──
    file_list = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            if glob_filter and not _match_glob(fname, rel, glob_filter):
                continue
            file_list.append((full, rel))

    if not file_list:
        return []

    # 文件名排序，保证遍历顺序确定
    file_list.sort(key=lambda t: t[1])

    # ── 预构建快速搜索函数 ──
    fast_searcher = _make_fast_searcher(
        regex.pattern, bool(regex.flags & re.IGNORECASE)
    )

    # ── 文件数 >= 10 时启用并行 ──
    worker_count = min(os.cpu_count() or 4, 12)
    use_parallel = len(file_list) >= 10

    results = []
    counter = {"count": 0}
    lock = Lock() if use_parallel else None

    if use_parallel:
        file_index = {rel_path: i for i, (_, rel_path) in enumerate(file_list)}

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for full_path, rel_path in file_list:
                if head_limit is not None and counter["count"] >= head_limit:
                    break
                fut = executor.submit(
                    _search_file_streaming,
                    full_path, rel_path, regex, fast_searcher,
                    output_mode, show_n, A, B, head_limit,
                    counter, lock,
                )
                futures[fut] = rel_path

            if output_mode == "content":
                # 收集 (file_index, file_results) → 排序 → flatten
                per_file = []
                for fut in as_completed(futures):
                    try:
                        file_results, _match_count = fut.result()
                    except Exception:
                        continue
                    if file_results:
                        rel_path = futures[fut]
                        idx = file_index.get(rel_path, 10 ** 9)
                        per_file.append((idx, file_results))
                per_file.sort(key=lambda t: t[0])
                for _idx, file_results in per_file:
                    results.extend(file_results)
            else:
                for fut in as_completed(futures):
                    rel_path = futures[fut]
                    try:
                        file_results, match_count = fut.result()
                    except Exception:
                        continue

                    if output_mode == "files_with_matches":
                        if file_results:
                            results.append(rel_path)
                            if head_limit is not None:
                                counter["count"] += 1
                                if counter["count"] >= head_limit:
                                    for f in futures:
                                        f.cancel()
                                    break
                    elif output_mode == "count":
                        if match_count:
                            results.append((rel_path, match_count))
                            if head_limit is not None:
                                counter["count"] += 1
                                if counter["count"] >= head_limit:
                                    for f in futures:
                                        f.cancel()
                                    break

            # 非 content 模式也需要排序
            if output_mode == "files_with_matches":
                results.sort(key=lambda p: file_index.get(p, 10 ** 9))
            elif output_mode == "count":
                results.sort(key=lambda t: file_index.get(t[0], 10 ** 9))
    else:
        for full_path, rel_path in file_list:
            if head_limit is not None and counter["count"] >= head_limit:
                break
            file_results, match_count = _search_file_streaming(
                full_path, rel_path, regex, fast_searcher,
                output_mode, show_n, A, B, head_limit,
                counter, None,
            )
            if output_mode == "files_with_matches":
                if file_results:
                    results.append(rel_path)
                    if head_limit is not None:
                        counter["count"] += 1
            elif output_mode == "count":
                if match_count:
                    results.append((rel_path, match_count))
                    if head_limit is not None:
                        counter["count"] += 1
            else:
                results.extend(file_results)

    return results
