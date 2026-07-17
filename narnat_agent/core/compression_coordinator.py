"""压缩协调器 —— 上下文压缩流程编排

从 Agent._handle_compress() 提取。算法逻辑原样保留。
"""

from ..config.loader import Config
from .message_manager import MessageManager
from .llm import LLMClient
from .context import ContextManager
from ..tools.tool_context import ToolContext
from ..ui.ui_design import UIInterface
from ..ui.interrupt import _interrupt_ctrl
from ..logger import AgentLogger


class CompressionCoordinator:
    """上下文压缩协调器"""

    def __init__(self, config: Config, msg_manager: MessageManager,
                 llm: LLMClient, context: ContextManager,
                 tool_context: ToolContext, ui: UIInterface,
                 logger: AgentLogger):
        self._config = config
        self._msg_manager = msg_manager
        self._llm = llm
        self._context = context
        self._tool_context = tool_context
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
        result = self._msg_manager.handle_compress(
            pending_input,
            self._config.system_prompt,
            self._llm,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            on_interrupt=on_interrupt,
            on_llm_error=on_llm_error,
        )
        if result:
            self._ui.end_compressing()
            self._context.reset()
            self._tool_context.clear_read_files()
        return result
