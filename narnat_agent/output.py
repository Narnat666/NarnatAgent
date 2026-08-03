"""
终端输出抽象层 —— ANSI颜色常量 + 线程安全写入 + 主题系统

core/tools 依赖此模块，不依赖 ui。
ui 层也引用此模块的常量，保持颜色一致性。

主题系统:
  _BASE_DEFS     →  11 个基础语义色  →  C_* 实例
  _DERIVED_DEFS  →  40 个派生 token  →  MD_* / CB_* / DIFF_* / UI_* / CMD_*
  全部通过 narnat.json → "界面" 分组可配置
"""

import os
import sys
import threading

from .config.defaults import DEFAULT_CONTEXT_WINDOW


# ═══════════════════════════════════════════════════════════════
# stdout 线程安全写入
# ═══════════════════════════════════════════════════════════════

_stdout_lock = threading.Lock()


def write(text: str) -> None:
    with _stdout_lock:
        sys.stdout.write("\r" + text)
        sys.stdout.flush()


def try_write(text: str) -> bool:
    if _stdout_lock.acquire(blocking=False):
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
            return True
        finally:
            _stdout_lock.release()
    return False


# ═══════════════════════════════════════════════════════════════
# 终端颜色能力检测
# ═══════════════════════════════════════════════════════════════

def _supports_truecolor() -> bool:
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("ConEmuANSI") == "ON":
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if mode.value & 0x0004:
                    return True
        except Exception:
            pass
    return False


_TRUECOLOR = _supports_truecolor()

# ── 标准 ANSI 16 色调色板（xterm 兼容值），用于非 TrueColor 终端降级 ──
_ANSI16 = [
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


def _ansi_color(code: str, r: int, g: int, b: int) -> str:
    if _TRUECOLOR:
        return f"\x1b[{code};2;{r};{g};{b}m"
    # 非 TrueColor: 在标准 16 色调色板中找欧氏距离最近的颜色
    is_bg = code == "48"
    best = (None, float("inf"))
    for fg, bg, rr, gg, bb in _ANSI16:
        dist = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if dist < best[1]:
            best = (bg if is_bg else fg, dist)
    return best[0]


def _hex_to_ansi(hex_str: str, bg: bool = False) -> str:
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return _ansi_color("48" if bg else "38", r, g, b)


# ═══════════════════════════════════════════════════════════════
# _Color 可变容器（apply_style 时改 _value，全局生效）
# ═══════════════════════════════════════════════════════════════

class _Color:
    __slots__ = ('_value',)
    def __init__(self, value: str): self._value = value
    def __str__(self):  return self._value
    def __repr__(self): return self._value


# ═══════════════════════════════════════════════════════════════
# 样式常量（不可配置）
# ═══════════════════════════════════════════════════════════════

RST = _Color("\x1b[0m")
BLD = _Color("\x1b[1m")
DIM = _Color("\x1b[2m")
R, B, D = RST, BLD, DIM


# ═══════════════════════════════════════════════════════════════
# 基础色板 — 单一定义源  (name, hex, is_background)
# ═══════════════════════════════════════════════════════════════

_BASE_DEFS = [
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

# 生成 C_* 实例
for _name, _hex, _is_bg in _BASE_DEFS:
    globals()[f"C_{_name.upper()}"] = _Color(_hex_to_ansi(_hex, bg=_is_bg))

# 查找表（配方解析 + apply_style 用）
_BASE_COLORS: dict[str, _Color] = {n: globals()[f"C_{n.upper()}"] for n, _, _ in _BASE_DEFS}
_BG_LOOKUP = {n: bg for n, _, bg in _BASE_DEFS}
_BASE_HEX: dict[str, str] = {n: h for n, h, _ in _BASE_DEFS}


# ═══════════════════════════════════════════════════════════════
# 配方解析器
# ═══════════════════════════════════════════════════════════════

_STYLE_MAP = {"bold": "\x1b[1m", "dim": "\x1b[2m", "italic": "\x1b[3m", "underline": "\x1b[4m"}


def _parse_recipe(value: str) -> str:
    """将配方字符串解析为 ANSI 序列。

    token: bold | dim | italic | underline | 基础色名 | #RRGGBB | bg:xxx
    """
    if not value or not value.strip():
        return ""
    parts: list[str] = []
    for tok in value.strip().split():
        if tok in _STYLE_MAP:
            parts.append(_STYLE_MAP[tok])
        elif tok.startswith("bg:"):
            v = tok[3:]
            parts.append(_hex_to_ansi(v, bg=True) if v.startswith("#") else _BASE_COLORS.get(v, _Color(""))._value)
        elif tok.startswith("#"):
            parts.append(_hex_to_ansi(tok, bg=False))
        elif tok in _BASE_COLORS:
            parts.append(_BASE_COLORS[tok]._value)
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════
# 派生 token — 单一定义源  (变量名, 配方键, 默认配方)
# ═══════════════════════════════════════════════════════════════

_DERIVED_DEFS = [
    # ── Markdown ──
    ("MD_H1",            "markdown.heading_h1",     "bold accent"),
    ("MD_H3",            "markdown.heading_h3",     "bold success"),
    ("MD_H4",            "markdown.heading_h4",     "bold primary"),
    ("MD_BOLD",          "markdown.bold",           "bold primary"),
    ("MD_ITALIC",        "markdown.italic",         "dim primary"),
    ("MD_STRIKE",        "markdown.strikethrough",  "error"),
    ("MD_CODE",          "markdown.code_inline",    "warning"),
    ("MD_LINK",          "markdown.link",           "link"),
    ("MD_IMAGE",         "markdown.image",          "dim secondary"),
    ("MD_BLOCKQUOTE",    "markdown.blockquote",     "dim secondary"),
    ("MD_HR",            "markdown.hr",             "secondary"),
    ("MD_UL",            "markdown.list_unordered", "success"),
    ("MD_OL",            "markdown.list_ordered",   "secondary"),
    ("MD_TASK_DONE",     "markdown.task_done",      "success"),
    ("MD_TASK_UNDONE",   "markdown.task_undone",    "secondary"),
    ("MD_TABLE_BORDER",  "markdown.table_border",   "link"),
    ("MD_TABLE_CONTENT", "markdown.table_content",  "primary"),
    # ── 代码块 ──
    ("CB_LINE_NO",       "codeblock.line_number",   "secondary"),
    ("CB_LANG_LABEL",    "codeblock.lang_label",    "secondary bg:code_bg"),
    ("CB_LANG_CYAN",     "codeblock.lang_cyan",     "#5EEAD4"),
    ("CB_LANG_YELLOW",   "codeblock.lang_yellow",   "#FBBF24"),
    ("CB_LANG_GREEN",    "codeblock.lang_green",    "#34D399"),
    ("CB_LANG_MAGENTA",  "codeblock.lang_magenta",  "#E879F9"),
    ("CB_LANG_RED",      "codeblock.lang_red",      "#F87171"),
    ("CB_LANG_BLUE",     "codeblock.lang_blue",     "#A78BFA"),
    ("CB_LANG_GRAY",     "codeblock.lang_gray",     "#64748B"),
    # ── Diff ──
    ("DIFF_HEADER",      "diff.header",             "bold accent"),
    ("DIFF_RANGE",       "diff.range",              "dim accent"),
    ("DIFF_ADDED",       "diff.added",              "success"),
    ("DIFF_REMOVED",     "diff.removed",            "error"),
    ("DIFF_CONTEXT",     "diff.context",            "secondary"),
    # ── UI 框架 ──
    ("UI_HEADER",           "ui.header",            "accent"),
    ("UI_SPINNER",          "ui.spinner",           "emphasis"),
    ("UI_INTERRUPTED",      "ui.interrupted",       "warning"),
    ("UI_INTERRUPTED_HINT", "ui.interrupted_hint",  "secondary"),
    ("UI_STATS_LABEL",      "ui.stats_label",       "secondary"),
    ("UI_STATS_VALUE",      "ui.stats_value",       "warning"),
    ("UI_SEPARATOR",        "ui.separator",         "secondary"),
    # ── 命令输出 ──
    ("CMD_SUCCESS",     "cmd.success",             "success"),
    ("CMD_ERROR",       "cmd.error",               "error"),
    ("CMD_HINT",        "cmd.hint",                "warning"),
    ("CMD_HIGHLIGHT",   "cmd.highlight",           "accent"),
    ("CMD_MUTED",       "cmd.muted",               "secondary"),
]

# 生成 _Color 实例
_DERIVED: dict[str, _Color] = {}
for _varname, _key, _recipe in _DERIVED_DEFS:
    _token = _Color(_parse_recipe(_recipe))
    _DERIVED[_key] = _token
    globals()[_varname] = _token


# ═══════════════════════════════════════════════════════════════
# 旧别名 — @deprecated: 请使用 C_* 语义名，以下别名将在后续版本移除
# ═══════════════════════════════════════════════════════════════

G   = C_SECONDARY;   C   = C_ACCENT;     E   = C_SUCCESS
Y   = C_WARNING;     X   = C_ERROR;      U   = C_LINK
M   = C_DECORATION;  O   = C_EMPHASIS;   BG  = C_CODE_BG
W   = C_USER;        W7  = C_PRIMARY
GRY = C_SECONDARY;   CYN = C_ACCENT;     GRN = C_SUCCESS
YLW = C_WARNING;     RED = C_ERROR;      BLU = C_LINK
MAG = C_DECORATION;  ORG = C_EMPHASIS;   BG8 = C_CODE_BG
WHT = C_USER;        WHT7 = C_PRIMARY


# ═══════════════════════════════════════════════════════════════
# prompt_toolkit 样式（ptk Style 字符串，非 ANSI）
# ═══════════════════════════════════════════════════════════════

PTK_PROMPT_SYMBOL = "bold #00ff00"
PTK_PROMPT_TEXT   = "#ffffff"
PTK_PROMPT_CUSTOM = "#FFFFD0"


# ═══════════════════════════════════════════════════════════════
# apply_style — 对外唯一入口
# ═══════════════════════════════════════════════════════════════

def _resolve_ptk_style(raw: str) -> str:
    """将含配方 token 的 ptk 样式字符串转为 ptk 可识别的 #hex 格式。

    "bold 流光青" → "bold #5EEAD4"
    "纯白"        → "#ffffff"
    """
    parts = []
    for tok in raw.strip().split():
        if tok in _STYLE_MAP:
            parts.append(tok)
        elif tok in _BASE_HEX:
            parts.append(_BASE_HEX[tok])
        elif tok in _BASE_COLORS:
            import re
            m = re.search(r"2;(\d+);(\d+);(\d+)", _BASE_COLORS[tok]._value)
            if m:
                parts.append(f"#{int(m.group(1)):02x}{int(m.group(2)):02x}{int(m.group(3)):02x}")
            else:
                parts.append(tok)
        else:
            parts.append(tok)
    return " ".join(parts)


def apply_style(ui_config: dict) -> None:
    """加载 narnat.json "界面" 分组全部颜色配置。

    传入的 dict 即 UIConfig.raw，结构见 config/loader.py。
    """
    global SHOW_COST, SHOW_BALANCE, MAX_TOKENS
    global SHOW_RATIO, CONTEXT_WINDOW
    global PTK_PROMPT_SYMBOL, PTK_PROMPT_TEXT, PTK_PROMPT_CUSTOM

    # 1. 基础色板（"颜色" 分区：纯调色板，只注册 hex 值。跳过 _ 开头的元数据 key）
    colors = ui_config.get("colors", {})
    # 先将所有基础色恢复默认hex，确保 apply_style({}) 能完整重置
    _DEFAULT_HEX = {n: h for n, h, _ in _BASE_DEFS}
    for name in list(_BASE_HEX):
        _BASE_HEX[name] = _DEFAULT_HEX.get(name, _BASE_HEX[name])
    for name, val in colors.items():
        if name.startswith("_") or not str(val).startswith("#"):
            continue
        ansi = _hex_to_ansi(str(val), bg=_BG_LOOKUP.get(name, False))
        if name in _BASE_COLORS:
            _BASE_COLORS[name]._value = ansi
        else:
            _BASE_COLORS[name] = _Color(ansi)
        _BASE_HEX[name] = str(val)

    # 1b. "基础色" 分区：角色 → 调色板引用（如 "用户": "纯白" → C_USER = #FFFFFF）
    base_colors = ui_config.get("base_colors", {})
    for name, val in base_colors.items():
        if name.startswith("_") or str(val).startswith("#"):
            continue
        ref = str(val)
        ref_hex = str(colors.get(ref, ""))
        if ref_hex.startswith("#"):
            ansi = _hex_to_ansi(ref_hex, bg=_BG_LOOKUP.get(name, False))
            _BASE_HEX[name] = ref_hex
        elif ref in _BASE_HEX:
            ref_hex = _BASE_HEX[ref]
            ansi = _hex_to_ansi(ref_hex, bg=_BG_LOOKUP.get(name, False))
            _BASE_HEX[name] = ref_hex
        elif ref in _BASE_COLORS:
            ansi = _BASE_COLORS[ref]._value
        else:
            continue
        if name in _BASE_COLORS:
            _BASE_COLORS[name]._value = ansi
        else:
            _BASE_COLORS[name] = _Color(ansi)

    # 1c. "代码块.背景" → C_CODE_BG（仅代码块使用，归入代码块区域）
    codeblock = ui_config.get("codeblock", {})
    if "background" in codeblock:
        ref = str(codeblock["background"])
        ref_hex = colors.get(ref, "")
        if isinstance(ref_hex, str) and ref_hex.startswith("#"):
            _BASE_COLORS["code_bg"]._value = _hex_to_ansi(ref_hex, bg=True)
            _BASE_HEX["code_bg"] = ref_hex
        elif ref in _BASE_HEX:
            _BASE_COLORS["code_bg"]._value = _hex_to_ansi(_BASE_HEX[ref], bg=True)
            _BASE_HEX["code_bg"] = _BASE_HEX[ref]
        elif ref in _BASE_COLORS:
            import re
            m = re.search(r"2;(\d+);(\d+);(\d+)", _BASE_COLORS[ref]._value)
            if m:
                _BASE_COLORS["code_bg"]._value = _ansi_color("48",
                    int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 2. 派生 token（用户覆盖优先，未覆盖的用默认配方重解析以跟随基础色更新）
    for _varname, full_key, default_recipe in _DERIVED_DEFS:
        section, short_key = full_key.split(".", 1)
        user = ui_config.get(section, {}).get(short_key)
        _DERIVED[full_key]._value = _parse_recipe(str(user) if user is not None else default_recipe)

    # 3. prompt_toolkit 样式（解析配方 → ptk 兼容的 #hex 格式）
    p = ui_config.get("prompt", {})
    if "symbol" in p: PTK_PROMPT_SYMBOL = _resolve_ptk_style(p["symbol"])
    if "text"   in p: PTK_PROMPT_TEXT   = _resolve_ptk_style(p["text"])
    else:
        # 未显式设置 prompt.text 时，默认跟随 C_USER（用户颜色），
        # 确保 "颜色.用户" 的修改能自动反映到用户输入文字颜色。
        user_hex = _BASE_HEX.get("user")
        if user_hex:
            PTK_PROMPT_TEXT = user_hex
        else:
            import re
            user_val = _BASE_COLORS.get("user", _Color(""))._value
            m = re.search(r"2;(\d+);(\d+);(\d+)", user_val)
            if m:
                PTK_PROMPT_TEXT = f"#{int(m.group(1)):02x}{int(m.group(2)):02x}{int(m.group(3)):02x}"
    if "custom" in p: PTK_PROMPT_CUSTOM = _resolve_ptk_style(p["custom"])

    # 4. 显示开关
    SHOW_COST    = bool(ui_config.get("show_cost", False))
    SHOW_BALANCE = bool(ui_config.get("show_balance", False))
    MAX_TOKENS   = int(ui_config.get("max_output_tokens", 128000))
    SHOW_RATIO   = bool(ui_config.get("show_ratio", False))
    CONTEXT_WINDOW = int(ui_config.get("context_window", DEFAULT_CONTEXT_WINDOW))


# ═══════════════════════════════════════════════════════════════
# 显示开关默认值
# ═══════════════════════════════════════════════════════════════

SHOW_COST = False
SHOW_BALANCE = False
MAX_TOKENS = 128000
SHOW_RATIO = False
CONTEXT_WINDOW = DEFAULT_CONTEXT_WINDOW
