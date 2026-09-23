# S4 规格写作任务：capability `sessions` + `stats`

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `sessions` 与 `stats` 两个 capability 编写行为规格。

- `sessions` 覆盖：会话状态机（三态）、持久化（保存/加载/删除/树形展示）、探索分支（/explore /done 增量合并）、交互命令实现（/save /ls /cd /rm /skill /thinking /thinkback /mode /goal /compact /exit 中的会话侧行为）
- `stats` 覆盖：统计与费用（token/缓存/费用累计、成本日志 CSV 写入与轮转、余额查询）

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R2_report_core_session.md` — 现状报告（session_callbacks/stats/billing 部分）
4. `docs/recast/reports/R7_report_config.md` — 现状报告（session_store 部分）
5. `docs/recast/reports/R6_report_ui.md` — 现状报告（session_commands 部分）
6. 需要核实时直接读源码（core/session_callbacks.py、core/stats.py、core/billing.py、config/session_store.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/sessions/spec.md` 与 `openspec/changes/recast-v2/specs/stats/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；命令名（/save /ls /cd /rm /explore /done /skill /thinking /thinkback /mode /goal /compact /exit）、会话文件路径规则、树形展示文案格式等对外契约必须精确
- **sessions 覆盖主题**（至少）：
  1. 三态状态机：NoSession（未保存）→ RootSession（/save 后）→ ChildSession（/explore 创建）；各状态可用命令集不同
  2. 命令行为：`/save <名称>`（空名自动命名）、`/cd <名称>`（进入历史会话，加载消息）、`/ls [--all]`（默认精简：今天全部+更早最多 3 个父会话；--all 树形全展开）、`/rm <名称|--all>`（删除标记，退出时生效）、`/exit`（退出语义随状态变化）
  3. 持久化文件：`sessions/<名称>.json` 与子会话 `<父名>/<子名>.json`；文件名安全替换规则；原子写（临时文件+改名）；surrogate 清洗
  4. 探索分支：/explore <名称> 从当前会话复制创建子分支；/done 时 AI 总结为结构化结论增量合并回父会话（memory/target 分界、轮次号、多轮合并）；子分支继承父全部上下文
  5. 技能：/skill <名称> 加载系统技能或项目技能（目录/文件.md 层级路径）为系统消息注入；技能树列举
  6. 思考/模型/目标命令的读写配置行为：/thinking <强度>、/thinkback [on|off]、/mode <模型>（写回 narnat.json）、/goal on [N] / off / 查看
  7. 删除标记与清理：resolve_session_name 解析规则、退出时执行删除
  8. 自动保存触发与门槛（Token 量）、退出保存
- **stats 覆盖主题**（至少）：
  1. 用量累计：输入/输出/缓存 token、缓存命中率、费用（按定价表查表计算；未知模型零费用）
  2. 成本日志 CSV：启用开关、输出文件（默认 data/cost_log.csv）、每行字段、容量上限与轮转（主名→_bak.csv，磁盘保留 1 活动+1 备份）、写入失败静默处理
  3. 余额查询：启用开关、查询地址/认证方式（bearer/x-api-key）/JSONPath 取值/货币路径、失败静默；按轮次间隔查询节奏
  4. 统计栏数据供给（显示归属 ui/output，本能力只写数据口径）
- **兼容怪癖**：明显缺陷的边缘行为集中写入各自 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述两个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 两个文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. sessions Requirement 数 ≥ 9、stats Requirement 数 ≥ 4；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单（sessions 8 项、stats 4 项）逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
