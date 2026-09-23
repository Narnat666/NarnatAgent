"""动画（思考中 / 正在压缩 / 正在合并）—— 原地刷新帧循环与成对操作端口。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「动画（思考中 / 正在压缩 / 正在合并）」：三类动画（思考中延迟 0.666 秒启动，
  避免串行工具间短暂空白闪烁；压缩 / 合并立即启动）；帧序列为 `* 标签` 及其后
  1、2、3 个点的 4 帧循环，粗体与暗色交替；帧行以回车覆盖、两空格前缀、行尾清行；
  帧间隔 0.15 秒；写失败（输出锁被占用）时丢帧且不等待；动画启动时隐藏光标，
  结束时清行并恢复光标；延迟期间收到停止信号不启动；压缩与合并为成对的开始 /
  结束操作（结束对未启动情形幂等，等待线程退出的上限 1 秒）；
- 「流式输出会话句柄协议」：思考中动画由 `OutputSink` 内部控制（创建时启动、
  feed / finish / abort 时停止），无独立入口；
- contracts.output.Animator 端口（交互模式实现即本模块的 `UiAnimator`，
  headless 的空实现见 `headless.HeadlessAnimator`）。

结构（design D7）：无模块级可变状态——停止信号与动画线程全部在 `Animation` 实例内；
颜色经构造注入的 `Theme` 实时取用，写出经注入的 `Console`（纯文本模式不重申终端
能力、不补行首回车）。
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

from ..output import Console, Theme

__all__ = [
    "ANIMATION_FRAME_INTERVAL",
    "Animation",
    "COMPRESSING_LABEL",
    "SPINNER_DELAY",
    "SPINNER_LABEL",
    "SUMMARIZING_LABEL",
    "THREAD_JOIN_LIMIT",
    "UiAnimator",
    "frame_texts",
    "join_thread",
]

# 帧间隔（秒）：每帧停留时长
ANIMATION_FRAME_INTERVAL = 0.15

# 思考中动画的延迟启动时长（秒）：避开串行工具之间的短暂空白，防闪烁
SPINNER_DELAY = 0.666

# 延迟等待期的轮询粒度（秒）
DELAY_TICK = 0.05

# 等待动画线程退出的上限（秒）
THREAD_JOIN_LIMIT = 1.0

# 三类动画标签（已发布文案）
SPINNER_LABEL = "思考中"
COMPRESSING_LABEL = "正在压缩"
SUMMARIZING_LABEL = "正在合并"


def frame_texts(theme: Theme, label: str) -> Tuple[str, str, str, str]:
    """4 帧文本：`* 标签` 后接 0/1/2/3 个点，粗体（1、3 帧）与暗色（2、4 帧）交替。

    各行以补位空格对齐（与现状逐字一致），配合回车覆盖实现原地刷新。
    """
    return (
        f"{theme.b}{theme.ui_spinner}* {theme.rst}{theme.ui_spinner}{label}   {theme.rst}",
        f"{theme.d}{theme.ui_spinner}* {theme.rst}{theme.ui_spinner}{label}.  {theme.rst}",
        f"{theme.b}{theme.ui_spinner}* {theme.rst}{theme.ui_spinner}{label}.. {theme.rst}",
        f"{theme.d}{theme.ui_spinner}* {theme.rst}{theme.ui_spinner}{label}...{theme.rst}",
    )


def join_thread(thread: threading.Thread, max_wait: float = THREAD_JOIN_LIMIT) -> None:
    """等待线程退出，最长 `max_wait` 秒。

    比裸 `join(timeout=N)` 更可靠地处理短超时残留：以 0.1 秒粒度轮询到期限为止。
    """
    deadline = time.time() + max_wait
    while thread.is_alive() and time.time() < deadline:
        thread.join(0.1)


class Animation:
    """单个原地刷新的帧动画（三类动画共用同一实现）。

    生命周期：`start()`（延迟到期后隐藏光标并循环写帧）→ `stop()`（置停止信号、
    等待线程退出并清理）；重复 start / stop 幂等。
    """

    def __init__(self, console: Console, theme: Theme, label: str, delay: float = 0.0):
        """
        Args:
            console: 输出原语（帧写经 `try_write`，启动/收尾序列经 `write`）；
            theme: 颜色体系（帧文本取粗体、暗色与动画色）；
            label: 动画标签（"思考中" / "正在压缩" / "正在合并"）；
            delay: 启动延迟秒数（0 立即启动；延迟期间收到停止信号则不启动）。
        """
        self._console = console
        self._theme = theme
        self._label = label
        self._delay = max(0.0, float(delay))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frames: Optional[Tuple[str, ...]] = None

    @property
    def running(self) -> bool:
        """动画线程是否仍在运行（延迟等待期亦视为运行）。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动动画（已在运行时幂等返回）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止动画并等待线程退出（未启动 / 已停止时幂等无输出）。"""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        join_thread(thread)
        self._thread = None

    def write_frame(self, index: int) -> bool:
        """写一帧：回车覆盖 + 两空格前缀 + 行尾清行。

        非阻塞写出（`Console.try_write`）：输出锁被占用时丢帧并返回 False，
        不等待、不阻塞调用方；帧序号由调用方推进，与是否写出成功无关。
        """
        return self._console.try_write(f"\r  {self._frame(index)}\x1b[K")

    def _frame(self, index: int) -> str:
        """按序号取帧文本（取色在首次使用时发生，`apply_style` 后立即生效）。"""
        if self._frames is None:
            self._frames = frame_texts(self._theme, self._label)
        return self._frames[index % len(self._frames)]

    def _run(self) -> None:
        """线程体：延迟等待 → 隐藏光标 → 帧循环 → 清行并恢复光标。"""
        if self._delay > 0:
            elapsed = 0.0
            while elapsed < self._delay:
                if self._stop.is_set():
                    return  # 延迟期间收到停止信号：不启动，不留视觉残留
                time.sleep(DELAY_TICK)
                elapsed += DELAY_TICK
        self._console.write("\x1b[?25l")
        index = 0
        while not self._stop.is_set():
            self.write_frame(index)
            index += 1
            self._stop.wait(ANIMATION_FRAME_INTERVAL)
        self._console.write("\r\x1b[K")
        self._console.write("\x1b[?25h")


class UiAnimator:
    """压缩 / 合并动画的成对操作（`contracts.output.Animator` 的交互模式实现）。

    - `begin_compressing` / `begin_summarizing`：立即启动对应动画（重复 begin 前
      先停旧动画，保证同一时刻至多一个该标签的动画）；
    - `end_compressing` / `end_summarizing`：对未启动情形幂等（无异常、无输出），
      等待动画线程退出的上限 1 秒。
    """

    def __init__(self, console: Console, theme: Theme):
        self._console = console
        self._theme = theme
        self._compressing: Optional[Animation] = None
        self._summarizing: Optional[Animation] = None

    def begin_compressing(self) -> None:
        """启动「正在压缩」动画（立即启动）。"""
        self._compressing = self._start(self._compressing, COMPRESSING_LABEL)

    def end_compressing(self) -> None:
        """停止「正在压缩」动画（未启动时幂等）。"""
        self._compressing = self._stop(self._compressing)

    def begin_summarizing(self) -> None:
        """启动「正在合并」动画（立即启动）。"""
        self._summarizing = self._start(self._summarizing, SUMMARIZING_LABEL)

    def end_summarizing(self) -> None:
        """停止「正在合并」动画（未启动时幂等）。"""
        self._summarizing = self._stop(self._summarizing)

    def _start(self, current: Optional[Animation], label: str) -> Animation:
        """启动一个标签的动画（替换同标签旧动画）。"""
        if current is not None:
            current.stop()
        animation = Animation(self._console, self._theme, label)
        animation.start()
        return animation

    @staticmethod
    def _stop(current: Optional[Animation]) -> None:
        """停止动画并清引用（None 时保持 None，即幂等）。"""
        if current is None:
            return None
        current.stop()
        return None
