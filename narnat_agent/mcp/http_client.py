"""MCP Streamable HTTP 客户端 —— 单个远程 MCP 服务器连接（JSON-RPC 2.0 over HTTP）

对标 codex 的 rmcp-client Streamable HTTP 传输：
- 单端点 POST 承载 JSON-RPC 2.0 消息（MCP Streamable HTTP 传输规范 2025-03-26+）；
  Accept 同时声明 application/json 与 text/event-stream —— 服务端可整包 JSON 回
  （AnySearch），也可用 SSE 帧流式回（DeepWiki/Context7/GitMCP）
- SSE 流可能保持打开：取到本次请求的响应即停止读取并关闭流，不能读到流结束
- 会话头：服务端返回 Mcp-Session-Id 时记录，后续所有请求（含 notify）都带上
- 无长连接：alive 在 close() 前恒为 True（否则 McpManager.call_tool 会把每次正常
  调用误判为"连接中断，请重试"），断连检测由请求失败承担
- 握手顺序：initialize 请求 → 收响应 → notifications/initialized 通知（不等待）
"""

import json
import threading

import httpx

from .client import (CLIENT_NAME, CLIENT_TITLE, CLIENT_VERSION, PROTOCOL_VERSION,
                     McpError, _format_content)
from ..tools.exec_signal import error_line

# 固定请求头：MCP 规范要求客户端在 POST 的 Accept 里同时声明可接受的两种响应格式
_FIXED_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# tools/list 分页循环的安全上限（与 stdio 客户端同值，防异常服务端无限返回 nextCursor）
_MAX_LIST_PAGES = 50

# notify 的内部超时（规范里服务端对通知只回 202/无 body，必要时等个网络往返足够）
_NOTIFY_TIMEOUT = 10.0


class McpHttpClient:
    """单个 MCP 远程服务器连接（Streamable HTTP）。一个实例对应一个 URL。"""

    def __init__(self, name, url, headers=None, logger=None):
        self.name = name
        self._logger = logger
        self._url = url
        self._closed = False
        self._next_id = 1
        self._id_lock = threading.Lock()
        # 服务端会话 id：多数无状态服务器（AnySearch）不返回，返回的必须透传
        self._session_id = None

        self._headers = dict(_FIXED_HEADERS)
        for key, value in (headers or {}).items():
            self._headers[str(key)] = str(value)
        # 默认超时置空：超时由每次请求的 timeout 参数精确控制（不落入 httpx 默认 5s）
        self._client = httpx.Client(timeout=None)

    # ═══════════════════════════════════════════════════════════
    # MCP 协议操作
    # ═══════════════════════════════════════════════════════════

    def initialize(self, timeout: float) -> dict:
        """initialize 握手 + notifications/initialized。返回服务端 initialize 结果

        协议版本宽松接受：服务端回低于请求的版本（如 GitMCP 回 2025-03-26）不报错。
        """
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": CLIENT_NAME,
                "title": CLIENT_TITLE,
                "version": CLIENT_VERSION,
            },
        }, timeout=timeout)
        self.notify("notifications/initialized")
        return result

    def list_tools(self, timeout: float) -> list:
        """列出全部工具（自动翻页 nextCursor）"""
        tools = []
        cursor = None
        for _ in range(_MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=timeout)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool_name: str, arguments: dict, timeout: float) -> str:
        """调用工具，返回格式化文本结果（content 文本块拼接）"""
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=timeout)
        text = _format_content(result)
        if result.get("isError"):
            # 框架判定为失败（isError 标志），带不可伪造标签，防服务端输出来误导 UI 判定
            return error_line(f"{text or '工具返回错误'}")
        return text if text else "(工具无输出)"

    def notify(self, method: str, params: dict = None) -> None:
        """发送通知（无 id，不等待响应；服务端按规范回 202/无 body）"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._post(msg, method, _NOTIFY_TIMEOUT)

    @property
    def alive(self) -> bool:
        """HTTP 无长连接：close() 前恒为 True（断连检测由请求失败承担）"""
        return not self._closed

    def close(self) -> None:
        """关闭 HTTP 客户端（幂等；无子进程，仅释放连接池）"""
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # 内部实现
    # ═══════════════════════════════════════════════════════════

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        """发送请求并等待响应。超时/服务端错误/非法响应抛 McpError"""
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        resp = self._post({"jsonrpc": "2.0", "id": req_id, "method": method,
                           "params": params or {}}, method, timeout)
        if resp is None:   # 服务端未回响应（空 body / 只发了通知）
            raise McpError(f"{method} 失败: 服务端未返回响应")
        if "error" in resp:
            err = resp["error"]
            detail = err.get("message") if isinstance(err, dict) else err
            raise McpError(f"{method} 失败: {detail}")
        return resp.get("result") or {}

    def _post(self, msg: dict, method: str, timeout: float):
        """POST 一条 JSON-RPC 消息。

        带 id 的请求返回到与 id 匹配的响应消息；无 id 的通知只确认送达（回 None）。
        HTTP 非 2xx / 网络异常 / 超时抛 McpError。
        """
        if self._closed:
            raise McpError("连接已关闭")
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req_id = msg.get("id")
        try:
            with self._client.stream("POST", self._url, json=msg,
                                     headers=headers, timeout=timeout) as resp:
                session_id = resp.headers.get("Mcp-Session-Id")
                if session_id and session_id != self._session_id:
                    self._session_id = session_id
                    self._log(f"服务端会话: {session_id}")
                if not 200 <= resp.status_code < 300:
                    body = resp.read().decode("utf-8", errors="replace")
                    raise McpError(f"{method} HTTP {resp.status_code}: {body[:200]}")
                if req_id is None:
                    return None
                return self._read_message(resp, method, req_id)
        except httpx.TimeoutException:
            raise McpError(f"{method} 超时({timeout:g}s)")
        except httpx.HTTPError as e:
            raise McpError(f"{method} 失败: {e}")

    def _read_message(self, resp, method: str, req_id):
        """从响应读出 id 匹配的 JSON-RPC 消息（服务端通知/请求忽略）。

        text/event-stream：逐行做 SSE 事件解析，取到匹配响应即返回（流可能保持打开，
        不能读到流结束）；其它 content-type：整包 body 当 JSON 解析。
        """
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" not in ctype:
            text = resp.read().decode("utf-8", errors="replace").strip()
            if not text:
                return None
            return self._loads(text, method)

        data_lines = []
        for line in resp.iter_lines():
            if line == "":                     # 空行 = 一个事件结束（SSE 帧边界）
                msg = _sse_message(data_lines)
                data_lines = []
                if msg is not None and msg.get("id") == req_id:
                    return msg
            elif line.startswith(":"):         # SSE 注释行（保活注释等）
                continue
            else:
                field, _, value = line.partition(":")
                if field == "data":
                    data_lines.append(value[1:] if value.startswith(" ") else value)
        msg = _sse_message(data_lines)         # 服务端关闭流前最后一个事件
        return msg if msg is not None and msg.get("id") == req_id else None

    @staticmethod
    def _loads(text: str, method: str):
        """整包 JSON 解析（非法时抛 McpError，不泄漏 json 模块异常）"""
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            raise McpError(f"{method} 失败: 响应非JSON({text[:200]})")
        return msg if isinstance(msg, dict) else None

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            try:
                self._logger.info(f"mcp.{self.name}", msg)
            except Exception:
                pass


def _sse_message(data_lines: list):
    """把一个 SSE 事件的 data 行解析为 JSON 消息（多行 data 按 \n 拼接；非 JSON 帧忽略）"""
    if not data_lines:
        return None
    try:
        msg = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return msg if isinstance(msg, dict) else None
