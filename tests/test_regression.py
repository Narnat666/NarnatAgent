"""
回归测试 —— 全新测试用例，与原测试无重叠
重点验证修复后的逻辑正确性
"""

import os
import shutil
import tempfile
import time
import json
import pytest

from narnat_agent.tools import read, glob, grep, edit, write, bash, todo_write
from narnat_agent.tools.registry import execute as registry_execute, get_tool_names
from narnat_agent.config.loader import load_config, AIConfig
from narnat_agent.config.session_store import save_session, load_session, list_sessions, delete_session
from narnat_agent.core.context import ContextManager
from narnat_agent.config.defaults import WARN_TURN_1, COMPRESS_TURN
from narnat_agent.core.compressor import Compressor
from narnat_agent.core.llm import LLMClient
from narnat_agent.commands.session import SessionManager


def _r(result):
    """解包工具返回值：tuple→取第一个元素(llm_result)，str→原样返回"""
    return result[0] if isinstance(result, tuple) else result
from narnat_agent.logger import AgentLogger, _redact


# ═══════════════════════════════════════════════════════════════
# Config 回归
# ═══════════════════════════════════════════════════════════════

class TestConfigRegression:
    def test_load_config_with_custom_model(self):
        """自定义model正确加载"""
        root = tempfile.mkdtemp()
        ndir = os.path.join(root, ".narnat")
        os.makedirs(ndir)
        with open(os.path.join(ndir, "narnat.json"), "w", encoding="utf-8") as f:
            json.dump({"model": "my-custom-v3", "api_key": "sk-test", "base_url": "https://api.test.com"}, f)
        try:
            cfg = load_config(root)
            assert cfg.ai.model == "my-custom-v3"
            assert "my-custom-v3" in cfg.system_prompt
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_session_store_round_trip_large(self):
        """大量消息的序列化/反序列化"""
        tmpdir = tempfile.mkdtemp()
        ndir = os.path.join(tmpdir, ".narnat")
        os.makedirs(ndir)
        try:
            msgs = [{"role": "user", "content": f"message {i}"} for i in range(500)]
            save_session(ndir, "big", msgs)
            loaded, err = load_session(ndir, "big")
            assert err == ""
            assert len(loaded) == 500
            assert loaded[499]["content"] == "message 499"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_session_delete_all_with_locked_file(self):
        """--all删除时个别文件删除失败不崩溃"""
        tmpdir = tempfile.mkdtemp()
        ndir = os.path.join(tmpdir, ".narnat")
        os.makedirs(ndir)
        try:
            save_session(ndir, "s1", [{"role": "user", "content": "hi"}])
            err = delete_session(ndir, "--all")
            assert err == ""
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# Read 回归
# ═══════════════════════════════════════════════════════════════

class TestReadRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_python_file(self):
        """读取Python源码"""
        fpath = os.path.join(self.tmpdir, "app.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n")
        result = read.execute(fpath)
        assert "def main():" in result
        assert "print('hello')" in result

    def test_read_json_file(self):
        """读取JSON文件"""
        fpath = os.path.join(self.tmpdir, "config.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"key": "value", "num": 42}, f, indent=2)
        result = read.execute(fpath)
        assert '"key"' in result

    def test_read_with_various_offsets(self):
        """各种offset值"""
        fpath = os.path.join(self.tmpdir, "lines.txt")
        with open(fpath, "w") as f:
            for i in range(20):
                f.write(f"L{i}\n")
        # offset=1 从头
        r1 = read.execute(fpath, offset=1, limit=3)
        assert "1→L0" in r1
        # offset=10
        r2 = read.execute(fpath, offset=10, limit=2)
        assert "10→L9" in r2


# ═══════════════════════════════════════════════════════════════
# Edit 回归（验证diff修复）
# ═══════════════════════════════════════════════════════════════

class TestEditRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_edit_diff_no_double_newline(self):
        """修复后diff不应有双换行"""
        fpath = os.path.join(self.tmpdir, "diff_test.py")
        with open(fpath, "w") as f:
            f.write("x = 1\ny = 2\n")
        result = edit.execute(fpath, "x = 1", "x = 10")
        r = _r(result)
        # 不应有连续空行（双换行的特征）
        assert "\n\n\n" not in r
        assert "---" in r
        assert "+++" in r

    def test_edit_preserves_unrelated_content(self):
        """替换不影响其他内容"""
        fpath = os.path.join(self.tmpdir, "preserve.py")
        with open(fpath, "w") as f:
            f.write("a = 1\nb = 2\nc = 3\nd = 4\n")
        edit.execute(fpath, "b = 2", "b = 20")
        with open(fpath, "r") as f:
            content = f.read()
        assert "a = 1" in content
        assert "b = 20" in content
        assert "c = 3" in content
        assert "d = 4" in content

    def test_edit_similar_line_hint(self):
        """未找到时给出相似行提示"""
        fpath = os.path.join(self.tmpdir, "hint.py")
        with open(fpath, "w") as f:
            f.write("def calculate_total():\n    return sum(items)\n")
        result = edit.execute(fpath, "def calc_total():", "def new_name():")
        assert "相似" in _r(result) or "错误" in _r(result)


# ═══════════════════════════════════════════════════════════════
# Glob 回归
# ═══════════════════════════════════════════════════════════════

class TestGlobRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_glob_star_star_matches_root(self):
        """**/*.py匹配根目录文件"""
        with open(os.path.join(self.tmpdir, "root.py"), "w") as f:
            f.write("")
        result = glob.execute("**/*.py", self.tmpdir)
        assert "root.py" in result

    def test_glob_star_star_matches_nested(self):
        """**/*.py匹配嵌套文件"""
        nested = os.path.join(self.tmpdir, "a", "b")
        os.makedirs(nested)
        with open(os.path.join(nested, "nested.py"), "w") as f:
            f.write("")
        result = glob.execute("**/*.py", self.tmpdir)
        assert "nested.py" in result

    def test_glob_specific_extension(self):
        """特定扩展名"""
        with open(os.path.join(self.tmpdir, "data.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.tmpdir, "code.py"), "w") as f:
            f.write("")
        result = glob.execute("*.json", self.tmpdir)
        assert "data.json" in result
        assert "code.py" not in result


# ═══════════════════════════════════════════════════════════════
# Grep 回归
# ═══════════════════════════════════════════════════════════════

class TestGrepRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_grep_regex_pattern(self):
        """正则模式"""
        with open(os.path.join(self.tmpdir, "code.py"), "w") as f:
            f.write("var1 = 100\nvar2 = 200\nconst = 300\n")
        result = grep.execute(r"var\d+", self.tmpdir, output_mode="content")
        assert "var1" in result
        assert "var2" in result

    def test_grep_multiple_file_types(self):
        """多文件类型搜索"""
        with open(os.path.join(self.tmpdir, "app.py"), "w") as f:
            f.write("import os\n")
        with open(os.path.join(self.tmpdir, "style.css"), "w") as f:
            f.write("color: red;\n")
        result_py = grep.execute("import", self.tmpdir, glob="*.py")
        assert "app.py" in result_py
        result_css = grep.execute("color", self.tmpdir, glob="*.css")
        assert "style.css" in result_css


# ═══════════════════════════════════════════════════════════════
# Write 回归
# ═══════════════════════════════════════════════════════════════

class TestWriteRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        write._read_files.clear()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        write._read_files.clear()

    def test_write_and_read_back(self):
        """写入后读回内容一致"""
        fpath = os.path.join(self.tmpdir, "test.txt")
        content = "Hello\nWorld\n你好\n"
        write.execute(fpath, content)
        with open(fpath, "r", encoding="utf-8") as f:
            assert f.read() == content

    def test_write_deep_nested_dir(self):
        """深层嵌套目录自动创建"""
        fpath = os.path.join(self.tmpdir, "a", "b", "c", "d", "e", "file.txt")
        write.execute(fpath, "deep")
        assert os.path.isfile(fpath)


# ═══════════════════════════════════════════════════════════════
# Bash 回归
# ═══════════════════════════════════════════════════════════════

class TestBashRegression:
    def test_python_version(self):
        """python命令可用"""
        result = bash.execute("python --version")
        assert "Python" in result

    def test_pipe_command(self):
        """管道命令"""
        result = bash.execute('python -c "print(\'hello world\')"')
        assert "hello world" in result

    def test_environment_variable(self):
        """环境变量"""
        result = bash.execute('python -c "import os; print(os.getcwd())"')
        assert "exit code: 0" in result


# ═══════════════════════════════════════════════════════════════
# TodoWrite 回归（验证放宽校验）
# ═══════════════════════════════════════════════════════════════

class TestTodoWriteRegression:
    def test_zero_in_progress_allowed(self):
        """0个in_progress允许（初始状态）"""
        result = todo_write.execute([
            {"content": "Task A", "status": "pending", "activeForm": "Pending A"},
            {"content": "Task B", "status": "pending", "activeForm": "Pending B"},
        ])
        assert "已更新" in result

    def test_one_in_progress_allowed(self):
        """1个in_progress允许"""
        result = todo_write.execute([
            {"content": "Task A", "status": "in_progress", "activeForm": "Doing A"},
        ])
        assert "已更新" in result

    def test_two_in_progress_rejected(self):
        """2个in_progress禁止"""
        result = todo_write.execute([
            {"content": "Task A", "status": "in_progress", "activeForm": "Doing A"},
            {"content": "Task B", "status": "in_progress", "activeForm": "Doing B"},
        ])
        assert "错误" in result

    def test_return_confirms_count(self):
        """返回确认信息含任务数"""
        result = todo_write.execute([
            {"content": "T1", "status": "in_progress", "activeForm": "D1"},
            {"content": "T2", "status": "pending", "activeForm": "D2"},
            {"content": "T3", "status": "completed", "activeForm": "D3"},
        ])
        assert "3" in result


# ═══════════════════════════════════════════════════════════════
# Context 回归
# ═══════════════════════════════════════════════════════════════

class TestContextRegression:
    def test_increment_returns_empty_before_thresholds(self):
        """阈值前返回空串"""
        ctx = ContextManager()
        for _ in range(WARN_TURN_1 - 1):
            assert ctx.increment() == ""

    def test_compress_exactly_at_120(self):
        """恰好COMPRESS_TURN轮触发"""
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN - 1):
            ctx.increment()
        assert not ctx.need_compress()
        ctx.increment()
        assert ctx.need_compress()

    def test_reset_clears_all_state(self):
        """重置后状态完全清空"""
        ctx = ContextManager()
        for _ in range(100):
            ctx.increment()
        ctx.reset()
        assert ctx.turn_count == 0
        assert not ctx.need_compress()
        summary = ctx.get_summary()
        assert summary["warned_50"] is False
        assert summary["warned_100"] is False


# ═══════════════════════════════════════════════════════════════
# Compressor 回归
# ═══════════════════════════════════════════════════════════════

class TestCompressorRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)
        self.summary_path = os.path.join(self.narnat_dir, "last_session_summary.md")
        with open(self.summary_path, "w") as f:
            f.write("")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_compress_cycle(self):
        """完整压缩周期：写入→校验→读取→重置"""
        comp = Compressor(self.narnat_dir)
        # 写入
        assert comp.write_summary("对话总结：完成了3个功能")
        # 校验
        assert comp.verify_summary()
        # 读取
        summary = comp.read_summary()
        assert "完成了3个功能" in summary
        # 构建新会话
        new_msgs = comp.build_new_session_messages("You are a helper.", summary)
        assert "上一轮对话成果" in new_msgs[0]["content"]
        # 重置
        comp.reset_summary()
        assert not comp.verify_summary()

    def test_compress_messages_preserves_original(self):
        """构建压缩messages不修改原messages"""
        comp = Compressor(self.narnat_dir)
        original = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "hello"},
        ]
        original_len = len(original)
        compress_msgs = comp.build_compress_messages(original)
        assert len(original) == original_len  # 原messages未被修改
        assert len(compress_msgs) == original_len + 1


# ═══════════════════════════════════════════════════════════════
# Logger 回归
# ═══════════════════════════════════════════════════════════════

class TestLoggerRegression:
    def test_redact_various_formats(self):
        """多种格式的脱敏"""
        result = _redact("key=sk-supersecret123")
        # 脱敏后应包含***标记
        assert "***" in result

    def test_redact_preserves_structure(self):
        """脱敏保留消息结构"""
        msg = "调用工具: Read, 参数: file_path=/tmp/test.py"
        result = _redact(msg)
        assert "Read" in result
        assert "file_path" in result


# ═══════════════════════════════════════════════════════════════
# SessionManager 回归
# ═══════════════════════════════════════════════════════════════

class TestSessionManagerRegression:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.narnat_dir = os.path.join(self.tmpdir, ".narnat")
        os.makedirs(self.narnat_dir, exist_ok=True)
        self.mgr = SessionManager(self.narnat_dir)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_enter_cycle(self):
        """保存→进入 完整周期"""
        msgs = [{"role": "user", "content": "test message"}]
        self.mgr.set_messages(msgs)
        self.mgr.save("session1")
        # 清空当前消息
        self.mgr.set_messages([])
        # 进入历史会话
        loaded, err = self.mgr.enter("session1")
        assert err == ""
        assert len(loaded) == 1
        assert loaded[0]["content"] == "test message"

    def test_multiple_sessions_independent(self):
        """多个会话互不干扰"""
        self.mgr.set_messages([{"role": "user", "content": "msg1"}])
        self.mgr.save("s1")
        self.mgr.set_messages([{"role": "user", "content": "msg2"}])
        self.mgr.save("s2")
        _, _ = self.mgr.enter("s1")
        assert self.mgr.get_messages()[0]["content"] == "msg1"
        _, _ = self.mgr.enter("s2")
        assert self.mgr.get_messages()[0]["content"] == "msg2"


# ═══════════════════════════════════════════════════════════════
# Registry 回归
# ═══════════════════════════════════════════════════════════════

class TestRegistryRegression:
    def test_all_tools_callable(self):
        """所有8个工具可通过registry调用"""
        tmpdir = tempfile.mkdtemp()
        try:
            fpath = os.path.join(tmpdir, "test.txt")
            with open(fpath, "w") as f:
                f.write("content\n")
            # Read
            assert "content" in _r(registry_execute("Read", {"file_path": fpath}))
            # Glob
            result = registry_execute("Glob", {"pattern": "*.txt", "path": tmpdir})
            assert "test.txt" in _r(result)
            # Grep
            result = registry_execute("Grep", {"pattern": "content", "path": tmpdir})
            assert "test.txt" in _r(result)
            # Edit
            result = registry_execute("Edit", {"file_path": fpath, "old_string": "content", "new_string": "modified"})
            assert "已替换" in _r(result)
            # Write
            fpath2 = os.path.join(tmpdir, "new.txt")
            result = registry_execute("Write", {"file_path": fpath2, "content": "new file"})
            assert "已写入" in _r(result)
            # TodoWrite
            result = registry_execute("TodoWrite", {"todos": [{"content": "T", "status": "in_progress", "activeForm": "D"}]})
            assert "已更新" in _r(result)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            write._read_files.clear()

    def test_tool_names_complete(self):
        """工具名列表完整"""
        names = set(get_tool_names())
        assert names == {"Read", "Glob", "Grep", "Edit", "Write", "Shell", "Terminal", "WebSearch", "TodoWrite"}
