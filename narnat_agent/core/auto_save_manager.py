"""自动保存管理器 —— 后台 LLM 命名 + 写磁盘

从 Agent._try_auto_save() / _do_auto_save() / _wait_auto_save() / _auto_save_on_exit() 提取。
算法逻辑原样保留。
"""

import threading
from typing import Optional

from ..config.loader import Config
from .message_list import MessageList
from .session_callbacks import SessionManager
from .summarizer import Summarizer
from ..logger import AgentLogger
from ..output import write as _stdout_write, D, E, R


class AutoSaveManager:
    """自动保存管理器"""

    def __init__(self, config: Config, message_list: MessageList,
                 session_mgr: SessionManager, summarizer: Summarizer,
                 logger: AgentLogger):
        self._config = config
        self._message_list = message_list
        self._mgr = session_mgr
        self._summary = summarizer
        self._logger = logger
        self._auto_save_thread: Optional[threading.Thread] = None

    def try_save(self):
        """启动后台线程做 LLM 命名 + 写磁盘。主线程立即返回。"""
        if not self._config.session.auto_save:
            return
        if self._mgr._auto_save_done:
            return
        from .session_callbacks import NoSession
        if not isinstance(self._mgr.state, NoSession):
            return
        self._mgr._auto_save_done = True

        self._auto_save_thread = threading.Thread(
            target=self._do_save, daemon=True)
        self._auto_save_thread.start()

    def _do_save(self):
        """后台线程：LLM 命名 → 写磁盘。不碰状态切换。"""
        name = self._summary.name_session(self._message_list.view().to_list())
        if not name:
            return
        from ..config.session_store import save_session
        save_session(self._config.paths.narnat_dir, name,
                     self._message_list.view().to_list())
        self._mgr._pending_auto_save_name = name

    def wait(self):
        """等待后台自动保存完成，执行状态切换。幂等调用。"""
        if self._auto_save_thread is not None:
            self._auto_save_thread.join(timeout=5)
            self._auto_save_thread = None
        name = self._mgr._pending_auto_save_name
        if name:
            self._mgr._pending_auto_save_name = None
            self._mgr.switch_state(self._mgr.create_root_state(name))

    def on_exit(self):
        """退出时保存当前会话并清理延迟删除"""
        self.wait()
        if self._mgr.state.session_name():
            self._mgr.on_auto_save()
            saved_name = self._mgr.state.session_name()
            _stdout_write(f"  {D}会话已自动保存: {E}{saved_name}{R}\n")
        self._mgr.cleanup_deletes()
