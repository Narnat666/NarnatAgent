"""
工具注册表 —— 名称→实现映射 + LLM工具定义生成
"""

import json
from typing import Dict, List, Any, Callable, Optional

from . import read, glob, grep, edit, write, bash, web_search, todo_write


# ═══════════════════════════════════════════════════════════════
# 工具实现映射
# ═══════════════════════════════════════════════════════════════

_TOOL_IMPLEMENTATIONS: Dict[str, Callable] = {
    "Read": read.execute,
    "Glob": glob.execute,
    "Grep": grep.execute,
    "Edit": edit.execute,
    "Write": write.execute,
    "Shell": bash.execute,
    "WebSearch": web_search.execute,
    "TodoWrite": todo_write.execute,
}


# ═══════════════════════════════════════════════════════════════
# LLM工具定义（传给LLM的tools参数）
# ═══════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "读取文件内容，带行号。默认读全文，offset/limit仅用于大文件分段",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "offset": {"type": "integer", "description": "起始行号(1-based)，省略则从头读"},
                    "limit": {"type": "integer", "description": "最大行数，省略则读全文"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "按文件名模式搜索文件，如 **/*.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob模式，如 **/*.py"},
                    "path": {"type": "string", "description": "搜索根目录，省略为当前工作目录"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "按正则搜索文件内容，定位关键行",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "搜索路径，可以是目录或文件，省略为当前工作目录"},
                    "glob": {"type": "string", "description": "限定文件类型，如 *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "输出格式，默认files_with_matches",
                    },
                    "i": {"type": "boolean", "description": "忽略大小写"},
                    "n": {"type": "boolean", "description": "显示行号(content模式)"},
                    "multiline": {"type": "boolean", "description": "多行匹配模式"},
                    "A": {"type": "integer", "description": "匹配行后显示N行上下文"},
                    "B": {"type": "integer", "description": "匹配行前显示N行上下文"},
                    "C": {"type": "integer", "description": "匹配行前后各显示N行上下文"},
                    "head_limit": {"type": "integer", "description": "限制输出前N条匹配结果"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "修改文件。行号模式: Edit(file, line_start, line_end, new_string) 替换指定行范围，N行换N行可不重读连续Edit，N行换M行(N≠M)后必须重新Read；字符串模式: Edit(file, old_string, new_string) 精确替换匹配文本",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "old_string": {"type": "string", "description": "要替换的原文（字符串模式，必须精确匹配）"},
                    "new_string": {"type": "string", "description": "替换后的新文"},
                    "replace_all": {"type": "boolean", "description": "替换所有匹配（字符串模式，默认只替换第一个）"},
                    "line_start": {"type": "integer", "description": "起始行号（行号模式，从1开始）"},
                    "line_end": {"type": "integer", "description": "结束行号（行号模式，含此行；0或省略则等于line_start）"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "创建新文件或完整覆写文件。修改已有文件应优先用Edit",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": f"执行shell命令。语法: {__import__('sys').platform}。禁止用于文件操作",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": f"shell命令(语法: {__import__('sys').platform})"},
                    "description": {"type": "string", "description": "命令描述(5-10字)"},
                    "timeout": {"type": "integer", "description": "超时毫秒数，默认120000，最大600000"},
                    "run_in_background": {"type": "boolean", "description": "后台运行"},
                    "dangerouslyDisableSandbox": {"type": "boolean", "description": "禁用沙箱"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "联网搜索，获取实时信息或在线文档",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num": {"type": "integer", "description": "返回结果数，默认5"},
                    "lr": {"type": "string", "description": "语言限制，如lang_en/lang_zh-CN"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TodoWrite",
            "description": "创建和管理结构化任务列表，用于跟踪当前编码会话的进度。复杂多步骤任务必须使用。任何时刻恰好只有1个任务处于in_progress状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "任务描述，祈使语气，如'Run tests'",
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "进行时形式，执行时显示，如'Running tests'",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "任务状态",
                                },
                            },
                            "required": ["content", "status", "activeForm"],
                        },
                        "description": "任务列表",
                    },
                },
                "required": ["todos"],
            },
        },
    },
]


def execute(name: str, arguments: Dict[str, Any]) -> str:
    """
    执行指定工具。

    Args:
        name: 工具名称
        arguments: 工具参数字典

    Returns:
        工具执行结果字符串
    """
    impl = _TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return f"错误: 未知工具: {name}"

    try:
        return impl(**arguments)
    except TypeError as e:
        return f"错误: 工具参数错误({name}): {e}"
    except Exception as e:
        return f"错误: 工具执行失败({name}): {e}"


def get_tool_names() -> List[str]:
    """返回所有工具名称"""
    return list(_TOOL_IMPLEMENTATIONS.keys())


def get_tool_definitions() -> List[Dict]:
    """返回LLM工具定义列表"""
    return TOOL_DEFINITIONS
