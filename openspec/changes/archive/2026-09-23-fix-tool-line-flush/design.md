# Design

## Context

见 `proposal.md`（Why）。关键现状（已核实）：

- 旧实现 `tool_dispatcher._run_single`：`stream.pause_spinner()` → `stream.flush_renderer()` → 显示摘要 → 执行 → 显示差异/失败 → `stream.resume_spinner()`。
- 新实现 `conversation/dispatch.py::_run_single`：只显示摘要/差异/失败，未接触句柄的 `pause/flush/resume`；`ui/stream.py::UiSink` 与 `ui/headless.py::HeadlessSink` 均已实现三方法（重启用例：`pause` 并行计数、`flush` 带"注入守卫"、`resume` 计数递减且已中止不重启）。
- `contracts.OutputSink` 协议未声明 `flush/pause/resume`（虽被规格「流式输出会话句柄协议」定义）。
- 计划优先拦截路径在旧实现中同样未做停/落（本次保持）。

## Goals / Non-Goals

**Goals:**
- 工具行显示前落定 AI 文本、显示完成后恢复动画；与旧实现逐点等价（受等价对照基准守护）。
- 协议与规格对齐：`contracts.OutputSink` 补齐三者声明，消费方（调度器）以结构化子协议声明所需能力。

**Non-Goals:**
- 不改渲染器 / 句柄实现的内部语义。
- 不为计划拦截路径引入停/落（旧实现亦无；属既有行为面，另案处理）。

## Decisions

### D1. 调度器按"窄子协议"依赖显示协调能力

在 `conversation/dispatch.py` 的 `CancelProbe` 旁新增 `DisplayProbe(CancelProbe)` 协议（`pause/flush/resume` 三方法），执行路径签名由 `CancelProbe` 升为 `DisplayProbe`。

- 理由：调度器只消费取消查询与显示协调两个面，不引入完整 `OutputSink` 的宽依赖（与既有 `CancelProbe` 同风格；mock 友好）。
- 备选：直接依赖 `contracts.OutputSink`（依赖面过宽，且会把 feed/finish 等无关能力带进类型契约）。
- 实现方（`UiSink` / `HeadlessSink`）天然结构匹配，无需改动实现。

### D2. 停/落/恢复采用 try/finally 包裹

`_run_single` 改为：

```
sink.pause(); sink.flush()
try:
    显示摘要 → 执行 → 差异/失败显示
finally:
    sink.resume()
```

- 理由：正常路径与旧实现逐点等价；异常路径下旧实现会泄漏 pause（动画停在暂停态），新实现以 finally 保证计数平衡（`ui` 句柄的 resume 本身带"已中止不重启"防护）——异常路径的动画恢复属收敛性修正，且异常文本与失败提示显示均不变（其余可观察行为等价）。
- 备选：不复用 try/finally 逐字照搬（保留 pause 泄漏缺陷）——不采纳：该泄漏在"上一工具异常 + 后续仍有工具"时会让思考动画在本轮永久停摆，属明确缺陷。

### D3. 传参方式：随线程池调用点下传

`sink` 沿 `execute → _run_parallel / _run_write_groups / _run_serial / _run_sequential_group → _run_single` 以参数下传（不存实例属性）。

- 理由：`sink` 生命周期是"单回合"（每回合新建句柄），调度器是长生命周期对象；实例属性会引入跨回合的悬垂引用，且违反"状态随调用流动"的既有结构（旧实现同样以参数下传）。

### D4. 计划拦截路径保持现状

拦截路径的显示不做停/落（旧实现亦无）。理由：行为金标准 = 旧实现；如需对齐"所有工具行显示前落定"，应作为独立变更评估（拦截时通常无 AI 文本同轮悬置，影响面小）。

## Risks / Trade-offs

- [并行路径下多个工具线程各自停/落，与动画暂停计数交互] → 句柄的 pause/resume 已按"并行计数"实现（首个暂停即停动画、归零才重启）；落定为幂等追加输出，重复调用无残留（受 `flush` 守卫与渲染器行为约束）。等价对照与新增顺序断言共同守护。
- [静默模式行为] → headless 三类动作为无效果 / 落定守卫；交互模式静默工具开关仅影响写行，不影响停/落/恢复（与旧实现一致）。
- [误把渲染缓冲落定扩展成"每行都落定"影响表格稳定渲染] → 落定仅在工具行显示前发生（工具调用是"文本→动作"的天然分界），渲染器既有落定语义（半行渲染 / 表格降级）保持不变。

## Migration Plan

1. 代码：`contracts/output.py` 补协议声明；`conversation/dispatch.py` 接线。
2. 测试：`tests/unit/test_conversation.py` 桩件补记录 + 顺序断言（并行 / 写入 / 串行三类路径至少各一，含静默模式用例）。
3. 验证：全量单测 + 分层检查；真机读屏验证显示顺序（装置 `docs/recast/esc_probe_live`）。
4. 归档 change 更新基线规格。

回滚：单文件级改动，`git revert` 即可。
