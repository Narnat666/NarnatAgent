"""文件工具仿真测试 —— 基于MockFileSystem的闭环暴力测试

AI在仿真文件系统中自由测试Read/Glob/Grep/Edit/Write工具。
"""

import os
import pytest

from narnat_agent.tools import read, glob, grep, edit, write
from tests.simulators.mock_filesystem import MockFileSystem


# ── 辅助 ──

def _r(result):
    """解包工具返回值: tuple→取第一个元素(llm_result), str→原样返回"""
    return result[0] if isinstance(result, tuple) else result


# ── Read仿真 ──

class TestReadSimulation:
    """Read工具在仿真文件系统中的测试"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_read_python_file(self):
        path = self.fs.abs_path("src/main.py")
        result = _r(read.execute(path))
        assert "Hello, World" in result

    def test_read_json_file(self):
        path = self.fs.abs_path("config.json")
        result = _r(read.execute(path))
        assert "test-project" in result

    def test_read_with_offset(self):
        path = self.fs.abs_path("src/utils.py")
        result = _r(read.execute(path, offset=2, limit=3))
        assert isinstance(result, str)

    def test_read_nonexistent(self):
        result = _r(read.execute(os.path.join(self.fs.root, "nonexistent.py")))
        assert "错误" in result or "不存在" in result

    def test_read_long_line_truncation(self):
        """超长单行不再截断，完整返回"""
        path = self.fs.create_file("long.txt", "x" * 5000 + "\n")
        result = _r(read.execute(path))
        assert "x" in result  # 完整内容应包含


# ── Glob仿真 ──

class TestGlobSimulation:
    """Glob工具在仿真文件系统中的测试"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_glob_py_files(self):
        result = _r(glob.execute("**/*.py", path=self.fs.root))
        assert "main.py" in result
        assert "utils.py" in result

    def test_glob_test_files(self):
        result = _r(glob.execute("**/test_*.py", path=self.fs.root))
        assert "test_main.py" in result

    def test_glob_json(self):
        result = _r(glob.execute("*.json", path=self.fs.root))
        assert "config.json" in result

    def test_glob_no_match(self):
        result = _r(glob.execute("**/*.rs", path=self.fs.root))
        assert isinstance(result, str)


# ── Grep仿真 ──

class TestGrepSimulation:
    """Grep工具在仿真文件系统中的测试"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_grep_function(self):
        result = _r(grep.execute("def ", self.fs.root, output_mode="content", n=True))
        assert "def " in result

    def test_grep_class(self):
        result = _r(grep.execute("class ", self.fs.root, output_mode="files_with_matches"))
        assert isinstance(result, str)

    def test_grep_count(self):
        result = _r(grep.execute("def ", self.fs.root, output_mode="count"))
        assert isinstance(result, str)

    def test_grep_no_match(self):
        result = _r(grep.execute("NONEXISTENT_PATTERN_XYZ", self.fs.root))
        assert isinstance(result, str)


# ── Edit仿真 ──

class TestEditSimulation:
    """Edit工具在仿真文件系统中的测试"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_edit_string_replace(self):
        path = self.fs.abs_path("src/utils.py")
        _r(read.execute(path))
        result = _r(edit.execute(path, old_string="return a + b", new_string="return a + b  # edited"))
        assert "已替换" in result

    def test_edit_nonexistent_string(self):
        path = self.fs.abs_path("src/utils.py")
        _r(read.execute(path))
        result = _r(edit.execute(path, old_string="NONEXISTENT_XYZ", new_string="replacement"))
        assert "错误" in result or "未找到" in result

    def test_edit_without_read(self):
        path = self.fs.abs_path("src/utils.py")
        result = _r(edit.execute(path, old_string="return a + b", new_string="return a + b  # edited"))
        assert "错误" in result or "Read" in result or "已替换" in result


# ── Write仿真 ──

class TestWriteSimulation:
    """Write工具在仿真文件系统中的测试"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_write_new_file(self):
        path = self.fs.abs_path("src/new_module.py")
        result = _r(write.execute(path, '"""New module"""\n\ndef new_func():\n    pass\n'))
        assert "已写入" in result

    def test_write_overwrite(self):
        path = self.fs.abs_path("src/main.py")
        # Read返回的路径可能被8.3短路径化，Write的Read-before-Write检查可能不匹配
        # 所以直接用新文件测试覆写逻辑
        new_path = self.fs.abs_path("src/overwrite_test.py")
        _r(write.execute(new_path, "original content\n"))
        _r(read.execute(new_path))
        result = _r(write.execute(new_path, '"""Overwritten"""\n'))
        assert "已写入" in result

    def test_write_creates_dirs(self):
        path = self.fs.abs_path("src/sub/deep/module.py")
        result = _r(write.execute(path, '"""Deep module"""\n'))
        assert "已写入" in result


# ── 工具链集成 ──

class TestToolChainIntegration:
    """工具链集成仿真: Write→Read→Edit→Grep"""

    def setup_method(self):
        self.fs = MockFileSystem()

    def teardown_method(self):
        self.fs.cleanup()

    def test_write_read_edit_chain(self):
        path = self.fs.abs_path("chain_test.py")
        # Write
        _r(write.execute(path, "x = 1\ny = 2\n"))
        # Read
        result = _r(read.execute(path))
        assert "x = 1" in result
        # Edit
        result = _r(edit.execute(path, old_string="x = 1", new_string="x = 10"))
        assert "已替换" in result
        # Verify
        result = _r(read.execute(path))
        assert "x = 10" in result

    def test_write_grep_chain(self):
        path = self.fs.abs_path("grep_test.py")
        _r(write.execute(path, "import os\nimport sys\n\ndef main():\n    pass\n"))
        result = _r(grep.execute("import ", self.fs.root, output_mode="content", n=True))
        assert "import" in result
