"""Diff着色工具 —— 对unified diff文本添加ANSI颜色

颜色常量从output.py导入，保持全局一致性。
"""

from ..output import RST as R, BLD as B, DIM as D, GRY as G, CYN as C, GRN as E, RED as X


def colorize_diff(diff_text: str) -> str:
    """对 unified diff 文本着色：-行红色、+行绿色、@@行青色暗淡，其余灰色。

    保留行首的 +/- 符号，仅对内容着色，不改变文本结构。
    """
    if not diff_text or diff_text == "[无差异]":
        return f"{G}[无差异]{R}"

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
