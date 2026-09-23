"""可取消的阻塞调用等待原语 —— 请求发送阶段的取消检查点。

设计依据：`docs/recast/esc_review_notes.md` 根因 A/B/P3。慢路径机制链：请求发送
（`httpx.Client.send` / `openai` 的 `chat.completions.create`）阻塞在建连或等响应头
阶段时，生成器体卡在该调用上，流循环的 0.05 秒取消轮询尚不可达（根因 B）；而中断
触发的 `close()` 对"连接建立中"的在途请求是空操作，请求只能自行跑到响应头到达
（根因 A，实测 5~11.5 秒）。本模块把阻塞调用移入子线程，主流程以 `poll_seconds`
粒度轮询取消标记：取消命中即尽力掐断并立即返回，不再等阻塞调用自行结束。

待 spec 同步：specs/llm「取消与中断」目前只约束"流式接收期间"的轮询，本模块把
轮询扩展到"请求发送期间"（有意行为变更，消除慢路径）。
"""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

__all__ = ["run_cancelable", "safe_close"]

T = TypeVar("T")

CANCEL_POLL_SECONDS = 0.05
"""取消标记轮询间隔缺省值（秒），与流式接收的轮询粒度一致（specs/llm「取消与中断」）。"""

SEND_THREAD_NAME = "narnat-llm-send"
"""阻塞发送线程名（排障用：线程转储里可直接定位请求发送线程）。"""

NO_RESULT = object()
"""结果槽初值哨兵：区分"阻塞调用尚未产出结果"与"结果为 None"。"""


def safe_close(target) -> None:
    """尽力关闭句柄（异常忽略）——中断路径不得因关闭失败影响收敛。"""
    try:
        target.close()
    except Exception:
        pass


def run_cancelable(do_block: Callable[[], T],
                   cancel_check: Callable[[], bool] | None,
                   on_cancel: Callable[[], None] | None = None,
                   poll_seconds: float = CANCEL_POLL_SECONDS) -> tuple[bool, T | None, Exception | None]:
    """在子线程执行阻塞调用；主流程以 poll_seconds 粒度轮询取消标记。

    返回 (cancelled, result, error)：
    - cancelled=True：主流程立即返回（不等子线程）；on_cancel() 已调用（尽力掐断，
      对已建立连接有效）；子线程在阻塞解除后自毁 result（close 幂等，双保险）。
    - cancelled=False：result 或 error 二选一（阻塞调用的结果）。

    取代"在主流程直接调用阻塞发送"的时序：正常情况下子线程完成即唤醒主流程返回，
    不引入固定延迟；取消命中时主流程不再等服务端（`cancel_check` 为 None 时退化为
    纯等待，与直接调用等价）。
    """
    done = threading.Event()
    cancelled = threading.Event()
    box: dict = {"result": NO_RESULT, "error": None}

    def _worker() -> None:
        try:
            box["result"] = do_block()
        except BaseException as exc:  # 非 Exception 基类异常同样交回主流程上抛
            box["error"] = exc
        finally:
            done.set()
            # 兜底自毁：取消已置位且阻塞调用随后才产出结果时由子线程补关。close 幂等，
            # 任何时序下结果至多被关闭两次（主线程竞态窗口一次、此处一次）。
            if cancelled.is_set() and box["result"] is not NO_RESULT:
                safe_close(box["result"])

    threading.Thread(target=_worker, name=SEND_THREAD_NAME, daemon=True).start()

    while True:
        # 完成即唤醒（正常路径零额外延迟）；超时说明阻塞未结束，此时才查取消标记
        if done.wait(poll_seconds):
            break
        if cancel_check is not None and cancel_check():
            cancelled.set()
            if on_cancel is not None:
                try:
                    on_cancel()
                except Exception:
                    pass
            # 关闭竞态窗口：取消置位与阻塞完成同时发生时结果可能已产生，主线程补关一次
            if box["result"] is not NO_RESULT:
                safe_close(box["result"])
            return True, None, None

    if box["error"] is not None:
        return False, None, box["error"]
    return False, box["result"], None
