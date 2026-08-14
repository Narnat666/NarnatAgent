"""DSH 前端宿主服务器（标准库实现）

路由：
- 静态: /, /assets/**, /favicon.svg, /manifest.webmanifest → dsh_static/
  （index.html 注入 window.__DSH_BOOT__）
- /plugins/<id>/client.js(.map) → 插件 bundle（按 plugins_index.json 映射）
- POST /api/<method> → client-request 信封 → handlers.dispatch
- POST /api/respond → client-response 收据（无挂起审批 → not-pending）
- GET  /api/events.mux | /api/events.host → WS 升级或 SSE 下行
- GET  /api/session.export → ZIP 下载
- 信任栅栏：Host 必须回环（DNS-rebinding 防线）
"""

import io
import json
import mimetypes
import os
import posixpath
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import handlers, protocol, translate
from .events import EventHub
from .ws import SseStream, WsConn, handshake_response

_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def _is_loopback_host(host: str) -> bool:
    h = (host or "").split(":", 1)[0].strip("[]").lower()
    return h in ("localhost", "::1", "127.0.0.1") or h.startswith("127.")


class _BridgeHTTPServer(ThreadingHTTPServer):
    # 启动时会并发拉起 ~40 条插件包连接 + 两条事件流，默认 backlog=5 会让后到的
    # WebSocket SYN 被丢（浏览器表现为 "closed before the connection is
    # established"）。与 node http(511) 对齐。
    request_queue_size = 128
    daemon_threads = True
    # 注意：不设 allow_reuse_address —— Windows 上它允许同一端口被二次绑定，
    # 会掩盖"端口已被占用"的问题（两个实例并存、请求随机路由）。

    def handle_error(self, request, client_address):
        """吞掉浏览器断开导致的连接错误噪音（WinError 10053/10054 等）。"""
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, OSError)):
            return
        try:
            super().handle_error(request, client_address)
        except Exception:
            pass


class DshServer:
    def __init__(self, bridge, static_dir: str, port: int = 8765, host: str = "127.0.0.1"):
        self._bridge = bridge
        self._static_dir = static_dir
        self._port = port
        self._host = host
        self._load_assets()
        handler = self._make_handler()
        self._httpd = _BridgeHTTPServer((host, port), handler)
        self._httpd.bridge = bridge
        self._httpd.hub = bridge.hub

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "NarnatDshBridge/1.0"

            # ── 基础 ──
            def _trusted(self) -> bool:
                host = self.headers.get("Host") or ""
                if not _is_loopback_host(host):
                    return False
                site = (self.headers.get("sec-fetch-site") or "").lower()
                if site == "cross-site":
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    try:
                        ohost = urlparse(origin).netloc
                    except Exception:
                        ohost = None
                    if ohost and ohost != host:
                        return False
                return True

            def _send_bytes(self, body: bytes, ctype: str, code: int = 200,
                            extra_headers=None, cache="no-cache"):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", cache)
                    for k, v in (extra_headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _send_json(self, obj, code: int = 200):
                self._send_bytes(
                    json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", code)

            def _path(self) -> str:
                return urlparse(self.path).path

            def log_message(self, *args):
                pass  # 静默

            # ── 静态 ──
            def _serve_static(self):
                path = self._path()
                if path == "/":
                    return self._serve_index()
                if path.startswith("/plugins/"):
                    return self._serve_plugin(path)
                rel = unquote(path.lstrip("/"))
                rel = posixpath.normpath(rel)
                if rel.startswith("..") or rel.startswith("/") or "\\" in rel:
                    self._send_json({"error": "forbidden"}, 403)
                    return
                full = os.path.join(server._static_dir, rel)
                if not os.path.isfile(full):
                    # SPA 回退：未知路径给 index.html（带注入）
                    return self._serve_index()
                ext = os.path.splitext(full)[1].lower()
                ctype = _MIME_OVERRIDES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
                try:
                    with open(full, "rb") as f:
                        body = f.read()
                except OSError:
                    self._send_json({"error": "not found"}, 404)
                    return
                self._send_bytes(body, ctype, cache="no-cache")

            def _serve_index(self):
                try:
                    with open(server._index_path, "rb") as f:
                        body = f.read()
                except OSError:
                    self._send_json({"error": "index.html 缺失（请先运行 collect_dsh）"}, 500)
                    return
                if server._index_injected is None:
                    server._index_injected = self._inject_boot(body.decode("utf-8")).encode("utf-8")
                self._send_bytes(server._index_injected, "text/html; charset=utf-8", cache="no-cache")

            def _inject_boot(self, html: str) -> str:
                graph = server._plugins_json
                js = json.dumps(graph, ensure_ascii=False).replace("<", "\\u003c")
                script = f"<script>window.__DSH_BOOT__ = {js}</script>"
                head = html.find("<head>")
                if head != -1:
                    return html[:head + 6] + script + html[head + 6:]
                return script + html

            def _serve_plugin(self, path: str):
                rel = unquote(path[len("/plugins/"):])
                if not rel.endswith("/client.js") and not rel.endswith("/client.js.map"):
                    self._send_json({"error": "not found"}, 404)
                    return
                pid = rel[: -len("/client.js.map")] if rel.endswith("/client.js.map") else rel[: -len("/client.js")]
                file_rel = server._plugins_index.get(pid)
                if file_rel is None:
                    self._send_json({"error": "not found"}, 404)
                    return
                full = os.path.join(server._static_dir, file_rel)
                if rel.endswith(".map"):
                    full = full + ".map"
                try:
                    with open(full, "rb") as f:
                        body = f.read()
                except OSError:
                    self._send_json({"error": "not found"}, 404)
                    return
                ctype = "application/json" if full.endswith(".map") else "text/javascript"
                self._send_bytes(body, ctype, cache="no-cache")

            # ── GET ──
            def do_GET(self):
                if not self._trusted():
                    self._send_json({"error": "forbidden"}, 403)
                    return
                path = self._path()
                if path == "/plugins/events":
                    # client-hmr 的 inspect/HMR 通道：提供空闲 SSE 即可（无 HMR 能力）
                    return self._handle_idle_sse()
                if path == "/api/events.mux":
                    return self._handle_downlink(mux=True)
                if path == "/api/events.host":
                    return self._handle_downlink(mux=False)
                if path == "/api/session.export":
                    return self._handle_export()
                if path.startswith("/api/"):
                    self._send_json({"error": "not found"}, 404)
                    return
                self._serve_static()

            def _handle_idle_sse(self):
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                # 保持打开；客户端断开时写入失败退出
                try:
                    while True:
                        time.sleep(15)
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _handle_downlink(self, mux: bool):
                upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
                if upgrade:
                    return self._handle_ws_upgrade(mux)
                # SSE
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                lock = threading.Lock()
                stream = SseStream(self.wfile, lock)
                stream.start()
                hub: EventHub = self.server.hub
                if mux:
                    hub.register_mux(stream)
                else:
                    hub.register_host(stream)
                try:
                    # 保持连接（SSE 由 send_json 直接写入 wfile）
                    while not stream.closed:
                        time.sleep(5)
                finally:
                    hub.unregister(stream)
                    stream.close()

            def _handle_ws_upgrade(self, mux: bool):
                headers = {k.lower(): v for k, v in self.headers.items()}
                resp = handshake_response(headers)
                if resp is None:
                    self._send_json({"error": "bad upgrade"}, 400)
                    return
                # 在 handler 线程内完成整个 WS 生命周期（与 node ws 相同模型）：
                # 不提前返回，避免 socketserver finish() 关闭 makefile 波及连接。
                try:
                    self.wfile.write(resp.encode("ascii"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                self.close_connection = True  # 泵结束后由 socketserver 关闭连接
                conn = WsConn(self.connection, "")
                hub: EventHub = self.server.hub
                if mux:
                    hub.register_mux(conn)
                else:
                    hub.register_host(conn)
                # 阻塞读循环：客户端消息违规 → 1008；对端关闭 → 返回
                conn.pump_read(on_client_message=lambda: None,
                               on_close=lambda reason="": hub.unregister(conn))

            def _handle_export(self):
                qs = parse_qs(urlparse(self.path).query)
                sid = (qs.get("sessionId") or [""])[0]
                include_desc = (qs.get("includeDescendants") or ["false"])[0].lower() == "true"
                buf = io.BytesIO()
                bridge = self.server.bridge
                try:
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        self._zip_session(zf, bridge, sid, include_desc)
                except handlers.HandledError as e:
                    self._send_json({"error": e.message}, 404)
                    return
                body = buf.getvalue()
                name = bridge.title_of(sid) or "session"
                self._send_bytes(body, "application/zip", 200, extra_headers={
                    "Content-Disposition": f'attachment; filename="{name}.zip"',
                }, cache="no-store")

            def _zip_session(self, zf, bridge, sid, include_desc):
                messages, err = bridge.load_session_messages(sid)
                if err:
                    raise handlers.HandledError("session-not-found", err, {})
                name = bridge.title_of(sid) or "session"
                safe = name.replace("/", "_").replace("\\", "_")[:120]
                payload = json.dumps(
                    {"name": name, "messages": messages},
                    ensure_ascii=False, indent=2).encode("utf-8")
                zf.writestr(f"{safe}.json", payload)
                if include_desc:
                    parent = bridge.find_session(sid)
                    if parent:
                        pname = parent[0]
                        try:
                            from ...config import session_store
                            tree = session_store.list_sessions_tree(bridge._mgr.narnat_dir)
                            for root in tree:
                                if root.get("name") == pname:
                                    for child in root.get("children") or []:
                                        cname = child.get("name") or ""
                                        csid = translate.session_id(cname, pname)
                                        self._zip_session(zf, bridge, csid, False)
                        except Exception:
                            pass

            # ── POST ──
            def do_POST(self):
                if not self._trusted():
                    self._send_json({"error": "forbidden"}, 403)
                    return
                path = self._path()
                length = int(self.headers.get("Content-Length") or "0")
                if length > 160 * 1024 * 1024:
                    self._send_json({"error": "too large"}, 413)
                    return
                raw = self.rfile.read(length) if length else b""
                if path == "/api/respond":
                    return self._handle_respond(raw)
                if not path.startswith("/api/"):
                    self._send_json({"error": "not found"}, 404)
                    return
                method = path[len("/api/"):]
                msg = protocol.parse_client_request(raw)
                if msg is None:
                    self._send_json({"error": "invalid client-request"}, 400)
                    return
                rpc_id = msg["rpcId"]
                if msg["method"] != method:
                    result = protocol.error("bad-request",
                                            f"method {msg['method']} does not match endpoint {method}",
                                            {"issues": []})
                else:
                    result = handlers.dispatch(self.server.bridge, method, msg.get("payload"))
                self._send_json(protocol.server_response(rpc_id, result))

            def _handle_respond(self, raw: bytes):
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json({"accepted": False, "reason": "bad-response"}, 400)
                    return
                if not isinstance(body, dict) or body.get("type") != "client-response":
                    self._send_json({"accepted": False, "reason": "bad-response"}, 400)
                    return
                # 适配器从不发出可应答帧（无 approval/question）→ 恒 not-pending
                self._send_json({"accepted": False, "reason": "not-pending"})

        return Handler

    def _load_assets(self):
        self._index_path = os.path.join(self._static_dir, "index.html")
        self._index_injected = None
        try:
            with open(os.path.join(self._static_dir, "plugins.json"), "r", encoding="utf-8") as f:
                self._plugins_json = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._plugins_json = {"rev": "missing", "entries": []}
        try:
            with open(os.path.join(self._static_dir, "plugins_index.json"), "r", encoding="utf-8") as f:
                self._plugins_index = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._plugins_index = {}

    def serve_forever(self):
        print(f"[dsh-bridge] DSH 前端界面: http://{self._host}:{self._port}/")
        try:
            self._httpd.serve_forever()
        except Exception as e:
            print(f"[dsh-bridge] 服务器异常: {e}")

    def shutdown(self):
        threading.Thread(target=self._httpd.shutdown, daemon=True).start()
