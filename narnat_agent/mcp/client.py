"""MCP stdio 客户端 —— 单个 MCP 服务器连接（JSON-RPC 2.0 over 标准输入输出）

对标 codex 的 rmcp-client stdio 传输：
- 子进程 stdin/stdout 承载按行分帧的 JSON-RPC 2.0 消息（MCP stdio 传输规范）
- stderr 单独泵线程收集进日志，不污染协议流
- 子进程放独立进程组（POSIX setsid / Windows 新进程组）：用户的 ESC/Ctrl+C
  中断不会误杀服务端；程序退出时显式关闭
- 握手顺序：initialize 请求 → 收响应 → notifications/initialized 通知

线程模型：
- 调用方线程：request() 写入请求，等待自己 id 对应的响应
- 读取线程（daemon）：持续读 stdout 行，分发响应 / 通知 / 服务端请求
"""

import json
import os
import signal
import subprocess
import sys
import threading
from io import TextIOWrapper
from queue import Empty, Queue

from ..tools.exec_signal import error_line

# ── 客户端标识（initialize 握手用，服务端一般不校验）──
PROTOCOL_VERSION = "2025-06-18"   # MCP 规范稳定版
CLIENT_NAME = "narnat-agent"
CLIENT_TITLE = "Narnat Agent"
CLIENT_VERSION = "1.0"

# tools/list 分页循环的安全上限（防异常服务端无限返回 nextCursor）
_MAX_LIST_PAGES = 50


class McpError(Exception):
    """MCP 传输/协议错误（服务端错误响应、超时、进程退出等）"""


class McpStdioClient:
    """单个 MCP stdio 服务器连接。一个实例对应一个服务端子进程。"""

    def __init__(self, name, command, args=(), env=None, cwd=None, logger=None):
        self.name = name
        self._logger = logger
        self._proc = None
        self._dead = False
        self._next_id = 1
        self._pending = {}            # 请求 id → 响应队列
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()   # 主线程请求 + 读取线程回服务端请求，共用 stdin

        cmd_env = dict(os.environ)
        if env:
            cmd_env.update({str(k): str(v) for k, v in env.items()})

        popen_kwargs = {}
        if sys.platform == "win32":
            # 新进程组：不接收本进程控制台的 Ctrl+C 事件
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # 新会话（等同 codex 的 process_group(0)）：Ctrl+C 不波及服务端
            popen_kwargs["start_new_session"] = True

        try:
            self._proc = subprocess.Popen(
                [command, *[str(a) for a in args]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
                env=cmd_env,
                **popen_kwargs,
            )
        except OSError as e:
            raise McpError(f"启动失败: {e}")

        # 二进制管道 + 显式 UTF-8 文本包装：写入侧 newline="\n" 保证协议帧
        # 严格以 \n 分隔（不带 \r\n），读取侧通用换行、容错非 UTF-8 输出
        self._stdin = TextIOWrapper(self._proc.stdin, encoding="utf-8",
                                    errors="replace", newline="\n", write_through=True)
        self._stdout = TextIOWrapper(self._proc.stdout, encoding="utf-8",
                                     errors="replace", newline="")
        self._stderr = TextIOWrapper(self._proc.stderr, encoding="utf-8",
                                     errors="replace", newline="")

        threading.Thread(target=self._reader_loop, daemon=True,
                         name=f"mcp-{name}-reader").start()
        threading.Thread(target=self._stderr_loop, daemon=True,
                         name=f"mcp-{name}-stderr").start()

    # ═══════════════════════════════════════════════════════════
    # MCP 协议操作
    # ═══════════════════════════════════════════════════════════

    def initialize(self, timeout: float) -> dict:
        """initialize 握手 + notifications/initialized。返回服务端 initialize 结果"""
        result = self.request("initialize", {
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
            result = self.request("tools/list", params, timeout=timeout)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool_name: str, arguments: dict, timeout: float) -> str:
        """调用工具，返回格式化文本结果（content 文本块拼接）"""
        result = self.request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=timeout)
        text = _format_content(result)
        if result.get("isError"):
            # 框架判定为失败（isError 标志），带不可伪造标签，防服务端输出来误导 UI 判定
            return error_line(f"{text or '工具返回错误'}")
        return text if text else "(工具无输出)"

    def notify(self, method: str, params: dict = None) -> None:
        """发送通知（无 id，不等待响应）"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: dict, timeout: float) -> dict:
        """发送请求并等待响应。超时/服务端错误/进程退出抛 McpError"""
        q = Queue()
        with self._pending_lock:
            if self._dead:
                raise McpError("进程已退出")
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = q

        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                        "params": params or {}})
            try:
                resp = q.get(timeout=timeout)
            except Empty:
                raise McpError(f"{method} 超时({timeout:g}s)")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

        if resp is None:   # EOF 哨兵
            raise McpError("进程已退出")
        if "error" in resp:
            err = resp["error"]
            detail = err.get("message") if isinstance(err, dict) else err
            raise McpError(f"{method} 失败: {detail}")
        return resp.get("result") or {}

    @property
    def alive(self) -> bool:
        """服务端进程是否仍在运行"""
        return self._proc is not None and self._proc.poll() is None and not self._dead

    def close(self, grace: float = 3.0) -> None:
        """关闭连接：先关 stdin 让服务端自行退出（MCP 规范推荐），超时再杀"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            self._stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        self._terminate(proc)

    # ═══════════════════════════════════════════════════════════
    # 内部实现
    # ═══════════════════════════════════════════════════════════

    def _send(self, msg: dict) -> None:
        """写入一行 JSON-RPC 消息（线程安全）"""
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            if self._dead:
                raise McpError("进程已退出")
            try:
                self._stdin.write(line)
                self._stdin.flush()
            except (OSError, ValueError) as e:
                self._dead = True
                raise McpError(f"写入失败: {e}")

    def _terminate(self, proc) -> None:
        """强杀服务端（Windows 杀主进程；POSIX 杀进程组，连同其子进程）"""
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _reader_loop(self) -> None:
        """读取线程：分发响应到等待者，忽略通知，回绝服务端请求"""
        stdout = self._stdout
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._log(f"忽略非JSON输出: {line[:200]}")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    self._dispatch_response(msg)
                elif "method" in msg:
                    if "id" in msg:
                        # 服务端主动请求（sampling/roots 等）：本客户端不实现，按规范回错
                        self._reply_error(msg["id"], -32601, "Method not found")
                        self._log(f"服务端请求 {msg.get('method')} 未实现，已回绝")
                    else:
                        self._log(f"通知 {msg.get('method')}")
        except OSError as e:
            self._log(f"读取中断: {e}")
        finally:
            self._dead = True
            with self._pending_lock:
                queues = list(self._pending.values())
            for q in queues:
                q.put(None)   # EOF 哨兵：唤醒全部等待者

    def _stderr_loop(self) -> None:
        """stderr 泵线程：服务端诊断信息进日志"""
        try:
            for line in self._stderr:
                line = line.strip()
                if line:
                    self._log(f"stderr: {line[:500]}")
        except (OSError, ValueError):
            pass

    def _dispatch_response(self, msg: dict) -> None:
        with self._pending_lock:
            q = self._pending.get(msg.get("id"))
        if q is not None:
            q.put(msg)

    def _reply_error(self, req_id, code: int, message: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": code, "message": message}})
        except McpError:
            pass

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            try:
                self._logger.info(f"mcp.{self.name}", msg)
            except Exception:
                pass


def _format_content(result: dict) -> str:
    """MCP 工具结果 → 文本：content 内容块拼接，无 content 时退回 structuredContent"""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(str(item.get("text", "")))
        elif item_type == "image":
            parts.append(f"[图片: {item.get('mimeType') or '未知类型'}]")
        elif item_type == "resource":
            res = item.get("resource") or {}
            text = res.get("text")
            parts.append(str(text) if text else f"[资源: {res.get('uri', '')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)
