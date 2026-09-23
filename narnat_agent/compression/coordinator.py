"""压缩协调器 —— 触发入口、摘要编排、溢出恢复与手动压缩。

行为契约：`openspec/changes/recast-v2/specs/compression/spec.md`
（Requirement：运行中主动压缩 / 摘要请求构造的执行 / 结果归因与历史不变性 /
溢出恢复 / 手动压缩 / 兼容性怪癖保持）。

结构（design D1/D7/D8）：
- 全部依赖构造注入：配置面、消息历史（`MessageStore`）、LLM 流（`LLMStreamSource`
  契约，实际由 `llm` 积木实现）、占比基准（`CompressionContext`）、压缩动画
  （`Animator` 端口）、中断信号（`InterruptSignal`）、日志端口（`LogSink`）；
- 失败路径不再经 `on_interrupt` / `on_llm_error` 回调通知，统一由结果对象
  `CompressResult` 承载归因；入口按归因善后（占比重置 / 延后重试、用户输入写回）；
- 占比的纯查询与刷新归 `CompressionContext`，本类不再做转发层；
- 失败与中断路径不改动历史（仅成功路径经 `MessageStore.replace_all` 一次性整体替换）。
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from ..contracts.interrupt import InterruptSignal
from ..contracts.llm_events import get_finish_reason, is_text_delta
from ..contracts.output import Animator
from ..messages import MessageStore
from .compressor import Compressor, estimate_message_tokens, select_cut_index
from .context import CompressionContext, LogSink

__all__ = [
    "MANUAL_FAIL_TEXT",
    "OVERFLOW_CONTINUE_MESSAGE",
    "CompressResult",
    "CompressionCoordinator",
]

# 溢出恢复与运行中自查压缩成功后追加的内部继续指令：告知模型历史已被压缩、
# 任务继续。以字面 "[]" 开头是已发布的兼容怪癖（specs/compression「兼容性怪癖保持」），
# 不得改动前缀。
OVERFLOW_CONTINUE_MESSAGE = (
    "[]对话历史过长已自动压缩，早前内容已总结为上下文成果。"
    "请基于当前上下文直接继续推进任务，无需复述历史。"
)

# 手动压缩失败原因 → 用户提示（失败时历史保持原样）
MANUAL_FAIL_TEXT = {
    "llm_error": "压缩失败: LLM调用出错，历史未变更",
    "empty_summary": "压缩失败: 总结为空，历史未变更",
    "overflow": "压缩失败: 压缩请求自身超出模型上下文限制，历史未变更",
}

# 摘要请求失败归因 → 日志文案（不含「历史未变更」尾缀，两者口径不同勿合并）
FAIL_LOG_TEXT = {
    "llm_error": "压缩失败: LLM调用出错",
    "empty_summary": "压缩失败: 总结为空",
    "overflow": "压缩失败: 压缩请求自身超出模型上下文限制",
}


class LLMStreamSource(Protocol):
    """摘要请求所需的 LLM 流式接口（`llm` 积木 `chat_stream` 的契约子集）。

    返回事件流迭代器（dict 事件，形态见 `contracts.llm_events`）；`no_tools=True`
    表示请求不携带任何工具定义；`cancel_check` 为取消查询可调用对象，为真时
    LLM 层静默结束流（不产出任何事件）。
    """

    def chat_stream(self, messages: list[dict[str, Any]], no_tools: bool = False,
                    no_thinking: bool = False, cancel_check=None) -> Iterator[dict[str, Any]]:
        """发起流式请求，返回事件流生成器。"""
        ...


@dataclass(frozen=True)
class CompressResult:
    """一次压缩执行的结果。

    - `ok`：成功与否（成功 = 历史已被「系统提示词 + 摘要 + 保留尾部」整体替换）；
    - `replaced` / `tokens`：被摘要替换掉的对话消息条数与其估算 token 数
      （统计口径，仅供提示展示）；
    - `reason`：失败归因，取值集合固定——`empty`（无历史可压缩，仅手动路径）、
      `interrupted`（用户中断）、`llm_error`（摘要请求出错）、`empty_summary`
      （总结为空）、`overflow`（压缩请求自身超限）；成功时为空串。
    """

    ok: bool
    replaced: int = 0
    tokens: int = 0
    reason: str = ""


class CompressionCoordinator:
    """上下文压缩协调器（三个入口 + 运行中自查）。

    - `compress(pending_input)`：用户轮边界的自动压缩（会话层在请求前判定占比后调用）；
    - `compress_no_input()`：无输入压缩，运行中自查与溢出恢复共用（重建以内部继续指令收尾）；
    - `compress_manual()`：手动压缩（`/compact` 命令）。

    溢出恢复的「每次运行最多一次」记账归调用方（会话层），本类不持有运行态。
    """

    def __init__(self, config, store: MessageStore, llm: LLMStreamSource,
                 context: CompressionContext, animator: Animator,
                 interrupt: InterruptSignal, logger: LogSink | None = None) -> None:
        """构造协调器。

        - `config`：配置面（需要 `system_prompt` 与 `session.retain_tokens`，每次
          压缩现读，不缓存）；
        - `store`：消息历史唯一所有者（读经视图，成功路径经 `replace_all` 替换）；
        - `llm`：摘要请求的 LLM 流（`LLMStreamSource`）；
        - `context`：占比基准（失败/成功的占位善后经其方法生效）；
        - `animator`：压缩动画端口（压缩期间显示「正在压缩」，headless 为空实现）；
        - `interrupt`：中断信号总线（压缩取消检查与手动压缩的临时模式切换）；
        - `logger`：日志端口，缺省不记录日志。
        """
        self._config = config
        self._store = store
        self._llm = llm
        self._context = context
        self._animator = animator
        self._interrupt = interrupt
        self._logger = logger
        self._compressor = Compressor()

    # ── 三个压缩入口 ──

    def compress(self, pending_input: str) -> CompressResult:
        """用户轮边界的自动压缩（占比达阈值时由会话层在请求前调用）。

        成功时本次输入随重建一并写入历史（不重复追加）；失败与被中断时输入仍
        写入历史（本轮不产生 AI 回复）；被中断重置占比与告警、摘要请求出错把
        占比延后（阈值 − 10）。不做「无历史可压缩」提前拒绝——重建承担着把
        触发输入写入历史并收尾的职责（specs/compression「兼容性怪癖保持」）。
        """
        self._animator.begin_compressing()
        result = self._run(pending_input)
        self._animator.end_compressing()
        if result.ok:
            self._context.reset()
            return result
        if result.reason == "interrupted":
            self._context.reset()
        else:
            self._log_fail(result.reason)
            self._context.set_retry_soon()
        self._store.append_user(pending_input)
        return result

    def compress_no_input(self) -> CompressResult:
        """无输入压缩：运行中自查与溢出恢复共用的入口。

        正常请求被服务端以上下文超限拒绝后由会话层调用（此时本轮 assistant
        尚未写入历史，压缩后可原地重发请求）；重建以内部继续指令收尾。
        被中断重置占比与告警、摘要请求出错把占比延后（阈值 − 10）。
        """
        self._animator.begin_compressing()
        result = self._run(OVERFLOW_CONTINUE_MESSAGE)
        self._animator.end_compressing()
        if result.ok or result.reason == "interrupted":
            self._context.reset()
        else:
            self._log_fail(result.reason)
            self._context.set_retry_soon()
        return result

    def compress_manual(self) -> tuple[str, str]:
        """手动压缩（`/compact` 命令）：不受占比阈值限制、不携带输入、不追加消息。

        压缩期间临时切到运行模式（Esc 取消与运行中压缩手感一致），无论成功、
        失败或异常，结束后必须恢复为输入等待状态。成功后重置占比与告警。
        返回 `(status, text)`：status = `ok`（成功）/ `empty`（无历史可压缩）/
        `error`（被取消或失败）；text 为对应用户提示（文案见
        specs/compression「手动压缩」）。
        """
        try:
            self._interrupt.enter_run_mode()
            self._animator.begin_compressing()
            result = self._run(None)
        finally:
            # 无论压缩还是界面回调抛异常，都必须回到输入模式，
            # 否则按键采集会残留在运行态
            try:
                self._animator.end_compressing()
            finally:
                self._interrupt.enter_input_mode()

        if result.ok:
            self._context.reset()
            if self._logger is not None:
                self._logger.info(
                    "compression",
                    f"手动压缩完成: 替换{result.replaced}条, 约{result.tokens}tokens",
                )
            return "ok", f"已压缩 {result.replaced} 条历史消息（约 {result.tokens} tokens）"
        if result.reason == "empty":
            return "empty", "没有可压缩的历史对话"
        if result.reason == "interrupted":
            if self._logger is not None:
                self._logger.info("compression", "手动压缩被中断")
            return "error", "压缩已取消，历史未变更"
        self._log_fail(result.reason)
        # 未知归因不宣称历史状态（异常可能发生在历史已替换之后）
        return "error", MANUAL_FAIL_TEXT.get(result.reason, "压缩失败: 未识别的失败原因")

    # ── 运行中自查 ──

    def mid_run_guard(self) -> bool:
        """运行中自查：占比超阈值时先压缩再发本次请求（无新用户输入）。

        由会话层在每轮请求发出前调用（AI 自我运行期间不经过用户轮边界，占比由
        每轮 usage 到达时刷新）。返回 True = 执行了压缩且成功；失败旁路为
        「继续尝试本次请求」（由调用方提示）。
        """
        if not self._context.need_compress():
            return False
        if self._logger is not None:
            self._logger.warning("compression", "运行中占比超阈值，主动压缩历史")
        return self.compress_no_input().ok

    # ── 压缩执行 ──

    def _run(self, pending_input: str | None) -> CompressResult:
        """执行一次压缩：切点选择 → 摘要请求 → 重建会话（一次性整体替换）。

        `pending_input` 为压缩后写入历史的本次输入；None = 手动压缩（不携带输入）。
        失败与中断路径不改动历史；成功路径整体替换并返回替换统计。
        """
        if self._logger is not None:
            self._logger.info("compression", f"压缩触发, messages={len(self._store)}条")

        full_messages = self._store.view().to_list()

        # 对话区起点：system 消息（含技能注入）不进保留区，
        # 重建时由新的 system_prompt + 摘要承担
        start = 0
        while start < len(full_messages) and full_messages[start].get("role") == "system":
            start += 1

        # 保留预算覆盖了全部对话（或本就没有对话）：压缩无收益，在消耗一次
        # 总结请求前先拒绝。仅手动压缩（无新输入）启用；自动路径保持旧行为——
        # 其重建过程承担着把触发输入写入历史并收尾的职责，不能在此提前返回
        cut = select_cut_index(full_messages, self._config.session.retain_tokens)
        if pending_input is None and (start >= len(full_messages)
                                      or (cut is not None and cut <= start)):
            if self._logger is not None:
                self._logger.info("compression", "无历史可压缩，跳过")
            return CompressResult(False, reason="empty")

        # 被摘要替换掉的对话消息（统计口径，仅供提示展示）
        replaced_messages = full_messages[start:cut] if cut is not None else full_messages[start:]
        replaced = len(replaced_messages)
        replaced_tokens = sum(estimate_message_tokens(m) for m in replaced_messages)

        # 请求前先判取消：用户已按 Esc（含上一轮残留标记）时不再发起摘要请求
        if self._interrupt.is_set:
            return CompressResult(False, reason="interrupted")

        # 构建摘要请求（全部历史 + 指令，摘要质量不受尾部切分影响）并收集正文
        compress_messages = self._compressor.build_compress_messages(full_messages)
        summary_parts: list[str] = []
        llm_error = False
        for chunk in self._llm.chat_stream(compress_messages, no_tools=True,
                                           cancel_check=lambda: self._interrupt.is_set):
            if self._interrupt.is_set:
                return CompressResult(False, reason="interrupted")
            finish_reason = get_finish_reason(chunk)
            if finish_reason == "context_overflow":
                # 压缩请求自身也超限（全量历史 + 指令仍超窗口）：单独归因，
                # 否则会被下游误报为「总结为空」，掩盖真实原因
                return CompressResult(False, reason="overflow")
            if finish_reason == "error":
                llm_error = True
                break
            # 摘要正文只收集正常文本增量（带工具调用字段的块不计入）
            if is_text_delta(chunk):
                summary_parts.append(chunk["content"])

        if llm_error:
            return CompressResult(False, reason="llm_error")

        # llm 层在取消时静默结束流（不产出任何事件），此处补判一次：
        # 否则「零输出 + 已取消」会被误归因为「总结为空」，掩盖真实原因
        if not summary_parts and self._interrupt.is_set:
            return CompressResult(False, reason="interrupted")

        summary = "".join(summary_parts)
        if not summary.strip():
            return CompressResult(False, reason="empty_summary")

        # 先完整构建新会话（含本次输入），再一次性整体替换旧会话
        tail = full_messages[cut:] if cut is not None else []
        dropped: list[dict[str, Any]] = []
        if pending_input is None:
            # 手动压缩：不追加输入。尾部末条是 user 的罕见情形（上一轮没留下
            # AI 输出/历史含连续 user）整段剔除——否则下次输入会形成连续 user
            # （Anthropic 严格后端强制角色交替）；其内容已进摘要
            while tail and tail[-1].get("role") == "user":
                dropped.append(tail[-1])
                tail = tail[:-1]
            # 被剔除的消息同样由摘要替换掉，计入替换统计（口径=从历史消失的条数）
            replaced += len(dropped)
            replaced_tokens += sum(estimate_message_tokens(m) for m in dropped)
        new_messages = self._compressor.build_new_session_messages(
            self._config.system_prompt, summary, tail)
        if pending_input is not None:
            if tail and tail[-1].get("role") == "user":
                # 防连续 user（Anthropic 严格后端强制角色交替）：尾部末条已是
                # user 时把新输入以空行拼接合并进该条
                merged = dict(tail[-1])
                merged["content"] = (tail[-1].get("content") or "") + "\n\n" + pending_input
                new_messages[-1] = merged
            else:
                new_messages.append({"role": "user", "content": pending_input})
        self._store.replace_all(new_messages)

        if self._logger is not None:
            self._logger.info(
                "compression",
                f"压缩成功，新会话已创建, 保留尾部={len(tail)}条",
            )
        return CompressResult(True, replaced, replaced_tokens)

    # ── 内部辅助 ──

    def _log_fail(self, reason: str) -> None:
        """按归因记录摘要请求失败日志（未知归因不编造文案）。"""
        if self._logger is None:
            return
        text = FAIL_LOG_TEXT.get(reason)
        if text is not None:
            self._logger.error("compression", text)
