"""组装层 —— 唯一组装点

构造所有对象，注入依赖。Agent 不需要知道任何子模块的构造细节。
依赖顺序就是代码顺序，Python的顺序执行特性使依赖链一目了然。
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .config.loader import Config, load_config
from .core.llm import LLMClient, set_retry_count
from .core.context import ContextManager
from .core.compressor import Compressor
from .core.tool_dispatcher import ToolDispatcher
from .core.message_manager import MessageManager
from .core.message_list import MessageList
from .core.stats import StatsTracker
from .core.session_callbacks import SessionManager
from .core.tool_callbacks import SafetyCallbacks, TodoCallbacks
from .core.agent_loop import AgentLoop
from .core.summarizer import Summarizer
from .core.auto_save_manager import AutoSaveManager
from .core.compression_coordinator import CompressionCoordinator
from .tools.tool_context import ToolContext
from .tools.terminal import set_max_sessions, cleanup as _terminal_cleanup
from .tools.bash import cleanup as _bash_cleanup
from .ui.ui_design import UIInterface, apply_style
from .ui.interrupt import _interrupt_ctrl
from .logger import AgentLogger
from .output import write as _stdout_write, D, E, R


class Assembly:
    """唯一组装点 — 构造所有对象，注入依赖"""

    @classmethod
    def build(cls, project_root: Optional[str] = None, debug: bool = False) -> 'AssemblyResult':
        # 1. 配置
        config = load_config(project_root)

        # 2. UI 样式（show_cost/show_balance/max_tokens 被 loader 弹出到 UIConfig 属性，
        #   需补回 raw 中供 apply_style 读取）
        config.ui.raw["show_cost"] = config.ui.show_cost
        config.ui.raw["show_balance"] = config.ui.show_balance
        config.ui.raw["max_output_tokens"] = config.ui.max_output_tokens
        apply_style(config.ui.raw)

        # 3. 全局工具配置（过渡期保留全局 setter，Phase 5 消除）
        set_max_sessions(config.tools.max_sessions)
        set_retry_count(config.ai.retry_count)

        # 4. 日志
        logger = AgentLogger(config.paths.logs_dir)
        if debug:
            logger.start(config.paths.logs_dir)

        # 5. 工具定义（LLM 需要它，但 LLM 不直接依赖 tools 包）
        from .tools.registry import get_tool_definitions
        tool_definitions = get_tool_definitions()

        # 6. LLM
        llm = LLMClient(
            config.ai,
            logger,
            max_output_tokens=config.ui.max_output_tokens,
            tool_definitions=tool_definitions,
        )

        # 6. 上下文管理
        context = ContextManager(
            logger,
            config.session.warn_turn_1,
            config.session.warn_turn_2,
            config.session.compress_turn,
        )

        # 7. 压缩器
        compressor = Compressor()

        # 8. 消息列表（唯一所有者）
        message_list = MessageList(config.system_prompt)
        msg_manager = MessageManager(message_list, compressor, logger)

        # 9. 摘要器
        summarizer = Summarizer(llm, config, logger)

        # 10. 会话管理器
        session_mgr = SessionManager(
            config.paths.narnat_dir,
            message_list,
            context_manager=context,
            config_dir=config.paths.config_dir,
            thinking_effort_getter=lambda: config.ai.thinking_effort,
            thinking_effort_setter=lambda v: setattr(config.ai, 'thinking_effort', v),
            thinking_options=config.ai.thinking_options,
            summarize_func=lambda msgs, cancel: summarizer.summarize(msgs, cancel),
            summary_anim_start=None,  # 由 Agent 在 run() 中设置
            summary_anim_stop=None,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            name_func=lambda msgs: summarizer.name_session(msgs),
        )

        # 11. UI
        ui = UIInterface(config.ai.model, session_mgr)

        # 补充 session_mgr 的 UI 回调（需要 ui 先创建）
        session_mgr.summary_anim_start = lambda: ui.begin_summarizing()
        session_mgr.summary_anim_stop = lambda: ui.end_summarizing()

        # 12. 工具上下文
        tool_context = ToolContext(
            confirm_callback=SafetyCallbacks.confirm_delete if sys.platform == "win32" else None,
            ui_callback=TodoCallbacks.on_todo_update,
            api_keys=config.api_keys,
            ignore_dirs=list(config.tools.ignore_dirs),
            git_skip_confirm=config.safety.git_skip_confirm,
            rm_skip_confirm=config.safety.rm_skip_confirm,
            max_transfer_mb=config.tools.max_transfer_mb,
            max_tool_output_chars=config.tools.max_output_chars,
            require_plan=config.plan.require_plan,
            min_tools=config.plan.min_tools,
        )

        # 13. 工具调度器
        dispatcher = ToolDispatcher(
            tool_context,
            ThreadPoolExecutor(max_workers=16),
            logger,
        )

        # 14. 统计追踪
        stats = StatsTracker(
            config.ai.model,
            config.pricing.user_pricing,
            config.balance,
        )

        # 15. 自动保存管理器
        auto_save_mgr = AutoSaveManager(
            config, message_list, session_mgr, summarizer, logger,
        )

        # 16. 压缩协调器
        compression_coordinator = CompressionCoordinator(
            config, msg_manager, llm, context, tool_context, ui, logger,
        )

        # 17. AgentLoop
        agent_loop = AgentLoop(
            llm, msg_manager, dispatcher, tool_context,
            stats, ui, config, logger,
        )

        return AssemblyResult(
            config=config,
            logger=logger,
            llm=llm,
            context=context,
            message_list=message_list,
            msg_manager=msg_manager,
            session_mgr=session_mgr,
            ui=ui,
            tool_context=tool_context,
            dispatcher=dispatcher,
            stats=stats,
            auto_save_mgr=auto_save_mgr,
            compression_coordinator=compression_coordinator,
            agent_loop=agent_loop,
        )


class AssemblyResult:
    """组装结果 — 持有所有构造好的对象引用"""

    def __init__(self, config, logger, llm, context, message_list,
                 msg_manager, session_mgr, ui, tool_context, dispatcher,
                 stats, auto_save_mgr, compression_coordinator, agent_loop):
        self.config = config
        self.logger = logger
        self.llm = llm
        self.context = context
        self.message_list = message_list
        self.msg_manager = msg_manager
        self.session_mgr = session_mgr
        self.ui = ui
        self.tool_context = tool_context
        self.dispatcher = dispatcher
        self.stats = stats
        self.auto_save_mgr = auto_save_mgr
        self.compression_coordinator = compression_coordinator
        self.agent_loop = agent_loop
