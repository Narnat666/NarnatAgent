# Tasks

> 实施原则：以 `specs/` 为行为金标准、`design.md` 为结构金标准；全部开发在 `v2/` 隔离目录进行，稳定版保持可运行；每块积木完成后立即测试并对照 spec 验收。

## 1. 工具链与骨架（Stage 0）

- [ ] 1.1 创建 `v2/` 结构：`v2/narnat_agent/`（新包骨架）、`v2/tests/{unit,e2e,baseline}/`、`v2/main.py`（入口副本，导入新包）、`v2/pytest.ini`
- [ ] 1.2 编写快照基准提取脚本 `v2/tests/baseline/extract.py`：对旧实现运行，产出基准 JSON/文本（配置解析矩阵、压缩切点、标签编解码、thinking 映射、渲染纯函数输出、会话树文本、费用计算）
- [ ] 1.3 编写分层检查脚本 `v2/tests/check_layering.py`：验证 import 分层（app→业务→基础→contracts）、无同层互引、无模块级可变状态（白名单）、无跨模块 `_private` 访问
- [ ] 1.4 生成并固化基线产物（提交 `v2/tests/baseline/*`）

## 2. contracts 积木（Stage 1a）

- [ ] 2.1 `contracts/llm_events.py`：事件类型联合（ContentDelta/ThinkingDelta/ToolCallsReady/UsageReport/StreamFinished+FinishReason/StreamInterrupted/RetryNotice）+ 空响应诊断；字段语义逐一对齐现状 chunk 协议
- [ ] 2.2 `contracts/tool.py`：`Tool` Protocol、`ToolResult`、工具定义数据类、确认挂起类型
- [ ] 2.3 `contracts/output.py`：`OutputSink` Protocol、`InteractionPort` Protocol、`TurnStats`
- [ ] 2.4 `contracts/interrupt.py`：`InterruptBus` 协议与订阅接口
- [ ] 2.5 contracts 单元测试（数据结构序列化/构造），跑通分层检查

## 3. 基础积木（Stage 1b，可并行）

- [ ] 3.1 `config`：loader/defaults/skill_store 重写；对照 `specs/config`；单元测试覆盖解析矩阵与容错；快照对照通过
- [ ] 3.2 `output`：输出原语与颜色体系（色板/角色/配方/plain/quiet/DisplayState/VT）；对照 `specs/output`；快照对照（配方解析）
- [ ] 3.3 `interrupt`：InterruptBus 实例与订阅（LLM 断连/进程杀树/远程中断/等待唤醒）；对照 `specs/interrupt`
- [ ] 3.4 `messages`：消息存储/视图/repair；对照 `specs/messages`；单测覆盖 repair 全分支
- [ ] 3.5 `llm`：双协议后端/重试/thinking 映射/工具定义管理/事件产出；对照 `specs/llm`；单测（事件、映射表）与快照对照

## 4. 中层积木（Stage 2，按依赖并行）

- [ ] 4.1 `compression`：切点/摘要编排/溢出恢复/手动压缩；对照 `specs/compression`；快照对照（切点选择）
- [ ] 4.2 `stats`：用量/费用/余额/成本日志轮转；对照 `specs/stats`；快照对照（费用计算）
- [ ] 4.3 `tools` 框架：registry/signal（标签协议）/env（ToolEnv 与 Trackers）；对照 `specs/tools-*` 公共面；快照对照（标签编解码）
- [ ] 4.4 `tools/file`：Read/Glob/Grep/Edit/Write；对照 `specs/tools-file`；单测（参数校验、编码、忽略目录）
- [ ] 4.5 `tools/shell`：Shell + 后台任务；对照 `specs/tools-shell`；单测（参数、分段、截断文案）
- [ ] 4.6 `tools/remote`：Terminal + Serial（含 D11 明示修复项：会话数配置接线）；对照 `specs/tools-remote`
- [ ] 4.7 `tools/websearch` + `tools/plan`（TodoWrite/GoalComplete）；对照 `specs/tools-websearch`、`specs/tools-todo`
- [ ] 4.8 `mcp`：客户端/管理器/连接工具；对照 `specs/mcp`
- [ ] 4.9 `ui/render`：解析→布局→着色管道（保守搬运+快照锁定）；对照 `specs/ui` 渲染面
- [ ] 4.10 `ui/stream`+`ui/output-sink`：OutputSink 实现（spinner/统计栏/中断提示）；对照 `specs/ui` 流协议面
- [ ] 4.11 `ui/prompt`+`ui/commands`：输入会话/补全/命令分发；对照 `specs/ui` 输入面
- [ ] 4.12 `ui/headless`：纯文本 OutputSink 与 InteractionPort 实现

## 5. 上层积木（Stage 3）

- [ ] 5.1 `conversation`：内循环/调度/删除确认/流重试/溢出恢复/软提醒/目标模式；对照 `specs/conversation`
- [ ] 5.2 `sessions`：三态状态机/持久化/探索分支/命令实现；对照 `specs/sessions`；快照对照（会话树文本）
- [ ] 5.3 `ui` 装配整合：UIInterface 等价物（start/read_input/dispatch/create_stream/animator）

## 6. 应用层（Stage 4）

- [ ] 6.1 `app/assembly`：线性装配（无后置补线）；对照 `specs/app` 装配面
- [ ] 6.2 `app/interactive` + `app/headless`：主循环/目标模式续跑/哨兵输出/退出清理；对照 `specs/app`
- [ ] 6.3 `v2/main.py` 与根 `main.py` 导入对齐（切换前用 v2 版测试）

## 7. 验证与切换（Stage 5）

- [ ] 7.1 全量单元测试通过（`pytest v2/tests/unit`）
- [ ] 7.2 快照对照全部通过（新实现 vs 基线）
- [ ] 7.3 headless 端到端冒烟：版本/单轮对话/工具调用（读/写/命令）/目标模式哨兵/中断
- [ ] 7.4 人工冒烟清单：启动/流式渲染（含表格/代码块/CJK）/输入编辑/命令与补全/压缩提示/会话保存切换/退出清理
- [ ] 7.5 切换提交：`git rm -r narnat_agent` → `git mv v2/narnat_agent narnat_agent` → `git mv v2/tests tests` → 根 `main.py` 更新导入 → 删除 `v2/` 残留 → 冒烟复验
- [ ] 7.6 归档：`openspec archive recast-v2`（规格进入 `openspec/specs/`）
