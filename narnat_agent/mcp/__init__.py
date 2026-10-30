"""MCP 集成 —— 连接通道（stdio 子进程 / Streamable HTTP，JSON-RPC 2.0）

职责只有一个：把 AI 给的服务器配置变成可持续的通道，并保证断开与回收干净。
- 连接：本地子进程或远程 URL → initialize 握手 → tools/list → 记录工具定义清单
  （清单只作目录查询用：工具定义绝不注入 LLM 工具表，调度统一走 MCP 工具的
  action="call"，工具表因此与连接状态完全解耦，缓存前缀不会被连接动作作废）
- 传输选择：配置里 url 非空走远程 Streamable HTTP，否则启动本地 stdio 子进程
- 通道保活：通道中断（服务端进程退出 / 请求失败）时，用连接时记录的配置自动重连
  （要求 AI 重试，不重复执行调用），AI 无需重新提供配置
- 回收：连接/断开、会话结束、异常退出（atexit）都保证子进程不残留
"""

import atexit
import threading

from .client import McpError, McpStdioClient
from .http_client import McpHttpClient
from ..config.defaults import DEFAULT_MCP_TOOL_TIMEOUT
from ..config.loader import parse_mcp_server

_MAX_SERVERS = 8   # 并发 MCP 服务器上限（与后台任务槽位同源：防进程表失控增长）

__all__ = ["McpManager", "McpError"]


class McpManager:
    """MCP 服务器运行时连接管理（AI 按需连接，与 Terminal 同构）

    narnat.json 不配置服务器名单：AI 需要什么就连什么（connect），
    用完可断开（disconnect），进程随会话结束回收。
    """

    def __init__(self, logger=None):
        self._logger = logger
        self._clients = {}          # 服务器名 → 客户端（供退出清理）
        self._server_tools = {}     # 服务器名 → {服务端工具名: {description, inputSchema}}
        self._specs = {}            # 服务器名 → 连接时记录的配置（自动重连用）
        self._tool_timeouts = {}    # 服务器名 → 默认工具调用超时（秒）
        self._lock = threading.Lock()
        self._closed = False
        self._atexit_registered = False

    def cleanup(self) -> None:
        """关闭全部 MCP 连接（服务端子进程退出）。程序退出路径调用"""
        with self._lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
            self._server_tools.clear()
            self._specs.clear()
            self._tool_timeouts.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # 运行时接口（MCP 工具调用：AI 想连谁连谁）
    # ═══════════════════════════════════════════════════════════

    def connected(self) -> list:
        """当前已连接的服务器：[{"name", "alive", "tool_count"}]

        供 list_servers 与断开动作使用（AI 看不到的服务器清单由 AI 自己掌握）。
        """
        with self._lock:
            return [{"name": name,
                     "alive": bool(client.alive),
                     "tool_count": len(self._server_tools.get(name, {}))}
                    for name, client in self._clients.items()]

    def server_tools(self, name: str) -> list:
        """该服务器可用工具的完整定义：[{"name", "description", "inputSchema"}]

        未连接返回 []。这是 list_tools 与 connect 返回值的唯一数据源。
        """
        name = str(name or "").strip()
        with self._lock:
            tools = dict(self._server_tools.get(name) or {})
        return [{"name": tool_name,
                 "description": meta.get("description") or "",
                 "inputSchema": meta.get("inputSchema") or {}}
                for tool_name, meta in tools.items()]

    def connect(self, name: str, spec: dict) -> tuple:
        """运行时连接一个服务器（幂等：同名已连接且存活则直接返回）。

        Args:
            name: 服务器名（后续 call / list_tools / disconnect 的句柄）
            spec: 本地 {"command","args","env","cwd",...} 或远程 {"url","headers",...}，
                  另支持 startup_timeout_sec / tool_timeout_sec / 工具白黑名单等
        Returns: (可用工具数, [服务端工具名, ...])
        """
        name = str(name or "").strip()
        if not name:
            raise McpError("缺少服务器名（name）")
        cfg = parse_mcp_server(name, dict(spec or {}, 启用=True))
        if cfg is None:
            raise McpError("配置非法")

        with self._lock:
            if self._closed:
                raise McpError("程序正在退出，无法连接")
            client = self._clients.get(name)
            if client is None and len(self._clients) >= _MAX_SERVERS:
                raise McpError(
                    f"已达并发上限({_MAX_SERVERS}个)，当前已连接: "
                    f"{'、'.join(self._clients)}。请先 disconnect 不需要的服务器"
                )
        if client is not None and client.alive:
            with self._lock:
                tools = list(self._server_tools.get(name) or {})
            return len(tools), tools
        if client is not None:
            # 连接已死：先清掉旧通道（关死进程、丢弃旧清单）再重建
            self.disconnect(name)

        connected = self._connect(cfg)          # 可能耗时（spawn+握手+列工具），不持锁
        client, tools = connected

        with self._lock:
            if self._closed:
                client.close()
                raise McpError("程序正在退出，无法连接")
            existing_tools = None
            if self._clients.get(name) is None:
                # 检查 + 登记在同一锁内原子完成（赢家路径）：
                # 并发同名 connect 只有一个能插入，其余走 existing_tools 分支丢弃
                self._clients[name] = client
                self._server_tools[name] = {
                    raw_name: {
                        "description": str(tool.get("description") or "").strip(),
                        "inputSchema": tool.get("inputSchema")
                        if isinstance(tool.get("inputSchema"), dict) else {},
                    }
                    for raw_name, tool in ((_tool_name(t), t) for t in tools)
                    if _tool_allowed(cfg, raw_name)
                }
                self._specs[name] = dict(spec or {})
                self._tool_timeouts[name] = cfg.tool_timeout
            else:
                # 并发同名 connect：已有连接胜出，丢弃本次新建的进程
                existing_tools = list(self._server_tools.get(name) or {})
        if existing_tools is not None:
            client.close()
            return len(existing_tools), existing_tools
        # 兜底：走不到 agent finally 的异常退出路径（输入界面 Ctrl+C 等）
        # 也能在解释器关闭时回收服务端子进程
        if not self._atexit_registered:
            atexit.register(self.cleanup)
            self._atexit_registered = True
        tool_names = list(self._server_tools.get(name) or {})
        self._log(f"{name}: 已连接，{len(tool_names)} 个工具可用")
        return len(tool_names), tool_names

    def disconnect(self, name: str) -> int:
        """断开服务器，返回其工具数。name 为 "all" 时断开全部"""
        name = str(name or "").strip()
        if name.lower() in ("all", "*", "全部"):
            with self._lock:
                names = list(self._clients.keys())
            count = 0
            for server in names:
                count += self.disconnect(server)
            return count

        with self._lock:
            client = self._clients.pop(name, None)
            tools = self._server_tools.pop(name, {})
            self._specs.pop(name, None)
            self._tool_timeouts.pop(name, None)
            if client is None:
                raise McpError(f"服务器 {name} 未连接")
        try:
            client.close()
        except Exception:
            pass
        self._log(f"{name}: 已断开，{len(tools)} 个工具不再可用")
        return len(tools)

    # ═══════════════════════════════════════════════════════════
    # 通道（调用转发 + 保活）
    # ═══════════════════════════════════════════════════════════

    def call_tool(self, server_name: str, tool_name: str, arguments: dict,
                  timeout: float = None) -> str:
        """经通道转发一次工具调用。

        保活语义：发现连接中断时，用连接时记录的配置自动重连，并要求 AI 重试
        （不代它重跑调用 —— 仿真/写操作绝不能悄悄执行两次）。
        timeout 缺省（None/<=0）时用连接时的 tool_timeout_sec。
        """
        with self._lock:
            if self._closed:
                raise McpError("程序正在退出")
            client = self._clients.get(server_name)
            if not timeout or timeout <= 0:
                timeout = self._tool_timeouts.get(server_name) or DEFAULT_MCP_TOOL_TIMEOUT
        if client is None:
            raise McpError(f"服务器 {server_name} 未连接")
        if not client.alive:
            self._reconnect(server_name)
            raise McpError("连接曾中断，已自动重连，请重试本次调用")
        try:
            return client.call_tool(tool_name, arguments, timeout)
        except McpError:
            if client.alive:
                raise          # 服务端自身错误/调用超时：原样上报
            self._reconnect(server_name)    # 调用期间进程中断：先修好通道
            raise McpError("连接曾中断，已自动重连，请重试本次调用")

    def _reconnect(self, server_name: str) -> None:
        """用连接时记录的配置重建通道（工具清单不变，只换连接）"""
        with self._lock:
            spec = dict(self._specs.get(server_name) or {})
        if not spec:
            raise McpError(f"服务器 {server_name} 无配置，无法自动重连")
        cfg = parse_mcp_server(server_name, dict(spec, 启用=True))
        if cfg is None or not (cfg.url.strip() or cfg.command.strip()):
            raise McpError(f"服务器 {server_name} 配置不完整，无法自动重连")
        client, _tools = self._connect(cfg)     # 新通道 + 握手 + 列工具
        with self._lock:
            old = self._clients.get(server_name)
            if old is None:
                # 重连期间已被 disconnect（或程序退出清理）：
                # 丢弃新建通道，不复活已断开的服务器
                orphan = client
            else:
                self._clients[server_name] = client
                orphan = None
        if orphan is not None:
            try:
                orphan.close()
            except Exception:
                pass
            raise McpError(f"服务器 {server_name} 已断开，无需重连")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._log(f"{server_name}: 连接中断，已自动重连")

    # ═══════════════════════════════════════════════════════════
    # 内部实现
    # ═══════════════════════════════════════════════════════════

    def _connect(self, cfg):
        """连接单个服务器：建通道（本地子进程 / 远程 HTTP）→ 握手 → 列工具"""
        if cfg.url.strip():
            client = McpHttpClient(cfg.name, cfg.url, headers=cfg.headers,
                                   logger=self._logger)
        else:
            if not cfg.command.strip():
                raise McpError("未配置启动命令（\"命令\"/\"command\"）或远程地址（\"地址\"/\"url\"）")
            client = McpStdioClient(
                cfg.name, cfg.command, cfg.args,
                env=cfg.env, cwd=cfg.cwd, logger=self._logger,
            )
        try:
            info = client.initialize(cfg.startup_timeout)
            tools = client.list_tools(cfg.startup_timeout)
        except McpError:
            client.close()
            raise
        server_info = info.get("serverInfo") or {}
        self._log(f"{cfg.name}: 已连接 {server_info.get('name', '?')} "
                  f"{server_info.get('version', '')}，{len(tools)} 个工具")
        return client, tools

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            try:
                self._logger.info("mcp", msg)
            except Exception:
                pass


def _tool_name(tool) -> str:
    """服务端工具定义里的名字（非 dict 或无名字返回空串）"""
    if not isinstance(tool, dict):
        return ""
    return str(tool.get("name") or "").strip()


def _tool_allowed(cfg, raw_name: str) -> bool:
    """白名单/黑名单过滤（按服务端原始工具名）"""
    if not raw_name:
        return False
    if cfg.enabled_tools and raw_name not in cfg.enabled_tools:
        return False
    return raw_name not in cfg.disabled_tools
