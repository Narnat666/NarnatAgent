"""diff 生成、着色与「正文相同但字节不同」的差异描述 —— 编辑/覆盖类工具的展示面。

契约来源：specs/tools-file「diff 生成与着色」「编辑类工具的返回形态与差分展示」。

行为搬运自旧实现 `narnat_agent/tools/diff_utils.py`（`colorize_diff` /
`describe_bytes_only_change` 逐字一致）+ `edit`/`write` 两份重复的 `_make_diff`
（v2 合并为单点 `make_diff`）。

取色协议（v2 结构重组）：旧实现直接取 `output` 模块的全局色（GRY/CYN/GRN/RED/RST），
新架构下颜色由实例承载——`colorize_diff` 接受可选的取色对象（装配时注入
`output.style.Theme`，其 `diff_header`/`diff_range`/`diff_added`/`diff_removed`/
`diff_context`/`rst` 属性为 ANSI 求值对象，纯文本模式下求值为空串即自动消色）；
未注入时使用与 output 默认色板一致的内置真彩序列（含"加粗青色/暗淡青色/红/绿/灰"
五种角色，与旧实现默认输出逐字节等价）。
"""
from __future__ import annotations

import difflib
import os
from typing import Protocol

__all__ = [
    "DEFAULT_DIFF_COLORS",
    "DiffColors",
    "colorize_diff",
    "describe_bytes_only_change",
    "make_diff",
]


class DiffColors(Protocol):
    """diff 着色取色协议（属性求值即 ANSI 前缀；装配注入主题对象）。"""

    diff_header: object
    diff_range: object
    diff_added: object
    diff_removed: object
    diff_context: object
    rst: object


class _DefaultDiffColors:
    """内置默认取色 —— 按 output 默认色板（真彩）固化的五种角色 + 复位。"""

    __slots__ = ()

    # bold accent（加粗青）
    diff_header = "\x1b[1m\x1b[38;2;94;234;212m"
    # dim accent（暗淡青）
    diff_range = "\x1b[2m\x1b[38;2;94;234;212m"
    # success（绿）
    diff_added = "\x1b[38;2;52;211;153m"
    # error（红）
    diff_removed = "\x1b[38;2;248;113;113m"
    # secondary（灰）
    diff_context = "\x1b[38;2;100;116;139m"
    rst = "\x1b[0m"


DEFAULT_DIFF_COLORS = _DefaultDiffColors()
"""未注入主题时的默认取色（与旧实现默认输出一致）。"""


def _pick(colors: object | None, attr: str, default: str) -> str:
    """取色：注入对象优先（实时求值，支持纯文本模式消色），否则用默认序列。"""
    if colors is None:
        return default
    return str(getattr(colors, attr, default))


def colorize_diff(diff_text: str, colors: DiffColors | None = None) -> str:
    """对 unified diff 文本着色：`---`/`+++` 加粗青、`@@` 暗淡青、`-` 红、`+` 绿、其余灰。

    保留行首的 +/- 符号，仅对内容着色，不改变文本结构；空输入或 `[无差异]`
    输出灰色的 `[无差异]`。
    """
    header = _pick(colors, "diff_header", DEFAULT_DIFF_COLORS.diff_header)
    range_ = _pick(colors, "diff_range", DEFAULT_DIFF_COLORS.diff_range)
    added = _pick(colors, "diff_added", DEFAULT_DIFF_COLORS.diff_added)
    removed = _pick(colors, "diff_removed", DEFAULT_DIFF_COLORS.diff_removed)
    context = _pick(colors, "diff_context", DEFAULT_DIFF_COLORS.diff_context)
    reset = _pick(colors, "rst", DEFAULT_DIFF_COLORS.rst)

    if not diff_text or diff_text == "[无差异]":
        return f"{context}[无差异]{reset}"

    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{header}{line}{reset}")
        elif line.startswith("@@"):
            out.append(f"{range_}{line}{reset}")
        elif line.startswith("-"):
            out.append(f"{removed}{line}{reset}")
        elif line.startswith("+"):
            out.append(f"{added}{line}{reset}")
        else:
            out.append(f"{context}{line}{reset}")
    return "\n".join(out)


def describe_bytes_only_change(old_bytes: bytes, new_bytes: bytes) -> str:
    """正文相同、字节却不同时的差异摘要（Write 变更判定用）。

    unified diff 基于 splitlines()，会抹平 CRLF/LF 与末尾换行差异，不能据此判断
    "文件未变化"——需按字节比较，并说明差异来源：行尾符 / 末尾换行 / 字节表示
    （如 GBK 重写为 UTF-8、BOM 增减）。
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


def make_diff(old_content: str, new_content: str, file_path: str) -> str:
    """生成 unified diff（`a/{文件名}` → `b/{文件名}`，行间以 `\\n` 连接）。

    无差异时返回字符串 `[无差异]`（与旧实现 `edit._make_diff` / `write._make_diff`
    的返回值逐字一致）。
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    basename = os.path.basename(file_path)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{basename}",
        tofile=f"b/{basename}",
        lineterm="",
    )
    result = "\n".join(diff)
    return result if result else "[无差异]"
