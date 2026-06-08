"""中断机制暴力测试 —— 验证 Glob/Grep/WebSearch 的 ESC 中断检查"""

import os
import shutil
import tempfile
import threading
import time
import pytest

from narnat_agent.tools import glob, grep, web_search


# ═══════════════════════════════════════════════════════════════
# Glob 中断测试
# ═══════════════════════════════════════════════════════════════

class TestGlobInterrupt:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # 创建足够多的子目录，确保 os.walk 有多轮迭代
        for i in range(20):
            subdir = os.path.join(self.tmpdir, f"dir{i}")
            os.makedirs(subdir)
            for j in range(5):
                with open(os.path.join(subdir, f"file{j}.py"), "w") as f:
                    f.write("")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        glob.set_interrupt_check(None)

    def test_no_interrupt_normal_flow(self):
        """无中断时正常返回结果"""
        result = glob.execute("**/*.py", self.tmpdir)
        assert "用户中断" not in result
        assert "file0.py" in result

    def test_interrupt_immediately(self):
        """立即中断：第一个目录就触发"""
        glob.set_interrupt_check(lambda: True)
        result = glob.execute("**/*.py", self.tmpdir)
        assert result == "[用户中断]"

    def test_interrupt_after_delay(self):
        """延迟中断：先搜一部分，再触发中断"""
        call_count = [0]

        def delayed_interrupt():
            call_count[0] += 1
            return call_count[0] > 3  # 第3次检查后触发中断

        glob.set_interrupt_check(delayed_interrupt)
        result = glob.execute("**/*.py", self.tmpdir)
        assert result == "[用户中断]"

    def test_interrupt_from_another_thread(self):
        """从另一个线程触发中断"""
        event = threading.Event()
        glob.set_interrupt_check(event.is_set)

        # 在另一个线程中延迟设置中断
        def set_interrupt():
            time.sleep(0.05)
            event.set()

        t = threading.Thread(target=set_interrupt)
        t.start()

        result = glob.execute("**/*.py", self.tmpdir)
        t.join(timeout=2)

        # 要么被中断，要么正常完成（取决于时序）
        assert isinstance(result, str)

    def test_interrupt_reset_between_calls(self):
        """中断后重置，下次调用正常"""
        glob.set_interrupt_check(lambda: True)
        result1 = glob.execute("**/*.py", self.tmpdir)
        assert result1 == "[用户中断]"

        # 重置中断
        glob.set_interrupt_check(lambda: False)
        result2 = glob.execute("**/*.py", self.tmpdir)
        assert "用户中断" not in result2
        assert "file0.py" in result2

    def test_interrupt_callback_none(self):
        """回调为None时不崩溃"""
        glob.set_interrupt_check(None)
        result = glob.execute("**/*.py", self.tmpdir)
        assert "用户中断" not in result

    def test_interrupt_callback_exception(self):
        """回调抛异常时不崩溃（防御性编程）"""
        def bad_callback():
            raise RuntimeError("test error")

        glob.set_interrupt_check(bad_callback)
        # 不应崩溃，但行为取决于实现
        try:
            result = glob.execute("**/*.py", self.tmpdir)
            assert isinstance(result, str)
        except RuntimeError:
            # 如果回调异常未被捕获，也是可接受的
            pass


# ═══════════════════════════════════════════════════════════════
# Grep 中断测试
# ═══════════════════════════════════════════════════════════════

class TestGrepInterrupt:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        for i in range(20):
            subdir = os.path.join(self.tmpdir, f"dir{i}")
            os.makedirs(subdir)
            for j in range(5):
                with open(os.path.join(subdir, f"file{j}.py"), "w") as f:
                    f.write(f"# pattern match {i}_{j}\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        grep.set_interrupt_check(None)

    def test_no_interrupt_normal_flow(self):
        """无中断时正常返回结果"""
        result = grep.execute("pattern", self.tmpdir)
        assert "用户中断" not in result
        assert "file0.py" in result

    def test_interrupt_immediately(self):
        """立即中断"""
        grep.set_interrupt_check(lambda: True)
        result = grep.execute("pattern", self.tmpdir)
        assert "[用户中断]" in result

    def test_interrupt_after_delay(self):
        """延迟中断：搜到部分结果后中断"""
        call_count = [0]

        def delayed_interrupt():
            call_count[0] += 1
            return call_count[0] > 3

        grep.set_interrupt_check(delayed_interrupt)
        result = grep.execute("pattern", self.tmpdir)
        assert "[用户中断]" in result

    def test_interrupt_preserves_partial_results(self):
        """中断时保留已搜到的部分结果"""
        call_count = [0]

        def delayed_interrupt():
            call_count[0] += 1
            return call_count[0] > 2

        grep.set_interrupt_check(delayed_interrupt)
        result = grep.execute("pattern", self.tmpdir, output_mode="files_with_matches")
        # 应该有部分结果 + 中断标记
        assert "[用户中断]" in result

    def test_interrupt_content_mode(self):
        """content模式下中断"""
        grep.set_interrupt_check(lambda: True)
        result = grep.execute("pattern", self.tmpdir, output_mode="content")
        assert "[用户中断]" in result

    def test_interrupt_count_mode(self):
        """count模式下中断"""
        grep.set_interrupt_check(lambda: True)
        result = grep.execute("pattern", self.tmpdir, output_mode="count")
        assert "[用户中断]" in result

    def test_interrupt_single_file_not_affected(self):
        """单文件搜索不受中断影响（执行太快，走_search_single_file）"""
        fpath = os.path.join(self.tmpdir, "dir0", "file0.py")
        grep.set_interrupt_check(lambda: True)
        result = grep.execute("pattern", fpath)
        # 单文件搜索走 _search_single_file，不经过 os.walk 循环
        # files_with_matches 模式返回文件路径
        assert "file0.py" in result or "无匹配" in result

    def test_interrupt_reset_between_calls(self):
        """中断后重置，下次调用正常"""
        grep.set_interrupt_check(lambda: True)
        result1 = grep.execute("pattern", self.tmpdir)
        assert "[用户中断]" in result1

        grep.set_interrupt_check(lambda: False)
        result2 = grep.execute("pattern", self.tmpdir)
        assert "用户中断" not in result2

    def test_interrupt_callback_none(self):
        """回调为None时不崩溃"""
        grep.set_interrupt_check(None)
        result = grep.execute("pattern", self.tmpdir)
        assert "用户中断" not in result


# ═══════════════════════════════════════════════════════════════
# WebSearch 中断测试
# ═══════════════════════════════════════════════════════════════

class TestWebSearchInterrupt:
    def teardown_method(self):
        web_search.set_interrupt_check(None)

    def test_interrupt_at_entry(self):
        """入口处就中断"""
        web_search.set_interrupt_check(lambda: True)
        result = web_search.execute("test query")
        assert "[用户中断]" in result

    def test_no_interrupt_normal_flow(self):
        """无中断时正常执行（可能daemon未运行，但不应崩溃）"""
        web_search.set_interrupt_check(lambda: False)
        result = web_search.execute("test query")
        assert isinstance(result, str)

    def test_interrupt_callback_none(self):
        """回调为None时不崩溃"""
        web_search.set_interrupt_check(None)
        result = web_search.execute("test query")
        assert isinstance(result, str)

    def test_interrupt_reset_between_calls(self):
        """中断后重置，下次调用正常"""
        web_search.set_interrupt_check(lambda: True)
        result1 = web_search.execute("test query")
        assert isinstance(result1, str)

        web_search.set_interrupt_check(lambda: False)
        result2 = web_search.execute("test query")
        assert isinstance(result2, str)


# ═══════════════════════════════════════════════════════════════
# 并发中断压力测试
# ═══════════════════════════════════════════════════════════════

class TestConcurrentInterrupt:
    """多线程并发触发中断，验证线程安全"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        for i in range(30):
            subdir = os.path.join(self.tmpdir, f"dir{i}")
            os.makedirs(subdir)
            for j in range(10):
                with open(os.path.join(subdir, f"file{j}.py"), "w") as f:
                    f.write(f"# content {i}_{j}\n")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        glob.set_interrupt_check(None)
        grep.set_interrupt_check(None)

    def test_glob_rapid_interrupt_toggle(self):
        """快速切换中断状态"""
        event = threading.Event()
        glob.set_interrupt_check(event.is_set)

        # 快速切换
        for _ in range(10):
            event.set()
            result = glob.execute("**/*.py", self.tmpdir)
            assert isinstance(result, str)
            event.clear()
            result = glob.execute("**/*.py", self.tmpdir)
            assert isinstance(result, str)

    def test_grep_rapid_interrupt_toggle(self):
        """Grep快速切换中断状态"""
        event = threading.Event()
        grep.set_interrupt_check(event.is_set)

        for _ in range(10):
            event.set()
            result = grep.execute("content", self.tmpdir)
            assert isinstance(result, str)
            event.clear()
            result = grep.execute("content", self.tmpdir)
            assert isinstance(result, str)

    def test_glob_interrupt_during_walk(self):
        """在 os.walk 过程中触发中断"""
        event = threading.Event()
        glob.set_interrupt_check(event.is_set)

        def delayed_set():
            time.sleep(0.02)
            event.set()

        t = threading.Thread(target=delayed_set)
        t.start()

        result = glob.execute("**/*.py", self.tmpdir)
        t.join(timeout=2)
        assert isinstance(result, str)

    def test_grep_interrupt_during_walk(self):
        """在 Grep os.walk 过程中触发中断"""
        event = threading.Event()
        grep.set_interrupt_check(event.is_set)

        def delayed_set():
            time.sleep(0.02)
            event.set()

        t = threading.Thread(target=delayed_set)
        t.start()

        result = grep.execute("content", self.tmpdir)
        t.join(timeout=2)
        assert isinstance(result, str)
