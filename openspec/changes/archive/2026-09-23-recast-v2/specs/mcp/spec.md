# Spec Delta

## Purpose

定义 MCP 集成能力的行为契约：`MCP` 连接通道工具（connect/disconnect 的参数面与结果文案）、连接流程（stdio 子进程启动、JSON-RPC 握手与工具列举）、MCP 工具的热注册与命名规则、工具调用转发与结果格式化、断开与进程终止、通道保活（断线自动重连与重试语义）、进程生命周期与回收、失败不阻塞原则。工具名、参数名、工具命名规则、错误文案与进程回收保证是面向 AI 与用户的已发布契约，必须跨版本保持等价。本规格中的「错误行」指形如 `[错误: …]` 的框架错误文本（附进程级随机标签、交付 AI 前剥离）。

## ADDED Requirements

### Requirement: MCP 工具名与参数契约

系统 SHALL 以固定工具名 `MCP` 注册连接通道工具，工具定义语义为「MCP 服务器连接通道（仅支持本地 stdio 型服务器）」。参数 SHALL 为：`action`（string，可选，取值 `connect`/`disconnect`，默认 `connect`；connect＝连接一个本地 MCP（stdio）服务器，连接后其工具以 `mcp__<服务器名>__<工具名>` 注册、后续轮次可直接调用；disconnect＝断开并注销其工具）；`name`（string，可选，服务器名，成为工具名前缀 `mcp__<name>__`；disconnect 用已连接的服务器名或 `all`（断开全部））；`config`（object，可选，connect 的启动配置，形如 `{"command": "python", "args": ["main.py"], "env": {}, "cwd": ""}`，中英文键均可，可选 `startup_timeout_sec` 启动超时、`tool_timeout_sec` 工具调用超时、`enabled_tools`/`disabled_tools` 工具白/黑名单）。全部参数均非必填。`action` 缺失或为空 SHALL 按 `connect`；执行前 SHALL 去除首尾空白并小写归一；其它取值 SHALL 返回错误行 `未知 action: {action}（可用: connect/disconnect）`。工具运行时上下文缺失或其中未初始化 MCP 连接管理时 SHALL 返回错误行 `MCP 管理器未初始化`。

#### Scenario: 默认 action
- **WHEN** 调用 `MCP` 不传 `action`
- **THEN** 按 `connect` 处理

#### Scenario: 未知 action
- **WHEN** `action` 传 `"foo"`
- **THEN** 返回 `[错误: 未知 action: foo（可用: connect/disconnect）]` 形态错误行

#### Scenario: 管理器缺失
- **WHEN** 未携带工具运行时上下文直接调用
- **THEN** 返回 `[错误: MCP 管理器未初始化]` 形态错误行

### Requirement: MCP connect 前置校验与结果文案

`MCP` 的 connect 分支 SHALL 依次校验并返回固定文案：`name` 去除空白后为空 → 错误行 `connect 需要 name（服务器名）`；`config` 缺失、非对象或空对象 → 错误行 `connect 需要 config（启动配置：command/args/env/cwd）`；配置解析失败或启动命令为空 → 错误行 `config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器`。

连接成功 SHALL 返回首行 `[已连接 MCP服务器 {name}：注册 {count} 个工具，后续轮次可直接调用]`，其后每行一个工具名（两个空格缩进）；工具名超过 20 个时只展示前 20 个，再追加一行 `  …共{N}个`（N 为注册工具总数）。

#### Scenario: 缺 name
- **WHEN** connect 时 `name` 为空白串
- **THEN** 返回 `[错误: connect 需要 name（服务器名）]` 形态错误行

#### Scenario: 缺 config
- **WHEN** connect 时未传 `config` 或传 `{}`
- **THEN** 返回 `[错误: connect 需要 config（启动配置：command/args/env/cwd）]` 形态错误行

#### Scenario: 缺启动命令
- **WHEN** `config` 中无启动命令
- **THEN** 返回 `[错误: config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器]` 形态错误行

#### Scenario: 成功展示与 20 项上限
- **WHEN** 某服务器连接成功并注册 25 个工具
- **THEN** 首行报告 `注册 25 个工具`，逐行展示前 20 个工具名后追加 `  …共25个`

### Requirement: MCP disconnect 行为与结果文案

`MCP` 的 disconnect 分支 SHALL 校验 `name`：去除空白后为空 → 错误行 `disconnect 需要 name（已连接的服务器名，或 all 断开全部）`。`name` 小写后为 `all`、`*` 或 `全部` → 断开全部已连接服务器，返回 `[已断开全部 MCP服务器，注销 {N} 个工具]`（N 为累计注销工具数）。指定名 SHALL 大小写不敏感地匹配已连接服务器（精确同名优先，否则取大小写不敏感的首个匹配）：匹配到时断开该服务器并返回 `[已断开 MCP服务器 {服务器名}，注销 {N} 个工具]`；无匹配时返回错误行 `服务器 {name} 未连接。当前已连接: {以、连接的清单，无服务器时为 (无)}`。

#### Scenario: 断开全部
- **WHEN** `action="disconnect"`、`name="all"`
- **THEN** 断开全部已连接服务器，返回 `[已断开全部 MCP服务器，注销 {N} 个工具]`

#### Scenario: 未连接
- **WHEN** 指定名不存在于已连接清单
- **THEN** 返回 `[错误: 服务器 {name} 未连接。当前已连接: {清单或(无)}]` 形态错误行

#### Scenario: 大小写不敏感匹配
- **WHEN** 服务器以 `Demo` 连接，disconnect 传 `demo`
- **THEN** 断开并返回 `[已断开 MCP服务器 Demo，注销 {N} 个工具]`

### Requirement: 连接流程与 JSON-RPC 握手协议

connect SHALL 以标准输入输出承载按行分帧的 JSON-RPC 2.0 消息启动并握手服务端子进程，流程固定为：启动子进程 → `initialize` 请求 → 收到响应后发送 `notifications/initialized` 通知 → `tools/list` 列举工具。`initialize` 请求参数 SHALL 为：`protocolVersion` = `2025-06-18`、`capabilities` = `{}`、`clientInfo` = `{"name": "narnat-agent", "title": "Narnat Agent", "version": "1.0"}`。`tools/list` SHALL 自动翻页（响应含 `nextCursor` 时携带 `cursor` 继续请求），翻页上限 50 页。握手与列工具 SHALL 使用同一启动超时（配置 `startup_timeout_sec`，解析规则见 config 规格）；握手或列工具失败 SHALL 关闭已启动的子进程（不残留进程）。

帧协议 SHALL 为：消息以 JSON 序列化（非 ASCII 字符不转义）加换行结尾、逐行写入；读取逐行解码（UTF-8 严格优先，Windows 平台失败时回退 GBK，再失败以替换字符兜底）；非 JSON 行与非对象消息 SHALL 被忽略（仅记日志）；响应按 `id` 匹配等待者；服务端通知（有 `method` 无 `id`）仅记日志；服务端主动请求（有 `method` 且有 `id`）SHALL 一律回绝（错误码 -32601）并记日志，连接保持可用。

#### Scenario: 握手顺序
- **WHEN** 连接一个正常 MCP 服务器
- **THEN** 依次发出 `initialize` 请求（上述参数）、`notifications/initialized` 通知、`tools/list` 请求

#### Scenario: 分页列举
- **WHEN** `tools/list` 响应带 `nextCursor`
- **THEN** 携带 `cursor` 继续请求直到无 cursor，各页工具合并为完整列表

#### Scenario: 启动失败
- **WHEN** 启动命令不存在或不可执行
- **THEN** 返回 `[错误: MCP connect 失败: 启动失败: {异常文本}]` 形态错误行

#### Scenario: 握手失败不残留进程
- **WHEN** `initialize` 或 `tools/list` 阶段失败
- **THEN** 已启动的子进程被关闭，返回 `[错误: MCP connect 失败: …]` 形态错误行

#### Scenario: 服务端主动请求回绝
- **WHEN** 服务端发来带 `id` 的请求（如 sampling）
- **THEN** 回复 `-32601` 错误响应并记日志，连接继续可用

### Requirement: MCP 工具热注册与命名规则

连接成功后，`tools/list` 返回的工具 SHALL 以 `mcp__<服务器名>__<工具名>`（前缀 `mcp__`）注册为可调用工具。命名规则 SHALL 为：服务器名与工具名片段先清洗——非 `A-Za-z0-9_-` 的字符全部替换为 `_` 并去除首尾 `_`；清洗后为空（如纯中文名）时以 `srv_` 加该名 SHA1 摘要前 8 位代替；完整名长度不超过 64 且未被占用时直接使用；超长或重名时截断为前 51 字符并追加 `_` 加该「服务器名与工具名」标识的 SHA1 摘要前 12 位（总长 ≤64），仍冲突则更换哈希后缀直到唯一。

注册 SHALL 立即生效、对下一轮 LLM 请求可见（工具定义热追加），且工具名与定义可被调用与列举；与既有工具重名（内置工具或已注册工具）时 SHALL 跳过该工具、不影响其余。工具描述缺省时兜底 `MCP工具 {原始工具名}（来自 {服务器名}）`；工具输入模式缺失、非对象或为空对象时以 `{"type": "object", "properties": {}}` 代替。`enabled_tools` 白名单非空时仅注册名单内工具；`disabled_tools` 黑名单内工具不注册（两者均按服务端原始工具名匹配，过滤发生在注册前）。调用转发 SHALL 使用服务端原始工具名。

#### Scenario: 命名规则
- **WHEN** 服务器 `demo` 提供工具 `search`
- **THEN** 注册名为 `mcp__demo__search`

#### Scenario: 非法字符替换
- **WHEN** 服务器名 `my server`、工具名 `a.b`
- **THEN** 片段清洗为 `my_server` 与 `a_b`，注册名为 `mcp__my_server__a_b`

#### Scenario: 纯中文名哈希兜底
- **WHEN** 服务器名或工具名为纯中文等无合法字符的名称
- **THEN** 该片段以 `srv_` 加 SHA1 摘要前 8 位代替（稳定生成合法名）

#### Scenario: 超长与重名截断
- **WHEN** 生成的名称超过 64 字符或与已用名冲突
- **THEN** 名称截断为前 51 字符加 `_` 与 12 位哈希后缀（总长 ≤64），且互不重复

#### Scenario: 白黑名单过滤
- **WHEN** `enabled_tools=["a"]` 且服务端提供 `a`、`b`
- **THEN** 只注册 `a`（以 `mcp__<服务器>__a` 形态注册），`b` 不注册

#### Scenario: 下一轮即可见
- **WHEN** 连接成功、工具注册完成
- **THEN** 下一轮 LLM 请求的工具定义已包含新注册的 MCP 工具

### Requirement: MCP 工具调用协议与结果格式化

调用已注册的 MCP 工具 SHALL 转发一次 `tools/call` 请求，参数为 `{"name": 服务端原始工具名, "arguments": 调用参数}`（调用参数缺失按空对象处理）；等待超时 SHALL 采用该连接配置的工具调用超时（`tool_timeout_sec`，解析规则见 config 规格）。响应结果 SHALL 按内容块拼接为文本：`text` 块取原文；`image` 块转为 `[图片: {MIME类型，缺失为“未知类型”}]`；`resource` 块取其文本（无文本时以 `[资源: {URI}]` 代替）；其它类型块以 JSON 序列化；内容块为空而有结构化内容时以 JSON 序列化兜底；各块以换行连接。拼接结果为空 SHALL 返回 `(工具无输出)`。响应带错误标志（`isError`）时 SHALL 返回错误行（文本为空时以 `工具返回错误` 兜底）。

等待超时 SHALL 以 `{method} 超时({超时值}s)` 形态报错（超时值为整数时显示为整数秒，如 `tools/call 超时(30s)`）；服务端返回错误响应 SHALL 以 `{method} 失败: {错误消息}` 形态报错。上述错误与该工具调用的其它连接类错误（程序退出中的 `程序正在退出`、服务器未连接的 `服务器 {name} 未连接`）经工具实现统一转为错误行 `MCP工具 {服务器名}/{原始工具名} 调用失败: {错误文本}` 返回，不得向会话抛异常。

#### Scenario: 文本内容
- **WHEN** 工具返回单个 `text` 块
- **THEN** 结果文本即该块原文

#### Scenario: 图片与资源块
- **WHEN** 工具返回 `image`（mimeType `image/png`）与无文本的 `resource`（URI `file:///x`）块
- **THEN** 拼接文本含 `[图片: image/png]` 与 `[资源: file:///x]`

#### Scenario: 错误标志
- **WHEN** 响应 `isError` 为真且无文本
- **THEN** 返回 `[错误: 工具返回错误]` 形态错误行

#### Scenario: 服务端错误响应
- **WHEN** 服务端返回带 `error` 的响应（消息为 `boom`）
- **THEN** 返回 `[错误: MCP工具 {服务器}/{工具} 调用失败: tools/call 失败: boom]` 形态错误行

#### Scenario: 调用超时
- **WHEN** 服务端在配置的工具调用超时内未响应
- **THEN** 返回 `[错误: MCP工具 {服务器}/{工具} 调用失败: tools/call 超时({N}s)]` 形态错误行

### Requirement: MCP 断开、工具注销与进程终止

断开服务器 SHALL 完成三件事：从工具注册中注销该服务器的全部工具（工具名从可调用清单与 LLM 工具表移除，随后调用返回 `[错误: 未知工具: {工具名}]`）、移除连接登记（连接、工具清单、配置）、终止服务端进程。注册与注销 SHALL 互斥（注销与「连接登记加注册」不可交错，杜绝无人跟踪的残留工具）。

进程终止策略 SHALL 为优雅优先：先关闭标准输入让服务端自行退出；3 秒宽限内未退出则强杀——Windows 杀主进程；类 Unix 杀进程组（先 SIGTERM，2 秒内未退再 SIGKILL）。对已断开或进程已退出的连接再次断开 SHALL 幂等（无副作用）；断开不存在的服务器 SHALL 以 `服务器 {name} 未连接` 形态报错。

#### Scenario: 注销工具
- **WHEN** 断开服务器 `demo`
- **THEN** 其工具从可调用清单与 LLM 工具表移除；随后调用 `mcp__demo__search` 返回 `[错误: 未知工具: mcp__demo__search]`

#### Scenario: 优雅关闭
- **WHEN** 断开时服务端进程正常响应标准输入关闭并在 3 秒内退出
- **THEN** 不发送强杀信号，进程正常退出

#### Scenario: 超时强杀
- **WHEN** 服务端 3 秒内未退出
- **THEN** 进程被强制终止（类 Unix 先杀进程组 SIGTERM，2 秒未退再 SIGKILL）

#### Scenario: 重复断开
- **WHEN** 对已断开的服务器再次断开
- **THEN** 以「未连接」报错（不产生进程或注册残留）

### Requirement: MCP 通道保活与自动重连

系统 SHALL 记录每次连接的原始配置用于断线恢复（AI 无需重新提供配置）。调用已注册 MCP 工具时：若连接进程已不在存活 → 先用记录配置自动重连，再报 `连接曾中断，已自动重连，请重试本次调用`（SHALL NOT 自动重跑本次调用）；调用过程中连接中断（传输错误且进程不再存活）→ 同样自动重连并报 `连接曾中断，已自动重连，请重试本次调用`；若进程仍存活（服务端自身错误或调用超时）→ 原样上报错误，不触发重连。重连 SHALL 只替换服务端进程，不改变已注册工具（工具名与定义保持）；重连推进期间该服务器已被断开（或程序正在退出）时 SHALL 丢弃新建通道并报 `服务器 {name} 已断开，无需重连`；无记录配置或配置不完整时 SHALL 报 `服务器 {name} 无配置，无法自动重连` / `服务器 {name} 配置不完整，无法自动重连`。上述错误均经工具实现转为调用失败错误行返回。

#### Scenario: 断线后重连并要求重试
- **WHEN** 服务器进程已死，AI 调用其工具
- **THEN** 自动重连成功并返回 `[错误: MCP工具 {服务器}/{工具} 调用失败: 连接曾中断，已自动重连，请重试本次调用]` 形态错误行；再次调用成功

#### Scenario: 不自动重跑
- **WHEN** 上述重连发生
- **THEN** 原调用不被自动重复执行（防写操作/仿真被静默执行两次）

#### Scenario: 服务端自身错误不重连
- **WHEN** 调用返回服务端错误响应且进程仍存活
- **THEN** 错误原样上报（`tools/call 失败: …`），不触发重连

#### Scenario: 重连期间已被断开
- **WHEN** 重连推进期间该服务器被断开
- **THEN** 新建通道被丢弃，报 `服务器 {name} 已断开，无需重连`

### Requirement: 进程生命周期与回收

MCP 服务端子进程 SHALL 置于独立进程组（Windows：新进程组且独立无窗口控制台；类 Unix：新会话），使本进程控制台的中断（ESC/Ctrl+C）不波及服务端，且服务端的原生控制台输出不污染本终端。子进程环境 SHALL 为父进程环境全量继承加配置 `env` 覆盖（键值转为字符串）；工作目录取配置 `cwd`（为空用当前目录）。

回收 SHALL 覆盖全部退出路径：会话结束（正常结束、中断、异常）关闭全部连接并清空登记；退出命令关闭全部连接；程序未走到上述路径而退出时，由解释器退出钩子兜底回收（首次成功连接后注册，幂等）。同时连接的服务器数上限 SHALL 为 8：达上限时新连接报错 `已达并发上限(8个)，当前已连接: {以、连接的清单}。请先 disconnect 不需要的服务器`；同名已连接且存活时 connect SHALL 幂等（直接返回既有工具数与清单，不新建进程）；同名存在但进程已死时 SHALL 先断开旧连接（注销旧工具、关闭死进程）再重建。

#### Scenario: 独立进程组
- **WHEN** 服务端启动
- **THEN** 它处于独立进程组/会话中（用户中断不误杀服务端，服务端控制台输出不进入本终端）

#### Scenario: 并发上限
- **WHEN** 已有 8 个服务器连接时连接第 9 个
- **THEN** 返回 `[错误: MCP connect 失败: 已达并发上限(8个)，当前已连接: …。请先 disconnect 不需要的服务器]` 形态错误行

#### Scenario: 幂等复用
- **WHEN** 同名服务器已连接且存活时再次 connect
- **THEN** 直接返回既有工具数与清单，不新建进程、不重复注册工具

#### Scenario: 死连接重建
- **WHEN** 同名服务器存在但进程已死时 connect
- **THEN** 先注销旧工具并关闭旧进程，再建立新连接

#### Scenario: 退出兜底回收
- **WHEN** 程序经异常路径直接退出（未走会话结束清理）
- **THEN** 解释器退出阶段回收全部 MCP 子进程（首次成功连接后已注册兜底）

### Requirement: 失败不阻塞原则与错误文案

MCP 的任一操作失败 SHALL NOT 中断会话（不抛进程级异常）：connect/disconnect 的连接与协议类错误 SHALL 转为错误行 `MCP {action} 失败: {错误文本}`；其他意外异常转为错误行 `MCP {action} 异常: {错误文本}`；MCP 工具调用失败转为 `MCP工具 {服务器}/{工具} 调用失败: {错误文本}`。错误行 SHALL 采用带标签错误行机制（`[错误: …]` 附进程级随机标签，标签在交付 AI 前剥离），使终端的失败显示不可被服务端输出伪造。关闭与清理阶段的单个连接关闭失败 SHALL 静默忽略，不影响其它连接与主流程。

#### Scenario: 连接失败不阻塞
- **WHEN** connect 因配置或启动错误失败
- **THEN** AI 收到 `[错误: MCP connect 失败: …]` 形态文本，会话继续可用

#### Scenario: 意外异常包装
- **WHEN** 连接执行中抛出非协议类意外异常
- **THEN** 返回 `[错误: MCP {action} 异常: {异常文本}]` 形态文本

#### Scenario: 工具调用失败可继续
- **WHEN** 某 MCP 工具调用失败
- **THEN** 该调用返回错误行，其余工具与会话不受影响

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（避免用户/AI 可感知差异），若未来修正须作为独立变更处理：① connect 强制按「启用」解析配置（配置中的启用开关被忽略，传 `false` 也连接）；② 断开全部为逐个断开、批内不捕获单点错误（某服务器在批处理中被并发断开引发的错误会中断整批，已断开的生效、后续跳过）；③ 工具名与内置工具冲突时注册被跳过，但该连接登记的工具数仍按全量记录（登记数可能大于实际注册数，断开时按登记清单注销）；④ 并发同名 connect 的落败方丢弃本次新建的进程并返回既有清单（不报错）；⑤ 程序退出清理不注销已注册的 MCP 工具、不触发工具表热更新回调；⑥ `tools/list` 翻页达 50 页上限时静默截断（无提示）；⑦ 服务端通知与其它非协议输出仅写日志（不影响协议流）。

#### Scenario: 启用开关被忽略（兼容怪癖）
- **WHEN** connect 的 `config` 含 `"启用": false`
- **THEN** 仍按启用解析并连接（现状保持）

#### Scenario: 登记数与注册数不一致（兼容怪癖）
- **WHEN** 某 MCP 工具名清洗后与内置工具重名
- **THEN** 该工具注册被跳过，但成功提示中的计数仍包含它（断开时按登记清单注销，现状保持）

#### Scenario: 分页静默截断（兼容怪癖）
- **WHEN** 服务端持续返回 `nextCursor` 超过 50 页
- **THEN** 只收集前 50 页工具（无提示，现状保持）
