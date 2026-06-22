"""
流式 Markdown→ANSI 渲染器

包含：
- 语言→颜色映射表
- InlineRules: 行内元素正则替换流水线
- BlockRule: 块级元素策略表
- CodeBlockRenderer: 代码块渲染
- StreamingRenderer: 流式状态机
"""

import re
import shutil
import unicodedata
from typing import Optional, Callable, List, Match

from .colors import (
    R, B, D, G, C, E, Y, X, U, M, O, BG,
    W7, BLU, CYN, GRN, GRY, YLW, RED,
    _stdout_write,
)


# ═══════════════════════════════════════════════════════════════
# 终端宽度与显示宽度
# ═══════════════════════════════════════════════════════════════

def _terminal_width() -> int:
    try:
        return min(shutil.get_terminal_size().columns, 160)
    except Exception:
        return 120


_re_ansi = re.compile(r"\x1b\[[0-9;]*m")


def _display_width(text: str) -> int:
    """终端显示宽度：CJK字符=2列，跳过ANSI转义序列。"""
    text = _re_ansi.sub("", text)
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


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
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

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
    if not diff_text or diff_text == "(无差异)":
        return f"{G}(无差异){R}"
    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{B}{C}{line}{R}")
        elif line.startswith("@@"):
            out.append(f"{D}{C}{line}{R}")
        elif line.startswith("-"):
            out.append(f"{X}{line}{R}")
        elif line.startswith("+"):
            out.append(f"{E}{line}{R}")
        else:
            out.append(f"{G}{line}{R}")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════
# 分隔线
# ═══════════════════════════════════════════════════════════════

def _sep() -> None:
    _stdout_write(f"  {G}{'─' * (_terminal_width() - 2)}{R}\n")


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
_COLOR_MAP = {"cyan": C, "yellow": Y, "green": E, "blue": U,
              "magenta": M, "red": X, "gray": G}
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
        text = cls._RE_STRIKE.sub(f"{X}\\1{R}", text)
        text = cls._RE_BOLD.sub(f"{B}{W7}\\1{R}", text)
        text = cls._RE_ITAL.sub(f"{D}{W7}\\1{R}", text)
        text = cls._RE_CODE.sub(f"{Y}\\1{R}", text)
        text = cls._RE_IMG.sub(f"{D}[img:\\1]{R}", text)
        text = cls._RE_LINK.sub(f"{U}\\1{R}", text)
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
        return f"  {B}{CYN}{body}{R}"
    if level == 3:
        return f"  {B}{GRN}{body}{R}"
    return f"  {B}{W7}{body}{R}"


def _render_hr(_line: str, _m: Match) -> str:
    return f"  {G}{_line.strip()}{R}"


def _render_task(_line: str, m: Match) -> str:
    done = m.group(1).lower() == "x"
    marker = f"{E}v{R}" if done else f"{G}o{R}"
    return f"   {marker} {W7}{InlineRules.render(m.group(2))}{R}"


def _render_ul(_line: str, _m: Match) -> str:
    return f"   {E}*{R} {W7}{InlineRules.render(_line[2:])}{R}"


def _render_ol(_line: str, m: Match) -> str:
    num = m.group(1)
    body_start = len(num) + 2
    return f"   {G}{num}.{R} {W7}{InlineRules.render(_line[body_start:])}{R}"


def _render_blockquote(_line: str, _m: Match) -> str:
    depth = 0
    rest = _line
    while rest.startswith(">"):
        rest = rest[1:]
        depth += 1
    body = rest.strip()
    return f"  {D}{G}{'| ' * depth}{R}{W7}{InlineRules.render(body)}{R}"


def _render_table_row(_line: str, _m: Match) -> str:
    cells = [c.strip() for c in _line.strip("|").split("|")]
    if _is_table_separator(cells):
        return ""
    return f"    {W7}" + " | ".join(InlineRules.render(c) for c in cells) + f"{R}"


def _render_paragraph(_line: str, _m: Match) -> str:
    return f"  {W7}{InlineRules.render(_line)}{R}"


_RE_TABLE_SEP = re.compile(r"^[-:]+$")


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
            _LANG_COLOR_MAP.get(lang.strip().lower(), "gray"), G)
        lines = []
        for i, raw in enumerate(body.split("\n"), 1):
            stripped = raw.rstrip()
            lines.append(f" {BG} {G}{i:>3} {R}{color}{stripped}{R}")
        label = lang.strip().lower() or "code"
        header = f"{G}{BG}  -- {label} --{R}"
        return header + "\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 流式 Markdown→ANSI 渲染器 (状态机 + list缓冲)
# ═══════════════════════════════════════════════════════════════

class StreamingRenderer:
    """
    字符级流式渲染器。
    状态机: NORMAL ↔ CODE_BLOCK
    _process_lines: 通用行消费者，消除 _feed_code/_feed_normal 的重复循环骨架。
    """

    def __init__(self) -> None:
        self.width = _terminal_width()
        self._buf_parts: List[str] = []
        self._in_code = False
        self._code_lang = ""
        self._code_lines: List[str] = []
        self._normal_line_count = 0
        self._table_rows: List[str] = []       # 缓存原始行（非分隔行）
        self._table_has_separator = False      # 是否见过分隔行

    def _buf_append(self, text: str) -> None:
        self._buf_parts.append(text)

    def _buf_get_and_clear(self) -> str:
        result = "".join(self._buf_parts)
        self._buf_parts.clear()
        return result

    def _process_lines(self, chunk: str,
                       handler: Callable[[str, str], bool]) -> None:
        """
        通用行消费者：
        - 累积 chunk 到缓冲区，逐完整行调用 handler(line, rest)。
        - handler 返回 True: 终止消费（handler 已自行处理 rest）。
        - handler 返回 False: 继续消费下一行。
        """
        self._buf_append(chunk)
        raw = self._buf_get_and_clear()
        while "\n" in raw:
            line, rest = raw.split("\n", 1)
            if handler(line, rest):
                return
            raw = rest + self._buf_get_and_clear()
        self._buf_append(raw)

    def feed(self, chunk: str) -> None:
        # 循环处理：handler可能切换状态（_in_code），
        # 切换后需要用新handler继续处理剩余内容
        self._buf_append(chunk)
        raw = self._buf_get_and_clear()
        while raw:
            handler = self._on_code_line if self._in_code else self._on_normal_line
            # 逐行处理
            while "\n" in raw:
                line, rest = raw.split("\n", 1)
                if handler(line, rest):
                    # handler返回True：状态已切换，用新handler继续处理rest
                    raw = rest
                    break
                raw = rest
            else:
                # while正常结束（无更多完整行），剩余放回缓冲区
                self._buf_append(raw)
                return
            # handler返回True后，raw=rest，继续外层while用新handler处理
            # 但先合并缓冲区中可能新追加的内容
            extra = self._buf_get_and_clear()
            if extra:
                raw = raw + extra

    def _on_code_line(self, line: str, rest: str) -> bool:
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

    def _flush_table(self) -> None:
        if not self._table_rows:
            self._table_has_separator = False
            return
        if not self._table_has_separator:
            # 无分隔行 → 不是表格，逐行按段落输出
            for line in self._table_rows:
                rendered = _render_paragraph(line, None)
                if rendered:
                    _stdout_write(rendered + "\n")
            self._table_rows.clear()
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
        # 无数据行 → 不是真正的表格（只有表头+分隔行或孤立分隔行）
        if not data_rows:
            for line in self._table_rows:
                rendered = _render_paragraph(line, None)
                if rendered:
                    _stdout_write(rendered + "\n")
            self._table_rows.clear()
            self._table_has_separator = False
            return
        # 数据行全部为空 → 不算表格
        data_cells = [[c.strip() for c in line.strip("|").split("|")] for line in data_rows]
        if not any(any(c for c in row) for row in data_cells):
            for line in self._table_rows:
                rendered = _render_paragraph(line, None)
                if rendered:
                    _stdout_write(rendered + "\n")
            self._table_rows.clear()
            self._table_has_separator = False
            return

        # ── 计算列宽（带终端宽度限制） ──
        term_w = _terminal_width()
        # 表格可用宽度 = 终端宽度 - 4(缩进) - 1(左边框) - 1(右边框)
        table_avail = max(term_w - 6, 20)

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
            # ── 正常宽度：原逻辑，不折行 ──
            widths = natural_widths
            self._render_table_block(rendered, widths, cols)
        else:
            # ── 超宽：需要限制列宽+折行 ──
            # 策略：给每列设上限，均等分配可用宽度
            # 优先保证每列至少6字符（3个中文字符），剩余按自然宽度比例分配
            min_col = 6
            avail_for_content = table_avail - col_overhead
            # 如果连最小宽度都放不下，减少列数（分块）
            if cols * min_col > avail_for_content:
                # 分块：计算每块能放多少列
                self._render_table_chunked(rendered, cols, table_avail, col_overhead, min_col)
            else:
                # 限制列宽：先保证每列min_col，剩余按自然宽度比例分配
                widths = [min_col] * cols
                remaining = avail_for_content - cols * min_col
                if remaining > 0 and sum(natural_widths) > 0:
                    for i in range(cols):
                        ratio = natural_widths[i] / sum(natural_widths)
                        widths[i] += int(remaining * ratio)
                    # 修正舍入误差
                    diff = avail_for_content - sum(widths)
                    if diff > 0:
                        for i in range(diff):
                            widths[i % cols] += 1

                # 折行渲染
                self._render_table_block(rendered, widths, cols, wrap=True)

        self._table_rows.clear()
        self._table_has_separator = False

    def _render_table_block(self, rendered: List[List[str]],
                            widths: List[int], cols: int,
                            wrap: bool = False) -> None:
        """渲染一个表格块。

        Args:
            rendered: 已做InlineRules渲染的单元格文本
            widths: 各列宽度
            cols: 列数
            wrap: 是否对超宽单元格折行
        """
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
            self._render_wrapped_table(wrapped, widths, cols)
        else:
            # 原逻辑：单行单元格
            sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
            border = f"    {BLU}{sep}{R}"

            def _row(cells):
                parts = [" " + c + " " * (widths[i] - _display_width(c) + 1) for i, c in enumerate(cells)]
                return f"    {BLU}|{R}{W7}" + f"{BLU}|{R}{W7}".join(parts) + f"{BLU}|{R}"

            parts = [border]
            for row in rendered:
                parts.append(_row(row))
                parts.append(border)
            _stdout_write("\n".join(parts) + "\n")

    def _render_wrapped_table(self, wrapped: List[List[List[str]]],
                              widths: List[int], cols: int) -> None:
        """渲染折行表格。

        Args:
            wrapped: wrapped[row][col] = [line1, line2, ...] 折行后的文本
            widths: 各列宽度
            cols: 列数
        """
        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        border = f"    {BLU}{sep}{R}"

        def _pad_cell(text: str, width: int) -> str:
            """右填充到指定显示宽度"""
            dw = _display_width(text)
            pad = max(0, width - dw)
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
                    cell_parts.append(f"{BLU}|{R}{W7}" + _pad_cell(text, w))
                parts.append(f"    " + "".join(cell_parts) + f"{BLU}|{R}")
        parts.append(border)
        _stdout_write("\n".join(parts) + "\n")

    def _render_table_chunked(self, rendered: List[List[str]],
                              cols: int, table_avail: int,
                              col_overhead: int, min_col: int) -> None:
        """列数过多时按列分块输出，首列作为锚点重复。

        Args:
            rendered: 已渲染的单元格
            cols: 总列数
            table_avail: 表格可用宽度
            col_overhead: 列的边框+内边距总开销
            min_col: 每列最小宽度
        """
        # 每列开销：3 (| + 两边空格)
        per_col_overhead = 3
        # 锚点列(第0列)固定占用
        anchor_width = min_col
        # 每块可用宽度（减去锚点列）
        chunk_avail = table_avail - anchor_width - per_col_overhead - 1  # -1 右边框
        # 每块能放的附加列数
        cols_per_chunk = max(1, chunk_avail // (min_col + per_col_overhead))

        # 分块：第0列(锚点) + 每块cols_per_chunk个附加列
        data_col_indices = list(range(1, cols))
        chunks: List[List[int]] = []
        i = 0
        while i < len(data_col_indices):
            chunk = [0] + data_col_indices[i:i + cols_per_chunk]
            chunks.append(chunk)
            i += cols_per_chunk

        total_chunks = len(chunks)

        for chunk_idx, col_indices in enumerate(chunks):
            # 提取子表数据
            sub_rendered = []
            for row in rendered:
                sub_row = [row[c] if c < len(row) else "" for c in col_indices]
                sub_rendered.append(sub_row)

            # 计算子表列宽
            sub_cols = len(col_indices)
            sub_widths = [0] * sub_cols
            for row in sub_rendered:
                for i, c in enumerate(row):
                    w = _display_width(c)
                    if w > sub_widths[i]:
                        sub_widths[i] = w

            # 检查子表是否超宽，超宽则限制列宽
            sub_col_overhead = 3 * sub_cols + 1
            sub_total = sum(sub_widths) + sub_col_overhead
            if sub_total > table_avail:
                avail_for_content = table_avail - sub_col_overhead
                for i in range(sub_cols):
                    sub_widths[i] = min(sub_widths[i], max(min_col, avail_for_content // sub_cols))
                self._render_table_block(sub_rendered, sub_widths, sub_cols, wrap=True)
            else:
                self._render_table_block(sub_rendered, sub_widths, sub_cols, wrap=False)

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

    def flush(self) -> None:
        # 先消费缓冲区残留行（流式输出最后一行常不带 \n）
        remaining = self._buf_get_and_clear()
        if remaining.strip():
            self._on_normal_line(remaining, "")
        if self._in_code:
            self._flush_code_block()
            self._in_code = False
        if self._table_rows:
            self._flush_table()
            self._table_has_separator = False
        leftover = self._buf_get_and_clear()
        if leftover.strip():
            _stdout_write(render_line(leftover) + "\n")
