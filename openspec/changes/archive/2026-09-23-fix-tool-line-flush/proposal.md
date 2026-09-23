# Proposal

## Why

重构后的工具调度显示丢失了旧实现的"显示前落定"动作：旧实现的单工具执行路径在显示调用摘要前会先 `pause_spinner() + flush_renderer()`（暂停思考动画、把 AI 已输出的流式文本落定到终端），执行完再 `resume_spinner()`；重构后 `ToolDispatcher` 只保留了"显示"本身，未接 UI 句柄的 `pause/flush/resume`。

后果（用户实测 log）：AI 先输出一段说明文字（如「我先看看目录结构和规模。」）随即发起工具调用时，这段文字仍悬在渲染缓冲中；工具调度行（`[执行命令] …`、`[读取] …`）直接写终端，全部工具行显示完、回合收尾（`finish` 落定缓冲）后，AI 的文字才一次性出现——严重倒序，属用户可感知的显示回归。

## What Changes

- **conversation（工具调度显示面）**：单个工具执行前 SHALL 先暂停动画并落定渲染缓冲，再输出调用摘要；执行与差异/失败显示后 SHALL 恢复动画。静默模式下仍执行停/落/恢复（与旧实现一致，不因不显示而跳过）。
- **实现落地（非行为面）**：`contracts.OutputSink` 补齐已由规格声明的 `flush / pause / resume` 方法声明（实现早已存在：交互句柄与 headless 句柄均实现了三者）；`conversation` 的调度器把句柄的这三个动作接入工具显示路径。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `conversation`: 「工具调度分组与结果回传」中的终端显示要求补充"显示前暂停动画并落定渲染缓冲、执行后恢复动画"，并新增对应 Scenario。

## Impact

- 代码：`narnat_agent/conversation/dispatch.py`（显示路径接线）、`narnat_agent/contracts/output.py`（协议补声明）。
- 测试：`tests/unit/test_conversation.py`（桩件补 `pause/flush/resume` 记录 + 顺序回归断言）。
- 用户可见：工具行不再抢在 AI 文本之前；显示顺序恢复为「AI 文本 → 工具行 → 结果提示」。
- 兼容性：静默（headless/`-l` 关闭）行为不变（headless 句柄三方法均为无效果/落定守卫，无输出变化）；计划优先拦截路径的显示保持现状（旧实现该路径亦未做停/落，本次不对齐外行为）。
