"""interrupt 积木自测 —— 信号语义 / 广播订阅 / 双模式状态机 / 按键采集。

对齐 `openspec/changes/recast-v2/specs/interrupt/spec.md` 的 Scenario 映射表
（「归他处」表示该 Scenario 的用户可见部分由其他积木实现，本积木实现其依赖面）：

| spec Scenario | 覆盖测试 |
|---|---|
| 运行中按 Esc | `test_escape_key_triggers_raise_through_default_listener`（标志+广播；提示文案归 ui） |
| 源码运行中按 Ctrl+C | `test_sigint_default_handler_in_source_mode` |
| 打包运行中按 Ctrl+C | `test_sigint_ignored_when_frozen` |
| 输入等待中的按键 | `test_input_mode_has_no_listener_thread`（归 ui 的输入会话） |
| 创建流时启用采集 | `test_enter_run_mode_clears_flag_and_starts_listener` |
| 流结束回到输入模式 | `test_enter_input_mode_stops_listener_and_clears_flag` |
| 手动压缩临时切模式 | `test_manual_compaction_cycle_returns_to_input_mode` |
| 入口清零 | `test_residual_interrupt_cleared_on_next_run` |
| AI 输出中按 Esc | `test_raise_broadcasts_in_subscribe_order`（LLM 断连订阅接线归 llm 积木） |
| 前台命令执行中按 Esc | `test_subscriber_exception_is_swallowed_and_others_still_run`（杀树订阅归 tools-shell） |
| 后台等待中按 Esc | `test_subscribe_during_broadcast_not_called_this_round`（等待唤醒订阅归 tools-shell） |
| SSH 命令执行中按 Esc | `test_raise_is_idempotent_and_repeats_broadcast`（远程中断订阅归 tools-remote） |
| 压缩期间按 Esc | `test_manual_compaction_cycle_returns_to_input_mode`（放弃动作归 compression） |
| 工具批执行中按 Esc | `test_raise_sets_flag_before_broadcasting`（调度检查点归 conversation） |
| 输入模式下按键不被吞 | `test_input_mode_has_no_listener_thread` |
| 运行模式连按 Esc | `test_scan_escape_double_escape_is_interrupt` / `..._consumes_at_most_five_bytes` |
| 切换不残留 | `test_listener_switch_leaves_single_thread` |
| 功能键不误触发 | `test_scan_escape_sequence_is_not_interrupt` / `test_console_input_source_ignores_non_escape_events` |
| 输入重定向时降级 | `test_listener_degrades_when_source_unavailable` / `..._console_unavailable` |
| POSIX 终端恢复 | `test_posix_source_restores_terminal_on_close` / `test_posix_atexit_registered_once` |
| 中断后立即回到提示符 | `test_signal_flag_lifecycle`（即读即见；提示与输入会话重建归 ui） |
| 程序异常与中断提示区分 | 归 ui 积木（`abort(message)`）；本积木只提供恒定广播语义 |
| 中断轮次不触发自动保存 | 归 conversation/sessions 积木（读 `is_set` 检查点） |
| SIGINT 处理恒定分支（兼容怪癖） | `test_no_saved_sigint_dead_branch_in_sources` / `test_sigint_*` |
| 订阅者异常被吞（兼容怪癖） | `test_subscriber_exception_is_swallowed_and_others_still_run` |
"""
from __future__ import annotations

import ast
import ctypes
import signal
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import narnat_agent.interrupt as interrupt_pkg
from narnat_agent.contracts.interrupt import InterruptSignal
from narnat_agent.interrupt import InterruptBus, KeyListener, poll_keys, scan_escape
from narnat_agent.interrupt import keys as keys_module
from narnat_agent.interrupt.keys import (
    ESC_BYTE,
    POLL_INTERVAL_SECONDS,
    STOP_JOIN_TIMEOUT_SECONDS,
    _ConsoleInputSource,
    _MsvcrtSource,
    _PosixSource,
)

INTERRUPT_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "interrupt"
INTERRUPT_SOURCES = sorted(INTERRUPT_DIR.glob("*.py"))


# ═══════════════════════════════════════════════════════════════
# 测试替身
# ═══════════════════════════════════════════════════════════════


class _ScriptedSource:
    """脚本化字符源：按序产出字节；空脚本时短暂让出（模拟真实源的节流）。"""

    def __init__(self, script=(), *, escape_immediate=False, probe_timeout=0.0,
                 consume_timeout=0.0, idle_sleep=0.002, fail_with=None):
        self._script = list(script)
        self.escape_immediate = escape_immediate
        self.probe_timeout = probe_timeout
        self.consume_timeout = consume_timeout
        self._idle_sleep = idle_sleep
        self._fail_with = fail_with
        self.read_timeouts: list[float] = []
        self.closed = False

    def read(self, timeout):
        self.read_timeouts.append(timeout)
        if self._fail_with is not None:
            raise self._fail_with
        if self._script:
            return self._script.pop(0)
        time.sleep(self._idle_sleep)
        return None

    def close(self):
        self.closed = True


class _FakeListener:
    """采集器替身：只记录 start/stop，用于验证模式机。"""

    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


class _FakeMsvcrt:
    def __init__(self, chars=()):
        self._chars = list(chars)
        self.calls: list[str] = []

    def kbhit(self):
        self.calls.append("kbhit")
        return bool(self._chars)

    def getch(self):
        self.calls.append("getch")
        return self._chars.pop(0)


class _FakeKernel32:
    """ReadConsoleInputW 的输入记录填充器（内存布局见 keys.py 注释）。"""

    def __init__(self, events=()):
        self._events = list(events)
        self.wait_calls: list[tuple] = []

    def GetStdHandle(self, which):
        return 1234

    def WaitForSingleObject(self, handle, milliseconds):
        self.wait_calls.append((handle, milliseconds))
        return 0 if self._events else 0x102  # WAIT_OBJECT_0 / WAIT_TIMEOUT

    def ReadConsoleInputW(self, handle, buffer, length, read_ref):
        event = self._events.pop(0)
        ctypes.memmove(buffer, event, len(event))
        read_ref._obj.value = 1
        return 1


def _key_event(vk_code, *, key_down=True):
    """构造一条 KEY_EVENT 输入记录（20 字节）。"""
    record = bytearray(20)
    record[0:2] = (0x0001).to_bytes(2, "little")
    record[4:8] = (1 if key_down else 0).to_bytes(4, "little")
    record[10:12] = vk_code.to_bytes(2, "little")
    return bytes(record)


class _FakeTermios:
    TCSADRAIN = 1

    def __init__(self):
        self.settings = "original-settings"
        self.getattr_calls: list[int] = []
        self.setattr_calls: list[tuple] = []

    def tcgetattr(self, fd):
        self.getattr_calls.append(fd)
        return self.settings

    def tcsetattr(self, fd, action, settings):
        self.setattr_calls.append((fd, action, settings))


class _FakeTty:
    def __init__(self):
        self.calls: list[int] = []

    def setcbreak(self, fd):
        self.calls.append(fd)


class _FakeSelect:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls: list[tuple] = []

    def select(self, rlist, wlist, xlist, timeout):
        self.calls.append((list(rlist), timeout))
        return (list(rlist) if self.ready else []), [], []


class _FakeOs:
    def __init__(self, script=()):
        self._script = list(script)
        self.read_calls: list[tuple] = []

    def read(self, fd, size):
        self.read_calls.append((fd, size))
        if self._script:
            return self._script.pop(0)
        return b""


class _FakeAtexit:
    def __init__(self):
        self.register_calls: list[tuple] = []

    def register(self, func, *args):
        self.register_calls.append((func, args))


def _fake_posix_modules():
    """伪造 termios / tty 模块，返回（模块对, 记录器）。"""
    fake_termios = _FakeTermios()
    termios = types.ModuleType("termios")
    termios.TCSADRAIN = _FakeTermios.TCSADRAIN
    termios.tcgetattr = fake_termios.tcgetattr
    termios.tcsetattr = fake_termios.tcsetattr

    fake_tty = _FakeTty()
    tty = types.ModuleType("tty")
    tty.setcbreak = fake_tty.setcbreak
    return (termios, tty), (fake_termios, fake_tty)


@pytest.fixture
def restore_sigint():
    """SIGINT 处理在测试后复原（本模块测试会改写进程级信号处理）。"""
    original = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, original)


# ═══════════════════════════════════════════════════════════════
# 1. 信号语义与广播订阅（Requirement: 中断广播与订阅者语义）
# ═══════════════════════════════════════════════════════════════


def test_bus_satisfies_interrupt_signal_protocol():
    """结构对齐契约：`InterruptBus` 满足 `contracts.interrupt.InterruptSignal`。"""
    assert isinstance(InterruptBus(listener=_FakeListener()), InterruptSignal)


def test_signal_flag_lifecycle():
    """标志即读即见：初始未置位 / 触发置位 / 清除复位（消费方检查点语义）。"""
    bus = InterruptBus(listener=_FakeListener())
    assert bus.is_set is False
    bus.raise_()
    assert bus.is_set is True
    bus.clear()
    assert bus.is_set is False


def test_raise_broadcasts_in_subscribe_order():
    """多订阅者按注册顺序同步调用（顺序即装配顺序）。"""
    bus = InterruptBus(listener=_FakeListener())
    calls = []
    bus.subscribe(lambda: calls.append("llm"))
    bus.subscribe(lambda: calls.append("shell"))
    bus.subscribe(lambda: calls.append("remote"))
    bus.raise_()
    assert calls == ["llm", "shell", "remote"]


def test_raise_sets_flag_before_broadcasting():
    """标志先于广播置位：订阅者触发瞬间即可读到已置位状态。"""
    bus = InterruptBus(listener=_FakeListener())
    seen = []
    bus.subscribe(lambda: seen.append(bus.is_set))
    bus.raise_()
    assert seen == [True]


def test_subscriber_exception_is_swallowed_and_others_still_run():
    """兼容怪癖：单个订阅动作失败被静默吞掉，不阻断其余订阅、不影响标志。"""
    bus = InterruptBus(listener=_FakeListener())
    calls = []

    def broken():
        calls.append("broken")
        raise RuntimeError("订阅者故障")

    bus.subscribe(broken)
    bus.subscribe(lambda: calls.append("after"))
    bus.raise_()  # 不抛异常
    assert calls == ["broken", "after"]
    assert bus.is_set is True


def test_raise_is_idempotent_and_repeats_broadcast():
    """重复触发：标志保持置位，广播逐次执行（订阅动作须自幂等）。"""
    bus = InterruptBus(listener=_FakeListener())
    calls = []
    bus.subscribe(lambda: calls.append(1))
    bus.raise_()
    bus.raise_()
    assert calls == [1, 1]
    assert bus.is_set is True


def test_subscribe_during_broadcast_not_called_this_round():
    """广播遍历的是订阅表快照：广播期间新注册的订阅者下一轮才生效。"""
    bus = InterruptBus(listener=_FakeListener())
    calls = []

    def late_subscriber():
        calls.append("late")
        bus.subscribe(lambda: calls.append("next-round"))

    bus.subscribe(late_subscriber)
    bus.raise_()
    assert calls == ["late"]
    bus.raise_()
    assert calls == ["late", "late", "next-round"]


# ═══════════════════════════════════════════════════════════════
# 2. 运行/输入双模式状态机（Requirement: 运行/输入双模式状态机）
# ═══════════════════════════════════════════════════════════════


def test_enter_run_mode_clears_flag_and_starts_listener():
    """创建流时启用采集：清除既有标志 + 启动 Esc 采集。"""
    listener = _FakeListener()
    bus = InterruptBus(listener=listener)
    bus.raise_()
    bus.enter_run_mode()
    assert bus.is_set is False
    assert listener.starts == 1


def test_enter_input_mode_stops_listener_and_clears_flag():
    """流结束（正常或中止）回到输入模式：停止采集 + 清除标志。"""
    listener = _FakeListener()
    bus = InterruptBus(listener=listener)
    bus.enter_run_mode()
    bus.raise_()
    bus.enter_input_mode()
    assert listener.stops == 1
    assert bus.is_set is False


def test_manual_compaction_cycle_returns_to_input_mode():
    """手动压缩：压缩期间临时启用采集，结束（无论成败）回到输入模式。"""
    listener = _FakeListener()
    bus = InterruptBus(listener=listener)
    bus.enter_input_mode()
    bus.enter_run_mode()  # 压缩开始
    bus.raise_()          # 压缩期间按 Esc
    bus.enter_input_mode()  # 压缩结束
    assert (listener.starts, listener.stops) == (1, 2)
    assert bus.is_set is False


def test_residual_interrupt_cleared_on_next_run():
    """入口清零：上一轮以 Esc 中断结束，新一局创建流时残留标志被清除。"""
    bus = InterruptBus(listener=_FakeListener())
    bus.raise_()
    assert bus.is_set is True
    bus.enter_run_mode()
    assert bus.is_set is False


def test_enter_modes_from_worker_thread_do_not_touch_signal_handlers():
    """SIGINT 设置只在主线程生效；非主线程调用模式切换不抛异常。"""
    bus = InterruptBus(listener=_FakeListener())
    original = signal.getsignal(signal.SIGINT)
    errors = []

    def worker():
        try:
            bus.enter_run_mode()
            bus.enter_input_mode()
        except Exception as exc:  # pragma: no cover - 失败时用于报告
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2.0)
    assert errors == []
    assert signal.getsignal(signal.SIGINT) is original


# ═══════════════════════════════════════════════════════════════
# 3. 按键判定（Requirement: 键盘监听生命周期 / 平台按键采集差异）
# ═══════════════════════════════════════════════════════════════


def test_scan_escape_single_escape_is_interrupt():
    """单次 Esc（判定窗口内无后续字节）→ 中断。"""
    assert scan_escape(_ScriptedSource([])) is True


def test_scan_escape_sequence_is_not_interrupt():
    """功能键/方向键（Esc 开头的转义序列）→ 消费但不中断。"""
    assert scan_escape(_ScriptedSource([b"[", b"A"])) is False


def test_scan_escape_double_escape_is_interrupt():
    """连按 Esc：消费窗口内出现第二个 Esc → 立即中断。"""
    assert scan_escape(_ScriptedSource([ESC_BYTE])) is True


def test_scan_escape_consumes_at_most_five_bytes():
    """消费上限 5 字节：窗口内第二个 Esc 触发中断；上限之外的 Esc 不触发（兼容怪癖）。"""
    inside = _ScriptedSource([b"1", b"2", b"3", ESC_BYTE])
    assert scan_escape(inside) is True

    beyond = _ScriptedSource([b"1", b"2", b"3", b"4", b"5", ESC_BYTE])
    assert scan_escape(beyond) is False


def test_scan_escape_settle_then_probe_with_source_timeouts():
    """判定窗口（20 毫秒静默后探测）使用源上的平台超时参数。"""
    source = _ScriptedSource([b"[", b"A"], probe_timeout=0.01, consume_timeout=0.005)
    scan_escape(source)
    assert source.read_timeouts == [0.01, 0.005, 0.005]


def test_scan_escape_immediate_source_skips_window():
    """事件源（Windows 非原生控制台）：Esc 键按下事件即刻判定中断，不进入判定窗口。"""
    source = _ScriptedSource([], escape_immediate=True)
    assert scan_escape(source) is True
    assert source.read_timeouts == []


def test_platform_source_parameters_match_legacy_timing():
    """平台差异只剩超时参数：原生 msvcrt 立即查询；POSIX 探测 10ms / 消费 5ms。"""
    native = _MsvcrtSource(_FakeMsvcrt([]))
    assert (native.escape_immediate, native.probe_timeout, native.consume_timeout) == (
        False, 0.0, 0.0)

    posix = _PosixSource(3, _FakeTermios(), _FakeTty(), _FakeSelect())
    assert (posix.escape_immediate, posix.probe_timeout, posix.consume_timeout) == (
        False, 0.01, 0.005)

    event = _ConsoleInputSource(_FakeKernel32(), ctypes)
    assert event.escape_immediate is True


# ═══════════════════════════════════════════════════════════════
# 4. 采集循环（共享骨架：三套旧轮询收敛点）
# ═══════════════════════════════════════════════════════════════


def test_poll_keys_triggers_once_and_returns():
    """触发一次即结束采集（旧实现三处 break/return 的共享语义）。"""
    fired = []
    poll_keys(_ScriptedSource([ESC_BYTE]), threading.Event(), lambda: fired.append(1))
    assert fired == [1]


def test_poll_keys_discards_plain_keys():
    """普通按键被丢弃，直到出现 Esc。"""
    fired = []
    source = _ScriptedSource([b"a", b"b", ESC_BYTE])
    poll_keys(source, threading.Event(), lambda: fired.append(1))
    assert fired == [1]


def test_poll_keys_ignores_escape_sequence_and_keeps_listening():
    """转义序列被吞掉后继续监听，随后的单次 Esc 仍可中断。"""
    fired = []
    source = _ScriptedSource([ESC_BYTE, b"[", b"A", ESC_BYTE])
    poll_keys(source, threading.Event(), lambda: fired.append(1))
    assert fired == [1]


def test_poll_keys_exits_silently_on_source_error():
    """源异常（控制台消失等）静默退出，不向上抛。"""
    fired = []
    source = _ScriptedSource([], fail_with=OSError("控制台不可用"))
    poll_keys(source, threading.Event(), lambda: fired.append(1))
    assert fired == []


def test_poll_keys_exits_when_stop_set():
    """停止信号置位后循环退出（最迟一个轮询周期）。"""
    stop = threading.Event()
    thread = threading.Thread(
        target=poll_keys, args=(_ScriptedSource([b"x"]), stop, lambda: None))
    thread.start()
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_poll_keys_swallows_handler_exception():
    """回调异常被吞掉（中断路径不允许异常打断流程）。"""
    def broken():
        raise RuntimeError("订阅链故障")

    poll_keys(_ScriptedSource([ESC_BYTE]), threading.Event(), broken)


# ═══════════════════════════════════════════════════════════════
# 5. 采集线程生命周期（Requirement: 键盘监听生命周期）
# ═══════════════════════════════════════════════════════════════


def test_listener_runs_daemon_thread_and_closes_source():
    """采集线程为守护线程；停止后字符源被关闭。"""
    source = _ScriptedSource([])
    listener = KeyListener(lambda: None, source_factory=lambda: source)
    listener.start()
    assert listener.running is True
    thread = listener._thread
    assert thread is not None and thread.daemon is True
    listener.stop()
    assert listener.running is False
    assert source.closed is True


def test_listener_switch_leaves_single_thread():
    """切换不残留：连续启动时先停旧线程，任意时刻至多一个采集线程。"""
    sources = []

    def factory():
        source = _ScriptedSource([])
        sources.append(source)
        return source

    listener = KeyListener(lambda: None, source_factory=factory)
    listener.start()
    first = listener._thread
    listener.start()
    deadline = time.monotonic() + 1.0
    while first.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    active = [t for t in threading.enumerate() if t.name == "narnat-key-listener"]
    listener.stop()
    assert first.is_alive() is False
    assert len(active) <= 1


def test_listener_stop_waits_at_most_join_timeout():
    """停止采集最多等待 0.2 秒，不同步确认线程退出（避免阻塞输入界面回归）。"""
    entered = threading.Event()

    class _SlowSource:
        escape_immediate = False
        probe_timeout = 0.0
        consume_timeout = 0.0

        def read(self, timeout):
            entered.set()
            time.sleep(0.6)
            return None

        def close(self):
            pass

    listener = KeyListener(lambda: None, source_factory=_SlowSource)
    listener.start()
    assert entered.wait(1.0)
    started = time.monotonic()
    listener.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert STOP_JOIN_TIMEOUT_SECONDS == 0.2


def test_listener_degrades_when_source_unavailable():
    """源构建失败（无控制台）→ 静默降级：不报错、无采集线程。"""
    listener = KeyListener(lambda: None, source_factory=lambda: None)
    listener.start()
    time.sleep(0.05)
    assert listener.running is False
    listener.stop()


def test_open_source_degrades_when_terminal_unavailable(monkeypatch):
    """POSIX：无法 import termios（管道/重定向输入等价情形）→ 返回 None。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "termios", None)
    assert KeyListener(lambda: None)._open_source() is None


def test_open_source_degrades_when_windows_console_unavailable(monkeypatch):
    """Windows：取得控制台失败 → 返回 None（静默降级）。"""
    def broken():
        raise OSError("无法取得控制台")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(keys_module, "_open_windows_source", broken)
    assert KeyListener(lambda: None)._open_source() is None


def test_input_mode_has_no_listener_thread():
    """输入模式不启动采集线程：不消费、不吞掉输入会话的任何按键。"""
    listener = KeyListener(lambda: None, source_factory=lambda: _ScriptedSource([]))
    bus = InterruptBus(listener=listener)
    bus.enter_run_mode()
    assert listener.running is True
    bus.enter_input_mode()
    assert listener.running is False


def test_escape_key_triggers_raise_through_default_listener():
    """默认装配链路：Esc 采集线程 → `bus.raise_()`（置标志 + 广播）。"""
    bus = InterruptBus()
    assert isinstance(bus._listener, KeyListener)
    bus._listener._source_factory = lambda: _ScriptedSource([ESC_BYTE])
    seen = []
    bus.subscribe(lambda: seen.append(1))
    bus.enter_run_mode()
    deadline = time.monotonic() + 1.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen == [1]
    assert bus.is_set is True
    bus.enter_input_mode()
    assert bus.is_set is False


# ═══════════════════════════════════════════════════════════════
# 6. 平台字符源（Windows msvcrt / ReadConsoleInput；POSIX termios）
# ═══════════════════════════════════════════════════════════════


def test_msvcrt_source_reads_immediately_when_key_available():
    fake = _FakeMsvcrt([ESC_BYTE])
    source = _MsvcrtSource(fake)
    assert source.read(POLL_INTERVAL_SECONDS) == ESC_BYTE
    assert fake.calls == ["kbhit", "getch"]  # 首次查询即命中，不等待


def test_msvcrt_source_waits_for_timeout_when_idle(monkeypatch):
    """空闲时按传入的超时上限等待（维持轮询周期，不忙等）。"""
    slept = []
    monkeypatch.setattr(keys_module.time, "sleep", lambda seconds: slept.append(seconds))
    source = _MsvcrtSource(_FakeMsvcrt([]))
    assert source.read(POLL_INTERVAL_SECONDS) is None
    assert slept == [POLL_INTERVAL_SECONDS]

    immediate = _MsvcrtSource(_FakeMsvcrt([]))
    assert immediate.read(0.0) is None  # 探测路径：超时 0 时立即查询


def test_msvcrt_source_never_flushes_input_buffer():
    """兼容怪癖：原生路径不清空输入缓冲（只调用 kbhit/getch）。"""
    msvcrt = _FakeMsvcrt([ESC_BYTE])
    _MsvcrtSource(msvcrt).read(POLL_INTERVAL_SECONDS)
    assert msvcrt.calls == ["kbhit", "getch"]


def test_console_input_source_maps_escape_key_event_to_interrupt():
    """非原生控制台：Esc 键按下事件 → 中断判定（立即语义）。"""
    kernel32 = _FakeKernel32([_key_event(0x1B)])
    source = _ConsoleInputSource(kernel32, ctypes)
    assert source.read(POLL_INTERVAL_SECONDS) == ESC_BYTE
    assert kernel32.wait_calls[0][1] == int(POLL_INTERVAL_SECONDS * 1000)


def test_console_input_source_ignores_non_escape_events():
    """非原生控制台：方向键与键抬起事件不触发中断。"""
    kernel32 = _FakeKernel32([_key_event(0x25), _key_event(0x1B, key_down=False)])
    source = _ConsoleInputSource(kernel32, ctypes)
    assert source.read(POLL_INTERVAL_SECONDS) is None
    assert source.read(POLL_INTERVAL_SECONDS) is None


def test_console_input_source_drives_poll_loop():
    """事件源走同一采集循环：Esc 事件触发回调并退出。"""
    fired = []
    kernel32 = _FakeKernel32([_key_event(0x1B)])
    poll_keys(_ConsoleInputSource(kernel32, ctypes), threading.Event(),
              lambda: fired.append(1))
    assert fired == [1]


def test_console_input_source_raises_on_read_failure():
    """ReadConsoleInputW 失败 → OSError（采集循环据此静默退出）。"""
    class _FailingKernel32(_FakeKernel32):
        def ReadConsoleInputW(self, handle, buffer, length, read_ref):
            return 0

    source = _ConsoleInputSource(_FailingKernel32([_key_event(0x1B)]), ctypes)
    with pytest.raises(OSError):
        source.read(0.0)


def test_posix_source_uses_single_char_mode_and_restores_terminal():
    """POSIX：切换单字符模式（cbreak）并在 close 时恢复原设置。"""
    termios = _FakeTermios()
    tty = _FakeTty()
    source = _PosixSource(3, termios, tty, _FakeSelect())
    assert termios.getattr_calls == [3]
    assert tty.calls == [3]
    source.close()
    assert termios.setattr_calls == [(3, _FakeTermios.TCSADRAIN, termios.settings)]


def test_posix_source_read_uses_select_timeout(monkeypatch):
    """POSIX 读取：select 等待至多 timeout 秒后读一个字节。"""
    select_module = _FakeSelect(ready=True)
    monkeypatch.setattr(keys_module, "os", _FakeOs([ESC_BYTE]))
    source = _PosixSource(3, _FakeTermios(), _FakeTty(), select_module)
    assert source.read(POLL_INTERVAL_SECONDS) == ESC_BYTE
    assert select_module.calls[0][1] == POLL_INTERVAL_SECONDS

    idle = _PosixSource(3, _FakeTermios(), _FakeTty(), _FakeSelect(ready=False))
    assert idle.read(POLL_INTERVAL_SECONDS) is None


def _patch_posix_environment(monkeypatch, modules):
    """把 POSIX 源构建所需环境（平台、termios/tty 模块、可 fileno 的 stdin）打上补丁。"""
    monkeypatch.setitem(sys.modules, "termios", modules[0])
    monkeypatch.setitem(sys.modules, "tty", modules[1])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(fileno=lambda: 3))


def test_posix_source_degrades_when_termios_setup_fails(monkeypatch):
    """无法设置终端模式（termios 取属性失败）→ 构建失败（采集器降级）。"""
    modules, (fake_termios, _) = _fake_posix_modules()

    def broken_getattr(fd):
        raise OSError("非 tty")

    fake_termios.tcgetattr = broken_getattr
    modules[0].tcgetattr = broken_getattr
    _patch_posix_environment(monkeypatch, modules)
    assert KeyListener(lambda: None)._open_source() is None


def test_open_source_degrades_when_stdin_has_no_fileno(monkeypatch):
    """标准输入不可用（无 fileno）→ 构建失败（静默降级）。"""
    modules, _ = _fake_posix_modules()
    monkeypatch.setitem(sys.modules, "termios", modules[0])
    monkeypatch.setitem(sys.modules, "tty", modules[1])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace())
    assert KeyListener(lambda: None)._open_source() is None


def test_posix_atexit_registered_once(monkeypatch):
    """进程退出兜底恢复仅注册一次。"""
    modules, (fake_termios, _) = _fake_posix_modules()
    _patch_posix_environment(monkeypatch, modules)
    fake_atexit = _FakeAtexit()
    monkeypatch.setattr(keys_module, "atexit", fake_atexit)

    listener = KeyListener(lambda: None)
    first = listener._open_posix_source()
    second = listener._open_posix_source()
    assert first is not None and second is not None
    assert len(fake_atexit.register_calls) == 1
    func, args = fake_atexit.register_calls[0]
    assert (func, args) == (keys_module._restore_term, (3, fake_termios.settings))


def test_restore_term_uses_termios_tcsetattr(monkeypatch):
    """atexit 兜底恢复实际调用 termios.tcsetattr（原动作等价）。"""
    modules, (fake_termios, _) = _fake_posix_modules()
    monkeypatch.setitem(sys.modules, "termios", modules[0])
    keys_module._restore_term(3, "settings-x")
    assert fake_termios.setattr_calls == [(3, _FakeTermios.TCSADRAIN, "settings-x")]


def test_restore_term_swallows_missing_termios(monkeypatch):
    """无 termios 环境下兜底恢复静默（进程退出路径不允许异常）。"""
    monkeypatch.setitem(sys.modules, "termios", None)
    keys_module._restore_term(3, "settings-x")


# ═══════════════════════════════════════════════════════════════
# 7. SIGINT 处理（Requirement: 中断来源与按键语义 / 兼容性怪癖保持）
# ═══════════════════════════════════════════════════════════════


def test_sigint_default_handler_in_source_mode(restore_sigint, monkeypatch):
    """源码形态：Ctrl+C 交默认信号处理（触发与 Esc 相同的中断路径）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    bus = InterruptBus(listener=_FakeListener())
    bus.enter_run_mode()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    bus.enter_input_mode()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_sigint_ignored_when_frozen(restore_sigint, monkeypatch):
    """打包形态：SIGINT 被忽略（防止杀死进程），Esc 为唯一中断手段。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    bus = InterruptBus(listener=_FakeListener())
    bus.enter_input_mode()
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    bus.enter_run_mode()
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN


def test_no_saved_sigint_dead_branch_in_sources():
    """兼容怪癖：不存在「保存并恢复原处理」路径（现状 `_saved_sigint` 恒 None 的死逻辑已删）。"""
    for path in INTERRUPT_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "saved_sigint" not in names, path.name


# ═══════════════════════════════════════════════════════════════
# 8. 结构约束（无模块级可变状态）
# ═══════════════════════════════════════════════════════════════


def test_no_mutable_module_level_state():
    """无模块级可变全局：顶层赋值名必须为全大写常量（其余状态全部在实例内）。"""
    assert INTERRUPT_SOURCES, "interrupt 积木文件缺失"
    for path in INTERRUPT_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            names = []
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for name in names:
                assert name.isupper() or name.startswith("__"), (path.name, name)


def test_package_all_resolvable():
    """聚合导出面：`__all__` 中的名全部可从积木包解析。"""
    for name in interrupt_pkg.__all__:
        assert hasattr(interrupt_pkg, name), name
