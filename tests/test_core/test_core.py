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
        for _ in range(WARN_TURN_1 - 1):
            warn = ctx.increment()
            assert warn == ""

    def test_warning_at_50(self):
        ctx = ContextManager()
        for _ in range(WARN_TURN_1 - 1):
            ctx.increment()
        warn = ctx.increment()  # 第WARN_TURN_1次
        assert str(WARN_TURN_1) in warn

    def test_warning_at_100(self):
        ctx = ContextManager()
        for _ in range(WARN_TURN_2):
            ctx.increment()
        warn = ctx.increment()  # 第WARN_TURN_2+1次
        assert ctx.turn_count == WARN_TURN_2 + 1

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

    def test_set_retry_soon(self):
        """压缩失败后近期重试"""
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN):
            ctx.increment()
        assert ctx.need_compress()
        ctx.set_retry_soon()
        # 重试点应小于COMPRESS_TURN
        assert ctx.turn_count < COMPRESS_TURN
        # 再过几轮还不触发
        for _ in range(COMPRESS_TURN - ctx.turn_count - 1):
            ctx.increment()
        assert not ctx.need_compress()
        # 再过1轮触发
        ctx.increment()
        assert ctx.need_compress()

    def test_get_summary(self):
        ctx = ContextManager()
        ctx.increment()
        summary = ctx.get_summary()
        assert summary["turn_count"] == 1

    def test_warning_only_once(self):
        """WARN_TURN_1警告只触发一次"""
        ctx = ContextManager()
        warnings = []
        for _ in range(WARN_TURN_1 + 10):
            warn = ctx.increment()
            if warn:
                warnings.append(warn)
        w1 = [w for w in warnings if str(WARN_TURN_1) in w]
        assert len(w1) == 1


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
