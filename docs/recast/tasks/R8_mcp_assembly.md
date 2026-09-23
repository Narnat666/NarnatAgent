# R8 调研任务：MCP 客户端 / 日志 / 组装层 / 入口（mcp / logger / assembly / main）

## 任务目标

narnat agent（工作目录 `D:\desktop\NarnatAgent`）即将**全面重构**：以"积木思想"重写为结构清晰、可独立测试、故障隔离的新架构，功能与现有稳定版等价（用户无感知）。重构前需要精确掌握现状。

你负责调研 **MCP 客户端、日志、组装层与入口**，产出结构化现状报告。报告双重用途：
1. 供架构师编写行为规格（specs，行为金标准）——需要精确到"行为"层面；
2. 供架构师设计新结构——需要精确到"职责/接口/状态/依赖"层面。

报告读者是没有读过这些代码的架构师，必须自包含、可核对、不臆测。

## 调研对象（逐文件**完整通读**，禁止只 grep 不读全文）

| 文件 | 行数(约) |
|------|------|
| narnat_agent/mcp/__init__.py | 343 |
| narnat_agent/mcp/client.py | 342 |
| narnat_agent/assembly.py | 231 |
| narnat_agent/logger.py | 109 |
| main.py | 73 |
| narnat_agent/__init__.py | ? |

产出报告到：`D:\desktop\NarnatAgent\docs\recast\reports\R8_report_mcp_assembly.md`

## 报告格式（严格遵守）

对每个源文件一节，含以下字段：

```markdown
### <文件路径>（<行数>行）

**职责**：一句话。

**对外接口**（public 类/函数/方法，逐个列签名）：
- `签名`：用途一句话

**依赖**（本文件 import 了哪些内部模块，附行号）

**被依赖**（谁 import 本文件：用 grep 搜索模块名/类名，列出 import 方及行号）

**状态**：
- 模块级全局：名称（是否可变，读/写位置行号）
- 类变量：...
- 实例状态：关键属性清单

**行为要点**（编号；每条附行号；这是写规格的素材，要写"可观察行为"）：
1. ...

**边界/异常行为**（编号；附行号；空值/0/负数/超长/编码/平台分支/重试/回退等）：
1. ...

**补丁痕迹**（编号；附行号+证据；判据：跨层引用、可变全局、职责混杂、重复代码、脆弱边界、
吞异常、魔法值、历史包袱注释、死代码；每条标严重度 高/中/低）：
1. ...

**可测性**：
- 可独立单测的部分（列函数名+原因）
- 需要集成测试的部分（列原因）
- 无法自动化的部分（时序/终端交互等，列原因）
```

特别关注（本组重点）：
- mcp/__init__.py 的 **McpManager**：连接/断开/工具热注册（set_tool_sinks → llm.add/remove_tool_definitions）、工具命名规则（mcp__<server>__<tool>，非法字符处理、64 字符截断、哈希后缀）、重连逻辑（_reconnect 与"要求重试本次调用"语义）、进程生命周期（新进程组、atexit 回收、并发上限 8）、失败不阻塞原则。
- mcp/client.py 的 **McpStdioClient**：JSON-RPC 2.0 协议细节（握手 initialize、tools/list、tools/call）、行解码（_decode_line 编码回退）、stderr 线程、进程终止（_terminate 优雅→强杀）。
- assembly.py 的**组装全链路**：每个对象的构造参数与注入的回调/lambda 清单（逐个列出——这是"依赖注入现状图"，新架构设计的直接输入）、构造顺序依赖、headless 分支差异（confirm_cb、HeadlessUI）、build 返回值 AssemblyResult 的字段清单。
- logger.py 的**日志系统**：启动/关闭、脱敏（_redact 正则）、日志文件命名与轮转、debug/info/warning/error 级别。
- main.py 的**入口**：argparse 参数、`-p` headless 分支（set_plain/set_quiet_tools、run_headless）、异常处理（顶层 try）。
- `narnat_agent/__init__.py` 的内容。

报告结尾附**总表**：

```markdown
## 总表
### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）
### 模块级可变状态全清单（含读写方）
### 补丁痕迹 TOP10（按严重度排序，含文件:行号）
### Assembly 依赖注入全景（对象 → 构造参数 → 注入来源；标出 lambda/闭包接线清单）
```

## 边界条款

- **只读任务**：除产出报告文件外，禁止创建/修改/删除任何文件；禁止一切 git 写操作（add/commit/checkout/stash 等）；git 只读命令（log/show/diff/status）可用。
- 禁止运行 narnat / python main.py 等会发起网络调用或启动进程的命令。
- 允许：Read、Grep、Glob、dir、git log/show/diff。
- 报告若超 1500 行，可拆为 `R8_report_mcp_assembly_part1.md` / `_part2.md`，并在首文件注明。
- 不确定的事项明确标注「未验证」，禁止臆测。

## 验收标准（可计算判据）

1. 报告文件存在，覆盖清单中全部 6 个文件（每个文件一个 `###` 节，缺一即不合格）。
2. 每个"补丁痕迹"和"行为要点"条目均附行号。
3. "被依赖"清单可用 grep 复验（抽查 5 条必须一致）。
4. 总表四项（依赖矩阵、可变状态全清单、TOP10、Assembly 全景）齐备。
5. 所有接口签名与源码逐字一致（复审用 grep 对照）。

## 失败报告格式

若无法完成，停止并报告：
1. 已尝试的方案；
2. 实际输出或报错原文（引用，不得转述）；
3. 当前怀疑原因。
「确认失败」是合法终点，禁止伪造通过。
