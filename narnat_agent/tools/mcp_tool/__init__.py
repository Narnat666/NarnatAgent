"""MCP 工具 —— 纯连接通道（connect / disconnect）

只做两件事：用 AI 给的配置把 MCP 服务器连成通道（工具热注册，立即可用）、
以及断开通道。不查配置、不做候选、不教 AI 去哪找 —— 服务器信息来源由 AI 负责。
"""

from ...mcp import McpError
from ..exec_signal import error_line

DEFINITION = {
    "type": "function",
    "function": {
        "name": "MCP",
        "description": (
            "MCP 服务器连接通道：connect 连接一个本地 MCP（stdio）服务器，连接后其工具以 "
            "mcp__<服务器名>__<工具名> 注册，后续轮次可直接调用；"
            "disconnect 断开并注销其工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "disconnect"],
                    "description": "操作类型（默认connect）",
                },
                "name": {
                    "type": "string",
                    "description": "服务器名（成为工具名前缀 mcp__<name>__）；disconnect 用已连接的服务器名或 all（断开全部）",
                },
                "config": {
                    "type": "object",
                    "description": ("connect 的启动配置：{\"command\": \"python\", "
                                    "\"args\": [\"main.py\"], \"env\": {}, \"cwd\": \"\"}"
                                    "（中英文键均可；可选 startup_timeout_sec/tool_timeout_sec/"
                                    "enabled_tools/disabled_tools）"),
                },
            },
            "required": [],
        },
    },
}

_MAX_TOOLS_SHOWN = 20       # connect 结果里工具名展示上限


def execute(action: str = "connect", name: str = "", config: dict = None,
            _tool_context=None) -> str:
    """MCP 连接通道（参数说明见 DEFINITION）"""
    manager = getattr(_tool_context, "mcp_manager", None) if _tool_context else None
    if manager is None:
        return error_line("MCP 管理器未初始化")

    action = (action or "connect").strip().lower()
    try:
        if action == "connect":
            return _connect(manager, name, config)
        if action == "disconnect":
            return _disconnect(manager, name)
        return error_line(f"未知 action: {action}（可用: connect/disconnect）")
    except McpError as e:
        return error_line(f"MCP {action} 失败: {e}")
    except Exception as e:
        return error_line(f"MCP {action} 异常: {e}")


def _connect(manager, name: str, config: dict) -> str:
    name = str(name or "").strip()
    if not name:
        return error_line("connect 需要 name（服务器名）")
    if not isinstance(config, dict) or not config:
        return error_line("connect 需要 config（启动配置：command/args/env/cwd）")

    from ...config.loader import parse_mcp_server
    cfg = parse_mcp_server(name, config)
    if cfg is None or not cfg.command.strip():
        return error_line("config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器")

    count, tool_names = manager.connect(name, config)
    lines = [f"[已连接 MCP服务器 {name}：注册 {count} 个工具，后续轮次可直接调用]"]
    for tool_name in tool_names[:_MAX_TOOLS_SHOWN]:
        lines.append(f"  {tool_name}")
    if len(tool_names) > _MAX_TOOLS_SHOWN:
        lines.append(f"  …共{len(tool_names)}个")
    return "\n".join(lines)


def _disconnect(manager, name: str) -> str:
    name = str(name or "").strip()
    if not name:
        return error_line("disconnect 需要 name（已连接的服务器名，或 all 断开全部）")
    if name.lower() in ("all", "*", "全部"):
        removed = manager.disconnect("all")
        return f"[已断开全部 MCP服务器，注销 {removed} 个工具]"

    connected = [c["name"] for c in manager.connected()]
    target = name if name in connected else next(
        (n for n in connected if n.lower() == name.lower()), None)
    if target is None:
        return error_line(f"服务器 {name} 未连接。当前已连接: "
                          f"{'、'.join(connected) if connected else '(无)'}")
    removed = manager.disconnect(target)
    return f"[已断开 MCP服务器 {target}，注销 {removed} 个工具]"
