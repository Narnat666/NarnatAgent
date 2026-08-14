"""DshBridge —— DSH Web 前端与 Narnat Agent 核心之间的桥

职责：
1. 启动 HTTP/WS 服务器（server.py），托管 DSH 前端静态工件 + 协议端点
2. 输入合并：终端 stdin 与 Web prompt 汇入同一队列，主循环统一消费
3. 流包装：把 AgentLoop 的 UIStreamSession 包装成同时向 DSH 前端广播
   turn/step 事件与 assistant/chunk 增量
4. 轮次后消息差量同步：把新追加的 assistant/tool 消息翻译成 DSH 表面事件
5. 会话映射：Narnat (name, parent) ↔ DSH sessionId；切换/新建/保存同步
"""

import json
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ...config import session_store
from . import handlers, translate
from .events import EventHub
from .server import DshServer


def _set_path(obj: dict, path: List[Any], value: Any):
    if not path:
        return
    node = obj
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[path[-1]] = value


def _unset_path(obj: dict, path: List[Any]):
    if not path:
        return
    node = obj
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            return
        node = node[part]
    node.pop(path[-1], None)


def _deep_merge(target: dict, patch: dict):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


class WebStreamBridge:
    """包装终端 UIStreamSession：转发渲染调用 + 广播 DSH 流事件。"""

    def __init__(self, bridge: "DshBridge", inner, sid: str, turn: int):
        self._bridge = bridge
        self._inner = inner
        self._sid = sid
        self._turn = turn
        self._step = 0
        self._step_open = False
        self._cancelled = False
    # ── 属性（AgentLoop 依赖） ──
    @property
    def cancelled(self) -> bool:
        return self._cancelled or bool(getattr(self._inner, "cancelled", False))

    @property
    def aborted(self) -> bool:
        return bool(getattr(self._inner, "aborted", False))

    # ── 流接口 ──
    def begin(self):
        if hasattr(self._inner, "begin"):
            self._inner.begin()

    def feed(self, chunk: str):
        if hasattr(self._inner, "feed"):
            self._inner.feed(chunk)
        self._ensure_step()
        self._bridge.hub.emit_session_event(self._sid, {
            "type": "assistant/chunk",
            "seq": self._bridge.hub.next_seq(self._sid),
            "time": translate._now_ms(),
            "data": {
                "turn": self._turn,
                "step": self._step,
                "chunk": {"type": "text-delta", "index": 0, "text": chunk},
            },
        })

    def pause_spinner(self):
        if hasattr(self._inner, "pause_spinner"):
            self._inner.pause_spinner()

    def flush_renderer(self):
        if hasattr(self._inner, "flush_renderer"):
            self._inner.flush_renderer()

    def resume_spinner(self):
        if hasattr(self._inner, "resume_spinner"):
            self._inner.resume_spinner()

    def finish(self, *args, **kwargs):
        try:
            self._inner.finish(*args, **kwargs)
        except TypeError:
            pass
        # step/end 不在这里发：由 turn_finished 在 assistant/message 之后统一发出
        # （否则 step 先关闭，客户端会把流式片段冻结成"已停止"节点）

    def abort(self):
        if hasattr(self._inner, "abort"):
            self._inner.abort()
        # 同上：step/end 交给 turn_finished

    def cancel(self):
        """Web 端中断请求。"""
        self._cancelled = True
        try:
            from ...core import interrupt
            interrupt.abort_request()
        except Exception:
            pass

    def _ensure_step(self):
        if self._step_open:
            return
        # step 号 = 本回合内 assistant 消息数 + 1
        msgs = self._bridge.current_messages()
        assistants_in_turn = 0
        for m in reversed(msgs):
            role = m.get("role")
            if role == "user":
                break
            if role == "assistant":
                assistants_in_turn += 1
        self._step = assistants_in_turn + 1
        self._bridge.hub.emit_session_event(self._sid, {
            "type": "step/start",
            "seq": self._bridge.hub.next_seq(self._sid),
            "time": translate._now_ms(),
            "data": {"turn": self._turn, "step": self._step},
        })
        self._step_open = True
        # 记录给 turn_finished 用（step/end 在最终消息之后发出）
        self._bridge._last_step_info = (self._sid, self._turn, self._step)


class DshBridge:
    def __init__(self, parts, static_dir: str, port: int = 8765, debug: bool = False):
        self._parts = parts
        self._config = parts.config
        self._mgr = parts.session_mgr
        self._msg_manager = parts.msg_manager
        self._stats = parts.stats
        self._debug = debug

        self.hub = EventHub()
        self._lock = threading.Lock()
        self._input_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._terminal_thread: Optional[threading.Thread] = None
        self._stream: Optional[WebStreamBridge] = None
        self._stream_lock = threading.Lock()

        # 会话索引与 seq 基线
        self._sid_index: Dict[str, Tuple[str, Optional[str]]] = {}  # sid -> (name, parent)
        self._titles: Dict[str, str] = {}
        self._current_sid: str = translate.LIVE_SESSION_ID
        self._msg_snapshot_start: int = 0
        self._claimed_user_index: Optional[int] = None
        self._claimed_turn: Optional[int] = None
        self._last_step_info: Optional[tuple] = None  # (sid, turn, step) 待关闭的流式 step
        self._turn_interrupted: bool = False
        self._busy: bool = False  # 当前是否有轮次在执行（DSH 的 running 语义）

        self._server = DshServer(self, static_dir, port)
        self._server_thread: Optional[threading.Thread] = None

        # DSH 客户端设置存储（ui-onboarding/主题等写入经 settings.* 端点持久化）
        self._settings_lock = threading.Lock()
        self._settings: Dict[str, Any] = {}
        self._settings_revisions: Dict[str, int] = {}
        self._load_settings()

    # ── 生命周期 ──

    def start(self):
        """启动服务器线程、终端输入线程，attach 全部会话。"""
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="dsh-web-server")
        self._server_thread.start()
        self._terminal_thread = threading.Thread(
            target=self._terminal_reader, daemon=True, name="dsh-terminal-reader")
        self._terminal_thread.start()
        self.refresh_sessions(attach=True)
        # live（未保存）会话也要有订阅基线
        self.hub.attach_session(
            translate.LIVE_SESSION_ID,
            translate.count_events(self.current_messages()),
            "未保存会话")
        self._snapshot_start()

    def shutdown(self):
        self._server.shutdown()

    def _terminal_reader(self):
        """后台线程：终端 UI 输入 → 桥队列（无 TTY 时退化为纯 Web 模式）。"""
        ui = self._parts.ui
        failures = 0
        while True:
            try:
                text = ui.read_input()
                failures = 0
            except Exception:
                failures += 1
                if failures > 5:
                    # 无 TTY（后台运行等）：终端读取不可用，仅保留 Web 输入
                    return
                time.sleep(0.5)
                continue
            if text is None:
                continue
            self._input_queue.put(text)

    # ── 输入回路 ──

    def read_input(self) -> Optional[str]:
        """主循环的输入源（阻塞；合并终端与 Web）。"""
        text = self._input_queue.get()
        if text is None:
            return None
        stripped = text.strip()
        if stripped and not stripped.startswith("/"):
            self._claim_turn(stripped)
        return text

    def submit_input(self, text: str):
        """Web prompt → 输入队列。"""
        if not text or not text.strip():
            return
        self._input_queue.put(text)

    def _claim_turn(self, text: str):
        """用户消息被接收：发 turn/start + user/message，并置忙。"""
        with self._lock:
            sid = self._current_sid
            msgs = self.current_messages()
            turn = sum(1 for m in msgs if m.get("role") == "user") + 1
            index = len(msgs)  # 该消息将 append 到的下标
            self._claimed_user_index = index
            self._claimed_turn = turn
            self._busy = True
        self.hub.emit_host_frame({
            "type": "host/session-status",
            "sessionId": sid,
            "running": True,
        })
        self.hub.emit_session_event(sid, {
            "type": "turn/start",
            "seq": self.hub.next_seq(sid),
            "time": translate._now_ms(),
            "data": {"turn": turn},
        })
        self.hub.emit_session_event(sid, {
            "type": "user/message",
            "seq": self.hub.next_seq(sid),
            "time": translate._now_ms(),
            "data": {
                "id": translate._msg_id(sid, index),
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "source": {"kind": "user"},
            },
            "surfaceOp": "append",
        })

    # ── 流包装 ──

    def wrap_stream(self, inner):
        sid = self.current_sid()
        # turn 必须与 claim 时一致：claim 在用户消息入列表前计算（+1），
        # 此时消息已入列表，直接复用 claim 的编号（否则流片段与最终消息
        # 的 turn 不一致，客户端会渲染成"已停止"片段+重复消息）。
        with self._lock:
            turn = self._claimed_turn
        if turn is None:
            msgs = self.current_messages()
            turn = sum(1 for m in msgs if m.get("role") == "user") or 1
        stream = WebStreamBridge(self, inner, sid, turn)
        with self._stream_lock:
            self._stream = stream
        return stream

    def cancel_current(self):
        with self._stream_lock:
            if self._stream is not None:
                self._stream.cancel()

    # ── 轮次结束同步 ──

    def _snapshot_start(self):
        self._msg_snapshot_start = len(self.current_messages())
        self._claimed_user_index = None
        self._claimed_turn = None
        self._last_step_info = None

    def turn_finished(self, ok: bool):
        """agent_loop 返回后由 Agent.run 调用：差量同步消息 → DSH 事件。"""
        sid = self.current_sid()
        msgs = self.current_messages()
        with self._lock:
            start = self._msg_snapshot_start
            claimed = self._claimed_user_index
        if len(msgs) < start:
            start = 0  # 压缩/重建导致列表收缩
        if claimed is not None and claimed >= len(msgs):
            claimed = None

        # 全量翻译后截取差量（保证 turn/step 编号与历史一致）
        provider = getattr(self._config.ai, "protocol", "narnat")
        model = getattr(self._config.ai, "model", "narnat")
        snapshot_msgs = msgs[:start]
        baseline_events = translate.messages_to_events(
            snapshot_msgs, sid, 0, provider, model)
        all_events = translate.messages_to_events(
            msgs, sid, 0, provider, model)
        delta = all_events[len(baseline_events):]

        emitted_last_assistant = False
        for ev in delta:
            etype = ev.get("type")
            # turn/step 结构已由实时流发出；此处只补表面事件
            if etype not in ("assistant/message", "tool/result"):
                continue
            # 跳过 claim 时已实时发出的 user/message（安全分支）
            if etype == "user/message":
                continue
            if etype == "assistant/message":
                emitted_last_assistant = True
                ev["data"]["usage"] = {
                    "inputTokens": int(getattr(self._stats, "input_tokens", 0) or 0),
                    "outputTokens": int(getattr(self._stats, "output_tokens", 0) or 0),
                }
            else:
                emitted_last_assistant = False
            self.hub.emit_session_event(sid, {
                "type": ev["type"],
                "seq": self.hub.next_seq(sid),
                "time": translate._now_ms(),
                "data": ev["data"],
                "surfaceOp": "append",
            })
        if emitted_last_assistant:
            pass  # usage 已附在最后一条 assistant/message 上

        # TodoWrite → todo/write 快照 + todos 投影
        todos = translate.last_todos(msgs)
        if todos is not None:
            last_todo_event = self._find_todo_event(delta)
            if last_todo_event:
                self.hub.emit_session_event(sid, {
                    "type": "todo/write",
                    "seq": self.hub.next_seq(sid),
                    "time": translate._now_ms(),
                    "data": {"todos": todos},
                })
            self.hub.emit_session_frame({
                "type": "session/projection",
                "sessionId": sid,
                "key": "todos",
                "value": todos,
                "seq": self.hub.last_seq(sid),
            })

        # 流式 step 收尾：assistant/message 之后、turn/end 之前（DSH 原生顺序）
        with self._lock:
            last_step = self._last_step_info
        if last_step is not None:
            step_sid, step_turn, step_num = last_step
            self.hub.emit_session_event(step_sid, {
                "type": "step/end",
                "seq": self.hub.next_seq(step_sid),
                "time": translate._now_ms(),
                "data": {"turn": step_turn, "step": step_num},
            })

        # turn/end 收尾
        turn = sum(1 for m in msgs if m.get("role") == "user")
        self.hub.emit_session_event(sid, {
            "type": "turn/end",
            "seq": self.hub.next_seq(sid),
            "time": translate._now_ms(),
            "data": {
                "turn": turn,
                "reason": "completed" if ok else "interrupted",
            },
        })

        # 置闲 + 会话状态帧
        with self._lock:
            self._busy = False
        self.hub.emit_host_frame({
            "type": "host/session-status",
            "sessionId": sid,
            "running": False,
        })

        # 会话状态可能因 /save 等命令改变 → 刷新
        self.refresh_sessions(attach=True)
        self._snapshot_start()

    @staticmethod
    def _find_todo_event(delta) -> Optional[Dict[str, Any]]:
        for ev in delta:
            if ev.get("type") != "assistant/message":
                continue
            for block in ev["data"]["message"]["content"]:
                if block.get("type") == "tool-call" and block.get("name") == "TodoWrite":
                    return ev
        return None

    # ── 会话映射 ──

    def current_sid(self) -> str:
        name = self._mgr.state.session_name()
        parent = self._mgr.state.session_parent()
        return translate.session_id(name, parent)

    def current_messages(self) -> List[Dict[str, Any]]:
        try:
            return list(self._mgr.get_messages())
        except Exception:
            return []

    def refresh_sessions(self, attach: bool = False):
        """重建 sid 索引；登记新会话到 hub；广播 host 帧。"""
        try:
            tree = session_store.list_sessions_tree(self._mgr.narnat_dir)
        except Exception:
            tree = []
        with self._lock:
            old_ids = set(self._sid_index.keys())
            new_index: Dict[str, Tuple[str, Optional[str]]] = {}
            for root in tree:
                name = root.get("name") or ""
                sid = translate.session_id(name)
                new_index[sid] = (name, None)
                self._titles[sid] = name
                for child in root.get("children") or []:
                    cname = child.get("name") or ""
                    csid = translate.session_id(cname, name)
                    new_index[csid] = (cname, name)
                    self._titles[csid] = cname
            self._sid_index = new_index
            added = set(new_index.keys()) - old_ids
            removed = old_ids - set(new_index.keys())
            self._current_sid = self.current_sid()
        for sid in added:
            if attach:
                self.hub.attach_session(sid, self._seq_baseline(sid), self._titles.get(sid, ""))
            name, parent = self._sid_index.get(sid, (None, None))
            self.hub.emit_host_frame({
                "type": "host/session-added",
                "sessionId": sid,
                "blank": False,
                **({"parentSessionId": translate.session_id(parent)} if parent else {}),
            })
        for sid in removed:
            self.hub.detach_session(sid)
            self.hub.emit_host_frame({"type": "host/session-removed", "sessionId": sid})
        # 投影快照：标题/列表元数据/todos（列表行读 title 投影键）
        for sid, (name, parent) in self._sid_index.items():
            self._publish_session_projections(sid, name, parent)
        # live（未保存）会话投影
        if not self._mgr.has_active_session():
            seq = max(self.hub.last_seq(translate.LIVE_SESSION_ID), 0)
            msgs = self.current_messages()
            self.hub.set_projection(translate.LIVE_SESSION_ID, "title", "未保存会话", seq)
            self.hub.set_projection(translate.LIVE_SESSION_ID, "sessionListMetadata", {
                "blank": len([m for m in msgs if m.get("role") != "system"]) == 0,
                "lastPromptAt": translate._now_ms(),
            }, seq)
            self.hub.set_projection(translate.LIVE_SESSION_ID, "todos",
                                    translate.last_todos(msgs), seq)
        # 工作区视图变化（会话增删后客户端 upsert）
        self.hub.emit_host_frame({
            "type": "host/workspace-changed",
            "workspace": self.workspace_view(),
        })

    def _publish_session_projections(self, sid: str, name: str, parent: Optional[str]):
        """发布某会话的投影快照（title / sessionListMetadata / todos）。"""
        if sid == self._current_sid:
            messages = self.current_messages()
        else:
            try:
                messages, err = session_store.load_session(self._mgr.narnat_dir, name, parent)
                if err:
                    messages = []
            except Exception:
                messages = []
        # 帧 schema 要求投影 seq ≥ 0（-1 仅 subscribed 帧允许）
        seq = max(self.hub.last_seq(sid), 0)
        blank = len([m for m in messages if m.get("role") != "system"]) == 0
        self.hub.set_projection(sid, "title", name, seq)
        self.hub.set_projection(sid, "sessionListMetadata", {
            "blank": blank,
            "lastPromptAt": translate._now_ms(),
        }, seq)
        todos = translate.last_todos(messages)
        self.hub.set_projection(sid, "todos", todos, seq)

    def _seq_baseline(self, sid: str) -> int:
        name, parent = self._sid_index.get(sid, (None, None))
        if sid == self._current_sid:
            return translate.count_events(self.current_messages())
        if name is None:
            return 0
        try:
            messages, err = session_store.load_session(self._mgr.narnat_dir, name, parent)
            if err:
                return 0
            provider = getattr(self._config.ai, "protocol", "narnat")
            model = getattr(self._config.ai, "model", "narnat")
            return translate.count_events(messages)
        except Exception:
            return 0

    def find_session(self, sid: str) -> Optional[Tuple[str, Optional[str]]]:
        with self._lock:
            return self._sid_index.get(sid)

    def session_list_rows(self) -> List[Dict[str, Any]]:
        try:
            tree = session_store.list_sessions_tree(self._mgr.narnat_dir)
        except Exception:
            tree = []
        rows = translate.session_tree_to_summaries(tree, self._current_sid)
        # running 语义 = 当前是否有轮次在执行（idle 时全部 False）
        for row in rows:
            row["running"] = False
        # live（未保存）会话排在最前
        if not self._mgr.has_active_session():
            rows.insert(0, {
                "sessionId": translate.LIVE_SESSION_ID,
                "updatedAt": translate._now_ms(),
                "running": self._busy,
                "blank": len(self.current_messages()) <= 1,
                "projections": {
                    "asOfSeq": self.hub.last_seq(translate.LIVE_SESSION_ID),
                    "values": {
                        "title": "未保存会话",
                        "sessionListMetadata": {
                            "blank": len(self.current_messages()) <= 1,
                            "lastPromptAt": translate._now_ms(),
                        },
                    },
                },
            })
        return rows

    def workspace_view(self) -> Dict[str, Any]:
        """虚拟"Narnat 工作区"：DSH 的会话必须归属工作区，输入框才会解锁。

        用一个固定工作区承载全部 Narnat 会话（含未保存的 live 会话）。
        """
        with self._lock:
            session_ids = [translate.LIVE_SESSION_ID] + sorted(
                set(self._sid_index.keys()) - {translate.LIVE_SESSION_ID})
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        return {
            "workspaceId": translate.WORKSPACE_ID,
            "path": self.project_root(),
            "title": "Narnat 会话",
            "sessionIds": session_ids,
            "createdAt": iso,
            "updatedAt": iso,
        }

    def load_session_messages(self, sid: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """取会话消息：live 取内存，其余读盘。返回 (messages, error)。"""
        if sid == self._current_sid:
            return self.current_messages(), None
        target = self.find_session(sid)
        if target is None:
            return None, "会话不存在"
        name, parent = target
        try:
            messages, err = session_store.load_session(self._mgr.narnat_dir, name, parent)
        except Exception as e:
            return None, str(e)
        if err:
            return None, err
        return messages, None

    def title_of(self, sid: str) -> str:
        with self._lock:
            return self._titles.get(sid, "")

    def switch_to(self, sid: str) -> Optional[str]:
        """把 Agent 切到 sid 指向的已保存会话。返回错误消息或 None。"""
        if sid == self._current_sid:
            return None
        target = self.find_session(sid)
        if target is None:
            return "会话不存在"
        name, parent = target
        ref = f"{parent}/{name}" if parent else name
        try:
            err = self._mgr.on_enter(ref)
        except Exception as e:
            return str(e)
        if err:
            return err
        self._current_sid = sid
        # 保存当前消息快照与 seq 基线，后续事件从历史尾继续
        self._snapshot_start()
        return None

    # ── 其它 ──

    def project_root(self) -> str:
        return getattr(self._config.paths, "project_root", "") or ""

    def data_dir(self) -> str:
        return getattr(self._config.paths, "data_dir", "") or ""

    # ── DSH 客户端设置存储 ──

    _DEFAULT_SETTINGS = {
        "ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"},
    }

    def _settings_path(self) -> str:
        return os.path.join(self.data_dir() or ".narnat/data", "dsh_settings.json")

    def _load_settings(self):
        with self._settings_lock:
            self._settings = dict(self._DEFAULT_SETTINGS)
            try:
                with open(self._settings_path(), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for ns, value in data.items():
                        if isinstance(value, dict):
                            self._settings[ns] = value
            except (OSError, json.JSONDecodeError):
                pass
            self._settings_revisions = {ns: 1 for ns in self._settings}

    def _save_settings_locked(self):
        try:
            os.makedirs(os.path.dirname(self._settings_path()), exist_ok=True)
            tmp = self._settings_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._settings_path())
        except OSError:
            pass

    def settings_namespaces(self) -> List[Dict[str, Any]]:
        with self._settings_lock:
            return [{
                "ns": ns,
                "schema": {},
                "value": value,
                "applies": "live",
                "secrets": [],
                "revision": self._settings_revisions.get(ns, 1),
            } for ns, value in sorted(self._settings.items())]

    def settings_update(self, ns: str, patch: Dict[str, Any],
                        expected_revision: Optional[int] = None) -> Dict[str, Any]:
        """settings.update：深合并 patch 到命名空间值。"""
        with self._settings_lock:
            rev = self._settings_revisions.get(ns, 1)
            if expected_revision is not None and rev != expected_revision:
                raise handlers.HandledError(
                    "settings-conflict", "设置已被修改，请刷新",
                    {"ns": ns, "expected": expected_revision, "actual": rev})
            current = self._settings.setdefault(ns, {})
            _deep_merge(current, patch if isinstance(patch, dict) else {})
            rev += 1
            self._settings_revisions[ns] = rev
            self._save_settings_locked()
            return {
                "ns": ns,
                "schema": {},
                "value": current,
                "applies": "live",
                "secrets": [],
                "revision": rev,
            }

    def settings_replace(self, ns: str, section: Dict[str, Any],
                         expected_revision: Optional[int] = None) -> Dict[str, Any]:
        with self._settings_lock:
            rev = self._settings_revisions.get(ns, 1)
            if expected_revision is not None and rev != expected_revision:
                raise handlers.HandledError(
                    "settings-conflict", "设置已被修改，请刷新",
                    {"ns": ns, "expected": expected_revision, "actual": rev})
            self._settings[ns] = dict(section) if isinstance(section, dict) else {}
            rev += 1
            self._settings_revisions[ns] = rev
            self._save_settings_locked()
            return {
                "ns": ns,
                "schema": {},
                "value": self._settings[ns],
                "applies": "live",
                "secrets": [],
                "revision": rev,
            }

    def settings_mutate(self, ns: str, ops: List[Dict[str, Any]],
                        expected_revision: Optional[int] = None) -> Dict[str, Any]:
        with self._settings_lock:
            rev = self._settings_revisions.get(ns, 1)
            if expected_revision is not None and rev != expected_revision:
                raise handlers.HandledError(
                    "settings-conflict", "设置已被修改，请刷新",
                    {"ns": ns, "expected": expected_revision, "actual": rev})
            current = self._settings.setdefault(ns, {})
            for op in ops:
                if not isinstance(op, dict):
                    continue
                path = op.get("path") or []
                if op.get("op") == "set":
                    _set_path(current, path, op.get("value"))
                elif op.get("op") == "unset":
                    _unset_path(current, path)
            rev += 1
            self._settings_revisions[ns] = rev
            self._save_settings_locked()
            return {
                "ns": ns,
                "schema": {},
                "value": current,
                "applies": "live",
                "secrets": [],
                "revision": rev,
            }
