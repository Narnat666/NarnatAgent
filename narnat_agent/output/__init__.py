"""output 积木 —— 终端输出原语与颜色体系（聚合导出）。

积木构成：
- `console.Console`：输出原语（plain/quiet 开关、`write`/`try_write`、Windows
  虚拟终端启用与重申、真彩能力检测）——实例承载全部状态；
- `colors`：ANSI 生成（`hex_to_ansi`/`ansi_color`/`parse_recipe`）、颜色容器
  `Color`、色板与派生角色定义表（`BASE_DEFS`/`DERIVED_DEFS`）；
- `style.Theme`：颜色体系（色板登记、角色装配、ptk 样式，`apply_style` 应用）；
- `style.DisplayState`：显示开关五参数（装配写入、显示层读取）；
- `style.apply_style`：包级应用入口（`Theme.apply_style` 的薄转发，旧消费名可导入）。

输入契约：`apply_style` / `DisplayState.apply_config` 消费的是 config 归一化后的
标准「界面」配置（中英键裁决唯一归属 config 积木，见 specs/config 与 specs/output
的职责边界）。

装配用法（积木内无全局单例，全部构造注入）：

    from narnat_agent.output import Console, DisplayState, Theme

    console = Console()                       # 真彩能力检测 + Windows VT 启用
    console.set_plain(True)                   # headless 一次性任务（-p）置位
    console.set_quiet_tools(True)             # headless 默认静默（-l 关闭）
    theme = Theme(console)                    # 颜色体系（渲染层等持本引用）
    display = DisplayState()                  # 显示开关（统计栏读取）
    # ui_cfg：config 归一化后的标准「界面」配置 dict（装配侧组装分组建与
    # show_cost/show_balance/max_output_tokens/show_ratio/context_window）
    theme.apply_style(ui_cfg)                 # 启动阶段一次性应用
    display.apply_config(ui_cfg)

旧包消费名对照（迁移指引；语义不变、承载对象变为实例）：

| 旧名（`narnat_agent/output.py`） | 新路径 |
|---|---|
| `write(text)` / `try_write(text)` | `console.write(text)` / `console.try_write(text)` |
| `set_plain` / `is_plain` | `console.set_plain` / `console.is_plain` |
| `set_quiet_tools` / `is_quiet_tools` | `console.set_quiet_tools` / `console.is_quiet_tools` |
| `_stdout_lock` | `console.lock` |
| `apply_style(ui_config)` | `theme.apply_style(ui_config)`（包级入口 `apply_style(theme, ui_config)` 为薄转发，旧消费名保持可导入） |
| `DisplayState.show_cost` 等类属性 | `display.show_cost` 等实例属性 |
| `C_ACCENT` / `MD_H1` / `R` / `D` / `X` 等 | `theme.c_accent` / `theme.md_h1` / `theme.r` / `theme.d` / `theme.x` |
| `PTK_PROMPT_SYMBOL/TEXT/CUSTOM` | `theme.ptk_prompt_symbol/ptk_prompt_text/ptk_prompt_custom` |
| `_Color.__str__` / `__repr__` | `Color.__str__` / `Color.__repr__`（同语义） |
| `_parse_recipe` / `_hex_to_ansi` | `hex_to_ansi` / `theme.parse_recipe` |

颜色实例由 `Theme` 实例持有（`apply_style` 改写共享对象 → 持引用的模块立即生效）；
消费方通过构造注入取得 `Theme`，不得在模块级缓存颜色快照。
"""
from .colors import (
    BASE_DEFS,
    DERIVED_DEFS,
    Color,
    ansi_color,
    hex_to_ansi,
    parse_recipe,
)
from .console import Console, detect_truecolor
from .style import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    DisplayState,
    Theme,
    apply_style,
)

__all__ = [
    "BASE_DEFS",
    "DERIVED_DEFS",
    "Color",
    "Console",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
    "DisplayState",
    "Theme",
    "ansi_color",
    "apply_style",
    "detect_truecolor",
    "hex_to_ansi",
    "parse_recipe",
]
