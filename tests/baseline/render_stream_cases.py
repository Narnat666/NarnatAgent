"""渲染器流式场景用例集（T3.9 补充基准的输入契约，**纯数据**）。

与 `cases.py` 的分工：
- `cases.py`（T0.1）：旧实现的**纯函数**用例（`renderer.json` 的 71 用例）；
- 本文件（T3.9）：旧的 **`StreamingRenderer` 流式路径**用例——旧实现该路径直接写
  stdout，T0.1 明确未覆盖（见 `README.md` 第 4 节），T3.9 用"重定向 stdout + 固定
  宽度/纯文本开关"的方式把它纳入基准，作为"渲染管道重组但行为零变化"的判据。

场景格式：

    {"id": str, "width": int, "plain": bool, "visible": int,
     "ops": [("feed", 文本) | ("flush",) | ("reset",), ...]}

- `width`：终端宽度（旧实现经 `NARNAT_TERM_WIDTH` 环境变量、新实现经构造注入）；
- `visible`：srWindow 实测可见窗格宽度（0 = 不可用；两侧都固定注入）；
- `plain`：纯文本模式（headless 形态）；
- 覆盖：块级/行内形态、表格四形态（直排/折行/分块/降级）、缓冲上限与落定语义、
  代码块状态机、reset 重播、纯文本形态。

要求：本文件不得导入任何实现（新旧实现共用同一份输入）。
"""
from __future__ import annotations


def by_chars(text: str, step: int) -> list[str]:
    """按固定步长切片（模拟 token 流）。"""
    return [text[i:i + step] for i in range(0, len(text), step)]


def by_lines(text: str) -> list[str]:
    """按行切片（保留空行）。"""
    return [line + "\n" for line in text.split("\n")[:-1]]


TABLE_DIRECT = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
TABLE_NOSEP = "| A | B |\n| 1 | 2 |\n| 3 | 4 |\n"
TABLE_NOEDGE = "A | B |\nC | D |\n"
TABLE_LONG = "".join(f"| r{i} | v{i} |\n" for i in range(70))
TABLE_WIDE_3 = "| 姓名 | 说明 | 备注 |\n| --- | --- | --- |\n| 张三 | 很长的说明文本内容 | 备注信息 |\n"
TABLE_WIDE_8 = ("| c0 | c1 | c2 | c3 | c4 | c5 | c6 | c7 |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| a0 | a1 | a2 | a3 | a4 | a5 | a6 | a7 |\n")
TABLE_ALIGN = "| 左 | 中 | 右 |\n| :--- | :---: | ---: |\n| a | b | c |\n"
TABLE_SHORT_ROW = "| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |\n"
TABLE_ESCAPE = "| a \\| b | c |\n| --- | --- |\n| x | y |\n"
TABLE_EMPTY_DATA = "| A | B |\n| --- | --- |\n|  |  |\n"
TABLE_HEADER_ONLY = "| A | B |\n| --- | --- |\n"
TABLE_PLAIN_ROWS = "| a | b |\n| c | d |\n"
CODE_CLOSED = "```python\nprint(1)\nprint(2)\n```\n"
CODE_UNCLOSED = "```json\n{\"a\": 1}\n(\n"
CODE_LANG_SECOND = "```\n```python\nx = 1\n```\n"
CODE_UNKNOWN = "```UnKnown\nx\ny\n```\n"
MIXED = ("# 标题\n\n引言 `code` **粗**\n\n"
         "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
         "```python\ndef f():\n    return 1\n```\n\n"
         "- [x] 完成\n> 引用\n\n收尾段落\n")
LISTING = "- 项目\n1. 第一步\n- [ ] 待办\n>> 嵌套\n---\n***\n#### 四级\n"
FENCE_BETWEEN_TABLES = ("| A | B |\n| --- | --- |\n| 1 | 2 |\n"
                        "```py\nx\n```\n"
                        "| C | D |\n| --- | --- |\n| 3 | 4 |\n")


def _build() -> list[dict]:
    cases: list[dict] = []

    def add(sid: str, width: int, ops: list, plain: bool = False, visible: int = 0) -> None:
        cases.append({"id": sid, "width": width, "plain": plain,
                      "visible": visible, "ops": ops})

    # ── 块级 / 行内形态 ──
    add("lines_basic", 100, [("feed", LISTING), ("flush",)])
    add("inline_elements", 100,
        [("feed", "这是**加粗**与[链接](https://example.com)与~~删除~~与`码`\n"), ("flush",)])
    add("unfinished_line", 100, [("feed", "第一"), ("feed", "行\n"), ("flush",)])
    add("blank_lines", 100, [("feed", "\n   \n\t\n"), ("flush",)])
    add("carriage_return", 100, [("feed", "abc\r\n|x|y|\r\n"), ("flush",)])
    add("no_leading_pipe", 100, [("feed", TABLE_NOEDGE), ("flush",)])

    # ── 表格：直排 / 降级 / 缓冲上限 ──
    add("table_direct", 100, [("feed", TABLE_DIRECT), ("flush",)])
    add("table_direct_chunked_stream", 100,
        [("feed", c) for c in by_chars(TABLE_DIRECT, 3)] + [("flush",)])
    add("table_direct_lines", 100, [("feed", c) for c in by_lines(TABLE_DIRECT)] + [("flush",)])
    add("table_no_separator", 100, [("feed", TABLE_NOSEP), ("flush",)])
    add("table_no_separator_chars", 100,
        [("feed", c) for c in by_chars(TABLE_NOSEP, 2)] + [("flush",)])
    add("table_long_buffer", 100, [("feed", TABLE_LONG), ("flush",)])
    add("table_header_only", 100, [("feed", TABLE_HEADER_ONLY), ("flush",)])
    add("table_empty_data", 100, [("feed", TABLE_EMPTY_DATA), ("flush",)])
    add("table_then_text", 100, [("feed", TABLE_DIRECT + "后续段落\n"), ("flush",)])
    add("table_after_text_then_table", 100,
        [("feed", TABLE_PLAIN_ROWS + "mid\n| X | Y |\n| --- | --- |\n| 9 | 8 |\n"), ("flush",)])

    # ── 表格：宽度形态 ──
    add("table_wrap", 30, [("feed", TABLE_WIDE_3), ("flush",)])
    add("table_wrap_chars", 30, [("feed", c) for c in by_chars(TABLE_WIDE_3, 5)] + [("flush",)])
    add("table_chunked", 40, [("feed", TABLE_WIDE_8), ("flush",)])
    add("table_chunked_extra_chars", 40,
        [("feed", c) for c in by_chars(TABLE_WIDE_8, 4)] + [("flush",)])
    add("table_narrow_demote", 20, [("feed", TABLE_WIDE_8), ("flush",)])
    add("table_single_col", 30,
        [("feed", "| 只有一列很长的内容文本 |\n| --- |\n| 值也很长的一段文字 |\n"), ("flush",)])
    add("table_alignment", 100, [("feed", TABLE_ALIGN), ("flush",)])
    add("table_alignment_wrap", 24, [("feed", TABLE_ALIGN), ("flush",)])
    add("table_short_row", 100, [("feed", TABLE_SHORT_ROW), ("flush",)])
    add("table_escaped_pipe", 100, [("feed", TABLE_ESCAPE), ("flush",)])
    add("table_12cols", 30,
        [("feed", "| " + " | ".join(f"h{i}" for i in range(12)) + " |\n"
                  + "| " + " | ".join("---" for _ in range(12)) + " |\n"
                  + "| " + " | ".join(f"v{i}" for i in range(12)) + " |\n"), ("flush",)])

    # ── 落定语义 ──
    add("flush_half_line", 100, [("feed", "部分内容"), ("flush",)])
    add("flush_table_fragment", 100, [("feed", "| A | B | C"), ("flush",)])
    add("flush_table_fragment_then_more", 100,
        [("feed", "| A | B |"), ("feed", " C |"), ("flush",)])
    add("flush_in_code", 100, [("feed", "```python\nx = 1\n"), ("flush",)])
    add("flush_code_lang_only", 100, [("feed", "```python\n"), ("flush",)])
    add("flush_twice", 100, [("feed", "abc"), ("flush",), ("flush",)])
    add("flush_empty", 100, [("flush",)])

    # ── 代码块 ──
    add("code_closed", 100, [("feed", CODE_CLOSED), ("flush",)])
    add("code_closed_chars", 100, [("feed", c) for c in by_chars(CODE_CLOSED, 4)] + [("flush",)])
    add("code_unclosed", 100, [("feed", CODE_UNCLOSED), ("flush",)])
    add("code_lang_second_line", 100, [("feed", CODE_LANG_SECOND), ("flush",)])
    add("code_unknown_lang", 100, [("feed", CODE_UNKNOWN), ("flush",)])
    add("code_then_text", 100, [("feed", CODE_CLOSED + "之后文字\n"), ("flush",)])
    add("code_empty_block", 100, [("feed", "```\n```\n"), ("flush",)])
    add("code_trailing_spaces", 100,
        [("feed", "```json\n{   \n  \"a\": 1  \n}\n```\n"), ("flush",)])

    # ── 重置 / 混排 ──
    add("reset_then_replay", 100,
        [("feed", "我先了"), ("reset",), ("feed", "我先了解一下\n"), ("flush",)])
    add("reset_table_state", 100,
        [("feed", "| A | B |\n| --- |"), ("reset",), ("feed", TABLE_DIRECT), ("flush",)])
    add("reset_code_state", 100,
        [("feed", "```python\nx=1\n"), ("reset",), ("feed", "新内容\n"), ("flush",)])
    add("mixed_document", 100, [("feed", MIXED), ("flush",)])
    add("mixed_document_chars", 100, [("feed", c) for c in by_chars(MIXED, 6)] + [("flush",)])
    add("mixed_document_lines", 100, [("feed", c) for c in by_lines(MIXED)] + [("flush",)])

    # ── 纯文本（headless）形态 ──
    add("plain_table_direct", 100, [("feed", TABLE_DIRECT), ("flush",)], plain=True)
    add("plain_table_wrap", 30, [("feed", TABLE_WIDE_3), ("flush",)], plain=True)
    add("plain_code", 100, [("feed", CODE_CLOSED), ("flush",)], plain=True)
    add("plain_mixed_chars", 100, [("feed", c) for c in by_chars(MIXED, 5)] + [("flush",)], plain=True)
    add("plain_table_alignment", 100, [("feed", TABLE_ALIGN), ("flush",)], plain=True)
    add("plain_table_chunked", 40, [("feed", TABLE_WIDE_8), ("flush",)], plain=True)

    # ── 表格细节补充 ──
    add("table_multiline_header", 100,
        [("feed", "| a | b |\n| h2 |\n| --- | --- |\n| 1 | 2 |\n"), ("flush",)])
    add("table_buffer_60_with_sep", 100,
        [("feed", "| h | v |\n| --- | --- |\n" + "".join(f"| r{i} | v{i} |\n" for i in range(70))),
         ("flush",)])
    add("table_flag_reset_between", 100,
        [("feed", "| A |\n| x |\n中间段落\n| C | D |\n| --- | --- |\n| 1 | 2 |\n"), ("flush",)])
    add("table_exact_border", 40,
        [("feed", "| abcd | efgh |\n| --- | --- |\n| 1234 | 5678 |\n"), ("flush",)])
    add("table_srwindow_shrink", 100, [("feed", TABLE_WIDE_3), ("flush",)], visible=40)
    add("table_srwindow_shrink_narrow", 100, [("feed", TABLE_WIDE_8), ("flush",)], visible=24)
    add("table_wide_cjk_wrap", 26,
        [("feed", "| 中文列一 | 中文列二 |\n| --- | --- |\n"
                  "| 甲乙丙丁戊己庚辛 | 一二三四五六七八 |\n"), ("flush",)])
    add("table_then_code", 100, [("feed", TABLE_DIRECT + "```\nx\n```\n"), ("flush",)])
    add("table_row_only_then_flush", 100, [("feed", "| 单独一行 |\n"), ("flush",)])
    add("table_sep_only", 100, [("feed", "| --- | --- |\n"), ("flush",)])
    add("table_cells_uneven", 100,
        [("feed", "| A | B | C |\n| --- | --- | --- |\n| 1 |\n| 1 | 2 | 3 | 4 |\n"), ("flush",)])
    add("table_empty_cells", 100,
        [("feed", "| A |  | C |\n| --- | --- | --- |\n|  | b |  |\n"), ("flush",)])
    add("code_indented_fence", 100, [("feed", "   ```python\nx = 1\n   ```\n"), ("flush",)])
    add("code_after_table_buffer", 100,
        [("feed", "| A | B |\n| 1 | 2 |\n```\nx\n```\n"), ("flush",)])
    add("fence_between_tables", 100, [("feed", FENCE_BETWEEN_TABLES), ("flush",)])
    add("fence_between_tables_chars", 100,
        [("feed", c) for c in by_chars(FENCE_BETWEEN_TABLES, 3)] + [("flush",)])

    return cases


# 流式场景全集（旧实现提取器与新实现对照测试共用）
SCENARIOS = _build()
