"""
会话命令实现 —— /save /show /enter /delete /clear
"""

from typing import List, Dict, Any

from ..config.session_store import (
    save_session, load_session, list_sessions, delete_session,
    format_session_list,
)


class SessionManager:
    """会话管理器，处理/save /show /enter /delete命令"""

    def __init__(self, narnat_dir: str, logger=None):
        self._narnat_dir = narnat_dir
        self._logger = logger
        self._current_messages: List[Dict[str, Any]] = []

    def set_messages(self, messages: List[Dict[str, Any]]):
        """设置当前会话消息（由agent调用）"""
        self._current_messages = messages

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取当前会话消息"""
        return self._current_messages

    def save(self, name: str) -> str:
        """保存当前会话"""
        if not name.strip():
            return "名称不能为空"
        err = save_session(self._narnat_dir, name, self._current_messages)
        if err:
            return err
        if self._logger:
            self._logger.info("commands.session", f"保存会话: {name}")
        return ""

    def show(self) -> str:
        """列出所有已保存会话"""
        sessions = list_sessions(self._narnat_dir)
        return format_session_list(sessions)

    def enter(self, name: str) -> tuple:
        """
        进入指定历史会话。

        Returns:
            (messages, error) - messages非空且error为空串表示成功
        """
        messages, err = load_session(self._narnat_dir, name)
        if err:
            return [], err
        self._current_messages = messages
        if self._logger:
            self._logger.info("commands.session", f"进入会话: {name}")
        return messages, ""

    def delete(self, name: str) -> str:
        """删除指定会话"""
        err = delete_session(self._narnat_dir, name)
        if not err and self._logger:
            self._logger.info("commands.session", f"删除会话: {name}")
        return err
