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
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# ═══════════════════════════════════════════════════════════════
# ANSI 转义序列常量  (Salt Flow 配色 - 椒盐音乐风格)
# ═══════════════════════════════════════════════════════════════

RST = "\x1b[0m"
BLD = "\x1b[1m"
DIM = "\x1b[2m"

# 核心中性色（灰蓝调，现代沉浸感）
GRY = "\x1b[38;2;100;116;139m"      # 灰蓝 #64748B（次要文字、分隔线）

# 主题流光色（青绿/蓝紫为主，低饱和舒适）
CYN = "\x1b[38;2;94;234;212m"       # 流光青 #5EEAD4（主题主色、标题）
GRN = "\x1b[38;2;52;211;153m"       # 薄荷绿 #34D399（成功、添加行）
YLW = "\x1b[38;2;251;191;36m"       # 暖琥珀 #FBBF24（行内代码、提示）
RED = "\x1b[38;2;248;113;113m"      # 珊瑚红 #F87171（错误、删除行）
BLU = "\x1b[38;2;167;139;250m"      # 薰衣草紫 #A78BFA（链接、交互）
MAG = "\x1b[38;2;232;121;249m"      # 粉紫流光 #E879F9（装饰、品牌色）
ORG = "\x1b[38;2;251;146;60m"       # 桃橙色 #FB923C（强调、spinner）

# 背景色（深夜蓝黑，沉浸式代码块背景）
BG8 = "\x1b[48;2;15;23;42m"         # 深夜蓝 #0F172A

# 文字色（保持不变）
WHT = "\x1b[38;2;255;255;255m"      # 极致白色 #FFFFFF（用户输入）❗ 不变
WHT7 = "\x1b[38;2;255;255;208m"     # 偏黄米白 #FFFFD0（AI输出）❗ 不变

R, B, D, G, C = RST, BLD, DIM, GRY, CYN
E, Y, X, U, M, O, BG = GRN, YLW, RED, BLU, MAG, ORG, BG8
W, W7 = WHT, WHT7


def _terminal_width() -> int:
    try:
        return min(shutil.get_terminal_size().columns, 160)
    except Exception:
        return 120


def colorize_diff(diff_text: str) -> str:
    """对 unified diff 文本着色：-行红色、+行绿色、@@行青色暗淡，其余灰色。

    保留行首的 +/- 符号，仅对内容着色，不改变文本结构。
    """
    if not diff_text or diff_text == "(无差异)":
        return f"{G}(无差异){R}"

    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            # 文件头行：粗体 + 对应颜色
            out.append(f"{B}{C}{line}{R}")
        elif line.startswith("@@"):
            # 位置行：青色暗淡
            out.append(f"{D}{C}{line}{R}")
        elif line.startswith("-"):
            # 删除行：红色
            out.append(f"{X}{line}{R}")
        elif line.startswith("+"):
            # 添加行：绿色
            out.append(f"{E}{line}{R}")
        else:
            # 上下文行：灰色
            out.append(f"{G}{line}{R}")
    return "\n".join(out)


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
        # signal.signal只能在主线程调用
        if threading.current_thread() is threading.main_thread():
            if self._saved_sigint is not None:
                signal.signal(signal.SIGINT, self._saved_sigint)
                self._saved_sigint = None
            else:
                # 编译为exe后Ctrl+C会直接杀进程导致崩溃，忽略SIGINT，用ESC中断
                if getattr(sys, 'frozen', False):
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                else:
                    signal.signal(signal.SIGINT, signal.default_int_handler)

    def enter_run_mode(self) -> None:
        self._interrupt.clear()
        self._stop_poll.clear()
        self._poll_thread = threading.Thread(target=self._poll_esc, daemon=True)
        self._poll_thread.start()
        # signal.signal只能在主线程调用
        if threading.current_thread() is threading.main_thread():
            if self._saved_sigint is not None:
                signal.signal(signal.SIGINT, self._saved_sigint)
                self._saved_sigint = None
            else:
                # 编译为exe后Ctrl+C会直接杀进程导致崩溃，忽略SIGINT，用ESC中断
                if getattr(sys, 'frozen', False):
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                else:
                    signal.signal(signal.SIGINT, signal.default_int_handler)

    def _poll_esc(self) -> None:
        """轮询ESC键。Windows使用msvcrt，Unix使用select+termios。"""
        if sys.platform == "win32":
            self._poll_esc_windows()
        else:
            self._poll_esc_unix()

    def _poll_esc_windows(self) -> None:
        """Windows下检测ESC键。优先msvcrt，非原生控制台回退ReadConsoleInput。"""
        try:
            import msvcrt
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # 原生控制台: 使用msvcrt
                self._poll_esc_windows_native(msvcrt)
            else:
                # 非原生控制台(Windows Terminal等): 使用ReadConsoleInput
                self._poll_esc_windows_coninput(kernel32)
        except (ImportError, OSError, AttributeError):
            pass

    def _poll_esc_windows_native(self, msvcrt) -> None:
        """原生CMD下使用msvcrt检测ESC键。"""
        while not self._stop_poll.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b'\x1b':
                        time.sleep(0.02)
                        if not msvcrt.kbhit():
                            self._interrupt.set()
                            break
                        # 转义序列，消费掉后续字符
                        while msvcrt.kbhit():
                            msvcrt.getch()
            except OSError:
                break
            self._stop_poll.wait(0.03)

    def _poll_esc_windows_coninput(self, kernel32) -> None:
        """非原生控制台(Windows Terminal等)下使用ReadConsoleInput检测ESC键。"""
        import ctypes

        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE

        # INPUT_RECORD 结构体: 20 bytes (EventType 2bytes + padding 2bytes + Event 16bytes)
        # KEY_EVENT_RECORD: bKeyDown(4) + wRepeatCount(2) + wVirtualKeyCode(2) + wVirtualScanCode(2) + UnicodeChar(2) + dwControlKeyState(4)
        INPUT_RECORD_SIZE = 20
        KEY_EVENT = 0x0001
        VK_ESCAPE = 0x1B

        buf = (ctypes.c_char * (INPUT_RECORD_SIZE * 8))()  # 一次读8条
        records_read = ctypes.c_ulong()

        while not self._stop_poll.is_set():
            try:
                # WaitForSingleObject 等待控制台输入，超时50ms
                result = kernel32.WaitForSingleObject(handle, 50)
                if result != 0:  # WAIT_TIMEOUT=0x102, WAIT_FAILED=0xFFFFFFFF
                    continue

                # 读取输入记录
                if not kernel32.ReadConsoleInputW(
                    handle, buf, 8, ctypes.byref(records_read)
                ):
                    break

                for i in range(records_read.value):
                    offset = i * INPUT_RECORD_SIZE
                    event_type = int.from_bytes(
                        buf[offset:offset+2], byteorder='little', signed=False
                    )
                    if event_type != KEY_EVENT:
                        continue
                    # KEY_EVENT_RECORD 偏移4字节处是 bKeyDown (BOOL, 4 bytes)
                    key_down = int.from_bytes(
                        buf[offset+4:offset+8], byteorder='little', signed=False
                    )
                    if not key_down:
                        continue
                    # wVirtualKeyCode 偏移10字节处 (2 bytes)
                    vk_code = int.from_bytes(
                        buf[offset+10:offset+12], byteorder='little', signed=False
                    )
                    if vk_code == VK_ESCAPE:
                        self._interrupt.set()
                        return
            except (OSError, ValueError):
                break
            self._stop_poll.wait(0.02)

    def _poll_esc_unix(self) -> None:
        """Unix/Linux/macOS下使用select+termios检测ESC键。"""
        try:
            import select
            import termios
            import tty
        except ImportError:
            return

        fd = sys.stdin.fileno()
        old_settings = None
        try:
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)  # 设置为cbreak模式，允许单字符读取
        except (termios.error, OSError):
            return  # 无法设置终端模式（如管道输入）

        try:
            while not self._stop_poll.is_set():
                try:
                    # 使用select检测stdin是否有数据，超时30ms
                    ready, _, _ = select.select([sys.stdin], [], [], 0.03)
                    if ready:
                        ch = os.read(fd, 1)
                        if ch == b'\x1b':
                            # 等待短暂时间判断是否为转义序列
                            time.sleep(0.02)
                            ready2, _, _ = select.select([sys.stdin], [], [], 0.01)
                            if not ready2:
                                self._interrupt.set()
                                break
                            # 转义序列，消费掉后续字符
                            while True:
                                ready3, _, _ = select.select(
                                    [sys.stdin], [], [], 0.005)
                                if not ready3:
                                    break
                                os.read(fd, 1)
                except (OSError, ValueError):
                    break
        finally:
            # 恢复终端原始设置
            if old_settings is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except (termios.error, OSError):
                    pass


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
        text = cls._RE_BOLD.sub(f"{B}{W7}\\1{R}", text)
        text = cls._RE_ITAL.sub(f"{D}{W7}\\1{R}", text)
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
    return f"  {B}{W7}{body}{R}"


def _render_hr(_line: str, _m: Match) -> str:
    return f"  {G}{_line.strip()}{R}"


def _render_task(_line: str, m: Match) -> str:
    done = m.group(1).lower() == "x"
    marker = f"{E}v{R}" if done else f"{G}o{R}"
    return f"   {marker} {W7}{InlineRules.render(m.group(2))}{R}"


def _render_ul(_line: str, _m: Match) -> str:
    return f"   {E}*{R} {W7}{InlineRules.render(_line[2:])}{R}"


def _render_ol(_line: str, m: Match) -> str:
    num = m.group(1)
    body_start = len(num) + 2
    return f"   {G}{num}.{R} {W7}{InlineRules.render(_line[body_start:])}{R}"


def _render_blockquote(_line: str, _m: Match) -> str:
    depth = 0
    rest = _line
    while rest.startswith(">"):
        rest = rest[1:]
        depth += 1
    body = rest.strip()
    return f"  {D}{G}{'| ' * depth}{R}{W7}{InlineRules.render(body)}{R}"


def _render_table_row(_line: str, _m: Match) -> str:
    cells = [c.strip() for c in _line.strip("|").split("|")]
    if _is_table_separator(cells):
        return ""
    return f"    {W7}" + " | ".join(InlineRules.render(c) for c in cells) + f"{R}"


def _render_paragraph(_line: str, _m: Match) -> str:
    return f"  {W7}{InlineRules.render(_line)}{R}"


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
        # 循环处理：handler可能切换状态（_in_code），
        # 切换后需要用新handler继续处理剩余内容
        self._buf_append(chunk)
        raw = self._buf_get_and_clear()
        while raw:
            handler = self._on_code_line if self._in_code else self._on_normal_line
            # 逐行处理
            while "\n" in raw:
                line, rest = raw.split("\n", 1)
                if handler(line, rest):
                    # handler返回True：状态已切换，用新handler继续处理rest
                    raw = rest
                    break
                raw = rest
            else:
                # while正常结束（无更多完整行），剩余放回缓冲区
                self._buf_append(raw)
                return
            # handler返回True后，raw=rest，继续外层while用新handler处理
            # 但先合并缓冲区中可能新追加的内容
            extra = self._buf_get_and_clear()
            if extra:
                raw = raw + extra

    def _on_code_line(self, line: str, rest: str) -> bool:
        stripped = line.strip()
        if not self._code_lines and stripped.startswith("```"):
            self._code_lang = stripped[3:].strip()
            return False
        if stripped == "```":
            self._flush_code_block()
            self._in_code = False
            # 不再放回rest，由feed()外层循环用新handler处理
            return True
        self._code_lines.append(line)
        return False

    def _on_normal_line(self, line: str, rest: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("```"):
            self._in_code = True
            self._code_lang = stripped[3:].strip()
            # 不再放回rest，由feed()外层循环用新handler处理
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

    def on_list_names(self) -> list:
        """返回所有已保存会话的名称列表，供Tab补全使用"""
        return []

    def on_exit(self) -> str:
        """退出时自动保存，返回保存的会话名或空串"""
        return ""


# ═══════════════════════════════════════════════════════════════
# Tab 补全
# ═══════════════════════════════════════════════════════════════

class _CommandCompleter(Completer):
    """命令补全：/enter /delete 动态补全会话名，其余命令静态补全"""

    _COMMANDS = {
        "/clear":  "清理屏幕",
        "/save":   "保存当前会话",
        "/show":   "显示所有会话",
        "/enter":  "进入历史会话",
        "/delete": "删除会话",
        "/exit":   "退出程序",
    }
    # 需要动态补全会话名的命令
    _NAME_COMMANDS = {"/enter", "/delete"}

    def __init__(self, callbacks: SessionCallbacks):
        self._cb = callbacks

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        # 不以/开头的不补全
        if not text.startswith("/"):
            return

        parts = text.split()
        num_parts = len(parts)

        # 输入中光标前无空格 → 补全命令名
        if num_parts == 1 and not text.endswith(" "):
            word = parts[0].lower()
            for cmd, meta in self._COMMANDS.items():
                if cmd.startswith(word):
                    yield Completion(
                        cmd[len(word):],
                        start_position=0,
                        display_meta=meta,
                    )
            return

        # 命令后有空格 → 补全参数
        if num_parts >= 1:
            cmd = parts[0].lower()
            if cmd in self._NAME_COMMANDS:
                # 动态获取会话名
                names = self._cb.on_list_names()
                if num_parts == 1 and text.endswith(" "):
                    # 刚输入完命令+空格，补全所有会话名
                    for name in names:
                        yield Completion(name, start_position=0)
                elif num_parts == 2 and not text.endswith(" "):
                    # 正在输入会话名，按前缀过滤
                    prefix = parts[1]
                    for name in names:
                        if name.startswith(prefix):
                            yield Completion(
                                name[len(prefix):],
                                start_position=0,
                            )


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
        self._spinner_pause_count = 0  # 并行工具pause计数，归零才恢复spinner

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
        self._spinner_pause_count += 1
        if self._spinner_pause_count == 1 and not self._started and self._spinner_thread is not None and self._spinner_thread.is_alive():
            self._spinner_stop.set()
            # 清除spinner行
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

    def flush_renderer(self) -> None:
        """flush渲染器缓冲区，确保之前的文字已完整输出到终端"""
        if self._started:
            self._renderer.flush()

    def resume_spinner(self) -> None:
        """恢复spinner（工具执行后调用），所有并行工具完成后才真正恢复"""
        self._spinner_pause_count = max(0, self._spinner_pause_count - 1)
        if self._spinner_pause_count == 0 and not self._started:
            self._spinner_stop.clear()
            self._spinner_thread = threading.Thread(
                target=_spinner_thread,
                args=(self._spinner_stop,), daemon=True)
            self._spinner_thread.start()

    def finish(self, input_tokens: int = 0, output_tokens: int = 0,
               cache: int = 0, cost: float = 0.0) -> None:
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询，防止误触发
        self._spinner_stop.set()
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=0.5)
        # 清除spinner残留行
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
        self._renderer.flush()
        show_stats(input_tokens, output_tokens, cache, cost)

    def abort(self) -> None:
        _interrupt_ctrl.enter_input_mode()  # 立即停止ESC轮询
        self._spinner_stop.set()
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=0.5)
        # 清除spinner残留行
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
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