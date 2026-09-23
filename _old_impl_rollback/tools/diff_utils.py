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


def describe_bytes_only_change(old_bytes: bytes, new_bytes: bytes) -> str:
    """正文相同、字节却不同时的差异摘要（Write变更判定用）。

    unified diff基于splitlines()，会抹平CRLF/LF与末尾换行差异，不能据此判断
    "文件未变化"——需按字节比较，并说明差异来源：行尾符/末尾换行/字节表示
    （如GBK重写为UTF-8、BOM增减）。
    """
    def _style(b: bytes) -> str:
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        cr = b.count(b"\r") - crlf
        return "+".join(n for n, c in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if c) or "无换行符"

    old_style, new_style = _style(old_bytes), _style(new_bytes)
    old_nl, new_nl = old_bytes.endswith(b"\n"), new_bytes.endswith(b"\n")
    parts = []
    if old_style != new_style:
        parts.append(f"行尾符 {old_style}→{new_style}")
    if old_nl != new_nl:
        parts.append("末尾换行" + ("已添加" if new_nl else "已移除"))
    if not parts:
        # 行尾符与末尾换行都一致，字节却不同 → 换行以外的字节表示变化（编码/BOM）
        parts.append(f"换行符以外的字节表示变化（{len(old_bytes)}→{len(new_bytes)}字节，如编码或BOM差异）")
    return "、".join(parts)
