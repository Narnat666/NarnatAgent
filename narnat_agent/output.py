"""
终端输出抽象层 —— ANSI颜色常量 + 线程安全写入

core和tools依赖此模块，不依赖ui。
ui层也引用此模块的常量，保持颜色一致性。
"""

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
GRY = _Color("\x1b[38;2;100;116;139m")      # 灰蓝 #64748B（次要文字、分隔线）

# 主题流光色（青绿/蓝紫为主，低饱和舒适）
CYN = _Color("\x1b[38;2;94;234;212m")       # 流光青 #5EEAD4（主题主色、标题）
GRN = _Color("\x1b[38;2;52;211;153m")       # 薄荷绿 #34D399（成功、添加行）
YLW = _Color("\x1b[38;2;251;191;36m")       # 暖琥珀 #FBBF24（行内代码、提示）
RED = _Color("\x1b[38;2;248;113;113m")      # 珊瑚红 #F87171（错误、删除行）
BLU = _Color("\x1b[38;2;167;139;250m")      # 薰衣草紫 #A78BFA（链接、交互）
MAG = _Color("\x1b[38;2;232;121;249m")      # 粉紫流光 #E879F9（装饰、品牌色）
ORG = _Color("\x1b[38;2;251;146;60m")       # 桃橙色 #FB923C（强调、spinner）

# 背景色（深夜蓝黑，沉浸式代码块背景）
BG8 = _Color("\x1b[48;2;15;23;42m")         # 深夜蓝 #0F172A

# 文字色
WHT = _Color("\x1b[38;2;255;255;255m")      # 极致白色 #FFFFFF（用户输入）
WHT7 = _Color("\x1b[38;2;255;255;208m")     # 偏黄米白 #FFFFD0（AI输出）

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
