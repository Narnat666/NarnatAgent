"""
会话状态机 —— 三态模型：NoSession / RootSession / ChildSession

每个状态类封装自己的行为，命令可用性由类型决定：
  NoSession:   /save(诞生) /enter /delete(任意) /show /exit(退出agent)
  RootSession: /save(持久化) /enter /delete(仅删儿子) /explore /show /exit(→NoSession)
  ChildSession: /enter /done /show /exit(→RootSession)

SessionManager 持有共享资源，状态对象通过 manager 引用访问。
"""

import json
import os
from typing import Optional, List, Dict, Any, Callable, Set, Tuple

from ..config.session_store import (
    save_session, load_session, list_sessions, delete_session,
    format_session_list, list_sessions_tree, format_session_tree,
    load_session_meta,
)
from ..config.skill_store import load_skill, list_skill_names

BOUNDARY_MARKER_PREFIX = "━━━ 探索分支开始"


def _format_messages_text(messages: list) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            if content:
                lines.append(f"AI：{content}")
    return "\n".join(lines)


SUMMARY_TASK_TEMPLATE = """以下内容为历史已知背景，不需要重复总结，仅作参考：

{memory}

---

结合上述背景，只总结下面新对话：

{target}

总结要求：
1. 新对话做了哪些方向讨论
2. 新对话最终采用的方案及原因
3. 关键文件变动

要求：实事求是，简洁且抓住核心。"""


class SessionState:
    """状态基类 —— 定义可用命令接口"""

    def available_commands(self) -> Dict[str, str]:
        raise NotImplementedError

    def save(self, name: str) -> str:
        return "当前状态不可用 /save"

    def show(self) -> str:
        raise NotImplementedError

    def enter(self, name: str) -> str:
        raise NotImplementedError

    def delete(self, name: str) -> str:
        return "当前状态不可用 /delete"

    def explore(self, name: str) -> str:
        return "当前状态不可用 /explore"

    def done(self) -> str:
        return "当前状态不可用 /done"

    def exit(self) -> Tuple[str, Optional['SessionState']]:
        """返回 (消息, 新状态)。新状态为 None 表示 agent 应退出。"""
        raise NotImplementedError

    def auto_save(self):
        pass

    def session_name(self) -> Optional[str]:
        return None

    def session_parent(self) -> Optional[str]:
        return None

    def is_child(self) -> bool:
        return False


class NoSession(SessionState):
    """游离态 —— 不在任何会话中，可创建根会话、删除任意会话"""

    def __init__(self, mgr: 'SessionManager'):
        self._mgr = mgr

    def available_commands(self) -> Dict[str, str]:
        return {
            "/clear":    "清理屏幕",
            "/save":     "保存当前会话",
            "/show":     "显示所有会话",
            "/enter":    "进入历史会话",
            "/delete":   "删除会话",
            "/skill":    "加载技能",
            "/thinking": "切换思考强度",
            "/exit":     "退出程序",
        }

    def save(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        msgs = self._mgr.get_messages()
        err = save_session(self._mgr.narnat_dir, name, msgs)
        if err:
            return err
        new_state = self._mgr.create_root_state(name)
        self._mgr.switch_state(new_state)
        return ""

    def show(self) -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        result = format_session_tree(tree, None, None)
        if not result:
            return ""
        if self._mgr.pending_deletes:
            from ..ui.colors import X, R
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def delete(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        if name == "--all":
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                self._mgr.pending_deletes.add((root["name"], None))
                for child in root.get("children", []):
                    self._mgr.pending_deletes.add((child["name"], root["name"]))
            return ""
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        self._mgr.pending_deletes.add((target_name, target_parent))
        if target_parent is None:
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                if root["name"] == target_name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], target_name))
                    break
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:

        return ("", None)


class RootSession(SessionState):
    """根会话 —— 可创建子会话、删除自己的儿子"""

    def __init__(self, mgr: 'SessionManager', name: str):
        self._mgr = mgr
        self._name = name
        self._status: Optional[str] = None
        self._summary: Optional[str] = None
        self._msg_count: int = len(mgr.get_messages())

    def available_commands(self) -> Dict[str, str]:
        return {
            "/clear":    "清理屏幕",
            "/save":     "保存当前会话",
            "/show":     "显示所有会话",
            "/enter":    "进入历史会话",
            "/delete":   "删除子会话",
            "/skill":    "加载技能",
            "/thinking": "切换思考强度",
            "/explore":  "创建探索分支",
            "/exit":     "退出会话",
        }

    def _persist(self):
        msgs = self._mgr.get_messages()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        save_session(self._mgr.narnat_dir, self._name, msgs,
                     status=self._status or "active",
                     summary=self._summary)
        self._msg_count = len(msgs)

    def save(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        if name == self._name:
            self._persist()
        else:
            msgs = self._mgr.get_messages()
            save_session(self._mgr.narnat_dir, name, msgs)
            new_state = self._mgr.create_root_state(name)
            self._mgr.switch_state(new_state)
        return ""

    def show(self) -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        result = format_session_tree(tree, self._name, None)
        if self._mgr.pending_deletes:
            from ..ui.colors import X, R
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        self._persist()
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def delete(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        if name == "--all":
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                if root["name"] == self._name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], self._name))
                    return ""
            return "会话不存在"
        tree = list_sessions_tree(self._mgr.narnat_dir)
        for root in tree:
            if root["name"] == self._name:
                for child in root.get("children", []):
                    if child["name"] == name:
                        self._mgr.pending_deletes.add((name, self._name))
                        return ""
                return f"'{name}' 不是当前会话的子会话"
        return f"会话不存在: {name}"

    def explore(self, name: str) -> str:
        if not name:
            return "错误: 请指定分支名称"
        self._persist()
        msgs = [dict(m) for m in self._mgr.get_messages()]
        parent_msg_count = len(msgs)
        save_session(self._mgr.narnat_dir, name, msgs, parent=self._name,
                     status="new", parent_msg_count=parent_msg_count,
                     last_summarized_at=parent_msg_count)
        new_msgs, err = load_session(self._mgr.narnat_dir, name, parent=self._name)
        if err:
            return err
        self._mgr.replace_messages(new_msgs)
        new_state = self._mgr.create_child_state(name, self._name)
        self._mgr.switch_state(new_state)
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:

        return ("", NoSession(self._mgr))

    def auto_save(self):
        self._persist()

    def session_name(self) -> Optional[str]:
        return self._name

    def session_parent(self) -> Optional[str]:
        return None


class ChildSession(SessionState):
    """子会话 —— 可合并结论回父会话"""

    def __init__(self, mgr: 'SessionManager', name: str, parent: str):
        self._mgr = mgr
        self._name = name
        self._parent = parent
        self._status: str = self._read_meta_field("status", "active")
        self._summary: Optional[str] = self._read_meta_field("summary")
        self._parent_msg_count: int = self._read_meta_field("parent_msg_count") or 0
        self._last_summarized_at: int = (self._read_meta_field("last_summarized_at")
                                         or self._parent_msg_count)
        self._msg_count: int = len(mgr.get_messages())

    def _read_meta_field(self, field: str, default=None):
        meta = load_session_meta(self._mgr.narnat_dir, self._name, parent=self._parent)
        return meta.get(field, default)

    def available_commands(self) -> Dict[str, str]:
        return {
            "/clear":    "清理屏幕",
            "/show":     "显示所有会话",
            "/enter":    "进入历史会话",
            "/skill":    "加载技能",
            "/thinking": "切换思考强度",
            "/done":     "完成探索分支",
            "/exit":     "暂离探索分支",
        }

    def _persist(self):
        msgs = self._mgr.get_messages()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        save_session(self._mgr.narnat_dir, self._name, msgs,
                     parent=self._parent,
                     status=self._status or "active",
                     summary=self._summary,
                     parent_msg_count=self._parent_msg_count,
                     last_summarized_at=self._last_summarized_at)
        self._msg_count = len(msgs)

    def show(self) -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        result = format_session_tree(tree, self._name, self._parent)
        if self._mgr.pending_deletes:
            from ..ui.colors import X, R
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        self._persist()
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def done(self) -> str:
        if self._status == "completed":
            return "该探索分支已完成，不可重复 /done"
        msgs = list(self._mgr.get_messages())
        parent_msg_count = self._parent_msg_count
        if parent_msg_count == 0:
            for i, m in enumerate(msgs):
                if m.get("role") == "system" and BOUNDARY_MARKER_PREFIX in m.get("content", ""):
                    parent_msg_count = i
                    break

        last_summarized_at = self._last_summarized_at
        if last_summarized_at < parent_msg_count:
            last_summarized_at = parent_msg_count

        memory = msgs[:parent_msg_count]
        target = msgs[last_summarized_at:]

        if not target:
            return "没有新的讨论内容需要总结"

        memory_text = _format_messages_text(memory)
        target_text = _format_messages_text(target)
        task_content = SUMMARY_TASK_TEMPLATE.format(memory=memory_text, target=target_text)
        summary_msgs = [{"role": "user", "content": task_content}]

        if self._mgr.summary_anim_start:
            self._mgr.summary_anim_start()
        summary = ""
        if self._mgr.summarize_func:
            summary = self._mgr.summarize_func(summary_msgs, self._mgr.cancel_check)
        if self._mgr.summary_anim_stop:
            self._mgr.summary_anim_stop()
        if not summary:
            return "总结取消或失败"
        parent_msgs, err = load_session(self._mgr.narnat_dir, self._parent)
        if err:
            return f"无法加载父会话: {err}"
        round_num = sum(1 for m in parent_msgs
                        if m.get("role") == "system"
                        and f"子会话 [{self._name}]" in m.get("content", "")) + 1
        round_label = f" (第{round_num}轮)" if round_num > 1 else ""
        parent_msgs.append({"role": "system",
            "content": f"# 子会话 [{self._name}]{round_label} 结论\n\n{summary}"})
        save_session(self._mgr.narnat_dir, self._parent, parent_msgs)

        self._last_summarized_at = len(msgs)
        self._status = "completed"
        self._summary = summary
        child_msgs, _ = load_session(self._mgr.narnat_dir, self._name,
                                      parent=self._parent)
        save_session(self._mgr.narnat_dir, self._name, child_msgs,
                     parent=self._parent, status="completed", summary=summary,
                     parent_msg_count=self._parent_msg_count,
                     last_summarized_at=self._last_summarized_at)

        new_msgs, err = load_session(self._mgr.narnat_dir, self._parent)
        if err:
            return err
        self._mgr.replace_messages(new_msgs)
        new_state = self._mgr.create_root_state(self._parent)
        self._mgr.switch_state(new_state)
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:

        parent_msgs, err = load_session(self._mgr.narnat_dir, self._parent)
        if err:
            return (err, None)
        self._mgr.replace_messages(parent_msgs)
        new_state = self._mgr.create_root_state(self._parent)
        self._mgr.switch_state(new_state)
        return ("", new_state)

    def auto_save(self):
        self._persist()

    def session_name(self) -> Optional[str]:
        return self._name

    def session_parent(self) -> Optional[str]:
        return self._parent

    def is_child(self) -> bool:
        return True


class SessionManager:
    """会话管理器 —— 持有共享资源，管理状态切换和延迟删除"""

    def __init__(self, narnat_dir: str,
                 get_messages_func: Callable[[], List[Dict[str, Any]]],
                 set_messages_func: Callable[[List[Dict[str, Any]]], None],
                 context_manager=None,
                 config_dir: str = "",
                 thinking_effort_getter: Callable[[], str] = None,
                 thinking_effort_setter: Callable[[str], None] = None,
                 thinking_options: dict = None,
                 summarize_func: Callable[[List[Dict[str, Any]], Callable[[], bool]], str] = None,
                 summary_anim_start: Callable[[], None] = None,
                 summary_anim_stop: Callable[[], None] = None,
                 cancel_check: Callable[[], bool] = None):
        self.narnat_dir = narnat_dir
        self._get_messages = get_messages_func
        self._set_messages = set_messages_func
        self._context = context_manager
        self._config_dir = config_dir
        self._get_thinking_effort = thinking_effort_getter
        self._set_thinking_effort = thinking_effort_setter
        self._thinking_options = thinking_options or {"high": "高", "max": "全开"}
        self.summarize_func = summarize_func
        self.summary_anim_start = summary_anim_start
        self.summary_anim_stop = summary_anim_stop
        self.cancel_check = cancel_check or (lambda: False)
        self.pending_deletes: Set[Tuple[str, Optional[str]]] = set()
        self._state: SessionState = NoSession(self)

    def get_messages(self) -> List[Dict[str, Any]]:
        return self._get_messages()

    def replace_messages(self, new_msgs: List[Dict[str, Any]]):
        current = self._get_messages()
        current.clear()
        current.extend(new_msgs)
        if self._context is not None:
            self._context.sync_from_messages(current)

    def switch_state(self, new_state: SessionState):
        self._state = new_state

    def create_root_state(self, name: str) -> RootSession:
        return RootSession(self, name)

    def create_child_state(self, name: str, parent: str) -> ChildSession:
        return ChildSession(self, name, parent)

    def load_child_with_boundary(self, name: str, parent: str) -> Tuple[List[Dict[str, Any]], str]:
        new_msgs, err = load_session(self.narnat_dir, name, parent=parent)
        return new_msgs, err


    def apply_delete_marks(self, tree: List[Dict[str, Any]]):
        for root in tree:
            if (root["name"], None) in self.pending_deletes:
                root["_delete_marked"] = True
            for child in root.get("children", []):
                if (child["name"], root["name"]) in self.pending_deletes:
                    child["_delete_marked"] = True

    def cleanup_deletes(self):
        children = [(n, p) for n, p in self.pending_deletes if p is not None]
        roots = [(n, p) for n, p in self.pending_deletes if p is None]
        for name, parent in children:
            delete_session(self.narnat_dir, name, parent=parent)
        for name, parent in roots:
            delete_session(self.narnat_dir, name, parent=parent)
        self.pending_deletes.clear()

    def resolve_session_name(self, name: str) -> Tuple[Optional[str], Optional[str], str]:
        """解析会话名。返回 (target_name, target_parent, error_msg)"""
        if "/" in name:
            parts = name.split("/", 1)
            return parts[1], parts[0], ""
        tree = list_sessions_tree(self.narnat_dir)
        matches = []
        for root in tree:
            if root["name"] == name:
                matches.append((root["name"], None))
            for child in root.get("children", []):
                if child["name"] == name:
                    matches.append((child["name"], root["name"]))
        if len(matches) == 1:
            return matches[0][0], matches[0][1], ""
        if len(matches) > 1:
            paths = "\n".join(
                f"      {p}/{c}" if c else f"      {p}" for p, c in matches
            )
            return None, None, f"'{name}' 有多个，请用完整路径指定：\n{paths}"
        return None, None, f"会话不存在: {name}"

    @property
    def state(self) -> SessionState:
        return self._state

    # ── 代理方法，供外部直接调用 ──

    def on_save(self, name: str) -> str:
        return self._state.save(name)

    def on_show(self) -> str:
        return self._state.show()

    def on_enter(self, name: str) -> str:
        return self._state.enter(name)

    def on_delete(self, name: str) -> str:
        return self._state.delete(name)

    def on_explore(self, name: str) -> str:
        return self._state.explore(name)

    def on_done(self) -> str:
        return self._state.done()

    def on_exit(self) -> str:
        msg, new_state = self._state.exit()
        if new_state is not None:
            self._state = new_state
        return msg

    def on_auto_save(self):
        self._state.auto_save()

    def on_skill(self, name: str) -> str:
        content, err = load_skill(self.narnat_dir, name)
        if err:
            return err
        self._get_messages().append({"role": "system", "content": content})
        return ""

    def on_thinking(self, effort: str) -> str:
        options = self._thinking_options
        if not effort:
            current = (self._get_thinking_effort or (lambda: "high"))()
            current_label = options.get(current, current)
            return f"当前思考强度: {current_label}"
        effort_lower = effort.strip().lower()
        if effort_lower not in options:
            available = " / ".join(options.keys())
            return f"无效值: {effort_lower}（可用: {available}）"
        if self._set_thinking_effort:
            self._set_thinking_effort(effort_lower)
        if self._config_dir:
            config_path = os.path.join(self._config_dir, "narnat.json")
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["思考强度"] = effort_lower
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
        return f"思考强度已切换为: {options[effort_lower]}"

    def on_list_thinking_options(self) -> list:
        return list(self._thinking_options.keys())

    def on_list_names(self) -> list:
        tree = list_sessions_tree(self.narnat_dir)
        names = []
        for root in tree:
            names.append(root["name"])
            for child in root.get("children", []):
                names.append(f"{root['name']}/{child['name']}")
        return names

    def on_list_names_tree(self) -> list:
        return self.on_list_names()

    def on_list_skill_names(self) -> list:
        return list_skill_names(self.narnat_dir)

    def is_child_session(self) -> bool:
        return self._state.is_child()

    def has_active_session(self) -> bool:
        return self._state.session_name() is not None

    def should_exit_agent(self) -> bool:
        return isinstance(self._state, NoSession)

    def available_commands(self) -> Dict[str, str]:
        return self._state.available_commands()
