# S3 规格写作任务：capability `conversation` + `app`

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `conversation` 与 `app` 两个 capability 编写行为规格。

- `conversation` 覆盖：对话内循环（LLM 事件消费、工具调度协调、删除确认挂起、流中断重试、收尾软提醒、目标模式自动续跑与收尾轮）
- `app` 覆盖：应用装配与生命周期（唯一组装点、Agent 主循环、headless 运行、退出清理）

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R1_report_core_dialog.md` — 现状报告（agent_loop/agent 部分）
4. `docs/recast/reports/R2_report_core_session.md` — 现状报告（tool_dispatcher 部分）
5. `docs/recast/reports/R8_report_mcp_assembly.md` — 现状报告（assembly/main/headless 部分）
6. 需要核实时直接读源码（core/agent.py、core/agent_loop.py、core/tool_dispatcher.py、assembly.py、main.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/conversation/spec.md` 与 `openspec/changes/recast-v2/specs/app/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；对外命令名、参数名、文案（如 `[NN_DONE] reason=… rounds=…` 哨兵、`【自动续跑】`/`【系统提示】`注入文案语义）必须精确
- **conversation 覆盖主题**（至少）：
  1. 内循环状态机：repair → 请求（事件流）→ 有 tool_calls → 执行工具 → 回传结果 → 下一轮；纯文本完成/错误/中断各出口
  2. 工具调度策略：计划优先拦截（require_plan 且工具数≥min_tools 且无活跃计划时返回拦截提示）；分组（只读并行/写入按文件分组同文件串行/串行工具逐个）；结果按原始顺序
  3. 中断协调：流取消检查点、未完成工具补 "[用户中断]"、UI 打断提示
  4. 删除确认挂起（非 Windows）：AWAIT 标记 → 结束当前流 → 提示符确认 y/N → 确认后重执行（跳过确认）或取消回传 "[操作已取消…]"
  5. 流中断整轮重试：无 finish_reason 视为中断，重置渲染缓冲、退避重试（上限=配置重试次数）、耗尽报错；每轮收到完成标记重置预算
  6. 上下文溢出恢复：context_overflow → 压缩后重发；单次运行最多一次；不可恢复时报错收尾
  7. 收尾软提醒：计划未勾选提醒一次、后台任务运行中提醒一次（各自标志位，新任务复位；提醒注入不进终端）
  8. 统计栏结案信号：普通模式每轮显示；目标模式中间轮不显示（声明完成或强制收尾轮显示）；后台运行中不显示
  9. 目标模式续跑：GoalComplete 置位结束；未置位注入续跑提示；达轮数上限注入收尾指令 + 强制收尾轮；中断/异常/空回复不续跑
- **app 覆盖主题**（至少）：
  1. 启动装配：配置→样式→日志→MCP 管理器→工具定义→LLM→上下文→消息→会话→UI→调度器→统计→自动保存→压缩协调→内循环→Agent 的完整依赖次序（不写类名，写"组件"与职责）；headless 分支差异（无删除确认回调、纯文本 UI、系统提示词剥离 subagent:hide）
  2. 交互主循环：读输入→命令分发（`/` 前缀）→EXIT 语义（清理并退出）→压缩检查→追加输入→内循环→自动保存→窗口占比更新与告警
  3. 新任务状态复位：目标完成标记/软提醒标志/计划状态复位
  4. headless 运行：注入任务→目标模式（含 `-g` 覆盖）→哨兵输出 `[NN_DONE] reason=… rounds=…`（reason 取值集）→不读输入/不保存会话/不查余额/不显示统计栏
  5. 退出清理：线程池关闭、终端/串口/后台任务/MCP 清理、日志关闭；异常路径同样保证哨兵与清理
  6. 每次调度的余额查询节奏（按轮次间隔）与费用统计更新（归属 stats，此处只写主循环触发点）
- **兼容怪癖**：明显缺陷的边缘行为集中写入各自 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述两个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 两个文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. conversation Requirement 数 ≥ 9、app Requirement 数 ≥ 6；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单（conversation 9 项、app 6 项）逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
