"""工具参数归一化辅助函数。

LLM 生成的 JSON 参数偶发把布尔值写成字符串（如 replace_all="false"）。
Python 的 bool("false") 恒为 True，会导致与 AI 意图相反的行为
（Edit 全量替换、Grep 忽略大小写等）。统一在此归一化。

同理，编号类参数偶发写成字符串（编号："2"）；目标模式的 GoalBaseline /
GoalComplete 共用同一个 .编号 约定，归一化只在此处定义一份，避免两处漂移
（一处接受数字串、另一处拒绝会让 AI 拿到自相矛盾的拒绝文本）。
"""

from typing import Optional


def to_bool(v) -> bool:
    """把 LLM 可能传的布尔参数归一化为 bool。

    - bool → 原样
    - 字符串 "false"/"0"/"no"/"off"/"f"/"n"（不区分大小写、容忍空白）→ False
    - 字符串 "true"/"1"/"yes"/"on"/"t"/"y" → True
    - 其他（数字等）→ bool(v)
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "off", "f", "n", ""):
            return False
        if s in ("true", "1", "yes", "on", "t", "y"):
            return True
    return bool(v)


def to_positive_int(v) -> Optional[int]:
    """把 LLM 可能传的编号参数归一化为正整数。

    - 正整数 int → 原样
    - 纯数字字符串（容忍首尾空白）→ int
    - 其他（bool / 0 / 负数 / 非数字串 / None）→ None（调用方按"编号非法"拒绝）
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = int(v.strip()) if v.strip().isdecimal() else None
    return v if isinstance(v, int) and v > 0 else None
