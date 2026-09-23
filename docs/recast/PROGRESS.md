# recast-v2 重构进度笔记

> 本文件为重构工程的滚动状态记录（随进展更新）。规格与设计以 `openspec/changes/recast-v2/` 为准。

## 工程目标

以"积木思想"重写 narnat agent：功能与稳定版（v16.2.5）等价（用户无感知），结构按 `design.md` 重新裁剪。开发全程在 `v2/` 隔离目录，稳定版保持可运行，验证通过后一次性切换。

## 关键路径与产物

| 阶段 | 内容 | 状态 |
|---|---|---|
| 勘察 | 8 路只读调研报告（6,570 行）→ `docs/recast/reports/` | ✅ 完成（抽查验收通过） |
| 图纸 | openspec change `recast-v2`：proposal / design / tasks | ✅ 完成 |
| 规格 | 17 个 capability 规格（215 Requirements / 808 Scenarios）→ `openspec/changes/recast-v2/specs/` | ✅ 完成（validate --strict 通过） |
| Stage 0 | v2 骨架 + 快照基准（`v2/tests/baseline/`，11 文件 452 用例）+ 分层检查（`v2/tests/check_layering.py`） | ✅ 完成（幂等复验通过） |
| Stage 1a | contracts 积木（事件/工具/输出/中断协议 + Animator + 共享字面量） | ✅ 完成（独立复验通过） |
| Stage 1b | config / output / interrupt / messages / llm | ✅ 完成（含 T2.2b output 去双实现、T2.5b llm 去双轨两项修正） |
| Stage 2 | compression / stats / tools×6 / ui×3 / mcp | ✅ 完成（含 T3.11 token 估算归一修正） |
| Stage 3 | conversation / sessions / ui 整合 | ✅ 完成（conversation 73 用例、sessions 95 用例全绿） |
| Stage 4 | app 装配与主循环 + 入口 | ✅ 完成（T4.3：线性装配 + 主循环 / headless / 生命周期；42 用例 + 分层 + 版本冒烟通过；4 处后置补线消解证据见 `docs/recast/reports/T4.3_app_report.md`） |
| Stage 5 | 测试全绿 + E2E 冒烟 + 切换 | ✅ 完成（见下节证据；人工终端交互冒烟待用户执行） |
| 归档 | openspec change 归档（规格入 `openspec/specs/`） | ⏳ 待用户完成人工冒烟后执行 |

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

**切换方式**：旧包移至 `_old_impl_rollback/`（未跟踪），新包与测试上移；根 `main.py` 更新导入（`from narnat_agent.app import App`）。回滚 = 删除新包 + `_old_impl_rollback` 改回 `narnat_agent`（或 `git checkout 7d075a2 -- narnat_agent`）。

**未提交**：工作树变更未做任何 git 提交（保留用户审阅）；提交时建议 `git add -A && git commit`。

**待人工冒烟**（无法自动化的真实终端路径）：启动 UI / 流式渲染（表格·代码块·CJK）/ 输入编辑（Alt+Enter 等）/ 命令与 Tab 补全 / Esc 中断 / `/compact` / 会话 `/save` `/cd` / `/exit` 清理。

## 任务书索引（`docs/recast/tasks/`）

- 调研：`R1`~`R8`（已完成，报告在 `docs/recast/reports/`）
- 规格写作：`S1`~`S10`（已完成）
- 实施：`T0.1`（基准）、`T1.1`（contracts）、`T2.1`~`T2.5`+`T2.2b`/`T2.5b`（基础积木）、`T3.1`~`T3.11`（中层与工具族）、`T4.1`~`T4.3`（上层与装配，全部完成）；`T5.x`（切换与归档）待编写

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

## 运行与验证入口

```cmd
:: 分层检查
cd /d D:\desktop\NarnatAgent\v2 && python tests/check_layering.py
:: 单元测试
cd /d D:\desktop\NarnatAgent\v2 && python -m pytest tests/unit -q
:: 入口版本（不触发网络；应输出 narnat 16.2.5 且退出码 0）
cd /d D:\desktop\NarnatAgent\v2 && python main.py -v
:: 真实控制台交互装配冒烟（窗口一闪即关）
cd /d D:\desktop\NarnatAgent\v2 && python tests/_tmp_t43_console_smoke.py
:: 基准复现（旧实现）
cd /d D:\desktop\NarnatAgent && python v2\tests\baseline\extract_old.py
:: openspec 校验
cd /d D:\desktop\NarnatAgent && openspec validate recast-v2 --strict
```

## 待切换清单（Stage 5）

- 切换前：`v2/` 内 headless 真实 LLM 冒烟（`-p` 任务、`[NN_DONE]` 哨兵）与人工 UI 冒烟清单
  （见 `docs/recast/reports/T4.3_app_report.md` §7）。
- 切换：删旧包 + 上移新包与测试 + 根 `main.py` 改导入（一行）→ 复跑上方全部验证入口。

