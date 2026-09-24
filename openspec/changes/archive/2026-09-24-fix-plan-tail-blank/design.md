# Design

## Context

见 `proposal.md`（Why）。关键现状：

- 旧实现两条显示通道：`tool_dispatcher._show_diff`（差异块：多行 `\n\n` 收尾；单行 `[无差异]` 除外）与 `tool_callbacks.TodoCallbacks.on_todo_update`（计划：逐行 `line + "\n"`，无尾空行）。
- 重构后 `ToolResult.ui_text` 统一承载两种文本，唯一渲染口 `dispatch._show_diff` 按"差异"规则处理——计划后多一个空行（回归）。
- `ui_text` 的生产方：Edit/Write（diff）、远程编辑（diff）、TodoWrite（计划）、registry 二元组归一化（diff）。

## Goals / Non-Goals

**Goals:** 恢复两条通道的尾随规则差异；用显式字段（工具声明）取代渲染层的内容猜测。
**Non-Goals:** 不改渲染的缩进规则、静默跳过规则、diff 着色；不改删除确认重执行路径（非 Windows 专用，不承载计划显示，保持旧行为）。

## Decisions

### D1. 显示类别由 `ToolResult.ui_text_kind` 显式声明

字段取值 `UI_TEXT_DIFF`（默认）/ `UI_TEXT_LINES`，配套 `UI_DISPLAY_KIND` 类型别名与常量（同一事实唯一来源，防止散落字面量）。

- 理由：渲染层无法从内容可靠区分"差异"与"计划"（两者都是多行文本）；按内容猜测正是旧实现遗留的脆弱模式（如 `[无差异]` 字面量特判）。声明式字段让"新增一个带显示文本的工具"不再需要改渲染层。
- 备选：① 调度器按工具名硬编码（破坏"新增工具不改调度器"，且工具名清单在多处已有先例、不应再增）；② 都改为不补空行（改动了差异的旧行为，且差异块空行是已发布的视觉分隔）；③ 在计划末行追加空行标记（隐晦、且污染内容）。
- 默认值取 `UI_TEXT_DIFF`：既有构造点（二元组归一化、Edit/Write/远程）行为零变化。

### D2. 渲染分支在 `_show_diff` 内部按 kind 选择尾随

`kind == UI_TEXT_LINES → tail="\n"`；否则保持旧规则（多行 `\n\n`、单行 `[无差异]` `\n`）。

- 理由：改动最小、两条规则在同一处并列可读；消费方（调度器）只多传一个参数。
- 备选：拆成 `_show_diff` / `_show_lines` 两方法（调用点需按 kind 分派，等价但更分散）。

### D3. 注册表归一化透传 kind

`ToolRegistry._finalize` 构造新 `ToolResult` 时透传 `ui_text_kind`（与 `ui_text` 同路径）；`normalize_result` 的二元组路径使用默认值（diff）。

- 理由：`_finalize` 是执行入口的唯一出口，不透传会使工具声明的类别在归一化时丢失。

## Risks / Trade-offs

- [字段位置插入导致位置参数构造错位] → 全仓扫描确认仅一处测试使用多位置参数（已改为关键字并显式断言新顺序）；生产代码全部关键字或单参数构造。
- [计划是否该有空行的主观判断] → 以旧实现为唯一金标准（`on_todo_update` 无空行），并有真机对照证据（修复前后各一份输出）。旧实现行为亦已由 `specs/tools-todo` 显式化。
- [未来第三个显示类别] → 字段为枚举式字符串常量，扩展时新增常量与渲染分支即可；不在本次范围内。

## Migration Plan

1. 契约字段与常量 → 渲染分支 → 生产方声明 → 注册表透传（本次已完成）。
2. 测试：契约字段表、计划无尾空行（红绿验证）、差异块保持空行。
3. 真机验证：headless `-l` 对照（修复前空行 / 修复后无空行；`docs/recast/plan_display_*.txt`）。
4. 归档 change 更新基线规格。

回滚：单字段级改动，`git revert` 即可。
