# S10 规格写作任务：capability `tools-websearch` + `tools-todo` + `mcp`

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为三个 capability 编写行为规格。

- `tools-websearch`：WebSearch 工具（AnySearch API 调用与结果格式化）
- `tools-todo`：TodoWrite（计划同步与 UI 通知）+ GoalComplete（目标完成标记）
- `mcp`：MCP 集成——stdio 客户端（JSON-RPC 握手/工具列举/调用/重连）、连接管理（热注册/注销工具、进程生命周期）、MCP 工具（connect/disconnect）

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R5_report_tools_file_base.md` — 现状报告（web_search/todo_write/goal_complete/mcp_tool 部分）
4. `docs/recast/reports/R8_report_mcp_assembly.md` — 现状报告（mcp/__init__.py、mcp/client.py 部分）
5. 需要核实时直接读源码（narnat_agent/tools/web_search/、tools/todo_write/、tools/goal_complete/、tools/mcp_tool/、mcp/）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/tools-websearch/spec.md`、`openspec/changes/recast-v2/specs/tools-todo/spec.md`、`openspec/changes/recast-v2/specs/mcp/spec.md`（三个文件）
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；**工具名、参数名、输出格式、命名规则、错误文案属于对外契约，必须精确**
- **tools-websearch 覆盖**：工具参数契约（query 等）、API 调用（密钥来源：接口密钥组的 websearch/websearch_url；未配置密钥的行为）、结果格式化（标题/链接/摘要）、错误处理文案、超时
- **tools-todo 覆盖**：
  1. TodoWrite 参数契约（todos 数组：内容/状态字段）、状态更新对工具上下文与 UI 的同步（状态面板显示）、计划状态语义（进行中/已完成）
  2. GoalComplete 行为：目标模式外不可见（动态注入/移除）；调用后置位完成标记；主循环消费语义（复位）；调用时清理后台任务（如报告所述）
- **mcp 覆盖**（至少）：
  1. MCP 工具参数契约（action: connect/disconnect、name、config）
  2. 连接流程：配置解析（见 config 规格）、stdio 子进程启动、JSON-RPC 握手（initialize）、工具列举（tools/list）、工具热注册（下一轮即可见、命名规则 mcp__<服务器名>__<工具名>、非法字符替换、64 字符截断与哈希后缀、重名处理）
  3. 工具调用：请求/响应协议、超时参数、结果内容格式化、错误传播
  4. 断开：注销工具、进程终止（优雅→强杀）、`all` 语义
  5. 通道保活：进程中断自动重连（凭据保留）、重连后要求重试本次调用（不自动重跑）
  6. 进程生命周期：独立进程组、Esc/退出/异常均回收、并发上限 8
  7. 失败不阻塞原则与错误文案
- **兼容怪癖**：明显缺陷的边缘行为集中写入各自 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述三个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作；不发起网络/子进程调用
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 三个文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. tools-websearch Requirement ≥ 3、tools-todo ≥ 3、mcp ≥ 7；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号（工具名/参数名/输出文案除外）

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
