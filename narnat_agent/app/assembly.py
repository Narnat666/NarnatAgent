"""应用装配 —— 唯一构造点（design D8：线性装配，拒绝后置补线）。

构造顺序即依赖顺序；全部组件经构造注入取得依赖，装配产物不含散落 lambda
（必要的适配一律为有类型的小对象或绑定方法）。现状 4 处后置补线的消解：

1. 会话总结动画（旧 `session_mgr.summary_anim_*` 后置赋值）→ `sessions` 依赖
   `contracts.output.Animator` 端口，构造注入（交互 `UiAnimator` / headless
   `HeadlessAnimator`）；
2. 模型切换同步费用（旧直改 `stats._model`）→ `ModelState(on_model_change=
   stats.set_model)` 构造注入；
3. `/compact` 手动压缩（旧 `session_mgr.compact_func` 后置赋值）→ 调整构造顺序
   （先 `compression` 后 `sessions`），`compact_func=coordinator.compress_manual`
   构造注入；
4. MCP 工具热注册（旧 `mcp_manager.set_tool_sinks` 后置接线）→ `McpManager` 依赖
   `contracts.tool.ToolCatalog` 端口，构造注入 `ToolCatalogImpl(registry, …)`。

契约来源：`openspec/changes/recast-v2/specs/app/spec.md`「装配次序与依赖注入」
（23 步次序与三项"依赖就绪即接线"）、「headless 装配差异」（确认回调缺省、
纯文本界面）；对照现状 `narnat_agent/assembly.py`。

依赖规则（design D1）：本模块位于 L4，可依赖全部低层积木。
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from ..compression import CompressionContext, CompressionCoordinator
from ..config import defaults as config_defaults
from ..config import load_config
from ..conversation import DELETE_CONFIRM_PROMPT, ConversationLoop, ToolDispatcher
from ..conversation import GoalMode as GoalRunner
from ..contracts.interrupt import InterruptSignal
from ..interrupt import InterruptBus
from ..llm import LLMClient
from ..mcp import McpManager, McpTool
from ..messages import MessageStore
from ..output import Console, DisplayState, Theme
from ..sessions import GoalMode as SessionGoalMode
from ..sessions import ModelState, SessionManager, SessionStore
from ..stats import StatsTracker
from ..tools import (
    DeleteGateImpl,
    GoalStateImpl,
    PlanTrackerImpl,
    ReminderStateImpl,
    ToolCatalogImpl,
    ToolEnvImpl,
    ToolRegistry,
    ToolSettingsImpl,
    error_line,
)
from ..tools.file import build_file_tools
from ..tools.plan import GOAL_COMPLETE_DEFINITION, register_plan_tools
from ..tools.remote import RemoteFileAccess, SerialTool, TerminalTool
from ..tools.shell import BackgroundManager, ShellRuntime, ShellTool
from ..tools.websearch import WebSearchTool
from ..ui import (
    CommandCompleter,
    CommandRouter,
    HeadlessAnimator,
    HeadlessInteraction,
    InputSession,
    UiAnimator,
    UiInteraction,
    colorize_diff,
    prepare_console,
)
from .lifecycle import AgentLogger
from .summarizer import Summarizer

__all__ = ["App", "AppParts", "DeleteConfirmer", "InterruptProbe", "build"]

# 共享线程池的固定工作线程数（spec「装配次序与依赖注入」第 16 步）
TOOL_POOL_WORKERS = 16

LOG_MODULE = "app"


class InterruptProbe:
    """取消查询适配 —— 把中断总线实例适配为无参可调用（交付物签名要求）。

    `sessions` 与 `compression` 的取消检查端口形如 `Callable[[], bool]`；
    中断总线的状态是 `is_set` 属性，本对象把它显式化（替代散落 lambda）。
    """

    def __init__(self, signal: InterruptSignal) -> None:
        self._signal = signal

    def __call__(self) -> bool:
        """当前是否已置中断标志。"""
        return self._signal.is_set


class DeleteConfirmer:
    """删除确认适配 —— 命令文本 → 提示符下的一次用户确认（仅 Windows 使用）。

    工具侧契约 `ToolEnv.confirm` 为 `Callable[[str], bool]`（参数为命令文本，
    旧实现忽略之）；本对象把确认转交界面交互端口，提示文案为已发布字面量。
    """

    def __init__(self, interaction) -> None:
        self._interaction = interaction

    def __call__(self, command: str) -> bool:
        """在提示符下询问：输入 y/yes 为确认，其余（含无交互环境）为取消。"""
        return self._interaction.read_confirmation(DELETE_CONFIRM_PROMPT)


class DiffColorizer:
    """远程文件 diff 的 UI 着色适配（devN 路径差异展示）。

    取色来自注入的颜色体系（`output.Theme`）：纯文本模式下颜色求值为空串，
    与本地文件工具（`DiffColors` 注入同一主题）及旧实现行为一致；替代现状在
    装配点散落的着色 lambda。
    """

    def __init__(self, theme: Theme) -> None:
        self._theme = theme

    def __call__(self, diff_text: str) -> str:
        """对 unified diff 文本着色（保留行首 +/- 与文本结构）。"""
        return colorize_diff(diff_text, self._theme)


@dataclass
class AppParts:
    """装配产物 —— 持有全部组件引用（spec「装配次序与依赖注入」第 23 步）。

    字段即组件图：主循环 / headless / 生命周期清理消费本容器的公开字段。
    """

    config: Any
    logger: AgentLogger
    console: Console
    theme: Theme
    display: DisplayState
    interrupt: InterruptBus
    registry: ToolRegistry
    llm: LLMClient
    mcp_manager: McpManager
    env: ToolEnvImpl
    store: MessageStore
    stats: StatsTracker
    context: CompressionContext
    coordinator: CompressionCoordinator
    summarizer: Summarizer
    session_mgr: SessionManager
    ui: Any
    dispatcher: ToolDispatcher
    conversation: ConversationLoop
    goal_runner: GoalRunner
    thread_pool: ThreadPoolExecutor
    terminal: TerminalTool
    serial: SerialTool
    remote_files: RemoteFileAccess
    background: BackgroundManager


def build(project_root: Optional[str] = None, debug: bool = False,
          headless: bool = False, tool_log: bool = False) -> AppParts:
    """线性装配全部组件并返回装配产物。

    Args:
        project_root: 项目根目录（缺省自动发现）；透传配置加载；
        debug: 调试模式（启动文件日志）；
        headless: headless 运行（纯文本界面、不注册删除确认回调）；
        tool_log: headless 是否显示工具调度日志（`-l`；交互模式忽略）。

    Returns:
        `AppParts`：全部组件引用（构造即依赖就绪，无后置补线）。
    """
    # 1. 配置加载（headless 标志影响系统提示词的 subagent:hide 剥离）
    config = load_config(project_root, headless)

    # 2. 输出原语与样式来源（headless 先置位纯文本输出环境），随后把费用、余额、
    #    最大输出 token、窗口占比、上下文窗口五项写回界面配置并应用样式
    console = Console()
    if headless:
        prepare_console(console, tool_log)
    theme = Theme(console)
    display = DisplayState()
    ui_cfg = dict(config.ui.raw)
    ui_cfg["show_cost"] = config.ui.show_cost
    ui_cfg["show_balance"] = config.ui.show_balance
    ui_cfg["max_output_tokens"] = config.ui.max_output_tokens
    ui_cfg["show_ratio"] = config.session.show_ratio
    ui_cfg["context_window"] = config.ai.context_window
    theme.apply_style(ui_cfg)
    display.apply_config(ui_cfg)

    # 3. 工具全局配置：终端/串口最大会话数经构造注入（现状全局 set_max_sessions 的
    #    消解；明示修复项：串口与终端共用同一配置来源），构造见第 5 步工具族。

    # 4. 日志器（仅调试模式启动文件日志，未启动时日志调用静默丢弃）
    logger = AgentLogger(config.paths.logs_dir)
    if debug:
        logger.start(config.paths.logs_dir)

    # 5. 中断总线（订阅者在各自构造时接线：llm / shell / terminal / serial / background）
    interrupt = InterruptBus()

    # 6. 工具运行时状态（状态归主：计划 / 目标标记 / 提醒标志 / 删除确认门）
    plan_tracker = PlanTrackerImpl()
    goal_tracker = GoalStateImpl()
    reminders = ReminderStateImpl()
    delete_gate = DeleteGateImpl()

    # 7. 工具族构造与注册（注册顺序即工具定义列表顺序：文件族 → Shell → Terminal →
    #    WebSearch → 计划族 → Serial → MCP；GoalComplete 定义默认隐藏）
    runtime = ShellRuntime()
    background = BackgroundManager(runtime)
    shell_tool = ShellTool(runtime=runtime, background=background, signal=interrupt)
    terminal = TerminalTool(max_sessions=config.tools.max_sessions)
    serial = SerialTool(max_sessions=config.tools.max_sessions)
    # 中断订阅（ESC 打断）：远程与串口会话发送中断字符（前台命令经 ShellTool 构造
    # 时自行订阅、LLM 活动连接经 LLMClient 构造时自行订阅）
    interrupt.subscribe(terminal.kill_active_exec)
    interrupt.subscribe(serial.kill_active_exec)
    remote_files = RemoteFileAccess(terminal, DiffColorizer(theme))
    registry = ToolRegistry()
    for tool in build_file_tools(remote=remote_files, colors=theme):
        registry.register(tool)
    registry.register(shell_tool)
    registry.register(terminal)
    registry.register(WebSearchTool())
    register_plan_tools(registry, theme=theme, background=background)
    registry.register(serial)
    registry.register(McpTool(error_line))

    # 8. 消息历史（唯一所有者）与统计追踪
    store = MessageStore(config.system_prompt, log=logger)
    stats = StatsTracker(
        config.ai.model,
        config.pricing.user_pricing,
        config.balance,
        config.cost_log,
    )

    # 9. LLM 客户端（注入最大输出 token、工具定义快照与中断总线；thinking 解析
    #    函数按 llm 积木的必需注入约定取 config.defaults）
    llm = LLMClient(
        config.ai,
        logger,
        max_output_tokens=config.ui.max_output_tokens,
        tool_definitions=registry.get_tool_definitions(),
        interrupt=interrupt,
        goal_tool_definition=GOAL_COMPLETE_DEFINITION,
        thinking_rules=config_defaults,
    )

    # 10. MCP 连接管理器（懒连接，不发起连接）与工具热更新端口（ToolCatalog 消解
    #     现状 set_tool_sinks 后置接线）
    catalog = ToolCatalogImpl(
        registry, llm.add_tool_definitions, llm.remove_tool_definitions
    )
    mcp_manager = McpManager(catalog, error_line, logger)

    # 11. 压缩动画端口（交互为原地刷新动画，headless 为空实现）
    animator = HeadlessAnimator() if headless else UiAnimator(console, theme)

    # 12. 上下文占比基准与压缩协调器（先于会话管理器构造：`/compact` 经构造注入）
    context = CompressionContext(
        config.ai.context_window,
        config.session.warn_ratio,
        config.session.compress_ratio,
        logger,
    )
    coordinator = CompressionCoordinator(
        config, store, llm, context, animator, interrupt, logger
    )

    # 13. 摘要器（模型命名与模型总结，绑定方法注入会话管理器）
    summarizer = Summarizer(llm, SessionStore(config.paths.narnat_dir), logger)

    # 14. 目标模式（会话侧开关 + 轮后判定状态机）
    goal_runner = GoalRunner(goal_tracker, config.ai.goal_max_rounds)
    session_goal = SessionGoalMode(config.ai.goal_max_rounds, tool_gate=llm.set_goal_tool)

    # 15. 思考/模型配置句柄（模型切换同时写配置与统计：构造注入 stats.set_model）
    model_state = ModelState(
        config.ai, config.paths.config_dir, on_model_change=stats.set_model
    )

    # 16. 会话管理器（总结动画、手动压缩、目标模式、配置句柄全部构造注入）
    session_mgr = SessionManager(
        SessionStore(config.paths.narnat_dir),
        store,
        goal=session_goal,
        model_state=model_state,
        theme=theme,
        animator=animator,
        name_func=summarizer.name_session,
        summarize_func=summarizer.summarize,
        cancel_check=InterruptProbe(interrupt),
        compact_func=coordinator.compress_manual,
        auto_save_enabled=config.session.auto_save,
        auto_save_tokens=config.session.auto_save_tokens,
        skill_roots=config.skills.project_roots,
        skill_ignore_dirs=config.tools.ignore_dirs,
    )

    # 17. 界面（headless → 纯文本流与无交互替代；否则交互界面 + 命令路由 +
    #     输入会话与补全器）
    if headless:
        ui = HeadlessInteraction(theme, console)
    else:
        completer = CommandCompleter(session_mgr)
        input_session = InputSession(
            theme, interrupt, data_dir=config.paths.data_dir, completer=completer
        )
        ui = UiInteraction(
            theme, console, interrupt, display, input_session,
            CommandRouter(session_mgr, console),
        )

    # 18. 工具运行时门面（删除确认回调依赖界面就绪；headless 恒为 None）
    confirm = None
    if not headless and sys.platform == "win32":
        confirm = DeleteConfirmer(ui)
    env = ToolEnvImpl(
        settings=ToolSettingsImpl(
            ignore_dirs=list(config.tools.ignore_dirs),
            max_tool_output_chars=config.tools.max_output_chars,
            max_timeout_seconds=config.tools.max_timeout_seconds,
            max_transfer_mb=config.tools.max_transfer_mb,
            git_skip_confirm=config.safety.git_skip_confirm,
            rm_skip_confirm=config.safety.rm_skip_confirm,
            require_plan=config.plan.require_plan,
            min_tools=config.plan.min_tools,
            api_keys=dict(config.api_keys),
        ),
        plan=plan_tracker,
        goal=goal_tracker,
        reminders=reminders,
        delete_gate=delete_gate,
        confirm=confirm,
        mcp=mcp_manager,
    )

    # 19. 工具调度器（共享线程池由本装配产物持有，生命周期归 app）
    thread_pool = ThreadPoolExecutor(max_workers=TOOL_POOL_WORKERS)
    dispatcher = ToolDispatcher(
        registry,
        env,
        thread_pool,
        console,
        theme=theme,
        interrupt=interrupt,
        resolve_device=terminal.resolve_dev_display,
        logger=logger,
    )

    # 20. 对话内循环（消息、调度、工具上下文、统计、界面、配置、日志、压缩协调器）
    conversation = ConversationLoop(
        llm=llm,
        store=store,
        dispatcher=dispatcher,
        env=env,
        stats=stats,
        interaction=ui,
        console=console,
        ai_options=config.ai,
        data_dir=config.paths.data_dir,
        background=background,
        compression=coordinator,
        ratio=context,
        logger=logger,
    )

    return AppParts(
        config=config,
        logger=logger,
        console=console,
        theme=theme,
        display=display,
        interrupt=interrupt,
        registry=registry,
        llm=llm,
        mcp_manager=mcp_manager,
        env=env,
        store=store,
        stats=stats,
        context=context,
        coordinator=coordinator,
        summarizer=summarizer,
        session_mgr=session_mgr,
        ui=ui,
        dispatcher=dispatcher,
        conversation=conversation,
        goal_runner=goal_runner,
        thread_pool=thread_pool,
        terminal=terminal,
        serial=serial,
        remote_files=remote_files,
        background=background,
    )


class App:
    """应用门面 —— 唯一装配点的构造入口与两条运行入口。

    - `App(...)`：执行线性装配（`build`）并持有装配产物；
    - `run()`：交互主循环；
    - `run_headless(task, max_rounds)`：headless 一次性任务。
    """

    def __init__(self, project_root: Optional[str] = None, debug: bool = False,
                 headless: bool = False, tool_log: bool = False):
        self.parts = build(project_root, debug, headless, tool_log)

    def run(self) -> None:
        """运行交互主循环（见 `app.interactive`）。"""
        from .interactive import run_interactive

        run_interactive(self.parts)

    def run_headless(self, task: str, max_rounds: int = 0) -> None:
        """运行 headless 一次性任务（见 `app.headless`）。"""
        from .headless import run_headless

        run_headless(self.parts, task, max_rounds=max_rounds)
