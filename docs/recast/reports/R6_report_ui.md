# R6 现状调研报告：UI 层（渲染器 / 界面 / 交互命令 / 中断 / headless / 颜色输出）

> 调研对象：narnat agent（工作目录 `D:\desktop\NarnatAgent`）重构前稳定版。
> 方法：逐文件完整通读源码（非 grep 抽样）+ 全局 grep 复验依赖边。所有行号以当前工作区源码为准，可直接用 `Read`/`grep` 复核。
> 实测行数（与任务卡预期值有出入者已注明）：renderer.py **983** / output.py **510** / ui_design.py **428** / session_commands.py **407** / interrupt.py **278** / headless.py **87**（任务卡写 ~200，实际 87）/ colors.py **43**（任务卡写 ~50，实际 43）。
> 无法核实的事项标注「未验证」，不做臆测。

---

### narnat_agent/ui/renderer.py（983 行）

**职责**：流式 Markdown→ANSI 渲染器——把 LLM 增量到达的文本按行做块级/行内样式渲染，含代码块状态机、表格识别与稳定渲染策略、CJK 宽度计算、终端宽度探测；只写 stdout，不持有交互状态。

**对外接口**（public 类/函数/方法，逐个列签名）：

- `class RenderConfig`（43）：渲染参数容器（类属性常量集，见「状态」）。
- `def _windows_console_window_cols() -> int`（85）：Windows conhost 可见窗口列数（WT/ConPTY/非 Win 返回 0）。
- `def _terminal_width() -> int`（149）：当前终端宽度（env 强制 > conhost 实测 > shutil），clamp 到 [20, 160]。
- `def _srwindow_cols() -> int`（167）：用 GetConsoleScreenBufferInfo.srWindow 实测可见窗格列数（二次校验用）。
- `def _char_width(ch: str) -> int`（197）：单字符显示宽度（CJK W/F=2，其余含歧义宽度=1）。
- `def _display_width(text: str) -> int`（208）：文本显示宽度（剔除 ANSI 后逐字符累加）。
- `def _visual_chars(text: str) -> List[str]`（214）：把文本拆为"视觉字符"列表（ANSI 序列整段保留）。
- `def _wrap_cell(ansi_text: str, max_width: int) -> List[str]`（243）：按显示宽度对含 ANSI 文本折行，每行继承活跃颜色。
- `def colorize_diff(diff_text: str) -> str`（300）：对 unified diff 文本加 ANSI 颜色（`[无差异]` 特判）。
- `def _sep() -> None`（323）：输出一条 `'─' * (_terminal_width() - 2)` 分隔线（前缀两空格，UI_SEPARATOR 色）。
- `class InlineRules`（331）：行内正则替换流水线；`@classmethod def render(cls, text: str) -> str`（345-346）。
- `class BlockRule`（360）：`def __init__(self, priority: int, name: str, match: Callable[[str], Optional[Match]], render: Callable[[str, Match], str]) -> None`（367-369）；`__slots__ = ('priority', 'name', 'match', 'render')`（365）。
- 模块级 render 函数（BlockRule 的 render 回调）：`_render_heading(_line: str, m: Match) -> str`（376）、`_render_hr(_line, _m)`（386）、`_render_task(_line, m)`（390）、`_render_ul(_line, _m)`（396）、`_render_ol(_line, m)`（400）、`_render_blockquote(_line, _m)`（406）、`_render_table_row(_line, _m)`（416）、`_render_paragraph(_line, _m)`（423）。
- `def _split_cells(raw: str) -> List[str]`（427）：按未转义 `|` 拆分表格行（`\|` 还原为 `|`）。
- `def _fit_widths(natural: List[int], avail: int, min_col: int = 2) -> List[int]`（432）：按比例压缩列宽到总宽 avail。
- `def _is_table_separator(cells: List[str]) -> bool`（459）：判定行是否为 `---` 类分隔行。
- `BLOCK_RULES: List[BlockRule]`（465-474）：模块级规则表（按 priority 升序）。
- `def render_line(line: str) -> str`（477）：对单行应用第一个匹配的块规则；空白行返回 `""`。
- `class CodeBlockRenderer`（492）：`@staticmethod def render(lang: str, body: str, width: int) -> str`（495-496）——注意 width 参数未被使用（见补丁痕迹）。
- `class StreamingRenderer`（512）：
  - `def __init__(self) -> None`（518）
  - `def feed(self, chunk: str) -> None`（552）：流式入口（线程安全）。
  - `def flush(self, final: bool = False) -> None`（917）：消费缓冲残留（final 为历史遗留参数，行为已统一）。
  - `def reset(self) -> None`（967）：清空全部缓冲与状态（流中断重试前调用）。

**依赖**（本文件 import 的内部模块）：
- `.colors`（21-35）：R、B、D、C_*、MD_*、CB_*、DIFF_*、UI_SEPARATOR、_stdout_write。
- `..output`（36）：`is_plain`。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/ui/ui_design.py:45-48`：`from .renderer import (colorize_diff, _sep, _terminal_width, _display_width, InlineRules, BlockRule, CodeBlockRenderer, StreamingRenderer, render_line)`——其中仅 `_sep`（用于 60、141）与 `StreamingRenderer`（用于 173）实际使用，其余为 re-export（见补丁痕迹）。
- `narnat_agent/ui/headless.py:10`：`from .renderer import StreamingRenderer`（用于 17）。
- 生产代码之外：`tool_exp/` 下大量历史实验脚本以 `import narnat_agent.ui.renderer as R` 方式复用（非生产依赖）。

**状态**：
- 模块级全局：`BLOCK_RULES`（模块级可变 List，465-474 构造后运行时不再写，只读）；`RenderConfig` 各属性（类属性，恒定）；`InlineRules._RE_*`（类属性正则，恒定）。无进程级可变全局。
- 类变量：`RenderConfig.MAX_TERMINAL_WIDTH=160`（46）、`MAX_TABLE_BUFFER=60`（49）、`re_ansi`（52）、`LANG_GROUPS`（55-66）、`COLOR_MAP`（67-68）、`LANG_COLORS`（69）、`RE_TABLE_SEP`（72）、`RE_CELL_SPLIT`（75）、`re_head/re_hr/re_task/re_ul/re_ol`（78-82）。
- 实例状态（StreamingRenderer）：`width`（构造时快照 `_terminal_width()`，519，实际在代码块渲染中被忽略）、`_buf`（io.StringIO，520）、`_in_code`（521）、`_code_lang`（522）、`_code_lines: List[str]`（523）、`_table_rows: List[str]`（524）、`_table_has_separator`（525）、`_lock: threading.Lock`（526，并行工具线程并发 flush/feed 保护）。

**行为要点**（编号；附行号；"可观察行为"）：
1. 行内替换流水线顺序固定：删除线 → 粗体 → 斜体 → 行内代码 → 图片 → 链接（347-352）；替换互不回溯（一次 sub 到位）。链接正则做 `(?<!\x1b)` 前置守卫防止匹配 ANSI 序列中的 `[`（343）。
2. 块规则按 priority 升序取"第一个匹配"：heading(10) → hr(20) → task(30) → ul(40) → ol(50) → blockquote(60，`s.startswith(">")`) → table(70，`s.startswith("|") and s.endswith("|") and s.count("|") >= 2`) → paragraph(100，恒真兜底)（465-474）；`render_line` 对空白行输出空串（479-480）。
3. 块渲染视觉格式：标题前缀 2 空格缩进 + 按级别选色（≤2:H1 / ==3:H3 / else:H4，376-383）；任务项 3 空格缩进 + `v`/`o` 标记（`[x]`/`[X]` 判定完成，390-393）；无序列表 3 空格 + `* `（396-397）；有序列表 3 空格 + `{num}. `（400-403）；引用 `> ` 嵌套层数渲染为 `| ` * depth（406-413）；表格行 4 空格 + 单元格以 ` | ` 连接（416-420）；普通段落 2 空格 + C_PRIMARY（423-424）。
4. 表格候选行判定（流式路径）：`stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2` 才进表格缓冲（590）；不满足此形态（如 `A | B |` 无首竖线）按段落渲染（注释明确"宁可降级为段落"防误判，586-589）。
5. 表格缓冲策略：候选行全量缓冲在 `_table_rows`；分隔行出现即置 `_table_has_separator`（593-594）；缓冲达 `MAX_TABLE_BUFFER=60` 行立即 `_flush_table()` 落定（596-599）；遇非表格行时先落定缓冲；缓冲为空时清 `_table_has_separator` 防跨表污染（601-605）。
6. 表格落定决策树 `_flush_table`（621-722）：①无分隔行 → 逐行降级为段落输出（612-619、625-627）；②找第一个分隔行 sep_idx（631-636），其前为表头、其后为数据行（638-639）；③无数据行 → 降级段落（654-658）；④数据行全空 → 降级段落（660-664）；⑤常规宽度（`sum(natural)+3*cols+1 <= table_avail`）→ 不折行直排（700-703）；⑥超宽 → `avail_for_content = table_avail - col_overhead`；若 `avail_for_content < cols*2` 且 `table_avail < 20` → 降级段落（防"表格卡"碎片，711-713）；否则按列分块 `_render_table_chunked`（715）；否则 `_fit_widths` 压缩 + 折行渲染（717-719）。落定后清空缓冲与标志（721-722）。
7. 表格宽度双测量与收缩：`term_w = _terminal_width()`；若 `_srwindow_cols()` 返回 `0 < real_w < term_w` 则以 real_w 为准（667-674）；表格可用宽度 `table_avail = max(term_w - 6, 8)`（4 缩进 + 2 安全余量，676）。
8. 列对齐解析：分隔行 `:---`→left、`:---:`→center、`---:`→right（641-653）；仅 headless/wrap 渲染路径应用对齐（763-769、796-807）。
9. 不折行表格形态（724-777）：首末与每行前后均有 `+---+` 边框行（横线 `-` * (w+2)），数据行 `    |cell|cell|`，单元格左右各补 1 空格并按对齐补 pad（754-771）。
10. 折行表格形态 `_render_wrapped_table`（779-824）：每数据行前一条边框，行内多物理行（单元格折行取该行最大折行数，813-814）；末尾补一条边框（823）。
11. 分块表格 `_render_table_chunked`（826-905）：首列作锚点逐块重复，贪心装附加列（`_min_total(n)=2n+3n+1` 约束，862-877）；锚点旁放不下任何列时该列单独成块（878-883）；块间输出标签 `    ── 表格 (i/n) ──`（仅 total_chunks>1，903-905）；单列表按可用宽度压缩渲染（845-850）；连 1 列都放不下（`table_avail < 6`）降级段落（856-859）。
12. 代码块流式状态机（NORMAL ↔ CODE_BLOCK）：NORMAL 状态行首 ` ``` ` 且 strip 后以它开头 → 切 CODE_BLOCK，语言取 `stripped[3:].strip()`（578-584）；CODE_BLOCK 状态中第一行（`_code_lines` 为空）以 ` ``` ` 开头 → 视为语言行（564-569）；行 strip 后恰为 ` ``` ` → 闭合，回 NORMAL 并继续消费后续行（570-574）；其余行原样累积（575）。
13. 代码块渲染色：语言→色组映射 `LANG_GROUPS`（7 组 55 组名，55-66），`COLOR_MAP` 组→CB_LANG_* 色（67-68）；语言名 `strip().lower()`，未命中→`gray`（497-498）。
14. 代码块渲染形态（495-505）：header 行 `{CB_LANG_LABEL}  -- {label} --{R}`（label 空则 "code"）；每行 `f" {C_CODE_BG} {CB_LINE_NO}{i:>3} {R}{color}{stripped}{R}"`——行号从 1 起、宽 3 右对齐、行尾 rstrip；整块一次 `_stdout_write`（913）。
15. 终端宽度探测链（`_terminal_width`，149-164）：env `NARNAT_TERM_WIDTH` 为纯数字 → 直接用它（151-153）；否则 conhost 可见窗口值（155-157）；否则 `shutil.get_terminal_size().columns`，异常回退 120（159-163）；最终 clamp `[20, 160]`（164）。
16. Windows conhost 判定链（85-146）：非 win32 → 0（99-100）；`WT_SESSION` 环境变量存在 → 0（113-114）；`GetConsoleWindow()` 为 0（隐藏/ConPTY）→ 0（115-117）；否则先 GetClientRect 客户区宽 // 字体宽（120-131）；失败再退回 GetConsoleScreenBufferInfo.srWindow（133-143）；全过程异常静默返回 0（144-146）。
17. 宽字符口径：`east_asian_width` ∈ {W,F} 记 2 列；歧义宽度（A 类，如 →≈）按 1 列（197-205，注释给出与终端实际渲染对齐的理由）；宽度计算前剔除 `\x1b[...m` 序列（52、210）。
18. 软折行 `_wrap_cell`（243-293）：先 `max_width<=0` 直接原样返回（255-256）；逐视觉字符累计宽度，超限时当前行行尾补 `\x1b[0m`（纯文本模式 `is_plain()` 为真则跳过，282），新行开头重放活跃 ANSI 序列（284）；`\x1b[0m` 清空活跃序列栈（269-271）；空文本返回 `[""]`（290-292）。
19. 流式并发：`feed`/`flush`/`reset` 均持 `_lock`（554、933、976）；`_process_lines` 在 handler 触发状态切换（返回 True）时把未消费的 rest 放回缓冲，由 `feed` 外层循环换 handler 继续（537-562，避免大批量重入）。
20. 行首 `\r` 剥离：`_on_code_line`/`_on_normal_line` 都执行 `line.replace("\r", "")`（565、579），防终端回车覆盖行首显示。
21. `flush`（917-965）语义：残留非空时——在代码块内交 `_on_code_line`（940-941）；否则若是表格形态（`|` 首尾、≥2 个 `|`）直接按段落渲染（943-953，防"半行被切断的表格片段"污染表格缓冲）；其余交 `_on_normal_line`（954-955）；随后 leftover 二次取缓冲兜底 `render_line` 直出（956-958）；未闭合代码块强制渲染（960-962）；表格缓冲落定（964-965）；「不跨轮保留任何缓冲」（注释 927-928）。
22. `reset`（967-983）：清 buf、`_in_code`、`_code_lang`、`_code_lines`、`_table_rows`、`_table_has_separator`——用于服务端流中断自动重试前，防半行/表头/未闭合代码块与重试流拼接重复（docstring 968-975）。
23. `colorize_diff` 行首判定序列：`---`/`+++`（header）→ `@@`（range）→ `-`（removed）→ `+`（added）→ 其余 context（305-315）；空串与 `[无差异]` 输出 `{C_SECONDARY}[无差异]{R}`（302-303，注意此处不经过 MD_* 而用基础色）。
24. `_sep` 每次调用重新测终端宽度（323-324），宽度永远 ≥20 → 至少 18 个 `─`。

**边界/异常行为**（编号；附行号）：
1. 空/空白行：`render_line` 返回 ""，流式路径不输出（479-480、606-608）。
2. `_fit_widths`：cols<=0 返回 []（439-440）；`avail < cols*min_col` 时均分 `max(1, avail//cols)`（441-443）；舍入余额按列轮转 +1 补偿（452-455）；不超 avail 上界。
3. `_split_cells`：先 `strip("|")` 去首尾竖线再拆分并 strip 各格；`\|` 还原（427-429）。
4. `_is_table_separator`：全空 cells 返回 False；仅对非空 cell 全部匹配 `^[-:]+$` 才为 True（459-462）。
5. 超长/超宽：表格超总宽走 fit/wrap/chunked/降级四路（700-719）；单元格折行保证每物理行 ≤ 列宽（243-293）；`_wrap_cell(max_width<=0)` 直接原样返回（255-256）。
6. 超长表格缓冲：60 行强制落定，避免"长表格期间无输出"（596-599）。
7. 编码/控制符：`\r` 一律剥离（565、579）；ANSI 序列在宽度计算与拆字时被识别为 0 宽单元（52、214-240）。
8. 平台分支：conhost 可见窗口/字体宽度测量仅在 win32 尝试，其余静默回退（85-146 各 return 0 路径、144-146 吞异常）。
9. 终端宽度获取失败：`shutil.get_terminal_size` 异常兜底 120，再 clamp（160-164）。
10. 未闭合代码块（流中断）：`flush` 强制渲染已累积内容不吞（960-962）；`feed` 中已进入 CODE_BLOCK 但后续行全部被缓冲——不丢（574-575 + 960-962）。
11. 表格数据行单元格数不一致：取最大列数补齐短行空串（679-684）；渲染时越界列取 `widths[-1]`/空（746、817-819）。
12. 语言名大小写/空格：统一 `strip().lower()`（497-498、503）；空语言标签回退 "code"（503）。
13. 无分隔行的伪表格（如纯 `|a|b|` 多行）：全部缓冲后随着下一个非表格行触发 demote（段落化），信息不丢（625-627）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `flush(final)` 的 `final` 参数为"历史遗留"（917-934，docstring 自承认"旧实现曾区分最终/非最终行为，现行为已统一，仅保留以兼容调用方"）；UIStreamSession.finish 传 True、flush_renderer 传 False（ui_design.py:236、259）但行为无差异。严重度：中。
2. `CodeBlockRenderer.render(lang, body, width)` 的 `width` 参数函数体内未使用（496-505），调用处仍传 `self.width`（909-912）——死参数 + 死状态。严重度：低。
3. `colorize_diff` 与 `tools/diff_utils.py:9` 重复实现（注释自承认"与 tools/diff_utils.py 逻辑一致，此处供 UI 层使用"，297-298），且本文件版本在 ui_design 中 import 后无消费者。严重度：中。
4. ctypes 手写内存偏移两处（CONSOLE_FONT_INFO 104-131、CONSOLE_SCREEN_BUFFER_INFO 133-143、167-194），结构布局靠注释说明硬编码，无版本/尺寸校验——脆弱边界。严重度：中。
5. 两套 ANSI 处理：正则 `re_ansi`（52）与手写扫描 `_visual_chars`（214-240）。严重度：低。
6. 表格候选行判定式（`|` 首尾 + count>=2）在 472（BLOCK_RULES lambda）、590（_on_normal_line）、949-950（_flush_locked）三处重复字面实现。严重度：低。
7. `line.replace("\r", "")` 在 `_on_code_line`/`_on_normal_line` 重复（565、579）。严重度：低。
8. 模块级可变列表 `BLOCK_RULES`（465-474）为全局策略表——运行时只读但结构上是可变全局。严重度：低。
9. `_demote_to_paragraphs` 用 `_render_paragraph(line, None)` 调用需要 Match 的渲染函数（614-615）——类型契约靠"该函数不读 m"的隐式约定维持。严重度：低。
10. 表格渲染路径分支极多（正常/折行/分块/降级四种形态 × 宽度双测量 × 缓冲上限），为长期补丁堆叠的结果（621-722 注释中多次提及历史回归："表格回归的根源""表格卡乱表"等，596-597、707-710、926-928）。严重度：中。

**可测性**：
- 可独立单测（纯函数）：`InlineRules.render`、`render_line`、`_split_cells`、`_is_table_separator`、`_fit_widths`、`_char_width`、`_display_width`、`_visual_chars`、`_wrap_cell`、`colorize_diff`、`CodeBlockRenderer.render`、各 `_render_*`（除依赖终端/输出者）。
- 可单测但需捕获 stdout：`_sep`、`StreamingRenderer`（feed/flush/reset 全流程可用 StringIO/捕获 sys.stdout 断言输出序列与缓冲状态；表格四路径可用构造输入覆盖；`_lock` 并发可用双线程 stress）。
- 需要集成测试：真实终端下的宽度探测（`_terminal_width`/`_windows_console_window_cols`/`_srwindow_cols` 依赖 conhost 窗口与字体度量）；ConPTY 与管道环境下配色/折行端到端表现。
- 无法自动化：终端 pending-wrap、窗口 resize 竞态、VT 开关被外部子进程重置等真实时序缺陷（历史 bug 均属此类，只能仿真+人工复现）。

---

### narnat_agent/output.py（510 行）

**职责**：终端输出抽象层——ANSI 颜色常量与主题系统（基础色板 + 派生 token + 配方解析 + apply_style 全局生效）、线程安全 stdout 写入、plain/quiet 全局开关、Windows VT 启用与重申、显示开关状态（DisplayState）。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `def set_plain(plain: bool = True) -> None`（32）：切换纯文本模式（headless 启动时调用）。
- `def is_plain() -> bool`（38）：查询纯文本模式（运行时读取，不能 import 值快照）。
- `def set_quiet_tools(quiet: bool = True) -> None`（49）：切换工具调度日志静默。
- `def is_quiet_tools() -> bool`（54）：查询静默开关。
- `def _enable_vt_on_windows() -> None`（63）：Windows 启用 VT 并缓存句柄（模块 import 时调用一次，113）。
- `def _assert_vt() -> None`（92）：写出前重申 VT（防子进程重置 console mode）。
- `def write(text: str) -> None`（116）：持锁写出（非 plain 时前缀 `\r`）。
- `def try_write(text: str) -> bool`（127)：非阻塞写（拿不到锁放弃，返回 False）。
- `def _supports_truecolor() -> bool`（143）：truecolor 能力检测（环境变量 + win32 console mode）。
- `def _ansi_color(code: str, r: int, g: int, b: int) -> str`（190）：按能力生成 truecolor 序列或最近 16 色。
- `def _hex_to_ansi(hex_str: str, bg: bool = False) -> str`（203）：#RRGGBB → ANSI。
- `class _Color`（213）：`__slots__ = ('_value',)`（214）；`def __init__(self, value: str)`（215）；`__str__` 返回 `""`（plain）或 `_value`（216）；`__repr__` 返回 `_value`（217）。
- `def _parse_recipe(value: str) -> str`（265）：配方字符串 → ANSI 序列（token：bold|dim|italic|underline|基础色名|#RRGGBB|bg:xxx）。
- `def _resolve_ptk_style(raw: str) -> str`（376）：含配方 token 的 ptk 样式串 → ptk #hex 格式。
- `def apply_style(ui_config: dict) -> None`（400）：加载 narnat.json "界面" 分组全部颜色/开关配置。
- `class DisplayState`（500）：`show_cost=False`、`show_balance=False`、`max_tokens=128000`、`show_ratio=False`、`context_window=DEFAULT_CONTEXT_WINDOW`（506-510）。
- 数据结构常量：`_ANSI16`（168-187，16 色表带 RGB）、`_BASE_DEFS`（234-246）、`_DERIVED_DEFS`（290-339）；生成变量：`C_*`（249-250，11 个）、`MD_* / CB_* / DIFF_* / UI_* / CMD_*`（342-346，40 个）、旧别名（353-360）、`PTK_PROMPT_SYMBOL/PTK_PROMPT_TEXT/PTK_PROMPT_CUSTOM`（367-369）。

**依赖**（本文件 import 的内部模块）：
- `.config.defaults`（17）：`DEFAULT_CONTEXT_WINDOW`。
- 标准库：os/sys/threading（13-15）；函数内 `import ctypes`（79、101、153）、`import re`（389、458、481）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `main.py:45`：`from narnat_agent.output import set_plain, set_quiet_tools`（46-47 调用）。
- `narnat_agent/core/agent.py:17`：`from ..output import write as _stdout_write, X, R`。
- `narnat_agent/core/agent_loop.py:20`：`from ..output import write as _stdout_write`。
- `narnat_agent/core/auto_save_manager.py:16`：`from ..output import write as _stdout_write, D, E, R`。
- `narnat_agent/core/tool_callbacks.py:9`：`from ..output import write as _stdout_write, B, D, E, G, R, Y, is_quiet_tools`（31 检查静默）。
- `narnat_agent/core/tool_dispatcher.py:18`：`from ..output import write as _stdout_write, D, R, X, is_quiet_tools`（412、529、538 检查静默）。
- `narnat_agent/tools/diff_utils.py:6`：`from ..output import RST as R, BLD as B, DIM as D, GRY as G, CYN as C, GRN as E, RED as X`。
- `narnat_agent/ui/colors.py:9-42`：四段 re-export import。
- `narnat_agent/ui/headless.py:9`：`from ..output import write as _stdout_write`。
- `narnat_agent/ui/renderer.py:36`：`from ..output import is_plain`（282 消费）。
- `narnat_agent/ui/ui_design.py:43` `_stdout_lock, write, try_write`；`:130` `from .. import output as _output`（show_stats 动态读 DisplayState）；`:395` PTK 常量；`:424` R。

**状态**：
- 模块级全局（全部可变，读写方见括号）：
  - `_stdout_lock`（24，threading.Lock；写：无；读：write 117、try_write 128）。
  - `_PLAIN`（29；写：set_plain 34、main.py:46；读：is_plain 40、`_Color.__str__` 216、write 118）。
  - `_QUIET_TOOLS`（46；写：set_quiet_tools 50、main.py:47；读：is_quiet_tools 55、core/tool_callbacks.py:31、core/tool_dispatcher.py:412/529/538）。
  - `_vt_handle`（59；写：_enable_vt_on_windows 87；读：_assert_vt 98/104-107）。
  - `_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004`（60，常量）。
  - `_TRUECOLOR`（165，导入期快照；读：_ansi_color 191）。
  - `_ANSI16`（168-187，导入期构造后只读）。
  - `RST/BLD/DIM/R/B/D`（224-227，_Color 实例；apply_style 不改它们）。
  - `_BASE_COLORS`（253；写：apply_style 418/420/442/444；读：_parse_recipe 278/282、_resolve_ptk_style 388/390、apply_style 417/437/441/457/482）。
  - `_BASE_HEX`（255；写：apply_style 411-412/421/432/436/453/456；读：_resolve_ptk_style 386、apply_style 429/433-435/454-456/477）。
  - `_DERIVED`（342；写：346 初始化、apply_style 468；读：468）。
  - `C_*` 11 个、`MD_*/CB_*/DIFF_*/UI_*/CMD_*` 40 个 _Color 实例（其 `_value` 被 apply_style 多处改写：418/442/452/455/461-462/468）——"可变全局 + 引用共享"是主题生效机制。
  - `PTK_PROMPT_SYMBOL/TEXT/CUSTOM`（367-369；写：apply_style 405/472-486；读：ui_design.py:395-398）。
- 类变量：`DisplayState` 五个类属性（506-510；写：apply_style 489-493；读：ui_design.py:131-135、agent 侧未验证其它读点）。

**行为要点**（编号；附行号）：
1. plain 模式三重效果：`_Color.__str__` 恒为空串（216）→ 所有拼接出的 ANSI 消失但结构保留；`write` 不补行首 `\r`（118-120）；不执行 VT 重申（119）。`try_write` 无 plain 分支（128-136）——plain 下动画线程不会启动（HeadlessStream 无 spinner），实际无影响（未验证其它调用点）。
2. VT 启用：模块导入时执行一次 `_enable_vt_on_windows()`（113，注释说明必须在 `_supports_truecolor()` 之前让检测看到 VT）；`write` 每次写出前置 `\r` 前重申（122）；`try_write` 同样重申（130）。
3. VT 重申逻辑（92-109）：`_vt_handle is None` 直接跳过（无控制台/非 Win/管道）；GetConsoleMode 成功但缺 VT 位才 SetConsoleMode；异常静默。
4. truecolor 检测链（143-162）：`COLORTERM` ∈ {truecolor, 24bit} → True；有 `WT_SESSION` → True；`ConEmuANSI == "ON"` → True；win32 且 console mode & 0x0004 → True；否则 False。
5. 非 truecolor 降级（190-200）：欧氏距离在 16 色表（含亮色）中找最近邻；`code == "48"` 选背景码否则前景码。`_ANSI16` 值采用 xterm 兼容值（168-187）。
6. 配方解析 `_parse_recipe`（265-283）：空串/全空白返回 ""；按空白分词；样式词查 `_STYLE_MAP`（bold/dim/italic/underline，262）；`bg:` 前缀后接 `#hex` → 背景色，否则按基础色名查 `_BASE_COLORS`（未命中取 `_Color("")._value` == ""）；`#` 开头 → 前景 truecolor/16 色；基础色名 → 其 ANSI 值；**未知 token 静默忽略**（无 else 分支）。
7. 派生 token 解析：模块导入时 `_DERIVED[_key]` 与全局变量指向同一 `_Color` 实例（343-346）——apply_style 直接改实例 `_value`（468），所有拿到该实例的模块自动看到新色（"全局生效"机制）。
8. `apply_style(ui_config)` 五大步（400-493）：
   ① 基础色板：先把 `_BASE_HEX` 全部恢复默认（409-412，保证 `apply_style({})` 完整重置）；遍历 `colors` 分区，跳过 `_` 前缀与非 `#` 开头的值（413-415）；`_hex_to_ansi` 后写回已有 `_BASE_COLORS` 实例或新建（416-421）。
   ② `base_colors` 分区：角色 → 调色板引用（如 "用户": "纯白"）；目标值优先 `colors` 直查 hex（429-432）、再 `_BASE_HEX`（433-436）、再 `_BASE_COLORS` 取值（437-438），全未命中则跳过（439-440）。
   ③ `codeblock.background` → `C_CODE_BG`（447-462）：按 hex/名字/已有实例三分支，后会用 re 从 ANSI 反推 RGB 重建背景码（458-462）。
   ④ 派生 token 循环（465-468）：用户配置优先（`ui_config[section][short_key]`），未配置用默认配方**重解析**以跟随基础色更新。
   ⑤ PTK 样式（471-486）：`prompt.symbol/text/custom` → `_resolve_ptk_style`；未显式设置 `prompt.text` 时默认跟随 `_BASE_HEX["user"]`（474-485）。
   ⑥ DisplayState 五开关（489-493）：`show_cost/show_balance/max_output_tokens/show_ratio/context_window`，类型强转 bool/int。
9. `write` 的行协议（116-124）：持 `_stdout_lock`；非 plain 输出为 `"\r" + text`（依赖调用方 text 以 `\n` 结尾；`\r` 用于 prompt_toolkit 退出复位后的行首对齐）；每次 flush。
10. `try_write`（127-136）：`acquire(blocking=False)` 失败直接返回 False（动画掉帧容错）；成功路径 finally 释放。
11. `_Color` 求值：`str(color)` 动态读 `_PLAIN`（216），**repr 恒返回 ANSI 原文**（217）——日志/调试打印不受 plain 影响。
12. `apply_style` 幂等性：重复调用等价于重置后重放（基础色 409-412 重置保证）。

**边界/异常行为**（编号；附行号）：
1. 非 Windows / 无控制台（管道、重定向）：`_enable_vt_on_windows` 直接 return 或句柄无效（76-83），`_vt_handle` 保持 None，`_assert_vt` 全程跳过（98-99）——ANSI 原样输出（供重定向消费）。
2. `_supports_truecolor` 各项检测失败（ctypes 异常）：静默 False（160-161）。
3. `_parse_recipe` 对空值（270-271）、未知 token（循环无 else）、`bg:` 后带未知名（278）均静默容错（不抛异常、产出可能缺色的序列）。
4. `apply_style` 对畸形配置：`colors` 值非 `#` 开头跳过（414）；`base_colors` 引用解析失败跳过当前项（439-440）；`codeblock.background` 三分支全不匹配则不改（447-462 无 else）；`max_output_tokens/context_window` 用 `int()` 强转——**非数字会抛 ValueError**（491、493，未见捕获，未验证上游是否保证类型）。
5. `_hex_to_ansi` 对非法 hex（长度不足/非 hex 字符）：`int()` 会抛异常（204-205）；调用链上游（apply_style）无捕获（未验证配置 loader 是否前置校验）。
6. `_resolve_ptk_style`：token 命中 `_BASE_HEX` 用其 hex；命中 `_BASE_COLORS` 时用 re 从 ANSI `2;r;g;b` 反推 hex，匹配失败退回原 token 字面量（390-394）；其余 token（含样式词之外的未知词）原样透传（395-396）——ptk 对未知 token 的行为交给 ptk。
7. `code_bg` 出现在 `_BG_LOOKUP`（True，254），apply_style 基础色应用时天然按背景生成（416）。
8. 16 色降级模式下 `_Color._value` 变成 `\x1b[3Xm`/`\x1b[4Xm`——`_resolve_ptk_style` 的 re 反推将匹配失败，PTK_PROMPT_TEXT 可能保持 token 字面量而非 hex（390-394，未验证下游 ptk 的容错）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `apply_style` 单体巨型函数（400-493，94 行）：串行改写 5 组模块级可变容器 + PTK 全局 + DisplayState，职责混杂（配置解析/颜色计算/状态归位全在一处）。严重度：高。
2. `_resolve_ptk_style` 从已生成的 ANSI 字符串正则反推 RGB（390-392 `re.search(r"2;(\d+);(\d+);(\d+)", _BASE_COLORS[tok]._value)`）——字符串往返解析，脆弱且信息有损（16 色降级形态必失败）。严重度：中。
3. 函数内 `import re` 三处（389、458、481）与 `import ctypes` 三处（79、101、153）——局部 import 散布。严重度：低。
4. 旧别名区声明 deprecated 但保留（349-360，含 G/C/E/Y/X/U/M/O/W7 及 GRY/CYN/... 两套高频别名），是 core/tools 仍在消费的历史包袱（tools/diff_utils.py:6 即消费旧名）。严重度：中。
5. `apply_style` 的 "1b" 分支在 ref 命中 `_BASE_COLORS` 时直接复制 `_value`（437-438）——不重算 bg，对背景类角色可能沿用前景码（脆弱边界）。严重度：低。
6. `codeblock.background` 分支（446-462）与基础色逻辑部分重复（同样的 hex→ansi、名字→hex、ANSI→RGB 三条解析路径再次实现）。严重度：低。
7. `_Color.__str__` 动态读模块全局 `_PLAIN`（216）——渲染器/命令层拼接字符串时颜色与 plain 状态的耦合是隐式的（无法从调用点看出）。严重度：中。
8. 模块导入副作用：`_enable_vt_on_windows()`（113）在 import 期改控制台状态；`_TRUECOLOR`（165）导入期定死——测试需在 import 前布局环境变量。严重度：中。
9. `write()` 每次输出隐性前置 `\r`（123）——只对 prompt_toolkit 交互场景正确的输出协议，对 headless/重定向由 plain 模式绕开（118-120），两条路径行为分叉。严重度：低。

**可测性**：
- 可独立单测（纯函数）：`_parse_recipe`、`_hex_to_ansi`、`_ansi_color`（需固定 `_TRUECOLOR`）、`_resolve_ptk_style`、`set_plain/is_plain`、`set_quiet_tools/is_quiet_tools`、`apply_style`（前后断言 `_BASE_COLORS/_DERIVED/PTK_*/DisplayState` 值；注意它会改全局，测试需重置或子进程隔离）。
- 可单测但需捕获 stdout 且 mock 平台：`write`/`try_write`（`_PLAIN` 两分支、锁竞争用双线程）；`_supports_truecolor`（monkeypatch os.environ 与 sys.platform；`_TRUECOLOR` 为导入期快照，只能在 import 前 setup）。
- 需要集成测试：真实 Windows 控制台下的 VT 启用/重申（含子进程重置 console mode 的回归场景）；truecolor 与 16 色降级在实际终端中的观感。
- 无法自动化：VT mode 被外部程序（cmd.exe/ssh 子进程）重置的时序（历史注释 70-73 描述实测 mode=0x3/0x7 漂移），只能仿真注入。

---

### narnat_agent/ui/ui_design.py（428 行）

**职责**：UI 总接口——输入读取（prompt_toolkit 会话与键绑定）、命令分发入口、流式输出会话（UIStreamSession）生命周期编排（spinner/渲染器/flush/统计栏/中断联动）、动画线程（思考中/压缩/合并）。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `def show_header(msg: str) -> None`（58）：头部横幅 + 分隔线。
- `def _join_thread(t: threading.Thread, max_wait: float = 1.0) -> None`（63）：带期限的线程等待。
- `def _animation_thread(stop: threading.Event, label: str, delay: float = 0.0) -> None`（70-71）：通用 4 帧动画线程。
- `def _spinner_thread(stop: threading.Event) -> None`（106）：思考中动画（延迟 666ms）。
- `def _compress_thread(stop: threading.Event) -> None`（111）：压缩动画。
- `def _summary_thread(stop: threading.Event) -> None`（116）：合并动画。
- `def show_interrupted() -> None`（121）：'已打断/继续...' 提示。
- `def show_stats(input_tokens: int, output_tokens: int, cache_ratio: float = 0.0, cost: float = 0.0, balance: float = 0.0, thinking_effort: str = "高") -> None`（125-128）：统计栏。
- `class UIStreamSession`（160）：
  - `def __init__(self) -> None`（172）
  - `def _start_spinner(self) -> None`（181）
  - `def _stop_spinner(self) -> None`（191）
  - `@property def cancelled(self) -> bool`（204-206）
  - `@property def aborted(self) -> bool`（208-210）
  - `def begin(self) -> None`（212）
  - `def feed(self, chunk: str) -> None`（215）
  - `def pause_spinner(self) -> None`（221）
  - `def flush_renderer(self) -> None`（229）
  - `def reset_renderer(self) -> None`（238）
  - `def resume_spinner(self) -> None`（243）
  - `def finish(self, input_tokens: int = 0, output_tokens: int = 0, cache_ratio: float = 0.0, cost: float = 0.0, balance: float = 0.0, thinking_effort: str = "高", with_stats: bool = True) -> None`（253-256）
  - `def abort(self, message: Optional[str] = None) -> None`（263）
- `class UIInterface`（279）：
  - `def __init__(self, model_name: str = "narnat", session_manager=None, data_dir: str = "") -> None`（282-283）
  - `def start(self) -> None`（293）
  - `def read_input(self) -> Optional[str]`（298）
  - `def read_input_with_prompt(self, prompt_text: str) -> Optional[str]`（309）
  - `def dispatch_command(self, cmd: str, args: str) -> CommandResult`（321）
  - `def create_stream(self) -> UIStreamSession`（324）
  - `def on_interrupted(self) -> None`（330）
  - `def begin_compressing(self) -> None`（334）
  - `def end_compressing(self) -> None`（341）
  - `def begin_summarizing(self) -> None`（350）
  - `def end_summarizing(self) -> None`（357）
- `def _make_keybindings() -> KeyBindings`（370）：Enter 提交、Alt+Enter / Ctrl+O / Alt+J 换行。
- `def _get_prompt_style() -> Style`（394）：ptk Style（prompt 段用 PTK_PROMPT_SYMBOL，默认段 PTK_PROMPT_TEXT）。
- `def _create_session(session_manager, data_dir: str = "") -> PromptSession`（402）：多行 + 补全器 + 键绑定 + FileHistory。
- `def read_input(session: PromptSession) -> Optional[str]`（415）：`session.prompt([("class:prompt", "# ")])`。
- `def read_input_with_prompt(session: PromptSession, prompt_text: str) -> Optional[str]`（422）：`session.prompt(ANSI(f"{C_PRIMARY}{prompt_text}{R}"))`。

**依赖**（本文件 import 的内部模块）：
- `.colors`（33-42）：re-export 一大批常量（见 colors.py 节）。
- `..output`（43）：`_stdout_lock, write as _stdout_write, try_write as _stdout_try_write`；函数内（130）：`from .. import output as _output`；函数内（395）：PTK 常量；函数内（424）：R。
- `.interrupt`（44）：`InterruptController, _interrupt_ctrl`（InterruptController 名字未被使用）。
- `.renderer`（45-48）：colorize_diff、_sep、_terminal_width、_display_width、InlineRules、BlockRule、CodeBlockRenderer、StreamingRenderer、render_line（其中仅 _sep、StreamingRenderer 在正文使用）。
- `.session_commands`（49）：`_CommandCompleter, _dispatch_command, CommandResult`。
- 外部：prompt_toolkit（20-24），os/sys/threading/time（14-17）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/assembly.py:27`：`from .ui.ui_design import UIInterface, apply_style`（apply_style 在 48 调用；UIInterface 在 144 构造）。
- `narnat_agent/core/agent_loop.py:18`：`from ..ui.ui_design import UIInterface`（30 类型注解）。
- `narnat_agent/core/compression_coordinator.py:15`：`from ..ui.ui_design import UIInterface`（38 注解）。
- 运行期接口调用（实例注入，非 import）：
  - `core/agent.py`：43 `start`、49 `read_input`、65 `dispatch_command`、117/156/226/254 `create_stream`、123 `on_interrupted`。
  - `core/agent_loop.py`：117/212/235/294 `on_interrupted`、443 `read_input_with_prompt`、464 `create_stream`。
  - `core/compression_coordinator.py`：50/55/71/82/86/101/138 `end_compressing`、60/90/124 `begin_compressing`。
  - `assembly.py:147-148`：`summary_anim_start/stop = lambda: ui.begin_summarizing()/end_summarizing()`。
  - UIStreamSession 方法面（同经注入）：`core/agent.py`:124/130 `abort`、132/141/159/236 `aborted`、231；`core/agent_loop.py`:88-95/132/146/172/188/193/298/329/365/382 `feed/begin/flush_renderer/reset_renderer`、110-112 `cancelled`、116/210/234/293 `abort`、156/176/199/301/330/400/432 `finish`；`core/tool_dispatcher.py`:137-326 大量 `cancelled` 检查、222-223 `pause_spinner+flush_renderer`、272 `resume_spinner`。

**状态**：
- 模块级全局：`_ANIMATION_FRAME_INTERVAL = 0.15`（56，常量）。
- 类变量：无（UIStreamSession/UIInterface 均实例状态）。
- 实例状态：
  - UIStreamSession：`_renderer: StreamingRenderer`（173）、`_spinner_stop: threading.Event`（174）、`_spinner_thread: Optional[Thread]`（175）、`_started`（176，仅 flush_renderer 守卫）、`_aborted`（177）、`_spinner_pause_count`（178，并行工具计数）、`_spinner_lock`（179）。
  - UIInterface：`_model`（284）、`_mgr`（285）、`_data_dir`（286）、`_session: Optional[PromptSession]`（287）、压缩动画 `_compress_stop/_compress_thread`（288-289）、合并动画 `_summary_stop/_summary_thread_var`（290-291）。

**行为要点**（编号；附行号）：
1. `show_header`（58-60）：输出 `  {UI_HEADER}{msg}{R}\n` 后立刻 `_sep()`。
2. 动画线程协议（70-104）：delay>0 时先以 50ms tick 轮询等待 stop（79-86）；启动时写 `\x1b[?25l`（隐藏光标，88）；4 帧文本为 `* label   ` → `* label.  ` → `* label.. ` → `* label...`，粗体/暗色交替（90-95）；每帧 `\r  {frame}\x1b[K`，用 `try_write` 非阻塞写（98，拿不到锁丢帧）；帧间隔 `_ANIMATION_FRAME_INTERVAL`=0.15s（100）；退出时清行并恢复光标 `\r\x1b[K` + `\x1b[?25h`（102-103）。
3. 三动画标签：思考中（延迟 0.666s 起，108）、正在压缩（无延迟，113）、正在合并（无延迟，118）。
4. `show_interrupted`（121-122）：`\n  已打断\n  继续...\n`（UI_INTERRUPTED / UI_INTERRUPTED_HINT 两色）。
5. `show_stats`（125-153）：每次调用先动态读取 DisplayState 五开关（130-135）；数字格式化——input/output ≥1000 显示 `x.xk` 否则原值（137-138）；max_tokens ≥1000 显示取整 k（139）；思考强度恒显示（140）；`_sep()` 后拼装：缓存段仅 `cache_ratio > 0` 显示（143-144，上限 100%）；费用段仅 show_cost（145）；余额段仅 show_balance 且 balance>0（146）；窗口占比段仅 show_ratio，`context_window>0 且 input_tokens>0` 显百分比取整，否则 `--`（148-152）。整行结构：`  输入:{si} 输出:{so}{cs}{th}{rt}  最大输出:{mt}`（UI_STATS_LABEL 色）+ 费用/余额（UI_STATS_VALUE 色）（153）。
6. UIStreamSession 正常生命周期：`begin()` 启动 spinner（212-213）→ `feed(chunk)` 首次置 `_started`、停 spinner、转发 `_renderer.feed`（215-219）→ （工具执行期）`pause_spinner` 计数 +1、首个暂停者真正停 spinner（221-227）；`flush_renderer` 仅在 `_started` 后生效（229-236）→ `resume_spinner` 计数 -1、归零才重启（243-251）→ `finish(...)`：先 `_interrupt_ctrl.enter_input_mode()` 停 ESC 轮询（257）→ 停 spinner → `_renderer.flush(final=True)` → 按 with_stats 显示统计栏（253-261）。
7. `abort(message=None)`（263-272）：置 `_aborted=True`（266，阻止后台线程 resume_spinner 重启）→ `enter_input_mode()`（267）→ 停 spinner → message 为空显示默认"已打断"，否则原样输出 message（269-272）。
8. `cancelled` property 直接读进程级中断单例 `_interrupt_ctrl.is_set`（204-206）——流会话的"取消"语义完全由全局中断状态单一来源决定。
9. UIInterface 输入链路：`start()` 先 `enter_input_mode`、打印 header、创建 ptk 会话（293-296）；`read_input`/`read_input_with_prompt` 每次调用 `clear()` + `enter_input_mode()`（302-303、314-315）；返回 None（Ctl+C/EOF）时重建会话对象（305-307、317-319）。
10. `create_stream()`（324-328）：`_interrupt_ctrl.enter_run_mode()`（内部 clear + 启动 ESC 轮询线程）→ 构造 UIStreamSession → `begin()` 启动 spinner。
11. `on_interrupted()`（330-332）：回到输入模式 + 重建 ptk 会话（丢弃可能残留的输入缓冲）。
12. 压缩/合并动画为一对 begin/end（334-363）：begin 创建 Event+daemon Thread 启动；end 置 Event、`_join_thread` 等待、清引用；`end_compressing` 对未启动（None）幂等（342-343）。
13. prompt_toolkit 配置：`multiline=True`（409）；键绑定 Enter=提交、Alt+Enter / Ctrl+O / Alt+J=插入换行（370-391，均 eager）；样式从 output 的 PTK 常量取（394-399）；history 文件 = `{data_dir}/.narnat_history`，data_dir 为空时 `~/.narnat_history`（402-406）；completer=`_CommandCompleter(session_manager)`（410）。
14. 输入提示符：普通输入固定 `# `（417）；自定义提示用 ANSI 包装 C_PRIMARY 色（426，用于删除确认等场景）。
15. win32 下模块导入时 `sys.stdout.reconfigure(encoding="utf-8")`（26-30，失败静默）。

**边界/异常行为**（编号；附行号）：
1. `read_input`/`read_input_with_prompt` 捕获 `(KeyboardInterrupt, EOFError)` 返回 None（418-419、427-428）——None 语义="重开会话/继续"。（在 core/agent.py:50-51 表现为 `continue`；未验证 agent_loop.py `_handle_delete_confirm` 对 None 的处理路径 444-445 已置 `""` → 视为取消。）
2. 非 win32 平台：reconfigure 分支不执行（26 判断）。
3. spinner 重复启动防护：`_start_spinner` 判 `_spinner_thread is not None` 直接返回（183-184）；`_stop_spinner` 对 None 幂等（193-195）。
4. `resume_spinner` 在 `_aborted` 后恒不重启（245-246）；pause 计数用 `max(0, ...)` 防负（248）。
5. `flush_renderer` 在从未 `feed` 过时不动渲染器（235-236）。
6. `end_compressing`/`end_summarizing` 对未启动/已结束幂等（342-343、358-363）。
7. `_join_thread` 最长等 1s，循环 `join(0.1)` 处理短超时残留（63-67）。
8. ptk 会话创建失败（prompt_toolkit 环境缺失，如无 tty）：**未验证**（未见捕获；read_input 的异常面依赖 ptk 自身行为）。
9. `read_input` 中 `assert self._session is not None`（301）——start() 失败时抛 AssertionError（未验证实际场景）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `finish()`/`abort()` 内直接调用全局 `_interrupt_ctrl.enter_input_mode()`（257、267）——流会话与进程级中断控制器隐式耦合，隐藏的状态切换副作用混在输出生命周期里。严重度：中。
2. `_stdout_lock` 导入未使用（43）；`InterruptController` 导入未使用（44）；`colorize_diff, _terminal_width, _display_width, InlineRules, BlockRule, CodeBlockRenderer, render_line` 导入未使用（46-47）——re-export 面与实际消费者（仅 _sep、StreamingRenderer、_CommandCompleter、_dispatch_command、CommandResult 被用）不匹配。严重度：低。
3. `show_stats` 内函数级 `from .. import output as _output` 再逐属性读（130-135）——绕过模块属性快照问题的补偿写法（对比：PTK 常量在 _get_prompt_style 内 395 import、R 在 424 import，三处局部 import 风格不一）。严重度：低。
4. 魔法值：`_ANIMATION_FRAME_INTERVAL=0.15`（56）、spinner 延迟 `0.666`（108）、`_join_thread` 默认 `1.0`（63）。严重度：低。
5. `UIStreamSession._started` 的语义注释"仅用于flush_renderer守卫，不干预spinner"（176）——一个布尔承载双关语义（曾经也控制 spinner 的历史残留）。严重度：低。
6. `finish` 在 `_started=False`（从未 feed）时仍 flush + 显示统计栏（253-261）——行为正确性依赖 renderer.flush 的空缓冲无害性（隐式约定）。严重度：低。
7. UIInterface 的 `session_manager=None`/`data_dir=""` 默认值（282-283）使构造可以不注入会话管理器，但 `dispatch_command` 会直接向 `_dispatch_command` 传 None（321-322）——宽松构造参数是测试友好面，也是默认误用面。严重度：低。

**可测性**：
- 可独立单测（捕获 stdout）：`show_header`、`show_interrupted`、`show_stats`（monkeypatch DisplayState 组合 × 数值区间边界 999/1000、负余额、context_window=0）；`_create_session` 的 history 路径选择；`_make_keybindings` 结构。
- 可单测但需伪 renderer/伪中断：UIStreamSession 全状态机（started/aborted/pause_count 守卫、finish 顺序：enter_input_mode→stop_spinner→flush→stats 的调用序列断言）；UIInterface.read_input 的"None→重建会话"分支（stub PromptSession）。
- 需要集成/时序测试：spinner 线程实际帧输出（0.15s 节奏、666ms 延迟、光标隐藏恢复序列）；_join_thread 的短超时残留场景。
- 无法自动化：prompt_toolkit 真终端交互（按键、补全菜单渲染、Alt+Enter 等键序列）——需人工或 pexpect 类工具（未验证本项目是否有此类设施）。

---

### narnat_agent/ui/session_commands.py（407 行）

**职责**：会话管理命令的实现与分发（/save /ls /cd /rm /skill /compact /explore /done /goal /thinking /thinkback /mode /clear /exit）+ Tab 补全（命令名、会话名树、技能层级、静态选项）。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `class CommandResult(IntEnum)`（25）：`UNKNOWN = 0`、`HANDLED = 1`、`EXIT = 2`（27-29）。
- `class _CommandCompleter(Completer)`（36）：
  - 类属性 `_NAME_COMMANDS = {"/cd": "on_list_names_tree", "/rm": "on_list_rm_names", "/thinking": "on_list_thinking_options", "/mode": "on_list_model_names"}`（39-44）；`_STATIC_OPTIONS = {"/ls": ["--all"]}`（46-48）。
  - `def __init__(self, mgr)`（50-51）
  - `def get_completions(self, document, complete_event)`（53，generator）
  - `def _skill_completions(self, word: str)`（108，generator）
- `def _register(name: str)`（159）：命令注册装饰器。
- `def _require_args(args: str, hint: str) -> Optional[str]`（167）：缺失参数时打印 hint 并返回 `"missing_args"`。
- 命令处理器（签名统一 `(args: str, mgr) -> CommandResult`）：`_cmd_clear`（176）、`_cmd_compact`（182）、`_cmd_explore`（203）、`_cmd_done`（216）、`_cmd_goal`（226）、`_cmd_save`（277）、`_cmd_ls`（292）、`_cmd_cd`（302）、`_cmd_skill`（314）、`_cmd_rm`（326）、`_cmd_thinking`（339）、`_cmd_thinkback`（346）、`_cmd_mode`（354）、`_cmd_exit`（366）。
- `def _dispatch_command(cmd: str, args: str, mgr) -> CommandResult`（385）：统一分发入口。
- 模块级注册表 `_commands: Dict[str, Callable]`（156）。

**依赖**（本文件 import 的内部模块）：
- `.colors`（20-22）：`R, CMD_SUCCESS, CMD_ERROR, CMD_HINT, CMD_HIGHLIGHT, CMD_MUTED, _stdout_write`。
- 外部：`prompt_toolkit.completion.Completer/Completion`（18）、enum/typing（15-16）。
- mgr 鸭子接口（调用的 SessionManager 方法，定义在 core/session_callbacks.py）：`available_commands()`、`on_save/on_show/on_enter/on_delete/on_explore/on_done/on_compact/on_skill/on_thinking/on_thinkback/on_mode/on_exit`、`on_list_names_tree/on_list_rm_names/on_list_thinking_options/on_list_model_names/on_list_skill_tree`、`is_child_session()`、`should_exit_agent()`，以及**私有属性** `_goal_enabled/_goal_max_rounds/_goal_default_rounds/_set_goal_tool`（242-268）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/ui/ui_design.py:49`：`from .session_commands import _CommandCompleter, _dispatch_command, CommandResult`（_CommandCompleter 用于 410，_dispatch_command 用于 322，CommandResult 用于返回注解 321）。
- 无其它生产模块引用。

**状态**：
- 模块级全局：`_commands: Dict[str, Callable]`（156，装饰器注册期写入 162；`_dispatch_command` 读取 404）。
- 类变量：`_CommandCompleter._NAME_COMMANDS`（39-44）、`_STATIC_OPTIONS`（46-48）——恒定。
- 实例状态：`_CommandCompleter._mgr`（51，只读引用）。命令处理器无状态。

**行为要点**（编号；附行号）：
1. 命令可用性由**会话状态**决定（`mgr.available_commands()` 返回 `{命令: 描述}` dict）——三种状态矩阵（定义于 core/session_callbacks.py:95-109 / 202-217 / 406-419）：
   - NoSession（游离态）：/clear /compact /save /ls /cd /rm /skill /thinking /thinkback /mode /goal /exit。
   - RootSession：上述 + /explore，/rm 语义为"删除子会话"。
   - ChildSession：/clear /compact /ls /cd /skill /thinking /thinkback /mode /goal /done /exit（无 /save /rm /explore）。
2. 补全逻辑（53-106）：仅当输入以 `/` 开头（55-56）；`num_parts==1 且不以空格结尾` → 命令名前缀补全，补全片段 `cmd[len(word):]`、显示 meta 描述（62-71）；`/skill` 特判走 `_skill_completions`（75-77）；`_NAME_COMMANDS` 内的命令：刚输入完命令+空格 → 列出全部候选名（80-82）；`num_parts==2 且未以空格结尾` → 按 prefix 补全，prefix 含 `/` 时只补最后一段（`start_position=-len(child_prefix)`，84-96）；静态选项 `/ls --all`（104-106）。
3. 技能补全 `_skill_completions`（108-149）：参数分隔符 `\` 兼容为 `/`（115）；已输入目录层级逐层下钻树（123-132）；目录项补全加尾 `/` 且 meta "目录"（143-145）；`single` 目录与 .md 叶子裸名显示，meta 按 origin 显示"系统技能/项目技能"（146-149）；名字已完整输入时——目录补 `/`（meta "进入目录"），文件不产出空补全（137-142）。
4. 注册表分发：处理函数经 `@_register("name")` 注册进 `_commands`（159-164）——新增命令无需改 `_dispatch_command`（docstring 11-12）。
5. `_cmd_clear`（175-178）：输出 `\033[2J\033[H` 清屏，恒 HANDLED。
6. `_cmd_compact`（181-199）：有参数提示用法；调 `mgr.on_compact()` 返回 `(status, text)`；内部异常兜底为错误提示（189-193）；`status=="error" 且 text 含"取消"` → 用 CMD_MUTED（用户主动取消非错误，194-195）；颜色映射 `{"ok": CMD_SUCCESS, "empty": CMD_HINT}`，其余 CMD_ERROR（197）。
7. `_cmd_goal`（225-273）：`/goal on [N]`——`rest` 非空时 int 解析，非法→"无效轮数"、<1→"轮数必须为正整数"（234-241）；置 `mgr._goal_enabled=True`、`mgr._goal_max_rounds=override`（242-243）；`getattr(mgr,'_set_goal_tool',None)` 真时注入 GoalComplete 工具（245-246）；提示含"本轮上限 N 轮"或纯开启（247-251）。`/goal off`——记录 was_enabled、置 False、轮数清 0、移除工具，按原状态提示两条文案之一（253-263）。无参查状态：limit = 临时覆盖 or 配置默认 or "默认"（266-270）。
8. `_cmd_save`（276-288）：转发 `mgr.on_save(args)`；成功且有参显示 args，无参显示 `mgr.state.session_name()`（285-287）。
9. `_cmd_ls`（291-298）：转发 `mgr.on_show(args)`；空结果提示"(无已保存会话)"。
10. `_cmd_cd`（301-310）/`_cmd_skill`（313-322）/`_cmd_rm`（325-335）：`_require_args` 守卫 → 转发 mgr → 成功/失败两分支文案；/rm 成功提示"已标记删除...(退出agent时生效)"。
11. `_cmd_thinking`（338-342）/`_cmd_thinkback`（345-350）：转发 mgr 结果并统一 CMD_HIGHLIGHT 输出。
12. `_cmd_mode`（353-362）：结果字符串前缀判定——`"无效值"`→CMD_ERROR；`"设置成功"`→CMD_SUCCESS；否则 CMD_HIGHLIGHT。
13. `_cmd_exit`（365-378）：先取 `was_child`（367）→ `mgr.on_exit()` → `should_exit_agent()` 真则返回 `CommandResult.EXIT`（371-372）；否则 was_child 时提示"已暂离探索分支(/cd 回来继续)"，否则"已退出会话"。
14. `_dispatch_command`（385-407）：`cmd.lower().lstrip("/")` 归一（390）；`clear` 特判恒可执行（391-392）；`mgr is None` → UNKNOWN（393-394）；`goal` 特判绕过可用性校验（395-396）；`available_commands()` 不含 `/{cmd}` → UNKNOWN（397-402）；注册表查无 → UNKNOWN（404-406）；否则调用处理器。

**边界/异常行为**（编号；附行号）：
1. 缺参命令统一经 `_require_args` 打印提示并返回 HANDLED（不 UNKNOWN）——如 `/cd` 无参（303-304）、`/skill`（315）、`/rm`（327）、`/explore`（204）。
2. `/clear` 不依赖 mgr（391-392 在任何可用性校验之前）——游离/会话中/headless 均恒可用（但 headless 无命令交互）。
3. `/goal` 不校验 available_commands（395-396）——任何状态可用。
4. cmd 大小写与斜杠：`lstrip("/")` 全剥前导斜杠、lower 归一（390）；`"///save"` 也会命中 save（未验证是否有语义要求）。
5. `/goal on` 后带多个 token（如 "on 5 6"）：`int("5 6")` 抛 ValueError → 显示"无效轮数: 5 6"（234-237）。
6. 补全对 `num_parts > 2` 只有 `/skill` 有响应（75-77 提前 return；其它命令静默无补全）。
7. 名称补全 prefix 含 `/` 时，仅当候选名同时满足 `startswith(prefix)` 且自身含 `/`（90）。
8. `_cmd_mode` 对 mgr 返回的任意非前缀匹配文案回退 CMD_HIGHLIGHT（360-361）——新文案漂移不会报错，只是着色不同（魔法字符串脆弱面）。
9. `_cmd_exit` 中 `on_exit()` 返回非空时先打错误再继续走 EXIT 判定（369-372）——错误提示与退出可能同帧发生（未验证 mgr 的实际返回组合）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `_cmd_goal` 直接写 `mgr._goal_enabled`/`mgr._goal_max_rounds` 私有属性（242-243、254-256、266-268），并用 `getattr(mgr, '_set_goal_tool', None)`/`getattr(mgr, '_goal_default_rounds', 0)` 防御性探测（245、258、268）——命令层跨越多层直捣会话管理器内部状态。严重度：高。
2. `_dispatch_command` 为 clear/goal 保留特判分支（391-396），使注册表中 `@_register("clear")`（175）与 `@_register("goal")`（225）的条目成为**从不经注册表使用的死条目**（handler 走 `_cmd_clear`/`_cmd_goal` 直调）。严重度：中。
3. `_cmd_compact` 用中文子串 `"取消" in text` 判定用户取消（194）、`_cmd_mode` 用 `startswith("无效值")/("设置成功")` 判定结果类别（356-359）——跨模块魔法字符串协议（文案即接口）。严重度：中。
4. `CommandResult` 注释"继承 int，与旧代码完全兼容"（26）——曾经历从 int 常量到 IntEnum 的迁移，int 兼容是历史包袱面（core/agent.py:66/73 直接与 2/1 数字比较，未验证是否刻意）。严重度：低。
5. `_dispatch_command` 中 `available = mgr.available_commands()` 后遗留双空行（397-399）。严重度：低。
6. `_cmd_clear` 硬编码 ANSI `\033[2J\033[H`（177）绕过 _Color/plain 体系——headless 下若被调用会输出裸转义（当前 headless 无命令路径，潜在面）。严重度：低。
7. `_CommandCompleter` 的多分支 `num_parts` 判定（62-106）与 prefix/`start_position` 计算耦合在单一方法内，`_NAME_COMMANDS` 通过 getattr 动态派发（79）——字符串取方法名，静态不可查。严重度：低。
8. `/goal` 的参数解析只认 `"on"` 或 `"on "` 前缀（229）——`/goal on5` 落入"查状态"分支（266），语义不明确（潜在误用面）。严重度：低。

**可测性**：
- 可独立单测：全部 `_cmd_*`（构造 stub mgr 断言 stdout 文案与返回值）；`_dispatch_command`（stub available_commands 测 clear/goal/UNKNOWN/正常五分支）；`_CommandCompleter.get_completions`（feed Document 文本，断言 Completion 序列；树形 mgr stub 覆盖技能层级）。
- 需要集成测试：与真实 SessionManager 的联动（on_save/on_exit 实际状态迁移、available_commands 状态矩阵）——建议作为会话状态机集成用例。
- 无法自动化：无（补全器可纯逻辑驱动；命令均无终端交互依赖，除 prompt_toolkit import 本身）。

---

### narnat_agent/ui/interrupt.py（278 行）

**职责**：ESC/SIGINT 中断控制器——RUN_MODE 期间以轮询线程监听 ESC 键，触发时"切断大动脉"（置中断事件 + 关 LLM 连接 + 杀全部活跃子进程）；INPUT_MODE 停止轮询；含 Windows（msvcrt / ReadConsoleInput）与 Unix（termios+select）两平台键盘路径。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `def _try_restore_term(fd: int, settings) -> None`（16）：atexit 恢复终端设置（吞异常）。
- `def _on_esc_detected(ctrl: "InterruptController") -> None`（25）：ESC 触发立即动作（轮询线程内）。
- `class InterruptController`（50）：
  - `def __init__(self) -> None`（56）
  - `@property def is_set(self) -> bool`（63-65）
  - `def clear(self) -> None`（67）
  - `def enter_input_mode(self) -> None`（70）
  - `def enter_run_mode(self) -> None`（89）
  - `def _poll_esc(self, stop: threading.Event) -> None`（112）
  - `def _poll_esc_windows(self, stop: threading.Event) -> None`（119）
  - `def _poll_esc_windows_native(self, stop: threading.Event, msvcrt) -> None`（139）
  - `def _poll_esc_windows_coninput(self, stop: threading.Event, kernel32) -> None`（165）
  - `def _poll_esc_unix(self, stop: threading.Event) -> None`（217）
- 模块级单例 `_interrupt_ctrl = InterruptController()`（278）。

**依赖**（本文件 import 的内部模块）：
- `..core.interrupt`（37，函数内延迟）：`abort_request`（core/interrupt.py:19）。
- `..tools.bash`（38，函数内延迟）：`kill_active`（tools/bash/__init__.py:233）。
- `..tools.terminal`（39，函数内延迟）：`kill_active_exec as _kill_term`（tools/terminal/__init__.py:158）。
- `..tools.serial`（40，函数内延迟）：`kill_active_exec as _kill_serial`（tools/serial/__init__.py:152）。
- 外部：atexit/os/sys/signal/threading/time（7-12）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/assembly.py:28`：`from .ui.interrupt import _interrupt_ctrl`（131 `cancel_check=lambda: _interrupt_ctrl.is_set`）。
- `narnat_agent/core/compression_coordinator.py:16`：`from ..ui.interrupt import _interrupt_ctrl`（65/95/129 cancel_check；123 `enter_run_mode`；140 `enter_input_mode`）。
- `narnat_agent/ui/ui_design.py:44`：`from .interrupt import InterruptController, _interrupt_ctrl`（206、257、267、294、302-303、314-315、325、331 使用）。

**状态**：
- 模块级全局：`_interrupt_ctrl`（278，单例；其自身状态见下）。
- 实例状态（InterruptController）：`_interrupt: threading.Event`（57，中断标志）、`_stop_poll: threading.Event`（58，轮询停止信号）、`_poll_thread: Optional[Thread]`（59）、`_saved_sigint`（60，**永不赋值，恒 None**）、`_atexit_registered`（61，atexit 去重）。

**行为要点**（编号；附行号）：
1. 状态机 INPUT_MODE ↔ RUN_MODE，由 `enter_input_mode`（70-87）/`enter_run_mode`（89-110）显式切换。RUN_MODE 才有轮询线程。
2. `enter_input_mode`（70-87）：置 stop、若线程存活 `join(timeout=0.2)`（不等同步退出，避免阻塞输入界面回归，73-74）、清线程引用、`_interrupt.clear()`（76）；主线程里处理 SIGINT——`_saved_sigint` 非 None 则恢复，否则 frozen 忽略 SIGINT、非 frozen 用 default_int_handler（77-87）。
3. `enter_run_mode`（89-110）：先 clear 中断（90）；停旧轮询线程（92-94）；**为每个轮询线程新建独立 stop Event**（96，防旧线程被新线程 clear 唤醒）；创建 daemon 轮询线程启动（97-99）；SIGINT 处理块与 enter_input_mode 逐行相同（100-110）。
4. ESC 检测平台分派（112-117）：win32 → Windows 路径，否则 Unix 路径。
5. Windows 路径再分派（119-137）：GetStdHandle(-10) 有效且 GetConsoleMode 成功 → 原生 conhost 用 msvcrt（127-132）；否则（Windows Terminal/ConPTY）用 ReadConsoleInputW（133-135）；import/OS 错误静默（136-137）。
6. Windows native 轮询（139-163）：每轮 `msvcrt.kbhit()` 检查（143）；读到 `\x1b` 后 sleep 0.02 再查 kbhit——无人跟随即是单 ESC → `_on_esc_detected` 并退出线程（146-149）；有人跟随则是转义序列 → 最多再消费 5 字节（151-156，防连按 ESC 后新输入被当序列尾巴吞）；消费窗口内再出现 `\x1b` → 判定连按 ESC → 立即打断（157-160）；异常 OSError 退出（161-162）；轮询周期 stop.wait(0.03)（163）。
7. Windows coninput 轮询（165-215）：一次读 8 条 INPUT_RECORD（177、188-189）；`WaitForSingleObject(handle, 50)` 50ms 等待（183-185）；逐条解析——EventType==KEY_EVENT(1)、bKeyDown 非 0、wVirtualKeyCode==0x1B 时触发（193-211）；异常静默退出（213-214）；周期 stop.wait(0.02)（215）。
8. Unix 轮询（217-274）：`termios.tcgetattr` + `tty.setcbreak` 进 cbreak 单字符模式（229-230）；atexit 兜底恢复原设置（仅注册一次，233-235）；设置失败（管道输入等）直接返回（236-237）；循环 select 30ms（243）；单 ESC（后 10ms 无数据）→ 打断（246-251）；转义序列消费上限 5 字节、双 ESC 立即打断（253-265）；finally 恢复终端（268-274）。
9. `_on_esc_detected` "切断大动脉"三步（25-47）：`ctrl._interrupt.set()`（35）→ 延迟导入并依次调 `abort_request()`（关 LLM 活动请求）、`kill_active()`（bash）、`_kill_term()`（terminal/SSH）、`_kill_serial()`（serial）（37-44）；全部包在 try/except 吞异常中（45-47，注释：kill 失败由主线程检查点的 kill 兜底）；四函数均幂等自述（30-33）。
10. 中断后的收敛路径（由消费方完成，见各调用点）：`_interrupt.set()` 后主线程任何 `_interrupt_ctrl.is_set` 检查点（agent_loop 循环、tool_dispatcher 循环、LLM cancel_check、compression cancel_check）立即发现并中止。

**边界/异常行为**（编号；附行号）：
1. `_saved_sigint` 恒 None（60 初始化后无写入点）——enter_* 两个模式中的"恢复已保存 SIGINT"分支永远不执行；实际行为总是分支 else：frozen→SIG_IGN、否则 default_int_handler（77-87、100-110）。**这是死代码路径**（见补丁痕迹）。
2. signal.signal 调用被 `threading.current_thread() is threading.main_thread()` 守卫（78、101）——非主线程调用 enter_* 时不报错、不设 signal。
3. 轮询线程启动/停止竞态防御：每个线程独立 stop Event（96）+ 旧线程先停（92-94）+ `is_alive()` 判断（93）。
4. ImportError/OSError/AttributeError（148-149 的 msvcrt 环境异常）→ 静默无 ESC 中断能力（136-137）——管道/重定向输入下 ESC 监听不可用。
5. Unix：无法 tcsetattr（如非 tty）→ `return`，无中断能力（236-237）；`os.read(fd,1)` 异常退出循环（266-267）；finally 恢复不可用终端吞 termios.error/OSError（272-274）。
6. coninput 读事件失败（ReadConsoleInputW 返回 0）→ 退出线程（190-191）。
7. `_on_esc_detected` 四个 kill 调用任一抛异常 → 整体吞掉（45-47）；但 `_interrupt.set()` 已在最前执行（35），保证主循环必然感知。
8. 双 ESC 连按语义：两个窗口（native 157-160、unix 262-265）中若转义序列消费区内出现第二个 ESC → 立即打断（用户视角：连按 ESC 总能打断）。
9. `_stop_poll.set()` 与 `stop.wait()` 配合——wait 超时粒度 20-50ms，即中断延迟（触发到主循环感知）最坏约 30-50ms。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `_saved_sigint` 死逻辑：初始化后无任何赋值（60、79-81、102-104），"保存并恢复原 SIGINT"分支不可达——两处 enter_* 中该分支为死代码。严重度：中。
2. `enter_input_mode` 与 `enter_run_mode` 的 SIGINT 处理块（77-87 vs 100-110）为逐字重复代码（含注释"编译为exe后Ctrl+C会直接杀进程导致崩溃，忽略SIGINT，用ESC中断"重复两遍）。严重度：中。
3. `_on_esc_detected` 延迟导入 core/tools 四个模块（37-40）——ui→core/tools 跨层引用，注释自承认"延迟导入避免 ui↔tools 模块循环依赖"；中断路径依赖 core.interrupt 注册表（core/llm.py:140 注册 abort_active_llm_request）。严重度：中（架构耦合，但有明示理由）。
4. 三处轮询实现（native 139-163、coninput 165-215、unix 217-274）各自手写 ESC/转义序列判定与消费窗口，逻辑相似度极高（20ms/5 字节/双 ESC 判定在 native 与 unix 完全同构）但零共享。严重度：中。
5. coninput 的 INPUT_RECORD 手动内存偏移解析（171-212）：硬编码 20 字节结构、偏移 4/10、一次读 8 条——平台 ABI 假设无校验。严重度：低。
6. `_poll_esc_windows` 的异常分流只捕 `(ImportError, OSError, AttributeError)`（136）而内部 `_poll_esc_windows_native` 只捕 `OSError`（161）、coninput 捕 `(OSError, ValueError)`（213）——异常面按补丁史拼贴，不一致。严重度：低。
7. 吞异常三处（_try_restore_term 21-22、_on_esc_detected 45-47、宽异常面静默）——中断路径可靠性优先于可观测性的设计取向，但对诊断不利。严重度：低。

**可测性**：
- 可单测（monkeypatch）：`enter_input_mode`/`enter_run_mode` 的线程与 Event 状态迁移（断言 `_poll_thread` 启停、stop event 更换）；`clear`/`is_set`；`_on_esc_detected`（monkeypatch `..core.interrupt.abort_request` 与三个 kill 函数，断言调用顺序与吞异常行为）；非主线程调用 enter_* 的 signal 守卫。
- 需要集成测试：与 UIStreamSession/agent_loop 的中断端到端（ESC→set→stream.abort→主循环收敛）。
- 无法自动化：真实键盘 ESC 时序（msvcrt kbhit/getch 行缓冲、转义序列切分、连按 ESC、ReadConsoleInput 事件流）；Unix 真实 tty 的 cbreak/atexit 恢复——需人工或专用伪终端设施（未验证项目是否有）。

---

### narnat_agent/ui/headless.py（87 行）

**职责**：headless（`nn -p` 一次性任务）UI 替身——纯文本流输出，无键入、无 spinner、无中断、无统计栏；接口与 UIInterface/UIStreamSession 同形以便装配层无差别注入。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `class HeadlessStream`（13）：
  - `def __init__(self)`（16）：`self._renderer = StreamingRenderer()`；`self._started = False`；`self.aborted = False`；`self.cancelled = False`（19-20）。
  - `def begin(self)`（22，pass）
  - `def feed(self, chunk: str)`（25）
  - `def pause_spinner(self)`（29，pass）
  - `def resume_spinner(self)`（32，pass）
  - `def flush_renderer(self)`（35）
  - `def reset_renderer(self)`（39）
  - `def finish(self, *args, **kwargs)`（42）
  - `def abort(self, message=None)`（46）
- `class HeadlessUI`（53）：
  - `def __init__(self, model_name: str = "")`（56）
  - `def start(self)`（59，pass）
  - `def read_input(self)`（62，返回 None）
  - `def read_input_with_prompt(self, prompt_text: str = "")`（65，返回 None）
  - `def dispatch_command(self, cmd: str, args: str)`（68，返回 0）
  - `def create_stream(self)`（71，返回 HeadlessStream）
  - `def on_interrupted(self)`（74，pass）
  - `def begin_compressing(self)`（77，pass）/ `def end_compressing(self)`（80，pass）
  - `def begin_summarizing(self)`（83，pass）/ `def end_summarizing(self)`（86，pass）

**依赖**（本文件 import 的内部模块）：
- `..output`（9）：`write as _stdout_write`（仅 abort 的 message 路径用，50）。
- `.renderer`（10）：`StreamingRenderer`（17）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/assembly.py:141`：`from .ui.headless import HeadlessUI`（142 `ui = HeadlessUI(config.ai.model)`，headless 分支）。
- 运行期接口面（经注入，非 import）：`core/agent.py:226/254`（create_stream）、`core/agent_loop.py` 与 `core/tool_dispatcher.py` 的完整 stream 协议调用面（feed/finish/abort/begin/flush_renderer/reset_renderer/pause_spinner/resume_spinner/cancelled/aborted 全部同形承载）。

**状态**：
- 模块级全局：无。
- 实例状态：HeadlessStream——`_renderer`（17）、`_started`（18）、`aborted`（19，公开属性，abort 置 True）、`cancelled`（20，**恒 False 的实例属性**，非 property）；HeadlessUI——`_model`（57，存而不用）。

**行为要点**（编号；附行号）：
1. `feed` 恒置 `_started=True` 后转发 `_renderer.feed(chunk)`（25-27）——与 UIStreamSession.feed 的差异：无 spinner 停用、无中断联动。
2. `flush_renderer` 有 `_started` 守卫（35-37）；`reset_renderer` 无守卫直接转发（39-40）。
3. `finish(*args, **kwargs)` 忽略全部统计参数，仅 `_renderer.flush(final=True)`（42-44）——headless 不显示统计栏。
4. `abort(message=None)`：置 `aborted=True` → flush → message 非空时原样输出（46-50）——无"已打断"默认提示（对比 UIStreamSession.abort）。
5. `HeadlessUI` 全 no-op/None/0：read_input 返 None（62-63）；dispatch_command 返 `0`（69，注释等价 CommandResult.UNKNOWN，但返回裸 int 而非枚举）；create_stream 每次新建 HeadlessStream（71-72）。
6. 纯文本输出的实现分两层（不在本文件）：`main.py:45-47` 在 Agent 构造前 `set_plain(True)`（全局去色）+ `set_quiet_tools(not args.tool_log)`（默认静默调度日志）；本文件的 StreamingRenderer 在 plain 下结构（表格/列表/代码块）保留、颜色消失（renderer.py:216/282）。

**边界/异常行为**（编号；附行号）：
1. `cancelled` 恒 False（20）——headless 不可打断；上游 `while not stream.cancelled`/检查点全部无害通过（headless 无 ESC 轮询线程，_interrupt_ctrl 处于初始未 set 状态）。
2. `begin()` no-op（22-23）——agent_loop 在轮间调 `stream.begin()`（agent_loop.py:95/366/383）重启 spinner 的语义在 headless 下天然消失。
3. `pause_spinner`/`resume_spinner` no-op（29-33）——tool_dispatcher 的 pause/resume 计数（222/272）在 headless 下无副作用。
4. `abort` 不 re-raise、不中断进程（46-50）——agent.py:231/236 对其 `aborted` 置位后的判定路径照常工作。
5. `finish` 收到任意参数不校验类型（42）——接口漂移静默容忍。
6. `HeadlessUI.dispatch_command` 返回 int 0——core/agent.py 的数字比较（66/73）在 headless 下不会发生（run_headless 不走命令路径，186-276）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. `HeadlessStream` 与 `UIStreamSession`（ui_design.py:160-272）是两套平行实现的同形接口（9 个同名成员，行为子集），无共享基类/协议——接口演进需双改。严重度：中（重复代码，但有明确取舍背景）。
2. `HeadlessUI.__init__` 存 `_model` 后全类不再使用（57）——死状态。严重度：低。
3. `dispatch_command` 返回裸 `0` 加注释"CommandResult.UNKNOWN"（69）——类型注释补偿，int/enum 双制式渗透。严重度：低。
4. `finish(*args, **kwargs)` 全吞签名（42）——接口契约靠文档而非签名表达。严重度：低。
5. `flush_renderer` 有 `_started` 守卫而 `reset_renderer` 没有（35-40）——不对称守卫（reset 对空缓冲无害，属隐式约定）。严重度：低。

**可测性**：
- 可独立单测：全部方法（HeadlessStream 捕获 stdout 断言 feed→渲染、abort 两分支；HeadlessUI 断言返回值）。
- 需要集成测试：与 assembly 的 headless 装配分支（141-144）联测；与 agent.run_headless 的 end-to-end（`[NN_DONE]` 哨兵行为）。
- 无法自动化：无。

---

### narnat_agent/ui/colors.py（43 行）

**职责**：ui 层历史 import 路径的兼容重导出——所有颜色/写入/样式定义唯一源在 `narnat_agent/output.py`，本模块零逻辑、仅 `from ..output import ...`。

**对外接口**（本文件无函数/类定义，全部为 re-export 名字；按块列出与行号）：
- 基础常量（9-13）：`_Color, _stdout_lock, _ansi_color, write as _stdout_write, try_write as _stdout_try_write, RST, BLD, DIM, R, B, D`。
- 基础色（16-20）：`C_PRIMARY, C_SECONDARY, C_USER, C_ACCENT, C_SUCCESS, C_WARNING, C_ERROR, C_LINK, C_DECORATION, C_EMPHASIS, C_CODE_BG`。
- 旧别名（23-26）：`G, C, E, Y, X, U, M, O, BG, W, W7, GRY, CYN, GRN, YLW, RED, BLU, MAG, ORG, BG8, WHT, WHT7`。
- 派生 token 与接口（29-42）：`MD_*`（16 个）、`CB_*`（9 个）、`DIFF_*`（5 个）、`UI_*`（7 个）、`CMD_*`（5 个）、`PTK_PROMPT_SYMBOL, PTK_PROMPT_TEXT, PTK_PROMPT_CUSTOM, apply_style, DisplayState`。

**依赖**（本文件 import 的内部模块）：
- `..output`（9、16、23、29，四段 import）。

**被依赖**（谁 import 本文件，grep 可复验）：
- `narnat_agent/ui/renderer.py:21-35`：`from .colors import (...)`（44 个名字）。
- `narnat_agent/ui/session_commands.py:20-22`：`from .colors import (R, CMD_SUCCESS, CMD_ERROR, CMD_HINT, CMD_HIGHLIGHT, CMD_MUTED, _stdout_write)`。
- `narnat_agent/ui/ui_design.py:33-42`：`from .colors import (...)`（33 个名字）。
- `narnat_agent/core/session_callbacks.py:136、248、440`：`from ..ui.colors import C, R, X`（函数内延迟导入，用于会话列表着色）——**core → ui 的反向依赖**。
- 生产代码之外：`tool_exp/` 历史实验脚本（如 RR_77.py:18、RR_82.py:21）。

**状态**：
- 本模块自身无状态；所有名字是指向 output.py 中同一对象的引用（`_Color` 实例按引用共享——apply_style 改 `_value` 后经本模块消费的模块自动生效）。**未验证**：是否有任何代码靠 `colors.X = ...` 赋值（grep 未见，均为读取）。

**行为要点**（编号；附行号）：
1. 导入即完成全部绑定（9-42）——无任何运算、无副作用（副作用在 output.py 侧）。
2. docstring 自述定位："所有定义统一在 output.py，此模块仅做兼容重导出，保持 ui 层历史 import 路径不变"（3-6）。
3. 消费方的常量来源二象性：renderer/session_commands/ui_design 经本模块拿名字（子模块内 import 一致性），而 core/tools/main 直接 `from ..output import`——同一 `_Color` 实例的两条 import 路径。
4. `C/R/X` 三别名被 core/session_callbacks 用于对会话列表文本做 `str.replace` 着色（136-139、248-251、440-443）——颜色对象以 f-string 插值出 ANSI 串。

**边界/异常行为**（编号；附行号）：
1. 若 output.py 删除/重命名某名字，本模块 import 即失败（导入期硬绑定，无 __getattr__ 兜底）——43-42 行全部为静态 import。
2. 无自身异常处理；错误面完全继承 output.py 的导入期行为（如 `_enable_vt_on_windows` 静默路径）。

**补丁痕迹**（编号；行号+证据；严重度）：
1. 双路径维护（output.py 单一定义源 + 本模块 43 行 re-export 面）：任何 output 名字增删需同步此处（如 `_stdout_lock`/`_ansi_color` 也被导出，本模块 10 行——`_ansi_color` 在生产代码中无消费者，是纯历史面）。严重度：低（设计自明，但存在误导性 import 路径），累计维护成本中。
2. `core/session_callbacks.py` 经 `..ui.colors` 反向依赖 ui 层拿颜色（136、248、440）——"core/tools 依赖 output 不依赖 ui"的架构声明（output.py docstring 4-5）在此被打破（虽然经 output 中转，路径却是 ui）。严重度：中。

**可测性**：
- 可单测：re-export 一致性（`assert colors.R is output.R` 等对象同一性检查；名字集合与 output `__all__`/文档对照）。
- 无法/无需自动化：无独立行为可测。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

本组内部（ui 包内 + output）：
- ui/renderer.py → ui/colors.py (21-35)
- ui/renderer.py → output.py (36)
- ui/ui_design.py → ui/colors.py (33-42)
- ui/ui_design.py → output.py (43, 130, 395, 424)
- ui/ui_design.py → ui/interrupt.py (44)
- ui/ui_design.py → ui/renderer.py (45-48)
- ui/ui_design.py → ui/session_commands.py (49)
- ui/session_commands.py → ui/colors.py (20-22)
- ui/colors.py → output.py (9, 16, 23, 29)
- ui/headless.py → output.py (9)
- ui/headless.py → ui/renderer.py (10)

本组 → 外部：
- output.py → config/defaults.py (17)
- ui/interrupt.py → core/interrupt.py (37，延迟)
- ui/interrupt.py → tools/bash (38，延迟)
- ui/interrupt.py → tools/terminal (39，延迟)
- ui/interrupt.py → tools/serial (40，延迟)

外部 → 本组：
- main.py → output.py (45)
- assembly.py → ui/ui_design.py (27)、ui/interrupt.py (28)、ui/headless.py (141)
- core/agent.py → output.py (17)
- core/agent_loop.py → output.py (20)、ui/ui_design.py (18)
- core/auto_save_manager.py → output.py (16)
- core/tool_callbacks.py → output.py (9)
- core/tool_dispatcher.py → output.py (18)
- core/compression_coordinator.py → ui/ui_design.py (15)、ui/interrupt.py (16)
- core/session_callbacks.py → ui/colors.py (136, 248, 440)
- tools/diff_utils.py → output.py (6)

### 模块级可变状态全清单（含读写方）

| 变量 | 文件:行 | 写方 | 读方 |
|---|---|---|---|
| `_PLAIN` | output.py:29 | set_plain 34-35；main.py:46 | is_plain 40；_Color.__str__ 216；write 118 |
| `_QUIET_TOOLS` | output.py:46 | set_quiet_tools 50-51；main.py:47 | is_quiet_tools 55；core/tool_callbacks.py:31；core/tool_dispatcher.py:412/529/538 |
| `_vt_handle` | output.py:59 | _enable_vt_on_windows 87 | _assert_vt 98/104-107 |
| `_TRUECOLOR`（导入期快照） | output.py:165 | （无运行时写） | _ansi_color 191 |
| `_BASE_COLORS` dict | output.py:253 | apply_style 418/420/442/444 | _parse_recipe 278/282；_resolve_ptk_style 388/390；apply_style 417/437/441/457/482 |
| `_BASE_HEX` dict | output.py:255 | apply_style 411-412/421/432/436/453/456 | _resolve_ptk_style 386；apply_style 429/433-435/454-456/477 |
| `_DERIVED` dict | output.py:342 | 导入期 346；apply_style 468 | apply_style 468 |
| `C_*`（11 个 `_Color._value`） | output.py:249-250 | apply_style 416-421/431-444 | 全体拼接消费方（core/tools/ui/main） |
| `MD_/CB_/DIFF_/UI_/CMD_*`（40 个 `_Color._value`） | output.py:342-346 | apply_style 468 | renderer/session_commands/session_callbacks 等 |
| `PTK_PROMPT_SYMBOL/TEXT/CUSTOM` | output.py:367-369 | apply_style 405/472-486 | ui_design._get_prompt_style 395-398 |
| `DisplayState.show_cost/show_balance/max_tokens/show_ratio/context_window` | output.py:506-510 | apply_style 489-493 | ui_design.show_stats 131-135 |
| `_interrupt` Event（单例内） | interrupt.py:57 | _on_esc_detected 35；clear 68；enter_input_mode 76；enter_run_mode 90 | is_set 65（经 property 遍布 core/ui 检查点） |
| `_stop_poll` Event（单例内） | interrupt.py:58 | enter_input_mode 71；enter_run_mode 92/96（换新对象） | 轮询线程 141/180/240 |
| `_poll_thread` | interrupt.py:59 | enter_input_mode 75；enter_run_mode 97-99 | is_alive 72/93 |
| `_saved_sigint`（恒 None 死逻辑） | interrupt.py:60 | enter_* 81/104（仅置 None） | 79/102 |
| `_atexit_registered` | interrupt.py:61 | 233-235 | 233 |
| `_commands` 注册表 | session_commands.py:156 | _register 162（装饰期） | _dispatch_command 404 |
| `BLOCK_RULES` | renderer.py:465 | 导入期（构造后只读） | render_line 481 |
| `_interrupt_ctrl`（单例引用） | interrupt.py:278 | （无重绑定） | assembly/ui_design/compression_coordinator |
| `_abort_callback` | core/interrupt.py:10 | register_abort（core/llm.py:140 注册） | abort_request 21 |

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

1. **[高] `_cmd_goal` 跨层直写会话管理器私有状态**（session_commands.py:242-243、245、254-256、258、266-268）：`mgr._goal_enabled/_goal_max_rounds` 直改 + `getattr(mgr,'_set_goal_tool',None)` 防御探测 + `_goal_default_rounds` 兜底读取——命令层→会话层私有属性三处穿透。
2. **[高] `apply_style` 单体巨型函数改写全模块可变全局**（output.py:400-493）：94 行内串行改写 `_BASE_COLORS/_BASE_HEX/_DERIVED/PTK_*/DisplayState` 五组全局，职责混杂且全局副作用不可控（配合模块导入期 `_enable_vt_on_windows()` 113 与 `_TRUECOLOR` 快照 165）。
3. **[中] 中断"大动脉"函数的跨层延迟导入**（interrupt.py:37-40）：ui → core/tools 四模块函数内延迟导入（注释自承认循环依赖规避）；ui 层深度耦合 core.interrupt 回调注册表与三个工具 kill 函数。
4. **[中] core → ui 反向依赖取色**（core/session_callbacks.py:136、248、440）：core 经 `..ui.colors` 拿 `C/R/X` 做 replace 着色，打破 output.py 声明的"core/tools 依赖 output 不依赖 ui"（output.py:4-5）。
5. **[中] `_saved_sigint` 死逻辑与 enter_* 双份重复代码**（interrupt.py:60、79-81、102-104 与 77-87 vs 100-110 逐字重复）。
6. **[中] SIGINT 处理块两处逐字重复**（interrupt.py:77-87 与 100-110）——同 5 号合并计为"enter_* 孪生实现"。
7. **[中] 表格渲染四形态路由的补丁堆叠**（renderer.py:621-722 与 596-599、707-715、926-928 注释史）：正常/折行/chunked/降级 × 宽度双测量（667-674）× 60 行缓冲上限，多个历史回归的补偿代码（"表格回归的根源""表格卡"）。
8. **[中] `flush(final)` 死参数**（renderer.py:917-934，docstring 自承认历史遗留）与 `CodeBlockRenderer.render` 死参数 `width`（renderer.py:496-505、909-912）——接口上的历史包袱双处。
9. **[中] 魔法字符串协议**：compact 中文子串判定"取消"（session_commands.py:194）、mode 前后缀判定"无效值/设置成功"（356-359）、`_resolve_ptk_style` 从 ANSI 反推 RGB（output.py:390-392）——跨模块以文案/字节形态为契约。
10. **[低] 未使用导入与死条目面**：ui_design.py:43-47（_stdout_lock、InterruptController、colorize_diff 等 8 名 re-export 无消费者）；session_commands.py:391-396（clear/goal 特判使注册表两条目成死条目）；colors.py:10（_ansi_color 无生产消费者）。

（其余低严重度痕迹：ctypes 手写内存偏移 renderer.py:104-146/167-194、interrupt.py:171-212；三套轮询同构不共享 interrupt.py:139-274；HeadlessStream 与 UIStreamSession 平行实现 headless.py:13-50；旧别名弃用区 output.py:353-360；表格候选判定三处重复 renderer.py:472/590/949。）

### 本组对外契约清单（被本组之外模块依赖的 public API，即新架构必须保持的行为面）

**output.py（本组对外最大面）**：
- `write(text)`：持锁、`\r` 前缀（非 plain）、每写 flush——被 main、core ×4、tools/diff_utils 使用。
- `try_write(text) -> bool`：非阻塞写——被 ui_design 动画线程使用。
- `set_plain(bool)` / `is_plain() -> bool`——被 main.py:46、renderer.py:282。
- `set_quiet_tools(bool)` / `is_quiet_tools() -> bool`——被 main.py:47、core/tool_callbacks.py:31、core/tool_dispatcher.py:412/529/538。
- `apply_style(dict)`——被 assembly.py:48。
- `DisplayState` 五属性——被 ui_design.show_stats、assembly 侧写路径（经 apply_style）。
- 颜色对象（`_Color` 实例）：`R/B/D/X/E/G/Y` 及全部 `C_*/MD_*/CB_*/DIFF_*/UI_*/CMD_*`——被 core（agent/agent_loop/auto_save_manager/tool_callbacks/tool_dispatcher）、tools(diff_utils)、ui 全体以 f-string 插值消费；`str()` 语义（plain→""）是行为契约。
- `PTK_PROMPT_SYMBOL/TEXT/CUSTOM`——被 ui_design._get_prompt_style。
- `_Color.__repr__` 恒返回 ANSI 原文（调试面）。

**ui/ui_design.py**：
- `UIInterface` 全方法（`start/read_input/read_input_with_prompt/dispatch_command/create_stream/on_interrupted/begin_compressing/end_compressing/begin_summarizing/end_summarizing`）——被 core/agent.py、core/agent_loop.py、core/compression_coordinator.py、assembly.py 按完整协议调用；`dispatch_command` 返回值语义（0/1/2 即 UNKNOWN/HANDLED/EXIT）被 core/agent.py:66-74 数字比较。
- `apply_style`（经该模块 re-export 被 assembly.py:27 导入）。
- `UIStreamSession` 全成员协议（`feed/finish/abort/begin/flush_renderer/reset_renderer/pause_spinner/resume_spinner/cancelled/aborted`）——被 agent_loop/agent/tool_dispatcher 按双实现（UIStreamSession + HeadlessStream）同一协议调用。

**ui/interrupt.py**：
- `_interrupt_ctrl` 单例的 `is_set` / `clear` / `enter_input_mode` / `enter_run_mode`——被 assembly.py:131、compression_coordinator（65/95/123/129/140）、ui_design（12 处）；`is_set` 还间接被全部 cancel_check 消费。

**ui/headless.py**：
- `HeadlessUI`（与 UIInterface 同形全方法）——被 assembly.py:141-144 注入。
- `HeadlessStream`（与 UIStreamSession 同形协议）——被 headless 模式下 agent_loop 全协议调用。

**ui/colors.py**：
- `C/R/X` 三别名——被 core/session_callbacks.py:136/248/440（唯一 core 消费点）。
- 其余 40+ 名字为 ui 包内消费（renderer/session_commands/ui_design）。

**ui/session_commands.py**：
- `_dispatch_command(cmd, args, mgr) -> CommandResult`——被 ui_design.dispatch_command。
- `_CommandCompleter(mgr)`——被 ui_design._create_session。
- `CommandResult`（0/1/2）——被 ui_design 与 core/agent.py 间接消费。

**ui/renderer.py**：
- `_sep()`——被 ui_design（show_header/show_stats）。
- `StreamingRenderer`（feed/flush/reset）——被 ui_design.UIStreamSession 与 headless.HeadlessStream。

---

## 附：验收对照（自查）

1. 覆盖 7 文件：renderer.py / output.py / ui_design.py / session_commands.py / interrupt.py / headless.py / colors.py——每文件独立 `###` 节，含全部字段。✔
2. 全部行为要点与补丁痕迹条目附行号（行号取自逐行通读的源码）。✔
3. "被依赖"清单均为 grep 实证（`from ... import` 原文引用，附行号），可用 `Grep` 复验。✔
4. 总表四项齐备：依赖矩阵 / 可变状态全清单 / 补丁痕迹 TOP10 / 对外契约清单。✔
5. 接口签名逐字复制自源码（含默认值与注解）。✔
6. 不确定项已标注「未验证」（output.py 边界 4/5/8、ui_design.py 边界 8/9、session_commands.py 边界 4/9、headless.py 部分面）。✔
