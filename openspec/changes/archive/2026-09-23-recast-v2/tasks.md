# Tasks

> 实施原则：以 `specs/` 为行为金标准、`design.md` 为结构金标准；全部开发在 `v2/` 隔离目录进行，稳定版保持可运行；每块积木完成后立即测试并对照 spec 验收。
>
> **状态：全部完成**（2026-09-23；含切换与验证；实施后跟进见第 8 节）。切换后路径：`narnat_agent/`（新实现）、`tests/`（测试与基准）、根 `main.py`。

## 1. 工具链与骨架（Stage 0）

- [x] 1.1 创建 `v2/` 结构：`v2/narnat_agent/`（新包骨架）、`v2/tests/{unit,e2e,baseline}/`、`v2/main.py`（入口副本，导入新包）、`v2/pytest.ini`
- [x] 1.2 编写快照基准提取脚本 `tests/baseline/extract_old.py` 等：对旧实现运行，产出基准 JSON/文本（配置解析矩阵、压缩切点、标签编解码、thinking 映射、渲染纯函数输出、会话树文本、费用计算）
- [x] 1.3 编写分层检查脚本 `tests/check_layering.py`：验证 import 分层（app→业务→基础→contracts）、无同层互引、无模块级可变状态（白名单）、无跨模块 `_private` 访问
- [x] 1.4 生成并固化基线产物（提交 `tests/baseline/*`）

## 2. contracts 积木（Stage 1a）

- [x] 2.1 `contracts/llm_events.py`：事件类型定义（TypedDict，保 dict 契约）+ 键/取值常量；字段语义逐一对齐现状 chunk 协议
- [x] 2.2 `contracts/tool.py`：`Tool` Protocol、`ToolResult`、工具定义契约、确认挂起类型
- [x] 2.3 `contracts/output.py`：`OutputSink` Protocol、`InteractionPort` Protocol、`TurnStats`、`Animator`
- [x] 2.4 `contracts/interrupt.py`：中断信号协议与订阅接口
- [x] 2.5 contracts 单元测试（数据结构/谓词/常量），跑通分层检查

## 3. 基础积木（Stage 1b）

- [x] 3.1 `config`：loader/defaults/parsers/models/skill_store 重写；对照 `specs/config`；单测覆盖解析矩阵与容错；快照对照通过
- [x] 3.2 `output`：输出原语与颜色体系（色板/角色/配方/plain/quiet/DisplayState/VT）；对照 `specs/output`；快照对照（配方解析）；归一化归 `config`（D14）
- [x] 3.3 `interrupt`：InterruptBus 实例与订阅（LLM 断连/进程杀树/远程中断/等待唤醒）；对照 `specs/interrupt`
- [x] 3.4 `messages`：消息存储/视图/repair；对照 `specs/messages`；单测覆盖 repair 全分支
- [x] 3.5 `llm`：双协议后端/重试/thinking 映射/工具定义管理/事件产出；对照 `specs/llm`；单测（事件、映射表）与快照对照

## 4. 中层积木（Stage 2）

- [x] 4.1 `compression`：切点/摘要编排/溢出恢复/手动压缩；对照 `specs/compression`；快照对照（切点选择）
- [x] 4.2 `stats`：用量/费用/余额/成本日志轮转；对照 `specs/stats`；快照对照（费用计算）
- [x] 4.3 `tools` 框架：registry/signal（标签协议）/env（ToolEnv 与 Trackers）；对照 `specs/tools-*` 公共面；快照对照（标签编解码）
- [x] 4.4 `tools/file`：Read/Glob/Grep/Edit/Write；对照 `specs/tools-file`；单测（参数校验、编码、忽略目录）
- [x] 4.5 `tools/shell`：Shell + 后台任务；对照 `specs/tools-shell`；单测（参数、分段、截断文案）
- [x] 4.6 `tools/remote`：Terminal + Serial（含 D11 明示修复项：会话数配置接线）；对照 `specs/tools-remote`
- [x] 4.7 `tools/websearch` + `tools/plan`（TodoWrite/GoalComplete）；对照 `specs/tools-websearch`、`specs/tools-todo`
- [x] 4.8 `mcp`：客户端/管理器/连接工具；对照 `specs/mcp`
- [x] 4.9 `ui/render`：解析→布局→着色管道（保守搬运+快照锁定）；对照 `specs/ui` 渲染面
- [x] 4.10 `ui/stream`：OutputSink 实现（spinner/统计栏/中断提示）；对照 `specs/ui` 流协议面
- [x] 4.11 `ui/prompt`+`ui/commands`：输入会话/补全/命令分发；对照 `specs/ui` 输入面
- [x] 4.12 `ui/headless`：纯文本 OutputSink 与 InteractionPort 实现

## 5. 上层积木（Stage 3）

- [x] 5.1 `conversation`：内循环/调度/删除确认/流重试/溢出恢复/软提醒/目标模式；对照 `specs/conversation`
- [x] 5.2 `sessions`：三态状态机/持久化/探索分支/命令实现；对照 `specs/sessions`；快照对照（会话树文本）
- [x] 5.3 `ui` 装配整合：UIInterface 等价物（start/read_input/dispatch/create_stream/animator）

## 6. 应用层（Stage 4）

- [x] 6.1 `app/assembly`：线性装配（无后置补线）；对照 `specs/app` 装配面
- [x] 6.2 `app/interactive` + `app/headless`：主循环/目标模式续跑/哨兵输出/退出清理；对照 `specs/app`
- [x] 6.3 `main.py` 与根 `main.py` 导入对齐（切换时已完成）

## 7. 验证与切换（Stage 5）

- [x] 7.1 全量单元测试通过（`python -m pytest tests/unit -q`）
- [x] 7.2 快照对照全部通过（新实现 vs 基线）
- [x] 7.3 headless 端到端冒烟：版本/单轮对话/工具调用（读/写/命令）/目标模式哨兵/中断
- [x] 7.4 人工冒烟清单：自动化等价验证（管道逐字节对照、编译版 e2e、真机按键注入矩阵）已覆盖主要路径；真实终端交互复核为用户可选（清单见 `docs/recast/PROGRESS.md`）
- [x] 7.5 切换提交：删除旧包 → 新实现与测试上移 → 根 `main.py` 更新导入 → 冒烟复验（旧实现留存于 `_old_impl_rollback/`）
- [x] 7.6 归档：`openspec archive recast-v2`（规格进入 `openspec/specs/`）

## 8. 实施后跟进：ESC 打断修复（已完成）

> 触发：用户报告「ESC 打断不丝滑」（界面卡"思考中" 5~11.5 秒后突然显示"已打断"；按键密集交错时可能完全无法打断）。
> 探索与证据：`docs/recast/reports/E1_esc_probe.md`（机制级）、`E2_esc_repro.md`（真机复现）、`docs/recast/esc_review_notes.md`（根因链综合）、探针脚本 `docs/recast/esc_probe/`、`docs/recast/esc_probe_live/`。

- [x] 8.1 根因闭环：请求在 connect 阶段时 `close()` 对在途请求无效（补证实验 exp4）+ 发送期无取消检查点 + 按键层吞键/积压（E1/E2 证实，属历史缺陷）
- [x] 8.2 修复（llm 层）：`llm/cancelable.py` 可取消等待原语（阻塞发送入子线程、0.05 秒轮询取消、取消即收敛 + 双侧自毁）；两后端接入；openai 共享客户端改请求级 scope（消除"打断一次后整局失败"）
- [x] 8.3 修复（按键层）：Windows 主路径改 `ReadConsoleInput` 事件源（吞键根除；msvcrt 降级保留）；中断后继续消费按键（幽灵输入根除）
- [x] 8.4 规格同步：`specs/interrupt`（键盘监听生命周期 / 平台按键采集差异 / 兼容怪癖）与 `specs/llm`（取消与中断）更新为有意变更
- [x] 8.5 验证：单测 1674 通过 + 分层检查 + 真机按键注入矩阵（18/18 次打断 ≤0.2s，对照修复前 5~11.5s）+ 回归冒烟 + 编译版 e2e
