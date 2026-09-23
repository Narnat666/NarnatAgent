"""主题 —— 色板应用、派生角色装配、ptk 样式、显示开关状态。

契约来源：`openspec/changes/recast-v2/specs/output/spec.md`
输入契约：`Theme.apply_style` / `DisplayState.apply_config` 消费的是 config
归一化后的标准「界面」配置（分组名/组内键名/开关键均为英文标准形；中英键裁决
唯一归属 config 积木——见 specs/config「配置键名兼容」与 specs/output
「中文键名与英文别名」的职责边界）。
- 「基础色板与角色引用」「派生角色与配方解析」：`Theme.apply_style` 的
  色板登记与角色装配（用户覆盖优先，未覆盖角色用默认配方重解析）；
- 「主题应用时机与全局生效」：启动时一次应用、每次先恢复默认再重放（幂等）、
  提示符文字色默认跟随「用户」色；
- 「显示开关数据流」：`DisplayState` 五开关（装配阶段一次性写入，渲染层读取）。

结构（design D7）：颜色实例、色板、PTK 样式与显示开关全部由实例承载；
消费方（渲染层、工具调度、headless 输出等）构造注入同一 `Theme` /
`DisplayState`，`apply_style` 改写共享的 `Color` 对象即达成"全局生效"语义。
"""
from __future__ import annotations

import re

from .colors import (
    BASE_ALIASES,
    BASE_BG,
    BASE_DEFS,
    BASE_NAMES,
    DERIVED_DEFS,
    HEX_RGB_RE,
    PTK_CUSTOM_DEFAULT,
    PTK_SYMBOL_DEFAULT,
    PTK_TEXT_DEFAULT,
    STYLE_CONSTANTS,
    STYLE_MAP,
    Color,
    ansi_color,
    hex_to_ansi,
    parse_recipe,
)
from .console import Console

__all__ = ["DEFAULT_CONTEXT_WINDOW", "DEFAULT_MAX_TOKENS", "DisplayState", "Theme",
           "apply_style"]

# 显示开关默认值（spec「显示开关数据流」）
DEFAULT_MAX_TOKENS = 128_000
DEFAULT_CONTEXT_WINDOW = 1_000_000


class DisplayState:
    """显示开关运行时状态（spec「显示开关数据流」）。

    数据流：配置 → 装配时一次性写入本对象 → 显示层读取（运行期改配置文件
    不热更新）。五项参数：

    - `show_cost`：显示费用（默认否）；
    - `show_balance`：显示余额（默认否；余额为 0 时渲染层不显示余额段）；
    - `max_tokens`：最大输出 token（默认 128000；「界面」未配置时的模型回填值
      由 config 归一化写入 `max_output_tokens`，本对象只消费）；
    - `show_ratio`：窗口占比（来源「压缩」分组的「占比显示」，装配补入）；
    - `context_window`：上下文窗口（来源「智能体」分组的「上下文窗口大小」，装配补入）。
    """

    def __init__(
        self,
        *,
        show_cost: bool = False,
        show_balance: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        show_ratio: bool = False,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
    ):
        self.show_cost = bool(show_cost)
        self.show_balance = bool(show_balance)
        self.max_tokens = int(max_tokens)
        self.show_ratio = bool(show_ratio)
        self.context_window = int(context_window)

    def apply_config(self, ui_config: dict) -> None:
        """按标准「界面」配置写入五个开关（覆盖旧值；非 dict 按空配置容错）。

        输入契约为 config 归一化后的配置（见模块 docstring）：直接读标准英文键
        `show_cost`/`show_balance`/`max_output_tokens`/`show_ratio`/
        `context_window`，缺省用默认值（非空字符串按真处理的怪癖保持）。
        """
        cfg = ui_config if isinstance(ui_config, dict) else {}
        self.show_cost = bool(cfg.get("show_cost", False))
        self.show_balance = bool(cfg.get("show_balance", False))
        self.max_tokens = int(cfg.get("max_output_tokens", DEFAULT_MAX_TOKENS))
        self.show_ratio = bool(cfg.get("show_ratio", False))
        self.context_window = int(cfg.get("context_window", DEFAULT_CONTEXT_WINDOW))


class Theme:
    """颜色体系实例（色板 + 派生角色 + ptk 样式）。

    消费方在装配时构造注入（design D7）：持本实例引用即"持有颜色引用"，
    `apply_style` 之后无需重新加载、立即生效。访问路径：

    - `theme.md_h1` / `theme.c_accent` / `theme.rst`：具名颜色（旧名小写，见
      `colors.DERIVED_DEFS` / `colors.BASE_ALIASES`）；
    - `theme.base("accent")` / `theme.role("markdown.heading_h1")`：按键查询；
    - `theme.parse_recipe(value)` / `theme.resolve_ptk_style(raw)`：配方与
      ptk 样式解析（读当前色板）；
    - `theme.ptk_prompt_symbol` / `ptk_prompt_text` / `ptk_prompt_custom`：
      输入提示符样式（ptk Style 字符串）。
    """

    def __init__(self, console: Console):
        self._console = console
        self._truecolor = bool(console.truecolor)
        self._palette: dict[str, Color] = {}
        self._hex_table: dict[str, str] = {}
        self._roles: dict[str, Color] = {}

        # 样式常量（不可配置）
        for attr, sequence in STYLE_CONSTANTS:
            setattr(self, attr, Color(sequence, console))
        self.r = self.rst
        self.b = self.bld
        self.d = self.dim

        # 基础色板（11 个内置色）
        for name, hex_value, is_bg in BASE_DEFS:
            color = Color(hex_to_ansi(hex_value, bg=is_bg, truecolor=self._truecolor), console)
            self._palette[name] = color
            self._hex_table[name] = hex_value
            setattr(self, f"c_{name}", color)
        for alias, target in BASE_ALIASES:
            setattr(self, alias, getattr(self, target))

        # 派生角色（43 个，默认配方解析）
        for attr, key, recipe in DERIVED_DEFS:
            color = Color(parse_recipe(recipe, self._palette, self._truecolor), console)
            self._roles[key] = color
            setattr(self, attr, color)

        # prompt_toolkit 样式默认值
        self.ptk_prompt_symbol = PTK_SYMBOL_DEFAULT
        self.ptk_prompt_text = PTK_TEXT_DEFAULT
        self.ptk_prompt_custom = PTK_CUSTOM_DEFAULT

    # ── 查询 ──

    def base(self, name: str) -> Color:
        """按色板名取基础色（未登记的名字抛 KeyError）。"""
        return self._palette[name]

    def role(self, key: str) -> Color:
        """按配置键取派生角色（如 `"markdown.heading_h1"`）。"""
        return self._roles[key]

    @property
    def hex_table(self) -> dict[str, str]:
        """当前色板名 → 十六进制值（副本；调试/快照用）。"""
        return dict(self._hex_table)

    @property
    def palette(self) -> dict[str, Color]:
        """当前色板名 → 颜色对象（副本；调试用）。"""
        return dict(self._palette)

    def parse_recipe(self, value: str) -> str:
        """按当前色板解析配方字符串（spec「派生角色与配方解析」）。"""
        return parse_recipe(value, self._palette, self._truecolor)

    def resolve_ptk_style(self, raw: str) -> str:
        """含配方 token 的 ptk 样式串 → ptk 可识别的 `#hex` 格式。

        基础色名优先取十六进制表；仅登记而未同步 hex 的名字从 ANSI 反推
        RGB，反推失败（如 16 色降级形态）保留原名文本（兼容怪癖，现状保持）。
        """
        parts: list[str] = []
        for token in raw.strip().split():
            if token in STYLE_MAP:
                parts.append(token)
            elif token in self._hex_table:
                parts.append(self._hex_table[token])
            elif token in self._palette:
                match = re.search(HEX_RGB_RE, self._palette[token].value)
                if match:
                    parts.append(
                        f"#{int(match.group(1)):02x}{int(match.group(2)):02x}"
                        f"{int(match.group(3)):02x}"
                    )
                else:
                    parts.append(token)
            else:
                parts.append(token)
        return " ".join(parts)

    # ── 主题应用（spec「主题应用时机与全局生效」）──

    def apply_style(self, ui_config: dict) -> None:
        """一次性应用「界面」配置（颜色 + ptk 样式）。

        输入契约为 config 归一化后的标准「界面」配置（英文分组与键名；见
        specs/config 与 specs/output 的职责边界），非 dict 按空配置容错。
        幂等：每次先恢复全部基础色的默认值并清空自定义登记，再重放本份配置
        （空配置应用等于完全恢复默认）。显示开关请另调
        `DisplayState.apply_config`（同一份配置 dict 可复用）。
        """
        cfg = ui_config if isinstance(ui_config, dict) else {}
        self._reset()
        self._apply_palette(cfg.get("colors"))
        self._apply_base_colors(cfg.get("base_colors"), cfg.get("colors"))
        self._apply_codeblock_bg(cfg)
        self._apply_derived(cfg)
        self._apply_prompt(cfg.get("prompt"))

    def _reset(self) -> None:
        """恢复默认色板（值 + 十六进制表）与 ptk 样式默认值，清空自定义登记。"""
        for name, hex_value, is_bg in BASE_DEFS:
            self._palette[name]._set(
                hex_to_ansi(hex_value, bg=is_bg, truecolor=self._truecolor)
            )
            self._hex_table[name] = hex_value
        for name in [n for n in self._palette if n not in BASE_NAMES]:
            del self._palette[name]
        for name in [n for n in self._hex_table if n not in BASE_NAMES]:
            del self._hex_table[name]
        self.ptk_prompt_symbol = PTK_SYMBOL_DEFAULT
        self.ptk_prompt_text = PTK_TEXT_DEFAULT
        self.ptk_prompt_custom = PTK_CUSTOM_DEFAULT

    def _apply_palette(self, colors_config: object) -> None:
        """① 色板登记：跳过 `_` 前缀键与非 `#` 开头值，登记的新名可被引用。"""
        if not isinstance(colors_config, dict):
            return
        for name, val in colors_config.items():
            if not isinstance(name, str) or name.startswith("_"):
                continue
            text = str(val)
            if not text.startswith("#"):
                continue
            try:
                ansi = hex_to_ansi(text, bg=BASE_BG.get(name, False), truecolor=self._truecolor)
            except (ValueError, IndexError):
                continue
            color = self._palette.get(name)
            if color is None:
                color = Color(ansi, self._console)
                self._palette[name] = color
            else:
                color._set(ansi)
            self._hex_table[name] = text

    def _apply_base_colors(self, base_colors_config: object, colors_config: object) -> None:
        """② 角色 → 调色板引用（调色板直查 hex → 同名色 → 已登记颜色，未命中跳过）。

        `#` 开头的项被跳过（十六进制直写不经本分组）。
        """
        if not isinstance(base_colors_config, dict):
            return
        colors_cfg = colors_config if isinstance(colors_config, dict) else {}
        for name, val in base_colors_config.items():
            if not isinstance(name, str) or name.startswith("_"):
                continue
            text = str(val)
            if text.startswith("#"):
                continue
            ref = text
            ref_hex = colors_cfg.get(ref)
            try:
                if isinstance(ref_hex, str) and ref_hex.startswith("#"):
                    ansi = hex_to_ansi(
                        ref_hex, bg=BASE_BG.get(name, False), truecolor=self._truecolor
                    )
                    self._hex_table[name] = ref_hex
                elif ref in self._hex_table:
                    ref_hex = self._hex_table[ref]
                    ansi = hex_to_ansi(
                        ref_hex, bg=BASE_BG.get(name, False), truecolor=self._truecolor
                    )
                    self._hex_table[name] = ref_hex
                elif ref in self._palette:
                    ansi = self._palette[ref].value
                else:
                    continue
            except (ValueError, IndexError):
                continue
            color = self._palette.get(name)
            if color is None:
                color = Color(ansi, self._console)
                self._palette[name] = color
            else:
                color._set(ansi)

    def _apply_codeblock_bg(self, cfg: dict) -> None:
        """③ 「代码块.背景」→ 代码块背景色（同解析顺序，命中后按背景色语义生成）。"""
        codeblock = cfg.get("codeblock")
        if not isinstance(codeblock, dict) or "background" not in codeblock:
            return
        colors_cfg = cfg.get("colors")
        colors_cfg = colors_cfg if isinstance(colors_cfg, dict) else {}
        ref = str(codeblock["background"])
        ref_hex = colors_cfg.get(ref)
        if isinstance(ref_hex, str) and ref_hex.startswith("#"):
            try:
                ansi = hex_to_ansi(ref_hex, bg=True, truecolor=self._truecolor)
            except (ValueError, IndexError):
                return
            self._palette["code_bg"]._set(ansi)
            self._hex_table["code_bg"] = ref_hex
        elif ref in self._hex_table:
            try:
                ansi = hex_to_ansi(
                    self._hex_table[ref], bg=True, truecolor=self._truecolor
                )
            except (ValueError, IndexError):
                return
            self._palette["code_bg"]._set(ansi)
            self._hex_table["code_bg"] = self._hex_table[ref]
        elif ref in self._palette:
            match = re.search(HEX_RGB_RE, self._palette[ref].value)
            if match:
                self._palette["code_bg"]._set(
                    ansi_color(
                        "48",
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        truecolor=self._truecolor,
                    )
                )

    def _apply_derived(self, cfg: dict) -> None:
        """④ 派生角色：用户覆盖优先，未覆盖用默认配方重解析（跟随基础色）。"""
        for _attr, key, default_recipe in DERIVED_DEFS:
            section_name, short_key = key.split(".", 1)
            section = cfg.get(section_name)
            user_value = section.get(short_key) if isinstance(section, dict) else None
            value = str(user_value) if user_value is not None else default_recipe
            self._roles[key]._set(parse_recipe(value, self._palette, self._truecolor))

    def _apply_prompt(self, prompt_config: object) -> None:
        """⑤ ptk 提示符样式；未显式配置「文字」时默认跟随「用户」色。"""
        prompt = prompt_config if isinstance(prompt_config, dict) else {}
        symbol = prompt.get("symbol")
        text = prompt.get("text")
        custom = prompt.get("custom")
        if isinstance(symbol, str):
            self.ptk_prompt_symbol = self.resolve_ptk_style(symbol)
        if isinstance(text, str):
            self.ptk_prompt_text = self.resolve_ptk_style(text)
        else:
            user_hex = self._hex_table.get("user")
            if user_hex:
                self.ptk_prompt_text = user_hex
            else:
                match = re.search(HEX_RGB_RE, self._palette["user"].value)
                if match:
                    self.ptk_prompt_text = (
                        f"#{int(match.group(1)):02x}{int(match.group(2)):02x}"
                        f"{int(match.group(3)):02x}"
                    )
        if isinstance(custom, str):
            self.ptk_prompt_custom = self.resolve_ptk_style(custom)


def apply_style(theme: Theme, ui_config: dict) -> None:
    """应用「界面」主题配置（等价 `theme.apply_style(ui_config)`）。

    包级入口，保留 `narnat_agent.output.apply_style` 旧消费名的可导入性
    （全部状态承载在 `Theme` 实例上，本函数不持有任何状态）；输入契约与
    `Theme.apply_style` 相同（config 归一化后的标准「界面」配置）。
    """
    theme.apply_style(ui_config)
