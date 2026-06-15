"""
工具注册表 —— 名称→实现映射 + LLM工具定义生成
"""

import json
from typing import Dict, List, Any, Callable, Optional

from . import read, glob, grep, edit, write, bash, terminal, web_search, todo_write


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
    "Terminal": terminal.execute,
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
            "description": "Read file content with line numbers. Default max 2000 lines and 128KB total output, truncated with notice if exceeded. limit must be > 0. Use offset+limit to read in chunks. When remote=True, read remote file via SFTP",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute file path"},
                    "offset": {"type": "integer", "description": "Starting line (1-based). Omit to read from beginning"},
                    "limit": {"type": "integer", "description": "Max lines, must be > 0, default 2000"},
                    "remote": {"type": "boolean", "description": "Read remote file via SFTP (requires prior Terminal connect)"},
                    "host": {"type": "string", "description": "Remote host IP (only used when remote=True)"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Search files by name pattern recursively in all subdirectories, e.g. *.py. Results truncated to 500 by default, truncation notice returned when exceeded, increase max_results for full list",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. *.py. Avoid **/*.py unless you are certain there are very few files"},
                    "path": {"type": "string", "description": "Root directory to search. Omit for current working directory"},
                    "max_results": {"type": "integer", "description": "Max result count, default 500. Must be a positive integer"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file content by regex to locate key lines. Results truncated to 100 by default (controlled by head_limit), truncation notice returned when exceeded, increase head_limit for full results",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern"},
                    "path": {"type": "string", "description": "Search path. Suggest specifying a specific directory or file. Use glob to limit file types to avoid excessive output"},
                    "glob": {"type": "string", "description": "Limit file types, e.g. *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "Output format, default files_with_matches",
                    },
                    "i": {"type": "boolean", "description": "Case insensitive"},
                    "n": {"type": "boolean", "description": "Show line numbers (content mode)"},
                    "multiline": {"type": "boolean", "description": "Multiline matching mode"},
                    "A": {"type": "integer", "description": "Show N lines of context after match"},
                    "B": {"type": "integer", "description": "Show N lines of context before match"},
                    "C": {"type": "integer", "description": "Show N lines of context before and after match"},
                    "head_limit": {"type": "integer", "description": "Limit output to first N matches, default 100. Must be a positive integer"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Edit file. Line mode: Edit(file, line_start, line_end, new_string) replace line range. String mode: Edit(file, old_string, new_string) exact match replace. When remote=True, edit remote file via SFTP",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Text to replace (string mode, must match exactly)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all matches (string mode, default replaces first only)"},
                    "line_start": {"type": "integer", "description": "Start line (line mode, 1-based)"},
                    "line_end": {"type": "integer", "description": "End line (line mode, inclusive; 0 or omit equals line_start)"},
                    "remote": {"type": "boolean", "description": "Edit remote file via SFTP (requires prior Terminal connect)"},
                    "host": {"type": "string", "description": "Remote host IP (only used when remote=True)"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Create new file or overwrite entirely. Prefer Edit for modifying existing files. When remote=True, write remote file via SFTP",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Full file content"},
                    "remote": {"type": "boolean", "description": "Write remote file via SFTP (requires prior Terminal connect)"},
                    "host": {"type": "string", "description": "Remote host IP (only used when remote=True)"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": f"Execute shell commands. Syntax: {__import__('sys').platform}. Forbidden for file operations. Output truncated to 2000 chars by default, truncation notice returned when exceeded, increase max_output_chars for full output",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": f"Shell command (syntax: {__import__('sys').platform})"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds, default 120, max 600"},
                    "run_in_background": {"type": "boolean", "description": "Run in background"},
                    "max_output_chars": {"type": "integer", "description": "Max output chars, default 2000. Must be a positive integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Terminal",
            "description": "Multi-terminal persistent SSH, max 5 concurrent terminals. connect to establish session (with optional or auto session_id), exec to run commands on a terminal, input to send interactive input (e.g. sudo password), enabling parallel multi-terminal control. Output truncated to 2000 chars by default, truncation notice returned when exceeded, increase max_output_chars for full output",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["connect", "exec", "input", "status", "close"],
                        "description": "Action, default exec: connect=establish SSH session, exec=execute command, input=send interactive input (e.g. sudo password), status=view sessions, close=close session",
                    },
                    "host": {"type": "string", "description": "Remote host IP or domain"},
                    "username": {"type": "string", "description": "SSH username"},
                    "port": {"type": "integer", "description": "SSH port, default 22"},
                    "key_path": {"type": "string", "description": "SSH private key path, e.g. ~/.ssh/id_rsa"},
                    "password": {"type": "string", "description": "SSH password (key_path takes priority if set)"},
                    "sudo_password": {"type": "string", "description": "Sudo password (set at connect time, auto-injected when subsequent exec encounters sudo prompt)"},
                    "command": {"type": "string", "description": "Command to execute (required when action=exec)"},
                    "input": {"type": "string", "description": "Interactive input (required when action=input, e.g. sudo password, y/n confirmation)"},
                    "timeout": {"type": "integer", "description": "Command timeout in seconds, default 120s. AI will be notified if command is still running after timeout. Set a positive integer to customize"},
                    "session_id": {"type": "integer", "description": "Terminal ID 0-4, default -1 auto-select. Specified or auto-assigned during connect, specifies which terminal on exec"},
                    "max_output_chars": {"type": "integer", "description": "Max output chars, default 2000. Must be a positive integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Web search (AnySearch main engine, searches GitHub code/technical docs/official docs; auto-fallback to Bing+Baidu on failure). Must include Sources links at end of response after searching",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num": {"type": "integer", "description": "Number of results, default 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TodoWrite",
            "description": "Create and manage a structured task list to track progress in current coding session. Required for complex multi-step tasks. Exactly 1 task in_progress at any time",
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
                                    "description": "Task description in imperative mood, e.g. 'Run tests'",
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Progressive form displayed during execution, e.g. 'Running tests'",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Task status",
                                },
                            },
                            "required": ["content", "status", "activeForm"],
                        },
                        "description": "Task list",
                    },
                },
                "required": ["todos"],
            },
        },
    },
]


def execute(name: str, arguments: Dict[str, Any]) -> tuple:
    """
    执行指定工具。

    Args:
        name: 工具名称
        arguments: 工具参数字典

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本结果，传给LLM
        - color_diff: 着色diff文本，传给终端展示；空串表示无需展示
    """
    impl = _TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return (f"错误: 未知工具: {name}", "")

    try:
        result = impl(**arguments)
        # Edit/Write 返回 (llm_result, color_diff) 元组
        if isinstance(result, tuple):
            return result
        # 其他工具返回纯字符串
        return (result, "")
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
