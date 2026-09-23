"""
ANSI 颜色常量与配色管理 —— 从 output.py 重导出

所有定义统一在 output.py，此模块仅做兼容重导出，
保持 ui 层历史 import 路径不变。
"""

# 基础常量
from ..output import (
    _Color, _stdout_lock, _ansi_color,
    write as _stdout_write, try_write as _stdout_try_write,
    RST, BLD, DIM, R, B, D,
)

# 基础色
from ..output import (
    C_PRIMARY, C_SECONDARY, C_USER,
    C_ACCENT, C_SUCCESS, C_WARNING,
    C_ERROR, C_LINK, C_DECORATION, C_EMPHASIS, C_CODE_BG,
)

# 旧别名
from ..output import (
    G, C, E, Y, X, U, M, O, BG, W, W7,
    GRY, CYN, GRN, YLW, RED, BLU, MAG, ORG, BG8, WHT, WHT7,
)

# 派生 token
from ..output import (
    MD_H1, MD_H3, MD_H4, MD_BOLD, MD_ITALIC, MD_STRIKE,
    MD_CODE, MD_LINK, MD_IMAGE, MD_BLOCKQUOTE, MD_HR,
    MD_UL, MD_OL, MD_TASK_DONE, MD_TASK_UNDONE,
    MD_TABLE_BORDER, MD_TABLE_CONTENT,
    CB_LINE_NO, CB_LANG_LABEL,
    CB_LANG_CYAN, CB_LANG_YELLOW, CB_LANG_GREEN,
    CB_LANG_MAGENTA, CB_LANG_RED, CB_LANG_BLUE, CB_LANG_GRAY,
    DIFF_HEADER, DIFF_RANGE, DIFF_ADDED, DIFF_REMOVED, DIFF_CONTEXT,
    UI_HEADER, UI_SPINNER, UI_INTERRUPTED, UI_INTERRUPTED_HINT,
    UI_STATS_LABEL, UI_STATS_VALUE, UI_SEPARATOR,
    CMD_SUCCESS, CMD_ERROR, CMD_HINT, CMD_HIGHLIGHT, CMD_MUTED,
    PTK_PROMPT_SYMBOL, PTK_PROMPT_TEXT, PTK_PROMPT_CUSTOM,
    apply_style, DisplayState,
)
