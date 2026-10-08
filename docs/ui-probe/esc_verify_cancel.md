# ESC 中断"验证期间长耗时只读工具执行"修复与验证记录（2026-10-08）

> 对应任务书：`D:\tmp\vfprobe\task_verify_esc_cancel.md`
> 证据目录：`D:\tmp\vfprobe\`（probe 脚本 + result*.json，可复跑，勿删）

## 1. 现象与根因

- 现象：goal 模式"⚙ 正在验证完成声明（独立复核）…"期间按 ESC 无反应（实测 >3 分钟）。
- 根因：验证器执行 Grep/Glob（纯 Python 遍历）时主线程同步阻塞在 `registry.execute`
  内，遍历/扫描循环**无任何取消检查点**；ESC 已被轮询线程置位，但工具无法中断。
- 附带两个 ESC 检测缺陷（`ui/interrupt.py`）：
  1. `WaitForSingleObject(...) != 0xFFFFFFFF` 判定句柄可用性——ctypes 默认 restype
     为 c_int（有符号），WAIT_FAILED 读出为 -1，比较恒真 → 误走 `ReadConsoleInput`
     分支，其中 `WaitForSingleObject(handle,50)` 持续返回 -1 被 `!= 0` 吞掉 →
     空转死循环（实测 2s 内 27,368,792 次调用），ESC 永久失效（应降级 msvcrt）。
  2. 该分支 `ReadConsoleInputW` 失败即 `break`——轮询线程永久退出、无降级。

## 2. 改动（白名单内）

| 文件 | 改动 |
|---|---|
| `tools/tool_context.py` | 新增字段 `cancel_check: Optional[Callable[[], bool]] = None`（默认 None=行为不变） |
| `tools/glob/__init__.py` | 新增 `_CancelGate`（首次立即查、其后每 32 次调用真查一次）与 `_CANCELLED_TEXT`；`_collect` 目录项/文件循环检查取消，`execute` 遍历前检查、取消即返回标记 |
| `tools/grep/__init__.py` | 复用 `_CancelGate`；os.walk 目录/文件循环、并行与串行扫描循环、`_scan_file` 每 64KB chunk 各设检查点；取消即返回 `[已取消: 用户中断，扫描提前结束]` |
| `core/goal_verifier.py` | `verify()` 把 `cancel_check` 注入验证器 ToolContext（finally 清理）；工具返回后命中取消 → `verdict="interrupted"`；`_execute_tools` 每次调用前检查；`_run_round` 在 `finish is None` 且取消置位时归入 `cancelled`（不再误报"响应流中断"） |
| `ui/interrupt.py` | 自检改按 c_int 语义比较 `-1`；`_poll_esc_windows_coninput` 返回 bool，句柄失效/读取失败时**降级 msvcrt 继续轮询**（不再退出线程、不再空转） |
| `assembly.py` | 主会话 ToolContext 注入 `cancel_check=lambda: _interrupt_ctrl.is_set` |

## 3. 验收判据与结果（证据在 `D:\tmp\vfprobe\`）

| 验收项 | 命令 | 结果 |
|---|---|---|
| 1 工具层取消 | `python accept1_tool_cancel.py` | grep 0.002s / glob 0.000s 返回，含"取消"标记 |
| 1b 扫描中途取消 | `python accept1b_tool_cancel_timed.py` | grep 1.006s 返回（取消→返回 0.006s）；glob 0.3s |
| 2 验证器路径（真控制台注入 ESC） | `python run_probe2.py` | `verdict=interrupted`，注入→返回 **0.009s**，走事件源分支 |
| 3 真机 goal 全链路 ×3 | `python accept3_live.py` | 3/3 达标，注入→"已打断" 0.003 / 0.002 / 0.002s |
| 补充：主会话 Grep 扫描期间 ESC | `python run_live2.py` | 屏幕"已打断" 0.091s；日志中 grep 结果为 `[已取消: 用户中断，扫描提前结束]` |
| 4 无取消等价 | `python baseline_collect.py baseline\|after`、`python accept4_equiv.py` | grep 逐字节一致；旧/新实现同刻 14 组调用全一致（glob 的 baseline/after 差异仅因本修复改动文件 mtime 变新→top-10 排序变化，总匹配数 61 不变） |
| 5 静态检查 | `python -m compileall -q narnat_agent`；`python -c "import narnat_agent"` | 退出码 0；无异常 |
| 检测缺陷对照 | `python accept5_interrupt_defects.py` | 旧：误走 coninput 空转 / 读失败退出线程；新：均降级 msvcrt |

## 4. 注意

- Grep/Glob 在**无取消**场景的输出格式、截断提示、跳过汇总语义均未改动。
- `Read` 未改：其读取循环有界（limit ≤ 2000 行），行数统计上限 20MB（`READ_MAX_COUNT_BYTES`），
  不存在分钟级长循环；主会话中 ESC 仍由 ToolDispatcher 的等待循环即时生效。
- 已知同类模式残留（未改，超出白名单）：`tool_exp/repro_del_confirm/probe_steal.py`
  亦用 `WaitForSingleObject` 而无 c_int 语义比较。
