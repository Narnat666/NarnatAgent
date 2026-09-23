"""MCP 连接管理器 —— 服务器配置到持久 stdio 通道的生命周期管理。

契约来源：specs/mcp 全部 Requirement（connect/disconnect 流程与文案、「MCP 工具热
注册与命名规则」、「MCP 通道保活与自动重连」、「进程生命周期与回收」、「失败不阻塞
原则与错误文案」、「兼容性怪癖保持」）。行为搬运自旧实现 `narnat_agent/mcp/__init__.py`，
结构改进（design D8）：
- 工具热注册不再经函数内延迟导入直取 `tools.registry`，改由构造注入的
  `contracts.tool.ToolCatalog` 端口完成（mcp 不 import tools）；
- 旧 `connect()` 的 6 项职责拆为「输入归一/解析 → 幂等与上限检查 → 启动握手列工具
  → 锁内原子登记注册」四个方法；
- `set_tool_sinks` 后置接线取消（工具表热更新并入 ToolCatalog 端口）；
- atexit 兜底注册的检查+置位收纳进同一把锁（消灭并发 connect 的重复注册窗口）。

职责只有一个：把 AI 给的服务器配置变成可持续的通道，并保证断开与回收干净。
"""
from __future__ import annotations

import atexit
import hashlib
import re
import threading
from collections.abc import Callable

from ..config import McpServerConfig, parse_mcp_server
from ..contracts.tool import ConnectedServer, ToolCatalog
from .client import McpError, McpStdioClient
from .tool import McpDynamicTool

__all__ = [
    "HASH_LEN",
    "MAX_NAME_LEN",
    "MAX_SERVERS",
    "TOOL_PREFIX",
    "McpManager",
    "make_unique_name",
    "sanitize_name_part",
]

# 工具名约束：Anthropic/OpenAI 均要求 ^[a-zA-Z0-9_-]{1,64}$
MAX_NAME_LEN = 64
"""工具名长度上限（超长截断并追加哈希后缀）。"""

HASH_LEN = 12
"""截断/重名后缀的哈希长度（截断前缀 64-13=51 字符 + `_` + 12 位摘要）。"""

NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
"""工具名片段非法字符（替换为 `_`）。"""

TOOL_PREFIX = "mcp__"
"""MCP 工具名前缀（`mcp__<服务器名>__<工具名>`）。"""

MAX_SERVERS = 8
"""并发 MCP 服务器上限（防进程/工具表失控增长）。"""


class McpManager:
    """MCP 服务器运行时连接管理（懒连接：AI 按需 connect/disconnect）。

    narnat.json 不配置服务器名单：AI 需要什么就连什么（connect），用完可断开
    （disconnect），进程随会话结束回收；同名服务器连接期间断线时用记录配置自动
    重连（只修通道、不代 AI 重跑调用）。
    """

    def __init__(self, catalog: ToolCatalog, error_line: Callable[[str], str],
                 logger=None, client_factory: Callable[..., McpStdioClient] | None = None) -> None:
        """构造管理器（不连接任何服务器）。

        - `catalog`：工具热注册端口（装配点组合注册表与 LLM 工具表）；
        - `error_line`：框架错误行生成器（带进程级随机标签），供动态工具实现使用；
        - `logger`：鸭子类型日志器（`info(module, msg)`），None 表示不记日志；
        - `client_factory`：客户端工厂（缺省 `McpStdioClient`；测试可注入假客户端）。
        """
        self._catalog = catalog
        self._error_line = error_line
        self._logger = logger
        self._client_factory = client_factory if client_factory is not None else McpStdioClient
        self._clients: dict[str, McpStdioClient] = {}   # 服务器名 → 客户端（供退出清理）
        self._server_tools: dict[str, list[str]] = {}   # 服务器名 → 登记的工具名清单
        self._specs: dict[str, dict] = {}               # 服务器名 → 连接时配置（自动重连用）
        self._lock = threading.Lock()
        self._closed = False
        self._atexit_registered = False

    def cleanup(self) -> None:
        """关闭全部 MCP 连接（服务端子进程退出）。程序退出路径调用；幂等。

        兼容怪癖⑤：不注销已注册的 MCP 工具、不触发工具表热更新回调——工具注册
        残留至进程结束；单个连接关闭失败静默忽略，不影响其它连接与主流程。
        """
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

    def connected(self) -> list[ConnectedServer]:
        """当前已连接的服务器清单 `[{"name", "tool_count"}, …]`（按连接插入序）。

        供断开动作与提示文案使用（AI 看不到的服务器清单由 AI 自己掌握）。
        """
        with self._lock:
            return [{"name": name, "tool_count": len(self._server_tools.get(name, ()))}
                    for name in self._clients]

    def connect(self, name: str, config: dict) -> tuple[int, list[str]]:
        """运行时连接一个服务器并热注册其工具（幂等：同名已连接且存活直接返回）。

        流程：输入归一与配置解析 → 幂等/并发上限检查 → 启动进程+握手+列工具（可能
        耗时，不持锁）→ 锁内原子登记并注册（与 disconnect 的注销互斥）。

        Returns: `(注册工具数, [LLM 工具名, …])`；失败抛 `McpError` 由工具层转错误行。
        """
        name, cfg = self._prepare(name, config)
        reused = self._reuse_if_connected(name)
        if reused is not None:
            return reused
        client, tools = self._spawn(cfg)
        return self._commit(name, cfg, config, client, tools)

    def disconnect(self, name: str) -> int:
        """断开服务器并注销其工具，返回注销的工具数。

        `name` 为 `all`/`*`/`全部`（大小写不敏感）时逐个断开全部已连接服务器并累计
        注销数；兼容怪癖②：批内不捕获单点错误（某服务器在批处理中被并发断开引发的
        错误会中断整批，已断开的生效、后续跳过）。断开不存在的服务器抛
        `McpError("服务器 {name} 未连接")`。
        """
        name = str(name or "").strip()
        if name.lower() in ("all", "*", "全部"):
            with self._lock:
                servers = list(self._clients.keys())
            removed = 0
            for server in servers:
                removed += self.disconnect(server)
            return removed

        with self._lock:
            client = self._clients.pop(name, None)
            tool_names = self._server_tools.pop(name, [])
            self._specs.pop(name, None)
            if client is None:
                raise McpError(f"服务器 {name} 未连接")
            # 注销与「连接登记 + 注册」在同一把锁内互斥，杜绝无人跟踪的残留工具
            self._catalog.unregister_dynamic(tool_names)
        try:
            client.close()
        except Exception:
            pass
        self._log(f"{name}: 已断开，注销 {len(tool_names)} 个工具")
        return len(tool_names)

    def call_tool(self, server_name: str, tool_name: str, arguments: dict,
                  timeout: float) -> tuple[str, bool]:
        """经通道转发一次工具调用，返回 `(格式化文本, isError 标志)`。

        保活语义（specs/mcp「MCP 通道保活与自动重连」）：
        - 连接进程已不在存活 → 先用记录配置自动重连，再抛「请重试本次调用」
          （不代 AI 重跑调用——仿真/写操作绝不能悄悄执行两次）；
        - 调用过程中连接中断且进程不再存活 → 自动重连后同样要求重试；
        - 进程仍存活（服务端自身错误/调用超时）→ 原样上报错误，不触发重连；
        - 未连接或程序正在退出 → 对应错误。
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

    # ═══════════════════════════════════════════════════════════
    # 连接流程（拆分自旧实现单函数 6 项职责）
    # ═══════════════════════════════════════════════════════════

    def _prepare(self, name: str, config: dict) -> tuple[str, McpServerConfig]:
        """输入归一与配置解析；空名/非法配置抛 `McpError`。

        兼容怪癖①：强制按「启用」解析（配置中的启用开关被忽略，传 false 也连接）。
        """
        name = str(name or "").strip()
        if not name:
            raise McpError("缺少服务器名（name）")
        cfg = parse_mcp_server(name, dict(config or {}, 启用=True))
        if cfg is None:
            raise McpError("配置非法")
        return name, cfg

    def _reuse_if_connected(self, name: str) -> tuple[int, list[str]] | None:
        """幂等复用与死连接清理；无既有连接时返回 None（走重建）。

        - 同名已连接且存活 → 直接返回既有工具数与清单（不新建进程、不重复注册）；
        - 同名存在但进程已死 → 先断开旧连接（注销旧工具、关闭死进程）再重建——
          否则旧工具名成孤儿（无管理器跟踪，disconnect 注销不掉）；
        - 程序正在退出或已达并发上限 → 抛 `McpError`。
        """
        with self._lock:
            if self._closed:
                raise McpError("程序正在退出，无法连接")
            client = self._clients.get(name)
            if client is None and len(self._clients) >= MAX_SERVERS:
                raise McpError(
                    f"已达并发上限({MAX_SERVERS}个)，当前已连接: "
                    f"{'、'.join(self._clients)}。请先 disconnect 不需要的服务器"
                )
            if client is not None and client.alive:
                tools = list(self._server_tools.get(name, ()))
                return len(tools), tools
        if client is not None:
            self.disconnect(name)
        return None

    def _spawn(self, cfg: McpServerConfig):
        """启动进程 → 握手 → 列工具；失败关闭已启动的子进程（不残留）并重抛。"""
        if not cfg.command.strip():
            raise McpError("未配置启动命令（\"命令\"/\"command\"）")
        client = self._client_factory(
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

    def _commit(self, name: str, cfg: McpServerConfig, config: dict, client,
                tools: list) -> tuple[int, list[str]]:
        """锁内原子完成「登记 + 工具注册 + 定义热更新」（赢家路径）。

        并发同名 connect 只有一个能插入登记，其余走落败路径丢弃本次新建的进程并
        返回既有清单（兼容怪癖④，不报错）；程序在握手期间退出则关闭本次通道。
        首次成功连接后注册解释器退出钩子兜底回收子进程（幂等：锁内检查+置位）。
        """
        entries: list[McpDynamicTool] = []
        existing: list[str] | None = None
        arm_exit_hook = False
        with self._lock:
            if self._closed:
                client.close()
                raise McpError("程序正在退出，无法连接")
            if self._clients.get(name) is None:
                used_names = set(self._catalog.get_tool_names())
                entries = self._build_tools(cfg, tools, used_names)
                self._clients[name] = client
                self._server_tools[name] = [entry.name for entry in entries]
                self._specs[name] = dict(config or {})
                self._catalog.register_dynamic(entries)
                arm_exit_hook = not self._atexit_registered
                self._atexit_registered = True
            else:
                existing = list(self._server_tools.get(name, ()))
        if existing is not None:
            # 并发同名 connect：已有连接胜出，丢弃本次新建的进程与工具
            client.close()
            return len(existing), existing
        if arm_exit_hook:
            # 兜底：走不到正常清理路径的异常退出（解释器关闭）也能回收子进程
            atexit.register(self.cleanup)
        names = [entry.name for entry in entries]
        self._log(f"{name}: 已连接，注册 {len(entries)} 个工具")
        return len(entries), names

    def _build_tools(self, cfg: McpServerConfig, tools: list,
                     used_names: set) -> list[McpDynamicTool]:
        """`tools/list` 条目 → 动态工具对象列表（逐个过滤被剔除的条目）。"""
        built: list[McpDynamicTool] = []
        for tool in tools:
            entry = self._build_tool(cfg, tool, used_names)
            if entry is not None:
                built.append(entry)
        return built

    def _build_tool(self, cfg: McpServerConfig, tool, used_names: set):
        """单个工具条目 → 动态工具对象；被过滤（非法/白黑名单）时返回 None。

        白名单非空时仅注册名单内工具；黑名单内工具不注册（均按服务端原始工具名
        匹配，过滤发生在注册前）；描述缺省兜底、输入模式缺失时以空对象模式代替；
        实现闭包绑定**服务端原始工具名**（调用转发用，非清洗后 LLM 名）。
        """
        if not isinstance(tool, dict):
            return None
        raw_name = str(tool.get("name") or "").strip()
        if not raw_name:
            return None
        if cfg.enabled_tools and raw_name not in cfg.enabled_tools:
            return None
        if raw_name in cfg.disabled_tools:
            return None

        llm_name = make_unique_name(cfg.name, raw_name, used_names)
        description = str(tool.get("description") or "").strip() or \
            f"MCP工具 {raw_name}（来自 {cfg.name}）"
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}
        definition = {
            "type": "function",
            "function": {"name": llm_name, "description": description, "parameters": schema},
        }
        return McpDynamicTool(
            manager=self, definition=definition, server_name=cfg.name,
            raw_name=raw_name, timeout=cfg.tool_timeout, error_line=self._error_line,
        )

    # ═══════════════════════════════════════════════════════════
    # 通道保活（自动重连）与内部实现
    # ═══════════════════════════════════════════════════════════

    def _reconnect(self, server_name: str) -> None:
        """用连接时记录的配置重建通道（只换服务端进程，工具注册不变）。

        - 无记录配置/配置不完整 → 抛「无法自动重连」错误；
        - 重连推进期间该服务器已被断开（或程序正在退出）→ 丢弃新建通道并抛
          「服务器 {name} 已断开，无需重连」。
        """
        with self._lock:
            spec = dict(self._specs.get(server_name) or {})
        if not spec:
            raise McpError(f"服务器 {server_name} 无配置，无法自动重连")
        cfg = parse_mcp_server(server_name, dict(spec, 启用=True))
        if cfg is None or not cfg.command.strip():
            raise McpError(f"服务器 {server_name} 配置不完整，无法自动重连")
        client, _tools = self._spawn(cfg)       # 新进程 + 握手 + 列工具
        with self._lock:
            old = self._clients.get(server_name)
            orphan = client if old is None else None
            if old is not None:
                self._clients[server_name] = client
        if orphan is not None:
            # 重连期间已被断开：丢弃新建通道，不复活已断开的服务器
            try:
                orphan.close()
            except Exception:
                pass
            raise McpError(f"服务器 {server_name} 已断开，无需重连")
        try:
            old.close()
        except Exception:
            pass
        self._log(f"{server_name}: 连接中断，已自动重连")

    def _log(self, msg: str) -> None:
        """写 mcp 模块日志（logger 未注入或写入失败时静默）。"""
        if self._logger is not None:
            try:
                self._logger.info("mcp", msg)
            except Exception:
                pass


def sanitize_name_part(name: str) -> str:
    """清洗工具名片段为 `[A-Za-z0-9_-]`；清不出有效字符时以哈希兜底（如纯中文名）。

    规则（specs/mcp「MCP 工具热注册与命名规则」）：非法字符全部替换为 `_` 并去除
    首尾 `_`；清洗后为空时以 `srv_` 加该名 SHA1 摘要前 8 位代替（稳定生成合法名）。
    """
    cleaned = NAME_SAFE_RE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "srv_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return cleaned


def make_unique_name(server_name: str, tool_name: str, used_names: set) -> str:
    """生成模型可见工具名 `mcp__<服务器>__<工具>`（唯一且 ≤64 字符，并登记占用）。

    - 完整名长度不超过 64 且未被占用时直接使用；
    - 超长或重名时截断为前 51 字符并追加 `_` 加「服务器名与工具名」标识的 SHA1
      摘要前 12 位（总长 ≤64），仍冲突则更换哈希后缀直到唯一。
    """
    base = (f"{TOOL_PREFIX}{sanitize_name_part(server_name)}"
            f"__{sanitize_name_part(tool_name)}")
    if len(base) <= MAX_NAME_LEN and base not in used_names:
        used_names.add(base)
        return base
    identity = f"{server_name}\0{tool_name}"
    attempt = 0
    while True:
        raw = identity if attempt == 0 else f"{identity}\0{attempt}"
        suffix = "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:HASH_LEN]
        name = base[:MAX_NAME_LEN - len(suffix)] + suffix
        if name not in used_names:
            used_names.add(name)
            return name
        attempt += 1
