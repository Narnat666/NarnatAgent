"""DSH 中转模块冒烟测试（不启动完整 Agent，用 stub 桥验证传输层）

用法: python -m narnat_agent.web.dsh.smoke_test
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from types import SimpleNamespace

from .events import EventHub
from .server import DshServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsh_static")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class StubBridge:
    def __init__(self):
        self.hub = EventHub()
        self._config = SimpleNamespace(ai=SimpleNamespace(protocol="anthropic", model="deepseek-v4-flash"))
        self._sid_index = {}
        self._titles = {}
        self._mgr = None
        self._current_sid = "narnat-live"

    def current_sid(self):
        return self._current_sid

    def current_messages(self):
        return []

    def session_list_rows(self):
        return []

    def load_session_messages(self, sid):
        return None, "会话不存在"

    def title_of(self, sid):
        return self._titles.get(sid, "")

    def find_session(self, sid):
        return self._sid_index.get(sid)

    def project_root(self):
        return os.getcwd()

    def data_dir(self):
        return os.path.join(os.getcwd(), ".narnat", "data")

    def attach_demo_session(self, sid, baseline, title):
        self._sid_index[sid] = (title, None)
        self._titles[sid] = title
        self.hub.attach_session(sid, baseline, title)


def _http(method, path, body=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _rpc(method, payload):
    msg = {"type": "client-request", "rpcId": "t-1", "method": method, "payload": payload}
    status, _, body = _http("POST", f"/api/{method}",
                            json.dumps(msg).encode("utf-8"),
                            {"Content-Type": "application/json"})
    return status, json.loads(body.decode("utf-8"))


def _ws_handshake(path):
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("ascii").split("\r\n")
    status = lines[0]
    accept = ""
    for line in lines[1:]:
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
    expect = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
    frames = []
    s.settimeout(3)
    data = rest
    try:
        while len(frames) < 3:
            data += s.recv(65536)
            while len(data) >= 2:
                ln = data[1] & 0x7F
                hlen = 2 + (8 if ln == 127 else 2 if ln == 126 else 0)
                if len(data) < hlen:
                    break
                if ln == 126:
                    plen = struct.unpack(">H", data[2:4])[0]
                elif ln == 127:
                    plen = struct.unpack(">Q", data[2:10])[0]
                else:
                    plen = ln
                if len(data) < hlen + plen:
                    break
                payload = data[hlen:hlen + plen]
                data = data[hlen + plen:]
                frames.append(json.loads(payload.decode("utf-8")))
    except socket.timeout:
        pass
    s.close()
    return status, accept == expect, frames


def main():
    global PORT
    PORT = 8766
    bridge = StubBridge()
    bridge.attach_demo_session("demo-1", 2, "示例会话")
    server = DshServer(bridge, STATIC_DIR, port=PORT)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    failures = []

    def check(name, cond, detail=""):
        print(("  [ok] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # 1. index 注入 __DSH_BOOT__
    status, headers, body = _http("GET", "/")
    html = body.decode("utf-8")
    check("GET / 200 + text/html", status == 200 and "text/html" in headers.get("Content-Type", ""))
    check("index.html 注入 __DSH_BOOT__", "__DSH_BOOT__" in html and '"entries"' in html)
    check("__DSH_BOOT__ 含插件行", '"@deepseek-ai/dsh-client-runtime"' in html)

    # 2. 插件 bundle
    status, headers, body = _http("GET", "/plugins/@deepseek-ai/dsh-client-runtime/client.js")
    check("GET /plugins/<id>/client.js 200 JS", status == 200
          and headers.get("Content-Type", "").startswith("text/javascript") and len(body) > 1000)

    # 3. shell 资产
    status, headers, body = _http("GET", "/assets/index-Dqw48FrP.js")
    check("GET /assets/index-*.js 200", status == 200 and len(body) > 1000)

    # 4. RPC 信封
    status, resp = _rpc("llm.models", {})
    check("POST /api/llm.models → server-response ok",
          status == 200 and resp.get("type") == "server-response" and resp["rpcId"] == "t-1"
          and resp["result"]["ok"] is True and resp["result"]["value"]["groups"][0]["models"][0]["id"] == "deepseek-v4-flash",
          json.dumps(resp, ensure_ascii=False)[:200])
    status, resp = _rpc("session.list", {})
    check("POST /api/session.list → ok items",
          resp.get("result", {}).get("ok") is True and isinstance(resp["result"]["value"]["items"], list))
    status, resp = _rpc("goal.create", {"sessionId": "demo-1", "objective": "x"})
    check("POST /api/goal.create → 业务错误(200+ok:false)",
          status == 200 and resp["result"]["ok"] is False and resp["result"]["error"]["code"] == "internal")
    status, _, body = _http("POST", "/api/nope", b"{bad json")
    check("非法信封 → 400", status == 400)

    # 5. SSE
    def sse_read():
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/events.mux")
        with urllib.request.urlopen(req, timeout=5) as resp:
            first = resp.readline().decode("utf-8")
            resp.readline()  # 空行
            third = resp.readline().decode("utf-8")
            return first, third

    sse_result = []
    st = threading.Thread(target=lambda: sse_result.append(sse_read()), daemon=True)
    st.start()
    time.sleep(1.0)
    st.join(timeout=4)
    check("SSE /api/events.mux 头与首帧 (: connected + data)",
          sse_result and sse_result[0][0].startswith(": connected")
          and sse_result[0][1].startswith("data: {") and "session/subscribed" in sse_result[0][1],
          repr(sse_result)[:200])

    # 6. WebSocket
    ws_status, ws_ok, frames = _ws_handshake("/api/events.mux")
    check("WS 升级 101 + Accept 正确", " 101 " in ws_status and ws_ok, ws_status)
    check("WS 收到 subscribed 基线帧", any(
        f.get("type") == "server-request" and f.get("method") == "session/subscribed" for f in frames),
        json.dumps(frames, ensure_ascii=False)[:300])

    server.shutdown()
    print(f"\n{'全部通过' if not failures else '失败: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
