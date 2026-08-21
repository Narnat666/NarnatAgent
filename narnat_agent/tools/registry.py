"""
工具注册表 —— 名称→实现映射 + LLM工具定义

每个工具是tools/下的一个子目录，__init__.py导出execute和DEFINITION。
新增工具：新建目录 + 在此处加2行导入。
"""

import json
import re
from typing import Dict, List, Any, Callable, Optional

from .tool_context import ToolContext

# ── 显式导入各工具（Nuitka安全） ──
from .read import execute as read_execute, DEFINITION as READ_DEF
from .glob import execute as glob_execute, DEFINITION as GLOB_DEF
from .grep import execute as grep_execute, DEFINITION as GREP_DEF
from .edit import execute as edit_execute, DEFINITION as EDIT_DEF
from .write import execute as write_execute, DEFINITION as WRITE_DEF
from .bash import execute as bash_execute, DEFINITION as BASH_DEF
from .terminal import execute as terminal_execute, DEFINITION as TERMINAL_DEF

from .web_search import execute as web_search_execute, DEFINITION as WEBSEARCH_DEF
from .todo_write import execute as todo_write_execute, DEFINITION as TODOWRITE_DEF
from .serial import execute as serial_execute, DEFINITION as SERIAL_DEF
from .goal_complete import execute as goal_complete_execute, DEFINITION as GOALCOMPLETE_DEF


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
    "GoalComplete": goal_complete_execute,
}


# ═══════════════════════════════════════════════════════════════
# LLM工具定义（从各工具模块收集）
# ═══════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[Dict] = [
    READ_DEF, GLOB_DEF, GREP_DEF, EDIT_DEF, WRITE_DEF,
    BASH_DEF, TERMINAL_DEF, WEBSEARCH_DEF, TODOWRITE_DEF,
    SERIAL_DEF,
]
# GoalComplete 不进默认工具定义：由 /goal 开启时通过 LLMClient.set_goal_tool(True) 动态注入，
# 普通模式不暴露给 LLM（执行能力始终注册，兼容历史残留调用）。


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

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
    impl = _TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return (f"[错误: 未知工具: {name}]", "")

    # 需要tool_context的工具：注入上下文参数
    _CONTEXT_TOOLS = {"Shell", "Terminal", "TodoWrite", "WebSearch", "Write", "Read", "Glob", "Grep", "Edit", "Serial", "GoalComplete"}

    try:
        if tool_context and name in _CONTEXT_TOOLS:
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
                llm_result = (
                    llm_result[:head]
                    + f"\n...[全局截断: 输出共{original_len}字符, 已达全局上限{limit_kb}KB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]"
                    + llm_result[-tail:]
                )

        return (llm_result, color_diff)
    except TypeError as e:
        return (f"[错误: 工具参数错误({name}): {_friendly_type_error(name, impl, e)}]", "")
    except Exception as e:
        return (f"[错误: 工具执行失败({name}): {e}]", "")


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


def get_tool_names() -> List[str]:
    """返回所有工具名称"""
    return list(_TOOL_IMPLEMENTATIONS.keys())


def get_tool_definitions() -> List[Dict]:
    """返回LLM工具定义列表"""
    return TOOL_DEFINITIONS
