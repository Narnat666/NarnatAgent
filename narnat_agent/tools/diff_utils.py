"""Diff着色工具 —— 对unified diff文本添加ANSI颜色

从ui_design.py中提取，供tools层使用，避免tools→ui的跨层依赖。
颜色常量在此模块内自包含，与ui/colors.py保持同步。
"""

# ── ANSI颜色常量（与ui/colors.py保持一致） ──
RST = "\x1b[0m"
BLD = "\x1b[1m"
DIM = "\x1b[38;2;100;116;139m"   # 灰蓝/次要文字
CYN = "\x1b[38;2;94;234;212m"    # 流光青/标题色
GRN = "\x1b[38;2;52;211;153m"    # 薄荷绿/成功色
RED = "\x1b[38;2;248;113;113m"   # 珊瑚红/错误色

# 简写别名
R = RST
B = BLD
D = DIM
G = DIM   # 上下文行用暗淡色
C = CYN
E = GRN
X = RED


def colorize_diff(diff_text: str) -> str:
    """对 unified diff 文本着色：-行红色、+行绿色、@@行青色暗淡，其余灰色。

    保留行首的 +/- 符号，仅对内容着色，不改变文本结构。
    """
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
