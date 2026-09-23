"""流式输出会话句柄 —— 交互模式的 OutputSink / InteractionPort 实现。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「流式输出会话句柄协议」：创建时进入运行模式并启动思考动画；`feed` 收到文本块
  即停止动画并交渲染器增量渲染；`flush` 落定守卫（从未注入内容时不动作）；
  `pause` / `resume` 并行计数（首个暂停者停止动画，计数归零才重启、不为负、
  句柄已中止后不重启）；`finish` 切回输入模式 → 停止动画 → 落定缓冲 → 按
  `with_stats` 显示统计栏；`abort` 标记已中止 → 切回输入模式 → 停止动画 →
  无消息显示默认打断提示、有消息原样输出；`restart_attempt` 重试重播（缓冲重置）；
  状态查询 `cancelled`（进程级中断）与 `aborted`（中止标记）；
- 「统计栏」：`stats_line` 的逐字段格式与开关联动；
- 「打断提示与输入态恢复」：默认打断提示文案与输入态恢复时机；
- `contracts.output` 的 `OutputSink` / `InteractionPort` / `TurnStats` 语义。

分工（design D4/D8）：渲染器（`StreamingRenderer`）是本模块内部件、不外露；思考中
动画、落定守卫、统计栏、打断提示与中断模式切换全部收口在 `UiSink` 内；会话级交互
（创建流句柄、确认读取、中断收敛）由 `UiInteraction` 实现，conversation 只依赖端口。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from ..contracts import TurnStats
from ..contracts.interrupt import InterruptSignal
from ..output import Console, DisplayState, Theme
from .animator import SPINNER_DELAY, SPINNER_LABEL, Animation
from .commands import CommandResult, CommandRouter
from .markdown import MarkdownStyler
from .prompt import InputSession, write_banner
from .render import StreamingRenderer

__all__ = [
    "UiInteraction",
    "UiSink",
    "format_max_tokens",
    "format_tokens",
    "stats_line",
    "write_notice",
]

# 默认打断提示文案（无自定义消息时的两行提示）
INTERRUPTED_TEXT = "已打断"
INTERRUPTED_HINT = "继续..."


def format_tokens(value: int) -> str:
    """token 数格式化：不小于 1000 显示 `x.xk`（1 位小数），否则显示原值。"""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def format_max_tokens(value: int) -> str:
    """最大输出格式化：不小于 1000 显示取整 k，否则显示原值。"""
    return f"{value / 1000:.0f}k" if value >= 1000 else str(value)


def stats_line(theme: Theme, display: DisplayState, stats: TurnStats) -> str:
    """统计行文本（不含分隔线；开关每次显示时从 `display` 实时读取）。

    结构：`输入:{输入} 输出:{输出}{缓存段}{思考段}{窗口占比段}  最大输出:{上限}`
    `{费用段}{余额段}`——缓存段仅比例大于 0 时显示（上限 100%，1 位小数）；思考段
    恒显示；窗口占比段仅开关开启时显示（窗口或输入无效时 `--`）；费用段仅"显示费用"
    开启时显示（4 位小数）；余额段仅"显示余额"开启且余额大于 0 时显示（2 位小数）。
    """
    input_text = format_tokens(stats.input_tokens)
    output_text = format_tokens(stats.output_tokens)
    max_text = format_max_tokens(display.max_tokens)
    cache = (
        f"  缓存:{min(100.0, stats.cache_ratio * 100):.1f}%"
        if stats.cache_ratio > 0
        else ""
    )
    thinking = f"  思考:{stats.thinking_effort}"
    ratio = ""
    if display.show_ratio:
        if display.context_window > 0 and stats.input_tokens > 0:
            ratio = f"  窗口占比:{stats.input_tokens / display.context_window * 100:.0f}%"
        else:
            ratio = "  窗口占比:--"
    cost = f"  费用:¥{stats.cost:.4f}" if display.show_cost else ""
    balance = (
        f"  余额:¥{stats.balance:.2f}"
        if display.show_balance and stats.balance > 0
        else ""
    )
    return (
        f"  {theme.ui_stats_label}输入:{input_text} 输出:{output_text}"
        f"{cache}{thinking}{ratio}  最大输出:{max_text}{theme.rst}"
        f"{theme.ui_stats_value}{cost}{balance}{theme.rst}\n"
    )


def write_notice(styler: MarkdownStyler, console: Console, text: str) -> None:
    """按段落形态逐行写出系统提示文本（空白行不产生输出）。

    提示文本不走内容增量缓冲与表格 / 代码块状态机（不经流式管线），但形态与内容行
    一致：两空格缩进 + 行内着色，纯文本模式下即纯文本（结构与旧实现经内容管线落到
    段落的结果相同）。
    """
    for line in text.split("\n"):
        rendered = styler.render_line(line)
        if rendered:
            console.write(rendered + "\n")


class UiSink:
    """交互模式的流式输出会话句柄（`contracts.output.OutputSink` 实现）。

    一个 AI 回合一个句柄；回合内多轮复用同一句柄时用 `begin()` 重启思考动画。
    线程安全：并行工具线程会并发调用 `pause` / `resume`（计数经互斥锁保护）。
    """

    def __init__(
        self,
        theme: Theme,
        console: Console,
        interrupt: InterruptSignal,
        display: DisplayState,
        *,
        renderer: Optional[StreamingRenderer] = None,
        spinner: Optional[Animation] = None,
    ):
        """
        Args:
            theme: 颜色体系（统计栏、提示行、打断提示取色）；
            console: 输出原语（写出与纯文本模式判定）；
            interrupt: 中断信号总线（`cancelled` 查询与运行 / 输入模式切换）；
            display: 显示开关五参数（统计栏开关实时读取）；
            renderer: 渲染器（缺省内部构造；测试可注入替身）；
            spinner: 思考中动画（缺省内部构造带 0.666 秒延迟的动画；测试可注入替身）。
        """
        self._theme = theme
        self._console = console
        self._interrupt = interrupt
        self._display = display
        self._styler = MarkdownStyler(theme, console)
        self._renderer = renderer if renderer is not None else StreamingRenderer(theme, console)
        self._spinner = (
            spinner
            if spinner is not None
            else Animation(console, theme, SPINNER_LABEL, delay=SPINNER_DELAY)
        )
        self._started = False        # 是否注入过内容（落定守卫）
        self._aborted = False        # 中止标记
        self._pause_count = 0        # 并行暂停计数
        self._pause_lock = threading.Lock()

    # ── 状态查询 ──

    @property
    def cancelled(self) -> bool:
        """取消查询：反映进程级中断状态。"""
        return self._interrupt.is_set

    @property
    def aborted(self) -> bool:
        """已中止查询：反映中止标记。"""
        return self._aborted

    # ── 生命周期 ──

    def begin(self) -> None:
        """启动思考动画（回合内多轮复用同一句柄时用于重启；幂等）。"""
        self._spinner.start()

    # ── 内容注入与缓冲 ──

    def feed(self, text: str) -> None:
        """内容增量：停止思考动画并交给渲染器增量渲染。"""
        self._started = True
        self._stop_animation()
        self._renderer.feed(text)

    def notify(self, text: str) -> None:
        """状态提示行（重试提示、压缩提示等）：先落定缓冲，再按段落形态写出。"""
        self._stop_animation()
        self._renderer.flush()
        write_notice(self._styler, self._console, text)

    def flush(self) -> None:
        """落定渲染缓冲（守卫：从未注入内容时不转发，对应「落定守卫」）。"""
        if self._started:
            self._renderer.flush()

    def restart_attempt(self) -> None:
        """重试重播：此前未完成内容作废（渲染缓冲与状态清理、落定守卫复位）。

        在服务端流中断自动重试之前调用，防止残留与重播内容拼接错乱。
        """
        self._renderer.reset()
        self._started = False

    # ── 动画并行暂停 / 恢复 ──

    def pause(self) -> None:
        """暂停思考动画（并行计数 +1；首个暂停者真正停止动画）。"""
        with self._pause_lock:
            self._pause_count += 1
            first = self._pause_count == 1
        if first:
            self._stop_animation()

    def resume(self) -> None:
        """恢复思考动画（计数递减且不为负；归零时才重启；已中止后不再启动）。"""
        if self._aborted:
            return
        with self._pause_lock:
            self._pause_count = max(0, self._pause_count - 1)
            restart = self._pause_count == 0
        if restart:
            self._spinner.start()

    # ── 收尾 ──

    def finish(self, stats: TurnStats, with_stats: bool = True) -> None:
        """收尾：切回输入模式 → 停止动画 → 落定缓冲 → 按 `with_stats` 显示统计栏。"""
        self._interrupt.enter_input_mode()
        self._stop_animation()
        self._renderer.flush()
        if with_stats:
            self.show_stats(stats)

    def show_stats(self, stats: TurnStats) -> None:
        """显示统计栏：先输出分隔线，再输出单行统计（开关实时读取）。"""
        self._styler.separator()
        self._console.write(stats_line(self._theme, self._display, stats))

    def abort(self, message: Optional[str] = None) -> None:
        """中止：标记已中止 → 切回输入模式 → 停止动画 → 显示打断提示或自定义消息。"""
        self._aborted = True
        self._interrupt.enter_input_mode()
        self._stop_animation()
        if message is None:
            theme = self._theme
            self._console.write(
                f"\n  {theme.ui_interrupted}{INTERRUPTED_TEXT}{theme.rst}\n"
                f"  {theme.ui_interrupted_hint}{INTERRUPTED_HINT}{theme.rst}\n"
            )
        else:
            self._console.write(message + "\n")

    def _stop_animation(self) -> None:
        """停止思考动画（未启动时幂等无输出）。"""
        self._spinner.stop()


class UiInteraction:
    """交互模式的会话级交互端口（`contracts.output.InteractionPort` 实现）。

    同时承载主循环入口：`start`（启动横幅）、`read_input`（读取一次输入）、
    `dispatch_command`（命令路由）。headless 的同形实现见 `headless.HeadlessInteraction`。
    """

    def __init__(
        self,
        theme: Theme,
        console: Console,
        interrupt: InterruptSignal,
        display: DisplayState,
        input_session: InputSession,
        router: "CommandRouter",
        *,
        sink_factory: Optional[Callable[[], UiSink]] = None,
    ):
        """
        Args:
            theme / console: 颜色体系与输出原语（启动横幅取色与写出）；
            interrupt: 中断信号总线（运行 / 输入模式切换）；
            display: 显示开关（透传给流句柄）；
            input_session: 输入会话（`prompt.InputSession`）；
            router: 命令路由（`commands.CommandRouter`）；
            sink_factory: 流句柄工厂（缺省内部构造 `UiSink`；测试可注入替身）。
        """
        self._theme = theme
        self._console = console
        self._interrupt = interrupt
        self._display = display
        self._input = input_session
        self._router = router
        self._make_sink = sink_factory or (
            lambda: UiSink(theme, console, interrupt, display)
        )

    def start(self, model_name: str) -> None:
        """启动界面：进入输入模式、输出横幅（模型名，主色，两空格缩进）与分隔线，
        并建立输入会话（失败即上抛，与旧实现启动时机一致）。"""
        self._interrupt.enter_input_mode()
        write_banner(self._theme, self._console, model_name)
        self._input.ensure_ready()

    def begin_turn(self) -> UiSink:
        """开始一个 AI 回合：进入运行模式（清除中断标志并开始 ESC 打断监听）、
        创建流句柄并启动思考动画。"""
        self._interrupt.enter_run_mode()
        sink = self._make_sink()
        sink.begin()
        return sink

    def read_input(self) -> Optional[str]:
        """读取一次用户输入（空输入即 None，调用方按继续处理）。"""
        return self._input.read()

    def read_confirmation(self, prompt: str) -> bool:
        """在自定义提示符下读取一次确认：`y` / `yes`（忽略大小写与首尾空白）为 True。"""
        answer = self._input.read(prompt)
        return (answer or "").strip().lower() in ("y", "yes")

    def notify_interrupted(self) -> None:
        """中断收敛回调：停止采集、切回输入模式并重建输入会话（丢弃残留编辑缓冲）。"""
        self._interrupt.enter_input_mode()
        self._input.rebuild()

    def dispatch_command(self, command: str, args: str) -> CommandResult:
        """命令分发入口（首个词 + 参数字符串；归一与门禁见 `commands.CommandRouter`）。"""
        line = f"{command} {args}" if args else command
        return self._router.dispatch(line)
