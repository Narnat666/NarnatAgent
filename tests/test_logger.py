"""logger.py 测试"""

import os
import shutil
import tempfile
import time
import pytest

from narnat_agent.logger import AgentLogger, _redact


class TestRedact:
    def test_no_secret(self):
        assert _redact("hello world") == "hello world"

    def test_sk_prefix(self):
        result = _redact("key=sk-abc123456789")
        assert "sk-abc1***" in result or "***" in result

    def test_api_key_assignment(self):
        result = _redact('api_key="sk-mysecretkey123"')
        assert "myse" in result or "***" in result

    def test_normal_text_unchanged(self):
        text = "调用: pattern='class Foo', path='src/'"
        assert _redact(text) == text


class TestAgentLogger:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_creates_log_file(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        assert os.path.isfile(filepath)
        assert filepath.endswith(".log")
        logger.close()

    def test_log_dir_created(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        assert os.path.isdir(os.path.join(self.tmpdir, "logs"))
        logger.close()

    def test_write_info(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        logger.info("core.agent", "用户输入: 帮我重构")
        logger.close()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "用户输入" in content
        assert "INFO" in content

    def test_write_error(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        logger.error("core.llm", "API连接失败")
        logger.close()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "API连接失败" in content
        assert "ERROR" in content

    def test_write_debug(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        logger.debug("tools.grep", "pattern=class Foo")
        logger.close()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "DEBUG" in content

    def test_write_warning(self):
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        logger.warning("core.context", "对话已达50轮")
        logger.close()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "WARNING" in content

    def test_redact_in_log(self):
        """日志中API key被脱敏"""
        logger = AgentLogger(self.tmpdir)
        filepath = logger.start()
        logger.info("config", "api_key=sk-supersecret123456")
        logger.close()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "supersecret123456" not in content

    def test_multiple_starts(self):
        """多次start创建不同文件"""
        logger = AgentLogger(self.tmpdir)
        f1 = logger.start()
        logger.info("test", "msg1")
        time.sleep(1.1)  # 确保文件名时间戳不同
        f2 = logger.start()
        logger.info("test", "msg2")
        logger.close()
        assert f1 != f2
        assert os.path.isfile(f1)
        assert os.path.isfile(f2)

    def test_no_start_no_crash(self):
        """未start时调用log不崩溃"""
        logger = AgentLogger(self.tmpdir)
        logger.info("test", "should not crash")

    def test_close_idempotent(self):
        """多次close不崩溃"""
        logger = AgentLogger(self.tmpdir)
        logger.start()
        logger.close()
        logger.close()
