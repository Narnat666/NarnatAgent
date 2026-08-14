"""Narnat 数据 → DSH 前端协议的翻译层（纯函数）

映射关系：
- Narnat 会话 (name, parent) → DSH sessionId（uuid5 确定性生成，跨重启稳定）
- Narnat 消息（OpenAI 风格） → DSH SessionEvent 序列
    user       → turn/start + user/message
    assistant  → step/start + assistant/message (+ tool/call 内含)
    tool       → tool/result
    末尾       → step/end + turn/end
- 会话名 → title 投影；消息计数 → sessionListMetadata 投影
- TodoWrite 工具调用 → todo/write 事件 + todos 投影
- 模型配置 → session.models / llm.models 形状
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ...config import session_store

# uuid5 命名空间（固定，保证 sessionId 跨重启稳定）
_SID_NS = uuid.UUID("6b8e4a1c-9d3f-4a2b-b5c7-1f0e8d2a4c6b")

# NoSession（未 /save）时的合成 live 会话 id
LIVE_SESSION_ID = "narnat-live"

# 虚拟"Narnat 工作区" id（DSH 会话必须归属工作区才能解锁输入框）
WORKSPACE_ID = "narnat-workspace"


def session_id(name: Optional[str], parent: Optional[str] = None) -> str:
    if not name:
        return LIVE_SESSION_ID
    key = f"{parent or ''}\x00{name}"
    return str(uuid.uuid5(_SID_NS, key))


def _msg_id(sid: str, index: int) -> str:
    return str(uuid.uuid5(_SID_NS, f"{sid}\x00msg\x00{index}"))


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _is_error_result(text: str) -> bool:
    head = text.strip()[:24]
    return ("错误" in head or "失败" in head or "[用户中断]" in text
            or "[操作已取消" in text or "拦截" in head)


# ── 消息 → 事件 ──

def count_events(messages: List[Dict[str, Any]]) -> int:
    """确定性事件数（= 历史 replay 的事件数），用于 live seq 基线。"""
    return len(messages_to_events(messages, "x", 0, "narnat", "narnat"))


def messages_to_events(
    messages: List[Dict[str, Any]],
    sid: str,
    base_time_ms: int,
    provider: str = "narnat",
    model: str = "narnat",
) -> List[Dict[str, Any]]:
    """把 OpenAI 风格消息列表翻译成 DSH SessionEvent 序列。

    跳过 system 消息（DSH 里是 seed，不进入有序表面）。
    每个事件: {type, seq, time, data}；seq 从 0 递增，time = base_time_ms + i。
    """
    events: List[Dict[str, Any]] = []
    turn = 0
    step = 0
    turn_open = False
    step_open = False
    index = 0

    def emit(etype: str, data: Dict[str, Any], surface_op: Optional[str] = None) -> None:
        event = {"type": etype, "seq": len(events), "time": base_time_ms + len(events), "data": data}
        if surface_op is not None:
            event["surfaceOp"] = surface_op
        events.append(event)

    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            if turn_open:
                emit("turn/end", {"turn": turn, "reason": "completed"})
                turn_open = False
            turn += 1
            step = 0
            emit("turn/start", {"turn": turn})
            turn_open = True
            emit("user/message", {
                "id": _msg_id(sid, index),
                "role": "user",
                "content": [{"type": "text", "text": m.get("content") or ""}],
                "source": {"kind": "user"},
            }, surface_op="append")
            index += 1
            continue
        if role == "assistant":
            if not turn_open:
                turn += 1
                emit("turn/start", {"turn": turn})
                turn_open = True
            step += 1
            if step_open:
                emit("step/end", {"turn": turn, "step": step - 1})
            emit("step/start", {"turn": turn, "step": step})
            step_open = True
            content_blocks: List[Dict[str, Any]] = []
            text = m.get("content")
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                content_blocks.append({
                    "type": "tool-call",
                    "id": tc.get("id") or _msg_id(sid, index),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
            if not content_blocks:
                content_blocks.append({"type": "text", "text": ""})
            emit("assistant/message", {
                "turn": turn,
                "step": step,
                "message": {
                    "id": _msg_id(sid, index),
                    "role": "assistant",
                    "content": content_blocks,
                    "source": {"kind": "model", "provider": provider, "model": model},
                },
            }, surface_op="append")
            index += 1
            continue
        if role == "tool":
            if not turn_open:
                turn += 1
                emit("turn/start", {"turn": turn})
                turn_open = True
            if not step_open:
                step += 1
                emit("step/start", {"turn": turn, "step": step})
                step_open = True
            call_id = m.get("tool_call_id") or ""
            result = m.get("content") or ""
            emit("tool/result", {
                "turn": turn,
                "step": step,
                "message": {
                    "id": _msg_id(sid, index),
                    "role": "user",
                    "content": [{
                        "type": "tool-result",
                        "toolCallId": call_id,
                        "content": [{"type": "text", "text": result}],
                        **({"isError": True} if _is_error_result(result) else {}),
                    }],
                    "source": {"kind": "tool", "callId": call_id},
                },
            }, surface_op="append")
            index += 1
            continue

    if step_open:
        emit("step/end", {"turn": turn, "step": step})
    if turn_open:
        emit("turn/end", {"turn": turn, "reason": "completed"})
    return events


def last_todos(messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """最后一次 TodoWrite 的 todos（DSH TodoItem 形状: {content, status}）。"""
    last: Optional[List[Dict[str, Any]]] = None
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") != "TodoWrite":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            todos = args.get("todos")
            if not isinstance(todos, list):
                continue
            last = [{"content": t.get("content", ""), "status": t.get("status", "pending")}
                    for t in todos if isinstance(t, dict)]
    return last


# ── 会话摘要 ──

def summary_row(
    name: str,
    timestamp: float,
    message_count: int,
    parent: Optional[str] = None,
    running: bool = False,
) -> Dict[str, Any]:
    """Narnat 会话信息 → DSH SessionSummary 行（含 projections）。"""
    sid = session_id(name, parent)
    ts_ms = int(timestamp * 1000)
    blank = message_count == 0
    row: Dict[str, Any] = {
        "sessionId": sid,
        "updatedAt": ts_ms,
        "running": running,
        "blank": blank,
    }
    if parent:
        row["parentSessionId"] = session_id(parent)
    # 列表行的 title / sessionListMetadata 投影（侧栏标题来源）
    row["projections"] = {
        "asOfSeq": max(message_count - 1, -1),
        "values": {
            "title": name,
            "sessionListMetadata": {
                "blank": blank,
                "lastPromptAt": ts_ms if message_count > 0 else None,
            },
        },
    }
    return row


def session_tree_to_summaries(tree: List[Dict[str, Any]], current_sid: str) -> List[Dict[str, Any]]:
    """list_sessions_tree 结果 → 扁平 SessionSummary 列表（根+子，当前会话置 running）。"""
    rows: List[Dict[str, Any]] = []
    for root in tree:
        name = root.get("name") or ""
        rows.append(summary_row(
            name,
            root.get("timestamp", 0),
            root.get("message_count", 0),
            running=session_id(name) == current_sid,
        ))
        for child in root.get("children") or []:
            cname = child.get("name") or ""
            rows.append(summary_row(
                cname,
                child.get("timestamp", 0),
                child.get("message_count", 0),
                parent=name,
                running=session_id(cname, name) == current_sid,
            ))
    rows.sort(key=lambda r: r["updatedAt"], reverse=True)
    return rows


# ── 历史 ──

def history_for(
    messages: List[Dict[str, Any]],
    sid: str,
    base_time_ms: int,
    title: str,
    provider: str,
    model: str,
    blank: bool,
) -> Dict[str, Any]:
    """→ session.history 的 value: {events, hasMore, projections}。"""
    events = messages_to_events(messages, sid, base_time_ms, provider, model)
    todos = last_todos(messages)
    projections = {
        "asOfSeq": events[-1]["seq"] if events else -1,
        "values": {
            "title": title,
            "sessionListMetadata": {
                "blank": blank,
                "lastPromptAt": base_time_ms if messages else None,
            },
            "todos": todos,
        },
    }
    return {
        "events": [{"event": e} for e in events],
        "hasMore": False,
        "projections": projections,
    }


# ── 模型目录 ──

def model_catalog(protocol: str, model: str,
                  thinking_options: Optional[Dict[str, str]] = None,
                  default_effort: Optional[str] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"id": model, "name": model}
    # 思考强度元数据 → DSH 模型选择器里的 effort 下拉（Narnat /thinking 映射）
    if thinking_options:
        efforts = [{"id": eid, "name": label}
                   for eid, label in thinking_options.items() if isinstance(label, str)]
        if efforts:
            entry["reasoning"] = {
                "efforts": efforts,
                **({"defaultEffort": default_effort}
                   if default_effort in thinking_options else {}),
            }
    return {
        "groups": [{
            "id": "narnat",
            "name": "Narnat 配置",
            "models": [entry],
        }],
        "failures": [],
    }


def session_models(protocol: str, model: str,
                   thinking_options: Optional[Dict[str, str]] = None,
                   default_effort: Optional[str] = None) -> Dict[str, Any]:
    return {
        "current": {
            "provider": "narnat",
            "model": model,
            **({"reasoningEffort": default_effort} if default_effort else {}),
        },
        # routable=false 会让客户端封锁输入框（"当前模型不可用，请先选择模型"）；
        # 我们支持运行时切换模型，故为 true
        "routable": True,
        **model_catalog(protocol, model, thinking_options, default_effort),
    }


def host_describe(version: str, cwd: str, protocol: str, model: str,
                  attached_session_ids: List[str]) -> Dict[str, Any]:
    return {
        "version": version,
        "cwd": cwd,
        "provider": protocol,
        "model": model,
        # 注意：schema 要求 attachedSessions 是已挂载会话的【数量】，不是 id 列表
        "attachedSessions": len(attached_session_ids),
        "canOpenPath": False,
    }


# ── 目录浏览（host.listDirectory）──

def list_directory(path: Optional[str], cwd: str) -> Dict[str, Any]:
    target = os.path.abspath(path) if path else os.path.abspath(cwd)
    if not os.path.isdir(target):
        raise FileNotFoundError(target)
    home = os.path.expanduser("~")
    entries: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(target), key=lambda s: s.lower())
    except OSError:
        raise
    for n in names:
        if n.startswith("."):
            continue
        entries.append({
            "name": n,
            "path": os.path.join(target, n),
            "hidden": False,
        })
    # crumbs: home → target 的祖先链
    crumbs: List[Dict[str, Any]] = [{"name": "Home", "path": home, "hidden": False}]
    rel = os.path.relpath(target, home)
    if rel != "." and not rel.startswith(".."):
        acc = home
        for part in rel.split(os.sep):
            if not part:
                continue
            acc = os.path.join(acc, part)
            crumbs.append({"name": part, "path": acc, "hidden": False})
    return {
        "path": target,
        "home": home,
        "crumbs": crumbs,
        "entries": entries,
        "truncated": False,
    }
