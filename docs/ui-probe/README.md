# AI 操控 narnat 界面：真机验证装置与调试思维

> 一套"黑盒真机测试夹具"：让 AI（或人）像真实用户一样操控运行中的 narnat——
> 注入按键、读屏幕文本、量时序——把"只能手动复现"的 UI 问题变成可重复、可量化的实验。
>
> **给未来 AI 会话的话**：当你遇到无法用单测/读代码解决的 UI 行为问题（用户说"卡住了"
> "按键没反应""显示不对"），或需要验证某个修复对真实交互的影响时，读本文档，用这套装置做实验。

---

## 1. 何时使用（触发条件）

- 用户报告 UI/交互行为问题：按键无响应、打断慢、卡"思考中"、显示错乱、输入错位等；
- 需要验证修复前后交互行为是否等价（回归验证），且单测覆盖不到；
- 排查"时序类"问题：某个操作在某状态下发生/不发生（这类问题代码审查几乎无法证伪）。

**不适用**：纯逻辑 bug（用单测）、headless 模式（用 `main.py -p` 直接测）。

## 2. 原理：三件套 + 编排

```
[注入] WriteConsoleInputW ──→ 目标控制台输入队列 ──→ 目标程序读键（等价真人按键）
[观察] ReadConsoleOutputCharacterW ←── 目标控制台屏幕缓冲区（读回渲染后的文本）
[附着] AttachConsole(pid)  ── 进入目标控制台命名空间（前提）
[编排] subprocess 启动目标(-d 记日志) + NARNAT_HOME 隔离 + taskkill 清理
```

关键点：

- **免焦点注入**：直接写目标控制台输入队列，目标窗口无需在前台（相比
  SendInput/keybd_event 的核心优势——不影响用户当前操作，可后台批量跑）；
- **黑盒**：不修改被测程序、不加日志埋点，任何有控制台 UI 的程序都适用（narnat、nn 等）；
- **注入即"按键事件"**：keydown + keyup 两条 KEY_EVENT 记录，程序侧与真实键盘无差别。

## 3. 文件与用法

```
docs/ui-probe/
├── winctl.py      # 底座：AttachConsole + 注入 + 读屏（ConsoleIO 类）
├── ui_driver.py   # 场景驱动：跑一条实验场景，产出 result.json
├── ui_run.py      # 编排器：启动目标 → 调驱动 → 清理（日常用这个）
└── README.md      # 本文档
```

### 3.1 跑内置场景

```cmd
cd /d <仓库根>\docs\ui-probe

:: 场景 1：发消息 → Esc 打断 → 打断后输入 → 提交 → 下一轮跑完（全链路）
python ui_run.py --scenario after_esc_input

:: 场景 2：正常路径对照（发消息不按键，验证完整完成）
python ui_run.py --scenario no_key

:: 原生控制台（非 ConPTY）再跑一遍
python ui_run.py --scenario after_esc_input --console conhost

:: 对照实验：把旧版 narnat 导出到任意目录，--target 指向它
python ui_run.py --scenario after_esc_input --target "%TEMP%\narnat_old"
```

输出在 `%TEMP%\narnat_ui_probe_runs\<run_id>\result.json`（可用 `--runs-dir` 覆盖）。
隔离配置在 `%TEMP%\narnat_ui_probe_home`（首次自动从目标目录 `.narnat` 复制，排除 logs）。

### 3.2 一次性实验（最小示例）

不写驱动文件，用 Python 直接操控（附到已运行的目标 pid）：

```python
import sys, time
sys.path.insert(0, r"<仓库根>\docs\ui-probe")
import winctl

io = winctl.ConsoleIO(12345)        # 目标进程 pid
io.type_text("你好"); io.press("enter")   # 像用户一样输入并发消息
time.sleep(2)
print(io.screen_text())             # 读当前屏幕文本
io.press("esc")                     # 按 Esc
io.close()
```

### 3.3 winctl API 速查

| 方法 | 说明 |
|---|---|
| `ConsoleIO(pid, attach_timeout=10)` | 附着目标控制台（失败自动重试到超时）|
| `press(key)` | 按键：`esc` / `backtick` / `enter` / `ctrl-c` / 任意单字符 |
| `type_text(text)` | 逐字符注入文本 |
| `screen_text()` | 读可见窗口全部文本（行去尾空格）|
| `cursor()` | 光标 (x, y) |
| `close()` | 释放句柄 |

### 3.4 result.json 字段

| 字段 | 说明 |
|---|---|
| `status` | `ok` / `not_ready`（目标没起来）/ `error` |
| `checks` | 各布尔判定（如 `interrupted`、`typing_exact`、`round2_completed`）|
| `marks` | 各事件相对注入时刻的耗时（秒），如 `已打断`=0.043 |
| `t_esc_epoch` | Esc 注入瞬间的墙钟时间（**与 narnat 日志对齐用**，见 4.4）|
| `screens` | 各阶段屏幕快照（事后复查断言是否合理）|

## 4. 调试思维（核心方法论）

> 这一节比脚本更重要。脚本只是手，判据与对照才是脑。

### 4.1 能真机就别猜

UI 行为问题（按键、时序、渲染）**只有真机能测**。读代码得出的"应该没问题"不算数；
一个修复是否有效，用真机上跑出的数字说话。先设计一个"能复现问题的最小按键序列"，
再谈修复。

### 4.2 对照组原则：失败先问"旧版是否同样失败"

验证修复时，**修复前版本（baseline）必须用同一装置跑一遍**。判定流程：

```
修复后出现"失败样本" → 用旧版跑同样场景 →
  旧版也失败（或更糟）  → 不是回归，检查实验设计（见 4.3/4.6）
  旧版成功、新版失败    → 才是回归，去查代码
```

本次实战（ESC 打断修复）中，新版出现 3 条"未打断"样本，一度疑似回归；用**未修复的旧版**
跑同样场景，旧版同样失败 2/4——且日志证明失败样本的 Esc 落在"AI 已答完"之后（见 4.3）。
对照实验直接把"疑似回归"证伪。

### 4.3 时序判据必须钉死"当时状态"（最大的坑）

凡是"某操作在某状态下发生/不发生"的实验，**必须记录操作瞬间的系统状态**，否则全是假数据。
典型陷阱（踩过）：

- 判据 `"思考中" in 屏幕` **不可靠**——完成后该词会残留在屏幕上；
- 可靠判据：**完成标志**（narnat 本轮统计行含 `费用:`）或提示符回归；
- 实验要同时记录"操作注入瞬间的状态"，例如本装置记录 `completed_at_esc`
  （Esc 注入瞬间本轮是否已完成）。

教训案例：驱动在"思考中"出现后固定延迟 0.5~1.0s 注入 Esc，而短对话 1 秒左右就答完——
Esc 落在 AI 完成之后，"没打断"是**无目标可打断**（正常行为），不是缺陷。上了
`completed_at_esc` 判据后，全部样本 100% 收敛：**运行中 8/8 成功，已完成 3/3 无目标**。

### 4.4 三源交叉验证

单一证据会骗人，至少交叉两源：

1. **屏幕文本**（用户视角的真实观感）；
2. **result.json**（结构化时刻/判定，可批量统计）；
3. **narnat 进程日志**（`<home>/.narnat/logs/*.log`，精确到秒级事件，如
   "响应流中断"、"第 N 轮"）。

对齐手段：result.json 的 `t_esc_epoch` 与日志行时间戳比对。
典型案例：某条 run 屏幕显示"未打断"，日志显示该轮请求 11:50:24 `响应完成`、
而 Esc 注入时刻也是 11:50:24——**同一秒**，确证"Esc 到达时已无目标"。

### 4.5 隔离与自清理（最低事故原则）

- 目标进程一律用 `NARNAT_HOME` 指向隔离目录（首次从工作树 `.narnat` 复制配置），
  **不写工作树 .narnat**；
- 结果一律写 `%TEMP%`，**不污染工作区**（`git status` 始终干净）；
- 跑完 kill 目标进程树 + 测试期间新建的 conhost；
- 注入是"发给目标控制台的真实按键"——**别对着用户正在使用的窗口做实验**，
  实验期间也不要手动开其它 narnat 实例（目标 pid 靠进程差集识别，会认错）。

### 4.6 假失败 vs 真回归

出现"失败"时按顺序排除：① 操作时刻状态（4.3）→ ② 装置因素（注入成功性、
目标就绪、时序窗口）→ ③ 环境（wt/conhost、编码）→ ④ 对照组（4.2）→ ⑤ 才怀疑代码。
**先证伪实验，再怀疑实现。**

### 4.7 量化为王

判据用数字写死，别用"变快了"：延迟 = `marks["已打断"]` − 注入时刻，判定如
`≤0.6s`；样本数写死（如 3 次，3/3 达标才算过）。修复的效果 = 修复前对照数字 vs
修复后数字。

## 5. 扩展新场景

在 `ui_driver.py` 的 `main()` 里加 `scenario` 分支即可。常用断言套路：

```python
# 等提示符（输入态）
at_prompt(screen)                    # 底部 3 行内出现 "#" 起始行
# 等完成标志（本轮结束）
"费用:" in screen
# 等状态词（注意 4.3 的残留陷阱）
"已打断" in screen / "思考中" in screen
# 取输入行内容（校验回显精确性）
input_line_of(screen)
```

更多按键序列（9 种计划：baseline / wrong_then_esc / alternate / rage / delayed /
esc_then_backtick / dense_alternate 等）与"旧 vs 新"自动对照，见 **narnat_v2 分支**上的
完整 E2 装置 `docs/recast/esc_probe_live/`（含 runner/driver/证据提取脚本与历史证据）：

```cmd
git archive --format=tar narnat_v2 docs/recast/esc_probe_live -o "%TEMP%\rig.tar"
cd /d "%TEMP%" && mkdir rig && tar -xf rig.tar -C rig
cd rig\docs\recast\esc_probe_live
python runner.py --version new --plan esc_then_backtick --attempt 1 --console wt
```

## 6. 已知坑

| 坑 | 说明 |
|---|---|
| AttachConsole 后 stdout 失效 | 调用进程标准句柄被接管；驱动结果一律写文件，不 print |
| wt（ConPTY）与 conhost 行为可不同 | 两个都测（`--console wt` / `conhost`）|
| 注入成功不等于被处理 | `_write` 校验写出条数；但"被程序消费"要看屏幕变化 |
| ctrl-c 清输入缓冲 | 会重建输入会话；实验开始用它清残留，之后别误用 |
| "思考中"残留 | 完成后仍可能留在屏幕上，不能当运行中判据（用 `费用:`）|
| 目标必须是真实交互进程 | 用 `python main.py -d` 启动（-d 同时产出详细日志）|
| 进程识别靠差集 | 实验期间别同时手动起其它 `main.py` 实例 |
| 结构体对齐断言 | winctl.py 里有 `assert sizeof(INPUT_RECORD)==20`，改结构要过断言 |

## 7. 实战案例（ESC 打断延迟修复验证）

**问题**：AI 运行中按 Esc 打断慢（5~11.5s）或完全无效。

**修复**：请求发送可取消（阻塞调用移入子线程 + 0.05s 轮询取消标记）+ Windows 按键
采集改事件源（根除"Esc 被当成转义序列前缀吞掉"）。

**验证（本装置全程）**：

| 验证项 | 手段 | 结果 |
|---|---|---|
| 打断即时性（密集按键矩阵） | runner 12 条 run | 修复后 0.00~0.21s（修复前：特定序列完全无打断）|
| 独立复验 | 6 条 run | 全部 0.02~0.20s |
| 打断后 UI 可用性 | `after_esc_input` 场景 | 8/8 运行中打断成功（0.04~0.13s），回显精确，下一轮完整跑完 |
| 疑点排查 | 旧版对照 + 日志时间戳 | "未打断"样本 = Esc 落在已完成之后（非回归，证伪）|
| 正常路径 | `no_key` 场景 + conhost | 完整完成，无双终端差异 |

## 8. 边界

- 本装置是**测试夹具**，只注入按键与读屏，不修改 narnat 任何代码；
- 注入的按键对目标程序是真实事件——别对用户正在操作的实例做实验；
- Windows 专用（Win32 控制台 API）；Linux/macOS 可用 pty 方案另实现。
