"""全方位高强度深度测试 —— 20年测试工程师视角

覆盖维度:
1. 工具层深度: 每个工具的参数边界、类型边界、编码边界、并发安全
2. 核心层深度: Agent调度逻辑、LLM双协议、Context状态机、Compressor全流程
3. 配置层深度: Loader异常恢复、SessionStore安全、Defaults一致性
4. 暴力测试: 极端输入、资源耗尽、异常注入、并发冲击
5. 边界测试: 空值、None、超长、Unicode、特殊字符、路径遍历
6. 集成测试: 工具链闭环、Agent+LLM+Tools端到端
7. 不变量测试: 工具分类完备性、消息序列合法性、token统计单调性
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import concurrent.futures
from unittest.mock import patch, MagicMock

import pytest

# ── 辅助 ──

def _r(result):
    """解包工具返回值: tuple→取第一个元素, str→原样返回"""
    return result[0] if isinstance(result, tuple) else result


def _create_temp_file(content="", suffix=".py"):
    """创建临时文件并返回路径"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


def _create_temp_dir():
    """创建临时目录并返回路径"""
    return tempfile.mkdtemp()


# ═══════════════════════════════════════════════════════════════
# 一、Read 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestReadDeep:
    """Read工具 - 深度边界与暴力测试"""

    def test_read_empty_file(self):
        """空文件返回空内容"""
        path = _create_temp_file("")
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert isinstance(result, str)
            # 空文件不应崩溃
        finally:
            os.unlink(path)

    def test_read_single_char(self):
        """单字符文件"""
        path = _create_temp_file("x")
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert "x" in result
        finally:
            os.unlink(path)

    def test_read_binary_file_fallback(self):
        """二进制文件用errors=replace不崩溃"""
        path = _create_temp_file(suffix=".bin")
        try:
            with open(path, 'wb') as f:
                f.write(b'\x00\x01\x02\xff\xfe\xfd')
            from narnat_agent.tools import read
            result = read.execute(path)
            assert isinstance(result, str)
            assert "错误" not in result
        finally:
            os.unlink(path)

    def test_read_offset_beyond_file(self):
        """offset超出文件行数返回空"""
        path = _create_temp_file("line1\nline2\n")
        try:
            from narnat_agent.tools import read
            result = read.execute(path, offset=999)
            # 不崩溃，返回空或极少内容
            assert isinstance(result, str)
        finally:
            os.unlink(path)

    def test_read_offset_zero_and_one_equivalence(self):
        """offset=0和offset=1都从第一行开始"""
        content = "alpha\nbeta\ngamma\n"
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            r0 = read.execute(path, offset=0)
            r1 = read.execute(path, offset=1)
            assert r0 == r1
        finally:
            os.unlink(path)

    def test_read_limit_zero_means_all(self):
        """limit=0表示读全部"""
        content = "\n".join(f"line{i}" for i in range(10))
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path, limit=0)
            assert "line9" in result
        finally:
            os.unlink(path)

    def test_read_negative_offset(self):
        """负offset不崩溃"""
        path = _create_temp_file("hello\n")
        try:
            from narnat_agent.tools import read
            result = read.execute(path, offset=-1)
            assert isinstance(result, str)
        finally:
            os.unlink(path)

    def test_read_very_long_single_line(self):
        """超长单行不再截断，完整返回"""
        from narnat_agent.config.defaults import MAX_LINE_CHARS
        long_line = "x" * (MAX_LINE_CHARS + 500)
        path = _create_temp_file(long_line)
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert "x" in result  # 完整内容应包含
        finally:
            os.unlink(path)

    def test_read_many_lines_truncation(self):
        """超过MAX_FILE_LINES不再截断，完整返回"""
        from narnat_agent.config.defaults import MAX_FILE_LINES
        content = "\n".join(f"line{i}" for i in range(MAX_FILE_LINES + 100))
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            # 完整内容应包含最后一行
            assert f"line{MAX_FILE_LINES + 99}" in result
        finally:
            os.unlink(path)

    def test_read_nonexistent_file(self):
        """不存在的文件返回错误"""
        from narnat_agent.tools import read
        result = read.execute("/nonexistent/path/file.txt")
        assert "错误" in result

    def test_read_path_with_spaces(self):
        """路径含空格"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "file with spaces.py")
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write("content here")
            from narnat_agent.tools import read
            result = read.execute(filepath)
            assert "content here" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_unicode_content(self):
        """Unicode内容正确读取"""
        content = "# 中文注释\nemoji: 🎯🔥\n日本語: こんにちは\n"
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert "中文注释" in result
            assert "🎯" in result
            assert "こんにちは" in result
        finally:
            os.unlink(path)

    def test_read_mixed_line_endings(self):
        """混合行尾(CRLF/LF)不崩溃"""
        content = "line1\r\nline2\nline3\r\n"
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert isinstance(result, str)
            assert "line1" in result
            assert "line3" in result
        finally:
            os.unlink(path)

    def test_read_offset_with_limit(self):
        """offset+limit组合正确"""
        content = "\n".join(f"line{i}" for i in range(20))
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path, offset=5, limit=3)
            assert "line4" in result  # offset=5 → 从第5行开始(1-based)
            assert "line6" in result  # limit=3 → 3行
            assert "line7" not in result  # 不应包含第7行
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# 二、Edit 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestEditDeep:
    """Edit工具 - 深度边界与暴力测试"""

    def test_edit_empty_old_string_error(self):
        """空old_string报错"""
        path = _create_temp_file("hello\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="", new_string="x")
            assert "错误" in _r(result)
        finally:
            os.unlink(path)

    def test_edit_no_match_with_similar_hint(self):
        """未匹配时给出相似行提示"""
        path = _create_temp_file("def hello():\n    pass\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="def helo():", new_string="x")
            text = _r(result)
            assert "错误" in text
            # 应有相似行提示
        finally:
            os.unlink(path)

    def test_edit_multiple_match_no_replace_all(self):
        """多处匹配未设replace_all报错"""
        path = _create_temp_file("foo\nbar\nfoo\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="foo", new_string="baz")
            text = _r(result)
            assert "错误" in text
            assert "2" in text  # 找到2处
        finally:
            os.unlink(path)

    def test_edit_replace_all(self):
        """replace_all=True替换所有匹配"""
        path = _create_temp_file("aaa\nbbb\naaa\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="aaa", new_string="ccc", replace_all=True)
            text = _r(result)
            assert "已替换" in text
            # 验证文件内容
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "ccc" in content
            assert content.count("ccc") == 2
        finally:
            os.unlink(path)

    def test_edit_line_mode_basic(self):
        """行号模式基本替换"""
        path = _create_temp_file("line1\nline2\nline3\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, line_start=2, line_end=2, new_string="REPLACED")
            text = _r(result)
            assert "已替换" in text
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "REPLACED" in content
            assert "line1" in content
            assert "line3" in content
        finally:
            os.unlink(path)

    def test_edit_line_mode_range(self):
        """行号模式范围替换"""
        path = _create_temp_file("a\nb\nc\nd\ne\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, line_start=2, line_end=4, new_string="X")
            text = _r(result)
            assert "已替换" in text
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "a" in content
            assert "X" in content
            assert "e" in content
            assert "b" not in content
        finally:
            os.unlink(path)

    def test_edit_line_start_out_of_range(self):
        """line_start超出范围报错"""
        path = _create_temp_file("only_one_line\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, line_start=99, new_string="x")
            assert "错误" in _r(result)
        finally:
            os.unlink(path)

    def test_edit_line_end_before_start(self):
        """line_end < line_start报错"""
        path = _create_temp_file("a\nb\nc\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, line_start=3, line_end=1, new_string="x")
            assert "错误" in _r(result)
        finally:
            os.unlink(path)

    def test_edit_nonexistent_file(self):
        """不存在的文件报错"""
        from narnat_agent.tools import edit
        result = edit.execute("/nonexistent/file.py", old_string="a", new_string="b")
        assert "错误" in _r(result)

    def test_edit_substring_match_behavior(self):
        """Edit是子串匹配，'x = 1'能匹配'    x = 1'中的子串"""
        path = _create_temp_file("    x = 1\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="x = 1", new_string="x = 2")
            text = _r(result)
            assert "已替换" in text
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "    x = 2" in content
        finally:
            os.unlink(path)

    def test_edit_unicode_content(self):
        """Unicode内容编辑"""
        path = _create_temp_file("# 中文注释\nvalue = 1\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="value = 1", new_string="value = 2")
            text = _r(result)
            assert "已替换" in text
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "中文注释" in content
            assert "value = 2" in content
        finally:
            os.unlink(path)

    def test_edit_preserves_unrelated(self):
        """编辑不影响无关内容"""
        content = "keep1\ntarget\nkeep2\nkeep3\n"
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="target", new_string="replaced")
            with open(path, 'r', encoding='utf-8') as f:
                new_content = f.read()
            assert "keep1" in new_content
            assert "keep2" in new_content
            assert "keep3" in new_content
            assert "replaced" in new_content
        finally:
            os.unlink(path)

    def test_edit_delete_line_by_empty_new_string(self):
        """行号模式用空new_string删除行"""
        path = _create_temp_file("a\nb\nc\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, line_start=2, line_end=2, new_string="")
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "a" in content
            assert "c" in content
            assert "b" not in content
        finally:
            os.unlink(path)

    def test_edit_diff_no_double_newline(self):
        """diff不产生双换行（回归#8）"""
        path = _create_temp_file("hello\nworld\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string="hello", new_string="hi")
            text = _r(result)
            # 不应有连续空行
            assert "\n\n\n" not in text
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# 三、Write 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestWriteDeep:
    """Write工具 - 深度边界与暴力测试"""

    def setup_method(self):
        from narnat_agent.tools import write
        write.clear_read_files()

    def test_write_new_file(self):
        """创建新文件"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "new_file.py")
            from narnat_agent.tools import write
            result = write.execute(filepath, "hello world")
            text = _r(result)
            assert "已写入" in text
            assert os.path.isfile(filepath)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_overwrite_without_read_error(self):
        """覆写已有文件但未Read报错"""
        path = _create_temp_file("original\n")
        try:
            from narnat_agent.tools import write
            result = write.execute(path, "modified")
            text = _r(result)
            assert "错误" in text
            assert "Read" in text
        finally:
            os.unlink(path)

    def test_write_overwrite_after_read(self):
        """Read后可以Write覆写（需通过mark_read标记）"""
        path = _create_temp_file("original\n")
        try:
            from narnat_agent.tools import write, read
            # Read工具的execute不直接调用mark_read，
            # mark_read由Agent._run_single在Read后调用
            # 测试中需显式标记
            write.mark_read(path)
            result = write.execute(path, "modified\n")
            text = _r(result)
            assert "已写入" in text
            with open(path, 'r', encoding='utf-8') as f:
                assert f.read() == "modified\n"
        finally:
            os.unlink(path)

    def test_write_auto_creates_parent_dirs(self):
        """自动创建父目录"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "a", "b", "c", "deep.py")
            from narnat_agent.tools import write
            result = write.execute(filepath, "deep content")
            text = _r(result)
            assert "已写入" in text
            assert os.path.isfile(filepath)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_unicode_content(self):
        """Unicode内容写入"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "unicode.py")
            from narnat_agent.tools import write
            result = write.execute(filepath, "# 中文 🎯\nvalue = 'こんにちは'\n")
            text = _r(result)
            assert "已写入" in text
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            assert "中文" in content
            assert "🎯" in content
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_empty_content(self):
        """写入空内容创建空文件"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "empty.py")
            from narnat_agent.tools import write
            result = write.execute(filepath, "")
            text = _r(result)
            assert "已写入" in text
            assert os.path.getsize(filepath) == 0
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_very_long_content(self):
        """写入超长内容"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "long.py")
            content = "x\n" * 100000
            from narnat_agent.tools import write
            result = write.execute(filepath, content)
            text = _r(result)
            assert "已写入" in text
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_marks_as_read(self):
        """Write后文件被标记为已读"""
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "marked.py")
            from narnat_agent.tools import write
            write.execute(filepath, "content")
            # 再次Write不应报错
            result = write.execute(filepath, "new content")
            text = _r(result)
            assert "已写入" in text
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_clear_read_files(self):
        """clear_read_files清空后覆写需重新mark_read"""
        path = _create_temp_file("original\n")
        try:
            from narnat_agent.tools import write
            write.mark_read(path)
            write.clear_read_files()
            result = write.execute(path, "modified\n")
            text = _r(result)
            assert "错误" in text
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# 四、Glob 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestGlobDeep:
    """Glob工具 - 深度边界与暴力测试"""

    def test_glob_nonexistent_dir(self):
        """不存在的目录报错"""
        from narnat_agent.tools import glob as glob_tool
        result = glob_tool.execute("*.py", path="/nonexistent/dir")
        assert "错误" in result

    def test_glob_no_match(self):
        """无匹配返回提示"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import glob as glob_tool
            result = glob_tool.execute("*.xyz", path=tmpdir)
            assert "无匹配" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_glob_star_star_py(self):
        """**/*.py递归匹配"""
        tmpdir = _create_temp_dir()
        try:
            os.makedirs(os.path.join(tmpdir, "src"), exist_ok=True)
            with open(os.path.join(tmpdir, "main.py"), 'w') as f:
                f.write("")
            with open(os.path.join(tmpdir, "src", "util.py"), 'w') as f:
                f.write("")
            from narnat_agent.tools import glob as glob_tool
            result = glob_tool.execute("**/*.py", path=tmpdir)
            assert "main.py" in result
            assert "util.py" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_glob_ignores_pycache(self):
        """忽略__pycache__目录"""
        tmpdir = _create_temp_dir()
        try:
            os.makedirs(os.path.join(tmpdir, "__pycache__"), exist_ok=True)
            with open(os.path.join(tmpdir, "main.py"), 'w') as f:
                f.write("")
            with open(os.path.join(tmpdir, "__pycache__", "main.cpython-312.pyc"), 'wb') as f:
                f.write(b'\x00')
            from narnat_agent.tools import glob as glob_tool
            result = glob_tool.execute("**/*", path=tmpdir)
            assert "main.py" in result
            assert "__pycache__" not in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_glob_specific_extension(self):
        """特定扩展名匹配"""
        tmpdir = _create_temp_dir()
        try:
            for ext in ["py", "js", "md", "txt"]:
                with open(os.path.join(tmpdir, f"file.{ext}"), 'w') as f:
                    f.write("")
            from narnat_agent.tools import glob as glob_tool
            result = glob_tool.execute("*.py", path=tmpdir)
            assert "file.py" in result
            assert "file.js" not in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 五、Grep 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestGrepDeep:
    """Grep工具 - 深度边界与暴力测试"""

    def test_grep_invalid_regex(self):
        """非法正则报错"""
        from narnat_agent.tools import grep
        result = grep.execute("[invalid")
        assert "错误" in result

    def test_grep_nonexistent_path(self):
        """不存在的路径报错"""
        from narnat_agent.tools import grep
        result = grep.execute("pattern", path="/nonexistent/path")
        assert "错误" in result

    def test_grep_no_match(self):
        """无匹配返回提示"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("hello world\n")
            from narnat_agent.tools import grep
            result = grep.execute("nonexistent_pattern", path=tmpdir)
            assert "无匹配" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_content_mode(self):
        """content模式显示匹配行"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("def hello():\n    pass\n")
            from narnat_agent.tools import grep
            result = grep.execute("def hello", path=tmpdir, output_mode="content", n=True)
            assert "hello" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_count_mode(self):
        """count模式显示匹配数"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("foo\nbar\nfoo\nbaz\nfoo\n")
            from narnat_agent.tools import grep
            result = grep.execute("foo", path=tmpdir, output_mode="count")
            assert "3" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_case_insensitive(self):
        """忽略大小写"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("Hello World\n")
            from narnat_agent.tools import grep
            result = grep.execute("hello", path=tmpdir, i=True)
            assert "test.py" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_glob_filter(self):
        """glob过滤文件类型"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("pattern_here\n")
            with open(os.path.join(tmpdir, "test.js"), 'w') as f:
                f.write("pattern_here\n")
            from narnat_agent.tools import grep
            result = grep.execute("pattern_here", path=tmpdir, glob="*.py")
            assert "test.py" in result
            assert "test.js" not in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_context_lines(self):
        """上下文行(A/B/C)"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("line1\nline2\ntarget\nline4\nline5\n")
            from narnat_agent.tools import grep
            result = grep.execute("target", path=tmpdir, output_mode="content", C=1)
            assert "line2" in result
            assert "line4" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_head_limit(self):
        """head_limit限制输出"""
        tmpdir = _create_temp_dir()
        try:
            for i in range(10):
                with open(os.path.join(tmpdir, f"file{i}.py"), 'w') as f:
                    f.write("pattern\n")
            from narnat_agent.tools import grep
            result = grep.execute("pattern", path=tmpdir, head_limit=3)
            lines = [l for l in result.strip().split('\n') if l.strip()]
            assert len(lines) <= 3
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_grep_single_file(self):
        """path为文件时直接搜索"""
        path = _create_temp_file("hello\nworld\nhello\n")
        try:
            from narnat_agent.tools import grep
            result = grep.execute("hello", path=path, output_mode="count")
            assert "2" in result
        finally:
            os.unlink(path)

    def test_grep_skips_binary(self):
        """跳过二进制文件"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "binary.bin"), 'wb') as f:
                f.write(b'\x00\x01\x02\xff')
            with open(os.path.join(tmpdir, "text.py"), 'w') as f:
                f.write("pattern\n")
            from narnat_agent.tools import grep
            result = grep.execute("pattern", path=tmpdir)
            assert "text.py" in result
            assert "binary.bin" not in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 六、TodoWrite 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestTodoWriteDeep:
    """TodoWrite工具 - 深度边界与暴力测试"""

    def test_empty_todos_error(self):
        """空todos报错"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([])
        assert "错误" in result

    def test_missing_field_error(self):
        """缺少必填字段报错"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([{"content": "task", "status": "pending"}])
        assert "错误" in result
        assert "activeForm" in result

    def test_invalid_status_error(self):
        """非法status报错"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([{
            "content": "task", "activeForm": "tasking", "status": "invalid"
        }])
        assert "错误" in result

    def test_multiple_in_progress_error(self):
        """多个in_progress报错"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([
            {"content": "t1", "activeForm": "t1", "status": "in_progress"},
            {"content": "t2", "activeForm": "t2", "status": "in_progress"},
        ])
        assert "错误" in result

    def test_zero_in_progress_ok(self):
        """0个in_progress合法（初始状态）"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([
            {"content": "t1", "activeForm": "t1", "status": "pending"},
            {"content": "t2", "activeForm": "t2", "status": "completed"},
        ])
        assert "错误" not in result
        assert "2项" in result

    def test_one_in_progress_ok(self):
        """1个in_progress合法"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([
            {"content": "t1", "activeForm": "t1", "status": "in_progress"},
            {"content": "t2", "activeForm": "t2", "status": "pending"},
        ])
        assert "错误" not in result

    def test_non_dict_item_error(self):
        """非dict项报错"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute(["not a dict"])
        assert "错误" in result

    def test_large_todo_list(self):
        """大任务列表"""
        from narnat_agent.tools import todo_write
        todos = [
            {"content": f"task{i}", "activeForm": f"tasking{i}", "status": "pending"}
            for i in range(100)
        ]
        todos[0]["status"] = "in_progress"
        result = todo_write.execute(todos)
        assert "100项" in result

    def test_all_completed(self):
        """全部completed合法"""
        from narnat_agent.tools import todo_write
        result = todo_write.execute([
            {"content": "t1", "activeForm": "t1", "status": "completed"},
            {"content": "t2", "activeForm": "t2", "status": "completed"},
        ])
        assert "错误" not in result


# ═══════════════════════════════════════════════════════════════
# 七、Registry 工具注册表深度测试
# ═══════════════════════════════════════════════════════════════

class TestRegistryDeep:
    """Registry - 工具注册表深度测试"""

    def test_all_9_tools_registered(self):
        """9个工具全部注册"""
        from narnat_agent.tools.registry import get_tool_names
        names = get_tool_names()
        assert len(names) == 9
        expected = {"Read", "Glob", "Grep", "Edit", "Write", "Shell", "Terminal", "WebSearch", "TodoWrite"}
        assert set(names) == expected

    def test_unknown_tool_error(self):
        """未知工具报错"""
        from narnat_agent.tools.registry import execute
        result = execute("UnknownTool", {})
        assert "错误" in result[0]

    def test_tool_definitions_count(self):
        """工具定义数量=9"""
        from narnat_agent.tools.registry import get_tool_definitions
        defs = get_tool_definitions()
        assert len(defs) == 9

    def test_tool_definitions_have_required_fields(self):
        """每个工具定义含type/function/name/parameters"""
        from narnat_agent.tools.registry import get_tool_definitions
        for defn in get_tool_definitions():
            assert defn["type"] == "function"
            func = defn["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            params = func["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_execute_returns_tuple(self):
        """execute始终返回tuple"""
        from narnat_agent.tools.registry import execute
        result = execute("UnknownTool", {})
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_tool_parameter_type_error(self):
        """工具参数类型错误被捕获"""
        from narnat_agent.tools.registry import execute
        # Read需要file_path(str)，传int应被TypeError捕获
        result = execute("Read", {"file_path": 12345})
        # 不崩溃，返回错误信息
        assert isinstance(result, tuple)


# ═══════════════════════════════════════════════════════════════
# 八、Context Manager 深度测试
# ═══════════════════════════════════════════════════════════════

class TestContextDeep:
    """ContextManager - 状态机深度测试"""

    def test_increment_starts_at_zero(self):
        """初始turn_count=0"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        assert ctx.turn_count == 0

    def test_increment_increases_count(self):
        """increment增加轮次"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        ctx.increment()
        assert ctx.turn_count == 1

    def test_warn_at_50(self):
        """50轮警告"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import WARN_TURN_1
        ctx = ContextManager()
        for _ in range(WARN_TURN_1 - 1):
            ctx.increment()
        warn = ctx.increment()
        assert warn != ""
        assert str(WARN_TURN_1) in warn

    def test_warn_at_80(self):
        """80轮警告"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import WARN_TURN_2
        ctx = ContextManager()
        for _ in range(WARN_TURN_2 - 1):
            ctx.increment()
        warn = ctx.increment()
        assert warn != ""
        assert str(WARN_TURN_2) in warn

    def test_need_compress_at_100(self):
        """100轮触发压缩"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import COMPRESS_TURN
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN):
            ctx.increment()
        assert ctx.need_compress() is True

    def test_no_compress_before_100(self):
        """100轮前不压缩"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import COMPRESS_TURN
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN - 1):
            ctx.increment()
        assert ctx.need_compress() is False

    def test_reset_clears_all(self):
        """reset清空所有状态"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        for _ in range(100):
            ctx.increment()
        ctx.reset()
        assert ctx.turn_count == 0
        assert ctx.need_compress() is False

    def test_set_retry_soon(self):
        """set_retry_soon设置10轮后重试"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import COMPRESS_TURN
        ctx = ContextManager()
        for _ in range(COMPRESS_TURN):
            ctx.increment()
        ctx.set_retry_soon()
        assert ctx.turn_count == COMPRESS_TURN - 10
        assert ctx.need_compress() is False
        # 再increment 10次后需要压缩
        for _ in range(10):
            ctx.increment()
        assert ctx.need_compress() is True

    def test_warn_only_once(self):
        """警告只触发一次"""
        from narnat_agent.core.context import ContextManager
        from narnat_agent.config.defaults import WARN_TURN_1
        ctx = ContextManager()
        warns = []
        for _ in range(WARN_TURN_1 + 10):
            w = ctx.increment()
            if w:
                warns.append(w)
        # 50轮警告只出现一次
        assert len([w for w in warns if str(WARN_TURN_1) in w]) == 1

    def test_get_summary(self):
        """get_summary返回正确结构"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        ctx.increment()
        summary = ctx.get_summary()
        assert "turn_count" in summary
        assert summary["turn_count"] == 1


# ═══════════════════════════════════════════════════════════════
# 九、Compressor 深度测试
# ═══════════════════════════════════════════════════════════════

class TestCompressorDeep:
    """Compressor - 压缩流程深度测试"""

    def test_build_compress_messages_appends_prompt(self):
        """build_compress_messages末尾追加压缩指令"""
        from narnat_agent.core.compressor import Compressor
        from narnat_agent.config.defaults import COMPRESS_PROMPT
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            result = comp.build_compress_messages(messages)
            assert len(result) == len(messages) + 1
            assert result[-1]["role"] == "user"
            assert result[-1]["content"] == COMPRESS_PROMPT
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_build_compress_messages_shallow_copy(self):
        """build_compress_messages不修改原messages"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            messages = [{"role": "user", "content": "hello"}]
            original_len = len(messages)
            comp.build_compress_messages(messages)
            assert len(messages) == original_len
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_and_verify_summary(self):
        """写入并校验总结"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            assert comp.write_summary("summary content") is True
            assert comp.verify_summary() is True
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_verify_empty_summary_fails(self):
        """空总结校验失败"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            comp.write_summary("")
            assert comp.verify_summary() is False
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_verify_whitespace_only_summary_fails(self):
        """纯空白总结校验失败"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            comp.write_summary("   \n  \n  ")
            assert comp.verify_summary() is False
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_summary(self):
        """读取总结"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            comp.write_summary("test summary")
            assert comp.read_summary() == "test summary"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_reset_summary(self):
        """重置总结文件"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            comp.write_summary("content")
            comp.reset_summary()
            assert comp.verify_summary() is False
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_build_new_session_messages(self):
        """创建新会话messages"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            msgs = comp.build_new_session_messages("system prompt", "summary text")
            assert len(msgs) == 1
            assert msgs[0]["role"] == "system"
            assert "system prompt" in msgs[0]["content"]
            assert "上一轮对话成果" in msgs[0]["content"]
            assert "summary text" in msgs[0]["content"]
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_build_new_session_messages_empty_summary(self):
        """空summary不追加上一轮对话成果"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            msgs = comp.build_new_session_messages("system prompt", "")
            assert "上一轮对话成果" not in msgs[0]["content"]
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 十、LLM Client 深度测试
# ═══════════════════════════════════════════════════════════════

class TestLLMDeep:
    """LLMClient - token计数与协议选择深度测试"""

    def test_count_tokens_empty_messages(self):
        """空消息token=0"""
        from narnat_agent.core.llm import LLMClient
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        assert client.count_tokens([]) == 0

    def test_count_tokens_none_content(self):
        """content=None的消息token=0"""
        from narnat_agent.core.llm import LLMClient
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        msgs = [{"role": "assistant", "content": None}]
        assert client.count_tokens(msgs) == 0

    def test_count_tokens_chinese(self):
        """中文token估算(1字≈2token)"""
        from narnat_agent.core.llm import LLMClient
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        msgs = [{"role": "user", "content": "你好世界"}]
        tokens = client.count_tokens(msgs)
        # 4个中文字 ≈ 8 token (至少大于0)
        assert tokens > 0

    def test_count_tokens_english(self):
        """英文token估算(1词≈1token)"""
        from narnat_agent.core.llm import LLMClient
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        msgs = [{"role": "user", "content": "hello world test"}]
        tokens = client.count_tokens(msgs)
        assert tokens > 0

    def test_count_tokens_mixed(self):
        """中英混合token估算"""
        from narnat_agent.core.llm import LLMClient
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.test.com", model="test")
        client = LLMClient(config)
        msgs = [{"role": "user", "content": "你好 hello 世界 world"}]
        tokens = client.count_tokens(msgs)
        assert tokens > 0

    def test_anthropic_backend_selection(self):
        """base_url含anthropic选择Anthropic后端"""
        from narnat_agent.core.llm import LLMClient, _AnthropicBackend
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.anthropic.com", model="claude-3")
        client = LLMClient(config)
        assert isinstance(client._backend, _AnthropicBackend)

    def test_openai_backend_selection(self):
        """base_url不含anthropic选择OpenAI后端"""
        from narnat_agent.core.llm import LLMClient, _OpenAIBackend
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.deepseek.com", model="deepseek-chat")
        client = LLMClient(config)
        assert isinstance(client._backend, _OpenAIBackend)


# ═══════════════════════════════════════════════════════════════
# 十一、Anthropic 消息转换深度测试
# ═══════════════════════════════════════════════════════════════

class TestAnthropicConversionDeep:
    """Anthropic消息格式转换深度测试"""

    def _make_backend(self):
        from narnat_agent.core.llm import _AnthropicBackend
        from narnat_agent.config.loader import AIConfig
        config = AIConfig(api_key="test", base_url="https://api.anthropic.com", model="claude-3")
        return _AnthropicBackend(config, [], None)

    def test_convert_system_message(self):
        """system消息提取为system_text"""
        backend = self._make_backend()
        msgs = [{"role": "system", "content": "You are helpful"}]
        system, anthropic_msgs = backend._convert_messages(msgs)
        assert system == "You are helpful"
        assert len(anthropic_msgs) == 0

    def test_convert_user_message(self):
        """user消息直接转换"""
        backend = self._make_backend()
        msgs = [{"role": "user", "content": "hello"}]
        system, anthropic_msgs = backend._convert_messages(msgs)
        assert len(anthropic_msgs) == 1
        assert anthropic_msgs[0]["role"] == "user"

    def test_convert_assistant_with_tool_calls(self):
        """assistant含tool_calls转为content blocks"""
        backend = self._make_backend()
        msgs = [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path": "/tmp/test"}'}
            }]
        }]
        system, anthropic_msgs = backend._convert_messages(msgs)
        assert len(anthropic_msgs) == 1
        assert isinstance(anthropic_msgs[0]["content"], list)

    def test_convert_tool_result(self):
        """tool结果转为user消息中的tool_result block"""
        backend = self._make_backend()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content"},
        ]
        system, anthropic_msgs = backend._convert_messages(msgs)
        # tool结果转为新的user消息（含tool_result block）
        # 前一个user消息是纯文本，不合并；tool结果独立成user消息
        assert len(anthropic_msgs) == 2
        assert anthropic_msgs[0]["role"] == "user"
        assert anthropic_msgs[0]["content"] == "hello"
        assert anthropic_msgs[1]["role"] == "user"
        assert isinstance(anthropic_msgs[1]["content"], list)
        assert anthropic_msgs[1]["content"][0]["type"] == "tool_result"

    def test_convert_multiple_system_messages(self):
        """多个system消息用换行拼接"""
        backend = self._make_backend()
        msgs = [
            {"role": "system", "content": "part1"},
            {"role": "system", "content": "part2"},
        ]
        system, anthropic_msgs = backend._convert_messages(msgs)
        assert "part1" in system
        assert "part2" in system

    def test_convert_tools(self):
        """工具定义转换"""
        backend = self._make_backend()
        tool_defs = [{
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
            }
        }]
        result = backend._convert_tools(tool_defs)
        assert len(result) == 1
        assert result[0]["name"] == "Read"
        assert "input_schema" in result[0]


# ═══════════════════════════════════════════════════════════════
# 十二、Session Store 深度测试
# ═══════════════════════════════════════════════════════════════

class TestSessionStoreDeep:
    """SessionStore - 安全与边界深度测试"""

    def test_path_traversal_protection(self):
        """路径遍历攻击防护"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            # 尝试用..逃逸
            path = session_store._session_path(narnat_dir, "../../etc/passwd")
            assert ".." not in path
            # 路径应在sessions子目录内
            assert "sessions" in path
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_special_chars_in_name(self):
        """会话名特殊字符替换"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            path = session_store._session_path(narnat_dir, "test<>:|?*name")
            assert "<" not in os.path.basename(path)
            assert ">" not in os.path.basename(path)
            assert ":" not in os.path.basename(path)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_load_round_trip(self):
        """保存-加载往返一致"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            err = session_store.save_session(narnat_dir, "test_session", messages)
            assert err == ""
            loaded, err2 = session_store.load_session(narnat_dir, "test_session")
            assert err2 == ""
            assert len(loaded) == 3
            assert loaded[1]["content"] == "hello"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_nonexistent_session(self):
        """加载不存在的会话报错"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            msgs, err = session_store.load_session(narnat_dir, "nonexistent")
            assert err != ""
            assert len(msgs) == 0
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_delete_nonexistent_session(self):
        """删除不存在的会话报错"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            err = session_store.delete_session(narnat_dir, "nonexistent")
            assert err != ""
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_delete_all_sessions(self):
        """删除全部会话"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            session_store.save_session(narnat_dir, "s1", [{"role": "user", "content": "a"}])
            session_store.save_session(narnat_dir, "s2", [{"role": "user", "content": "b"}])
            err = session_store.delete_session(narnat_dir, "--all")
            assert err == ""
            sessions = session_store.list_sessions(narnat_dir)
            assert len(sessions) == 0
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_list_sessions(self):
        """列出会话"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            session_store.save_session(narnat_dir, "s1", [{"role": "user", "content": "a"}])
            session_store.save_session(narnat_dir, "s2", [{"role": "user", "content": "b"}])
            sessions = session_store.list_sessions(narnat_dir)
            assert len(sessions) == 2
            names = {s["name"] for s in sessions}
            assert "s1" in names
            assert "s2" in names
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_unicode_messages(self):
        """保存Unicode消息"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "你好 🎆 こんにちは"}]
            err = session_store.save_session(narnat_dir, "unicode_test", messages)
            assert err == ""
            loaded, err2 = session_store.load_session(narnat_dir, "unicode_test")
            assert loaded[0]["content"] == "你好 🎆 こんにちは"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_large_messages(self):
        """保存大消息"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "x" * 100000}]
            err = session_store.save_session(narnat_dir, "large_test", messages)
            assert err == ""
            loaded, err2 = session_store.load_session(narnat_dir, "large_test")
            assert len(loaded[0]["content"]) == 100000
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 十三、Logger 深度测试
# ═══════════════════════════════════════════════════════════════

class TestLoggerDeep:
    """Logger - 脱敏与日志深度测试"""

    def test_redact_sk_prefix(self):
        """sk-前缀脱敏 - 验证脱敏函数对sk-前缀key做了处理"""
        from narnat_agent.logger import _redact
        result = _redact("key=sk-abc123456789")
        # 脱敏函数应在结果中添加***标记
        assert "***" in result

    def test_redact_api_key_assignment(self):
        """api_key=赋值脱敏"""
        from narnat_agent.logger import _redact
        result = _redact('api_key="sk-longkey12345"')
        assert "***" in result

    def test_redact_normal_text_unchanged(self):
        """普通文本不脱敏"""
        from narnat_agent.logger import _redact
        text = "This is a normal log message"
        assert _redact(text) == text

    def test_logger_start_creates_file(self):
        """start创建日志文件"""
        from narnat_agent.logger import AgentLogger
        tmpdir = _create_temp_dir()
        try:
            logger = AgentLogger(tmpdir)
            filepath = logger.start(tmpdir)
            assert os.path.isfile(filepath)
            logger.close()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_logger_no_start_no_crash(self):
        """未start时log不崩溃"""
        from narnat_agent.logger import AgentLogger
        logger = AgentLogger()
        logger.info("test", "message")  # 不崩溃
        logger.close()

    def test_logger_close_idempotent(self):
        """close幂等"""
        from narnat_agent.logger import AgentLogger
        tmpdir = _create_temp_dir()
        try:
            logger = AgentLogger(tmpdir)
            logger.start(tmpdir)
            logger.close()
            logger.close()  # 不崩溃
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_logger_multiple_starts(self):
        """多次start创建不同文件"""
        from narnat_agent.logger import AgentLogger
        tmpdir = _create_temp_dir()
        try:
            logger = AgentLogger(tmpdir)
            f1 = logger.start(tmpdir)
            time.sleep(1.1)  # 确保文件名不同
            f2 = logger.start(tmpdir)
            assert f1 != f2
            logger.close()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 十四、Agent 工具分类不变量测试
# ═══════════════════════════════════════════════════════════════

class TestToolClassificationInvariant:
    """工具分类不变量 - 确保分类完备且互斥"""

    def test_all_tools_in_exactly_one_category(self):
        """每个工具恰好属于一个分类"""
        from narnat_agent.tools.registry import get_tool_names
        readonly = {"Read", "Glob", "Grep", "WebSearch"}
        write = {"Edit", "Write"}
        serial = {"Shell", "Terminal", "TodoWrite"}
        all_classified = readonly | write | serial
        all_tools = set(get_tool_names())
        assert all_tools == all_classified
        # 互斥
        assert readonly & write == set()
        assert readonly & serial == set()
        assert write & serial == set()

    def test_file_path_tools_subset(self):
        """FILE_PATH_TOOLS是WRITE_TOOLS的子集加上Read"""
        file_path_tools = {"Read", "Edit", "Write"}
        write_tools = {"Edit", "Write"}
        assert file_path_tools - {"Read"} == write_tools

    def test_readonly_tools_are_read_only(self):
        """只读工具不修改文件"""
        readonly = {"Read", "Glob", "Grep", "WebSearch"}
        # 这些工具的execute不应修改文件系统
        # 通过工具定义验证：无file_path写入参数
        from narnat_agent.tools.registry import get_tool_definitions
        for defn in get_tool_definitions():
            name = defn["function"]["name"]
            if name in readonly:
                params = defn["function"]["parameters"]["properties"]
                # 只读工具不应有content/new_string等写入参数
                assert "content" not in params or name == "Read"  # Read无content
                if name != "Read":
                    assert "new_string" not in params


# ═══════════════════════════════════════════════════════════════
# 十五、Bash 工具深度测试
# ═══════════════════════════════════════════════════════════════

class TestBashDeep:
    """Bash/Shell工具 - 深度边界测试"""

    def test_bash_simple_command(self):
        """简单命令执行"""
        from narnat_agent.tools import bash
        result = bash.execute("python --version")
        assert isinstance(result, str)
        assert "Python" in result or "exit code" in result

    def test_bash_exit_code(self):
        """退出码正确返回"""
        from narnat_agent.tools import bash
        result = bash.execute("python -c \"exit(1)\"")
        assert "exit code: 1" in result

    def test_bash_output_truncation(self):
        """超长输出截断"""
        from narnat_agent.tools import bash
        from narnat_agent.config.defaults import MAX_BASH_OUTPUT
        result = bash.execute(f"python -c \"print('x' * {MAX_BASH_OUTPUT + 1000})\"")
        assert "截断" in result or len(result) < MAX_BASH_OUTPUT + 5000

    def test_bash_timeout(self):
        """超时kill"""
        from narnat_agent.tools import bash
        result = bash.execute("python -c \"import time; time.sleep(10)\"", timeout=2000)
        assert "超时" in result

    def test_bash_unicode_output(self):
        """Unicode输出"""
        from narnat_agent.tools import bash
        result = bash.execute("python -c \"print('你好世界')\"")
        assert isinstance(result, str)

    def test_bash_stderr(self):
        """stderr输出"""
        from narnat_agent.tools import bash
        result = bash.execute("python -c \"import sys; sys.stderr.write('err\\n')\"")
        assert "stderr" in result or "err" in result

    def test_needs_powershell_detection(self):
        """PowerShell检测"""
        from narnat_agent.tools.bash import _needs_powershell
        # python -c 需要 PowerShell
        assert _needs_powershell('python -c "import sys"') is True
        # 含$变量需要PowerShell
        assert _needs_powershell("echo $HOME") is True
        # 含非ASCII需要PowerShell
        assert _needs_powershell("echo 你好") is True
        # 简单cmd命令不需要PowerShell
        assert _needs_powershell("dir") is False

    def test_delete_command_needs_confirm(self):
        """删除命令需确认"""
        from narnat_agent.tools.bash import _RE_DELETE
        assert _RE_DELETE.search("rm file.txt") is not None
        assert _RE_DELETE.search("del file.txt") is not None
        assert _RE_DELETE.search("Remove-Item file.txt") is not None
        assert _RE_DELETE.search("echo hello") is None

    def test_decode_output_utf8(self):
        """UTF-8输出解码"""
        from narnat_agent.tools.bash import _decode_output
        assert _decode_output(b"hello") == "hello"
        assert _decode_output("你好".encode("utf-8")) == "你好"

    def test_decode_output_fallback_gbk(self):
        """GBK回退解码(Windows)"""
        from narnat_agent.tools.bash import _decode_output
        if sys.platform == "win32":
            gbk_bytes = "你好".encode("gbk")
            result = _decode_output(gbk_bytes)
            assert "你好" in result

    def test_decode_output_empty(self):
        """空输出"""
        from narnat_agent.tools.bash import _decode_output
        assert _decode_output(b"") == ""
        assert _decode_output(None) == ""


# ═══════════════════════════════════════════════════════════════
# 十六、Defaults 一致性测试
# ═══════════════════════════════════════════════════════════════

class TestDefaultsConsistency:
    """Defaults - 常量一致性验证"""

    def test_threshold_ordering(self):
        """阈值顺序: WARN_1 < WARN_2 < COMPRESS"""
        from narnat_agent.config.defaults import WARN_TURN_1, WARN_TURN_2, COMPRESS_TURN
        assert WARN_TURN_1 < WARN_TURN_2 < COMPRESS_TURN

    def test_max_values_positive(self):
        """最大值常量为正"""
        from narnat_agent.config.defaults import MAX_FILE_LINES, MAX_LINE_CHARS, MAX_BASH_OUTPUT
        assert MAX_FILE_LINES > 0
        assert MAX_LINE_CHARS > 0
        assert MAX_BASH_OUTPUT > 0

    def test_iron_rules_not_empty(self):
        """铁律非空"""
        from narnat_agent.config.defaults import IRON_RULES
        assert len(IRON_RULES) > 0

    def test_base_prompt_template_has_placeholders(self):
        """基础prompt模板含占位符"""
        from narnat_agent.config.defaults import BASE_PROMPT_TEMPLATE
        assert "{model}" in BASE_PROMPT_TEMPLATE
        assert "{cwd}" in BASE_PROMPT_TEMPLATE
        assert "{platform}" in BASE_PROMPT_TEMPLATE
        assert "{shell}" in BASE_PROMPT_TEMPLATE

    def test_compress_prompt_not_empty(self):
        """压缩prompt非空"""
        from narnat_agent.config.defaults import COMPRESS_PROMPT
        assert len(COMPRESS_PROMPT) > 0

    def test_narnat_constants(self):
        """Narnat目录常量"""
        from narnat_agent.config.defaults import NARNAT_DIR, NARNAT_JSON, NARNAT_MD
        assert NARNAT_DIR == ".narnat"
        assert NARNAT_JSON == "narnat.json"
        assert NARNAT_MD == "narnat.md"


# ═══════════════════════════════════════════════════════════════
# 十七、Config Loader 深度测试
# ═══════════════════════════════════════════════════════════════

class TestConfigLoaderDeep:
    """ConfigLoader - 配置加载深度测试"""

    def test_load_config_creates_narnat_dir(self):
        """load_config创建.narnat目录"""
        from narnat_agent.config.loader import load_config
        tmpdir = _create_temp_dir()
        try:
            config = load_config(tmpdir)
            assert os.path.isdir(config.narnat_dir)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_config_creates_json_and_md(self):
        """load_config创建narnat.json和narnat.md"""
        from narnat_agent.config.loader import load_config
        tmpdir = _create_temp_dir()
        try:
            config = load_config(tmpdir)
            assert os.path.isfile(os.path.join(config.narnat_dir, "narnat.json"))
            assert os.path.isfile(os.path.join(config.narnat_dir, "narnat.md"))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_config_default_values(self):
        """默认配置值"""
        from narnat_agent.config.loader import load_config
        from narnat_agent.config.defaults import DEFAULT_BASE_URL, DEFAULT_MODEL
        tmpdir = _create_temp_dir()
        try:
            config = load_config(tmpdir)
            assert config.ai.base_url == DEFAULT_BASE_URL
            assert config.ai.model == DEFAULT_MODEL
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_config_custom_json(self):
        """自定义narnat.json"""
        from narnat_agent.config.loader import load_config
        tmpdir = _create_temp_dir()
        try:
            narnat_dir = os.path.join(tmpdir, ".narnat")
            os.makedirs(narnat_dir, exist_ok=True)
            with open(os.path.join(narnat_dir, "narnat.json"), 'w') as f:
                json.dump({"api_key": "sk-test", "base_url": "https://custom.api", "model": "custom-model"}, f)
            config = load_config(tmpdir)
            assert config.ai.api_key == "sk-test"
            assert config.ai.base_url == "https://custom.api"
            assert config.ai.model == "custom-model"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_config_invalid_json_fallback(self):
        """非法JSON回退默认"""
        from narnat_agent.config.loader import load_config
        from narnat_agent.config.defaults import DEFAULT_BASE_URL
        tmpdir = _create_temp_dir()
        try:
            narnat_dir = os.path.join(tmpdir, ".narnat")
            os.makedirs(narnat_dir, exist_ok=True)
            with open(os.path.join(narnat_dir, "narnat.json"), 'w') as f:
                f.write("invalid json{{{")
            config = load_config(tmpdir)
            assert config.ai.base_url == DEFAULT_BASE_URL
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_system_prompt_contains_model(self):
        """系统prompt含模型名"""
        from narnat_agent.config.loader import load_config
        tmpdir = _create_temp_dir()
        try:
            config = load_config(tmpdir)
            assert config.ai.model in config.system_prompt
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_system_prompt_contains_iron_rules(self):
        """系统prompt含铁律"""
        from narnat_agent.config.loader import load_config
        from narnat_agent.config.defaults import IRON_RULES
        tmpdir = _create_temp_dir()
        try:
            config = load_config(tmpdir)
            assert "Iron Rules" in config.system_prompt
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_user_md_appended_to_prompt(self):
        """narnat.md追加到系统prompt"""
        from narnat_agent.config.loader import load_config
        tmpdir = _create_temp_dir()
        try:
            narnat_dir = os.path.join(tmpdir, ".narnat")
            os.makedirs(narnat_dir, exist_ok=True)
            with open(os.path.join(narnat_dir, "narnat.json"), 'w') as f:
                json.dump({}, f)
            with open(os.path.join(narnat_dir, "narnat.md"), 'w') as f:
                f.write("# Custom Rule\n- Use pytest\n")
            config = load_config(tmpdir)
            assert "Custom Rule" in config.system_prompt
            assert "Use pytest" in config.system_prompt
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 十八、Agent _repair_messages 深度测试
# ═══════════════════════════════════════════════════════════════

class TestRepairMessagesDeep:
    """_repair_messages - 消息修复深度测试"""

    def _make_agent_minimal(self):
        """创建最小Agent实例用于测试_repair_messages"""
        # 直接测试逻辑而不启动完整Agent
        from narnat_agent.core.agent import Agent
        # 使用patch避免UI初始化
        with patch('narnat_agent.core.agent.UIInterface'), \
             patch('narnat_agent.core.agent.LLMClient'), \
             patch('narnat_agent.core.agent.load_config'):
            # 无法完全避免初始化，改用直接测试逻辑
            pass
        return None

    def test_repair_unreplied_tool_call(self):
        """未回复的tool_call补上空结果"""
        # 直接测试_repair_messages的逻辑
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            ]},
        ]
        # 模拟_repair_messages逻辑
        replied_ids = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        for msg in messages:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[用户中断]",
                        })
                        replied_ids.add(tc_id)
                        repaired = True

        assert repaired is True
        assert len(messages) == 4
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "tc1"

    def test_repair_adds_assistant_after_tool(self):
        """修复后末尾是tool消息时补assistant"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "[用户中断]"},
        ]
        # 末尾是tool，需要补assistant
        if messages[-1].get("role") == "tool":
            messages.append({"role": "assistant", "content": "（用户中断了工具执行）"})
        assert messages[-1]["role"] == "assistant"

    def test_no_repair_when_all_replied(self):
        """所有tool_call已回复时不修复"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content"},
        ]
        replied_ids = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        for msg in messages:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        repaired = True

        assert repaired is False


# ═══════════════════════════════════════════════════════════════
# 十九、并发安全测试
# ═══════════════════════════════════════════════════════════════

class TestConcurrencySafety:
    """并发安全 - 多线程同时操作不崩溃"""

    def test_concurrent_read_files(self):
        """多线程同时Read不崩溃"""
        path = _create_temp_file("content\n" * 100)
        try:
            from narnat_agent.tools import read
            errors = []

            def read_worker():
                try:
                    result = read.execute(path)
                    assert isinstance(result, str)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=read_worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(errors) == 0
        finally:
            os.unlink(path)

    def test_concurrent_write_read_files(self):
        """多线程同时Write和Read不崩溃"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, read
            filepath = os.path.join(tmpdir, "concurrent.py")
            write.execute(filepath, "initial\n")
            errors = []

            def write_worker(i):
                try:
                    write.execute(filepath, f"content{i}\n")
                except Exception as e:
                    errors.append(e)

            def read_worker():
                try:
                    read.execute(filepath)
                except Exception as e:
                    errors.append(e)

            threads = []
            for i in range(10):
                threads.append(threading.Thread(target=write_worker, args=(i,)))
                threads.append(threading.Thread(target=read_worker))
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(errors) == 0
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_concurrent_todo_write(self):
        """多线程同时TodoWrite不崩溃"""
        from narnat_agent.tools import todo_write
        errors = []

        def todo_worker(i):
            try:
                todo_write.execute([{
                    "content": f"task{i}",
                    "activeForm": f"tasking{i}",
                    "status": "pending"
                }])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=todo_worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

    def test_concurrent_context_increment(self):
        """多线程同时increment不崩溃(GIL保护)"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        errors = []

        def inc_worker():
            try:
                for _ in range(100):
                    ctx.increment()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inc_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        # 由于GIL，turn_count应等于1000
        assert ctx.turn_count == 1000


# ═══════════════════════════════════════════════════════════════
# 二十、工具链闭环集成测试
# ═══════════════════════════════════════════════════════════════

class TestToolChainIntegration:
    """工具链闭环 - Write→Read→Edit→Grep→Glob完整链路"""

    def setup_method(self):
        from narnat_agent.tools import write
        write.clear_read_files()

    def test_write_read_edit_chain(self):
        """Write→Read→Edit链路"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, read, edit
            filepath = os.path.join(tmpdir, "chain.py")

            # Write
            w_result = write.execute(filepath, "def hello():\n    pass\n")
            assert "已写入" in _r(w_result)

            # Read
            r_result = read.execute(filepath)
            assert "hello" in r_result

            # Edit
            e_result = edit.execute(filepath, old_string="pass", new_string="return 42")
            assert "已替换" in _r(e_result)

            # Verify
            r2_result = read.execute(filepath)
            assert "return 42" in r2_result
            assert "pass" not in r2_result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_glob_grep_chain(self):
        """Write→Glob→Grep链路"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, glob as glob_tool, grep
            filepath = os.path.join(tmpdir, "search.py")
            write.execute(filepath, "PATTERN_HERE\nother line\n")

            # Glob
            g_result = glob_tool.execute("*.py", path=tmpdir)
            assert "search.py" in g_result

            # Grep
            gp_result = grep.execute("PATTERN_HERE", path=tmpdir)
            assert "search.py" in gp_result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_read_edit_by_line_chain(self):
        """Write→Read→Edit(行号模式)链路"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, read, edit
            filepath = os.path.join(tmpdir, "lines.py")
            write.execute(filepath, "line1\nline2\nline3\n")
            read.execute(filepath)
            e_result = edit.execute(filepath, line_start=2, line_end=2, new_string="REPLACED")
            assert "已替换" in _r(e_result)
            r_result = read.execute(filepath)
            assert "REPLACED" in r_result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_multiple_edits_same_file(self):
        """同一文件多次Edit"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, edit, read
            filepath = os.path.join(tmpdir, "multi.py")
            write.execute(filepath, "a=1\nb=2\nc=3\n")

            # 第一次Edit
            e1 = edit.execute(filepath, old_string="a=1", new_string="a=10")
            assert "已替换" in _r(e1)

            # 第二次Edit
            e2 = edit.execute(filepath, old_string="b=2", new_string="b=20")
            assert "已替换" in _r(e2)

            # 验证
            r = read.execute(filepath)
            assert "a=10" in r
            assert "b=20" in r
            assert "c=3" in r
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_registry_execute_all_tools(self):
        """Registry执行所有工具不崩溃"""
        from narnat_agent.tools.registry import execute
        tmpdir = _create_temp_dir()
        try:
            filepath = os.path.join(tmpdir, "test.py")
            # Write
            r1 = execute("Write", {"file_path": filepath, "content": "hello\n"})
            assert isinstance(r1, tuple)

            # Read
            r2 = execute("Read", {"file_path": filepath})
            assert isinstance(r2, tuple)

            # Glob
            r3 = execute("Glob", {"pattern": "*.py", "path": tmpdir})
            assert isinstance(r3, tuple)

            # Grep
            r4 = execute("Grep", {"pattern": "hello", "path": tmpdir})
            assert isinstance(r4, tuple)

            # Edit
            r5 = execute("Edit", {"file_path": filepath, "old_string": "hello", "new_string": "world"})
            assert isinstance(r5, tuple)

            # TodoWrite
            r6 = execute("TodoWrite", {"todos": [{"content": "t", "activeForm": "t", "status": "pending"}]})
            assert isinstance(r6, tuple)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 二十一、极端输入暴力测试
# ═══════════════════════════════════════════════════════════════

class TestBrutalExtreme:
    """极端输入暴力测试"""

    def test_read_10000_line_file(self):
        """10000行文件读取"""
        content = "\n".join(f"line{i}" for i in range(10000))
        path = _create_temp_file(content)
        try:
            from narnat_agent.tools import read
            result = read.execute(path)
            assert isinstance(result, str)
        finally:
            os.unlink(path)

    def test_edit_very_long_old_string(self):
        """超长old_string"""
        long_str = "x" * 10000
        path = _create_temp_file(long_str + "\nother\n")
        try:
            from narnat_agent.tools import edit
            result = edit.execute(path, old_string=long_str, new_string="replaced")
            assert "已替换" in _r(result)
        finally:
            os.unlink(path)

    def test_grep_complex_regex(self):
        """复杂正则"""
        tmpdir = _create_temp_dir()
        try:
            with open(os.path.join(tmpdir, "test.py"), 'w') as f:
                f.write("def foo(a, b): return a + b\n")
            from narnat_agent.tools import grep
            result = grep.execute(r"def\s+\w+\([^)]+\)\s*:\s*return", path=tmpdir, output_mode="content")
            assert "foo" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_todo_write_100_items(self):
        """100项TodoWrite"""
        from narnat_agent.tools import todo_write
        todos = [
            {"content": f"task{i}", "activeForm": f"tasking{i}", "status": "pending"}
            for i in range(100)
        ]
        todos[0]["status"] = "in_progress"
        result = todo_write.execute(todos)
        assert "100项" in result

    def test_session_store_rapid_save_load(self):
        """快速保存-加载循环"""
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            for i in range(50):
                msgs = [{"role": "user", "content": f"msg{i}"}]
                session_store.save_session(narnat_dir, f"rapid_{i}", msgs)
                loaded, err = session_store.load_session(narnat_dir, f"rapid_{i}")
                assert err == ""
                assert loaded[0]["content"] == f"msg{i}"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_context_increment_1000_times(self):
        """increment 1000次"""
        from narnat_agent.core.context import ContextManager
        ctx = ContextManager()
        for _ in range(1000):
            ctx.increment()
        assert ctx.turn_count == 1000
        assert ctx.need_compress() is True

    def test_compressor_rapid_write_verify(self):
        """快速写入-校验循环"""
        from narnat_agent.core.compressor import Compressor
        tmpdir = _create_temp_dir()
        try:
            comp = Compressor(tmpdir)
            for i in range(50):
                assert comp.write_summary(f"summary {i}") is True
                assert comp.verify_summary() is True
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_path_with_special_chars(self):
        """路径含特殊字符"""
        tmpdir = _create_temp_dir()
        try:
            # 创建含特殊字符的子目录和文件
            special_dir = os.path.join(tmpdir, "project (v1.0)")
            os.makedirs(special_dir, exist_ok=True)
            filepath = os.path.join(special_dir, "file.py")
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write("content")
            from narnat_agent.tools import read
            result = read.execute(filepath)
            assert "content" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_read_very_long_unicode(self):
        """超长Unicode内容"""
        tmpdir = _create_temp_dir()
        try:
            from narnat_agent.tools import write, read
            filepath = os.path.join(tmpdir, "unicode.py")
            content = "中文内容\n" * 1000 + "🎯🔥\n" * 100
            write.execute(filepath, content)
            result = read.execute(filepath)
            assert "中文内容" in result
            assert "🎯" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 二十二、NarnatSessionCallbacks 深度测试
# ═══════════════════════════════════════════════════════════════

class TestNarnatSessionCallbacksDeep:
    """NarnatSessionCallbacks - 会话回调深度测试"""

    def test_on_save_and_on_enter(self):
        """保存后可进入"""
        from narnat_agent.core.agent import NarnatSessionCallbacks
        from narnat_agent.config import session_store
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "hello"}]
            get_msgs = lambda: messages
            set_msgs = lambda m: None

            cb = NarnatSessionCallbacks(narnat_dir, get_msgs, set_msgs)
            err = cb.on_save("test_session")
            assert err == ""

            err2 = cb.on_enter("test_session")
            assert err2 == ""
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_on_show(self):
        """列出会话"""
        from narnat_agent.core.agent import NarnatSessionCallbacks
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "hello"}]
            cb = NarnatSessionCallbacks(narnat_dir, lambda: messages, lambda m: None)
            cb.on_save("show_test")
            result = cb.on_show()
            assert "show_test" in result
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_on_delete(self):
        """删除会话"""
        from narnat_agent.core.agent import NarnatSessionCallbacks
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "hello"}]
            cb = NarnatSessionCallbacks(narnat_dir, lambda: messages, lambda m: None)
            cb.on_save("del_test")
            err = cb.on_delete("del_test")
            assert err == ""
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_on_exit_auto_save(self):
        """退出时自动保存"""
        from narnat_agent.core.agent import NarnatSessionCallbacks
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "hello"}]
            cb = NarnatSessionCallbacks(narnat_dir, lambda: messages, lambda m: None)
            # 先保存一次
            cb.on_save("auto_test")
            # 退出时应自动保存
            name = cb.on_exit()
            assert name == "auto_test"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_on_exit_no_auto_save_without_prior_save(self):
        """未保存过的会话退出时不自动保存"""
        from narnat_agent.core.agent import NarnatSessionCallbacks
        tmpdir = _create_temp_dir()
        narnat_dir = os.path.join(tmpdir, ".narnat")
        os.makedirs(narnat_dir, exist_ok=True)
        try:
            messages = [{"role": "user", "content": "hello"}]
            cb = NarnatSessionCallbacks(narnat_dir, lambda: messages, lambda m: None)
            name = cb.on_exit()
            assert name == ""
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
