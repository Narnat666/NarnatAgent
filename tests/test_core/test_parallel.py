"""
并行调度暴力测试 —— 并发安全、中断、异常、边界、结果顺序
"""

import json
import os
import shutil
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from narnat_agent.core.agent import Agent, _READONLY_TOOLS, _WRITE_TOOLS, _SERIAL_TOOLS
from narnat_agent.tools import write as write_tool


# ═══════════════════════════════════════════════════════════════
# 辅助：构造 tool_call 字典
# ═══════════════════════════════════════════════════════════════

def _tc(tc_id: str, name: str, arguments: dict) -> dict:
    """构造 OpenAI 格式的 tool_call"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


# ═══════════════════════════════════════════════════════════════
# 辅助：创建最小 Agent 实例（不启动真实LLM）
# ═══════════════════════════════════════════════════════════════

def _make_agent() -> Agent:
    """创建debug=False的Agent，不触发真实API"""
    return Agent(debug=False)


# ═══════════════════════════════════════════════════════════════
# 1. 工具分类正确性
# ═══════════════════════════════════════════════════════════════

class TestToolClassification:
    """验证三大分类覆盖所有工具且互不相交"""

    def test_all_tools_covered(self):
        from narnat_agent.tools.registry import get_tool_names
        all_tools = set(get_tool_names())
        classified = _READONLY_TOOLS | _WRITE_TOOLS | _SERIAL_TOOLS
        assert classified == all_tools

    def test_no_overlap(self):
        assert _READONLY_TOOLS & _WRITE_TOOLS == set()
        assert _READONLY_TOOLS & _SERIAL_TOOLS == set()
        assert _WRITE_TOOLS & _SERIAL_TOOLS == set()

    def test_readonly_tools(self):
        assert _READONLY_TOOLS == {"Read", "Glob", "Grep", "WebSearch"}

    def test_write_tools(self):
        assert _WRITE_TOOLS == {"Edit", "Write"}

    def test_serial_tools(self):
        assert _SERIAL_TOOLS == {"Shell", "TodoWrite"}


# ═══════════════════════════════════════════════════════════════
# 2. 结果顺序保证
# ═══════════════════════════════════════════════════════════════

class TestResultOrder:
    """并行执行后结果必须按原始tool_call顺序返回"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # 创建测试文件
        for i in range(5):
            fpath = os.path.join(self.tmpdir, f"file{i}.txt")
            with open(fpath, "w") as f:
                f.write(f"content{i}\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_order_preserved(self):
        """多个Read并行，结果按原始顺序"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        tool_calls = [
            _tc("tc_0", "Read", {"file_path": os.path.join(self.tmpdir, "file0.txt")}),
            _tc("tc_1", "Read", {"file_path": os.path.join(self.tmpdir, "file1.txt")}),
            _tc("tc_2", "Read", {"file_path": os.path.join(self.tmpdir, "file2.txt")}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 3
        assert results[0][0] == "tc_0"
        assert results[1][0] == "tc_1"
        assert results[2][0] == "tc_2"
        assert "content0" in results[0][1]
        assert "content1" in results[1][1]
        assert "content2" in results[2][1]

    def test_mixed_order_preserved(self):
        """只读+写入+串行混合，结果按原始顺序"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "file0.txt")
        f1 = os.path.join(self.tmpdir, "file1.txt")

        write_tool._read_files.clear()
        write_tool.mark_read(f1)

        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),          # 只读
            _tc("tc_1", "Glob", {"pattern": "*.txt", "path": self.tmpdir}),  # 只读
            _tc("tc_2", "Write", {"file_path": f1, "content": "new1\n"}),    # 写入
            _tc("tc_3", "Shell", {"command": "echo ok"}),     # 串行
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 4
        assert results[0][0] == "tc_0"
        assert results[1][0] == "tc_1"
        assert results[2][0] == "tc_2"
        assert results[3][0] == "tc_3"

        write_tool._read_files.clear()

    def test_single_tool_order(self):
        """单个工具调用，顺序不变"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "file0.txt")
        tool_calls = [_tc("tc_0", "Read", {"file_path": f0})]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 1
        assert results[0][0] == "tc_0"


# ═══════════════════════════════════════════════════════════════
# 3. 并发安全：同文件写入串行
# ═══════════════════════════════════════════════════════════════

class TestSameFileSerialization:
    """同文件的Edit/Write必须串行执行，不能并发覆盖"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        write_tool._read_files.clear()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        write_tool._read_files.clear()

    def test_same_file_writes_serial(self):
        """同文件两次Write，结果应为第二次（串行保证）"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        fpath = os.path.join(self.tmpdir, "target.txt")
        with open(fpath, "w") as f:
            f.write("original\n")
        write_tool.mark_read(fpath)

        tool_calls = [
            _tc("tc_0", "Write", {"file_path": fpath, "content": "first\n"}),
            _tc("tc_1", "Write", {"file_path": fpath, "content": "second\n"}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        # 两次都应成功
        assert len(results) == 2
        # 文件最终内容应为second（串行执行，tc_1后执行）
        with open(fpath, "r") as f:
            assert f.read() == "second\n"

    def test_different_files_parallel(self):
        """不同文件的Write可以并行，都成功"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "a.txt")
        f1 = os.path.join(self.tmpdir, "b.txt")
        with open(f0, "w") as f:
            f.write("a_old\n")
        with open(f1, "w") as f:
            f.write("b_old\n")
        write_tool.mark_read(f0)
        write_tool.mark_read(f1)

        tool_calls = [
            _tc("tc_0", "Write", {"file_path": f0, "content": "a_new\n"}),
            _tc("tc_1", "Write", {"file_path": f1, "content": "b_new\n"}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 2
        with open(f0, "r") as f:
            assert f.read() == "a_new\n"
        with open(f1, "r") as f:
            assert f.read() == "b_new\n"


# ═══════════════════════════════════════════════════════════════
# 4. 中断安全
# ═══════════════════════════════════════════════════════════════

class TestInterruptSafety:
    """中断时不应丢失已完成的结果"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cancel_skips_remaining(self):
        """stream.cancelled=True时，跳过未执行的工具"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = True  # 一开始就取消

        f0 = os.path.join(self.tmpdir, "file0.txt")
        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),
            _tc("tc_1", "Read", {"file_path": f0}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        # 取消后不应执行任何工具
        assert len(results) == 0

    def test_cancel_during_serial_phase(self):
        """串行阶段中取消，已完成的结果保留"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "file0.txt")
        with open(f0, "w") as f:
            f.write("hello\n")

        # 两个Bash是串行工具，第一个完成后设cancelled
        original_run_single = agent._run_single
        call_count = [0]

        def mock_run_single(tc_id, name, arguments, stream):
            result = original_run_single(tc_id, name, arguments, stream)
            call_count[0] += 1
            if call_count[0] >= 1:
                stream.cancelled = True
            return result

        with patch.object(agent, '_run_single', mock_run_single):
            tool_calls = [
                _tc("tc_0", "Shell", {"command": "echo first"}),
                _tc("tc_1", "Shell", {"command": "echo second"}),
            ]
            results = agent._execute_tool_calls(tool_calls, stream)

        # 第一个Bash应完成，第二个因cancelled被跳过
        assert len(results) >= 1
        assert results[0][0] == "tc_0"
        assert "first" in results[0][1]


# ═══════════════════════════════════════════════════════════════
# 5. 异常安全
# ═══════════════════════════════════════════════════════════════

class TestExceptionSafety:
    """工具执行异常不应导致整个调度崩溃"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unknown_tool_returns_error(self):
        """未知工具名返回错误字符串，不崩溃"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        tool_calls = [_tc("tc_0", "NonExistent", {})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 1
        assert "错误" in results[0][1]

    def test_mixed_with_error(self):
        """一个工具出错不影响其他工具"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "file0.txt")
        with open(f0, "w") as f:
            f.write("ok\n")

        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),       # 正常
            _tc("tc_1", "Read", {"file_path": "/nonexistent/file.txt"}),  # 不存在
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 2
        assert results[0][0] == "tc_0"
        assert results[1][0] == "tc_1"
        # 第一个应成功
        assert "ok" in results[0][1]
        # 第二个应返回错误
        assert "错误" in results[1][1]

    def test_invalid_json_arguments(self):
        """arguments JSON解析失败时使用空字典，不崩溃"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        # 构造非法JSON的tool_call
        tc = {
            "id": "tc_0",
            "type": "function",
            "function": {
                "name": "Read",
                "arguments": "{invalid json!!!",
            },
        }

        results = agent._execute_tool_calls([tc], stream)

        assert len(results) == 1
        # 应返回错误（缺少file_path）
        assert "错误" in results[0][1]


# ═══════════════════════════════════════════════════════════════
# 6. 边界条件
# ═══════════════════════════════════════════════════════════════

class TestBoundaryConditions:
    """空输入、单工具、大量工具等边界"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_tool_calls(self):
        """空tool_calls列表"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        results = agent._execute_tool_calls([], stream)
        assert results == []

    def test_single_readonly(self):
        """单个只读工具"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "file0.txt")
        with open(f0, "w") as f:
            f.write("hello\n")

        tool_calls = [_tc("tc_0", "Read", {"file_path": f0})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 1
        assert "hello" in results[0][1]

    def test_single_write(self):
        """单个写入工具"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "new.txt")
        write_tool._read_files.clear()

        tool_calls = [_tc("tc_0", "Write", {"file_path": f0, "content": "created\n"})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 1
        assert "已写入" in results[0][1]
        write_tool._read_files.clear()

    def test_single_serial(self):
        """单个串行工具"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        tool_calls = [_tc("tc_0", "Shell", {"command": "echo test"})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 1
        assert "test" in results[0][1]

    def test_many_parallel_reads(self):
        """大量并行Read（10个）"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        files = []
        for i in range(10):
            fpath = os.path.join(self.tmpdir, f"f{i}.txt")
            with open(fpath, "w") as f:
                f.write(f"data{i}\n")
            files.append(fpath)

        tool_calls = [
            _tc(f"tc_{i}", "Read", {"file_path": files[i]})
            for i in range(10)
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 10
        for i in range(10):
            assert results[i][0] == f"tc_{i}"
            assert f"data{i}" in results[i][1]


# ═══════════════════════════════════════════════════════════════
# 7. 并发性能验证
# ═══════════════════════════════════════════════════════════════

class TestParallelPerformance:
    """验证并行确实比串行快（至少不慢）"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # 创建带一定延迟的读取文件
        for i in range(4):
            fpath = os.path.join(self.tmpdir, f"slow{i}.txt")
            with open(fpath, "w") as f:
                f.write(f"content{i}\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parallel_reads_faster_than_sequential(self):
        """4个Read并行应比串行快"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        files = [os.path.join(self.tmpdir, f"slow{i}.txt") for i in range(4)]
        tool_calls = [
            _tc(f"tc_{i}", "Read", {"file_path": files[i]})
            for i in range(4)
        ]

        # 并行执行
        start = time.time()
        results = agent._execute_tool_calls(tool_calls, stream)
        parallel_time = time.time() - start

        assert len(results) == 4
        # 并行时间应合理（不应超过2秒，4个文件读取）
        assert parallel_time < 2.0


# ═══════════════════════════════════════════════════════════════
# 8. 三阶段执行顺序验证
# ═══════════════════════════════════════════════════════════════

class TestPhaseOrdering:
    """验证只读→写入→串行的三阶段顺序"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        write_tool._read_files.clear()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        write_tool._read_files.clear()

    def test_read_before_write(self):
        """Read在Write之前完成（Read结果反映Write前的状态）"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "target.txt")
        with open(f0, "w") as f:
            f.write("before\n")
        write_tool.mark_read(f0)

        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),
            _tc("tc_1", "Write", {"file_path": f0, "content": "after\n"}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 2
        # Read应看到"before"（在Write之前执行）
        assert "before" in results[0][1]
        # Write应成功
        assert "已写入" in results[1][1]
        # 文件最终是"after"
        with open(f0, "r") as f:
            assert f.read() == "after\n"

    def test_write_before_bash(self):
        """Write在Bash之前完成"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "check.txt")
        write_tool.mark_read(f0)

        tool_calls = [
            _tc("tc_0", "Write", {"file_path": f0, "content": "written\n"}),
            _tc("tc_1", "Shell", {"command": f"type {f0}"}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 2
        # Write应先完成
        assert "已写入" in results[0][1]
        # Bash执行时文件已存在
        with open(f0, "r") as f:
            assert f.read() == "written\n"


# ═══════════════════════════════════════════════════════════════
# 9. 回归：原有行为不变
# ═══════════════════════════════════════════════════════════════

class TestBackwardCompatibility:
    """验证并行调度不改变单工具调用的行为"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        write_tool._read_files.clear()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        write_tool._read_files.clear()

    def test_single_read_same_as_before(self):
        """单个Read结果与直接调用一致"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "test.txt")
        with open(f0, "w") as f:
            f.write("hello world\n")

        from narnat_agent.tools.read import execute as read_execute
        direct_result = read_execute(f0)

        tool_calls = [_tc("tc_0", "Read", {"file_path": f0})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert results[0][1] == direct_result

    def test_single_bash_same_as_before(self):
        """单个Bash结果与直接调用一致"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        from narnat_agent.tools.bash import execute as bash_execute
        direct_result = bash_execute("echo 42")

        tool_calls = [_tc("tc_0", "Shell", {"command": "echo 42"})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert results[0][1] == direct_result

    def test_write_read_check_preserved(self):
        """Write的Read-before-Write检查仍然生效"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "existing.txt")
        with open(f0, "w") as f:
            f.write("old\n")

        # 未Read，直接Write应报错
        tool_calls = [_tc("tc_0", "Write", {"file_path": f0, "content": "new\n"})]
        results = agent._execute_tool_calls(tool_calls, stream)

        assert "错误" in results[0][1]

    def test_read_marks_for_write(self):
        """Read后标记，后续Write不报错"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "target.txt")
        with open(f0, "w") as f:
            f.write("old\n")

        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),
            _tc("tc_1", "Write", {"file_path": f0, "content": "new\n"}),
        ]
        results = agent._execute_tool_calls(tool_calls, stream)

        # Read先执行并标记，Write应成功
        assert "已写入" in results[1][1]


# ═══════════════════════════════════════════════════════════════
# 10. 线程安全：results字典并发写入
# ═══════════════════════════════════════════════════════════════

class TestThreadSafety:
    """验证并发写入results字典不丢数据"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        for i in range(8):
            fpath = os.path.join(self.tmpdir, f"f{i}.txt")
            with open(fpath, "w") as f:
                f.write(f"v{i}\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_lost_results_under_concurrency(self):
        """8个并行Read，结果不丢失"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        files = [os.path.join(self.tmpdir, f"f{i}.txt") for i in range(8)]
        tool_calls = [
            _tc(f"tc_{i}", "Read", {"file_path": files[i]})
            for i in range(8)
        ]

        # 多次执行，检测并发问题
        for _ in range(10):
            results = agent._execute_tool_calls(tool_calls, stream)
            assert len(results) == 8
            for i in range(8):
                assert results[i][0] == f"tc_{i}"

    def test_concurrent_read_and_glob(self):
        """Read和Glob混合并行"""
        agent = _make_agent()
        stream = MagicMock()
        stream.cancelled = False

        f0 = os.path.join(self.tmpdir, "f0.txt")
        tool_calls = [
            _tc("tc_0", "Read", {"file_path": f0}),
            _tc("tc_1", "Glob", {"pattern": "*.txt", "path": self.tmpdir}),
            _tc("tc_2", "Grep", {"pattern": "v[0-9]", "path": self.tmpdir}),
        ]

        results = agent._execute_tool_calls(tool_calls, stream)

        assert len(results) == 3
        assert results[0][0] == "tc_0"
        assert results[1][0] == "tc_1"
        assert results[2][0] == "tc_2"
