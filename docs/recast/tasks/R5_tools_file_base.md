# R5 调研任务：文件类工具与工具基建（read/glob/grep/edit/write/web/todo/registry 等）

## 任务目标

narnat agent（工作目录 `D:\desktop\NarnatAgent`）即将**全面重构**：以"积木思想"重写为结构清晰、可独立测试、故障隔离的新架构，功能与现有稳定版等价（用户无感知）。重构前需要精确掌握现状。

你负责调研 **文件类工具与工具基建**，产出结构化现状报告。报告双重用途：
1. 供架构师编写行为规格（specs，行为金标准）——需要精确到"行为"层面；
2. 供架构师设计新结构——需要精确到"职责/接口/状态/依赖"层面。

报告读者是没有读过这些代码的架构师，必须自包含、可核对、不臆测。

## 调研对象（逐文件**完整通读**，禁止只 grep 不读全文）

| 文件 | 行数(约) |
|------|------|
| narnat_agent/tools/grep/__init__.py | 591 |
| narnat_agent/tools/glob/__init__.py | 576 |
| narnat_agent/tools/read/__init__.py | 247 |
| narnat_agent/tools/edit/__init__.py | 230 |
| narnat_agent/tools/write/__init__.py | 129 |
| narnat_agent/tools/web_search/__init__.py | 178 |
| narnat_agent/tools/registry.py | 197 |
| narnat_agent/tools/tool_context.py | 120 |
| narnat_agent/tools/mcp_tool/__init__.py | 111 |
| narnat_agent/tools/diff_utils.py | 80 |
| narnat_agent/tools/param_utils.py | 60 |
| narnat_agent/tools/token_estimate.py | 40 |
| narnat_agent/tools/todo_write/__init__.py | 60 |
| narnat_agent/tools/goal_complete/__init__.py | 40 |
| narnat_agent/tools/__init__.py | ? |

产出报告到：`D:\desktop\NarnatAgent\docs\recast\reports\R5_report_tools_file_base.md`

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
- 每个工具的 **DEFINITION 数据契约**（JSON schema 参数定义全文——这直接进 specs）。
- read 的**编码自动识别**（UTF-8/GBK）、行号、截断与续读提示、远程读取（device 参数）。
- glob 的 `**` 与 `{}` 花括号展开、按修改时间倒序、忽略目录（ignore_dirs）过滤逻辑。
- grep 的**多路径数组**、head_limit 降级为文件清单规则、忽略目录过滤、二进制文件跳过。
- edit 的**编码与换行符保持**、精确匹配替换、replace_all。
- registry 的**工具注册机制**（注册表结构、get_tool_definitions 过滤规则、GoalComplete 动态注入/移除）。
- tool_context 的**运行时上下文**（已读文件追踪、删除确认状态、todo 状态、api_keys、配置下发）——这是跨工具共享状态的枢纽，要写全。
- param_utils 的参数校验/中文错误消息生成模式。
- diff_utils 的 diff 生成与着色。
- token_estimate 的估算算法。

报告结尾附**总表**：

```markdown
## 总表
### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）
### 模块级可变状态全清单（含读写方）
### 补丁痕迹 TOP10（按严重度排序，含文件:行号）
### 本组对外契约清单（被本组之外模块依赖的 public API，即新架构必须保持的行为面）
```

## 边界条款

- **只读任务**：除产出报告文件外，禁止创建/修改/删除任何文件；禁止一切 git 写操作（add/commit/checkout/stash 等）；git 只读命令（log/show/diff/status）可用。
- 禁止运行 narnat / python main.py 等会发起网络调用或启动进程的命令；禁止执行 web_search（网络调用）。
- 允许：Read、Grep、Glob、dir、git log/show/diff。
- 报告若超 1500 行，可拆为 `R5_report_tools_file_base_part1.md` / `_part2.md`，并在首文件注明。
- 不确定的事项明确标注「未验证」，禁止臆测。

## 验收标准（可计算判据）

1. 报告文件存在，覆盖清单中全部 15 个文件（每个文件一个 `###` 节，缺一即不合格）。
2. 每个"补丁痕迹"和"行为要点"条目均附行号。
3. "被依赖"清单可用 grep 复验（抽查 5 条必须一致）。
4. 总表三项（依赖矩阵、可变状态全清单、TOP10）齐备。
5. 所有接口签名与源码逐字一致（复审用 grep 对照）。

## 失败报告格式

若无法完成，停止并报告：
1. 已尝试的方案；
2. 实际输出或报错原文（引用，不得转述）；
3. 当前怀疑原因。
「确认失败」是合法终点，禁止伪造通过。
