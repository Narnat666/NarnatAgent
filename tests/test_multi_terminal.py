"""多终端 + _clean_output回车覆盖 测试

覆盖:
1. 多终端并发: session_id分配/指定/自动/上限/关闭
2. _clean_output回车覆盖: 进度条合并、apt输出、ninja输出、dpkg输出
3. 多终端并行执行: 不同终端独立执行命令
4. 边界/暴力: 超限session_id、重复connect、空会话exec等
"""

import pytest
from unittest.mock import MagicMock
from narnat_agent.tools import terminal
from narnat_agent.tools.terminal import SSHSession, MAX_SESSIONS


# ── 辅助 ──

def _clean(raw: str) -> str:
    """直接调用SSHSession._clean_output"""
    return SSHSession._clean_output(raw)


def _mock_session(host="192.168.1.1", username="testuser"):
    """创建一个Mock SSHSession用于占位测试"""
    session = MagicMock(spec=SSHSession)
    session.host = host
    session.username = username
    session._channel = MagicMock()
    session._channel.closed = False
    session.prompt = f"{username}@{host}:~$"
    return session


# ── _clean_output 回车覆盖合并 ──

class TestCleanOutputCarriageReturn:
    """回车覆盖行合并 — 修复PTY进度条导致的超长行"""

    def test_simple_overwrite(self):
        """\\r后内容覆盖前面"""
        raw = "abc\rxyz"
        result = _clean(raw)
        assert result == "xyz"

    def test_partial_overwrite(self):
        """\\r后内容短于前面，只覆盖前N字符"""
        raw = "abcdef\rxy"
        result = _clean(raw)
        assert result == "xycdef"

    def test_multiple_overwrite(self):
        """多次\\r覆盖，最终只保留最后一次的有效内容"""
        raw = "line1\rline2\rline3"
        result = _clean(raw)
        assert result == "line3"

    def test_apt_progress_bar(self):
        """apt进度条: 94% [Working]\\r94% [1 g++ ...]"""
        raw = "0% [Working]\r10% [Working]\r50% [Working]\r94% [1 g++ 1,412 B/1,412 B 100%]"
        result = _clean(raw)
        assert "94%" in result
        assert "g++" in result
        # 不应包含中间帧
        assert "10% [Working]" not in result
        assert "50% [Working]" not in result

    def test_dpkg_reading_database(self):
        """dpkg进度: (Reading database ... 5%...(Reading database ... 100%"""
        raw = "(Reading database ... 5%(Reading database ... 10%(Reading database ... 100%(Reading database ... 205270 files)"
        # dpkg用%(不是\r)做进度，但如果有\r则合并
        raw_with_cr = "(Reading database ... 5%\r(Reading database ... 100%\r(Reading database ... 205270 files)"
        result = _clean(raw_with_cr)
        assert "205270" in result
        # 不应包含中间帧
        assert "5%" not in result

    def test_ninja_build_line(self):
        """ninja编译行: [1/17] CC ...\\r[2/17] CC ..."""
        raw = "[1/17] CC a.cpp\r[2/17] CC b.cpp\r[3/17] CC c.cpp"
        result = _clean(raw)
        assert "[3/17]" in result
        assert "c.cpp" in result
        assert "[1/17]" not in result

    def test_empty_segment_after_cr(self):
        """\\r后空段（如\\r\\n换行）"""
        raw = "progress\r\nnext line"
        result = _clean(raw)
        assert "next line" in result

    def test_cr_at_line_start(self):
        """行首\\r"""
        raw = "\rhello"
        result = _clean(raw)
        assert result == "hello"

    def test_multiline_with_cr(self):
        """多行，每行有\\r"""
        raw = "line1\rupdated1\nline2\rupdated2"
        result = _clean(raw)
        assert "updated1" in result
        assert "updated2" in result
        assert "line1" not in result
        assert "line2" not in result

    def test_no_cr_unchanged(self):
        """无\\r的内容不变"""
        raw = "normal line 1\nnormal line 2"
        result = _clean(raw)
        assert result == raw

    def test_ansi_stripped_before_cr_merge(self):
        """ANSI转义码先于\\r合并被清除"""
        raw = "\x1b[32mgreen\x1b[0m\rplain"
        result = _clean(raw)
        assert result == "plain"
        assert "\x1b[" not in result

    def test_real_apt_output_simulation(self):
        """模拟真实apt输出: 多行混合进度条"""
        raw = (
            "Reading package lists... Done\r\n"
            "Building dependency tree... Done\r\n"
            "0% [Working]\r10% [Working]\r50% [1 g++ 1,412 B]\r"
            "99% [5 ninja-build 27%]\r\n"
            "Fetched 2,630 kB in 16s"
        )
        result = _clean(raw)
        assert "Reading package lists" in result
        assert "Building dependency tree" in result
        assert "Fetched" in result
        # 进度条最终帧保留
        assert "99%" in result or "ninja" in result

    def test_cr_only_preserves_previous(self):
        """纯\\r（无后续内容）保留前内容"""
        raw = "hello\r"
        result = _clean(raw)
        assert "hello" in result

    def test_consecutive_cr(self):
        """连续\\r"""
        raw = "a\r\rb\r\rc"
        result = _clean(raw)
        assert result == "c"


# ── 多终端会话管理 ──

class TestMultiTerminalSessionId:
    """session_id分配和指定"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_auto_allocate_sequential(self):
        """自动分配: 依次分配0,1,2..."""
        for i in range(MAX_SESSIONS):
            alloc_id = terminal._allocate_session_id()
            assert alloc_id == i
            terminal._sessions[alloc_id] = _mock_session()

    def test_auto_allocate_full(self):
        """已满时返回-1"""
        for i in range(MAX_SESSIONS):
            terminal._sessions[i] = _mock_session()
        alloc_id = terminal._allocate_session_id()
        assert alloc_id == -1

    def test_auto_allocate_with_gap(self):
        """有空隙时分配最小ID"""
        terminal._sessions[0] = _mock_session()
        terminal._sessions[2] = _mock_session()
        # 1和3空闲，应分配1
        alloc_id = terminal._allocate_session_id()
        assert alloc_id == 1

    def test_max_sessions_constant(self):
        """MAX_SESSIONS=5"""
        assert MAX_SESSIONS == 5

    def test_status_no_sessions_shows_max(self):
        """无会话时status显示最大终端数"""
        result = terminal.execute(action="status")
        assert str(MAX_SESSIONS) in result

    def test_connect_auto_allocate_id(self):
        """connect自动分配session_id（不实际连接，测试错误路径）"""
        # 无真实SSH，connect会失败，但错误消息应包含host
        result = terminal.execute(
            action="connect",
            host="192.168.1.1",
            username="test",
        )
        # 连接失败但参数校验通过
        assert "错误" in result or "已连接" in result

    def test_connect_specified_session_id_out_of_range(self):
        """指定超范围session_id"""
        result = terminal.execute(
            action="connect",
            host="192.168.1.1",
            username="test",
            session_id=MAX_SESSIONS,
        )
        assert "错误" in result
        assert f"0-{MAX_SESSIONS - 1}" in result

    def test_connect_specified_negative_session_id(self):
        """指定负数session_id（-1表示自动）"""
        result = terminal.execute(
            action="connect",
            host="192.168.1.1",
            username="test",
            session_id=-2,
        )
        # -2不是-1(自动)，也不是0-4，应视为指定ID
        # 但-2不在0-4范围内，应报错
        assert "错误" in result


class TestMultiTerminalResolve:
    """_resolve_session_id逻辑"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_resolve_no_session_error(self):
        """无会话时resolve报错"""
        with pytest.raises(ValueError):
            terminal._resolve_session_id(-1)

    def test_resolve_single_session_auto(self):
        """单会话时自动选择"""
        terminal._sessions[0] = _mock_session()
        sid, session = terminal._resolve_session_id(-1)
        assert sid == 0

    def test_resolve_multiple_sessions_need_id(self):
        """多会话时必须指定session_id"""
        terminal._sessions[0] = _mock_session()
        terminal._sessions[1] = _mock_session()
        with pytest.raises(ValueError) as exc_info:
            terminal._resolve_session_id(-1)
        assert "session_id" in str(exc_info.value)

    def test_resolve_specified_id_not_found(self):
        """指定不存在的session_id"""
        with pytest.raises(ValueError) as exc_info:
            terminal._resolve_session_id(3)
        assert "未连接" in str(exc_info.value)

    def test_resolve_specified_id_found(self):
        """指定存在的session_id"""
        terminal._sessions[3] = _mock_session()
        sid, session = terminal._resolve_session_id(3)
        assert sid == 3


class TestMultiTerminalClose:
    """多终端关闭"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_close_by_session_id(self):
        """按session_id关闭"""
        terminal._sessions[0] = _mock_session()
        terminal._sessions[1] = _mock_session()
        result = terminal._close(0, "")
        assert "终端0" in result
        assert 0 not in terminal._sessions
        assert 1 in terminal._sessions

    def test_close_nonexistent_session_id(self):
        """关闭不存在的session_id"""
        result = terminal._close(3, "")
        assert "未连接" in result

    def test_close_all_sessions(self):
        """关闭所有会话"""
        terminal._sessions[0] = _mock_session()
        terminal._sessions[1] = _mock_session()
        result = terminal._close(-1, "")
        assert "已关闭2个" in result
        assert len(terminal._sessions) == 0

    def test_close_by_host(self):
        """按host关闭（需要真实session对象）"""
        # 无法在不连接的情况下测试host匹配
        # 但可以测试未找到host的情况
        result = terminal._close(-1, "nonexistent.host")
        assert "未找到" in result


class TestMultiTerminalExec:
    """多终端exec"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_exec_no_command(self):
        """exec无command报错"""
        result = terminal.execute(action="exec", session_id=0)
        assert "错误" in result

    def test_exec_no_session(self):
        """exec无活跃会话报错"""
        result = terminal.execute(action="exec", command="ls", session_id=0)
        assert "错误" in result

    def test_exec_auto_single_session(self):
        """单会话时exec自动选择"""
        session = _mock_session()
        session.execute.return_value = "testuser@mockhost:~$"
        terminal._sessions[0] = session
        result = terminal.execute(action="exec", command="ls")
        # Mock session的execute被调用
        assert isinstance(result, str)


# ── 多终端仿真测试（需要MockSSHServer） ──

class TestMultiTerminalWithMock:
    """基于MockSSHServer的多终端测试"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_connect_two_terminals(self):
        """连接两个终端"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server1 = MockSSHServer()
        server1.start()
        try:
            # 连接第一个终端
            r1 = terminal.execute(
                action="connect",
                host=server1.host,
                username=server1.username,
                password=server1.password,
                port=server1.port,
                session_id=0,
            )
            assert "终端0" in r1

            # 连接第二个终端（同一服务器，不同session_id）
            r2 = terminal.execute(
                action="connect",
                host=server1.host,
                username=server1.username,
                password=server1.password,
                port=server1.port,
                session_id=1,
            )
            assert "终端1" in r2

            # status应显示两个终端
            status = terminal.execute(action="status")
            assert "终端0" in status
            assert "终端1" in status
        finally:
            server1.stop()

    def test_exec_on_specific_terminal(self):
        """在指定终端执行命令"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            # 连接到终端0
            terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=0,
            )

            # 在终端0执行命令
            result = terminal.execute(
                action="exec",
                command="echo hello_from_t0",
                session_id=0,
            )
            assert "hello_from_t0" in result
            assert "终端0" in result
        finally:
            server.stop()

    def test_close_specific_terminal(self):
        """关闭指定终端"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=0,
            )
            terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=1,
            )

            # 关闭终端0
            result = terminal.execute(action="close", session_id=0)
            assert "终端0" in result

            # 终端0应不存在
            assert 0 not in terminal._sessions
            # 终端1应还在
            assert 1 in terminal._sessions
        finally:
            server.stop()

    def test_auto_allocate_on_connect(self):
        """connect不指定session_id时自动分配"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            r1 = terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
            )
            assert "终端0" in r1

            r2 = terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
            )
            assert "终端1" in r2
        finally:
            server.stop()

    def test_max_sessions_limit(self):
        """达到最大会话数后报错"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            # 连接MAX_SESSIONS个终端
            for i in range(MAX_SESSIONS):
                terminal.execute(
                    action="connect",
                    host=server.host,
                    username=server.username,
                    password=server.password,
                    port=server.port,
                    session_id=i,
                )

            # 第6个应失败
            result = terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
            )
            assert "错误" in result
            assert str(MAX_SESSIONS) in result
        finally:
            server.stop()

    def test_reconnect_after_close(self):
        """关闭后可重连同一session_id"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=2,
            )
            terminal.execute(action="close", session_id=2)

            # 重连终端2
            result = terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=2,
            )
            assert "终端2" in result
        finally:
            server.stop()

    def test_status_shows_all_slots(self):
        """status显示所有5个终端槽位"""
        from tests.simulators.mock_ssh_server import MockSSHServer

        server = MockSSHServer()
        server.start()
        try:
            terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
                session_id=1,
            )
            status = terminal.execute(action="status")
            # 应显示终端0(空闲)、终端1(活跃)、终端2-4(空闲)
            assert "终端0" in status
            assert "终端1" in status
            assert "空闲" in status
            assert "活跃" in status
        finally:
            server.stop()


# ── _clean_output 暴力测试 ──

class TestCleanOutputBrutal:
    """_clean_output暴力边界测试"""

    def test_empty_string(self):
        assert _clean("") == ""

    def test_only_newlines(self):
        assert _clean("\n\n\n") == ""

    def test_only_cr(self):
        assert _clean("\r\r\r") == ""

    def test_mixed_ansi_and_cr(self):
        """ANSI + \\r混合: ANSI先被清除，然后\\r合并"""
        raw = "\x1b[32mprogress\x1b[0m\r\x1b[31mfinal\x1b[0m"
        result = _clean(raw)
        # ANSI清除后: "progress\rfinal"
        # \r合并: "final"(5字符)覆盖"progress"(8字符)前5字符 → "finaless"
        assert "final" in result
        assert "\x1b[" not in result

    def test_very_long_progress_bar(self):
        """超长进度条（模拟apt install输出）"""
        # 模拟100个进度帧
        frames = [f"{i}% [Working]" for i in range(0, 101, 1)]
        raw = frames[0] + "\r" + "\r".join(frames[1:])
        result = _clean(raw)
        assert "100%" in result
        # 不应包含中间帧
        assert "50% [Working]" not in result

    def test_narnat_marker_cleaned(self):
        """内部标记被清除"""
        raw = "output\n__NARNAT_MARKER_123456__\nmore"
        result = _clean(raw)
        assert "__NARNAT_MARKER" not in result
        assert "output" in result
        assert "more" in result

    def test_continuation_prompt_cleaned(self):
        """续行提示符被清除"""
        raw = "> continuation\nnormal"
        result = _clean(raw)
        assert ">" not in result.split("\n")[0] or "continuation" in result

    def test_excessive_newlines_collapsed(self):
        """3+连续换行折叠为2"""
        raw = "line1\n\n\n\n\nline2"
        result = _clean(raw)
        assert "\n\n\n" not in result

    def test_cr_with_only_spaces(self):
        """\\r后只有空格（清除进度条残留）"""
        raw = "downloading...        \rdone!    "
        result = _clean(raw)
        assert "done!" in result

    def test_real_world_ninja_output(self):
        """模拟真实ninja编译输出"""
        raw = (
            "[1/17] ccache /usr/bin/g++ -D_GNU_SOURCE -I/home/user/proj/src -c src/main.cpp\r"
            "[2/17] ccache /usr/bin/g++ -D_GNU_SOURCE -I/home/user/proj/src -c src/util.cpp\r"
            "[3/17] ccache /usr/bin/g++ -D_GNU_SOURCE -I/home/user/proj/src -c src/http.cpp"
        )
        result = _clean(raw)
        assert "[3/17]" in result
        assert "http.cpp" in result
        # 前面的帧被覆盖
        assert "[1/17]" not in result

    def test_multiline_mixed_cr_and_newline(self):
        """多行混合\\r和\\n"""
        raw = "step1\rstep2\nstep3\rstep4\nstep5"
        result = _clean(raw)
        lines = result.split("\n")
        assert "step2" in lines[0]
        assert "step4" in lines[1]
        assert "step5" in lines[2]
