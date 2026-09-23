"""退出码与错误标签协议 —— 框架判定侧共用的进程级随机标签信号。

[exit code: N] 是给人/AI 读的文本，但命令自身的输出里可能出现同款文字
（diff、日志、脚本回显里引用历史结果）：仅靠文本识别会把成功命令误报为
失败，或把失败漏判。故框架生成该行时附加一个进程级随机标签，判定侧只认
带标签的行——命令无从得知标签，无法伪造；标签在结果交给 AI 之前剥离，
AI 看到的内容与不带标签时完全一致。

同一进程内标签相同：多段命令会输出多个退出码行（各失败段+末尾总码），
判定取最后一个，即整体退出码。

行为搬运自旧实现 `narnat_agent/tools/exec_signal.py`（编解码与判定逐字一致）；
契约来源：specs/tools-shell「退出码与错误标签协议」「输出截断」。

结构约定：模块级随机标签与编译正则以全大写常量定义（结构检查
`tests/check_layering.py` 的「模块级可变状态」规则只允许全大写名），
随机值在模块导入时生成一次（进程级常量）。
"""
from __future__ import annotations

import re
import uuid

__all__ = [
    "error_line",
    "has_error",
    "parse_rc",
    "rc_line",
    "safe_cut_points",
    "strip_tags",
    "tag_error",
]

# 退出码标签：进程级随机，命令无法预知/伪造
TAG = uuid.uuid4().hex[:8]
# 框架错误标签：与退出码标签同一机制，独立取值。
# 工具层（Shell/Terminal等）生成的错误消息带此标签；命令输出无法预知该随机值，
# UI 判定失败时只认标签——文本里恰好出现"[错误"/"[超时"等字样绝不触发失败显示。
ERR_TAG = uuid.uuid4().hex[:8]

RC_LINE_RE = re.compile(r"\[exit code: (-?\d+)\] \[" + TAG + r"\]")
TAG_RE = re.compile(r" ?\[" + TAG + r"\]")
ERR_TAG_RE = re.compile(r" ?\[" + ERR_TAG + r"\]")


def rc_line(rc: int) -> str:
    """生成框架退出码行（带标签）。仅框架自身输出使用，不要用于命令输出文本"""
    return f"[exit code: {rc}] [{TAG}]"


def error_line(msg: str) -> str:
    """生成框架错误行（带不可伪造标签）。仅框架自身错误输出使用：
    工具层错误（连接失败/参数非法/权限拒绝等）。命令输出打印任意文本
    都无法命中该随机标签，UI据此100%确定失败，从根上杜绝误判。"""
    return f"[错误: {msg}] [{ERR_TAG}]"


def tag_error(text: str) -> str:
    """给框架生成的失败消息附加错误标签。
    用于非"[错误"前缀的框架失败形态（如 Shell 超时杀进程提示"[超时: ...已终止]"）。"""
    return f"{text} [{ERR_TAG}]"


def has_error(result: str) -> bool:
    """结果是否含框架错误标签。UI 判定失败显示的唯一依据（命令输出不可伪造）。"""
    return f"[{ERR_TAG}]" in result


def parse_rc(result: str) -> int | None:
    """取结果中最后一个框架退出码；结果不含框架退出码行时返回 None"""
    found = RC_LINE_RE.findall(result)
    return int(found[-1]) if found else None


def strip_tags(result: str) -> str:
    """剥离全部框架标签（退出码标签+错误标签）：AI 只应看到无标签文本"""
    return ERR_TAG_RE.sub("", TAG_RE.sub("", result))


def safe_cut_points(text: str, head_pos: int, tail_pos: int) -> tuple[int, int]:
    """截断切点吸附：框架标签不允许被切开（完整留给 strip_tags 剥离）。

    截断若从标签中间切开，残缺片段无法被 strip_tags 匹配，会泄漏给 AI，
    且 has_error/parse_rc 对截断结果失效。规则：切点落在标签内部时——
    头部切点吸附到标签结尾（标签留在头部侧）、尾部切点吸附到标签
    开头（标签留在尾部侧）；吸附后两切点相向越过（仅紧贴 max_chars
    的退化场景）时取尾部切点收口，保证两侧都不切标签。
    """
    spans = [m.span() for m in TAG_RE.finditer(text)]
    spans += [m.span() for m in ERR_TAG_RE.finditer(text)]
    spans.sort()
    for start, end in spans:
        if start < head_pos < end:
            head_pos = end
    for start, end in reversed(spans):
        if start < tail_pos < end:
            tail_pos = start
    if head_pos > tail_pos:
        head_pos = tail_pos
    return head_pos, tail_pos
