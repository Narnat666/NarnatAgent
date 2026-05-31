"""
narnat UI - 简洁输入交互界面
输入 -> Markdown流式渲染 -> token/费用显示
运行: python ui_design.py

架构:
  InterruptController  ── 封装 ESC/SIGINT 中断管理（线程安全）
  InlineRules          ── 行内元素正则替换流水线（纯函数，无状态）
  BlockRule            ── 块级元素策略表（优先级排序，开闭原则）
  CodeBlockRenderer    ── 代码块渲染（语言着色+行号）
  StreamingRenderer    ── 流式状态机（字符→行→ANSI，list+join消除O(n²)）
"""

import os
import sys
import re
import shutil
import signal
import threading
import time
from typing import Optional, Callable, List, Match

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# ═══════════════════════════════════════════════════════════════
# ANSI 转义序列常量
# ═══════════════════════════════════════════════════════════════

RST = "\x1b[0m"
BLD = "\x1b[1m"
DIM = "\x1b[2m"
GRY = "\x1b[90m"
CYN = "\x1b[36m"
GRN = "\x1b[32m"
YLW = "\x1b[33m"
RED = "\x1b[31m"
BLU = "\x1b[34m"
MAG = "\x1b[35m"
ORG = "\x1b[38;5;214m"
BG8 = "\x1b[48;5;236m"

R, B, D, G, C = RST, BLD, DIM, GRY, CYN
E, Y, X, U, M, O, BG = GRN, YLW, RED, BLU, MAG, ORG, BG8


def _terminal_width() -> int:
    try:
        return min(shutil.get_terminal_size().columns, 160)
    except Exception:
        return 120


def _sep() -> None:
    sys.stdout.write(f"  {G}{'─' * (_terminal_width() - 2)}{R}\n")


# ═══════════════════════════════════════════════════════════════
# InterruptController ── 封装的中断管理器
# ═══════════════════════════════════════════════════════════════

class InterruptController:
    """
    线程安全的 ESC/SIGINT 中断管理。
    状态机: INPUT_MODE ↔ RUN_MODE
    """

    def __init__(self) -> None:
        self._interrupt = threading.Event()
        self._stop_poll = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._saved_sigint = None

    @property
    def is_set(self) -> bool:
        return self._interrupt.is_set()

    def clear(self) -> None:
        self._interrupt.clear()

    def enter_input_mode(self) -> None:
        self._stop_poll.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        self._interrupt.clear()
        if self._saved_sigint is not None:
            signal.signal(signal.SIGINT, self._saved_sigint)
            self._saved_sigint = None
        else:
            signal.signal(signal.SIGINT, signal.default_int_handler)

    def enter_run_mode(self) -> None:
        self._interrupt.clear()
        self._stop_poll.clear()
        self._poll_thread = threading.Thread(target=self._poll_esc, daemon=True)
        self._poll_thread.start()
        # 恢复SIGINT为默认行为（编译为exe后Ctrl+C直接退出，不拦截）
        if self._saved_sigint is not None:
            signal.signal(signal.SIGINT, self._saved_sigint)
            self._saved_sigint = None
        else:
            signal.signal(signal.SIGINT, signal.default_int_handler)

    def _poll_esc(self) -> None:
        while not self._stop_poll.is_set():
            try:
                import msvcrt
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b'\x1b':
                        self._interrupt.set()
                        break
            except (ImportError, OSError):
                pass
            self._stop_poll.wait(0.05)


_interrupt_ctrl = InterruptController()


# ═══════════════════════════════════════════════════════════════
# 语言→颜色映射表  (数据驱动，O(1) 查找)
# ═══════════════════════════════════════════════════════════════

_LANG_COLOR_MAP: dict = {}
_LANG_GROUPS = [
    ("cyan",    ["python", "py", "pyi", "go", "dart", "r", "rstats", "diff", "patch"]),
    ("yellow",  ["javascript", "js", "mjs", "typescript", "ts", "tsx", "jsx",
                 "java", "kt", "scala", "kotlin"]),
    ("green",   ["bash", "sh", "zsh", "shell", "fish", "cpp", "c", "h", "hpp",
                 "cc", "dockerfile", "docker", "makefile", "cmake"]),
    ("magenta", ["json", "jsonc", "toml", "yaml", "yml", "sql", "mysql",
                 "pgsql", "php", "lua", "perl", "pl", "ini", "cfg", "conf"]),
    ("red",     ["html", "htm", "xml", "svg", "rust", "rs", "ruby", "rb", "swift"]),
    ("blue",    ["css", "scss", "sass", "less"]),
    ("gray",    ["markdown", "md", "text", "txt", "log"]),
]
_COLOR_MAP = {"cyan": C, "yellow": Y, "green": E, "blue": U,
              "magenta": M, "red": X, "gray": G}
for _grp_color, _grp_langs in _LANG_GROUPS:
    for _lang in _grp_langs:
        _LANG_COLOR_MAP[_lang] = _grp_color


# ═══════════════════════════════════════════════════════════════
# 行内 Markdown 解析器 ── 正则替换流水线
# ═══════════════════════════════════════════════════════════════

class InlineRules:
    """
    纯函数流水线：输入原始文本 → 依次应用替换规则 → 输出 ANSI 文本
    规则顺序体现 Markdown 优先级：删除线 > 粗体 > 斜体 > 行内代码 > 图片 > 链接
    """

    _RE_STRIKE = re.compile(r"~~(.+?)~~")
    _RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
    _RE_ITAL = re.compile(r"\*(.+?)\*")
    _RE_CODE = re.compile(r"`([^`]+)`")
    _RE_IMG = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
    _RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")

    @classmethod
    def render(cls, text: str) -> str:
        text = cls._RE_STRIKE.sub(f"{X}\\1{R}", text)
        text = cls._RE_BOLD.sub(f"{B}\\1{R}", text)
        text = cls._RE_ITAL.sub(f"{D}\\1{R}", text)
        text = cls._RE_CODE.sub(f"{Y}\\1{R}", text)
        text = cls._RE_IMG.sub(f"{D}[img:\\1]{R}", text)
        text = cls._RE_LINK.sub(f"{U}\\1{R}", text)
        return text


# ═══════════════════════════════════════════════════════════════
# 块级 Markdown 策略表 ── 优先级排序的规则引擎
# ═══════════════════════════════════════════════════════════════

class BlockRule:
    """
    块级渲染规则：封装 (优先级, 名称, 匹配函数, 渲染函数)
    优先级越低越先匹配
    """
    __slots__ = ('priority', 'name', 'match', 'render')

    def __init__(self, priority: int, name: str,
                 match: Callable[[str], Optional[Match]],
                 render: Callable[[str, Match], str]) -> None:
        self.priority = priority
        self.name = name
        self.match = match
        self.render = render


def _render_heading(_line: str, m: Match) -> str:
    level = len(m.group(1))
    body = InlineRules.render(m.group(2))
    if level <= 2:
        return f"  {B}{CYN}{body}{R}"
    if level == 3:
        return f"  {B}{GRN}{body}{R}"
    return f"  {B}{body}{R}"


def _render_hr(_line: str, _m: Match) -> str:
    return f"  {G}{_line.strip()}{R}"


def _render_task(_line: str, m: Match) -> str:
    done = m.group(1).lower() == "x"
    marker = f"{E}v{R}" if done else f"{G}o{R}"
    return f"   {marker} {InlineRules.render(m.group(2))}"


def _render_ul(_line: str, _m: Match) -> str:
    return f"   {E}*{R} {InlineRules.render(_line[2:])}"


def _render_ol(_line: str, m: Match) -> str:
    num = m.group(1)
    body_start = len(num) + 2
    return f"   {G}{num}.{R} {InlineRules.render(_line[body_start:])}"


def _render_blockquote(_line: str, _m: Match) -> str:
    depth = 0
    rest = _line
    while rest.startswith(">"):
        rest = rest[1:]
        depth += 1
    body = rest.strip()
    return f"  {D}{G}{'| ' * depth}{R}{InlineRules.render(body)}"


def _render_table_row(_line: str, _m: Match) -> str:
    cells = [c.strip() for c in _line.strip("|").split("|")]
    if _is_table_separator(cells):
        return ""
    return "    " + " | ".join(InlineRules.render(c) for c in cells)


def _render_paragraph(_line: str, _m: Match) -> str:
    return "  " + InlineRules.render(_line)


_RE_TABLE_SEP = re.compile(r"^[-:]+$")


def _is_table_separator(cells: List[str]) -> bool:
    if not any(c for c in cells):
        return False
    return all(_RE_TABLE_SEP.match(c) for c in cells if c)


_re_head = re.compile(r"^(#{1,6})\s+(.+)")
_re_hr = re.compile(r"^[-*_]{3,}\s*$")
_re_task = re.compile(r"^[-*+]\s+\[([ xX])\]\s*(.*)")
_re_ul = re.compile(r"^[-*+]\s")
_re_ol = re.compile(r"^(\d+)[.)]\s")

BLOCK_RULES: List[BlockRule] = sorted([
    BlockRule(10, "heading",     lambda s: _re_head.match(s),   _render_heading),
    BlockRule(20, "hr",          lambda s: _re_hr.match(s),     _render_hr),
    BlockRule(30, "task",        lambda s: _re_task.match(s),   _render_task),
    BlockRule(40, "ul",          lambda s: _re_ul.match(s),     _render_ul),
    BlockRule(50, "ol",          lambda s: _re_ol.match(s),     _render_ol),
    BlockRule(60, "blockquote",  lambda s: s.startswith(">"),   _render_blockquote),
    BlockRule(70, "table",       lambda s: "|" in s and s.count("|") >= 2, _render_table_row),
    BlockRule(100, "paragraph",  lambda s: True,                _render_paragraph),
], key=lambda r: r.priority)


def render_line(line: str) -> str:
    """对单行文本应用块级规则表中的第一个匹配规则"""
    if not line.strip():
        return ""
    for rule in BLOCK_RULES:
        m = rule.match(line)
        if m:
            return rule.render(line, m)
    return InlineRules.render(line)


# ═══════════════════════════════════════════════════════════════
# 代码块渲染器
# ═══════════════════════════════════════════════════════════════

class CodeBlockRenderer:
    """渲染围栏代码块：语言标签 + 行号 + ANSI 着色"""

    MAX_LINE_LEN_MARGIN = 8

    @staticmethod
    def render(lang: str, body: str, width: int) -> str:
        color = _COLOR_MAP.get(
            _LANG_COLOR_MAP.get(lang.strip().lower(), "gray"), G)
        max_content = width - CodeBlockRenderer.MAX_LINE_LEN_MARGIN
        lines = []
        for i, raw in enumerate(body.split("\n"), 1):
            stripped = raw.rstrip()
            if len(stripped) > max_content:
                stripped = stripped[:max_content - 3] + "..."
            lines.append(f" {BG} {G}{i:>3} {R}{color}{stripped}{R}")
        label = lang.strip().lower() or "code"
        header = f"{G}{BG}  -- {label} --{R}"
        return header + "\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 流式 Markdown→ANSI 渲染器 (状态机 + list缓冲)
# ═══════════════════════════════════════════════════════════════

class StreamingRenderer:
    """
    字符级流式渲染器。
    状态机: NORMAL ↔ CODE_BLOCK
    _process_lines: 通用行消费者，消除 _feed_code/_feed_normal 的重复循环骨架。
    """

    def __init__(self) -> None:
        self.width = _terminal_width()
        self._buf_parts: List[str] = []
        self._in_code = False
        self._code_lang = ""
        self._code_lines: List[str] = []
        self._normal_line_count = 0

    def _buf_append(self, text: str) -> None:
        self._buf_parts.append(text)

    def _buf_get_and_clear(self) -> str:
        result = "".join(self._buf_parts)
        self._buf_parts.clear()
        return result

    def _process_lines(self, chunk: str,
                       handler: Callable[[str, str], bool]) -> None:
        """
        通用行消费者：
        - 累积 chunk 到缓冲区，逐完整行调用 handler(line, rest)。
        - handler 返回 True: 终止消费（handler 已自行处理 rest）。
        - handler 返回 False: 继续消费下一行。
        """
        self._buf_append(chunk)
        raw = self._buf_get_and_clear()
        while "\n" in raw:
            line, rest = raw.split("\n", 1)
            if handler(line, rest):
                return
            raw = rest + self._buf_get_and_clear()
        self._buf_append(raw)

    def feed(self, chunk: str) -> None:
        if self._in_code:
            self._process_lines(chunk, self._on_code_line)
        else:
            self._process_lines(chunk, self._on_normal_line)

    def _on_code_line(self, line: str, rest: str) -> bool:
        stripped = line.strip()
        if not self._code_lines and stripped.startswith("```"):
            self._code_lang = stripped[3:].strip()
            return False
        if stripped == "```":
            self._flush_code_block()
            self._in_code = False
            self._buf_append(rest)
            return True
        self._code_lines.append(line)
        return False

    def _on_normal_line(self, line: str, rest: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("```"):
            self._in_code = True
            self._code_lang = stripped[3:].strip()
            self._buf_append(rest)
            return True
        rendered = render_line(line)
        if not rendered:
            # 跳过空行，避免段落间多余换行
            return False
        sys.stdout.write(rendered + "\n")
        sys.stdout.flush()
        self._normal_line_count += 1
        return False

    def _flush_code_block(self) -> None:
        if self._code_lines:
            rendered = CodeBlockRenderer.render(
                self._code_lang,
                "\n".join(self._code_lines),
                self.width)
            sys.stdout.write(rendered + "\n")
        self._code_lines.clear()
        self._code_lang = ""
        sys.stdout.flush()

    def flush(self) -> None:
        if self._in_code:
            self._flush_code_block()
            self._in_code = False
        remaining = self._buf_get_and_clear()
        if remaining.strip():
            sys.stdout.write(render_line(remaining) + "\n")
            sys.stdout.flush()
        sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════
# 界面辅助函数
# ═══════════════════════════════════════════════════════════════

def show_header(msg: str) -> None:
    print(f"  {C}{msg}{R}")
    _sep()


def _spinner_thread(stop: threading.Event) -> None:
    frames = (f"{B}{O}*{R}", f"{D}{O}\u2736{R}")
    i = 0
    while not stop.is_set():
        sys.stdout.write(f"\r  {frames[i]} {O}思考中...{R}")
        sys.stdout.flush()
        i = (i + 1) % 2
        stop.wait(0.15)
    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()


def _compress_thread(stop: threading.Event) -> None:
    frames = (f"{B}{O}*{R}", f"{D}{O}\u2736{R}")
    i = 0
    while not stop.is_set():
        sys.stdout.write(f"\r  {frames[i]} {O}正在压缩...{R}")
        sys.stdout.flush()
        i = (i + 1) % 2
        stop.wait(0.15)
    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()


def show_interrupted() -> None:
    sys.stdout.write(f"\n  {Y}已打断{R}\n  {G}继续...{R}\n")
    sys.stdout.flush()


def show_stats(input_tokens: int, output_tokens: int,
               cache: int = 0, cost: float = 0.0) -> None:
    si = f"{input_tokens / 1000:.1f}k" if input_tokens >= 1000 else str(input_tokens)
    so = f"{output_tokens / 1000:.1f}k" if output_tokens >= 1000 else str(output_tokens)
    _sep()
    cs = f"  缓存:{cache / 1000:.1f}k" if cache > 0 else ""
    co = f"  费用:${cost:.4f}" if cost > 0 else ""
    sys.stdout.write(f"  {G}输入:{si} 输出:{so}{cs}{R}{Y}{co}{R}\n")


# ═══════════════════════════════════════════════════════════════
# 会话管理回调接口 ── 后端实现
# ═══════════════════════════════════════════════════════════════

class SessionCallbacks:
    """
    后端实现此类的四个方法，传入 UIInterface。
    方法返回字符串：空串表示成功，非空串为给用户的错误提示。
    """

    def on_save(self, name: str) -> str:
        """保存当前会话，参数为用户输入的名称"""
        return ""

    def on_show(self) -> str:
        """列出所有已保存会话，返回给用户的展示文本"""
        return ""

    def on_enter(self, name: str) -> str:
        """进入指定历史会话，返回会话文本或错误提示"""
        return ""

    def on_delete(self, name: str) -> str:
        """删除指定会话(name不为空)或全部(name为空或--all)，返回结果"""
        return ""

    def on_exit(self) -> str:
        """退出时自动保存，返回保存的会话名或空串"""
        return ""


# ═══════════════════════════════════════════════════════════════
# Tab 补全
# ═══════════════════════════════════════════════════════════════

_CMD_COMPLETER = WordCompleter(
    ["/clear", "/save", "/show", "/enter", "/delete", "/exit"],
    ignore_case=True, sentence=True,
    meta_dict={
        "/clear":  "清理屏幕",
        "/save":   "保存当前会话",
        "/show":   "显示所有会话",
        "/enter":  "进入历史会话",
        "/delete": "删除会话",
        "/exit":   "退出程序",
    })


def _dispatch_command(cmd: str, args: str, cb: SessionCallbacks) -> bool:
    cmd = cmd.lower().lstrip("/")
    if cmd == "clear":
        os.system("cls" if sys.platform == "win32" else "clear")
        return True
    if cmd == "save":
        if not args:
            print(f"  {Y}用法: /save <名称>{R}")
            return True
        result = cb.on_save(args)
        if result:
            print(f"  {X}{result}{R}")
        else:
            print(f"  {E}会话已保存: {C}{args}{R}")
        return True
    if cmd == "show":
        result = cb.on_show()
        if result:
            print(result)
        else:
            print(f"  {G}(无已保存会话){R}")
        return True
    if cmd == "enter":
        if not args:
            print(f"  {Y}用法: /enter <名称>{R}")
            return True
        result = cb.on_enter(args)
        if result:
            print(f"  {X}{result}{R}")
        else:
            print(f"  {E}已进入会话: {C}{args}{R}")
        return True
    if cmd == "delete":
        if not args:
            print(f"  {Y}用法: /delete <名称 | --all>{R}")
            return True
        result = cb.on_delete(args)
        if result:
            print(f"  {X}{result}{R}")
        else:
            print(f"  {E}已删除: {C}{args}{R}")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# UIInterface ── 后端对接接口
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
        self._started = False
        self._spinner_paused = False

    @property
    def cancelled(self) -> bool:
        return _interrupt_ctrl.is_set

    def begin(self) -> None:
        self._spinner_stop.clear()
        self._spinner_thread = threading.Thread(
            target=_spinner_thread,
            args=(self._spinner_stop,), daemon=True)
        self._spinner_thread.start()

    def feed(self, chunk: str) -> None:
        if not self._started:
            self._started = True
            self._spinner_stop.set()
            if self._spinner_thread is not None:
                self._spinner_thread.join(timeout=0.5)
        self._renderer.feed(chunk)

    def pause_spinner(self) -> None:
        """暂停spinner（工具执行前调用），清除当前行避免闪烁"""
        if not self._started and self._spinner_thread is not None and self._spinner_thread.is_alive():
            self._spinner_stop.set()
            self._spinner_paused = True
            # 清除spinner行
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

    def flush_renderer(self) -> None:
        """flush渲染器缓冲区，确保之前的文字已完整输出到终端"""
        if self._started:
            self._renderer.flush()

    def resume_spinner(self) -> None:
        """恢复spinner（工具执行后调用，仅当AI还在思考时）"""
        if self._spinner_paused and not self._started:
            self._spinner_paused = False
            self._spinner_stop.clear()
            self._spinner_thread = threading.Thread(
                target=_spinner_thread,
                args=(self._spinner_stop,), daemon=True)
            self._spinner_thread.start()

    def finish(self, input_tokens: int = 0, output_tokens: int = 0,
               cache: int = 0, cost: float = 0.0) -> None:
        self._spinner_stop.set()
        self._renderer.flush()
        show_stats(input_tokens, output_tokens, cache, cost)

    def abort(self) -> None:
        self._spinner_stop.set()
        show_interrupted()


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
        self._session = _create_session()

    def read_input(self) -> Optional[str]:
        if self._session is None:
            self.start()
        assert self._session is not None
        _interrupt_ctrl.clear()
        _interrupt_ctrl.enter_input_mode()
        line = read_input(self._session)
        if line is None:
            self._session = _create_session()
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
        self._session = _create_session()

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


_PROMPT_STYLE = Style.from_dict({"prompt": "bold #00ff00"})


def _create_session() -> PromptSession:
    return PromptSession(
        style=_PROMPT_STYLE,
        multiline=True,
        completer=_CMD_COMPLETER,
        key_bindings=_make_keybindings())


def read_input(session: PromptSession) -> Optional[str]:
    try:
        return session.prompt([("class:prompt", "# ")])
    except (KeyboardInterrupt, EOFError):
        return None



