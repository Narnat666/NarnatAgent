"""父代理快速实验：close() 在「连接建立中」是否中断在途请求？

背景：E1 exp2 证明了「请求已发出、等响应头」阶段 close 有效（0.0s 中断）。
E2 慢路径（11.5s）提示另一种可能：close 发生得更早（connect/DNS/TLS 阶段），
此时连接池还没有可用连接——close 可能只是空操作，请求照常发出并等待响应。

本实验用一个「TCP 握手不响应」的地址（不可路由 IP）模拟长时间的连接建立阶段，
分别在 (A) connect 期间、(B) 连接后等响应期间调用 close()，观察 send() 何时返回。
"""
from __future__ import annotations

import threading
import time

import httpx

# 不可路由地址：SYN 无响应 → connect 卡住（本机 Windows 通常 21s 左右超时）
DEAD_ADDR = "http://10.255.255.1:81/"
# 本地快速失败地址：连接被立即拒绝（用于对照）
LOCAL_REFUSED = "http://127.0.0.1:1/"


def probe(url: str, close_after: float, label: str) -> None:
    client = httpx.Client(timeout=httpx.Timeout(connect=8.0, read=5.0, write=5.0, pool=5.0))
    t0 = time.perf_counter()
    outcome: dict = {}

    def run() -> None:
        try:
            resp = client.send(client.build_request("GET", url), stream=True)
            outcome["ok"] = time.perf_counter() - t0
            resp.close()
        except Exception as e:
            outcome["err"] = f"{type(e).__name__}: {e}"
            outcome["err_at"] = time.perf_counter() - t0

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(close_after)
    t_close = time.perf_counter() - t0
    try:
        client.close()
        outcome["close_ok"] = t_close
    except Exception as e:
        outcome["close_err"] = f"{type(e).__name__}: {e}"
    thread.join(timeout=30)
    print(f"\n[{label}] url={url} close@{close_after}s")
    print(f"   close 调用于 {t_close:.3f}s")
    print(f"   结果: {outcome}")
    print(f"   send 线程存活到实验结束: {thread.is_alive()}")


def main() -> None:
    print("=" * 70)
    print("实验 A：connect 进行中 close（不可路由地址，connect 预期卡住）")
    probe(DEAD_ADDR, 0.5, "A connect期间close")
    print("\n" + "=" * 70)
    print("实验 B：对照——连接被立即拒绝（close 前请求已失败）")
    probe(LOCAL_REFUSED, 0.001, "B 快速失败对照")


if __name__ == "__main__":
    main()
