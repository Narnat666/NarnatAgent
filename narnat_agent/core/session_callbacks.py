"""
会话回调实现 —— /save /load /skill 等命令的具体逻辑

从agent.py中提取，Agent只负责组装。
"""

from typing import Optional, List, Dict, Any, Callable

from ..config.session_store import save_session, load_session, list_sessions, delete_session, format_session_list
from ..config.skill_store import load_skill, list_skill_names
from ..ui.session_commands import SessionCallbacks


class NarnatSessionCallbacks(SessionCallbacks):
    """会话命令回调实现"""

    def __init__(self, narnat_dir: str, get_messages_func: Callable[[], List[Dict[str, Any]]],
                 context_manager=None):
        self._narnat_dir = narnat_dir
        self._get_messages = get_messages_func
        self._active_name: Optional[str] = None
        self._context = context_manager

    def on_save(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        msgs = self._get_messages()
        err = save_session(self._narnat_dir, name, msgs)
        if err:
            return err
        self._active_name = name
        return ""

    def on_show(self) -> str:
        sessions = list_sessions(self._narnat_dir)
        return format_session_list(sessions)

    def on_enter(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        msgs, err = load_session(self._narnat_dir, name)
        if err:
            return err
        current = self._get_messages()
        current.clear()
        current.extend(msgs)
        self._active_name = name
        # 同步轮次计数，避免压缩机制失效
        if self._context is not None:
            self._context.sync_from_messages(current)
        return ""

    def on_delete(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        err = delete_session(self._narnat_dir, name)
        if err:
            return err
        if self._active_name == name:
            self._active_name = None
        return ""

    def on_list_names(self) -> list:
        return [s["name"] for s in list_sessions(self._narnat_dir)]

    def on_skill(self, name: str) -> str:
        content, err = load_skill(self._narnat_dir, name)
        if err:
            return err
        self._get_messages().append({"role": "system", "content": content})
        return ""

    def on_exit(self) -> str:
        if not self._active_name:
            return ""
        msgs = self._get_messages()
        err = save_session(self._narnat_dir, self._active_name, msgs)
        if err:
            return ""
        return self._active_name

    def on_list_skill_names(self) -> list:
        return list_skill_names(self._narnat_dir)
