"""Grep工具 —— 按内容搜索代码，定位关键行

新版设计（以AI为本，零调度负担）：
- 无 output_mode/n 参数：默认返回「文件表头(含计数) + 带行号的匹配行」，
  与 ripgrep 的 heading 输出形态一致（AI 训练数据中最熟悉的格式）。
- head_limit 默认 30（AI 显式传值的历史众数）。
- 预算按文件边界生效：当前展开的文件完整输出，之后文件降级为清单
  「文件名 (N处)」，AI 既能拿重点详情也能拿全局地图，无孤儿行。
- path 支持数组：文件/目录可混填，按给定顺序输出，部分缺失仅警告。
"""

import fnmatch
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..param_utils import to_bool
from ..glob import _expand_braces, _unescape_braces

# ── 滚动缓冲区大小（64KB，与 ripgrep 的 DEFAULT_BUFFER_CAPACITY 一致）──
_BUFFER_SIZE = 64 * 1024

# ── ReDoS 防护：正则最大长度 ──
_MAX_PATTERN_LENGTH = 4096

# ── 单文件最大大小（100MB），超出跳过 ──
_MAX_FILE_SIZE = 100 * 1024 * 1024

# ── 超长行上限（1MB），leftover 超过此值视为异常文件提前结束 ──
_MAX_LINE_LENGTH = 1 * 1024 * 1024

# ── 正则元字符集，用于判断 pattern 是否为纯文本 ──
_RE_META_CHARS = set(r".*+?[]{}()\|^$")

# ── 二进制检测：首块中 NUL 字节阈值 ──
_BINARY_NUL_THRESHOLD = 1

# ── head_limit 默认值：AI 显式传值的历史众数（514次中132次传30）──
_DEFAULT_HEAD_LIMIT = 30

# ── 并行阈值：目录内文件数 >= 此值时启用线程池 ──
_PARALLEL_MIN_FILES = 10

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": (
            "正则搜索文件内容（仅支持本机文件，不支持远程设备文件）。"
            "path 支持目录、单个文件，或多个路径的数组（文件/目录可混合，按给定顺序输出）。"
            "glob 支持花括号多模式（与Glob工具语法一致），如 *.{c,h}、src/**/*.{py,md}。"
            "默认返回每个命中文件的分组结果：文件表头（含匹配计数）+ 带行号的匹配行；"
            "匹配行累计达到head_limit后，剩余文件仅列文件表头（含计数）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式",
                },
                "path": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "搜索路径（默认当前目录）；可填目录、单个文件，或多个路径的数组，如 [\"src/a.c\", \"include\"]",
                },
                "glob": {
                    "type": "string",
                    "description": "文件过滤，如*.py、src/*.c、**/*.c、*.{c,h}（花括号多模式，默认空）",
                },
                "i": {
                    "type": "boolean",
                    "description": "是否忽略大小写（默认否）",
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
                    "description": "最大返回匹配行数（正整数，默认30）；达到后剩余文件仅列文件名（含计数），增大可展开更多匹配行",
                },
            },
            "required": ["pattern"],
        },
    },
}


def _detect_text_encoding(head: bytes) -> str:
    """utf-8 严格解码成功 → utf-8-sig；失败 → gbk（策略与Read一致）。

    此前固定utf-8+replace解码流式读取：GBK中文文件每个字节都被替换为U+FFFD，
    任何中文pattern都无法命中，AI会误以为内容不存在。首块判定编码后按正确
    编码打开。尾部3字节窗口重试防止多字节序列边界截断误判。
    """
    trial = head
    for _ in range(3):
        try:
            trial.decode("utf-8")
            return "utf-8-sig"
        except UnicodeDecodeError as e:
            if e.start >= len(trial) - 3:
                trial = head[: e.start]
                continue
            break
    return "gbk"


def execute(
    pattern: str,
    path="",
    glob: str = "",
    i: bool = False,
    A: int = 0,
    B: int = 0,
    C: int = 0,
    head_limit: int = _DEFAULT_HEAD_LIMIT,
    _tool_context=None,
    **kwargs,
) -> str:
    # ── CLI风格参数别名兼容: -C/-B/-A/-i/-head_limit → C/B/A/i/head_limit ──
    # LLM训练数据中grep/ripgrep的CLI用法极常见，模型本能地传-C/-i等带横杠参数。
    # 未知的横杠参数保留原名，走下方TypeError提示有效参数，不静默吞掉错字。
    aliases = {}
    for key in list(kwargs):
        if key.startswith("-") and key[1:] in ("i", "A", "B", "C", "head_limit"):
            aliases[key[1:]] = kwargs.pop(key)
    if kwargs:
        raise TypeError(f"got an unexpected keyword argument '{next(iter(kwargs))}'")
    # 显式传参优先，别名仅补默认值（head_limit无法区分"未传"与"传默认值"，直接覆盖）
    if "i" in aliases and not i:
        i = aliases["i"]
    if "A" in aliases and not A:
        A = aliases["A"]
    if "B" in aliases and not B:
        B = aliases["B"]
    if "C" in aliases and not C:
        C = aliases["C"]
    if "head_limit" in aliases:
        head_limit = aliases["head_limit"]

    # AI可能传字符串类型的数值参数，统一转int（A/B/C/head_limit）
    try:
        A = int(A) if A is not None else 0
        B = int(B) if B is not None else 0
        C = int(C) if C is not None else 0
        head_limit = int(head_limit) if head_limit is not None else None
    except (TypeError, ValueError):
        return "[错误: A/B/C/head_limit需为整数]"

    # ── ReDoS 防护 ──
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return f"[错误: 正则表达式过长（>{_MAX_PATTERN_LENGTH}字符），拒绝执行以防ReDoS]"

    flags = re.IGNORECASE if to_bool(i) else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[错误: 非法正则: {e}]"

    if C > 0:
        A = C
        B = C

    if head_limit is not None and head_limit <= 0:
        return "[错误: head_limit需为正整数]"

    fast_searcher = _make_fast_searcher(regex.pattern, bool(flags & re.IGNORECASE))

    # ── path 归一化为列表（单值/数组均可），保持 AI 给定的顺序 ──
    paths = path if isinstance(path, (list, tuple)) else [path]
    if not paths or all(p is None for p in paths):
        paths = [""]

    ignore_dirs = set(_tool_context.ignore_dirs) if _tool_context and _tool_context.ignore_dirs else set()

    # ── 收集搜索目标 (target, label, is_file)：去重、缺失警告但不中断 ──
    warnings = []
    entries = []
    seen = set()
    cwd = os.getcwd()
    for raw in paths:
        if raw is None:
            continue
        p = str(raw).strip()
        if not p:
            p = cwd
        if os.path.isfile(p):
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            entries.append((p, p, True))
        elif os.path.isdir(p):
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            entries.append((p, p, False))
        else:
            warnings.append(p)

    if not entries:
        msg = "[无匹配]"
        if warnings:
            head = "、".join(warnings[:5]) + ("等" if len(warnings) > 5 else "")
            msg = f"路径不存在，已跳过: {head}\n{msg}"
        return msg

    # ── 全局预算上下文（跨所有 path 项共享）──
    ctx = {"expanded": 0, "limit_hit": False, "seen_files": set()}
    results = []
    for target, label, is_file in entries:
        _search_target(target, label, is_file, regex, fast_searcher,
                       glob, A, B, head_limit, ignore_dirs, results, ctx)

    # ── 无匹配：带搜索范围帮 AI 定位（Shell cd 会改变 cwd）──
    if not results:
        if len(entries) == 1:
            target_label, is_file = entries[0][1], entries[0][2]
            no_match = "[无匹配]" if is_file else f"[无匹配（搜索目录: {target_label}）]"
        else:
            names = "、".join(e[1] for e in entries[:3])
            if len(entries) > 3:
                names += "等"
            no_match = f"[无匹配（搜索范围: {names}）]"
        if warnings:
            head = "、".join(warnings[:5]) + ("等" if len(warnings) > 5 else "")
            return f"路径不存在，已跳过: {head}\n{no_match}"
        return no_match

    output = "\n".join(results)
    if ctx["limit_hit"]:
        output += f"\n...[已截断: 达到head_limit({head_limit})，剩余文件仅列文件名（含计数）。增大head_limit可展开更多匹配行]"
    if warnings:
        head = "、".join(warnings[:5]) + ("等" if len(warnings) > 5 else "")
        output = f"路径不存在，已跳过: {head}\n{output}"
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
# 二进制检测（内联在扫描中，避免双重 I/O）
# ═══════════════════════════════════════════════════════════════

def _check_binary_first_chunk(first_chunk: bytes) -> bool:
    """检查首块中是否含 NUL 字节。"""
    return first_chunk.count(0) >= _BINARY_NUL_THRESHOLD


# ═══════════════════════════════════════════════════════════════
# 滚动缓冲流式扫描（核心引擎）
# ═══════════════════════════════════════════════════════════════

def _scan_file(file_path, regex, fast_searcher, A, B, collect_budget):
    """全扫单个文件，返回 (count, blocks, in_file_trunc) 或 None（跳过）。

    - count: 该文件全部匹配数（准确计数，供表头显示）
    - blocks: [ [before_lines, match_line, after_lines] ]，行元素为 (line_num, text)
    - in_file_trunc: 该文件还有未收集进 blocks 的匹配（collect_budget 受限）
    - collect_budget: 最多收集多少个匹配块（None=无限）；超过预算的匹配仅计数

    二进制/超100MB/超长行异常的文件提前结束（与旧版语义一致）。
    """
    # ── 单次 I/O：rb 打开，检查二进制，文件大小 ──
    try:
        raw_f = open(file_path, "rb")
    except (PermissionError, OSError):
        return None

    try:
        raw_f.seek(0, 2)  # SEEK_END
        if raw_f.tell() > _MAX_FILE_SIZE:
            return None
        raw_f.seek(0)

        first_chunk = raw_f.read(_BUFFER_SIZE)
        if _check_binary_first_chunk(first_chunk):
            return None
        encoding = _detect_text_encoding(first_chunk)
    except (PermissionError, OSError):
        return None
    finally:
        raw_f.close()

    # 重新以文本模式打开（首块已判定非二进制；编码由首块探测得出，
    # GBK 文件按正确编码解码，中文 pattern 才能命中）
    try:
        f = open(file_path, "r", encoding=encoding, errors="replace", newline="")
    except (PermissionError, OSError):
        return None

    count = 0
    blocks = []
    leftover = ""
    line_num = 0
    before_window = deque()   # (line_num, line_text)，用于 before_context
    pending = None            # 收集中的块: [before[], (match_num, match_text), after[]]
    pending_after = 0
    budget = collect_budget   # None = 无限

    aborted = False
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

            # ── 超长行防护：视为异常文件，提前结束 ──
            if len(leftover) > _MAX_LINE_LENGTH:
                aborted = True
                break

            for line in lines:
                # 剥离行尾残留 \r（缓冲区恰好在 \r|\n 分裂时）
                line = line.rstrip("\r")
                line_num += 1

                # ── 快路径（纯文本）或慢路径（正则）──
                if fast_searcher:
                    matched = fast_searcher(line)
                else:
                    matched = bool(regex.search(line))

                if matched:
                    count += 1
                    # 新匹配打断尚未收满 after 的块
                    if pending is not None:
                        blocks.append(pending)
                        pending = None
                        pending_after = 0
                    if budget is None or len(blocks) < budget:
                        pending = [list(before_window), (line_num, line), []]
                        before_window.clear()
                        pending_after = A
                    else:
                        # 预算耗尽，仅计数
                        before_window.clear()
                        pending_after = 0
                elif pending_after > 0:
                    # after_context 行
                    pending[2].append((line_num, line))
                    pending_after -= 1
                    if pending_after == 0:
                        blocks.append(pending)
                        pending = None
                else:
                    # 维护 before_context 滑动窗口（仅在收集中）
                    if B > 0 and (budget is None or len(blocks) < budget):
                        before_window.append((line_num, line))
                        if len(before_window) > B:
                            before_window.popleft()

        # ── 处理文件末尾的不完整行 ──
        if leftover and not aborted and len(leftover) < _MAX_LINE_LENGTH:
            leftover = leftover.rstrip("\r")
            line_num += 1
            if fast_searcher:
                matched = fast_searcher(leftover)
            else:
                matched = bool(regex.search(leftover))
            if matched:
                count += 1
                if pending is not None:
                    blocks.append(pending)
                    pending = None
                if budget is None or len(blocks) < budget:
                    blocks.append([list(before_window), (line_num, leftover), []])

        # 收尾：after 未收满的块照常入列（文件末尾 after 行数不足是正常现象）
        if pending is not None:
            blocks.append(pending)
            pending = None
    finally:
        f.close()

    return count, blocks, count > len(blocks)


# ═══════════════════════════════════════════════════════════════
# 结果输出 — ripgrep heading 形态 + 预算文件边界降级
# ═══════════════════════════════════════════════════════════════

def _emit_file(label, count, blocks, in_file_trunc, head_limit, results, ctx):
    """按预算把单个文件的结果追加到 results（共享顺序/预算上下文）。

    规则：
    - 预算已耗尽 → 该文件仅输出清单行「label (N处)」（表头计数准确，来自全扫）
    - 否则展开：表头 + 全部匹配块（含上下文）；当前文件一旦展开即完整，
      文件边界生效，不产生无主行号
    """
    if count == 0:
        return

    budget = head_limit
    if budget is not None and ctx["expanded"] >= budget:
        # 清单行紧凑排列（类 files_with_matches 形态），不空行分隔
        results.append(f"{label} ({count}处)")
        ctx["limit_hit"] = True
        return

    if results:
        results.append("")   # 展开组间空行分隔（rg heading 风格）

    results.append(f"{label} ({count}处):")
    last_line = 0
    for before, (mnum, mtext), after in blocks:
        first_line = before[0][0] if before else mnum
        if last_line > 0 and last_line + 1 < first_line:
            results.append("--")
        for bnum, btext in before:
            results.append(f"{bnum}-{btext}")
            last_line = bnum
        results.append(f"{mnum}:{mtext}")
        last_line = mnum
        for anum, atext in after:
            results.append(f"{anum}-{atext}")
            last_line = anum
        ctx["expanded"] += 1

    if in_file_trunc:
        remaining = count - len(blocks)
        results.append(f"...[该文件其余{remaining}处匹配未展开: 已达head_limit({budget})。增大head_limit或缩小搜索范围查看其余]")


# ═══════════════════════════════════════════════════════════════
# 目标搜索 — 文件/目录统一入口
# ═══════════════════════════════════════════════════════════════

def _search_target(target, label, is_file, regex, fast_searcher, glob_filter,
                   A, B, head_limit, ignore_dirs, results, ctx):
    """搜索一个目标（文件或目录），结果按预算追加到 results。

    目录内文件按相对路径排序（确定性输出）；文件数 >= 10 并行扫描。
    """
    if is_file:
        file_items = [(target, label)]
    else:
        file_items = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, target)
                if glob_filter and not _match_glob(fname, rel, glob_filter):
                    continue
                file_items.append((full, rel))
        file_items.sort(key=lambda t: t[1])

    if not file_items:
        return

    use_parallel = len(file_items) >= _PARALLEL_MIN_FILES

    if use_parallel:
        # 并行下无法预知每个文件轮到时的剩余预算，统一按 head_limit 收集，
        # 输出阶段按顺序消费预算（收集超量部分丢弃，代价可控）
        per_file_budget = head_limit
        collected = {}
        worker_count = min(os.cpu_count() or 4, 12)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for full, rel in file_items:
                key = os.path.normcase(os.path.abspath(full))
                if key in ctx["seen_files"]:
                    continue
                ctx["seen_files"].add(key)
                fut = executor.submit(_scan_file, full, regex, fast_searcher, A, B, per_file_budget)
                futures[fut] = rel
            for fut in as_completed(futures):
                rel = futures[fut]
                try:
                    collected[rel] = fut.result()
                except Exception:
                    collected[rel] = None
        for full, rel in file_items:
            item = collected.get(rel)
            if item is None:
                continue
            count, blocks, in_file_trunc = item
            _emit_file(rel, count, blocks, in_file_trunc, head_limit, results, ctx)
    else:
        for full, rel in file_items:
            key = os.path.normcase(os.path.abspath(full))
            if key in ctx["seen_files"]:
                continue
            ctx["seen_files"].add(key)
            item = _scan_file(full, regex, fast_searcher, A, B,
                              _remaining_budget(head_limit, ctx))
            if item is None:
                continue
            count, blocks, in_file_trunc = item
            _emit_file(rel, count, blocks, in_file_trunc, head_limit, results, ctx)


def _remaining_budget(head_limit, ctx):
    """当前剩余可展开的匹配行数（None = 无限）。"""
    if head_limit is None:
        return None
    return head_limit - ctx["expanded"]


# ═══════════════════════════════════════════════════════════════
# glob 过滤 — 与 Glob 工具语义对齐
# ═══════════════════════════════════════════════════════════════

def _match_glob(fname: str, rel: str, glob_filter: str) -> bool:
    """glob 过滤：支持纯文件名或相对路径（含通配符），与 Glob 工具语义对齐。

    - 匹配对象为相对路径或纯文件名，二者任一命中即通过；
    - 花括号展开与 Glob 工具一致（复用 _expand_braces/_unescape_braces）；
    - Windows 下正反斜杠等价（glob 与 rel 均归一化为 /），且大小写不敏感
      （fnmatch 内部经 normcase，与 Windows 文件系统语义一致；POSIX 下 normcase 恒等）；
    - **/ 前缀可匹配零层目录（即根目录下的文件）。
    """
    if os.name == "nt":
        glob_filter = glob_filter.replace("\\", "/")
        rel = rel.replace("\\", "/")
    raw_patterns = _expand_braces(glob_filter)
    patterns = []
    for p in raw_patterns:
        p = _unescape_braces(p)
        patterns.append(p)
        if p.startswith("**/"):
            patterns.append(p[3:])  # **/ 可匹配零层目录
    return any(
        fnmatch.fnmatch(fname, p) or fnmatch.fnmatch(rel, p)
        for p in patterns
    )
