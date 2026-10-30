"""MCP 工具 —— 连接 + 统一调度（工具表静态化）

工具定义恒为一项（action 枚举），服务端工具不再展开成 mcp__<服务器>__<工具>：
connect 建通道并返回工具目录，call 按名字调度，忘了细节用 list_servers / list_tools 查。
工具表因此与连接状态完全解耦——缓存前缀（system → tools → messages）不会因连接动作失效。
"""

from ...mcp import McpError
from ..exec_signal import error_line

DEFINITION = {
    "type": "function",
    "function": {
        "name": "MCP",
        "description": (
            "MCP服务器连接与统一调度：连接后保持，服务端工具经 call 按名转发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "call", "disconnect", "list_servers", "list_tools"],
                    "description": (
                        "操作类型。默认 connect。"
                        "connect 连接服务器并返回工具目录；"
                        "call 调度该服务器的一个工具；"
                        "disconnect 断开（name 可填 all）；"
                        "list_servers 列出已连服务器（存活状态、工具数）；"
                        "list_tools 列出某服务器的工具名、描述与参数签名。"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "服务器名。connect 时自定义并作为后续调度句柄；"
                                 "call / list_tools / disconnect 用它指定目标。",
                },
                "config": {
                    "type": "object",
                    "description": (
                        "connect 的连接配置（中英文键均可）：本地 {\"command\",\"args\",\"env\",\"cwd\"}，"
                        "远程 {\"url\",\"headers\"}。可选 startup_timeout_sec、tool_timeout_sec、"
                        "enabled_tools/disabled_tools。"
                    ),
                },
                "tool": {
                    "type": "string",
                    "description": "call 的服务端工具名（取自 connect 返回目录或 list_tools）。",
                },
                "arguments": {
                    "type": "object",
                    "description": "call 的工具参数对象，原样转发给服务端；键与类型见 list_tools 的参数签名。",
                },
                "timeout": {
                    "type": "number",
                    "description": "call 的超时秒数。默认用连接时的 tool_timeout_sec。",
                },
            },
            "required": [],
        },
    },
}


CAPABILITY = {
    "label": "MCP",
    "dispatch": "serial",
    "summary": "mcp",
    # call 会原样透传服务端文本：失败判定只认框架不可伪造标签
    "trusted_output": False,
    "plugin_label": "MCP 服务器管理",
}

_MAX_TOOLS_SHOWN = 20       # connect 结果里工具名展示上限
_DESC_ONE_LINE = 80         # 目录里的一句话描述上限（压成单行后截断）


def execute(action: str = "connect", name: str = "", config: dict = None,
            tool: str = "", arguments: dict = None, timeout: float = None,
            _tool_context=None) -> str:
    """MCP 连接与统一调度（参数说明见 DEFINITION）"""
    manager = getattr(_tool_context, "mcp_manager", None) if _tool_context else None
    if manager is None:
        return error_line("MCP 管理器未初始化")

    action = (action or "connect").strip().lower()
    name = str(name or "").strip()
    try:
        if action == "connect":
            return _connect(manager, name, config)
        if action == "call":
            return _call(manager, name, tool, arguments, timeout)
        if action == "list_servers":
            return _list_servers(manager)
        if action == "list_tools":
            return _list_tools(manager, name)
        if action == "disconnect":
            return _disconnect(manager, name)
        return error_line(f"未知 action: {action}"
                          f"（可用: connect/call/disconnect/list_servers/list_tools）")
    except McpError as e:
        return error_line(f"MCP {action} 失败: {e}")
    except Exception as e:
        return error_line(f"MCP {action} 异常: {e}")


def _connect(manager, name: str, config: dict) -> str:
    if not name:
        return error_line("connect 需要 name（服务器名，作为后续调度句柄）")
    if not isinstance(config, dict) or not config:
        return error_line("connect 需要 config（连接配置：command/args/env/cwd 或 url/headers）")

    from ...config.loader import parse_mcp_server
    cfg = parse_mcp_server(name, config)
    if cfg is None or not (cfg.url.strip() or cfg.command.strip()):
        return error_line("未配置启动命令（\"命令\"/\"command\"）或远程地址（\"地址\"/\"url\"）")

    count, tool_names = manager.connect(name, config)
    if count == 0:
        return (f"[已连接 MCP服务器 {name}：没有可用工具"
                f"（检查 enabled_tools/disabled_tools 或服务端 tools/list）]")

    metas = {m["name"]: m for m in manager.server_tools(name)}
    lines = [f"[已连接 MCP服务器 {name}：注册 {count} 个工具，后续用 action=\"call\" 调度]"]
    for tool_name in list(tool_names)[:_MAX_TOOLS_SHOWN]:
        desc = _one_line((metas.get(tool_name) or {}).get("description"))
        lines.append(f"  {tool_name}  {desc or '(无描述)'}")
    if count > _MAX_TOOLS_SHOWN:
        lines.append(f"  ...共{count}个（用 action=\"list_tools\" 查全部）")
    return "\n".join(lines)


def _call(manager, name: str, tool: str, arguments: dict, timeout) -> str:
    if not name:
        return error_line("call 需要 name（服务器名）。先 action=\"list_servers\" 查已连接服务器")
    tool = str(tool or "").strip()
    if not tool:
        return error_line(f"call 需要 tool（服务端工具名）。"
                          f"先 action=\"list_tools\"（name=\"{name}\"）查可用工具")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return error_line("call 的 arguments 需要是对象（键值对），不是字符串")
    try:
        timeout = float(timeout) if timeout not in (None, "", 0) else None
    except (TypeError, ValueError):
        return error_line(f"timeout 需为秒数，收到 {timeout!r}")
    if timeout is not None and timeout <= 0:
        timeout = None

    known = {m["name"] for m in manager.server_tools(name)}
    if not known:
        if not any(c["name"] == name for c in manager.connected()):
            return error_line(f"服务器 {name} 未连接。先 action=\"connect\" 连接，"
                              f"或 action=\"list_servers\" 查看已连接服务器")
        return error_line(f"服务器 {name} 没有可用工具。"
                          f"先 action=\"list_tools\"（name=\"{name}\"）查看")
    if tool not in known:
        return error_line(f"服务器 {name} 没有工具 {tool}。"
                          f"先 action=\"list_tools\"（name=\"{name}\"）查可用工具名再 call")

    return manager.call_tool(name, tool, arguments, timeout)


def _list_servers(manager) -> str:
    servers = manager.connected()
    if not servers:
        return "[已连接 MCP服务器：0 个（用 action=\"connect\" 连接）]"
    lines = [f"[已连接 MCP服务器：{len(servers)} 个]"]
    for server in servers:
        state = "存活" if server.get("alive") else "连接已断（下次 call 会自动重连）"
        lines.append(f"  {server['name']}  {state}  {server.get('tool_count', 0)} 个工具")
    return "\n".join(lines)


def _list_tools(manager, name: str) -> str:
    if not name:
        return error_line("list_tools 需要 name（服务器名）。"
                          "先 action=\"list_servers\" 查已连接服务器")
    tools = manager.server_tools(name)
    if not tools:
        connected = [c["name"] for c in manager.connected()]
        if name not in connected:
            return error_line(f"服务器 {name} 未连接。当前已连接: "
                              f"{'、'.join(connected) if connected else '(无)'}")
        return f"[MCP服务器 {name} 没有可用工具]"
    lines = [f"[MCP服务器 {name} 工具清单：{len(tools)} 个]"]
    for tool in tools:
        desc = _one_line(tool.get("description"))
        lines.append(f"  {tool['name']}  {desc or '(无描述)'}")
        lines.append(f"    参数: {_params_signature(tool.get('inputSchema'))}")
    return "\n".join(lines)


def _disconnect(manager, name: str) -> str:
    if not name:
        return error_line("disconnect 需要 name（已连接的服务器名，或 all 断开全部）")
    if name.lower() in ("all", "*", "全部"):
        names = [c["name"] for c in manager.connected()]
        if not names:
            return "[当前无已连接的 MCP服务器，无需断开]"
        count = manager.disconnect("all")
        return (f"[已断开全部 MCP服务器：{len(names)} 个"
                f"（{'、'.join(names)}），共 {count} 个工具不再可用]")

    connected = [c["name"] for c in manager.connected()]
    target = name if name in connected else next(
        (n for n in connected if n.lower() == name.lower()), None)
    if target is None:
        return error_line(f"服务器 {name} 未连接。当前已连接: "
                          f"{'、'.join(connected) if connected else '(无)'}")
    count = manager.disconnect(target)
    return f"[已断开 MCP服务器 {target}：{count} 个工具不再可用]"


def _one_line(text, limit: int = _DESC_ONE_LINE) -> str:
    """描述压成一句话（去换行/多余空白，超长截断）"""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _params_signature(schema) -> str:
    """inputSchema → 参数签名文本（name: type + 必填标记）"""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return "(无)"
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    parts = []
    for key, spec in props.items():
        ptype = str((spec or {}).get("type") or "any") if isinstance(spec, dict) else "any"
        parts.append(f"{key}: {ptype}{'(必填)' if key in required else ''}")
    return ", ".join(parts)
