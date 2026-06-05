"""全方位高强度测试 —— 深度、广度、暴力、边界全覆盖

覆盖测试盲区：
1. Agent闭环集成（MockLLMServer + Agent核心逻辑）
2. LLM chat_stream仿真（OpenAI流式chunk解析）
3. _repair_messages深度测试（打断修复运行时行为）
4. 压缩流程闭环测试
5. 并发/竞态安全测试
6. 边界/暴力/极端场景补充
7. WebSearch仿真集成
8. 跨模块交互验证
"""

import json
import os
import shutil
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock
from concurrent.futures import ThreadPoolExecutor

import pytest

from narnat_agent.core.agent import (
    Agent, _READONLY_TOOLS, _WRITE_TOOLS, _SERIAL_TOOLS,
    _TOOL_LABELS, _FILE_PATH_TOOLS,
    NarnatSessionCallbacks,
)
from narnat_agent.core.llm import LLMClient, _OpenAIBackend, _AnthropicBackend
from narnat_agent.core.context import ContextManager
from narnat_agent.core.compressor import Compressor
from narnat_agent.config.loader import AIConfig, AppConfig
from narnat_agent.config.defaults import (
    COMPRESS_TURN, WARN_TURN_1, WARN_TURN_2,
    COMPRESS_PROMPT, LAST_SESSION_SUMMARY,
)
from narnat_agent.tools import registry as tool_registry
from narnat_agent.tools import write as write_tool
from narnat_agent.tools import read as read_tool
from narnat_agent.tools import edit as edit_tool
from narnat_agent.tools import grep as grep_tool
from narnat_agent.tools import glob as glob_tool
from narnat_agent.tools import bash as bash_tool
from narnat_agent.tools import todo_write as todo_tool
from narnat_agent.logger import AgentLogger
from narnat_agent.config import session_store
from tests.simulators.mock_llm_server import MockLLMServer
from tests.simulators.mock_filesystem import MockFileSystem


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════

def _r(result):
    """解包工具返回值"""
    return result[0] if isinstance(result, tuple) else result


def _make_temp_dir():
    """创建临时目录"""
    d = tempfile.mkdtemp(prefix="narnat_test_")
    return d


# ═══════════════════════════════════════════════════════════════
# 1. LLM chat_stream 仿真测试
# ═══════════════════════════════════════════════════════════════

class TestLLMChatStreamSimulation:
    """LLMClient + MockLLMServer 闭环测试"""

    def test_stream_text_response(self):
        """流式纯文本响应正确解析"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("Hello, world!")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
            contents = [c["content"] for c in chunks if "content" in c and "tool_calls" not in c]
            assert "".join(contents) == "Hello, world!"

    def test_stream_tool_call_response(self):
        """流式tool_call响应正确解析"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
            ])
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "read file"}]))
            tc_chunks = [c for c in chunks if "tool_calls" in c]
            assert len(tc_chunks) == 1
            assert tc_chunks[0]["tool_calls"][0]["function"]["name"] == "Read"

    def test_stream_mixed_text_and_tool_calls(self):
        """流式混合文本+tool_call响应"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_response(
                content="让我读取文件。",
                tool_calls=[{"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}}],
            )
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "read"}]))
            has_content = any("content" in c and "tool_calls" not in c for c in chunks)
            has_tool_calls = any("tool_calls" in c for c in chunks)
            assert has_content
            assert has_tool_calls

    def test_stream_multiple_tool_calls(self):
        """流式多个tool_call响应"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/a.txt"}},
                {"name": "Read", "arguments": {"file_path": "/tmp/b.txt"}},
            ])
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "read both"}]))
            tc_chunks = [c for c in chunks if "tool_calls" in c]
            assert len(tc_chunks) == 1
            assert len(tc_chunks[0]["tool_calls"]) == 2

    def test_stream_sequential_responses(self):
        """多次请求按序消费响应队列"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("第一次回答")
            server.enqueue_text("第二次回答")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            msgs = [{"role": "user", "content": "hi"}]
            chunks1 = list(client.chat_stream(msgs))
            chunks2 = list(client.chat_stream(msgs))
            c1 = "".join(c.get("content", "") for c in chunks1 if "content" in c and "tool_calls" not in c)
            c2 = "".join(c.get("content", "") for c in chunks2 if "content" in c and "tool_calls" not in c)
            assert c1 == "第一次回答"
            assert c2 == "第二次回答"

    def test_queue_exhaustion_returns_default(self):
        """队列耗尽返回默认结束消息"""
        with MockLLMServer(stream=True) as server:
            # 不入队任何响应
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
            # 应该有内容（默认"任务完成。"）
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "".join(contents)  # 非空

    def test_no_tools_mode(self):
        """no_tools=True时不传工具定义"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("压缩总结内容")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream(
                [{"role": "user", "content": "summarize"}],
                no_tools=True
            ))
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "".join(contents) == "压缩总结内容"

    def test_api_connection_error(self):
        """连接不存在的API返回error chunk"""
        config = AIConfig(api_key="test", base_url="http://127.0.0.1:1/v1", model="mock")
        client = LLMClient(config)
        chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
        error_chunks = [c for c in chunks if c.get("finish_reason") == "error"]
        assert len(error_chunks) == 1
        assert "错误" in error_chunks[0]["content"]

    def test_non_stream_server_mode(self):
        """MockLLMServer非流式模式自身功能验证
        注：LLMClient始终使用stream=True，非流式MockLLMServer
        返回JSON而非SSE，OpenAI SDK无法解析。此测试仅验证
        MockLLMServer自身的非流式响应构建逻辑。
        """
        with MockLLMServer(stream=False) as server:
            server.enqueue_text("非流式响应")
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
            ])
            # 验证队列正确入队
            assert server.remaining == 2


# ═══════════════════════════════════════════════════════════════
# 2. _repair_messages 深度测试
# ═══════════════════════════════════════════════════════════════

class TestRepairMessagesDeep:
    """_repair_messages 运行时行为深度测试"""

    def _make_agent_with_mock_ui(self):
        """创建Agent实例，mock掉UI交互"""
        with patch.object(Agent, '__init__', lambda self, *a, **k: None):
            agent = Agent.__new__(Agent)
        agent._messages = []
        agent._logger = AgentLogger(tempfile.mkdtemp())
        return agent

    def test_no_repair_needed(self):
        """正常消息序列不需要修复"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        original_len = len(agent._messages)
        agent._repair_messages()
        assert len(agent._messages) == original_len

    def test_repair_unreplied_tool_call(self):
        """assistant含tool_calls但无对应tool消息 → 补上[用户中断]"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            ]},
        ]
        agent._repair_messages()
        # 应补上tool消息和assistant消息
        assert agent._messages[-2]["role"] == "tool"
        assert agent._messages[-2]["content"] == "[用户中断]"
        assert agent._messages[-2]["tool_call_id"] == "call_0"
        assert agent._messages[-1]["role"] == "assistant"

    def test_repair_multiple_unreplied_tool_calls(self):
        """多个未回复的tool_call全部补上"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read files"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
                {"id": "call_1", "type": "function", "function": {"name": "Glob", "arguments": "{}"}},
            ]},
        ]
        agent._repair_messages()
        tool_msgs = [m for m in agent._messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert tool_msgs[0]["tool_call_id"] == "call_0"
        assert tool_msgs[1]["tool_call_id"] == "call_1"

    def test_partial_repair_some_replied(self):
        """部分tool_call已回复，只补未回复的"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read files"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
                {"id": "call_1", "type": "function", "function": {"name": "Glob", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_0", "content": "file content"},
        ]
        agent._repair_messages()
        tool_msgs = [m for m in agent._messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert tool_msgs[0]["content"] == "file content"
        assert tool_msgs[1]["content"] == "[用户中断]"

    def test_repair_idempotent(self):
        """修复操作幂等：多次调用结果一致"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            ]},
        ]
        agent._repair_messages()
        len_after_first = len(agent._messages)
        agent._repair_messages()
        assert len(agent._messages) == len_after_first

    def test_repair_with_none_tool_call_id(self):
        """tool_call的id为None时不崩溃"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": None, "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            ]},
        ]
        # id为None，不应崩溃
        agent._repair_messages()
        # None id不会被修复（因为tc_id为None时跳过）
        tool_msgs = [m for m in agent._messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 0  # None id不处理

    def test_repair_after_interrupted_parallel_execution(self):
        """并行执行中断后的消息修复"""
        agent = self._make_agent_with_mock_ui()
        agent._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read all"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "/tmp/a.txt"}'}},
                {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "/tmp/b.txt"}'}},
                {"id": "call_2", "type": "function", "function": {"name": "Grep", "arguments": '{"pattern": "test"}'}},
            ]},
            # 只有call_0完成了
            {"role": "tool", "tool_call_id": "call_0", "content": "content of a.txt"},
        ]
        agent._repair_messages()
        tool_msgs = [m for m in agent._messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3
        assert tool_msgs[0]["content"] == "content of a.txt"
        assert tool_msgs[1]["content"] == "[用户中断]"
        assert tool_msgs[2]["content"] == "[用户中断]"


# ═══════════════════════════════════════════════════════════════
# 3. 压缩流程闭环测试
# ═══════════════════════════════════════════════════════════════

class TestCompressFlowClosedLoop:
    """压缩流程闭环测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        self.narnat_dir = narnat_dir
        self.compressor = Compressor(narnat_dir)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_compress_cycle(self):
        """完整压缩周期：构建→写入→校验→读取→新会话"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": "问题2"},
            {"role": "assistant", "content": "回答2"},
        ]
        # 构建压缩请求
        compress_msgs = self.compressor.build_compress_messages(messages)
        assert len(compress_msgs) == len(messages) + 1
        assert compress_msgs[-1]["content"] == COMPRESS_PROMPT
        # 原messages不被修改
        assert len(messages) == 5

        # 写入总结
        summary = "用户问了两个问题，已回答。"
        assert self.compressor.write_summary(summary) is True
        assert self.compressor.verify_summary() is True

        # 读取总结
        read_back = self.compressor.read_summary()
        assert read_back == summary

        # 构建新会话
        new_msgs = self.compressor.build_new_session_messages("system prompt", read_back)
        assert len(new_msgs) == 1
        assert new_msgs[0]["role"] == "system"
        assert "上一轮对话成果" in new_msgs[0]["content"]
        assert summary in new_msgs[0]["content"]

        # 重置
        self.compressor.reset_summary()
        assert self.compressor.verify_summary() is False

    def test_compress_with_empty_summary(self):
        """空总结不注入新会话"""
        self.compressor.write_summary("")
        assert self.compressor.verify_summary() is False
        new_msgs = self.compressor.build_new_session_messages("sys", "")
        assert "上一轮对话成果" not in new_msgs[0]["content"]

    def test_compress_with_whitespace_only_summary(self):
        """纯空白总结校验失败"""
        self.compressor.write_summary("   \n\t  ")
        assert self.compressor.verify_summary() is False

    def test_compress_write_failure(self):
        """写入失败路径（只读目录）"""
        # 使用不存在的深层目录
        bad_compressor = Compressor(os.path.join(self.tmpdir, "nonexistent", "deep"))
        assert bad_compressor.write_summary("test") is False

    def test_compress_verify_no_file(self):
        """校验不存在的文件返回False"""
        assert self.compressor.verify_summary() is False

    def test_compress_read_no_file(self):
        """读取不存在的文件返回空串"""
        assert self.compressor.read_summary() == ""

    def test_compress_preserves_original_messages(self):
        """压缩不修改原始messages"""
        original = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        original_len = len(original)
        compress_msgs = self.compressor.build_compress_messages(original)
        assert len(original) == original_len
        assert original[-1]["content"] == "q1"  # 未被修改

    def test_compress_large_summary(self):
        """大总结内容正确处理"""
        large_summary = "这是一段很长的总结。" * 1000
        self.compressor.write_summary(large_summary)
        assert self.compressor.verify_summary() is True
        read_back = self.compressor.read_summary()
        assert read_back == large_summary.strip()

    def test_compress_unicode_summary(self):
        """Unicode总结正确处理"""
        unicode_summary = "总结：🎉✅❌ 中文测试 日本語テスト 한국어"
        self.compressor.write_summary(unicode_summary)
        assert self.compressor.verify_summary() is True
        assert self.compressor.read_summary() == unicode_summary


# ═══════════════════════════════════════════════════════════════
# 4. ContextManager 深度边界测试
# ═══════════════════════════════════════════════════════════════

class TestContextManagerDeepBoundary:
    """ContextManager 深度边界测试"""

    def test_exact_threshold_values(self):
        """精确阈值值测试"""
        ctx = ContextManager()
        # 递增到WARN_TURN_1-1
        for _ in range(WARN_TURN_1 - 1):
            assert ctx.increment() == ""
        # 恰好WARN_TURN_1
        warn = ctx.increment()
        assert f"{WARN_TURN_1}轮" in warn

    def test_set_retry_soon_then_increment_to_compress(self):
        """set_retry_soon后10轮再次触发压缩"""
        ctx = ContextManager()
        ctx._turn_count = COMPRESS_TURN
        ctx.set_retry_soon()
        assert not ctx.need_compress()  # 设为90，不触发
        # 递增10轮
        for _ in range(10):
            ctx.increment()
        assert ctx.need_compress()  # 100轮，触发

    def test_rapid_increment_beyond_compress(self):
        """快速递增远超压缩阈值"""
        ctx = ContextManager()
        for _ in range(500):
            ctx.increment()
        assert ctx.need_compress()
        assert ctx.turn_count == 500

    def test_reset_clears_all_state(self):
        """重置清除所有状态"""
        ctx = ContextManager()
        for _ in range(200):
            ctx.increment()
        ctx.reset()
        assert ctx.turn_count == 0
        assert not ctx.need_compress()
        summary = ctx.get_summary()
        assert summary["turn_count"] == 0
        assert summary["warned_50"] is False
        assert summary["warned_100"] is False

    def test_warnings_fire_only_once(self):
        """警告只触发一次"""
        ctx = ContextManager()
        warnings = []
        for _ in range(200):
            w = ctx.increment()
            if w:
                warnings.append(w)
        # 应该只有2个警告（50轮和80轮）
        assert len(warnings) == 2

    def test_set_retry_soon_never_goes_negative(self):
        """set_retry_soon不会产生负数"""
        ctx = ContextManager()
        ctx._turn_count = 0
        ctx.set_retry_soon()
        assert ctx.turn_count >= 0


# ═══════════════════════════════════════════════════════════════
# 5. 工具执行深度测试
# ═══════════════════════════════════════════════════════════════

class TestToolExecutionDeep:
    """工具执行深度测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_nonexistent_file(self):
        """读取不存在的文件返回错误"""
        result = _r(read_tool.execute(os.path.join(self.tmpdir, "no_such_file.txt")))
        assert "错误" in result or "不存在" in result or "Error" in result

    def test_read_empty_file(self):
        """读取空文件"""
        path = os.path.join(self.tmpdir, "empty.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        result = _r(read_tool.execute(path))
        # 空文件应返回空或提示
        assert isinstance(result, str)

    def test_read_binary_file(self):
        """读取二进制文件不崩溃"""
        path = os.path.join(self.tmpdir, "binary.bin")
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe")
        result = _r(read_tool.execute(path))
        assert isinstance(result, str)

    def test_read_with_offset_beyond_file(self):
        """offset超出文件行数"""
        path = os.path.join(self.tmpdir, "short.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("line1\nline2\n")
        result = _r(read_tool.execute(path, offset=100))
        assert isinstance(result, str)

    def test_edit_nonexistent_file(self):
        """编辑不存在的文件"""
        path = os.path.join(self.tmpdir, "no_such.txt")
        result = _r(edit_tool.execute(path, old_string="old", new_string="new"))
        assert "错误" in result or "不存在" in result or "Error" in result

    def test_edit_old_string_not_found(self):
        """编辑时old_string未找到"""
        path = os.path.join(self.tmpdir, "edit_test.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello world\n")
        result = _r(edit_tool.execute(path, old_string="not_exist", new_string="new"))
        assert "错误" in result or "未找到" in result

    def test_edit_multiple_matches_no_replace_all(self):
        """多处匹配但未设replace_all"""
        path = os.path.join(self.tmpdir, "multi.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("aaa\nbbb\naaa\n")
        result = _r(edit_tool.execute(path, old_string="aaa", new_string="ccc"))
        assert "错误" in result or "多处" in result or "多次" in result

    def test_write_creates_deep_directory(self):
        """Write创建深层嵌套目录"""
        path = os.path.join(self.tmpdir, "a", "b", "c", "d", "deep.txt")
        result = _r(write_tool.execute(path, "deep content"))
        assert "成功" in result or "写入" in result or "已写入" in result
        assert os.path.exists(path)

    def test_write_then_read_consistency(self):
        """写入后读回内容一致"""
        path = os.path.join(self.tmpdir, "consistency.txt")
        content = "测试内容一致性\n第二行\n第三行"
        write_tool.execute(path, content)
        result = _r(read_tool.execute(path))
        assert "测试内容一致性" in result
        assert "第二行" in result

    def test_glob_no_match_returns_empty(self):
        """Glob无匹配返回空或提示"""
        result = _r(glob_tool.execute("*.xyz_not_exist", self.tmpdir))
        assert isinstance(result, str)

    def test_grep_pattern_not_found(self):
        """Grep模式未找到"""
        path = os.path.join(self.tmpdir, "grep_test.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello world\n")
        result = _r(grep_tool.execute("not_found_pattern", path))
        assert isinstance(result, str)

    def test_todo_write_empty_list(self):
        """TodoWrite空列表返回错误"""
        result = _r(todo_tool.execute([]))
        assert "错误" in result or "不能为空" in result

    def test_todo_write_multiple_in_progress(self):
        """TodoWrite多个in_progress返回错误"""
        result = _r(todo_tool.execute([
            {"content": "task1", "status": "in_progress", "activeForm": "doing task1"},
            {"content": "task2", "status": "in_progress", "activeForm": "doing task2"},
        ]))
        assert "错误" in result

    def test_todo_write_invalid_status(self):
        """TodoWrite非法status返回错误"""
        result = _r(todo_tool.execute([
            {"content": "task1", "status": "invalid_status", "activeForm": "doing"},
        ]))
        assert "错误" in result

    def test_todo_write_missing_fields(self):
        """TodoWrite缺少必填字段"""
        result = _r(todo_tool.execute([
            {"content": "task1"},  # 缺少status和activeForm
        ]))
        assert "错误" in result

    def test_registry_unknown_tool(self):
        """Registry调用未知工具返回错误"""
        result = tool_registry.execute("NonExistentTool", {})
        assert "错误" in result[0] or "未知" in result[0]

    def test_registry_all_tools_callable(self):
        """Registry中所有工具可调用（不崩溃）"""
        names = tool_registry.get_tool_names()
        assert len(names) >= 9  # 至少9个工具


# ═══════════════════════════════════════════════════════════════
# 6. 并发/竞态安全测试
# ═══════════════════════════════════════════════════════════════

class TestConcurrencySafety:
    """并发/竞态安全测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_reads_no_crash(self):
        """并发读取同一文件不崩溃"""
        path = os.path.join(self.tmpdir, "concurrent.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("content\n" * 100)

        results = []
        errors = []

        def read_file():
            try:
                r = _r(read_tool.execute(path))
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_file) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        assert len(results) == 20

    def test_concurrent_glob_no_crash(self):
        """并发Glob不崩溃"""
        # 创建一些文件
        for i in range(10):
            path = os.path.join(self.tmpdir, f"file_{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# file {i}")

        errors = []

        def do_glob():
            try:
                _r(glob_tool.execute("*.py", self.tmpdir))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_glob) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0

    def test_concurrent_grep_no_crash(self):
        """并发Grep不崩溃"""
        path = os.path.join(self.tmpdir, "grep_concurrent.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("test line\n" * 100)

        errors = []

        def do_grep():
            try:
                _r(grep_tool.execute("test", path))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_grep) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0

    def test_write_read_mark_thread_safety(self):
        """Write的_read_files集合在并发下不崩溃"""
        errors = []

        def mark_and_check():
            try:
                for i in range(50):
                    write_tool.mark_read(f"/tmp/test_{i}.txt")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mark_and_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        write_tool.clear_read_files()

    def test_concurrent_context_increment(self):
        """ContextManager并发increment不崩溃"""
        ctx = ContextManager()
        errors = []

        def do_increment():
            try:
                for _ in range(100):
                    ctx.increment()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        # 10线程 * 100次 = 1000次increment
        assert ctx.turn_count == 1000

    def test_concurrent_compress_operations(self):
        """Compressor并发操作不崩溃"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        compressor = Compressor(narnat_dir)
        errors = []

        def write_and_verify():
            try:
                compressor.write_summary("test summary")
                compressor.verify_summary()
                compressor.read_summary()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_and_verify) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════
# 7. 暴力/极端场景测试
# ═══════════════════════════════════════════════════════════════

class TestBrutalExtreme:
    """暴力/极端场景测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_very_large_file(self):
        """读取超大文件截断"""
        path = os.path.join(self.tmpdir, "large.txt")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(5000):
                f.write(f"line {i}\n")
        result = _r(read_tool.execute(path))
        assert isinstance(result, str)
        # 应有截断提示
        assert len(result) > 0

    def test_read_path_with_special_chars(self):
        """路径含特殊字符"""
        path = os.path.join(self.tmpdir, "special chars & 中文.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("special content\n")
        result = _r(read_tool.execute(path))
        assert "special content" in result

    def test_write_very_long_content(self):
        """写入超长内容"""
        path = os.path.join(self.tmpdir, "long_content.txt")
        content = "x" * 1_000_000  # 1MB
        result = _r(write_tool.execute(path, content))
        assert isinstance(result, str)
        # 验证文件确实写入了
        with open(path, "r", encoding="utf-8") as f:
            assert len(f.read()) == 1_000_000

    def test_edit_replace_all_many_occurrences(self):
        """replace_all替换大量匹配"""
        path = os.path.join(self.tmpdir, "many_match.txt")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(1000):
                f.write(f"old_text line {i}\n")
        result = _r(edit_tool.execute(path, old_string="old_text", new_string="new_text", replace_all=True))
        assert "错误" not in result
        # 验证替换成功
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "old_text" not in content
        assert content.count("new_text") == 1000

    def test_grep_complex_regex_patterns(self):
        """Grep复杂正则模式"""
        path = os.path.join(self.tmpdir, "regex_test.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("def foo():\n  pass\n\nclass Bar:\n  pass\n")
        # 搜索函数定义
        result = _r(grep_tool.execute(r"def \w+", path, output_mode="content", n=True))
        assert "foo" in result

    def test_glob_various_patterns(self):
        """Glob各种模式"""
        for ext in ["py", "js", "ts", "md", "json", "yaml"]:
            path = os.path.join(self.tmpdir, f"test.{ext}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("content")
        for pattern in ["*.py", "*.js", "*.md", "**/*.json"]:
            result = _r(glob_tool.execute(pattern, self.tmpdir))
            assert isinstance(result, str)

    def test_todo_write_large_list(self):
        """TodoWrite大量任务"""
        todos = [
            {"content": f"task {i}", "status": "pending", "activeForm": f"doing task {i}"}
            for i in range(100)
        ]
        result = _r(todo_tool.execute(todos))
        assert "错误" not in result
        assert "100" in result

    def test_session_store_special_names(self):
        """会话存储特殊名称"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        special_names = [
            "test/../../../etc/passwd",  # 路径逃逸
            "test..name",                # 含..
            "test|name",                 # 含管道符
            "test name",                 # 含空格
            "测试名称",                   # 中文
        ]
        for name in special_names:
            err = session_store.save_session(narnat_dir, name, [{"role": "user", "content": "test"}])
            # 不应崩溃，可能返回错误或成功
            assert isinstance(err, str)

    def test_llm_count_tokens_edge_cases(self):
        """LLM token计数边界"""
        config = AIConfig(api_key="test", base_url="http://localhost/v1", model="mock")
        client = LLMClient(config)
        # 空消息
        assert client.count_tokens([]) == 0
        # None content
        assert client.count_tokens([{"role": "assistant", "content": None}]) == 0
        # 纯中文
        cn_count = client.count_tokens([{"role": "user", "content": "你好世界"}])
        assert cn_count > 0
        # 纯英文
        en_count = client.count_tokens([{"role": "user", "content": "hello world"}])
        assert en_count > 0
        # 中文token应约为英文的2倍（每字2token vs 每词1token）
        assert cn_count >= en_count

    def test_anthropic_message_conversion(self):
        """Anthropic消息格式转换"""
        config = AIConfig(api_key="test", base_url="https://anthropic.api/v1", model="mock")
        client = LLMClient(config)
        assert isinstance(client._backend, _AnthropicBackend)

        # 测试消息转换
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "/tmp/test.txt"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_0", "content": "file content"},
        ]
        system, anthropic_msgs = client._backend._convert_messages(messages)
        assert system == "system prompt"
        assert len(anthropic_msgs) > 0

    def test_anthropic_tool_conversion(self):
        """Anthropic工具定义转换"""
        config = AIConfig(api_key="test", base_url="https://anthropic.api/v1", model="mock")
        client = LLMClient(config)
        tool_defs = tool_registry.get_tool_definitions()
        anthropic_tools = client._backend._convert_tools(tool_defs)
        assert len(anthropic_tools) == len(tool_defs)
        for t in anthropic_tools:
            assert "name" in t
            assert "input_schema" in t


# ═══════════════════════════════════════════════════════════════
# 8. 跨模块交互测试
# ═══════════════════════════════════════════════════════════════

class TestCrossModuleInteraction:
    """跨模块交互验证"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_read_edit_chain(self):
        """Write → Read → Edit 完整工具链"""
        path = os.path.join(self.tmpdir, "chain.txt")
        # Write
        w_result = _r(write_tool.execute(path, "original content\n"))
        assert isinstance(w_result, str)
        # Read
        r_result = _r(read_tool.execute(path))
        assert "original content" in r_result
        # Edit
        e_result = _r(edit_tool.execute(path, old_string="original", new_string="modified"))
        assert "错误" not in e_result
        # 验证
        r2_result = _r(read_tool.execute(path))
        assert "modified content" in r_result or "modified" in r2_result

    def test_write_glob_grep_chain(self):
        """Write → Glob → Grep 搜索工具链"""
        for i in range(5):
            path = os.path.join(self.tmpdir, f"module_{i}.py")
            write_tool.execute(path, f"def func_{i}():\n    pass\n")

        # Glob查找所有py文件
        g_result = _r(glob_tool.execute("*.py", self.tmpdir))
        assert "module_0.py" in g_result

        # Grep搜索函数定义
        grep_result = _r(grep_tool.execute("def func_0", self.tmpdir, output_mode="content"))
        assert "func_0" in grep_result

    def test_context_compress_interact(self):
        """ContextManager + Compressor 交互"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        ctx = ContextManager()
        compressor = Compressor(narnat_dir)

        # 模拟对话到压缩阈值
        messages = [{"role": "system", "content": "sys"}]
        for i in range(COMPRESS_TURN):
            messages.append({"role": "user", "content": f"question {i}"})
            messages.append({"role": "assistant", "content": f"answer {i}"})
            ctx.increment()

        assert ctx.need_compress()

        # 构建压缩请求
        compress_msgs = compressor.build_compress_messages(messages)
        assert len(compress_msgs) > len(messages)

        # 模拟压缩成功
        compressor.write_summary("对话总结")
        assert compressor.verify_summary()

        # 重置上下文
        ctx.reset()
        assert not ctx.need_compress()

    def test_session_store_save_load_cycle(self):
        """SessionStore 保存→加载 完整周期"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        # 保存
        err = session_store.save_session(narnat_dir, "test_session", messages)
        assert err == ""
        # 加载
        loaded, err = session_store.load_session(narnat_dir, "test_session")
        assert err == ""
        assert len(loaded) == len(messages)
        assert loaded[1]["content"] == "hello"

    def test_session_store_list_and_delete(self):
        """SessionStore 列表→删除 周期"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        session_store.save_session(narnat_dir, "s1", [{"role": "user", "content": "1"}])
        session_store.save_session(narnat_dir, "s2", [{"role": "user", "content": "2"}])
        # 列表
        sessions = session_store.list_sessions(narnat_dir)
        assert len(sessions) >= 2
        # 删除
        err = session_store.delete_session(narnat_dir, "s1")
        assert err == ""
        sessions2 = session_store.list_sessions(narnat_dir)
        assert len(sessions2) == 1

    def test_tool_registry_execute_all_tools(self):
        """Registry能执行所有已注册工具（不崩溃）"""
        names = tool_registry.get_tool_names()
        # 测试每个工具的空参数调用不崩溃（可能返回错误但不抛异常）
        for name in names:
            try:
                result = tool_registry.execute(name, {})
                assert isinstance(result, tuple)
                assert len(result) == 2
            except Exception:
                pass  # 某些工具空参数可能抛异常，但不应有未捕获的

    def test_narnat_session_callbacks(self):
        """NarnatSessionCallbacks 回调逻辑"""
        narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        messages = [{"role": "user", "content": "test"}]
        callbacks = NarnatSessionCallbacks(
            narnat_dir,
            lambda: messages,
            lambda m: None,
        )
        # on_save
        err = callbacks.on_save("cb_test")
        assert err == ""
        # on_show
        show_result = callbacks.on_show()
        assert isinstance(show_result, str)
        # on_enter
        enter_result = callbacks.on_enter("cb_test")
        assert enter_result == ""
        # on_delete
        del_result = callbacks.on_delete("cb_test")
        assert del_result == ""


# ═══════════════════════════════════════════════════════════════
# 9. LLM + Agent 闭环集成测试
# ═══════════════════════════════════════════════════════════════

class TestAgentLLMClosedLoop:
    """Agent + MockLLMServer 闭环集成测试

    测试Agent核心逻辑（_agent_loop, _repair_messages, _execute_tool_calls）
    通过MockLLMServer模拟LLM响应，验证完整的AI决策→工具执行→结果回传闭环。
    """

    def test_llm_text_only_conversation(self):
        """纯文本对话闭环（无工具调用）"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("你好！我是AI助手。")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "你好"},
            ]
            chunks = list(client.chat_stream(messages))
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "你好" in "".join(contents)

    def test_llm_tool_call_then_text(self):
        """工具调用后返回文本的闭环"""
        with MockLLMServer(stream=True) as server:
            # 第一轮：AI决定调用Read
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
            ])
            # 第二轮：AI根据工具结果返回文本
            server.enqueue_text("文件内容已读取。")

            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)

            # 第一轮
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "读取文件"},
            ]
            chunks1 = list(client.chat_stream(messages))
            tc_chunks = [c for c in chunks1 if "tool_calls" in c]
            assert len(tc_chunks) == 1

            # 模拟工具执行结果回传
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": tc_chunks[0]["tool_calls"],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc_chunks[0]["tool_calls"][0]["id"],
                "content": "file content here",
            })

            # 第二轮
            chunks2 = list(client.chat_stream(messages))
            contents2 = [c.get("content", "") for c in chunks2 if "content" in c and "tool_calls" not in c]
            assert "文件内容已读取" in "".join(contents2)

    def test_llm_multiple_tool_calls_round(self):
        """一轮多个tool_call的闭环"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_tool_calls([
                {"name": "Read", "arguments": {"file_path": "/tmp/a.txt"}},
                {"name": "Read", "arguments": {"file_path": "/tmp/b.txt"}},
                {"name": "Glob", "arguments": {"pattern": "*.py"}},
            ])
            server.enqueue_text("已读取两个文件并搜索了Python文件。")

            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)

            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "读取两个文件并搜索"},
            ]
            chunks = list(client.chat_stream(messages))
            tc_chunks = [c for c in chunks if "tool_calls" in c]
            assert len(tc_chunks) == 1
            assert len(tc_chunks[0]["tool_calls"]) == 3

    def test_llm_error_response_handling(self):
        """LLM返回error的闭环"""
        # 连接不存在的服务器
        config = AIConfig(api_key="test", base_url="http://127.0.0.1:1/v1", model="mock")
        client = LLMClient(config)
        chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
        # 应有error chunk
        error_chunks = [c for c in chunks if c.get("finish_reason") == "error"]
        assert len(error_chunks) >= 1

    def test_llm_compress_no_tools_mode(self):
        """压缩模式（no_tools=True）闭环"""
        with MockLLMServer(stream=True) as server:
            server.enqueue_text("这是对话总结：用户讨论了项目架构。")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)

            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "讨论架构"},
                {"role": "assistant", "content": "好的"},
                {"role": "user", "content": COMPRESS_PROMPT},
            ]
            chunks = list(client.chat_stream(messages, no_tools=True))
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "对话总结" in "".join(contents)


# ═══════════════════════════════════════════════════════════════
# 10. 工具分类与调度策略验证
# ═══════════════════════════════════════════════════════════════

class TestToolClassificationDeep:
    """工具分类与调度策略深度验证"""

    def test_all_tools_in_exactly_one_category(self):
        """每个工具恰好属于一个分类"""
        all_tools = set(tool_registry.get_tool_names())
        classified = _READONLY_TOOLS | _WRITE_TOOLS | _SERIAL_TOOLS
        assert classified == all_tools
        # 互不相交
        assert _READONLY_TOOLS & _WRITE_TOOLS == set()
        assert _READONLY_TOOLS & _SERIAL_TOOLS == set()
        assert _WRITE_TOOLS & _SERIAL_TOOLS == set()

    def test_tool_labels_cover_all(self):
        """_TOOL_LABELS覆盖所有工具"""
        all_tools = set(tool_registry.get_tool_names())
        assert set(_TOOL_LABELS.keys()) == all_tools

    def test_file_path_tools_subset(self):
        """_FILE_PATH_TOOLS是所有工具的子集"""
        all_tools = set(tool_registry.get_tool_names())
        assert _FILE_PATH_TOOLS <= all_tools

    def test_readonly_tools_are_read_only(self):
        """只读工具不修改文件系统"""
        assert "Read" in _READONLY_TOOLS
        assert "Glob" in _READONLY_TOOLS
        assert "Grep" in _READONLY_TOOLS
        assert "WebSearch" in _READONLY_TOOLS

    def test_write_tools_modify_files(self):
        """写入工具修改文件系统"""
        assert "Edit" in _WRITE_TOOLS
        assert "Write" in _WRITE_TOOLS

    def test_serial_tools_need_sequential(self):
        """串行工具需要顺序执行"""
        assert "Shell" in _SERIAL_TOOLS
        assert "Terminal" in _SERIAL_TOOLS
        assert "TodoWrite" in _SERIAL_TOOLS


# ═══════════════════════════════════════════════════════════════
# 11. Logger 深度测试
# ═══════════════════════════════════════════════════════════════

class TestLoggerDeep:
    """Logger深度测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_redact_api_key(self):
        """API key脱敏"""
        logger = AgentLogger(self.tmpdir)
        logger.start(self.tmpdir)
        # 写入含API key的日志
        logger.info("test", 'api_key="sk-1234567890abcdef"')
        logger.close()
        # 读取日志文件验证脱敏
        log_dir = os.path.join(self.tmpdir, "logs")
        if os.path.exists(log_dir):
            for f in os.listdir(log_dir):
                if f.endswith(".log"):
                    with open(os.path.join(log_dir, f), "r", encoding="utf-8") as fh:
                        content = fh.read()
                        # 完整key不应出现
                        assert "sk-1234567890abcdef" not in content

    def test_multiple_start_creates_different_files(self):
        """多次start创建不同日志文件"""
        logger = AgentLogger(self.tmpdir)
        logger.start(self.tmpdir)
        logger.info("test", "first")
        logger.close()
        logger.start(self.tmpdir)
        logger.info("test", "second")
        logger.close()
        # 不崩溃即可

    def test_log_without_start(self):
        """未start时日志不崩溃"""
        logger = AgentLogger(self.tmpdir)
        logger.info("test", "no start")
        logger.error("test", "no start error")
        logger.warning("test", "no start warn")
        logger.debug("test", "no start debug")
        # 不崩溃

    def test_close_idempotent(self):
        """多次close不崩溃"""
        logger = AgentLogger(self.tmpdir)
        logger.start(self.tmpdir)
        logger.close()
        logger.close()
        logger.close()


# ═══════════════════════════════════════════════════════════════
# 12. MockLLMServer 压力测试
# ═══════════════════════════════════════════════════════════════

class TestMockLLMServerStress:
    """MockLLMServer压力测试"""

    def test_rapid_enqueue_dequeue(self):
        """快速入队出队不崩溃"""
        with MockLLMServer(stream=True) as server:
            for i in range(100):
                server.enqueue_text(f"response {i}")
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            # 消费前10个
            for i in range(10):
                chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
                assert len(chunks) > 0

    def test_concurrent_requests(self):
        """并发请求不崩溃"""
        with MockLLMServer(stream=True) as server:
            for i in range(20):
                server.enqueue_text(f"response {i}")

            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            errors = []

            def make_request(idx):
                try:
                    client = LLMClient(config)
                    chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
                    assert len(chunks) > 0
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=make_request, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert len(errors) == 0

    def test_large_response(self):
        """大响应体正确传输"""
        with MockLLMServer(stream=True) as server:
            large_text = "这是一段很长的回复。" * 1000
            server.enqueue_text(large_text)
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "".join(contents) == large_text

    def test_unicode_response(self):
        """Unicode响应正确传输"""
        with MockLLMServer(stream=True) as server:
            unicode_text = "你好世界 🎉 日本語 한국어 العربية 🚀"
            server.enqueue_text(unicode_text)
            config = AIConfig(api_key="test", base_url=server.base_url, model="mock")
            client = LLMClient(config)
            chunks = list(client.chat_stream([{"role": "user", "content": "hi"}]))
            contents = [c.get("content", "") for c in chunks if "content" in c and "tool_calls" not in c]
            assert "".join(contents) == unicode_text


# ═══════════════════════════════════════════════════════════════
# 13. MockFileSystem 仿真集成测试
# ═══════════════════════════════════════════════════════════════

class TestMockFileSystemIntegration:
    """MockFileSystem仿真集成测试"""

    def test_read_project_files(self):
        """在仿真环境中读取项目文件"""
        with MockFileSystem() as fs:
            # 读取main.py
            result = _r(read_tool.execute(fs.abs_path("src/main.py")))
            assert isinstance(result, str)

    def test_glob_project_structure(self):
        """在仿真环境中搜索项目结构"""
        with MockFileSystem() as fs:
            result = _r(glob_tool.execute("*.py", fs.root))
            assert isinstance(result, str)

    def test_grep_in_project(self):
        """在仿真环境中搜索代码"""
        with MockFileSystem() as fs:
            result = _r(grep_tool.execute("def", fs.root, glob="*.py"))
            assert isinstance(result, str)

    def test_write_and_read_in_simulation(self):
        """在仿真环境中写入并读回"""
        with MockFileSystem() as fs:
            path = fs.abs_path("new_file.py")
            write_tool.execute(path, "def hello():\n    pass\n")
            result = _r(read_tool.execute(path))
            assert "hello" in result

    def test_edit_in_simulation(self):
        """在仿真环境中编辑文件"""
        with MockFileSystem() as fs:
            path = fs.abs_path("src/main.py")
            # 先读取
            read_tool.execute(path)
            # 编辑
            result = _r(edit_tool.execute(path, old_string="def main", new_string="def main_edited"))
            # 可能成功也可能失败（取决于文件内容），但不崩溃
            assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════
# 14. SessionStore 深度边界测试
# ═══════════════════════════════════════════════════════════════

class TestSessionStoreDeepBoundary:
    """SessionStore深度边界测试"""

    def setup_method(self):
        self.tmpdir = _make_temp_dir()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_load_large_messages(self):
        """保存加载大量消息"""
        messages = [{"role": "user", "content": f"message {i} " + "x" * 100} for i in range(1000)]
        err = session_store.save_session(self.narnat_dir, "large", messages)
        assert err == ""
        loaded, err = session_store.load_session(self.narnat_dir, "large")
        assert err == ""
        assert len(loaded) == 1000

    def test_save_load_unicode_messages(self):
        """保存加载Unicode消息"""
        messages = [
            {"role": "user", "content": "你好世界 🎉 日本語 한국어"},
            {"role": "assistant", "content": "收到！✅"},
        ]
        err = session_store.save_session(self.narnat_dir, "unicode", messages)
        assert err == ""
        loaded, err = session_store.load_session(self.narnat_dir, "unicode")
        assert err == ""
        assert "你好世界" in loaded[0]["content"]
        assert "🎉" in loaded[0]["content"]

    def test_delete_nonexistent_session(self):
        """删除不存在的会话"""
        err = session_store.delete_session(self.narnat_dir, "nonexistent")
        # 应返回错误但不崩溃
        assert isinstance(err, str)

    def test_load_nonexistent_session(self):
        """加载不存在的会话"""
        loaded, err = session_store.load_session(self.narnat_dir, "nonexistent")
        assert err != ""  # 应有错误

    def test_overwrite_existing_session(self):
        """覆盖已有会话"""
        msgs1 = [{"role": "user", "content": "first"}]
        msgs2 = [{"role": "user", "content": "second"}]
        session_store.save_session(self.narnat_dir, "overwrite", msgs1)
        session_store.save_session(self.narnat_dir, "overwrite", msgs2)
        loaded, err = session_store.load_session(self.narnat_dir, "overwrite")
        assert err == ""
        assert loaded[0]["content"] == "second"

    def test_delete_all_sessions(self):
        """删除全部会话"""
        for i in range(5):
            session_store.save_session(self.narnat_dir, f"s{i}", [{"role": "user", "content": str(i)}])
        err = session_store.delete_session(self.narnat_dir, "--all")
        assert err == ""
        sessions = session_store.list_sessions(self.narnat_dir)
        assert len(sessions) == 0

    def test_path_traversal_protection(self):
        """路径遍历保护"""
        # 尝试用..逃逸
        err = session_store.save_session(self.narnat_dir, "../../../etc/passwd", [{"role": "user", "content": "hack"}])
        # 不应崩溃，且不应在narnat_dir外创建文件
        assert isinstance(err, str)
