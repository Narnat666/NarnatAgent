"""
工具注册表 —— 名称→实现映射 + LLM工具定义

每个工具是tools/下的一个子目录，__init__.py导出execute和DEFINITION。
新增工具：新建目录 + 在此处加2行导入。
"""

import re
from typing import Dict, List, Any, Callable, Optional, Set

from ..config.defaults import DEFAULT_PLUGIN_TOOLS
from .exec_signal import error_line
from .tool_context import ToolContext
from .token_estimate import estimate_text_tokens

# ── 显式导入各工具（Nuitka安全） ──
from . import background  # noqa: F401  Shell 工具的 bg 附属模块（bash 内延迟导入；显式导入确保 Nuitka 打包）
from .read import execute as read_execute, DEFINITION as READ_DEF, CAPABILITY as READ_CAP
from .glob import execute as glob_execute, DEFINITION as GLOB_DEF, CAPABILITY as GLOB_CAP
from .grep import execute as grep_execute, DEFINITION as GREP_DEF, CAPABILITY as GREP_CAP
from .edit import execute as edit_execute, DEFINITION as EDIT_DEF, CAPABILITY as EDIT_CAP
from .write import execute as write_execute, DEFINITION as WRITE_DEF, CAPABILITY as WRITE_CAP
from .bash import execute as bash_execute, DEFINITION as BASH_DEF, CAPABILITY as BASH_CAP
from .terminal import execute as terminal_execute, DEFINITION as TERMINAL_DEF, CAPABILITY as TERMINAL_CAP

from .web_search import execute as web_search_execute, DEFINITION as WEBSEARCH_DEF, CAPABILITY as WEBSEARCH_CAP
from .todo_write import execute as todo_write_execute, DEFINITION as TODOWRITE_DEF, CAPABILITY as TODOWRITE_CAP
from .serial import execute as serial_execute, DEFINITION as SERIAL_DEF, CAPABILITY as SERIAL_CAP
from .mcp_tool import execute as mcp_execute, DEFINITION as MCP_DEF, CAPABILITY as MCP_CAP
from .goal_baseline import execute as goal_baseline_execute, CAPABILITY as GOALBASELINE_CAP
from .goal_complete import execute as goal_complete_execute, CAPABILITY as GOALCOMPLETE_CAP


# ═══════════════════════════════════════════════════════════════
# 工具实现映射
# ═══════════════════════════════════════════════════════════════

_TOOL_IMPLEMENTATIONS: Dict[str, Callable] = {
    "Read": read_execute,
    "Glob": glob_execute,
    "Grep": grep_execute,
    "Edit": edit_execute,
    "Write": write_execute,
    "Shell": bash_execute,
    "Terminal": terminal_execute,

    "WebSearch": web_search_execute,
    "TodoWrite": todo_write_execute,
    "Serial": serial_execute,
    "MCP": mcp_execute,
    "GoalBaseline": goal_baseline_execute,
    "GoalComplete": goal_complete_execute,
}


# ═══════════════════════════════════════════════════════════════
# LLM工具定义（从各工具模块收集）
# ═══════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[Dict] = [
    READ_DEF, GLOB_DEF, GREP_DEF, EDIT_DEF, WRITE_DEF,
    BASH_DEF, TERMINAL_DEF, WEBSEARCH_DEF, TODOWRITE_DEF,
    SERIAL_DEF, MCP_DEF,
]
# GoalComplete 不进默认工具定义：由 /goal 开启时通过 LLMClient.set_goal_tool(True) 动态注入，
# 普通模式不暴露给 LLM（执行能力始终注册，兼容历史残留调用）。GoalBaseline 同此机制。


# ═══════════════════════════════════════════════════════════════
# 运行时动态工具（MCP 服务器工具等，由 assembly 启动时注册）
# ═══════════════════════════════════════════════════════════════

_DYNAMIC_IMPLEMENTATIONS: Dict[str, Callable] = {}
_DYNAMIC_DEFINITIONS: List[Dict] = []


def register_dynamic_tools(entries: List[tuple]) -> None:
    """注册运行时动态工具（当前来源：MCP 服务器 tools/list）。

    entries: [(工具名, LLM工具定义, 实现函数), ...]
    与内置工具重名时跳过（内置优先），重复调用幂等。
    """
    for name, definition, impl in entries:
        if name in _TOOL_IMPLEMENTATIONS or name in _DYNAMIC_IMPLEMENTATIONS:
            continue
        _DYNAMIC_IMPLEMENTATIONS[name] = impl
        _DYNAMIC_DEFINITIONS.append(definition)


def unregister_dynamic_tools(names: List[str]) -> None:
    """注销运行时动态工具（MCP 断开时调用）。内置工具不受影响"""
    targets = set(names or ())
    if not targets:
        return
    for name in targets:
        _DYNAMIC_IMPLEMENTATIONS.pop(name, None)
    _DYNAMIC_DEFINITIONS[:] = [
        d for d in _DYNAMIC_DEFINITIONS
        if d.get("function", {}).get("name") not in targets
    ]


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 工具能力声明聚合（各工具 __init__.py 的 CAPABILITY，导入时汇总）
#
# 声明随工具自身维护，消费方一律查表/派生，不再维护第二份名单：
#   label           终端显示标签
#   dispatch        调度分类：readonly | write | serial（三值封闭）
#   summary         调用行摘要风格键（core/tool_dispatcher.py 的 _SUMMARY_STYLES）
#   trusted_output  输出是否框架可信文本（False=含外部不可信文本，失败判定只认框架标签）
#   plugin_label    /plugin 状态表说明文案（可缺省，缺省回退 label）
# ═══════════════════════════════════════════════════════════════

_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "Read": READ_CAP,
    "Glob": GLOB_CAP,
    "Grep": GREP_CAP,
    "Edit": EDIT_CAP,
    "Write": WRITE_CAP,
    "Shell": BASH_CAP,
    "Terminal": TERMINAL_CAP,
    "WebSearch": WEBSEARCH_CAP,
    "TodoWrite": TODOWRITE_CAP,
    "Serial": SERIAL_CAP,
    "MCP": MCP_CAP,
    "GoalBaseline": GOALBASELINE_CAP,
    "GoalComplete": GOALCOMPLETE_CAP,
}

# 未声明工具的能力（动态工具 mcp__*/未知工具，按名缓存，查询 O(1)）
_UNDECLARED_CAPABILITIES: Dict[str, Dict[str, Any]] = {}


def capability_names() -> List[str]:
    """全部已声明能力的工具名（顺序同本表定义）"""
    return list(_CAPABILITIES)


def get_capability(name: str) -> Dict[str, Any]:
    """工具能力查询（O(1)）。

    未声明的动态工具（mcp__*，MCP 服务器热注册）与未知工具返回保守默认：
    串行调度 + 原名标签；动态工具输出含服务端文本（不可信，失败判定只认框架标签）
    且摘要风格为 mcp，未知工具无摘要。
    """
    cap = _CAPABILITIES.get(name)
    if cap is not None:
        return cap
    cap = _UNDECLARED_CAPABILITIES.get(name)
    if cap is None:
        if name.startswith("mcp__"):
            cap = {"label": name, "dispatch": "serial", "summary": "mcp",
                   "trusted_output": False}
        else:
            cap = {"label": name, "dispatch": "serial", "summary": "none",
                   "trusted_output": True}
        _UNDECLARED_CAPABILITIES[name] = cap
    return cap


def _needs_context(name: str) -> bool:
    """该工具执行时是否注入 _tool_context（由实现函数签名推导，替代人工名单）"""
    impl = _TOOL_IMPLEMENTATIONS.get(name) or _DYNAMIC_IMPLEMENTATIONS.get(name)
    if impl is None:
        return False
    try:
        import inspect
        return "_tool_context" in inspect.signature(impl).parameters
    except (TypeError, ValueError):
        return False


def execute(name: str, arguments: Dict[str, Any], tool_context: Optional[ToolContext] = None) -> tuple:
    """
    执行指定工具。

    Args:
        name: 工具名称
        arguments: 工具参数字典
        tool_context: 工具运行时上下文（回调和状态）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本结果，传给LLM
        - color_diff: 着色diff文本，传给终端展示；空串表示无需展示
    """
    impl = _TOOL_IMPLEMENTATIONS.get(name) or _DYNAMIC_IMPLEMENTATIONS.get(name)
    if impl is None:
        return (error_line(f"未知工具: {name}"), "")

    try:
        if tool_context and _needs_context(name):
            result = impl(**arguments, _tool_context=tool_context)
        else:
            result = impl(**arguments)
        # Edit/Write 返回 (llm_result, color_diff) 元组
        if isinstance(result, tuple):
            llm_result, color_diff = result
        else:
            llm_result, color_diff = result, ""

        # ── 全局输出硬截断（保留首尾：尾部常含提示符等关键状态信息）──
        if tool_context and tool_context.max_tool_output_chars > 0:
            if len(llm_result) > tool_context.max_tool_output_chars:
                original_len = len(llm_result)
                limit_kb = tool_context.max_tool_output_chars // 1024
                max_chars = tool_context.max_tool_output_chars
                head = max_chars * 2 // 3
                tail = max_chars - head
                est = estimate_text_tokens(llm_result)  # ≈token（AI预算单位，混合密度估算）
                llm_result = (
                    llm_result[:head]
                    + f"\n...[全局截断: 输出共{original_len}字符(≈{est}token), 已达全局上限{limit_kb}KB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]"
                    + llm_result[-tail:]
                )

        return (llm_result, color_diff)
    except TypeError as e:
        return (error_line(f"工具参数错误({name}): {_friendly_type_error(name, impl, e)}"), "")
    except Exception as e:
        return (error_line(f"工具执行失败({name}): {e}"), "")


def _friendly_type_error(name: str, impl: Callable, err: TypeError) -> str:
    """把Python原生TypeError转成对LLM友好的中文提示，并列出该工具的有效参数。

    LLM 传错参数名（如 filepath 而非 file_path）或漏传必填参数时，
    原生的英文报错信息虽可读但不够直接；列出有效参数名能让AI一次修正。
    """
    msg = str(err)
    # 提取该工具的有效参数名（排除框架注入的 _tool_context 和别名兼容用的 **kwargs）
    try:
        import inspect
        params = [
            p for p in inspect.signature(impl).parameters
            if p not in ("_tool_context", "kwargs")
        ]
    except Exception:
        params = []

    m = re.search(r"unexpected keyword argument '([^']+)'", msg)
    if m and params:
        return (f"收到未知参数 '{m.group(1)}'。"
                f"{name} 的有效参数: {', '.join(params)}")
    if "missing" in msg:
        # 支持单/多参数缺失: "missing 1 required positional argument: 'a'"
        # 或 "missing 2 required positional arguments: 'a' and 'b'"
        missing = re.findall(r"'([^']+)'", msg)
        if missing and params:
            return (f"缺少必填参数: {', '.join(missing)}。"
                    f"{name} 的有效参数: {', '.join(params)}")
    return msg


# ═══════════════════════════════════════════════════════════════
# 插件工具开关（Terminal/WebSearch/Serial/MCP）
#
# 关闭的语义只有一件事：该工具定义不再随请求发给 LLM（省token + 精简聚焦）。
# 不做执行层拦截；已连接的 SSH/串口/MCP 子进程保持存活，重开后工具立刻回来。
# ═══════════════════════════════════════════════════════════════

# 名单唯一来源：config.defaults.DEFAULT_PLUGIN_TOOLS（此处只做别名，不再重复定义）
PLUGIN_TOOL_NAMES: tuple = DEFAULT_PLUGIN_TOOLS

_disabled_plugins: Set[str] = set()


def _definition_name(definition: Dict) -> str:
    return definition.get("function", {}).get("name") or ""


def resolve_plugin_name(name: str) -> Optional[str]:
    """插件名规范化（大小写不敏感）；非插件名返回 None"""
    target = (name or "").strip().lower()
    for plugin in PLUGIN_TOOL_NAMES:
        if plugin.lower() == target:
            return plugin
    return None


def set_plugin_enabled(name: str, enabled: bool) -> bool:
    """设置插件工具开关。命中已知插件名返回 True，否则 False（不改动任何状态）"""
    canonical = resolve_plugin_name(name)
    if canonical is None:
        return False
    if enabled:
        _disabled_plugins.discard(canonical)
    else:
        _disabled_plugins.add(canonical)
    return True


def is_plugin_enabled(name: str) -> bool:
    canonical = resolve_plugin_name(name)
    return canonical is not None and canonical not in _disabled_plugins


def get_plugin_states() -> Dict[str, bool]:
    """全部插件工具的开关状态（顺序同 PLUGIN_TOOL_NAMES）"""
    return {name: name not in _disabled_plugins for name in PLUGIN_TOOL_NAMES}


def apply_plugin_config(states: Dict[str, bool]) -> None:
    """启动时批量应用配置开关（assembly 注入，须早于 get_tool_definitions()）"""
    for name, enabled in (states or {}).items():
        set_plugin_enabled(name, enabled)


def plugin_definition_names(name: str) -> List[str]:
    """该插件在 LLM 工具表中的全部定义名（MCP 含 mcp__ 动态工具）。非插件名返回 []"""
    canonical = resolve_plugin_name(name)
    if canonical is None:
        return []
    if canonical != "MCP":
        return [canonical]
    names = [canonical]
    names += [n for n in (_definition_name(d) for d in _DYNAMIC_DEFINITIONS) if n]
    return names


def get_plugin_definitions(name: str) -> List[Dict]:
    """取该插件相关定义（不受开关状态过滤），供开启时热恢复到 LLM 工具表"""
    canonical = resolve_plugin_name(name)
    if canonical is None:
        return []
    defs = [d for d in TOOL_DEFINITIONS if _definition_name(d) == canonical]
    if canonical == "MCP":
        defs += list(_DYNAMIC_DEFINITIONS)
    return defs


def get_tool_names() -> List[str]:
    """返回所有工具名称（含运行时动态注册的工具）"""
    return list(_TOOL_IMPLEMENTATIONS.keys()) + list(_DYNAMIC_IMPLEMENTATIONS.keys())


def get_tool_definitions() -> List[Dict]:
    """返回LLM工具定义列表（含运行时动态注册的工具；不含已关闭的插件工具）"""
    defs = [d for d in TOOL_DEFINITIONS if _definition_name(d) not in _disabled_plugins]
    if "MCP" in _disabled_plugins:
        return defs
    return defs + list(_DYNAMIC_DEFINITIONS)
