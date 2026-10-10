"""工具参数归一化辅助函数。

LLM 生成的 JSON 参数偶发把布尔值写成字符串（如 replace_all="false"）。
Python 的 bool("false") 恒为 True，会导致与 AI 意图相反的行为
（Edit 全量替换、Grep 忽略大小写等）。统一在此归一化。
"""


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
