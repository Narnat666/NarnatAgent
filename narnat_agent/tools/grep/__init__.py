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
from ..glob import _expand_braces, _unescape_braces, _ignored_dirs_hint
from ..read import _detect_text_encoding, _sniff_bom_encoding

class GrepLimits:
    """Grep 工具边界参数（原模块级常量收敛为类成员）"""
    # ── 滚动缓冲区大小（64KB，与 ripgrep 的 DEFAULT_BUFFER_CAPACITY 一致）──
    BUFFER_SIZE = 64 * 1024

    # ── ReDoS 防护：正则最大长度 ──
    MAX_PATTERN_LENGTH = 4096

    # ── 单文件最大大小（100MB），超出跳过 ──
    MAX_FILE_SIZE = 100 * 1024 * 1024

    # ── 超长行显示截断阈值（1MB）：超过则完整参与匹配、仅显示前 MAX_LINE_DISPLAY 字符 ──
    MAX_LINE_LENGTH = 1 * 1024 * 1024

    # ── 超长行硬上限（8MB）：leftover 超过此值才中止该文件（其后内容不搜索）──
    MAX_LINE_HARD = 8 * 1024 * 1024

    # ── 超长行显示截断长度（与 Read 工具单行截断一致，防 MB 级单行灌满输出）──
    MAX_LINE_DISPLAY = 2000

    # ── 正则元字符集，用于判断 pattern 是否为纯文本 ──
    RE_META_CHARS = set(r".*+?[]{}()\|^$")

    # ── 二进制检测：首块中 NUL 字节阈值 ──
    BINARY_NUL_THRESHOLD = 1

    # ── head_limit 默认值：AI 显式传值的历史众数（514次中132次传30）──
    DEFAULT_HEAD_LIMIT = 30

    # ── 并行阈值：目录内文件数 >= 此值时启用线程池 ──
    PARALLEL_MIN_FILES = 10

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": (
            "正则搜索文件内容（仅支持本机文件，不支持远程设备文件）。"
            "默认返回每个命中文件的分组结果：文件表头（含匹配计数）+ 带行号的匹配行。"
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
                    "description": (
                        "搜索路径（默认当前目录）；可填目录、单个文件，或多个路径的数组"
                        "（文件/目录可混合，按给定顺序输出），如 [\"src/a.c\", \"include\"]"
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "文件过滤，如*.py、src/*.c、**/*.c、*.{c,h}、src/**/*.{py,md}"
                        "（花括号多模式，与Glob工具语法一致，默认空）"
                    ),
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
                    "description": (
                        "最大返回匹配行数（正整数，默认30）；达到后剩余文件仅列文件名（含计数），"
                        "增大可展开更多匹配行"
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
}


CAPABILITY = {
    "label": "搜索内容",
    "dispatch": "readonly",
    "summary": "pattern",
    "trusted_output": True,
}


def _group_spans(pattern: str) -> list:
    """返回每个括号组的内容区间 [(content_start, content_end)]（转义/字符类内不识别）"""
    spans = []
    stack = []
    in_class = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
        elif c == "(":
            stack.append(i)
        elif c == ")":
            if stack:
                gs = stack.pop()
                spans.append((gs + 1, i))
        i += 1
    return spans


def _penalty_quantifier_after(pattern: str, i: int) -> bool:
    """位置 i 起是否有"放大性"量词。

    * / + → 是；{m,} / {m,n}（变长）→ 是；{n}（固定次数）→ n ≥ 16 时是
    （少量固定重复安全；大量固定重复可变元素仍指数，覆盖 (a?){40} 型）。
    """
    if i >= len(pattern):
        return False
    c = pattern[i]
    if c in ("*", "+"):
        return True
    if c == "{":
        end = pattern.find("}", i)
        if end == -1:
            return False
        body = pattern[i + 1:end]
        if "," in body:
            return True
        try:
            return int(body) >= 16
        except ValueError:
            return False
    return False


def _segment_has_nested_risk(seg: str) -> bool:
    """组内容是否含可嵌套放大的量化元素（* / + / ? / 变长 {m,n}），或存在
    "交替前缀重叠"（| 两侧紧邻字面相同，覆盖 (a|aa)+ 型灾难形态写法）。"""
    in_class = False
    i, n = 0, len(seg)
    # 跳过组前缀（?: ?= ?! ?<= ?<! ?P<name>）——其中的 ? 是语法标记不是量词
    if seg.startswith(("?:", "?=", "?!")):
        i = 2
    elif seg.startswith(("?<=", "?<!")):
        i = 3
    elif seg.startswith("?P<"):
        gt = seg.find(">")
        if gt != -1:
            i = gt + 1
    while i < n:
        c = seg[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
        elif c in ("*", "+", "?"):
            return True
        elif c == "{":
            end = seg.find("}", i)
            if end != -1 and "," in seg[i + 1:end]:
                return True
        elif c == "|":
            prev_c = seg[i - 1] if i > 0 else ""
            nxt_c = seg[i + 1] if i + 1 < n else ""
            if (prev_c and nxt_c and prev_c == nxt_c
                    and prev_c not in "\\|()[]*+?{}"):
                return True
        i += 1
    return False


def _has_too_many_optional(pattern: str, limit: int = 16) -> bool:
    """平铺 ? 量词过多 → 灾难性回溯（无分组形态的 ReDoS 变体：如 40 个 a?
    后接 a{40}，2^40 组合）。不计非贪婪修正（*? / +? / }? / ??）与组前缀
    （(?: (?= (?! (?<= (?<! 内的 ?）。"""
    count = 0
    in_class = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
        elif c == "?":
            prev = pattern[i - 1] if i > 0 else ""
            if prev not in ("*", "+", "}", "?", "("):
                count += 1
                if count > limit:
                    return True
        i += 1
    return False


def _has_nested_quantifier(pattern: str) -> bool:
    """检测灾难性回溯的常见根源形态（启发式，非完备）：

    1) 组内容含量化元素（* / + / ? / 变长 {m,n}，任意深度）且组自身被
       * / + / 变长{} 修饰——如 (a+)+、(a?)*、(a{1,3})+、((a+))+；
    2) 被放大性量词修饰的组内存在交替前缀重叠——如 (a|aa)+、(a|a)+。

    字符类、转义、组前缀（?: 等）内不检测。误报时给出改写指引——
    宁可要求改写，不可挂死（启发式已知不覆盖全部等价形态）。
    """
    for cs, ce in _group_spans(pattern):
        if not _penalty_quantifier_after(pattern, ce + 1):
            continue
        if _segment_has_nested_risk(pattern[cs:ce]):
            return True
    return False


def execute(
    pattern: str,
    path="",
    glob: str = "",
    i: bool = False,
    A: int = 0,
    B: int = 0,
    C: int = 0,
    head_limit: int = GrepLimits.DEFAULT_HEAD_LIMIT,
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
    # OverflowError：JSON 里的 1e999 → float('inf')，int(inf) 抛 OverflowError
    try:
        A = int(A) if A is not None else 0
        B = int(B) if B is not None else 0
        C = int(C) if C is not None else 0
        head_limit = int(head_limit) if head_limit is not None else None
    except (TypeError, ValueError, OverflowError):
        return "[错误: A/B/C/head_limit需为整数]"

    # pattern=None 防御：必填参数传 null 时 len(None) 抛 TypeError 逃出工具层；
    # 空串 "" 是合法 pattern（命中所有行），只拒 null（文案与 Glob 对齐）
    if pattern is None:
        return "[错误: pattern不能为空]"
    # 非字符串类型（int/bool/list 等）同族防御：len()/re.compile() 会抛 TypeError 逃逸
    if not isinstance(pattern, str):
        return "[错误: pattern需为字符串]"

    # ── ReDoS 防护 ──
    if len(pattern) > GrepLimits.MAX_PATTERN_LENGTH:
        return f"[错误: 正则表达式过长（>{GrepLimits.MAX_PATTERN_LENGTH}字符），拒绝执行以防ReDoS]"
    if _has_nested_quantifier(pattern):
        return ("[错误: 正则含嵌套量词（如 (a+)+、(a+)*b），可能灾难性回溯导致挂死，"
                "请改写（避免对含 +/* 的分组再加 +/*，必要时展开重复）]")
    if _has_too_many_optional(pattern):
        return ("[错误: 正则含过多可选量词 ?（可能灾难性回溯导致挂死），"
                "请改写（减少可选元素或展开重复）]")

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

    # str() 兜底：绕过 loader 直接构造 ToolContext 时元素可能是数字/对象，
    # set() 对不可哈希元素抛 TypeError（配置侧已在 loader 过滤，此处仅防御）
    ignore_dirs = ({str(d) for d in _tool_context.ignore_dirs}
                   if _tool_context and _tool_context.ignore_dirs else set())

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
    ctx = {"expanded": 0, "limit_hit": False, "seen_files": set(),
           "skipped": _new_skip_stats()}
    results = []
    for target, label, is_file in entries:
        _search_target(target, label, is_file, regex, fast_searcher,
                       glob, A, B, head_limit, ignore_dirs, results, ctx)

    skip_line = _skip_summary_line(ctx)
    # 目录不可访问 / 二进制截断 提示行（仅在触发时出现，单列一行避免改动既有汇总行文本）
    hint_lines = [h for h in (_dir_error_line(ctx), _binary_cut_line(ctx)) if h]

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
            no_match = f"路径不存在，已跳过: {head}\n{no_match}"
        for line in hint_lines:
            no_match += f"\n{line}"
        if skip_line:
            no_match += f"\n{skip_line}"
        # 忽略目录提示：无匹配时说明范围被配置收缩（有匹配时不加，避免噪音）
        ignored_hint = _ignored_dirs_hint(ignore_dirs)
        if ignored_hint:
            no_match += f"\n{ignored_hint}"
        return no_match

    output = "\n".join(results)
    if ctx["limit_hit"]:
        output += f"\n...[已截断: 达到head_limit({head_limit})，剩余文件仅列文件名（含计数）。增大head_limit可展开更多匹配行]"
    if warnings:
        head = "、".join(warnings[:5]) + ("等" if len(warnings) > 5 else "")
        output = f"路径不存在，已跳过: {head}\n{output}"
    for line in hint_lines:
        output += f"\n{line}"
    if skip_line:
        output += f"\n{skip_line}"
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
        if ch in GrepLimits.RE_META_CHARS:
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
    return first_chunk.count(0) >= GrepLimits.BINARY_NUL_THRESHOLD


def _clip_line(text: str) -> str:
    """超长行显示截断：超过 MAX_LINE_LENGTH 的行仅显示前 MAX_LINE_DISPLAY 字符。

    匹配判定与计数基于完整行，不受截断影响；仅避免 MB 级单行灌满输出。
    提示文案与 Read 工具的单行截断一致（AI 见到的格式统一）。
    """
    if len(text) <= GrepLimits.MAX_LINE_LENGTH:
        return text
    return (text[:GrepLimits.MAX_LINE_DISPLAY]
            + f"...[单行截断: 本行共{len(text)}字符,"
              f"仅显示前{GrepLimits.MAX_LINE_DISPLAY}字符]")


# ═══════════════════════════════════════════════════════════════
# 滚动缓冲流式扫描（核心引擎）
# ═══════════════════════════════════════════════════════════════

def _scan_file(file_path, regex, fast_searcher, A, B, collect_budget):
    """全扫单个文件，返回 (count, blocks, in_file_trunc, aborted)；跳过时返回哨兵字符串
    （"skip_oversize"/"skip_binary"/"skip_unreadable"，供上层汇总提示）。

    - count: 该文件全部匹配数（准确计数，供表头显示）
    - blocks: [ [before_lines, match_line, after_lines] ]，行元素为 (line_num, text)
    - in_file_trunc: 该文件还有未收集进 blocks 的匹配（collect_budget 受限）
    - aborted: 扫描到超长行（> MAX_LINE_LENGTH）提前结束，剩余内容未搜索
    - collect_budget: 最多收集多少个匹配块（None=无限）；超过预算的匹配仅计数

    二进制/超100MB/不可读的文件跳过（与旧版语义一致）。
    """
    # ── 单次 I/O：rb 打开，检查二进制，文件大小 ──
    try:
        raw_f = open(file_path, "rb")
    except (PermissionError, OSError):
        return "skip_unreadable"

    try:
        raw_f.seek(0, 2)  # SEEK_END
        if raw_f.tell() > GrepLimits.MAX_FILE_SIZE:
            return "skip_oversize"
        raw_f.seek(0)

        first_chunk = raw_f.read(GrepLimits.BUFFER_SIZE)
        encoding = _sniff_bom_encoding(first_chunk)
        if encoding is None:
            # BOM 嗅探优先于二进制判定：UTF-16 文本含 NUL，不能被误判为二进制
            if _check_binary_first_chunk(first_chunk):
                return "skip_binary"
            encoding = _detect_text_encoding(first_chunk)
    except (PermissionError, OSError):
        return "skip_unreadable"
    finally:
        raw_f.close()

    # 重新以文本模式打开（首块已判定非二进制；编码由首块探测得出，
    # GBK 文件按正确编码解码，中文 pattern 才能命中）
    try:
        f = open(file_path, "r", encoding=encoding, errors="replace", newline="")
    except (PermissionError, OSError):
        return "skip_unreadable"

    count = 0
    blocks = []
    leftover = ""
    line_num = 0
    before_window = deque()   # (line_num, line_text)，用于 before_context
    pending = None            # 收集中的块: [before[], (match_num, match_text), after[]]
    pending_after = 0
    budget = collect_budget   # None = 无限

    aborted = None   # None=正常；"longline"=超长行中止；"binary"=二进制截断
    try:
        while True:
            chunk = f.read(GrepLimits.BUFFER_SIZE)
            if not chunk:
                break

            # ── CRLF 归一化（防止 \r\n 在缓冲区边界分裂）──
            chunk = chunk.replace("\r\n", "\n")

            data = leftover + chunk
            lines = data.split("\n")
            leftover = lines.pop()

            # ── 超长行硬上限：仅超过 8MB 才中止；1MB~8MB 行完整参与匹配、
            #    显示时截断（旧版 1MB 即中止会丢掉长行之后的匹配）──
            if len(leftover) > GrepLimits.MAX_LINE_HARD:
                aborted = "longline"
                break

            # ── 流式二进制检测：NUL 出现在文件任意位置（不限于首块）即停止
            #    该文件后续搜索（已收集的匹配保留，与 rg 的 quit 语义一致）。
            #    chunk 级预筛（一次 C 扫描）使无 NUL 的常规文件几乎零开销；
            #    仅含 NUL 的 chunk 才逐行定位停点 ──
            chunk_has_nul = "\x00" in chunk or "\x00" in leftover
            stop_binary = False
            for line in lines:
                # 剥离行尾残留 \r（缓冲区恰好在 \r|\n 分裂时）
                line = line.rstrip("\r")
                line_num += 1

                if chunk_has_nul and "\x00" in line:
                    aborted = "binary"
                    stop_binary = True
                    break

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
                        pending = [list(before_window), (line_num, _clip_line(line)), []]
                        before_window.clear()
                        pending_after = A
                    else:
                        # 预算耗尽，仅计数
                        before_window.clear()
                        pending_after = 0
                elif pending_after > 0:
                    # after_context 行
                    pending[2].append((line_num, _clip_line(line)))
                    pending_after -= 1
                    if pending_after == 0:
                        blocks.append(pending)
                        pending = None
                else:
                    # 维护 before_context 滑动窗口（仅在收集中）
                    if B > 0 and (budget is None or len(blocks) < budget):
                        before_window.append((line_num, _clip_line(line)))
                        if len(before_window) > B:
                            before_window.popleft()

            if stop_binary:
                break

        # ── 处理文件末尾的不完整行 ──
        if leftover and not aborted:
            leftover = leftover.rstrip("\r")
            if "\x00" in leftover:
                aborted = "binary"   # 二进制数据不参与匹配，避免 NUL 行进入输出
            elif len(leftover) < GrepLimits.MAX_LINE_HARD:
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
                        blocks.append([list(before_window),
                                       (line_num, _clip_line(leftover)), []])

        # 收尾：after 未收满的块照常入列（文件末尾 after 行数不足是正常现象）
        if pending is not None:
            blocks.append(pending)
            pending = None
    finally:
        f.close()

    return count, blocks, count > len(blocks), aborted


# ═══════════════════════════════════════════════════════════════
# 跳过文件汇总（超100MB/二进制/不可读/超长行中止 → 提示而非静默）
# ═══════════════════════════════════════════════════════════════

_SKIP_KINDS = {"skip_oversize": "oversize",
               "skip_binary": "binary",
               "skip_unreadable": "unreadable",
               "skip_longline": "longline",
               "skip_binary_cut": "binary_cut"}
_SKIP_MAX_SAMPLES = 3


def _new_skip_stats() -> dict:
    """跳过统计容器（跨 path 项共享，挂在 ctx 上）。"""
    return {"oversize": 0, "binary": 0, "unreadable": 0, "longline": 0,
            "binary_cut": 0, "direrror": 0,
            "paths": [], "dir_paths": [], "cut_paths": []}


def _record_skip(ctx, sentinel: str, rel: str):
    """记录一个被跳过的文件（分类计数 + 最多 3 个示例路径）。"""
    stats = ctx["skipped"]
    stats[_SKIP_KINDS[sentinel]] += 1
    if sentinel == "skip_binary_cut":
        # 二进制截断=部分搜索（已有匹配输出），不列入"跳过示例"，
        # 避免与完全跳过的文件混淆；示例单独存 cut_paths
        if len(stats["cut_paths"]) < _SKIP_MAX_SAMPLES:
            stats["cut_paths"].append(rel)
        return
    if len(stats["paths"]) < _SKIP_MAX_SAMPLES:
        stats["paths"].append(rel)


def _skip_summary_line(ctx) -> str:
    """跳过提示行；无跳过返回 ""。"""
    stats = ctx["skipped"]
    total = (stats["oversize"] + stats["binary"] + stats["unreadable"]
             + stats["longline"])
    if total <= 0:
        return ""
    return (f"[已跳过 {total} 个文件: 超100MB({stats['oversize']}) / "
            f"二进制({stats['binary']}) / 不可读({stats['unreadable']}) / "
            f"超长行中止({stats['longline']})（如 {'、'.join(stats['paths'])}）]")


def _binary_cut_line(ctx) -> str:
    """二进制截断提示行（NUL 出现在文件中段：该文件只搜索了 NUL 之前的部分）。"""
    stats = ctx["skipped"]
    if not stats["binary_cut"]:
        return ""
    return (f"[二进制截断 {stats['binary_cut']} 个文件（搜索到二进制数据处停止，"
            f"其后内容未搜索；如需完整搜索请用其他工具提取文本）"
            f"{('（如 ' + '、'.join(stats['cut_paths']) + '）') if stats['cut_paths'] else ''}]")


def _dir_error_line(ctx) -> str:
    """目录遍历错误提示行（权限不足/路径过长等导致目录内容未被搜索）。

    此前 os.walk 静默吞掉目录级错误，输出"[无匹配]"——AI 会把"未读到"
    误判为"内容不存在"。此处显式提示，错误可见。
    """
    stats = ctx["skipped"]
    if not stats["direrror"]:
        return ""
    samples = "、".join(stats["dir_paths"])
    return (f"[目录不可访问 {stats['direrror']} 个（权限/路径长度等原因，"
            f"其内容未被搜索）{('（如 ' + samples + '）') if samples else ''}]")


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

def _is_nt_reparse_dir(path: str) -> bool:
    """路径是否 Windows 重解析点目录（junction 等）。

    os.walk(followlinks=False) 只挡符号链接，junction 的 is_symlink() 为
    False 会被递归进入（自反 junction 死循环），需查 st_reparse_tag。
    非 Windows 无此属性，getattr 兜底 0 → 不跳过。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0))


def _search_target(target, label, is_file, regex, fast_searcher, glob_filter,
                   A, B, head_limit, ignore_dirs, results, ctx):
    """搜索一个目标（文件或目录），结果按预算追加到 results。

    目录内文件按相对路径排序（确定性输出）；文件数 >= 10 并行扫描。
    """
    if is_file:
        file_items = [(target, label)]
    else:
        file_items = []

        def _on_walk_error(exc):
            # os.walk 默认静默吞掉目录级错误（权限不足、路径超长等），
            # 输出"[无匹配]"会让 AI 把"未读到"误判为"内容不存在"；
            # 改为计数并在结果末尾显式提示（错误可见化）
            stats = ctx["skipped"]
            stats["direrror"] += 1
            if len(stats["dir_paths"]) < _SKIP_MAX_SAMPLES:
                stats["dir_paths"].append(str(getattr(exc, "filename", None) or target))

        for dirpath, dirnames, filenames in os.walk(target, onerror=_on_walk_error):
            dirnames[:] = [d for d in dirnames
                           if d not in ignore_dirs
                           and not _is_nt_reparse_dir(os.path.join(dirpath, d))]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    rel = os.path.relpath(full, target)
                except ValueError:
                    # 保留设备名文件（如 CON，ntpath 规范化后成为独立 mount
                    # \\.\CON）：relpath 跨 mount 抛 ValueError。此类文件无法按
                    # 普通路径读取，跳过并计入"不可读"，避免单文件中断整树搜索
                    _record_skip(ctx, "skip_unreadable", full)
                    continue
                if glob_filter and not _match_glob(fname, rel, glob_filter):
                    continue
                file_items.append((full, rel))
        file_items.sort(key=lambda t: t[1])

    if not file_items:
        return

    use_parallel = len(file_items) >= GrepLimits.PARALLEL_MIN_FILES

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
            if isinstance(item, str):
                _record_skip(ctx, item, rel)
                continue
            count, blocks, in_file_trunc, aborted = item
            if aborted == "longline":
                _record_skip(ctx, "skip_longline", rel)
            elif aborted == "binary":
                _record_skip(ctx, "skip_binary_cut", rel)
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
            if isinstance(item, str):
                _record_skip(ctx, item, rel)
                continue
            count, blocks, in_file_trunc, aborted = item
            if aborted == "longline":
                _record_skip(ctx, "skip_longline", rel)
            elif aborted == "binary":
                _record_skip(ctx, "skip_binary_cut", rel)
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
