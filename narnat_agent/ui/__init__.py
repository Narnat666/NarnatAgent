"""ui 积木 —— 交互界面（渲染面 + 交互层）。

积木构成：
- `width`：终端宽度探测（env 强制 / conhost 可见窗口实测 / srWindow 二次校验）
  与显示宽度度量（CJK 双宽、ANSI 剔除、软折行）；
- `markdown`：Markdown 解析与着色——行内替换流水线（`InlineRules`）、块级规则表
  （`BlockRule` / `MarkdownStyler`）、表格单元格拆分与列宽拟合（`split_cells` /
  `is_table_separator` / `fit_widths`）、代码块（`CodeBlockRenderer`）、差异着色
  （`colorize_diff`）；
- `render`：流式渲染器 `StreamingRenderer`（普通行 ↔ 代码块状态机、表格缓冲与
  落定路由）与表格布局 `TableRenderer`（直排 / 折行 / 分块）；
- `animator`：原地刷新帧动画（思考中 / 正在压缩 / 正在合并）与成对操作端口实现
  `UiAnimator`；
- `stream`：流式输出会话句柄 `UiSink`（`OutputSink` 实现：思考中动画、落定守卫、
  暂停恢复计数、统计栏、打断提示）与会话级交互 `UiInteraction`（`InteractionPort`
  实现 + 主循环读入口）、统计栏格式化 `stats_line`；
- `commands`：命令分发骨架 `CommandRouter`（归一 / 可用性门禁 / 三态结果）与
  Tab 补全器 `CommandCompleter`（候选来自注入的命令源端口 `CommandSource`）；
- `prompt`：输入会话 `InputSession`（`# ` 提示符、多行、历史文件、Ctrl+C/EOF）、
  键绑定、启动横幅；
- `headless`：纯文本实现（`HeadlessSink` / `HeadlessInteraction` /
  `HeadlessAnimator`）与 headless 输出环境置位（`prepare_console`）。

设计决策（design D7/D12）：颜色一律经构造注入的 `output.Theme` 取用（渲染器不含
全局颜色态，`apply_style` 之后立即生效）；宽度探测与平台探测经构造注入可替换；
终端状态（纯文本 / 静默开关）承载在注入的 `output.Console` 实例上，模块内无全局
状态；命令实现与可用性表归 `sessions` 积木，经 `CommandSource` 端口注入。
渲染管道按"解析 → 布局 → 着色"切分，算法语义与旧实现逐函数对应。

装配用法：

    from narnat_agent.output import Console, DisplayState, Theme
    from narnat_agent.ui import CommandCompleter, CommandRouter, InputSession, UiInteraction

    console = Console()
    theme = Theme(console)          # 启动阶段 apply_style(ui_cfg) 后立即生效
    display = DisplayState()
    completer = CommandCompleter(command_source)     # sessions 提供的命令源
    session = InputSession(theme, interrupt, data_dir="", completer=completer)
    ui = UiInteraction(theme, console, interrupt, display, session,
                       CommandRouter(command_source, console))
    ui.start(model_name)            # 横幅 + 分隔线
    sink = ui.begin_turn()          # 进入运行模式 + 启动思考动画
    sink.feed("## 标题\n")
    sink.finish(stats)              # 落定 + 统计栏
"""
from .animator import (
    ANIMATION_FRAME_INTERVAL,
    SPINNER_DELAY,
    SPINNER_LABEL,
    Animation,
    UiAnimator,
    frame_texts,
    join_thread,
)
from .commands import (
    CLEAR_SCREEN,
    CommandCompleter,
    CommandReply,
    CommandResult,
    CommandRouter,
    CommandSource,
    SkillNode,
)
from .headless import (
    HeadlessAnimator,
    HeadlessInteraction,
    HeadlessSink,
    enable_utf8_stdout,
    prepare_console,
)
from .markdown import (
    LANG_GROUPS,
    BlockRule,
    CodeBlockRenderer,
    InlineRules,
    MarkdownStyler,
    colorize_diff,
    fit_widths,
    is_table_separator,
    split_cells,
)
from .prompt import (
    PROMPT_SYMBOL,
    InputSession,
    history_path,
    make_keybindings,
    prompt_style,
    write_banner,
)
from .render import StreamingRenderer, TableRenderer
from .stream import (
    UiInteraction,
    UiSink,
    format_max_tokens,
    format_tokens,
    stats_line,
    write_notice,
)
from .width import (
    MAX_TERMINAL_WIDTH,
    char_width,
    display_width,
    srwindow_cols,
    terminal_width,
    visual_chars,
    windows_console_window_cols,
    wrap_cell,
)

__all__ = [
    "ANIMATION_FRAME_INTERVAL",
    "CLEAR_SCREEN",
    "LANG_GROUPS",
    "MAX_TERMINAL_WIDTH",
    "PROMPT_SYMBOL",
    "SPINNER_DELAY",
    "SPINNER_LABEL",
    "Animation",
    "BlockRule",
    "CodeBlockRenderer",
    "CommandCompleter",
    "CommandReply",
    "CommandResult",
    "CommandRouter",
    "CommandSource",
    "HeadlessAnimator",
    "HeadlessInteraction",
    "HeadlessSink",
    "InlineRules",
    "InputSession",
    "MarkdownStyler",
    "SkillNode",
    "StreamingRenderer",
    "TableRenderer",
    "UiAnimator",
    "UiInteraction",
    "UiSink",
    "char_width",
    "colorize_diff",
    "display_width",
    "enable_utf8_stdout",
    "fit_widths",
    "format_max_tokens",
    "format_tokens",
    "frame_texts",
    "history_path",
    "is_table_separator",
    "join_thread",
    "make_keybindings",
    "prepare_console",
    "prompt_style",
    "split_cells",
    "srwindow_cols",
    "stats_line",
    "terminal_width",
    "visual_chars",
    "windows_console_window_cols",
    "wrap_cell",
    "write_banner",
    "write_notice",
]
