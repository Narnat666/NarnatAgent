# tests/ —— 测试与验证入口

行为规格位于 `openspec/specs/`（215 条需求，重构完成后的长期金标准）；本目录的测试与基准用于验证实现与规格、与旧实现行为的逐条对照。

## 目录

| 路径 | 作用 |
|---|---|
| `unit/` | 单元测试（pytest，1600+ 用例）：逐积木行为、修复回归、spec Scenario 映射 |
| `check_layering.py` | 结构护栏：import 分层（app→业务→基础→contracts）、跨模块私有访问、模块级可变状态 |
| `baseline/` | 快照基准：旧实现（v16.2.5）固化的行为输出 + 提取脚本；单测据此做等价对照 |

## 运行

```cmd
cd /d D:\desktop\NarnatAgent
python -m pytest tests/unit -q        :: 全量单元测试
python tests/check_layering.py        :: 结构护栏
```

## 基准（baseline）说明

- `baseline/data*/`、`baseline/stream/`、`baseline/ui/` 是**旧实现的固化输出**，作为"行为等价"的判据；请勿改动。
- `extract_old*.py` 系列脚本面向**旧实现**（git `7d075a2`）；当前工作树已是新实现，**不要重跑这些脚本覆盖基准**（会以新实现输出污染旧基准）。仅在回滚到旧实现做再基准时使用，详见 `baseline/README.md`。
- 扩展场景：ESC 打断的机制探针在 `docs/recast/esc_probe/`，真机端到端装置在 `docs/recast/esc_probe_live/`（均可复跑）。

## 相关文档

- 重构进度与验证记录：`docs/recast/PROGRESS.md`
- 现状调研报告（R1–R8）：`docs/recast/reports/`
- openspec 工作流：`openspec/specs/`（基线规格）、`openspec/changes/archive/`（历次变更）
