"""MCP 工具 —— 连接通道工具（connect/disconnect）与运行时动态工具对象。

契约来源：specs/mcp「MCP 工具名与参数契约」「MCP connect 前置校验与结果文案」
「MCP disconnect 行为与结果文案」「MCP 工具调用协议与结果格式化」「MCP 通道保活与
自动重连」「失败不阻塞原则与错误文案」。工具定义与全部文案搬运自旧实现
`narnat_agent/tools/mcp_tool/__init__.py`（逐字一致）。

结构要点：
- `McpTool`：固定工具名 `MCP` 的连接通道，经 `env.mcp`（`contracts.tool.McpPort`）
  访问连接管理——运行时上下文缺失或未初始化时返回「MCP 管理器未初始化」；
- `McpDynamicTool`：连接成功后按命名规则热注册的 MCP 工具（`contracts.tool.Tool`
  协议实现），调用转发到管理器的调用通道；失败（含 `isError` 响应）转带标签错误行，
  不向会话抛异常；
- 错误行生成器由装配点构造注入（`tools.signal.error_line`），mcp 积木不依赖 tools。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..config import parse_mcp_server
from ..contracts.tool import ToolDefinition, ToolEnv, ToolResult
from .client import McpError

__all__ = ["MCP_DEFINITION", "MAX_TOOLS_SHOWN", "McpCaller", "McpDynamicTool", "McpTool"]

MAX_TOOLS_SHOWN = 20
"""connect 结果里工具名展示上限（超出时只展示前 20 个并追加合计行）。"""

MCP_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "MCP",
        "description": "MCP 服务器连接通道（仅支持本地 stdio 型服务器）。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "disconnect"],
                    "description": (
                        "操作类型（默认connect）。"
                        "connect 连接一个本地 MCP（stdio）服务器，连接后其工具以 "
                        "mcp__<服务器名>__<工具名> 注册，后续轮次可直接调用；"
                        "disconnect 断开并注销其工具"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "服务器名（成为工具名前缀 mcp__<name>__）；"
                        "disconnect 用已连接的服务器名或 all（断开全部）"
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "connect 的启动配置：\n"
                        "{\"command\": \"python\", \"args\": [\"main.py\"], "
                        "\"env\": {}, \"cwd\": \"\"}\n"
                        "（中英文键均可；可选 startup_timeout_sec 启动超时、"
                        "tool_timeout_sec 工具调用超时、"
                        "enabled_tools/disabled_tools 工具白/黑名单）"
                    ),
                },
            },
            "required": [],
        },
    },
}


class McpCaller(Protocol):
    """调用通道协议（`McpManager` 满足）：经通道转发一次工具调用。"""

    def call_tool(self, server_name: str, tool_name: str, arguments: dict,
                  timeout: float) -> tuple[str, bool]:
        """返回 `(格式化文本, isError 标志)`；传输/协议类错误抛 `McpError`。"""
        ...


class McpTool:
    """`MCP` 连接通道工具（connect / disconnect），`contracts.tool.Tool` 协议实现。

    只做两件事：用 AI 给的配置把 MCP 服务器连成通道（工具热注册、立即可用）、
    以及断开通道；服务器信息来源由 AI 负责。任一失败均转为错误行（不阻塞会话）。
    """

    name = "MCP"

    def __init__(self, error_line: Callable[[str], str]) -> None:
        """`error_line`：框架错误行生成器（带进程级随机标签），装配点注入。"""
        self._error_line = error_line

    def definition(self) -> ToolDefinition:
        """LLM 工具定义（固定文案契约）。"""
        return MCP_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """执行 connect/disconnect 分支，返回结果文本或框架错误行。

        `action` 缺失或为空（含纯空白）按 `connect`；执行前去除首尾空白并小写归一；
        其它取值返回「未知 action」错误行；意外异常包装为「MCP {action} 异常」错误行。
        """
        manager = env.mcp if env is not None else None
        if manager is None:
            return ToolResult(llm_text=self._error_line("MCP 管理器未初始化"))

        action = str(args.get("action") or "").strip().lower() or "connect"
        try:
            if action == "connect":
                return ToolResult(
                    llm_text=self._connect(manager, args.get("name"), args.get("config")))
            if action == "disconnect":
                return ToolResult(llm_text=self._disconnect(manager, args.get("name")))
            return ToolResult(llm_text=self._error_line(
                f"未知 action: {action}（可用: connect/disconnect）"))
        except McpError as e:
            return ToolResult(llm_text=self._error_line(f"MCP {action} 失败: {e}"))
        except Exception as e:
            return ToolResult(llm_text=self._error_line(f"MCP {action} 异常: {e}"))

    def _connect(self, manager, name, config) -> str:
        """connect 分支：前置校验（name/config/command）→ 连接 → 成功清单展示。"""
        name = str(name or "").strip()
        if not name:
            return self._error_line("connect 需要 name（服务器名）")
        if not isinstance(config, dict) or not config:
            return self._error_line("connect 需要 config（启动配置：command/args/env/cwd）")
        cfg = parse_mcp_server(name, config)
        if cfg is None or not cfg.command.strip():
            return self._error_line("config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器")

        count, tool_names = manager.connect(name, config)
        lines = [f"[已连接 MCP服务器 {name}：注册 {count} 个工具，后续轮次可直接调用]"]
        for tool_name in tool_names[:MAX_TOOLS_SHOWN]:
            lines.append(f"  {tool_name}")
        if len(tool_names) > MAX_TOOLS_SHOWN:
            lines.append(f"  …共{len(tool_names)}个")
        return "\n".join(lines)

    def _disconnect(self, manager, name) -> str:
        """disconnect 分支：`all`/`*`/`全部` 断开全部；指定名按大小写不敏感匹配。"""
        name = str(name or "").strip()
        if not name:
            return self._error_line("disconnect 需要 name（已连接的服务器名，或 all 断开全部）")
        if name.lower() in ("all", "*", "全部"):
            removed = manager.disconnect("all")
            return f"[已断开全部 MCP服务器，注销 {removed} 个工具]"

        connected = [server["name"] for server in manager.connected()]
        target = name if name in connected else next(
            (n for n in connected if n.lower() == name.lower()), None)
        if target is None:
            return self._error_line(f"服务器 {name} 未连接。当前已连接: "
                                    f"{'、'.join(connected) if connected else '(无)'}")
        removed = manager.disconnect(target)
        return f"[已断开 MCP服务器 {target}，注销 {removed} 个工具]"


class McpDynamicTool:
    """连接成功后注册的 MCP 工具（服务端工具 → LLM 可见工具）。

    调用转发到管理器的 `call_tool`（含断线自动重连）；失败一律转带标签错误行：
    `isError` 响应对应 `[错误: {文本或「工具返回错误」}]`、传输/协议错误对应
    `[错误: MCP工具 {服务器}/{原始工具} 调用失败: {错误文本}]`；空结果以
    `(工具无输出)` 兜底。调用转发使用服务端原始工具名（LLM 名只作注册可见名）。
    """

    def __init__(self, manager: McpCaller, definition: ToolDefinition, server_name: str,
                 raw_name: str, timeout: float, error_line: Callable[[str], str]) -> None:
        self._manager = manager
        self._definition = definition
        self._server_name = server_name
        self._raw_name = raw_name
        self._timeout = timeout
        self._error_line = error_line

    @property
    def name(self) -> str:
        """LLM 可见工具名（`mcp__<服务器>__<工具>` 形态，唯一且 ≤64 字符）。"""
        return self._definition["function"]["name"]

    def definition(self) -> ToolDefinition:
        """LLM 工具定义（名称/描述/输入模式）。"""
        return self._definition

    def execute(self, args: dict[str, object], env: ToolEnv | None = None) -> ToolResult:
        """经通道转发一次调用；失败不抛异常（转错误行）。"""
        try:
            text, is_error = self._manager.call_tool(
                self._server_name, self._raw_name, dict(args or {}), self._timeout)
        except McpError as e:
            return ToolResult(llm_text=self._error_line(
                f"MCP工具 {self._server_name}/{self._raw_name} 调用失败: {e}"))
        if is_error:
            return ToolResult(llm_text=self._error_line(text or "工具返回错误"))
        return ToolResult(llm_text=text or "(工具无输出)")
