# S9 规格写作任务：capability `tools-remote`（Terminal SSH / Serial 串口）

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `tools-remote` capability 编写行为规格。

`tools-remote` 覆盖：Terminal（多终端持久 SSH：会话管理、命令执行、交互输入、sudo 密码回填、文件传输）与 Serial（串口：扫描/连接/交互/裸监听）。

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R4_report_tools_terminal_serial.md` — 现状报告（terminal 全部 + serial 全部）
4. 需要核实时直接读源码（narnat_agent/tools/terminal/__init__.py、terminal/ssh_session.py、terminal/remote.py、tools/serial/__init__.py、serial/serial_session.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/tools-remote/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；**工具名（Terminal/Serial）、action 名、参数名、输出格式、错误文案属于对外契约，必须精确**
- **覆盖主题**（至少）：
  1. Terminal 参数契约（action 集合与各 action 的参数、默认值）
  2. SSH 会话管理：连接（认证方式：密码/密钥）、会话编号（0-4）、最大会话数（配置驱动、内部钳制范围）、状态查询、关闭、并发上限文案
  3. 命令执行：提示符检测（默认字符集与自定义正则）、输出清洗（ANSI 剥离、回显剔除）、退出码与错误标签、超时后行为（命令仍在运行、如何查看）、输出截断
  4. 交互输入：input 语义（发送输入/应答提示符）、sudo 密码自动回填（提示正则匹配、尝试上限、拒绝检测）、Ctrl+C（^C 发送）
  5. 断线重连：凭据复用、cwd 恢复、重连后状态
  6. 文件传输：本机↔远程、远程↔远程、大小上限（配置驱动）、路径校验、传输失败文案
  7. Serial 参数契约与行为：扫描（列出可用串口）、连接（波特率/数据位/校验/停止位/流控/行结束符参数及默认值）、exec 与 raw_exec（纯超时收集、不依赖提示符）、input、状态、关闭；会话数与编号
  8. Serial 与 Terminal 的对称面与差异（差异必须显式写出：如 raw_exec 无提示符检测、无超时后收敛机制等——以报告为准）
  9. 清理：会话清理（退出时/显式调用）语义；中断传播（ESC 中断远程执行的语义）
  10. CSV/设备引用约定：dev 编号体系（dev0=本机、dev1..n=被控设备）在工具参数与结果文案中的体现
- **兼容怪癖**：明显缺陷的边缘行为集中写入 `### Requirement: 兼容性怪癖保持`（如 Serial 配置未接线的现状——若重构中接线，则作为独立标注的"修复项"，不要默默改变行为）
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述一个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作；不发起任何 SSH/串口连接
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. Requirement 数 ≥ 10；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单 10 项逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号（工具名/参数名/输出文案除外）

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
