"""commands/session.py 测试"""

import os
import shutil
import tempfile
import pytest

from narnat_agent.commands.session import SessionManager


class TestSessionManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)
        self.mgr = SessionManager(self.narnat_dir)
        self.mgr.set_messages([
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save(self):
        err = self.mgr.save("test1")
        assert err == ""

    def test_save_empty_name(self):
        err = self.mgr.save("")
        assert "不能为空" in err

    def test_show_empty(self):
        result = self.mgr.show()
        assert result == ""

    def test_show_after_save(self):
        self.mgr.save("s1")
        result = self.mgr.show()
        assert "s1" in result

    def test_enter(self):
        self.mgr.save("s1")
        messages, err = self.mgr.enter("s1")
        assert err == ""
        assert len(messages) == 3

    def test_enter_not_exist(self):
        messages, err = self.mgr.enter("ghost")
        assert err != ""
        assert messages == []

    def test_delete(self):
        self.mgr.save("s1")
        err = self.mgr.delete("s1")
        assert err == ""

    def test_delete_all(self):
        self.mgr.save("s1")
        self.mgr.save("s2")
        err = self.mgr.delete("--all")
        assert err == ""
        assert self.mgr.show() == ""

    def test_get_set_messages(self):
        msgs = [{"role": "user", "content": "test"}]
        self.mgr.set_messages(msgs)
        assert self.mgr.get_messages() == msgs
