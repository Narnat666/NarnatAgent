"""事件广播中枢 —— mux/host 双流 + 每会话 seq 基线

对应 DSH /api/events.mux 与 /api/events.host 下行流：
- mux 订阅者收到所有会话的事件帧与控制帧；订阅时对每个已 attach 会话
  发送 session/subscribed 基线帧
- host 订阅者收到宿主级帧（session-added/removed/status 等），无基线
- 每会话 seq 单调递增：基线 = 该会话已持久化消息折算的事件数，
  在线事件在其上递增，保证 history(seq 0..n-1) 与在线流无缝衔接
"""

import threading
import time
from typing import Any, Dict, List, Set

from . import protocol


def _now_ms() -> int:
    return int(time.time() * 1000)


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._mux_transports: Set[Any] = set()
        self._host_transports: Set[Any] = set()
        # session_id -> 下一个可用 seq（即已投递事件数）
        self._seq: Dict[str, int] = {}
        # session_id -> 会话标题（供订阅基线之外的元信息，可空）
        self._titles: Dict[str, str] = {}
        # session_id -> {key: (value, seq)} 投影快照（新订阅者基线重放用）
        self._projections: Dict[str, Dict[str, tuple]] = {}

    # ── 订阅 ──

    def register_mux(self, transport) -> None:
        """注册 mux 订阅者：先发 subscribed 基线，再重放投影快照。"""
        with self._lock:
            self._mux_transports.add(transport)
            sessions = list(self._seq.keys())
            snapshots = {
                sid: dict(proj)
                for sid, proj in self._projections.items()
            }
        for sid in sessions:
            transport.send_json(protocol.server_request_frame({
                "type": "session/subscribed",
                "sessionId": sid,
                "lastSeq": self.last_seq(sid),
            }))
            for key, (value, seq) in snapshots.get(sid, {}).items():
                transport.send_json(protocol.server_request_frame({
                    "type": "session/projection",
                    "sessionId": sid,
                    "key": key,
                    "value": value,
                    "seq": seq,
                }))

    def register_host(self, transport) -> None:
        with self._lock:
            self._host_transports.add(transport)

    def unregister(self, transport) -> None:
        with self._lock:
            self._mux_transports.discard(transport)
            self._host_transports.discard(transport)

    # ── seq 管理 ──

    def attach_session(self, session_id: str, baseline: int, title: str = "") -> None:
        """登记会话；baseline = 已持久化事件数（历史尾 seq+1）。"""
        with self._lock:
            self._seq.setdefault(session_id, baseline)
            self._titles[session_id] = title

    def detach_session(self, session_id: str) -> None:
        with self._lock:
            self._seq.pop(session_id, None)
            self._titles.pop(session_id, None)

    def reset_seq(self, session_id: str, baseline: int) -> None:
        with self._lock:
            self._seq[session_id] = baseline

    def last_seq(self, session_id: str) -> int:
        """最近投递的 seq（无事件时为 -1，对应 DSH 的 lastSeq 约定）。"""
        with self._lock:
            return self._seq.get(session_id, 0) - 1

    def next_seq(self, session_id: str) -> int:
        with self._lock:
            seq = self._seq.get(session_id, 0)
            self._seq[session_id] = seq + 1
            return seq

    # ── 广播 ──

    def set_projection(self, session_id: str, key: str, value: Any, seq: int) -> None:
        """存储投影快照并广播 session/projection 帧（客户端按更高 seq 胜合并）。"""
        with self._lock:
            self._projections.setdefault(session_id, {})[key] = (value, seq)
        self.emit_session_frame({
            "type": "session/projection",
            "sessionId": session_id,
            "key": key,
            "value": value,
            "seq": seq,
        })

    def emit_session_event(self, session_id: str, event: Dict[str, Any]) -> None:
        """投递一条 session/event 帧（event 已含 type/seq/time/data）。"""
        frame = {"type": "session/event", "sessionId": session_id, "event": event}
        self._broadcast(self._mux_transports, frame)

    def emit_session_frame(self, frame: Dict[str, Any]) -> None:
        """投递任意 mux 控制帧（session/queue、session/jobs、session/projection 等）。"""
        self._broadcast(self._mux_transports, frame)

    def emit_host_frame(self, frame: Dict[str, Any]) -> None:
        self._broadcast(self._host_transports, frame)

    def _broadcast(self, transports: Set[Any], frame: Dict[str, Any]) -> None:
        with self._lock:
            targets = list(transports)
        dead = []
        for t in targets:
            if not t.send_json(protocol.server_request_frame(frame)):
                dead.append(t)
        if dead:
            with self._lock:
                for t in dead:
                    transports.discard(t)

    def session_ids(self) -> List[str]:
        with self._lock:
            return list(self._seq.keys())
