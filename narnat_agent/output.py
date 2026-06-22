"""
终端输出抽象层 —— ANSI颜色常量 + 线程安全写入

core和tools依赖此模块，不依赖ui。
ui层也引用此模块的常量，保持颜色一致性。
"""

import os
import sys
import threading


# ── stdout 并发写入锁 ── spinner 用 try_write（拿不到跳帧），其余阻塞 ──
_stdout_lock = threading.Lock()


def write(text: str) -> None:
    """阻塞拿锁写入 stdout + flush。每次写前 \r 归位列0，防止 spinner 残留光标。"""
    with _stdout_lock:
        sys.stdout.write("\r" + text)
        sys.stdout.flush()


def try_write(text: str) -> bool:
    """非阻塞拿锁写入。spinner/compress 动画帧专用，拿不到跳过该帧。"""
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
    """检测终端是否支持真彩色(24-bit)ANSI颜色。

    不支持真彩色的终端会把 \\x1b[38;2;R;G;Bm 序列错误解析，
    导致部分转义码变成可见文本（如显示 "1m[" 而非着色）。
    """
    # COLORTERM 环境变量是检测真彩色的标准方式
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    # Windows Terminal 支持
    if os.environ.get("WT_SESSION"):
        return True
    # ConEmu 支持
    if os.environ.get("ConEmuANSI") == "ON":
        return True
    # Windows: 检查虚拟终端处理是否启用
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # 虚拟终端已启用 → Windows 10+ 支持 truecolor
                if mode.value & 0x0004:
                    return True
        except Exception:
            pass
    # TERM=xterm-256color 不一定支持真彩色，保守返回 False
    return False


_TRUECOLOR = _supports_truecolor()

# 真彩色 → 基本16色降级映射
_FALLBACK_MAP = {
    # (fg/bg, R, G, B) -> ANSI基本色码
    # 灰蓝 #64748B -> 37(白灰)
    (38, 100, 116, 139): "\x1b[37m",
    # 流光青 #5EEAD4 -> 36(青)
    (38, 94, 234, 212): "\x1b[36m",
    # 薄荷绿 #34D399 -> 32(绿)
    (38, 52, 211, 153): "\x1b[32m",
    # 暖琥珀 #FBBF24 -> 33(黄)
    (38, 251, 191, 36): "\x1b[33m",
    # 珊瑚红 #F87171 -> 31(红)
    (38, 248, 113, 113): "\x1b[31m",
    # 薰衣草紫 #A78BFA -> 35(紫)
    (38, 167, 139, 250): "\x1b[35m",
    # 粉紫流光 #E879F9 -> 35(紫)
    (38, 232, 121, 249): "\x1b[35m",
    # 桃橙色 #FB923C -> 33(黄)
    (38, 251, 146, 60): "\x1b[33m",
    # 深夜蓝 #0F172A (背景) -> 44(蓝底)
    (48, 15, 23, 42): "\x1b[44m",
    # 极致白 #FFFFFF -> 37(白)
    (38, 255, 255, 255): "\x1b[37m",
    # 偏黄米白 #FFFFD0 -> 37(白)
    (38, 255, 255, 208): "\x1b[37m",
}


def _ansi_color(code: str, r: int, g: int, b: int) -> str:
    """生成ANSI颜色码，不支持真彩色时降级为基本16色。"""
    if _TRUECOLOR:
        return f"\x1b[{code};2;{r};{g};{b}m"
    key = (int(code), r, g, b)
    if key in _FALLBACK_MAP:
        return _FALLBACK_MAP[key]
    # 不在映射中的颜色：按RGB分量映射到最近的ANSI基本色
    if code == "48":
        # 背景色：暗色映射到44(蓝底)，亮色映射到47(白底)
        return "\x1b[44m" if (r + g + b) < 384 else "\x1b[47m"
    # 前景色：按RGB主分量映射
    if r > g and r > b:
        return "\x1b[31m"   # 红
    if g > r and g > b:
        return "\x1b[32m"   # 绿
    if b > r and b > g:
        return "\x1b[35m"   # 紫
    if r > 200 and g > 200:
        return "\x1b[33m"   # 黄
    if g > 200 and b > 200:
        return "\x1b[36m"   # 青
    return "\x1b[37m"       # 白


# ═══════════════════════════════════════════════════════════════
# ANSI 转义序列常量  (Salt Flow 配色 - 椒盐音乐风格)
# 可通过 .narnat/narnat.json 自定义
# ═══════════════════════════════════════════════════════════════

class _Color:
    """可变颜色容器：f"{R}text{R}" 直接可用，apply_style() 更新 _value 全局生效"""
    __slots__ = ('_value',)
    def __init__(self, value: str):
        self._value = value
    def __str__(self):
        return self._value
    def __repr__(self):
        return self._value


RST = _Color("\x1b[0m")
BLD = _Color("\x1b[1m")
DIM = _Color("\x1b[2m")

# 核心中性色（灰蓝调，现代沉浸感）
GRY = _Color(_ansi_color("38", 100, 116, 139))      # 灰蓝 #64748B（次要文字、分隔线）

# 主题流光色（青绿/蓝紫为主，低饱和舒适）
CYN = _Color(_ansi_color("38", 94, 234, 212))       # 流光青 #5EEAD4（主题主色、标题）
GRN = _Color(_ansi_color("38", 52, 211, 153))       # 薄荷绿 #34D399（成功、添加行）
YLW = _Color(_ansi_color("38", 251, 191, 36))       # 暖琥珀 #FBBF24（行内代码、提示）
RED = _Color(_ansi_color("38", 248, 113, 113))      # 珊瑚红 #F87171（错误、删除行）
BLU = _Color(_ansi_color("38", 167, 139, 250))      # 薰衣草紫 #A78BFA（链接、交互）
MAG = _Color(_ansi_color("38", 232, 121, 249))      # 粉紫流光 #E879F9（装饰、品牌色）
ORG = _Color(_ansi_color("38", 251, 146, 60))       # 桃橙色 #FB923C（强调、spinner）

# 背景色（深夜蓝黑，沉浸式代码块背景）
BG8 = _Color(_ansi_color("48", 15, 23, 42))         # 深夜蓝 #0F172A

# 文字色
WHT = _Color(_ansi_color("38", 255, 255, 255))      # 极致白色 #FFFFFF（用户输入）
WHT7 = _Color(_ansi_color("38", 255, 255, 208))     # 偏黄米白 #FFFFD0（AI输出）

# 简写别名
R, B, D, G, C = RST, BLD, DIM, GRY, CYN
E, Y, X, U, M, O, BG = GRN, YLW, RED, BLU, MAG, ORG, BG8
W, W7 = WHT, WHT7

# 配色键映射（供 apply_style 使用）
STYLE_KEY_MAP = {
    "用户输入色":   ("WHT",  False),
    "AI输出色":     ("WHT7", False),
    "标题色":       ("CYN",  False),
    "成功色":       ("GRN",  False),
    "行内代码色":   ("YLW",  False),
    "错误色":       ("RED",  False),
    "链接色":       ("BLU",  False),
    "装饰色":       ("MAG",  False),
    "加载动画色":   ("ORG",  False),
    "次要文字色":   ("GRY",  False),
    "代码块背景色":  ("BG8",  True),
}

# UI显示开关（由 apply_style 设置）
SHOW_COST = False
SHOW_BALANCE = False
MAX_TOKENS = 128000
