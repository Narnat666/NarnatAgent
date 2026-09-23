"""压缩协调器 —— 上下文压缩流程编排

从 Agent._handle_compress() 提取。算法逻辑原样保留。
溢出恢复入口（compress_no_input）供 agent_loop 在请求被 400 上下文超限
拒绝时使用：压缩后追加内部继续指令，不携带新用户输入。
手动压缩入口（compress_manual）供 /compact 命令使用：不携带任何输入。
"""

from typing import Tuple

from ..config.loader import Config
from .message_manager import MessageManager
from .llm import LLMClient
from .context import ContextManager
from ..ui.ui_design import UIInterface
from ..ui.interrupt import _interrupt_ctrl
from ..logger import AgentLogger

# 溢出恢复压缩成功后追加的内部继续消息：告知模型历史已被压缩、任务继续。
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


class CompressionCoordinator:
    """上下文压缩协调器"""

    def __init__(self, config: Config, msg_manager: MessageManager,
                 llm: LLMClient, context: ContextManager,
                 ui: UIInterface,
                 logger: AgentLogger):
        self._config = config
        self._msg_manager = msg_manager
        self._llm = llm
        self._context = context
        self._ui = ui
        self._logger = logger

    def compress(self, pending_input: str) -> bool:
        """处理上下文压缩。成功=True，失败/中断=False。"""
        def on_interrupt():
            self._ui.end_compressing()
            self._context.reset()
            self._msg_manager.append_user(pending_input)

        def on_llm_error(msg):
            self._ui.end_compressing()
            self._logger.error("compressor", msg)
            self._context.set_retry_soon()
            self._msg_manager.append_user(pending_input)

        self._ui.begin_compressing()
        res = self._msg_manager.handle_compress(
            pending_input,
            self._config.system_prompt,
            self._llm,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            on_interrupt=on_interrupt,
            on_llm_error=on_llm_error,
            retain_tokens=self._config.session.retain_tokens,
        )
        if res.ok:
            self._ui.end_compressing()
            self._context.reset()
        return res.ok

    def compress_no_input(self) -> bool:
        """溢出恢复压缩：请求被 400 上下文超限拒绝后调用，无新用户输入。

        成功后消息以内部继续指令收尾（OpenAI 协议要求消息序列以
        user/assistant 收尾，system 兜底不可行），调用方随后重发请求。
        """
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
        )
        if res.ok:
            self._ui.end_compressing()
            self._context.reset()
        return res.ok

    def compress_manual(self) -> Tuple[str, str]:
        """手动压缩（/compact 命令）：不受占比阈值限制，不携带新输入。

        与自动压缩的区别：压缩后不追加任何 user 消息（命令不是对话内容）；
        失败/取消不改动占比与重试状态（历史未变，真实 token 数不变）。
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
        self._logger.warning("compressor", "运行中占比超阈值，主动压缩历史")
        return self.compress_no_input()
