# Design

## Context

见 `proposal.md` 的 Why。设计相关的现状约束（来源：`docs/recast/reports/` 8 份现状调研）：

- 现有包 `narnat_agent/` 约 18,457 行、100 个文件，功能稳定（v16.2.5），零测试。
- 历史上已做过一次 OOP 重构（`docs/architecture-refactor-summary.md`），但仍遗留：4 处装配后置补线（`summary_anim_*`/`_set_model`/`compact_func`/`set_tool_sinks`）、12 个 lambda 接线、模块级可变态（`_interrupt_ctrl`/`_abort_callback`/`_PLAIN`/`_QUIET_TOOLS`/`Background._slots` 等）、私有互摸（`_goal_enabled`/`_last_round_ok`/`_executor`/`_client`/`_channel` 等 30+ 处）、职责混杂（900 行会话类、调度器含渲染代码）。
- 运行约束：重构期间现有稳定版必须保持可用（子代理/主代理运行于其上）；新代码需在隔离位置开发，验证后一次性切换。
- 平台约束：Windows（cmd）为主开发平台，行为需兼容 Linux/macOS（bash）。
- 外部契约冻结：narnat.json 键、会话文件、磁盘布局、CLI、命令集、输出格式、`[NN_DONE]` 哨兵、工具名/参数/输出文本全部不变。

## Goals / Non-Goals

**Goals:**
- 以"积木"为单元重组包结构：每个积木独立目录、独立可测、故障隔离；跨积木只走显式接口（`contracts/`）。
- 消灭调研确认的全部结构问题：私有互摸、可变全局、后置补线、职责混杂、结构性重复、死代码/断链。
- 建立三层测试体系（单元 / 快照对照 / 端到端冒烟），为"行为等价"提供可计算证据。
- 依赖方向可机械检查（import 分层规则），防止回潮。

**Non-Goals:**
- 不新增用户可见功能；不修改配置格式、文件格式、命令集、输出样式（除明示修复项）。
- 不优化性能指标（保持现状等价即可）。
- 不改编译链路（Nuitka 命令与产物形态不变）。
- 不清理 `tool_exp/`、`subagent_test/` 等历史实验目录（不属于交付面）。

## Decisions

### D1. 积木化目录 + 单向 contracts 层

新包按能力域切为 15 个顶层积木，跨积木接口与数据类集中于 `contracts/`（纯定义、零逻辑）：

```
narnat_agent/
├── app/            # 组装（唯一构造点）、交互/headless 主循环、生命周期清理
├── contracts/      # 共享契约：llm_events / tool / output / interrupt（Protocol+dataclass）
├── config/         # narnat.json/narnat.md/skills 加载、默认值、路径
├── llm/            # 双协议客户端、重试、thinking 映射
├── messages/       # 消息唯一所有者、只读视图、repair
├── compression/    # 切点选择、摘要编排、溢出恢复
├── conversation/   # 内循环、工具调度、目标模式、软提醒
├── sessions/       # 三态状态机、持久化、命令实现
├── stats/          # 用量/费用/余额
├── interrupt/      # 中断总线（实例）
├── output/         # 输出原语与颜色体系
├── ui/             # 渲染、输入、流句柄、补全、headless 输出
├── tools/          # 注册表、标签协议、工具族（file/shell/remote/websearch/plan）
├── mcp/            # MCP 客户端与管理器
└── __init__.py
```

依赖规则（机械可查）：
`app → {conversation, sessions, ui, tools, mcp, stats, compression} → {llm, messages, output, interrupt, config} → contracts`
同层之间只许经 `contracts` 交互；任何积木不得 import `app`。

**理由**：现状 `core/tools/ui` 三分导致跨层直取（`core/agent_loop.py → tools/background`、`ui/interrupt.py → tools/bash`、`mcp → tools/registry` 等函数内延迟导入规避循环）。contracts 层把"共享面"从实现中剥离，循环自然消失。
**备选**：① 维持现状三分（问题保留）；② 每积木自带接口模块（循环依赖无法根治）；③ 单一 contracts.py 大文件（粒度过粗，违背积木隔离）。

### D2. LLM 事件流显式化（TypedDict 集中定义）

`llm` 积木产出的事件保持**字典形态**（与 `specs/llm` 的契约逐字一致），但键集与取值集中在 `contracts/llm_events.py` 以 TypedDict + 联合类型定义：内容增量、工具调用就绪、用量报告、完成（含思考与签名）、流中断（kind/detail）、重试通知、错误/超限完成。消费方（conversation）按类型标注读取，键名不再散落即兴判断。

**理由**：现状 8 个消费点依赖 dict 键集隐式协议（`"content" in chunk` 式探测），键集语义无单一定义处；同时规格将 dict 键集定义为已发布契约（快照对照零转换成本），dataclass 化会引入不必要的序列化适配与搬运风险。TypedDict 单一定义处 + 类型标注即达成"显式协议"目标。
**备选**：① dataclass 事件联合（类型最强，但契约转换成本高、搬运风险大）；② 保持 dict 散落使用（隐式协议永存）。
**约束**：字段与取值集逐一对齐现状语义（等价性优先），不做语义升级；`llm` 另提供类型化辅助谓词（如判断事件种类）供消费方使用。

### D3. 工具协议显式化

`contracts/tool.py` 定义 `Tool` Protocol：`name` / `definition()` / `execute(args, env) → ToolResult`。`ToolResult` 承载：给模型的文本（已剥标签）、UI 展示文本（着色 diff 等）、确认挂起请求（如有）、失败标记。工具实现各自独立模块，装配时注册进注册表。

**理由**：现状工具 = 模块级 `execute(**kwargs)` 函数 + 模块级 `DEFINITION` + `(llm_result, color_diff)` 元组返回 + `_tool_context` 注入参数，四个约定散落且靠 registry 桥接。显式协议让"新增一块积木（工具）不改通途"（需求文档场景 A）成立。
**备选**：保持函数式（改动最小但契约弱）。

### D4. 输出与交互端口化

`contracts/output.py` 定义两个端口：
- `OutputSink`（一个 AI 回合的输出通道）：`feed(内容)` / `notify(状态提示)` / `finish(TurnStats, with_stats)` / `abort(message)` / `restart_attempt()`（重试重播：此前未完成内容作废、渲染缓冲清理）/ `cancelled` / `aborted`。
- `InteractionPort`（会话级交互）：`begin_turn() → OutputSink` / `read_confirmation(prompt) → bool` / `notify_interrupted()`。

交互模式由 `ui` 实现（渲染+spinner+统计栏），headless 由纯文本实现；`conversation` 只依赖端口。

**理由**：现状 `AgentLoop` 直接持有 `UIInterface`/`UIStreamSession` 具体对象（duck typing 方法集：begin/feed/finish/abort/pause_spinner/resume_spinner/flush_renderer/reset_renderer），headless 靠另一套对象满足同一隐式接口。端口化后两种运行模式共用同一内循环，接口可文档化、可 mock。
**备选**：保留隐式 duck typing（headless 分支继续特化）。
**约束**：spinner/flush/reset 等渲染细节收进 sink 实现内部；`restart_attempt` 语义对齐现状 `reset_renderer` + 重播行为。

### D5. 中断总线实例化

模块级 `_interrupt_ctrl`（UI 层单例）与 `_abort_callback`（core 层全局）合并为 `interrupt` 积木的 `InterruptBus` 实例：UI 采集按键 → `bus.raise_()`；订阅方（LLM 客户端断连、前台进程杀树、远程执行中断、后台等待唤醒）在构造时 `bus.subscribe(handler)`。双模式（运行/输入）语义、轮询时机保持现状等价。

**理由**：全局单例是隐式依赖与测试隔离的头号敌人；实例可注入、可观测、可独立测试。
**备选**：保留全局（最小改动）。

### D6. 状态归主：Trackers 替换杂物袋

拆解现状 `ToolContext`（配置+状态+回调混装）为职责单一的小对象，各有明确属主，装配注入：

| 对象 | 承载 | 主要读写方 |
|---|---|---|
| `ToolEnv` | 只读配置与认证面（忽略目录、密钥、输出/超时上限、免确认开关、计划开关、传输上限） | 各工具只读 |
| `PlanTracker` | 当前 todo 列表 | TodoWrite 写；会话收尾提醒/UI 读 |
| `GoalState` | 目标完成标记 | GoalComplete 写；主循环读并复位 |
| `ReminderState` | 软提醒已触发标志（计划/后台） | 内循环写；新任务复位 |
| `DeleteGate` | 删除确认挂起项与"已确认"一次性标记 | 工具写挂起；内循环确认后消费 |
| `ReadFileTracker` | 已读文件集合 | Read/Edit 交互 |

**理由**：现状 `_delete_confirmed`/`goal_complete`/`todo_reminded`/`bg_reminded` 等私有标志被跨模块直接读写（含主循环跨对象摸 `session_mgr._goal_enabled`），"所有权"名存实亡。归主后每项状态只有一个修改入口。
**备选**：保留单 dataclass（最小改动，杂物袋永存）。

### D7. 全面消灭私有互摸

所有现有跨对象私有访问点（30+ 处，见汇报）逐一改为公开 API，典型：
- `agent → session_mgr._goal_enabled/_goal_max_rounds` → `sessions` 暴露目标模式设置/查询 API；
- `agent → agent_loop._last_round_ok/_last_content_parts` → 内循环返回 `TurnOutcome`（正常完成/中断/异常/空回复 + 已产出内容）；
- `agent → dispatcher._executor` → 线程池生命周期归 `app` 持有；
- `session_callbacks → stats._model` → `stats.set_model()`；
- `terminal/ui → SSHSession._client/_channel` → 会话对象暴露 `send_interrupt()` / `is_closed()` / `open_sftp()`；
- `session_mgr.compact_func/summary_anim_*` 后置赋值 → 构造注入（见 D8）。

**理由**：需求文档"积木之间只靠显式接口连接，不掏对方内部"。私有访问是"换一块积木牵动全身"的直接原因。

### D8. 装配线性化，拒绝后置补线

`app/assembly.py` 是唯一构造点，构造顺序即依赖顺序，禁止构造后裸赋值。现状 4 处后置接线逐一消解：
- 会话总结动画 → `sessions` 依赖 `Animator` 端口（`ui` 与 headless 各自实现），构造注入；
- 模型切换同步费用 → `stats` 暴露 `set_model()`，由模型切换命令的实现方调用（或模型状态收进单一 `ModelState`）；
- `/compact` 命令 → 调整构造顺序（先 `compression` 后 `sessions`），构造注入；
- MCP 工具热注册 → `mcp` 依赖 `ToolCatalog` 端口（`llm` 实现"增删工具定义"），构造注入。

如遇真正的双向关系（UI ↔ 命令），采用"端口 + 显式 attach"（如 `ui.attach_commands(session_commands)`），attach 是一次性显式 API 而非散落 lambda。
**理由**：12 lambda + 4 后置赋值的装配是"依赖关系不清晰"的根源；线性装配让依赖图可读、可测。

### D9. 隔离开发与一次性切换

- 新包开发于 `v2/`（`v2/narnat_agent/` 新包 + `v2/tests/` 测试 + `v2/main.py` 测试入口副本）。稳定版 `narnat_agent/` 原位不动。
- 完成门禁：单元测试全绿 + 快照对照通过 + headless 端到端冒烟通过 + 人工检查清单通过。
- 切换：`git rm -r narnat_agent` → `git mv v2/narnat_agent narnat_agent` → `git mv v2/tests tests` → 根 `main.py` 更新导入路径（一行）→ 删除 `v2/` 残留 → 冒烟复验。
- 回滚：切换前一切改动均在 `narnat_v2` 分支；切换是单一提交，回退即 `git revert`/`reset`。

**理由**：重构期间工具链（nn 子代理）必须持续可用；分阶段"边改边用"会把新旧混合成更大的补丁堆，正违背本次目的。
**备选**：原地渐进替换（风险：重构期自身不可用；混装结构）。

### D10. 三层测试体系

1. **单元测试**（pytest）：确定性纯逻辑——配置解析矩阵、消息 repair 规则、压缩切点、标签协议编解码、thinking 映射表、工具参数校验、渲染纯函数（行解析/宽度/表格）、会话名解析等。
2. **快照对照测试**：旧实现生成基准（脚本对旧包运行提取输出），新实现比对。覆盖：配置解析全矩阵、压缩切点选择、标签编解码、渲染函数输出、会话树格式化文本、费用计算。
3. **端到端冒烟**（headless，真实 LLM）：版本输出、单轮对话、工具调用（读/写/命令）、目标模式与 `[NN_DONE]` 哨兵、中断行为。
   UI 流式交互路径无法自动化部分：人工冒烟清单（启动/输入/流式渲染/中断/压缩/会话切换/退出）。

**理由**：功能等价需要可计算证据；"对旧实现生成基准"是最强等价判据，且不需要旧实现先有测试。
**备选**：只写新实现测试（无法证明等价）。

### D11. 兼容性策略与明示修复项

- 对外契约全部冻结；调研报告中的"兼容怪癖"保持等价（如字符串布尔容错、`忽略目录` 字符串拆分、120+ 处吞异常行为按报告逐项核对）。
- 明示修复项（有意为之的行为变化，除此之外零差异）：
  1. `工具.SSH最大会话数` 对串口生效（现状断链：配置只喂给 SSH 终端，串口恒用默认 5）——默认值场景无差异；
  2. 清理确认无调用者的死代码（如 `parse_rc`、`_saved_sigint`），清理不改变任何可观察行为；
  3. 会话保存失败路径：现状静默忽略错误（UI 误报成功）；新实现原样报错且不切换会话状态（仅在文件系统异常时可见）；
  4. 会话删除失败路径：现状抛异常可致程序异常退出；新实现静默忽略失败（仅在文件系统异常时可见）。

**理由**：用户要求"用户无感知"，必须把有意差异控制到最小并显式记录，杜绝"顺手优化"。

### D12. 渲染器保守搬运

`ui/render` 的 Markdown 流式渲染是"多轮补丁堆叠区"（表格四形态路由、稳定渲染策略）。重写按"解析 → 布局 → 着色"管道切分，但**算法语义逐函数搬运核对**，并用快照测试锁定输出（含 CJK 宽度、表格降级、代码块增量）。

**理由**：渲染是用户可见度最高的部分，行为面复杂；结构整理与算法保守是最好的组合。

### D13. 依赖检查机械化

提供一个 import 分层检查脚本（`v2/tests/check_layering.py`）：扫描新包 import 图，验证：无跨层 import、无积木互引、无模块级可变状态（白名单除外）、无 `_private` 跨模块访问（静态检查约定）。作为测试门禁之一。

**理由**：规则不被检查就会回潮；积木架构需要机械化护栏。

### D14. 配置归一化唯一归属 config

「界面」配置的归一化（中文键→英文、扁平键迁移、开关弹出、配方值中文色名替换）唯一实现在 `config` 积木；`output.apply_style` 为纯消费（直接读标准英文分组），对齐旧实现的职责边界与基准用例构造方式（`v2/tests/baseline/data/output_style.json`）。

**理由**：审计发现双实现已出现裁决分歧（config：英文键优先/直赋覆盖；output：中文优先/不覆盖）——同层双轨即"补丁痕迹"的再生。归一化属配置解析职责；output 职责为颜色装配。
**备选**：① 归一化归 output（与"配置解析"语义错位）；② 保留双实现（分歧持续存在）。
**约束**：中英键并存裁决规则以旧实现为唯一事实源（顶层开关英文优先；分组名迁移仅当英文键不存在；组内键与旧扁平键直赋覆盖），已同步修正 `specs/config` 与 `specs/output`。

## Risks / Trade-offs

- [行为等价验证覆盖不全（UI 时序/线程路径无法自动化）] → 渲染与中断类代码保守搬运 + 人工冒烟清单 + 快照测试兜底确定性部分；报告"未验证事项"逐条列入实施检查单。
- [规模大（18k 行重写），子代理产出质量波动] → 逐积木任务书+验收（换手段复验）；每块完成后立即测试，不合格重派；规格（specs）作为唯一行为判据。
- [Windows/Linux 平台差异被忽略] → 平台分支行为逐条来自报告"边界行为"清单；Windows 实测 + Linux 路径静态核对（不改动既有分支结构）。
- [LLM 厂商适配细节（thinking 回传/参数映射）出错代价高] → 映射表逐行搬运 + 单测覆盖每个厂商分支。
- [快照测试的"基准"本身来自旧实现，可能固化旧 bug] → 这是刻意选择：本次目标即"等价"，旧 bug 属于行为面；除 D11 明示修复项外不修正。
- [切换时新包启动失败导致工具链中断] → 切换前在 `v2/` 完成全部 headless 冒烟（用真实密钥）；切换后立即复验；git 分支提供一键回滚。

## Migration Plan

1. 规格与设计冻结（本 change 的 proposal/specs/design/tasks 完成）→ `openspec validate` 通过。
2. 逐积木实施（见 tasks.md）：在 `v2/` 开发 + 测试；每块独立验收。
3. 集成：`v2/` 内完成装配 + 端到端冒烟（headless 真实任务集）。
4. 切换（单一提交）：删除旧包、上移新包与测试、更新根 `main.py` 导入；运行完整冒烟；`[NN_DONE]` 哨兵与核心命令复验。
5. 归档 change（`openspec archive recast-v2`），规格进入 `openspec/specs/` 作为长期金标准。

回滚策略：任一步不通过 → 不改动主分支工作区（稳定版一直在原位）；已合入的中间提交可 `git revert`。

## Open Questions

1. `output/` 与 `ui/` 的边界细化（颜色体系归属哪个积木）——实施时按"ui 依赖 output、output 零 ui 依赖"的原则落地，不影响规格与任务拆分。
2. 快照基准文件的存放位置（`v2/tests/baseline/` vs 独立脚本产物）——实施时确定，不影响行为定义。
