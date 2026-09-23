"""流式 Markdown 渲染器 —— 解析 → 布局 → 着色管道的流式入口。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「流式 Markdown 增量渲染」：只有以换行结束的完整行才立即渲染，未完成行留缓冲；
  去空白后为空的行不产生输出；
- 「渲染缓冲落定与重置」：落定立即输出残留（代码块内按代码行累积、表格形态残留
  按段落输出且不并入表格缓冲、其余按普通行渲染；未闭合代码块强制渲染不吞内容；
  表格缓冲整体落定），所有落定调用点行为一致（不存在最终/非最终差异）；
  重置清空全部缓冲与状态（流中断重试前调用）；
- 「表格稳定渲染」：候选行缓冲（首尾 `|` 且竖线不少于 2 个）、分隔行标记、60 行
  强制落定、遇非表格行先落定、缓冲为空清标志；落定决策树（不完整降级 → 直排 /
  折行 / 分块 / 极端窄宽降级段落）。

布局分工：
- `TableRenderer`：表格三形态布局与着色（直排 / 折行 / 分块），分块在连一列都放
  不下时返回"需降级"由流式状态机统一降级为段落；
- `StreamingRenderer`：状态机（普通行 ↔ 代码块）、表格缓冲与落定路由、锁保护的
  流式入口（并行工具线程可能并发 feed/flush/reset）。

颜色经构造注入的 `Theme` 取用；宽度探测经构造注入（默认 `terminal_width` /
`srwindow_cols`，测试可替换），表格落定时实时探测（窗口 resize 即时生效）。
"""
from __future__ import annotations

import io
import threading
from typing import Callable, List, Optional

from ..output import Console, Theme
from .markdown import MarkdownStyler, fit_widths, is_table_separator, split_cells
from .width import display_width, srwindow_cols, terminal_width, wrap_cell

__all__ = ["StreamingRenderer", "TableRenderer"]

# 表格行缓冲上限：超长表格时立即落定，避免长时间无输出（"整体收集"的固有代价防御）
MAX_TABLE_BUFFER = 60

# 表格可用宽度 = 终端宽度 - 4(缩进) - 2(安全余量，防终端 pending-wrap 折行破坏边框)
TABLE_INDENT_MARGIN = 6

# 每列的边框+内边距开销：1(|) + 1(左空格) + 1(右空格) = 3
PER_COL_OVERHEAD = 3


class TableRenderer:
    """表格布局渲染器：直排 / 折行 / 分块三形态（单元格文本已行内着色）。

    分块形态在"连一列都画不下"时不做降级输出，而是返回 False 交由
    `StreamingRenderer` 统一按段落降级（保持与其它降级路径同一出口）。
    """

    def __init__(self, theme: Theme, console: Console):
        self._theme = theme
        self._console = console

    def render_block(self, rendered: List[List[str]], widths: List[int], cols: int,
                     wrap: bool = False,
                     alignments: Optional[List[str]] = None) -> None:
        """渲染一个表格块。

        Args:
            rendered: 已做InlineRules渲染的单元格文本
            widths: 各列宽度
            cols: 列数
            wrap: 是否对超宽单元格折行
            alignments: 各列对齐方式 ("left"/"center"/"right")，默认全左对齐
        """
        theme = self._theme
        if alignments is None:
            alignments = ["left"] * cols

        # 如果需要折行，先对每个单元格做折行处理
        if wrap:
            wrapped: List[List[List[str]]] = []
            for row in rendered:
                wrapped_row: List[List[str]] = []
                for i, c in enumerate(row):
                    w = widths[i] if i < len(widths) else widths[-1]
                    lines = wrap_cell(c, w, self._console)
                    wrapped_row.append(lines)
                wrapped.append(wrapped_row)
            # 渲染折行表格
            self._render_wrapped(wrapped, widths, cols, alignments)
        else:
            # 单行单元格（带对齐）
            sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
            border = f"    {theme.md_table_border}{sep}{theme.rst}"

            def _row(cells):
                parts = []
                for i, c in enumerate(cells):
                    dw = display_width(c)
                    pad = widths[i] - dw
                    al = alignments[i] if i < len(alignments) else "left"
                    if al == "right":
                        parts.append(" " * (pad + 1) + c + " ")
                    elif al == "center":
                        lp = pad // 2
                        rp = pad - lp
                        parts.append(" " * (lp + 1) + c + " " * (rp + 1))
                    else:
                        parts.append(" " + c + " " * (pad + 1))
                return (f"    {theme.md_table_border}|{theme.rst}{theme.md_table_content}"
                        + f"{theme.md_table_border}|{theme.rst}{theme.md_table_content}".join(parts)
                        + f"{theme.md_table_border}|{theme.rst}")

            parts = [border]
            for row in rendered:
                parts.append(_row(row))
                parts.append(border)
            self._console.write("\n".join(parts) + "\n")

    def _render_wrapped(self, wrapped: List[List[List[str]]],
                        widths: List[int], cols: int,
                        alignments: Optional[List[str]] = None) -> None:
        """渲染折行表格。

        Args:
            wrapped: wrapped[row][col] = [line1, line2, ...] 折行后的文本
            widths: 各列宽度
            cols: 列数
            alignments: 各列对齐方式
        """
        theme = self._theme
        if alignments is None:
            alignments = ["left"] * cols

        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        border = f"    {theme.md_table_border}{sep}{theme.rst}"

        def _pad_cell(text: str, width: int, al: str = "left") -> str:
            """按对齐方式填充到指定显示宽度"""
            dw = display_width(text)
            pad = max(0, width - dw)
            if al == "right":
                return " " * (pad + 1) + text + " "
            elif al == "center":
                lp = pad // 2
                rp = pad - lp
                return " " * (lp + 1) + text + " " * (rp + 1)
            else:
                return " " + text + " " * (pad + 1)

        parts: List[str] = []
        for row in wrapped:
            parts.append(border)
            # 该行中单元格的最大折行数
            max_lines = max(len(cell) for cell in row)
            for line_idx in range(max_lines):
                cell_parts: List[str] = []
                for col_idx in range(cols):
                    cell = row[col_idx] if col_idx < len(row) else [""]
                    text = cell[line_idx] if line_idx < len(cell) else ""
                    w = widths[col_idx] if col_idx < len(widths) else widths[-1]
                    al = alignments[col_idx] if col_idx < len(alignments) else "left"
                    cell_parts.append(f"{theme.md_table_border}|{theme.rst}{theme.md_table_content}"
                                      + _pad_cell(text, w, al))
                parts.append("    " + "".join(cell_parts) + f"{theme.md_table_border}|{theme.rst}")
        parts.append(border)
        self._console.write("\n".join(parts) + "\n")

    def render_chunked(self, rendered: List[List[str]],
                       natural_widths: List[int],
                       cols: int, table_avail: int,
                       alignments: Optional[List[str]] = None) -> bool:
        """列数过多、放不下时按列分块输出，首列作为锚点重复。

        每个分块都是完整的子表（锚点列 + 若干附加列），
        块内列宽用 `fit_widths` 保证子表总宽不超过 table_avail，
        避免输出行超出终端宽度被强制折行、破坏边框。

        Args:
            rendered: 已做 InlineRules 渲染的单元格
            natural_widths: 各列自然宽度
            cols: 总列数
            table_avail: 表格可用宽度（含边框）
            alignments: 各列对齐方式

        Returns:
            True 已按分块渲染；False 终端太窄连一列都画不下，调用方应降级为段落。
        """
        theme = self._theme
        if alignments is None:
            alignments = ["left"] * cols
        if cols <= 1:
            # 单列表：直接按可用宽度压缩渲染
            avail_for_content = max(1, table_avail - 4)
            widths = fit_widths([natural_widths[0]], avail_for_content)
            self.render_block(rendered, widths, 1, wrap=True, alignments=alignments[:1])
            return True

        # 每列开销：3（| + 两侧空格）；表额外开销：最右 | = 1
        per_col_overhead = PER_COL_OVERHEAD
        # 单列子表至少需要的宽度：min_col(2) + 3 + 1 = 6
        min_single_col_total = 2 + per_col_overhead + 1
        if table_avail < min_single_col_total:
            # 终端太窄，连一列表格都画不下 → 降级为段落输出
            return False

        # 分块：第0列(锚点) + 每块若干附加列；放不下锚点的列单独成块
        def _min_total(n: int) -> int:
            """n 列子表的最小总宽：每列 2 内容 + 3 边框内边距 + 1 右框"""
            return 2 * n + per_col_overhead * n + 1

        data_col_indices = list(range(1, cols))
        chunks: List[List[int]] = []
        i = 0
        while i < len(data_col_indices):
            chunk = [0]
            # 贪心加入附加列：保证块内每列最少 2 字符宽
            while i < len(data_col_indices):
                trial = chunk + [data_col_indices[i]]
                if _min_total(len(trial)) > table_avail:
                    break
                chunk = trial
                i += 1
            if len(chunk) == 1:
                # 锚点旁放不下任何列 → 该列单独输出（无锚点）
                chunks.append([data_col_indices[i]])
                i += 1
            else:
                chunks.append(chunk)

        total_chunks = len(chunks)

        for chunk_idx, col_indices in enumerate(chunks):
            # 提取子表数据
            sub_rendered = [[row[c] if c < len(row) else "" for c in col_indices]
                            for row in rendered]
            sub_alignments = [alignments[c] if c < len(alignments) else "left"
                              for c in col_indices]
            sub_natural = [natural_widths[c] if c < len(natural_widths) else 2
                           for c in col_indices]

            # 子表列宽：保证 sum + 开销 ≤ table_avail
            sub_cols = len(col_indices)
            avail_for_content = table_avail - (per_col_overhead * sub_cols + 1)
            sub_widths = fit_widths(sub_natural, max(sub_cols * 1, avail_for_content))
            self.render_block(sub_rendered, sub_widths, sub_cols,
                              wrap=True, alignments=sub_alignments)

            # 分块标签
            if total_chunks > 1:
                self._console.write(
                    f"    {theme.d}── 表格 ({chunk_idx + 1}/{total_chunks}) ──{theme.rst}\n"
                )
        return True


class StreamingRenderer:
    """流式 Markdown 渲染器（增量文本 → 逐行渲染）。

    状态机：NORMAL ↔ CODE_BLOCK；表格候选行进入缓冲，落定时按四形态路由。
    线程安全：feed / flush / reset 均持同一把锁（并行工具线程可能并发调用）。
    """

    def __init__(self, theme: Theme, console: Console, *,
                 width_fn: Callable[[], int] = terminal_width,
                 visible_width_fn: Callable[[], int] = srwindow_cols):
        """
        Args:
            theme: 颜色体系（构造注入，生命周期内共享）；
            console: 输出原语（写出与纯文本模式判定）；
            width_fn: 终端宽度探测（默认 `width.terminal_width`，测试可替换）；
            visible_width_fn: 可见窗格实测宽度（默认 `width.srwindow_cols`，
                表格渲染前二次校验收缩，返回 0 表示不可用）。
        """
        self._console = console
        self._styler = MarkdownStyler(theme, console)
        self._tables = TableRenderer(theme, console)
        self._width_fn = width_fn
        self._visible_width_fn = visible_width_fn
        self._buf = io.StringIO()
        self._in_code = False
        self._code_lang = ""
        self._code_lines: List[str] = []
        self._table_rows: List[str] = []       # 缓存原始行（非分隔行）
        self._table_has_separator = False      # 是否见过分隔行
        self._lock = threading.Lock()          # 并行工具线程可能并发 flush/feed

    def _buf_append(self, text: str) -> None:
        self._buf.write(text)

    def _buf_get_and_clear(self) -> str:
        result = self._buf.getvalue()
        self._buf.seek(0)
        self._buf.truncate(0)
        return result

    def _process_lines(self, raw: str,
                       handler: Callable[[str, str], bool]) -> bool:
        """逐完整行消费 raw。
        返回 True: handler 触发状态切换，未消费部分已放回 _buf。
        返回 False: 全部完整行已消费，未完成行已放回 _buf。
        """
        while "\n" in raw:
            line, rest = raw.split("\n", 1)
            if handler(line, rest):
                self._buf_append(rest)
                return True
            raw = rest
        self._buf_append(raw)
        return False

    def feed(self, chunk: str) -> None:
        """流式输入入口。状态切换时自动换 handler 继续消费剩余行。"""
        with self._lock:
            self._buf_append(chunk)
            raw = self._buf_get_and_clear()
            while raw:
                handler = self._on_code_line if self._in_code else self._on_normal_line
                if not self._process_lines(raw, handler):
                    break
                # 状态已切换，取出 handler 放回的剩余行继续处理
                raw = self._buf_get_and_clear()

    def _on_code_line(self, line: str, rest: str) -> bool:
        line = line.replace("\r", "")  # 剥离\r控制符：防终端回车覆盖行首显示
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
        line = line.replace("\r", "")  # 剥离\r控制符：防终端回车覆盖行首显示
        stripped = line.strip()
        if stripped.startswith("```"):
            self._in_code = True
            self._code_lang = stripped[3:].strip()
            return True
        # 表格候选行：必须以|开头且以|结尾（标准markdown表格形态）。
        # 严格判定优先防误判：普通文本行含竖线（如"参见下表：| A | B |"）若被当作
        # 表格候选，会混入真实表格导致列数错乱。无首|的表格（`A | B |`）宁可降级
        # 为段落渲染，信息无损，也不误判。
        is_table = stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2
        if is_table:
            cells = split_cells(stripped)
            if is_table_separator(cells):
                self._table_has_separator = True
            self._table_rows.append(stripped)
            # 超长表格防御：整体收集有"长表格期间无输出"的固有代价，
            # 缓冲达到上限即落定（完整表格渲染/不完整段落化），保证输出持续可见。
            if len(self._table_rows) >= MAX_TABLE_BUFFER:
                self._flush_table()
            return False
        # 非竖线行：先刷出缓冲；若缓冲为空则清标志位防跨表格污染
        if self._table_rows:
            self._flush_table()
        else:
            self._table_has_separator = False
        rendered = self._styler.render_line(line)
        if not rendered:
            return False
        self._console.write(rendered + "\n")
        return False

    def _demote_to_paragraphs(self) -> None:
        """将缓冲的表格候选行降级为段落逐行输出，并清空表格状态。"""
        for line in self._table_rows:
            rendered = self._styler.paragraph(line)
            if rendered:
                self._console.write(rendered + "\n")
        self._table_rows.clear()
        self._table_has_separator = False

    def _flush_table(self) -> None:
        if not self._table_rows:
            self._table_has_separator = False
            return
        if not self._table_has_separator:
            # 无分隔行 → 不是表格，降级为段落输出
            self._demote_to_paragraphs()
            return
        # 有分隔行 → 拆分表头和数据
        # 找出分隔行位置（第一个全由---组成的分隔行）
        sep_idx = -1
        for i, raw in enumerate(self._table_rows):
            cells = split_cells(raw)
            if is_table_separator(cells):
                sep_idx = i
                break
        # 表头行=分隔行之前的行，数据行=分隔行之后的行
        header_rows = self._table_rows[:sep_idx] if sep_idx >= 0 else self._table_rows
        data_rows = self._table_rows[sep_idx + 1:] if sep_idx >= 0 else []

        # 从分隔行解析列对齐：:--- → left, :---: → center, ---: → right
        alignments: List[str] = []
        if sep_idx >= 0:
            sep_cells = split_cells(self._table_rows[sep_idx])
            for cell in sep_cells:
                l = cell.startswith(":")
                r = cell.endswith(":")
                if l and r:
                    alignments.append("center")
                elif r:
                    alignments.append("right")
                else:
                    alignments.append("left")
        # 无数据行 → 不是真正的表格（只有表头+分隔行或孤立分隔行）
        if not data_rows:
            self._demote_to_paragraphs()
            return
        # 数据行全部为空 → 不算表格
        data_cells = [split_cells(line) for line in data_rows]
        if not any(any(c for c in row) for row in data_cells):
            self._demote_to_paragraphs()
            return

        # ── 计算列宽（带终端宽度限制） ──
        term_w = self._width_fn()
        # 二次校验：以 srWindow 实测可见窗格宽为准收缩。
        # 宽度测量路径偶发偏大（resize 竞态 / DPI 取整 / GetClientRect 未就绪
        # 返回 0 走错误回退）时，行宽会超出实际窗格被终端折行 → 表格紊乱。
        # 实测值 ≤ 测量值时以实测值为准，保证输出行不超出可见窗格。
        real_w = self._visible_width_fn()
        if real_w and 0 < real_w < term_w:
            term_w = real_w
        table_avail = max(term_w - TABLE_INDENT_MARGIN, 8)

        all_rows = header_rows + data_rows
        rows_cells = [split_cells(line) for line in all_rows]
        cols = max(len(row) for row in rows_cells)
        # 补齐短行
        for row in rows_cells:
            while len(row) < cols:
                row.append("")

        rendered = [[self._styler.inline(c) for c in row] for row in rows_cells]

        # 自然列宽（内容决定）
        natural_widths = [0] * cols
        for row in rendered:
            for i, c in enumerate(row):
                w = display_width(c)
                if w > natural_widths[i]:
                    natural_widths[i] = w

        col_overhead = PER_COL_OVERHEAD * cols + 1  # +1 是最右边的 |
        total_natural = sum(natural_widths) + col_overhead

        if total_natural <= table_avail:
            # ── 正常宽度：不折行 ──
            widths = natural_widths
            self._tables.render_block(rendered, widths, cols, alignments=alignments)
        else:
            # ── 超宽：限制列宽 + 折行，保证整表不超出终端宽度 ──
            avail_for_content = table_avail - col_overhead
            # 连每列 2 字符都放不下 → 按列分块输出。
            # 但分块在极端窄宽度下会产生"每块1列、单元格碎成小块"的不可读形态
            # （如 term_w≤16 时 4+ 列表格 → 8 字符小块 + 分块标签 = 用户看到的"表格卡"乱表）。
            # 此时降级为段落输出：信息不丢、可读性远好于碎片。
            if avail_for_content < cols * 2:
                if table_avail < 20:
                    self._demote_to_paragraphs()
                else:
                    # 分块：连一列都放不下时返回 False → 统一走段落降级
                    if not self._tables.render_chunked(rendered, natural_widths, cols,
                                                       table_avail, alignments=alignments):
                        self._demote_to_paragraphs()
            else:
                widths = fit_widths(natural_widths, avail_for_content)
                # 折行渲染
                self._tables.render_block(rendered, widths, cols, wrap=True,
                                          alignments=alignments)

        self._table_rows.clear()
        self._table_has_separator = False

    def _flush_code_block(self) -> None:
        if self._code_lines:
            rendered = self._styler.code_block(self._code_lang, "\n".join(self._code_lines))
            self._console.write(rendered + "\n")
        self._code_lines.clear()
        self._code_lang = ""

    def flush(self) -> None:
        """消费缓冲区残留内容。

        flush 只出现在 LLM 回复文本完整到达之后（工具执行前 / 回复结束时调用），
        因此缓冲里的内容就是本轮回复的尾部，一律立即落定：
        - 普通文字半行 → 立即渲染（先于工具调度摘要显示）
        - 表格候选行 → 并入表格缓冲后 _flush_table 落定
          （完整表格渲染为表格；不完整降级为段落）
        - 代码块内容 → 累积后整块落定（不吞内容）

        不跨轮保留任何缓冲：上一轮回复尾部的表格行若残留，会与下一轮回复的
        表格行混合渲染成错乱表格（列数错位），这是"表格回归"的根源。

        所有落定调用点行为一致，不存在"最终 / 非最终"差异。
        """
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """flush 的锁内实现（调用方需持有 _lock）。"""
        remaining = self._buf_get_and_clear()
        if remaining.strip():
            if self._in_code:
                self._on_code_line(remaining, "")
            else:
                # 残留行是"未完成行"（没有换行结尾）。它可能是被流式 chunk
                # 切断的表格行片段：若按表格候选行缓冲，会在下方 _flush_table
                # 渲染出残缺表格（列内容丢失），后半行到达时又裸奔为段落 →
                # 表格错乱。因此表格形态的未完成行一律降级为段落输出
                # （信息不丢，也不污染表格缓冲）。
                stripped = remaining.strip()
                if (stripped.startswith("|") and stripped.endswith("|")
                        and stripped.count("|") >= 2):
                    rendered = self._styler.paragraph(stripped)
                    if rendered:
                        self._console.write(rendered + "\n")
                else:
                    self._on_normal_line(remaining, "")
            leftover = self._buf_get_and_clear()
            if leftover.strip():
                self._console.write(self._styler.render_line(leftover) + "\n")
        # 代码块落定：未闭合也渲染，避免内容被吞
        if self._in_code:
            self._flush_code_block()
            self._in_code = False
        # 表格落定：完整表格渲染；不完整（无分隔行/无数据行）降级为段落
        if self._table_rows:
            self._flush_table()

    def reset(self) -> None:
        """清空全部缓冲与状态（响应流中断自动重试前调用）。

        服务端中途断开时，本轮已 feed 的半行文字/表格行/代码块残留在状态机中；
        重试流会从开头重播同一内容，若不清理会导致：
        - 半行文字与重播内容拼接重复（"我先了" + "我先了解一下…"）
        - 表格表头/数据行重复渲染
        - 未闭合的代码块把重试提示与内容全部吞掉
        """
        with self._lock:
            self._buf.seek(0)
            self._buf.truncate(0)
            self._in_code = False
            self._code_lang = ""
            self._code_lines.clear()
            self._table_rows.clear()
            self._table_has_separator = False
