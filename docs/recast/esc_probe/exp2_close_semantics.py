"""实验二：连接关闭语义验证（本地回环 stub server，不访问任何 LLM API）。

验证三条断言（"中断置位后为何很久才显示"的基石证据）：

A. `httpx.Client.close()` 不中断**正在等待响应头**的请求
   （对应 OpenAIBackend create() 阻塞期、AnthropicBackend send() 阻塞期）；
B. 关闭后的 httpx / OpenAI 客户端**不可复用**（后续请求直接抛错）
   ——对应"在响应头到达前按 Esc，会把共享客户端永久关闭"；
C. 对照：已建立流的 `response.close()` 能立即中断 `iter_lines()`
   ——对应"流就绪后 handle=流，abort 有效"。

复跑：
    cd /d D:\\desktop\\NarnatAgent && chcp 65001 >nul && python docs\\recast\\esc_probe\\exp2_close_semantics.py
产物：
    docs/recast/esc_probe/out/exp2_results.json
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_DIR = Path(__file__).resolve().parent / "out"

HEADER_DELAY_SECONDS = 8.0   # 服务端"思考"到返回响应头之间的静默
CLOSE_AT_SECONDS = 2.0       # 在请求进行到该时刻时关闭客户端


class SlowHandler(BaseHTTPRequestHandler):
    """stub 服务端：/slow 延迟返回响应头；/stream 持续推送 SSE 行。"""

    def log_message(self, *args):  # 静默
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/slow" or self.path.startswith("/v1/"):
            time.sleep(HEADER_DELAY_SECONDS)          # 模拟响应头前的长思考
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: hello\n\n")
                self.wfile.flush()
            except Exception:
                pass
        elif self.path == "/stream":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(600):                   # 最多 60 秒
                    self.wfile.write(f"data: chunk{i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except Exception:
                return


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def case_a_close_while_waiting_header(base_url: str) -> dict:
    client = httpx.Client(timeout=httpx.Timeout(connect=5.0, read=300.0,
                                                 write=60.0, pool=30.0))
    box: dict = {}
    t0 = time.perf_counter()

    def worker():
        try:
            req = client.build_request("POST", f"{base_url}/slow", json={})
            resp = client.send(req, stream=True)
            box["outcome"] = f"响应头到达 status={resp.status_code}"
            resp.close()
        except Exception as exc:
            box["outcome"] = f"异常 {type(exc).__name__}: {exc}"
        box["finished_at"] = time.perf_counter() - t0

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(CLOSE_AT_SECONDS)
    t_close = time.perf_counter() - t0
    client.close()
    thread.join(HEADER_DELAY_SECONDS + 5)

    delay_after_close = (box["finished_at"] - t_close) if "finished_at" in box else None
    return {
        "case": "A 等待响应头期间 close()",
        "header_delay_s": HEADER_DELAY_SECONDS,
        "close_at_s": round(t_close, 2),
        "request_finished_at_s": round(box.get("finished_at", -1), 2),
        "delay_after_close_s": round(delay_after_close, 2) if delay_after_close else None,
        "outcome": box.get("outcome"),
        "verdict": ("close 未中断进行中的请求（请求在服务端返回响应头后才结束）"
                    if delay_after_close and delay_after_close > 1.0
                    else "close 提前中断了请求"),
    }


def case_a2_openai_close_while_waiting(base_url: str) -> dict:
    """真实 OpenAI SDK 客户端：等待响应头期间 close() —— 与 narnat 的 abort 路径同构。"""
    from openai import OpenAI

    client = OpenAI(api_key="sk-fake", base_url=f"{base_url}/v1", max_retries=0)
    box: dict = {}
    t0 = time.perf_counter()

    def worker():
        try:
            stream = client.chat.completions.create(
                model="fake", messages=[{"role": "user", "content": "hi"}], stream=True)
            box["outcome"] = f"响应头到达（返回 {type(stream).__name__}）"
            stream.close()
        except Exception as exc:
            box["outcome"] = f"异常 {type(exc).__name__}: {exc}"
        box["finished_at"] = time.perf_counter() - t0

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(CLOSE_AT_SECONDS)
    t_close = time.perf_counter() - t0
    client.close()
    thread.join(HEADER_DELAY_SECONDS + 5)
    delay = (box["finished_at"] - t_close) if "finished_at" in box else None
    return {
        "case": "A2 真实 OpenAI SDK：等待响应头期间 close()",
        "header_delay_s": HEADER_DELAY_SECONDS,
        "close_at_s": round(t_close, 2),
        "create_finished_at_s": round(box.get("finished_at", -1), 2),
        "delay_after_close_s": round(delay, 2) if delay else None,
        "outcome": box.get("outcome"),
        "verdict": ("SDK close() 中断了进行中的请求（与 narnat abort 路径同构）"
                    if delay is not None and delay < 1.0 else "close 未中断请求"),
    }


def case_b_reuse_after_close(base_url: str) -> dict:
    client = httpx.Client(timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0))
    client.close()
    httpx_error = None
    try:
        client.post(f"{base_url}/slow", json={})
    except Exception as exc:
        httpx_error = f"{type(exc).__name__}: {exc}"

    openai_error = None
    try:
        from openai import OpenAI

        oc = OpenAI(api_key="sk-fake", base_url=f"{base_url}/v1", max_retries=0)
        oc.close()
        oc.chat.completions.create(model="fake", messages=[{"role": "user", "content": "hi"}])
    except Exception as exc:
        openai_error = f"{type(exc).__name__}: {exc}"

    return {
        "case": "B 关闭后复用客户端",
        "httpx_reuse_error": httpx_error,
        "openai_reuse_error": openai_error,
        "verdict": "关闭后的客户端不可复用（后续请求直接抛错）"
        if httpx_error and openai_error else "复用未报错（与预期不符）",
    }


def case_c_close_stream_resp(base_url: str) -> dict:
    client = httpx.Client(timeout=httpx.Timeout(connect=5.0, read=60.0,
                                                 write=60.0, pool=30.0))
    req = client.build_request("POST", f"{base_url}/stream", json={})
    resp = client.send(req, stream=True)
    box: dict = {"lines": 0, "outcome": None}
    t0 = time.perf_counter()

    def reader():
        try:
            for _ in resp.iter_lines():
                box["lines"] += 1
        except Exception as exc:
            box["outcome"] = f"异常 {type(exc).__name__}: {exc}"
        else:
            box["outcome"] = "迭代正常结束"
        box["finished_at"] = time.perf_counter() - t0

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(2.0)
    t_close = time.perf_counter() - t0
    resp.close()
    thread.join(5)
    client.close()
    delay = (box["finished_at"] - t_close) if "finished_at" in box else None
    return {
        "case": "C 流已建立后 close(response)",
        "lines_before_close": box["lines"],
        "close_at_s": round(t_close, 2),
        "reader_finished_at_s": round(box.get("finished_at", -1), 2),
        "delay_after_close_s": round(delay, 3) if delay is not None else None,
        "outcome": box.get("outcome"),
        "verdict": "response.close() 立即中断 iter_lines（流阶段 abort 有效）"
        if delay is not None and delay < 0.5 else "未立即中断（与预期不符）",
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    server, base_url = start_server()
    print(f"stub server: {base_url}（本地回环，无外网请求）")
    results = []
    try:
        for case_fn in (case_a_close_while_waiting_header,
                        case_a2_openai_close_while_waiting,
                        case_b_reuse_after_close,
                        case_c_close_stream_resp):
            res = case_fn(base_url)
            results.append(res)
            print("\n" + res["case"])
            for key, value in res.items():
                if key not in ("case", "verdict"):
                    print(f"    {key} = {value}")
            print(f"    → {res['verdict']}")
    finally:
        server.shutdown()

    (OUT_DIR / "exp2_results.json").write_text(
        json.dumps({"cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写 {OUT_DIR / 'exp2_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
