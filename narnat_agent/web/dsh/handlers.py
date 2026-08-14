"""DSH 前端协议端点实现 —— 把 Narnat Agent 数据翻译成 DSH 协议

每个 handler: (bridge, payload) -> value dict；抛 HandledError 时转为
{ok:false, error:{code,message,details}}。payload 为业务参数本体
（apiproxy 的 UNARY_ROUTES 直接校验 message.payload）。
"""

import json
import os
import uuid
from typing import Any, Callable, Dict, Optional

from ...config import session_store
from . import protocol, translate


class HandledError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _cfg(bridge, attr: str, default: Any = None) -> Any:
    return getattr(bridge._config.ai, attr, default)


# ── session.* ──

def session_list(bridge, payload: Dict[str, Any]):
    return {"items": bridge.session_list_rows()}


def session_search(bridge, payload: Dict[str, Any]):
    query = str(payload.get("query") or "")
    items = []
    for sid, (name, parent) in bridge._sid_index.items():
        hay = name or ""
        if query.lower() in hay.lower():
            items.append({"sessionId": sid, "snippet": hay[:300]})
    items = items[:20]
    return {"items": items, "hasMore": False}


def session_create(bridge, payload: Dict[str, Any]):
    # 新会话 = 退出当前会话并重置消息列表
    try:
        bridge._mgr.on_exit()
    except Exception:
        pass
    system_prompt = getattr(bridge._config, "system_prompt", "") or ""
    try:
        bridge._mgr.replace_messages([{"role": "system", "content": system_prompt}])
    except Exception:
        pass
    sid = translate.LIVE_SESSION_ID
    bridge.hub.attach_session(sid, 0, "未保存会话")
    bridge.hub.reset_seq(sid, 0)
    bridge.refresh_sessions(attach=True)
    return {"sessionId": sid}


def session_history(bridge, payload: Dict[str, Any]):
    sid = str(payload.get("sessionId") or "")
    messages, err = bridge.load_session_messages(sid)
    if err:
        raise HandledError("session-not-found", err, {"sessionId": sid})
    title = bridge.title_of(sid) or ("未保存会话" if sid == bridge.current_sid() else sid)
    base_ms = translate._now_ms()
    if sid != bridge.current_sid():
        meta = {}
        target = bridge.find_session(sid)
        if target:
            try:
                meta = session_store.load_session_meta(bridge._mgr.narnat_dir, *target)
            except Exception:
                meta = {}
        base_ms = int(float(meta.get("timestamp") or 0) * 1000) or base_ms
    return translate.history_for(
        messages,
        sid,
        base_ms,
        title,
        _cfg(bridge, "protocol", "narnat"),
        _cfg(bridge, "model", "narnat"),
        blank=len(messages) <= 1,
    )


def session_models(bridge, payload: Dict[str, Any]):
    return translate.session_models(
        _cfg(bridge, "protocol", "narnat"),
        _cfg(bridge, "model", "narnat"),
        thinking_options=_cfg(bridge, "thinking_options") or {},
        default_effort=_cfg(bridge, "thinking_effort"),
    )


def session_select_model(bridge, payload: Dict[str, Any]):
    model = str(payload.get("model") or "")
    effort = payload.get("reasoningEffort")
    if not model:
        raise HandledError("bad-request", "model required", {})
    try:
        if model:
            bridge._config.ai.model = model
        # 思考强度切换 → 写入运行时配置（/thinking 同款语义）
        if effort and isinstance(effort, str):
            options = getattr(bridge._config.ai, "thinking_options", {}) or {}
            if effort in options or effort in ("high", "max"):
                bridge._config.ai.thinking_effort = effort
    except Exception:
        pass
    return {"selected": {
        "provider": "narnat",
        "model": model,
        **({"reasoningEffort": effort} if effort else {}),
    }}


def _rename_saved_session(bridge, sid: str, new_title: str):
    target = bridge.find_session(sid)
    if target is None:
        raise HandledError("session-not-found", "会话不存在", {"sessionId": sid})
    name, parent = target
    title = new_title.strip()
    if not title or title in ("未保存会话",):
        raise HandledError("title-invalid", "标题无效", {"sessionId": sid})
    sdir = os.path.join(bridge._mgr.narnat_dir, "data", "sessions")
    safe_new = session_store._safe_filename(title)
    safe_old = session_store._safe_filename(name)
    base = os.path.join(sdir, safe_new if parent is None else session_store._safe_filename(parent))
    old_path = os.path.join(sdir, safe_old + ".json") if parent is None else \
        os.path.join(sdir, session_store._safe_filename(parent), safe_old + ".json")
    if not os.path.isfile(old_path):
        raise HandledError("session-not-found", "会话文件不存在", {"sessionId": sid})
    new_path = os.path.join(sdir, safe_new + ".json") if parent is None else \
        os.path.join(sdir, session_store._safe_filename(parent), safe_new + ".json")
    if os.path.abspath(old_path) != os.path.abspath(new_path):
        if os.path.exists(new_path):
            raise HandledError("title-invalid", "同名会话已存在", {"sessionId": sid})
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.replace(old_path, new_path)
    try:
        with open(new_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["name"] = title
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise HandledError("title-invalid", f"重命名失败: {e}", {"sessionId": sid})
    bridge.refresh_sessions(attach=True)
    new_sid = translate.session_id(title, parent)
    return new_sid


def session_rename(bridge, payload: Dict[str, Any]):
    sid = str(payload.get("sessionId") or "")
    title = str(payload.get("title") or "")
    if sid == bridge.current_sid() and not bridge._mgr.has_active_session():
        raise HandledError("title-invalid", "未保存会话不可重命名", {"sessionId": sid})
    new_sid = _rename_saved_session(bridge, sid, title)
    return {"title": title, "seq": bridge.hub.last_seq(new_sid)}


def session_fork(bridge, payload: Dict[str, Any]):
    sid = str(payload.get("sessionId") or "")
    if sid != bridge.current_sid():
        raise HandledError("fork-unavailable", "只能分支当前会话", {"sessionId": sid})
    import time as _t
    name = f"分支-{_t.strftime('%m%d-%H%M')}"
    try:
        err = bridge._mgr.on_explore(name)
    except Exception as e:
        raise HandledError("fork-unavailable", str(e), {"sessionId": sid})
    if err:
        raise HandledError("fork-unavailable", err, {"sessionId": sid})
    bridge.refresh_sessions(attach=True)
    child_sid = bridge.current_sid()
    return {"sessionId": child_sid}


def session_prompt(bridge, payload: Dict[str, Any]):
    sid = str(payload.get("sessionId") or "")
    content = payload.get("content") or []
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image":
            raise HandledError("attachment-error", "Narnat 不支持图片附件", {})
    text = "\n".join(texts)
    if not text.strip():
        raise HandledError("bad-request", "empty prompt", {})
    if sid != bridge.current_sid():
        err = bridge.switch_to(sid)
        if err:
            raise HandledError("session-not-found", err, {"sessionId": sid})
    bridge.submit_input(text)
    return {"accepted": True}


def session_attachment(bridge, payload: Dict[str, Any]):
    raise HandledError("attachment-error", "Narnat 无附件存储", {})


def session_update_queue(bridge, payload: Dict[str, Any]):
    item_id = str(payload.get("itemId") or "")
    raise HandledError("queue-item-not-found", "队列为空", {"itemId": item_id})


def session_cancel(bridge, payload: Dict[str, Any]):
    bridge.cancel_current()
    return {"accepted": True}


# ── subagent.*（Narnat 无子代理 → 空/错误） ──

def subagent_list(bridge, payload: Dict[str, Any]):
    return {"entries": [], "parentAvailable": True}


def subagent_history(bridge, payload: Dict[str, Any]):
    raise HandledError("subagent-not-found", "Narnat 无子代理", {
        "parentSessionId": str(payload.get("parentSessionId") or ""),
        "childSessionId": str(payload.get("childSessionId") or ""),
    })


def subagent_prompt(bridge, payload: Dict[str, Any]):
    raise HandledError("subagent-not-found", "Narnat 无子代理", {
        "parentSessionId": str(payload.get("parentSessionId") or ""),
        "childSessionId": str(payload.get("childSessionId") or ""),
    })


def subagent_interrupt(bridge, payload: Dict[str, Any]):
    raise HandledError("subagent-not-found", "Narnat 无子代理", {
        "parentSessionId": str(payload.get("parentSessionId") or ""),
        "childSessionId": str(payload.get("childSessionId") or ""),
    })


# ── host.* ──

def host_describe(bridge, payload: Dict[str, Any]):
    return translate.host_describe(
        "narnat-15.5.1",
        bridge.project_root(),
        _cfg(bridge, "protocol", "narnat"),
        _cfg(bridge, "model", "narnat"),
        [bridge.current_sid()],
    )


def host_pick_directory(bridge, payload: Dict[str, Any]):
    return {"path": None}


def host_list_directory(bridge, payload: Dict[str, Any]):
    try:
        return translate.list_directory(payload.get("path"), bridge.project_root())
    except FileNotFoundError:
        raise HandledError("directory-unreadable", "目录不可读", {"path": str(payload.get("path") or "")})
    except OSError as e:
        raise HandledError("directory-unreadable", str(e), {"path": str(payload.get("path") or "")})


def host_create_directory(bridge, payload: Dict[str, Any]):
    path = str(payload.get("path") or "")
    name = str(payload.get("name") or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HandledError("directory-create-failed", "非法目录名", {"path": path})
    target = os.path.join(path, name)
    if os.path.exists(target):
        raise HandledError("directory-exists", "目录已存在", {"path": target})
    try:
        os.makedirs(target)
    except OSError as e:
        raise HandledError("directory-create-failed", str(e), {"path": target})
    return {"path": target}


def host_open_path(bridge, payload: Dict[str, Any]):
    path = str(payload.get("path") or "")
    try:
        os.startfile(path)  # Windows
    except Exception:
        raise HandledError("internal", "无法打开路径", {})
    return {"opened": True}


# ── workspace.*（虚拟"Narnat 工作区"承载全部会话）──

def workspace_list(bridge, payload: Dict[str, Any]):
    return {"items": [bridge.workspace_view()], "archivedSessionIds": []}


def workspace_create(bridge, payload: Dict[str, Any]):
    # 目录选择器可能把当前目录提交过来；直接返回既有虚拟工作区
    return {"workspace": bridge.workspace_view(), "created": False}


def workspace_rename(bridge, payload: Dict[str, Any]):
    if str(payload.get("workspaceId") or "") != translate.WORKSPACE_ID:
        raise HandledError("workspace-not-found", "Narnat 无此工作区", {"workspaceId": str(payload.get("workspaceId") or "")})
    return {"workspace": bridge.workspace_view()}


def workspace_delete(bridge, payload: Dict[str, Any]):
    raise HandledError("workspace-not-found", "Narnat 工作区不可删除", {"workspaceId": str(payload.get("workspaceId") or "")})


def workspace_insert_before(bridge, payload: Dict[str, Any]):
    return {"workspaceIds": [translate.WORKSPACE_ID]}


def workspace_insert_session_before(bridge, payload: Dict[str, Any]):
    # 单工作区内排序：接受为无操作
    if str(payload.get("workspaceId") or "") != translate.WORKSPACE_ID:
        raise HandledError("workspace-not-found", "Narnat 无此工作区", {"workspaceId": str(payload.get("workspaceId") or "")})
    return {"workspace": bridge.workspace_view()}


def workspace_archive_session(bridge, payload: Dict[str, Any]):
    raise HandledError("session-not-found", "Narnat 无归档", {"sessionId": str(payload.get("sessionId") or "")})


# ── skill.list ──

def skill_list(bridge, payload: Dict[str, Any]):
    skills = []
    try:
        from ...config.skill_store import list_skill_names
        for name in list_skill_names(bridge._mgr.narnat_dir):
            skills.append({"name": name, "description": "", "modelInvocable": True})
    except Exception:
        pass
    return {"skills": skills}


# ── agentPreset.*（Narnat 无预设 → 空/错误） ──

def agent_preset_list(bridge, payload: Dict[str, Any]):
    return {"presets": [], "authorable": False, "hasDocument": False}


def _preset_not_found(payload):
    agent = str(payload.get("agentPreset") or "")
    raise HandledError("agent-preset-not-found", "Narnat 无 agent 预设", {"agentPreset": agent, "available": []})


def agent_preset_select(bridge, payload): _preset_not_found(payload)
def agent_preset_read(bridge, payload): _preset_not_found(payload)
def agent_preset_copy(bridge, payload): _preset_not_found(payload)
def agent_preset_open_document(bridge, payload): _preset_not_found(payload)
def agent_preset_remove(bridge, payload): _preset_not_found(payload)


# ── goal.*（Narnat 无目标模块 → 内部错误；goal 投影恒空，UI 不展示） ──

def _goal_unsupported(payload):
    raise HandledError("internal", "Narnat 无目标(goal)模块", {})


def goal_create(bridge, payload): _goal_unsupported(payload)
def goal_edit(bridge, payload): _goal_unsupported(payload)
def goal_pause(bridge, payload): _goal_unsupported(payload)
def goal_resume(bridge, payload): _goal_unsupported(payload)
def goal_complete(bridge, payload): _goal_unsupported(payload)
def goal_clear(bridge, payload): _goal_unsupported(payload)


# ── settings.* ──

def settings_describe(bridge, payload: Dict[str, Any]):
    return {"writable": True, "hasDocument": False, "namespaces": bridge.settings_namespaces()}


def settings_open_document(bridge, payload: Dict[str, Any]):
    return {"opened": True}


def settings_update(bridge, payload):
    return bridge.settings_update(
        str(payload.get("ns") or ""),
        payload.get("patch") or {},
        expected_revision=payload.get("expectedRevision"),
    )


def settings_replace(bridge, payload):
    return bridge.settings_replace(
        str(payload.get("ns") or ""),
        payload.get("section") or {},
        expected_revision=payload.get("expectedRevision"),
    )


def settings_mutate(bridge, payload):
    return bridge.settings_mutate(
        str(payload.get("ns") or ""),
        payload.get("ops") or [],
        expected_revision=payload.get("expectedRevision"),
    )


# ── credentials.* ──

def credentials_describe(bridge, payload: Dict[str, Any]):
    return {"credentials": {}}


def credentials_set(bridge, payload):
    raise HandledError("credential-rejected", "Narnat 凭据在 .narnat/config/narnat.json 配置", {"ref": str(payload.get("ref") or "")})


def credentials_unset(bridge, payload):
    raise HandledError("credential-rejected", "Narnat 凭据在 .narnat/config/narnat.json 配置", {"ref": str(payload.get("ref") or "")})


# ── llm.* ──

def llm_providers(bridge, payload: Dict[str, Any]):
    return {"providers": [{
        "provider": "narnat",
        "displayName": "Narnat 模型配置",
        "settingsNs": "narnat",
        "settingsPath": [],
        "active": True,
    }]}


def llm_models(bridge, payload: Dict[str, Any]):
    return translate.model_catalog(
        _cfg(bridge, "protocol", "narnat"),
        _cfg(bridge, "model", "narnat"),
        thinking_options=_cfg(bridge, "thinking_options") or {},
        default_effort=_cfg(bridge, "thinking_effort"),
    )


def llm_discover_models(bridge, payload: Dict[str, Any]):
    raise HandledError("model-discovery-failed", "Narnat 无模型发现", {"settingsNs": str(payload.get("settingsNs") or "")})


# ── typert remote（两段式端点，payload 为 {args:{...}}）──

_COMMAND_DESCRIPTIONS = {
    "clear": "清屏",
    "save": "保存当前会话",
    "ls": "列出会话",
    "cd": "进入已保存会话",
    "rm": "标记删除会话",
    "skill": "加载技能",
    "thinking": "调整思考强度",
    "explore": "创建探索分支",
    "done": "合并探索分支结论",
    "exit": "退出会话",
}


def remote_commands_list(bridge, args: Dict[str, Any]):
    """commands/list：把 Narnat 斜杠命令映射为 DSH 命令描述符。"""
    try:
        available = bridge._mgr.available_commands() or []
    except Exception:
        available = []
    names = set(str(c).lstrip("/") for c in available)
    names.add("clear")
    commands = []
    for name in sorted(names):
        commands.append({
            "name": name,
            "description": _COMMAND_DESCRIPTIONS.get(name, ""),
        })
    return commands


def remote_commands_execute(bridge, args: Dict[str, Any]):
    """commands/execute：把命令行走输入队列（Agent 主循环内处理斜杠命令）。"""
    line = str(args.get("line") or "")
    if not line.strip():
        raise HandledError("bad-request", "empty command line", {})
    bridge.submit_input(line if line.startswith("/") else "/" + line)
    return {"commandId": str(uuid.uuid4()), "result": {"kind": "success"}}


REMOTE_HANDLERS: Dict[str, Callable] = {
    "commands/list": remote_commands_list,
    "commands/execute": remote_commands_execute,
}


# ── 注册表 ──

HANDLERS: Dict[str, Callable] = {
    "session.list": session_list,
    "session.search": session_search,
    "session.create": session_create,
    "session.history": session_history,
    "session.models": session_models,
    "session.selectModel": session_select_model,
    "session.rename": session_rename,
    "session.fork": session_fork,
    "session.prompt": session_prompt,
    "session.attachment": session_attachment,
    "session.updateQueue": session_update_queue,
    "session.cancel": session_cancel,
    "subagent.list": subagent_list,
    "subagent.history": subagent_history,
    "subagent.prompt": subagent_prompt,
    "subagent.interrupt": subagent_interrupt,
    "host.describe": host_describe,
    "host.pickDirectory": host_pick_directory,
    "host.listDirectory": host_list_directory,
    "host.createDirectory": host_create_directory,
    "host.openPath": host_open_path,
    "workspace.list": workspace_list,
    "workspace.create": workspace_create,
    "workspace.rename": workspace_rename,
    "workspace.delete": workspace_delete,
    "workspace.insertBefore": workspace_insert_before,
    "workspace.insertSessionBefore": workspace_insert_session_before,
    "workspace.archiveSession": workspace_archive_session,
    "skill.list": skill_list,
    "agentPreset.list": agent_preset_list,
    "agentPreset.select": agent_preset_select,
    "agentPreset.read": agent_preset_read,
    "agentPreset.copy": agent_preset_copy,
    "agentPreset.openDocument": agent_preset_open_document,
    "agentPreset.remove": agent_preset_remove,
    "goal.create": goal_create,
    "goal.edit": goal_edit,
    "goal.pause": goal_pause,
    "goal.resume": goal_resume,
    "goal.complete": goal_complete,
    "goal.clear": goal_clear,
    "settings.describe": settings_describe,
    "settings.openDocument": settings_open_document,
    "settings.update": settings_update,
    "settings.replace": settings_replace,
    "settings.mutate": settings_mutate,
    "credentials.describe": credentials_describe,
    "credentials.set": credentials_set,
    "credentials.unset": credentials_unset,
    "llm.providers": llm_providers,
    "llm.models": llm_models,
    "llm.discoverModels": llm_discover_models,
}


def dispatch(bridge, method: str, payload: Any):
    """执行一个 RPC 方法，返回 protocol 的 result dict。"""
    handler = HANDLERS.get(method)
    if handler is not None:
        if not isinstance(payload, dict):
            payload = {}
        try:
            value = handler(bridge, payload)
            return protocol.ok(value)
        except HandledError as e:
            return protocol.error(e.code, e.message, e.details)
        except Exception as e:
            return protocol.error("internal", f"{method}: {e}", {})
    remote = REMOTE_HANDLERS.get(method)
    if remote is not None:
        # typert remote：payload = {args: {...}}
        args = (payload or {}).get("args", {}) if isinstance(payload, dict) else {}
        if not isinstance(args, dict):
            args = {}
        try:
            value = remote(bridge, args)
            return protocol.ok(value)
        except HandledError as e:
            return protocol.error(e.code, e.message, e.details)
        except Exception as e:
            return protocol.error("internal", f"{method}: {e}", {})
    # 其它 typert remote（goals/dynamic/pluginInventory/messageFeedback）与未知端点
    return protocol.error("internal", f"端点不可用: {method}", {})
