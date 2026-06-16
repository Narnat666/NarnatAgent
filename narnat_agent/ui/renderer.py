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
    _RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")

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
        # 合并表头和数据行计算列宽
        all_rows = header_rows + data_rows
        rows_cells = [[c.strip() for c in line.strip("|").split("|")] for line in all_rows]
        cols = max(len(row) for row in rows_cells)
        widths = [0] * cols
        rendered = [[InlineRules.render(c) for c in row] for row in rows_cells]
        for row in rendered:
            for i, c in enumerate(row):
                w = _display_width(c)
                if w > widths[i]:
                    widths[i] = w

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
        self._table_rows.clear()
        self._table_has_separator = False

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
