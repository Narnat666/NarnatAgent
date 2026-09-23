# S2 规格写作任务：capability `messages` + `compression`

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `messages` 与 `compression` 两个 capability 编写行为规格。

- `messages` 覆盖：消息存储（唯一所有者与只读视图、受控修改接口）、中断修复（repair）
- `compression` 覆盖：上下文压缩（触发判定、切点选择、摘要请求、重建会话、溢出恢复、手动压缩、token 估算）

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R1_report_core_dialog.md` — 现状报告（compressor/compression_coordinator/context 部分）
4. `docs/recast/reports/R2_report_core_session.md` — 现状报告（message_manager 部分）
5. 需要核实时直接读源码（core/message_list.py、core/message_manager.py、core/compressor.py、core/compression_coordinator.py、core/context.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/messages/spec.md` 与 `openspec/changes/recast-v2/specs/compression/spec.md`（两个文件）
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤
  - 例外：消息字典字段名（role/content/tool_calls/tool_call_id/thinking/thinking_signature）、system/user/assistant/tool 角色语义等对外数据结构契约必须精确
- **messages 覆盖主题**（至少）：
  1. 消息序列与角色：system 首条、user 输入、assistant（含 tool_calls/thinking/thinking_signature 可选字段）、tool 结果
  2. repair 修复规则（打断后不完整序列：为未回复 tool_call 补 "[用户中断]"；已修复且尾部为 tool 时补合成 assistant【思考模式下须带非空 thinking 占位】）
  3. 只读视图/受控修改语义（外部拿不到内部列表引用；替换为原子操作）
  4. 中断补齐：逐 tool_call 补 "[用户中断]"（仅未完成者）
- **compression 覆盖主题**（至少）：
  1. 触发：窗口占比（输入 token / 上下文窗口）≥ 压缩阈值（默认 95%）先压缩再请求；≥告警阈值（默认 50%）提示一次
  2. 摘要请求：全部历史 + 压缩指令；no_tools；超限单独归因（overflow）
  3. 切点选择：保留尾部 token 预算（默认 16000；0=全量压缩）；平衡切割保证 tool_call/tool_result 配对不拆散
  4. 重建：system + 摘要（结构化检查点模板）+ 保留尾部；防连续 user（尾部末条为 user 时合并/剔除规则）；手动压缩尾 user 剔除
  5. 结果归因：empty/interrupted/llm_error/empty_summary/overflow 各分支及中断/失败时消息不被改动
  6. 溢出恢复：请求被服务端拒绝（上下文超限）→ 压缩后原地重发；每次运行最多一次
  7. 手动压缩（/compact）：保留尾部 + Esc 取消
  8. token 估算：启发式算法口径（报告与源码为准）
- 两个 spec 有引用关系（messages 的 repair 独立；压缩的编排在 compression）——重叠处归属行为主体，另一侧一句话引用
- **兼容怪癖**：明显缺陷的边缘行为集中写入各自 `### Requirement: 兼容性怪癖保持`

## 边界条款

- 只写上述两个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 两个文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. 每文件 Requirement 数 ≥ 6；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单（messages 4 项、compression 8 项）逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
