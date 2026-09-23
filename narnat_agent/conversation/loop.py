"""对话内循环 —— LLM 事件消费、工具调度协调与单轮结局（TurnOutcome）。

契约来源：`openspec/changes/recast-v2/specs/conversation/spec.md`
（内循环轮转与事件流消费 / 对话出口 / 用户中断协调 / 删除确认挂起 / 流中断整轮重试 /
上下文压力应对 / 收尾软提醒 / 统计栏结案信号），事件形态与谓词取自
`contracts/llm_events.py`，输出与交互取自 `contracts/output.py`。

行为搬运自旧实现 `narnat_agent/core/agent_loop.py`（轮转、事件消费优先级、各出口
文案与副作用逐条等价），结构变化（design D4/D7）：
- 单轮结局以返回值 `TurnOutcome` 表达（正常完成 / 中断 / 错误 / 空回复 + 已产出
  内容），主循环不再读内循环的私有字段（`_last_round_ok` / `_last_content_parts`
  私有互摸消灭；异常路径经公开只读属性 `last_content` 取残留文本）；
- 主循环状态（目标模式参数）经 `run(goal_mode=…)` 形参传入，本类不持有跨轮状态；
- 输出经 `OutputSink` 端口：AI 文本走 `feed`，系统提示行走 `notify`
  （内容增量与提示行的落定/动画时序收进界面实现内部）；工具调度行与提示性旁注
  （后台任务、调试文件落点）经 `ConsolePort.write` 输出，静默判定归调度层；
- 中断经 `InterruptSignal` 总线；压缩能力（运行中自查与溢出恢复）构造注入，
  None 表示禁用。

依赖规则：本积木位于 L3，可依赖 L0-L2 积木；除 contracts 外只经结构化协议声明
依赖面（llm / compression / stats / console 等），不 import 具体实现类。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..contracts.llm_events import (
    KEY_CONTENT,
    KEY_RETRY_NOTICE,
    get_finish_reason,
    get_stream_interrupted,
    get_thinking,
    get_thinking_signature,
    get_tool_calls,
    get_usage,
    is_retry_notice,
    is_stream_interrupted,
    is_text_delta,
)
from ..contracts.output import InteractionPort, OutputSink, TurnStats
from ..contracts.tool import ToolEnv, ToolResult
from ..llm.retry import (
    DEFAULT_MAX_RETRIES,
    backoff_base,
    clamp_retry_count,
    retry_sleep,
)
from .dispatch import CancelProbe, ConsolePort, LogPort, ToolDispatcher
from .reminders import bg_reminder_text, plan_reminder_text, unfinished_items

__all__ = [
    "BG_RUNNING_NOTICE",
    "COMPACT_DONE_NOTICE",
    "COMPACT_FAILED_NOTICE",
    "COMPACT_RETRY_NOTICE",
    "DELETE_CANCELLED_TEXT",
    "DELETE_CONFIRM_PROMPT",
    "EMPTY_REPLY_TEXTS",
    "OVERFLOW_FAILED_NOTICE",
    "OVERFLOW_GIVEUP_NOTICE",
    "OVERFLOW_NOTICE",
    "OVER_RATIO_NOTICE",
    "TURN_COMPLETED",
    "TURN_EMPTY",
    "TURN_ERROR",
    "TURN_INTERRUPTED",
    "AIOptions",
    "BackgroundPort",
    "CompressionPort",
    "ConversationLoop",
    "HistoryPort",
    "LLMStreamSource",
    "RatioPort",
    "StatsPort",
    "TurnOutcome",
    "stream_interrupt_give_up_notice",
    "stream_interrupt_retry_notice",
]

# ═══════════════════════════════════════════════════════════════
# 单轮结局
# ═══════════════════════════════════════════════════════════════

TURN_COMPLETED = "completed"
"""本轮正常完成（无工具调用的纯文本轮收尾；目标模式续跑的唯一依据）。"""

TURN_INTERRUPTED = "interrupted"
"""用户中断（消费中 / 工具执行中 / 退避等待中取消；不计正常完成）。"""

TURN_ERROR = "error"
"""错误出口（结束标记为 error、流中断重试耗尽、溢出不可恢复；不写历史）。"""

TURN_EMPTY = "empty"
"""空回复（无文本产出；已按结束标记提示并落盘调试文件）。"""


@dataclass(frozen=True)
class TurnOutcome:
    """一次内循环调用的结局（主循环据此判定续跑与善后）。

    - `status`：`completed` / `interrupted` / `error` / `empty` 四态；
    - `content`：本轮已产出的文本（正常完成与消费中中断路径已同步写入历史；
      其余出口不写入，仅供主循环参考）。
    """

    status: str
    content: str = ""

    @property
    def completed(self) -> bool:
        """本轮是否正常完成（纯文本轮收尾）。"""
        return self.status == TURN_COMPLETED

    @property
    def interrupted(self) -> bool:
        """本轮是否被用户中断。"""
        return self.status == TURN_INTERRUPTED

    @property
    def failed(self) -> bool:
        """本轮是否以错误出口结束（不写历史）。"""
        return self.status == TURN_ERROR

    @property
    def empty(self) -> bool:
        """本轮是否为空回复。"""
        return self.status == TURN_EMPTY


# ═══════════════════════════════════════════════════════════════
# 已发布文案（逐字对齐旧实现，不得改写）
# ═══════════════════════════════════════════════════════════════

OVER_RATIO_NOTICE = "\n⚠ 上下文占比超阈值，正在自动压缩历史…\n"
COMPACT_DONE_NOTICE = "  压缩完成，继续任务…\n"
COMPACT_FAILED_NOTICE = "  自动压缩失败，继续尝试本次请求…\n"
OVERFLOW_NOTICE = "\n⚠ 上下文超限，正在自动压缩历史…\n"
COMPACT_RETRY_NOTICE = "  压缩完成，重试请求…\n"
OVERFLOW_GIVEUP_NOTICE = (
    "\n\n⚠ 请求超出模型上下文限制。"
    "请尝试缩减本次输入，或 /save 保存会话后开启新对话。\n"
)
OVERFLOW_FAILED_NOTICE = (
    "\n\n⚠ 自动压缩失败，请求仍超出模型上下文限制。"
    "请尝试缩减本次输入，或 /save 保存会话后开启新对话。\n"
)

BG_RUNNING_NOTICE = (
    "  ⚠ 后台 {count} 个任务仍在运行"
    "（Shell(bg=\"wait\") 等待 / Shell(bg=\"cancel\", id=N) 清理）\n"
)

DELETE_CONFIRM_PROMPT = "  确认执行此命令? [y/N]: "
"""删除确认提示符（非 Windows 挂起确认；已发布字面量）。"""

DELETE_CANCELLED_TEXT = "[操作已取消: 此命令需用户确认]"
"""取消确认时回传给模型的结果文本（已发布字面量）。"""

EMPTY_REPLY_TEXTS = {
    "stop": "⚠ AI 返回了空回复，请尝试缩短对话或稍后重试。",
    "max_tokens": "⚠ AI 思考超过了最大输出限制，请增大限制或缩短对话。",
    "content_filter": "⚠ AI 返回被安全策略拦截，请调整提问内容。",
    "server_busy": "⚠ 服务器繁忙，请稍后重试。",
    "error": "⚠ AI 调用出错，请查看上方错误信息。",
    "stream_interrupted": "⚠ 服务端响应流中断，请稍后重试。",
}
"""空回复按结束标记的固定提示（未知原因用通用失败文案）。"""


def stream_interrupt_retry_notice(base: int, attempt: int, limit: int) -> str:
    """流中断自动重试的提示（含退避秒数与「第 N/上限 次」）。"""
    return (
        f"\n⚠ 服务端响应流中断，约{base}s后自动重试"
        f"（第{attempt}/{limit}次）…\n"
    )


def stream_interrupt_give_up_notice(limit: int) -> str:
    """流中断重试耗尽的提示。"""
    return f"\n\n⚠ 服务端响应流中断，已自动重试{limit}次仍失败，请稍后重试。\n"


# ═══════════════════════════════════════════════════════════════
# 依赖端口（结构化协议：实现方在装配点注入）
# ═══════════════════════════════════════════════════════════════


class LLMStreamSource(Protocol):
    """LLM 流式接口面（实现方：`llm.LLMClient`）。"""

    def chat_stream(self, messages: list[dict], no_tools: bool = False,
                    no_thinking: bool = False, cancel_check=None) -> Iterator[dict]:
        """发起流式请求，返回事件流生成器（形态见 contracts.llm_events）。"""
        ...

    def set_retry_count(self, n: int) -> None:
        """同步重试上限（网络层与流中断重试共用同一配置来源）。"""
        ...

    @property
    def raw_sse(self) -> list[str] | None:
        """最近一轮的原始 SSE 数据段（空回复调试文件用；其他协议为 None）。"""
        ...


class HistoryPort(Protocol):
    """对话历史面（实现方：`messages.MessageStore`）。"""

    def view(self):
        """取只读实时视图（`to_list()` 导出浅拷贝）。"""
        ...

    def repair(self) -> bool:
        """修复被中断留下的不完整消息序列（每次请求前调用）。"""
        ...

    def append_user(self, content: str) -> None:
        """追加用户消息（用户输入或系统提醒）。"""
        ...

    def append_assistant(self, content: str | None, tool_calls: list | None = None,
                         thinking: str | None = None,
                         thinking_signature: str | None = None) -> None:
        """追加 assistant 消息。"""
        ...

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        """回填某次工具调用的结果。"""
        ...

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        """为未完成的工具调用补齐 `[用户中断]` 结果。"""
        ...


class StatsPort(Protocol):
    """Token 与费用统计面（实现方：`stats.StatsTracker`）。"""

    @property
    def input_tokens(self) -> int:
        """输入 token（最近一轮）。"""
        ...

    @property
    def output_tokens(self) -> int:
        """输出 token（全程累计）。"""
        ...

    @property
    def cache_hit_ratio(self) -> float:
        """缓存命中率（0~1）。"""
        ...

    @property
    def cost(self) -> float:
        """费用（全程累计）。"""
        ...

    @property
    def balance(self) -> float:
        """余额（最近一次成功查询值）。"""
        ...

    def update(self, usage: dict) -> None:
        """更新一轮用量。"""
        ...


class AIOptions(Protocol):
    """内循环所需的人工智能配置面（实现方：`config.AIConfig`）。"""

    retry_count: int
    """重试次数上限（钳制 1–10，缺失或非法时回落 3）。"""

    thinking_effort: str
    """思考强度取值（经 `thinking_options` 映射为中文标签）。"""

    thinking_options: dict
    """思考强度标签映射表（缺失时标签回退原始取值）。"""


class CompressionPort(Protocol):
    """压缩执行面（实现方：`compression.CompressionCoordinator`）。"""

    def mid_run_guard(self) -> bool:
        """运行中自查：占比超阈值时执行无新输入压缩，返回是否压缩成功。"""
        ...

    def compress_no_input(self) -> "CompressOutcomePort":
        """无输入压缩（溢出恢复入口）。"""
        ...


class CompressOutcomePort(Protocol):
    """压缩结果面（实现方：`compression.CompressResult`）。"""

    @property
    def ok(self) -> bool:
        """压缩是否成功（历史已被整体替换）。"""
        ...


class RatioPort(Protocol):
    """窗口占比面（实现方：`compression.CompressionContext`）。"""

    def need_compress(self) -> bool:
        """占比是否达到压缩阈值（无数据一律为否）。"""
        ...

    def update_ratio(self, input_tokens: int) -> None:
        """按服务端返回的输入 token 数刷新占比。"""
        ...


class BackgroundPort(Protocol):
    """受管后台任务面（实现方：`tools.shell.BackgroundManager`）。"""

    def running_count(self) -> int:
        """运行中任务数。"""
        ...

    def running_summary(self) -> str:
        """运行中任务摘要（无任务时为空串）。"""
        ...


# ═══════════════════════════════════════════════════════════════
# 内循环
# ═══════════════════════════════════════════════════════════════


class ConversationLoop:
    """工具调度内循环 —— 单轮对话内「请求 → 收事件 → 执行工具 → 回传」的轮转控制。

    固定循环（每轮）：运行中上下文自查 → 请求前修复 → 流式请求 → 逐事件消费 →
    有工具调用则执行并回传后进入下一轮请求；无工具调用的纯文本轮按收尾软提醒
    （计划未勾选 / 后台任务运行中，各触发一次）可能继续内循环，最终正常收尾。
    出口副作用：统计栏显示条件（普通模式每轮显示；目标模式中间轮静默；后台任务
    运行中抑制并提示；删除确认引起的中间结束不显示）与 `TurnOutcome` 返回值。
    """

    def __init__(
        self,
        *,
        llm: LLMStreamSource,
        store: HistoryPort,
        dispatcher: ToolDispatcher,
        env: ToolEnv,
        stats: StatsPort,
        interaction: InteractionPort,
        console: ConsolePort,
        ai_options: AIOptions,
        data_dir: str,
        background: BackgroundPort | None = None,
        compression: CompressionPort | None = None,
        ratio: RatioPort | None = None,
        logger: LogPort | None = None,
    ) -> None:
        """构造内循环（依赖全部经构造注入）。

        - `llm`：流式请求与重试上限同步；
        - `store`：消息历史唯一所有者（读经视图、写经受控接口）；
        - `dispatcher`：工具调度（分组执行、结果回传、调度行显示；取消广播归其职责）；
        - `env`：工具运行时门面（计划 / 目标标记 / 提醒标志 / 删除确认门）；
        - `stats`：统计读数（统计栏与占比刷新）；
        - `interaction`：会话级交互（建流、确认读取、打断收敛）；
        - `console`：终端写出（后台任务提示、调试文件落点）；
        - `ai_options`：重试上限与思考强度标签来源；
        - `data_dir`：空回复调试文件落盘目录；
        - `background`：受管后台任务面（None 表示无后台任务来源）；
        - `compression` / `ratio`：压缩执行与占比面（None 表示禁用溢出恢复与自查）；
        - `logger`：日志端口（None 时不记录）。
        """
        self._llm = llm
        self._store = store
        self._dispatcher = dispatcher
        self._env = env
        self._stats = stats
        self._interaction = interaction
        self._console = console
        self._ai = ai_options
        self._data_dir = data_dir
        self._background = background
        self._compression = compression
        self._ratio = ratio
        self._logger = logger
        self._last_parts: list[str] = []

    # ── 对外只读状态 ──

    @property
    def last_content(self) -> str:
        """最近一轮已产出的文本（程序异常路径由主循环补写历史用）。"""
        return "".join(self._last_parts)

    # ── 主入口 ──

    def run(self, sink: OutputSink, goal_mode: bool = False,
            force_final: bool = False) -> TurnOutcome:
        """驱动一轮对话，返回本轮结局 `TurnOutcome`。

        - `sink`：本轮的流式输出会话句柄（主循环经 `InteractionPort.begin_turn()`
          创建；删除确认挂起后会以新句柄继续）；
        - `goal_mode`：目标模式（中间轮不显示统计栏，AI 已声明完成或强制收尾轮才显示）；
        - `force_final`：强制收尾轮（轮数上限注入收尾指令的那一轮，正常完成时显示统计栏）。
        """
        retry_limit = self._retry_limit()
        self._llm.set_retry_count(retry_limit)
        retries = 0
        overflow_recovered = False

        while True:
            # a. 运行中自查：占比超阈值时先压缩再发本次请求
            if (self._compression is not None and self._ratio is not None
                    and self._ratio.need_compress()):
                sink.notify(OVER_RATIO_NOTICE)
                if self._compression.mid_run_guard():
                    sink.notify(COMPACT_DONE_NOTICE)
                else:
                    sink.notify(COMPACT_FAILED_NOTICE)

            # b. 请求前修复被中断留下的不完整消息序列
            self._store.repair()

            # c. 流式请求与事件消费
            content_parts: list[str] = []
            self._last_parts = content_parts
            tool_calls_result: list = []
            call_usage = None
            finish_reason: str | None = None
            interrupted_info = None
            thinking_text = None
            thinking_signature = None

            for event in self._llm.chat_stream(
                self._store.view().to_list(),
                cancel_check=lambda: sink.cancelled,
            ):
                # 检查点①：事件消费循环内
                if sink.cancelled:
                    return self._close_interrupted(sink, content_parts)

                calls = get_tool_calls(event)
                if calls is not None:
                    tool_calls_result = calls

                thinking = get_thinking(event)
                if thinking is not None:
                    thinking_text = thinking
                signature = get_thinking_signature(event)
                if signature is not None:
                    thinking_signature = signature

                # 纯文本增量：仅当该事件不含工具调用时才上屏并计入本轮缓冲
                if is_text_delta(event):
                    delta = event[KEY_CONTENT]
                    sink.feed(delta)
                    content_parts.append(delta)

                usage = get_usage(event)
                if usage is not None:
                    call_usage = usage

                if is_stream_interrupted(event):
                    interrupted_info = get_stream_interrupted(event)
                    continue

                if is_retry_notice(event):
                    # 重试提示直通终端，不写入对话历史
                    sink.notify(event[KEY_RETRY_NOTICE])
                    continue

                reason = get_finish_reason(event)
                if reason is not None:
                    finish_reason = reason
                    if reason == "context_overflow":
                        break
                    if reason == "error":
                        # 错误出口不留痕：不向历史写入任何消息，直接收尾
                        self._finish(sink)
                        return TurnOutcome(TURN_ERROR, self.last_content)

            # d. 上下文超限溢出恢复（每次运行最多一次）
            if finish_reason == "context_overflow":
                if overflow_recovered or self._compression is None:
                    sink.notify(OVERFLOW_GIVEUP_NOTICE)
                    self._finish(sink)
                    return TurnOutcome(TURN_ERROR, self.last_content)
                overflow_recovered = True
                self._log_warning("请求被拒绝: 上下文超限，执行压缩恢复")
                sink.restart_attempt()
                sink.notify(OVERFLOW_NOTICE)
                if self._compression.compress_no_input().ok:
                    self._log_info("压缩恢复完成，重发请求")
                    sink.notify(COMPACT_RETRY_NOTICE)
                    continue
                sink.notify(OVERFLOW_FAILED_NOTICE)
                self._finish(sink)
                return TurnOutcome(TURN_ERROR, self.last_content)

            # 检查点②：事件流结束后
            if sink.cancelled:
                return self._abort_round(sink)

            # 收到任何正常完成标记（含工具轮）→ 重置流中断重试预算
            if finish_reason is not None:
                retries = 0

            # e. 工具轮：assistant 先写入历史 → 执行工具 → 回传结果 → 下一轮
            if tool_calls_result:
                self._store.append_assistant(
                    "".join(content_parts) or None,
                    tool_calls=tool_calls_result,
                    thinking=thinking_text,
                    thinking_signature=thinking_signature,
                )
                tool_results = self._dispatcher.execute(tool_calls_result, sink)

                # 检查点③：工具执行返回后
                if sink.cancelled:
                    completed_ids = {tc_id for tc_id, _ in tool_results}
                    self._store.append_interrupted_tools(tool_calls_result, completed_ids)
                    return self._abort_round(sink)

                pending = self._env.delete_gate.take()
                if pending is not None:
                    confirm_id = next(
                        (tc_id for tc_id, result in tool_results if result.await_confirm),
                        None,
                    )
                    if confirm_id is not None:
                        sink = self._handle_delete_confirm(
                            sink, confirm_id, pending, tool_results
                        )
                        continue

                for tc_id, result in tool_results:
                    self._store.append_tool_result(tc_id, result.llm_text)

                self._update_usage(call_usage)
                continue

            # f. 无完成标记 → 服务端响应流中断：丢弃截断内容并整轮重试
            if finish_reason is None:
                if retries < retry_limit:
                    retries += 1
                    kind = (interrupted_info or {}).get("kind", "unknown")
                    base = backoff_base(retries - 1)
                    self._log_warning(
                        f"响应流中断({kind})，{base}s后自动重试"
                        f"(第{retries}/{retry_limit}次)"
                    )
                    # 渲染缓冲清理：重试流会从头重播，残留内容会与重播拼接错乱
                    sink.restart_attempt()
                    sink.notify(stream_interrupt_retry_notice(base, retries, retry_limit))
                    # 检查点④：退避等待期间（等待可被取消）
                    if not retry_sleep(retries - 1, lambda: sink.cancelled):
                        return self._abort_round(sink)
                    continue
                sink.notify(stream_interrupt_give_up_notice(retry_limit))
                self._finish(sink)
                return TurnOutcome(TURN_ERROR, self.last_content)

            # g. 无工具调用：纯文本完成或空回复
            if not content_parts:
                self._dump_empty_debug(
                    content_parts, tool_calls_result, finish_reason, call_usage
                )
                reason = finish_reason or "stream_interrupted"
                text = EMPTY_REPLY_TEXTS.get(
                    reason, f"⚠ AI 返回异常（{reason}），请稍后重试。"
                )
                sink.notify(f"\n\n{text}\n")
                self._finish(sink)
                return TurnOutcome(TURN_EMPTY, "")

            # 纯文本轮不回传思考（学官方 harness：reasoning 仅工具轮回传）
            self._store.append_assistant("".join(content_parts))
            self._update_usage(call_usage)

            # h. 收尾软提醒（各触发一次，仅注入 AI 上下文）
            unfinished = unfinished_items(self._env.plan.current())
            if unfinished and self._env.reminders.try_trigger_plan():
                self._store.append_user(plan_reminder_text(unfinished))
                self._resume_waiting(sink)
                continue
            bg_summary = self._background.running_summary() if self._background else ""
            if bg_summary and self._env.reminders.try_trigger_bg():
                self._store.append_user(bg_reminder_text(bg_summary))
                self._resume_waiting(sink)
                continue

            # i. 正常收尾（统计栏按结案信号规则显示）
            self._finish_normal(sink, goal_mode, force_final)
            return TurnOutcome(TURN_COMPLETED, self.last_content)

    # ── 出口辅助 ──

    def _close_interrupted(self, sink: OutputSink,
                           content_parts: Sequence[str]) -> TurnOutcome:
        """消费中中断：已产出文本先按纯文本轮写入历史，随后结束流并提示打断。"""
        text = "".join(content_parts)
        if text:
            self._store.append_assistant(text)
        return self._abort_round(sink, text)

    def _abort_round(self, sink: OutputSink, content: str = "") -> TurnOutcome:
        """中断出口：结束当前流、通知界面显示打断提示，本轮不计正常完成。"""
        sink.abort()
        self._interaction.notify_interrupted()
        return TurnOutcome(TURN_INTERRUPTED, content or self.last_content)

    def _finish(self, sink: OutputSink, with_stats: bool = True) -> None:
        """按统计参数落定本轮（提前结束的出口与删除确认中间结束用）。"""
        sink.finish(self._turn_stats(), with_stats=with_stats)

    def _finish_normal(self, sink: OutputSink, goal_mode: bool,
                       force_final: bool) -> None:
        """正常收尾：按结案信号规则决定是否显示统计栏。

        普通模式每轮显示；目标模式中间轮静默（AI 已声明完成或强制收尾轮显示）；
        后台仍有运行中任务时一律不显示并提示处理方式。
        """
        show_stats = (not goal_mode) or self._env.goal.is_set or force_final
        bg_running = self._background.running_count() if self._background else 0
        if bg_running:
            show_stats = False
            self._console.write(BG_RUNNING_NOTICE.format(count=bg_running))
        self._finish(sink, with_stats=show_stats)

    def _turn_stats(self) -> TurnStats:
        """统计栏读数（输入 / 输出 / 缓存比例 / 费用 / 余额 / 思考强度）。"""
        return TurnStats(
            input_tokens=self._stats.input_tokens,
            output_tokens=self._stats.output_tokens,
            cache_ratio=self._stats.cache_hit_ratio,
            cost=self._stats.cost,
            balance=self._stats.balance,
            thinking_effort=self._thinking_label(),
        )

    def _resume_waiting(self, sink: OutputSink) -> None:
        """软提醒注入后继续内循环：落定上一轮文字并重启等待动画（无文本输出）。

        以 `notify("")` 表达「仅落定与重启」：界面实现须在输出前落定渲染缓冲、
        输出后重启思考动画（对应旧实现的 `flush_renderer()` + `begin()`）。
        """
        sink.notify("")

    # ── 删除确认挂起（非 Windows）──

    def _handle_delete_confirm(self, sink: OutputSink, confirm_id: str,
                               pending: tuple[str, dict],
                               tool_results: Sequence[tuple[str, ToolResult]]) -> OutputSink:
        """删除确认：结束本轮输出（不显示统计栏）→ 提示符下等待确认 → 重新执行或取消。

        确认后置跳过确认标记并重新执行同一命令（不再触发确认），回传剥离框架标签
        后的结果，差异输出以缩进格式打印；取消后回传固定取消文案。两条路径都以
        新的流会话继续内循环。
        """
        tool_name, arguments = pending

        # 先回传非确认的工具结果
        for tc_id, result in tool_results:
            if tc_id != confirm_id:
                self._store.append_tool_result(tc_id, result.llm_text)

        # 结束当前流式输出（本轮还没结束，不显示统计栏）
        self._finish(sink, with_stats=False)

        if self._interaction.read_confirmation(DELETE_CONFIRM_PROMPT):
            self._env.delete_gate.mark_confirmed()
            result = self._dispatcher.execute_one(tool_name, arguments)
            self._store.append_tool_result(confirm_id, result.llm_text)
            if result.ui_text:
                self._console.write(
                    "\n".join(f"  {line}" for line in result.ui_text.split("\n")) + "\n\n"
                )
        else:
            self._store.append_tool_result(confirm_id, DELETE_CANCELLED_TEXT)

        return self._interaction.begin_turn()

    # ── 内部辅助 ──

    def _update_usage(self, usage: Mapping | None) -> None:
        """用量到达时更新统计并刷新窗口占比（供下一轮运行中自查读取）。"""
        if not usage:
            return
        self._stats.update(dict(usage))
        if self._ratio is not None:
            self._ratio.update_ratio(self._stats.input_tokens)

    def _retry_limit(self) -> int:
        """流中断重试上限：配置的 LLM 重试次数（钳制 1–10），非法值回落默认。"""
        try:
            return clamp_retry_count(int(self._ai.retry_count))
        except (TypeError, ValueError, AttributeError):
            return DEFAULT_MAX_RETRIES

    def _thinking_label(self) -> str:
        """思考强度中文标签（映射缺失时回退原始取值）。"""
        options = getattr(self._ai, "thinking_options", None) or {}
        effort = getattr(self._ai, "thinking_effort", "") or ""
        return options.get(effort, effort)

    def _dump_empty_debug(self, content_parts: Sequence[str], tool_calls_result: list,
                          finish_reason: str | None, call_usage) -> None:
        """空回复时把请求、响应与原始流信息落盘为调试文件（时间戳精确到秒）。"""
        debug_path = os.path.join(
            self._data_dir,
            f"debug_empty_{time.strftime('%Y%m%d_%H%M%S')}.json",
        )
        debug_data = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": {"messages": self._store.view().to_list()},
            "response": {
                "raw_sse_lines": self._llm.raw_sse or [],
                "parsed_content": "".join(content_parts),
                "parsed_tool_calls": tool_calls_result,
                "parsed_finish_reason": finish_reason,
                "call_usage": call_usage,
            },
        }
        try:
            with open(debug_path, "w", encoding="utf-8") as file:
                json.dump(debug_data, file, ensure_ascii=False, indent=2)
            self._console.write(f"  ⚠ 调试日志已写入: {debug_path}\n")
        except OSError:
            pass

    def _log_info(self, message: str) -> None:
        """记录内循环 info 日志（未注入日志端口时静默）。"""
        if self._logger is not None:
            self._logger.info("conversation", message)

    def _log_warning(self, message: str) -> None:
        """记录内循环 warning 日志（未注入日志端口时静默）。"""
        if self._logger is not None:
            self._logger.warning("conversation", message)
