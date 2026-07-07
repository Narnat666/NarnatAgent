"""
工具注册表 —— 名称→实现映射 + LLM工具定义

每个工具是tools/下的一个子目录，__init__.py导出execute和DEFINITION。
新增工具：新建目录 + 在此处加2行导入。
"""

import json
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
}


# ═══════════════════════════════════════════════════════════════
# LLM工具定义（从各工具模块收集）
# ═══════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[Dict] = [
    READ_DEF, GLOB_DEF, GREP_DEF, EDIT_DEF, WRITE_DEF,
    BASH_DEF, TERMINAL_DEF, WEBSEARCH_DEF, TODOWRITE_DEF,
]


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
        return (f"错误: 未知工具: {name}", "")

    # 需要tool_context的工具：注入上下文参数
    _CONTEXT_TOOLS = {"Shell", "Terminal", "TodoWrite", "WebSearch", "Write", "Read", "Glob", "Grep", "Edit"}

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

        # ── 全局输出硬截断 ──
        if tool_context and tool_context.max_tool_output_chars > 0:
            if len(llm_result) > tool_context.max_tool_output_chars:
                original_len = len(llm_result)
                limit_kb = tool_context.max_tool_output_chars // 1024
                llm_result = (
                    llm_result[:tool_context.max_tool_output_chars]
                    + f"\n...[已截断: 输出共{original_len}字符, 已达全局上限{limit_kb}KB]"
                )

        return (llm_result, color_diff)
    except TypeError as e:
        return (f"错误: 工具参数错误({name}): {e}", "")
    except Exception as e:
        return (f"错误: 工具执行失败({name}): {e}", "")


def get_tool_names() -> List[str]:
    """返回所有工具名称"""
    return list(_TOOL_IMPLEMENTATIONS.keys())


def get_tool_definitions() -> List[Dict]:
    """返回LLM工具定义列表"""
    return TOOL_DEFINITIONS
