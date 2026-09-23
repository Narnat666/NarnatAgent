"""颜色体系 —— ANSI 序列生成、颜色容器、色板/角色定义表、配方解析。

契约来源：`openspec/changes/recast-v2/specs/output/spec.md`
- 「基础色板与角色引用」：`BASE_DEFS` 的 11 个基础色、`#RRGGBB` 登记、
  `_` 前缀键跳过、非法项静默跳过；
- 「派生角色与配方解析」：`DERIVED_DEFS` 的 43 个派生角色、`parse_recipe`
  空白分词与未知 token 静默忽略、空值/全空白解析为无样式；
- 「终端能力适配」：真彩 24 位序列与 `ANSI16` 最近邻降级；
- 「兼容性怪癖保持」：`bg:名字` 取该色的**前景**值（现状保持）、
  `Color` 调试表示恒为 ANSI 原文。

结构（design D1/D7）：纯定义 + 纯函数（无模块级可变状态）；`Color` 的求值
依赖构造注入的输出端口（plain 开关由实例承载，见 `console.Console`），
色板与角色的可变状态由 `style.Theme` 实例持有。
"""
from __future__ import annotations

from typing import Mapping, Protocol

__all__ = [
    "ANSI16",
    "BASE_DEFS",
    "DERIVED_DEFS",
    "STYLE_CONSTANTS",
    "STYLE_MAP",
    "Color",
    "ansi_color",
    "hex_to_ansi",
    "parse_recipe",
]


class PlainSource(Protocol):
    """plain 状态提供方（`Color` 求值时实时查询，非构造时快照）。"""

    def is_plain(self) -> bool:
        """是否为纯文本模式。"""
        ...


# ═══════════════════════════════════════════════════════════════
# 标准 ANSI 16 色调色板（xterm 兼容值）——非真彩终端的降级目标
# 每项：(前景码, 背景码, r, g, b)，降级按欧氏距离取最近邻
# ═══════════════════════════════════════════════════════════════

ANSI16 = [
    # 标准低亮色（30-37 / 40-47）
    ("\x1b[30m", "\x1b[40m", 0, 0, 0),
    ("\x1b[31m", "\x1b[41m", 205, 0, 0),
    ("\x1b[32m", "\x1b[42m", 0, 205, 0),
    ("\x1b[33m", "\x1b[43m", 205, 205, 0),
    ("\x1b[34m", "\x1b[44m", 0, 0, 238),
    ("\x1b[35m", "\x1b[45m", 205, 0, 205),
    ("\x1b[36m", "\x1b[46m", 0, 205, 205),
    ("\x1b[37m", "\x1b[47m", 229, 229, 229),
    # 标准高亮色（90-97 / 100-107）
    ("\x1b[90m", "\x1b[100m", 127, 127, 127),
    ("\x1b[91m", "\x1b[101m", 255, 0, 0),
    ("\x1b[92m", "\x1b[102m", 0, 255, 0),
    ("\x1b[93m", "\x1b[103m", 255, 255, 0),
    ("\x1b[94m", "\x1b[104m", 92, 92, 255),
    ("\x1b[95m", "\x1b[105m", 255, 0, 255),
    ("\x1b[96m", "\x1b[106m", 0, 255, 255),
    ("\x1b[97m", "\x1b[107m", 255, 255, 255),
]

# 配方样式词（不可配置）
STYLE_MAP = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "italic": "\x1b[3m",
    "underline": "\x1b[4m",
}

# 样式常量（不可配置）：复位 / 粗体 / 暗色
STYLE_CONSTANTS = (
    ("rst", "\x1b[0m"),
    ("bld", "\x1b[1m"),
    ("dim", "\x1b[2m"),
)

# 基础色板 —— 单一事实源 (name, hex, is_background)
# 对外契约：11 个内置色与默认十六进制值不得更改
BASE_DEFS = [
    ("primary",    "#FFFFD0", False),
    ("secondary",  "#64748B", False),
    ("user",       "#FFFFFF", False),
    ("accent",     "#5EEAD4", False),
    ("success",    "#34D399", False),
    ("warning",    "#FBBF24", False),
    ("error",      "#F87171", False),
    ("link",       "#A78BFA", False),
    ("decoration", "#E879F9", False),
    ("emphasis",   "#FB923C", False),
    ("code_bg",    "#0F172A", True),
]

BASE_NAMES = frozenset(name for name, _hex, _bg in BASE_DEFS)
BASE_BG = {name: bg for name, _hex, bg in BASE_DEFS}

# 派生角色 —— 单一事实源 (属性名, 配置键, 默认配方)
# 属性名为旧实现变量名的小写形式（如 MD_H1 → md_h1），供消费方按名取色
DERIVED_DEFS = [
    # ── 标注 markdown ──
    ("md_h1",            "markdown.heading_h1",     "bold accent"),
    ("md_h3",            "markdown.heading_h3",     "bold success"),
    ("md_h4",            "markdown.heading_h4",     "bold primary"),
    ("md_bold",          "markdown.bold",           "bold primary"),
    ("md_italic",        "markdown.italic",         "dim primary"),
    ("md_strike",        "markdown.strikethrough",  "error"),
    ("md_code",          "markdown.code_inline",    "warning"),
    ("md_link",          "markdown.link",           "link"),
    ("md_image",         "markdown.image",          "dim secondary"),
    ("md_blockquote",    "markdown.blockquote",     "dim secondary"),
    ("md_hr",            "markdown.hr",             "secondary"),
    ("md_ul",            "markdown.list_unordered", "success"),
    ("md_ol",            "markdown.list_ordered",   "secondary"),
    ("md_task_done",     "markdown.task_done",      "success"),
    ("md_task_undone",   "markdown.task_undone",    "secondary"),
    ("md_table_border",  "markdown.table_border",   "link"),
    ("md_table_content", "markdown.table_content",  "primary"),
    # ── 代码块 codeblock ──
    ("cb_line_no",       "codeblock.line_number",   "secondary"),
    ("cb_lang_label",    "codeblock.lang_label",    "secondary bg:code_bg"),
    ("cb_lang_cyan",     "codeblock.lang_cyan",     "#5EEAD4"),
    ("cb_lang_yellow",   "codeblock.lang_yellow",   "#FBBF24"),
    ("cb_lang_green",    "codeblock.lang_green",    "#34D399"),
    ("cb_lang_magenta",  "codeblock.lang_magenta",  "#E879F9"),
    ("cb_lang_red",      "codeblock.lang_red",      "#F87171"),
    ("cb_lang_blue",     "codeblock.lang_blue",     "#A78BFA"),
    ("cb_lang_gray",     "codeblock.lang_gray",     "#64748B"),
    # ── 差异 diff ──
    ("diff_header",      "diff.header",             "bold accent"),
    ("diff_range",       "diff.range",              "dim accent"),
    ("diff_added",       "diff.added",              "success"),
    ("diff_removed",     "diff.removed",            "error"),
    ("diff_context",     "diff.context",            "secondary"),
    # ── 框架 ui ──
    ("ui_header",           "ui.header",            "accent"),
    ("ui_spinner",          "ui.spinner",           "emphasis"),
    ("ui_interrupted",      "ui.interrupted",       "warning"),
    ("ui_interrupted_hint", "ui.interrupted_hint",  "secondary"),
    ("ui_stats_label",      "ui.stats_label",       "secondary"),
    ("ui_stats_value",      "ui.stats_value",       "warning"),
    ("ui_separator",        "ui.separator",         "secondary"),
    # ── 命令 cmd ──
    ("cmd_success",      "cmd.success",             "success"),
    ("cmd_error",        "cmd.error",               "error"),
    ("cmd_hint",         "cmd.hint",                "warning"),
    ("cmd_highlight",    "cmd.highlight",           "accent"),
    ("cmd_muted",        "cmd.muted",               "secondary"),
]

# 基础色短别名（旧实现 G/C/E/Y/X/U/M/O/BG/W/W7 与 GRY/CYN/... 的等价名）
BASE_ALIASES = [
    ("g", "c_secondary"), ("c", "c_accent"), ("e", "c_success"),
    ("y", "c_warning"), ("x", "c_error"), ("u", "c_link"),
    ("m", "c_decoration"), ("o", "c_emphasis"), ("bg", "c_code_bg"),
    ("w", "c_user"), ("w7", "c_primary"),
    ("gry", "c_secondary"), ("cyn", "c_accent"), ("grn", "c_success"),
    ("ylw", "c_warning"), ("red", "c_error"), ("blu", "c_link"),
    ("mag", "c_decoration"), ("org", "c_emphasis"), ("bg8", "c_code_bg"),
    ("wht", "c_user"), ("wht7", "c_primary"),
]

# prompt_toolkit 样式默认值（ptk Style 字符串，非 ANSI）
PTK_SYMBOL_DEFAULT = "bold #00ff00"
PTK_TEXT_DEFAULT = "#ffffff"
PTK_CUSTOM_DEFAULT = "#FFFFD0"

# 16 色限制说明：非真彩终端下 hex 被近似为最近邻 ANSI 码
HEX_RGB_RE = r"2;(\d+);(\d+);(\d+)"


class Color:
    """ANSI 颜色求值容器（旧实现 `_Color` 的等价物）。

    行为契约（spec「全局输出开关」「兼容性怪癖保持」）：
    - `str(color)`：plain 模式（实时查询注入的 console）下求值为空串，
      否则为 ANSI 原文——拼接出的颜色消失但渲染结构保留；
    - `repr(color)`：恒为 ANSI 原文，不受 plain 模式影响（调试表示）。
    """

    __slots__ = ("_value", "_console")

    def __init__(self, value: str, console: PlainSource):
        self._value = value
        self._console = console

    def __str__(self) -> str:
        return "" if self._console.is_plain() else self._value

    def __repr__(self) -> str:
        return self._value

    @property
    def value(self) -> str:
        """ANSI 原文字符串（供需要精确取值的消费方，如快照/调试）。"""
        return self._value

    def _set(self, value: str) -> None:
        """写入 ANSI 原文（仅主题装配使用，spec「主题应用时机与全局生效」）。"""
        self._value = value


def ansi_color(code: str, r: int, g: int, b: int, truecolor: bool = True) -> str:
    """按终端能力生成颜色序列。

    - 真彩：`ESC[38/48;2;r;g;bm`；
    - 非真彩：在 `ANSI16` 表中按欧氏距离取最近邻（`code == "48"` 选背景码，
      否则前景码）——spec「终端能力适配」「16 色降级」。
    """
    if truecolor:
        return f"\x1b[{code};2;{r};{g};{b}m"
    is_bg = code == "48"
    best = (None, float("inf"))
    for fg, bg, rr, gg, bb in ANSI16:
        dist = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if dist < best[1]:
            best = (bg if is_bg else fg, dist)
    return best[0]


def hex_to_ansi(hex_str: str, bg: bool = False, truecolor: bool = True) -> str:
    """`#RRGGBB`（允许省略 `#`）→ ANSI 序列。

    非法十六进制（长度不足/非 hex 字符）抛 `ValueError`——调用方
    （`Theme` 配置应用路径）按 spec「畸形配置静默容错」跳过该项。
    """
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return ansi_color("48" if bg else "38", r, g, b, truecolor=truecolor)


def parse_recipe(
    value: str, palette: Mapping[str, Color], truecolor: bool = True
) -> str:
    """配方字符串 → ANSI 序列（spec「派生角色与配方解析」）。

    token: `bold` | `dim` | `italic` | `underline` | 基础色名 | `#RRGGBB`
    | `bg:xxx`（xxx 为十六进制或基础色名）。

    - 空值/全空白 → 无样式（空串）；
    - 未知 token 静默忽略；
    - `bg:#RRGGBB` → 背景色；`bg:名字` → 取该色**前景**值（兼容怪癖，现状保持）；
    - 非法十六进制 token 静默跳过（畸形配置容错，不中断）。
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    parts: list[str] = []
    for tok in value.strip().split():
        if tok in STYLE_MAP:
            parts.append(STYLE_MAP[tok])
        elif tok.startswith("bg:"):
            v = tok[3:]
            try:
                if v.startswith("#"):
                    parts.append(hex_to_ansi(v, bg=True, truecolor=truecolor))
                else:
                    color = palette.get(v)
                    if color is not None:
                        parts.append(color.value)
            except (ValueError, IndexError):
                continue
        elif tok.startswith("#"):
            try:
                parts.append(hex_to_ansi(tok, bg=False, truecolor=truecolor))
            except (ValueError, IndexError):
                continue
        elif tok in palette:
            parts.append(palette[tok].value)
    return "".join(parts)
