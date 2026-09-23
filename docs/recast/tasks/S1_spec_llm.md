# S1 规格写作任务：capability `llm`（LLM 双协议客户端）

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `llm` capability 编写行为规格。

`llm` 覆盖：LLM 双协议客户端（openai/anthropic）——流式请求、统一事件流、重试与退避、thinking 参数厂商适配、工具定义动态管理、上下文超限检测。

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围（capability 定义）
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**（注意：Requirement 是行为契约，不是实现描述）
3. `docs/recast/reports/R1_report_core_dialog.md` — 现状调研报告（你写规格的事实依据；llm.py 部分）
4. 需要核实时直接读源码 `narnat_agent/core/llm.py`

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/llm/spec.md`
- 格式：`# Spec Delta` + `## Purpose`（1-2 句）+ `## ADDED Requirements`；每条 `### Requirement: <行为名>` 至少 1 个 `#### Scenario: <场景名>`，场景句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：写"系统 SHALL …"，不得出现内部类名/函数名/行号/库名/实现步骤
  - 例外：对外契约必须精确——事件字段名（如 `content`/`tool_calls`/`usage`/`finish_reason` 的取值集合）、参数名、环境行为
- 每条需求必须可测（scenario 即潜在测试用例）
- 覆盖度：报告"对外契约清单"中属于 llm 的条目**必须全部进规格**（事件协议 13 条重点）
- 覆盖主题清单（至少）：
  1. 请求构建：协议选择（anthropic vs 其他→openai）、消息与工具定义传递、温度/最大输出 token 可选性、thinking 参数按（协议×模型前缀）映射（DeepSeek/GLM/Kimi/MiMo/Qwen/GPT/Claude）与回传开关
  2. 事件流：content 增量、tool_calls（流式聚合）、thinking 与 thinking_signature、usage、finish_reason 取值（stop/max_tokens/content_filter/server_busy/error/context_overflow/stream_interrupted…）、retry_notice、stream_interrupted（kind/detail）
  3. 中断：cancel_check 语义（流内与重试等待）、中断时静默结束流
  4. 重试：网络/5xx/429 重试、退避序列、重试通知事件、上下文超限识别（不重试）
  5. 工具定义动态管理：目标模式注入/移除 GoalComplete、MCP 热注册/注销、重名与改名
  6. 协议转换：messages 格式转换（system 提升、角色交替、tool 消息映射、thinking 回传格式按厂商契约）
  7. 余额查询不属于本能力（归 `stats`）
- **兼容怪癖**：明显缺陷的边缘行为集中写入 `### Requirement: 兼容性怪癖保持`（保持现状，来源以报告"边界/异常行为"为准）
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述一个文件；不改任何其他文件（报告、源码、其他 spec 一律不动）
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`。若失败：先读错误定位，**只修自己文件的问题**；错误指向其他 spec 文件时忽略并在报告中说明。

## 验收标准（可计算）

1. 文件存在于 `openspec/changes/recast-v2/specs/llm/spec.md`
2. `openspec validate recast-v2` 输出无指向本文件的错误
3. Requirement 数 ≥ 8；每条 Requirement 的 Scenario 数 ≥ 1
4. 场景块含 `- **WHEN**` 与 `- **THEN**` 行（逐条自查）
5. 主题清单 7 项逐项可对应（在报告中列出映射：主题→Requirement 名）
6. 全文搜索无：类名（如 `LLMClient`）、函数名（如 `chat_stream`）、行号引用

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
