"""最小 WebSocket（RFC 6455，服务端下行）与 SSE 下行实现

DSH 前端协议的两路下行流 /api/events.mux 与 /api/events.host：
- 浏览器优先 WebSocket；也接受 GET SSE（text/event-stream）等价路径
- 纯下行：收到客户端 WS 消息 → 1008 关闭（协议违规）
- 帧格式: JSON 文本帧，即 protocol.server_request_frame 的结果
仅使用标准库。
"""

import base64
import hashlib
import json
import socket
import struct
import threading
from typing import Dict, Optional

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("utf-8")).digest()).decode("ascii")


class WsConn:
    """一条已升级的 WebSocket 连接（下行专用）。

    由 server.py 在 upgrade 时创建，随后交给事件泵线程。
    """

    def __init__(self, sock: socket.socket, accept_key: str):
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = False
        self.accept_key = accept_key
        sock.settimeout(None)  # 阻塞读

    def send_text(self, text: str) -> bool:
        """发送文本帧。连接已关闭返回 False。"""
        payload = text.encode("utf-8")
        with self._lock:
            if self._closed:
                return False
            try:
                header = bytes([0x81])  # FIN + text
                n = len(payload)
                if n < 126:
                    header += bytes([n])
                elif n < 65536:
                    header += bytes([126]) + struct.pack(">H", n)
                else:
                    header += bytes([127]) + struct.pack(">Q", n)
                self._sock.sendall(header + payload)
                return True
            except OSError:
                self._closed = True
                return False

    def send_json(self, frame: Dict) -> bool:
        return self.send_text(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    def close(self, code: int = 1000, reason: str = ""):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                payload = struct.pack(">H", code) + reason.encode("utf-8")[:123]
                self._sock.sendall(bytes([0x88, 0x80 | min(len(payload), 125)]) + payload)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def pump_read(self, on_client_message=None, on_close=None):
        """读循环（在专用线程中运行）：处理 ping/close，客户端消息视为违规。

        on_client_message: 收到客户端消息时回调（我们以 1008 关闭）。
        on_close: 连接断开后回调（可带一个参数接收退出原因）。
        """
        reason = "eof"
        try:
            while not self._closed:
                hdr = self._read_exact(2)
                if hdr is None:
                    break
                b0, b1 = hdr[0], hdr[1]
                opcode = b0 & 0x0F
                masked = bool(b1 & 0x80)
                length = b1 & 0x7F
                if length == 126:
                    ext = self._read_exact(2)
                    if ext is None:
                        break
                    length = struct.unpack(">H", ext)[0]
                elif length == 127:
                    ext = self._read_exact(8)
                    if ext is None:
                        break
                    length = struct.unpack(">Q", ext)[0]
                if masked:
                    mask = self._read_exact(4)
                    if mask is None:
                        break
                else:
                    mask = None
                payload = self._read_exact(length)
                if payload is None:
                    break
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == _OP_CLOSE:
                    reason = "client-close"
                    break
                if opcode == _OP_PING:
                    with self._lock:
                        if not self._closed:
                            try:
                                self._sock.sendall(bytes([0x8A, len(payload)]) + payload)
                            except OSError:
                                reason = "ping-send-failed"
                                break
                    continue
                if opcode == _OP_PONG or opcode == _OP_CONT:
                    continue
                # 任何其它客户端消息 = 协议违规（下行专用通道）
                if on_client_message:
                    on_client_message()
                reason = "protocol-violation"
                self.close(1008, "downlink only")
                break
        except OSError as e:
            reason = f"oserror:{e}"
        finally:
            self._closed = True
            if on_close:
                try:
                    on_close(reason)
                except TypeError:
                    on_close()

    def _read_exact(self, n: int) -> Optional[bytes]:
        buf = b""
        try:
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf
        except OSError:
            return None


def handshake_response(headers: Dict[str, str]) -> Optional[str]:
    """根据升级请求头生成 101 响应头文本；不可升级返回 None。"""
    key = headers.get("sec-websocket-key")
    if not key:
        return None
    accept = _accept_key(key.strip())
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )


class SseStream:
    """SSE 下行流（非升级 GET 时的等价路径）。

    DSH 宿主格式：开头发 ": connected\\n\\n"，每帧 "data: {json}\\n\\n"。
    """

    def __init__(self, wfile, lock: threading.Lock):
        self._wfile = wfile
        self._lock = lock
        self._closed = False

    def start(self):
        with self._lock:
            if not self._closed:
                try:
                    self._wfile.write(b": connected\n\n")
                    self._wfile.flush()
                except OSError:
                    self._closed = True

    def send_json(self, frame: Dict) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                self._wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self._wfile.flush()
                return True
            except OSError:
                self._closed = True
                return False

    def close(self):
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed
