# S8 规格写作任务：capability `tools-shell`（Shell 与后台任务）

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `tools-shell` capability 编写行为规格。

`tools-shell` 覆盖：Shell 工具（本地命令执行：平台自适应、cd 持久化、多段执行、python 直执行、输出截断）+ 后台任务机制（槽位、提交/等待/状态/取消）+ 退出码标签协议（exec_signal）。

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R3_report_tools_bash_bg.md` — 现状报告（bash/background/exec_signal 全部）
4. 需要核实时直接读源码（narnat_agent/tools/bash/__init__.py、tools/background/__init__.py、tools/exec_signal.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/tools-shell/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；**工具名、参数名、输出文本格式（`[exit code: N]`、`[stderr]`、`[用户中断]`、`[超时: …]`、`[跳过: …]`、提示符等）属于对外契约，必须精确**
- **覆盖主题**（至少）：
  1. Shell 工具参数契约（command/timeout/max_output_chars/max_output_tokens 别名/background/bg/id；无必填参数）+ 参数归一化（字符串数字、别名覆盖、非法值报错文案）
  2. 安全确认：删除类命令与 git 命令的确认规则（rm 免确认/git 免确认开关）；Windows 同步确认回调；非 Windows 挂起确认机制（返回 AWAIT 标记 + 暂存命令 + 主循环确认后重执行）
  3. 前台执行：平台自适应（Windows cmd / POSIX bash）、超时终止（杀进程树）、中断（ESC 杀前台进程、输出保留、无退出码行）、输出解码（UTF-8/GBK 回退）
  4. 结果格式：正常（退出码行+标签+stdout+stderr 段+提示符）、中断（无退出码行）、超时（带错误标签+提示文案）、各自精确格式
  5. cd 持久化：纯 cd 由宿主直接改变工作目录（后续工具共享）；无参数 cd 平台语义差异（cmd 显示当前目录 / bash 回 $HOME）；cd 失败文案
  6. 多段命令：`&&`/`||` 分段（引号外/括号外/转义跳过）、跳过文案、每段独立超时预算递减、段失败退出码语义、总退出码行、cd 段特殊处理
  7. python 命令直执行（Windows 单段与各平台多段）：命中条件语义（解释器名/全路径、`-c` 载荷、后缀/管道/重定向剥离）、绕 cmd 直启、`>nul`/`>file`/`>>file`/`| cmd` 后缀语义
  8. 输出截断：保留首尾比例、中段截断提示文案（含 ≈token）、框架标签不被切开
  9. 后台任务：提交（background=true 或 bg 参数）返回句柄与结果文件路径、槽位机制（8 槽、编号复用、终态释放）、状态查询、等待（任一完成即返回/超时/中断等待不杀任务）、取消（杀进程树）、输出落盘与归档（.prev）、会话隔离（每进程专属临时目录）、启动预清与结束清理
  10. 退出码标签协议：随机标签附加/剥离语义（AI 可见文本不含随机标签、UI 判定不受命令输出伪造影响）、错误标签与 `[exit code: N]` 判定顺序
  11. 工具描述文本要点（平台标签渲染）
- **兼容怪癖**：明显缺陷的边缘行为集中写入 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述一个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作；不实际执行任何命令
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. Requirement 数 ≥ 11；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单 11 项逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号（工具名/参数名/输出文案除外）

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
