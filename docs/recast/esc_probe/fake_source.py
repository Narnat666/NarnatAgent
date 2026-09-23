"""假字符源 —— 受控投喂「字节序列 + 到达时刻」，驱动真实的 poll_keys / scan_escape。

设计约束（对齐主线 `narnat_agent/interrupt/keys.py` 的 KeySource 契约）：

- `read(timeout)` 语义与 `_MsvcrtSource.read` 一致：有数据立即返回；无数据则等待至多
  timeout 秒后再查一次；仍无返回 None。因此「字节在 read 的 sleep 期间到达」会在
  sleep 结束后才被取走——与真实轮询（30 毫秒周期）的送达延迟一致；
- 到达时刻为相对 t0（`start_clock()` 设定）的秒数，负值表示「采集启动前已积压」；
- 全程只做时间调度，不模拟平台控制台差异（那属真机实验 E2）。

本模块不修改主线代码，仅被本目录实验脚本导入。
"""
from __future__ import annotations

import time

ESC = b"\x1b"
BQ = b"`"


class ScriptedSource:
    """按时间表投喂字节的 KeySource 实现。

    Attributes:
        reads:     [(t_rel, timeout, returned)] 每次 read 调用记录；
        delivered: [(t_rel, byte)] 实际交付的字节；
        close_called: 采集线程收尾是否调用了 close()。
    """

    def __init__(self, schedule, *, escape_immediate: bool = False,
                 probe_timeout: float = 0.0, consume_timeout: float = 0.0,
                 name: str = "scripted") -> None:
        self.escape_immediate = escape_immediate
        self.probe_timeout = probe_timeout
        self.consume_timeout = consume_timeout
        self.name = name
        self._schedule = sorted((float(t), bytes(b)) for t, b in schedule)
        self._index = 0
        self._t0: float | None = None
        self.reads: list[tuple[float, float, bytes | None]] = []
        self.delivered: list[tuple[float, bytes]] = []
        self.close_called = False

    # ── 时钟 ──

    def start_clock(self) -> None:
        """设定 t0（时间表以此为基准；应在 enter_run_mode() 之前调用）。"""
        self._t0 = time.perf_counter()

    @property
    def t0(self) -> float | None:
        return self._t0

    def now(self) -> float:
        return time.perf_counter() - self._t0

    # ── KeySource 协议 ──

    def read(self, timeout: float) -> bytes | None:
        if self._t0 is None:
            raise RuntimeError("start_clock() 未调用")
        pending = self._peek()
        if pending is not None and pending[0] <= self.now():
            return self._take()
        if timeout <= 0:
            self.reads.append((self.now(), timeout, None))
            return None
        # 与 _MsvcrtSource 一致：整段等待后再查一次
        time.sleep(timeout)
        pending = self._peek()
        if pending is not None and pending[0] <= self.now():
            return self._take()
        self.reads.append((self.now(), timeout, None))
        return None

    def close(self) -> None:
        self.close_called = True

    # ── 内部 ──

    def _peek(self):
        if self._index >= len(self._schedule):
            return None
        return self._schedule[self._index]

    def _take(self) -> bytes:
        t_arrival, data = self._schedule[self._index]
        self._index += 1
        now = self.now()
        self.delivered.append((now, data))
        self.reads.append((now, None, data))
        return data

    @property
    def remaining(self) -> list[tuple[float, bytes]]:
        """未被投喂（仍在台面下）的字节。"""
        return self._schedule[self._index:]

    @property
    def delivered_bytes(self) -> bytes:
        return b"".join(b for _, b in self.delivered)


def describe_bytes(data: bytes) -> str:
    """字节序列的可读描述（ESC → <Esc>；可打印字符原样）。"""
    out = []
    i = 0
    while i < len(data):
        if data[i:i + 1] == ESC:
            out.append("<Esc>")
        else:
            try:
                out.append(data[i:i + 1].decode("ascii"))
            except UnicodeDecodeError:
                out.append(f"\\x{data[i]:02x}")
        i += 1
    return "".join(out)
