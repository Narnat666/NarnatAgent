"""headless 纯文本输出 —— OutputSink / InteractionPort / Animator 的无交互实现。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`「headless 纯文本输出」：
- 全局去色（所有颜色求值为空、无 ANSI 转义、输出行不补行首回车——由 `Console`
  纯文本模式承担）、渲染结构（表格边框、列表缩进、代码块行号）保留；
- 不显示动画与统计栏、不读取输入、不监听打断、不自动保存、不查询余额；
- 工具调度日志默认静默（不输出工具调用摘要、计划更新等调度行），`-l` 开启全量；
- 流会话替身：取消查询恒为假（不可打断）；中止置位中止标记；收尾忽略全部统计参数
  （不显示统计栏）；中止时仅当消息非空才输出（无默认打断提示）；开始 / 暂停 / 恢复
  动画无效果；输入读取恒返回空；命令分发恒返回未识别。

结构（design D4/D7）：与交互模式共用契约端口（`contracts.output`），全部实现为
纯文本、无副作用；终端状态（纯文本 / 静默开关）由装配侧经 `prepare_console` 一次性
置位，本模块的实现自身不改全局态。
"""
from __future__ import annotations

import sys
from typing import Callable, IO, Optional

from ..contracts import TurnStats
from ..output import Console, Theme
from .commands import CommandResult
from .markdown import MarkdownStyler
from .render import StreamingRenderer
from .stream import write_notice

__all__ = [
    "HeadlessAnimator",
    "HeadlessInteraction",
    "HeadlessSink",
    "enable_utf8_stdout",
    "prepare_console",
]


def enable_utf8_stdout(stream: Optional[IO[str]] = None) -> None:
    """把标准输出切到 UTF-8（headless 启动时一次；失败静默）。

    仅对支持 `reconfigure` 的文本流生效（Windows 控制台默认代码页会让中文输出
    乱码）；管道 / 重定向或包装流不支持时静默跳过。
    """
    target = sys.stdout if stream is None else stream
    try:
        target.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass


def prepare_console(
    console: Console, tool_log: bool = False, stream: Optional[IO[str]] = None
) -> None:
    """置位 headless 输出环境：强制 UTF-8 + 全局去色 + 工具调度日志静默。

    Args:
        console: 输出原语（置纯文本模式与静默开关）；
        tool_log: 是否显示工具调度日志（False 默认静默，True 即 `-l` 全量）；
        stream: 强制 UTF-8 的输出流（缺省标准输出；测试可注入）。
    """
    enable_utf8_stdout(stream)
    console.set_plain(True)
    console.set_quiet_tools(not tool_log)


class HeadlessSink:
    """headless 流会话替身（`contracts.output.OutputSink` 的纯文本实现）。

    无动画、无统计栏、无中断联动：`feed` 直接交渲染器（纯文本模式下结构保留），
    收尾只落定内容；动画相关调用（`begin` / `pause` / `resume`）无效果。
    """

    def __init__(
        self,
        theme: Theme,
        console: Console,
        *,
        renderer: Optional[StreamingRenderer] = None,
    ):
        """
        Args:
            theme: 颜色体系（纯文本模式下求值为空，仍用于渲染结构）；
            console: 输出原语（纯文本模式由装配侧置位）；
            renderer: 渲染器（缺省内部构造；测试可注入替身）。
        """
        self._console = console
        self._styler = MarkdownStyler(theme, console)
        self._renderer = (
            renderer if renderer is not None else StreamingRenderer(theme, console)
        )
        self._started = False
        self._aborted = False

    # ── 状态查询 ──

    @property
    def cancelled(self) -> bool:
        """取消查询：headless 恒为假（不可打断）。"""
        return False

    @property
    def aborted(self) -> bool:
        """已中止查询：反映中止标记。"""
        return self._aborted

    # ── 生命周期与动画（无效果） ──

    def begin(self) -> None:
        """开始等待提示：headless 无动画，无效果。"""

    def pause(self) -> None:
        """暂停动画：headless 无动画，无效果。"""

    def resume(self) -> None:
        """恢复动画：headless 无动画，无效果。"""

    # ── 内容注入与缓冲 ──

    def feed(self, text: str) -> None:
        """内容增量：直接交渲染器增量渲染。"""
        self._started = True
        self._renderer.feed(text)

    def notify(self, text: str) -> None:
        """状态提示行：先落定缓冲，再按纯文本段落形态写出。"""
        self._renderer.flush()
        write_notice(self._styler, self._console, text)

    def flush(self) -> None:
        """落定渲染缓冲（守卫：从未注入内容时不转发）。"""
        if self._started:
            self._renderer.flush()

    def restart_attempt(self) -> None:
        """重试重播：此前未完成内容作废（渲染缓冲与状态清理、落定守卫复位）。"""
        self._renderer.reset()
        self._started = False

    # ── 收尾 ──

    def finish(self, stats: Optional[TurnStats] = None, with_stats: bool = False) -> None:
        """收尾：只落定内容（忽略全部统计参数，不显示统计栏）。"""
        self._renderer.flush()

    def abort(self, message: Optional[str] = None) -> None:
        """中止：置位中止标记 → 落定内容 → 仅当消息非空时原样输出（无默认提示）。"""
        self._aborted = True
        self._renderer.flush()
        if message:
            self._console.write(message + "\n")


class HeadlessInteraction:
    """headless 会话级交互替身（`contracts.output.InteractionPort` 的无交互实现）。

    不监听打断（不切换中断模式）、不读取输入（恒返回空）、命令分发恒返回未识别；
    每次 `begin_turn` 产出一个新的纯文本流句柄。主循环入口与交互模式同形，
    便于装配侧无差别注入。
    """

    def __init__(
        self,
        theme: Theme,
        console: Console,
        *,
        sink_factory: Optional[Callable[[], HeadlessSink]] = None,
    ):
        self._make_sink = sink_factory or (lambda: HeadlessSink(theme, console))

    def start(self, model_name: str = "") -> None:
        """启动界面：headless 不输出横幅，无效果。"""

    def begin_turn(self) -> HeadlessSink:
        """开始一个 AI 回合：创建纯文本流句柄（不进入运行模式、无动画）。"""
        return self._make_sink()

    def read_input(self) -> None:
        """读取用户输入：headless 不读取输入，恒返回空。"""
        return None

    def read_confirmation(self, prompt: str = "") -> bool:
        """读取确认：headless 无交互环境，恒为 False（取消）。"""
        return False

    def notify_interrupted(self) -> None:
        """中断收敛回调：headless 不可打断，无效果。"""

    def dispatch_command(self, command: str, args: str) -> CommandResult:
        """命令分发：headless 无命令路径，恒返回未识别。"""
        return CommandResult.UNKNOWN


class HeadlessAnimator:
    """压缩 / 合并动画的空实现（`contracts.output.Animator`）：均为无效果。"""

    def begin_compressing(self) -> None:
        """开始压缩动画：无效果。"""

    def end_compressing(self) -> None:
        """结束压缩动画：无效果。"""

    def begin_summarizing(self) -> None:
        """开始合并动画：无效果。"""

    def end_summarizing(self) -> None:
        """结束合并动画：无效果。"""
