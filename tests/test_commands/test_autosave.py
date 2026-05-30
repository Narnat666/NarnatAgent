"""
自动保存测试 —— 验证 /save 后退出自动保存，未 /save 则不保存
"""

import os
import json
import shutil
import tempfile

import pytest

from narnat_agent.core.agent import NarnatSessionCallbacks
from narnat_agent.config.session_store import load_session, list_sessions


class TestAutoSaveOnExit:
    """验证 /save 后退出自动保存"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)
        self.messages = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        self.cb = NarnatSessionCallbacks(
            self.narnat_dir,
            lambda: self.messages,
            lambda msgs: setattr(self, 'messages', msgs),
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_save_no_autosave(self):
        """从未 /save 过，退出时不自动保存"""
        result = self.cb.on_exit()
        assert result == ""
        # 没有任何会话被保存
        sessions = list_sessions(self.narnat_dir)
        assert len(sessions) == 0

    def test_save_then_exit_autosaves(self):
        """/save test 后，退出时自动保存"""
        # /save test
        err = self.cb.on_save("test")
        assert err == ""

        # 继续聊天（追加消息）
        self.messages.append({"role": "user", "content": "more chat"})
        self.messages.append({"role": "assistant", "content": "response"})

        # 退出
        saved_name = self.cb.on_exit()
        assert saved_name == "test"

        # 验证：自动保存的会话包含后续聊天
        loaded, err = load_session(self.narnat_dir, "test")
        assert err == ""
        assert len(loaded) == 5  # system + hello + hi + more chat + response
        assert loaded[-2]["content"] == "more chat"

    def test_enter_then_exit_autosaves(self):
        """/enter test 后，退出时自动保存（因为 enter 标记了会话名）"""
        # 先保存一个会话
        from narnat_agent.config.session_store import save_session
        save_session(self.narnat_dir, "mywork", [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "old question"},
        ])

        # /enter mywork
        err = self.cb.on_enter("mywork")
        assert err == ""

        # 继续聊天
        self.messages.append({"role": "user", "content": "new question"})
        self.messages.append({"role": "assistant", "content": "new answer"})

        # 退出
        saved_name = self.cb.on_exit()
        assert saved_name == "mywork"

        # 验证：自动保存包含新聊天
        loaded, err = load_session(self.narnat_dir, "mywork")
        assert err == ""
        assert any(m["content"] == "new question" for m in loaded)

    def test_save_then_enter_different_then_exit(self):
        """/save a, /enter b, 退出时自动保存为 b"""
        from narnat_agent.config.session_store import save_session
        save_session(self.narnat_dir, "b", [
            {"role": "system", "content": "You are a helper."},
        ])

        # /save a
        self.cb.on_save("a")

        # /enter b — 切换到会话b
        self.cb.on_enter("b")

        # 退出时应保存为 b
        saved_name = self.cb.on_exit()
        assert saved_name == "b"

    def test_delete_active_session_no_autosave(self):
        """/save test, /delete test, 退出时不自动保存"""
        self.cb.on_save("test")

        # 删除当前活跃会话
        err = self.cb.on_delete("test")
        assert err == ""

        # 退出时不再自动保存
        saved_name = self.cb.on_exit()
        assert saved_name == ""

    def test_delete_other_session_still_autosave(self):
        """/save test, /delete other, 退出时仍自动保存 test"""
        from narnat_agent.config.session_store import save_session
        save_session(self.narnat_dir, "other", [
            {"role": "system", "content": "You are a helper."},
        ])

        self.cb.on_save("test")

        # 删除 other（不是当前活跃会话）
        self.cb.on_delete("other")

        # 退出时仍自动保存 test
        saved_name = self.cb.on_exit()
        assert saved_name == "test"

    def test_multiple_exits_only_saves_once(self):
        """多次调用 on_exit，只有第一次有效"""
        self.cb.on_save("test")

        saved1 = self.cb.on_exit()
        assert saved1 == "test"

        # 第二次调用，_active_name 已清空
        saved2 = self.cb.on_exit()
        assert saved2 == ""

    def test_save_overwrites_previous_name(self):
        """/save a, /save b, 退出时保存为 b"""
        self.cb.on_save("a")
        self.cb.on_save("b")

        saved_name = self.cb.on_exit()
        assert saved_name == "b"

        # a 也存在（第一次 /save a 时保存了）
        loaded_a, _ = load_session(self.narnat_dir, "a")
        assert len(loaded_a) > 0

        # b 也存在，且是最新内容
        loaded_b, _ = load_session(self.narnat_dir, "b")
        assert len(loaded_b) > 0


class TestAutoSaveIntegration:
    """集成测试：模拟完整的 save → chat → exit 流程"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_lifecycle(self):
        """完整生命周期：新对话 → /save → 聊天 → 退出 → /enter → 聊天 → 退出"""
        messages = [{"role": "system", "content": "You are a helper."}]
        cb = NarnatSessionCallbacks(
            self.narnat_dir,
            lambda: messages,
            lambda msgs: setattr(self, '_msgs', msgs) or setattr(messages, '__self__', None) or True,
        )
        # 简化：直接修改 messages 列表
        def set_msgs(msgs):
            nonlocal messages
            messages = msgs
        cb = NarnatSessionCallbacks(
            self.narnat_dir,
            lambda: messages,
            set_msgs,
        )

        # 1. 新对话，聊几句
        messages.append({"role": "user", "content": "question1"})
        messages.append({"role": "assistant", "content": "answer1"})

        # 2. /save work
        err = cb.on_save("work")
        assert err == ""

        # 3. 继续聊天
        messages.append({"role": "user", "content": "question2"})
        messages.append({"role": "assistant", "content": "answer2"})

        # 4. 退出（自动保存）
        saved = cb.on_exit()
        assert saved == "work"

        # 5. 验证保存了4条消息（system + q1 + a1 + q2 + a2 = 5）
        loaded, err = load_session(self.narnat_dir, "work")
        assert err == ""
        assert len(loaded) == 5

        # 6. 重新进入
        messages2 = []
        def set_msgs2(msgs):
            nonlocal messages2
            messages2 = msgs
        cb2 = NarnatSessionCallbacks(
            self.narnat_dir,
            lambda: messages2,
            set_msgs2,
        )
        err = cb2.on_enter("work")
        assert err == ""
        assert len(messages2) == 5

        # 7. 继续聊天
        messages2.append({"role": "user", "content": "question3"})
        messages2.append({"role": "assistant", "content": "answer3"})

        # 8. 退出（应自动保存）
        saved = cb2.on_exit()
        assert saved == "work"

        # 9. 验证：现在有7条消息
        loaded2, err = load_session(self.narnat_dir, "work")
        assert err == ""
        assert len(loaded2) == 7
        assert loaded2[-2]["content"] == "question3"
