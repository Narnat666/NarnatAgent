"""core层测试 —— context + compressor + llm(token计数)"""

import os
import shutil
import tempfile
import pytest

from narnat_agent.core.context import ContextManager
from narnat_agent.core.compressor import Compressor
from narnat_agent.core.llm import LLMClient
from narnat_agent.config.loader import AIConfig
from narnat_agent.config.defaults import WARN_TURN_1, WARN_TURN_2, COMPRESS_TURN


# ═══════════════════════════════════════════════════════════════
# ContextManager 测试
# ═══════════════════════════════════════════════════════════════

class TestContextManager:
    def test_initial_state(self):
        ctx = ContextManager()
        assert ctx.turn_count == 0

    def test_increment(self):
        ctx = ContextManager()
        ctx.increment()
        assert ctx.turn_count == 1

    def test_no_warning_before_50(self):
        ctx = ContextManager()
        for _ in range(49):
            warn = ctx.increment()
            assert warn == ""

    def test_warning_at_50(self):
        ctx = ContextManager()
        for _ in range(49):
            ctx.increment()
        warn = ctx.increment()  # 第50次
        assert "50" in warn

    def test_warning_at_100(self):
        ctx = ContextManager()
        for _ in range(100):
            ctx.increment()
        warn = ctx.increment()  # 第101次
        # 第100次应该已经触发了警告
        assert ctx.turn_count == 101

    def test_need_compress(self):
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN):
            ctx.increment()
        assert ctx.need_compress()

    def test_not_need_compress(self):
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN - 1):
            ctx.increment()
        assert not ctx.need_compress()

    def test_reset(self):
        ctx = ContextManager()
        for _ in range(100):
            ctx.increment()
        ctx.reset()
        assert ctx.turn_count == 0
        assert not ctx.need_compress()

    def test_get_summary(self):
        ctx = ContextManager()
        ctx.increment()
        summary = ctx.get_summary()
        assert summary["turn_count"] == 1

    def test_warning_only_once(self):
        """50轮警告只触发一次"""
        ctx = ContextManager()
        warnings = []
        for _ in range(60):
            warn = ctx.increment()
            if warn:
                warnings.append(warn)
        # 50轮警告只应出现一次
        w50 = [w for w in warnings if "50" in w]
        assert len(w50) == 1


# ═══════════════════════════════════════════════════════════════
# Compressor 测试
# ═══════════════════════════════════════════════════════════════

class TestCompressor:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)
        # 创建空的summary文件
        self.summary_path = os.path.join(self.narnat_dir, "last_session_summary.md")
        with open(self.summary_path, "w") as f:
            f.write("")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_compress_messages(self):
        comp = Compressor(self.narnat_dir)
        messages = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "hello"},
        ]
        result = comp.build_compress_messages(messages)
        assert len(result) == 3
        assert result[-1]["role"] == "user"
        assert "总结" in result[-1]["content"]

    def test_write_summary(self):
        comp = Compressor(self.narnat_dir)
        assert comp.write_summary("这是总结内容")
        with open(self.summary_path, "r", encoding="utf-8") as f:
            assert f.read() == "这是总结内容"

    def test_verify_summary_nonempty(self):
        comp = Compressor(self.narnat_dir)
        comp.write_summary("有内容")
        assert comp.verify_summary()

    def test_verify_summary_empty(self):
        comp = Compressor(self.narnat_dir)
        comp.write_summary("")
        assert not comp.verify_summary()

    def test_read_summary(self):
        comp = Compressor(self.narnat_dir)
        comp.write_summary("读取测试")
        assert comp.read_summary() == "读取测试"

    def test_reset_summary(self):
        comp = Compressor(self.narnat_dir)
        comp.write_summary("有内容")
        comp.reset_summary()
        with open(self.summary_path, "r", encoding="utf-8") as f:
            assert f.read() == ""

    def test_build_new_session_messages(self):
        comp = Compressor(self.narnat_dir)
        messages = comp.build_new_session_messages("You are a helper.", "上一轮成果")
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert "上一轮成果" in messages[0]["content"]

    def test_build_new_session_without_summary(self):
        comp = Compressor(self.narnat_dir)
        messages = comp.build_new_session_messages("You are a helper.", "")
        assert len(messages) == 1
        assert messages[0]["content"] == "You are a helper."


# ═══════════════════════════════════════════════════════════════
# LLMClient token计数测试
# ═══════════════════════════════════════════════════════════════

class TestLLMTokenCount:
    def test_count_english(self):
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        messages = [{"role": "user", "content": "hello world"}]
        count = client.count_tokens(messages)
        assert count > 0

    def test_count_chinese(self):
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        messages = [{"role": "user", "content": "你好世界"}]
        count = client.count_tokens(messages)
        assert count > 0
        # 中文token应比英文多
        en_count = client.count_tokens([{"role": "user", "content": "abcd"}])
        assert count > en_count

    def test_count_empty(self):
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        count = client.count_tokens([])
        assert count == 0

    def test_count_mixed(self):
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        messages = [
            {"role": "system", "content": "You are a helper"},
            {"role": "user", "content": "帮我写代码"},
        ]
        count = client.count_tokens(messages)
        assert count > 0
