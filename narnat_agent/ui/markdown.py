"""Markdown 解析与着色 —— 行内替换流水线、块级规则表、代码块与差异着色。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「流式 Markdown 增量渲染」：块级形态按固定优先级取第一个匹配（标题 10 → 分隔线
  20 → 任务项 30 → 无序列表 40 → 有序列表 50 → 引用 60 → 表格行 70 → 段落 100
  兜底），正文经行内规则着色；行内元素按固定顺序一次替换到位（删除线 → 粗体 →
  斜体 → 行内代码 → 图片 → 链接），方括号识别不作用于已生成的样式序列内部；
- 「围栏代码块渲染」：语言标签行、行号宽 3 右对齐、行尾空白剥除、7 组语言配色
  （未识别按灰）；
- 「表格稳定渲染」：单元格拆分（反斜杠转义竖线还原为竖线）、分隔行判定、
  列宽压缩拟合。

结构（design D7/D12）：解析（`split_cells` / `is_table_separator` / 规则表）与
着色（`MarkdownStyler`）分离；颜色一律经构造注入的 `Theme` 取用，模块级只保留
不可变数据表（语言分组、正则），不缓存任何颜色实例或运行时开关。
"""
from __future__ import annotations

import re
from typing import Callable, List, Match, Optional

from ..output import Console, Theme
from .width import terminal_width

__all__ = [
    "GROUP_COLOR_ATTRS",
    "LANG_GROUPS",
    "LANG_GROUP_OF",
    "RE_CELL_SPLIT",
    "RE_HEAD",
    "RE_HR",
    "RE_OL",
    "RE_TABLE_SEP",
    "RE_TASK",
    "RE_UL",
    "BlockRule",
    "CodeBlockRenderer",
    "InlineRules",
    "MarkdownStyler",
    "colorize_diff",
    "fit_widths",
    "is_table_separator",
    "split_cells",
]

# 表格识别与拆分
RE_TABLE_SEP = re.compile(r"^[-:]+$")

# 表格单元格拆分：| 前面不是反斜杠的 | 才是列分隔符（支持转义竖线 \| 作为单元格内容）
RE_CELL_SPLIT = re.compile(r"(?<!\\)\|")

# 块级 Markdown 匹配
RE_HEAD = re.compile(r"^(#{1,6})\s+(.+)")
RE_HR = re.compile(r"^[-*_]{3,}\s*$")
RE_TASK = re.compile(r"^[-*+]\s+\[([ xX])\]\s*(.*)")
RE_UL = re.compile(r"^[-*+]\s")
RE_OL = re.compile(r"^(\d+)[.)]\s")

# 语言→颜色分组（数据驱动，O(1) 查找）；未识别的语言按 gray
LANG_GROUPS = [
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

# 分组 → 主题颜色属性名（取色时经注入的 Theme 动态解析，不缓存颜色实例）
GROUP_COLOR_ATTRS = {
    "cyan": "cb_lang_cyan",
    "yellow": "cb_lang_yellow",
    "green": "cb_lang_green",
    "blue": "cb_lang_blue",
    "magenta": "cb_lang_magenta",
    "red": "cb_lang_red",
    "gray": "cb_lang_gray",
}

# 语言名 → 分组名
LANG_GROUP_OF = {lang: group for group, langs in LANG_GROUPS for lang in langs}


def split_cells(raw: str) -> List[str]:
    """按列分隔符拆分表格行，转义竖线（反斜杠+竖线）作为单元格内容保留。"""
    return [c.strip().replace("\\|", "|") for c in RE_CELL_SPLIT.split(raw.strip("|"))]


def is_table_separator(cells: List[str]) -> bool:
    """判定一行是否为表格分隔行（各非空单元格仅由 `-` 与 `:` 组成）。"""
    if not any(c for c in cells):
        return False
    return all(RE_TABLE_SEP.match(c) for c in cells if c)


def fit_widths(natural: List[int], avail: int, min_col: int = 2) -> List[int]:
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
    return widths


class InlineRules:
    """行内元素替换流水线：输入原始文本 → 依次应用替换规则 → 输出 ANSI 文本。

    规则顺序体现 Markdown 优先级：删除线 > 粗体 > 斜体 > 行内代码 > 图片 > 链接；
    替换互不回溯（一次 sub 到位）。链接正则带 `(?<!\x1b)` 前置守卫，
    避免匹配已生成样式序列中的 `[`（方括号识别不作用于样式序列内部）。
    """

    RE_STRIKE = re.compile(r"~~(.+?)~~")
    RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
    RE_ITALIC = re.compile(r"\*(.+?)\*")
    RE_CODE = re.compile(r"`([^`]+)`")
    RE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
    # 链接：[ 前面不能是 \x1b（避免匹配 ANSI 转义序列中的 [）
    RE_LINK = re.compile(r"(?<!\x1b)\[([^\]]+)\]\([^)]+\)")

    def __init__(self, theme: Theme):
        self._theme = theme

    def render(self, text: str) -> str:
        """应用全部行内替换（颜色求值实时读注入主题的当前值与纯文本模式）。"""
        theme = self._theme
        text = self.RE_STRIKE.sub(f"{theme.md_strike}\\1{theme.rst}", text)
        text = self.RE_BOLD.sub(f"{theme.md_bold}\\1{theme.rst}", text)
        text = self.RE_ITALIC.sub(f"{theme.md_italic}\\1{theme.rst}", text)
        text = self.RE_CODE.sub(f"{theme.md_code}\\1{theme.rst}", text)
        text = self.RE_IMAGE.sub(f"{theme.md_image}\\1{theme.rst}", text)
        text = self.RE_LINK.sub(f"{theme.md_link}\\1{theme.rst}", text)
        return text


class BlockRule:
    """块级渲染规则：封装 (优先级, 名称, 匹配函数, 渲染函数)，优先级越低越先匹配。"""

    __slots__ = ('priority', 'name', 'match', 'render')

    def __init__(self, priority: int, name: str,
                 match: Callable[[str], Optional[Match]],
                 render: Callable[[str, Optional[Match]], str]) -> None:
        self.priority = priority
        self.name = name
        self.match = match
        self.render = render


class CodeBlockRenderer:
    """渲染围栏代码块：语言标签 + 行号 + ANSI 着色。"""

    def __init__(self, theme: Theme):
        self._theme = theme

    def render(self, lang: str, body: str) -> str:
        """整块成文：标签行 `  -- 语言 --`（空语言显示 code）+ 每行 ` 行号 内容`。"""
        theme = self._theme
        group = LANG_GROUP_OF.get(lang.strip().lower(), "gray")
        color = getattr(theme, GROUP_COLOR_ATTRS.get(group, "cb_lang_gray"))
        lines = []
        for i, raw in enumerate(body.split("\n"), 1):
            stripped = raw.rstrip()
            lines.append(f" {theme.c_code_bg} {theme.cb_line_no}{i:>3} {theme.rst}{color}{stripped}{theme.rst}")
        label = lang.strip().lower() or "code"
        header = f"{theme.cb_lang_label}  -- {label} --{theme.rst}"
        return header + "\n" + "\n".join(lines)


def colorize_diff(diff_text: str, theme: Theme) -> str:
    """对 unified diff 文本添加 ANSI 颜色（空文本与 `[无差异]` 均显示无差异）。"""
    if not diff_text or diff_text == "[无差异]":
        return f"{theme.c_secondary}[无差异]{theme.rst}"
    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{theme.diff_header}{line}{theme.rst}")
        elif line.startswith("@@"):
            out.append(f"{theme.diff_range}{line}{theme.rst}")
        elif line.startswith("-"):
            out.append(f"{theme.diff_removed}{line}{theme.rst}")
        elif line.startswith("+"):
            out.append(f"{theme.diff_added}{line}{theme.rst}")
        else:
            out.append(f"{theme.diff_context}{line}{theme.rst}")
    return "\n".join(out)


class MarkdownStyler:
    """Markdown 着色层：块级形态路由、行内替换、代码块、差异与分隔线。

    颜色全部经构造注入的 `Theme` 实时取用（`apply_style` 之后立即生效），
    输出经注入的 `Console` 写出（纯文本模式下结构保留、颜色为空）。
    """

    def __init__(self, theme: Theme, console: Console):
        self._theme = theme
        self._console = console
        self._inline = InlineRules(theme)
        self._code = CodeBlockRenderer(theme)
        self._rules: List[BlockRule] = [
            BlockRule(10, "heading", lambda s: RE_HEAD.match(s), self._render_heading),
            BlockRule(20, "hr", lambda s: RE_HR.match(s), self._render_hr),
            BlockRule(30, "task", lambda s: RE_TASK.match(s), self._render_task),
            BlockRule(40, "ul", lambda s: RE_UL.match(s), self._render_ul),
            BlockRule(50, "ol", lambda s: RE_OL.match(s), self._render_ol),
            BlockRule(60, "blockquote", lambda s: s.startswith(">"), self._render_blockquote),
            BlockRule(70, "table",
                      lambda s: s.startswith("|") and s.endswith("|") and s.count("|") >= 2,
                      self._render_table_row),
            BlockRule(100, "paragraph", lambda _s: True, lambda line, _m: self.paragraph(line)),
        ]

    # ── 行内 / 代码块 / 差异 / 分隔线 ──

    def inline(self, text: str) -> str:
        """行内元素着色（删除线 → 粗体 → 斜体 → 行内代码 → 图片 → 链接）。"""
        return self._inline.render(text)

    def code_block(self, lang: str, body: str) -> str:
        """围栏代码块整块渲染（语言标签 + 行号 + 语言配色）。"""
        return self._code.render(lang, body)

    def diff(self, diff_text: str) -> str:
        """unified diff 着色。"""
        return colorize_diff(diff_text, self._theme)

    def separator(self) -> None:
        """输出分隔线：两空格缩进 + 终端宽度减 2 个 `─`（分隔线色）。"""
        width = terminal_width() - 2
        self._console.write(
            f"  {self._theme.ui_separator}{'─' * width}{self._theme.rst}\n"
        )

    # ── 块级 ──

    def render_line(self, line: str) -> str:
        """对单行文本应用块级规则表中的第一个匹配规则（空白行输出空串）。"""
        if not line.strip():
            return ""
        for rule in self._rules:
            m = rule.match(line)
            if m:
                return rule.render(line, m)
        return self.inline(line)

    def paragraph(self, line: str) -> str:
        """普通段落：两空格缩进 + 行内着色（主色）。"""
        return f"  {self._theme.c_primary}{self.inline(line)}{self._theme.rst}"

    def _render_heading(self, _line: str, m: Optional[Match]) -> str:
        theme = self._theme
        level = len(m.group(1))
        body = self.inline(m.group(2))
        if level <= 2:
            return f"  {theme.md_h1}{body}{theme.rst}"
        if level == 3:
            return f"  {theme.md_h3}{body}{theme.rst}"
        return f"  {theme.md_h4}{body}{theme.rst}"

    def _render_hr(self, line: str, _m: Optional[Match]) -> str:
        return f"  {self._theme.md_hr}{line.strip()}{self._theme.rst}"

    def _render_task(self, _line: str, m: Optional[Match]) -> str:
        theme = self._theme
        done = m.group(1).lower() == "x"
        marker = f"{theme.md_task_done}v{theme.rst}" if done else f"{theme.md_task_undone}o{theme.rst}"
        return f"   {marker} {theme.c_primary}{self.inline(m.group(2))}{theme.rst}"

    def _render_ul(self, line: str, _m: Optional[Match]) -> str:
        theme = self._theme
        return f"   {theme.md_ul}*{theme.rst} {theme.c_primary}{self.inline(line[2:])}{theme.rst}"

    def _render_ol(self, line: str, m: Optional[Match]) -> str:
        theme = self._theme
        num = m.group(1)
        body_start = len(num) + 2
        return (f"   {theme.md_ol}{num}.{theme.rst} "
                f"{theme.c_primary}{self.inline(line[body_start:])}{theme.rst}")

    def _render_blockquote(self, line: str, _m: Optional[Match]) -> str:
        theme = self._theme
        depth = 0
        rest = line
        while rest.startswith(">"):
            rest = rest[1:]
            depth += 1
        body = rest.strip()
        return (f"  {theme.md_blockquote}{'| ' * depth}{theme.rst}"
                f"{theme.c_primary}{self.inline(body)}{theme.rst}")

    def _render_table_row(self, line: str, _m: Optional[Match]) -> str:
        theme = self._theme
        cells = split_cells(line)
        if is_table_separator(cells):
            return ""
        return (f"    {theme.md_table_content}"
                + " | ".join(self.inline(c) for c in cells)
                + f"{theme.rst}")
