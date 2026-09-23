# E2 实验装置：ESC 打断"不丝滑"真机复现（黑盒端到端）

配套报告：`docs/recast/reports/E2_esc_repro.md`。

## 文件构成

| 文件 | 作用 |
|---|---|
| `winctl.py` | Win32 原语：`AttachConsole` + `WriteConsoleInputW`（注入按键事件）+ `ReadConsoleOutputCharacterW`（读屏幕） |
| `driver.py` | 驱动器：等待 UI 就绪 → 清输入缓冲 → 注入"你好"+回车 → 按序列注入按键 → 20ms 采样屏幕 → 写 `result.json`/`frames.jsonl`（含每个按键与"思考中/已打断/最大输出"时刻） |
| `runner.py` | 运行器：启动目标进程（新/旧实现；`--console wt/conhost`）→ 取 pid（进程差集+命令行匹配）→ 调 driver → `taskkill` 清理 → 归档 narnat 日志 |
| `summarize.py` | 汇总 `runs/` → `summary.md`（明细表 + 版本×序列矩阵 + 时间线） |
| `evidence.py` | `python evidence.py <run_id片段>`：打印单条 run 的事件/标记/帧/日志 |
| `timeline.py` | `python timeline.py <run_id...>`：导出帧时间线到 `timeline_raw.txt` |
| `runs/` | 全部原始数据（`result.json`、`frames.jsonl`、`narnat_*.log`、`runner.log`、`meta.json`） |

## 复跑

前置：`git archive` 可取 `7d075a2`（旧实现自动导出到 `%TEMP%\narnat_old`）；`main.py` 所在的 `.narnat/config/narnat.json` 存在（实验用其副本）。

```cmd
cd /d D:\desktop\NarnatAgent\docs\recast\esc_probe_live

:: 全矩阵（新/旧 × 5 序列 × 3 次，串行；约 15 分钟）
python runner.py --matrix --console wt

:: 单条
python runner.py --version new --plan alternate --attempt 1 --console wt
python runner.py --version old --plan rage --attempt 1 --console conhost

:: 汇总与证据
python summarize.py
python evidence.py new_wt_alternate_r1
python timeline.py new_wt_alternate_r1_20260923_171156
```

## 按键计划（driver.py: PLANS，相对"回车注入完成"时刻 t0）

| 名称 | 计划 |
|---|---|
| `baseline` | +0.12s ESC |
| `wrong_then_esc` | +0.12s `` ` ``，+0.26s ESC |
| `alternate` | +0.12s 起 `` ` ``/ESC 交替各 5 轮（间隔 140ms） |
| `rage` | +1.00s 起 ESC ×10（间隔 100ms） |
| `delayed` | +2.00s ESC |
| `alternate_then_rage`（探索） | `alternate` 后 +2.00s 起 ESC ×10 |
| `none`（对照） | 不注入按键 |

## 隔离与安全

- 目标进程用 `NARNAT_HOME=%TEMP%\narnat_esc_home_new` / `_old`（`.narnat` 配置副本），**不写主线工作树 `.narnat`**；
- 不修改任何主线源码/测试；旧实现仅用 `git archive`（只读）导出；
- 每 run 新进程、串行执行；结束 `taskkill /F /T` 清理目标进程树并回收实验新建的 conhost；
- 注入走 `WriteConsoleInputW`，**不抢窗口焦点**；不影响其它终端会话。
