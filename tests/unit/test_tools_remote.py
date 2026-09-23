"""tools/remote 工具族自测 —— T3.6 交付物（Terminal SSH + Serial 串口）。

## spec Scenario 覆盖映射（`openspec/changes/recast-v2/specs/tools-remote/spec.md`）

| # | Requirement | 覆盖（测试函数） | 未验证项 |
|---|---|---|---|
| 1 | Terminal 工具参数契约 | test_terminal_default_action / unknown_action / numeric_coercion_error / default_timeouts / definition_contract | — |
| 2 | SSH 连接建立与认证 | test_connect_requires_host_username / password_path_used_as_key / auth_failure_hints / connect_receipt / connect_dup_hint / banner_folding | 真实 SSH 握手/认证（真连接） |
| 3 | SSH 会话编号与容量 | test_connect_slots_exhausted / dead_slot_reclaimed / connect_existing_slot / connect_slot_out_of_range | — |
| 4 | Terminal 状态查询与关闭 | test_status_block / status_no_sessions / close_all / close_dev0 / cleanup_clears | — |
| 5 | SSH 命令执行与完成检测 | test_exec_prefix / exec_missing_command / exec_bad_timeout / exec_busy_session / test_baseline_groups(parse_output...) | 真实 PTY 完整读循环（真连接） |
| 6 | Terminal 输出清洗与截断 | test_baseline_groups(truncate/clean) / test_truncate_hint_format / test_truncate_invalid_limit | — |
| 7 | 退出码与框架错误标签 | test_error_lines_tagged_and_stripped / test_fake_error_text_not_flagged | — |
| 8 | 超时后行为与后台收敛 | test_exec_timeout_marks_busy / test_conn_lost_returns_error / test_zero_output_conn_lost | 真实设备超时后 watcher 收敛 |
| 9 | SSH 交互输入语义 | test_input_idle_rejected / test_input_ctrl_c_interrupts / test_input_ctrl_c_not_effective / test_input_missing_text | 真实设备 ^C 生效时序 |
| 10 | sudo 密码自动注入 | test_sudo_prompt_without_password / test_baseline_groups(无) | 真实 sudo 多级注入（真连接） |
| 11 | 断线自动重连 | test_reconnect_on_entry_success / on_entry_failure / during_exec | 真实 TCP 断线时序 |
| 12 | 文件传输 | test_transfer_both_local / same_path / missing_paths / dir_rejected / size_limit / local_to_remote_receipt / device_not_connected / local_parent_created / remote_to_remote_stream | 真实 SFTP 传输（真连接） |
| 13 | Serial 工具参数契约 | test_serial_default_action / unknown_action / numeric_error / line_ending_escapes / definition_contract | — |
| 14 | Serial 扫描 | test_scan_list_format / scan_empty / scan_import_error | 真实串口枚举（真设备） |
| 15 | Serial 连接与会话编号 | test_serial_connect_requires_port / port_in_use / slots_exhausted / connect_receipt / port_case_insensitive / placeholder_cleanup | 真实串口打开 |
| 16 | Serial 命令执行与提示符检测 | test_serial_exec_prefix / missing_command / busy / device_disconnected / slot_reclaimed | 真实提示符稳定性采样 |
| 17 | Serial 裸执行与纯监听 | test_raw_exec_timeout_tag / listen_with_output / listen_without_output / raw_bad_timeout | 真实裸机设备 |
| 18 | Serial 交互输入 | test_serial_input_missing / ctrl_c_sends_raw / idle_input_is_command | 真实设备 ^C |
| 19 | Serial 输出清洗与截断 | test_baseline_groups(serial.*) / test_serial_backpressure | 真实刷屏设备 |
| 20 | Serial 状态、关闭与清理 | test_serial_status_* / close_* / resolve_* / cleanup | — |
| 21 | 中断传播（ESC） | test_terminal_esc_focus_and_busy / test_terminal_esc_quirk_last_wins / test_serial_esc_all_active | 真实进程终止时序 |
| 22 | 设备引用约定（devN） | test_resolve_unique / multiple / dev0_exec / ip_loose / invalid_device / dev_display / file_tool_normalize | — |
| 23 | 远程文件访问 | test_remote_read_* / write_* / edit_* / make_diff | 真实 SFTP/errno（真连接） |
| 24 | 删除命令安全确认 | test_confirm_windows_reject / pend_await_confirm / confirmed_pass / git_diff / rm_skip | 交互确认 UI（真终端） |
| 25 | Serial 会话上限配置接线（修复项） | test_serial_max_sessions_config / clamp / defaults | — |
| 26 | 兼容性怪癖保持 | test_esc_quirk / serial_prompt_info_space / silent_param_fallback / serial_no_backlog / utf8_replacement | — |

## 基准对照（旧实现行为基准）

与旧实现（`narnat_agent/`）的纯函数/静态方法/裸实例方法逐用例对照：
`baseline/data_remote/remote.json`（`extract_old_remote.py` 生成，24 组 139 用例），
用例输入集与比对脚本共用 `baseline/remote_cases.py`。
"""
from __future__ import annotations

import errno
import json
import re
import socket
import stat as stat_module
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from narnat_agent.contracts.tool import AWAIT_CONFIRM
from narnat_agent.tools import signal
from narnat_agent.tools.env import ToolEnvImpl, ToolSettingsImpl
from narnat_agent.tools.registry import ToolRegistry
from narnat_agent.tools.remote import common as common_module
from narnat_agent.tools.remote import remote_files as remote_files_module
from narnat_agent.tools.remote import serial as serial_module
from narnat_agent.tools.remote import serial_session as serial_session_module
from narnat_agent.tools.remote import ssh_session as ssh_session_module
from narnat_agent.tools.remote import terminal as terminal_module
from narnat_agent.tools.remote import transfer as transfer_module
from narnat_agent.tools.remote.common import SessionSlots
from narnat_agent.tools.remote.remote_files import RemoteFileAccess, colorize_diff
from narnat_agent.tools.remote.serial import SerialTool
from narnat_agent.tools.remote.serial_session import SerialSession
from narnat_agent.tools.remote.ssh_session import SSHSession
from narnat_agent.tools.remote.terminal import TerminalTool
from tests.baseline import remote_cases

TESTS_DIR = Path(__file__).resolve().parents[1]
BASELINE_FILE = TESTS_DIR / "baseline" / "data_remote" / "remote.json"


# ═══════════════════════════════════════════════════════════════
# 测试替身（stub 会话 / 伪通道 / 伪 SFTP）
# ═══════════════════════════════════════════════════════════════


class StubSSH:
    """SSHSession 的最小替身：记录调用序列，模拟断线/重连/忙状态。"""

    def __init__(self, host: str = "10.0.0.1", username: str = "root",
                 cwd: str = "~", closed: bool = False, busy: bool = False,
                 initial: str = "", reconnect_ok: bool = True,
                 execute_result: str = "ok", execute_error: Exception | None = None):
        self.host = host
        self.username = username
        self._cwd = cwd
        self._closed = closed
        self._busy = busy
        self._initial_output = initial
        self._reconnect_ok = reconnect_ok
        self.execute_result = execute_result
        self.execute_error = execute_error
        self.exec_calls: list[tuple] = []
        self.input_calls: list[tuple] = []
        self.interrupts = 0
        self.reconnects = 0
        self.close_calls = 0
        self.initialize_calls = 0
        self.sftp = None

    @property
    def prompt(self) -> str:
        return f"{self.username}@{self.host}:{self._cwd}$"

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def initial_output(self) -> str:
        return self._initial_output

    def is_closed(self) -> bool:
        return self._closed

    def send_interrupt(self) -> None:
        self.interrupts += 1

    def open_sftp(self):
        if self.sftp is None:
            raise RuntimeError("no sftp")
        return self.sftp

    def initialize(self) -> None:
        self.initialize_calls += 1

    def execute(self, command: str, timeout: int = 0, max_output_chars: int = 8000) -> str:
        self.exec_calls.append((command, timeout, max_output_chars))
        if self.execute_error is not None:
            raise self.execute_error
        return self.execute_result

    def send_input(self, text: str, timeout: int = 0, max_output_chars: int = 8000) -> str:
        self.input_calls.append((text, timeout, max_output_chars))
        if self.execute_error is not None:
            raise self.execute_error
        return self.execute_result

    def reconnect(self) -> None:
        self.reconnects += 1
        if not self._reconnect_ok:
            raise RuntimeError("reconnect failed")
        self._closed = False

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True


class StubSerial:
    """SerialSession 的最小替身。"""

    def __init__(self, port: str = "COM3", baudrate: int = 115200,
                 alive: bool = True, busy: bool = False, initial: str = "",
                 execute_result: str = "ok"):
        self.port = port
        self.baudrate = baudrate
        self._alive = alive
        self._busy = busy
        self.initial_output = initial
        self.execute_result = execute_result
        self.exec_calls: list[tuple] = []
        self.raw_calls: list[tuple] = []
        self.input_calls: list[tuple] = []
        self.kills = 0
        self.close_calls = 0

    @property
    def prompt_info(self) -> str:
        return f"{self.port} @{self.baudrate}"

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def is_alive(self) -> bool:
        return self._alive

    def kill_active(self) -> None:
        self.kills += 1

    def execute(self, command: str, timeout: int = 120, max_output_chars: int = 8000) -> str:
        self.exec_calls.append((command, timeout, max_output_chars))
        return self.execute_result

    def raw_execute(self, command: str, timeout: int = 120, max_output_chars: int = 8000) -> str:
        self.raw_calls.append((command, timeout, max_output_chars))
        return self.execute_result

    def send_input(self, text: str, timeout: int = 120, max_output_chars: int = 8000) -> str:
        self.input_calls.append((text, timeout, max_output_chars))
        return self.execute_result

    def close(self) -> None:
        self.close_calls += 1
        self._alive = False


class FakeChannel:
    """伪 PTY 通道：按脚本返回 bytes / 抛异常 / 调用回调生成数据；记录 send 调用。"""

    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self.sent: list[str] = []
        self.closed = False
        self._timeout = 0.5

    def recv(self, _n: int) -> bytes:
        if not self._chunks:
            raise socket.timeout()
        chunk = self._chunks.pop(0)
        if callable(chunk):
            chunk = chunk(self)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")

    def send(self, data: str) -> None:
        self.sent.append(data)

    def settimeout(self, value: float) -> None:
        self._timeout = value

    def gettimeout(self) -> float:
        return self._timeout

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """伪 paramiko.SSHClient（仅供 close 路径）。"""

    def close(self) -> None:
        pass


class FakeSFTPFile:
    def __init__(self, sftp: "FakeSFTP", path: str, mode: str):
        self._sftp = sftp
        self._path = path
        self._mode = mode
        self._data = sftp.files.get(path, b"") if "r" in mode else b""
        self._pos = 0
        self._closed = False
        if "w" in mode:
            sftp.files[path] = b""

    def read(self, size: int = -1) -> bytes:
        if self._mode == "r":
            data = self._data
            return data if size is None or size < 0 else data
        data = self._data[self._pos:]
        if size is None or size < 0:
            self._pos = len(self._data)
            return data
        chunk = data[:size]
        self._pos += len(chunk)
        return chunk

    def readline(self) -> bytes:
        data = self._data[self._pos:]
        if not data:
            return b""
        idx = data.find(b"\n")
        line = data[: idx + 1] if idx >= 0 else data
        self._pos += len(line)
        return line

    def write(self, data: bytes) -> None:
        self._sftp.files[self._path] = self._sftp.files.get(self._path, b"") + data

    def seek(self, pos: int, whence: int = 0) -> None:
        self._pos = pos

    def close(self) -> None:
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeSFTP:
    """伪 SFTP 客户端（内存文件表：支持 stat/open/close/put/get）。"""

    def __init__(self, files=None, dirs=None, errors=None, put_raises=None):
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: set[str] = set(dirs or set())
        # 已存在文件的父目录视为存在（贴近真实 SFTP 视图）
        for path in self.files:
            parent = path.rsplit("/", 1)[0]
            if parent:
                self.dirs.add(parent)
        self.errors: dict[str, OSError] = dict(errors or {})
        self.put_raises = put_raises
        self.put_calls: list[tuple] = []
        self.get_calls: list[tuple] = []
        self.close_calls = 0
        self.made_dirs: list[str] = []
        self.open_mode_raises: dict[str, Exception] = {}

    def stat(self, path: str):
        if path in self.errors:
            raise self.errors[path]
        if path in self.dirs:
            return SimpleNamespace(st_mode=stat_module.S_IFDIR, st_size=0)
        if path in self.files:
            return SimpleNamespace(st_mode=stat_module.S_IFREG, st_size=len(self.files[path]))
        raise IOError(errno.ENOENT, "no such file", path)

    def open(self, path: str, mode: str = "r"):
        if path in self.open_mode_raises:
            raise self.open_mode_raises[path]
        if "w" in mode and "/" in path:
            parent = path.rsplit("/", 1)[0]
            if parent not in self.dirs and parent:
                raise IOError(errno.ENOENT, "no such dir", parent)
        return FakeSFTPFile(self, path, mode)

    def mkdir(self, path: str) -> None:
        if path in self.errors:
            raise self.errors[path]
        self.dirs.add(path)
        self.made_dirs.append(path)

    def put(self, local: str, remote: str) -> None:
        if self.put_raises is not None:
            raise self.put_raises
        self.put_calls.append((local, remote))
        self.files[remote] = Path(local).read_bytes()

    def get(self, remote: str, local: str) -> None:
        self.get_calls.append((remote, local))
        data = self.files.get(remote)
        if data is None:
            raise IOError(errno.ENOENT, "no such file", remote)
        Path(local).write_bytes(data)

    def close(self) -> None:
        self.close_calls += 1


# ═══════════════════════════════════════════════════════════════
# 构造辅助
# ═══════════════════════════════════════════════════════════════


def _env(**settings) -> ToolEnvImpl:
    return ToolEnvImpl(settings=ToolSettingsImpl(**settings))


def _bare_ssh(channel: FakeChannel, *, busy: bool = False, echo: bool = True,
              sudo_password: str | None = None, cwd: str = "~",
              username: str = "root", host: str = "10.0.0.1") -> SSHSession:
    """构造未走 __init__ 的 SSHSession（伪造实例，仅注读循环所需的内部状态）。"""
    s = object.__new__(SSHSession)
    s.host = host
    s.username = username
    s.port = 22
    s._cwd = cwd
    s._channel = channel
    s._client = FakeClient()
    s._busy = busy
    s._last_command = ""
    s._echo_enabled = echo
    s._interrupt = threading.Event()
    s._pending_marker = ""
    s._pending_pwd_marker = ""
    s._sentinel_sent = False
    s._watcher_stop = threading.Event()
    s._watcher_thread = None
    s._watcher_finished = False
    s._backlog = ""
    s._backlog_lock = threading.Lock()
    s._sudo_password = sudo_password
    s._sudo_mismatch = False
    s._initial_output = ""
    return s


def _bare_serial(*, busy: bool = False, dead: bool = False, buffer: str = "",
                 prompt_pattern: str = "", line_ending: str = "\n",
                 ser=None) -> SerialSession:
    """构造未走 __init__ 的 SerialSession（伪造实例）。"""
    import codecs
    s = object.__new__(SerialSession)
    s.port = "COM3"
    s.baudrate = 115200
    s.line_ending = line_ending
    s._prompt_re = SerialSession.PROMPT_RE if not prompt_pattern else re.compile(prompt_pattern)
    s._ser = ser if ser is not None else FakeSerial()
    s._busy = busy
    s._interrupt = threading.Event()
    s._dead = dead
    s._buffer = buffer
    s._lock = threading.Lock()
    s._cond = threading.Condition(s._lock)
    s._reader_alive = True
    s._decoder = codecs.getincrementaldecoder("utf-8")("replace")
    s.initial_output = ""
    return s


def _feed_serial(session: SerialSession, text: str, delay: float = 0.05) -> None:
    """延迟向会话缓冲写入设备输出（模拟 reader 线程），唤醒等待方。"""
    def _feed():
        with session._cond:
            session._buffer += text
            session._cond.notify_all()

    timer = threading.Timer(delay, _feed)
    timer.daemon = True
    timer.start()


class FakeSerial:
    """伪 pyserial.Serial：记录写入，支持 open/close 状态。"""

    def __init__(self, is_open: bool = True):
        self.is_open = is_open
        self.writes: list[bytes] = []
        self.flushes = 0
        self.closed_calls = 0

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise __import__("serial").SerialException("not open")
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed_calls += 1
        self.is_open = False


def _tool_with(*sessions: StubSSH, max_sessions: int = 5) -> TerminalTool:
    tool = TerminalTool(max_sessions=max_sessions)
    for sid, session in enumerate(sessions):
        if session is not None:
            tool._slots.put(sid, session)
    return tool


def _serial_tool_with(*sessions: StubSerial, max_sessions: int = 5) -> SerialTool:
    tool = SerialTool(max_sessions=max_sessions)
    for sid, session in enumerate(sessions):
        if session is not None:
            tool._slots.put(sid, session)
    return tool


def _text(result) -> str:
    """取工具结果的 AI 视角文本（剥离框架标签）。"""
    return signal.strip_tags(result.llm_text)


# ═══════════════════════════════════════════════════════════════
# 基准对照（旧实现行为基准：24 组 / 139 用例）
# ═══════════════════════════════════════════════════════════════


def _api_new() -> SimpleNamespace:
    """构造新实现的 api 命名空间（与 extract_old_remote.make_api 同形）。"""

    def ssh_parse_output(raw, marker, pwd_marker, last_command):
        probe = object.__new__(SSHSession)
        probe._last_command = last_command
        return probe._parse_output(raw, marker, pwd_marker)

    def ssh_parse_partial(raw, marker):
        probe = object.__new__(SSHSession)
        return probe._parse_partial_output(raw, marker)

    def ssh_extract_cwd(raw, marker, pwd_marker):
        probe = object.__new__(SSHSession)
        return probe._extract_cwd(raw, marker, pwd_marker)

    def serial_probe(pattern: str):
        probe = object.__new__(SerialSession)
        probe._prompt_re = SerialSession.PROMPT_RE
        if pattern:
            probe._prompt_re = re.compile(pattern)
        return probe

    return SimpleNamespace(
        rc_line=signal.rc_line,
        error_line=signal.error_line,
        tag_error=signal.tag_error,
        terminal_definition=terminal_module.DEFINITION,
        serial_definition=serial_module.DEFINITION,
        ssh_truncate=ssh_session_module.truncate_output,
        ssh_clean=SSHSession._clean_output,
        ssh_ps1=SSHSession._ps1_candidate,
        ssh_sentinel=SSHSession._sentinel_line_present,
        ssh_strip_echo=SSHSession._strip_echo,
        ssh_strip_trailing_prompt=SSHSession._strip_trailing_prompt,
        ssh_strip_caret_echo=SSHSession._strip_caret_echo,
        ssh_parse_output=ssh_parse_output,
        ssh_parse_partial=ssh_parse_partial,
        ssh_extract_cwd=ssh_extract_cwd,
        serial_truncate=serial_session_module.truncate_output,
        serial_clean=SerialSession._clean_output,
        serial_merge_cr=common_module.merge_cr_line,
        serial_strip_command_echo=SerialSession._strip_command_echo,
        serial_dedup_prompt_lines=lambda output: serial_probe("")._dedup_prompt_lines(output),
        serial_polish_output=lambda raw, sent: serial_probe("")._polish_output(raw, sent),
        serial_is_at_prompt=lambda text, pattern: serial_probe(pattern)._is_at_prompt(text),
        format_size=transfer_module.format_size,
        check_transfer_size=transfer_module.check_transfer_size,
        make_diff=RemoteFileAccess._make_diff,
        describe_bytes_change=remote_files_module.describe_bytes_only_change,
        detect_encoding=remote_files_module.detect_text_encoding,
    )


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_new() -> SimpleNamespace:
    return _api_new()


@pytest.mark.parametrize("group", sorted(remote_cases.CASES))
def test_baseline_groups(group, baseline, api_new):
    """各组用例：新实现结果与旧实现基准逐用例一致（标签已模式化）。"""
    expected = {case["id"]: case["result"] for case in baseline["groups"][group]}
    got = {case["id"]: case["result"] for case in remote_cases.run_group(api_new, group)}
    for case_id, exp in expected.items():
        assert got.get(case_id) == exp, f"{group}::{case_id}"


def test_baseline_covers_expected_groups(baseline):
    """基准组齐全（24 组），防止提取器与用例集漂移。"""
    assert set(baseline["groups"]) == set(remote_cases.CASES)
    assert sum(len(v) for v in baseline["groups"].values()) == 139


# ═══════════════════════════════════════════════════════════════
# R1/R13 参数契约与 Tool 协议面
# ═══════════════════════════════════════════════════════════════

TERMINAL_PARAMS = ["action", "host", "username", "port", "password", "command",
                   "input", "timeout", "max_output_chars", "source_host",
                   "source_path", "target_host", "target_path"]
SERIAL_PARAMS = ["action", "port", "baudrate", "databits", "parity", "stopbits",
                 "flow_control", "line_ending", "prompt_pattern", "command",
                 "input", "timeout", "session_id", "max_output_chars"]


def test_terminal_definition_params():
    definition = TerminalTool().definition()
    assert definition["type"] == "function"
    function = definition["function"]
    assert function["name"] == "Terminal"
    assert list(function["parameters"]["properties"]) == TERMINAL_PARAMS
    assert function["parameters"]["required"] == []
    # 内部参数（session_id/key_path/sudo_password）不暴露
    assert "session_id" not in function["parameters"]["properties"]
    assert function["parameters"]["properties"]["action"]["enum"] == [
        "connect", "exec", "input", "status", "close", "transfer"]


def test_serial_definition_params():
    definition = SerialTool().definition()
    function = definition["function"]
    assert function["name"] == "Serial"
    assert list(function["parameters"]["properties"]) == SERIAL_PARAMS
    assert function["parameters"]["required"] == []
    assert function["parameters"]["properties"]["action"]["enum"] == [
        "scan", "connect", "exec", "raw_exec", "input", "status", "close"]


def test_tool_protocol_and_registry_integration():
    registry = ToolRegistry()
    terminal = TerminalTool()
    serial = SerialTool()
    assert registry.register(terminal) and registry.register(serial)
    assert registry.get_tool_names() == ["Terminal", "Serial"]
    names = [d["function"]["name"] for d in registry.get_tool_definitions()]
    assert names == ["Terminal", "Serial"]
    result = registry.execute("Terminal", {"action": "nope"}, _env())
    assert result.is_error is True
    assert result.llm_text == "[错误: 未知action 'nope'，可选: connect/exec/input/status/close/transfer]"


def test_terminal_default_action_is_exec():
    out = _text(TerminalTool().execute({}))
    assert out == "[错误: exec需要提供command]"


def test_terminal_unknown_action():
    out = _text(TerminalTool().execute({"action": "boom"}))
    assert out == "[错误: 未知action 'boom'，可选: connect/exec/input/status/close/transfer]"
    assert _text(TerminalTool().execute({"action": ""})) == \
        "[错误: 未知action ''，可选: connect/exec/input/status/close/transfer]"


@pytest.mark.parametrize("args", [
    {"port": "not-a-number"},
    {"timeout": "abc"},
    {"max_output_chars": "x"},
    {"session_id": "y"},
])
def test_terminal_numeric_coercion_error(args):
    result = TerminalTool().execute(args)
    assert _text(result) == "[错误: port/timeout/session_id/max_output_chars需为整数]"
    assert signal.has_error(result.llm_text)


def test_terminal_numeric_strings_accepted(monkeypatch):
    """字符串数字被强转（port/timeout/session_id/max_output_chars）。"""
    factory = _install_fake_ssh(monkeypatch)
    TerminalTool().execute({"action": "connect", "host": "h", "username": "u",
                            "port": "2222", "timeout": "20"})
    kwargs = factory.instances[0].kwargs
    assert kwargs["port"] == 2222 and kwargs["timeout"] == 20
    session = StubSSH()
    tool = _tool_with(session)
    tool.execute({"action": "exec", "command": "ls", "timeout": "30",
                  "max_output_chars": "100"})
    assert session.exec_calls == [("ls", 30, 100)]


def test_terminal_default_timeouts(monkeypatch):
    """connect 缺省 15 秒、exec/input 缺省 120 秒。"""
    factory = _install_fake_ssh(monkeypatch)
    TerminalTool().execute({"action": "connect", "host": "h", "username": "u"})
    assert factory.instances[0].kwargs["timeout"] == 15
    session = StubSSH()
    tool = _tool_with(session)
    tool.execute({"action": "exec", "command": "ls"})
    tool.execute({"action": "input", "input": "y"})
    assert session.exec_calls == [("ls", 120, 8000)]
    assert session.input_calls == [("y", 120, 8000)]


def test_terminal_connect_timeout_fallback_and_clamp(monkeypatch):
    factory = _install_fake_ssh(monkeypatch)
    tool = TerminalTool()
    tool.execute({"action": "connect", "host": "h", "username": "u", "timeout": 0})
    assert factory.instances[0].kwargs["timeout"] == 15  # 非正值回落 15
    tool.execute({"action": "connect", "host": "h", "username": "u", "timeout": -3})
    assert factory.instances[1].kwargs["timeout"] == 15
    tool.execute({"action": "connect", "host": "h", "username": "u", "timeout": 60},
                 _env(max_timeout_seconds=5))
    assert factory.instances[2].kwargs["timeout"] == 5  # 受全局超时上限截断


def test_serial_default_action_and_unknown_action():
    out = _text(SerialTool().execute({}))
    assert out == "[错误: exec 需要提供 command]"
    assert _text(SerialTool().execute({"action": "zzz"})) == \
        "[错误: 未知action 'zzz'，可选: scan/connect/exec/raw_exec/input/status/close]"


@pytest.mark.parametrize("args", [
    {"baudrate": "x"}, {"databits": "x"}, {"stopbits": "x"},
    {"timeout": "x"}, {"session_id": "x"}, {"max_output_chars": "x"},
])
def test_serial_numeric_coercion_error(args):
    result = SerialTool().execute(args)
    assert _text(result) == "[错误: baudrate/databits/stopbits/timeout/session_id/max_output_chars需为数值]"


def test_serial_line_ending_escapes_and_fallback(monkeypatch):
    factory = _install_fake_serial(monkeypatch)
    tool = SerialTool()
    tool.execute({"action": "connect", "port": "COM3", "line_ending": "\\r\\n"})
    assert factory.instances[0].kwargs["line_ending"] == "\r\n"
    tool.execute({"action": "connect", "port": "COM4", "line_ending": "\\r"})
    assert factory.instances[1].kwargs["line_ending"] == "\r"
    tool.execute({"action": "connect", "port": "COM5", "line_ending": "bogus"})
    assert factory.instances[2].kwargs["line_ending"] == "\n"


# ═══════════════════════════════════════════════════════════════
# R2/R3 SSH 连接建立、认证与会话编号
# ═══════════════════════════════════════════════════════════════


class _FakeSSHFactory:
    def __init__(self, error: Exception | None = None, initial: str = ""):
        self.error = error
        self.initial = initial
        self.instances: list[StubSSH] = []

    def __call__(self, **kwargs) -> StubSSH:
        if self.error is not None:
            raise self.error
        session = StubSSH(host=kwargs["host"], username=kwargs["username"],
                          initial=self.initial)
        session.kwargs = kwargs
        self.instances.append(session)
        return session


def _install_fake_ssh(monkeypatch, error=None, initial: str = "") -> _FakeSSHFactory:
    factory = _FakeSSHFactory(error=error, initial=initial)
    monkeypatch.setattr(terminal_module, "SSHSession", factory)
    return factory


def test_connect_requires_host_and_username():
    tool = TerminalTool()
    assert _text(tool.execute({"action": "connect", "username": "u"})) == \
        "[错误: connect需要提供host和username]"
    assert _text(tool.execute({"action": "connect", "host": "h"})) == \
        "[错误: connect需要提供host和username]"


def test_connect_password_path_used_as_key(monkeypatch, tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("PRIVATE", encoding="utf-8")
    factory = _install_fake_ssh(monkeypatch)
    tool = TerminalTool()
    tool.execute({"action": "connect", "host": "h", "username": "u",
                  "password": str(key)})
    kwargs = factory.instances[0].kwargs
    assert kwargs["key_path"] == str(key)
    assert "password" not in kwargs
    assert kwargs["sudo_password"] == ""  # 私钥路径不作为登录密码


def test_connect_private_key_like_but_missing(monkeypatch):
    _install_fake_ssh(monkeypatch, error=paramiko.AuthenticationException("bad"))
    tool = TerminalTool()
    out = _text(tool.execute({"action": "connect", "host": "h", "username": "u",
                              "password": "~/.ssh/no_such_key"}))
    assert out.startswith("[错误: 认证失败(u@h)，请检查password")
    assert "password疑似私钥路径但本地文件不存在，请确认路径或改填登录密码" in out


def test_connect_auth_failure_without_password(monkeypatch):
    _install_fake_ssh(monkeypatch, error=paramiko.AuthenticationException("bad"))
    out = _text(TerminalTool().execute({"action": "connect", "host": "h", "username": "u"}))
    assert "未提供password，大多数设备需要密码认证，请在password参数填登录密码后重试" in out


def test_connect_ssh_exception_and_generic_failure(monkeypatch):
    _install_fake_ssh(monkeypatch, error=paramiko.SSHException("proto"))
    assert _text(TerminalTool().execute(
        {"action": "connect", "host": "h", "username": "u", "password": "p"})) == \
        "[错误: SSH连接失败(u@h): proto]"
    _install_fake_ssh(monkeypatch, error=OSError("net down"))
    assert _text(TerminalTool().execute(
        {"action": "connect", "host": "h", "username": "u", "password": "p"})) == \
        "[错误: 连接失败(u@h): net down]"


def test_connect_receipt_with_and_without_initial_output(monkeypatch):
    _install_fake_ssh(monkeypatch)
    out = _text(TerminalTool().execute(
        {"action": "connect", "host": "10.0.0.1", "username": "root"}))
    assert out == "[已连接 dev1(终端0): root@10.0.0.1]\nroot@10.0.0.1:~$"

    _install_fake_ssh(monkeypatch, initial="Welcome\nLast login: now")
    out = _text(TerminalTool().execute(
        {"action": "connect", "host": "10.0.0.1", "username": "root"}))
    assert out == "[已连接 dev1(终端0): root@10.0.0.1]\nWelcome\nLast login: now"


def test_connect_banner_folding(monkeypatch):
    banner = "\n".join(["Banner-1", "line2", "line3", "line4", "line5", "Last login: t"])
    _install_fake_ssh(monkeypatch, initial=banner)
    out = _text(TerminalTool().execute(
        {"action": "connect", "host": "h", "username": "u"}))
    lines = out.split("\n")
    assert lines[0] == "[已连接 dev1(终端0): u@h]"
    assert lines[1] == "Banner-1"
    assert lines[2] == "...(已省略3行登录横幅)"
    assert lines[3:] == ["line5", "Last login: t"]


def test_connect_duplicate_hint(monkeypatch):
    _install_fake_ssh(monkeypatch)
    existing = StubSSH(host="h", username="u")
    tool = _tool_with(existing)
    out = _text(tool.execute({"action": "connect", "host": "h", "username": "u"}))
    assert "[注意: 相同设备已有连接: dev1(终端0)。如非必要请勿重复连接，可直接用 host=dev1 执行命令]" in out


def test_connect_existing_slot_returns_info(monkeypatch):
    _install_fake_ssh(monkeypatch)
    session = StubSSH(host="h", username="u", cwd="/srv")
    tool = _tool_with(session)
    out = _text(tool.execute({"action": "connect", "session_id": 0, "host": "other",
                              "username": "x"}))
    assert out == "[dev1(终端0)已连接: u@h]\nu@h:/srv$"
    assert len(tool._slots.entries()) == 1  # 未新建会话


def test_connect_slot_out_of_range():
    tool = TerminalTool(max_sessions=2)
    assert _text(tool.execute({"action": "connect", "host": "h", "username": "u",
                               "session_id": 2})) == "[错误: session_id范围0-1]"


def test_connect_slots_exhausted(monkeypatch):
    _install_fake_ssh(monkeypatch)
    tool = _tool_with(StubSSH(host="a"), StubSSH(host="b"), max_sessions=2)
    out = _text(tool.execute({"action": "connect", "host": "c", "username": "u"}))
    assert out == "[错误: 已达最大会话数(2)，当前已连接: ['dev1(终端0)', 'dev2(终端1)']，请先close释放]"


def test_connect_dead_slot_reclaimed(monkeypatch):
    _install_fake_ssh(monkeypatch)
    dead = StubSSH(host="a", closed=True)
    tool = _tool_with(dead, max_sessions=2)
    out = _text(tool.execute({"action": "connect", "host": "b", "username": "u"}))
    assert out.startswith("[已连接 dev1(终端0): u@b]")
    assert dead.close_calls == 1  # 回收时关闭原会话
    assert tool._slots.get(0) is not dead


def test_alloc_slot_prefers_free_then_reclaims():
    slots = SessionSlots(2)
    assert slots.allocate(lambda s: False) == 0
    slots.put(0, StubSSH())
    assert slots.allocate(lambda s: False) == 1
    slots.put(1, StubSSH())
    assert slots.allocate(lambda s: False) == -1  # 满
    slots.put(1, StubSSH(closed=True))
    assert slots.allocate(lambda s: s.is_closed()) == 1  # 死会话槽位回收
    slots.put(1, None)
    assert slots.allocate(lambda s: s.is_closed()) == -1  # 占位不可抢占


def test_alloc_skips_placeholder_slot():
    slots = SessionSlots(2)
    slots.put(0, None)
    assert slots.allocate(lambda s: False) == 1


# ═══════════════════════════════════════════════════════════════
# R4 状态查询与关闭 / 清理
# ═══════════════════════════════════════════════════════════════


def test_status_block_and_no_sessions():
    tool = TerminalTool(max_sessions=2)
    assert _text(tool.execute({"action": "status"})) == (
        "[SSH会话]\ndev0: 本机(当前设备)\n  dev1(终端0): [未连接]\n"
        "  dev2(终端1): [未连接]\n(无已连接设备，最多支持2个并发终端)")

    busy = StubSSH(host="10.0.0.2", username="root", cwd="/srv", busy=True)
    tool = _tool_with(StubSSH(host="10.0.0.1", username="u", cwd="/home/u"), busy,
                      max_sessions=2)
    assert _text(tool.execute({"action": "status"})) == (
        "[SSH会话]\ndev0: 本机(当前设备)\n"
        "  dev1(终端0): u@10.0.0.1 [活跃|闲] 目录:/home/u\n"
        "  dev2(终端1): root@10.0.0.2 [活跃|忙] 目录:/srv")
    dead = StubSSH(host="h", closed=True)
    tool2 = _tool_with(dead)
    assert "  dev1(终端0): root@h [已断开|闲] 目录:~" in _text(tool2.execute({"action": "status"}))


def test_close_variants():
    tool = _tool_with(StubSSH(host="a"), StubSSH(host="b"))
    assert _text(tool.execute({"action": "close", "host": "dev0"})) == \
        "[dev0是当前设备(本机)，无需关闭]"
    assert _text(tool.execute({"action": "close", "host": "dev9"})) == "[dev9未连接]"
    assert _text(tool.execute({"action": "close", "host": "web-server"})) == (
        "[错误: 设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备；本机执行命令请用Shell工具)。"
        "当前已连接: dev1(root@a)、dev2(root@b)]")
    assert _text(tool.execute({"action": "close", "host": "dev2"})) == "[已关闭 dev2(终端1)]"
    assert _text(tool.execute({"action": "close", "session_id": 5})) == "[dev6(终端5)未连接]"
    assert _text(tool.execute({"action": "close"})) == "[已关闭1个会话]"
    assert tool._slots.entries() == {}


def test_cleanup_closes_all_and_clears():
    first, second = StubSSH(), StubSSH()
    tool = _tool_with(first, second)
    tool.cleanup()
    assert (first.close_calls, second.close_calls) == (1, 1)
    assert tool._slots.entries() == {}
    assert tool._slots.active_sids() == [] and tool._slots.focus() is None


# ═══════════════════════════════════════════════════════════════
# R5/R7/R8 exec 工具层：前缀、校验、错误标签、重连
# ═══════════════════════════════════════════════════════════════


def test_exec_prefix_and_error_labels():
    tool = _tool_with(StubSSH(host="h", execute_result="hello"))
    result = tool.execute({"action": "exec", "command": "ls"})
    assert result.llm_text == "[dev1(终端0)] hello"
    assert result.is_error is False

    # 框架错误行带不可伪造标签；经注册表入口后 is_error 置位、标签剥离（specs/tools-shell 判定协议）
    registry = ToolRegistry()
    registry.register(tool)
    missing = registry.execute("Terminal", {"action": "exec"}, _env())
    assert missing.is_error is True
    assert missing.llm_text == "[错误: exec需要提供command]"
    assert signal.has_error(tool.execute({"action": "exec"}).llm_text)
    bad_timeout = _text(tool.execute({"action": "exec", "command": "ls", "timeout": 0}))
    assert bad_timeout == "[错误: timeout需为正整数（秒）]"


def test_exec_timeout_clamped_by_settings():
    session = StubSSH()
    tool = _tool_with(session)
    tool.execute({"action": "exec", "command": "sleep 99", "timeout": 600},
                 _env(max_timeout_seconds=30))
    assert session.exec_calls == [("sleep 99", 30, 8000)]
    tool.execute({"action": "exec", "command": "sleep 9", "timeout": 600},
                 _env(max_timeout_seconds=0))
    assert session.exec_calls[1] == ("sleep 9", 600, 8000)  # 0=不限制


def test_exec_failure_label_and_reconnect_sequences():
    failing = StubSSH(host="h", execute_error=RuntimeError("boom"))
    tool = _tool_with(failing)
    assert _text(tool.execute({"action": "exec", "command": "ls"})) == \
        "[错误: dev1(终端0)命令执行失败: boom]"

    # 执行中连接中断 + 重连成功 → 提示重试
    mid = StubSSH(host="h", execute_error=RuntimeError("chan closed"), closed=False)
    mid.execute_error = RuntimeError("boom")

    def _die(command, timeout=0, max_output_chars=8000):
        mid._closed = True
        raise RuntimeError("chan closed")

    mid.execute = _die
    tool = _tool_with(mid)
    assert _text(tool.execute({"action": "exec", "command": "ls"})) == \
        "[错误: dev1(终端0)连接曾中断，已自动重连，请重试命令]"
    assert mid.reconnects == 1

    # 执行中连接中断 + 重连失败 → 会话保留（不丢设备编号）
    dead = StubSSH(host="h", reconnect_ok=False)
    dead.execute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    tool = _tool_with(dead)
    out = _text(tool.execute({"action": "exec", "command": "ls"}))
    assert out == "[错误: dev1(终端0)命令执行失败: x]"
    dead._closed = True
    out = _text(tool.execute({"action": "exec", "command": "ls"}))
    assert out == ("[错误: dev1(终端0)连接中断，自动重连失败（设备可能未开机/网络不通）。"
                   "设备恢复后重试将自动重连]")
    assert tool._slots.get(0) is dead  # 凭据保留


def test_exec_entry_reconnect_paths():
    # 入口即发现断线 + 重连成功 → 正常执行
    recovered = StubSSH(host="h", closed=True, reconnect_ok=True, execute_result="ok")
    tool = _tool_with(recovered)
    assert _text(tool.execute({"action": "exec", "command": "ls"})) == "[dev1(终端0)] ok"
    assert recovered.reconnects == 1

    # 入口即发现断线 + 重连失败 → 报错
    broken = StubSSH(host="h", closed=True, reconnect_ok=False)
    tool = _tool_with(broken)
    out = _text(tool.execute({"action": "exec", "command": "ls"}))
    assert out.endswith("连接中断，自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连]")
    assert broken.exec_calls == []


def test_input_prefix_and_missing_text():
    session = StubSSH(execute_result="sent")
    tool = _tool_with(session)
    assert _text(tool.execute({"action": "input", "input": "y"})) == "[dev1(终端0)] sent"
    assert _text(tool.execute({"action": "input"})) == "[错误: input需要提供input内容]"
    assert session.input_calls == [("y", 120, 8000)]


def test_fake_error_text_in_output_not_flagged():
    """命令输出里的 `[错误: …]` 无框架标签 → 不判失败（标签先判定后剥离）。"""
    tool = _tool_with(StubSSH(execute_result="[错误: 伪造] 这是命令输出"))
    result = tool.execute({"action": "exec", "command": "cat log"})
    assert result.is_error is False
    assert "[错误: 伪造]" in result.llm_text


def test_exec_registers_and_clears_focus():
    seen = {}

    def _exec(command, timeout=0, max_output_chars=8000):
        seen["focus"] = tool._slots.focus()
        return "ok"

    session = StubSSH(host="h")
    session.execute = _exec
    tool = _tool_with(session)
    tool.execute({"action": "exec", "command": "ls"})
    assert seen["focus"] is session
    assert tool._slots.focus() is None


# ═══════════════════════════════════════════════════════════════
# R21 中断传播（ESC）与怪癖
# ═══════════════════════════════════════════════════════════════


def test_terminal_esc_interrupts_focus_and_busy_sessions():
    active = StubSSH(host="a")
    busy = StubSSH(host="b", busy=True)
    tool = _tool_with(active, busy)
    tool._slots.set_focus(active)
    tool.kill_active_exec()
    assert (active.interrupts, busy.interrupts) == (1, 1)


def test_terminal_esc_quirk_focus_is_last_registered():
    """兼容怪癖：活跃执行为单引用，多会话并发时后者覆盖前者（ESC 只打最近发起者）。"""
    first = StubSSH(host="a")
    second = StubSSH(host="b")
    tool = _tool_with(first, second)
    tool._slots.set_focus(first)
    tool._slots.set_focus(second)
    tool.kill_active_exec()
    assert (first.interrupts, second.interrupts) == (0, 1)


def test_serial_esc_covers_all_active_sessions():
    first, second, idle = StubSerial(port="COM3"), StubSerial(port="COM4"), StubSerial(port="COM5")
    tool = _serial_tool_with(first, second, idle)
    tool._slots.add_active(0)
    tool._slots.add_active(1)
    tool.kill_active_exec()
    assert (first.kills, second.kills, idle.kills) == (1, 1, 0)


# ═══════════════════════════════════════════════════════════════
# R22 设备引用约定（devN）
# ═══════════════════════════════════════════════════════════════


def test_resolve_unique_and_multiple():
    session = StubSSH(host="10.0.0.1", execute_result="ok")
    tool = _tool_with(session)
    assert _text(tool.execute({"action": "exec", "command": "ls"})) == "[dev1(终端0)] ok"

    two = _tool_with(StubSSH(host="10.0.0.1"), StubSSH(host="10.0.0.2"))
    out = _text(two.execute({"action": "exec", "command": "ls"}))
    assert out == ("[错误: 有多个会话，请指定host=dev编号，当前已连接: "
                   "dev1(root@10.0.0.1)、dev2(root@10.0.0.2)]")

    none = _text(TerminalTool().execute({"action": "exec", "command": "ls"}))
    assert none == "[错误: 无活跃会话，请先connect]"


def test_resolve_dev0_rejected_for_exec():
    tool = _tool_with(StubSSH())
    out = _text(tool.execute({"action": "exec", "command": "ls", "host": "dev0"}))
    assert out == ("[错误: dev0是本机，无SSH会话。本机执行命令请用 Shell 工具"
                   "（Terminal 的 exec 用于SSH远程设备，dev0仅用于transfer）]")


def test_resolve_dev_missing_and_invalid():
    tool = _tool_with(StubSSH(host="10.0.0.1"))
    out = _text(tool.execute({"action": "exec", "command": "ls", "host": "dev3"}))
    assert out == "[错误: dev3未连接，请先connect。当前已连接: dev1(root@10.0.0.1)]"
    out = _text(tool.execute({"action": "exec", "command": "ls", "host": "web1"}))
    assert out == ("[错误: 设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备；"
                   "本机执行命令请用Shell工具)。当前已连接: dev1(root@10.0.0.1)]")
    empty = _text(TerminalTool().execute({"action": "exec", "command": "ls", "host": "web1"}))
    assert empty.endswith("当前无已连接设备，请先connect]")


def test_resolve_ip_loose_reference():
    session = StubSSH(host="10.0.0.1", username="root", execute_result="ok")
    tool = _tool_with(session)
    assert _text(tool.execute({"action": "exec", "command": "ls",
                               "host": "10.0.0.1"})) == "[dev1(终端0)] ok"
    assert _text(tool.execute({"action": "exec", "command": "ls",
                               "host": "root@10.0.0.1"})) == "[dev1(终端0)] ok"

    dup = _tool_with(StubSSH(host="10.0.0.1"), StubSSH(host="10.0.0.1"))
    out = _text(dup.execute({"action": "exec", "command": "ls", "host": "10.0.0.1"}))
    assert out == ("[错误: 多个会话匹配 '10.0.0.1'，请用dev编号区分: "
                   "dev1(root@10.0.0.1)、dev2(root@10.0.0.1)]")


def test_dev_display_and_tool_normalization():
    tool = _tool_with(StubSSH(host="10.0.0.7"))
    local = terminal_module.local_host()
    assert tool.resolve_dev_display("") == local
    assert tool.resolve_dev_display("dev0") == local
    assert tool.resolve_dev_display("dev1") == "10.0.0.7"
    assert tool.resolve_dev_display("dev2") == "dev2"
    assert tool.resolve_dev_display("10.0.0.9") == "10.0.0.9"

    assert tool.normalize_device_for_tools("") == ""
    assert tool.normalize_device_for_tools("dev0") == ""
    assert tool.normalize_device_for_tools("DEV1") == "DEV1"
    assert tool.normalize_device_for_tools("server") is None
    assert tool.file_tool_device_hint() == \
        "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)。当前已连接: dev1(root@10.0.0.7)"
    assert TerminalTool().file_tool_device_hint() == \
        "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)。当前无已连接设备，请先Terminal connect"
    assert tool.device_error("dev1") is None
    assert tool.device_error("") is None
    assert signal.has_error(tool.device_error("x"))


# ═══════════════════════════════════════════════════════════════
# R13~R20/R25 Serial：连接、执行、裸执行、输入、状态、上限接线
# ═══════════════════════════════════════════════════════════════


class _FakeSerialFactory:
    def __init__(self, error: Exception | None = None, initial: str = ""):
        self.error = error
        self.initial = initial
        self.instances: list[StubSerial] = []

    def __call__(self, **kwargs) -> StubSerial:
        if self.error is not None:
            raise self.error
        session = StubSerial(port=kwargs["port"], baudrate=kwargs["baudrate"],
                             initial=self.initial)
        session.kwargs = kwargs
        self.instances.append(session)
        return session


def _install_fake_serial(monkeypatch, error=None, initial: str = "") -> _FakeSerialFactory:
    factory = _FakeSerialFactory(error=error, initial=initial)
    monkeypatch.setattr(serial_module, "SerialSession", factory)
    return factory


def test_scan_list_format(monkeypatch):
    import serial.tools.list_ports as list_ports
    ports = [
        SimpleNamespace(device="COM3", description="USB-SERIAL CH340",
                        hwid="USB VID:PID=1A86:7523"),
        SimpleNamespace(device="COM4", description="n/a", hwid="n/a"),
        SimpleNamespace(device="COM5", description="", hwid=""),
        SimpleNamespace(device="COM3", description="COM3", hwid="hw"),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    out = _text(SerialTool().execute({"action": "scan"}))
    assert out.split("\n") == [
        "可用串口:",
        "  COM3  — USB-SERIAL CH340  [USB VID:PID=1A86:7523]",
        "  COM4",
        "  COM5  — (无描述)",
        "  COM3  [hw]",
    ]


def test_scan_empty(monkeypatch):
    import serial.tools.list_ports as list_ports
    monkeypatch.setattr(list_ports, "comports", lambda: [])
    assert _text(SerialTool().execute({"action": "scan"})) == "[未检测到串口设备]"


def test_scan_failure(monkeypatch):
    import serial.tools.list_ports as list_ports

    def _boom():
        raise OSError("driver error")

    monkeypatch.setattr(list_ports, "comports", _boom)
    assert _text(SerialTool().execute({"action": "scan"})) == \
        "[错误: 扫描串口失败: driver error]"


def test_serial_connect_requires_port():
    assert _text(SerialTool().execute({"action": "connect"})) == \
        "[错误: connect 需要提供 port（串口设备名）]"


def test_serial_connect_receipt_and_initial_output(monkeypatch):
    _install_fake_serial(monkeypatch, initial="U-Boot 2020\nlogin: ")
    out = _text(SerialTool().execute({"action": "connect", "port": "COM3"}))
    assert out == "[已连接终端0: COM3 @115200]\nU-Boot 2020\nlogin: "

    _install_fake_serial(monkeypatch)
    assert _text(SerialTool().execute({"action": "connect", "port": "COM4", "baudrate": "9600"})) == \
        "[已连接终端0: COM4 @9600]"


def test_serial_connect_port_in_use_and_case_insensitive(monkeypatch):
    _install_fake_serial(monkeypatch)
    tool = _serial_tool_with(StubSerial(port="COM3"))
    assert _text(tool.execute({"action": "connect", "port": "COM3"})) == \
        "[错误: COM3 已被终端0占用，请先 close 终端0]"
    monkeypatch.setattr(sys, "platform", "win32")
    assert _text(tool.execute({"action": "connect", "port": "com3"})) == \
        "[错误: com3 已被终端0占用，请先 close 终端0]"


def test_serial_connect_existing_slot_and_out_of_range(monkeypatch):
    _install_fake_serial(monkeypatch)
    tool = _serial_tool_with(StubSerial(port="COM3"), max_sessions=2)
    assert _text(tool.execute({"action": "connect", "port": "COM9", "session_id": 0})) == \
        "[串口终端0已连接: COM3 @115200]"
    assert _text(tool.execute({"action": "connect", "port": "COM9", "session_id": 2})) == \
        "[错误: session_id 范围 0-1]"


def test_serial_connect_slots_exhausted_and_placeholder_release(monkeypatch):
    tool = _serial_tool_with(StubSerial(port="COM1"), StubSerial(port="COM2"),
                             max_sessions=2)
    out = _text(tool.execute({"action": "connect", "port": "COM3"}))
    assert out == "[错误: 已达最大会话数(2)，当前终端: [0, 1]，请先 close 释放]"

    _install_fake_serial(monkeypatch, error=RuntimeError("access denied"))
    fresh = SerialTool()
    out = _text(fresh.execute({"action": "connect", "port": "COM3"}))
    assert out == "[错误: 无法打开串口 COM3: access denied]"
    assert fresh._slots.entries() == {}  # 失败释放占位槽


def test_serial_connect_open_failure_keeps_other_slots(monkeypatch):
    _install_fake_serial(monkeypatch, error=RuntimeError("busy"))
    tool = _serial_tool_with(StubSerial(port="COM1"))
    tool.execute({"action": "connect", "port": "COM2"})
    assert sorted(tool._slots.entries()) == [0]
    assert tool.execute({"action": "status"}).llm_text is not None


def test_serial_prompt_pattern_invalid_propagates(monkeypatch):
    _install_fake_serial(monkeypatch, error=ValueError("无效的 prompt_pattern 正则: bad"))
    out = _text(SerialTool().execute({"action": "connect", "port": "COM3",
                                      "prompt_pattern": "["}))
    assert out == "[错误: 无法打开串口 COM3: 无效的 prompt_pattern 正则: bad]"


def test_serial_connect_reclaims_dead_slot(monkeypatch):
    _install_fake_serial(monkeypatch)
    dead = StubSerial(port="COM1", alive=False)
    tool = _serial_tool_with(dead, max_sessions=1)
    out = _text(tool.execute({"action": "connect", "port": "COM2"}))
    assert out == "[已连接终端0: COM2 @115200]"
    assert dead.close_calls == 1


def test_serial_exec_prefix_and_validation():
    session = StubSerial(port="COM3", execute_result="boot ok")
    tool = _serial_tool_with(session)
    assert _text(tool.execute({"action": "exec", "command": "ver"})) == "[终端0] boot ok"
    assert session.exec_calls == [("ver", 120, 8000)]
    assert _text(tool.execute({"action": "exec"})) == "[错误: exec 需要提供 command]"
    assert _text(tool.execute({"action": "exec", "command": "x", "timeout": 0})) == \
        "[错误: timeout 需为正整数（秒）]"


def test_serial_exec_clamped_by_settings():
    session = StubSerial()
    tool = _serial_tool_with(session)
    tool.execute({"action": "exec", "command": "ver", "timeout": 300},
                 _env(max_timeout_seconds=10))
    assert session.exec_calls == [("ver", 10, 8000)]


def test_serial_device_disconnected_reclaims_slot():
    dead = StubSerial(port="COM3", alive=False)
    tool = _serial_tool_with(dead)
    out = _text(tool.execute({"action": "exec", "command": "ver"}))
    assert out == "[错误: 终端0串口已断开，请重新 connect]"
    assert tool._slots.entries() == {}


def test_serial_write_failure_label():
    session = StubSerial()
    session.execute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write err"))
    tool = _serial_tool_with(session)
    assert _text(tool.execute({"action": "exec", "command": "ver"})) == \
        "[错误: 终端0命令执行失败: write err]"
    session.send_input = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    assert _text(tool.execute({"action": "input", "input": "y"})) == \
        "[错误: 终端0输入发送失败: x]"


def test_serial_raw_exec_and_listen_routes():
    session = StubSerial(execute_result="chunk")
    tool = _serial_tool_with(session)
    assert _text(tool.execute({"action": "raw_exec", "command": "at"})) == "[终端0] chunk"
    assert _text(tool.execute({"action": "raw_exec"})) == "[终端0] chunk"  # 空命令=纯监听
    assert session.raw_calls == [("at", 120, 8000), ("", 120, 8000)]
    assert _text(tool.execute({"action": "raw_exec", "timeout": 0})) == \
        "[错误: timeout 需为正整数（秒）]"


def test_serial_input_missing_and_idle_is_command():
    session = StubSerial(execute_result="done")
    tool = _serial_tool_with(session)
    assert _text(tool.execute({"action": "input"})) == "[错误: input 需要提供 input 内容]"
    assert _text(tool.execute({"action": "input", "input": "y"})) == "[终端0] done"
    assert session.input_calls == [("y", 120, 8000)]


def test_serial_resolve_paths():
    tool = _serial_tool_with(StubSerial(port="COM3"))
    # 自动选择唯一会话
    assert _text(tool.execute({"action": "exec", "command": "ver"})) == "[终端0] ok"
    # 指定 session_id 未连接 / 占位
    assert _text(tool.execute({"action": "exec", "command": "ver", "session_id": 3})) == \
        "[错误: 终端3未连接，请先 connect]"
    placeholder_tool = SerialTool()
    placeholder_tool._slots.put(0, None)
    assert _text(placeholder_tool.execute(
        {"action": "exec", "command": "ver", "session_id": 0})) == \
        "[错误: 终端0正在连接中，请稍候]"
    # 按端口匹配（大小写不敏感）
    monkeypatch_tool = _serial_tool_with(StubSerial(port="COM3"))
    assert _text(monkeypatch_tool.execute(
        {"action": "exec", "command": "ver", "port": "COM3"})) == "[终端0] ok"
    assert _text(monkeypatch_tool.execute(
        {"action": "exec", "command": "ver", "port": "COM9"})) == \
        "[错误: 端口 COM9 未连接，请先 connect 或 status 查看已连接的串口]"
    # 多会话歧义
    multi = _serial_tool_with(StubSerial(port="COM3"), StubSerial(port="COM4"))
    out = _text(multi.execute({"action": "exec", "command": "ver"}))
    assert out == ("[错误: 有2个会话，请指定 session_id 或 port（串口设备名，如COM1、/dev/ttyUSB0）。\n"
                   "终端0: COM3 @115200\n终端1: COM4 @115200]")
    assert _text(SerialTool().execute({"action": "exec", "command": "ver"})) == \
        "[错误: 无活跃会话，请先 connect]"


def test_serial_status_and_close_variants():
    assert _text(SerialTool().execute({"action": "status"})) == \
        "[无活跃串口会话，最多支持5个并发终端]"
    tool = _serial_tool_with(StubSerial(port="COM3"), StubSerial(port="COM4", busy=True, alive=False),
                             max_sessions=4)
    assert _text(tool.execute({"action": "status"})) == (
        "[串口会话]\n  终端0: COM3 @115200 [活跃|闲]\n  终端1: COM4 @115200 [已断开|忙]\n  [2个空闲]")
    tool._slots.put(3, None)
    assert "  终端3: [连接中...]" in _text(tool.execute({"action": "status"}))

    assert _text(tool.execute({"action": "close", "session_id": 2})) == "[终端2未连接]"
    assert _text(tool.execute({"action": "close", "session_id": 3})) == "[终端3连接中，已取消]"
    assert _text(tool.execute({"action": "close", "port": "COM9"})) == "[错误: 端口 COM9 未连接]"
    assert _text(tool.execute({"action": "close", "port": "COM4"})) == "[已关闭终端1]"
    assert _text(tool.execute({"action": "close"})) == "[已关闭1个串口会话]"
    assert tool._slots.entries() == {}


def test_serial_cleanup_and_close_all_clears_active():
    first, second = StubSerial(port="COM1"), StubSerial(port="COM2")
    tool = _serial_tool_with(first, second)
    tool._slots.add_active(1)
    assert _text(tool.execute({"action": "close"})) == "[已关闭2个串口会话]"
    assert (first.close_calls, second.close_calls) == (1, 1)
    assert tool._slots.active_sids() == []

    third = StubSerial(port="COM3")
    tool2 = _serial_tool_with(third)
    tool2.cleanup()
    assert third.close_calls == 1 and tool2._slots.entries() == {}


def test_serial_max_sessions_from_config_and_clamp():
    """明示修复项：`工具.SSH最大会话数` 对 Serial 生效（含内部钳制 1-10）。"""
    assert SerialTool()._slots.max_sessions == 5
    assert SerialTool(2)._slots.max_sessions == 2
    assert SerialTool(20)._slots.max_sessions == 10
    assert SerialTool(0)._slots.max_sessions == 1
    assert TerminalTool(2)._slots.max_sessions == SerialTool(2)._slots.max_sessions
    tool = SerialTool(2)
    tool._slots.put(0, StubSerial(port="COM1"))
    tool._slots.put(1, StubSerial(port="COM2"))
    assert "[错误: 已达最大会话数(2)" in _text(tool.execute(
        {"action": "connect", "port": "COM3"}))


# ═══════════════════════════════════════════════════════════════
# Serial 会话层：静默回落、裸执行、纯监听、背压、怪癖
# ═══════════════════════════════════════════════════════════════


class RecordingSerial:
    """记录参数的伪 pyserial.Serial（供 SerialSession 构造路径测试）。"""

    def __init__(self):
        self.is_open = False
        self.port = None
        self.baudrate = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.xonxoff = None
        self.rtscts = None
        self.timeout = None

    def open(self):
        self.is_open = True

    def read(self, _n):
        import time as _time
        _time.sleep(0.02)
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def test_serial_session_param_silent_fallback(monkeypatch):
    """兼容怪癖：非法 databits/parity/stopbits/flow_control 静默回落默认值，无提示。"""
    import serial as pyserial
    created: list[RecordingSerial] = []

    def _factory():
        inst = RecordingSerial()
        created.append(inst)
        return inst

    monkeypatch.setattr(pyserial, "Serial", _factory)
    monkeypatch.setattr(SerialSession, "_drain_initial", lambda self, *a, **k: "")

    good = SerialSession(port="COM1", baudrate=9600, databits=7, parity="e",
                         stopbits=2, flow_control="hardware")
    assert created[-1].bytesize == pyserial.SEVENBITS
    assert created[-1].parity == pyserial.PARITY_EVEN
    assert created[-1].stopbits == pyserial.STOPBITS_TWO
    assert created[-1].rtscts is True and created[-1].xonxoff is False
    good.close()

    bad = SerialSession(port="COM2", databits=9, parity="X", stopbits=3,
                        flow_control="bogus")
    assert created[-1].bytesize == pyserial.EIGHTBITS
    assert created[-1].parity == pyserial.PARITY_NONE
    assert created[-1].stopbits == pyserial.STOPBITS_ONE
    assert created[-1].rtscts is False and created[-1].xonxoff is False
    bad.close()

    soft = SerialSession(port="COM3", flow_control="software")
    assert created[-1].xonxoff is True
    soft.close()


def test_serial_session_invalid_prompt_pattern_raises():
    with pytest.raises(ValueError, match="无效的 prompt_pattern 正则"):
        SerialSession(port="COM9", prompt_pattern="[")


def test_serial_raw_execute_pure_timeout_tags():
    """raw_exec/纯监听标签（有输出/无输出/超时）。"""
    session = _bare_serial()
    _feed_serial(session, "boot: v1.0\n")
    out = session.raw_execute("", timeout=0.3)
    assert out.endswith("[监听结束: 已达0.3秒]")
    assert "boot: v1.0" in out

    empty = _bare_serial()
    assert empty.raw_execute("", timeout=0.1) == "[监听结束: 0.1秒内无输出]"

    sender = _bare_serial()
    _feed_serial(sender, "chunk-1\n")
    out = sender.raw_execute("at", timeout=0.3)
    assert out.endswith("[超时: 命令执行超过0.3秒]")
    assert "chunk-1" in out


def test_serial_raw_execute_interrupted_tag():
    session = _bare_serial()
    timer = threading.Timer(0.05, session._interrupt.set)
    timer.daemon = True
    timer.start()
    assert "[用户中断]" in session.raw_execute("at", timeout=1)


def test_serial_execute_timeout_resets_busy():
    """兼容怪癖：Serial 超时不收敛——忙状态立即复位、不缓存后台输出。"""
    session = _bare_serial()
    _feed_serial(session, "running...\n")
    out = session.execute("sleep 100", timeout=0.3)
    assert "[超时: 命令执行超过0.3秒]" in out
    assert "running..." in out
    assert session.busy is False


def test_serial_send_ctrl_c():
    session = _bare_serial()
    _feed_serial(session, "resp\nCOM3 > ")
    out = session.send_input("^C", timeout=0.5)
    assert "COM3 >" in out
    assert session._ser.writes == [b"\x03"]

    stuck = _bare_serial()
    out = stuck.send_input("^C", timeout=0.1)
    assert out.endswith("[已发送Ctrl+C]")
    assert stuck._ser.writes == [b"\x03"]


def test_serial_reader_loop_backpressure():
    """背压截断：缓冲超限时丢弃最早一半并前置标注（静默数据丢失）。"""
    session = _bare_serial(buffer="")
    payload = "A" * 1_100_000

    class OneShotSerial(FakeSerial):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def read(self, _n):
            self.calls += 1
            if self.calls == 1:
                return payload.encode("utf-8")
            raise RuntimeError("unplugged")

    session._ser = OneShotSerial()
    session._reader_loop()
    assert session._dead is True
    assert session._buffer.startswith("...[背压截断: 丢弃前")
    assert len(session._buffer) < SerialSession.BUFFER_MAX_CHARS + 100


def test_serial_utf8_replacement_quirk():
    """兼容怪癖：固定 UTF-8 解码（无编码探测），非 UTF-8 字节呈现替换字符。"""
    raw = "中文".encode("gbk").decode("utf-8", errors="replace")
    assert "\ufffd" in raw
    assert "\ufffd" in SerialSession._clean_output(raw)
    assert "\ufffd" in SSHSession._clean_output(raw)


def test_serial_prompt_info_space_quirk():
    session = StubSerial(port="COM3", baudrate=115200)
    assert session.prompt_info == "COM3 @115200"


def test_await_confirm_constant_shared(monkeypatch):
    """怪癖统一：Terminal/Serial 的待确认返回值同为 `contracts.AWAIT_CONFIRM` 常量。"""
    monkeypatch.setattr(sys, "platform", "linux")  # 非 Windows 走挂起路径
    tool = _tool_with(StubSSH(host="h"))
    env = _env()
    out = _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, env))
    assert out == AWAIT_CONFIRM
    serial = _serial_tool_with(StubSerial(port="COM3"))
    env2 = _env()
    out2 = _text(serial.execute({"action": "exec", "command": "del /f x"}, env2))
    assert out2 == AWAIT_CONFIRM
    assert env.delete_gate.take()[0] == "Terminal"
    assert env2.delete_gate.take()[0] == "Serial"


# ═══════════════════════════════════════════════════════════════
# R24 删除命令安全确认
# ═══════════════════════════════════════════════════════════════


def test_confirm_windows_flow(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    tool = _tool_with(StubSSH(host="h", execute_result="ok"))

    rejected = ToolEnvImpl(settings=ToolSettingsImpl(), confirm=lambda _cmd: False)
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, rejected)) == \
        "[操作已取消: 此命令需用户确认]"

    accepted = ToolEnvImpl(settings=ToolSettingsImpl(), confirm=lambda _cmd: True)
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, accepted)) == \
        "[dev1(终端0)] ok"

    headless = _env()  # confirm 未注入 → 不拦截
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, headless)) == \
        "[dev1(终端0)] ok"


def test_confirm_non_windows_pend_and_confirm(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tool = _tool_with(StubSSH(host="h", execute_result="ok"))
    env = _env(rm_skip_confirm=False)
    first = _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, env))
    assert first == AWAIT_CONFIRM
    name, arguments = env.delete_gate.take()
    assert name == "Terminal" and arguments["action"] == "exec" \
        and arguments["command"] == "rm -rf /tmp/x"
    assert arguments["timeout"] == 120 and arguments["max_output_chars"] == 8000

    env.delete_gate.mark_confirmed()
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, env)) == \
        "[dev1(终端0)] ok"
    assert env.delete_gate.consume_confirmed() is False  # 一次性标记已消费


def test_confirm_skip_switches_and_headless(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tool = _tool_with(StubSSH(host="h", execute_result="ok"))
    # 免确认开关
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"},
                              _env(rm_skip_confirm=True))) == "[dev1(终端0)] ok"
    # git 免确认开关（Terminal 侧）
    assert _text(tool.execute({"action": "exec", "command": "git clean -fd"},
                              _env(git_skip_confirm=True))) == "[dev1(终端0)] ok"
    # 无 env（headless）直接放行
    assert _text(tool.execute({"action": "exec", "command": "rm -rf /tmp/x"}, None)) == \
        "[dev1(终端0)] ok"
    # 非删除命令不拦截
    assert _text(tool.execute({"action": "exec", "command": "ls -la"},
                              _env())) == "[dev1(终端0)] ok"


def test_confirm_git_scope_differs_between_tools(monkeypatch):
    """各工具确认范围差异：Terminal 命中 git 触发确认，Serial 不触发。"""
    monkeypatch.setattr(sys, "platform", "linux")
    terminal = _tool_with(StubSSH(host="h", execute_result="ok"))
    assert _text(terminal.execute({"action": "exec", "command": "git status"},
                                  _env())) == AWAIT_CONFIRM
    serial = _serial_tool_with(StubSerial(port="COM3", execute_result="ok"))
    assert _text(serial.execute({"action": "exec", "command": "git status"},
                                _env())) == "[终端0] ok"
    # 删除动词后必须跟空白或 /（\b 边界防误伤普通词）
    assert _text(terminal.execute({"action": "exec", "command": "formatting tool"},
                                  _env())) == "[dev1(终端0)] ok"
    assert _text(terminal.execute({"action": "exec", "command": "Remove-Item x"},
                                  _env())) == AWAIT_CONFIRM


def test_confirm_covers_input_and_raw_exec(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tool = _tool_with(StubSSH(host="h", execute_result="ok"))
    env = _env()
    assert _text(tool.execute({"action": "input", "input": "rm -rf /"}, env)) == AWAIT_CONFIRM
    name, arguments = env.delete_gate.take()
    assert name == "Terminal" and arguments["action"] == "input" and arguments["input"] == "rm -rf /"

    serial = _serial_tool_with(StubSerial(port="COM3", execute_result="ok"))
    env2 = _env()
    assert _text(serial.execute({"action": "raw_exec", "command": "rm -rf /"}, env2)) == AWAIT_CONFIRM
    assert env2.delete_gate.take()[1]["action"] == "raw_exec"
    # 纯监听（空命令）跳过确认
    assert _text(serial.execute({"action": "raw_exec", "command": ""}, _env())) == "[终端0] ok"


# ═══════════════════════════════════════════════════════════════
# R5/R6/R8/R9/R10 SSH 会话层：忙/超时/断连/中断/输入/sudo
# ═══════════════════════════════════════════════════════════════


def test_ssh_busy_session_rejects_exec():
    session = _bare_ssh(FakeChannel([]), busy=True)
    out = session.execute("ls", timeout=1)
    assert "上一个命令尚未完成，此终端暂不可用。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它" in out
    assert out.endswith("root@10.0.0.1:~$")


def test_ssh_normal_completion_end_to_end():
    """完整读循环：命令回显 → 输出 → PS1 静默确认 → 延迟哨兵 → 退出码/cwd/提示符。"""
    session = _bare_ssh(FakeChannel([]))

    def _sentinel_reply(_channel) -> bytes:
        marker = session._pending_marker
        pwd_marker = session._pending_pwd_marker
        return (f"rc=$?; printf '\\n'; echo {marker}0; pwd -P; echo {pwd_marker}\r\n"
                f"{marker}0\r\n/home/root\r\n{pwd_marker}\r\nroot@dev:~$ ").encode("utf-8")

    # drain 阶段吃掉两次超时；随后回显+输出、PS1、两次静默（触发哨兵）、哨兵输出
    session._channel = FakeChannel([
        socket.timeout(), socket.timeout(),
        b"ls -l\na.txt\n",
        b"root@dev:~$ ",
        socket.timeout(), socket.timeout(),
        _sentinel_reply,
    ])
    out = signal.strip_tags(session.execute("ls -l", timeout=5))
    assert out == "[exit code: 0]\na.txt\nroot@10.0.0.1:~$"
    assert session.busy is False
    assert session.cwd == "/home/root"
    # 命令本体与哨兵分两次写入（哨兵延迟注入协议：命令执行完才发哨兵）
    assert session._channel.sent[0] == "ls -l\n"
    assert len(session._channel.sent) == 2
    assert "echo" in session._channel.sent[1] and session._pending_marker in session._channel.sent[1]


def test_ssh_timeout_marks_busy_and_starts_watcher():
    channel = FakeChannel([b"cmd\nsome output\n", socket.timeout()])
    session = _bare_ssh(channel)
    out = signal.strip_tags(session._read_until_marker("__M__", "__P__", timeout=0.3))
    assert "some output" in out
    assert "[超时: 命令执行超过0.3秒，仍在后台运行。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]" in out
    assert out.endswith("root@10.0.0.1:~$")
    assert session.busy is True
    assert session._watcher_thread is not None and session._watcher_thread.is_alive()
    session.close()


def test_ssh_zero_output_means_conn_lost():
    channel = FakeChannel([socket.timeout()] * 4)
    session = _bare_ssh(channel)
    out = signal.strip_tags(session._read_until_marker("__M__", "__P__", timeout=0.2))
    assert out.startswith("[错误: 连接已中断（设备可能关机/重启或网络不通），命令结果未知]")
    assert session.busy is False
    assert channel.closed is True  # 主动关闭通道，让下次调用立即重连


def test_ssh_eof_means_conn_lost():
    channel = FakeChannel([b""])
    session = _bare_ssh(channel)
    out = signal.strip_tags(session._read_until_marker("__M__", "__P__", timeout=1))
    assert "[错误: 连接已中断（设备可能关机/重启或网络不通），命令结果未知]" in out


def test_ssh_zero_output_with_echo_off_falls_back_to_timeout():
    """echo off 的 tty 下零输出无区分度 → 退回超时语义（不误报断连）。"""
    channel = FakeChannel([socket.timeout()] * 4)
    session = _bare_ssh(channel, echo=False)
    out = signal.strip_tags(session._read_until_marker("__M__", "__P__", timeout=0.2))
    assert "[超时: 命令执行超过0.2秒" in out
    session.close()


def test_ssh_interrupt_returns_user_interrupt_tag():
    channel = FakeChannel([b"partial\n", socket.timeout()])
    session = _bare_ssh(channel)
    session._interrupt.set()
    out = signal.strip_tags(session._read_until_marker("__M__", "__P__", timeout=1))
    assert "[用户中断]" in out
    assert session.busy is False
    assert session._interrupt.is_set() is False  # 中断标志被清除（避免污染后续命令）


def test_ssh_input_idle_rejected_with_backlog():
    session = _bare_ssh(FakeChannel([]), busy=False)
    session._append_backlog("background done")
    out = session.send_input("y")
    assert out == ("[后台命令已完成，输出如下]\nbackground done\n"
                   "[当前无命令等待输入，输入内容未发送（避免被当作命令执行）。如需执行命令请用 exec]")
    assert session._channel.sent == []  # 未发送


def test_ssh_input_ctrl_c_success():
    channel = FakeChannel([b"^C\r\nroot@dev:~$ "])
    session = _bare_ssh(channel, busy=True)
    out = session.send_input("^C", timeout=1)
    assert out == "[已中断: 正在运行的命令已被 ^C 终止]\nroot@10.0.0.1:~$"
    assert session.busy is False
    assert channel.sent == ["\x03"]  # 原始 Ctrl+C（不追加换行）


def test_ssh_input_ctrl_c_not_effective():
    channel = FakeChannel([b"still running\n", socket.timeout()])
    session = _bare_ssh(channel, busy=True)
    out = session.send_input("^C", timeout=0.2)
    assert "[^C已发送但命令未终止，仍在后台运行。可稍后再试，或由用户按ESC中断]" in out
    assert session.busy is True
    session.close()


def test_ssh_input_text_appends_newline_and_waits():
    channel = FakeChannel([b"Password: \n\x1b[?2004h> \n", b"__NARNAT_PWD_5__\n", b"root@dev:~$ "])
    session = _bare_ssh(channel, busy=True)
    session._pending_marker = "__NARNAT_MARKER_5__"
    session._pending_pwd_marker = "__NARNAT_PWD_5__"
    session._sentinel_sent = True
    out = session.send_input("secret", timeout=1)
    assert channel.sent == ["secret\n"]
    assert "root@10.0.0.1:~$" in out


def test_ssh_sudo_prompt_without_password_asks_input():
    channel = FakeChannel([b"doing stuff\nPassword: "])
    session = _bare_ssh(channel, sudo_password=None)
    out = session._read_until_marker("__M__", "__P__", timeout=3)
    assert out.endswith("[检测到密码提示，请用input action输入密码]")
    assert session.busy is True
    session.close()


def test_truncate_hint_and_invalid_limit():
    text = "x" * 100
    out = signal.strip_tags(ssh_session_module.truncate_output(text, 30))
    assert "[中间截断: 输出共100字符, 已保留首20字符+尾10字符(≈" in out
    assert signal.strip_tags(ssh_session_module.truncate_output(text, 0)) == \
        "[错误: max_output_chars需为正整数]"
    serial_out = signal.strip_tags(serial_session_module.truncate_output(text, 0))
    assert serial_out == "[错误: max_output_chars需为正整数]"
    assert "≈" not in signal.strip_tags(serial_session_module.truncate_output(text, 30))


def test_ssh_drain_backlog_thread_safety():
    session = _bare_ssh(FakeChannel([]))
    session._append_backlog("first")
    session._append_backlog("second")
    assert session._drain_backlog() == "first\nsecond"
    assert session._drain_backlog() == ""


# ═══════════════════════════════════════════════════════════════
# R12 文件传输
# ═══════════════════════════════════════════════════════════════


def test_transfer_both_local_and_same_path():
    tool = TerminalTool()
    assert _text(tool.execute({"action": "transfer", "source_path": "/a", "target_path": "/b"})) == \
        "[错误: 源和目标都是本机(dev0)，请使用本地文件操作工具]"
    device = StubSSH(host="10.0.0.1")
    tool = _tool_with(device)
    assert _text(tool.execute({"action": "transfer", "source_host": "dev1",
                               "source_path": "/a", "target_host": "dev1",
                               "target_path": "/a"})) == "[错误: 源和目标相同，无需传输]"


def test_transfer_missing_paths():
    tool = TerminalTool()
    assert _text(tool.execute({"action": "transfer", "target_path": "/b"})) == \
        "[错误: transfer需要提供source_path（源文件路径）]"
    assert _text(tool.execute({"action": "transfer", "source_path": "/a"})) == \
        "[错误: transfer需要提供target_path（目标文件路径）]"


def test_transfer_source_dir_rejected(tmp_path):
    directory = tmp_path / "folder"
    directory.mkdir()
    tool = _tool_with(StubSSH(host="10.0.0.1"))
    out = _text(tool.execute({"action": "transfer", "source_path": str(directory),
                              "target_host": "dev1", "target_path": "/tmp/x"}))
    assert "源是目录，transfer仅支持文件传输。目录请先用 Shell 打包为单个文件（如 tar/zip）再传输" in out


def test_transfer_size_limit(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    tool = _tool_with(StubSSH(host="10.0.0.1"))
    out = _text(tool.execute({"action": "transfer", "source_path": str(big),
                              "target_host": "dev1", "target_path": "/tmp/x"},
                             _env(max_transfer_mb=1)))
    assert out == "[错误: 文件大小 1.0MB 超过传输上限 1.0MB]"


def test_transfer_local_to_remote_receipt_and_parent_dir(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("payload", encoding="utf-8")
    session = StubSSH(host="10.0.0.1")
    sftp = FakeSFTP()
    session.sftp = sftp
    tool = _tool_with(session)
    out = _text(tool.execute({"action": "transfer", "source_path": str(source),
                              "target_host": "dev1", "target_path": "/opt/app/data.txt"}))
    assert out == f"[已传输: 本机:{source} → dev1:/opt/app/data.txt (7B)]"
    assert sftp.files["/opt/app/data.txt"] == b"payload"
    assert sftp.made_dirs == ["/opt", "/opt/app"]


def test_transfer_device_not_connected(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("x", encoding="utf-8")
    tool = TerminalTool()
    out = _text(tool.execute({"action": "transfer", "source_path": str(source),
                              "target_host": "dev2", "target_path": "/tmp/x"}))
    assert out == "[错误: 目标设备 dev2 未连接或已断开，请先connect。当前已连接: (无)]"
    out = _text(tool.execute({"action": "transfer", "source_host": "dev2",
                              "source_path": "/tmp/x", "target_path": str(source)}))
    assert out == "[错误: 源设备 dev2 未连接或已断开，请先connect。当前已连接: (无)]"


def test_transfer_invalid_device_reference():
    tool = _tool_with(StubSSH(host="10.0.0.1"))
    out = _text(tool.execute({"action": "transfer", "source_host": "web",
                              "source_path": "/a", "target_host": "dev1",
                              "target_path": "/b"}))
    assert out.startswith("[错误: 设备引用统一用devN编号")


def test_transfer_remote_to_local_and_parent_created(tmp_path):
    session = StubSSH(host="10.0.0.1")
    sftp = FakeSFTP(files={"/srv/log.txt": b"remote-data"})
    session.sftp = sftp
    tool = _tool_with(session)
    target = tmp_path / "nested" / "log.txt"
    out = _text(tool.execute({"action": "transfer", "source_host": "dev1",
                              "source_path": "/srv/log.txt", "target_path": str(target)}))
    assert out == f"[已传输: dev1:/srv/log.txt → 本机:{target} (11B)]"
    assert target.read_bytes() == b"remote-data"


def test_transfer_remote_source_dir_rejected(tmp_path):
    session = StubSSH(host="10.0.0.1")
    session.sftp = FakeSFTP(dirs={"/srv"})
    tool = _tool_with(session)
    out = _text(tool.execute({"action": "transfer", "source_host": "dev1",
                              "source_path": "/srv", "target_path": str(tmp_path / "x")}))
    assert "目录请先用 exec 打包为单个文件（如 tar/zip）再传输: dev1:/srv" in out


def test_transfer_remote_to_remote_streams_and_reports_interrupt():
    src = StubSSH(host="10.0.0.1")
    tgt = StubSSH(host="10.0.0.2")
    src_sftp = FakeSFTP(files={"/data/big.bin": b"z" * 4096})
    tgt_sftp = FakeSFTP()
    src.sftp, tgt.sftp = src_sftp, tgt_sftp
    tool = _tool_with(src, tgt)
    out = _text(tool.execute({"action": "transfer", "source_host": "dev1",
                              "source_path": "/data/big.bin", "target_host": "dev2",
                              "target_path": "/backup/big.bin"}))
    assert out == "[已传输: dev1:/data/big.bin → dev2:/backup/big.bin (4.0KB)]"
    assert tgt_sftp.files["/backup/big.bin"] == b"z" * 4096
    assert tgt_sftp.made_dirs == ["/backup"]


def test_transfer_remote_to_remote_interrupt_message():
    src = StubSSH(host="10.0.0.1")
    tgt = StubSSH(host="10.0.0.2")
    src.sftp = FakeSFTP(files={"/data/big.bin": b"z" * 100})
    tgt_sftp = FakeSFTP()
    tgt_sftp.open_mode_raises["/backup/big.bin"] = IOError("connection reset")
    tgt.sftp = tgt_sftp
    tool = _tool_with(src, tgt)
    out = _text(tool.execute({"action": "transfer", "source_host": "dev1",
                              "source_path": "/data/big.bin", "target_host": "dev2",
                              "target_path": "/backup/big.bin"}))
    assert out == "[错误: 传输中断，已传输 0B/100B: connection reset]"


def test_format_size_and_limit_helpers():
    assert transfer_module.format_size(0) == "0B"
    assert transfer_module.format_size(1023) == "1023B"
    assert transfer_module.format_size(1536) == "1.5KB"
    assert transfer_module.format_size(1024 * 1024 * 2 + 512 * 1024) == "2.5MB"
    assert transfer_module.check_transfer_size(10, 0) is None
    assert transfer_module.check_transfer_size(2 * 1024 * 1024, 1) == \
        "文件大小 2.0MB 超过传输上限 1.0MB"


# ═══════════════════════════════════════════════════════════════
# R23 远程文件访问（Read/Edit/Write 的 devN 路径）
# ═══════════════════════════════════════════════════════════════


def _file_access(*sessions: StubSSH) -> RemoteFileAccess:
    return RemoteFileAccess(_tool_with(*sessions))


def _device_with_sftp(sftp: FakeSFTP, host: str = "10.0.0.1") -> tuple[StubSSH, RemoteFileAccess]:
    session = StubSSH(host=host)
    session.sftp = sftp
    return session, RemoteFileAccess(_tool_with(session))


def test_remote_file_no_session_messages():
    access = _file_access()
    assert access.read("/x", host="dev1") == "[错误: 无可用SSH会话，请先Terminal connect建立连接]"
    assert access.write("/x", "c", host="dev1") == \
        ("[错误: 无可用SSH会话，请先Terminal connect建立连接]", "")
    assert access.edit("/x", old_string="a", host="dev1") == \
        ("[错误: 无可用SSH会话，请先Terminal connect建立连接]", "")

    connected = _file_access(StubSSH(host="10.0.0.1"))
    out = connected.read("/x", host="dev2")
    assert out == ("[错误: 指定设备 dev2 未连接。设备标识只支持devN编号(dev1..devn)。"
                   "当前已连接: dev1(root@10.0.0.1)]")


def test_remote_read_limit_validation_and_format():
    _, access = _device_with_sftp(FakeSFTP(files={"/f.txt": b"one\ntwo\n"}))
    assert access.read("/f.txt", limit=0) == "[错误: limit需为正整数]"
    assert access.read("/f.txt") == "  1→one\n  2→two"


def test_remote_read_directory_binary_and_encoding():
    _, access = _device_with_sftp(FakeSFTP(dirs={"/d"}))
    assert access.read("/d") == "[错误: 远程路径是目录: /d，请用 Terminal exec 查看目录内容]"

    _, binary = _device_with_sftp(FakeSFTP(files={"/b.bin": b"a\x00b"}))
    assert binary.read("/b.bin") == \
        "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请用 Terminal exec 处理]"

    _, gbk = _device_with_sftp(FakeSFTP(files={"/g.txt": "中文内容\n".encode("gbk")}))
    assert gbk.read("/g.txt") == "  1→中文内容"


def test_remote_read_offset_truncate_and_empty():
    _, access = _device_with_sftp(FakeSFTP(files={"/f.txt": b"a\nb\nc\n"}))
    assert access.read("/f.txt", offset=10) == "[无内容: offset=10 已超出文件末尾（文件共3行）]"
    assert access.read("/f.txt", offset=2) == "  2→b\n  3→c"
    limited = access.read("/f.txt", limit=2)
    assert limited == ("  1→a\n  2→b\n"
                       "  ... [截断: 已显示 2 行。使用 offset=3 参数可读取其余部分]")
    assert access.read("/f.txt", offset=-5) == access.read("/f.txt")
    _, empty = _device_with_sftp(FakeSFTP(files={"/e.txt": b""}))
    assert empty.read("/e.txt") == "[文件为空]"


def test_remote_read_errno_classification():
    sftp = FakeSFTP()
    sftp.open_mode_raises["/gone"] = OSError(errno.ENOENT, "no such file")
    sftp.open_mode_raises["/denied"] = OSError(errno.EACCES, "permission denied")
    sftp.open_mode_raises["/weird"] = OSError(errno.EIO, "io error")
    _, access = _device_with_sftp(sftp)
    assert access.read("/gone") == "[错误: 远程文件不存在: /gone]"
    assert access.read("/denied") == "[错误: 远程文件权限不足（EACCES）: /denied]"
    assert access.read("/weird").startswith("[错误: 远程读取失败: /weird (")


def test_remote_write_success_diff_and_parent_dir():
    sftp = FakeSFTP(files={"/tmp/x.txt": b"a\n"})
    _, access = _device_with_sftp(sftp)
    llm, ui = access.write("/tmp/x.txt", "b\n", host="dev1")
    assert llm.startswith("[dev1] [已写入(远程): /tmp/x.txt (2字节)]\n--- a/x.txt\n+++ b/x.txt")
    assert sftp.files["/tmp/x.txt"] == b"b\n"
    assert "diff.added" not in ui and "\x1b[" in ui  # 着色 diff（UI 视角）

    new_file = FakeSFTP()
    _, fresh = _device_with_sftp(new_file)
    llm, _ = fresh.write("/opt/app/new.txt", "hi", host="")
    assert llm == "[已写入(远程): /opt/app/new.txt (2字节)]"
    assert new_file.made_dirs == ["/opt", "/opt/app"]
    assert new_file.files["/opt/app/new.txt"] == b"hi"


def test_remote_write_directory_and_creation_failure():
    _, access = _device_with_sftp(FakeSFTP(dirs={"/d"}))
    assert access.write("/d", "x", host="dev1") == \
        ("[错误: 远程路径是目录: /d，请使用正确的文件路径]", "")

    blocked = FakeSFTP(errors={"/ro": OSError(errno.EACCES, "denied")})
    _, denied = _device_with_sftp(blocked)
    assert denied.write("/ro/x.txt", "x", host="dev1") == \
        ("[错误: 无法创建远程目标目录: /ro（可能无写权限）]", "")


def test_remote_write_byte_level_hints():
    _, access = _device_with_sftp(FakeSFTP(files={"/f.txt": b"same\n"}))
    llm, _ = access.write("/f.txt", "same\n", host="dev1")
    assert llm == "[dev1] [提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]"

    _, crlf = _device_with_sftp(FakeSFTP(files={"/f.txt": b"line\r\n"}))
    llm, ui = crlf.write("/f.txt", "line\n", host="dev1")
    assert llm == "[dev1] [提示: 文件已写入，正文内容相同，但字节层面有变化（行尾符 CRLF→LF）]"
    assert "字节变化" in ui


def test_remote_edit_success_and_normalization():
    sftp = FakeSFTP(files={"/f.txt": "line1\r\nline2\r\n".encode("utf-8")})
    _, access = _device_with_sftp(sftp)
    llm, ui = access.edit("/f.txt", old_string="line1\nline2", new_string="X", host="dev1")
    assert llm.startswith("[dev1] [已替换1处]\n--- a/f.txt\n+++ b/f.txt")
    assert sftp.files["/f.txt"] == b"X\r\n"  # CRLF 文件按 CRLF 归一化写回
    assert "\x1b[" in ui


def test_remote_edit_validation_and_uniqueness():
    _, access = _device_with_sftp(FakeSFTP(files={"/f.txt": b"x\nx\nx\n"}))
    assert access.edit("/f.txt", old_string="", new_string="y", host="dev1") == \
        ("[错误: old_string不能为空]", "")
    assert access.edit("/f.txt", old_string="nope", new_string="y", host="dev1") == \
        ("[错误: 未找到匹配文本。请先Read确认远程文件内容。]", "")
    # 注: 旧实现该文案结尾无 `]`（与 spec 字面不一致的现状偏差，按 D11 保持等价）
    assert access.edit("/f.txt", old_string="x", new_string="y", host="dev1") == \
        ("[错误: 找到3处匹配，old_string不唯一。请扩大上下文使其唯一，或设置replace_all=True", "")
    llm, _ = access.edit("/f.txt", old_string="x", new_string="y", replace_all=True, host="dev1")
    assert llm.startswith("[dev1] [已替换3处]")


def test_remote_edit_no_change_hint_and_non_utf8_rejected():
    _, access = _device_with_sftp(FakeSFTP(files={"/f.txt": b"abc\n"}))
    llm, ui = access.edit("/f.txt", old_string="abc", new_string="abc", host="dev1")
    assert llm == "[dev1] [提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]"
    assert "[无差异]" in ui

    _, gbk = _device_with_sftp(FakeSFTP(files={"/g.txt": "中文".encode("gbk")}))
    llm, _ = gbk.edit("/g.txt", old_string="x", new_string="y", host="dev1")
    assert llm == ("[错误: 远程文件非UTF-8编码，为防止内容损坏已拒绝编辑: /g.txt。"
                   "请用Terminal exec处理（如转码为UTF-8后再编辑）]")


def test_remote_edit_read_errors():
    sftp = FakeSFTP()
    sftp.open_mode_raises["/gone"] = OSError(errno.ENOENT, "no such file")
    sftp.open_mode_raises["/denied"] = OSError(errno.EACCES, "permission denied")
    _, access = _device_with_sftp(sftp)
    assert access.edit("/gone", old_string="a", host="dev1") == \
        ("[错误: 远程文件不存在: /gone，如需创建请用Write工具]", "")
    assert access.edit("/denied", old_string="a", host="dev1") == \
        ("[错误: 远程文件权限不足（EACCES），未做任何修改: /denied]", "")


def test_remote_write_failure_message():
    sftp = FakeSFTP()
    sftp.open_mode_raises["/f.txt"] = OSError(errno.EIO, "io error")
    _, access = _device_with_sftp(sftp)
    llm, _ = access.write("/f.txt", "x", host="dev1")
    assert llm == "[错误: 远程写入失败: [Errno 5] io error]"


def test_colorize_diff_structure():
    diff = "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new\n same"
    out = colorize_diff(diff)
    lines = out.split("\n")
    assert lines[0].startswith("\x1b[1m\x1b[38;2;94;234;212m---")
    assert lines[2].startswith("\x1b[2m\x1b[38;2;94;234;212m@@")
    assert lines[3].startswith("\x1b[38;2;248;113;113m-old")
    assert lines[4].startswith("\x1b[38;2;52;211;153m+new")
    assert lines[5].startswith("\x1b[38;2;100;116;139m same")
    assert colorize_diff("[无差异]") == "\x1b[38;2;100;116;139m[无差异]\x1b[0m"


def test_remote_access_uses_injected_colorizer():
    def fake_colorize(diff: str) -> str:
        return f"<{diff}>"

    session = StubSSH(host="10.0.0.1")
    session.sftp = FakeSFTP(files={"/f.txt": b"a\n"})
    access = RemoteFileAccess(_tool_with(session), colorize=fake_colorize)
    _, ui = access.write("/f.txt", "b\n", host="dev1")
    assert ui.startswith("<--- a/f.txt")




