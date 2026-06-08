"""tools层暴力测试 —— 每个工具覆盖正常+边界+异常"""

import os
import shutil
import tempfile
import pytest

from narnat_agent.tools import read, glob, grep, edit, write, bash, terminal, todo_write
from narnat_agent.tools.registry import execute as registry_execute, get_tool_names, get_tool_definitions


# ═══════════════════════════════════════════════════════════════
# Read 测试
# ═══════════════════════════════════════════════════════════════

class TestRead:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.txt")
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write("line1\nline2\nline3\nline4\nline5\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_full(self):
        result = read.execute(self.test_file)
        assert "1→line1" in result
        assert "5→line5" in result

    def test_read_with_offset(self):
        result = read.execute(self.test_file, offset=3)
        assert "3→line3" in result
        assert "1→line1" not in result

    def test_read_with_limit(self):
        result = read.execute(self.test_file, offset=1, limit=2)
        assert "1→line1" in result
        assert "2→line2" in result
        assert "3→line3" not in result

    def test_read_not_exist(self):
        result = read.execute("/nonexistent/file.txt")
        assert "错误" in result

    def test_read_empty_file(self):
        empty = os.path.join(self.tmpdir, "empty.txt")
        with open(empty, "w") as f:
            pass
        result = read.execute(empty)
        # 空文件应返回空或无错误
        assert "错误" not in result

    def test_read_unicode(self):
        uni = os.path.join(self.tmpdir, "uni.txt")
        with open(uni, "w", encoding="utf-8") as f:
            f.write("你好世界\n")
        result = read.execute(uni)
        assert "你好世界" in result


# ═══════════════════════════════════════════════════════════════
# Glob 测试
# ═══════════════════════════════════════════════════════════════

class TestGlob:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmpdir, "src"))
        for name in ["main.py", "utils.py", "readme.md", "src/helper.py"]:
            with open(os.path.join(self.tmpdir, name), "w") as f:
                f.write("")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_glob_py(self):
        result = glob.execute("*.py", self.tmpdir)
        assert "main.py" in result
        assert "utils.py" in result

    def test_glob_recursive(self):
        result = glob.execute("**/*.py", self.tmpdir)
        assert "helper.py" in result

    def test_glob_no_match(self):
        result = glob.execute("*.rs", self.tmpdir)
        assert "无匹配" in result

    def test_glob_invalid_dir(self):
        result = glob.execute("*.py", "/nonexistent/dir")
        assert "错误" in result


# ═══════════════════════════════════════════════════════════════
# Grep 测试
# ═══════════════════════════════════════════════════════════════

class TestGrep:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with open(os.path.join(self.tmpdir, "app.py"), "w", encoding="utf-8") as f:
            f.write("class Foo:\n    pass\n\nclass Bar(Foo):\n    pass\n")
        with open(os.path.join(self.tmpdir, "util.py"), "w", encoding="utf-8") as f:
            f.write("def helper():\n    return 'foo'\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_grep_files_with_matches(self):
        result = grep.execute("class Foo", self.tmpdir)
        assert "app.py" in result

    def test_grep_content_mode(self):
        result = grep.execute("class Foo", self.tmpdir, output_mode="content")
        assert "app.py" in result
        assert "class Foo" in result

    def test_grep_count_mode(self):
        result = grep.execute("class", self.tmpdir, output_mode="count")
        assert "app.py" in result

    def test_grep_invalid_regex(self):
        result = grep.execute("class Foo[", self.tmpdir)
        assert "错误" in result

    def test_grep_case_insensitive(self):
        result = grep.execute("CLASS", self.tmpdir, i=True)
        assert "app.py" in result

    def test_grep_no_match(self):
        result = grep.execute("NonExistentPattern", self.tmpdir)
        assert "无匹配" in result

    def test_grep_glob_filter(self):
        result = grep.execute("class", self.tmpdir, glob="*.py")
        assert "app.py" in result

    def test_grep_file_path_files_with_matches(self):
        """path参数接受文件路径"""
        file_path = os.path.join(self.tmpdir, "app.py")
        result = grep.execute("class Foo", file_path)
        assert "app.py" in result

    def test_grep_file_path_content_mode(self):
        """文件路径+content模式"""
        file_path = os.path.join(self.tmpdir, "app.py")
        result = grep.execute("class Foo", file_path, output_mode="content", n=True)
        assert "class Foo" in result

    def test_grep_file_path_count_mode(self):
        """文件路径+count模式"""
        file_path = os.path.join(self.tmpdir, "app.py")
        result = grep.execute("class", file_path, output_mode="count")
        assert "app.py" in result

    def test_grep_file_path_no_match(self):
        """文件路径搜索无匹配"""
        file_path = os.path.join(self.tmpdir, "app.py")
        result = grep.execute("NonExistentPattern", file_path)
        assert "无匹配" in result

    def test_grep_file_path_with_context(self):
        """文件路径+上下文行"""
        file_path = os.path.join(self.tmpdir, "app.py")
        result = grep.execute("class Foo", file_path, output_mode="content", C=1)
        assert "class Foo" in result
        assert "pass" in result


# ═══════════════════════════════════════════════════════════════
# Edit 测试
# ═══════════════════════════════════════════════════════════════

class TestEdit:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "edit_test.py")
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write("def hello():\n    print('hello')\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_edit_exact_match(self):
        result, color_diff = edit.execute(self.test_file, "print('hello')", "print('world')")
        assert "已替换" in result
        with open(self.test_file, "r") as f:
            assert "world" in f.read()

    def test_edit_not_found(self):
        result, color_diff = edit.execute(self.test_file, "nonexistent_code", "new_code")
        assert "错误" in result

    def test_edit_empty_old_string(self):
        result, color_diff = edit.execute(self.test_file, "", "new")
        assert "错误" in result

    def test_edit_file_not_exist(self):
        result, color_diff = edit.execute("/nonexistent/file.py", "old", "new")
        assert "错误" in result

    def test_edit_multiple_match_no_replace_all(self):
        """多处匹配且未设replace_all"""
        fpath = os.path.join(self.tmpdir, "multi.py")
        with open(fpath, "w") as f:
            f.write("x = 1\nx = 2\n")
        result, color_diff = edit.execute(fpath, "x", "y")
        assert "不唯一" in result

    def test_edit_replace_all(self):
        fpath = os.path.join(self.tmpdir, "multi.py")
        with open(fpath, "w") as f:
            f.write("x = 1\nx = 2\n")
        result, color_diff = edit.execute(fpath, "x", "y", replace_all=True)
        assert "已替换" in result
        with open(fpath, "r") as f:
            content = f.read()
        assert content.count("y") == 2

    def test_edit_shows_diff(self):
        result, color_diff = edit.execute(self.test_file, "print('hello')", "print('world')")
        assert "---" in result or "已替换" in result


# ═══════════════════════════════════════════════════════════════
# Write 测试
# ═══════════════════════════════════════════════════════════════

class TestWrite:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # 清理read标记
        write._read_files.clear()

    def test_write_new_file(self):
        fpath = os.path.join(self.tmpdir, "new.py")
        result, color_diff = write.execute(fpath, "print('hello')")
        assert "已写入" in result
        with open(fpath, "r") as f:
            assert f.read() == "print('hello')"

    def test_write_creates_parent_dir(self):
        fpath = os.path.join(self.tmpdir, "sub", "dir", "file.py")
        result, color_diff = write.execute(fpath, "content")
        assert "已写入" in result
        assert os.path.isfile(fpath)

    def test_write_empty_content(self):
        fpath = os.path.join(self.tmpdir, "empty.txt")
        result, color_diff = write.execute(fpath, "")
        assert "已写入" in result

    def test_write_overwrite_without_read(self):
        """覆写已有文件但未Read，应报错"""
        fpath = os.path.join(self.tmpdir, "existing.py")
        with open(fpath, "w") as f:
            f.write("old content")
        result, color_diff = write.execute(fpath, "new content")
        assert "错误" in result

    def test_write_overwrite_after_read(self):
        """Read后覆写，应成功"""
        fpath = os.path.join(self.tmpdir, "existing.py")
        with open(fpath, "w") as f:
            f.write("old content")
        write.mark_read(fpath)
        result, color_diff = write.execute(fpath, "new content")
        assert "已写入" in result


# ═══════════════════════════════════════════════════════════════
# Bash 测试
# ═══════════════════════════════════════════════════════════════

class TestBash:
    def test_echo(self):
        result = bash.execute("echo hello")
        assert "hello" in result

    def test_exit_code(self):
        result = bash.execute("python -c \"exit(0)\"")
        assert "exit code: 0" in result

    def test_ssh_command_not_blocked(self):
        """SSH命令不再被拦截，AI写什么就执行什么"""
        result = bash.execute("ssh -o BatchMode=yes -o ConnectTimeout=1 nonexistent@127.0.0.1 echo test")
        # 应该尝试执行（可能连接失败，但不应被拦截）
        assert "禁止" not in result

    def test_delete_needs_confirm(self):
        """删除命令被拦截"""
        bash.set_confirm_callback(lambda cmd: False)  # 拒绝
        result = bash.execute("rm test.txt")
        assert "取消" in result
        bash.set_confirm_callback(None)

    def test_delete_allowed(self):
        """删除命令被允许"""
        bash.set_confirm_callback(lambda cmd: True)  # 允许
        result = bash.execute("echo deleting")
        # echo不是删除命令，直接执行
        assert "deleting" in result
        bash.set_confirm_callback(None)

    def test_no_command_translation(self):
        """命令不再被翻译：cat还是cat，不会变成type"""
        # 在Windows上，cat命令应该原样传递给shell
        # 如果翻译了，cmd会执行type，PowerShell会执行Get-Content
        # 我们验证bash模块不再有_adapt_windows_command和_adapt_powershell_command
        assert not hasattr(bash, '_adapt_windows_command')
        assert not hasattr(bash, '_adapt_powershell_command')


# ═══════════════════════════════════════════════════════════════
# TodoWrite 测试
# ═══════════════════════════════════════════════════════════════

class TestTodoWrite:
    def test_normal_create(self):
        result = todo_write.execute([
            {"content": "Task 1", "status": "in_progress", "activeForm": "Doing task 1"},
            {"content": "Task 2", "status": "pending", "activeForm": "Doing task 2"},
        ])
        assert "已更新" in result

    def test_empty_todos(self):
        result = todo_write.execute([])
        assert "错误" in result

    def test_no_in_progress(self):
        """0个in_progress现在允许（初始状态）"""
        result = todo_write.execute([
            {"content": "Task 1", "status": "pending", "activeForm": "Doing task 1"},
        ])
        assert "已更新" in result

    def test_multiple_in_progress(self):
        result = todo_write.execute([
            {"content": "Task 1", "status": "in_progress", "activeForm": "Doing task 1"},
            {"content": "Task 2", "status": "in_progress", "activeForm": "Doing task 2"},
        ])
        assert "错误" in result

    def test_missing_field(self):
        result = todo_write.execute([
            {"content": "Task 1", "status": "in_progress"},
        ])
        assert "错误" in result

    def test_invalid_status(self):
        result = todo_write.execute([
            {"content": "Task 1", "status": "running", "activeForm": "Doing task 1"},
        ])
        assert "错误" in result

    def test_completed_and_pending(self):
        result = todo_write.execute([
            {"content": "Task 1", "status": "completed", "activeForm": "Done task 1"},
            {"content": "Task 2", "status": "in_progress", "activeForm": "Doing task 2"},
            {"content": "Task 3", "status": "pending", "activeForm": "Doing task 3"},
        ])
        assert "已更新" in result


# ═══════════════════════════════════════════════════════════════
# Registry 测试
# ═══════════════════════════════════════════════════════════════

class TestRegistry:
    def test_all_9_tools_registered(self):
        names = get_tool_names()
        assert len(names) == 9
        expected = {"Read", "Glob", "Grep", "Edit", "Write", "Shell", "Terminal",
                    "WebSearch", "TodoWrite"}
        assert set(names) == expected

    def test_tool_definitions_count(self):
        defs = get_tool_definitions()
        assert len(defs) == 9

    def test_execute_known_tool(self):
        tmpdir = tempfile.mkdtemp()
        try:
            fpath = os.path.join(tmpdir, "test.txt")
            with open(fpath, "w") as f:
                f.write("hello\n")
            result, color_diff = registry_execute("Read", {"file_path": fpath})
            assert "hello" in result
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_execute_unknown_tool(self):
        result, color_diff = registry_execute("UnknownTool", {})
        assert "错误" in result

    def test_tool_definition_structure(self):
        """每个工具定义都有正确的JSON结构"""
        for defn in get_tool_definitions():
            assert defn["type"] == "function"
            func = defn["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            params = func["parameters"]
            assert params["type"] == "object"
            assert "required" in params


# ═══════════════════════════════════════════════════════════════
# 补充边界+异常测试（十轮验证深度）
# ═══════════════════════════════════════════════════════════════

class TestReadBoundary:
    """Read边界测试"""
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_offset_beyond_file(self):
        """offset超过文件行数"""
        fpath = os.path.join(self.tmpdir, "short.txt")
        with open(fpath, "w") as f:
            f.write("only one line\n")
        result = read.execute(fpath, offset=100)
        assert "错误" not in result  # 不应崩溃

    def test_read_large_offset_with_limit(self):
        """大offset+limit"""
        fpath = os.path.join(self.tmpdir, "multi.txt")
        with open(fpath, "w") as f:
            for i in range(100):
                f.write(f"line{i}\n")
        result = read.execute(fpath, offset=50, limit=10)
        assert "50→line49" in result

    def test_read_binary_fallback(self):
        """二进制文件用errors=replace不崩溃"""
        fpath = os.path.join(self.tmpdir, "binary.bin")
        with open(fpath, "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe")
        result = read.execute(fpath)
        assert "错误" not in result

    def test_read_single_line_no_newline(self):
        """单行无换行"""
        fpath = os.path.join(self.tmpdir, "nolf.txt")
        with open(fpath, "w") as f:
            f.write("no newline at end")
        result = read.execute(fpath)
        assert "no newline at end" in result


class TestEditBoundary:
    """Edit边界测试"""
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_edit_with_indentation(self):
        """缩进必须精确匹配"""
        fpath = os.path.join(self.tmpdir, "indent.py")
        with open(fpath, "w") as f:
            f.write("def foo():\n    x = 1\n    y = 2\n")
        result, color_diff = edit.execute(fpath, "    x = 1", "    x = 10")
        assert "已替换" in result

    def test_edit_indent_mismatch(self):
        """缩进不匹配时，Edit是子串匹配，'x = 1'能匹配到'    x = 1'中的子串"""
        fpath = os.path.join(self.tmpdir, "indent2.py")
        with open(fpath, "w") as f:
            f.write("def foo():\n    x = 1\n")
        # Edit是子串匹配，'x = 1'是'    x = 1'的子串，所以会匹配成功
        result, color_diff = edit.execute(fpath, "x = 1", "x = 10")
        assert "已替换" in result

    def test_edit_multiline_replacement(self):
        """多行替换"""
        fpath = os.path.join(self.tmpdir, "multi.py")
        with open(fpath, "w") as f:
            f.write("a = 1\nb = 2\nc = 3\n")
        result, color_diff = edit.execute(fpath, "a = 1\nb = 2", "a = 10\nb = 20")
        assert "已替换" in result
        with open(fpath, "r") as f:
            content = f.read()
        assert "a = 10" in content
        assert "b = 20" in content

    def test_edit_same_old_new(self):
        """old_string和new_string相同"""
        fpath = os.path.join(self.tmpdir, "same.py")
        with open(fpath, "w") as f:
            f.write("x = 1\n")
        result, color_diff = edit.execute(fpath, "x = 1", "x = 1")
        # 应该替换但无差异
        assert "已替换" in result or "无差异" in result


class TestGrepBoundary:
    """Grep边界测试"""
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_grep_head_limit(self):
        """head_limit限制输出"""
        with open(os.path.join(self.tmpdir, "many.py"), "w") as f:
            for i in range(100):
                f.write(f"x = {i}\n")
        result = grep.execute("x = ", self.tmpdir, output_mode="content", head_limit=3)
        lines = [l for l in result.split("\n") if l.strip() and "无匹配" not in l]
        assert len(lines) <= 3

    def test_grep_context_lines(self):
        """上下文行"""
        with open(os.path.join(self.tmpdir, "ctx.py"), "w") as f:
            f.write("line1\nline2\ntarget\nline4\nline5\n")
        result = grep.execute("target", self.tmpdir, output_mode="content", C=1)
        assert "line2" in result or "line4" in result

    def test_grep_empty_dir(self):
        """空目录"""
        empty_dir = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty_dir)
        result = grep.execute("pattern", empty_dir)
        assert "无匹配" in result


class TestGlobBoundary:
    """Glob边界测试"""
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_glob_deep_directory(self):
        """深层目录"""
        deep = os.path.join(self.tmpdir, "a", "b", "c", "d")
        os.makedirs(deep)
        with open(os.path.join(deep, "deep.py"), "w") as f:
            f.write("")
        result = glob.execute("**/*.py", self.tmpdir)
        assert "deep.py" in result

    def test_glob_ignore_dirs(self):
        """忽略.git/__pycache__"""
        git_dir = os.path.join(self.tmpdir, ".git", "objects")
        os.makedirs(git_dir)
        with open(os.path.join(git_dir, "data.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.tmpdir, "real.py"), "w") as f:
            f.write("")
        result = glob.execute("**/*.py", self.tmpdir)
        assert "real.py" in result
        assert "data.py" not in result


class TestBashBoundary:
    """Bash边界测试"""
    def test_stderr(self):
        """stderr输出"""
        result = bash.execute("python -c \"import sys; sys.stderr.write('err msg\\n')\"")
        assert "err msg" in result

    def test_nonzero_exit(self):
        """非零退出码"""
        result = bash.execute("python -c \"exit(1)\"")
        assert "exit code: 1" in result

    def test_multiline_output(self):
        """多行输出"""
        result = bash.execute("python -c \"for i in range(5): print(f'line{i}')\"")
        for i in range(5):
            assert f"line{i}" in result


class TestTodoWriteBoundary:
    """TodoWrite边界测试"""
    def test_many_tasks(self):
        """大量任务"""
        todos = [{"content": f"Task {i}", "status": "pending", "activeForm": f"Doing {i}"} for i in range(50)]
        todos[0]["status"] = "in_progress"
        result = todo_write.execute(todos)
        assert "已更新" in result

    def test_all_completed_except_one(self):
        """全部完成+1个in_progress"""
        todos = [
            {"content": "Done 1", "status": "completed", "activeForm": "Done 1"},
            {"content": "Done 2", "status": "completed", "activeForm": "Done 2"},
            {"content": "Current", "status": "in_progress", "activeForm": "Doing current"},
        ]
        result = todo_write.execute(todos)
        assert "已更新" in result

    def test_non_dict_item(self):
        """非字典项"""
        result = todo_write.execute(["not a dict"])
        assert "错误" in result


# ═══════════════════════════════════════════════════════════════
# Diff 着色测试
# ═══════════════════════════════════════════════════════════════

from narnat_agent.ui.ui_design import colorize_diff, R, X, E, C, D, G, B


class TestColorizeDiff:
    """colorize_diff 着色函数测试"""

    def test_empty_diff(self):
        result = colorize_diff("")
        assert "(无差异)" in result

    def test_no_diff_text(self):
        result = colorize_diff("(无差异)")
        assert "(无差异)" in result

    def test_delete_line_red(self):
        """-行着红色"""
        diff = "-old line"
        result = colorize_diff(diff)
        assert X in result  # RED
        assert "old line" in result

    def test_add_line_green(self):
        """+行着绿色"""
        diff = "+new line"
        result = colorize_diff(diff)
        assert E in result  # GREEN
        assert "new line" in result

    def test_hunk_header_cyan(self):
        """@@行着青色暗淡"""
        diff = "@@ -1,3 +1,3 @@"
        result = colorize_diff(diff)
        assert C in result  # CYAN
        assert D in result  # DIM

    def test_file_header_bold_cyan(self):
        """---和+++行着粗体青色"""
        diff = "--- a/file.py\n+++ b/file.py"
        result = colorize_diff(diff)
        assert B in result  # BOLD
        assert C in result  # CYAN

    def test_context_line_gray(self):
        """上下文行着灰色"""
        diff = " unchanged line"
        result = colorize_diff(diff)
        assert G in result  # GRAY

    def test_full_diff(self):
        """完整diff着色"""
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1,3 +1,3 @@\n line1\n-old line\n+new line"
        result = colorize_diff(diff)
        # 验证所有行都被着色
        assert "--- a/test.py" in result
        assert "+++ b/test.py" in result
        assert "@@ -1,3 +1,3 @@" in result
        assert "old line" in result
        assert "new line" in result


class TestEditColorDiff:
    """Edit工具返回着色diff的测试"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "edit_test.py")
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write("def hello():\n    print('hello')\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_edit_returns_color_diff(self):
        """Edit返回的color_diff非空且包含ANSI着色"""
        result, color_diff = edit.execute(
            self.test_file, "print('hello')", "print('world')")
        assert "已替换" in result
        assert color_diff  # 非空
        assert X in color_diff  # 红色（删除行）
        assert E in color_diff  # 绿色（添加行）

    def test_edit_error_returns_empty_color_diff(self):
        """Edit错误时color_diff为空"""
        result, color_diff = edit.execute(
            self.test_file, "nonexistent", "new")
        assert "错误" in result
        assert color_diff == ""


class TestWriteColorDiff:
    """Write工具返回着色diff的测试"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        write._read_files.clear()

    def test_write_new_file_no_color_diff(self):
        """新建文件时color_diff为空"""
        fpath = os.path.join(self.tmpdir, "new.py")
        result, color_diff = write.execute(fpath, "print('hello')")
        assert "已写入" in result
        assert color_diff == ""

    def test_write_overwrite_has_color_diff(self):
        """覆写已有文件时color_diff非空"""
        fpath = os.path.join(self.tmpdir, "existing.py")
        with open(fpath, "w") as f:
            f.write("old content\n")
        write.mark_read(fpath)
        result, color_diff = write.execute(fpath, "new content\n")
        assert "已写入" in result
        assert color_diff  # 非空
        assert X in color_diff  # 红色（删除行）
        assert E in color_diff  # 绿色（添加行）


# ═══════════════════════════════════════════════════════════════
# Terminal 暴力测试
# ═══════════════════════════════════════════════════════════════

class TestTerminalBasic:
    """Terminal工具基础测试（不需要真实SSH连接）"""

    def test_status_no_sessions(self):
        """无会话时status返回空"""
        terminal._sessions.clear()
        result = terminal.execute(action="status")
        assert "无活跃" in result

    def test_connect_missing_params(self):
        """connect缺少host或username"""
        result = terminal.execute(action="connect")
        assert "错误" in result
        result = terminal.execute(action="connect", host="1.2.3.4")
        assert "错误" in result

    def test_exec_no_command(self):
        """exec缺少command"""
        result = terminal.execute(action="exec", host="1.2.3.4")
        assert "错误" in result

    def test_exec_no_session(self):
        """exec时无活跃会话"""
        terminal._sessions.clear()
        result = terminal.execute(action="exec", command="ls")
        assert "错误" in result

    def test_close_no_host(self):
        """close无host时关闭所有（即使为空也不崩溃）"""
        terminal._sessions.clear()
        result = terminal.execute(action="close")
        assert "已关闭" in result

    def test_unknown_action(self):
        """未知action"""
        result = terminal.execute(action="invalid")
        assert "错误" in result

    def test_input_no_content(self):
        """input缺少内容"""
        result = terminal.execute(action="input")
        assert "错误" in result

    def test_input_no_session(self):
        """input时无活跃会话"""
        terminal._sessions.clear()
        result = terminal.execute(action="input", input="mypassword")
        assert "错误" in result

    def test_connect_unreachable_host(self):
        """连接不可达主机应返回错误而非崩溃"""
        result = terminal.execute(
            action="connect",
            host="192.0.2.1",  # TEST-NET-1, 不可路由
            username="test",
            password="test",
        )
        assert "错误" in result or "失败" in result

    def test_connect_invalid_credentials(self):
        """错误凭据应返回认证错误"""
        # 连接localhost但用错误用户名
        result = terminal.execute(
            action="connect",
            host="127.0.0.1",
            username="nonexistent_user_xyz",
            password="wrong_password",
        )
        assert "错误" in result or "失败" in result


class TestTerminalSessionManagement:
    """Terminal会话管理测试"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_status_empty(self):
        result = terminal.execute(action="status")
        assert "无活跃" in result

    def test_close_nonexistent_host(self):
        """关闭不存在的会话"""
        result = terminal.execute(action="close", host="10.0.0.1")
        assert "未找到" in result

    def test_exec_with_single_session_auto_select(self):
        """只有一个会话时exec自动选择（模拟：无真实连接，测试逻辑）"""
        # 这个测试验证当只有一个session时，不指定host也能自动选择
        # 由于没有真实SSH，我们直接测试_sessions为空时的错误
        result = terminal.execute(action="exec", command="ls")
        assert "错误" in result  # 无会话


class TestTerminalBrutal:
    """Terminal暴力测试：极端场景"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_rapid_connect_failures(self):
        """快速连续连接失败不崩溃"""
        for i in range(10):
            result = terminal.execute(
                action="connect",
                host=f"192.0.2.{i}",
                username="test",
                password="test",
            )
            # 每次都应返回错误，不应崩溃
            assert isinstance(result, str)

    def test_exec_without_connect(self):
        """未连接就exec"""
        for i in range(5):
            result = terminal.execute(action="exec", command=f"cmd{i}")
            assert "错误" in result

    def test_status_after_many_failed_connects(self):
        """多次失败连接后status仍正常"""
        for i in range(5):
            terminal.execute(
                action="connect",
                host=f"192.0.2.{i}",
                username="test",
                password="test",
            )
        result = terminal.execute(action="status")
        assert isinstance(result, str)

    def test_close_all_repeatedly(self):
        """反复关闭所有会话不崩溃"""
        for _ in range(5):
            result = terminal.execute(action="close")
            assert "已关闭" in result

    def test_mixed_actions_rapid(self):
        """快速混合操作不崩溃"""
        actions = [
            {"action": "status"},
            {"action": "exec", "command": "ls"},
            {"action": "close"},
            {"action": "connect", "host": "192.0.2.1", "username": "test", "password": "test"},
            {"action": "status"},
        ]
        for kwargs in actions:
            result = terminal.execute(**kwargs)
            assert isinstance(result, str)
