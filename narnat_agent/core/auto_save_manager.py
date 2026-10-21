"""自动保存管理器 —— 后台 LLM 命名 + 写磁盘

从 Agent._try_auto_save() / _do_auto_save() / _wait_auto_save() / _auto_save_on_exit() 提取。
算法逻辑原样保留。
"""

import sys
import threading
from typing import Callable, Optional

from ..config.loader import Config
from .message_list import MessageList
from .session_callbacks import SessionManager, messages_fingerprint, readable_save_error
from .stats import StatsTracker
from .summarizer import Summarizer
from ..logger import AgentLogger
from ..output import write as _stdout_write, D, E, R


def _has_user_message(msgs) -> bool:
    """列表里是否有用户提问（与 carry_over 的"没什么可存"判据同一口径）"""
    return any(m.get("role") == "user" for m in (msgs or []))


class AutoSaveManager:
    """自动保存管理器"""

    def __init__(self, config: Config, message_list: MessageList,
                 session_mgr: SessionManager, summarizer: Summarizer,
                 stats: StatsTracker, logger: AgentLogger):
        self._config = config
        self._message_list = message_list
        self._mgr = session_mgr
        self._summary = summarizer
        self._stats = stats
        self._logger = logger
        self._auto_save_thread: Optional[threading.Thread] = None
        # 通知钩子（rpc 层注入；core 不 import rpc）
        self._on_saved: Optional[Callable[[str], None]] = None
        self._on_switched: Optional[Callable[[str], None]] = None
        # 状态切换闸门：同一份自动保存只切一次状态、只通知一次
        self._switch_lock = threading.Lock()

    def set_hooks(self, on_saved: Optional[Callable[[str], None]] = None,
                  on_switched: Optional[Callable[[str], None]] = None) -> None:
        """注入「已落盘」/「已切状态」回调。

        on_saved(name)：写盘成功、状态尚未切换时触发。
        on_switched(name)：会话状态切到 RootSession(name) 后触发，仅一次。
        """
        self._on_saved = on_saved
        self._on_switched = on_switched

    def try_save(self):
        """启动后台线程做 LLM 命名 + 写磁盘。主线程立即返回。

        Token门槛：服务器返回的输入token量（prompt_tokens，含全部历史）
        大于 auto_save_tokens 阈值后才自动保存，避免用户随口聊几句就生成命名会话；
        阈值0 = 无门槛，用户输入即自动保存。
        """
        if not self._config.session.auto_save:
            return
        if self._mgr._auto_save_done:
            return
        from .session_callbacks import NoSession
        if not isinstance(self._mgr.state, NoSession):
            return
        threshold = self._config.session.auto_save_tokens
        if threshold > 0 and self._stats.input_tokens <= threshold:
            return
        self._mgr._auto_save_done = True

        self._auto_save_thread = threading.Thread(
            target=self._do_save, daemon=True)
        self._auto_save_thread.start()

    def _do_save(self):
        """后台线程：LLM 命名 → 写磁盘。不碰状态切换。"""
        # 完整历史（归档 + 当前）：压缩替换的是「发给模型的上下文」，自动保存
        # 落盘的必须是用户看得见的全量历史，否则压缩后自动保存也会丢原文。
        # 命名与指纹比较同样基于这份内容（与 _persist / carry_over 同一口径）。
        msgs0 = self._message_list.full_history()
        if not _has_user_message(msgs0):
            return   # 没有对话内容（只剩 system prompt）：无需命名
        fingerprint0 = messages_fingerprint(msgs0)
        name = self._summary.name_session(msgs0)
        if not name:
            return
        msgs = self._message_list.full_history()
        # 命名期间 MessageList 可能被整体换掉（都要等 LLM 回复，窗口以秒计）：
        #   - 用户点了「新建对话」→ 清成只剩 system prompt；
        #   - 用户 session/switch / /cd 到别的会话 → 换成那份会话的内容。
        # 后一种此前没判：本线程拿「别的会话的内容」配「给上一段起的新名字」写盘，
        # 磁盘上就凭空多出一份与目标会话 messages 逐字相同的副本（实测切会话后
        # 多出 `确认收到指令.json`，messages 与目标会话 sha256 全等）。
        # 判据用全仓唯一的 messages_fingerprint，不再另写比较逻辑。
        if not self._same_segment(msgs0, fingerprint0, msgs):
            return
        fingerprint = messages_fingerprint(msgs)
        # 「新建对话」可能已经等不及这条线程，自己用快速名把这段存下来了
        # （见 SessionManager.on_new → NoSession.carry_over）。
        # 那就别再写一份，否则同一个对话会出现两条记录。
        # 判据是**内容指纹相同**，不是"carry_over 发生过"：布尔标记会跨段泄漏，
        # carry_over 之后新一段的内容明明不同，却被误判成已存过而永不落盘。
        if self._take_carried_over(fingerprint):
            return
        from ..config.session_store import save_session
        from .session_callbacks import (_check_reserved_session_name,
                                        _report_conflict_notice, unique_session_name)
        err = _check_reserved_session_name(name)
        out = {}
        if not err:
            # 与历史会话重名：手动 /save 会拒绝并让用户换名，自动保存无人可问——
            # 追加序号另存为新会话，绝不覆盖既有会话（与 carry_over 共用同一份兜底）
            name = unique_session_name(self._config.paths.narnat_dir, name)
            # expected_signature=""：这段内容本该落到一个还不存在的文件。
            # save_session 用 O_EXCL 原子占住这个名字——两个实例给同一段对话
            # 起了同名时（同一个开头 → 同一个 LLM 标题，实测 1/3 概率撞车），
            # 后到的一方不会覆盖先到的，而是自动另存为 "X (2)" 并回报实际落点。
            err = save_session(self._config.paths.narnat_dir, name, msgs,
                               expected_signature="", result=out)
            actual = out.get("name")
            if actual and actual != name:
                name = actual
                _report_conflict_notice(name)
        if err:
            self._logger.warning("core.auto_save", f"自动保存失败: {err}")
            _stdout_write(f"  {D}⚠ 自动保存失败: {readable_save_error(err)}{R}\n")
            return   # 不设 pending：不切换状态、不误报已保存
        # 记下「这份内容已经在磁盘上」：退出兜底据此跳过重复保存
        self._mgr._last_saved_fingerprint = fingerprint
        if self._take_carried_over(fingerprint):
            # 写盘期间 on_new 的快速保存抢先落盘了同一份内容 → 本份作废
            return
        self._mgr._pending_auto_save_name = name
        # 落盘成功即通知界面一次（此刻状态尚未切换），再切状态、再通知一次。
        # 不等 wait()：用户不点「新建」时 wait 永远不会被调用，界面就一直停在
        # 「未保存」，后续轮次的 on_auto_save 也写不回这条会话。
        self._fire(self._on_saved, name)
        self._switch_pending()

    @staticmethod
    def _same_segment(msgs0, fingerprint0: str, msgs) -> bool:
        """`msgs` 是否仍是刚命名的那一段内容（命名期间没被换成别的会话）。

        指纹相同 → 同一段；指纹不同但 msgs0 恰好是 msgs 的前缀 → 同一段在命名
        期间又多了几条消息（轮末之后理论上不该发生，真发生时也不能整段丢保存）。
        其余（新建清空 / 切会话 / 消息被替换）一律判「归属已变」，放弃本次保存。
        前缀判定同样走 messages_fingerprint，与「是不是同一份内容」同一口径。
        """
        if messages_fingerprint(msgs) == fingerprint0:
            return True
        n = len(msgs0)
        return len(msgs) > n and messages_fingerprint(msgs[:n]) == fingerprint0

    def _take_carried_over(self, fingerprint: str) -> bool:
        """carry_over 已把这段内容落盘 → 放弃本份，并把会话状态切到那份会话。

        指纹相同 = 磁盘上已经有这整段对话，再写一份就是重复记录。
        但只"跳过写盘"会把用户留在 NoSession 游离态：这段不会再自动保存
        （_auto_save_done 已消费），再点「新建」也只是空转，后续聊的内容
        谁都不管。所以跳过时改为切到 carry_over 存成的那份会话，并沿用
        pending + _switch_lock 的既有路径通知界面
        `session/saved{name, current, switched:true}`（只切一次、只通知一次）。
        返回 True 表示本份已被接管，调用方应直接返回。
        """
        if self._mgr._carried_over_fingerprint != fingerprint:
            return False
        name = self._mgr._carried_over_name
        # 先清字段再切状态：这一段的事到此为止，别影响下一段
        self._mgr._carried_over_fingerprint = None
        self._mgr._carried_over_name = None
        if name:
            self._mgr._pending_auto_save_name = name
            self._switch_pending()
        return True

    def _fire(self, hook, name: str) -> None:
        """回调失败不影响保存本身"""
        if hook is None or not name:
            return
        try:
            hook(name)
        except Exception:
            pass

    def _switch_pending(self) -> Optional[str]:
        """把待切换的自动保存名消费成 RootSession 状态。

        幂等 + 线程安全：名字被取走后 pending 置空，后到的调用（wait() 与
        后台线程可能同时走到这里）取不到东西，不会重复切换/重复通知。
        返回实际切换到（或先前已切换）的会话名；没切则 None。
        """
        from .session_callbacks import NoSession
        with self._switch_lock:
            name = self._mgr._pending_auto_save_name
            if not name:
                return None
            self._mgr._pending_auto_save_name = None
            if not isinstance(self._mgr.state, NoSession):
                # 用户已进入其他会话/新建：不再改状态，名字仅作废
                return None
            try:
                self._mgr.switch_state(self._mgr.create_root_state(name))
            except Exception:
                return None
        self._fire(self._on_switched, name)
        return name

    def wait(self) -> Optional[str]:
        """等待后台自动保存完成，执行状态切换。幂等调用。

        返回被切换到（或已由后台线程切换）的会话名；未发生切换返回 None。
        """
        if self._auto_save_thread is not None:
            self._auto_save_thread.join(timeout=5)
            self._auto_save_thread = None
        return self._switch_pending()

    def on_exit(self):
        """退出时保存当前会话并清理延迟删除"""
        self.wait()
        if self._mgr.state.session_name():
            # 只在**确认落盘成功**后才打印「会话已自动保存」。auto_save() 成功
            # 才回会话名，失败回 None；拿 state.session_name() 当成功依据会是
            # 谎报——写盘失败时它照样有名字，用户看到「已自动保存: X」而磁盘上
            # 什么都没有（P1，实测 WinError 5 时仍报成功）。
            saved_name = self._mgr.on_auto_save()
            if saved_name:
                _stdout_write(f"  {D}会话已自动保存: {E}{saved_name}{R}\n")
            elif _has_user_message(self._mgr.get_messages()):
                # 有内容却回 None = 写盘失败（空对话不算失败，auto_save 也不报名字）
                reason = readable_save_error(getattr(self._mgr, "_last_save_error", "")
                                             or "未知原因")
                sys.stderr.write(
                    f"  ⚠ 退出时会话未能保存（{reason}），这段内容不会留在磁盘上\n")
                sys.stderr.flush()
        self._mgr.cleanup_deletes()
