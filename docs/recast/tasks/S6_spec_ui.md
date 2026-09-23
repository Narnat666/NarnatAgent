# S6 规格写作任务：capability `ui`（交互界面）

## 背景

narnat agent 正在全面重构（change `recast-v2`）。**规格（specs）是行为金标准**：指导重写实现、验收行为等价。你负责为 `ui` capability 编写行为规格。

`ui` 覆盖：交互界面——流式 Markdown 渲染（表格稳定渲染/CJK 宽度/代码高亮）、输入会话（prompt_toolkit 语义）、命令注册与 Tab 补全、流会话句柄（feed/finish/abort 协议）、headless 纯文本输出。

## 输入素材（必读，按序）

1. `openspec/changes/recast-v2/proposal.md` — 重构范围
2. `openspec/changes/recast-v2/specs/config/spec.md` — **格式与粒度范本，严格模仿**
3. `docs/recast/reports/R6_report_ui.md` — 现状报告（renderer/ui_design/session_commands/headless/colors 部分）
4. 需要核实时直接读源码（ui/renderer.py、ui/ui_design.py、ui/session_commands.py、ui/headless.py）

## 写作要求

- 输出文件：`openspec/changes/recast-v2/specs/ui/spec.md`
- 格式：`# Spec Delta` + `## Purpose` + `## ADDED Requirements`；每条 `### Requirement:` ≥1 个 `#### Scenario:`，句式 `- **WHEN** …` / `- **THEN** …`
- 结构标题与 SHALL/MUST 保留英文；正文全部简体中文
- **规格是行为契约**：不写内部类名/函数名/行号/实现步骤；对用户可见行为（渲染效果、按键、提示符、命令列表、Tab 补全）必须精确
- **覆盖主题**（至少）：
  1. 流式渲染：Markdown 元素（标题/粗体/斜体/行内代码/链接/引用/列表/分隔线/任务框）增量渲染；渲染器缓冲语义（flush 落定/final 收尾/reset 清空）
  2. 表格稳定渲染：流式输入下的表格识别与稳定输出策略（不完整行的降级处理、跨行渲染规则）——以报告描述为准
  3. 代码块：围栏识别、语言标签、行号、背景色、增量渲染
  4. 宽度处理：CJK 双宽字符计算、终端宽度获取、软换行/断行规则
  5. 输入会话：`#` 提示符、多行输入（Alt+Enter/Ctrl+O/Alt+J 换行）、回车提交、历史文件、Ctrl+C/EOF 语义
  6. 命令注册与补全：`/` 开头命令的 Tab 补全（可用命令随会话状态变化）、命令分发结果语义（继续/跳过/退出）
  7. 流会话句柄协议：feed（内容/提示注入）、finish（统计栏显示参数）、abort（自定义消息/默认打断提示）、cancelled 查询、spinner 暂停/恢复（并行工具计数）、flush/reset 渲染缓冲——这是核心与 UI 的接口契约，写清语义不写实现
  8. spinner 动画：思考中（延迟启动）/正在压缩/正在合并三类动画的触发与停止时机、光标隐藏/恢复
  9. 状态栏（统计栏）：显示内容与格式（输入/输出/缓存/思考强度/窗口占比/最大输出/费用/余额）与各开关联动
  10. headless 输出：纯文本模式（无颜色、无动画）、静默工具日志开关、哨兵行输出
  11. 打断提示与输入态恢复：用户中断后的显示（"已打断/继续..."）
- **兼容怪癖**：明显缺陷的边缘行为集中写入 `### Requirement: 兼容性怪癖保持`
- 不要发明现状没有的行为；不要遗漏主流行为面

## 边界条款

- 只写上述一个文件；不改任何其他文件
- 不运行 narnat、不执行 git 写操作
- 完成后运行 `cd /d D:\desktop\NarnatAgent && openspec validate recast-v2`；只修自己文件的问题，错误指向他人文件则忽略并说明

## 验收标准（可计算）

1. 文件存在
2. `openspec validate recast-v2` 无指向本文件的错误
3. Requirement 数 ≥ 10；每条 Scenario 数 ≥ 1
4. 场景块均含 WHEN/THEN 行
5. 主题清单 11 项逐项映射到 Requirement（报告列出映射）
6. 全文无内部类名/函数名/行号

## 失败报告格式

若无法完成，停止并报告：①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
