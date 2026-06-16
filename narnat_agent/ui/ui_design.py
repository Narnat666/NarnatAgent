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

import sys
import threading
import time
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# ── 从子模块 re-export，保持外部导入兼容 ──
from .colors import (
    _Color, RST, BLD, DIM, GRY, CYN, GRN, YLW, RED, BLU, MAG, ORG, BG8, WHT, WHT7,
    R, B, D, G, C, E, Y, X, U, M, O, BG, W, W7,
    _STYLE_KEY_MAP, SHOW_COST, SHOW_BALANCE, MAX_TOKENS,
    _stdout_lock, _stdout_write, _stdout_try_write,
    apply_style,
)
from .interrupt import InterruptController, _interrupt_ctrl
from .renderer import (
    colorize_diff, _sep, _terminal_width, _display_width,
    InlineRules, BlockRule, CodeBlockRenderer, StreamingRenderer, render_line,
)
from .session_commands import (
    SessionCallbacks, _CommandCompleter, _dispatch_command,
)


# ═══════════════════════════════════════════════════════════════
# 界面辅助函数
# ═══════════════════════════════════════════════════════════════

def show_header(msg: str) -> None:
    _stdout_write(f"  {C}{msg}{R}\n")
    _sep()


def _spinner_thread(stop: threading.Event) -> None:
    """显示思考中动画。先等待666ms，避免串行工具间的短暂空白闪烁。
    666ms后屏幕仍空白才启动动画，4帧循环：* 思考中 → * 思考中. → * 思考中.. → * 思考中...
    左侧 * 粗细交替，右侧 ... 从0到3循环，"思考中"三字位置锁定。
    退出前自清屏幕残留，与 _stop_spinner 形成双道防护。"""
    # 延迟666ms，期间每50ms检查一次stop
    delay = 0.666
    elapsed = 0.0
    tick = 0.05
    while elapsed < delay:
        if stop.is_set():
            return
        time.sleep(tick)
        elapsed += tick

    # 隐藏光标（阻塞锁，必须执行）
    _stdout_write("\x1b[?25l")

    frames = (
        f"{B}{O}* {R}{O}思考中   {R}",
        f"{D}{O}* {R}{O}思考中.  {R}",
        f"{B}{O}* {R}{O}思考中.. {R}",
        f"{D}{O}* {R}{O}思考中...{R}",
    )
    i = 0
    while not stop.is_set():
        _stdout_try_write(f"\r  {frames[i]}\x1b[K")
        i = (i + 1) % 4
        stop.wait(0.15)

    # 退出前自清：擦除动画行 + 恢复光标（阻塞锁，必须执行）
    _stdout_write("\r\x1b[K")
    _stdout_write("\x1b[?25h")


def _compress_thread(stop: threading.Event) -> None:
    frames = (f"{B}{O}*{R}", f"{D}{O}✶{R}")
    i = 0
    while not stop.is_set():
        _stdout_try_write(f"\r  {frames[i]} {O}正在压缩...{R}\x1b[K")
        i = (i + 1) % 2
        stop.wait(0.15)
    _stdout_write("\r\x1b[K")


def show_interrupted() -> None:
    _stdout_write(f"\n  {Y}已打断{R}\n  {G}继续...{R}\n")


def show_stats(input_tokens: int, output_tokens: int,
               cache: int = 0, cost: float = 0.0,
               balance: float = 0.0) -> None:
    si = f"{input_tokens / 1000:.1f}k" if input_tokens >= 1000 else str(input_tokens)
    so = f"{output_tokens / 1000:.1f}k" if output_tokens >= 1000 else str(output_tokens)
    mt = f"{MAX_TOKENS / 1000:.0f}k" if MAX_TOKENS >= 1000 else str(MAX_TOKENS)
    _sep()
    cs = f"  缓存:{cache / 1000:.1f}k" if cache > 0 else ""
    co = f"  费用:¥{cost:.4f}" if SHOW_COST and cost > 0 else ""
    ba = f"  余额:¥{balance:.2f}" if SHOW_BALANCE and balance > 0 else ""
    _stdout_write(f"  {G}输入:{si} 输出:{so}{cs}  最大输出:{mt}{R}{Y}{co}{ba}{R}\n")


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
        if self._spinner_thread is None:
            return
        self._spinner_stop.set()
        self._spinner_thread.join(timeout=0.5)
        self._spinner_thread = None
        # 清理：擦除动画行 + 恢复光标
        _stdout_write("\r\x1b[K")
        _stdout_write("\x1b[?25h")

    @property
    def cancelled(self) -> bool:
        return _interrupt_ctrl.is_set

    def begin(self) -> None:
        self._start_spinner()

    def feed(self, chunk: str) -> None:
        if not self._started:
            self._started = True
        self._stop_spinner()
        self._renderer.feed(chunk)

    def pause_spinner(self) -> None:
        """暂停spinner（工具执行前调用），避免与工具输出竞争同一行"""
        self._spinner_pause_count += 1
        if self._spinner_pause_count == 1:
            self._stop_spinner()

    def flush_renderer(self) -> None:
        """flush渲染器缓冲区，确保之前的文字已完整输出到终端"""
        if self._started:
            self._renderer.flush()

    def resume_spinner(self) -> None:
        """恢复spinner（工具执行后调用），所有并行工具完成后才真正恢复"""
        if self._aborted:
            return
        self._spinner_pause_count = max(0, self._spinner_pause_count - 1)
        if self._spinner_pause_count == 0:
            self._start_spinner()

    def finish(self, input_tokens: int = 0, output_tokens: int = 0,
               cache: int = 0, cost: float = 0.0,
               balance: float = 0.0) -> None:
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询，防止误触发
        self._stop_spinner()
        self._renderer.flush()
        show_stats(input_tokens, output_tokens, cache, cost, balance)

    def abort(self) -> None:
        self._aborted = True  # 标记已打断，防止后台线程resume_spinner重启
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询
        self._stop_spinner()
        show_interrupted()


# ═══════════════════════════════════════════════════════════════
# UIInterface ── 后端对接接口
# ═══════════════════════════════════════════════════════════════

class UIInterface:
    """UI 总接口，后端只和此类交互"""

    def __init__(self, model_name: str = "narnat",
                 callbacks: Optional[SessionCallbacks] = None) -> None:
        self._model = model_name
        self._callbacks = callbacks or SessionCallbacks()
        self._session: Optional[PromptSession] = None
        self._compress_stop: Optional[threading.Event] = None
        self._compress_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        _interrupt_ctrl.enter_input_mode()
        show_header(self._model)
        self._session = _create_session(self._callbacks)

    def read_input(self) -> Optional[str]:
        if self._session is None:
            self.start()
        assert self._session is not None
        _interrupt_ctrl.clear()
        _interrupt_ctrl.enter_input_mode()
        line = read_input(self._session)
        if line is None:
            self._session = _create_session(self._callbacks)
        return line

    def dispatch_command(self, cmd: str, args: str) -> bool:
        return _dispatch_command(cmd, args, self._callbacks)

    def create_stream(self) -> UIStreamSession:
        _interrupt_ctrl.enter_run_mode()  # 内部已clear
        session = UIStreamSession()
        session.begin()
        return session

    def on_interrupted(self) -> None:
        _interrupt_ctrl.enter_input_mode()
        self._session = _create_session(self._callbacks)

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
            self._compress_thread.join(timeout=0.5)
        self._compress_stop = None
        self._compress_thread = None

    def auto_save(self) -> str:
        """退出时自动保存，返回保存的会话名或空串"""
        return self._callbacks.on_exit()


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


_PROMPT_STYLE = Style.from_dict({
    "prompt": "bold #00ff00",       # # 提示符保持绿色
    "": "#ffffff",                  # 用户输入文本：极致白色
})


def _create_session(callbacks: SessionCallbacks) -> PromptSession:
    return PromptSession(
        style=_PROMPT_STYLE,
        multiline=True,
        completer=_CommandCompleter(callbacks),
        key_bindings=_make_keybindings())


def read_input(session: PromptSession) -> Optional[str]:
    try:
        return session.prompt([("class:prompt", "# ")])
    except (KeyboardInterrupt, EOFError):
        return None
