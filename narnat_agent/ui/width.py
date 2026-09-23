"""终端宽度探测与显示宽度度量 —— 渲染管道的度量层。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`「终端宽度与显示宽度」：
- 终端宽度优先级：环境变量 `NARNAT_TERM_WIDTH`（纯数字）→ Windows 传统控制台
  可见窗口实测 → 系统终端尺寸（获取失败兜底 120），最终 clamp 到 [20, 160]；
  另提供 `srwindow_cols`（srWindow 实测可见窗格）供表格渲染前二次校验收缩；
- 显示宽度：东亚宽度 W/F 记 2 列，其余（含歧义宽度字符）记 1 列；计算与拆字前
  剔除 ANSI 转义序列；
- 软折行：按显示宽度逐视觉单元累计断行，行尾补样式重置（纯文本模式跳过）、
  新行开头重放折行前的活跃样式、遇重置序列清空活跃样式、列宽不为正时原样
  返回单行、空文本产生一个空行。

结构（design D7）：全部为无状态纯函数，无模块级可变全局；`wrap_cell` 的纯文本
判定经构造注入的 `Console` 实时查询（不做导入期快照）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from typing import List

from ..output import Console

__all__ = [
    "FALLBACK_TERMINAL_WIDTH",
    "FORCED_WIDTH_ENV",
    "MAX_TERMINAL_WIDTH",
    "MIN_TERMINAL_WIDTH",
    "RE_ANSI",
    "char_width",
    "display_width",
    "srwindow_cols",
    "terminal_width",
    "visual_chars",
    "windows_console_window_cols",
    "wrap_cell",
]

# 终端宽度上限（超宽终端下限制渲染行宽，避免过长的表格边框）
MAX_TERMINAL_WIDTH = 160

# 终端宽度下限（极窄环境下保底，保证分隔线至少 18 个字符）
MIN_TERMINAL_WIDTH = 20

# 终端尺寸探测失败时的兜底宽度
FALLBACK_TERMINAL_WIDTH = 120

# 手动强制终端宽度的环境变量（纯数字，极端环境兜底）
FORCED_WIDTH_ENV = "NARNAT_TERM_WIDTH"

# ANSI 转义序列（宽度计算时剔除）
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def windows_console_window_cols() -> int:
    """Windows 控制台「可见窗口」宽度（列数）。

    仅对传统 conhost（真实控制台窗口）生效，此时缓冲区宽度可能大于可见窗口
    宽度（如 120 缓冲 + 80 窗口），按缓冲区宽度渲染会导致输出行超出可见窗口、
    被终端强制折行，破坏表格对齐。

    判定顺序：
    1. Windows Terminal / ConPTY 环境（有 WT_SESSION 或 GetConsoleWindow 为 0）
       → 返回 0，调用方回退到 shutil（ConPTY 下缓冲区宽度与窗格一致）；
    2. conhost 可见窗口：用客户区宽度 / 字体宽度换算真实列数；
    3. conhost 隐藏窗口（如从启动器拉起）：退而求其次用 srWindow。
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
        # srWindow = SMALL_RECT: Left(2) Top(2) Right(2) Bottom(2)，即 raw[10:12]=Left, raw[12:14]=Top,
        # raw[14:16]=Right, raw[16:18]=Bottom
        info = ctypes.create_string_buffer(22)
        if kernel32.GetConsoleScreenBufferInfo(handle, info):
            left = int.from_bytes(info.raw[10:12], "little", signed=True)
            right = int.from_bytes(info.raw[14:16], "little", signed=True)
            cols = right - left + 1
            if cols > 0:
                return cols
    except Exception:
        pass
    return 0


def terminal_width() -> int:
    """当前终端宽度（clamp 到 [20, 160]）。

    手动兜底：极端环境下终端宽度检测不可靠时，可用环境变量强制指定；
    传统 conhost（有真实可见窗口）以可见窗口宽度为准，防止按缓冲区宽度渲染
    导致折行；ConPTY（Windows Terminal/VS Code 等）或管道下缓冲区宽度即实际
    可用宽度。
    """
    forced = os.environ.get(FORCED_WIDTH_ENV, "").strip()
    if forced.isdigit():
        return max(min(int(forced), MAX_TERMINAL_WIDTH), MIN_TERMINAL_WIDTH)
    win_cols = windows_console_window_cols()
    if win_cols:
        cols = win_cols
    else:
        try:
            cols = shutil.get_terminal_size().columns
        except Exception:
            cols = FALLBACK_TERMINAL_WIDTH
    return max(min(cols, MAX_TERMINAL_WIDTH), MIN_TERMINAL_WIDTH)


def srwindow_cols() -> int:
    """GetConsoleScreenBufferInfo.srWindow 实测可见窗格列数。

    这是终端「当前真实可见宽度」的最可靠来源（不依赖字体像素换算、不依赖
    shutil 缓存）。用于表格渲染前的二次校验：若其它宽度测量路径偶发偏大
    （窗口 resize 竞态、DPI 取整、GetClientRect 未就绪返回 0 等），
    以实测值为准收缩表格行宽，保证输出行不超出可见窗格、不被终端折行。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if not handle or handle in (-1, 0):
            return 0
        # CONSOLE_SCREEN_BUFFER_INFO: dwSize(4) dwCursorPos(4) wAttr(2) srWindow(8) maxWinSize(4)
        # srWindow = SMALL_RECT: Left(2) Top(2) Right(2) Bottom(2)，即 raw[10:12]=Left, raw[12:14]=Top,
        # raw[14:16]=Right, raw[16:18]=Bottom
        info = ctypes.create_string_buffer(22)
        if kernel32.GetConsoleScreenBufferInfo(handle, info):
            left = int.from_bytes(info.raw[10:12], "little", signed=True)
            right = int.from_bytes(info.raw[14:16], "little", signed=True)
            cols = right - left + 1
            return cols if cols > 0 else 0
    except Exception:
        pass
    return 0


def char_width(ch: str) -> int:
    """单字符的终端显示宽度（CJK/全角=2，其余=1）。

    歧义宽度字符（east_asian_width == 'A'，如 → ≈ 等）统一按 1 列计算：
    Windows Terminal / VS Code / 多数 Linux 终端默认按窄字符渲染，
    中文版控制台对 GBK 中不存在的字符（如 →）同样按窄字符渲染。
    若按 2 列计算，单元格右边框会向左偏移 1 列，出现可见的边框不齐。
    """
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    """终端显示宽度：CJK字符=2列，跳过ANSI转义序列。"""
    text = RE_ANSI.sub("", text)
    return sum(char_width(ch) for ch in text)


def visual_chars(text: str) -> List[str]:
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


def wrap_cell(ansi_text: str, max_width: int, console: Console) -> List[str]:
    """将含ANSI转义的文本按显示宽度折行。

    Args:
        ansi_text: 含ANSI颜色码的文本
        max_width: 最大显示宽度（必须>0）
        console: 输出原语（纯文本模式下行尾不补样式重置）

    Returns:
        折行后的文本列表，每个元素显示宽度<=max_width。
        空文本返回 [""]。
        每行自动继承之前的ANSI状态，确保颜色不断裂。
    """
    if max_width <= 0:
        return [ansi_text]

    chars = visual_chars(ansi_text)
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
        cw = char_width(ch)

        if current_width + cw > max_width:
            # 超出宽度，换行
            # 当前行追加重置，避免颜色泄漏到后续内容（纯文本模式无颜色，跳过）
            lines.append(current + ("" if console.is_plain() else "\x1b[0m"))
            # 新行开头重放活跃ANSI状态
            current = "".join(active_ansi) + ch
            current_width = cw
        else:
            current += ch
            current_width += cw

    if current or not lines:
        lines.append(current)

    return lines
