# E1 报告：ESC 打断"不丝滑"根因分析（机制级）

- 任务：`docs/recast/tasks/E1_esc_probe_mechanism.md`
- 实验代码：`docs/recast/esc_probe/`（可复跑；只读导入主线，不修改任何主线文件；不访问任何 LLM API）
- 数据产物：`docs/recast/esc_probe/out/exp1_results.json`、`exp2_results.json`、`exp3_results.json`
- 边界遵守：未做真机按键注入（E2 职责）；未执行任何 git 写操作

---

## 0. 结论摘要（TL;DR）

| # | 根因主张 | 对治层次 | 证据强度 |
| --- | --- | --- | --- |
| P1 | `scan_escape` 用"20ms 窗口 + 最多吞 5 字节"猜测"Esc 键"还是"转义序列前缀"。**Esc 之后 20ms 内出现非 Esc 字节、且其后 5 字节内无 Esc → Esc 被整段吞掉，判定"非中断"**（按键完全无效） | 按键采集层 | **已实验证实**（exp1 C04/C08/C09） |
| P2 | "先按错键再按 Esc"**不是**失效原因；敏感条件是 Esc **之后** 20ms 内是否有别的键。交替连按时第 2 次 Esc 才生效（置位延后 50~72ms），用户体感"要多按几次/时好时坏" | 按键采集层 | **已实验证实**（exp1 C02/C03/C11/C12/C13 vs C04） |
| P3 | 响应头到达前的中断会把**共享 OpenAI 客户端整个 close 掉**：请求被中断（收敛快），但该客户端**不可复用** → **打断一次后，后续所有轮次请求全部失败** | LLM 连接层 | **已实验证实**（exp2 A/A2/B + exp3 D） |
| P4 | `create()/send()` 阻塞期**没有取消检查点**，收敛完全依赖"close 能中断在途请求"这一底层库实现细节；该依赖若失效，中断要等响应头到达才显示（反事实实测 4.07s） | LLM 收敛层 | 实验 + 代码依据（exp3 A/C） |
| P5 | `KeyListener` 源构建失败**静默降级**：无报错、无提示、UI 照常显示"思考中"，Esc 永久无效 | 按键采集层 | 行为已实验证实（exp1 降级验证）；**真机是否命中待 E2** |
| P6 | 中断置位后到"已打断"显示之间**没有任何即时反馈**（动画继续转、无任何输出），用户无法区分"按键被吞"与"中断已生效但收敛中"，于是继续狂按 | 体验层 | 代码依据；真机体感待 E2 |

**与用户现象的对应**：

- "UI 一直显示思考中、无法打断、没有输出" —— P1/P5（按键无效）或 P6（已生效但无反馈）都会产生同样观感；
- "先按错键（`），后续又按 esc 就会出现这种问题" —— **用户的归因被实验反证**（P2）：错键在前不影响判定，命中吞键的时序是"Esc 在前、错键紧随 20ms 内"；
- "等待了一会，才显示打断了 / 狂按 esc 也没有用" —— 离线实测"置位 → 显示"在本机全部 ≤0.3s（exp3 A/B/E），**未能复现秒级延迟**；秒级延迟的可能机制见 §3.3 与 §7（P4 的结构风险、P6 的体感、按键未到达采集器）。

---

## 1. 链路：从"按键进入系统"到"UI 显示已打断"

### 1.1 分环节表

| # | 环节 | 文件:行号 | 状态变化 | 触发条件 |
| --- | --- | --- | --- | --- |
| 1 | 输入读取（输入模式） | `app/interactive.py:126-127` → `ui/stream.py:299-301` → `ui/prompt.py:126-148` | prompt_toolkit 持有控制台，采集器未启动 | 主循环每次读输入；`prompt.py:138` 先 `enter_input_mode()` |
| 2 | 用户回车提交 | `ui/prompt.py:55-57` 键绑定 `enter` → `validate_and_handle()` | prompt 返回输入文本 | 用户按 Enter |
| 3 | 调度入口 | `app/interactive.py:160-164`（`_prepare_input` → `_dispatch_turn`）→ `:57-58` | 追加用户消息，准备轮次 | 输入非空且非命令 |
| 4 | 创建流会话 | `ui/stream.py:291-297`（`begin_turn`） | ① `interrupt.enter_run_mode()`：`bus.py:87-95` → `clear()` 清标志、`keys.py:267-274` 停旧线程并以**独立停止信号**启动新采集线程；② 建 `UiSink`；③ `sink.begin()` → `animator.py:110-115` 启动"思考中"动画（延迟 0.666s，`animator.py:44`） | 每次进入一轮 AI 回合 |
| 5 | 采集线程建源 | `keys.py:284-298`（`_run`）→ `:300-307`/`:324-337` | Windows 原生控制台 → `_MsvcrtSource`（不清缓冲）；否则 → `_ConsoleInputSource`（事件源，`escape_immediate=True`）；**失败即静默 return（P5）** | 线程启动后立即 |
| 6 | 采集循环 | `keys.py:224-243`（`poll_keys`） | 30ms 周期 `read`（`keys.py:38`）；非 Esc 字节丢弃；Esc 字节进入判定 | 运行模式期间 |
| 7 | Esc 判定 | `keys.py:203-221`（`scan_escape`） | `sleep(20ms)` → 探测读（超时 0）→ 无后续 → **中断**；有后续 → 最多再收 4 字节 → 其中含 Esc → **中断**，否则 **非中断（Esc + 已收字节全部丢弃）** | 每读到 `b"\x1b"`（`keys.py:234`） |
| 8 | 中断置位与广播 | `keys.py:239-242` → `bus.py:72-85`（`raise_`） | `_signal.set()` 后**同步**调用全部订阅者（逐个吞异常） | 判定为中断时（采集线程内） |
| 9 | 订阅者动作 | `assembly.py:220-221`（terminal/serial）、`llm/client.py:68-69`（LLM）→ `client.py:132-134` → `llm/runtime.py:93-100`（关闭 `_active_handle`） | 关闭句柄；句柄可能是 **OpenAI 客户端对象**或**响应流**（见 §3.3） | 广播时 |
| 10 | 主流程收敛检查点 | `conversation/loop.py:440-443`（`cancel_check=lambda: sink.cancelled`）、检查点①`:445-446`、②`:507-508`、③`:525-528`、④`:562-563`；生成器内轮询 `llm/openai_backend.py:243-259` / `llm/anthropic_backend.py:295-311`（50ms，`llm/runtime.py:29`） | 命中 → 结束流 → 出口 | 每次事件/每条消息循环 |
| 11 | 出口与显示 | `conversation/loop.py:604-616`（`_close_interrupted`/`_abort_round`）→ `ui/stream.py:228-240`（`abort`） | `enter_input_mode()`（停采集 + 清标志，`bus.py:97-106`）→ 停动画 → 输出"已打断/继续..." | 收敛触发 |
| 12 | 回输入界面 | `loop.py:615` → `ui/stream.py:308-311` | `enter_input_mode()` + **重建输入会话**（丢弃残留编辑缓冲，`ui/prompt.py:150-152`） | 中断收敛后 |

### 1.2 判定规则（照实表述，`keys.py:203-221`）

```
读到 Esc：
  sleep 20ms（keys.py:41）
  b1 ← 探测读（msvcrt 源超时 0：keys.py:87-90）
  若 b1 为 None                     → 中断
  否则消费 b2..b5（各 0 超时立即读，最多再 4 字节）
  若 {b1..b5} 含 Esc                → 中断（连按判定）
  否则                              → 非中断：Esc 与已收字节一并丢弃
```

**会被吞的充分条件**：Esc 之后 20ms 内到达一个非 Esc 字节，且其后"立即读窗口"（约 <1ms，最多再读 4 字节）内没有 Esc 到达。

---

## 2. 受控实验与证据

### 2.1 实验一：按键序列 × 到达时刻 → 判定表（`exp1_key_judgement.py`，15 组）

方法：假字符源按时间表投喂字节，驱动**真实的** `KeyListener`/`poll_keys`/`scan_escape`/`InterruptBus`；
"到达时刻 0" 表示采集启动瞬间该字节已在控制台缓冲（对应"回车后立即按键"的积压形态）。

| 用例 | 序列（相对 t0） | 判定 | 置位时刻 | 被吞字节 |
| --- | --- | --- | --- | --- |
| C01 | 单 Esc | **中断** | 21ms | — |
| C02 | `` ` `` → Esc(+100ms) | **中断** | 142ms | — |
| C03 | `` ` `` → Esc(+15ms) | **中断** | 52ms | — |
| C04 | **Esc → `` ` ``(+10ms)** | **未中断** | — | `<Esc>`+`` ` `` 全吞 |
| C05 | Esc → `` ` ``(+5ms) → Esc(+10ms) | **中断** | 21ms | Esc、`` ` `` 全吞（靠第 2 个 Esc 生效） |
| C06 | Esc → Esc(+100ms) | **中断** | 21ms | —（判中断后采集线程退出，第 2 个 Esc 未被消费） |
| C07 | Esc → Esc(+10ms) | **中断** | 21ms | — |
| C08 | Esc → `abcde`（各 +2ms） | **未中断** | — | `<Esc>abcde` 全吞 |
| C09 | Esc → `abcdef` | **未中断** | — | `<Esc>abcde` 吞，`f` 留缓冲被丢弃 |
| C10 | Esc → `abcdef` → Esc（第 8 字节） | **中断** | 41ms | 前 7 字节全吞，靠第 2 个 Esc 生效 |
| C11 | Esc/`` ` `` 交替 ×3 轮（间隔 15ms） | **中断** | 72ms | 第 1 个 Esc+`` ` `` 被吞，第 2 个生效 |
| C12 | `` ` ``/Esc 交替 ×3 轮（间隔 15ms，错键在先） | **中断** | 51ms | 第 1 个 Esc+`` ` `` 被吞，第 2 个生效 |
| C13 | Esc/`` ` `` 交替 ×3 轮（间隔 30ms > 窗口） | **中断** | 20ms | 第 1 个 Esc 即生效 |
| C14 | 事件源对照（`escape_immediate=True`），Esc 与 `` ` `` 交错 | **中断** | **0.4ms** | —（交错不影响） |
| C15 | 采集启动前已积压：`` ` ``(-500ms)、Esc(-300ms) | **中断** | 21ms | —（不清缓冲 ⇒ 按键保留） |
| 稳定性 | C12 复跑 ×5 | 5/5 中断 | 50.6~51.8ms | — |

**读法**：

- **P2 反证**：错键在前（C02/C03/C12/C15）全部能中断 —— 用户"先按错了一个键"的归因不成立；
- **P1 成立**：C04/C08/C09 是"判了非中断、按键全吞"的直接证据；
- **批量积压效应**：C15 证明"采集启动前的按键不会丢"（`_MsvcrtSource` 不清缓冲，`keys.py:78-84`），但积压按键被采集器**连续快速读出**后，等效间隔≈0 → 每个 Esc 的 20ms 窗口里都可能有后随字节（C08/C09 用 2ms 间隔模拟），吞键概率随积压键数上升；
- **平台分叉的意义**：走 `_ConsoleInputSource`（Windows Terminal 等，`escape_immediate=True`）时 C14 表明交错完全不影响判定。**反向推论（可用于 E2 定位）：只有走 msvcrt 字节流源的环境才可能出现"交错吞键"**。

### 2.2 实验二：连接关闭语义（`exp2_close_semantics.py`，本地回环 stub server）

| 用例 | 场景 | 结果 |
| --- | --- | --- |
| A | `httpx.Client.close()`（请求阻塞在等响应头，2s 时 close） | **close 立即中断在途请求**（0.0s，`ReadError`），不等到 8s |
| A2 | **真实 OpenAI SDK** `client.close()`（同场景） | **close 立即中断在途 `create()`**（0.0s，`APIConnectionError`）——与 narnat `abort` 路径同构 |
| B | 关闭后复用客户端 | httpx：`RuntimeError: Cannot send a request, as the client has been closed.`；OpenAI：`APIConnectionError` —— **不可复用**（SDK docstring：`The client will *not* be usable after this.`，`openai/_base_client.py:893-901`） |
| C | 流已建立后 `response.close()` | 立即中断 `iter_lines()`（0.001s，`ReadError`） |

**结论**：本机 `httpx 0.28.1`/`openai 2.16.0` 下，**close 能中断在途请求**（这一条修正了"close 只关空闲连接"的常见假设）；
但"关闭后不可复用"意味着 §3.3 的 P3 是真实且严重的。

### 2.3 实验三：中断置位 → 「已打断」的端到端延迟（`exp3_latency_chain.py`）

方法：真 `ConversationLoop` + 真 `UiSink` + 真 `OpenAIBackend` + 真 `InterruptBus`，LLM 侧换成可编程假客户端
（其 close/中断语义按实验二实测建模），记录完整时间线。

| 场景 | 置位时 handle | 中断 → 显示延迟 | 备注 |
| --- | --- | --- | --- |
| A 响应头等待期间 | `FakeOpenAIClient`（=真实路径的共享 OpenAI 客户端） | **0.0s**（3 次复跑一致） | close 中断 create，生成器异常分支命中 `cancel_check`（`openai_backend.py:196-197`） |
| B 流式输出期间 | 响应流 | **0.036~0.058s**（3 次） | 流关闭 → 队列 `STREAM_END` → 检查点②（`loop.py:507-508`） |
| E 响应头已到、服务端思考静默（**最贴近用户场景**） | 响应流 | **0.008~0.059s**（2 次） | 同上 |
| C 反事实：close **不能**中断在途请求 | 共享客户端 | **4.06~4.09s**（3 次） | 一直等到响应头到达（假设性库行为，用于演示 P4 的结构风险） |
| D 场景 A 之后第二轮 | 共享客户端（已关闭） | 第二轮直接 `error`（3 次） | 产出 `[错误: API调用失败(APIConnectionError，重试1次)]`（含 1 次退避 ≈1s） |

**结论**：

1. **只要中断标志被置位**，本机各阶段的收敛延迟均 ≤0.3s（含退避分片 0.2s，`llm/retry.py:71-85`）——**LLM 层解释不了"很久"**；
2. **P3 是真实的会话级故障**：在"响应头到达前"按 Esc 会关掉共享客户端，**后续整局对话全部失败**（D 场景 + 实验二 B 双证）；
3. **P4 的结构风险**：收敛依赖"close 中断在途请求"这一外部细节（C 场景演示了依赖失效的后果：等到响应头）。

---

## 3. 时序推演（任务点名四项）

### 3.1 `scan_escape` 在"错键与 Esc 交错"时的判定

| 交错形态 | 判定 | 依据 |
| --- | --- | --- |
| `` ` ``…Esc（错键在前，任意间隔） | 中断（`` ` `` 被丢弃） | 实验 C02/C03/C15 |
| Esc…`` ` ``（错键在后，间隔 <20ms，此后无键） | **非中断**，Esc+`` ` `` 被吞 | 实验 C04 |
| Esc…`` ` ``…Esc（Esc 后 5 字节内再见 Esc） | 中断（判"连按"） | 实验 C05/C07/C10/C11 |
| Esc…`abcde`（5 字节内无 Esc） | **非中断**，吞 6 字节 | 实验 C08/C09 |
| Esc/`` ` `` 交替（15ms） | 第 2 个 Esc 生效（置位 51~72ms） | 实验 C11/C12（复跑 ×5 稳定） |
| Esc/`` ` `` 交替（30ms > 窗口） | 第 1 个 Esc 即生效 | 实验 C13 |

### 3.2 `enter_run_mode()` 的 `clear()` + 采集启动 与"立即按键"的竞争

时间轴（Windows 原生控制台）：

```
[用户按 Enter] ──prompt_toolkit 消费并退出──► [主循环：wait_auto_save → _prepare_input → _dispatch_turn]
                                                     │
                                                     ├─ begin_turn(): interrupt.enter_run_mode()（clear + 启采集，keys.py:267-274）
                                                     └─ sink.begin()：动画延迟 0.666s 后才可见（animator.py:44/140-156）
```

- **落入控制台缓冲的按键 → 不丢**：`_MsvcrtSource` 刻意不清空缓冲（`keys.py:78-84`；兼容怪癖，spec `interrupt/spec.md:142`），采集启动后按序读出（实验 C15：启动前积压的 `` ` ``/Esc 被正确处理）；
- **落入 prompt_toolkit 的按键 → 丢失**：`prompt_toolkit` 在 `_is_running=True` 期间消费输入（`application.py:679-694`：`read_from_input` → `input.read_keys()`，其 Win32 实现一次 `ReadConsoleInputW` 批量取 2048 条事件，`input/win32.py:255-261`）；退出时把 key_processor 队列里未处理的按键**存为进程内 typeahead**（`application.py:783` + `input/typeahead.py:48-57`），下次 prompt 时回放（`application.py:676-677`）——**这些按键不会回到控制台缓冲，采集器永远读不到**。退出后 `read_from_input` 有 `_is_running` 保护不再消费（`application.py:686-687`）。
- **窗口边界**：从 Enter 被处理到 `self._is_running = False`（`application.py:751-761`，含 `_redraw(render_as_done=True)` 重绘）；此窗口内按的键会被 prompt_toolkit 吃掉。窗口时长与终端刷新速度相关，**离线无法测定**（见 §7 H3）；
- **动画延迟的干扰项**：`sink.begin()` 后 0.666s 内"思考中"并不显示（`animator.py:44/140-147`），用户"立即按键"时 UI 可能还什么都没显示——这不影响判定，但会影响用户对时机的判断。

### 3.3 "已打断"的延迟来源（谁在阻塞）

检查点/开销清单（含实测）：

| 环节 | 机制 | 文件:行号 | 延迟 |
| --- | --- | --- | --- |
| 按键 → 置位 | 30ms 轮询 + 20ms 判定窗口（+吞键重试） | `keys.py:38/41/203-243` | 20~72ms（实验 C01~C15） |
| 采集线程停止 | `join(timeout=0.2)`（不同步确认） | `keys.py:47/276-282` | ≤0.2s |
| 生成器内取消轮询 | 队列 `get(timeout=0.05)` | `openai_backend.py:245-247`、`anthropic_backend.py:297-299`、`runtime.py:29` | ≤50ms（实验 B/E：8~58ms） |
| `create()/send()` 阻塞期 | **无取消检查点**，依赖 abort 关闭句柄中断请求 | `openai_backend.py:149`、`anthropic_backend.py:151` | 0.0s（实验 A）；反事实 4.07s（实验 C） |
| HTTP/流中断重试退避 | `retry_sleep` 0.2s 分片取消检查 | `llm/retry.py:71-85`、`loop.py:562` | ≤0.2s |
| 工具执行 | 广播杀进程树/发中断字符（本场景无工具） | `assembly.py:220-221`、`llm/client.py:68-69` | — |
| 流中断看门狗 | 仅静默 >180s 才触发（`runtime.py:32`） | `openai_backend.py:251-258` | 不参与"打断" |

**关于"abort 后流中断进入重试退避"**：中断触发后，被 close 的流会让 reader 线程异常退出，生成器上报"流中断"事件（`openai_backend.py:353-356`）；若此时检查点②尚未命中，`loop.py:549-564` 会进入整轮重试分支（先发提示、`sink.restart_attempt()`、再退避等待）。退避由 `retry_sleep(retries - 1, lambda: sink.cancelled)` 驱动，**取消在 ≤0.2s 分片内生效**（`llm/retry.py:45/71-85`）——即退避不会吞掉中断，最坏贡献 0.2s 延迟；HTTP 层重试退避（`openai_backend.py:175/184/204`、`anthropic_backend.py:184/205/242`）同理。

**结论**：离线实测"置位 → 显示"总延迟 **≤0.3s**（除反事实 C）。因此"过了很久才显示已打断"最可能对应以下三种组合之一，均属**"中断标志很久才被置位"或"体感"**，而不是收敛本身慢：

1. **按键层没生效**：吞键（P1）需要反复按；或按键根本没到采集器（P5 降级 / §3.2 prompt_toolkit 窗口）；
2. **中断早已生效但无任何即时反馈**（P6）：用户无法判断，继续按（反复 `raise_` 是幂等的），直到收敛输出"已打断"——用户把"从第一次按到看到提示"的整体时长感知为"很久"；
3. **依赖失效的结构风险**（P4）：若用户侧运行环境的底层网络库 close 不能中断在途请求（或响应头长期不来），才可能出现"等到响应头才显示"的真正秒级延迟（实验 C 给出机制演示）。

### 3.4 `KeyListener` 静默降级：证实与证伪

- **证实（离线）**：`source_factory` 抛异常 / 返回 None 两种情况（`keys.py:286-291`）→ 线程 100ms 内退出、`running=False`、**无异常冒出、无任何提示**（exp1 降级验证）；此后 Esc 永久无效，而"思考中"动画照常播放。
- **触发条件（代码）**：`keys.py:300-307`/`324-337` —— stdin 不是控制台（管道/重定向，`GetConsoleMode` 失败 → 改走 `_ConsoleInputSource`，而该源在非控制台句柄上 `ReadConsoleInputW` 失败 → `poll_keys` 捕获 `OSError` 静默 return）；或任何构造异常。
- **真机证伪方法（给 E2）**：在目标终端里跑
  `python -c "import ctypes,msvcrt;h=ctypes.windll.kernel32.GetStdHandle(-10);m=ctypes.c_ulong();print('GetConsoleMode=',bool(ctypes.windll.kernel32.GetConsoleMode(h,ctypes.byref(m))),'mode=',hex(m.value))"`
  —— `True` 且模式含 `0x80`（`ENABLE_VIRTUAL_TERMINAL_INPUT`）说明会走 msvcrt 字节流源（P1 适用）；`False` 则走事件源（P1 不适用，需另找原因）。**未在真机执行，标记「未验证」。**

---

## 4. 根因主张清单（主张 / 依据 / 强度）

| 主张 | 依据（文件:行号） | 实验证据 | 强度 |
| --- | --- | --- | --- |
| P1 判定窗口吞键 | `keys.py:41/44/203-221/86-90` | exp1 C04/C08/C09 | 证实 |
| P2 敏感条件在 Esc 之后，非"先按错键" | 同上 | exp1 C02/C03/C12/C15 反证 + C04 正证 | 证实 |
| P3 `abort` 关闭共享 OpenAI 客户端 → 永久失效 | `openai_backend.py:124/224`、`runtime.py:93-100`、`openai/_base_client.py:893-901` | exp2 A2/B、exp3 D | 证实 |
| P4 `create()` 阻塞期无取消检查点 | `openai_backend.py:149`、`anthropic_backend.py:151`、异常分支 `196-197/216-217/234-235` | exp3 A（0.0s）/C（4.07s 反事实） | 证实（机制）+ 结构风险 |
| P5 静默降级 | `keys.py:286-291/300-307/324-337` | exp1 降级验证 | 行为证实，命中条件待真机 |
| P6 无即时反馈 | `stream.py:162-164`（begin 只启动动画）与 `228-240`（abort 才输出）、`animator.py:140-156` | 代码依据 + 实验 B/E 显示时刻即收敛时刻 | 机制成立，体感影响待真机 |

---

## 5. 修复方案（≥2 候选，**不实施**）

### F1 按键采集改"事件语义"，取消字节流+时间窗口猜测（对治 P1/P2，推荐）

- **对治根因**：P1（20ms 窗口吞键）、P2（判定依赖按键相对时序导致"时好时坏"）。
- **做法**：Windows 统一以 `ReadConsoleInput` 事件源为主路径——只认"键按下且 `wVirtualKeyCode == VK_ESCAPE`"的事件（`keys.py:109-162` 已实现，`escape_immediate=True`），msvcrt 字节流仅作降级。事件层天然区分"Esc 键按下"与"Alt+`` ` `` 组合/方向键序列"（后者是同一事件携带 `ALT_PRESSED`/非 VK_ESCAPE，不产生独立的 VK_ESCAPE 事件）。
- **影响面**：所有 Windows 用户的中断判定变为"按下即中"（实验 C14：0.4ms），不再吞键；两条 Windows 路径行为统一。
- **代价与风险**：① 需真机回归"方向键/功能键不误触发"（spec `interrupt/spec.md:112-114`）与 Alt 组合；② ConPTY/终端兼容需实测；③ 降级条件（非控制台句柄）保留；④ 若事件源在个别环境不可用，需回退 F4。
- **为什么最根本**：把"猜"（时间窗口推断）换成"知道"（事件语义），整类"吞键/漏键"问题消失，且不引入任何延时或轮询补偿。

### F2 请求级可取消 + 禁止关闭共享客户端（对治 P3/P4，推荐）

- **对治根因**：P3（共享客户端被永久关闭 → 打断一次后全崩）、P4（阻塞期无检查点、收敛依赖库实现细节）。
- **做法**：① 活动句柄只登记**本次请求**的实例（响应流/连接），OpenAI 路径不再 `attach_handle(self._client)`（`openai_backend.py:124`）——或改为"每轮请求独立客户端/独立 stream 句柄，请求结束即释放"；② `abort` 语义收敛为"关闭本次请求的可中断句柄"，共享客户端生命周期归会话；③ `create()/send()` 阻塞期引入显式取消检查（例如读超时缩短后轮询 `cancel_check`，或把阻塞调用置于可被取消的执行体），使收敛不再依赖"close 恰好能中断在途请求"。
- **影响面**：中断在响应头前也能瞬时收敛且**不再污染后续请求**（消除 D 场景）；正常路径行为不变。
- **代价与风险**：触及两个 LLM 后端的连接管理，需回归重试矩阵/看门狗/流中断语义（`tests/unit/test_llm.py` 等）；OpenAI 客户端从"长连接复用"变为"按需构造"可能有轻微建连开销（可忽略）。

### F3 中断"已受理"即时反馈（对治 P6，建议与 F1/F2 并行）

- **对治根因**：P6（置位后无任何可见变化，用户无法区分"被吞"与"已生效"）。
- **做法**：`InterruptBus.raise_()` 增加一个幂等的"受理反馈"动作（由 UI 注册）：立即停止"思考中"动画并打印单行"正在中断…"；收敛后仍显示"已打断"。可由 `bus.subscribe(...)` 承载（已有订阅机制，`bus.py:68-70`）。
- **影响面**：用户按下 Esc 立刻有可见反馈 → 不再狂按；即使收敛有延迟（网络/服务端原因），体感也显著改善。
- **代价与风险**：输出需与 prompt_toolkit 交还次序配合（现有 abort 已处理行首对齐，`console.py:132-145`）；多来源中断（Esc/Ctrl+C/工具中断）需幂等去重；文案需定稿。

### F4 判定规则最小修正（F1 的降级方案，可选）

- **对治根因**：P1 中"误把 Esc+普通字符判成转义序列"的子集。
- **做法**：消费窗口后若收到的字节不构成合法转义序列起始（第二字节不是 `[`/`O`/`0x00`/`0xE0` 前缀），则**仍判中断**（普通字符不可能是 CSI/SS3 序列的组成部分）。
- **风险**：改变 spec 明示保持的兼容怪癖（`spec.md:142`）；在 VT 输入模式下 Alt 组合正是 `Esc`+字符（`input/win32.py:516-522` 的等价语义），会误判为中断——**需真机确认**，故仅作降级方案。

**推荐组合**：**F1 + F2 + F3**（分别对治 按键层/请求层/体验层）；F4 仅在事件源不可用的平台作回退。

---

## 6. 待验证假设清单（交 E2 与父代理）

| # | 假设 | 建议验证方法 | 关联主张 |
| --- | --- | --- | --- |
| H1 | 目标环境走 msvcrt 字节流源（P1 适用的前提） | 跑 §3.4 的一行探测命令，记录 `GetConsoleMode` 结果与 `mode` 位 | P1/P5 |
| H2 | 按键在"运行模式启动前"被 prompt_toolkit 退出窗口消费（长度 ≈ 重绘耗时） | 扫描"回车后 x ms 按一次 Esc"（x=0,10,20,…200），统计生效率曲线；若 0~50ms 生效率显著低 → 假设成立 | §3.2 |
| H3 | **"Esc 后 20ms 内紧跟错键（C04 型时序）"是按键层最可靠的失效复现** | E2 序列 3 改为严格的"Esc 先按、`` ` `` 在 10~15ms 后按、随后停手"，重复 ≥5 次 | P1/P2 |
| H4 | 中断生效时刻 vs 响应头到达时刻：若"已打断"出现在服务端首个响应之后 → abort 未中断在途请求 | 用 `-d` 日志时间戳 + 屏幕读数对齐；对照 exp3 A/E 的 0.0s/8ms 基准 | P4 |
| H5 | 打断一次后，**下一轮请求是否立即失败**（P3 的会话级故障是否被用户命中） | E2 序列：在"响应头到达前"按 Esc 打断 → 再提一个简单问题，观察是否报 `API调用失败` | P3 |
| H6 | 旧实现（git `7d075a2`）行为是否与新实现一致 | 代码对照已确认判定算法逐行等价（`_old_impl_rollback/ui/interrupt.py:139-163` 对 `keys.py:203-221`）；E2 做黑盒对照确认 | P1/P2 |
| H7 | 采集器是否曾静默降级（用户环境） | E2 在 `-d` 日志或调试探针中打印 `KeyListener.running` 与所选源类型 | P5 |
| H8 | 用户对"很久"的量化（秒级？）与"是否在响应头前按的" | E2 用控制台注入 + 读屏时间线测量（任务已给出注入方案） | P4/P6 |

---

## 7. 未验证与不确定项（如实标注）

1. **秒级延迟未能在离线复现**：本机（httpx 0.28.1 / openai 2.16.0）下"置位 → 显示"≤0.3s。因此 P4/P6 只是"候选机制"，**真机时间线由 E2 给出**（标「未验证」）。
2. **prompt_toolkit 退出窗口的实际时长与吞键概率**：源码依据充分（`application.py:679-694/751-761/783`，`typeahead.py:48-57`），但**未在真机测量**（标「未验证」）。
3. **真机下 `_MsvcrtSource` 读到的字节形态**（cooked 模式按键 vs VT 输入模式序列、Alt 组合表示）**未在本机实测**；实验一用"到达时刻表"建模，未模拟平台字节形态差异（标「未验证」）。
4. **`msvcrt.getch()` 与 prompt_toolkit `ReadConsoleInputW` 在同一控制台缓冲上的竞争**（退出残留读取线程）在源码层未发现"退出后继续消费"路径（`application.py:686-687` 保护），但**未真机验证**（标「未验证」）。
5. 用户报告"狂按 esc 也没有用"若为**按键已生效但无反馈**（P6），离线无法区分其与"按键被吞"的主观差异——需 E2 的屏幕时间线（标「未验证」）。
6. 本报告的延迟数字均为本机环境实测；用户环境（终端宿主、网络、服务端行为）可能不同。

---

## 8. 复跑与产物

```cmd
cd /d D:\desktop\NarnatAgent
chcp 65001 >nul
python docs\recast\esc_probe\exp1_key_judgement.py    :: 判定表：15 组 + 稳定性 ×5 + 降级验证
python docs\recast\esc_probe\exp2_close_semantics.py  :: 关闭语义：4 用例（本地回环，无外网）
python docs\recast\esc_probe\exp3_latency_chain.py    :: 端到端延迟：5 场景 + 完整时间线
```

- 三脚本幂等可复跑；exp1 复跑 3 次结论一致（均 12 组中断 / 3 组被吞、稳定性 5/5 中断、降级 2/2）；exp3 复跑 3 次时间线一致（A=0.0s ×3；B=27/58/36ms；C=4.06/4.07/4.09s；D 第二轮均立即失败；E=8/59ms）。
- 完整复跑日志：`docs/recast/esc_probe/out/run_all.log`；产物 JSON 位于 `docs/recast/esc_probe/out/`。
- 未修改任何 `narnat_agent/`、`tests/`、`main.py` 文件（`git status --porcelain narnat_agent tests main.py` 输出为空）。
