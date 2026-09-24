# Proposal

## Why

工具显示文本的**尾随空行规则**在重构中合并到单一渲染通道时丢失了区分：

- 旧实现有两条显示通道：差异（`_show_diff`：多行差异块**以空行收尾**、单行 `[无差异]` 不加空行）与 TodoWrite 计划（`ui_callback`：逐行输出、**不补空行**）；
- 重构后计划显示改走 `ToolResult.ui_text`，而唯一的渲染口 `_show_diff` 统一按"差异块"处理（多行 → `\n\n` 收尾）——计划块后多出一个空行。

用户实测：`[更新计划] 4项` 与其后的状态行列表、再到后续 AI 文本之间出现不应有的空行（计划列表与"代码量（逐行统计，非估算）"之间）。真机对照已复现（`docs/recast/plan_display_broken.txt`：计划行后紧跟空行；修复后 `plan_display_new.txt`：无空行）。

## What Changes

- **contracts（tool）**：`ToolResult` 增加 `ui_text_kind` 字段（`UI_TEXT_DIFF` / `UI_TEXT_LINES`，默认差异块）——把"尾随空行"的决策由渲染层的内容猜测改为**工具声明的显示类别**；配套常量与类型（`UI_TEXT_DIFF` / `UI_TEXT_LINES` / `UI_DISPLAY_KIND`）。
- **conversation（工具调度显示）**：`_show_diff` 按 `ui_text_kind` 渲染——`lines` 逐行缩进后以单换行收尾（不补空行），`diff` 保持旧规则（多行空行收尾、单行 `[无差异]` 除外）。删除确认重执行路径保持现状（旧行为，且不承载计划显示）。
- **tools（plan / registry）**：TodoWrite 的计划 `ui_text` 声明为 `UI_TEXT_LINES`；注册表归一化透传 `ui_text_kind`。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `tools-todo`: 「TodoWrite 状态同步（工具上下文与终端显示）」明确计划为**状态行列表**：逐行输出、不以空行收尾、直接接后续输出（对齐旧 `ui_callback` 通道）。
- `tools-file`: 「编辑类工具的返回形态与差分展示」明确差异块**多行时以空行收尾**（与后续输出分隔）、单行 `[无差异]` 提示除外——把旧行为显式化，与状态行列表区分。

## Impact

- 代码：`narnat_agent/contracts/tool.py`（字段与常量）、`contracts/__init__.py`（导出）、`conversation/dispatch.py`（渲染分支）、`tools/plan.py`（TodoWrite 声明类别）、`tools/registry.py`（透传）。
- 测试：`test_contracts.py`（字段表 + 位置参数构造修正）、`test_conversation.py`（新增"行列表无尾空行 / 差异块有尾空行"回归用例）、`test_tools_misc.py`（计划 `ui_text_kind` 断言）。
- 用户可见：计划与后续输出之间不再出现多余空行；差异显示（Edit/Write）保持原样。
- 兼容性：`ui_text_kind` 默认值为差异块——既有构造点（Edit/Write/远程/二元组归一化）行为零变化。
