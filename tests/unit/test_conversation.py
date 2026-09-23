"""conversation 积木自测（T4.1）—— spec Scenario 全覆盖 + fake 全流程 + 调度分组单测。

覆盖清单（对齐 T4.1 任务书测试要求）：
1. `specs/conversation` 全部 41 个 Scenario（映射表见下）；
2. fake LLM（脚本化事件序列）+ fake 工具（可编程结果）+ fake OutputSink / 交互端口
   的全流程：纯文本轮 / 工具轮 / 多轮 / 中断 / 重试 / 溢出 / 确认挂起 / 续跑；
3. 调度分组策略单测（分组结果、同文件串行、结果顺序保持）+ 终端调度行；
4. 结构检查：`TurnOutcome` / `last_content` 公开 API 且源码不含旧私有互摸名。

spec Scenario 映射表
════════════════════

| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 内循环轮转与事件流消费 | 工具轮形成闭环 | `test_s01_tool_round_closes_loop` |
| 2 | 内循环轮转与事件流消费 | 请求前修复 | `test_s02_repair_before_request` |
| 3 | 内循环轮转与事件流消费 | 带工具调用的文本不上屏 | `test_s03_text_with_tool_calls_not_shown` |
| 4 | 内循环轮转与事件流消费 | 重试提示直通终端 | `test_s04_retry_notice_passthrough` |
| 5 | 对话出口 | 纯文本完成 | `test_s05_plain_text_completion` |
| 6 | 对话出口 | 错误出口不留痕 | `test_s06_error_exit_no_trace` |
| 7 | 对话出口 | 空回复分类提示 | `test_s07_empty_reply_texts`（参数化 7 种原因） |
| 8 | 工具调度分组与结果回传 | 只读并行 | `test_s08_readonly_parallel` |
| 9 | 工具调度分组与结果回传 | 同文件写入串行 | `test_s09_same_file_write_serial` |
| 10 | 工具调度分组与结果回传 | 串行组逐个执行 | `test_s10_serial_group_sequential` |
| 11 | 工具调度分组与结果回传 | 结果顺序与缺失 | `test_s11_results_order_and_missing` |
| 12 | 工具调度分组与结果回传 | 执行异常 | `test_s12_execution_exception_two_forms` |
| 13 | 计划优先拦截 | 满足条件拦截 | `test_s13_plan_block_hits` |
| 14 | 计划优先拦截 | 批内含计划工具放行 | `test_s14_plan_block_contains_plan_tool` |
| 15 | 计划优先拦截 | 已有进行中计划放行 | `test_s15_plan_block_active_plan` |
| 16 | 计划优先拦截 | 数量低于门槛放行 | `test_s16_plan_block_below_threshold` |
| 17 | 用户中断协调 | 流消费中中断 | `test_s17_interrupt_during_consume` |
| 18 | 用户中断协调 | 工具执行中中断 | `test_s18_interrupt_during_tools` |
| 19 | 用户中断协调 | 退避等待中中断 | `test_s19_interrupt_during_backoff` |
| 20 | 删除确认挂起 | 确认后重新执行 | `test_s20_delete_confirm_approved` |
| 21 | 删除确认挂起 | 取消 | `test_s21_delete_confirm_rejected` |
| 22 | 删除确认挂起 | 无输入按取消 | `test_s22_delete_confirm_empty_input` |
| 23 | 流中断整轮重试 | 中断后重试成功 | `test_s23_stream_retry_success` |
| 24 | 流中断整轮重试 | 重试耗尽 | `test_s24_stream_retry_exhausted` |
| 25 | 流中断整轮重试 | 完成标记重置预算 | `test_s25_retry_budget_reset_after_completion` |
| 26 | 上下文压力应对 | 首次溢出恢复成功 | `test_s26_overflow_recover_success` |
| 27 | 上下文压力应对 | 再次溢出放弃 | `test_s27_overflow_again_give_up` |
| 28 | 上下文压力应对 | 压缩能力未注入 | `test_s28_overflow_without_compression` |
| 29 | 上下文压力应对 | 运行中自查 | `test_s29_mid_run_guard`（参数化成功/失败） |
| 30 | 收尾软提醒 | 计划未勾选提醒 | `test_s30_plan_reminder` |
| 31 | 收尾软提醒 | 后台任务提醒 | `test_s31_bg_reminder` |
| 32 | 收尾软提醒 | 提醒只触发一次 | `test_s32_reminder_once` |
| 33 | 统计栏结案信号 | 普通模式每轮显示 | `test_s33_stats_shown_normal_mode` |
| 34 | 统计栏结案信号 | 目标模式中间轮静默 | `test_s34_stats_hidden_goal_middle` |
| 35 | 统计栏结案信号 | 后台任务抑制统计栏 | `test_s35_stats_suppressed_by_bg` |
| 36 | 统计栏结案信号 | 删除确认中间结束不显示 | `test_s20_delete_confirm_approved`（断言 with_stats=False） |
| 37 | 目标模式自动续跑 | 声明完成结束续跑 | `test_s37_goal_declared_complete` |
| 38 | 目标模式自动续跑 | 未完成自动续跑 | `test_s38_goal_continue` |
| 39 | 目标模式自动续跑 | 达到轮数上限收尾 | `test_s39_goal_round_limit` |
| 40 | 目标模式自动续跑 | 异常不续跑 | `test_s40_goal_abnormal_no_continue` |
| 41 | 兼容性怪癖保持 | 上限为负立即收尾 | `test_s41_goal_negative_limit` |

调度与结构补充用例：`test_group_*`（分组纯函数）、`test_dispatch_*`（调度行/静默/差异
格式/计划工具排序执行）、`test_structure_*`（公开 API 与源码无私有互摸）。
"""
from __future__ import annotations

import ast
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from narnat_agent.contracts.tool import AWAIT_CONFIRM, ToolResult
from narnat_agent.conversation import (
    BG_REMINDER_TEMPLATE,
    CONTINUE_TEMPLATE,
    DELETE_CANCELLED_TEXT,
    DELETE_CONFIRM_PROMPT,
    EMPTY_REPLY_TEXTS,
    FINAL_ROUND_TEMPLATE,
    GOAL_CONTINUE,
    GOAL_END,
    GOAL_FINAL,
    PLAN_REMINDER_TEMPLATE,
    TURN_COMPLETED,
    TURN_EMPTY,
    TURN_ERROR,
    TURN_INTERRUPTED,
    ConversationLoop,
    GoalMode,
    ToolDispatcher,
    TurnOutcome,
    group_tool_calls,
    parse_tool_call,
    plan_reminder_text,
)
from narnat_agent.conversation.reminders import unfinished_items
from narnat_agent.messages import MessageStore
from narnat_agent.tools import (
    DeleteGateImpl,
    GoalStateImpl,
    PlanTrackerImpl,
    ReminderStateImpl,
    ToolEnvImpl,
    ToolSettingsImpl,
)

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "conversation"
SYSTEM_PROMPT = "系统提示"
TOOL_LABEL_OK = "ok"


# ═══════════════════════════════════════════════════════════════
# 桩件
# ═══════════════════════════════════════════════════════════════


class FakeSink:
    """`OutputSink` 桩：记录全部调用并允许脚本化取消 / 中止状态。"""

    def __init__(self) -> None:
        self.fed: list[str] = []
        self.notified: list[str] = []
        self.finish_calls: list[tuple[object, bool]] = []
        self.aborted = False
        self.abort_messages: list[str | None] = []
        self.cancelled = False
        self.restart_calls = 0

    def feed(self, text: str) -> None:
        self.fed.append(text)

    def notify(self, text: str) -> None:
        self.notified.append(text)

    def finish(self, stats, with_stats: bool = True) -> None:
        self.finish_calls.append((stats, with_stats))

    def abort(self, message: str | None = None) -> None:
        self.aborted = True
        self.abort_messages.append(message)

    def restart_attempt(self) -> None:
        self.restart_calls += 1

    @property
    def text(self) -> str:
        """本轮通知文本拼接（便于断言提示顺序与内容）。"""
        return "".join(self.notified)


class FakeInteraction:
    """`InteractionPort` 桩：记录确认提示符、打断通知与新建的流句柄。"""

    def __init__(self, confirm: bool = False) -> None:
        self._confirm = confirm
        self.confirm_prompts: list[str] = []
        self.sinks: list[FakeSink] = []
        self.interrupted_calls = 0

    def begin_turn(self) -> FakeSink:
        sink = FakeSink()
        self.sinks.append(sink)
        return sink

    def read_confirmation(self, prompt: str) -> bool:
        self.confirm_prompts.append(prompt)
        return self._confirm

    def notify_interrupted(self) -> None:
        self.interrupted_calls += 1


class FakeStats:
    """`StatsPort` 桩：按用量累计读数。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_hit_ratio = 0.0
        self.cost = 0.0
        self.balance = 0.0
        self.updates: list[dict] = []

    def update(self, usage: dict) -> None:
        self.updates.append(usage)
        self.input_tokens = usage["prompt_tokens"]
        self.output_tokens += usage["completion_tokens"]
        self.cache_hit_ratio = 0.5
        self.cost += 0.01
        self.balance = 12.0


class FakeConsole:
    """`ConsolePort` 桩：记录直接写出（工具调度行、后台提示、调试文件落点）。"""

    def __init__(self, quiet: bool = False) -> None:
        self.text: list[str] = []
        self._quiet = quiet

    def write(self, text: str) -> None:
        self.text.append(text)

    def is_quiet_tools(self) -> bool:
        return self._quiet

    @property
    def joined(self) -> str:
        return "".join(self.text)


class FakeInterrupt:
    """`InterruptSignal` 桩：记录触发次数与订阅者。"""

    def __init__(self) -> None:
        self.is_set = False
        self.raised = 0
        self.handlers: list = []

    def clear(self) -> None:
        self.is_set = False

    def raise_(self) -> None:
        self.is_set = True
        self.raised += 1

    def subscribe(self, handler) -> None:
        self.handlers.append(handler)


class FakeBackground:
    """后台任务面桩：脚本化运行中任务数与摘要。"""

    def __init__(self, count: int = 0, summary: str = "") -> None:
        self.count = count
        self.summary = summary

    def running_count(self) -> int:
        return self.count

    def running_summary(self) -> str:
        return self.summary


class FakeCompression:
    """压缩执行面桩：记录调用次数并返回脚本化结果。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.mid_run_calls = 0
        self.compress_calls = 0

    def mid_run_guard(self) -> bool:
        self.mid_run_calls += 1
        return self.ok

    def compress_no_input(self):
        self.compress_calls += 1
        return SimpleNamespace(ok=self.ok)


class FakeRatio:
    """窗口占比面桩：脚本化触发判定并记录刷新值。"""

    def __init__(self, need: bool = False) -> None:
        self.need = need
        self.updates: list[int] = []

    def need_compress(self) -> bool:
        return self.need

    def update_ratio(self, input_tokens: int) -> None:
        self.updates.append(input_tokens)


class FakeRegistry:
    """工具执行入口桩：记录调用并按名返回脚本化结果。

    `results[name]` 支持三种形态：`ToolResult`、可调用对象（按参数现算）、
    `ToolResult` 列表（按调用次序消费，超出后重复最后一项）。
    """

    def __init__(self, results: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results: dict = dict(results or {})
        self._counters: dict[str, int] = {}

    def execute(self, name: str, arguments=None, env=None) -> ToolResult:
        self.calls.append((name, dict(arguments or {})))
        result = self.results.get(name)
        if result is None:
            return ToolResult(llm_text=f"{name} {TOOL_LABEL_OK}")
        if isinstance(result, list):
            index = self._counters.get(name, 0)
            self._counters[name] = index + 1
            return result[min(index, len(result) - 1)]
        if callable(result):
            return result(name, arguments)
        return result


class FakeLLM:
    """LLM 流式面桩：按调用序号回放脚本化事件序列。

    - `after_yield(call_index, event_index)`：每个事件产出后调用（脚本化取消 /
      事件消费中途抛异常用）；
    - `after_script(call_index)`：脚本事件耗尽后调用（模拟"事件流结束但无完成标记"
      之外的边界：事件流结束后的取消检查点）；
    - `honor_cancel=True` 时模拟 llm 层在取消后静默结束流（该行为归 llm 积木，
      默认关闭以便本积木单独驱动「消费中取消」检查点）。
    """

    def __init__(self, scripts: list[list[dict]] | None = None) -> None:
        self.scripts = list(scripts or [])
        self.calls = 0
        self.requests: list[list[dict]] = []
        self.retry_counts: list[int] = []
        self.raw_sse: list[str] | None = None
        self.after_yield = None
        self.after_script = None
        self.honor_cancel = False

    def chat_stream(self, messages, no_tools: bool = False, no_thinking: bool = False,
                    cancel_check=None):
        index = min(self.calls, len(self.scripts) - 1) if self.scripts else 0
        script = self.scripts[index] if self.scripts else []
        self.calls += 1
        self.requests.append(list(messages))
        for position, event in enumerate(script):
            if (self.honor_cancel and cancel_check is not None
                    and cancel_check()):
                return
            yield event
            if self.after_yield is not None:
                self.after_yield(self.calls - 1, position)
        if self.after_script is not None:
            self.after_script(self.calls - 1)

    def set_retry_count(self, n: int) -> None:
        self.retry_counts.append(n)


def build_loop(
    scripts: list[list[dict]],
    *,
    results: dict | None = None,
    env: ToolEnvImpl | None = None,
    background: FakeBackground | None = None,
    compression: FakeCompression | None = None,
    ratio: FakeRatio | None = None,
    quiet: bool = False,
    confirm: bool = False,
    retry_count: int = 3,
    data_dir: str = "",
    pool: ThreadPoolExecutor | None = None,
    resolve_device=None,
    theme=None,
    logger=None,
):
    """装配内循环与全部桩件，返回 `(loop, parts)`。"""
    store = MessageStore(SYSTEM_PROMPT)
    env = env if env is not None else ToolEnvImpl()
    registry = FakeRegistry(results)
    stats = FakeStats()
    console = FakeConsole(quiet)
    interaction = FakeInteraction(confirm)
    interrupt = FakeInterrupt()
    ai_options = SimpleNamespace(
        retry_count=retry_count, thinking_effort="high",
        thinking_options={"high": "高"},
    )
    llm = FakeLLM(scripts)
    executor = pool if pool is not None else ThreadPoolExecutor(max_workers=4)
    dispatcher = ToolDispatcher(
        registry, env, executor, console, theme=theme,
        interrupt=interrupt, resolve_device=resolve_device, logger=logger,
    )
    loop = ConversationLoop(
        llm=llm, store=store, dispatcher=dispatcher, env=env, stats=stats,
        interaction=interaction, console=console, ai_options=ai_options,
        data_dir=data_dir, background=background, compression=compression,
        ratio=ratio, logger=logger,
    )
    parts = SimpleNamespace(
        loop=loop, store=store, env=env, registry=registry, stats=stats,
        console=console, interaction=interaction, interrupt=interrupt, llm=llm,
        dispatcher=dispatcher, executor=executor,
    )
    return loop, parts


def tool_call(tc_id: str, name: str, **arguments) -> dict:
    """构造一条 LLM 工具调用（参数 JSON 字符串形态）。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def plain_round(text: str) -> list[dict]:
    """一轮纯文本回复的事件序列（含用量）。"""
    return [
        {"content": text},
        {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 50}},
        {"finish_reason": "stop"},
    ]


def tool_round(tc_id: str, name: str, **arguments) -> list[dict]:
    """一轮工具调用回复的事件序列（带思考、签名与用量）。"""
    return [
        {
            "tool_calls": [tool_call(tc_id, name, **arguments)],
            "finish_reason": "tool_calls",
            "thinking": "思考内容",
            "thinking_signature": "签名",
        },
        {"usage": {"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 50}},
    ]


@pytest.fixture(scope="module")
def pool():
    """共享线程池（调度器执行面；模块结束后关闭）。"""
    executor = ThreadPoolExecutor(max_workers=4)
    yield executor
    executor.shutdown(wait=False)


def user_messages(store: MessageStore) -> list[str]:
    """当前历史中的用户消息文本。"""
    return [m.get("content") or "" for m in store.view() if m.get("role") == "user"]


def assistant_messages(store: MessageStore) -> list[dict]:
    """当前历史中的 assistant 消息。"""
    return [m for m in store.view() if m.get("role") == "assistant"]


# ═══════════════════════════════════════════════════════════════
# Requirement: 内循环轮转与事件流消费
# ═══════════════════════════════════════════════════════════════


def test_s01_tool_round_closes_loop(pool):
    """工具轮形成闭环：assistant 先入历史 → 结果逐条回传 → 自动下一轮请求。"""
    ratio = FakeRatio()
    scripts = [tool_round("c1", "Read", file_path="a.txt"), plain_round("完成")]
    loop, parts = build_loop(scripts, results={"Read": ToolResult(llm_text="文件内容")},
                             ratio=ratio, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assistants = assistant_messages(parts.store)
    assert assistants[0]["tool_calls"][0]["id"] == "c1"
    assert assistants[0]["thinking"] == "思考内容"
    assert assistants[0]["thinking_signature"] == "签名"
    assert assistants[0]["content"] is None
    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_messages] == [("c1", "文件内容")]
    assert parts.llm.calls == 2
    assert outcome.completed and outcome.content == "完成"
    assert parts.stats.updates  # 工具轮用量到达时更新统计与占比
    assert parts.stats.updates[0]["prompt_tokens"] == 100
    assert ratio.updates == [100, 100]  # 每轮 usage 到达时刷新窗口占比


def test_s02_repair_before_request(pool):
    """请求前修复：未获结果的工具调用先补齐结果、恢复合法序列。"""
    loop, parts = build_loop([plain_round("答")], pool=pool)
    parts.store.append_assistant(None, tool_calls=[tool_call("lost", "Read", file_path="a.txt")])
    loop.run(FakeSink())

    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["lost"]
    assert tool_messages[0]["content"] == "[用户中断]"


def test_s03_text_with_tool_calls_not_shown(pool):
    """带工具调用的文本不上屏、不计入本轮文本缓冲。"""
    events = [
        {
            "content": "不该显示的文本",
            "tool_calls": [tool_call("c1", "Read", file_path="a.txt")],
            "finish_reason": "tool_calls",
        }
    ]
    loop, parts = build_loop([events, plain_round("收尾")], pool=pool)
    sink = FakeSink()
    loop.run(sink)

    assert "不该显示的文本" not in "".join(sink.fed)
    assert "不该显示的文本" not in "".join(sink.notified)
    assert assistant_messages(parts.store)[0]["content"] is None


def test_s04_retry_notice_passthrough(pool):
    """重试提示直通终端（notify），不写入对话历史。"""
    notice = "\n⚠ 请求被限流(429)，约1s后自动重试（第1/3次）…\n"
    script = [{"retry_notice": notice}, {"content": "答"}, {"finish_reason": "stop"}]
    loop, parts = build_loop([script], pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert notice in sink.notified
    assert notice not in "\n".join(
        str(m.get("content") or "") for m in parts.store.view()
    )
    assert outcome.completed


# ═══════════════════════════════════════════════════════════════
# Requirement: 对话出口
# ═══════════════════════════════════════════════════════════════


def test_s05_plain_text_completion(pool):
    """纯文本完成：文本写入历史、记为正常完成、按统计栏规则收尾。"""
    loop, parts = build_loop([plain_round("答复正文")], pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert assistant_messages(parts.store)[-1]["content"] == "答复正文"
    assert "thinking" not in assistant_messages(parts.store)[-1]  # 纯文本轮不回传思考
    assert sink.finish_calls[-1][1] is True
    assert outcome.status == TURN_COMPLETED
    assert outcome.completed and not outcome.interrupted


def test_s06_error_exit_no_trace(pool):
    """错误出口不留痕：不向历史写入任何消息，直接收尾。"""
    script = [{"content": "[错误: API调用失败]", "finish_reason": "error"}]
    loop, parts = build_loop([script], pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert assistant_messages(parts.store) == []
    assert len(sink.finish_calls) == 1
    assert outcome.status == TURN_ERROR and not outcome.completed


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("stop", EMPTY_REPLY_TEXTS["stop"]),
        ("max_tokens", EMPTY_REPLY_TEXTS["max_tokens"]),
        ("content_filter", EMPTY_REPLY_TEXTS["content_filter"]),
        ("server_busy", EMPTY_REPLY_TEXTS["server_busy"]),
        ("stream_interrupted", EMPTY_REPLY_TEXTS["stream_interrupted"]),
        ("weird", "⚠ AI 返回异常（weird），请稍后重试。"),
    ],
)
def test_s07_empty_reply_texts(pool, tmp_path, reason, expected):
    """空回复分类提示 + 请求与响应落盘调试文件（时间戳精确到秒）。"""
    loop, parts = build_loop([[{"finish_reason": reason}]], pool=pool,
                             data_dir=str(tmp_path))
    sink = FakeSink()
    outcome = loop.run(sink)

    files = list(tmp_path.glob("debug_empty_*.json"))
    assert len(files) == 1
    dumped = json.loads(files[0].read_text(encoding="utf-8"))
    assert dumped["response"]["parsed_finish_reason"] == reason
    assert dumped["response"]["raw_sse_lines"] == []
    assert expected in sink.text
    assert "调试日志已写入" in parts.console.joined
    assert outcome.status == TURN_EMPTY


def test_s07b_empty_debug_filename_second_precision(pool, tmp_path, monkeypatch):
    """调试文件时间戳精确到秒：同一秒内两次空回复互相覆盖（兼容怪癖保持）。"""
    fake_time = SimpleNamespace(
        strftime=lambda fmt, *args: ("20240101_120000" if fmt == "%Y%m%d_%H%M%S"
                                     else "2024-01-01 12:00:00"),
    )
    monkeypatch.setattr("narnat_agent.conversation.loop.time", fake_time)
    loop, parts = build_loop([[{"finish_reason": "stop"}]], pool=pool,
                             data_dir=str(tmp_path))
    loop.run(FakeSink())
    loop.run(FakeSink())

    files = list(tmp_path.glob("debug_empty_*.json"))
    assert [path.name for path in files] == ["debug_empty_20240101_120000.json"]


# ═══════════════════════════════════════════════════════════════
# Requirement: 工具调度分组与结果回传
# ═══════════════════════════════════════════════════════════════


def test_s08_readonly_parallel(pool):
    """只读并行：两个只读工具同时在途，结果按原始顺序回传。"""
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(name, arguments):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.1)
        with lock:
            active["now"] -= 1
        return ToolResult(llm_text=f"{name} done")

    calls = [tool_call("a", "Read", file_path="a.txt"),
             tool_call("b", "Grep", pattern="x")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Read": slow, "Grep": slow}, pool=pool,
    )
    loop.run(FakeSink())

    assert active["peak"] == 2
    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["a", "b"]


def test_s09_same_file_write_serial(pool):
    """同文件写入串行、不同文件写入可并行。"""
    log: list[tuple[str, str]] = []
    lock = threading.Lock()

    def write_tool(name, arguments):
        path = arguments.get("file_path", "?")
        with lock:
            log.append(("start", path))
        time.sleep(0.05)
        with lock:
            log.append(("end", path))
        return ToolResult(llm_text=f"wrote {path}")

    calls = [tool_call("w1", "Edit", file_path="same.txt"),
             tool_call("w2", "Edit", file_path="same.txt"),
             tool_call("w3", "Write", file_path="other.txt")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Edit": write_tool, "Write": write_tool}, pool=pool,
    )
    loop.run(FakeSink())

    same_events = [entry for entry in log if entry[1] == "same.txt"]
    assert same_events == [("start", "same.txt"), ("end", "same.txt"),
                           ("start", "same.txt"), ("end", "same.txt")]


def test_s10_serial_group_sequential(pool):
    """串行组逐个执行：命令类工具并发峰值恒为 1。"""
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(name, arguments):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return ToolResult(llm_text=f"{name} done")

    calls = [tool_call("t1", "Shell", command="dir"),
             tool_call("t2", "Terminal", action="status"),
             tool_call("t3", "mcp__demo__ping", x=1)]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Shell": slow, "Terminal": slow, "mcp__demo__ping": slow}, pool=pool,
    )
    loop.run(FakeSink())

    assert active["peak"] == 1
    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["t1", "t2", "t3"]


def test_s11_results_order_and_missing(pool):
    """结果顺序与缺失：取消后仅已完成的结果按原始顺序返回，缺失条目留给上层补记。

    调度层直接驱动：只读工具先完成并被收下；串行工具执行中取消，其结果不返回，
    后续工具不再执行。
    """
    def tool(name, arguments):
        if name == "Shell":
            sink.cancelled = True  # 模拟执行中用户取消
        return ToolResult(llm_text=f"{name} done")

    calls = [tool_call("a", "Read", file_path="a.txt"),
             tool_call("s", "Shell", command="dir"),
             tool_call("s2", "Shell", command="echo")]
    sink = FakeSink()
    _loop, parts = build_loop([], results={"Read": tool, "Shell": tool}, pool=pool)

    results = parts.dispatcher.execute(calls, sink)

    assert [tc_id for tc_id, _ in results] == ["a"]
    assert results[0][1].llm_text == "Read done"
    assert [name for name, _ in parts.registry.calls] == ["Read", "Shell"]  # 取消后不再提交
    assert parts.interrupt.raised >= 1  # 取消时触发中断广播（杀进程由订阅者执行）


def test_s11b_interrupt_after_tool_results_discards_them(pool):
    """工具执行返回后取消：已完成结果不落历史，未获结果的调用由上层补记。

    与旧实现等价：完成标记与取消同帧时，结果不单独回填，消息序列由 repair 兜底。
    """
    sinks: dict[str, FakeSink] = {}
    lock = threading.Lock()

    def tool(name, arguments):
        with lock:
            sinks["sink"].cancelled = True
        time.sleep(0.05)
        return ToolResult(llm_text="done")

    calls = [tool_call("a", "Read", file_path="a.txt"),
             tool_call("b", "Read", file_path="b.txt")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Read": tool}, pool=pool,
    )
    sink = FakeSink()
    sinks["sink"] = sink
    outcome = loop.run(sink)

    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    ids = [m["tool_call_id"] for m in tool_messages]
    assert ids == [i for i in ["a", "b"] if i in ids]  # 顺序保持，缺失只在尾部
    assert all(m["content"] == "[用户中断]" for m in tool_messages)
    assert outcome.interrupted
    assert parts.interrupt.raised >= 1


def test_dispatch_results_keep_original_order(pool):
    """结果顺序：先提交的慢工具仍先于后提交的快工具出现在结果中。"""
    def tool(name, arguments):
        if arguments.get("file_path") == "slow.txt":
            time.sleep(0.1)
        return ToolResult(llm_text=str(arguments.get("file_path")))

    calls = [tool_call("s", "Read", file_path="slow.txt"),
             tool_call("f", "Read", file_path="fast.txt")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Read": tool}, pool=pool,
    )
    loop.run(FakeSink())

    tool_messages = [m for m in parts.store.view() if m.get("role") == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_messages] == [
        ("s", "slow.txt"), ("f", "fast.txt")]


def test_s12_execution_exception_two_forms(pool):
    """执行异常：只读阶段带工具名、写入与串行阶段不带（兼容怪癖保持）。"""
    def boom(name, arguments):
        raise RuntimeError("boom")

    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("r", "Read", file_path="a.txt")],
              "finish_reason": "tool_calls"}],
            [{"tool_calls": [tool_call("s", "Shell", command="x")],
              "finish_reason": "tool_calls"}],
            plain_round("ok"),
        ],
        results={"Read": boom, "Shell": boom}, pool=pool,
    )
    sink = FakeSink()
    loop.run(sink)

    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages[0] == "[错误: 工具执行失败(Read): boom]"
    assert tool_messages[1] == "[错误: 工具执行失败: boom]"
    assert parts.console.joined.count("失败]") == 2
    assert "[读取失败]" in parts.console.joined
    assert "[执行命令失败]" in parts.console.joined


# ═══════════════════════════════════════════════════════════════
# Requirement: 计划优先拦截
# ═══════════════════════════════════════════════════════════════


def plan_env(require_plan: bool = True, min_tools: int = 2):
    """带计划优先配置的工具环境。"""
    return ToolEnvImpl(settings=ToolSettingsImpl(require_plan=require_plan,
                                                  min_tools=min_tools))


def test_s13_plan_block_hits(pool):
    """满足条件拦截：整批回传提示与占位，被拦工具显示摘要与失败提示。"""
    env = plan_env()
    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("a", "Read", file_path="a.txt"),
                             tool_call("b", "Grep", pattern="x")],
              "finish_reason": "tool_calls"}],
            plain_round("ok"),
        ],
        env=env, pool=pool,
    )
    loop.run(FakeSink())

    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages[0] == (
        "[计划优先模式已开启: 请先使用TodoWrite制定计划（至少1项in_progress），"
        "再执行其他工具。当前尝试调用的工具: Read, Grep]"
    )
    assert tool_messages[1] == "[计划优先拦截，详见上方]"
    assert parts.registry.calls == []  # 未真正执行任何工具
    assert "[读取失败]" in parts.console.joined
    assert "[搜索内容失败]" in parts.console.joined


def test_s14_plan_block_contains_plan_tool(pool):
    """批内含计划工具放行（计划工具在串行组中最先执行）。"""
    env = plan_env()
    calls = [tool_call("a", "Read", file_path="a.txt"),
             tool_call("b", "Grep", pattern="x"),
             tool_call("t", "TodoWrite", todos=[{"content": "事", "status": "in_progress"}])]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        env=env, pool=pool,
    )
    loop.run(FakeSink())

    executed = [name for name, _ in parts.registry.calls]
    assert sorted(executed[:2]) == ["Grep", "Read"]  # 只读阶段并行
    assert executed[2] == "TodoWrite"  # 计划工具在串行组里最先执行


def test_s15_plan_block_active_plan(pool):
    """已有进行中计划放行。"""
    env = plan_env()
    env.plan.replace([{"content": "进行中", "status": "in_progress"}])
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("a", "Read", file_path="a.txt"),
                          tool_call("b", "Grep", pattern="x")],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        env=env, pool=pool,
    )
    loop.run(FakeSink())

    assert [name for name, _ in parts.registry.calls] == ["Read", "Grep"]


def test_s16_plan_block_below_threshold(pool):
    """非计划工具数量低于门槛放行。"""
    env = plan_env(min_tools=3)
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("a", "Read", file_path="a.txt"),
                          tool_call("b", "Grep", pattern="x")],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        env=env, pool=pool,
    )
    loop.run(FakeSink())

    assert [name for name, _ in parts.registry.calls] == ["Read", "Grep"]


# ═══════════════════════════════════════════════════════════════
# Requirement: 用户中断协调
# ═══════════════════════════════════════════════════════════════


def test_s17_interrupt_during_consume(pool):
    """流消费中中断：已产出文本先入历史、结束流并提示打断、不计正常完成。"""
    script = [{"content": "部分文本"}, {"content": "更多"}, {"finish_reason": "stop"}]
    loop, parts = build_loop([script], pool=pool)
    sink = FakeSink()
    parts.llm.after_yield = lambda call, position: setattr(sink, "cancelled", True) \
        if (call, position) == (0, 0) else None
    outcome = loop.run(sink)

    assert assistant_messages(parts.store)[-1]["content"] == "部分文本"
    assert sink.aborted and parts.interaction.interrupted_calls == 1
    assert outcome.status == TURN_INTERRUPTED and not outcome.completed


def test_s18_interrupt_during_tools(pool):
    """工具执行中中断：停止后续工具、为未获结果的调用补记 [用户中断]。"""
    sinks: dict[str, FakeSink] = {}
    lock = threading.Lock()

    def slow(name, arguments):
        with lock:
            sinks["sink"].cancelled = True
        return ToolResult(llm_text="done")

    calls = [tool_call("s1", "Shell", command="a"),
             tool_call("s2", "Shell", command="b")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        results={"Shell": slow}, pool=pool,
    )
    sink = FakeSink()
    sinks["sink"] = sink
    outcome = loop.run(sink)

    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages == ["[用户中断]", "[用户中断]"]
    assert [name for name, _ in parts.registry.calls] == ["Shell"]  # 后续工具不再执行
    assert sinks["sink"].aborted and parts.interaction.interrupted_calls == 1
    assert outcome.status == TURN_INTERRUPTED


def test_s19_interrupt_during_backoff(pool, monkeypatch):
    """退避等待中中断：立即放弃重试、结束流并提示打断。"""
    loop, parts = build_loop([[{"stream_interrupted": {"kind": "network", "detail": "x"}}]],
                             pool=pool)
    sink = FakeSink()
    saw = {}

    def fake_sleep(attempt, cancel_check=None):
        saw["attempt"] = attempt
        sink.cancelled = True  # 等待期间用户取消
        return not (cancel_check and cancel_check())

    monkeypatch.setattr("narnat_agent.conversation.loop.retry_sleep", fake_sleep)
    outcome = loop.run(sink)

    assert saw["attempt"] == 0
    assert sink.restart_calls == 1  # 重试前已重置渲染缓冲
    assert sink.aborted and parts.interaction.interrupted_calls == 1
    assert outcome.status == TURN_INTERRUPTED


# ═══════════════════════════════════════════════════════════════
# Requirement: 删除确认挂起（非 Windows）
# ═══════════════════════════════════════════════════════════════


def pending_delete_env() -> ToolEnvImpl:
    """删除确认挂起的工具环境（工具侧已暂存调用）。"""
    env = ToolEnvImpl()
    env.delete_gate.pend("Shell", {"command": "rm -rf x"})
    return env


def test_s20_delete_confirm_approved(pool):
    """确认后重新执行：置跳过确认标志、结果回传、新流继续内循环。

    确认引发的中间结束不显示统计栏（统计栏结案信号场景）。
    """
    env = pending_delete_env()
    await_result = ToolResult(llm_text=AWAIT_CONFIRM, await_confirm=True)
    rerun_result = ToolResult(llm_text="删除完成", ui_text="+ 已删除")
    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("d1", "Shell", command="rm -rf x")],
              "finish_reason": "tool_calls"}],
            plain_round("继续"),
        ],
        env=env, results={"Shell": [await_result, rerun_result]}, confirm=True,
        pool=pool,
    )
    sink = FakeSink()
    outcome = loop.run(sink)

    # 确认提示符与重新执行（同一命令、跳过确认）
    assert parts.interaction.confirm_prompts == [DELETE_CONFIRM_PROMPT]
    assert parts.env.delete_gate.consume_confirmed() is True
    assert [name for name, _ in parts.registry.calls] == ["Shell", "Shell"]
    assert parts.registry.calls[-1][1] == {"command": "rm -rf x"}
    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages == ["删除完成"]
    # 中间结束不显示统计栏；新流继续内循环后正常收尾
    assert sink.finish_calls[-1][1] is False
    assert len(parts.interaction.sinks) == 1
    assert outcome.completed and outcome.content == "继续"
    assert "  + 已删除" in parts.console.joined


def test_s21_delete_confirm_rejected(pool):
    """取消：回传固定取消文案，命令不执行，内循环以新流继续。"""
    env = pending_delete_env()
    await_result = ToolResult(llm_text=AWAIT_CONFIRM, await_confirm=True)
    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("d1", "Shell", command="rm -rf x")],
              "finish_reason": "tool_calls"}],
            plain_round("收到取消"),
        ],
        env=env, results={"Shell": await_result}, confirm=False, pool=pool,
    )
    sink = FakeSink()
    outcome = loop.run(sink)

    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages == [DELETE_CANCELLED_TEXT]
    assert parts.env.delete_gate.consume_confirmed() is False
    assert len(parts.interaction.sinks) == 1
    assert outcome.completed


def test_s22_delete_confirm_empty_input(pool):
    """无输入按取消（读取返回空，含无交互环境）：行为与输入 n 相同。"""
    env = pending_delete_env()
    await_result = ToolResult(llm_text=AWAIT_CONFIRM, await_confirm=True)
    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("d1", "Shell", command="rm -rf x")],
              "finish_reason": "tool_calls"}],
            plain_round("好的"),
        ],
        env=env, results={"Shell": await_result}, confirm=False, pool=pool,
    )
    sink = FakeSink()
    loop.run(sink)

    assert parts.interaction.confirm_prompts == [DELETE_CONFIRM_PROMPT]
    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages == [DELETE_CANCELLED_TEXT]


def test_delete_confirm_pending_without_await_marker(pool):
    """挂起记录存在但结果中无 AWAIT 标记：按普通工具轮回填，不进入确认流程。"""
    env = pending_delete_env()
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("d1", "Shell", command="rm -rf x")],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        env=env, results={"Shell": ToolResult(llm_text="命令输出")}, pool=pool,
    )
    sink = FakeSink()
    loop.run(sink)

    assert parts.interaction.confirm_prompts == []
    tool_messages = [m["content"] for m in parts.store.view() if m.get("role") == "tool"]
    assert tool_messages == ["命令输出"]


# ═══════════════════════════════════════════════════════════════
# Requirement: 流中断整轮重试
# ═══════════════════════════════════════════════════════════════


def test_s23_stream_retry_success(pool, monkeypatch):
    """中断后重试成功：丢弃截断内容、重置渲染缓冲、提示退避信息后重发。"""
    monkeypatch.setattr("narnat_agent.conversation.loop.retry_sleep",
                        lambda attempt, cancel_check=None: True)
    scripts = [
        [{"content": "截断内容"}],  # 无完成标记
        plain_round("重试成功"),
    ]
    loop, parts = build_loop(scripts, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert sink.restart_calls == 1
    assert "约1s后自动重试（第1/3次）" in sink.text
    assert parts.llm.calls == 2
    assert assistant_messages(parts.store)[-1]["content"] == "重试成功"
    assert all(m["content"] != "截断内容" for m in assistant_messages(parts.store))
    assert outcome.completed
    assert parts.llm.retry_counts == [3]


def test_s24_stream_retry_exhausted(pool, monkeypatch):
    """重试耗尽：提示已自动重试 N 次仍失败并收尾，截断内容不写入历史。"""
    monkeypatch.setattr("narnat_agent.conversation.loop.retry_sleep",
                        lambda attempt, cancel_check=None: True)
    scripts = [[{"content": "截断"}]]
    loop, parts = build_loop(scripts, retry_count=1, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert parts.llm.calls == 2  # 首发 + 1 次重试
    assert "已自动重试1次仍失败" in sink.text
    assert assistant_messages(parts.store) == []
    assert outcome.status == TURN_ERROR


def test_s25_retry_budget_reset_after_completion(pool, monkeypatch):
    """完成标记重置预算：收到正常结束标记后再次中断，从第 1 次重新开始。"""
    monkeypatch.setattr("narnat_agent.conversation.loop.retry_sleep",
                        lambda attempt, cancel_check=None: True)
    scripts = [
        [{"content": "截断一"}],
        tool_round("c1", "Read", file_path="a.txt"),
        [{"content": "截断二"}],
        plain_round("done"),
    ]
    loop, parts = build_loop(scripts, results={"Read": ToolResult(llm_text="ok")},
                             retry_count=1, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert sink.text.count("第1/1次") == 2
    assert outcome.completed
    assert parts.llm.calls == 4


# ═══════════════════════════════════════════════════════════════
# Requirement: 上下文压力应对
# ═══════════════════════════════════════════════════════════════


def test_s26_overflow_recover_success(pool):
    """首次溢出恢复成功：自动压缩历史后重发同一轮请求。"""
    compression = FakeCompression(ok=True)
    scripts = [[{"finish_reason": "context_overflow"}], plain_round("恢复成功")]
    loop, parts = build_loop(scripts, compression=compression, ratio=FakeRatio(), pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert compression.compress_calls == 1
    assert sink.restart_calls == 1
    assert "上下文超限，正在自动压缩历史" in sink.text
    assert "压缩完成，重试请求" in sink.text
    assert outcome.completed and parts.llm.calls == 2


def test_s27_overflow_again_give_up(pool):
    """再次溢出放弃：不再压缩，提示超出上下文限制与降级建议并收尾。"""
    compression = FakeCompression(ok=True)
    scripts = [
        [{"finish_reason": "context_overflow"}],
        [{"finish_reason": "context_overflow"}],
    ]
    loop, parts = build_loop(scripts, compression=compression, ratio=FakeRatio(), pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert compression.compress_calls == 1
    assert "请求超出模型上下文限制" in sink.text
    assert "/save 保存会话后开启新对话" in sink.text
    assert outcome.status == TURN_ERROR


def test_s28_overflow_without_compression(pool):
    """压缩能力未注入：直接提示超出上下文限制与降级建议并收尾。"""
    scripts = [[{"finish_reason": "context_overflow"}]]
    loop, parts = build_loop(scripts, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert "请求超出模型上下文限制" in sink.text
    assert outcome.status == TURN_ERROR


@pytest.mark.parametrize("guard_ok", [True, False])
def test_s29_mid_run_guard(pool, guard_ok):
    """运行中自查：占比达阈值时先提示并压缩（成功/失败各有提示），再继续请求。"""
    compression = FakeCompression(ok=guard_ok)
    loop, parts = build_loop([plain_round("答")], compression=compression,
                             ratio=FakeRatio(need=True), pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert compression.mid_run_calls == 1
    assert "上下文占比超阈值，正在自动压缩历史" in sink.text
    if guard_ok:
        assert "压缩完成，继续任务" in sink.text
    else:
        assert "自动压缩失败，继续尝试本次请求" in sink.text
    assert outcome.completed


def test_mid_run_guard_skipped_below_threshold(pool):
    """占比未达阈值：不做压缩、不发提示，直接发请求。"""
    compression = FakeCompression(ok=True)
    loop, parts = build_loop([plain_round("答")], compression=compression,
                             ratio=FakeRatio(need=False), pool=pool)
    sink = FakeSink()
    loop.run(sink)

    assert compression.mid_run_calls == 0
    assert "上下文占比超阈值" not in sink.text


# ═══════════════════════════════════════════════════════════════
# Requirement: 收尾软提醒
# ═══════════════════════════════════════════════════════════════


def test_s30_plan_reminder(pool):
    """计划未勾选提醒：置位标志并注入提醒消息，内循环继续（终端不显示）。"""
    env = ToolEnvImpl()
    env.plan.replace([
        {"content": "任务甲", "status": "completed"},
        {"content": "任务乙", "status": "pending"},
        {"content": "任务丙", "status": "in_progress"},
    ])
    loop, parts = build_loop([plain_round("第一轮"), plain_round("补勾后")], env=env,
                             pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    users = user_messages(parts.store)
    assert users == [plan_reminder_text([
        {"content": "任务乙", "status": "pending"},
        {"content": "任务丙", "status": "in_progress"},
    ])]
    assert users[0].startswith("[系统提醒]")
    assert "终端" not in sink.text and "[系统提醒]" not in sink.text
    assert parts.llm.calls == 2
    assert outcome.completed and outcome.content == "补勾后"


def test_plan_reminder_truncates_after_five(pool):
    """计划提醒最多列出前 5 项，多于 5 项时附「等共 N 项」。"""
    env = ToolEnvImpl()
    todos = [{"content": f"任务{i}", "status": "pending"} for i in range(1, 8)]
    env.plan.replace(todos)
    loop, parts = build_loop([plain_round("第一轮"), plain_round("结束")], env=env, pool=pool)
    loop.run(FakeSink())

    text = user_messages(parts.store)[0]
    assert "任务1、任务2、任务3、任务4、任务5等共7项" in text
    assert PLAN_REMINDER_TEMPLATE.format(names="任务1、任务2、任务3、任务4、任务5",
                                         tail="等共7项") == text


def test_s31_bg_reminder(pool):
    """后台任务提醒：置位标志并注入提醒消息，内循环继续。"""
    background = FakeBackground(count=1, summary="bg1: 构建中")
    loop, parts = build_loop([plain_round("第一轮"), plain_round("处理完")],
                             background=background, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    users = user_messages(parts.store)
    assert users == [BG_REMINDER_TEMPLATE.format(summary="bg1: 构建中")]
    assert parts.llm.calls == 2
    assert outcome.completed


def test_s32_reminder_once(pool):
    """提醒只触发一次：标志已置位且条件再次满足时不再注入，直接正常收尾。"""
    env = ToolEnvImpl()
    env.plan.replace([{"content": "任务乙", "status": "pending"}])
    env.reminders.try_trigger_plan()  # 预先置位（等价于上一次运行已提醒过）
    loop, parts = build_loop([plain_round("收尾")], env=env, pool=pool)
    sink = FakeSink()
    outcome = loop.run(sink)

    assert user_messages(parts.store) == []
    assert parts.llm.calls == 1
    assert outcome.completed


def test_reminder_state_not_touched_when_no_condition(pool):
    """无未完成项 / 无后台任务时不消耗提醒标志（各自独立计数）。"""
    env = ToolEnvImpl()
    env.plan.replace([{"content": "任务甲", "status": "completed"}])
    background = FakeBackground(count=0, summary="")
    loop, parts = build_loop([plain_round("收尾")], env=env, background=background, pool=pool)
    loop.run(FakeSink())

    assert env.reminders.try_trigger_plan() is True
    assert env.reminders.try_trigger_bg() is True


# ═══════════════════════════════════════════════════════════════
# Requirement: 统计栏结案信号
# ═══════════════════════════════════════════════════════════════


def test_s33_stats_shown_normal_mode(pool):
    """普通模式每轮显示统计栏。"""
    loop, parts = build_loop([plain_round("答")], pool=pool)
    sink = FakeSink()
    loop.run(sink)

    stats, with_stats = sink.finish_calls[-1]
    assert with_stats is True
    assert stats.thinking_effort == "高"
    assert stats.input_tokens == 100 and stats.output_tokens == 10


def test_s34_stats_hidden_goal_middle(pool):
    """目标模式中间轮静默（AI 未声明完成且非强制收尾轮）。"""
    loop, parts = build_loop([plain_round("答")], pool=pool)
    sink = FakeSink()
    loop.run(sink, goal_mode=True)

    assert sink.finish_calls[-1][1] is False


def test_s34b_stats_shown_when_goal_declared(pool):
    """目标模式下 AI 已声明完成（GoalComplete 置位）时显示统计栏。"""
    env = ToolEnvImpl()
    env.goal.mark()
    loop, parts = build_loop([plain_round("答")], env=env, pool=pool)
    sink = FakeSink()
    loop.run(sink, goal_mode=True)

    assert sink.finish_calls[-1][1] is True


def test_s34c_stats_shown_in_force_final(pool):
    """强制收尾轮（force_final）正常完成时显示统计栏。"""
    loop, parts = build_loop([plain_round("答")], pool=pool)
    sink = FakeSink()
    loop.run(sink, goal_mode=True, force_final=True)

    assert sink.finish_calls[-1][1] is True


def test_s35_stats_suppressed_by_bg(pool):
    """后台任务抑制统计栏：不显示统计栏，终端提示数量与处理方式。"""
    background = FakeBackground(count=2, summary="bg1: 跑着")
    env = ToolEnvImpl()
    env.reminders.try_trigger_bg()  # 提醒已触发过 → 本轮直接收尾
    loop, parts = build_loop([plain_round("答")], env=env, background=background, pool=pool)
    sink = FakeSink()
    loop.run(sink)

    assert sink.finish_calls[-1][1] is False
    assert "后台 2 个任务仍在运行" in parts.console.joined
    assert 'Shell(bg="wait")' in parts.console.joined


# ═══════════════════════════════════════════════════════════════
# Requirement: 目标模式自动续跑与强制收尾轮
# ═══════════════════════════════════════════════════════════════


def goal_mode(default_rounds: int = 100):
    """构造目标模式状态机与完成标记。"""
    goal_state = GoalStateImpl()
    return GoalMode(goal_state, default_rounds), goal_state


def test_s37_goal_declared_complete(pool):
    """声明完成结束续跑：完成标记复位，不再续跑。"""
    mode, goal_state = goal_mode()
    mode.start("任务", 10)
    goal_state.mark()

    decision = mode.after_round(TurnOutcome(TURN_COMPLETED, "答"))
    assert decision.ends
    assert goal_state.is_set is False


def test_s38_goal_continue(pool):
    """未完成自动续跑：注入续跑提示（文案精确）、轮计数加一。"""
    mode, _goal_state = goal_mode()
    mode.start("修好构建", 10)

    decision = mode.after_round(TurnOutcome(TURN_COMPLETED, "答"))
    assert decision.continues
    assert decision.text == CONTINUE_TEMPLATE.format(rounds=1, task="修好构建")
    assert decision.text == (
        "【自动续跑】已完成1轮，任务：修好构建\n"
        "请继续推进任务。若任务已完成，请调用GoalComplete工具声明完成。"
    )
    assert mode.rounds == 1


def test_s39_goal_round_limit(pool):
    """达到轮数上限收尾：注入收尾指令（文案精确），由主循环执行强制收尾轮。"""
    mode, _goal_state = goal_mode()
    mode.start("任务", 2)
    assert mode.after_round(TurnOutcome(TURN_COMPLETED, "一")).continues

    decision = mode.after_round(TurnOutcome(TURN_COMPLETED, "二"))
    assert decision.needs_final_round
    assert decision.text == FINAL_ROUND_TEMPLATE.format(limit=2)
    assert decision.text == (
        "【系统提示】目标模式已达到轮数上限（2轮），任务尚未完成。"
        "请向用户总结当前进度、已完成工作和未完成原因，无需继续执行新任务。"
    )


def test_s39b_goal_override_prefers_positive_value():
    """轮数上限取临时覆盖值（大于 0 时）或配置默认值。"""
    mode, _goal_state = goal_mode(default_rounds=50)
    mode.start("任务", 0)
    assert mode.limit == 50
    mode.start("任务", 3)
    assert mode.limit == 3


@pytest.mark.parametrize("outcome", [
    TurnOutcome(TURN_INTERRUPTED, "部分"),
    TurnOutcome(TURN_ERROR, ""),
    TurnOutcome(TURN_EMPTY, ""),
])
def test_s40_goal_abnormal_no_continue(pool, outcome):
    """异常不续跑：中断、错误、空回复一律结束续跑。"""
    mode, _goal_state = goal_mode()
    mode.start("任务", 10)
    mode.after_round(TurnOutcome(TURN_COMPLETED, "一"))

    decision = mode.after_round(outcome)
    assert decision.ends and decision.text == ""
    assert mode.rounds == 2  # 每轮结束计数加一


def test_s40b_goal_inactive_ends(pool):
    """非目标模式：判定恒为结束且不计数。"""
    mode, _goal_state = goal_mode()
    decision = mode.after_round(TurnOutcome(TURN_COMPLETED, "答"))
    assert decision.ends and mode.rounds == 0


def test_s41_goal_negative_limit(pool):
    """上限为负立即收尾（兼容怪癖）：首轮结束后即触发收尾指令。"""
    mode, _goal_state = goal_mode()
    mode.start("任务", -1)

    decision = mode.after_round(TurnOutcome(TURN_COMPLETED, "一"))
    assert decision.needs_final_round
    assert decision.text == FINAL_ROUND_TEMPLATE.format(limit=-1)


# ═══════════════════════════════════════════════════════════════
# 调度分组策略单测（纯函数）
# ═══════════════════════════════════════════════════════════════


def test_group_tool_calls_three_phases():
    """分组结果：只读组、写入按文件分桶、其余进串行组（保原始下标）。"""
    parsed = [
        ("a", "Read", {"file_path": "a.txt"}),
        ("b", "Edit", {"file_path": "x.txt"}),
        ("c", "Shell", {"command": "dir"}),
        ("d", "Grep", {"pattern": "x"}),
        ("e", "Write", {"file_path": "x.txt"}),
        ("f", "Edit", {"file_path": "y.txt"}),
        ("g", "Unknown", {}),
    ]
    readonly, write_groups, serial = group_tool_calls(parsed)

    assert [entry[1] for entry in readonly] == ["a", "d"]
    assert list(write_groups) == ["x.txt", "y.txt"]
    assert [entry[1] for entry in write_groups["x.txt"]] == ["b", "e"]
    assert [entry[1] for entry in write_groups["y.txt"]] == ["f"]
    assert [entry[1] for entry in serial] == ["c", "g"]
    assert [entry[0] for entry in readonly] == [0, 3]
    assert [entry[0] for entry in serial] == [2, 6]


def test_group_tool_calls_plan_tool_first():
    """串行组内计划工具稳定排序到最前，其余保持原始顺序。"""
    parsed = [
        ("s1", "Shell", {"command": "a"}),
        ("s2", "Serial", {"action": "scan"}),
        ("p", "TodoWrite", {"todos": []}),
        ("s3", "Terminal", {"action": "status"}),
    ]
    _readonly, _write_groups, serial = group_tool_calls(parsed)

    assert [entry[1] for entry in serial] == ["p", "s1", "s2", "s3"]


def test_group_tool_calls_invalid_arguments_empty_args():
    """工具参数为非法 JSON 时按空参数执行（写入类归到空路径桶）。"""
    parsed = [parse_tool_call({
        "id": "x", "type": "function",
        "function": {"name": "Edit", "arguments": "{not json"},
    })]
    assert parsed == [("x", "Edit", {})]

    _readonly, write_groups, _serial = group_tool_calls(parsed)
    assert list(write_groups) == [""]


def test_parse_tool_call_keeps_arguments():
    """解析保留合法参数（JSON 对象）。"""
    parsed = parse_tool_call(tool_call("x", "Read", file_path="a.txt"))
    assert parsed == ("x", "Read", {"file_path": "a.txt"})


# ═══════════════════════════════════════════════════════════════
# 终端调度行（摘要 / 失败提示 / 差异格式 / 静默）
# ═══════════════════════════════════════════════════════════════


def test_dispatch_call_line_and_quiet(pool):
    """调用摘要行：标签加关键参数；静默模式下不输出任何调度行。"""
    loop, parts = build_loop([[{"tool_calls": [tool_call("a", "Shell", command="dir")],
                                "finish_reason": "tool_calls"}], plain_round("ok")],
                             pool=pool)
    loop.run(FakeSink())
    assert "  [执行命令] dir\n" in parts.console.joined

    parts2 = build_loop([[{"tool_calls": [tool_call("a", "Shell", command="dir")],
                           "finish_reason": "tool_calls"}], plain_round("ok")],
                        pool=pool, quiet=True)[1]
    parts2.loop.run(FakeSink())
    assert parts2.console.text == []


def test_dispatch_file_summary_hides_local_device(pool):
    """文件类工具摘要显示 `file_path`；注入设备解析器时远程设备以显示名呈现。"""
    calls = [tool_call("a", "Read", file_path="a.txt", device="dev1")]
    loop, parts = build_loop(
        [[{"tool_calls": calls, "finish_reason": "tool_calls"}], plain_round("ok")],
        pool=pool, resolve_device=lambda dev: "10.0.0.9",
    )
    loop.run(FakeSink())

    assert "  [读取] 10.0.0.9:a.txt\n" in parts.console.joined


def test_dispatch_plan_tool_summary_and_display(pool):
    """计划工具摘要为项数；其 ui_text 按差异规则逐行缩进渲染。"""
    env = ToolEnvImpl()
    todos = [{"content": "任务甲", "status": "in_progress"}]
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("t", "TodoWrite", todos=todos)],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        env=env, results={"TodoWrite": ToolResult(llm_text="清单", ui_text="● 任务甲")},
        pool=pool,
    )
    loop.run(FakeSink())

    assert "  [更新计划] 1项\n" in parts.console.joined
    assert "  ● 任务甲" in parts.console.joined


def test_dispatch_diff_tail_rules(pool):
    """差异输出格式：普通差异块后接空行；单行「[无差异]」直接接后续输出。"""
    loop, parts = build_loop(
        [
            [{"tool_calls": [tool_call("e1", "Edit", file_path="a.txt")],
              "finish_reason": "tool_calls"}],
            [{"tool_calls": [tool_call("e2", "Edit", file_path="a.txt")],
              "finish_reason": "tool_calls"}],
            plain_round("ok"),
        ],
        results={"Edit": [ToolResult(llm_text="ok", ui_text="[无差异]"),
                          ToolResult(llm_text="ok", ui_text="--- a\n+++ b")]},
        pool=pool,
    )
    loop.run(FakeSink())

    text = parts.console.joined
    assert "  [无差异]\n" in text
    assert "  [无差异]\n\n" not in text
    assert "  --- a\n  +++ b\n\n" in text


def test_dispatch_failure_line_forms(pool):
    """失败判定：命令类只认框架错误标签；其余工具以结果前缀判定。"""
    results = {
        "Shell": ToolResult(llm_text="[错误: 命令超时]", is_error=True),
        "Read": ToolResult(llm_text="[错误: 文件不存在]"),
    }
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("s", "Shell", command="x"),
                          tool_call("r", "Read", file_path="a.txt")],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        results=results, pool=pool,
    )
    loop.run(FakeSink())

    assert "[执行命令失败]" in parts.console.joined
    assert "[读取失败]" in parts.console.joined


def test_mcp_tool_label_in_summary(pool):
    """MCP 工具标签显示所属服务器，摘要为工具名加紧凑参数。"""
    loop, parts = build_loop(
        [[{"tool_calls": [tool_call("m", "mcp__demo__ping", path="/x")],
           "finish_reason": "tool_calls"}], plain_round("ok")],
        pool=pool,
    )
    loop.run(FakeSink())

    assert '  [MCP:demo] ping {"path":"/x"}\n' in parts.console.joined


# ═══════════════════════════════════════════════════════════════
# 结构检查：公开 API 与无私有互摸
# ═══════════════════════════════════════════════════════════════


def test_structure_turn_outcome_vocabulary():
    """TurnOutcome 四态取值与派生属性（主循环据此判定续跑与善后）。"""
    assert (TURN_COMPLETED, TURN_INTERRUPTED, TURN_ERROR, TURN_EMPTY) == (
        "completed", "interrupted", "error", "empty")
    completed = TurnOutcome(TURN_COMPLETED, "正文")
    assert completed.completed and not completed.interrupted
    assert not completed.failed and not completed.empty
    assert TurnOutcome(TURN_INTERRUPTED).interrupted
    assert TurnOutcome(TURN_ERROR).failed
    assert TurnOutcome(TURN_EMPTY).empty


def test_structure_last_content_is_public(pool):
    """异常路径经公开属性 `last_content` 取残留文本（替代私有互摸）。"""
    loop, parts = build_loop([[{"content": "残留文本"}, {"content": "后续"}]], pool=pool)

    def boom(call, position):
        if (call, position) == (0, 0):
            raise RuntimeError("boom")

    parts.llm.after_yield = boom
    with pytest.raises(RuntimeError):
        loop.run(FakeSink())
    assert loop.last_content == "残留文本"


def test_structure_no_legacy_private_names_in_source():
    """源码不含旧私有互摸名与旧私有字段赋值（报告证据：D7 消解的机械化验证）。"""
    banned = {"_last_round_ok", "_last_content_parts", "_delete_confirmed",
              "_tool_context", "_bg_reminded", "_todo_reminded"}
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not (names & banned), f"{path.name} 出现旧私有名: {names & banned}"


def test_structure_loop_has_no_app_level_state(pool):
    """内循环不持有跨轮状态：目标模式参数经形参传入、结局经返回值表达。"""
    import inspect

    signature = inspect.signature(ConversationLoop.run)
    assert list(signature.parameters) == ["self", "sink", "goal_mode", "force_final"]
    assert not hasattr(ConversationLoop, "_last_round_ok")
