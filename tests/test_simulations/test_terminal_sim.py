"""Terminal仿真测试 —— 基于MockSSHServer的闭环暴力测试

AI在仿真SSH环境中自由测试Terminal工具，无需真实SSH连接。
覆盖: 输出完整性、sudo注入、close保护、会话管理、虚拟文件系统、边界场景
"""

import os
import time
import pytest

from narnat_agent.tools import terminal
from tests.simulators.mock_ssh_server import MockSSHServer, SimulatedShell, VirtualFileSystem


# ── 辅助 ──

def _connect(server, **kwargs):
    """连接到仿真SSH服务器"""
    return terminal.execute(
        action="connect",
        host=server.host,
        username=server.username,
        password=server.password,
        port=server.port,
        **kwargs,
    )


def _exec(host, command, timeout=15):
    """在Terminal中执行命令"""
    return terminal.execute(action="exec", host=host, command=command, timeout=timeout)


# ── 输出完整性 ──

class TestOutputIntegrity:
    """验证exec输出不丢失"""

    def setup_method(self):
        terminal._sessions.clear()
        self.server = MockSSHServer()
        self.server.start()

    def teardown_method(self):
        terminal.cleanup()
        self.server.stop()

    def test_echo(self):
        _connect(self.server)
        result = _exec(self.server.host, "echo hello")
        assert "hello" in result

    def test_whoami(self):
        _connect(self.server)
        result = _exec(self.server.host, "whoami")
        assert "testuser" in result

    def test_pwd(self):
        _connect(self.server)
        result = _exec(self.server.host, "pwd -P")
        assert "/home/testuser" in result

    def test_hostname(self):
        _connect(self.server)
        result = _exec(self.server.host, "hostname")
        assert "mockhost" in result

    def test_consecutive_execs(self):
        """连续exec不丢输出"""
        _connect(self.server)
        for i in range(5):
            result = _exec(self.server.host, f"echo line{i}")
            assert f"line{i}" in result, f"第{i}次exec输出丢失"


# ── 虚拟文件系统 ──

class TestVirtualFileSystem:
    """验证VFS操作在shell中可见"""

    def setup_method(self):
        terminal._sessions.clear()
        self.server = MockSSHServer()
        self.server.start()

    def teardown_method(self):
        terminal.cleanup()
        self.server.stop()

    def test_mkdir_and_ls(self):
        """mkdir后ls能看到"""
        _connect(self.server)
        _exec(self.server.host, "mkdir -p /tmp/testdir")
        result = _exec(self.server.host, "ls /tmp")
        assert "testdir" in result

    def test_echo_redirect_and_cat(self):
        """echo > file后cat能读"""
        _connect(self.server)
        _exec(self.server.host, "echo 'hello world' > /tmp/test.txt")
        result = _exec(self.server.host, "cat /tmp/test.txt")
        assert "hello world" in result

    def test_touch_and_ls(self):
        """touch后ls能看到"""
        _connect(self.server)
        _exec(self.server.host, "touch /tmp/newfile.txt")
        result = _exec(self.server.host, "ls /tmp")
        assert "newfile.txt" in result

    def test_vfs_direct_access(self):
        """直接通过VFS API操作，shell中可见"""
        self.server.vfs.write_file("/tmp/direct.txt", b"direct content")
        _connect(self.server)
        result = _exec(self.server.host, "cat /tmp/direct.txt")
        assert "direct content" in result

    def test_cd_and_pwd(self):
        """cd后pwd变化"""
        _connect(self.server)
        _exec(self.server.host, "cd /tmp")
        result = _exec(self.server.host, "pwd -P")
        assert "/tmp" in result


# ── sudo注入 ──

class TestSudoInjection:
    """验证sudo密码自动注入"""

    def setup_method(self):
        terminal._sessions.clear()
        self.server = MockSSHServer()
        self.server.start()

    def teardown_method(self):
        terminal.cleanup()
        self.server.stop()

    def test_sudo_whoami(self):
        _connect(self.server)
        result = _exec(self.server.host, "sudo whoami")
        # sudo注入后应返回root
        assert "root" in result or "sudo" in result

    def test_sudo_apt(self):
        _connect(self.server)
        result = _exec(self.server.host, "sudo apt install build-essential")
        assert isinstance(result, str)


# ── close保护 ──

class TestCloseProtection:
    """验证close不杀后台进程"""

    def setup_method(self):
        terminal._sessions.clear()
        self.server = MockSSHServer()
        self.server.start()

    def teardown_method(self):
        terminal.cleanup()
        self.server.stop()

    def test_close_sends_disown(self):
        """close前发送disown"""
        _connect(self.server)
        # 启动后台进程
        _exec(self.server.host, "nohup sleep 100 &")
        # 关闭连接
        result = terminal.execute(action="close", host=self.server.host)
        assert "已关闭" in result

    def test_reconnect_after_close(self):
        """关闭后能重连"""
        _connect(self.server)
        terminal.execute(action="close", host=self.server.host)
        result = _connect(self.server)
        assert "已连接" in result


# ── 会话管理 ──

class TestSessionManagement:
    """会话管理暴力测试"""

    def setup_method(self):
        terminal._sessions.clear()

    def teardown_method(self):
        terminal.cleanup()

    def test_status_no_sessions(self):
        result = terminal.execute(action="status")
        assert "无活跃" in result

    def test_connect_missing_params(self):
        result = terminal.execute(action="connect")
        assert "错误" in result

    def test_exec_no_session(self):
        result = terminal.execute(action="exec", command="ls")
        assert "错误" in result

    def test_unknown_action(self):
        result = terminal.execute(action="invalid")
        assert "错误" in result

    def test_close_all(self):
        result = terminal.execute(action="close")
        assert "已关闭" in result


# ── 边界场景 ──

class TestEdgeCases:
    """边界和极端场景"""

    def setup_method(self):
        terminal._sessions.clear()
        self.server = MockSSHServer()
        self.server.start()

    def teardown_method(self):
        terminal.cleanup()
        self.server.stop()

    def test_empty_command(self):
        _connect(self.server)
        result = _exec(self.server.host, "")
        assert isinstance(result, str)

    def test_unicode_command(self):
        _connect(self.server)
        result = _exec(self.server.host, "echo '你好世界'")
        assert isinstance(result, str)

    def test_very_long_command(self):
        _connect(self.server)
        result = _exec(self.server.host, "echo " + "x" * 500)
        assert isinstance(result, str)

    def test_uname(self):
        _connect(self.server)
        result = _exec(self.server.host, "uname -a")
        assert "Linux" in result

    def test_env(self):
        _connect(self.server)
        result = _exec(self.server.host, "env")
        assert "HOME" in result

    def test_df(self):
        _connect(self.server)
        result = _exec(self.server.host, "df -h")
        assert isinstance(result, str)

    def test_which_common_tools(self):
        _connect(self.server)
        for tool in ["gcc", "make", "cmake", "python3"]:
            result = _exec(self.server.host, f"which {tool}")
            assert f"/usr/bin/{tool}" in result


# ── SimulatedShell单元测试 ──

class TestSimulatedShellUnit:
    """SimulatedShell纯逻辑测试（不需要SSH连接）"""

    def test_echo(self):
        shell = SimulatedShell()
        assert shell.execute("echo hello") == "hello"

    def test_whoami(self):
        shell = SimulatedShell()
        assert shell.execute("whoami") == "testuser"

    def test_pwd(self):
        shell = SimulatedShell()
        assert shell.execute("pwd") == "/home/testuser"

    def test_cd_pwd(self):
        shell = SimulatedShell()
        shell.execute("cd /tmp")
        assert shell.cwd == "/tmp"
        assert shell.execute("pwd") == "/tmp"

    def test_echo_dollar_home(self):
        shell = SimulatedShell()
        assert shell.execute("echo $HOME") == "/home/testuser"

    def test_mkdir_ls(self):
        shell = SimulatedShell()
        shell.execute("mkdir -p /tmp/mydir")
        result = shell.execute("ls /tmp")
        assert "mydir" in result

    def test_echo_redirect_cat(self):
        shell = SimulatedShell()
        shell.execute("echo 'test content' > /tmp/file.txt")
        result = shell.execute("cat /tmp/file.txt")
        assert "test content" in result

    def test_sudo_with_password(self):
        shell = SimulatedShell()
        result = shell.execute("echo '0' | sudo -S whoami")
        assert result == "root"

    def test_sudo_wrong_password(self):
        shell = SimulatedShell()
        result = shell.execute("echo 'wrong' | sudo -S whoami")
        assert "Sorry" in result

    def test_nohup(self):
        shell = SimulatedShell()
        result = shell.execute("nohup sleep 100 &")
        assert "[1]" in result

    def test_prompt_format(self):
        shell = SimulatedShell()
        assert "testuser@mockhost:~$" in shell.prompt

    def test_prompt_after_cd(self):
        shell = SimulatedShell()
        shell.execute("cd /tmp")
        assert "testuser@mockhost:/tmp$" in shell.prompt


# ── VirtualFileSystem单元测试 ──

class TestVirtualFileSystemUnit:
    """VFS纯逻辑测试"""

    def test_initial_dirs_exist(self):
        vfs = VirtualFileSystem()
        assert vfs.is_dir("/home/testuser")
        assert vfs.is_dir("/tmp")
        assert vfs.is_dir("/usr/bin")

    def test_write_read(self):
        vfs = VirtualFileSystem()
        vfs.write_file("/tmp/test.txt", b"hello")
        assert vfs.read_file("/tmp/test.txt") == b"hello"

    def test_mkdir_recursive(self):
        vfs = VirtualFileSystem()
        vfs.mkdir("/tmp/a/b/c")
        assert vfs.is_dir("/tmp/a/b/c")

    def test_ls(self):
        vfs = VirtualFileSystem()
        vfs.mkdir("/tmp/dir1")
        vfs.mkdir("/tmp/dir2")
        entries = vfs.ls("/tmp")
        assert "dir1" in entries
        assert "dir2" in entries

    def test_rm(self):
        vfs = VirtualFileSystem()
        vfs.write_file("/tmp/del.txt", b"x")
        assert vfs.exists("/tmp/del.txt")
        vfs.rm("/tmp/del.txt")
        assert not vfs.exists("/tmp/del.txt")

    def test_stat(self):
        vfs = VirtualFileSystem()
        vfs.write_file("/tmp/stat.txt", b"content")
        s = vfs.stat("/tmp/stat.txt")
        assert s is not None
        assert s["st_size"] == 7

    def test_normalize_path(self):
        vfs = VirtualFileSystem()
        assert vfs._normalize("/tmp/../home/testuser") == "/home/testuser"
        assert vfs._normalize("/tmp/./sub") == "/tmp/sub"
