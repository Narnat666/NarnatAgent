"""
narnat UI - 简洁输入交互界面

输入 -> Markdown流式渲染 -> token/费用显示

架构:
  colors.py           ── ANSI颜色常量 + apply_style配色管理
  interrupt.py        ── ESC/SIGINT 中断控制器
  renderer.py         ── 流式Markdown→ANSI渲染器
  session_commands.py ── 会话管理命令 + Tab补全
  ui_design.py        ── UIInterface 总接口 + UIStreamSession
"""

import os
import sys
import threading
import time
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import ANSI

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# ── 从子模块 re-export，保持外部导入兼容 ──
from .colors import (
    _Color, RST, BLD, DIM, R, B, D,
    C_PRIMARY, C_SECONDARY, C_USER, C_ACCENT, C_SUCCESS, C_WARNING, C_ERROR, C_LINK, C_EMPHASIS,
    G, C, E, Y, X, U, M, O, W7,
    UI_HEADER, UI_SPINNER,
    UI_INTERRUPTED, UI_INTERRUPTED_HINT,
    UI_STATS_LABEL, UI_STATS_VALUE,
    PTK_PROMPT_SYMBOL, PTK_PROMPT_TEXT, PTK_PROMPT_CUSTOM,
    apply_style,
)
from ..output import _stdout_lock, write as _stdout_write, try_write as _stdout_try_write
from .interrupt import InterruptController, _interrupt_ctrl
from .renderer import (
    colorize_diff, _sep, _terminal_width, _display_width,
    InlineRules, BlockRule, CodeBlockRenderer, StreamingRenderer, render_line,
)
from .session_commands import _CommandCompleter, _dispatch_command, CommandResult


# ═══════════════════════════════════════════════════════════════
# 界面辅助函数
# ═══════════════════════════════════════════════════════════════

_ANIMATION_FRAME_INTERVAL = 0.15  # 动画帧间隔（秒）

def show_header(msg: str) -> None:
    _stdout_write(f"  {UI_HEADER}{msg}{R}\n")
    _sep()


def _join_thread(t: threading.Thread, max_wait: float = 1.0) -> None:
    """等待线程退出，最长等待 max_wait 秒。比裸 join(timeout=N) 更可靠地处理短超时残留。"""
    deadline = time.time() + max_wait
    while t.is_alive() and time.time() < deadline:
        t.join(0.1)


def _animation_thread(stop: threading.Event, label: str,
                      delay: float = 0.0) -> None:
    """通用动画线程：4帧循环 * label → * label. → * label.. → * label...

    Args:
        stop: 停止信号
        label: 动画标签文本（如 "思考中"、"正在压缩"）
        delay: 可选延迟启动秒数，期间每50ms检查stop。0表示立即启动。
    """
    if delay > 0:
        elapsed = 0.0
        tick = 0.05
        while elapsed < delay:
            if stop.is_set():
                return
            time.sleep(tick)
            elapsed += tick

    _stdout_write("\x1b[?25l")

    frames = (
        f"{B}{UI_SPINNER}* {R}{UI_SPINNER}{label}   {R}",
        f"{D}{UI_SPINNER}* {R}{UI_SPINNER}{label}.  {R}",
        f"{B}{UI_SPINNER}* {R}{UI_SPINNER}{label}.. {R}",
        f"{D}{UI_SPINNER}* {R}{UI_SPINNER}{label}...{R}",
    )
    i = 0
    while not stop.is_set():
        _stdout_try_write(f"\r  {frames[i]}\x1b[K")
        i = (i + 1) % 4
        stop.wait(_ANIMATION_FRAME_INTERVAL)

    _stdout_write("\r\x1b[K")
    _stdout_write("\x1b[?25h")


def _spinner_thread(stop: threading.Event) -> None:
    """思考中动画。延迟666ms启动，避免串行工具间短暂空白闪烁。"""
    _animation_thread(stop, "思考中", 0.666)


def _compress_thread(stop: threading.Event) -> None:
    """压缩动画。"""
    _animation_thread(stop, "正在压缩")


def _summary_thread(stop: threading.Event) -> None:
    """总结动画。"""
    _animation_thread(stop, "正在合并")


def show_interrupted() -> None:
    _stdout_write(f"\n  {UI_INTERRUPTED}已打断{R}\n  {UI_INTERRUPTED_HINT}继续...{R}\n")


def show_stats(input_tokens: int, output_tokens: int,
               cache_ratio: float = 0.0, cost: float = 0.0,
               balance: float = 0.0,
               thinking_effort: str = "高") -> None:
    # 动态从 output 模块读取，确保 apply_style 修改后生效
    from .. import output as _output
    _show_cost = _output.SHOW_COST
    _show_balance = _output.SHOW_BALANCE
    _max_tokens = _output.MAX_TOKENS
    _show_ratio = _output.SHOW_RATIO
    _context_window = _output.CONTEXT_WINDOW

    si = f"{input_tokens / 1000:.1f}k" if input_tokens >= 1000 else str(input_tokens)
    so = f"{output_tokens / 1000:.1f}k" if output_tokens >= 1000 else str(output_tokens)
    mt = f"{_max_tokens / 1000:.0f}k" if _max_tokens >= 1000 else str(_max_tokens)
    th = f"  思考:{thinking_effort}"
    _sep()
    cs = ""
    if cache_ratio > 0:
        cs = f"  缓存:{min(100.0, cache_ratio * 100):.1f}%"
    co = f"  费用:¥{cost:.4f}" if _show_cost else ""
    ba = f"  余额:¥{balance:.2f}" if _show_balance and balance > 0 else ""
    rt = ""
    if _show_ratio:
        if _context_window > 0 and input_tokens > 0:
            rt = f"  窗口占比:{input_tokens / _context_window * 100:.0f}%"
        else:
            rt = "  窗口占比:--"
    _stdout_write(f"  {UI_STATS_LABEL}输入:{si} 输出:{so}{cs}{th}{rt}  最大输出:{mt}{R}{UI_STATS_VALUE}{co}{ba}{R}\n")


# ═══════════════════════════════════════════════════════════════
# UIStreamSession ── 流式输出会话句柄
# ═══════════════════════════════════════════════════════════════

class UIStreamSession:
    """
    流式输出会话句柄。后端拿到这个对象后：
      while not session.cancelled:
          session.feed(token)
      session.finish(input_tokens, output_tokens)
    或异常时:
      session.abort()

    工具执行期间调用 pause_spinner/resume_spinner 避免闪烁。
    """

    def __init__(self) -> None:
        self._renderer = StreamingRenderer()
        self._spinner_stop = threading.Event()
        self._spinner_thread: Optional[threading.Thread] = None
        self._started = False  # 仅用于flush_renderer守卫，不干预spinner
        self._aborted = False  # abort后resume_spinner不应再启动新spinner
        self._spinner_pause_count = 0  # 并行工具pause计数，归零才恢复spinner
        self._spinner_lock = threading.Lock()  # 并行工具线程并发pause/resume的计数保护

    def _start_spinner(self) -> None:
        """启动spinner线程（带666ms延迟），如果已有则不重复启动"""
        if self._spinner_thread is not None:
            return
        self._spinner_stop.clear()
        self._spinner_thread = threading.Thread(
            target=_spinner_thread,
            args=(self._spinner_stop,), daemon=True)
        self._spinner_thread.start()

    def _stop_spinner(self) -> None:
        """停止spinner线程并等待其退出，主动清理避免竞态残留"""
        t = self._spinner_thread
        if t is None:
            return
        self._spinner_stop.set()
        if t.is_alive():
            _join_thread(t)
        self._spinner_thread = None
        # 清理：擦除动画行 + 恢复光标
        _stdout_write("\r\x1b[K")
        _stdout_write("\x1b[?25h")

    @property
    def cancelled(self) -> bool:
        return _interrupt_ctrl.is_set

    @property
    def aborted(self) -> bool:
        return self._aborted

    def begin(self) -> None:
        self._start_spinner()

    def feed(self, chunk: str) -> None:
        if not self._started:
            self._started = True
        self._stop_spinner()
        self._renderer.feed(chunk)

    def pause_spinner(self) -> None:
        """暂停spinner（工具执行前调用），避免与工具输出竞争同一行"""
        with self._spinner_lock:
            self._spinner_pause_count += 1
            first = self._spinner_pause_count == 1
        if first:
            self._stop_spinner()

    def flush_renderer(self) -> None:
        """flush渲染器缓冲区，确保之前的文字已完整输出到终端。

        一律立即落定：普通文字半行渲染、表格完整渲染（不完整降级为段落）、
        代码块内容渲染。不跨轮保留缓冲，避免上一轮表格行与下一轮混合错乱。
        """
        if self._started:
            self._renderer.flush(final=False)

    def reset_renderer(self) -> None:
        """清空渲染器缓冲与状态（响应流中断自动重试前调用），
        防止上一轮残留的半行文字/表格行/代码块与重试流拼接错乱。"""
        self._renderer.reset()

    def resume_spinner(self) -> None:
        """恢复spinner（工具执行后调用），所有并行工具完成后才真正恢复"""
        if self._aborted:
            return
        with self._spinner_lock:
            self._spinner_pause_count = max(0, self._spinner_pause_count - 1)
            restart = self._spinner_pause_count == 0
        if restart:
            self._start_spinner()

    def finish(self, input_tokens: int = 0, output_tokens: int = 0,
               cache_ratio: float = 0.0, cost: float = 0.0,
               balance: float = 0.0, thinking_effort: str = "高",
               with_stats: bool = True) -> None:
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询，防止误触发
        self._stop_spinner()
        self._renderer.flush(final=True)
        if with_stats:
            show_stats(input_tokens, output_tokens, cache_ratio, cost, balance, thinking_effort)

    def abort(self, message: Optional[str] = None) -> None:
        """中止输出。message 非空时显示自定义提示（如程序异常），
        否则显示默认的"已打断"提示。"""
        self._aborted = True  # 标记已打断，防止后台线程resume_spinner重启
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询
        self._stop_spinner()
        if message is None:
            show_interrupted()
        else:
            _stdout_write(message + "\n")


# ═══════════════════════════════════════════════════════════════
# UIInterface ── 后端对接接口
# ═══════════════════════════════════════════════════════════════

class UIInterface:
    """UI 总接口，后端只和此类交互"""

    def __init__(self, model_name: str = "narnat",
                 session_manager=None, data_dir: str = "") -> None:
        self._model = model_name
        self._mgr = session_manager
        self._data_dir = data_dir
        self._session: Optional[PromptSession] = None
        self._compress_stop: Optional[threading.Event] = None
        self._compress_thread: Optional[threading.Thread] = None
        self._summary_stop: Optional[threading.Event] = None
        self._summary_thread_var: Optional[threading.Thread] = None

    def start(self) -> None:
        _interrupt_ctrl.enter_input_mode()
        show_header(self._model)
        self._session = _create_session(self._mgr, self._data_dir)

    def read_input(self) -> Optional[str]:
        if self._session is None:
            self.start()
        assert self._session is not None
        _interrupt_ctrl.clear()
        _interrupt_ctrl.enter_input_mode()
        line = read_input(self._session)
        if line is None:
            self._session = _create_session(self._mgr, self._data_dir)
        return line

    def read_input_with_prompt(self, prompt_text: str) -> Optional[str]:
        """带自定义提示符的输入，用于删除确认等场景。"""
        if self._session is None:
            self.start()
        assert self._session is not None
        _interrupt_ctrl.clear()
        _interrupt_ctrl.enter_input_mode()
        line = read_input_with_prompt(self._session, prompt_text)
        if line is None:
            self._session = _create_session(self._mgr, self._data_dir)
        return line

    def dispatch_command(self, cmd: str, args: str) -> CommandResult:
        return _dispatch_command(cmd, args, self._mgr)

    def create_stream(self) -> UIStreamSession:
        _interrupt_ctrl.enter_run_mode()  # 内部已clear
        session = UIStreamSession()
        session.begin()
        return session

    def on_interrupted(self) -> None:
        _interrupt_ctrl.enter_input_mode()
        self._session = _create_session(self._mgr, self._data_dir)

    def begin_compressing(self) -> None:
        self._compress_stop = threading.Event()
        self._compress_thread = threading.Thread(
            target=_compress_thread,
            args=(self._compress_stop,), daemon=True)
        self._compress_thread.start()

    def end_compressing(self) -> None:
        if self._compress_stop is None:
            return
        self._compress_stop.set()
        if self._compress_thread is not None:
            _join_thread(self._compress_thread)
        self._compress_stop = None
        self._compress_thread = None

    def begin_summarizing(self) -> None:
        self._summary_stop = threading.Event()
        self._summary_thread_var = threading.Thread(
            target=_summary_thread,
            args=(self._summary_stop,), daemon=True)
        self._summary_thread_var.start()

    def end_summarizing(self) -> None:
        if self._summary_stop is not None:
            self._summary_stop.set()
        if self._summary_thread_var is not None:
            _join_thread(self._summary_thread_var)
        self._summary_stop = None
        self._summary_thread_var = None


# ═══════════════════════════════════════════════════════════════
# prompt_toolkit 配置
# ═══════════════════════════════════════════════════════════════

def _make_keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    # Alt+Enter 换行
    @kb.add("escape", "enter", eager=True)
    def _newline_alt_enter(event):
        event.current_buffer.insert_text("\n")

    # Ctrl+O 换行
    @kb.add("c-o", eager=True)
    def _newline_ctrl_o(event):
        event.current_buffer.insert_text("\n")

    # Alt+J 换行（vim 风格）
    @kb.add("escape", "j", eager=True)
    def _newline_alt_j(event):
        event.current_buffer.insert_text("\n")
    return kb


def _get_prompt_style() -> Style:
    from ..output import PTK_PROMPT_SYMBOL, PTK_PROMPT_TEXT
    return Style.from_dict({
        "prompt": PTK_PROMPT_SYMBOL,
        "": PTK_PROMPT_TEXT,
    })


def _create_session(session_manager, data_dir: str = "") -> PromptSession:
    if data_dir:
        history_path = os.path.join(data_dir, ".narnat_history")
    else:
        history_path = os.path.join(os.path.expanduser("~"), ".narnat_history")
    return PromptSession(
        style=_get_prompt_style(),
        multiline=True,
        completer=_CommandCompleter(session_manager),
        key_bindings=_make_keybindings(),
        history=FileHistory(history_path))


def read_input(session: PromptSession) -> Optional[str]:
    try:
        return session.prompt([("class:prompt", "# ")])
    except (KeyboardInterrupt, EOFError):
        return None


def read_input_with_prompt(session: PromptSession, prompt_text: str) -> Optional[str]:
    """带自定义提示符的输入，用于删除确认等场景。提示文本使用AI输出颜色。"""
    from ..output import R
    try:
        return session.prompt(ANSI(f"{C_PRIMARY}{prompt_text}{R}"))
    except (KeyboardInterrupt, EOFError):
        return None
