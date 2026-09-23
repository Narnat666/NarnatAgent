# E1 探索任务：ESC 打断"不丝滑"根因分析（机制级）

## 现象（用户报告原文，逐字引用）

> 我打开narnat程序，询问你好，然后立即按下`键和esc键，交替按，这个时候ui会一直显示思考中，无法打断，也没有输出，后续狂按esc也没有用
> 等待了一会，才显示打断了，即当用户想按esc打断的时候，先按错了一个键，后续又按esc就会出现这种问题
> 用户狂按esc，后续过了很久界面显示已打断

用户诉求：**从根本上解决**（要机制的根因与结构性修复，不要"加延时/加重试"式补丁）。

## 环境与代码线索（背景事实，非结论）

- 项目：`D:\desktop\NarnatAgent`（刚完成积木化重构并切换；旧实现完整保留在 `_old_impl_rollback/`，git 历史 `7d075a2` 可取）
- 中断链路相关模块（阅读起点）：
  - `narnat_agent/interrupt/keys.py`：按键采集（`KeyListener` / `poll_keys` / `scan_escape` / 三个平台字符源；注意 `_MsvcrtSource` 与 `_ConsoleInputSource` 两条 Windows 路径）
  - `narnat_agent/interrupt/bus.py`：`InterruptBus`（`raise_` 置位+广播；`enter_run_mode` 先 clear 再启动采集；`enter_input_mode` 停止采集）
  - `narnat_agent/ui/stream.py`：`UiSink`（`cancelled` 查询、`begin/feed/finish/abort`）、`UiInteraction.begin_turn`（`enter_run_mode` → 建 sink → `sink.begin()` 启动"思考中"动画）
  - `narnat_agent/conversation/loop.py`：内循环的取消检查点（chunk 循环内/循环后、流中断重试分支、溢出恢复分支）
  - `narnat_agent/llm/`：`client.py`（构造时 `interrupt.subscribe(self.abort_active_request)`）、`runtime.py`（`LLMRuntime.attach_handle/detach_handle/abort`；队列泵 `STREAM_POLL_SECONDS=0.05` 轮询与取消）、两个后端的取消检查点
  - `narnat_agent/app/assembly.py`：装配订阅清单（第 7/9 步）
- 背景事实：UI 显示"思考中"说明已调用 `sink.begin()`（即 `begin_turn` 已在执行路径上）；但采集器可能静默降级（`KeyListener._run` 建源失败即退出，不报错）。
- 用户可能的按键时序（假设空间，需实验甄别）：回车后立即按键，按键可能（a）在运行模式启动前进入控制台输入缓冲区、（b）被 prompt_toolkit 退出过程消费/残留、（c）在采集器判定窗口内与 ESC 交错到达。

## 任务目标

### 1. 机制级根因（必须可验证）

解释"先按错键（`），再按 ESC（交错/连按）"为何导致（a）无法/难以打断 与（b）延迟很久才显示"已打断"。要求：

- 给出从"按键进入系统"到"UI 显示已打断"的**完整链路**（每个环节标注：文件:行号、状态变化、触发条件）；
- 对关键判定做**时序推演**，至少覆盖：
  * `scan_escape` 的"20ms 判定窗口 + 最多消费 5 字节"在"错键与 ESC 交错"时的判定结果（什么序列会被判为"非中断"？什么序列会被消费吞掉？）；
  * `enter_run_mode()` 的 `clear()` + 采集启动 与用户"立即按键"的竞争（按键落在采集启动前会怎样？被谁消费？）；
  * 若"已打断"最终显示，其延迟来源（中断置位时刻 vs 显示时刻之间，谁在阻塞？LLM 连接 abort 是否命中活跃句柄？abort 后流中断若进入重试退避，取消检查在何时生效？）；
  * `KeyListener` 静默降级是否可能触发（此场景下如何证伪/证实）。
- 用**受控实验**验证推演：写独立脚本（放 `docs/recast/esc_probe/`），用**假字符源**（可控投喂字节序列与到达时间）驱动真实的 `poll_keys`/`scan_escape`/`InterruptBus`，产出"按键序列 × 时间间隔 → 判定结果"表格（≥8 组，含：纯 ESC；` 后 ESC；ESC 后紧跟 `；` ESC ` ESC 交错；ESC 后紧跟其他字符；两次 ESC 间隔 >20ms / <20ms 等）。
- 对"中断置位后响应链延迟"的分析需逐环节给出代码依据（如 LLM 层的取消轮询间隔、重试退避逻辑、conversation 检查点位置），并指出哪个环节可能造成"很久才显示"。

### 2. 修复方案（≥2 个候选，**不实施**）

每个方案给出：对治的根因环节、行为影响面（用户可感知变化）、代价与风险；说明哪个**最根本**及理由。禁止"加延时/加轮询频率"式补丁，除非论证它是唯一正解。

### 3. 待验证假设清单

列出无法离线验证、需真机确认的点（供 E2 与父代理验收使用）。

## 交付物

- 报告：`docs/recast/reports/E1_esc_probe.md`
- 实验脚本：`docs/recast/esc_probe/`（脚本须可复跑、自证结论）
- 允许复制代码到 `docs/recast/esc_probe/` 内做 POC；**不得修改** `narnat_agent/`、`tests/`、`main.py` 等主线文件

## 边界条款

- 不修改主线源码/测试；不执行 git 写操作（只读 git 命令可用）
- 实验不得依赖真实 API（不发起任何 LLM 网络请求）；用 fake/stub 驱动
- **不做真机按键注入实验**（那是 E2 的任务，避免互相干扰）
- 最多尝试 3 种分析路径；无法收敛则如实报告

## 验收标准（可计算）

1. 报告存在；每条根因主张附：文件:行号 + 时序推演 +（若有）实验证据
2. 判定实验表 ≥8 组，可由脚本复跑验证一致
3. 修复方案 ≥2 个，各含"对治根因""影响面""风险"三要素，并给出推荐
4. 全文无未标注依据的断言（不确定项必须标「未验证」）

## 失败报告格式

①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
