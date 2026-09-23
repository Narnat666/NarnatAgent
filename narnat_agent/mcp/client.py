"""MCP stdio 客户端 —— 单个 MCP 服务器连接（JSON-RPC 2.0 over 标准输入输出）。

契约来源：specs/mcp「连接流程与 JSON-RPC 握手协议」「MCP 工具调用协议与结果格式化」
「MCP 断开、工具注销与进程终止」「进程生命周期与回收」「兼容性怪癖保持」。行为搬运
自旧实现 `narnat_agent/mcp/client.py`（帧协议、双策略解码、进程隔离、优雅关闭→强杀
逐条等价），结构上把框架错误行的生成上移（本层只回传 `isError` 标志，错误行由工具
实现层用注入的错误行生成器产出——mcp 积木不依赖 `tools.signal` 的标签机制）。

线程模型：
- 调用方线程：`request()` 写入请求，等待自己 id 对应的响应；
- 读取线程（daemon）：持续读 stdout 行，分发响应 / 记录通知 / 回绝服务端请求；
- stderr 泵线程（daemon）：服务端诊断信息逐行进日志。

进程隔离（specs/mcp「进程生命周期与回收」）：子进程放独立进程组（类 Unix 新会话 /
Windows 新进程组 + 独立无窗口控制台）——用户的 ESC/Ctrl+C 中断不误杀服务端，服务端
原生直写的控制台输出不落入本终端。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from io import TextIOWrapper
from queue import Empty, Queue

__all__ = [
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
]

# ── 客户端标识（initialize 握手用，服务端一般不校验）──
PROTOCOL_VERSION = "2025-06-18"
"""MCP 规范稳定版协议版本（initialize 请求 `protocolVersion`）。"""

CLIENT_NAME = "narnat-agent"
"""客户端标识名（initialize 请求 `clientInfo.name`）。"""

CLIENT_TITLE = "Narnat Agent"
"""客户端显示名（initialize 请求 `clientInfo.title`）。"""

CLIENT_VERSION = "1.0"
"""客户端版本（initialize 请求 `clientInfo.version`）。"""

MAX_LIST_PAGES = 50
"""`tools/list` 分页循环的安全上限（防异常服务端无限返回 nextCursor；达上限静默截断）。"""

CLOSE_GRACE_SECONDS = 3.0
"""关闭连接的优雅宽限：关 stdin 后等待服务端自行退出的秒数，超时强杀。"""

KILL_WAIT_SECONDS = 2.0
"""强杀与进程组 SIGTERM→SIGKILL 之间的等待秒数。"""


class McpError(Exception):
    """MCP 传输/协议错误（服务端错误响应、超时、进程退出等）。"""


def decode_line(raw: bytes) -> str:
    """MCP 通道行解码：UTF-8 严格优先，Windows 平台失败回退 GBK，再失败 replace 兜底。

    与 shell 工具的 `_decode_output` 双策略一致：Windows 服务端（Sysplorer 等）
    常输出 GBK 中文，纯 UTF-8+replace 会把整段变成 U+FFFD 乱码。JSON-RPC 帧语法
    字符均为 ASCII，GBK 对 ASCII 解码结果与 UTF-8 一致，整行回退安全；一行内混合
    两种编码（病态服务端）退化为 replace 兜底不丢行。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if sys.platform == "win32":
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def format_content(result: dict) -> str:
    """MCP 工具结果 → 文本：content 内容块拼接，无内容块时退回 structuredContent。

    - `text` 块取原文；`image` 块转为 `[图片: {MIME类型或未知类型}]`；
    - `resource` 块取其文本（无文本时以 `[资源: {URI}]` 代替）；
    - 其它类型块以 JSON 序列化（非 ASCII 不转义）；非 dict 项取字符串形态；
    - 各块以换行连接；拼接结果为空且无结构化内容时返回空串（空结果兜底由调用方处理）。
    """
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


class McpStdioClient:
    """单个 MCP stdio 服务器连接。一个实例对应一个服务端子进程。"""

    def __init__(self, name, command, args=(), env=None, cwd=None, logger=None):
        """启动服务端子进程并开启读取/stderr 泵线程。

        - 子进程环境 = 父进程环境全量继承 + `env` 覆盖（键值转为字符串）；
        - 工作目录取 `cwd`（为空用当前目录）；启动失败抛 `McpError("启动失败: …")`；
        - `logger` 为鸭子类型（`info(module, msg)`），None 表示不记日志。
        """
        self.name = name
        self._logger = logger
        self._proc = None
        self._dead = False
        self._next_id = 1
        self._pending = {}                    # 请求 id → 响应队列
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()   # 调用方请求 + 读取线程回绝，共用 stdin

        cmd_env = dict(os.environ)
        if env:
            cmd_env.update({str(k): str(v) for k, v in env.items()})

        popen_kwargs = {}
        if sys.platform == "win32":
            # 新进程组：不接收本进程控制台的 Ctrl+C 事件。
            # CREATE_NO_WINDOW：子进程使用独立（无窗口）控制台 —— 否则 MCP 服务端会附着到
            #   narnat 所在终端控制台，其内嵌的 Sysplorer/MWorks 原生栈直写控制台的消息
            #   （Message(0) / Socket / 错误(xxxx) 等）会与 UI 输出杂糅（且多为 GBK 乱码）。
            #   独立控制台后这类直写只落在它自己的隐藏缓冲区；服务端"桌面上下文"的
            #   FreeConsole/AttachConsole(parent) 挂回动作也会因其 GetConsoleWindow()==0
            #   守卫而跳过。JSON-RPC 与日志均走管道/文件，不受影响。
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            # 新会话（等同 setsid）：Ctrl+C 不波及服务端
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

        # 写入侧 UTF-8 TextIOWrapper：newline="\n" 保证协议帧严格以 \n 分隔
        # （不带 \r\n）。读取侧用原始字节流 + 逐行双策略解码（见 decode_line）：
        # 流式 TextIOWrapper 无法在解码失败后回退 GBK，错误字节只能 replace 成乱码。
        self._stdin = TextIOWrapper(self._proc.stdin, encoding="utf-8",
                                    errors="replace", newline="\n", write_through=True)
        self._stdout = self._proc.stdout
        self._stderr = self._proc.stderr

        threading.Thread(target=self._reader_loop, daemon=True,
                         name=f"mcp-{name}-reader").start()
        threading.Thread(target=self._stderr_loop, daemon=True,
                         name=f"mcp-{name}-stderr").start()

    # ═══════════════════════════════════════════════════════════
    # MCP 协议操作
    # ═══════════════════════════════════════════════════════════

    def initialize(self, timeout: float) -> dict:
        """`initialize` 握手 + `notifications/initialized` 通知，返回服务端 initialize 结果。

        请求参数固定为：`protocolVersion`、`capabilities={}`、`clientInfo`
        （name/title/version，见模块常量）。
        """
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
        """列出全部工具（响应含 `nextCursor` 时携带 `cursor` 自动翻页，上限 50 页）。

        首轮请求不带 `cursor` 键；每页工具合并为完整列表；达页数上限静默截断
        （兼容怪癖⑥：无任何提示）。
        """
        tools = []
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout=timeout)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool_name: str, arguments: dict, timeout: float) -> tuple[str, bool]:
        """调用工具，返回 `(格式化文本, isError 标志)`。

        文本为 content 块拼接结果（可能为空串）；`isError` 标志与空文本兜底
        （`(工具无输出)` / 错误行）由调用方（动态工具实现）处理——本层不生成
        框架错误行。
        """
        result = self.request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=timeout)
        return format_content(result), bool(result.get("isError"))

    def notify(self, method: str, params: dict | None = None) -> None:
        """发送通知（无 id，不等待响应）。"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: dict, timeout: float) -> dict:
        """发送请求并等待响应；超时/服务端错误响应/进程退出抛 `McpError`。

        超时文案 `{method} 超时({超时值}s)`；服务端错误响应文案
        `{method} 失败: {错误消息}`。
        """
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
        """服务端进程是否仍在运行（进程存活且读取线程未置死）。"""
        return self._proc is not None and self._proc.poll() is None and not self._dead

    def close(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """关闭连接：先关 stdin 让服务端自行退出（MCP 规范推荐），宽限内未退则强杀。

        对进程已退出的连接幂等（立即返回，无副作用）。
        """
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
        """写入一行 JSON-RPC 消息（非 ASCII 不转义、严格 `\\n` 结尾；线程安全）。"""
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
        """强杀服务端：Windows 杀主进程；类 Unix 杀进程组（SIGTERM → 2 秒 → SIGKILL）。"""
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=KILL_WAIT_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _reader_loop(self) -> None:
        """读取线程：分发响应到等待者，通知仅记日志，服务端请求一律回绝（-32601）。

        退出时置死并向全部等待者广播 EOF 哨兵（唤醒超时等待）。
        """
        stdout = self._stdout
        try:
            while True:
                raw = stdout.readline()
                if not raw:
                    break
                line = decode_line(raw).strip()
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
        """stderr 泵线程：服务端诊断信息逐行进日志（逐行双策略解码）。"""
        try:
            for raw in self._stderr:
                line = decode_line(raw).strip()
                if line:
                    self._log(f"stderr: {line[:500]}")
        except (OSError, ValueError):
            pass

    def _dispatch_response(self, msg: dict) -> None:
        """把响应投递给其 id 对应的等待队列（无匹配者静默丢弃）。"""
        with self._pending_lock:
            q = self._pending.get(msg.get("id"))
        if q is not None:
            q.put(msg)

    def _reply_error(self, req_id, code: int, message: str) -> None:
        """回绝服务端请求（发送错误响应；写入失败静默）。"""
        try:
            self._send({"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": code, "message": message}})
        except McpError:
            pass

    def _log(self, msg: str) -> None:
        """写 `mcp.{name}` 模块日志（logger 未注入或写入失败时静默）。"""
        if self._logger is not None:
            try:
                self._logger.info(f"mcp.{self.name}", msg)
            except Exception:
                pass
