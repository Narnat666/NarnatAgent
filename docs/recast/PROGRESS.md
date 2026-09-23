# recast-v2 重构进度笔记

> 本文件为重构工程的滚动状态记录（随进展更新）。规格与设计以 `openspec/changes/recast-v2/` 为准。

## 工程目标

以"积木思想"重写 narnat agent：功能与稳定版（v16.2.5）等价（用户无感知），结构按 `design.md` 重新裁剪。开发全程在 `v2/` 隔离目录，稳定版保持可运行，验证通过后一次性切换。

## 关键路径与产物

| 阶段 | 内容 | 状态 |
|---|---|---|
| 勘察 | 8 路只读调研报告（6,570 行）→ `docs/recast/reports/` | ✅ 完成（抽查验收通过） |
| 图纸 | openspec change `recast-v2`：proposal / design / tasks | ✅ 完成 |
| 规格 | 17 个 capability 规格（215 Requirements / 808 Scenarios）→ 已归档入 `openspec/specs/`（基线） | ✅ 完成（validate --specs：17 passed） |
| Stage 0 | v2 骨架 + 快照基准（`v2/tests/baseline/`，11 文件 452 用例）+ 分层检查（`v2/tests/check_layering.py`） | ✅ 完成（幂等复验通过） |
| Stage 1a | contracts 积木（事件/工具/输出/中断协议 + Animator + 共享字面量） | ✅ 完成（独立复验通过） |
| Stage 1b | config / output / interrupt / messages / llm | ✅ 完成（含 T2.2b output 去双实现、T2.5b llm 去双轨两项修正） |
| Stage 2 | compression / stats / tools×6 / ui×3 / mcp | ✅ 完成（含 T3.11 token 估算归一修正） |
| Stage 3 | conversation / sessions / ui 整合 | ✅ 完成（conversation 73 用例、sessions 95 用例全绿） |
| Stage 4 | app 装配与主循环 + 入口 | ✅ 完成（T4.3：线性装配 + 主循环 / headless / 生命周期；42 用例 + 分层 + 版本冒烟通过；4 处后置补线消解证据见 `docs/recast/reports/T4.3_app_report.md`） |
| Stage 5 | 测试全绿 + E2E 冒烟 + 切换 | ✅ 完成（见下节证据） |
| 实施后跟进 | ESC 打断修复（D15）+ 规格同步 + 编译部署 | ✅ 完成（见下节） |
| 归档 | openspec change 归档（规格入 `openspec/specs/`） | ✅ 完成（2026-09-23 归档为 `2026-09-23-recast-v2`） |

## Stage 5 切换证据（父代理独立复验）

| 验证项 | 结果 |
|---|---|
| 全量单元测试（`python -m pytest tests/unit -q`） | 1657 passed, 6 skipped |
| 分层检查（`python tests/check_layering.py`） | OK（无违规） |
| `python main.py -v` | `narnat 16.2.5`（退出码 0） |
| headless 简单对话 | 通过，哨兵 `[NN_DONE] reason=goal_complete rounds=2` |
| headless 工具链路（Glob + Read + Shell） | 通过，三项结果正确 |
| headless 写入链路（Write → Edit → Read） | 通过，文件内容独立核对一致 |
| `-l` 工具调度日志 | 通过（`[执行命令]` / `[声明完成]` 等） |
| 管道场景（`echo /exit \| python main.py`）旧 vs 新 | **逐字节一致**（`fc /b` 无差异） |
| Nuitka 编译（README 正式命令，含 LTO） | 成功：`output/narnat.exe` 39.5MB 单文件 |
| 编译版 e2e（exe 置于含 `.narnat` 目录） | 通过：Shell + Glob + 表格渲染 + 哨兵全正常 |

**切换方式**：旧包移至 `_old_impl_rollback/`（已随 `79661ab [新增] 完成重构` 提交入库），新包与测试上移；根 `main.py` 更新导入（`from narnat_agent.app import App`）。回滚 = `git checkout 7d075a2 -- narnat_agent` 或从 `_old_impl_rollback/` 取回。

**提交状态**：重构已在 `79661ab` 提交；ESC 修复与探索记录随后作为独立提交入库（见 git log）。

**人工冒烟（用户可选复核）**：启动 UI / 流式渲染（表格·代码块·CJK）/ 输入编辑（Alt+Enter 等）/ 命令与 Tab 补全 / Esc 中断 / `/compact` / 会话 `/save` `/cd` / `/exit` 清理——自动化等价验证已覆盖主要路径（见上表）。

## 任务书索引（`docs/recast/tasks/`）

- 调研：`R1`~`R8`（已完成，报告在 `docs/recast/reports/`）
- 规格写作：`S1`~`S10`（已完成）
- 实施：`T0.1`（基准）、`T1.1`（contracts）、`T2.1`~`T2.5`+`T2.2b`/`T2.5b`（基础积木）、`T3.1`~`T3.11`（中层与工具族）、`T4.1`~`T4.3`（上层与装配）、`E1`/`E2`（ESC 探索）、`FIX-ESC-1`/`FIX-ESC-2`（ESC 修复）——全部完成

## 重要决策备忘（详见 `design.md`）

- D2 修订：事件协议用 TypedDict 集中定义（保 dict 契约），非 dataclass。
- D9：新包开发于 `v2/narnat_agent/`，切换 = 删除旧包 + 上移新包 + 根 main.py 改导入（单一提交，可回滚）。
- D11 明示修复项（有意差异，除此外零差异）：① 串口会话数配置接线；② 死代码清理；③ 会话保存失败原样报错不切状态；④ 会话删除失败静默忽略。
- D14：配置归一化唯一归属 config；`output.apply_style` 纯消费标准配置。
- 兼容怪癖（各 spec 的「兼容性怪癖保持」章节）必须保持，不得"顺手修正"。

## 已知修正记录（spec 与旧实现偏差，均已修正）

1. 中英键并存裁决（config/output spec）：实际规则为——顶层开关英文优先；分组名迁移仅当英文键不存在；组内键与旧扁平键为直赋（覆盖）。已修正实现（`v2/narnat_agent/config/parsers.py`）、两处 spec、测试。
2. output 归一化双实现：可观察差异 5 处（原 spec 要求 4 处 + 本项）；已按 D14 归一到 config（T2.2b）。
3. llm 双轨：SYNTHETIC_THINKING 与 thinking 表副本已上提/归一（T2.5b）。
4. token 估算双实现：唯一实现归 `messages/tokens.py`，两处消费方改转发（T3.11）。
5. 审计经验：spec 中"含否定/例外/顺序语义"的断言是偏差高发区（如"不覆盖""优先于"）；已抽查全 spec 此类断言。

## ESC 打断"不丝滑"问题：探索与修复（已闭环）

用户报告（原文）：打开 narnat，问"你好"，立即交替按 `` ` `` 与 ESC → UI 一直显示"思考中"无法打断；狂按 ESC 无效；过很久才显示"已打断"。

### 探索（E1/E2 + 父代理补证）

- **E1（机制级）** `docs/recast/reports/E1_esc_probe.md`：判定窗口吞键（P1，exp1 C04/C08/C09）；共享客户端污染（P3，openai 路径）；发送期无取消检查点（P4）；静默降级（P5）；无即时反馈（P6）。
- **E2（真机）** `docs/recast/reports/E2_esc_repro.md`：慢路径复现（5~11.5s，概率 ~1/3，新旧实现均命中→历史缺陷）；慢路径与按键序列无关；装置 `docs/recast/esc_probe_live/`（runner.py 可复跑）。
- **父代理补证** `esc_probe/exp4_close_during_connect.py`：**close() 不中断在途 connect**（请求继续到 8s 超时）——补齐机制链最后一环。
- **完整机制链**（见 `docs/recast/esc_review_notes.md`）：按键中断**成功触发** → 若请求在 connect 阶段则 close 无效（请求失控）→ 生成器卡在 send()（无取消检查点）→ 采集线程已退出（后续按键积压成"幽灵输入"）→ 只能等服务端响应头（TTFT 5~11.5s）→ 流循环首个 50ms 轮询才收敛。

### 修复（FIX-ESC-1/2 + spec 同步）

- **FIX-ESC-1（llm 层）**：`llm/cancelable.py` 新原语（阻塞发送入子线程、主流程 0.05s 轮询取消、取消即放弃等待 + 双侧自毁）；anthropic/openai 两后端接入；openai 共享客户端改请求级 scope（abort 不再杀死客户端 → P3 修复）。
- **FIX-ESC-2（按键层）**：Windows 主路径改 ReadConsoleInput 事件源（吞键根除；msvcrt 降级保留）；中断后继续消费按键（幽灵输入根除）。
- **spec 同步**：`specs/interrupt`（键盘监听生命周期/平台按键采集差异/兼容怪癖）与 `specs/llm`（取消与中断）已更新为有意变更，`openspec validate --strict` 通过。

### 验证（父代理亲自执行）

| 验证项 | 修复前（E2/对照） | 修复后 |
|---|---|---|
| 全量单测 + 分层检查 | — | 1674 passed / OK |
| `esc_then_backtick`（Esc 后 10ms 错键） | 旧实现：**完全无中断**（吞键） | **0.16/0.16/0.17s** |
| `dense_alternate`（15ms 密集交错） | 0.35~0.39s（靠第二 Esc） | **0.15/0.17/0.18s** |
| `baseline`×6 | 0.38~7.05~11.41s | **0.18~0.20s ×6** |
| `alternate`×3 | 0.06~11.26s | **0.07/0.09/0.11s** |
| `rage`×3 | 0.05~10.53s | **0.05/0.05/0.07s** |
| 幽灵输入（终屏残留） | 回合结束后 `` ` `` 落入输入框 | 终屏干净（`已打断/继续.../#`） |
| 回归冒烟（对话/工具/哨兵） | — | 通过 |

结论：**慢路径（5~11.5s）彻底消失——18/18 次打断延迟 ≤0.2s；吞键与幽灵输入根除；P3 客户端污染修复。**

### 编译与部署

- 新版本已编译：`output/narnat.exe`（39.5MB，2026-09-23 17:55，README 正式命令 + LTO）；编译版 e2e 验证通过。
- **用户实际使用的 `D:\AgentByNarnat\nn.exe` 需更新**（替换时被占用——当前会话进程占用中）：
  - 已备份旧版到 `D:\AgentByNarnat\nn.exe.bak.20260923`
  - 已提供更新脚本 `D:\AgentByNarnat\update_nn.bat`（纯英文，GBK 兼容）：**关闭所有 nn/narnat 窗口后双击运行**即完成替换（已实测：占用时正确报错并保护原文件）
  - 替换后回滚方式：`copy /y nn.exe.bak.20260923 nn.exe`
- git 状态备忘：重构已提交（`79661ab`）；ESC 探针/修复/spec/测试/文档随后作为独立提交入库。

## 运行与验证入口

```cmd
:: 分层检查
cd /d D:\desktop\NarnatAgent && python tests/check_layering.py
:: 单元测试
cd /d D:\desktop\NarnatAgent && python -m pytest tests/unit -q
:: 入口版本（不触发网络；应输出 narnat 16.2.5 且退出码 0）
cd /d D:\desktop\NarnatAgent && python main.py -v
:: 基准数据（旧实现固化的行为参考，勿重跑覆盖）：见 tests/baseline/README.md
:: openspec：基线规格清单 / 校验（change 已归档；新变更另行新建 change）
cd /d D:\desktop\NarnatAgent && openspec list --specs
cd /d D:\desktop\NarnatAgent && openspec validate --specs
```

