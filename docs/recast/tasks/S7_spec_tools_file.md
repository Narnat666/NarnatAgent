# S7 规格写作任务：capability `tools-file`（文件类工具族）

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `tools-file` capability 编写行为规格。

`tools-file` 覆盖：Read/Glob/Grep/Edit/Write 五个工具的完整行为（参数、输出格式、编码、忽略目录、截断协议、行号、diff 生成）。

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R5_report_tools_file_base.md` — 现状报告（read/glob/grep/edit/write/diff_utils/param_utils/token_estimate/registry/tool_context 部分）
4. 需要核实时直接读源码（narnat_agent/tools/read|glob|grep|edit|write/、tools/registry.py、tools/tool_context.py、tools/diff_utils.py、tools/param_utils.py、tools/token_estimate.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/tools-file/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；**工具名、参数名、参数默认值、输出格式、错误文案属于对外契约，必须精确写出**
- **覆盖主题**（至少）：
  1. 五个工具的工具名与参数契约（工具名 Read/Glob/Grep/Edit/Write；每个工具的参数名、类型、默认值、必填性；DEFINITION 描述要点）——直接进规格
  2. Read：文本读取+行号格式、编码自动识别（UTF-8/GBK）、截断规则与续读 offset 提示、远程读取（device 参数）、空文件/不存在/目录的错误文案
  3. Glob：`**` 递归与 `{}` 花括号展开、按修改时间倒序、忽略目录过滤（ignore_dirs 语义）、无匹配结果文案
  4. Grep：正则搜索语法、输出格式（文件分组+行号+命中计数）、多路径数组、head_limit 达到后降级为文件清单、忽略目录、二进制/大文件跳过规则、无匹配文案
  5. Edit：精确字符串替换、replace_all、编码与换行符自动保持、唯一性校验（多处匹配报错）、失败文案
  6. Write：创建/覆盖、编码保持
  7. 工具参数校验模式：未知参数报错（列出有效参数名）、缺参数报错、类型错误的中文提示（param_utils 模式）
  8. 工具执行入口协议：执行结果（给 AI 的文本 vs UI 展示差分）、错误包装（工具执行失败捕获）、动态工具（MCP）注册与注销后的可见性
  9. 工具运行时上下文（tool_context）：已读文件追踪（Edit 前须先 Read 的校验如有）、忽略目录、输出截断上限、超时上限、api_keys——写清数据语义（新架构中其职责会重新划分，但行为面保持）
  10. diff 生成：编辑类工具返回的着色 diff 格式（行前缀/颜色角色）
  11. token 估算：估算口径（中英文/代码的启发式）
- **兼容怪癖**：明显缺陷的边缘行为集中写入 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述一个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作；不实际调用工具
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. Requirement 数 ≥ 11；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单 11 项逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号（工具名/参数名除外）
7. 五个工具的参数契约完整（参数名逐一列出，与实际源码一致）

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
