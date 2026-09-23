"""工具参数归一化与绑定 —— 字符串布尔容错与「模拟 CPython 关键字绑定」的错误通道。

契约来源：
- specs/tools-file「Edit 参数契约与唯一性校验」（`replace_all` 的字符串布尔归一）；
- specs/tools-file「Grep 参数契约与输入校验」（CLI 风格别名、未知参数的中文提示）；
- specs/tools-file「工具执行入口协议」（友好提示的两条形态由执行入口按异常文本识别）。

行为搬运自旧实现 `narnat_agent/tools/param_utils.py`（`to_bool` 逐字一致）；
绑定函数为 v2 结构重组产物：旧实现靠 Python 原生签名绑定触发 TypeError，
新协议下工具收 `args` dict，故在此模拟同形异常文本（执行入口据此转中文提示）。
"""
from __future__ import annotations

__all__ = [
    "bind_params",
    "got_unexpected_kwarg",
    "missing_required",
    "settings_of",
    "to_bool",
]


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


def got_unexpected_kwarg(name: str) -> TypeError:
    """构造与 CPython 原生同形的「未知参数」异常文本（执行入口正则识别该形态）。"""
    return TypeError(f"got an unexpected keyword argument '{name}'")


def missing_required(names: list[str]) -> TypeError:
    """构造与 CPython 原生同形的「缺少必填参数」异常文本（单/多参数两种措辞）。"""
    if len(names) == 1:
        detail = f"1 required positional argument: '{names[0]}'"
    else:
        quoted = " and ".join(f"'{n}'" for n in names)
        detail = f"{len(names)} required positional arguments: {quoted}"
    return TypeError(f"missing {detail}")


def bind_params(args: dict, allowed: tuple[str, ...],
                required: tuple[str, ...] = ()) -> dict:
    """按「无 **kwargs 的函数」语义绑定参数（顺序与 CPython 一致）。

    - 含未知参数时优先报「未知参数」（与 CPython 的 `f(b=1)` 报错顺序一致）；
    - 其后检查必填参数缺失；参数值显式为 `None` 不算缺失（同原生绑定语义）；
    - 返回仅含 `allowed` 中出现过的参数（保持调用方传入顺序）。
    """
    unknown = [key for key in args if key not in allowed]
    if unknown:
        raise got_unexpected_kwarg(unknown[0])
    missing = [name for name in required if name not in args]
    if missing:
        raise missing_required(missing)
    return {key: value for key, value in args.items() if key in allowed}


def settings_of(env) -> object | None:
    """取工具运行时上下文（`ToolEnv`）的只读配置面；env 或 settings 缺失时返回 None。

    直接调用场景（env=None）下各工具按「无配置」语义降级：不施加全局截断、
    忽略目录为空——与旧实现 `_tool_context=None` 的行为一致。
    """
    return getattr(env, "settings", None) if env is not None else None
