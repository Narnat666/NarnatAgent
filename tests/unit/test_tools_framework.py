"""tools 框架层自测 —— T3.3 交付物（signal / registry / env / token_estimate）。

覆盖（对齐 T3.3 任务书测试要求）：
1. signal 全函数与基准 `baseline/data/exec_signal.json` 逐用例对照（标签模式化），
   外加标签进程级随机/稳定、伪造不生效、切点吸附不变量；
2. registry：注册/注销/重名/幂等、GoalComplete 定义过滤、执行入口错误文案对照
   `baseline/data/param_utils.json`（`registry._friendly_type_error` 与
   `registry.execute(错误路径)` 两组）、结果归一、标签先判定后剥离、全局截断；
3. Trackers 与 ToolSettings：零参构造默认值、实例隔离、引用语义（就地修正可见）、
   消费即复位、触发一次、挂起取出即清空；
4. token_estimate 与 `baseline/data/token_estimate.json` 两组用例对照；
5. 契约实现完整性（contracts 的 tool 协议族均有实现）与依赖纯净（tools 只向下引用
   contracts / messages）。
"""
from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path

import pytest

from narnat_agent.contracts.tool import (
    AWAIT_CONFIRM,
    DeleteGate,
    GoalState,
    McpPort,
    PlanTracker,
    ReminderState,
    Tool,
    ToolEnv,
    ToolResult,
    ToolSettings,
)
from narnat_agent.tools import signal, token_estimate
from narnat_agent.tools.env import (
    DeleteGateImpl,
    GoalStateImpl,
    PlanTrackerImpl,
    ReminderStateImpl,
    ToolEnvImpl,
    ToolSettingsImpl,
)
from narnat_agent.tools.registry import (
    ToolRegistry,
    friendly_type_error,
    normalize_result,
    valid_param_names,
)
from tests.baseline import cases

TESTS_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "tools"

# 基准文件把进程级随机标签模式化为 [<TAG>]（与 extract_old.py 同一约定）
MASK_TAG_RE = re.compile(r"\[[0-9a-f]{8}\]")
TAG_PLACEHOLDER = "[<TAG>]"


def _mask_tags(text: str) -> str:
    return MASK_TAG_RE.sub(TAG_PLACEHOLDER, text)


@lru_cache(maxsize=None)
def _baseline_groups(filename: str) -> dict[str, list[dict]]:
    payload = json.loads((TESTS_DIR / "baseline" / "data" / filename).read_text(encoding="utf-8"))
    return payload["groups"]


def _expected(filename: str, group: str, case_id: str) -> object:
    by_id = {case["id"]: case for case in _baseline_groups(filename)[group]}
    return by_id[case_id]["result"]


def _build_parts(parts: list) -> str:
    """按 cases.py 的 parts 协议拼装文本（含框架随机标签）。"""
    chunks: list[str] = []
    for kind, value in parts:
        if kind == "lit":
            chunks.append(value)
        elif kind == "rc":
            chunks.append(signal.rc_line(value))
        elif kind == "err_line":
            chunks.append(signal.error_line(value))
        elif kind == "tag_error":
            chunks.append(signal.tag_error(value))
        else:
            raise ValueError(f"未知 parts 类型: {kind}")
    return "".join(chunks)


# ═══════════════════════════════════════════════════════════════
# 1. signal —— 与基准逐用例对照（契约来源：specs/tools-shell）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_RC_LINE_CASES, ids=lambda c: c["id"])
def test_signal_rc_line_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.rc_line", case["id"])
    text = signal.rc_line(case["rc"])
    assert _mask_tags(text) == expected["text"]
    assert bool(MASK_TAG_RE.search(text)) is expected["has_tag"]
    assert len(MASK_TAG_RE.findall(text)) == expected["tag_count"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_ERROR_LINE_CASES, ids=lambda c: c["id"])
def test_signal_error_line_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.error_line", case["id"])
    text = signal.error_line(case["msg"])
    assert _mask_tags(text) == expected["text"]
    assert bool(MASK_TAG_RE.search(text)) is expected["has_tag"]
    assert len(MASK_TAG_RE.findall(text)) == expected["tag_count"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_TAG_ERROR_CASES, ids=lambda c: c["id"])
def test_signal_tag_error_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.tag_error", case["id"])
    text = signal.tag_error(case["text"])
    assert _mask_tags(text) == expected["text"]
    assert bool(MASK_TAG_RE.search(text)) is expected["has_tag"]
    assert len(MASK_TAG_RE.findall(text)) == expected["tag_count"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_HAS_ERROR_CASES, ids=lambda c: c["id"])
def test_signal_has_error_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.has_error", case["id"])
    text = _build_parts(case["parts"])
    assert signal.has_error(text) is expected["value"]
    assert _mask_tags(text) == expected["text"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_PARSE_RC_CASES, ids=lambda c: c["id"])
def test_signal_parse_rc_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.parse_rc", case["id"])
    text = _build_parts(case["parts"])
    assert signal.parse_rc(text) == expected["value"]
    assert _mask_tags(text) == expected["text"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_STRIP_TAGS_CASES, ids=lambda c: c["id"])
def test_signal_strip_tags_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.strip_tags", case["id"])
    text = _build_parts(case["parts"])
    assert signal.strip_tags(text) == expected["value"]
    assert _mask_tags(text) == expected["text"]


@pytest.mark.parametrize("case", cases.EXEC_SIGNAL_CUT_POINT_CASES, ids=lambda c: c["id"])
def test_signal_safe_cut_points_matches_baseline(case):
    expected = _expected("exec_signal.json", "exec_signal.safe_cut_points", case["id"])
    text = _build_parts(case["parts"])
    cut = list(signal.safe_cut_points(text, case["head"], case["tail"]))
    assert cut == expected["cut"]
    assert _mask_tags(text) == expected["text"]


def test_signal_tags_are_process_random_and_stable():
    """标签随机生成、进程内不变（同一函数多次调用标签相同）。"""
    assert re.fullmatch(r"[0-9a-f]{8}", signal.TAG)
    assert re.fullmatch(r"[0-9a-f]{8}", signal.ERR_TAG)
    assert signal.rc_line(0) == signal.rc_line(0)
    assert signal.rc_line(123) == f"[exit code: 123] [{signal.TAG}]"
    assert signal.error_line("m") == f"[错误: m] [{signal.ERR_TAG}]"
    assert signal.tag_error("m") == f"m [{signal.ERR_TAG}]"


def test_signal_fake_text_never_triggers():
    """命令输出打印同款文字（无随机标签）不触发失败判定与退出码解析。"""
    assert signal.has_error("[错误: 假消息]") is False
    assert signal.has_error("[exit code: 0]") is False
    assert signal.parse_rc("[exit code: 1]") is None


def test_signal_strip_removes_only_framework_tags():
    combined = signal.rc_line(0) + signal.error_line("boom")
    assert signal.strip_tags(combined) == "[exit code: 0][错误: boom]"
    plain = "命令输出 [错误: 假消息] 与 [exit code: 3] 原样保留"
    assert signal.strip_tags(plain) == plain


def test_signal_safe_cut_points_never_split_tag():
    """切点吸附不变量：任意 head/tail 组合下两个切点都不落在标签内部。"""
    text = "HEAD" + signal.rc_line(1) + signal.error_line("boom") + "TAIL"
    spans = [m.span() for m in MASK_TAG_RE.finditer(text)]
    assert len(spans) == 2
    for head in range(len(text) + 1):
        for tail in range(len(text) + 1):
            cut_head, cut_tail = signal.safe_cut_points(text, head, tail)
            assert cut_head <= cut_tail
            for start, end in spans:
                assert not start < cut_head < end
                assert not start < cut_tail < end


# ═══════════════════════════════════════════════════════════════
# 2. registry —— 测试替身与基准对照
# ═══════════════════════════════════════════════════════════════

READ_PARAMS = ["file_path", "offset", "limit", "device"]
EDIT_PARAMS = ["file_path", "old_string", "new_string", "replace_all", "device"]


def _read_impl(file_path: str, offset: int = 0, limit: int = 2000, device: str = "") -> str:
    return f"读取 {file_path}"


def _edit_impl(file_path: str, old_string: str = "", new_string: str = "",
               replace_all: bool = False, device: str = "") -> tuple:
    return ("[已替换1处]", "@@ diff")


class _FuncTool:
    """测试用工具：定义声明参数面，execute 以关键字展开调用具名实现。

    复刻现状参数错误形态：未知参数名/缺必填参数由具名实现抛 TypeError，
    由注册表执行入口包装为中文提示（与旧 `impl(**arguments)` 同源）。
    """

    def __init__(self, name: str, params: Sequence[str], impl: Callable[..., object]) -> None:
        self._name = name
        self._params = list(params)
        self._impl = impl

    @property
    def name(self) -> str:
        return self._name

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": f"{self._name} 测试工具",
                "parameters": {
                    "type": "object",
                    "properties": {param: {} for param in self._params},
                    "required": [],
                },
            },
        }

    def execute(self, args: dict, env) -> object:
        return self._impl(**args)


class _EnvTool:
    """测试用工具：记录收到的 args 与 env（验证注入协议）。"""

    def __init__(self, name: str, result: object = "ok") -> None:
        self._name = name
        self._result = result
        self.calls: list[tuple[dict, object]] = []

    @property
    def name(self) -> str:
        return self._name

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self._name, "description": "", "parameters": {}},
        }

    def execute(self, args: dict, env) -> object:
        self.calls.append((args, env))
        return self._result


@pytest.mark.parametrize("case", cases.PARAM_UTILS_FRIENDLY_ERROR_CASES, ids=lambda c: c["id"])
def test_friendly_type_error_matches_baseline(case):
    """`registry._friendly_type_error` 基准对照（有效参数列表来自工具定义）。"""
    expected = _expected("param_utils.json", "registry._friendly_type_error", case["id"])
    valid_params = {"Edit": EDIT_PARAMS, "Read": READ_PARAMS, "NoSuchTool": []}
    got = friendly_type_error(case["tool"], valid_params[case["tool"]], TypeError(case["err"]))
    assert got == expected


@pytest.mark.parametrize("case", cases.PARAM_UTILS_EXECUTE_ERROR_CASES, ids=lambda c: c["id"])
def test_registry_execute_error_paths_match_baseline(case):
    """执行入口错误路径基准对照（未知工具 / 未知参数 / 缺必填参数）。

    基准记录的是旧实现出口文本（仍带标签，剥离发生在调度层）；新架构把「标签判定 →
    剥离」提前到 registry 出口（契约：`ToolResult.llm_text` 一律无标签），故对照
    剥离标签后的等价文本，并断言失败标记已在剥离前判定置位。
    """
    expected = _expected("param_utils.json", "registry.execute(错误路径)", case["id"])
    registry = ToolRegistry()
    registry.register(_FuncTool("Edit", EDIT_PARAMS, _edit_impl))
    registry.register(_FuncTool("Read", READ_PARAMS, _read_impl))

    result = registry.execute(case["name"], case["arguments"])
    assert expected["llm_result"]["has_tag"] is True  # 基线文本确带标签
    assert result.llm_text == expected["llm_result"]["text"].replace(f" {TAG_PLACEHOLDER}", "")
    assert MASK_TAG_RE.search(result.llm_text) is None
    assert result.ui_text is None  # 旧实现的空串 color_diff → 契约语义 None（无差异输出）
    assert result.is_error is True


def test_registry_execute_success_and_normalization():
    registry = ToolRegistry()
    registry.register(_FuncTool("Read", READ_PARAMS, _read_impl))
    registry.register(_FuncTool("Edit", EDIT_PARAMS, _edit_impl))
    registry.register(_FuncTool("Plain", [], lambda: "纯文本"))
    registry.register(_FuncTool("Obj", [], lambda: ToolResult(llm_text="对象", ui_text="ui")))
    registry.register(_FuncTool("EmptyDiff", [], lambda: ("文本", "")))

    read = registry.execute("Read", {"file_path": "a.txt"})
    assert read == ToolResult(llm_text="读取 a.txt")

    edit = registry.execute("Edit", {"file_path": "a.txt"})
    assert edit == ToolResult(llm_text="[已替换1处]", ui_text="@@ diff")

    assert registry.execute("Plain", {}) == ToolResult(llm_text="纯文本")
    assert registry.execute("Obj", {}) == ToolResult(llm_text="对象", ui_text="ui")
    assert registry.execute("EmptyDiff", {}).ui_text is None


def test_normalize_result_forms():
    original = ToolResult(llm_text="t", ui_text="u", await_confirm=True, is_error=True)
    assert normalize_result(original) is original
    assert normalize_result("文本") == ToolResult(llm_text="文本")
    assert normalize_result(("文本", "@@ diff")) == ToolResult(llm_text="文本", ui_text="@@ diff")
    assert normalize_result(("文本", None)).ui_text is None
    assert normalize_result(("文本", "")).ui_text is None
    assert normalize_result(()).llm_text == ""


def test_registry_execute_injects_env_and_normalizes_arguments():
    registry = ToolRegistry()
    tool = _EnvTool("Echo")
    registry.register(tool)
    env = ToolEnvImpl()

    registry.execute("Echo", {"x": 1}, env)
    assert tool.calls[-1] == ({"x": 1}, env)

    registry.execute("Echo", None)
    assert tool.calls[-1] == ({}, None)

    registry.execute("Echo", "not-a-mapping")  # 非法 JSON 形态按空参数处理
    assert tool.calls[-1][0] == {}


def test_registry_judges_error_tag_before_stripping():
    """判定顺序（specs/tools-shell）：先以错误标签判定失败，后剥离标签交 LLM。"""
    registry = ToolRegistry()
    registry.register(_FuncTool("Err", [], lambda: signal.error_line("boom")))
    registry.register(_FuncTool("Fake", [], lambda: "[错误: 假消息]"))

    failed = registry.execute("Err", {})
    assert failed.is_error is True
    assert failed.llm_text == "[错误: boom]"  # 标签已剥离，文本保留

    fake = registry.execute("Fake", {})
    assert fake.is_error is False  # 同款文字无标签，不触发失败显示
    assert fake.llm_text == "[错误: 假消息]"


def test_registry_execute_wraps_generic_exception():
    def _boom() -> None:
        raise RuntimeError("内部错误")

    registry = ToolRegistry()
    registry.register(_FuncTool("Boom", [], _boom))
    result = registry.execute("Boom", {})
    assert result.is_error is True
    assert result.llm_text == "[错误: 工具执行失败(Boom): 内部错误]"


def test_registry_execute_does_not_catch_base_exception():
    def _interrupted() -> None:
        raise KeyboardInterrupt

    registry = ToolRegistry()
    registry.register(_FuncTool("Ki", [], _interrupted))
    with pytest.raises(KeyboardInterrupt):
        registry.execute("Ki", {})


def test_registry_await_confirm_forms():
    registry = ToolRegistry()
    registry.register(_FuncTool("AwaitText", [], lambda: AWAIT_CONFIRM))
    registry.register(_FuncTool("AwaitFlag", [],
                                lambda: ToolResult(llm_text=AWAIT_CONFIRM, await_confirm=True)))
    registry.register(_FuncTool("Normal", [], lambda: "正常"))

    text_form = registry.execute("AwaitText", {})
    assert text_form.await_confirm is True
    assert text_form.llm_text == AWAIT_CONFIRM

    flag_form = registry.execute("AwaitFlag", {})
    assert flag_form.await_confirm is True

    assert registry.execute("Normal", {}).await_confirm is False


def test_registry_global_truncation():
    limit = 1024
    head, tail = limit * 2 // 3, limit - limit * 2 // 3
    text = "a" * 2000 + "尾"
    registry = ToolRegistry()
    registry.register(_FuncTool("Big", [], lambda: text))
    env = ToolEnvImpl(settings=ToolSettingsImpl(max_tool_output_chars=limit))

    result = registry.execute("Big", {}, env)
    assert result.llm_text.startswith("a" * head)
    assert result.llm_text.endswith("a" * (tail - 1) + "尾")
    assert "共2001字符(≈501token)" in result.llm_text
    assert f"...[全局截断: 输出共{len(text)}字符(≈{token_estimate.estimate_text_tokens(text)}token)," \
           " 已达全局上限1KB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]" in result.llm_text


def test_registry_truncation_disabled_paths():
    text = "x" * 5000
    registry = ToolRegistry()
    registry.register(_FuncTool("Big", [], lambda: text))

    assert registry.execute("Big", {}).llm_text == text  # env 缺失：不截断
    unlimited = ToolEnvImpl(settings=ToolSettingsImpl(max_tool_output_chars=0))
    assert registry.execute("Big", {}, unlimited).llm_text == text  # 0=不限制
    roomy = ToolEnvImpl(settings=ToolSettingsImpl(max_tool_output_chars=65536))
    assert registry.execute("Big", {}, roomy).llm_text == text


def test_registry_truncation_kb_integer_division():
    registry = ToolRegistry()
    registry.register(_FuncTool("Big", [], lambda: "a" * 100))
    env = ToolEnvImpl(settings=ToolSettingsImpl(max_tool_output_chars=50))
    assert "已达全局上限0KB" in registry.execute("Big", {}, env).llm_text


def test_registry_register_dynamic_flow():
    registry = ToolRegistry()
    assert registry.register(_FuncTool("Read", READ_PARAMS, _read_impl)) is True
    assert registry.register(_FuncTool("Read", READ_PARAMS, _read_impl)) is False  # 重名跳过
    assert registry.has_tool("Read") is True

    demo = [_FuncTool("mcp__demo__search", [], lambda: "结果")]
    assert registry.register_dynamic(demo) == ["mcp__demo__search"]
    assert registry.register_dynamic(demo) == []  # 重复注册幂等
    assert registry.register_dynamic([_FuncTool("Read", [], lambda: "x")]) == []  # 与内置重名跳过

    assert registry.get_tool_names() == ["Read", "mcp__demo__search"]
    assert [d["function"]["name"] for d in registry.get_tool_definitions()] == \
        ["Read", "mcp__demo__search"]
    assert registry.has_tool("mcp__demo__search") is True

    called = registry.execute("mcp__demo__search", {})
    assert called == ToolResult(llm_text="结果")

    registry.unregister_dynamic(["mcp__demo__search"])
    assert registry.get_tool_names() == ["Read"]
    assert registry.has_tool("mcp__demo__search") is False
    unregistered = registry.execute("mcp__demo__search", {})
    assert unregistered.is_error is True
    assert unregistered.llm_text == "[错误: 未知工具: mcp__demo__search]"

    registry.unregister_dynamic([])  # 空名单与缺失名单不产生变更
    registry.unregister_dynamic(None)
    registry.unregister_dynamic(["Read"])  # 内置工具不受影响
    assert registry.get_tool_names() == ["Read"]


def test_registry_goal_definition_filter():
    registry = ToolRegistry()
    registry.register(_FuncTool("Read", READ_PARAMS, _read_impl))
    registry.register(_FuncTool("GoalComplete", [], lambda: "[GOAL_COMPLETE] 目标任务已声明完成。"),
                      expose_definition=False)

    assert registry.get_tool_names() == ["Read", "GoalComplete"]  # 执行能力始终注册
    default_names = [d["function"]["name"] for d in registry.get_tool_definitions()]
    assert default_names == ["Read"]
    goal_names = [d["function"]["name"] for d in registry.get_tool_definitions(include_goal=True)]
    assert goal_names == ["Read", "GoalComplete"]

    result = registry.execute("GoalComplete", {})
    assert result.is_error is False
    assert result.llm_text == "[GOAL_COMPLETE] 目标任务已声明完成。"


def test_valid_param_names_from_definition():
    assert valid_param_names(_FuncTool("Read", READ_PARAMS, _read_impl)) == READ_PARAMS
    assert valid_param_names(_EnvTool("Empty")) == []

    class _EmptyDefinition:
        name = "EmptyDef"

        def definition(self) -> dict:
            return {}

        def execute(self, args: dict, env) -> object:
            return ""

    assert valid_param_names(_EmptyDefinition()) == []

    class _RaisingDefinition(_EmptyDefinition):
        def definition(self) -> dict:
            raise RuntimeError("定义不可得")

    assert valid_param_names(_RaisingDefinition()) == []


def test_registry_tool_protocol_conformance():
    assert isinstance(_FuncTool("Read", READ_PARAMS, _read_impl), Tool)
    assert isinstance(_EnvTool("Echo"), Tool)


# ═══════════════════════════════════════════════════════════════
# 3. env —— Trackers 与 ToolSettings（状态归主语义）
# ═══════════════════════════════════════════════════════════════


def test_tool_settings_defaults_and_instance_isolation():
    settings = ToolSettingsImpl()
    assert settings.max_tool_output_chars == 65536
    assert settings.max_timeout_seconds == 1800
    assert settings.max_transfer_mb == 100
    assert settings.ignore_dirs == []
    assert settings.require_plan is False
    assert settings.min_tools == 2
    assert settings.git_skip_confirm is False
    assert settings.rm_skip_confirm is False
    assert settings.get_api_key("websearch") == ""

    first, second = ToolSettingsImpl(), ToolSettingsImpl()
    first.ignore_dirs.append("node_modules")
    first.api_keys["websearch"] = "sk-x"
    assert second.ignore_dirs == [] and second.api_keys == {}  # 可变默认不共享
    assert first.get_api_key("websearch") == "sk-x"
    assert first.get_api_key("unknown") == ""


def test_plan_tracker_reference_semantics():
    plan = PlanTrackerImpl()
    assert plan.current() == []

    todos = [{"content": "运行测试", "status": "in_progress"}]
    plan.replace(todos)
    assert plan.current() is todos  # 共享列表本体（就地修正可见）
    assert plan.current()[0] is todos[0]  # 共享元素对象

    todos[0]["status"] = "completed"  # 外部就地修正 → 计划状态同步反映
    assert plan.current()[0]["status"] == "completed"

    replacement = [{"content": "写测试", "status": "pending"}]
    plan.replace(replacement)
    assert plan.current() is replacement
    assert todos[0]["status"] == "completed"  # 旧列表不再被持有


def test_goal_state_consume_and_reset():
    goal = GoalStateImpl()
    assert goal.is_set is False
    assert goal.consume() is False

    goal.mark()
    assert goal.is_set is True
    assert goal.consume() is True  # 消费即复位
    assert goal.is_set is False
    assert goal.consume() is False

    goal.mark()
    goal.reset()  # 新任务复位
    assert goal.is_set is False


def test_reminder_state_trigger_once():
    reminders = ReminderStateImpl()
    assert reminders.try_trigger_plan() is True
    assert reminders.try_trigger_plan() is False  # 只触发一次
    assert reminders.try_trigger_bg() is True  # 两种提醒独立计数
    assert reminders.try_trigger_bg() is False
    assert reminders.try_trigger_plan() is False

    reminders.reset()
    assert reminders.try_trigger_plan() is True
    assert reminders.try_trigger_bg() is True


def test_delete_gate_pending_and_confirmed():
    gate = DeleteGateImpl()
    assert gate.take() is None

    gate.pend("Shell", {"command": "rm -rf build"})
    pending = gate.take()
    assert pending == ("Shell", {"command": "rm -rf build"})
    assert gate.take() is None  # 取出后清空暂存

    assert gate.consume_confirmed() is False
    gate.mark_confirmed()
    assert gate.consume_confirmed() is True  # 消费即复位
    assert gate.consume_confirmed() is False


class _McpStub:
    def connect(self, name: str, config: dict) -> tuple:
        return (0, [])

    def disconnect(self, name: str) -> int:
        return 0

    def connected(self) -> list:
        return []


def test_tool_env_aggregation_and_defaults():
    env = ToolEnvImpl()
    assert isinstance(env, ToolEnv)
    assert isinstance(env.settings, ToolSettings)
    assert isinstance(env.plan, PlanTracker)
    assert isinstance(env.goal, GoalState)
    assert isinstance(env.reminders, ReminderState)
    assert isinstance(env.delete_gate, DeleteGate)
    assert env.confirm is None
    assert env.mcp is None

    other = ToolEnvImpl()
    assert env.settings is not other.settings
    assert env.plan is not other.plan
    assert env.goal is not other.goal
    assert env.reminders is not other.reminders
    assert env.delete_gate is not other.delete_gate


def test_tool_env_accepts_injected_components():
    settings = ToolSettingsImpl(ignore_dirs=["node_modules"])
    plan = PlanTrackerImpl()
    gate = DeleteGateImpl()
    mcp = _McpStub()
    env = ToolEnvImpl(settings=settings, plan=plan, delete_gate=gate,
                      confirm=lambda command: True, mcp=mcp)

    assert env.settings is settings and env.plan is plan and env.delete_gate is gate
    assert env.confirm is not None and env.confirm("rm -rf x") is True
    assert isinstance(env.mcp, McpPort)

    with pytest.raises(AttributeError):  # 聚合面只读（无 setter）
        env.settings = settings
    with pytest.raises(AttributeError):
        env.mcp = None


def test_tool_env_shared_trackers_visible_across_aggregations():
    """同一 tracker 注入到多处于装配点完成（状态归主：单一属主、单一生效路径）。"""
    goal = GoalStateImpl()
    env = ToolEnvImpl(goal=goal)
    env.goal.mark()
    assert goal.is_set is True
    assert goal.consume() is True
    assert env.goal.is_set is False


# ═══════════════════════════════════════════════════════════════
# 4. token_estimate —— 基准对照（截断提示口径）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", cases.TOKEN_ESTIMATE_TEXT_CASES, ids=lambda c: c["id"])
def test_token_estimate_text_matches_baseline(case):
    expected = _expected("token_estimate.json", "token_estimate.estimate_text_tokens", case["id"])
    assert token_estimate.estimate_text_tokens(case["text"]) == expected


@pytest.mark.parametrize("case", cases.TOKEN_ESTIMATE_MESSAGE_CASES, ids=lambda c: c["id"])
def test_token_estimate_message_matches_baseline(case):
    expected = _expected("token_estimate.json", "token_estimate.estimate_message_tokens", case["id"])
    assert token_estimate.estimate_message_tokens(case["msg"]) == expected


# ═══════════════════════════════════════════════════════════════
# 5. 结构：依赖纯净（tools 只向下引用 contracts / messages）、聚合导出与模块级常量规约
# ═══════════════════════════════════════════════════════════════


def test_tools_package_exports_and_submodule_paths():
    from narnat_agent import tools as tools_package
    from narnat_agent.tools import env, registry, signal as signal_module
    from narnat_agent.tools import token_estimate as estimate_module

    for name in tools_package.__all__:
        assert hasattr(tools_package, name), f"__all__ 中的 {name!r} 无法从包解析"
    assert signal_module.__name__ == "narnat_agent.tools.signal"
    assert registry.__name__ == "narnat_agent.tools.registry"
    assert env.__name__ == "narnat_agent.tools.env"
    assert estimate_module.__name__ == "narnat_agent.tools.token_estimate"
    assert tools_package.ToolRegistry is registry.ToolRegistry
    assert tools_package.rc_line is signal_module.rc_line


def test_token_estimate_is_thin_forwarder():
    """T3.11：`tools.token_estimate` 只转发 `messages.tokens`（唯一实现），无本地副本。"""
    from narnat_agent.messages import tokens as messages_tokens

    assert token_estimate.estimate_text_tokens is messages_tokens.estimate_text_tokens
    assert token_estimate.estimate_message_tokens is messages_tokens.estimate_message_tokens


def test_tools_framework_dependencies_stay_in_lower_layer():
    """依赖纯净：禁止绝对导入新包；跨积木相对导入只许向下（contracts L0 / messages L1）。

    `tools.token_estimate` 为 `messages.tokens` 的转发壳（T3.11 唯一化）——messages（L1）
    是 tools（L2）的合法向下依赖（同 `tests/check_layering.py` 的层级规则）；同层/上层
    积木仍一律禁引。
    """
    allowed = ("contracts", "messages")
    sources = sorted(TOOLS_DIR.glob("*.py"))
    assert sources, "tools 目录不存在或为空"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("narnat_agent"), (
                        f"{path.name}: 禁止绝对导入新包（{alias.name}）")
            elif isinstance(node, ast.ImportFrom) and node.level:
                if node.level >= 2:
                    top = (node.module or "").split(".")[0]
                    assert top in allowed, (
                        f"{path.name}: 跨积木导入越层（{'.' * node.level}{node.module}）")
