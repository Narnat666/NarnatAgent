"""压缩协调器 —— 上下文压缩流程编排

从 Agent._handle_compress() 提取。算法逻辑原样保留。
溢出恢复入口（compress_no_input）供 agent_loop 在请求被 400 上下文超限
拒绝时使用：压缩后追加内部继续指令，不携带新用户输入。
手动压缩入口（compress_manual）供 /compact 命令使用：不携带任何输入。
"""

from typing import Tuple

from ..config.loader import Config
from ..config.defaults import INTERNAL_NOTICE_PREFIX
from .message_manager import MessageManager
from .llm import LLMClient
from .context import ContextManager
from ..ui.ui_design import UIInterface
from ..ui.interrupt import _interrupt_ctrl
from ..logger import AgentLogger
from ..output import write as _stdout_write

# 溢出恢复压缩成功后追加的内部继续消息：告知模型历史已被压缩、任务继续。
# 开头那个 `[]` 是历史遗留（早先的标记位），现在由 defaults.INTERNAL_NOTICE_MARKERS
# 统一登记识别。改这里时必须同步那边，否则这条注入会漏到界面历史里。
OVERFLOW_CONTINUE_MESSAGE = (
    "[]对话历史过长已自动压缩，早前内容已总结为上下文成果。"
    "请基于当前上下文直接继续推进任务，无需复述历史。"
)

# 手动压缩失败原因 → 用户提示（压缩失败时历史保持原样）
_MANUAL_FAIL_TEXT = {
    "llm_error": "压缩失败: LLM调用出错，历史未变更",
    "empty_summary": "压缩失败: 总结为空，历史未变更",
    "overflow": "压缩失败: 压缩请求自身超出模型上下文限制，历史未变更",
}

# 自动压缩失败原因 → 用户提示（历史保持原样，对话继续）
_AUTO_FAIL_REASON = {
    "llm_error": "LLM调用出错",
    "empty_summary": "总结为空",
    "overflow": "压缩请求自身超出模型上下文限制",
}


class CompressionCoordinator:
    """上下文压缩协调器"""

    def __init__(self, config: Config, msg_manager: MessageManager,
                 llm: LLMClient, context: ContextManager,
                 ui: UIInterface,
                 logger: AgentLogger, tool_context=None,
                 session_mgr=None, stats=None):
        self._config = config
        self._msg_manager = msg_manager
        self._llm = llm
        self._context = context
        self._ui = ui
        self._logger = logger
        self._tool_context = tool_context
        # 自动压缩成功后修正探索分支的 /done 增量基准（手动 /compact 由
        # session_callbacks 负责，见 compress_manual）
        self._session_mgr = session_mgr
        # 压缩摘要的 token/费用计入本会话消耗（与主口径同一份累计；不动
        # input_tokens 快照），并即时刷新状态栏 —— 见 stats.log_external_usage
        self._stats = stats

    def _log_usage(self, usage: dict) -> None:
        try:
            self._stats.log_external_usage(usage)
        except Exception:
            pass

    def _append_plan_reminder(self):
        """压缩后重注入当前计划：TodoWrite 调用历史被压缩吞掉后模型会忘记
        计划，而收尾软提醒/GoalComplete 交叉校验仍以计划为准；此处把当前
        未完成计划还给模型。无计划或全部完成时不注入。"""
        ctx = self._tool_context
        if ctx is None:
            return
        unfinished = [t for t in ctx.current_todos if t.get("status") != "completed"]
        if not unfinished:
            return
        lines = [f"{INTERNAL_NOTICE_PREFIX} 上下文已压缩。当前计划（未完成）："]
        for i, t in enumerate(unfinished, 1):
            label = "进行中" if t.get("status") == "in_progress" else "待处理"
            lines.append(f"{i}. [{label}] {t.get('content', '')}")
        self._msg_manager.append_user_merged("\n".join(lines))

    def _append_compaction_notice(self):
        """压缩成功后的提醒：历史已被摘要替换，按摘要继续。

        无开关、压缩成功即发（提示模型别复述已压缩的历史、别问"我们聊到哪了"）；
        仅注入AI上下文，终端不展示（内部纠偏，用户无需感知）。
        """
        self._msg_manager.append_user_merged(
            f"{INTERNAL_NOTICE_PREFIX} 对话历史过长已压缩，早前内容已总结为上下文成果；"
        )

    def _detach_prepended(self, prepended: dict) -> None:
        """把压缩前追加的那条输入从归档里摘掉。

        全量压缩（无保留尾部）会把列表里所有非 system 消息整体归档，"压缩前
        就先追加"的那条输入因此也会进归档；它同时又作为末条留在当前列表里
        （模型要回答的就是它）。归档 + 当前正是落盘/展示口径，不摘掉就会在会话
        文件里出现两遍。摘掉也符合本来语义：它是压缩后的最新一条，不是被摘要
        替换掉的历史（压缩成功后才追加的旧实现同样不进归档）。
        保留尾部路径下它本就在 tail 里、不在归档，此处为 no-op。
        """
        msgs = self._msg_manager._messages
        archived = getattr(msgs, "_archived", None)
        if not archived:
            return
        for i, m in enumerate(archived):
            if m is prepended:
                del archived[i]
                ids = getattr(msgs, "_archived_ids", None)
                if ids is not None:
                    ids.discard(id(prepended))
                return

    def _fix_prepended_tail(self, prepended: dict) -> None:
        """让压缩后的末条 user 完整保留原文，且不在归档里再存一份。

        输入已在列表里，交给 handle_compress 的是空串，于是：
        - 有保留尾部时它把空串并进尾条（内容尾部多出 "\\n\\n"）→ 抹掉即可；
        - 全量压缩（无保留尾部）时它追加一条空 user，原文只在摘要里 → 还原原文。
        两种情况都要把原文从归档里摘掉（见 _detach_prepended）。
        """
        view = self._msg_manager.view
        self._detach_prepended(prepended)
        if len(view) == 0:
            return
        last = view[-1]
        if last.get("role") != "user":
            return
        content = last.get("content")
        if not isinstance(content, str):
            return
        if not content.strip():
            last["content"] = prepended.get("content") or ""
        elif content.endswith("\n\n"):
            last["content"] = content[:-2]

    def _compress_budget(self) -> int:
        """压缩请求的输出上限：等于安全余量（与 deepseek-harness 同口径）。

        上限取余量而非固定常数：余量本身就是"给压缩输出留的空间"，
        两者取同一个值后，"历史 + 压缩输出 ≤ 窗口"由阈值夹取直接保证。
        下限 1024 防止用户把余量配成 0 时压缩输出被压到写不完全摘要。
        """
        return max(1024, int(self._config.session.compress_headroom))

    def compress(self, pending_input: str) -> bool:
        """处理上下文压缩。

        返回值：True=已妥善处理、可继续当前请求（压缩成功或无收益跳过）；
        False=本轮不再发出请求（用户取消压缩）。
        """
        # 保留预算内无可压缩历史：压缩零收益（切点只能全保留，重建=历史
        # 净增一条摘要）、每轮空转白耗一次摘要调用——与 compress_no_input/
        # mid_run_guard 同一守卫。此路径不做压缩但对话必须继续：输入按普通
        # 流程追加后放行（真超限由溢出恢复兜底）。
        if not self._msg_manager.has_compressible_history(self._config.session.retain_tokens):
            self._logger.warning(
                "compressor", "占比超阈值但保留预算内无可压缩历史，跳过压缩（避免空转）")
            self._msg_manager.append_user(pending_input)
            return True

        # 压缩窗口内用户随时可能关窗口/重启后端（真实模型 7~11 秒），而退出
        # 兜底（session_mgr.exit_flush）只落盘消息列表里的内容。原先输入挂在
        # 调用栈上、压缩成功后才追加，退出时这句话永久丢失且零提示（P0）。
        # 故压缩开始前就先追加：任何时刻退出它都在列表里、能被落盘。
        # 交给 handle_compress 的是空串——输入已在尾部，空串让它只并入尾部
        # 而不重复追加一次；拼接痕迹由 _fix_prepended_tail 收尾。
        self._msg_manager.repair()
        self._msg_manager.append_user(pending_input)
        prepended = self._msg_manager.view[-1]

        def on_interrupt():
            self._ui.end_compressing()
            self._context.reset()
            # 输入已在压缩开始前进列表，此处不得再追加（否则重复）

        def on_llm_error(msg):
            self._ui.end_compressing()
            self._logger.error("compressor", msg)
            self._context.set_retry_soon()
            # 同上：中断/失败不改动 messages，输入原本就在列表里

        self._ui.begin_compressing()
        res = self._msg_manager.handle_compress(
            "",
            self._config.system_prompt,
            self._llm,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            on_interrupt=on_interrupt,
            on_llm_error=on_llm_error,
            retain_tokens=self._config.session.retain_tokens,
            on_usage=self._log_usage if self._stats is not None else None,
            compress_max_tokens=self._compress_budget(),
        )
        if res.ok:
            self._ui.end_compressing()
            self._context.reset()
            # 必须在下面两条 append_user_merged 之前：它们并进末条 user 后，
            # 尾部的拼接痕迹就再也认不出来了
            self._fix_prepended_tail(prepended)
            self._append_compaction_notice()
            self._append_plan_reminder()
            if self._session_mgr is not None:
                # 自动压缩同手动 /compact：压缩把列表整体替换，探索分支的
                # _parent_msg_count/_last_summarized_at 需重置到摘要边界，
                # 否则 /done 静默丢增量（基类 no-op，仅 ChildSession 生效）
                self._session_mgr.state.reset_after_compact()
            return True
        if res.reason == "interrupted":
            return False
        # 压缩失败（LLM 出错/总结为空/压缩请求超限）：不阻塞对话——输入已由
        # 回调追加进历史，以现有历史继续本次请求（真超限由溢出恢复兜底），
        # 并提示用户本次未压缩
        _stdout_write(
            f"  自动压缩失败（{_AUTO_FAIL_REASON.get(res.reason, '未识别原因')}），以现有历史继续对话\n")
        return True

    def compress_no_input(self) -> bool:
        """溢出恢复压缩：请求被 400 上下文超限拒绝后调用，无新用户输入。

        成功后消息以内部继续指令收尾（OpenAI 协议要求消息序列以
        user/assistant 收尾，system 兜底不可行），调用方随后重发请求。
        """
        # 保留预算内无可压缩历史时：压缩重建 = 原历史全保留 + 摘要 + 继续指令
        #（净增消息）、白耗一次压缩调用，且重发必然再溢出。直接拒绝，
        # 由调用方走既有报错收尾（"请求仍超出模型上下文限制…"）。
        if not self._msg_manager.has_compressible_history(self._config.session.retain_tokens):
            self._logger.warning("compressor", "溢出恢复：保留预算内无可压缩历史，跳过压缩（避免净增与白耗）")
            return False

        def on_interrupt():
            self._ui.end_compressing()
            self._context.reset()

        def on_llm_error(msg):
            self._ui.end_compressing()
            self._logger.error("compressor", msg)
            self._context.set_retry_soon()

        self._ui.begin_compressing()
        res = self._msg_manager.handle_compress(
            OVERFLOW_CONTINUE_MESSAGE,
            self._config.system_prompt,
            self._llm,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            on_interrupt=on_interrupt,
            on_llm_error=on_llm_error,
            retain_tokens=self._config.session.retain_tokens,
            on_usage=self._log_usage if self._stats is not None else None,
            compress_max_tokens=self._compress_budget(),
        )
        if res.ok:
            self._ui.end_compressing()
            self._context.reset()
            self._append_plan_reminder()
            if self._session_mgr is not None:
                # 自动压缩同手动 /compact：压缩把列表整体替换，探索分支的
                # _parent_msg_count/_last_summarized_at 需重置到摘要边界，
                # 否则 /done 静默丢增量（基类 no-op，仅 ChildSession 生效）
                self._session_mgr.state.reset_after_compact()
        return res.ok

    def compress_manual(self) -> Tuple[str, str]:
        """手动压缩（/compact 命令）：不受占比阈值限制，不携带新输入。

        与自动压缩的区别：压缩后不为命令追加对话内容（命令本身不是对话内容），
        只追加系统提醒；失败/取消不改动占比与重试状态（历史未变，真实 token 数不变）。
        命令在输入模式下执行（ESC 轮询已停），此处临时切到运行模式，
        使压缩过程中的 Esc 取消与运行中压缩手感一致。

        返回 (status, text)：status="ok" 成功 / "empty" 无历史可压缩 /
        "error" 失败；text 为对应的用户提示。
        """
        def on_interrupt():
            self._logger.info("compressor", "手动压缩被中断")

        def on_llm_error(msg):
            self._logger.error("compressor", msg)

        try:
            _interrupt_ctrl.enter_run_mode()
            self._ui.begin_compressing()
            res = self._msg_manager.handle_compress(
                None,
                self._config.system_prompt,
                self._llm,
                cancel_check=lambda: _interrupt_ctrl.is_set,
                on_interrupt=on_interrupt,
                on_llm_error=on_llm_error,
                retain_tokens=self._config.session.retain_tokens,
                on_usage=self._log_usage if self._stats is not None else None,
                compress_max_tokens=self._compress_budget(),
            )
        finally:
            # 无论 handle_compress 还是 UI 回调抛异常，都必须回到输入模式，
            # 否则 ESC 轮询线程会残留在运行态
            try:
                self._ui.end_compressing()
            finally:
                _interrupt_ctrl.enter_input_mode()

        if res.ok:
            self._context.reset()
            self._append_compaction_notice()
            self._append_plan_reminder()
            self._logger.info(
                "compressor",
                f"手动压缩完成: 替换{res.replaced}条, 约{res.tokens}tokens",
            )
            return "ok", f"已压缩 {res.replaced} 条历史消息（约 {res.tokens} tokens）"
        if res.reason == "empty":
            return "empty", "没有可压缩的历史对话"
        if res.reason == "interrupted":
            return "error", "压缩已取消，历史未变更"
        # 未知 reason 不宣称历史状态（异常/失败可能发生在历史已替换之后）
        return "error", _MANUAL_FAIL_TEXT.get(res.reason, "压缩失败: 未识别的失败原因")

    def need_compress(self) -> bool:
        """占比是否已达压缩阈值（供 agent_loop 在运行中自查时先判后提示）"""
        return self._context.need_compress()

    def refresh_ratio(self, input_tokens: int) -> None:
        """运行中刷新窗口占比（agent_loop 每轮 usage 到达时调用）。

        占比原先只在用户轮结束（agent.py 轮末）更新，AI 自我运行期间连发
        多轮请求也读不到真实压力，运行中自查（mid_run_guard）因此永远不触发。
        """
        self._context.update_ratio(input_tokens)

    def mid_run_guard(self) -> bool:
        """运行中自查：占比超阈值则先压缩再发请求（无新用户输入）。

        由 agent_loop 在每轮请求发出前调用。AI 自我运行期间不经过用户轮
        边界，阈值预压缩（agent.py）无从触发，只能等 400 溢出兜底；此处
        补齐主动防线。压缩复用溢出恢复路径（compress_no_input）：压缩后
        以内部继续指令收尾，随后正常重发。
        返回 True=执行了压缩且成功。
        """
        if not self._context.need_compress():
            return False
        if not self._msg_manager.has_compressible_history(self._config.session.retain_tokens):
            self._logger.warning("compressor", "运行中占比超阈值，但保留预算内无可压缩历史，跳过本次压缩")
            return False
        self._logger.warning("compressor", "运行中占比超阈值，主动压缩历史")
        return self.compress_no_input()
