# Tasks

> 状态：全部完成（2026-09-23）。

## 1. 契约（contracts）

- [x] 1.1 `contracts/tool.py`：新增常量 `UI_TEXT_DIFF` / `UI_TEXT_LINES` 与类型别名 `UI_DISPLAY_KIND`；`ToolResult` 增加 `ui_text_kind` 字段（默认 `UI_TEXT_DIFF`）
- [x] 1.2 `contracts/__init__.py`：导出新符号

## 2. 实现

- [x] 2.1 `conversation/dispatch.py`：`_show_diff(ui_text, kind)` 按类别选择尾随（lines→单换行；diff→旧规则）；调用点传 `result.ui_text_kind`
- [x] 2.2 `tools/plan.py`：TodoWrite 的计划 `ui_text` 声明 `ui_text_kind=UI_TEXT_LINES`
- [x] 2.3 `tools/registry.py`：`_finalize` 透传 `ui_text_kind`

## 3. 测试

- [x] 3.1 `test_contracts.py`：字段表更新 + 位置参数构造修正（含新顺序断言）
- [x] 3.2 `test_conversation.py`：新增 `test_dispatch_plan_lines_no_tail_blank`（行列表无尾空行 + 差异块仍空行，红绿验证通过）
- [x] 3.3 `test_tools_misc.py`：计划 `ui_text_kind == UI_TEXT_LINES` 断言
- [x] 3.4 全量单测（1678 passed）+ 分层检查通过

## 4. 真机验证与归档

- [x] 4.1 真机对照：`docs/recast/plan_display_broken.txt`（修复前：计划行后空行）vs `plan_display_new.txt`（修复后：无空行）
- [ ] 4.2 `openspec archive fix-plan-tail-blank`；更新 `docs/recast/PROGRESS.md`（收尾执行）
