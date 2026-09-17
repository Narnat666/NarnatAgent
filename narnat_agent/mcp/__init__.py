"""MCP 集成 —— 连接通道（stdio 子进程 + JSON-RPC 2.0）

职责只有一个：把 AI 给的服务器配置变成可持续的通道，并保证断开与回收干净。
- 连接：子进程 → initialize 握手 → tools/list → 注册为 mcp__<服务器名>__<工具名>
- 通道保活：服务端进程中断时，用连接时记录的配置自动重连（要求 AI 重试，
  不重复执行调用），AI 无需重新提供配置
- 回收：连接/断开、会话结束、异常退出（atexit）都保证子进程不残留
"""

import atexit
import hashlib
import re
import threading

from .client import McpError, McpStdioClient
from ..config.loader import parse_mcp_server
from ..tools.exec_signal import error_line

# 工具名约束：Anthropic/OpenAI 均要求 ^[a-zA-Z0-9_-]{1,64}$
_MAX_NAME_LEN = 64
_HASH_LEN = 12
_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
TOOL_PREFIX = "mcp__"
_MAX_SERVERS = 8   # 并发 MCP 服务器上限（与后台任务槽位同源：防进程/工具表失控增长）

__all__ = ["McpManager", "McpError", "TOOL_PREFIX"]


class McpManager:
    """MCP 服务器运行时连接管理（AI 按需连接，与 Terminal 同构）

    narnat.json 不配置服务器名单：AI 需要什么就连什么（connect），
    用完可断开（disconnect），进程随会话结束回收。
    """

    def __init__(self, logger=None):
        self._logger = logger
        self._clients = {}                    # 服务器名 → McpStdioClient（供退出清理）
        self._server_tools = {}               # 服务器名 → [注册的 LLM 工具名]
        self._specs = {}                      # 服务器名 → 连接时记录的配置（自动重连用）
        self._lock = threading.Lock()
        self._closed = False
        self._atexit_registered = False
        self._tool_sink_add = None            # 工具定义热更新回调（assembly 注入）
        self._tool_sink_remove = None

    def cleanup(self) -> None:
        """关闭全部 MCP 连接（服务端子进程退出）。程序退出路径调用"""
        with self._lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
            self._server_tools.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # 运行时接口（MCP 工具调用：AI 想连谁连谁）
    # ═══════════════════════════════════════════════════════════

    def set_tool_sinks(self, on_add, on_remove) -> None:
        """注入工具定义热更新回调（assembly 在 LLM 创建后接线）。

        on_add(definitions) / on_remove(tool_names)：把运行时注册/注销的工具
        同步到 LLM 的工具表，使 AI 下一轮即可见（不接则仅 registry 生效）。
        """
        self._tool_sink_add = on_add
        self._tool_sink_remove = on_remove

    def connected(self) -> list:
        """当前已连接的服务器：[{"name", "tool_count"}]

        供断开动作与提示使用（AI 看不到的服务器清单由 AI 自己掌握）。
        """
        with self._lock:
            return [{"name": name, "tool_count": len(self._server_tools.get(name, ()))}
                    for name in self._clients]

    def connect(self, name: str, spec: dict) -> tuple:
        """运行时连接一个服务器（幂等：同名已连接且存活则直接返回）。

        Args:
            name: 服务器名（工具前缀 mcp__<name>__ 的来源）
            spec: {"command","args","env","cwd","startup_timeout_sec","tool_timeout_sec",...}
        Returns: (注册工具数, [LLM 工具名, ...])
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
                return len(self._server_tools.get(name, ())), list(self._server_tools.get(name, ()))
        if client is not None:
            # 连接已死但工具仍在注册表：先注销旧通道（关死进程、注销旧工具），
            # 再重建——否则旧工具名成孤儿（无管理器跟踪，disconnect 注销不掉）
            self.disconnect(name)

        connected = self._connect(cfg)          # 可能耗时（spawn+握手+列工具），不持锁
        client, tools = connected

        from ..tools.registry import get_tool_names, register_dynamic_tools
        with self._lock:
            if self._closed:
                client.close()
                raise McpError("程序正在退出，无法连接")
            existing_tools = None
            if self._clients.get(name) is None:
                # 检查 + 登记 + 注册在同一锁内原子完成（赢家路径）：
                # 并发同名 connect 只有一个能插入，其余走 existing_tools 分支丢弃；
                # disconnect 的注销也在锁内，无法插进"登记后注册前"，杜绝孤儿工具
                used_names = set(get_tool_names())
                entries = []
                for tool in tools:
                    entry = self._build_entry(cfg, client, tool, used_names)
                    if entry is not None:
                        entries.append(entry)
                self._clients[name] = client
                self._server_tools[name] = [e[0] for e in entries]
                self._specs[name] = dict(spec or {})
                register_dynamic_tools(entries)
                if self._tool_sink_add is not None:
                    self._tool_sink_add([e[1] for e in entries])
            else:
                # 并发同名 connect：已有连接胜出，丢弃本次新建的进程与工具
                existing_tools = list(self._server_tools.get(name, ()))
        if existing_tools is not None:
            client.close()
            return len(existing_tools), existing_tools
        # 兜底：走不到 agent finally 的异常退出路径（输入界面 Ctrl+C 等）
        # 也能在解释器关闭时回收服务端子进程
        if not self._atexit_registered:
            atexit.register(self.cleanup)
            self._atexit_registered = True
        self._log(f"{name}: 已连接，注册 {len(entries)} 个工具")
        return len(entries), [e[0] for e in entries]

    def disconnect(self, name: str) -> int:
        """断开服务器并注销其工具，返回注销的工具数。name 为 "all" 时断开全部"""
        name = str(name or "").strip()
        if name.lower() in ("all", "*", "全部"):
            with self._lock:
                names = list(self._clients.keys())
            removed = 0
            for server in names:
                removed += self.disconnect(server)
            return removed

        with self._lock:
            client = self._clients.pop(name, None)
            tool_names = self._server_tools.pop(name, [])
            self._specs.pop(name, None)
            if client is None:
                raise McpError(f"服务器 {name} 未连接")
            # 注销放进锁内：与 connect 的"检查+登记+注册"原子互斥，
            # 杜绝"注销跑在注册前 → 注册出无人跟踪的孤儿工具"的时序窗口
            from ..tools.registry import unregister_dynamic_tools
            unregister_dynamic_tools(tool_names)
            if self._tool_sink_remove is not None:
                self._tool_sink_remove(tool_names)
        try:
            client.close()
        except Exception:
            pass
        self._log(f"{name}: 已断开，注销 {len(tool_names)} 个工具")
        return len(tool_names)

    # ═══════════════════════════════════════════════════════════
    # 通道（调用转发 + 保活）
    # ═══════════════════════════════════════════════════════════

    def call_tool(self, server_name: str, tool_name: str, arguments: dict,
                  timeout: float) -> str:
        """经通道转发一次工具调用。

        保活语义：发现连接中断时，用连接时记录的配置自动重连，并要求 AI 重试
        （不代它重跑调用 —— 仿真/写操作绝不能悄悄执行两次）。
        """
        with self._lock:
            if self._closed:
                raise McpError("程序正在退出")
            client = self._clients.get(server_name)
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
        """用连接时记录的配置重建通道（工具注册不变，只换进程）"""
        with self._lock:
            spec = dict(self._specs.get(server_name) or {})
        if not spec:
            raise McpError(f"服务器 {server_name} 无配置，无法自动重连")
        cfg = parse_mcp_server(server_name, dict(spec, 启用=True))
        if cfg is None or not cfg.command.strip():
            raise McpError(f"服务器 {server_name} 配置不完整，无法自动重连")
        client, _tools = self._connect(cfg)     # 新进程 + 握手 + 列工具
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
        """连接单个服务器：启动进程 → 握手 → 列工具"""
        if not cfg.command.strip():
            raise McpError("未配置启动命令（\"命令\"/\"command\"）")
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

    def _build_entry(self, cfg, client, tool, used_names):
        """MCP 工具 → registry 条目 (名称, LLM定义, 实现)。被过滤时返回 None"""
        if not isinstance(tool, dict):
            return None
        raw_name = str(tool.get("name") or "").strip()
        if not raw_name:
            return None
        # 白名单/黑名单按服务端原始工具名过滤
        if cfg.enabled_tools and raw_name not in cfg.enabled_tools:
            return None
        if raw_name in cfg.disabled_tools:
            return None

        llm_name = _make_unique_name(cfg.name, raw_name, used_names)
        description = str(tool.get("description") or "").strip() or \
            f"MCP工具 {raw_name}（来自 {cfg.name}）"
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}

        definition = {
            "type": "function",
            "function": {
                "name": llm_name,
                "description": description,
                "parameters": schema,
            },
        }
        impl = _make_tool_impl(self, cfg.name, raw_name, cfg.tool_timeout)
        return llm_name, definition, impl

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            try:
                self._logger.info("mcp", msg)
            except Exception:
                pass


def _sanitize_name_part(name: str) -> str:
    """清洗工具名片段为 [A-Za-z0-9_-]；清不出有效字符时用哈希兜底（如纯中文名）"""
    cleaned = _NAME_SAFE_RE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "srv_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return cleaned


def _make_unique_name(server_name: str, tool_name: str, used_names: set) -> str:
    """生成模型可见工具名 mcp__<服务器>__<工具>。

    字符清洗 → 超长/重名时截断并追加 SHA1 哈希后缀（对标 codex 的
    normalize_tools_for_model：唯一且不超 API 长度上限）。
    """
    base = (f"{TOOL_PREFIX}{_sanitize_name_part(server_name)}"
            f"__{_sanitize_name_part(tool_name)}")
    if len(base) <= _MAX_NAME_LEN and base not in used_names:
        used_names.add(base)
        return base
    identity = f"{server_name}\0{tool_name}"
    attempt = 0
    while True:
        raw = identity if attempt == 0 else f"{identity}\0{attempt}"
        suffix = "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:_HASH_LEN]
        name = base[:_MAX_NAME_LEN - len(suffix)] + suffix
        if name not in used_names:
            used_names.add(name)
            return name
        attempt += 1


def _make_tool_impl(manager: McpManager, server_name: str, tool_name: str, timeout: float):
    """生成 registry 工具实现：经通道转发（含断线自动重连），失败返回框架错误行"""
    def _call(**kwargs):
        try:
            return manager.call_tool(server_name, tool_name, kwargs, timeout)
        except McpError as e:
            return error_line(f"MCP工具 {server_name}/{tool_name} 调用失败: {e}")
    return _call
