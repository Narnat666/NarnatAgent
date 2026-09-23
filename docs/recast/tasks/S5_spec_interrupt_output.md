# S5 规格写作任务：capability `interrupt` + `output`

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `interrupt` 与 `output` 两个 capability 编写行为规格。

- `interrupt` 覆盖：中断系统（ESC/SIGINT 信号采集、运行/输入双模式轮询、对 LLM/工具/后台的取消广播）
- `output` 覆盖：终端输出与颜色（色板+角色引用+配方解析、plain/quiet 模式、Windows VT 启用、DisplayState 显示开关）

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R6_report_ui.md` — 现状报告（output.py 与 ui/interrupt.py 部分）
4. `docs/recast/reports/R1_report_core_dialog.md` — 现状报告（core/interrupt.py 部分）
5. 需要核实时直接读源码（output.py、ui/interrupt.py、core/interrupt.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/interrupt/spec.md` 与 `openspec/changes/recast-v2/specs/output/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；对用户可观察的行为（按键效果、颜色配置键、显示开关、文案）必须精确
- **interrupt 覆盖主题**（至少）：
  1. 采集：Esc 按键与 Ctrl+C（SIGINT）两条来源；运行模式下 Esc 中断当前 AI 输出/工具执行
  2. 双模式：运行模式（输出/工具执行中，轮询采集）vs 输入模式（等待输入时，中断不误触发）；模式切换时机（流创建/结束/中止）
  3. 广播：中断信号传播至 LLM 请求（关闭连接）、前台 Shell 进程（杀进程树）、远程执行（SSH 会话）、后台任务等待；各订阅者的中断语义（等待中断不杀后台任务本身等）
  4. 键盘监听生命周期：仅在需要时轮询，输入态不误吞按键
  5. 平台差异：Windows（msvcrt/conhost）与 POSIX（termios）的按键读取差异（行为等价描述，不写库名）
- **output 覆盖主题**（至少）：
  1. 颜色体系：色板（具名色值，含十六进制）+ 角色引用（基础色/标注/代码块/差异/框架/命令/提示符分组）；配方组合（空格分隔如 `bold 蓝`、`italic dim 次文字`）；中文色名与英文别名
  2. 全局开关：plain（纯文本，无色，headless 用）与 quiet（静默工具日志）语义及影响面
  3. 终端能力适配：Windows VT 序列启用、truecolor 检测与降级、非 TTY/重定向场景
  4. DisplayState 显示开关：显示费用/显示余额/最大输出 token/窗口占比/上下文窗口的数据流（自配置→显示层）
  5. 主题应用时机：启动时应用一次（apply_style 语义），运行时颜色实例全局生效
  6. 输出原语语义：write/try_write 的差异（静默失败）、行首控制（spinner 擦除的 \r 行为）
- **兼容怪癖**：明显缺陷的边缘行为集中写入各自 `### Requirement: 兼容性怪癖保持`（如 interrupt 报告中的死逻辑，注明"保持现状"或按报告行为精确描述）
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述两个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 两个文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. interrupt Requirement 数 ≥ 5、output Requirement 数 ≥ 6；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单（interrupt 5 项、output 6 项）逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
