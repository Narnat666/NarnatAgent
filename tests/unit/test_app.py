"""app 积木自测（T4.3）—— spec Scenario 全覆盖 + fake 驱动主循环/headless + 装配冒烟。

覆盖清单（对齐 T4.3 任务书测试要求）：
1. `specs/app` 全部 26 个 Scenario（映射表见下）；
2. 主循环与 headless 用 fake 组件驱动（脚本化 `TurnOutcome` / `GoalDecision` /
   输入与命令三态）测流程、复位顺序、占比告警、哨兵行与清理次序；
3. 装配冒烟：真实装配成功、产物组件齐备且类型正确、headless 差异三处、
   分层检查脚本通过；
4. 结构证据：装配零 lambda、零属性后置赋值（design D8 的机械护栏）、
   盘面不出现 4 处旧后置补线的私有成员名、包级 `__init__` 轻量（不 eager import 全链）。

spec Scenario 映射表
════════════════════

| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 装配次序与依赖注入 | 依赖次序 | `test_s01_dependency_order` |
| 2 | 装配次序与依赖注入 | 模型切换同步 | `test_s02_model_switch_syncs_stats` |
| 3 | 装配次序与依赖注入 | 非调试不写日志 | `test_s03_no_log_file_without_debug` |
| 4 | headless 装配差异 | 无删除确认回调 | `test_s04_headless_no_confirm_callback` |
| 5 | headless 装配差异 | 纯文本界面 | `test_s05_headless_plain_ui` |
| 6 | headless 装配差异 | 提示词剥离 | `test_s06_headless_strips_hidden_block` |
| 7 | 启动入口与命令行契约 | 版本查询 | `test_s07_version_first` |
| 8 | 启动入口与命令行契约 | 空任务 | `test_s08_empty_prompt` |
| 9 | 启动入口与命令行契约 | 负数轮数 | `test_s09_negative_rounds` |
| 10 | 启动入口与命令行契约 | headless 输出环境 | `test_s10_headless_output_env` |
| 11 | 交互主循环 | 空输入与读取失败 | `test_s11_blank_input_or_read_failure` |
| 12 | 交互主循环 | 退出命令 | `test_s12_exit_command` |
| 13 | 交互主循环 | 未识别命令 | `test_s13_unknown_command_goes_to_model` |
| 14 | 交互主循环 | 压缩失败回到输入 | `test_s14_compress_failure_back_to_input` |
| 15 | 交互主循环 | 内循环异常 | `test_s15_turn_exception_keeps_process` |
| 16 | 交互主循环 | 轮末占比与告警 | `test_s16_ratio_refresh_and_warn` |
| 17 | 新任务状态复位 | 新任务复位 | `test_s17_task_state_reset_order` |
| 18 | headless 一次性运行 | 声明完成 | `test_s18_headless_goal_complete` |
| 19 | headless 一次性运行 | 轮数上限 | `test_s19_headless_round_limit` |
| 20 | headless 一次性运行 | 异常仍出哨兵 | `test_s20_headless_sentinel_on_failure` |
| 21 | headless 一次性运行 | 与交互模式的隔离 | `test_s21_headless_isolation` |
| 22 | 退出清理 | 退出命令清理 | `test_s22_exit_command_cleanup` |
| 23 | 退出清理 | 异常路径清理 | `test_s23_exception_path_cleanup` |
| 24 | 余额查询与费用统计触发点 | 未达间隔 | `test_s24_balance_round_counter` |
| 25 | 余额查询与费用统计触发点 | 命中间隔 | `test_s24_balance_round_counter`（真实 `StatsTracker` 间隔语义） |
| 26 | 兼容性怪癖保持 | 交互模式忽略 headless 参数 | `test_s26_interactive_ignores_headless_args` |

补充用例：`test_build_*`（装配冒烟与产物类型）、`test_structure_*`（零 lambda /
零属性赋值 / 无旧私有补线名 / 包级轻量）、`test_headless_done_reason_set`（哨兵
原因取值集）、`test_goal_continue_and_final`（目标模式续跑与强制收尾编排）。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import narnat_agent
from narnat_agent.app import headless as app_headless
from narnat_agent.app import interactive as app_interactive
from narnat_agent.app.lifecycle import cleanup_exit_command, cleanup_resources
from narnat_agent.conversation import (
    CONTINUE_TEMPLATE,
    FINAL_ROUND_TEMPLATE,
    GOAL_CONTINUE,
    GOAL_END,
    GOAL_FINAL,
    TURN_COMPLETED,
    TURN_ERROR,
    TURN_INTERRUPTED,
    GoalDecision,
    TurnOutcome,
)
from narnat_agent.ui import CommandResult

V2_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = V2_ROOT / "narnat_agent" / "app"


# ═══════════════════════════════════════════════════════════════
# fake 组件（主循环 / headless 驱动）
# ═══════════════════════════════════════════════════════════════


class LoopStop(Exception):
    """读输入队列耗尽时抛出，用于结束被驱动的主循环。"""


class ExitCalled(Exception):
    """`os._exit` 替身抛出，用于中断主循环并断言退出码。"""


class FakeLogger:
    """日志端口桩：记录调用并可查询关闭状态。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []
        self.closed = False

    def debug(self, module: str, msg: str) -> None:
        self.entries.append(("debug", module, msg))

    def info(self, module: str, msg: str) -> None:
        self.entries.append(("info", module, msg))

    def warning(self, module: str, msg: str) -> None:
        self.entries.append(("warning", module, msg))

    def error(self, module: str, msg: str) -> None:
        self.entries.append(("error", module, msg))

    def close(self) -> None:
        self.closed = True


class FakeConsole:
    """写出口桩：累积写出文本并（可选）把写出动作登记进事件序列。"""

    def __init__(self, events: list[str] | None = None) -> None:
        self.writes: list[str] = []
        self._events = events

    def write(self, text: str) -> None:
        self.writes.append(text)
        if self._events is not None:
            self._events.append("console_write")

    @property
    def text(self) -> str:
        return "".join(self.writes)


class FakeTheme:
    """着色端口桩：全部颜色求值为空串。"""

    d = ""
    e = ""
    r = ""
    x = ""


class FakeSink:
    """流会话句柄桩。"""

    def __init__(self) -> None:
        self.aborted = False
        self.abort_messages: list[str | None] = []
        self.finish_calls: list[tuple[object, bool]] = []

    def abort(self, message: str | None = None) -> None:
        self.aborted = True
        self.abort_messages.append(message)


class FakeUi:
    """交互端口桩：脚本化输入与命令三态；每次 begin_turn 产出新句柄。"""

    def __init__(self, inputs: list[str | None], results: list[CommandResult] | None = None):
        self.inputs = list(inputs)
        self.results = list(results or [])
        self.started: list[str] = []
        self.sinks: list[FakeSink] = []
        self.interrupted_calls = 0
        self.dispatched: list[tuple[str, str]] = []

    def start(self, model_name: str) -> None:
        self.started.append(model_name)

    def read_input(self) -> str | None:
        if not self.inputs:
            raise LoopStop()
        return self.inputs.pop(0)

    def dispatch_command(self, command: str, args: str) -> CommandResult:
        self.dispatched.append((command, args))
        if self.results:
            return self.results.pop(0)
        return CommandResult.UNKNOWN

    def begin_turn(self) -> FakeSink:
        sink = FakeSink()
        self.sinks.append(sink)
        return sink

    def notify_interrupted(self) -> None:
        self.interrupted_calls += 1


class FakeStats:
    """统计端口桩：记录余额查询触发点。"""

    def __init__(self) -> None:
        self.input_tokens = 100
        self.balance_calls: list[tuple[str, int]] = []

    def fetch_balance(self, api_key: str, round_num: int) -> None:
        self.balance_calls.append((api_key, round_num))


class FakeContext:
    """占比端口桩。"""

    def __init__(self) -> None:
        self.need = False
        self.warn = ""
        self.updates: list[int] = []
        self.need_calls = 0

    def need_compress(self) -> bool:
        self.need_calls += 1
        return self.need

    def update_ratio(self, input_tokens: int) -> None:
        self.updates.append(input_tokens)

    def check_warn(self) -> str:
        return self.warn


class FakeCoordinator:
    """压缩端口桩（可切换成功/失败，记录调用）。"""

    def __init__(self, events: list[str]) -> None:
        self.ok = True
        self.calls: list[str] = []
        self._events = events

    def compress(self, text: str) -> SimpleNamespace:
        self.calls.append(text)
        self._events.append("compress")
        return SimpleNamespace(ok=self.ok)


class FakeGoalTracker:
    """`GoalState` 桩（consume 可脚本化）。"""

    def __init__(self, events: list[str]) -> None:
        self.consume_result = False
        self.is_set = False
        self.reset_calls = 0
        self._events = events

    def mark(self) -> None:
        self.is_set = True

    def consume(self) -> bool:
        was = self.consume_result
        self.consume_result = False
        return was

    def reset(self) -> None:
        self.reset_calls += 1
        self.is_set = False
        self._events.append("goal_reset")


class FakeReminders:
    """`ReminderState` 桩。"""

    def __init__(self, events: list[str]) -> None:
        self.reset_calls = 0
        self._events = events

    def try_trigger_plan(self) -> bool:
        return False

    def try_trigger_bg(self) -> bool:
        return False

    def reset(self) -> None:
        self.reset_calls += 1
        self._events.append("reminders_reset")


class FakePlan:
    """`PlanTracker` 桩。"""

    def __init__(self, events: list[str]) -> None:
        self.replaced: list[list] = []
        self._events = events

    def current(self) -> list:
        return []

    def replace(self, todos: list) -> None:
        self.replaced.append(todos)
        self._events.append("plan_replace")


class FakeEnv:
    """`ToolEnv` 桩。"""

    def __init__(self, events: list[str]) -> None:
        self.goal = FakeGoalTracker(events)
        self.reminders = FakeReminders(events)
        self.plan = FakePlan(events)


class FakeStore:
    """消息历史桩。"""

    def __init__(self, events: list[str]) -> None:
        self.users: list[str] = []
        self.assistants: list[str] = []
        self._events = events

    def repair(self) -> None:
        self._events.append("repair")

    def append_user(self, content: str) -> None:
        self.users.append(content)
        self._events.append("user")

    def append_assistant(self, content: str | None, **kwargs) -> None:
        self.assistants.append(content or "")


class FakeSessionGoal:
    """会话侧目标模式桩（语义对齐 `sessions.GoalMode` 的公开面）。"""

    def __init__(self, enabled: bool = False, override: int = 0,
                 default: int = 0) -> None:
        self.enabled = enabled
        self.override_max_rounds = override
        self.default_max_rounds = default

    @property
    def max_rounds(self) -> int:
        return self.override_max_rounds or self.default_max_rounds

    def turn_on(self, override_max_rounds: int = 0) -> None:
        self.enabled = True
        self.override_max_rounds = max(0, int(override_max_rounds or 0))

    def turn_off(self) -> bool:
        was = self.enabled
        self.enabled = False
        self.override_max_rounds = 0
        return was


class FakeSessionManager:
    """会话管理器桩（只暴露主循环用到的公开 API）。"""

    def __init__(self, events: list[str]) -> None:
        self.goal = FakeSessionGoal()
        self.persisted = 0
        self.auto_saves: list[int] = []
        self.waits = 0
        self.exit_cleanups = 0
        self._name = "会话A"
        self._events = events

    def wait_auto_save(self) -> None:
        self.waits += 1

    def persist_current(self) -> None:
        self.persisted += 1

    def maybe_auto_save(self, input_tokens: int) -> None:
        self.auto_saves.append(input_tokens)

    def session_name(self) -> str | None:
        return self._name

    def exit_cleanup(self) -> str:
        self.exit_cleanups += 1
        self._events.append("session_saved")
        return f"会话已自动保存: {self._name}"


class FakeConversation:
    """内循环桩：脚本化结局（`TurnOutcome`）与可选异常。

    中断结局同时中止流句柄（对齐真实内循环的出口副作用：中断轮不落盘）。
    """

    def __init__(self, outcomes: list | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[bool, bool]] = []
        self.last_content = "残留文本"
        self._outcome_index = 0

    def run(self, sink, goal_mode: bool = False, force_final: bool = False) -> TurnOutcome:
        self.calls.append((goal_mode, force_final))
        item = self.outcomes[self._outcome_index] if self._outcome_index < len(self.outcomes) else None
        self._outcome_index += 1
        if isinstance(item, Exception):
            raise item
        outcome = item if item is not None else TurnOutcome(TURN_COMPLETED, "ok")
        if outcome.interrupted:
            sink.abort()
        return outcome


class FakeGoalRunner:
    """目标模式判定桩：脚本化 `GoalDecision`。"""

    def __init__(self, decisions: list[GoalDecision] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.rounds = 0
        self.limit = 3
        self.starts: list[tuple[str, int]] = []
        self.task = ""

    def start(self, task: str, max_rounds: int = 0) -> None:
        self.starts.append((task, max_rounds))
        self.task = task
        self.rounds = 0

    def after_round(self, outcome: TurnOutcome) -> GoalDecision:
        self.rounds += 1
        if self.decisions:
            return self.decisions.pop(0)
        return GoalDecision(GOAL_END)


class FakePool:
    """线程池桩。"""

    def __init__(self, events: list[str]) -> None:
        self.shutdowns: list[bool] = []
        self._events = events

    def shutdown(self, wait: bool = True) -> None:
        self.shutdowns.append(wait)
        self._events.append("pool_shutdown")


class FakeCleanupable:
    """带 cleanup 的组件桩（终端 / 串口）。"""

    def __init__(self, events: list[str], tag: str) -> None:
        self.calls = 0
        self._events = events
        self._tag = tag

    def cleanup(self) -> None:
        self.calls += 1
        self._events.append(self._tag)


class FakeBackground:
    """后台任务管理器桩。"""

    def __init__(self, events: list[str]) -> None:
        self.prepares = 0
        self.cleanups = 0
        self._events = events

    def prepare(self) -> None:
        self.prepares += 1
        self._events.append("bg_prepare")

    def cleanup_all(self) -> None:
        self.cleanups += 1
        self._events.append("bg_cleanup")


class FakeParts:
    """装配产物桩（主循环 / headless 的全部依赖面）。"""

    def __init__(self, inputs: list[str | None] | None = None, outcomes: list | None = None,
                 results: list[CommandResult] | None = None,
                 decisions: list[GoalDecision] | None = None) -> None:
        self.events: list[str] = []
        self.config = SimpleNamespace(ai=SimpleNamespace(model="m", api_key="k"))
        self.logger = FakeLogger()
        self.console = FakeConsole(self.events)
        self.theme = FakeTheme()
        self.registry = SimpleNamespace()
        self.llm = SimpleNamespace()
        self.mcp_manager = FakeCleanupable(self.events, "mcp_cleanup")
        self.env = FakeEnv(self.events)
        self.store = FakeStore(self.events)
        self.stats = FakeStats()
        self.context = FakeContext()
        self.coordinator = FakeCoordinator(self.events)
        self.summarizer = SimpleNamespace()
        self.session_mgr = FakeSessionManager(self.events)
        self.ui = FakeUi(list(inputs or []), results)
        self.conversation = FakeConversation(outcomes)
        self.goal_runner = FakeGoalRunner(decisions)
        self.thread_pool = FakePool(self.events)
        self.terminal = FakeCleanupable(self.events, "terminal_cleanup")
        self.serial = FakeCleanupable(self.events, "serial_cleanup")
        self.background = FakeBackground(self.events)


@pytest.fixture()
def exit_hook(monkeypatch):
    """把 `os._exit` 换成抛异常（断言退出码并中断主循环）。"""

    def _exit(code: int) -> None:
        raise ExitCalled(code)

    monkeypatch.setattr(os, "_exit", _exit)
    return _exit


# ═══════════════════════════════════════════════════════════════
# 真实装配 fixture（装配冒烟与接线证据）
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    """临时项目根（含预置 narnat.md：验证 headless 提示词剥离）。"""
    root = tmp_path_factory.mktemp("project")
    config_dir = root / ".narnat" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "narnat.md").write_text(
        "保留的用户指令\n"
        "<!-- subagent:hide -->\n隐藏的子代理指令\n<!-- /subagent:hide -->\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def safe_prompt_session():
    """把 ptk 会话构造替换为替身（pytest 下无真实控制台，控件构造会抛异常）。

    仅替换 `ui.prompt` 模块绑定的构造器——输入会话的交互行为归 ui 积木自测，
    本文件只验证装配与主循环编排。
    """
    prompt_module = sys.modules["narnat_agent.ui.prompt"]

    class DummySession:
        def prompt(self, *args, **kwargs):
            raise EOFError()

    original = prompt_module.PromptSession
    prompt_module.PromptSession = lambda *args, **kwargs: DummySession()
    yield
    prompt_module.PromptSession = original


@pytest.fixture(scope="module")
def parts(project, safe_prompt_session) -> object:
    """交互模式装配产物（真实装配，module 级复用）。"""
    from narnat_agent.app import build

    return build(str(project), debug=False, headless=False)


@pytest.fixture(scope="module")
def headless_parts(project) -> object:
    """headless 装配产物（默认静默工具日志）。"""
    from narnat_agent.app import build

    return build(str(project), debug=False, headless=True)


# ═══════════════════════════════════════════════════════════════
# 装配冒烟（结构检查含分层脚本）
# ═══════════════════════════════════════════════════════════════


def test_build_returns_all_components(parts) -> None:
    """装配产物组件齐备且类型正确（spec「装配次序与依赖注入」第 23 步）。"""
    from narnat_agent import compression, conversation, llm, mcp, messages, output, sessions, stats, tools, ui
    from narnat_agent.app import AgentLogger, Summarizer
    from narnat_agent.tools import shell as tools_shell
    from narnat_agent.tools import remote as tools_remote

    assert isinstance(parts.config.ai.model, str)
    assert isinstance(parts.logger, AgentLogger)
    assert isinstance(parts.console, output.Console)
    assert isinstance(parts.theme, output.Theme)
    assert isinstance(parts.display, output.DisplayState)
    assert isinstance(parts.registry, tools.ToolRegistry)
    assert isinstance(parts.llm, llm.LLMClient)
    assert isinstance(parts.mcp_manager, mcp.McpManager)
    assert isinstance(parts.env, tools.ToolEnvImpl)
    assert isinstance(parts.store, messages.MessageStore)
    assert isinstance(parts.stats, stats.StatsTracker)
    assert isinstance(parts.context, compression.CompressionContext)
    assert isinstance(parts.coordinator, compression.CompressionCoordinator)
    assert isinstance(parts.summarizer, Summarizer)
    assert isinstance(parts.session_mgr, sessions.SessionManager)
    assert isinstance(parts.ui, ui.UiInteraction)
    assert isinstance(parts.dispatcher, conversation.ToolDispatcher)
    assert isinstance(parts.conversation, conversation.ConversationLoop)
    assert isinstance(parts.goal_runner, conversation.GoalMode)
    assert isinstance(parts.terminal, tools_remote.TerminalTool)
    assert isinstance(parts.serial, tools_remote.SerialTool)
    assert isinstance(parts.background, tools_shell.BackgroundManager)
    # 工具定义快照已注入 LLM（内置定义不含隐藏的 GoalComplete）
    names = [d["function"]["name"] for d in parts.llm._runtime.tool_defs]
    assert "Read" in names and "Shell" in names
    assert "GoalComplete" not in names


def test_build_registry_tool_order(parts) -> None:
    """工具注册顺序即定义列表顺序（文件族在最前、GoalComplete 执行能力始终注册）。"""
    names = parts.registry.get_tool_names()
    assert names[:5] == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert names[5] == "Shell"
    assert "GoalComplete" in names


def test_build_interrupt_subscriptions(parts) -> None:
    """中断总线订阅齐备 —— LLM 活动连接、前台命令、远程与串口会话。"""
    handlers = parts.interrupt._handlers
    assert parts.llm.abort_active_request in handlers
    assert parts.background._runtime.interrupt in handlers
    assert parts.terminal.kill_active_exec in handlers
    assert parts.serial.kill_active_exec in handlers


def test_build_remote_diff_colors_follow_theme(parts) -> None:
    """远程 diff 着色随主题与纯文本模式（装配注入主题取色，非固定 ANSI）。"""
    from narnat_agent.app import DiffColorizer

    assert isinstance(parts.remote_files._colorize, DiffColorizer)
    diff = "--- a/x.py\n+++ b/x.py\n+新增\n-删除\n"
    colored = parts.remote_files._colorize(diff)
    assert "\x1b[" in colored
    parts.console.set_plain(True)
    try:
        assert parts.remote_files._colorize(diff) == diff
    finally:
        parts.console.set_plain(False)


def test_build_file_tools_take_theme(parts) -> None:
    """本地文件族 diff 取色同一主题（Edit/Write 注入 DiffColors）。"""
    edit = parts.registry._tools["Edit"]
    assert edit._colors is parts.theme


def test_layering_check_passes() -> None:
    """装配冒烟：分层检查脚本全包通过（验收标准 2）。"""
    script = V2_ROOT / "tests" / "check_layering.py"
    spec = importlib.util.spec_from_file_location("check_layering", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.main() == 0


# ═══════════════════════════════════════════════════════════════
# 结构证据（design D8 机械护栏）
# ═══════════════════════════════════════════════════════════════


def test_structure_no_lambda_in_assembly() -> None:
    """装配产物不含散落 lambda（任务书实施要求 1）。"""
    tree = ast.parse((PACKAGE_DIR / "assembly.py").read_text(encoding="utf-8"))
    lambdas = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
    assert lambdas == []


def test_structure_no_attribute_assignment_in_build() -> None:
    """禁止后置裸赋值：装配流程内不存在任何 `obj.attr = …` 语句。"""
    tree = ast.parse((PACKAGE_DIR / "assembly.py").read_text(encoding="utf-8"))
    build_fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build"
    )
    assignments = [
        node.lineno
        for node in ast.walk(build_fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    ]
    assert assignments == []


def test_structure_no_legacy_private_patching() -> None:
    """代码面不出现现状 4 处后置补线的旧私有成员名（注释与文档不计）。"""
    banned = {
        "set_tool_sinks", "summary_anim_start", "summary_anim_stop", "_set_model",
        "_set_goal_tool", "_executor", "_goal_enabled", "_goal_max_rounds",
        "_last_round_ok", "_last_content_parts", "_delete_confirmed",
    }
    used: set[str] = set()
    for path in PACKAGE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.arg):
                used.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                used.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                used.add(node.name)
    assert not (banned & used), banned & used


def test_structure_package_init_is_lightweight() -> None:
    """包级 `__init__` 轻量：import 包不加载任何积木，版本查询不触发网络。"""
    code = (
        "import sys, narnat_agent;"
        "assert 'narnat_agent.llm' not in sys.modules;"
        "assert 'narnat_agent.app' not in sys.modules;"
        "print(narnat_agent.__version__)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(V2_ROOT),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == narnat_agent.__version__


# ═══════════════════════════════════════════════════════════════
# Scenario 1-6：装配次序、headless 差异
# ═══════════════════════════════════════════════════════════════


def test_s01_dependency_order(parts) -> None:
    """依赖次序 —— 三项"依赖就绪即接线"以构造注入完成（无后置补线）。"""
    from narnat_agent.app import DeleteConfirmer, InterruptProbe
    from narnat_agent.sessions import ModelState
    from narnat_agent.ui import UiAnimator

    # 界面就绪后即可用的合并动画端口：会话管理器持同一实例（构造注入）
    assert isinstance(parts.session_mgr._animator, UiAnimator)
    # 压缩协调器就绪后即可用的手动压缩：会话管理器指向协调器绑定方法（构造注入）
    assert parts.session_mgr._compact_func == parts.coordinator.compress_manual
    # 统计就绪后的模型切换补线：ModelState 的同步回调即 stats.set_model（构造注入）
    assert isinstance(parts.session_mgr.model_state, ModelState)
    assert parts.session_mgr.model_state._on_model_change == parts.stats.set_model
    # 取消检查与确认回调为有类型小对象（非散落 lambda）
    assert isinstance(parts.session_mgr._cancel_check, InterruptProbe)
    if sys.platform == "win32":
        assert isinstance(parts.env.confirm, DeleteConfirmer)
    else:
        assert parts.env.confirm is None


def test_s02_model_switch_syncs_stats(parts) -> None:
    """模型切换同步 —— 配置模型名与统计组件使用的模型名同时更新。"""
    original = parts.config.ai.model
    options = parts.config.ai.model_options
    target = options[-1] if options else "测试模型"
    try:
        assert parts.session_mgr.switch_model(target) == f"设置成功：{target}"
        assert parts.config.ai.model == target
        assert parts.stats._model == target
    finally:
        parts.session_mgr.model_state._ai.model = original
        parts.stats.set_model(original)


def test_s03_no_log_file_without_debug(project, safe_prompt_session) -> None:
    """非调试不写日志 —— 不启动文件日志，日志调用静默丢弃。"""
    from narnat_agent.app import build

    parts = build(str(project), debug=False, headless=False)
    logs_dir = Path(parts.config.paths.logs_dir)
    before = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
    parts.logger.info("test", "不应落盘")
    after = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
    assert before == after


def test_s04_headless_no_confirm_callback(headless_parts) -> None:
    """无删除确认回调 —— 工具上下文不持有确认回调。"""
    assert headless_parts.env.confirm is None


def test_s05_headless_plain_ui(headless_parts, capsys) -> None:
    """纯文本界面 —— 界面为纯文本流实现，输出无颜色且统计栏调用被忽略。"""
    from narnat_agent.contracts import TurnStats
    from narnat_agent.ui import HeadlessInteraction, HeadlessSink

    assert isinstance(headless_parts.ui, HeadlessInteraction)
    assert headless_parts.console.is_plain() is True
    sink = headless_parts.ui.begin_turn()
    assert isinstance(sink, HeadlessSink)
    sink.feed("## 标题\n")
    sink.finish(TurnStats(input_tokens=123, output_tokens=456, cost=1.5, balance=9.9))
    written = capsys.readouterr().out
    assert "标题" in written
    assert "输入:" not in written and "费用" not in written and "\x1b[" not in written


def test_s06_headless_strips_hidden_block(project, safe_prompt_session) -> None:
    """提示词剥离 —— headless 加载配置时剔除 subagent:hide 配对区块。"""
    from narnat_agent.app import build

    headless = build(str(project), debug=False, headless=True)
    interactive = build(str(project), debug=False, headless=False)
    assert "隐藏的子代理指令" not in headless.config.system_prompt
    assert "保留的用户指令" in headless.config.system_prompt
    assert "隐藏的子代理指令" in interactive.config.system_prompt


# ═══════════════════════════════════════════════════════════════
# Scenario 7-10：命令行契约
# ═══════════════════════════════════════════════════════════════


def test_s07_version_first(capsys) -> None:
    """版本查询 —— `-v` 打印版本后立即返回，不执行任何其他分支。"""
    assert narnat_agent.run_cli(["-v", "-p", ""]) == 0
    assert capsys.readouterr().out.strip() == f"narnat {narnat_agent.__version__}"


def test_s08_empty_prompt(capsys) -> None:
    """空任务 —— `-p` 为纯空白时打印错误并以退出码 1 终止。"""
    assert narnat_agent.run_cli(["-p", "   "]) == 1
    assert "错误: -p 任务内容不能为空" in capsys.readouterr().out


def test_s09_negative_rounds(capsys) -> None:
    """负数轮数 —— `-g` 为负时打印错误并以退出码 1 终止。"""
    assert narnat_agent.run_cli(["-p", "任务", "-g", "-2"]) == 1
    assert "错误: -g 轮数上限必须为正整数" in capsys.readouterr().out


def test_s10_headless_output_env(project) -> None:
    """headless 输出环境 —— 去色与工具日志静默；`-l` 恢复全量工具日志。"""
    from narnat_agent.app import build

    quiet = build(str(project), debug=False, headless=True, tool_log=False)
    verbose = build(str(project), debug=False, headless=True, tool_log=True)
    assert quiet.console.is_plain() and quiet.console.is_quiet_tools()
    assert verbose.console.is_plain() and not verbose.console.is_quiet_tools()


def test_s26_interactive_ignores_headless_args(monkeypatch) -> None:
    """兼容怪癖：交互模式启动时带 `-l` 或 `-g` 被静默忽略、不产生提示。"""
    captured: dict = {}

    class FakeApp:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr("narnat_agent.app.App", FakeApp)
    assert narnat_agent.run_cli(["-l", "-g", "5"]) == 0
    assert captured["kwargs"] == {"debug": False}
    assert captured["ran"] is True


# ═══════════════════════════════════════════════════════════════
# Scenario 11-17：交互主循环
# ═══════════════════════════════════════════════════════════════


def test_s11_blank_input_or_read_failure() -> None:
    """空输入与读取失败 —— 不退出、不调度，直接回到输入。"""
    parts = FakeParts(inputs=[None, "   ", "\n"])
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.conversation.calls == []
    assert parts.ui.dispatched == []
    assert parts.store.users == []


def test_s12_exit_command(exit_hook) -> None:
    """退出命令 —— 会话落盘、退出日志、MCP 清理与日志关闭在进程终止前完成。

    注：`os._exit` 被替身替换为抛异常后，`run_interactive` 的 finally 会再跑一次
    常规清理（真实路径下 `os._exit` 不返回、finally 不执行）——故此处只断言
    「退出命令路径的四步清理在进程终止之前全部完成且顺序固定」。
    """
    parts = FakeParts(inputs=["/exit"], results=[CommandResult.EXIT])
    with pytest.raises(ExitCalled) as excinfo:
        app_interactive.run_interactive(parts)
    assert excinfo.value.args[0] == 0
    assert parts.session_mgr.exit_cleanups == 1
    assert parts.mcp_manager.calls >= 1
    assert parts.logger.closed is True
    assert parts.events.index("session_saved") < parts.events.index("mcp_cleanup")
    assert "会话已自动保存: 会话A" in parts.console.text
    assert any(entry[1] == "app" and "退出" in entry[2] for entry in parts.logger.entries)


def test_s13_unknown_command_goes_to_model() -> None:
    """未识别命令 —— 该输入按普通对话内容发送给 AI（不打印错误）。"""
    parts = FakeParts(inputs=["/unknown_cmd hello", "/exit_loop"],
                      results=[CommandResult.UNKNOWN, CommandResult.UNKNOWN])
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.conversation.calls == [(False, False), (False, False)]
    assert parts.store.users == ["/unknown_cmd hello", "/exit_loop"]


def test_s14_compress_failure_back_to_input() -> None:
    """压缩失败回到输入 —— 本轮不进入对话轮次。"""
    parts = FakeParts(inputs=["长输入", "再试一次"])
    parts.context.need = True
    parts.coordinator.ok = False
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.conversation.calls == []
    assert parts.coordinator.calls == ["长输入", "再试一次"]
    # 两次压缩都失败 → 用户输入不进入历史（压缩流程负责写入，失败时历史不变）
    assert parts.store.users == []
    # 压缩失败路径下余额查询与状态复位照常发生
    assert parts.stats.balance_calls == [("k", 1), ("k", 2)]
    assert parts.env.goal.reset_calls == 2


def test_s15_turn_exception_keeps_process() -> None:
    """内循环异常 —— 已产出文本写入历史、流以异常提示结束，进程不退出。"""
    parts = FakeParts(inputs=["任务"], outcomes=[RuntimeError("boom"), TurnOutcome(TURN_COMPLETED)])
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.conversation.calls == [(False, False)]
    # 异常轮之后循环继续（第二次读输入才触发 LoopStop）
    assert parts.ui.sinks[0].aborted is True
    assert "程序异常" in (parts.ui.sinks[0].abort_messages[0] or "")
    assert parts.store.assistants == ["残留文本"]
    assert parts.stats.balance_calls == [("k", 1)]


def test_s16_ratio_refresh_and_warn() -> None:
    """轮末占比与告警 —— 用最新输入用量刷新占比；达阈值输出一次性告警。"""
    parts = FakeParts(inputs=["任务", "第二轮"])
    parts.context.warn = "窗口占比已达80%，建议开启新对话"
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.context.updates == [parts.stats.input_tokens] * 2
    assert parts.console.text.count("窗口占比已达80%") == 2


def test_s17_task_state_reset_order() -> None:
    """新任务复位 —— 标记与计划在压缩检查与消息追加之前复位。"""
    parts = FakeParts(inputs=["任务"])
    parts.context.need = True
    parts.coordinator.ok = True
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    events = parts.events
    assert events.index("goal_reset") < events.index("compress")
    assert events.index("reminders_reset") < events.index("compress")
    assert events.index("plan_replace") < events.index("compress")
    assert parts.env.plan.replaced == [[]]


def test_goal_mode_continue_and_final() -> None:
    """目标模式编排 —— 续跑注入、收尾轮 force_final 与轮末保存。"""
    decisions = [
        GoalDecision(GOAL_CONTINUE, CONTINUE_TEMPLATE.format(rounds=1, task="任务")),
        GoalDecision(GOAL_FINAL, FINAL_ROUND_TEMPLATE.format(limit=3)),
    ]
    parts = FakeParts(
        inputs=["任务"],
        outcomes=[TurnOutcome(TURN_COMPLETED, "a"), TurnOutcome(TURN_COMPLETED, "b"),
                  TurnOutcome(TURN_COMPLETED, "c")],
        decisions=decisions,
    )
    parts.session_mgr.goal = FakeSessionGoal(enabled=True, override=2)
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.goal_runner.starts == [("任务", 2)]
    assert parts.conversation.calls == [(True, False), (True, False), (True, True)]
    assert parts.store.users[0] == "任务"
    assert CONTINUE_TEMPLATE.format(rounds=1, task="任务") in parts.store.users
    assert FINAL_ROUND_TEMPLATE.format(limit=3) in parts.store.users
    # 每轮正常结束都落盘并尝试自动保存（含强制收尾轮）
    assert parts.session_mgr.persisted == 3
    assert parts.session_mgr.auto_saves == [parts.stats.input_tokens] * 3


def test_turn_interrupted_not_saved() -> None:
    """中断轮不落盘（占比沿用上一轮值，不追加 Assistant 历史）。"""
    parts = FakeParts(inputs=["任务"], outcomes=[TurnOutcome(TURN_INTERRUPTED, "半截")])
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.session_mgr.persisted == 0
    assert parts.store.assistants == []


# ═══════════════════════════════════════════════════════════════
# Scenario 18-21：headless 一次性运行
# ═══════════════════════════════════════════════════════════════


def test_headless_done_reason_set() -> None:
    """哨兵原因取值集与哨兵行文本（已发布契约）。"""
    assert app_headless.DONE_REASONS == (
        "goal_complete", "round_limit", "round_failed", "aborted",
        "compress_failed", "unknown",
    )
    assert app_headless.done_line("goal_complete", 2) == \
        "\n[NN_DONE] reason=goal_complete rounds=2\n"


def test_s18_headless_goal_complete() -> None:
    """声明完成 —— 完成标记复位，退出原因 goal_complete，输出哨兵行。"""
    parts = FakeParts(inputs=[], outcomes=[TurnOutcome(TURN_COMPLETED, "done")])
    parts.env.goal.consume_result = True
    app_headless.run_headless(parts, " 任务 ", max_rounds=5)
    assert parts.store.users[0] == "任务"
    assert parts.conversation.calls == [(True, False)]
    assert "[NN_DONE] reason=goal_complete rounds=1" in parts.console.text
    assert parts.env.goal.consume_result is False


def test_s19_headless_round_limit() -> None:
    """轮数上限 —— 注入收尾指令并执行强制收尾轮，退出原因 round_limit。"""
    parts = FakeParts(inputs=[], outcomes=[TurnOutcome(TURN_COMPLETED, "a"),
                                          TurnOutcome(TURN_COMPLETED, "b")])
    app_headless.run_headless(parts, "任务", max_rounds=1)
    assert parts.conversation.calls == [(True, False), (True, True)]
    assert FINAL_ROUND_TEMPLATE.format(limit=1) in parts.store.users
    assert "[NN_DONE] reason=round_limit rounds=1" in parts.console.text


def test_s19b_headless_round_limit_default_config() -> None:
    """轮数上限取配置默认值（`-g` 缺省时）。"""
    parts = FakeParts(inputs=[], outcomes=[TurnOutcome(TURN_COMPLETED, "a")])
    parts.session_mgr.goal = FakeSessionGoal(enabled=False)
    app_headless.run_headless(parts, "任务", max_rounds=0)
    # 会话侧 target 桩默认 0 → 生效上限回落到会话目标状态默认值（0 轮即首轮收尾）
    assert "[NN_DONE] reason=round_limit rounds=1" in parts.console.text


def test_s20_headless_sentinel_on_failure() -> None:
    """异常仍出哨兵 —— 内循环异常 → aborted；压缩失败 → compress_failed。"""
    exc_parts = FakeParts(inputs=[], outcomes=[RuntimeError("boom")])
    app_headless.run_headless(exc_parts, "任务", max_rounds=3)
    assert "[NN_DONE] reason=aborted rounds=0" in exc_parts.console.text

    fail_parts = FakeParts(inputs=[], outcomes=[TurnOutcome(TURN_COMPLETED)])
    fail_parts.context.need = True
    fail_parts.coordinator.ok = False
    app_headless.run_headless(fail_parts, "任务", max_rounds=3)
    assert "[NN_DONE] reason=compress_failed rounds=0" in fail_parts.console.text

    failed_parts = FakeParts(inputs=[], outcomes=[TurnOutcome(TURN_ERROR)])
    app_headless.run_headless(failed_parts, "任务", max_rounds=3)
    assert "[NN_DONE] reason=round_failed rounds=1" in failed_parts.console.text


def test_s21_headless_isolation() -> None:
    """与交互模式的隔离 —— 不读输入、不自动保存、不查余额、不显示统计栏。"""
    parts = FakeParts(inputs=["不应被读取"], outcomes=[TurnOutcome(TURN_COMPLETED)])
    parts.env.goal.consume_result = True
    app_headless.run_headless(parts, "任务", max_rounds=2)
    assert parts.ui.dispatched == []
    assert parts.session_mgr.auto_saves == []
    assert parts.session_mgr.persisted == 0
    assert parts.session_mgr.waits == 0
    assert parts.stats.balance_calls == []
    # 句柄为纯文本替身：收尾不显示统计栏由 HeadlessSink 语义保证（见 S5）
    assert parts.conversation.calls == [(True, False)]


# ═══════════════════════════════════════════════════════════════
# Scenario 22-23：退出清理
# ═══════════════════════════════════════════════════════════════


def test_s22_exit_command_cleanup() -> None:
    """退出命令清理 —— 四步清理全部在进程终止前完成（且顺序固定）。"""
    parts = FakeParts(inputs=[])
    cleanup_exit_command(parts)
    assert parts.events.index("session_saved") < parts.events.index("mcp_cleanup")
    assert parts.logger.closed is True


def test_s23_exception_path_cleanup() -> None:
    """异常路径清理 —— 线程池、终端/串口/后台与 MCP 照常清理。"""
    parts = FakeParts(inputs=[])
    parts.ui = FakeUi(inputs=[])
    parts.ui.read_input = lambda: (_ for _ in ()).throw(RuntimeError("读输入失败"))
    with pytest.raises(RuntimeError):
        app_interactive.run_interactive(parts)
    assert parts.thread_pool.shutdowns == [False]
    assert parts.terminal.calls == 1
    assert parts.serial.calls == 1
    assert parts.background.cleanups == 1
    assert parts.mcp_manager.calls == 1


def test_s23b_headless_sentinel_before_cleanup() -> None:
    """headless 异常路径：哨兵行先于清理输出，且日志被关闭。"""
    parts = FakeParts(inputs=[], outcomes=[RuntimeError("boom")])
    app_headless.run_headless(parts, "任务", max_rounds=1)
    assert "[NN_DONE]" in parts.console.text
    assert parts.events.index("console_write") < parts.events.index("pool_shutdown")
    assert parts.thread_pool.shutdowns == [False]
    assert parts.background.cleanups == 1
    assert parts.logger.closed is True


# ═══════════════════════════════════════════════════════════════
# Scenario 24-25：余额查询触发点
# ═══════════════════════════════════════════════════════════════


def test_s24_balance_round_counter() -> None:
    """触发点 —— 每次进入调度轮次计数加一，密钥与轮次计数交统计组件。"""
    parts = FakeParts(inputs=["一", "二", "三"])
    with pytest.raises(LoopStop):
        app_interactive.run_interactive(parts)
    assert parts.stats.balance_calls == [("k", 1), ("k", 2), ("k", 3)]


def test_s25_balance_interval_semantics() -> None:
    """间隔语义（未达间隔不查询 / 命中间隔查询）由统计组件承担（真实实现验证）。"""
    from narnat_agent.stats import StatsTracker

    tracker = StatsTracker("m", {}, SimpleNamespace(enabled=False, url="", auth_method="bearer",
                                                    value_path="", currency_path=""))
    tracker.fetch_balance("key", 3)
    assert tracker.balance == 0.0
    tracker.fetch_balance("", 10)
    assert tracker.balance == 0.0
    tracker.fetch_balance("key", 10)  # 命中轮次但余额配置关闭 → 查询失败保持上次值
    assert tracker.balance == 0.0


def test_cleanup_resources_close_log_flag() -> None:
    """清理函数按调用方语义决定是否关闭日志（headless 关、交互不关）。"""
    interactive = FakeParts(inputs=[])
    cleanup_resources(interactive)
    assert interactive.logger.closed is False

    headless = FakeParts(inputs=[])
    cleanup_resources(headless, close_log=True)
    assert headless.logger.closed is True
