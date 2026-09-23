"""LLM 运行时支撑 —— 流队列泵、哨兵与实例级共享状态。

搬运来源：旧包 `narnat_agent/core/llm.py` 143-160 行（`_iter_to_queue`）与 171-176 行
（类级共享状态与阈值常量）——算法零改动，只把"类级全局"收敛为**实例状态**
（对齐 design D1/D6 与 specs/llm「取消与中断」「流中断上报与静默挂死检测」）。

本模块是 `llm` 积木内部的共享支撑面（client 与两个后端的公共依赖），不构成对外契约。
"""
from __future__ import annotations

import queue
import time
from typing import Any, Protocol

from .retry import DEFAULT_MAX_RETRIES, clamp_retry_count

__all__ = [
    "LLMConfig",
    "LLMRuntime",
    "STREAM_END",
    "STREAM_POLL_SECONDS",
    "STREAM_STALL_SECONDS",
    "iter_to_queue",
]

STREAM_END = object()
"""流队列哨兵：读线程把迭代器耗尽（含异常退出）后投递，主循环据此收束。"""

STREAM_POLL_SECONDS = 0.05
"""流式接收的队列轮询间隔（秒）：也是取消标记的轮询间隔（specs/llm「取消与中断」）。"""

STREAM_STALL_SECONDS = 180.0
"""流式静默阈值（秒）：收到首个数据块后静默超过该值视为挂死，主动断连。"""


class LLMConfig(Protocol):
    """`llm` 积木所需的配置面（实现方：`config` 积木的 `AIConfig`）。

    同层之间不得互相 import（design D1），故此处以结构化协议声明读取面；
    字段语义对齐 specs/config，`thinking_passback` 允许缺失，读取时按缺省 True 处理。
    """

    protocol: str
    """协议选择值：`anthropic` 走 Anthropic 兼容协议，其余任意值走 OpenAI 兼容协议。"""

    model: str
    api_key: str
    base_url: str
    temperature: float | None
    """采样温度；None 表示请求不携带 `temperature`。"""

    max_tokens: int | None
    """配置的最大输出 token 数；None 表示 anthropic 请求沿用构造注入的默认值。"""

    retry_count: int
    """重试次数上限（构造与每轮对话开始时同步，钳制到 1–10）。"""

    thinking_enabled: bool
    thinking_effort: str
    thinking_passback: bool


class LLMRuntime:
    """单个 `LLMClient` 实例的运行时状态（消灭旧实现的类级全局）。

    - `tool_defs`：工具定义列表——两个后端共享同一列表对象，每轮请求**现读**，
      原地增删对下一轮立即生效，且不改动调用方传入的原列表；
    - `thinking_rules`：thinking 解析提供方（**必需注入，无默认值**——未注入即构造
      报错，不静默回退到任何副本），需提供
      `resolve_thinking_params(protocol, model, thinking_enabled, effort)` 与
      `resolve_thinking_passback(protocol, model)`；唯一权威实现是 `config.defaults`
      （`llm` 与 `config` 同层不得互相 import，由装配方注入）；
    - `max_retries`：重试上限（限流类与服务端/网络类同值，1–10）；
    - 活跃请求句柄：请求发出前指向底层 HTTP 客户端、流就绪后指向响应流、
      本轮结束清空（含异常路径），中断触发时关闭。
    """

    def __init__(self, thinking_rules: Any, tool_defs: list | None = None,
                 max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        self.tool_defs: list = list(tool_defs or [])
        self.thinking_rules: Any = thinking_rules
        self.max_retries: int = clamp_retry_count(max_retries)
        self._active_handle: Any = None

    def attach_handle(self, handle) -> None:
        """登记活跃请求句柄（底层 HTTP 客户端或响应流）。"""
        self._active_handle = handle

    def detach_handle(self) -> None:
        """清空活跃请求句柄（本轮结束，含异常路径）。"""
        self._active_handle = None

    def abort(self) -> None:
        """关闭活跃请求句柄；无句柄为空操作，关闭异常忽略。"""
        handle = self._active_handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def iter_to_queue(iterator, q: queue.Queue, err_box: list | None = None,
                  data_ts: list | None = None) -> None:
    """后台线程：把迭代器元素逐个放入队列，最后放入 `STREAM_END` 哨兵。

    迭代异常（如服务端中途断开）记录到 `err_box` 首个槽位，供主线程判断流是否被
    中断（不静默吞掉，避免把截断响应当作正常完成）；`err_box` 已有内容
    （看门狗已记录）时不重复追加。`data_ts` 为最近收到数据的时间戳（单元素列表，
    看门狗用）。
    """
    try:
        for item in iterator:
            if data_ts is not None:
                data_ts[0] = time.time()
            q.put(item)
    except Exception as e:
        if err_box is not None and not err_box:
            err_box.append(e)
    finally:
        q.put(STREAM_END)
