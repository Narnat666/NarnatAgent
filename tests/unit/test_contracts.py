"""contracts 积木自测 —— 构造样例、字段完整性、谓词边界、常量一致性与协议形状。

覆盖（对齐 T1.1 任务书测试要求）：
1. 每个事件 TypedDict 的样例构造与消费形态（dict 形态 runtime 检查）；
2. 谓词边界：空 dict、未知键、finish_reason 取值（已知枚举 + 未知透传）；
3. 枚举常量与 R1 报告 870-879 清单逐项一致（数量断言）；
4. dataclass 字段完整性与关键默认值；
5. 协议形状（runtime_checkable 结构化检查，含负例）；
6. 包聚合导出完整性（`__all__` 全部可解析）；
7. 依赖纯净性：contracts 无新包其他积木 import（L0 硬约束）。
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

import narnat_agent.contracts as contracts
from narnat_agent.contracts import interrupt, llm_events, output, tool
from narnat_agent.contracts.llm_events import (
    FINISH_REASONS,
    INTERRUPT_KINDS,
    get_finish_reason,
    get_stream_interrupted,
    get_thinking,
    get_thinking_signature,
    get_tool_calls,
    get_usage,
    is_completion,
    is_retry_notice,
    is_stream_interrupted,
    is_text_delta,
)
from narnat_agent.contracts.output import Animator, InteractionPort, OutputSink, TurnStats
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

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "contracts"


# ═══════════════════════════════════════════════════════════════
# 1. 事件形态样例构造（R1 报告 870-879 清单逐项）
# ═══════════════════════════════════════════════════════════════


def test_shape_text_delta():
    """`{"content": str}`：文本增量。"""
    event = {"content": "增量文本"}
    assert is_text_delta(event) is True
    assert is_completion(event) is False
    assert get_finish_reason(event) is None
    assert get_tool_calls(event) is None


def test_shape_tool_calls_ready():
    """`{"tool_calls": [...], "finish_reason", "thinking"[, "thinking_signature"]}`。"""
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "Shell", "arguments": '{"command": "dir"}'},
    }
    event = {"tool_calls": [call], "finish_reason": "tool_calls", "thinking": None}
    assert is_text_delta(event) is False  # 含 tool_calls 即非纯文本增量
    assert get_tool_calls(event) == [call]
    assert get_finish_reason(event) == "tool_calls"
    assert get_thinking(event) is None
    assert get_thinking_signature(event) is None

    # Anthropic 形态：额外携带 thinking_signature
    signed = {"tool_calls": [call], "finish_reason": "tool_calls",
              "thinking": "思考正文", "thinking_signature": "sig-abc"}
    assert get_thinking(signed) == "思考正文"
    assert get_thinking_signature(signed) == "sig-abc"


def test_shape_completion_without_tools():
    """`{"finish_reason": str[, "thinking"]}`：无工具轮完成。"""
    event = {"finish_reason": "stop", "thinking": ""}
    assert is_completion(event) is True
    assert is_text_delta(event) is False
    assert get_finish_reason(event) == "stop"
    assert get_thinking(event) == ""  # 空串是值，不是缺省

    minimal = {"finish_reason": "max_tokens"}
    assert is_completion(minimal) is True
    assert get_thinking(minimal) is None


def test_shape_usage():
    """`{"usage": {"prompt_tokens", "completion_tokens", "cached_tokens"}}`。"""
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 40}
    event = {"usage": usage}
    assert get_usage(event) == usage
    assert is_completion(event) is False


def test_shape_retry_notice():
    """`{"retry_notice": str}`：重试通知（仅渲染、不入历史）。"""
    event = {"retry_notice": "\n⚠ 请求被限流(429)，约2s后自动重试（第1/3次）…\n"}
    assert is_retry_notice(event) is True
    assert is_text_delta(event) is False


def test_shape_stream_interrupted():
    """`{"stream_interrupted": {"kind", "detail"}}`：流中断。"""
    event = {"stream_interrupted": {"kind": "timeout", "detail": "读超时: 300s"}}
    assert is_stream_interrupted(event) is True
    info = get_stream_interrupted(event)
    assert info is not None
    assert info["kind"] in INTERRUPT_KINDS
    assert info["detail"] == "读超时: 300s"
    assert is_completion(event) is False  # 流中断不伪造完成原因


def test_shape_context_overflow():
    """`{"finish_reason": "context_overflow"}`：上下文超限（不带 content）。"""
    event = {"finish_reason": "context_overflow"}
    assert is_completion(event) is True
    assert get_finish_reason(event) == "context_overflow"
    assert is_text_delta(event) is False


def test_shape_error_completion():
    """`{"content": "[错误: …]", "finish_reason": "error"}`：错误完成。"""
    event = {"content": "[错误: API调用失败(TimeoutError): 超时]", "finish_reason": "error"}
    assert get_finish_reason(event) == "error"
    assert is_completion(event) is True
    # 含 content 且不含 tool_calls：命中纯文本增量判定（与现状消费顺序一致，
    # 错误出口不写入历史）
    assert is_text_delta(event) is True


# ═══════════════════════════════════════════════════════════════
# 2. 谓词边界
# ═══════════════════════════════════════════════════════════════

_ALL_PREDICATES_BOOL = (
    is_text_delta,
    is_completion,
    is_stream_interrupted,
    is_retry_notice,
)
_ALL_PREDICATES_NONE = (
    get_finish_reason,
    get_tool_calls,
    get_usage,
    get_thinking,
    get_thinking_signature,
    get_stream_interrupted,
)


@pytest.mark.parametrize("event", [{}, {"foo": 1}, {"content_": "x"}, {"finish": "stop"}])
def test_predicates_on_empty_and_unknown_events(event):
    for predicate in _ALL_PREDICATES_BOOL:
        assert predicate(event) is False
    for getter in _ALL_PREDICATES_NONE:
        assert getter(event) is None


def test_is_text_delta_semantics():
    """任务书锚点：含 content 且不含 tool_calls。"""
    assert is_text_delta({"content": "x"}) is True
    assert is_text_delta({"content": "x", "tool_calls": []}) is False
    assert is_text_delta({"tool_calls": []}) is False
    assert is_text_delta({"content": ""}) is True  # 空串仍是文本增量


def test_getters_value_type_guard():
    """取值类型不符时按缺省处理（None），不抛异常。"""
    assert get_finish_reason({"finish_reason": 123}) is None
    assert get_finish_reason({"finish_reason": None}) is None
    assert get_tool_calls({"tool_calls": {}}) is None
    assert get_usage({"usage": []}) is None
    assert get_thinking({"thinking": 5}) is None
    assert get_thinking_signature({"thinking_signature": 0}) is None
    assert get_stream_interrupted({"stream_interrupted": "boom"}) is None


def test_finish_reason_values():
    """已知取值逐项可读取；协议映射表外的未知原因原样透传。"""
    for reason in FINISH_REASONS:
        assert get_finish_reason({"finish_reason": reason}) == reason
    assert get_finish_reason({"finish_reason": "some_unknown_reason"}) == "some_unknown_reason"


def test_getters_preserve_object_identity():
    """get_* 返回事件内对象本体（消费方按引用读取，无拷贝语义变化）。"""
    calls = [{"id": "a", "type": "function",
              "function": {"name": "Read", "arguments": "{}"}}]
    event = {"tool_calls": calls}
    assert get_tool_calls(event) is calls
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "cached_tokens": 0}
    assert get_usage({"usage": usage}) is usage


def test_empty_tool_calls_list_is_not_none():
    """空列表表示「有缓冲但为空」，与无键（None）区分。"""
    assert get_tool_calls({"tool_calls": []}) == []


# ═══════════════════════════════════════════════════════════════
# 3. 枚举常量与 R1 清单逐项一致（数量断言）
# ═══════════════════════════════════════════════════════════════


def test_finish_reasons_constant():
    assert len(FINISH_REASONS) == 7
    assert FINISH_REASONS == (
        "stop",
        "tool_calls",
        "max_tokens",
        "content_filter",
        "server_busy",
        "error",
        "context_overflow",
    )


def test_interrupt_kinds_constant():
    assert len(INTERRUPT_KINDS) == 3
    assert INTERRUPT_KINDS == ("timeout", "network", "unknown")


def test_key_constants_values():
    """键名常量值逐字对照 R1 清单（禁止手误）。"""
    assert llm_events.KEY_CONTENT == "content"
    assert llm_events.KEY_TOOL_CALLS == "tool_calls"
    assert llm_events.KEY_FINISH_REASON == "finish_reason"
    assert llm_events.KEY_THINKING == "thinking"
    assert llm_events.KEY_THINKING_SIGNATURE == "thinking_signature"
    assert llm_events.KEY_USAGE == "usage"
    assert llm_events.KEY_RETRY_NOTICE == "retry_notice"
    assert llm_events.KEY_STREAM_INTERRUPTED == "stream_interrupted"
    assert llm_events.KEY_PROMPT_TOKENS == "prompt_tokens"
    assert llm_events.KEY_COMPLETION_TOKENS == "completion_tokens"
    assert llm_events.KEY_CACHED_TOKENS == "cached_tokens"
    assert llm_events.KEY_KIND == "kind"
    assert llm_events.KEY_DETAIL == "detail"
    assert llm_events.KEY_ID == "id"
    assert llm_events.KEY_TYPE == "type"
    assert llm_events.KEY_FUNCTION == "function"
    assert llm_events.KEY_NAME == "name"
    assert llm_events.KEY_ARGUMENTS == "arguments"


# ═══════════════════════════════════════════════════════════════
# 4. dataclass 字段完整性
# ═══════════════════════════════════════════════════════════════


def test_turn_stats_fields_and_defaults():
    assert [f.name for f in fields(TurnStats)] == [
        "input_tokens",
        "output_tokens",
        "cache_ratio",
        "cost",
        "balance",
        "thinking_effort",
    ]
    default = TurnStats()
    assert (default.input_tokens, default.output_tokens) == (0, 0)
    assert (default.cache_ratio, default.cost, default.balance) == (0.0, 0.0, 0.0)
    assert default.thinking_effort == "高"

    stats = TurnStats(input_tokens=12345, output_tokens=678, cache_ratio=0.5,
                      cost=0.0123, balance=9.99, thinking_effort="中")
    assert stats.input_tokens == 12345 and stats.balance == 9.99


def test_tool_result_fields_and_defaults():
    assert [f.name for f in fields(ToolResult)] == [
        "llm_text",
        "ui_text",
        "await_confirm",
        "is_error",
    ]
    result = ToolResult(llm_text="ok")
    assert result.ui_text is None
    assert result.await_confirm is False
    assert result.is_error is False

    failure = ToolResult(llm_text="[错误: …]", is_error=True)
    assert failure.is_error is True and failure.await_confirm is False
    assert ToolResult("t", "@@ diff", True, True) == ToolResult(
        llm_text="t", ui_text="@@ diff", await_confirm=True, is_error=True)


def test_await_confirm_literal():
    assert AWAIT_CONFIRM == "__AWAIT_CONFIRM__"


def test_tool_definition_shape():
    definition: tool.ToolDefinition = {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": "本地Shell — 执行命令。",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "Shell"
    assert definition["function"]["parameters"]["type"] == "object"


def test_todo_item_shape():
    item: tool.TodoItem = {"content": "运行测试", "status": "in_progress"}
    assert item["status"] in ("pending", "in_progress", "completed")


# ═══════════════════════════════════════════════════════════════
# 5. 协议形状（结构化检查 + 关键签名锚点）
# ═══════════════════════════════════════════════════════════════


class _SinkStub:
    @property
    def cancelled(self) -> bool:
        return False

    @property
    def aborted(self) -> bool:
        return False

    def feed(self, text: str) -> None: ...
    def notify(self, text: str) -> None: ...
    def flush(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def finish(self, stats: TurnStats, with_stats: bool = True) -> None: ...
    def abort(self, message: str | None = None) -> None: ...
    def restart_attempt(self) -> None: ...


class _InteractionStub:
    def begin_turn(self) -> OutputSink:
        return _SinkStub()

    def read_confirmation(self, prompt: str) -> bool:
        return False

    def notify_interrupted(self) -> None: ...


class _AnimatorStub:
    def begin_compressing(self) -> None: ...
    def end_compressing(self) -> None: ...
    def begin_summarizing(self) -> None: ...
    def end_summarizing(self) -> None: ...


class _InterruptStub:
    @property
    def is_set(self) -> bool:
        return False

    def clear(self) -> None: ...
    def raise_(self) -> None: ...
    def subscribe(self, handler) -> None: ...
    def enter_input_mode(self) -> None: ...
    def enter_run_mode(self) -> None: ...


class _SettingsStub:
    ignore_dirs = []
    max_tool_output_chars = 65536
    max_timeout_seconds = 1800
    max_transfer_mb = 100
    git_skip_confirm = False
    rm_skip_confirm = False
    require_plan = False
    min_tools = 2

    def get_api_key(self, name: str) -> str:
        return ""


class _PlanStub:
    def current(self) -> list:
        return []

    def replace(self, todos: list) -> None: ...


class _GoalStub:
    @property
    def is_set(self) -> bool:
        return False

    def mark(self) -> None: ...
    def consume(self) -> bool:
        return False

    def reset(self) -> None: ...


class _ReminderStub:
    def try_trigger_plan(self) -> bool:
        return False

    def try_trigger_bg(self) -> bool:
        return False

    def reset(self) -> None: ...


class _DeleteGateStub:
    def pend(self, tool_name: str, arguments: dict) -> None: ...

    def take(self):
        return None

    def mark_confirmed(self) -> None: ...

    def consume_confirmed(self) -> bool:
        return False


class _McpStub:
    def connect(self, name: str, config: dict) -> tuple:
        return (0, [])

    def disconnect(self, name: str) -> int:
        return 0

    def connected(self) -> list:
        return []


class _ToolEnvStub:
    def __init__(self) -> None:
        self.settings = _SettingsStub()
        self.plan = _PlanStub()
        self.goal = _GoalStub()
        self.reminders = _ReminderStub()
        self.delete_gate = _DeleteGateStub()
        self.confirm = None
        self.mcp = None


class _ToolStub:
    @property
    def name(self) -> str:
        return "Stub"

    def definition(self) -> tool.ToolDefinition:
        return {"type": "function",
                "function": {"name": "Stub", "description": "", "parameters": {}}}

    def execute(self, args: dict, env: ToolEnv) -> ToolResult:
        return ToolResult(llm_text="")


def test_protocol_shapes_positive():
    assert isinstance(_SinkStub(), OutputSink)
    assert isinstance(_InteractionStub(), InteractionPort)
    assert isinstance(_AnimatorStub(), Animator)
    assert isinstance(_InterruptStub(), interrupt.InterruptSignal)
    assert isinstance(_SettingsStub(), ToolSettings)
    assert isinstance(_PlanStub(), PlanTracker)
    assert isinstance(_GoalStub(), GoalState)
    assert isinstance(_ReminderStub(), ReminderState)
    assert isinstance(_DeleteGateStub(), DeleteGate)
    assert isinstance(_McpStub(), McpPort)
    assert isinstance(_ToolEnvStub(), ToolEnv)
    assert isinstance(_ToolStub(), Tool)


def test_protocol_shapes_negative():
    """缺任一成员的实现不满足协议（结构性检查有效）。"""
    class _IncompleteSink:
        def feed(self, text: str) -> None: ...

    assert not isinstance(_IncompleteSink(), OutputSink)
    assert not isinstance(object(), OutputSink)
    assert not isinstance(object(), ToolEnv)
    assert not isinstance(42, interrupt.InterruptSignal)


def test_output_sink_signature_anchors():
    """关键形参与默认值锚点（finish/abort 语义对齐 design D4）。"""
    finish_params = inspect.signature(OutputSink.finish).parameters
    assert list(finish_params) == ["self", "stats", "with_stats"]
    assert finish_params["with_stats"].default is True

    abort_params = inspect.signature(OutputSink.abort).parameters
    assert list(abort_params) == ["self", "message"]
    assert abort_params["message"].default is None


def test_interaction_port_signature_anchors():
    confirm_params = inspect.signature(InteractionPort.read_confirmation).parameters
    assert list(confirm_params) == ["self", "prompt"]
    assert list(inspect.signature(InteractionPort.begin_turn).parameters) == ["self"]


def test_interrupt_signal_signature_anchors():
    subscribe_params = inspect.signature(interrupt.InterruptSignal.subscribe).parameters
    assert list(subscribe_params) == ["self", "handler"]


# ═══════════════════════════════════════════════════════════════
# 6. 包聚合导出完整性
# ═══════════════════════════════════════════════════════════════


def test_package_all_resolvable():
    for name in contracts.__all__:
        assert hasattr(contracts, name), f"__all__ 中的 {name!r} 无法从包解析"


def test_submodule_all_reexported():
    for module in (llm_events, tool, output, interrupt):
        for name in module.__all__:
            assert hasattr(contracts, name), f"{module.__name__}.{name} 未重导出"
            assert getattr(contracts, name) is getattr(module, name)


def test_four_import_paths():
    """验收锚点：from narnat_agent.contracts import llm_events, tool, output, interrupt。"""
    assert llm_events.__name__ == "narnat_agent.contracts.llm_events"
    assert tool.__name__ == "narnat_agent.contracts.tool"
    assert output.__name__ == "narnat_agent.contracts.output"
    assert interrupt.__name__ == "narnat_agent.contracts.interrupt"


# ═══════════════════════════════════════════════════════════════
# 7. 依赖纯净性（L0：不得 import 新包其他积木）
# ═══════════════════════════════════════════════════════════════


def test_contracts_no_cross_building_imports():
    sources = sorted(CONTRACTS_DIR.glob("*.py"))
    assert sources, "contracts 目录不存在或为空"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("narnat_agent"), (
                        f"{path.name}: 禁止绝对导入新包（{alias.name}）")
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    assert not (node.module or "").startswith("narnat_agent"), (
                        f"{path.name}: 禁止绝对导入新包（{node.module}）")
                else:
                    assert node.level == 1, (
                        f"{path.name}: 相对导入越出 contracts 积木（level={node.level}）")


def test_contracts_dependencies_are_stdlib_only():
    """contracts 只允许标准库 typing / dataclasses / collections.abc 与 __future__。"""
    allowed = {"__future__", "typing", "dataclasses", "collections"}
    for path in sorted(CONTRACTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, (
                        f"{path.name}: 非法依赖 {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] in allowed, (
                    f"{path.name}: 非法依赖 {node.module}")
