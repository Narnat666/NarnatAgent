"""
流式 Markdown→ANSI 渲染器

包含：
- 语言→颜色映射表
- InlineRules: 行内元素正则替换流水线
- BlockRule: 块级元素策略表
- CodeBlockRenderer: 代码块渲染
- StreamingRenderer: 流式状态机
"""

import io
import os
import re
import shutil
import sys
import threading
import unicodedata
from typing import Optional, Callable, List, Match

from .colors import (
    R, B, D,
    C_PRIMARY, C_SECONDARY, C_CODE_BG,
    MD_H1, MD_H3, MD_H4,
    MD_BOLD, MD_ITALIC, MD_STRIKE, MD_CODE, MD_LINK, MD_IMAGE,
    MD_BLOCKQUOTE, MD_HR, MD_UL, MD_OL,
    MD_TASK_DONE, MD_TASK_UNDONE,
    MD_TABLE_BORDER, MD_TABLE_CONTENT,
    CB_LINE_NO, CB_LANG_LABEL,
    CB_LANG_CYAN, CB_LANG_YELLOW, CB_LANG_GREEN,
    CB_LANG_MAGENTA, CB_LANG_RED, CB_LANG_BLUE, CB_LANG_GRAY,
    DIFF_HEADER, DIFF_RANGE, DIFF_ADDED, DIFF_REMOVED, DIFF_CONTEXT,
    UI_SEPARATOR,
    _stdout_write,
)


# ═══════════════════════════════════════════════════════════════
# 终端宽度与显示宽度
# ═══════════════════════════════════════════════════════════════

_MAX_TERMINAL_WIDTH = 160


def _windows_console_window_cols() -> int:
    """Windows 控制台「可见窗口」宽度（列数）。

    仅对传统 conhost（真实控制台窗口）生效，此时缓冲区宽度可能大于
    可见窗口宽度（如 120 缓冲 + 80 窗口），按缓冲区宽度渲染会导致输出行
    超出可见窗口、被终端强制折行，破坏表格对齐。

    判定顺序：
    1. Windows Terminal / ConPTY 环境（有 WT_SESSION 或 GetConsoleWindow 为 0）
       → 返回 0，调用方回退到 shutil（ConPTY 下缓冲区宽度与窗格一致）。
    2. conhost 可见窗口：用客户区宽度 / 字体宽度换算真实列数。
    3. conhost 隐藏窗口（如从启动器拉起）：退而求其次用 srWindow，
       仍比缓冲区宽度可靠。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE

        # Windows Terminal / ConPTY：没有真实控制台窗口，交给 shutil
        if os.environ.get("WT_SESSION"):
            return 0
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return 0

        # conhost：量窗口客户区宽度
        rect = _RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            client_w = rect.right - rect.left
            if client_w > 0:
                # CONSOLE_FONT_INFO: DWORD nFont + COORD dwFontSize(X, Y)，共 8 字节
                info = ctypes.create_string_buffer(8)
                if kernel32.GetCurrentConsoleFont(handle, False, info):
                    font_x = int.from_bytes(info.raw[4:6], "little")
                    if font_x > 0:
                        cols = client_w // font_x
                        if cols > 0:
                            return cols

        # 隐藏窗口量不到客户区 → 退而求其次用 srWindow（仍优于缓冲区宽度）
        # CONSOLE_SCREEN_BUFFER_INFO: dwSize(4) dwCursorPos(4) wAttr(2) srWindow(8) maxWinSize(4)
        info = ctypes.create_string_buffer(22)
        if kernel32.GetConsoleScreenBufferInfo(handle, info):
            left = int.from_bytes(info.raw[10:12], "little", signed=True)
            right = int.from_bytes(info.raw[12:14], "little", signed=True)
            cols = right - left + 1
            if cols > 0:
                return cols
    except Exception:
        pass
    return 0


def _terminal_width() -> int:
    # 手动兜底：极端环境下终端宽度检测不可靠时，可用环境变量强制指定
    forced = os.environ.get("NARNAT_TERM_WIDTH", "").strip()
    if forced.isdigit():
        return max(min(int(forced), _MAX_TERMINAL_WIDTH), 20)
    # 传统 conhost（有真实可见窗口）：以可见窗口宽度为准，防止按缓冲区宽度渲染导致折行
    win_cols = _windows_console_window_cols()
    if win_cols:
        cols = win_cols
    else:
        # ConPTY（Windows Terminal/VS Code 等）或管道：缓冲区宽度即实际可用宽度
        try:
            cols = shutil.get_terminal_size().columns
        except Exception:
            cols = 120
    return max(min(cols, _MAX_TERMINAL_WIDTH), 20)


_re_ansi = re.compile(r"\x1b\[[0-9;]*m")


def _char_width(ch: str) -> int:
    """单字符的终端显示宽度（CJK/全角=2，其余=1）。

    歧义宽度字符（east_asian_width == 'A'，如 → ≈ 等）统一按 1 列计算：
    Windows Terminal / VS Code / 多数 Linux 终端默认按窄字符渲染，
    中文版控制台对 GBK 中不存在的字符（如 →）同样按窄字符渲染。
    若按 2 列计算，单元格右边框会向左偏移 1 列，出现可见的边框不齐。
    """
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    """终端显示宽度：CJK字符=2列，跳过ANSI转义序列。"""
    text = _re_ansi.sub("", text)
    return sum(_char_width(ch) for ch in text)


def _visual_chars(text: str) -> List[str]:
    """将文本拆分为视觉字符列表，跳过ANSI转义序列。

    返回列表中每个元素是一个"视觉字符"：
    - 普通ASCII字符：单字符字符串
    - CJK宽字符：单字符字符串（但占2列）
    - ANSI转义序列：完整的转义序列字符串（占0列）
    """
    result: List[str] = []
    i = 0
    raw = text
    while i < len(raw):
        if raw[i] == '\x1b':
            # ANSI转义序列：\x1b[ ... m
            j = i + 1
            if j < len(raw) and raw[j] == '[':
                j += 1
                while j < len(raw) and raw[j] not in 'mABCDEFGHJKSTfh':
                    j += 1
                if j < len(raw):
                    j += 1
                result.append(raw[i:j])
                i = j
                continue
        result.append(raw[i])
        i += 1
    return result


def _wrap_cell(ansi_text: str, max_width: int) -> List[str]:
    """将含ANSI转义的文本按显示宽度折行。

    Args:
        ansi_text: 含ANSI颜色码的文本
        max_width: 最大显示宽度（必须>0）

    Returns:
        折行后的文本列表，每个元素显示宽度<=max_width。
        空文本返回 [""]。
        每行自动继承之前的ANSI状态，确保颜色不断裂。
    """
    if max_width <= 0:
        return [ansi_text]

    chars = _visual_chars(ansi_text)
    lines: List[str] = []
    current = ""
    current_width = 0
    # 追踪当前活跃的ANSI序列，折行时在新行开头重放
    active_ansi: List[str] = []

    for ch in chars:
        if ch.startswith('\x1b'):
            # ANSI转义序列，不占宽度
            current += ch
            if ch == '\x1b[0m':
                # 重置序列，清空活跃状态
                active_ansi.clear()
            else:
                active_ansi.append(ch)
            continue

        # 计算该字符的显示宽度
        cw = _char_width(ch)

        if current_width + cw > max_width:
            # 超出宽度，换行
            # 当前行追加重置，避免颜色泄漏到后续内容
            lines.append(current + "\x1b[0m")
            # 新行开头重放活跃ANSI状态
            current = "".join(active_ansi) + ch
            current_width = cw
        else:
            current += ch
            current_width += cw

    if current or not lines:
        lines.append(current)

    return lines


# ═══════════════════════════════════════════════════════════════
# Diff 着色（与 tools/diff_utils.py 逻辑一致，此处供 UI 层使用）
# ═══════════════════════════════════════════════════════════════

def colorize_diff(diff_text: str) -> str:
    """对 unified diff 文本添加 ANSI 颜色"""
    if not diff_text or diff_text == "[无差异]":
        return f"{C_SECONDARY}[无差异]{R}"
    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{DIFF_HEADER}{line}{R}")
        elif line.startswith("@@"):
            out.append(f"{DIFF_RANGE}{line}{R}")
        elif line.startswith("-"):
            out.append(f"{DIFF_REMOVED}{line}{R}")
        elif line.startswith("+"):
            out.append(f"{DIFF_ADDED}{line}{R}")
        else:
            out.append(f"{DIFF_CONTEXT}{line}{R}")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════
# 分隔线
# ═══════════════════════════════════════════════════════════════

def _sep() -> None:
    _stdout_write(f"  {UI_SEPARATOR}{'─' * (_terminal_width() - 2)}{R}\n")


# ═══════════════════════════════════════════════════════════════
# 语言→颜色映射表  (数据驱动，O(1) 查找)
# ═══════════════════════════════════════════════════════════════

_LANG_COLOR_MAP: dict = {}
_LANG_GROUPS = [
    ("cyan",    ["python", "py", "pyi", "go", "dart", "r", "rstats", "diff", "patch"]),
    ("yellow",  ["javascript", "js", "mjs", "typescript", "ts", "tsx", "jsx",
                 "java", "kt", "scala", "kotlin"]),
    ("green",   ["bash", "sh", "zsh", "shell", "fish", "cpp", "c", "h", "hpp",
                 "cc", "dockerfile", "docker", "makefile", "cmake"]),
    ("magenta", ["json", "jsonc", "toml", "yaml", "yml", "sql", "mysql",
                 "pgsql", "php", "lua", "perl", "pl", "ini", "cfg", "conf"]),
    ("red",     ["html", "htm", "xml", "svg", "rust", "rs", "ruby", "rb", "swift"]),
    ("blue",    ["css", "scss", "sass", "less"]),
    ("gray",    ["markdown", "md", "text", "txt", "log"]),
]
_COLOR_MAP = {"cyan": CB_LANG_CYAN, "yellow": CB_LANG_YELLOW, "green": CB_LANG_GREEN,
              "blue": CB_LANG_BLUE, "magenta": CB_LANG_MAGENTA, "red": CB_LANG_RED, "gray": CB_LANG_GRAY}
for _grp_color, _grp_langs in _LANG_GROUPS:
    for _lang in _grp_langs:
        _LANG_COLOR_MAP[_lang] = _grp_color


# ═══════════════════════════════════════════════════════════════
# 行内 Markdown 解析器 ── 正则替换流水线
# ═══════════════════════════════════════════════════════════════

class InlineRules:
    """
    纯函数流水线：输入原始文本 → 依次应用替换规则 → 输出 ANSI 文本
    规则顺序体现 Markdown 优先级：删除线 > 粗体 > 斜体 > 行内代码 > 图片 > 链接
    """

    _RE_STRIKE = re.compile(r"~~(.+?)~~")
    _RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
    _RE_ITAL = re.compile(r"\*(.+?)\*")
    _RE_CODE = re.compile(r"`([^`]+)`")
    _RE_IMG = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
    # 链接：[ 前面不能是 \x1b（避免匹配 ANSI 转义序列中的 [）
    _RE_LINK = re.compile(r"(?<!\x1b)\[([^\]]+)\]\([^)]+\)")

    @classmethod
    def render(cls, text: str) -> str:
        text = cls._RE_STRIKE.sub(f"{MD_STRIKE}\\1{R}", text)
        text = cls._RE_BOLD.sub(f"{MD_BOLD}\\1{R}", text)
        text = cls._RE_ITAL.sub(f"{MD_ITALIC}\\1{R}", text)
        text = cls._RE_CODE.sub(f"{MD_CODE}\\1{R}", text)
        text = cls._RE_IMG.sub(f"{MD_IMAGE}\\1{R}", text)
        text = cls._RE_LINK.sub(f"{MD_LINK}\\1{R}", text)
        return text


# ═══════════════════════════════════════════════════════════════
# 块级 Markdown 策略表 ── 优先级排序的规则引擎
# ═══════════════════════════════════════════════════════════════

class BlockRule:
    """
    块级渲染规则：封装 (优先级, 名称, 匹配函数, 渲染函数)
    优先级越低越先匹配
    """
    __slots__ = ('priority', 'name', 'match', 'render')

    def __init__(self, priority: int, name: str,
                 match: Callable[[str], Optional[Match]],
                 render: Callable[[str, Match], str]) -> None:
        self.priority = priority
        self.name = name
        self.match = match
        self.render = render


def _render_heading(_line: str, m: Match) -> str:
    level = len(m.group(1))
    body = InlineRules.render(m.group(2))
    if level <= 2:
        return f"  {MD_H1}{body}{R}"
    if level == 3:
        return f"  {MD_H3}{body}{R}"
    return f"  {MD_H4}{body}{R}"


def _render_hr(_line: str, _m: Match) -> str:
    return f"  {MD_HR}{_line.strip()}{R}"


def _render_task(_line: str, m: Match) -> str:
    done = m.group(1).lower() == "x"
    marker = f"{MD_TASK_DONE}v{R}" if done else f"{MD_TASK_UNDONE}o{R}"
    return f"   {marker} {C_PRIMARY}{InlineRules.render(m.group(2))}{R}"


def _render_ul(_line: str, _m: Match) -> str:
    return f"   {MD_UL}*{R} {C_PRIMARY}{InlineRules.render(_line[2:])}{R}"


def _render_ol(_line: str, m: Match) -> str:
    num = m.group(1)
    body_start = len(num) + 2
    return f"   {MD_OL}{num}.{R} {C_PRIMARY}{InlineRules.render(_line[body_start:])}{R}"


def _render_blockquote(_line: str, _m: Match) -> str:
    depth = 0
    rest = _line
    while rest.startswith(">"):
        rest = rest[1:]
        depth += 1
    body = rest.strip()
    return f"  {MD_BLOCKQUOTE}{'| ' * depth}{R}{C_PRIMARY}{InlineRules.render(body)}{R}"


def _render_table_row(_line: str, _m: Match) -> str:
    cells = [c.strip() for c in _line.strip("|").split("|")]
    if _is_table_separator(cells):
        return ""
    return f"    {MD_TABLE_CONTENT}" + " | ".join(InlineRules.render(c) for c in cells) + f"{R}"


def _render_paragraph(_line: str, _m: Match) -> str:
    return f"  {C_PRIMARY}{InlineRules.render(_line)}{R}"


_RE_TABLE_SEP = re.compile(r"^[-:]+$")


def _fit_widths(natural: List[int], avail: int, min_col: int = 2) -> List[int]:
    """把各列自然宽度压缩到总宽度 avail 以内，保证 sum(widths) == avail（或尽量接近）。

    优先保证每列 min_col，剩余按自然宽度比例分配，最后修正舍入误差。
    avail 小于 cols*min_col 时退化为均分。
    """
    cols = len(natural)
    if cols <= 0:
        return []
    if avail < cols * min_col:
        base = max(1, avail // cols)
        widths = [base] * cols
    else:
        widths = [min_col] * cols
        remaining = avail - cols * min_col
        total_natural = sum(natural)
        if remaining > 0 and total_natural > 0:
            for i in range(cols):
                widths[i] += int(remaining * natural[i] / total_natural)
    # 修正舍入误差（不超出 avail）
    diff = avail - sum(widths)
    if diff > 0:
        for i in range(diff):
            widths[i % cols] += 1
    elif diff < 0:
        # 只可能出现在 avail < cols*min_col 时 base=avail//cols 但求和超出（不会），防御性裁剪
        widths[-1] = max(1, widths[-1] + diff)
    return widths


def _is_table_separator(cells: List[str]) -> bool:
    if not any(c for c in cells):
        return False
    return all(_RE_TABLE_SEP.match(c) for c in cells if c)


_re_head = re.compile(r"^(#{1,6})\s+(.+)")
_re_hr = re.compile(r"^[-*_]{3,}\s*$")
_re_task = re.compile(r"^[-*+]\s+\[([ xX])\]\s*(.*)")
_re_ul = re.compile(r"^[-*+]\s")
_re_ol = re.compile(r"^(\d+)[.)]\s")

BLOCK_RULES: List[BlockRule] = sorted([
    BlockRule(10, "heading",     lambda s: _re_head.match(s),   _render_heading),
    BlockRule(20, "hr",          lambda s: _re_hr.match(s),     _render_hr),
    BlockRule(30, "task",        lambda s: _re_task.match(s),   _render_task),
    BlockRule(40, "ul",          lambda s: _re_ul.match(s),     _render_ul),
    BlockRule(50, "ol",          lambda s: _re_ol.match(s),     _render_ol),
    BlockRule(60, "blockquote",  lambda s: s.startswith(">"),   _render_blockquote),
    BlockRule(70, "table",       lambda s: (s.startswith("|") or s.endswith("|")) and s.count("|") >= 2, _render_table_row),
    BlockRule(100, "paragraph",  lambda s: True,                _render_paragraph),
], key=lambda r: r.priority)


def render_line(line: str) -> str:
    """对单行文本应用块级规则表中的第一个匹配规则"""
    if not line.strip():
        return ""
    for rule in BLOCK_RULES:
        m = rule.match(line)
        if m:
            return rule.render(line, m)
    return InlineRules.render(line)


# ═══════════════════════════════════════════════════════════════
# 代码块渲染器
# ═══════════════════════════════════════════════════════════════

class CodeBlockRenderer:
    """渲染围栏代码块：语言标签 + 行号 + ANSI 着色"""

    @staticmethod
    def render(lang: str, body: str, width: int) -> str:
        color = _COLOR_MAP.get(
            _LANG_COLOR_MAP.get(lang.strip().lower(), "gray"), CB_LANG_GRAY)
        lines = []
        for i, raw in enumerate(body.split("\n"), 1):
            stripped = raw.rstrip()
            lines.append(f" {C_CODE_BG} {CB_LINE_NO}{i:>3} {R}{color}{stripped}{R}")
        label = lang.strip().lower() or "code"
        header = f"{CB_LANG_LABEL}  -- {label} --{R}"
        return header + "\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 流式 Markdown→ANSI 渲染器 (状态机 + list缓冲)
# ═══════════════════════════════════════════════════════════════

class StreamingRenderer:
    """
    字符级流式渲染器。
    状态机: NORMAL ↔ CODE_BLOCK
    """

    def __init__(self) -> None:
        self.width = _terminal_width()
        self._buf = io.StringIO()
        self._in_code = False
        self._code_lang = ""
        self._code_lines: List[str] = []
        self._normal_line_count = 0
        self._table_rows: List[str] = []       # 缓存原始行（非分隔行）
        self._table_has_separator = False      # 是否见过分隔行
        self._lock = threading.Lock()          # 并行工具线程可能并发 flush/feed

    def _buf_append(self, text: str) -> None:
        self._buf.write(text)

    def _buf_get_and_clear(self) -> str:
        result = self._buf.getvalue()
        self._buf.seek(0)
        self._buf.truncate(0)
        return result

    def _process_lines(self, raw: str,
                       handler: Callable[[str, str], bool]) -> bool:
        """逐完整行消费 raw。
        返回 True: handler 触发状态切换，未消费部分已放回 _buf_parts。
        返回 False: 全部完整行已消费，未完成行已放回 _buf_parts。
        """
        while "\n" in raw:
            line, rest = raw.split("\n", 1)
            if handler(line, rest):
                self._buf_append(rest)
                return True
            raw = rest
        self._buf_append(raw)
        return False

    def feed(self, chunk: str) -> None:
        """流式输入入口。状态切换时自动换 handler 继续消费剩余行。"""
        with self._lock:
            self._buf_append(chunk)
            raw = self._buf_get_and_clear()
            while raw:
                handler = self._on_code_line if self._in_code else self._on_normal_line
                if not self._process_lines(raw, handler):
                    break
                # 状态已切换，取出 handler 放回的剩余行继续处理
                raw = self._buf_get_and_clear()

    def _on_code_line(self, line: str, rest: str) -> bool:
        line = line.replace("\r", "")  # 剥离\r控制符：防终端回车覆盖行首显示
        stripped = line.strip()
        if not self._code_lines and stripped.startswith("```"):
            self._code_lang = stripped[3:].strip()
            return False
        if stripped == "```":
            self._flush_code_block()
            self._in_code = False
            # 不再放回rest，由feed()外层循环用新handler处理
            return True
        self._code_lines.append(line)
        return False

    def _on_normal_line(self, line: str, rest: str) -> bool:
        line = line.replace("\r", "")  # 剥离\r控制符：防终端回车覆盖行首显示
        stripped = line.strip()
        if stripped.startswith("```"):
            self._in_code = True
            self._code_lang = stripped[3:].strip()
            return True
        # 表格候选行：首尾有|且≥2个|。全部缓冲，flush时统一判断
        is_table = (stripped.startswith("|") or stripped.endswith("|")) and stripped.count("|") >= 2
        if is_table:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if _is_table_separator(cells):
                self._table_has_separator = True
            self._table_rows.append(stripped)
            return False
        # 非竖线行：先刷出缓冲；若缓冲为空则清标志位防跨表格污染
        if self._table_rows:
            self._flush_table()
        else:
            self._table_has_separator = False
        rendered = render_line(line)
        if not rendered:
            return False
        _stdout_write(rendered + "\n")
        self._normal_line_count += 1
        return False

    def _demote_to_paragraphs(self) -> None:
        """将缓冲的表格候选行降级为段落逐行输出，并清空表格状态。"""
        for line in self._table_rows:
            rendered = _render_paragraph(line, None)
            if rendered:
                _stdout_write(rendered + "\n")
        self._table_rows.clear()
        self._table_has_separator = False

    def _flush_table(self) -> None:
        if not self._table_rows:
            self._table_has_separator = False
            return
        if not self._table_has_separator:
            # 无分隔行 → 不是表格，降级为段落输出
            self._demote_to_paragraphs()
            return
        # 有分隔行 → 拆分表头和数据
        # 找出分隔行位置（第一个全由---组成的分隔行）
        sep_idx = -1
        for i, raw in enumerate(self._table_rows):
            cells = [c.strip() for c in raw.strip("|").split("|")]
            if _is_table_separator(cells):
                sep_idx = i
                break
        # 表头行=分隔行之前的行，数据行=分隔行之后的行
        header_rows = self._table_rows[:sep_idx] if sep_idx >= 0 else self._table_rows
        data_rows = self._table_rows[sep_idx + 1:] if sep_idx >= 0 else []

        # 从分隔行解析列对齐：:--- → left, :---: → center, ---: → right
        alignments: List[str] = []
        if sep_idx >= 0:
            sep_cells = [c.strip() for c in self._table_rows[sep_idx].strip("|").split("|")]
            for cell in sep_cells:
                l = cell.startswith(":")
                r = cell.endswith(":")
                if l and r:
                    alignments.append("center")
                elif r:
                    alignments.append("right")
                else:
                    alignments.append("left")
        # 无数据行 → 不是真正的表格（只有表头+分隔行或孤立分隔行）
        if not data_rows:
            # 无数据行 → 不是表格
            self._demote_to_paragraphs()
            return
        # 数据行全部为空 → 不算表格
        data_cells = [[c.strip() for c in line.strip("|").split("|")] for line in data_rows]
        if not any(any(c for c in row) for row in data_cells):
            # 数据行全空 → 不是表格
            self._demote_to_paragraphs()
            return

        # ── 计算列宽（带终端宽度限制） ──
        term_w = _terminal_width()
        # 表格可用宽度 = 终端宽度 - 4(缩进) - 2(安全余量，防终端 pending-wrap 折行破坏边框)
        table_avail = max(term_w - 6, 8)

        all_rows = header_rows + data_rows
        rows_cells = [[c.strip() for c in line.strip("|").split("|")] for line in all_rows]
        cols = max(len(row) for row in rows_cells)
        # 补齐短行
        for row in rows_cells:
            while len(row) < cols:
                row.append("")

        rendered = [[InlineRules.render(c) for c in row] for row in rows_cells]

        # 自然列宽（内容决定）
        natural_widths = [0] * cols
        for row in rendered:
            for i, c in enumerate(row):
                w = _display_width(c)
                if w > natural_widths[i]:
                    natural_widths[i] = w

        # 每列的边框+内边距开销：1(|) + 1(左空格) + 1(右空格) = 3
        col_overhead = 3 * cols + 1  # +1 是最右边的 |
        total_natural = sum(natural_widths) + col_overhead

        if total_natural <= table_avail:
            # ── 正常宽度：不折行 ──
            widths = natural_widths
            self._render_table_block(rendered, widths, cols, alignments=alignments)
        else:
            # ── 超宽：限制列宽 + 折行，保证整表不超出终端宽度 ──
            avail_for_content = table_avail - col_overhead
            # 连每列 2 字符都放不下 → 按列分块输出
            if avail_for_content < cols * 2:
                self._render_table_chunked(rendered, natural_widths, cols, table_avail, alignments=alignments)
            else:
                widths = _fit_widths(natural_widths, avail_for_content)
                # 折行渲染
                self._render_table_block(rendered, widths, cols, wrap=True, alignments=alignments)

        self._table_rows.clear()
        self._table_has_separator = False

    def _render_table_block(self, rendered: List[List[str]],
                            widths: List[int], cols: int,
                            wrap: bool = False,
                            alignments: Optional[List[str]] = None) -> None:
        """渲染一个表格块。

        Args:
            rendered: 已做InlineRules渲染的单元格文本
            widths: 各列宽度
            cols: 列数
            wrap: 是否对超宽单元格折行
            alignments: 各列对齐方式 ("left"/"center"/"right")，默认全左对齐
        """
        if alignments is None:
            alignments = ["left"] * cols

        # 如果需要折行，先对每个单元格做折行处理
        if wrap:
            wrapped: List[List[List[str]]] = []
            for row in rendered:
                wrapped_row: List[List[str]] = []
                for i, c in enumerate(row):
                    w = widths[i] if i < len(widths) else widths[-1]
                    lines = _wrap_cell(c, w)
                    wrapped_row.append(lines)
                wrapped.append(wrapped_row)
            # 渲染折行表格
            self._render_wrapped_table(wrapped, widths, cols, alignments)
        else:
            # 原逻辑：单行单元格（带对齐）
            sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
            border = f"    {MD_TABLE_BORDER}{sep}{R}"

            def _row(cells):
                parts = []
                for i, c in enumerate(cells):
                    dw = _display_width(c)
                    pad = widths[i] - dw
                    al = alignments[i] if i < len(alignments) else "left"
                    if al == "right":
                        parts.append(" " * (pad + 1) + c + " ")
                    elif al == "center":
                        lp = pad // 2
                        rp = pad - lp
                        parts.append(" " * (lp + 1) + c + " " * (rp + 1))
                    else:
                        parts.append(" " + c + " " * (pad + 1))
                return f"    {MD_TABLE_BORDER}|{R}{MD_TABLE_CONTENT}" + f"{MD_TABLE_BORDER}|{R}{MD_TABLE_CONTENT}".join(parts) + f"{MD_TABLE_BORDER}|{R}"

            parts = [border]
            for row in rendered:
                parts.append(_row(row))
                parts.append(border)
            _stdout_write("\n".join(parts) + "\n")

    def _render_wrapped_table(self, wrapped: List[List[List[str]]],
                              widths: List[int], cols: int,
                              alignments: Optional[List[str]] = None) -> None:
        """渲染折行表格。

        Args:
            wrapped: wrapped[row][col] = [line1, line2, ...] 折行后的文本
            widths: 各列宽度
            cols: 列数
            alignments: 各列对齐方式
        """
        if alignments is None:
            alignments = ["left"] * cols

        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        border = f"    {MD_TABLE_BORDER}{sep}{R}"

        def _pad_cell(text: str, width: int, al: str = "left") -> str:
            """按对齐方式填充到指定显示宽度"""
            dw = _display_width(text)
            pad = max(0, width - dw)
            if al == "right":
                return " " * (pad + 1) + text + " "
            elif al == "center":
                lp = pad // 2
                rp = pad - lp
                return " " * (lp + 1) + text + " " * (rp + 1)
            else:
                return " " + text + " " * (pad + 1)

        parts: List[str] = []
        for row in wrapped:
            parts.append(border)
            # 该行中单元格的最大折行数
            max_lines = max(len(cell) for cell in row)
            for line_idx in range(max_lines):
                cell_parts: List[str] = []
                for col_idx in range(cols):
                    cell = row[col_idx] if col_idx < len(row) else [""]
                    text = cell[line_idx] if line_idx < len(cell) else ""
                    w = widths[col_idx] if col_idx < len(widths) else widths[-1]
                    al = alignments[col_idx] if col_idx < len(alignments) else "left"
                    cell_parts.append(f"{MD_TABLE_BORDER}|{R}{MD_TABLE_CONTENT}" + _pad_cell(text, w, al))
                parts.append(f"    " + "".join(cell_parts) + f"{MD_TABLE_BORDER}|{R}")
        parts.append(border)
        _stdout_write("\n".join(parts) + "\n")

    def _render_table_chunked(self, rendered: List[List[str]],
                              natural_widths: List[int],
                              cols: int, table_avail: int,
                              alignments: Optional[List[str]] = None) -> None:
        """列数过多、放不下时按列分块输出，首列作为锚点重复。

        每个分块都是完整的子表（锚点列 + 若干附加列），
        块内列宽用 _fit_widths 保证子表总宽不超过 table_avail，
        避免输出行超出终端宽度被强制折行、破坏边框。

        Args:
            rendered: 已做 InlineRules 渲染的单元格
            natural_widths: 各列自然宽度
            cols: 总列数
            table_avail: 表格可用宽度（含边框）
            alignments: 各列对齐方式
        """
        if alignments is None:
            alignments = ["left"] * cols
        if cols <= 1:
            # 单列表：直接按可用宽度压缩渲染
            avail_for_content = max(1, table_avail - 4)
            widths = _fit_widths([natural_widths[0]], avail_for_content)
            self._render_table_block(rendered, widths, 1, wrap=True, alignments=alignments[:1])
            return

        # 每列开销：3（| + 两侧空格）；表额外开销：最右 | = 1
        per_col_overhead = 3
        # 单列子表至少需要的宽度：min_col(2) + 3 + 1 = 6
        min_single_col_total = 2 + per_col_overhead + 1
        if table_avail < min_single_col_total:
            # 终端太窄，连一列表格都画不下 → 降级为段落输出
            self._demote_to_paragraphs()
            return

        # 分块：第0列(锚点) + 每块若干附加列；放不下锚点的列单独成块
        def _min_total(n: int) -> int:
            """n 列子表的最小总宽：每列 2 内容 + 3 边框内边距 + 1 右框"""
            return 2 * n + per_col_overhead * n + 1

        data_col_indices = list(range(1, cols))
        chunks: List[List[int]] = []
        i = 0
        while i < len(data_col_indices):
            chunk = [0]
            # 贪心加入附加列：保证块内每列最少 2 字符宽
            while i < len(data_col_indices):
                trial = chunk + [data_col_indices[i]]
                if _min_total(len(trial)) > table_avail:
                    break
                chunk = trial
                i += 1
            if len(chunk) == 1:
                # 锚点旁放不下任何列 → 该列单独输出（无锚点）
                chunks.append([data_col_indices[i]])
                i += 1
            else:
                chunks.append(chunk)

        total_chunks = len(chunks)

        for chunk_idx, col_indices in enumerate(chunks):
            # 提取子表数据
            sub_rendered = [[row[c] if c < len(row) else "" for c in col_indices]
                            for row in rendered]
            sub_alignments = [alignments[c] if c < len(alignments) else "left"
                              for c in col_indices]
            sub_natural = [natural_widths[c] if c < len(natural_widths) else 2
                           for c in col_indices]

            # 子表列宽：保证 sum + 开销 ≤ table_avail
            sub_cols = len(col_indices)
            avail_for_content = table_avail - (per_col_overhead * sub_cols + 1)
            sub_widths = _fit_widths(sub_natural, max(sub_cols * 1, avail_for_content))
            self._render_table_block(sub_rendered, sub_widths, sub_cols,
                                     wrap=True, alignments=sub_alignments)

            # 分块标签
            if total_chunks > 1:
                _stdout_write(f"    {D}── 表格 ({chunk_idx + 1}/{total_chunks}) ──{R}\n")

    def _flush_code_block(self) -> None:
        if self._code_lines:
            rendered = CodeBlockRenderer.render(
                self._code_lang,
                "\n".join(self._code_lines),
                self.width)
            _stdout_write(rendered + "\n")
        self._code_lines.clear()
        self._code_lang = ""

    def flush(self, final: bool = False) -> None:
        """消费缓冲区残留内容。

        Args:
            final: True 表示输出已结束（如整轮回复完成），强制渲染残留表格；
                   False 表示中途 flush（如工具执行前），此时保留未完成的表格行，
                   避免把一张表格拆成两半渲染导致边框错乱。
        """
        with self._lock:
            self._flush_locked(final)

    def _flush_locked(self, final: bool) -> None:
        """flush 的锁内实现（调用方需持有 _lock）。"""
        # 缓冲区里最后一行可能不完整（流式输出最后一行常不带 \n）。
        # 非最终 flush 时：普通文字半行应立即渲染（AI 前置说明先于工具调度摘要显示），
        # 仅表格候选行/代码块内容保留缓冲——工具调用恰好在表格行中间插入时，
        # 半行被当作完整表格行缓存会导致表格错位（行被拆成两半）。
        remaining = self._buf_get_and_clear()
        if remaining.strip():
            if final:
                self._on_normal_line(remaining, "")
                leftover = self._buf_get_and_clear()
                if leftover.strip():
                    _stdout_write(render_line(leftover) + "\n")
            else:
                stripped = remaining.strip()
                is_table_row = (
                    (stripped.startswith("|") or stripped.endswith("|"))
                    and stripped.count("|") >= 2
                )
                if self._in_code or is_table_row or stripped.startswith("```"):
                    self._buf_append(remaining)
                else:
                    self._on_normal_line(remaining, "")
                    leftover = self._buf_get_and_clear()
                    if leftover.strip():
                        _stdout_write(render_line(leftover) + "\n")
        elif final:
            leftover = self._buf_get_and_clear()
            if leftover.strip():
                _stdout_write(render_line(leftover) + "\n")
        if self._in_code:
            if final:
                self._flush_code_block()
                self._in_code = False
            # 非最终 flush：代码块保留缓冲，等结束标记到齐后整体渲染，
            # 避免工具调用把代码块拆散（半行保留在 _buf 会破坏代码状态机）。
        if self._table_rows:
            if final:
                # 最终强制渲染：缓冲的表格行（可能不完整）按现有规则输出
                self._flush_table()
            # 非 final：保留缓冲。表格行只有遇到非竖线行（触发 _flush_table）
            # 或最终 flush 才会渲染，避免中途 flush 把一张表格拆成两半。

    def reset(self) -> None:
        """清空全部缓冲与状态（响应流中断自动重试前调用）。

        服务端中途断开时，本轮已 feed 的半行文字/表格行/代码块残留在状态机中；
        重试流会从开头重播同一内容，若不清理会导致：
        - 半行文字与重播内容拼接重复（"我先了" + "我先了解一下…"）
        - 表格表头/数据行重复渲染
        - 未闭合的代码块把重试提示与内容全部吞掉
        """
        with self._lock:
            self._buf.seek(0)
            self._buf.truncate(0)
            self._in_code = False
            self._code_lang = ""
            self._code_lines.clear()
            self._table_rows.clear()
            self._table_has_separator = False
            self._normal_line_count = 0
