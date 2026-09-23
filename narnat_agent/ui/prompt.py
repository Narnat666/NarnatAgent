"""输入会话 —— 多行输入、历史文件、自定义提示与 Ctrl+C/EOF 语义。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「输入会话」：提示符固定 `# `（提示符色）、输入文本使用用户输入色、Enter 提交；
  Alt+Enter / Ctrl+O / Alt+J 在光标处插入换行而不提交；历史持久化到数据目录下的
  `.narnat_history`（数据目录为空时用用户主目录下的同名文件）；Ctrl+C 与文件结束
  （EOF）使当前读取返回空输入并重建输入会话（丢弃残留编辑缓冲），调用方把空输入
  视为继续；自定义提示输入（如删除确认）以主色显示提示文本，Ctrl+C / EOF 同样返回
  空输入；每次读取前清除中断标志并进入输入模式；
- 「打断提示与输入态恢复」：中断收敛时重建输入会话（历史记录仍可用）；
- 「兼容性怪癖保持」：换行键与提交键的键位保持现状。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from ..contracts.interrupt import InterruptSignal
from ..output import Console, Theme
from .markdown import MarkdownStyler

__all__ = [
    "HISTORY_FILENAME",
    "PROMPT_SYMBOL",
    "InputSession",
    "history_path",
    "make_keybindings",
    "prompt_style",
    "write_banner",
]

# 主输入提示符（已发布形态）
PROMPT_SYMBOL = "# "

# 历史文件名（数据目录或用户主目录下）
HISTORY_FILENAME = ".narnat_history"


def history_path(data_dir: str = "") -> str:
    """历史文件路径：`{数据目录}/.narnat_history`；数据目录为空时用用户主目录。"""
    base = data_dir if data_dir else os.path.expanduser("~")
    return os.path.join(base, HISTORY_FILENAME)


def make_keybindings() -> KeyBindings:
    """键绑定：Enter 提交；Alt+Enter / Ctrl+O / Alt+J 在光标处插入换行（均 eager）。"""
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter", eager=True)
    def _newline_alt_enter(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-o", eager=True)
    def _newline_ctrl_o(event):
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "j", eager=True)
    def _newline_alt_j(event):
        event.current_buffer.insert_text("\n")

    return kb


def prompt_style(theme: Theme) -> Style:
    """提示符样式：`prompt` 段取提示符色，默认段取用户输入色。"""
    return Style.from_dict({
        "prompt": theme.ptk_prompt_symbol,
        "": theme.ptk_prompt_text,
    })


def write_banner(theme: Theme, console: Console, model_name: str) -> None:
    """输出启动横幅（模型名，主色，两空格缩进）与分隔线。"""
    console.write(f"  {theme.ui_header}{model_name}{theme.rst}\n")
    MarkdownStyler(theme, console).separator()


class InputSession:
    """多行输入会话（prompt_toolkit 会话的持有者与重建者）。

    会话对象在构造时创建；`read` 返回 None 表示空输入（Ctrl+C / EOF），
    并在该情形下重建会话丢弃残留编辑缓冲。
    """

    def __init__(
        self,
        theme: Theme,
        interrupt: InterruptSignal,
        *,
        data_dir: str = "",
        completer=None,
        session_factory: Optional[Callable[[], PromptSession]] = None,
    ):
        """
        Args:
            theme: 颜色体系（ptk 样式与自定义提示取色）；
            interrupt: 中断信号总线（每次读取前进入输入模式并清除中断标志）；
            data_dir: 数据目录（历史文件落点；为空时用用户主目录）；
            completer: 补全器（`commands.CommandCompleter`）；
            session_factory: 会话构造工厂（缺省内部构造 PromptSession；测试可注入替身）。
        """
        self._theme = theme
        self._interrupt = interrupt
        self._history_path = history_path(data_dir)
        self._completer = completer
        self._make_session = session_factory or self._build_session
        # 懒创建（对齐旧实现时机）：构造阶段不建立 ptk 会话（装配不依赖真实控制台），
        # 首次 `ensure_ready()` / `read()` 时才创建。
        self._session: Optional[PromptSession] = None

    def ensure_ready(self) -> None:
        """确保输入会话已建立（幂等；在启动界面或首次读取前调用）。"""
        if self._session is None:
            self._session = self._make_session()

    def read(self, prompt_text: Optional[str] = None) -> Optional[str]:
        """读取一次输入（Enter 提交；可含内部换行）。

        Args:
            prompt_text: 自定义提示文本（如 `  确认执行此命令? [y/N]: `）；
                None 时使用固定提示符 `# `。

        Returns:
            输入文本；Ctrl+C / EOF 返回 None（空输入，调用方按继续处理），
            并重建输入会话。
        """
        self.ensure_ready()
        self._interrupt.enter_input_mode()
        prompt = (
            [("class:prompt", PROMPT_SYMBOL)]
            if prompt_text is None
            else ANSI(f"{self._theme.c_primary}{prompt_text}{self._theme.rst}")
        )
        try:
            return self._session.prompt(prompt)
        except (KeyboardInterrupt, EOFError):
            self.rebuild()
            return None

    def rebuild(self) -> None:
        """重建输入会话（丢弃残留编辑缓冲；历史记录仍可用）。"""
        self._session = self._make_session()

    def _build_session(self) -> PromptSession:
        """构造 ptk 会话：多行 + 补全器 + 换行键绑定 + 文件历史。"""
        return PromptSession(
            style=prompt_style(self._theme),
            multiline=True,
            completer=self._completer,
            key_bindings=make_keybindings(),
            history=FileHistory(self._history_path),
        )
