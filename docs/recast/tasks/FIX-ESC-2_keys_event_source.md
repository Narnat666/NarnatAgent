# FIX-ESC-2 实施任务：Windows 主路径改事件源 + 中断后继续消费按键

## 背景（先读，证据可复跑）

用户报告"ESC 打断不丝滑"。探索闭环结论（全文见 `docs/recast/esc_review_notes.md`），本任务对治其中**按键层**两条缺陷：

- **缺陷 C1（吞键，边缘但真实）**：Windows msvcrt 字节流源下，`scan_escape`（`interrupt/keys.py:203-221`）用"20ms 窗口 + 最多吞 5 字节"猜测"Esc 键"还是"转义序列前缀"——**Esc 之后 20ms 内出现非 Esc 字节、且其后 5 字节内无 Esc → Esc 连同等字节被吞、判"非中断"**。证据：`docs/recast/esc_probe/exp1_key_judgement.py` 用例 C04/C08/C09（可复跑）。
- **缺陷 C2（按键积压/幽灵输入）**：`poll_keys`（`interrupt/keys.py:224-243`）判中断后**退出采集循环**；此后到收敛前（修复前可达 11.5s）运行模式内所有按键无人读取，在控制台缓冲积压，回输入态时被 prompt_toolkit 读走（E2 观察到残留 `` ` `` 落入输入框——"幽灵输入"）。
- 对照结论：Windows 非原生控制台的**事件源**（`_ConsoleInputSource`，现有代码 `keys.py:109-162`，`escape_immediate=True`）只认"键按下且 `wVirtualKeyCode == VK_ESCAPE`"的事件，交错按键不影响判定（exp1 C14：0.4ms 中断）——是本缺陷的根本解法。

**行为金标准**：`openspec/changes/recast-v2/specs/interrupt/spec.md`（「平台按键采集差异」「键盘监听生命周期」，含兼容性怪癖）。本任务是**有意行为变更**，spec 由父代理统一更新。

## 代码现状（已核实）

`narnat_agent/interrupt/keys.py`：
- `_open_windows_source()`（324-337）：`GetConsoleMode` 成功 → `_MsvcrtSource`（**字节流**，主路径）；失败 → `_ConsoleInputSource`（**事件源**）——**与理想相反**（事件源才是精确的，只因其被当作"非原生控制台"兜底）
- `_MsvcrtSource`（78-106）：`kbhit/getch` 读字节；注释说明"不清缓冲"为兼容怪癖
- `_ConsoleInputSource`（109-165）：`WaitForSingleObject` + `ReadConsoleInputW` 事件解析；构造时不验证句柄可用性
- `scan_escape`（203-221）：20ms 窗口 + 消费上限判定（Windows 与 POSIX 共用）
- `poll_keys`（224-243）：判中断 → `on_escape()` → **return**（退出循环）
- `KeyListener`（246-321）：start/stop/源构建；静默降级

## 交付物

1. `narnat_agent/interrupt/keys.py`（修改）
2. `tests/unit/test_interrupt.py`（新增/更新用例；保持全绿）

## 实施要求

### R3：Windows 主路径改事件源（吞键根治）

- `_open_windows_source()` 改为：**优先事件源，自检失败才降级 msvcrt**：
  ```python
  def _open_windows_source() -> KeySource:
      import ctypes
      import msvcrt
      kernel32 = ctypes.windll.kernel32
      try:
          source = _ConsoleInputSource(kernel32, ctypes)
          source.self_check()      # 新增：验证句柄可等待（见下）
          return source
      except Exception:
          return _MsvcrtSource(msvcrt)
  ```
- `_ConsoleInputSource.self_check()`（新增方法）：`kernel32.WaitForSingleObject(self._handle, 0)` 返回值 **!= WAIT_FAILED(0xFFFFFFFF)** 即通过；否则抛异常（走降级）。目的：句柄无效（NULL/INVALID_HANDLE_VALUE，如管道输入）时快速回退，不在 read 时才失败。
- `_MsvcrtSource` 与 `scan_escape` 的窗口判定**全部保留**（降级路径仍需要；POSIX 路径也共用 `scan_escape`，其正确性依赖现状——**不得改动判定逻辑**）。
- 更新模块 docstring：说明"Windows 主路径=事件源（精确按键语义）；msvcrt 字节流为降级路径（保留兼容怪癖）"；标注"待 spec 同步"。

### R4：中断后继续消费按键（消灭积压/幽灵输入）

- `poll_keys` 改为：判中断触发 `on_escape()` 一次后**不退出**，继续读取并丢弃按键（不再重复触发），直到 `stop`：
  ```python
  def poll_keys(source, stop, on_escape):
      fired = False
      while not stop.is_set():
          try:
              key = source.read(POLL_INTERVAL_SECONDS)
              interrupted = (not fired) and key == ESC_BYTE and scan_escape(source)
          except (OSError, ValueError):
              return
          if not interrupted:
              continue
          fired = True
          try:
              on_escape()
          except Exception:
              pass
          # 继续消费按键（丢弃、不重复触发）直到停止：消灭积压与幽灵输入
  ```
- 更新函数 docstring（原"触发一次即结束采集线程"改为"触发一次后继续消费按键直到停止"）。
- **注意既有单测**：如有用例锁定"中断后线程退出"的旧语义，更新为新语义（断言：中断触发一次 + 后续按键被消费 + stop 后退出），并在 docstring 注明"有意变更"。
- `KeyListener.start/stop` 生命周期不变（模式切换仍由 bus 驱动）。

### 通用约束

- 不改 POSIX 路径行为；不改 `scan_escape` 判定逻辑；不改 `InterruptBus`（`bus.py` 不在本任务范围）；
- 无模块级可变状态（现状保持）；中文 docstring；不改其他文件、不改 spec。

## 测试要求（新增/更新，全部离线）

1. **事件源优先选择**：mock `ctypes.windll.kernel32`（GetStdHandle/WaitForSingleObject）——self_check 通过 → 返回 `_ConsoleInputSource`；self_check 失败（WAIT_FAILED）→ 返回 `_MsvcrtSource`。
2. **self_check 边界**：WAIT_TIMEOUT(0x102)/WAIT_OBJECT_0(0) 视为可用；WAIT_FAILED 抛异常。
3. **poll_keys 新语义**（fake 源）：
   - 投喂 [ESC] → `on_escape` 触发 1 次；
   - 继续投喂 [`` ` ``、ESC、ESC] → **不重复触发**（计数仍为 1）；
   - 断言源在中断后**仍被继续读取**（fake 记录 read 次数）；
   - `stop.set()` → 循环退出。
4. **对照守护**：既有判定表相关用例（C01-C15 的函数级对应）全绿——证明 `scan_escape` 与 msvcrt 降级路径未变。
5. 全量 `tests/unit/test_interrupt.py` 绿；无其它测试文件回归失败（如 `test_ui_interaction.py` 因共享替身受影响，允许更新等价断言并注明）。

## 边界条款

- 只改 `narnat_agent/interrupt/keys.py` 与 `tests/unit/test_interrupt.py`（必要时最小更新 `tests/unit/test_ui_interaction.py` 的等价断言，需在报告注明理由）；不改其他文件、不改 spec
- 不运行 narnat；不执行 git 写操作
- 真机验证（E2 装置复跑）由父代理执行，不在本任务

## 验收标准（可计算）

1. `cd /d D:\desktop\NarnatAgent && python -m pytest tests/unit/test_interrupt.py -q` 全绿
2. `python -m pytest tests/unit -q` 全绿（无回归）
3. `python tests/check_layering.py` 无违规
4. 报告列出：新增用例名 + 断言摘要；旧语义更新点清单（如有）
5. `git status --porcelain` 仅含本任务范围内的文件改动

## 失败报告格式

①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
