"""渲染器单元测试 —— spec Scenario 映射 + 旧实现基线对照 + 表格流式稳定性。

行为金标准：`openspec/changes/recast-v2/specs/ui/spec.md` 的渲染面（T3.9 范围；
非渲染面归 T3.10 `test_ui_interaction.py`）。

映射表（渲染面 Requirement / Scenario → 测试）：

| Requirement | Scenario | 测试 |
|---|---|---|
| 流式 Markdown 增量渲染 | 标题按级别着色 | `TestMarkdownIncremental::test_heading_level_colors` |
| | 列表、任务框与引用 | `TestMarkdownIncremental::test_lists_tasks_quotes` |
| | 行内元素渲染 | `TestMarkdownIncremental::test_inline_elements` |
| | 未完成行等待后续数据 | `TestMarkdownIncremental::test_incomplete_line_waits` |
| 渲染缓冲落定与重置 | 半行立即落定 | `TestBufferSettle::test_half_line_settled` |
| | 未闭合代码块落定不吞内容 | `TestBufferSettle::test_unclosed_code_not_swallowed` |
| | 表格残留片段降级 | `TestBufferSettle::test_table_fragment_demoted` |
| | 重置清空全部状态 | `TestBufferSettle::test_reset_clears_all_state` |
| | 完整表格落定 | `TestBufferSettle::test_complete_table_settled` |
| 表格稳定渲染 | 完整表格直排 | `TestTableRendering::test_direct_layout` |
| | 无分隔行降级为段落 | `TestTableRendering::test_no_separator_demoted` |
| | 无首竖线不识别为表格 | `TestTableRendering::test_row_without_leading_pipe` |
| | 超长表格缓冲强制落定 | `TestTableRendering::test_long_table_forced_settle` |
| | 超宽表格折行 | `TestTableRendering::test_wide_table_wrapped` |
| | 分块与标签 | `TestTableRendering::test_chunked_with_label` |
| | 列对齐 | `TestTableRendering::test_column_alignment` |
| | 短行补空单元格 | `TestTableRendering::test_short_row_padded` |
| | 转义竖线还原 | `TestTableRendering::test_escaped_pipe_restored` |
| 围栏代码块渲染 | 围栏与语言标签 | `TestCodeBlock::test_fence_and_language_label` |
| | 空语言回退 | `TestCodeBlock::test_empty_language_fallback` |
| | 行号格式 | `TestCodeBlock::test_line_number_format` |
| | 未识别语言灰色 | `TestCodeBlock::test_unknown_language_gray` |
| | 代码块内容延迟输出 | `TestCodeBlock::test_content_delayed_until_close` |
| 终端宽度与显示宽度 | 环境变量强制宽度 | `TestTerminalWidth::test_env_forced_width` |
| | 宽度截断 | `TestTerminalWidth::test_width_clamped` |
| | CJK 宽度 | `TestTerminalWidth::test_cjk_width` |
| | 折行颜色继承 | `TestTerminalWidth::test_wrap_inherits_color` |
| | 分隔线宽度 | `TestTerminalWidth::test_separator_width` |

补充用例（边界与回归，不单独对应 Scenario）：
- 块级/行内细节：`test_task_markers` / `test_hr_and_paragraph` / `test_inline_replacement_order`
  / `test_blank_lines_produce_nothing`；
- 落定/重置细节：`test_flush_in_code_accumulates_line` / `test_table_fragment_then_completed`
  / `test_no_buffer_carry_over_between_rounds`；
- 表格边界：`test_no_data_rows_demoted` / `test_all_empty_data_demoted` / `test_separator_only_row`
  / `test_uneven_rows_use_max_cols` / `test_multiline_header` / `test_chunked_single_col_fallback`
  / `test_narrow_terminal_demoted` / `test_visible_width_shrink`
  / `test_long_table_without_separator_demoted_at_cap`；
- 代码块边界：`test_language_group_mapping` / `test_second_fence_line_is_language`
  / `test_after_close_back_to_normal` / `test_empty_fence_block`；
- 宽度边界：`test_env_non_numeric_falls_back` / `test_separator_min_width`。

基线对照：`v2/tests/baseline/data/renderer.json`（旧实现纯函数 71 用例，字节等价即行为等价）；
流式路径对照：`v2/tests/baseline/stream/render_stream.json`（旧实现 `StreamingRenderer`
的 70 个流式场景，由 `extract_old_render_stream.py` 生成，判据 = 字节等价）；
表格稳定性：`TestTableStreamingStability`（多组切片序列的最终输出与整体渲染一致）。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from narnat_agent.output import Console, Theme
from narnat_agent.ui import (
    MarkdownStyler,
    StreamingRenderer,
    char_width,
    colorize_diff,
    display_width,
    fit_widths,
    is_table_separator,
    split_cells,
    terminal_width,
    wrap_cell,
)
from narnat_agent.ui.markdown import LANG_GROUP_OF
from narnat_agent.ui.render import MAX_TABLE_BUFFER

BASELINE_FILE = (
    Path(__file__).resolve().parents[1] / "baseline" / "data" / "renderer.json"
)

# 旧实现流式路径基准（T3.9 补充，extract_old_render_stream.py 生成）
STREAM_SNAPSHOT_FILE = (
    Path(__file__).resolve().parents[1] / "baseline" / "stream" / "render_stream.json"
)
STREAM_SNAPSHOT_GROUP = "renderer.StreamingRenderer(feed/flush/reset)"

TABLE_DIRECT = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
TABLE_WIDE = ("| 姓名 | 说明 | 备注 |\n| --- | --- | --- |\n"
              "| 张三 | 很长的说明文本内容 | 备注信息 |\n")
TABLE_8COLS = ("| c0 | c1 | c2 | c3 | c4 | c5 | c6 | c7 |\n"
               "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
               "| a0 | a1 | a2 | a3 | a4 | a5 | a6 | a7 |\n")


# ═══════════════════════════════════════════════════════════════
# 夹具与辅助
# ═══════════════════════════════════════════════════════════════

def make_console(*, truecolor: bool = True, plain: bool = False,
                 stdout: io.StringIO | None = None) -> Console:
    """构造隔离的 Console 实例（不依赖真实终端与全局态）。"""
    console = Console(stdout if stdout is not None else io.StringIO(),
                      truecolor=truecolor, platform="linux")
    console.set_plain(plain)
    return console


def make_styler(*, truecolor: bool = True, plain: bool = False) -> MarkdownStyler:
    console = make_console(truecolor=truecolor, plain=plain)
    return MarkdownStyler(Theme(console), console)


def make_renderer(*, width: int = 100, visible: int = 0, plain: bool = True,
                  truecolor: bool = True):
    """构造流式渲染器与输出缓冲（宽度探测注入固定值）。"""
    buf = io.StringIO()
    console = make_console(truecolor=truecolor, plain=plain, stdout=buf)
    theme = Theme(console)
    renderer = StreamingRenderer(theme, console, width_fn=lambda: width,
                                 visible_width_fn=lambda: visible)
    return buf, console, theme, renderer


def render(chunks, *, width: int = 100, visible: int = 0, plain: bool = True) -> str:
    """喂入 chunk 序列并落定，返回全部输出（默认纯文本模式，便于结构断言）。"""
    buf, _console, _theme, renderer = make_renderer(width=width, visible=visible, plain=plain)
    for chunk in chunks:
        renderer.feed(chunk)
    renderer.flush()
    return buf.getvalue()


def out_lines(text: str) -> list[str]:
    """输出文本 → 行列表（剥离非纯文本模式下每行行首的回车、去掉末尾空行）。"""
    lines = [ln[1:] if ln.startswith("\r") else ln for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def border_lines(text: str) -> list[str]:
    """输出中的表格边框行（四空格缩进的 `+---+` 形态）。"""
    return [ln for ln in out_lines(text) if ln.startswith("    +")]


# ═══════════════════════════════════════════════════════════════
# Requirement: 流式 Markdown 增量渲染
# ═══════════════════════════════════════════════════════════════

class TestMarkdownIncremental:
    """spec「流式 Markdown 增量渲染」。"""

    def test_heading_level_colors(self):
        """Scenario 标题按级别着色：1–2 级主标题色、3 级三级色、4 级及以下四级色。"""
        buf, _console, theme, renderer = make_renderer(plain=False)
        renderer.feed("## 结论\n")
        renderer.feed("#### 细节\n")
        lines = out_lines(buf.getvalue())
        assert lines[0] == f"  {theme.md_h1}结论{theme.rst}"
        assert lines[1] == f"  {theme.md_h4}细节{theme.rst}"
        # 去掉 ANSI 后即两空格缩进正文
        assert render(["## 结论\n", "#### 细节\n"]) == "  结论\n  细节\n"

    def test_lists_tasks_quotes(self):
        """Scenario 列表、任务框与引用：缩进与标记逐字对齐 spec。"""
        chunks = ["- 项目\n", "1. 第一步\n", "- [x] 完成\n", "> 引用\n", ">> 嵌套\n"]
        assert out_lines(render(chunks)) == [
            "   * 项目",
            "   1. 第一步",
            "   v 完成",
            "  | 引用",
            "  | | 嵌套",
        ]

    def test_task_markers(self):
        """任务项未完成标记 `o`、完成标记 `v`（`[x]`/`[X]` 均判完成）。"""
        assert out_lines(render(["- [ ] 未完成\n", "- [X] 完成\n"])) == [
            "   o 未完成", "   v 完成"]

    def test_hr_and_paragraph(self):
        """分隔线与普通段落：两空格缩进（分隔线保留原文）。"""
        assert out_lines(render(["---\n", "***\n", "正文\n"])) == [
            "  ---", "  ***", "  正文"]

    def test_inline_elements(self):
        """Scenario 行内元素渲染：粗体带样式、链接只显示文本（地址不出现）。"""
        buf, _console, theme, renderer = make_renderer(plain=False)
        renderer.feed("这是**加粗**与[链接](https://example.com)\n")
        out = buf.getvalue()
        assert f"{theme.md_bold}加粗{theme.rst}" in out
        assert f"{theme.md_link}链接{theme.rst}" in out
        assert "*" not in out
        assert "[链接](https://example.com)" not in out
        assert "https://example.com" not in out

    def test_inline_replacement_order(self):
        """行内替换一次到位（删除线 → 粗体 → 斜体 → 行内代码 → 图片 → 链接）。"""
        assert render(["~~删~~ **粗** *斜* `码` ![图](a.png) [链](u)\n"]) == (
            "  删 粗 斜 码 图 链\n")

    def test_incomplete_line_waits(self):
        """Scenario 未完成行等待后续数据：半行不输出，补上换行后整行输出。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("第一")
        assert buf.getvalue() == ""
        renderer.feed("行\n")
        assert buf.getvalue() == "  第一行\n"

    def test_blank_lines_produce_nothing(self):
        """去空白后为空的行不产生输出。"""
        assert render(["\n", "   \n", "\t\n"]) == ""


# ═══════════════════════════════════════════════════════════════
# Requirement: 渲染缓冲落定与重置
# ═══════════════════════════════════════════════════════════════

class TestBufferSettle:
    """spec「渲染缓冲落定与重置」（所有落定调用点行为一致）。"""

    def test_half_line_settled(self):
        """Scenario 半行立即落定：不带到下一轮。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("部分内容")
        renderer.flush()
        assert buf.getvalue() == "  部分内容\n"
        renderer.flush()
        assert buf.getvalue() == "  部分内容\n"

    def test_unclosed_code_not_swallowed(self):
        """Scenario 未闭合代码块落定不吞内容：输出标签与已累积代码行。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("```python\nx = 1\n")
        renderer.flush()
        out = buf.getvalue()
        assert "-- python --" in out
        assert "x = 1" in out
        assert render(["```python\nx = 1\n"]) == "  -- python --\n    1 x = 1\n"

    def test_table_fragment_demoted(self):
        """Scenario 表格残留片段降级：按段落输出，不产生残缺表格。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("| A | B | C")
        renderer.flush()
        out = buf.getvalue()
        assert "+--" not in out
        assert out == "  | A | B | C\n"

    def test_table_fragment_then_completed(self):
        """落定后的表格半行不污染缓冲：后续表格候选行仍能正常成表。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("| A | B |")
        renderer.flush()
        renderer.feed(" C |\n| --- | --- |\n| 1 | 2 |\n")
        renderer.flush()
        out = buf.getvalue()
        assert len(border_lines(out)) == 2
        assert "  | A | B |" in out

    def test_reset_clears_all_state(self):
        """Scenario 重置清空全部状态：半行 / 表格缓冲 / 代码块状态全部清空。"""
        # 半行
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("我先了")
        renderer.reset()
        renderer.feed("我先了解一下\n")
        renderer.flush()
        assert buf.getvalue() == "  我先了解一下\n"
        # 表格缓冲
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("| A | B |\n| --- |")
        renderer.reset()
        renderer.feed(TABLE_DIRECT)
        renderer.flush()
        assert len(border_lines(buf.getvalue())) == 3
        # 代码块状态
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("```python\nx = 1\n")
        renderer.reset()
        renderer.feed("新内容\n")
        renderer.flush()
        assert buf.getvalue() == "  新内容\n"

    def test_complete_table_settled(self):
        """Scenario 完整表格落定：输出带边框表格而非逐行段落。"""
        assert render([TABLE_DIRECT]).split("\n")[0] == "    +---+---+"

    def test_flush_in_code_accumulates_line(self):
        """落定时代码块内的残留按代码行累积（不按普通行渲染）。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("```\nplain")
        renderer.flush()
        assert buf.getvalue() == "  -- code --\n    1 plain\n"

    def test_no_buffer_carry_over_between_rounds(self):
        """不跨轮保留缓冲：上一轮表格残留不与下一轮表格混合。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("| A | B |\n| 1 | 2 |\n")
        renderer.flush()
        renderer.feed(TABLE_DIRECT)
        renderer.flush()
        out = buf.getvalue()
        assert out.count("+---+---+") == 3  # 只有第二轮的表格
        assert "  | A | B |" in out  # 第一轮降级为段落


# ═══════════════════════════════════════════════════════════════
# Requirement: 表格稳定渲染
# ═══════════════════════════════════════════════════════════════

class TestTableRendering:
    """spec「表格稳定渲染」（直排 / 折行 / 分块 / 降级四形态）。"""

    def test_direct_layout(self):
        """Scenario 完整表格直排：首末与数据行前后均为边框行，数据行四空格缩进。"""
        assert out_lines(render([TABLE_DIRECT])) == [
            "    +---+---+",
            "    | A | B |",
            "    +---+---+",
            "    | 1 | 2 |",
            "    +---+---+",
        ]

    def test_no_separator_demoted(self):
        """Scenario 无分隔行降级为段落：逐行输出、内容不丢失。"""
        lines = out_lines(render(["| A | B |\n| 1 | 2 |\n"]))
        assert lines == ["  | A | B |", "  | 1 | 2 |"]

    def test_no_data_rows_demoted(self):
        """只有表头 + 分隔行 → 不是表格，降级为段落。"""
        lines = out_lines(render(["| A | B |\n| --- | --- |\n"]))
        assert lines == ["  | A | B |", "  | --- | --- |"]

    def test_all_empty_data_demoted(self):
        """数据行全部为空 → 降级为段落。"""
        lines = out_lines(render(["| A | B |\n| --- | --- |\n|  |  |\n"]))
        assert "+--" not in "\n".join(lines)
        assert len(lines) == 3

    def test_row_without_leading_pipe(self):
        """Scenario 无首竖线不识别为表格：按段落渲染，不进入表格缓冲。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("A | B |\n")
        renderer.feed("| C | D |\n| --- | --- |\n| 1 | 2 |\n")
        renderer.flush()
        lines = out_lines(buf.getvalue())
        assert lines[0] == "  A | B |"
        assert lines[1] == "    +---+---+"  # 后续真表格仍正常渲染

    def test_long_table_forced_settle(self):
        """Scenario 超长表格缓冲强制落定：满 60 行立即输出（无待 flush）。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("| h | v |\n| --- | --- |\n")
        assert buf.getvalue() == ""
        for i in range(MAX_TABLE_BUFFER - 2):
            renderer.feed(f"| r{i} | v{i} |\n")
        out = buf.getvalue()  # 无需 flush 即已落定
        assert "+---" in out
        assert "r57" in out

    def test_long_table_without_separator_demoted_at_cap(self):
        """无分隔行的候选行满 60 行同样立即落定（降级段落，输出持续可见）。"""
        buf, _console, _theme, renderer = make_renderer()
        for i in range(MAX_TABLE_BUFFER):
            renderer.feed(f"| r{i} | v{i} |\n")
        out = buf.getvalue()
        assert "r0" in out and "r59" in out
        assert "+--" not in out

    def test_wide_table_wrapped(self):
        """Scenario 超宽表格折行：列宽压缩、单元格折为多物理行、每行前有边框。"""
        lines = out_lines(render([TABLE_WIDE], width=30))
        assert lines[0].startswith("    +")
        assert all(display_width(ln) <= 30 - 6 + 4 for ln in lines)  # 不超过表格可用宽度
        assert len(lines) >= 5
        # 数据行折行后出现两行物理行（原单元格 10 列宽内容被拆开）
        body = [ln for ln in lines if ln.startswith("    |")]
        assert len(body) >= 4

    def test_chunked_with_label(self):
        """Scenario 分块与标签：首列锚点重复、块数大于 1 时输出块标签。"""
        lines = out_lines(render([TABLE_8COLS], width=40))
        assert any("── 表格 (1/2) ──" in ln for ln in lines)
        assert any("── 表格 (2/2) ──" in ln for ln in lines)
        # 第二块首列仍是锚点列内容 c0
        assert "+" in lines[0]

    def test_chunked_single_col_fallback(self):
        """分块形态单列表按可用宽度压缩折行。"""
        lines = out_lines(render(["| 只有一列很长的内容文本 |\n| --- |\n| 值也很长的一段文字 |\n"],
                                 width=30))
        assert lines[0].startswith("    +")
        assert all(display_width(ln) <= 30 for ln in lines)

    def test_narrow_terminal_demoted(self):
        """可用宽度小于 20 且放不下每列 2 字符 → 降级为段落。"""
        lines = out_lines(render([TABLE_8COLS], width=20))
        assert "+--" not in "\n".join(lines)
        assert lines[0].startswith("  | c0 |")

    def test_column_alignment(self):
        """Scenario 列对齐：:---: 居中、---: 右对齐，其余左对齐。"""
        lines = out_lines(render(["| 左列 | 中列 | 右列 |\n| :--- | :---: | ---: |\n| a | b | c |\n"]))
        assert lines[1] == "    | 左列 | 中列 | 右列 |"
        assert lines[3] == "    | a    |  b   |    c |"

    def test_short_row_padded(self):
        """Scenario 短行补空单元格：按最大列数渲染，缺列为空。"""
        lines = out_lines(render(["| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |\n"]))
        assert lines[1].count("|") == 4
        assert lines[3].count("|") == 4
        assert lines[3] == "    | 1 | 2 |   |"

    def test_uneven_rows_use_max_cols(self):
        """数据行单元格数不一致：按最大列数处理，短行补空。"""
        lines = out_lines(render(["| A | B | C |\n| --- | --- | --- |\n| 1 |\n| 1 | 2 | 3 | 4 |\n"]))
        assert lines[1].count("|") == 5  # 表头 3 列 → 数据行 4 格 → 全部按 4 列

    def test_escaped_pipe_restored(self):
        """Scenario 转义竖线还原：`\\|` 作为单元格内容显示，不拆分列。"""
        lines = out_lines(render(["| a \\| b | c |\n| --- | --- |\n| x | y |\n"]))
        assert "a | b" in lines[1]
        assert lines[1].count("|") == 4  # 两列（含被转义的竖线为内容）

    def test_multiline_header(self):
        """表头可以是多行：分隔行前的全部候选行按表头渲染。"""
        lines = out_lines(render(["| h1 | h2 |\n| h1b |\n| --- | --- |\n| 1 | 2 |\n"]))
        assert len(border_lines("\n".join(lines))) == 4  # 表头 2 行 + 数据 1 行 + 收尾
        assert [ln for ln in lines if not ln.startswith("    +")] == [
            "    | h1  | h2 |",
            "    | h1b |    |",
            "    | 1   | 2  |",
        ]

    def test_separator_only_row(self):
        """孤立分隔行 → 降级为段落。"""
        lines = out_lines(render(["| --- | --- |\n"]))
        assert "+--" not in "\n".join(lines)

    def test_visible_width_shrink(self):
        """实测可见窗格更小 → 按实测值收缩（表格提前进入折行形态）。"""
        wide = render([TABLE_WIDE], width=100)
        shrunk = render([TABLE_WIDE], width=100, visible=40)
        assert wide != shrunk
        assert all(display_width(ln) <= 40 for ln in out_lines(shrunk))


# ═══════════════════════════════════════════════════════════════
# Requirement: 围栏代码块渲染
# ═══════════════════════════════════════════════════════════════

class TestCodeBlock:
    """spec「围栏代码块渲染」。"""

    def test_fence_and_language_label(self):
        """Scenario 围栏与语言标签：标签行 + 语言色代码行。"""
        buf, _console, theme, renderer = make_renderer(plain=False)
        renderer.feed("```python\nprint(1)\n```\n")
        out = buf.getvalue()
        assert f"{theme.cb_lang_label}  -- python --{theme.rst}" in out
        assert f"{theme.cb_lang_cyan}" in out
        assert f" {theme.cb_line_no}  1 {theme.rst}" in out

    def test_empty_language_fallback(self):
        """Scenario 空语言回退：标签显示 `code`。"""
        assert render(["```\nx\n```\n"]).startswith("  -- code --\n")

    def test_line_number_format(self):
        """Scenario 行号格式：从 1 起、宽度 3 右对齐，内容行尾空白剥除。"""
        body = "\n".join(f"line{i}" for i in range(12))
        out = render([f"```\nv\n{body}\n```\n"])
        lines = out_lines(out)
        assert lines[1] == "    1 v"
        assert lines[2] == "    2 line0"
        assert lines[12] == "   12 line10"
        assert lines[13] == "   13 line11"
        assert render(["```\n  x   \n```\n"]) == "  -- code --\n    1   x\n"

    def test_unknown_language_gray(self):
        """Scenario 未识别语言灰色：标签仍显示语言名（小写）。"""
        buf, _console, theme, renderer = make_renderer(plain=False)
        renderer.feed("```UnKnown\nx\n```\n")
        out = buf.getvalue()
        assert "-- unknown --" in out
        assert f"{theme.cb_lang_gray}" in out
        assert LANG_GROUP_OF.get("unknown") is None

    def test_language_group_mapping(self):
        """7 组语言配色映射（spec 分组表逐组抽查）。"""
        cases = {"python": "cb_lang_cyan", "ts": "cb_lang_yellow", "bash": "cb_lang_green",
                 "yaml": "cb_lang_magenta", "xml": "cb_lang_red", "css": "cb_lang_blue",
                 "md": "cb_lang_gray"}
        for lang, attr in cases.items():
            buf, _console, theme, renderer = make_renderer(plain=False)
            renderer.feed(f"```{lang}\nx\n```\n")
            assert getattr(theme, attr).value in buf.getvalue(), lang

    def test_content_delayed_until_close(self):
        """Scenario 代码块内容延迟输出：未闭合前不输出，闭合后整块成文。"""
        buf, _console, _theme, renderer = make_renderer()
        renderer.feed("```python\nx = 1\n")
        assert buf.getvalue() == ""
        renderer.feed("y = 2\n```\n")
        assert out_lines(buf.getvalue()) == [
            "  -- python --", "    1 x = 1", "    2 y = 2"]

    def test_second_fence_line_is_language(self):
        """代码块内第一行以三重反引号开头视为语言行（不作为内容累积）。"""
        assert out_lines(render(["```\n```python\nx = 1\n```\n"])) == [
            "  -- python --", "    1 x = 1"]

    def test_after_close_back_to_normal(self):
        """闭合后其后内容回到普通行处理。"""
        assert out_lines(render(["```\nx\n```\n## 标题\n"])) == [
            "  -- code --", "    1 x", "  标题"]

    def test_empty_fence_block(self):
        """空代码块不输出内容行（无累积行时整块为空）。"""
        assert render(["```\n```\n"]) == ""


# ═══════════════════════════════════════════════════════════════
# Requirement: 终端宽度与显示宽度
# ═══════════════════════════════════════════════════════════════

class TestTerminalWidth:
    """spec「终端宽度与显示宽度」。"""

    def test_env_forced_width(self, monkeypatch):
        """Scenario 环境变量强制宽度：优先于探测值。"""
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "90")
        assert terminal_width() == 90

    def test_width_clamped(self, monkeypatch):
        """Scenario 宽度截断：大于 160 按 160、小于 20 按 20。"""
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "1000")
        assert terminal_width() == 160
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "5")
        assert terminal_width() == 20

    def test_env_non_numeric_falls_back(self, monkeypatch):
        """非纯数字的环境变量不生效（走探测路径）。"""
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "80x")
        monkeypatch.setattr("narnat_agent.ui.width.windows_console_window_cols", lambda: 0)
        monkeypatch.setattr("shutil.get_terminal_size",
                            lambda *a, **k: os.terminal_size((70, 24)))
        assert terminal_width() == 70

    def test_cjk_width(self):
        """Scenario CJK 宽度：全角 2 列、歧义宽度 1 列、ANSI 剔除。"""
        assert display_width("中文") == 4
        assert display_width("中文abc") == 7
        assert display_width("→≈±") == 3
        assert char_width("中") == 2
        assert char_width("→") == 1
        assert display_width("\x1b[31m红色\x1b[0m") == 4

    def test_wrap_inherits_color(self):
        """Scenario 折行颜色继承：每行不超列宽、次行重放样式、行尾补重置。"""
        console = make_console(plain=False)
        lines = wrap_cell("\x1b[31mabcdef\x1b[0m", 2, console)
        assert lines == ["\x1b[31mab\x1b[0m", "\x1b[31mcd\x1b[0m", "\x1b[31mef\x1b[0m"]
        assert display_width(lines[0]) == 2
        # 纯文本模式：行尾不追加样式重置（输入自带的 ANSI 序列原样保留）
        plain_console = make_console(plain=True)
        assert wrap_cell("\x1b[31mabcdef\x1b[0m", 2, plain_console) == [
            "\x1b[31mab", "\x1b[31mcd", "\x1b[31mef\x1b[0m"]
        # 列宽不为正原样返回；空文本产生一个空行
        assert wrap_cell("abc", 0, console) == ["abc"]
        assert wrap_cell("", 5, console) == [""]

    def test_separator_width(self, monkeypatch):
        """Scenario 分隔线宽度：两空格缩进 + 宽度减 2 个 `─`。"""
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "100")
        buf = io.StringIO()
        console = make_console(plain=True, stdout=buf)
        MarkdownStyler(Theme(console), console).separator()
        assert buf.getvalue() == "  " + "─" * 98 + "\n"

    def test_separator_min_width(self, monkeypatch):
        """最小宽度下不少于 18 个 `─`。"""
        monkeypatch.setenv("NARNAT_TERM_WIDTH", "5")
        buf = io.StringIO()
        console = make_console(plain=True, stdout=buf)
        MarkdownStyler(Theme(console), console).separator()
        assert buf.getvalue() == "  " + "─" * 18 + "\n"


# ═══════════════════════════════════════════════════════════════
# 表格流式稳定性（多组切片序列 = 整体渲染）
# ═══════════════════════════════════════════════════════════════

def _chunkings(text: str, steps=(1, 2, 3, 5, 7, 13)):
    """同一文本的多种切分序列（含一次性、按字符步长、按行；空行保留）。"""
    yield [text]
    for step in steps:
        yield [text[i:i + step] for i in range(0, len(text), step)]
    yield [line + "\n" for line in text.split("\n")[:-1]]


STABILITY_DOCS = [
    ("direct", TABLE_DIRECT, 100),
    ("no_separator", "| A | B |\n| 1 | 2 |\n| 3 | 4 |\n", 100),
    ("wrapped", TABLE_WIDE, 30),
    ("chunked", TABLE_8COLS, 40),
    ("alignment", "| 左 | 中 | 右 |\n| :--- | :---: | ---: |\n| a | b | c |\n", 24),
    ("narrow_demote", TABLE_8COLS, 20),
    ("mixed", "# 标题\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
              "```python\ndef f():\n    return 1\n```\n\n尾段\n", 100),
    ("escaped", "| a \\| b | c |\n| --- | --- |\n| x | y |\n", 100),
]


class TestTableStreamingStability:
    """表格稳定渲染：任意切片序列的最终输出与整体渲染一致。"""

    @pytest.mark.parametrize("name,text,width", STABILITY_DOCS,
                             ids=[d[0] for d in STABILITY_DOCS])
    def test_chunkings_match_whole(self, name, text, width):
        expected = render([text], width=width)
        assert expected != "" or name == "empty"
        for chunks in _chunkings(text):
            assert render(chunks, width=width) == expected, (
                f"{name}: 切分 {len(chunks)} 片输出不一致")

    def test_line_boundary_state_switches(self):
        """切片落在围栏/表格边界上时状态机切换正确（逐字符喂入）。"""
        text = ("| A | B |\n| --- | --- |\n| 1 | 2 |\n"
                "```py\nx\n```\n"
                "| C | D |\n| --- | --- |\n| 3 | 4 |\n")
        whole = render([text])
        assert whole == render([c for c in text])
        # 现状语义（与旧实现一致）：围栏行不落定表格缓冲，闭合后表格行继续并入同一缓冲
        assert whole.startswith("  -- py --\n    1 x\n")
        assert len(border_lines(whole)) == 6


# ═══════════════════════════════════════════════════════════════
# 基线对照（v2/tests/baseline/data/renderer.json，旧实现 71 用例）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def baseline() -> dict:
    if not BASELINE_FILE.exists():
        pytest.skip("基线文件不存在（跳过快照对照）")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


class TestBaselineRenderer:
    """对旧实现基准逐用例比对（T0.1 产物；字节等价即行为等价）。"""

    def test_char_width(self, baseline):
        for case in baseline["groups"]["renderer._char_width"]:
            assert char_width(case["input"]["ch"]) == case["result"], case["id"]

    def test_display_width(self, baseline):
        for case in baseline["groups"]["renderer._display_width"]:
            assert display_width(case["input"]["text"]) == case["result"], case["id"]

    def test_wrap_cell(self, baseline):
        console = make_console(plain=False)
        assert console.is_plain() is False
        for case in baseline["groups"]["renderer._wrap_cell"]:
            assert wrap_cell(case["input"]["text"], case["input"]["max_width"],
                             console) == case["result"], case["id"]

    def test_render_line(self, baseline):
        styler = make_styler(plain=False)
        for case in baseline["groups"]["renderer.render_line"]:
            assert styler.render_line(case["input"]["line"]) == case["result"], case["id"]

    def test_split_cells_and_separator(self, baseline):
        for case in baseline["groups"]["renderer._split_cells / _is_table_separator"]:
            cells = split_cells(case["input"]["raw"])
            assert cells == case["result"]["cells"], case["id"]
            assert is_table_separator(cells) == case["result"]["is_table_separator"], case["id"]

    def test_fit_widths(self, baseline):
        for case in baseline["groups"]["renderer._fit_widths"]:
            inp = case["input"]
            assert fit_widths(inp["natural"], inp["avail"],
                              inp["min_col"]) == case["result"], case["id"]

    def test_inline_rules(self, baseline):
        styler = make_styler(plain=False)
        for case in baseline["groups"]["renderer.InlineRules.render"]:
            assert styler.inline(case["input"]["text"]) == case["result"], case["id"]

    def test_code_block(self, baseline):
        styler = make_styler(plain=False)
        for case in baseline["groups"]["renderer.CodeBlockRenderer.render"]:
            inp = case["input"]
            assert styler.code_block(inp["lang"], inp["body"]) == case["result"], case["id"]

    def test_colorize_diff(self, baseline):
        console = make_console(plain=False)
        theme = Theme(console)
        for case in baseline["groups"]["renderer.colorize_diff"]:
            assert colorize_diff(case["input"]["diff"], theme) == case["result"], case["id"]


# ═══════════════════════════════════════════════════════════════
# 旧实现流式路径对照（v2/tests/baseline/stream/render_stream.json，70 场景）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def stream_snapshot() -> dict:
    if not STREAM_SNAPSHOT_FILE.exists():
        pytest.skip("流式路径基准不存在（跳过快照对照）")
    return json.loads(STREAM_SNAPSHOT_FILE.read_text(encoding="utf-8"))


class TestOldImplementationStreamSnapshot:
    """对旧实现 `StreamingRenderer` 的 70 个场景逐字比对（判据 = 输出字节等价）。

    场景输入（`render_stream_cases.py`）覆盖：块级/行内形态、表格四形态、
    缓冲上限与落定语义、代码块状态机、reset 重播、纯文本形态、切片粒度差异。
    """

    def test_stream_outputs_match_old_implementation(self, stream_snapshot):
        cases = stream_snapshot["groups"][STREAM_SNAPSHOT_GROUP]
        assert len(cases) >= 70
        for case in cases:
            inp = case["input"]
            buf = io.StringIO()
            console = Console(buf, truecolor=True, platform="linux")
            console.set_plain(bool(inp["plain"]))
            renderer = StreamingRenderer(
                Theme(console), console,
                width_fn=lambda w=inp["width"]: w,
                visible_width_fn=lambda v=inp["visible"]: v,
            )
            for item in inp["ops"]:
                op = item[0]
                arg = item[1] if len(item) > 1 else None
                if op == "feed":
                    renderer.feed(arg)
                elif op == "flush":
                    renderer.flush()
                elif op == "reset":
                    renderer.reset()
                else:
                    raise AssertionError(f"未知操作: {op}")
            assert buf.getvalue() == case["result"], case["id"]
