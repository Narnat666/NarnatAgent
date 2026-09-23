"""mcp 积木 —— MCP 集成（stdio 客户端 / 连接管理 / 连接通道工具）。

- `mcp.client`：单个服务器的 JSON-RPC 2.0 stdio 客户端（握手、列工具、调用、关闭、
  双策略解码、进程隔离）；
- `mcp.manager`：连接管理器（工具热注册与注销、断线自动重连、并发上限、退出回收、
  工具命名规则）——工具热注册经注入的 `contracts.tool.ToolCatalog` 端口完成；
- `mcp.tool`：`MCP` 连接通道工具（connect/disconnect）与动态工具对象。

装配（app 唯一构造点）：
`McpManager(ToolCatalogImpl(registry, llm.add_tool_definitions, llm.remove_tool_definitions),
signal.error_line, logger)`；`MCP` 工具经 `McpTool(signal.error_line)` 注册进注册表。

分层规则（design D1，机械化检查见 `tests/check_layering.py`）：本积木只依赖
`contracts` 与 `config`，不 import `tools`/`llm` 等同层积木（design D8）。
"""
from __future__ import annotations

from . import client, manager, tool
from .client import (
    CLIENT_NAME,
    CLIENT_TITLE,
    CLIENT_VERSION,
    CLOSE_GRACE_SECONDS,
    KILL_WAIT_SECONDS,
    MAX_LIST_PAGES,
    PROTOCOL_VERSION,
    McpError,
    McpStdioClient,
    decode_line,
    format_content,
)
from .manager import (
    HASH_LEN,
    MAX_NAME_LEN,
    MAX_SERVERS,
    TOOL_PREFIX,
    McpManager,
    make_unique_name,
    sanitize_name_part,
)
from .tool import MCP_DEFINITION, MAX_TOOLS_SHOWN, McpCaller, McpDynamicTool, McpTool

__all__ = [
    # 子模块（`from narnat_agent.mcp import client` 等三条路径）
    "client",
    "manager",
    "tool",
    # client —— 协议常量与客户端
    "CLIENT_NAME",
    "CLIENT_TITLE",
    "CLIENT_VERSION",
    "CLOSE_GRACE_SECONDS",
    "KILL_WAIT_SECONDS",
    "MAX_LIST_PAGES",
    "PROTOCOL_VERSION",
    "McpError",
    "McpStdioClient",
    "decode_line",
    "format_content",
    # manager —— 连接管理与命名规则
    "HASH_LEN",
    "MAX_NAME_LEN",
    "MAX_SERVERS",
    "TOOL_PREFIX",
    "McpManager",
    "make_unique_name",
    "sanitize_name_part",
    # tool —— 连接通道工具与动态工具
    "MCP_DEFINITION",
    "MAX_TOOLS_SHOWN",
    "McpCaller",
    "McpDynamicTool",
    "McpTool",
]
