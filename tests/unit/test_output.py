"""output 积木单元测试 —— spec Scenario 映射 + 基线对照。

行为金标准：`openspec/changes/recast-v2/specs/output/spec.md`（9 个 Requirement / 32 个 Scenario）。

映射表（Scenario → 测试）：

| Requirement | Scenario | 测试 |
|---|---|---|
| R1 基础色板与角色引用 | 默认色板生效 | `TestPalette::test_default_palette` |
| | 自定义调色板与角色引用 | `TestPalette::test_custom_palette_role_reference` |
| | 无效项静默跳过 | `TestPalette::test_invalid_entries_skipped` |
| R2 派生角色与配方解析 | 组合配方 | `TestRecipe::test_combined_recipe` |
| | 多样式与颜色组合 | `TestRecipe::test_multi_style_recipe` |
| | 未知 token 忽略 | `TestRecipe::test_unknown_token_ignored` |
| | 默认配方跟随基础色 | `TestRecipe::test_default_recipe_follows_base_color` |
| R3 中文键名与英文别名 | 中英键裁决（中文键生效/英文优先/旧扁平键迁移/配方中文化）归 config 积木测试面；output 纯消费归一化后的标准格式，见 `TestDisplayState::test_chinese_keys_not_consumed` | |
| R4 全局输出开关 | headless 纯文本 | `TestPlainQuiet::test_plain_color_empty_str` / `test_plain_write_no_cr` |
| | 静默工具日志 | `TestPlainQuiet::test_quiet_tools_switch` |
| | `-l` 显示全量日志 | `TestPlainQuiet::test_quiet_tools_switch`（关闭静默即全量） |
| R5 终端能力适配 | 真彩终端 | `TestTerminal::test_truecolor_sequence` |
| | 16 色降级 | `TestTerminal::test_16color_fallback` |
| | 重定向到文件 | `TestTerminal::test_no_console_skips_vt_reassert` |
| | 子进程重置后重申 | `TestTerminal::test_vt_reassert_after_reset` |
| R6 显示开关数据流 | 费用段开关（数据面） | `TestDisplayState::test_apply_config_all_switches` |
| | 余额为 0 不显示（数据面） | `TestDisplayState::test_apply_config_all_switches` |
| | 占比缺数据（数据面） | `TestDisplayState::test_apply_config_all_switches` |
| | 最大输出格式（数据面） | `TestDisplayState::test_defaults` / `test_apply_config_missing_keys_use_defaults` |
| R7 主题应用时机与全局生效 | 启动时应用一次 | `TestThemeApply::test_apply_once_takes_effect` |
| | 重复应用幂等 | `TestThemeApply::test_repeat_apply_idempotent` |
| | 提示符跟随用户色 | `TestThemeApply::test_prompt_follows_user_color` |
| R8 输出原语与行控制 | 非阻塞写出失败 | `TestWritePrimitives::test_try_write_fails_when_locked` |
| | 阻塞写出的行首回车 | `TestWritePrimitives::test_blocking_write_has_leading_cr` |
| | 纯文本模式无行首回车 | `TestPlainQuiet::test_plain_write_no_cr` |
| | 动画原地刷新 | `TestWritePrimitives::test_write_provides_no_erase_sequence`（擦除归调用方） |
| R9 兼容性怪癖保持 | 强调色替换怪癖 | `TestQuirks::test_token_after_replacement_ignored`（替换归 config；输出面验证无效 token 静默忽略） |
| | 调试表示不受纯文本影响 | `TestQuirks::test_debug_repr_ignores_plain` |
| | 元数据键跳过 | `TestQuirks::test_metadata_key_skipped` |

R6 的四个 Scenario 的统计栏**渲染**部分归 ui 积木（T4.10），本积木覆盖
"装配写入 → 显示层读取"的数据面；R8「动画原地刷新」的回车 + 擦除序列由调用方
自行实现，本积木验证原语不注入擦除。R3 的中英键裁决（含旧扁平键迁移、配方值
中文化）唯一归属 config 积木测试面，本积木用例一律直接喂归一化后的标准格式。

基线对照：`v2/tests/baseline/data/output_style.json`（旧实现行为基准，27 用例）。
"""
from __future__ import annotations

import io
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from narnat_agent.output import (
    BASE_DEFS,
    Console,
    Color,
    DisplayState,
    Theme,
    apply_style,
    hex_to_ansi,
)
from narnat_agent.output.colors import DERIVED_DEFS
from narnat_agent.output.console import ENABLE_VIRTUAL_TERMINAL_PROCESSING

BASELINE_FILE = (
    Path(__file__).resolve().parents[1] / "baseline" / "data" / "output_style.json"
)

# 默认色板全表（与基线 apply_style 用例的输入一致）
DEFAULT_COLORS = {name: hex_value for name, hex_value, _bg in BASE_DEFS}

ANSI_ACCENT = "\x1b[38;2;94;234;212m"
ANSI_PRIMARY = "\x1b[38;2;255;255;208m"
ANSI_SECONDARY = "\x1b[38;2;100;116;139m"
ANSI_CODE_BG = "\x1b[48;2;15;23;42m"


def make_console(
    *, truecolor: bool = True, stdout: io.StringIO | None = None, platform: str = "linux"
) -> Console:
    """构造隔离的 Console 实例（不依赖全局态与真实终端）。"""
    return Console(
        stdout if stdout is not None else io.StringIO(),
        truecolor=truecolor,
        platform=platform,
    )


def make_theme(*, truecolor: bool = True, console: Console | None = None) -> Theme:
    return Theme(console if console is not None else make_console(truecolor=truecolor))


class FakeCtypes:
    """最小 ctypes 替身：模拟 Windows 控制台 mode 读写。"""

    def __init__(self, mode_value: int, handle: int = 4242):
        self.mode = mode_value
        self.handle = handle
        self.set_calls: list[int] = []
        kernel32 = types.SimpleNamespace(
            GetStdHandle=self._get_std_handle,
            GetConsoleMode=self._get_console_mode,
            SetConsoleMode=self._set_console_mode,
        )
        self.c_ulong = _FakeCULong
        self.byref = lambda obj: types.SimpleNamespace(_obj=obj)
        self.windll = types.SimpleNamespace(kernel32=kernel32)

    def _get_std_handle(self, _std_id: int) -> int:
        return self.handle

    def _get_console_mode(self, _handle: int, mode_ref) -> int:
        mode_ref._obj.value = self.mode
        return 1

    def _set_console_mode(self, _handle: int, value: int) -> int:
        self.set_calls.append(value)
        self.mode = value
        return 1


class _FakeCULong:
    def __init__(self):
        self.value = 0


@pytest.fixture
def fake_ctypes(monkeypatch):
    """把 ctypes 换成替身（Console 内部 `import ctypes` 命中它）。"""

    def install(mode_value: int) -> FakeCtypes:
        fake = FakeCtypes(mode_value)
        monkeypatch.setitem(sys.modules, "ctypes", fake)
        return fake

    return install


# ═══════════════════════════════════════════════════════════════
# R1 基础色板与角色引用
# ═══════════════════════════════════════════════════════════════

class TestPalette:
    """spec「基础色板与角色引用」。"""

    def test_default_palette(self):
        """Scenario 默认色板生效：主文字 #FFFFD0、代码块背景 #0F172A。"""
        theme = make_theme()
        assert theme.base("primary").value == ANSI_PRIMARY
        assert theme.base("code_bg").value == ANSI_CODE_BG
        assert theme.hex_table["primary"] == "#FFFFD0"
        assert theme.hex_table["code_bg"] == "#0F172A"
        assert len(theme.hex_table) == 11
        # 角色：默认配方解析（bold accent）
        assert theme.md_h1.value == "\x1b[1m" + ANSI_ACCENT
        # 应用"未定义任何颜色"的配置后等价于默认
        theme.apply_style({})
        assert theme.base("primary").value == ANSI_PRIMARY
        assert theme.base("code_bg").value == ANSI_CODE_BG

    def test_custom_palette_role_reference(self):
        """Scenario 自定义调色板与角色引用：colors.纯白 + base_colors.user → 纯白。"""
        theme = make_theme()
        theme.apply_style({"colors": {"纯白": "#FFFFFF"}, "base_colors": {"user": "纯白"}})
        assert theme.base("user").value == "\x1b[38;2;255;255;255m"
        assert theme.hex_table["纯白"] == "#FFFFFF"
        assert theme.hex_table["user"] == "#FFFFFF"

    def test_reference_order_hex_then_builtin_then_registered(self):
        """解析顺序：调色板直查 hex → 同名色 → 已登记颜色。"""
        theme = make_theme()
        # 同名色引用（内置色名）：跟随色板当前值
        theme.apply_style({"colors": {"accent": "#123456"}, "base_colors": {"link": "accent"}})
        assert theme.base("link").value == "\x1b[38;2;18;52;86m"
        assert theme.hex_table["link"] == "#123456"

    def test_invalid_entries_skipped(self):
        """Scenario 无效项静默跳过：非 # 值、非法 hex、未登记引用、非 dict 分组。"""
        theme = make_theme()
        theme.apply_style({
            "colors": {"坏值": "红色", "坏hex": "#ZZZZZZ", "_注释": "#FFFFFF", "user": "#004455"},
            "base_colors": {"accent": "未登记名字"},
            "markdown": {"heading_h1": "bold accent"},
            "ui": "非 dict",
            "codeblock": None,
        })
        assert "坏值" not in theme.hex_table
        assert "坏hex" not in theme.hex_table
        # 其余配置照常生效
        assert theme.hex_table["user"] == "#004455"
        assert theme.md_h1.value == "\x1b[1m" + ANSI_ACCENT
        # 未登记引用被跳过：accent 保持默认
        assert theme.base("accent").value == ANSI_ACCENT

    def test_codeblock_background(self):
        """「代码块.背景」单独处理：按引用命中后以背景色语义生成。"""
        theme = make_theme()
        theme.apply_style({"colors": {"纯黑": "#000000"}, "codeblock": {"background": "纯黑"}})
        assert theme.base("code_bg").value == "\x1b[48;2;0;0;0m"
        assert theme.hex_table["code_bg"] == "#000000"
        # 引用内置名：按背景语义重新生成
        theme.apply_style({"codeblock": {"background": "accent"}})
        assert theme.base("code_bg").value == "\x1b[48;2;94;234;212m"
        # hex 直写不经此分组（与「基础色」的引用语义一致，现状保持）
        theme.apply_style({"codeblock": {"background": "#112233"}})
        assert theme.base("code_bg").value == ANSI_CODE_BG


# ═══════════════════════════════════════════════════════════════
# R2 派生角色与配方解析
# ═══════════════════════════════════════════════════════════════

class TestRecipe:
    """spec「派生角色与配方解析」。"""

    def test_combined_recipe(self):
        """Scenario 组合配方：bold accent → 粗体 + 强调色。"""
        theme = make_theme()
        theme.apply_style({"markdown": {"heading_h1": "bold accent"}})
        assert theme.md_h1.value == "\x1b[1m" + ANSI_ACCENT

    def test_multi_style_recipe(self):
        """Scenario 多样式与颜色组合：italic dim secondary。"""
        theme = make_theme()
        theme.apply_style({"markdown": {"italic": "italic dim secondary"}})
        assert theme.md_italic.value == "\x1b[3m\x1b[2m" + ANSI_SECONDARY

    def test_unknown_token_ignored(self):
        """Scenario 未知 token 忽略（如拼错色名），其余 token 正常生效。"""
        theme = make_theme()
        theme.apply_style({"markdown": {"heading_h1": "bold 拼错的色名"}})
        assert theme.md_h1.value == "\x1b[1m"

    def test_default_recipe_follows_base_color(self):
        """Scenario 默认配方跟随基础色：只改 accent 的 hex，用 accent 的角色随之更新。"""
        theme = make_theme()
        theme.apply_style({"colors": {"accent": "#FF0000"}})
        red = "\x1b[38;2;255;0;0m"
        assert theme.base("accent").value == red
        assert theme.md_h1.value == "\x1b[1m" + red       # 默认 bold accent
        assert theme.diff_header.value == "\x1b[1m" + red
        assert theme.cmd_highlight.value == red           # 默认 accent
        assert theme.md_h1.value != theme.md_code.value

    def test_empty_recipe_no_style(self):
        """空值/全空白解析为无样式。"""
        theme = make_theme()
        theme.apply_style({"ui": {"header": "", "separator": "   "}})
        assert theme.ui_header.value == ""
        assert theme.ui_separator.value == ""

    def test_bg_named_uses_foreground_quirk(self):
        """兼容怪癖：`bg:名字` 取该色前景值（现状保持）。"""
        theme = make_theme()
        assert theme.parse_recipe("bg:primary") == ANSI_PRIMARY
        assert theme.parse_recipe("bg:#112233") == "\x1b[48;2;17;34;51m"

    def test_derived_roles_table_complete(self):
        """派生角色按分组与配置键齐备（43 个，键与属性均可访问）。"""
        theme = make_theme()
        assert len(DERIVED_DEFS) == 43
        for attr, key, _recipe in DERIVED_DEFS:
            assert isinstance(getattr(theme, attr), Color)
            assert theme.role(key) is getattr(theme, attr)


# ═══════════════════════════════════════════════════════════════
# R4 全局输出开关
# ═══════════════════════════════════════════════════════════════

class TestPlainQuiet:
    """spec「全局输出开关」。"""

    def test_plain_color_empty_str(self):
        """Scenario headless 纯文本：颜色求值为空串（结构保留）。"""
        theme = make_theme()
        theme.apply_style({"colors": {"accent": "#FF0000"}})
        console = theme._console
        console.set_plain(True)
        assert str(theme.md_h1) == ""
        assert str(theme.base("accent")) == ""
        assert str(theme.rst) == ""
        assert f"{theme.md_h1}文本{theme.rst}" == "文本"
        assert theme.md_h1.value == "\x1b[1m\x1b[38;2;255;0;0m"  # 原文仍在
        console.set_plain(False)
        assert str(theme.md_h1) == "\x1b[1m\x1b[38;2;255;0;0m"

    def test_plain_write_no_cr(self):
        """Scenario headless 纯文本 / 纯文本模式无行首回车。"""
        out = io.StringIO()
        console = make_console(stdout=out)
        console.set_plain(True)
        console.write("第一行\n")
        console.write("第二行\n")
        assert out.getvalue() == "第一行\n第二行\n"
        assert "\x1b[" not in out.getvalue()

    def test_quiet_tools_switch(self):
        """Scenario 静默工具日志 / `-l` 显示全量日志（开关查询语义）。"""
        console = make_console()
        assert console.is_quiet_tools() is False          # 交互模式默认不静默
        console.set_quiet_tools(True)                     # headless 默认静默
        assert console.is_quiet_tools() is True
        console.set_quiet_tools(False)                    # -l 关闭静默
        assert console.is_quiet_tools() is False
        console.set_quiet_tools()
        assert console.is_quiet_tools() is True
        console.set_plain(True)
        assert console.is_plain() is True
        console.set_plain(False)
        assert console.is_plain() is False


# ═══════════════════════════════════════════════════════════════
# R5 终端能力适配
# ═══════════════════════════════════════════════════════════════

class TestTerminal:
    """spec「终端能力适配」。"""

    def test_truecolor_sequence(self):
        """Scenario 真彩终端：24 位色序列。"""
        assert hex_to_ansi("#1A2B3C", truecolor=True) == "\x1b[38;2;26;43;60m"
        assert hex_to_ansi("#1A2B3C", bg=True, truecolor=True) == "\x1b[48;2;26;43;60m"
        theme = make_theme(truecolor=True)
        assert theme.base("accent").value == ANSI_ACCENT

    def test_16color_fallback(self):
        """Scenario 16 色降级：按欧氏距离取最近邻（前景/背景码分别取）。"""
        assert hex_to_ansi("#1A2B3C", truecolor=False) == "\x1b[30m"
        assert hex_to_ansi("#1A2B3C", bg=True, truecolor=False) == "\x1b[40m"
        assert hex_to_ansi("FF0000", truecolor=False) == "\x1b[91m"
        assert hex_to_ansi("#000000", truecolor=False) == "\x1b[30m"
        theme = make_theme(truecolor=False)
        assert theme.base("accent").value == "\x1b[36m"
        assert theme.md_h1.value == "\x1b[1m\x1b[36m"

    def test_no_console_skips_vt_reassert(self):
        """Scenario 重定向到文件：无控制台句柄 → 不重申，ANSI 原样写出。"""
        out = io.StringIO()
        console = make_console(stdout=out, platform="linux")
        assert console._vt_handle is None
        console.write(ANSI_ACCENT + "彩色" + "\x1b[0m" + "\n")
        assert out.getvalue() == "\r" + ANSI_ACCENT + "彩色\x1b[0m\n"

    def test_vt_enabled_on_windows(self, fake_ctypes):
        """Windows 控制台启动时启用 VT（mode 位被写入）。"""
        fake = fake_ctypes(0)
        console = make_console(platform="win32")
        assert console._vt_handle == fake.handle
        assert fake.set_calls == [ENABLE_VIRTUAL_TERMINAL_PROCESSING]

    def test_vt_reassert_after_reset(self, fake_ctypes):
        """Scenario 子进程重置后重申：写出前检测缺位并重新启用。"""
        fake = fake_ctypes(0)
        console = make_console(platform="win32")
        assert len(fake.set_calls) == 1
        fake.mode = 0  # 模拟子进程把 console mode 重置回 legacy
        console.write("x\n")
        assert len(fake.set_calls) == 2
        assert fake.set_calls[-1] & ENABLE_VIRTUAL_TERMINAL_PROCESSING
        # mode 位已在时不再重复设置（仅检测）
        console.write("y\n")
        assert len(fake.set_calls) == 2

    def test_plain_skips_vt_reassert(self, fake_ctypes):
        """纯文本模式不执行终端能力重申。"""
        fake = fake_ctypes(0)
        console = make_console(platform="win32")
        console.set_plain(True)
        fake.mode = 0
        console.write("x\n")
        assert len(fake.set_calls) == 1  # 仅构造时的一次

    def test_no_handle_skips_vt(self, fake_ctypes):
        """无有效控制台句柄（管道/重定向）→ 句柄不缓存。"""
        fake = fake_ctypes(0)
        fake.handle = 0
        console = make_console(platform="win32")
        assert console._vt_handle is None
        assert fake.set_calls == []

    @pytest.mark.parametrize("env,value,expected", [
        ({"COLORTERM": "truecolor"}, None, True),
        ({"COLORTERM": "24bit"}, None, True),
        ({"COLORTERM": "TrueColor"}, None, True),
        ({"WT_SESSION": "1"}, None, True),
        ({"ConEmuANSI": "ON"}, None, True),
        ({"ConEmuANSI": "on"}, None, False),
        ({}, None, False),
    ])
    def test_truecolor_detection_env(self, monkeypatch, env, value, expected):
        """真彩检测链：环境变量分支（其余环境变量清空）。"""
        for key in ("COLORTERM", "WT_SESSION", "ConEmuANSI"):
            monkeypatch.delenv(key, raising=False)
        for key, val in env.items():
            monkeypatch.setenv(key, val)
        console = make_console(truecolor=None, platform="linux")
        assert console.truecolor is expected

    def test_truecolor_detection_windows_console_mode(self, monkeypatch, fake_ctypes):
        """真彩检测链：Windows 控制台 VT 位（构造时启用 VT → 检测为真彩）。"""
        for key in ("COLORTERM", "WT_SESSION", "ConEmuANSI"):
            monkeypatch.delenv(key, raising=False)
        fake_ctypes(0)
        assert make_console(truecolor=None, platform="win32").truecolor is True
        no_handle = fake_ctypes(0)
        no_handle.handle = 0                      # 无控制台句柄（管道/重定向）
        assert make_console(truecolor=None, platform="win32").truecolor is False


# ═══════════════════════════════════════════════════════════════
# R6 显示开关数据流
# ═══════════════════════════════════════════════════════════════

class TestDisplayState:
    """spec「显示开关数据流」（统计栏渲染归 ui 积木，此处覆盖数据供给）。"""

    def test_defaults(self):
        """默认值：费用/余额/占比否，最大输出 128000，上下文窗口 1000000。"""
        display = DisplayState()
        assert display.show_cost is False
        assert display.show_balance is False
        assert display.max_tokens == 128000
        assert display.show_ratio is False
        assert display.context_window == 1000000

    def test_apply_config_all_switches(self):
        """Scenario 费用段开关/余额为 0/占比缺数据（数据面）：装配写入五项参数。"""
        display = DisplayState()
        display.apply_config({
            "show_cost": True, "show_balance": True, "max_output_tokens": 4096,
            "show_ratio": True, "context_window": 200000,
        })
        assert display.show_cost is True
        assert display.show_balance is True
        assert display.max_tokens == 4096
        assert display.show_ratio is True
        assert display.context_window == 200000
        # 重放空配置 → 恢复默认（运行期不热更新，重放即重置）
        display.apply_config({})
        assert (display.show_cost, display.show_balance, display.max_tokens,
                display.show_ratio, display.context_window) == (
            False, False, 128000, False, 1000000)

    def test_apply_config_missing_keys_use_defaults(self):
        """标准配置缺省键取默认值（模型回填 max_output_tokens 由 config 归一化完成）。"""
        display = DisplayState()
        display.apply_config({"show_cost": True})
        assert (display.show_cost, display.show_balance, display.max_tokens,
                display.show_ratio, display.context_window) == (
            True, False, 128000, False, 1000000)

    def test_chinese_keys_not_consumed(self):
        """中英键裁决归 config：直接喂中文键不生效（output 纯消费标准格式）。"""
        display = DisplayState()
        display.apply_config({"显示费用": True})
        assert display.show_cost is False
        theme = make_theme()
        theme.apply_style({"颜色": {"accent": "#FF0000"}})
        assert theme.base("accent").value == ANSI_ACCENT

    def test_string_bool_quirk(self):
        """兼容怪癖：字符串布尔按真值处理（"false" → True）。"""
        display = DisplayState()
        display.apply_config({"show_cost": "false", "show_balance": ""})
        assert display.show_cost is True
        assert display.show_balance is False


# ═══════════════════════════════════════════════════════════════
# R7 主题应用时机与全局生效
# ═══════════════════════════════════════════════════════════════

class TestThemeApply:
    """spec「主题应用时机与全局生效」。"""

    def test_apply_once_takes_effect(self):
        """Scenario 启动时应用一次：应用后持引用的模块立即生效，运行期不重载。"""
        theme = make_theme()
        holder = theme.md_h1                       # 模拟消费方持有引用
        cfg = {"colors": {"accent": "#FF0000"}}
        theme.apply_style(cfg)
        assert str(holder) == "\x1b[1m\x1b[38;2;255;0;0m"
        cfg["colors"]["accent"] = "#00FF00"        # 运行期改配置对象不影响本会话
        assert str(holder) == "\x1b[1m\x1b[38;2;255;0;0m"

    def test_repeat_apply_idempotent(self):
        """Scenario 重复应用幂等：第二次 = 默认色板 + 第二份配置，第一次不残留。"""
        theme = make_theme()
        theme.apply_style({
            "colors": {"accent": "#FF0000", "自定义色": "#123456"},
            "markdown": {"heading_h1": "bold success"},
            "prompt": {"symbol": "bold #123456"},
        })
        assert theme.md_h1.value == "\x1b[1m\x1b[38;2;52;211;153m"
        theme.apply_style({})                      # 空配置 = 完全恢复默认
        assert theme.md_h1.value == "\x1b[1m" + ANSI_ACCENT
        assert theme.ptk_prompt_symbol == "bold #00ff00"
        assert "自定义色" not in theme.hex_table
        assert theme.hex_table == {name: hex_value for name, hex_value, _b in BASE_DEFS}
        # 同一份配置重复应用结果一致
        theme.apply_style({"colors": {"accent": "#FF0000"}})
        once = theme.md_h1.value
        theme.apply_style({"colors": {"accent": "#FF0000"}})
        assert theme.md_h1.value == once

    def test_prompt_follows_user_color(self):
        """Scenario 提示符跟随用户色：未配置「提示符.文字」时用「颜色.用户」hex。"""
        theme = make_theme()
        theme.apply_style({"colors": {"user": "#FF0000"}})
        assert theme.ptk_prompt_text == "#FF0000"
        # 显式配置优先（中文「强调」→ emphasis 的替换归 config，此处直接喂替换产物）
        theme.apply_style({"colors": {"user": "#FF0000"}, "prompt": {"text": "bold emphasis"}})
        assert theme.ptk_prompt_text == "bold #FB923C"
        # 默认色板：跟随默认用户色 #FFFFFF
        theme.apply_style({})
        assert theme.ptk_prompt_text == "#FFFFFF"
        # 提示符符号/自定义样式
        theme.apply_style({"prompt": {"symbol": "bold #00ff00", "custom": "#FFFFD0"}})
        assert (theme.ptk_prompt_symbol, theme.ptk_prompt_custom) == ("bold #00ff00", "#FFFFD0")

    def test_resolve_ptk_style(self):
        """ptk 样式解析：样式词保留、色名转 hex、未知 token 原样透传。"""
        theme = make_theme()
        assert theme.resolve_ptk_style("bold primary") == "bold #FFFFD0"
        assert theme.resolve_ptk_style("accent") == "#5EEAD4"
        assert theme.resolve_ptk_style("纯白") == "纯白"
        assert theme.resolve_ptk_style("") == ""

    def test_package_level_apply_style(self):
        """包级入口 `apply_style(theme, ui_config)`：转发实例方法（旧消费名可导入）。"""
        theme = make_theme()
        apply_style(theme, {"colors": {"accent": "#FF0000"}})
        assert theme.md_h1.value == "\x1b[1m\x1b[38;2;255;0;0m"


# ═══════════════════════════════════════════════════════════════
# R8 输出原语与行控制
# ═══════════════════════════════════════════════════════════════

class TestWritePrimitives:
    """spec「输出原语与行控制」。"""

    def test_blocking_write_has_leading_cr(self):
        """Scenario 阻塞写出的行首回车：非纯文本模式前置 \\r，且立即刷新。"""
        out = io.StringIO()
        console = make_console(stdout=out)
        console.write("内容\n")
        assert out.getvalue() == "\r内容\n"

    def test_try_write_fails_when_locked(self):
        """Scenario 非阻塞写出失败：锁被占用时放弃、返回 False、不输出。"""
        out = io.StringIO()
        console = make_console(stdout=out)
        assert console.lock.acquire(blocking=False)
        try:
            assert console.try_write("帧") is False
            assert out.getvalue() == ""
        finally:
            console.lock.release()
        assert console.try_write("帧") is True
        assert out.getvalue() == "帧"              # try_write 不补行首回车

    def test_write_provides_no_erase_sequence(self):
        """Scenario 动画原地刷新：原语不注入擦除序列（由调用方自行实现）。"""
        out = io.StringIO()
        console = make_console(stdout=out)
        console.try_write("\r\x1b[K帧1")
        assert out.getvalue() == "\r\x1b[K帧1"
        assert "擦除" not in out.getvalue() and out.getvalue().count("\x1b[K") == 1

    def test_write_follows_sys_stdout(self, capsys):
        """未注入 stdout 时动态跟随 `sys.stdout`（与旧实现一致）。"""
        console = Console(truecolor=True, platform="linux")
        console.write("hello\n")
        assert capsys.readouterr().out == "\rhello\n"

    def test_write_is_serialized_by_lock(self):
        """阻塞写出串行化：锁被占用时其它线程写出等待。"""
        out = io.StringIO()
        console = make_console(stdout=out)
        done = threading.Event()
        console.lock.acquire()
        try:
            thread = threading.Thread(target=lambda: (console.write("x"), done.set()))
            thread.start()
            assert done.wait(0.2) is False
        finally:
            console.lock.release()
        assert done.wait(2.0) is True
        thread.join()
        assert out.getvalue() == "\rx"


# ═══════════════════════════════════════════════════════════════
# R9 兼容性怪癖保持
# ═══════════════════════════════════════════════════════════════

class TestQuirks:
    """spec「兼容性怪癖保持」。"""

    def test_token_after_replacement_ignored(self):
        """Scenario 强调色替换怪癖：替换产物 `bold emphasis色` 中 `emphasis色` 为无效 token 被忽略。

        中文「强调色」→ 替换（且留下 `emphasis色` 形态）归 config 归一化；
        输出面契约是替换产物中的无效 token 静默忽略、仅剩粗体效果。
        """
        theme = make_theme()
        theme.apply_style({"markdown": {"heading_h1": "bold emphasis色"}})
        assert theme.md_h1.value == "\x1b[1m"
        assert theme.md_h1.value == "\x1b[1m" + ""  # 无颜色残留

    def test_debug_repr_ignores_plain(self):
        """Scenario 调试表示不受纯文本影响：repr 恒为 ANSI 原文。"""
        theme = make_theme()
        console = theme._console
        console.set_plain(True)
        assert str(theme.md_h1) == ""
        assert repr(theme.md_h1) == "\x1b[1m" + ANSI_ACCENT
        assert f"{theme.md_h1!r}" == "\x1b[1m" + ANSI_ACCENT

    def test_metadata_key_skipped(self):
        """Scenario 元数据键跳过：「颜色」分组的 `_` 前缀键不登记、不报错。"""
        theme = make_theme()
        theme.apply_style({"colors": {"_注释": "#FFFFFF", "_说明": 123, "好色": "#123456"}})
        assert "_注释" not in theme.hex_table
        assert "_说明" not in theme.hex_table
        assert theme.hex_table["好色"] == "#123456"

    def test_base_color_hex_value_skipped(self):
        """兼容怪癖：「基础色」分组直接写十六进制值的项被跳过。"""
        theme = make_theme()
        theme.apply_style({"base_colors": {"accent": "#FF0000"}})
        assert theme.base("accent").value == ANSI_ACCENT

    def test_ptk_hex_backtrack_failure_keeps_token(self):
        """兼容怪癖：hex 回推失败（16 色降级形态）保留原名文本。"""
        theme = make_theme(truecolor=False)
        theme._palette["临时色"] = Color("\x1b[36m", theme._console)  # 无 hex 同步
        assert theme.resolve_ptk_style("临时色") == "临时色"
        theme._palette["临时色"]._set("\x1b[38;2;18;52;86m")
        assert theme.resolve_ptk_style("临时色") == "#123456"

    def test_malformed_config_never_raises(self):
        """畸形颜色配置（非法 hex、类型不符、非 dict 分组）静默容错。"""
        theme = make_theme()
        theme.apply_style({
            "colors": {"坏hex": "#GGGGGG", "短hex": "#12", "数字值": 123456, "空值": None},
            "base_colors": {"accent": None, "未登记名": 123},
            "codeblock": {"background": "#ZZ"},
            "markdown": ["不是", "字典"],
            "prompt": {"symbol": None, "text": 5},
            "max_output_tokens": 128000,
        })
        assert theme.base("accent").value == ANSI_ACCENT


# ═══════════════════════════════════════════════════════════════
# 基线对照（v2/tests/baseline/data/output_style.json）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def baseline() -> dict:
    if not BASELINE_FILE.exists():
        pytest.skip("基线文件不存在（跳过快照对照）")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


class TestBaselineOutputStyle:
    """对旧实现基准逐用例比对（T0.1 产物；字节等价即行为等价）。"""

    def test_parse_recipe(self, baseline):
        theme = make_theme()
        for case in baseline["groups"]["output._parse_recipe"]:
            assert theme.parse_recipe(case["input"]["value"]) == case["result"], case["id"]

    def test_hex_to_ansi(self, baseline):
        for case in baseline["groups"]["output._hex_to_ansi"]:
            assert hex_to_ansi(case["input"]["hex"], bg=case["input"]["bg"],
                               truecolor=True) == case["result"], case["id"]

    def test_hex_to_ansi_fallback(self, baseline):
        for case in baseline["groups"]["output._hex_to_ansi(非TrueColor降级)"]:
            assert hex_to_ansi(case["input"]["hex"], bg=case["input"]["bg"],
                               truecolor=False) == case["result"], case["id"]

    def test_resolve_ptk_style(self, baseline):
        theme = make_theme()
        for case in baseline["groups"]["output._resolve_ptk_style"]:
            assert theme.resolve_ptk_style(case["input"]["value"]) == case["result"], case["id"]

    def test_apply_style_default_palette(self, baseline):
        case = baseline["groups"]["output.apply_style(默认色板)"][0]
        expected = case["result"]
        theme = make_theme()
        display = DisplayState()
        theme.apply_style(case["input"])
        display.apply_config(case["input"])
        for name, ansi in expected["tokens"].items():
            assert getattr(theme, name.lower()).value == ansi, name
        assert theme.ptk_prompt_symbol == expected["ptk"]["symbol"]
        assert theme.ptk_prompt_text == expected["ptk"]["text"]
        assert theme.ptk_prompt_custom == expected["ptk"]["custom"]
        assert theme.hex_table == expected["base_hex"]
        assert {
            "show_cost": display.show_cost,
            "show_balance": display.show_balance,
            "max_tokens": display.max_tokens,
            "show_ratio": display.show_ratio,
            "context_window": display.context_window,
        } == expected["display_state"]
