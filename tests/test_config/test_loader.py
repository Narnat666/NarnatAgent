"""config层测试 —— defaults + loader + session_store"""

import json
import os
import shutil
import tempfile
import pytest

from narnat_agent.config.defaults import (
    BASE_PROMPT_TEMPLATE, IRON_RULES, COMPRESS_PROMPT,
    COMPRESS_TURN, WARN_TURN_1, WARN_TURN_2,
    NARNAT_DIR, NARNAT_JSON, NARNAT_MD, LAST_SESSION_SUMMARY,
)
from narnat_agent.config.loader import (
    AIConfig, AppConfig, load_config, _build_system_prompt,
    _load_json, _load_user_md,
)
from narnat_agent.config.session_store import (
    save_session, load_session, list_sessions, delete_session,
    format_session_list,
)


# ═══════════════════════════════════════════════════════════════
# defaults.py 测试
# ═══════════════════════════════════════════════════════════════

class TestDefaults:
    def test_thresholds_order(self):
        """阈值递增"""
        assert WARN_TURN_1 < WARN_TURN_2 < COMPRESS_TURN

    def test_base_prompt_has_placeholder(self):
        """基础prompt包含{model}占位符"""
        assert "{model}" in BASE_PROMPT_TEMPLATE

    def test_iron_rules_not_empty(self):
        assert IRON_RULES.strip()

    def test_compress_prompt_not_empty(self):
        assert COMPRESS_PROMPT.strip()

    def test_base_prompt_format(self):
        """格式化后不含原始占位符"""
        result = BASE_PROMPT_TEMPLATE.format(model="test-model")
        assert "{model}" not in result
        assert "test-model" in result


# ═══════════════════════════════════════════════════════════════
# loader.py 测试
# ═══════════════════════════════════════════════════════════════

class TestLoader:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, NARNAT_DIR)
        os.makedirs(self.narnat_dir, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_json(self, data: dict):
        with open(os.path.join(self.narnat_dir, NARNAT_JSON), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _write_md(self, content: str):
        with open(os.path.join(self.narnat_dir, NARNAT_MD), "w", encoding="utf-8") as f:
            f.write(content)

    # -- _load_json --

    def test_load_json_default(self):
        """json不存在返回默认配置"""
        cfg = _load_json(self.narnat_dir)
        assert cfg.api_key == ""
        assert cfg.model == "deepseek-chat"

    def test_load_json_valid(self):
        self._write_json({"api_key": "sk-123", "base_url": "https://api.test.com", "model": "test-v1"})
        cfg = _load_json(self.narnat_dir)
        assert cfg.api_key == "sk-123"
        assert cfg.base_url == "https://api.test.com"
        assert cfg.model == "test-v1"

    def test_load_json_partial(self):
        """只写部分字段，其余用默认值"""
        self._write_json({"model": "my-model"})
        cfg = _load_json(self.narnat_dir)
        assert cfg.model == "my-model"
        assert cfg.api_key == ""

    def test_load_json_invalid(self):
        """非法json返回默认配置"""
        with open(os.path.join(self.narnat_dir, NARNAT_JSON), "w") as f:
            f.write("not json{{{")
        cfg = _load_json(self.narnat_dir)
        assert cfg.model == "deepseek-chat"

    # -- _load_user_md --

    def test_load_md_not_exist(self):
        assert _load_user_md(self.narnat_dir) == ""

    def test_load_md_empty(self):
        self._write_md("")
        assert _load_user_md(self.narnat_dir) == ""

    def test_load_md_content(self):
        self._write_md("# 项目规范\n- 使用Python 3.10+")
        result = _load_user_md(self.narnat_dir)
        assert "项目规范" in result

    # -- _build_system_prompt --

    def test_system_prompt_without_user_md(self):
        prompt = _build_system_prompt("test-model", "")
        assert "test-model" in prompt
        assert IRON_RULES.strip() in prompt
        # 不应出现多余的分隔

    def test_system_prompt_with_user_md(self):
        prompt = _build_system_prompt("test-model", "# 自定义规则")
        assert "test-model" in prompt
        assert "自定义规则" in prompt
        assert IRON_RULES.strip() in prompt

    # -- load_config 集成 --

    def test_load_config_creates_narnat_dir(self):
        """.narnat目录不存在时自动创建"""
        root = tempfile.mkdtemp()
        try:
            cfg = load_config(root)
            assert os.path.isdir(cfg.narnat_dir)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_load_config_creates_files(self):
        """自动创建narnat.json/narnat.md/last_session_summary.md"""
        root = tempfile.mkdtemp()
        try:
            cfg = load_config(root)
            assert os.path.isfile(os.path.join(cfg.narnat_dir, NARNAT_JSON))
            assert os.path.isfile(os.path.join(cfg.narnat_dir, NARNAT_MD))
            assert os.path.isfile(os.path.join(cfg.narnat_dir, LAST_SESSION_SUMMARY))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_load_config_full(self):
        """完整配置加载"""
        root = tempfile.mkdtemp()
        ndir = os.path.join(root, NARNAT_DIR)
        os.makedirs(ndir)
        with open(os.path.join(ndir, NARNAT_JSON), "w", encoding="utf-8") as f:
            json.dump({"api_key": "sk-test", "base_url": "https://api.test.com", "model": "v2"}, f)
        with open(os.path.join(ndir, NARNAT_MD), "w", encoding="utf-8") as f:
            f.write("# My Rules")
        try:
            cfg = load_config(root)
            assert cfg.ai.api_key == "sk-test"
            assert cfg.ai.model == "v2"
            assert "My Rules" in cfg.system_prompt
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# session_store.py 测试
# ═══════════════════════════════════════════════════════════════

class TestSessionStore:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, NARNAT_DIR)
        os.makedirs(self.narnat_dir, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sample_messages(self):
        return [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_save_and_load(self):
        """保存后加载，数据一致"""
        msgs = self._sample_messages()
        err = save_session(self.narnat_dir, "test1", msgs)
        assert err == ""
        loaded, err2 = load_session(self.narnat_dir, "test1")
        assert err2 == ""
        assert loaded == msgs

    def test_load_not_exist(self):
        """加载不存在的会话"""
        loaded, err = load_session(self.narnat_dir, "ghost")
        assert loaded == []
        assert "不存在" in err

    def test_list_empty(self):
        """无会话时列表为空"""
        assert list_sessions(self.narnat_dir) == []

    def test_list_after_save(self):
        """保存后列表非空"""
        save_session(self.narnat_dir, "s1", self._sample_messages())
        sessions = list_sessions(self.narnat_dir)
        assert len(sessions) == 1
        assert sessions[0]["name"] == "s1"
        assert sessions[0]["message_count"] == 3

    def test_delete_specific(self):
        """删除指定会话"""
        save_session(self.narnat_dir, "a", self._sample_messages())
        save_session(self.narnat_dir, "b", self._sample_messages())
        err = delete_session(self.narnat_dir, "a")
        assert err == ""
        sessions = list_sessions(self.narnat_dir)
        assert len(sessions) == 1
        assert sessions[0]["name"] == "b"

    def test_delete_all(self):
        """删除全部会话"""
        save_session(self.narnat_dir, "a", self._sample_messages())
        save_session(self.narnat_dir, "b", self._sample_messages())
        err = delete_session(self.narnat_dir, "--all")
        assert err == ""
        assert list_sessions(self.narnat_dir) == []

    def test_delete_not_exist(self):
        """删除不存在的会话"""
        err = delete_session(self.narnat_dir, "ghost")
        assert "不存在" in err

    def test_format_session_list_empty(self):
        assert format_session_list([]) == ""

    def test_format_session_list_nonempty(self):
        sessions = [{"name": "s1", "timestamp": 1700000000.0, "message_count": 5}]
        result = format_session_list(sessions)
        assert "s1" in result
        assert "5" in result

    def test_save_overwrite(self):
        """同名保存覆盖旧数据"""
        save_session(self.narnat_dir, "dup", [{"role": "user", "content": "old"}])
        save_session(self.narnat_dir, "dup", [{"role": "user", "content": "new"}])
        loaded, _ = load_session(self.narnat_dir, "dup")
        assert loaded[0]["content"] == "new"

    def test_special_chars_in_name(self):
        """名称含特殊字符不崩溃"""
        err = save_session(self.narnat_dir, "a/b\\c", self._sample_messages())
        assert err == ""
        loaded, err2 = load_session(self.narnat_dir, "a/b\\c")
        assert err2 == ""
        assert len(loaded) == 3
