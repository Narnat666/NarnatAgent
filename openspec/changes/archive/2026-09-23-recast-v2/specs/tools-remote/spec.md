# Spec Delta

## Purpose

定义远程执行能力（`tools-remote`）的行为契约：Terminal 工具（多终端持久 SSH——会话连接与管理、命令执行、交互输入、sudo 密码回填、文件传输）与 Serial 工具（串口——扫描/连接/执行/裸监听/交互）。工具名、action 名、参数名、结果前缀、输出格式与错误文案是 LLM 与 UI 共同依赖的已发布契约，必须逐字保持；远程文件访问（文件工具的 devN 路径）亦复用本能力的会话与错误契约。

## ADDED Requirements

### Requirement: Terminal 工具参数契约

Terminal 工具 SHALL 以单一入口暴露参数 `action`/`host`/`username`/`port`/`password`/`command`/`input`/`timeout`/`max_output_chars`/`source_host`/`source_path`/`target_host`/`target_path`；`action` 取值集合为 `connect`/`exec`/`input`/`status`/`close`/`transfer`，缺省 `exec`。默认值 SHALL 为：`port` 22、`max_output_chars` 8000、`timeout` connect 15 秒 / exec 与 input 120 秒。数值参数（`port`/`timeout`/`session_id`/`max_output_chars`）SHALL 接受字符串数字并强转。工具定义 SHALL NOT 向 AI 暴露内部参数（`session_id` 可被接受但不列出；设备引用一律走 `host`）。

#### Scenario: 缺省 action
- **WHEN** 调用 Terminal 未指定 action
- **THEN** 按 `exec` 执行

#### Scenario: 未知 action
- **WHEN** `action` 取集合外的值
- **THEN** 返回错误 `未知action '{action}'，可选: connect/exec/input/status/close/transfer`

#### Scenario: 数值参数强转失败
- **WHEN** `port`/`timeout`/`max_output_chars` 之一为非数字字符串
- **THEN** 返回错误 `port/timeout/session_id/max_output_chars需为整数`

#### Scenario: 缺省超时分层
- **WHEN** `action=connect` 未传 `timeout`，或 `action=exec` 未传 `timeout`
- **THEN** 分别按 15 秒与 120 秒执行

### Requirement: SSH 连接建立与认证

`connect` SHALL 要求 `host` 与 `username` 非空。认证凭据 SHALL 按以下规则归一：`password` 形似路径（以 `~`/`/`/`\`/`.` 开头，或含路径分隔符）且本地该文件存在时按私钥使用，否则按登录密码使用；未提供 `password` 时 SHALL 允许尝试默认密钥与 agent。sudo 密码缺省取登录密码。连接超时 SHALL 为非正值时回落 15 秒，并受全局工具超时上限（配置 `工具.超时上限秒`，默认 1800，0 表示不限制）约束。连接成功后 SHALL 返回设备编号回执，后附初始登录输出（超过 4 行时折叠为首行 + `...(已省略{N}行登录横幅)` + 末两行；为空时改附合成提示符 `{username}@{host}:{路径}$`，其中用户主目录显示为 `~`）。相同设备重复连接时 SHALL 附提示，引导直接复用已有 dev 编号。

#### Scenario: 缺少必填参数
- **WHEN** `connect` 未提供 `host` 或 `username`
- **THEN** 返回错误 `connect需要提供host和username`

#### Scenario: 密码参数实为私钥路径
- **WHEN** `password` 为 `~/.ssh/id_rsa` 且该文件存在
- **THEN** 按私钥认证连接，该值不作为登录密码

#### Scenario: 认证失败提示
- **WHEN** 连接因认证失败终止
- **THEN** 返回错误 `认证失败({username}@{host})，请检查password`；未提供 `password` 时附「未提供password，大多数设备需要密码认证，请在password参数填登录密码后重试」，`password` 疑似私钥路径但本地文件不存在时附对应路径提示

#### Scenario: 连接成功回执
- **WHEN** 连接成功
- **THEN** 返回以 `[已连接 dev{N}(终端{M}): {username}@{host}]` 开头的文本

#### Scenario: 重复连接提示
- **WHEN** 相同 host 与 username 已有其他会话
- **THEN** 回执附 `[注意: 相同设备已有连接: …如非必要请勿重复连接，可直接用 host=dev{N} 执行命令]`

### Requirement: SSH 会话编号与容量

内部会话槽 SHALL 以 0 起连续编号，最大并发数由配置 `工具.SSH最大会话数` 驱动（默认 5），内部 SHALL 钳制到 1-10。AI 侧编号为 `dev0`（本机）与 `dev1..devn`（被控设备，终端编号 = N-1）。自动分配 SHALL 复用空闲槽并回收已断开的死会话槽位（回收时关闭原会话）；槽位用尽时报错并提示释放。指定槽位已存在存活会话时 SHALL 返回已连接信息而不新建连接。

#### Scenario: 槽位用尽
- **WHEN** 已达上限且 `connect` 未指定 `session_id`
- **THEN** 返回错误 `已达最大会话数({N})，当前已连接: {清单}，请先close释放`

#### Scenario: 死会话槽位回收
- **WHEN** 某槽位会话通道已关闭，随后 `connect` 自动分配
- **THEN** 复用该槽位编号（原会话被关闭）

#### Scenario: 指定已连接槽位
- **WHEN** `connect` 指定 `session_id` 且该槽位有存活会话
- **THEN** 返回 `[{devN(终端M)}已连接: {username}@{host}]` 与当前提示符，不新建连接

#### Scenario: 指定编号越界
- **WHEN** `connect` 指定 `session_id` ≥ 最大会话数
- **THEN** 返回错误 `session_id范围0-{最大会话数-1}`

### Requirement: Terminal 状态查询与关闭

`status` SHALL 返回以 `[SSH会话]` 开头的块：固定行 `dev0: 本机(当前设备)`，其后每个槽位一行——已连接为 `  devN(终端M): {username}@{host} [{活跃|已断开}|{忙|闲}] 目录:{当前目录}`，未连接为 `  devN(终端M): [未连接]`；无任何会话时追加 `(无已连接设备，最多支持{N}个并发终端)`。`close` SHALL 支持三种方式：未指定 `host` 与 `session_id` 关闭全部并返回 `[已关闭{N}个会话]`；指定 `session_id` 关闭对应槽位；指定 `host=devN` 关闭对应设备；`dev0` SHALL 返回 `[dev0是当前设备(本机)，无需关闭]`；目标未连接返回 `[{devN(终端M)}未连接]` / `[dev{N}未连接]`。程序退出时 SHALL 关闭全部会话并清空注册表。

#### Scenario: 状态块格式
- **WHEN** 存在一个活跃且空闲的会话
- **THEN** 状态块含 `  dev1(终端0): {username}@{host} [活跃|闲] 目录:{cwd}`

#### Scenario: 无会话状态
- **WHEN** 无任何会话时执行 `status`
- **THEN** 状态块末尾追加 `(无已连接设备，最多支持{N}个并发终端)`

#### Scenario: 关闭全部
- **WHEN** `close` 未指定 `host` 与 `session_id`
- **THEN** 全部会话关闭，返回 `[已关闭{N}个会话]`

#### Scenario: 关闭 dev0
- **WHEN** `close` 指定 `host=dev0`
- **THEN** 返回 `[dev0是当前设备(本机)，无需关闭]`

#### Scenario: 退出清理
- **WHEN** 程序退出
- **THEN** 所有 Terminal 与 Serial 会话被关闭，注册表清空

### Requirement: SSH 命令执行与完成检测

`exec` SHALL 要求 `command` 非空、`timeout` 为正整数，`timeout` SHALL 受全局工具超时上限截断。系统 SHALL 通过提示符形态判定命令完成：`user@host:path[$#]` 严格形态与 ≤32 字符的 `$`/`#` 结尾宽形态（busybox/裸 root）均为候选，候选出现后需连续两次读取静默（无新数据）才认定完成。用于采集退出码与工作目录的内部标记 SHALL 在命令完成后发送，且 SHALL NOT 出现在返回文本中。结果 SHALL 以 `[devN(终端M)] ` 前缀返回，正文为「退出码行 + 命令输出 + 当前提示符」（命令无输出时只有退出码行与提示符）。终端繁忙（有未完成命令）时 `exec` SHALL 拒绝，返回 `[上一个命令尚未完成，此终端暂不可用。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]` 与当前提示符。

#### Scenario: 正常完成
- **WHEN** `exec` 一条成功命令
- **THEN** 返回 `[dev1(终端0)] ` + 退出码行 + 命令输出 + 提示符 `{username}@{host}:{路径}$`

#### Scenario: 缺少命令
- **WHEN** `exec` 未提供 `command`
- **THEN** 返回错误 `exec需要提供command`

#### Scenario: 非法超时
- **WHEN** `exec` 传入的 `timeout` ≤ 0
- **THEN** 返回错误 `timeout需为正整数（秒）`

#### Scenario: 繁忙终端
- **WHEN** 目标终端有未完成命令时调用 `exec`
- **THEN** 返回繁忙提示与当前提示符，不发送命令

### Requirement: Terminal 输出清洗与截断

返回给 AI 的输出 SHALL 先清洗：剥离 ANSI 转义序列、按 `\r` 覆盖语义合并行内内容、清理内部标记、把 3 个及以上连续换行压为 2 个、仅去首尾空行与行尾空白（保留行首空格）。PTY 命令回显 SHALL 被剔除：含内部标记的回显整行丢弃（若同行粘连真实输出则保留前缀）、续行回显按续行提示特征丢弃、首个非空行与命令首行/输入文本精确相等时仅丢弃一次（不相似不删）。输出长度超过 `max_output_chars` 时 SHALL 保留首部约 2/3 与尾部约 1/3，中段插入 `...[中间截断: 输出共{N}字符, 已保留首{X}字符+尾{Y}字符(≈{T}token)。增大max_output_chars可获取完整输出]`，且截断点 SHALL NOT 切开框架标签；`max_output_chars` ≤ 0 时 SHALL 返回错误 `max_output_chars需为正整数`。

#### Scenario: 截断提示格式
- **WHEN** 结果长度超过 `max_output_chars`
- **THEN** 返回文本含上述中间截断提示（含总字符数、保留字符数与 ≈token 估算）

#### Scenario: 非法输出上限
- **WHEN** `max_output_chars` 为 0 或负数
- **THEN** 整个结果被替换为错误 `max_output_chars需为正整数`

#### Scenario: 短输入不误删真实输出
- **WHEN** `input` 内容为 `G`，真实输出首行为 `GOT:G`
- **THEN** 该真实输出行保留（仅精确相等的行才判为回显）

### Requirement: 退出码与框架错误标签

框架生成的退出码行 SHALL 为 `[exit code: {N}]` 附一个进程级随机标签；框架生成的错误行 SHALL 为 `[错误: {消息}]` 附一个进程级随机标签。Terminal 的参数校验、连接、执行、传输等失败 SHALL 一律使用该带标签错误行。标签 SHALL 在结果交给 AI 前被剥离（AI 看到 `[exit code: {N}]` / `[错误: {消息}]` 本体），UI 的失败显示 SHALL 仅依据标签判定——命令输出中恰好出现同款文字 SHALL NOT 触发失败显示。退出码行 SHALL 位于输出之前；无法解析退出码时不输出该行。截断 SHALL 保证标签完整（不被从中切开）。

#### Scenario: 非零退出码
- **WHEN** 命令以退出码 3 结束
- **THEN** 结果含 `[exit code: 3]`（AI 视角，标签已剥离）

#### Scenario: 命令输出伪造错误文本
- **WHEN** 命令自身输出打印了 `[错误: xxx]` 字样
- **THEN** UI 不显示工具失败（无框架标签）

#### Scenario: 框架错误仍被识别
- **WHEN** 连接失败等框架错误返回
- **THEN** UI 显示失败，AI 看到的文本为 `[错误: …]` 本体

### Requirement: 超时后行为与后台收敛

`exec` 超过 `timeout` 时 SHALL 不终止远程进程：返回已读输出 + `[超时: 命令执行超过{N}秒，仍在后台运行。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]` + 当前提示符，并把终端标记为忙。系统 SHALL 在后台等待该命令完成、缓存其完成输出并自动解除忙状态；后续 `exec`/`input` SHALL 先回传缓存，格式为 `[后台命令已完成，输出如下]` + 输出 + 30 个 `-` 组成的分隔线 + 本次结果（本次结果为框架错误时错误置顶、缓存附后，前缀行同为 `[后台命令已完成，输出如下]`）；等待输入期间的回传前缀为 `[输入前输出]`。检测到连接实际中断（读到 EOF/套接字异常，或 exec 路径零输出且 tty 有回显）时 SHALL NOT 走超时分支，SHALL 关闭通道并返回错误 `连接已中断（设备可能关机/重启或网络不通），命令结果未知` + 提示符，且不置忙。

#### Scenario: 超时返回与置忙
- **WHEN** 命令在 `timeout` 内未完成
- **THEN** 返回已收集输出 + 超时提示 + 提示符，会话状态转为忙

#### Scenario: 后台完成输出回传
- **WHEN** 超时命令随后完成，再次调用 `exec` 或 `input`
- **THEN** 结果以 `[后台命令已完成，输出如下]` 开头附缓存输出，后接分隔线与本次结果

#### Scenario: 连接中断判定
- **WHEN** 读取过程中连接中断
- **THEN** 返回错误 `连接已中断（设备可能关机/重启或网络不通），命令结果未知` 与提示符，终端不置忙

### Requirement: SSH 交互输入语义

`input` SHALL 要求内容非空。终端空闲时 SHALL 拒绝发送，返回 `[当前无命令等待输入，输入内容未发送（避免被当作命令执行）。如需执行命令请用 exec]`（有已完成的缓存输出时先附 `[后台命令已完成，输出如下]` 块）；后台命令在接管期间恰好完成时同样拒绝，文案为 `[命令已完成，输入内容未发送（避免被当作命令执行）。如需执行命令请用 exec]`。内容为 `^C` 或 `\x03` 时 SHALL 发送原始 Ctrl+C 字节（不追加换行）：shell 回到提示符时解除忙状态并返回（输出 +）`[已中断: 正在运行的命令已被 ^C 终止]` + 提示符；未回到提示符时保持忙、重启后台收敛，返回（输出 +）`[^C已发送但命令未终止，仍在后台运行。可稍后再试，或由用户按ESC中断]` + 提示符。其他内容 SHALL 追加换行发送，并等待原命令完成（此路径 SHALL NOT 以零输出判定断连）。结果前缀同 `exec`。

#### Scenario: 空闲拒绝
- **WHEN** 终端无未完成命令时调用 `input`
- **THEN** 内容不被发送，返回拒绝提示

#### Scenario: ^C 中断成功
- **WHEN** 命令在后台运行时 `input` 发送 `^C` 且 shell 回到提示符
- **THEN** 返回输出（如有）+ `[已中断: 正在运行的命令已被 ^C 终止]` + 提示符，终端转为空闲

#### Scenario: ^C 未生效
- **WHEN** `^C` 发送后 shell 未回到提示符
- **THEN** 返回 `[^C已发送但命令未终止，仍在后台运行。可稍后再试，或由用户按ESC中断]` + 提示符，终端保持忙

#### Scenario: 缺少输入内容
- **WHEN** `input` 未提供内容
- **THEN** 返回错误 `input需要提供input内容`

### Requirement: sudo 密码自动注入

`connect` 时确定的 sudo 密码（缺省等于登录密码）SHALL 在命令等待密码时自动注入，注入通过通道直接写入、不经 shell 命令行。触发 SHALL 同时满足三个条件：输出末尾（末 2048 字符清洗后）匹配密码提示、原始输出尾部无换行、1.5 秒宽容期内内部完成标记未到达；提示识别 SHALL 覆盖 `[sudo]...password`、`Password:`、`密码:`、`passphrase for key`（忽略大小写）。单次读循环内注入尝试上限 SHALL 为 3 次。已注入后再次出现提示时：命中拒绝文案（`sorry...try again`、`incorrect password`、`bad password`，忽略大小写）或达到尝试上限 SHALL 停用本会话自动注入，置忙并启动后台收敛，返回（输出 +）`[{原因}：sudo密码与登录密码不同。请向用户询问sudo密码，然后用input输入；本会话后续不再自动注入]`，原因为 `自动注入的登录密码被sudo拒绝` 或 `多次注入后仍在等待密码`；无拒绝文案时视为命令链中下一个 sudo，继续注入。未设置 sudo 密码或已停用时 SHALL 置忙并启动后台收敛，返回（输出 +）`[检测到密码提示，请用input action输入密码]`。同一份旧提示 SHALL NOT 被重复评估触发重复注入。

#### Scenario: 命令自身打印密码字样
- **WHEN** 命令输出以换行结尾的 `Password:` 文本
- **THEN** 不触发自动注入

#### Scenario: 注入被拒
- **WHEN** 注入后命令再出密码提示且命中拒绝文案
- **THEN** 返回 `[自动注入的登录密码被sudo拒绝：sudo密码与登录密码不同。请向用户询问sudo密码，然后用input输入；本会话后续不再自动注入]`，本会话不再自动注入

#### Scenario: 无密码可注入
- **WHEN** 出现密码提示但会话未设置 sudo 密码
- **THEN** 返回 `[检测到密码提示，请用input action输入密码]`，终端置忙等待 `input`

#### Scenario: 多次注入仍失败
- **WHEN** 注入达到 3 次后提示仍在
- **THEN** 返回 `[多次注入后仍在等待密码：sudo密码与登录密码不同。…本会话后续不再自动注入]`

### Requirement: 断线自动重连

通道断开时 `exec`/`input` SHALL 自动尝试一次重连：复用原凭据，保留设备编号，恢复原工作目录（原目录为 `~` 或 `/` 时跳过，恢复命令失败被忽略）。并发发现断线时 SHALL 串行互斥，且已恢复的连接直接复用。重连成功时本次调用 SHALL 返回错误 `{devN(终端M)}连接曾中断，已自动重连，请重试命令`；重连失败返回错误 `{devN(终端M)}连接中断，自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连`，会话与凭据 SHALL 保留（不丢设备编号）。重连期间用户按 ESC SHALL 放弃本次重连（按失败处理）并清除中断标志，避免后续命令被误判为用户中断。重连后忙标志、待完成标记、后台缓存与后台收敛线程 SHALL 复位。

#### Scenario: 操作中断线后重连成功
- **WHEN** 执行中连接中断且自动重连成功
- **THEN** 返回 `…连接曾中断，已自动重连，请重试命令` 错误，设备编号与工作目录保留

#### Scenario: 重连失败保留会话
- **WHEN** 自动重连失败
- **THEN** 返回 `…自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连` 错误，会话不丢弃

#### Scenario: ESC 打断重连
- **WHEN** 重连过程中用户按 ESC
- **THEN** 本次重连被放弃并按失败处理，中断标志清除

#### Scenario: 入口即发现断线
- **WHEN** `exec`/`input` 进入时通道已关闭
- **THEN** 先自动重连，失败时返回重连失败错误

### Requirement: 文件传输

`transfer` SHALL 支持本机 → 被控设备、被控设备 → 本机、被控设备 → 被控设备三种路径；远程到远程 SHALL 以固定块流式中转（文件不落本机磁盘），中断时返回错误 `传输中断，已传输 {X}/{Y}: {e}`。`source_path`/`target_path` SHALL 必填；设备引用 SHALL 为合法 `devN`（空/`dev0` = 本机）。传输前 SHALL 校验大小上限（配置 `工具.最大传输文件MB`，默认 100，0 或负值表示不限制），超限返回错误 `文件大小 {X} 超过传输上限 {Y}`（大小按 1024 进制、保留 1 位小数的 B/KB/MB/GB 格式化）。目录 SHALL 被拒绝并给出打包指引（本机源提示用 Shell 打包、被控设备源提示用 exec 打包）。本机 → 远程 SHALL 自动逐级创建远程父目录（失败报 `无法创建远程目标目录: {父目录}（可能无写权限）`），远程 → 本机 SHALL 自动创建本地父目录。成功回执 SHALL 为 `[已传输: {源}:{源路径} → {目标}:{目标路径} ({大小})]`（本机一侧显示 `本机`，被控设备一侧显示 `devN`）。两侧都为本机时 SHALL 报错引导使用本地文件工具；源与目标完全相同时 SHALL 报错 `源和目标相同，无需传输`；设备未连接时 SHALL 报错并附当前已连接设备清单。

#### Scenario: 双本机
- **WHEN** `source_host` 与 `target_host` 均为本机（空/`dev0`）
- **THEN** 返回错误 `源和目标都是本机(dev0)，请使用本地文件操作工具`

#### Scenario: 源目标相同
- **WHEN** 两端设备与路径完全相同
- **THEN** 返回错误 `源和目标相同，无需传输`

#### Scenario: 目录被拒
- **WHEN** 传输源是目录
- **THEN** 返回错误，提示先用 Shell 或 exec 打包为单个文件再传输

#### Scenario: 超过大小上限
- **WHEN** 源文件大小超过上限
- **THEN** 返回错误 `文件大小 {X} 超过传输上限 {Y}`

#### Scenario: 传输成功回执
- **WHEN** 本机文件传到 `dev1`
- **THEN** 返回 `[已传输: 本机:{源路径} → dev1:{目标路径} ({大小})]`

#### Scenario: 缺少路径参数
- **WHEN** 未提供 `source_path` 或 `target_path`
- **THEN** 分别返回错误 `transfer需要提供source_path（源文件路径）` / `transfer需要提供target_path（目标文件路径）`

### Requirement: Serial 工具参数契约

Serial 工具 SHALL 以单一入口暴露参数 `action`/`port`/`baudrate`/`databits`/`parity`/`stopbits`/`flow_control`/`line_ending`/`prompt_pattern`/`command`/`input`/`timeout`/`session_id`/`max_output_chars`；`action` 取值集合为 `scan`/`connect`/`exec`/`raw_exec`/`input`/`status`/`close`，缺省 `exec`。默认值 SHALL 为：`baudrate` 115200、`databits` 8、`parity` `N`、`stopbits` 1、`flow_control` `none`、`line_ending` `\n`（接受转义字符串 `\n`/`\r\n`/`\r` 并还原，其他值回落 `\n`）、`timeout` 120、`max_output_chars` 8000。数值参数 SHALL 接受字符串数字并强转。`timeout` SHALL 受全局工具超时上限截断。

#### Scenario: 缺省 action
- **WHEN** 未指定 `action`
- **THEN** 按 `exec` 执行

#### Scenario: 转义行结束符
- **WHEN** `connect` 传 `line_ending` 为字符串 `\r\n`
- **THEN** 实际以 CRLF 作为行结束符

#### Scenario: 非法行结束符回落
- **WHEN** `line_ending` 为其他值
- **THEN** 回落为 `\n`

#### Scenario: 数值参数非法
- **WHEN** `baudrate`/`databits`/`stopbits`/`timeout`/`session_id`/`max_output_chars` 之一为非数字
- **THEN** 返回 `[错误: baudrate/databits/stopbits/timeout/session_id/max_output_chars需为数值]`

#### Scenario: 未知 action
- **WHEN** `action` 取集合外的值
- **THEN** 返回 `[错误: 未知action '{action}'，可选: scan/connect/exec/raw_exec/input/status/close]`

### Requirement: Serial 扫描

`scan` SHALL 列出本机可用串口：无设备返回 `[未检测到串口设备]`；有设备时首行 `可用串口:`，其后每行 `  {设备名}  — {描述}  [{硬件ID}]`，其中描述为 `n/a` 或与设备名相同则省略、缺失显示 `(无描述)`，硬件ID 为空或 `n/a` 则省略。pyserial 不可用 SHALL 返回 `[错误: 无法导入 pyserial，请确认已安装]`，扫描异常返回 `[错误: 扫描串口失败: {e}]`。

#### Scenario: 未检测到串口
- **WHEN** 系统无可用串口
- **THEN** 返回 `[未检测到串口设备]`

#### Scenario: 列表行格式
- **WHEN** 检测到 `COM3`（描述 `USB-SERIAL CH340`）
- **THEN** 输出行形如 `  COM3  — USB-SERIAL CH340  [{硬件ID}]`

### Requirement: Serial 连接与会话编号

`connect` SHALL 要求 `port` 非空。同一端口 SHALL 不允许并存两个存活会话。会话编号 SHALL 为 0..最大会话数-1（上限默认 5，内部钳制 1-10）；显式编号越界报 `[错误: session_id 范围 0-{N-1}]`，编号已有存活会话报 `[串口终端{sid}已连接: {port} @{baudrate}]`；自动分配 SHALL 跳过连接中的占位槽并回收已断开的死会话槽位，用尽时报 `[错误: 已达最大会话数({N})，当前终端: {清单}，请先 close 释放]`。连接过程 SHALL 先占位再于锁外打开串口（失败时释放占位），避免长时间持锁。成功回执 SHALL 为 `[已连接终端{sid}: {port} @{baudrate}]`，后附初始输出（连续 0.5 秒静默或最长 10 秒内收到的设备输出）。端口名在 Windows SHALL 大小写不敏感。打开失败返回 `[错误: 无法打开串口 {port}: {e}]`；`prompt_pattern` 非法正则返回错误含 `无效的 prompt_pattern 正则: {e}`。

#### Scenario: 缺少端口
- **WHEN** `connect` 未提供 `port`
- **THEN** 返回 `[错误: connect 需要提供 port（串口设备名）]`

#### Scenario: 端口已被占用
- **WHEN** 对已有存活会话的端口再次 `connect`
- **THEN** 返回 `[错误: {port} 已被终端{sid}占用，请先 close 终端{sid}]`

#### Scenario: 会话数用尽
- **WHEN** 已达上限且未指定 `session_id`
- **THEN** 返回 `[错误: 已达最大会话数({N})，当前终端: {清单}，请先 close 释放]`

#### Scenario: 连接成功回执
- **WHEN** 打开 `COM3` @115200 成功
- **THEN** 返回 `[已连接终端0: COM3 @115200]`（有初始输出时附后）

#### Scenario: Windows 端口名大小写
- **WHEN** Windows 上以 `com3` 引用已连接的 `COM3`
- **THEN** 视为同一端口

### Requirement: Serial 命令执行与提示符检测

`exec` SHALL 以「提示符检测 + 稳定性采样」判定完成：默认提示符为行尾的 `[\])$#%>:❯=@~]` 字符（后可有空白），自定义 `prompt_pattern` SHALL 完全替换该默认；发出后至少 0.5 秒才允许命中提示符，命中后需连续采样确认输出稳定（连续 3 次、间隔 0.1 秒）。结果 SHALL 以 `[终端{sid}] ` 前缀返回：命中提示符时返回清洗后的输出；未命中时返回（已收集输出 +）`[超时: 命令执行超过{N}秒]`，被用户中断时为 `[用户中断]`。超时后设备上的命令仍在运行，可用 `input` 发送 `^C` 中断；会话 SHALL NOT 保留后台收敛机制（超时即复位忙状态，命令的迟到输出在下次调用前被排空丢弃）。命令发送前 SHALL 清空缓冲，避免上一条命令的残留输出混入。`exec` SHALL 要求 `command` 非空、`timeout` 为正整数；会话已有未完成命令时 SHALL 返回 `[上一个命令尚未完成，此串口暂不可用]`；串口已断开时 SHALL 返回 `[错误: 串口 {port} 已断开]` 或 `[错误: 终端{sid}串口已断开，请重新 connect]` 并回收槽位；写入失败 SHALL 返回 `[错误: 串口写入失败: {e}]` 并标记会话断开。

#### Scenario: 正常完成
- **WHEN** 命令输出后设备回到提示符且输出稳定
- **THEN** 返回 `[终端0] ` + 清洗后的输出（不含命令回显）

#### Scenario: 超时返回
- **WHEN** 命令在 `timeout` 内未完成
- **THEN** 返回（已收集输出 +）`[超时: 命令执行超过{N}秒]`

#### Scenario: 会话繁忙
- **WHEN** 上一命令未完成时再次 `exec`
- **THEN** 返回 `[上一个命令尚未完成，此串口暂不可用]`

#### Scenario: 设备断开
- **WHEN** 会话已断开（如串口被拔出）时调用 `exec`
- **THEN** 返回 `[错误: 终端{sid}串口已断开，请重新 connect]` 并释放槽位

#### Scenario: 缺少命令
- **WHEN** `exec` 未提供 `command`
- **THEN** 返回 `[错误: exec 需要提供 command]`

### Requirement: Serial 裸执行与纯监听

`raw_exec` SHALL NOT 做提示符检测，按纯超时收集输出：`command` 非空时发送（追加行结束符）并等待 `timeout` 秒，返回（已收集输出 +）标签——正常超时 `[超时: 命令执行超过{N}秒]`、被中断 `[用户中断]`；`command` 为空时进入纯监听：不发送任何内容，仅收集设备主动输出，标签为 `[监听结束: 已达{N}秒]`（有输出）或 `[监听结束: {N}秒内无输出]`（无输出）。`raw_exec` SHALL 要求 `timeout` 为正整数；`command` 为空时 SHALL 跳过删除命令安全确认；其余校验、解析与断开处理与 `exec` 相同，结果前缀同样为 `[终端{sid}] `。

#### Scenario: 裸执行超时
- **WHEN** `raw_exec` 发送命令并等待超时
- **THEN** 返回 `[终端0] {输出}\n[超时: 命令执行超过{N}秒]`

#### Scenario: 纯监听有输出
- **WHEN** 不传 `command` 且窗口内设备有输出
- **THEN** 返回 `[终端0] {输出}\n[监听结束: 已达{N}秒]`

#### Scenario: 纯监听无输出
- **WHEN** 不传 `command` 且窗口内设备无输出
- **THEN** 返回 `[终端0] [监听结束: {N}秒内无输出]`

#### Scenario: 非法超时
- **WHEN** `raw_exec` 的 `timeout` ≤ 0
- **THEN** 返回 `[错误: timeout 需为正整数（秒）]`

### Requirement: Serial 交互输入

`input` SHALL 要求内容非空（否则 `[错误: input 需要提供 input 内容]`）。内容为 `^C` 或 `\x03` 时 SHALL 发送原始 Ctrl+C 字节（不追加行结束符）并等待提示符：命中提示符返回清洗后的输出，未命中返回（输出 +）`[已发送Ctrl+C]`。其他内容 SHALL 追加行结束符发送，其判定与返回与 `exec` 相同（即会话空闲时发送等同执行命令）。`input` SHALL 经过删除命令安全确认。

#### Scenario: 发送交互文本
- **WHEN** 设备提示等待输入时 `input` 发送文本
- **THEN** 文本追加行结束符发送，等待提示符或超时

#### Scenario: 空闲时发送
- **WHEN** 会话空闲时发送普通文本
- **THEN** 按命令发送并等待结果（与 Terminal 的空闲拒绝语义不同）

#### Scenario: ^C 中断
- **WHEN** `input` 发送 `^C`
- **THEN** 发送原始 Ctrl+C 且不追加行结束符；未回到提示符时返回 `[已发送Ctrl+C]`

#### Scenario: 缺少输入内容
- **WHEN** `input` 未提供内容
- **THEN** 返回 `[错误: input 需要提供 input 内容]`

### Requirement: Serial 输出清洗与截断

Serial 返回文本 SHALL 清洗：剥离 ANSI 转义序列、把 `\r\n` 归一为 `\n` 后再按 `\r` 覆盖合并行内内容、压缩 3 个及以上连续换行为 1 个空行、去除首尾空白。命令回显 SHALL 按相似度剥离首行（与所发文本相似度 ≥0.6，或长度 >20 的命令前缀 20 字符匹配），不相似时保留首行（避免设备不回显时丢真实输出）。相邻重复且末行匹配提示符的行 SHALL 去重一行。会话缓冲超过约 1,000,000 字符时 SHALL 丢弃最早一半并前置 `...[背压截断: 丢弃前{N}字符]`（属静默数据丢失）。输出超过 `max_output_chars` 时 SHALL 保留首 2/3 与尾 1/3，中段插入 `...[中间截断: 输出共{N}字符, 已保留首{X}字符+尾{Y}字符。增大max_output_chars可获取完整输出]`；`max_output_chars` ≤ 0 时 SHALL 返回 `[错误: max_output_chars需为正整数]`。

#### Scenario: 回显剥离
- **WHEN** 设备回显了刚发送的命令
- **THEN** 结果首行不含该回显

#### Scenario: 不回显设备保留首行
- **WHEN** 设备不回显且首行是真实输出
- **THEN** 首行保留

#### Scenario: 截断提示
- **WHEN** 输出超过 `max_output_chars`
- **THEN** 结果含 Serial 版中间截断提示（不含 token 估算）

#### Scenario: 背压截断
- **WHEN** 设备持续刷屏导致缓冲超限
- **THEN** 缓冲内容前置 `...[背压截断: 丢弃前{N}字符]`

### Requirement: Serial 状态、关闭与清理

`status` SHALL 在无会话时返回 `[无活跃串口会话，最多支持{N}个并发终端]`；有会话时返回 `[串口会话]` 与每个已分配编号一行 `  终端{sid}: {port} @{baudrate} [{活跃|已断开}|{忙|闲}]`（连接中的占位显示 `  终端{sid}: [连接中...]`），存在空闲槽位时追加 `  [{空闲数}个空闲]`。`close` SHALL 支持：未指定 `session_id` 与 `port` 关闭全部（含取消占位）并返回 `[已关闭{N}个串口会话]`；仅指定 `port` 按端口名匹配（大小写不敏感；未匹配返回 `[错误: 端口 {port} 未连接]`）；指定 `session_id` 关闭单个（未连接返回 `[终端{sid}未连接]`，占位返回 `[终端{sid}连接中，已取消]`，成功返回 `[已关闭终端{sid}]`）。`exec`/`raw_exec`/`input` 的会话解析 SHALL 支持显式 `session_id`、按 `port` 匹配、单会话自动选择三种路径；报错文案分别为 `终端{sid}未连接，请先 connect`、`终端{sid}正在连接中，请稍候`、`端口 {port} 未连接，请先 connect 或 status 查看已连接的串口`、`无活跃会话，请先 connect`、`有{N}个会话，请指定 session_id 或 port（串口设备名，如COM1、/dev/ttyUSB0）。` 后附各会话摘要。程序退出 SHALL 关闭全部会话、清空注册表与活跃执行集合。

#### Scenario: 无会话状态
- **WHEN** 无任何串口会话时执行 `status`
- **THEN** 返回 `[无活跃串口会话，最多支持{N}个并发终端]`

#### Scenario: 状态行格式
- **WHEN** 存在一个活跃空闲会话
- **THEN** 返回块含 `  终端0: COM3 @115200 [活跃|闲]`

#### Scenario: 关闭全部
- **WHEN** `close` 未指定 `session_id` 与 `port`
- **THEN** 返回 `[已关闭{N}个串口会话]` 并释放全部槽位

#### Scenario: 按端口关闭
- **WHEN** `close` 传入已连接的端口名
- **THEN** 返回 `[已关闭终端{sid}]`

#### Scenario: 解析多会话歧义
- **WHEN** 存在多个会话且未指定 `session_id` 与 `port`
- **THEN** 返回 `[错误: 有{N}个会话，请指定 session_id 或 port…]` 与各会话摘要

### Requirement: 中断传播（ESC）

用户按 ESC SHALL 立即向正在执行远程命令的会话发送 Ctrl+C 并置位中断标志：Terminal SHALL 覆盖当前活跃执行的会话以及所有处于忙状态的会话（含超时后仍在后台运行者）；Serial SHALL 覆盖所有活跃执行中的会话。被中断的命令 SHALL 以 `[用户中断]` 标签（Terminal 侧还附提示符与已解析的退出码行）返回，所涉会话 SHALL 回到空闲状态。中断期间哨兵已出现时 Terminal SHALL 走正常解析并保留退出码与目录更新；已完成但未被读取的后台输出 SHALL 不因中断而丢失。

#### Scenario: 繁忙会话被 ESC 打断
- **WHEN** 某会话处于忙状态且用户按 ESC
- **THEN** 该会话收到 Ctrl+C 与中断标志，命令终止后恢复空闲

#### Scenario: 中断结果标签
- **WHEN** 命令被 ESC 中断
- **THEN** 返回文本含 `[用户中断]`

#### Scenario: 中断后状态复位
- **WHEN** 中断完成
- **THEN** 会话的忙标志、待完成标记复位，后续调用可正常使用

### Requirement: 设备引用约定（devN）

设备引用 SHALL 统一为 `devN`（大小写不敏感）：`dev0` 与缺省表示本机，`dev1..devn` 表示被控设备（对应终端编号 N-1）。`exec`/`input` 未指定设备时 SHALL 在唯一会话下自动选择；无会话报错误 `无活跃会话，请先connect`；多会话报错误 `有多个会话，请指定host=dev编号，当前已连接: {清单}`；`host` SHALL 也接受 IP 或 `user@IP` 宽松引用（唯一命中即用，多命中要求改用 dev 编号并附候选）。`dev0` 用于 `exec`/`input` SHALL 报错误 `dev0是本机，无SSH会话。本机执行命令请用 Shell 工具（Terminal 的 exec 用于SSH远程设备，dev0仅用于transfer）`；`devN` 未连接报错误 `dev{N}未连接，请先connect。当前已连接: {清单}`；非法标识 SHALL 附指导语 `设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备；本机执行命令请用Shell工具)` 与已连接清单（无设备时追加 `当前无已连接设备，请先connect`）。已连接设备清单 SHALL 形如 `dev1(user@host)`（多个以 `、` 连接，无设备时为 `(无)`）。UI 摘要显示 SHALL 把设备引用翻译为可读名：本机 → 本机主机名，已连接的 `devN` → 该设备 IP，未连接或非 devN → 原样回显。文件工具（Read/Edit/Write）的 `device` 参数 SHALL 由同一约定归一：合法值规范化为本机空值或 `devN`，非法值报错误，指导语为 `设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)` 附已连接清单（无设备时追加 `请先Terminal connect`）。

#### Scenario: 唯一会话自动选择
- **WHEN** 仅有唯一会话时 `exec` 未指定 `host`
- **THEN** 自动使用该会话

#### Scenario: 多会话要求显式指定
- **WHEN** 存在多个会话且 `exec` 未指定 `host`
- **THEN** 返回错误 `有多个会话，请指定host=dev编号，当前已连接: {清单}`

#### Scenario: dev0 不能执行命令
- **WHEN** `exec` 指定 `host=dev0`
- **THEN** 返回错误 `dev0是本机，无SSH会话。本机执行命令请用 Shell 工具（Terminal 的 exec 用于SSH远程设备，dev0仅用于transfer）`

#### Scenario: IP 宽松引用
- **WHEN** `exec` 指定 `host` 为某已连接设备的 IP
- **THEN** 命中该会话执行命令（多台设备 IP 相同引用时报错要求用 dev 编号区分）

#### Scenario: UI 摘要设备名
- **WHEN** 工具调用参数为 `host=dev1` 且该设备已连接
- **THEN** UI 摘要显示该设备 IP 而非 `dev1`

### Requirement: 远程文件访问（Read/Edit/Write 的 devN 路径）

文件工具访问被控设备（`device=dev1..devn`）时 SHALL 复用 Terminal 会话：无任何会话返回 `[错误: 无可用SSH会话，请先Terminal connect建立连接]`；指定设备未连接返回 `[错误: 指定设备 {host} 未连接。设备标识只支持devN编号(dev1..devn)。当前已连接: {清单}]`；会话断线时自动重连，重连失败视为无会话。远程读取 SHALL 与本地 Read 对齐：`limit` ≤ 0 报 `[错误: limit需为正整数]`；以二进制模式按行读取（不整体载入内存）；仅读首 8KB 做二进制检测，含 NUL 字节报 `[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请用 Terminal exec 处理]`；路径是目录报 `[错误: 远程路径是目录: {路径}，请用 Terminal exec 查看目录内容]`；按探测到的编码解码（替换非法字节）；行格式 `  {行号}→{内容}`；`offset` ≤ 0 从头开始，超出末尾报 `[无内容: offset={offset} 已超出文件末尾（文件共{N}行）]`，空文件报 `[文件为空]`；截断时追加 `  ... [截断: 已显示 {limit} 行。使用 offset={下一起始行号} 参数可读取其余部分]`；错误分类 SHALL 区分不存在（`[错误: 远程文件不存在: {路径}]`）与权限不足（`[错误: 远程文件权限不足（EACCES）: {路径}]`），其余为 `[错误: 远程读取失败: …]`。远程写入 SHALL 在旧内容可读时生成 unified diff 并返回（AI 视角）`[{host}] [已写入(远程): {路径} ({N}字节)]` + diff；新旧内容字节级完全相同时返回 `[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]`，正文相同但字节有差异时返回 `[提示: 文件已写入，正文内容相同，但字节层面有变化（{差异描述}）]`；目标为目录报 `[错误: 远程路径是目录: {路径}，请使用正确的文件路径]`；新文件为绝对路径时 SHALL 自动逐级创建父目录，失败报 `[错误: 无法创建远程目标目录: {父目录}（可能无写权限）]`；写入失败报 `[错误: 远程写入失败: {e}]`。远程编辑 SHALL 拒绝非 UTF-8 文件（`[错误: 远程文件非UTF-8编码，为防止内容损坏已拒绝编辑: {路径}。请用Terminal exec处理（如转码为UTF-8后再编辑）]`），`old_string` 为空报 `[错误: old_string不能为空]`；按文件换行风格归一化 `old_string`/`new_string`（CRLF 文件把 `\n` 归一为 CRLF，否则归一为 LF）后匹配：0 处报 `[错误: 未找到匹配文本。请先Read确认远程文件内容。]`，多处且未开启 `replace_all` 报 `[错误: 找到{N}处匹配，old_string不唯一。请扩大上下文使其唯一，或设置replace_all=True]`；成功写回后返回 `[{host}] [已替换{N}处]` + diff，内容无实质变化时返回 `[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]`；diff 无差异时显示 `[无差异]`，diff 头为 `a/{文件名}` 与 `b/{文件名}`。

#### Scenario: 无会话
- **WHEN** 无任何 SSH 会话时以 `device=dev1` 调用 Read
- **THEN** 返回 `[错误: 无可用SSH会话，请先Terminal connect建立连接]`

#### Scenario: 目录路径
- **WHEN** 读取路径是远程目录
- **THEN** 返回 `[错误: 远程路径是目录: {路径}，请用 Terminal exec 查看目录内容]`

#### Scenario: 权限不足与不存在区分
- **WHEN** 远程文件存在但无读权限
- **THEN** 返回含 `EACCES` 的权限错误而非「不存在」

#### Scenario: 非 UTF-8 文件拒绝编辑
- **WHEN** 远程文件为 GBK 编码时调用 Edit
- **THEN** 拒绝编辑并提示用 Terminal exec 转码

#### Scenario: 行号格式与截断提示
- **WHEN** 读取多行文本文件
- **THEN** 每行形如 `  {行号}→{内容}`，截断时附续读 `offset` 提示

### Requirement: 删除命令安全确认

`exec`/`input`（Terminal 与 Serial）以及 `command` 非空的 `raw_exec` SHALL 在安全开关关闭时对删除类命令做确认：匹配删除动词（`rm`/`del`/`rd`/`rmdir`/`erase`/`format`，后接空白或 `/`，或 `Remove-Item`，忽略大小写）；Terminal 另外 SHALL 对含 `git` 的命令做确认（Serial 无此项）。Windows 平台 SHALL 通过确认回调同步询问，用户拒绝返回 `[操作已取消: 此命令需用户确认]` 且不执行；其他平台 SHALL 暂存待确认参数（工具名与完整参数）并返回字面量 `__AWAIT_CONFIRM__`，由主循环在提示符下确认后重新执行，用户已确认的一次调用 SHALL 直接放行。无工具上下文（如 headless）时 SHALL 直接放行。

#### Scenario: Windows 用户拒绝
- **WHEN** Windows 上删除命令确认被拒
- **THEN** 返回 `[操作已取消: 此命令需用户确认]`，命令不执行

#### Scenario: 非 Windows 待确认
- **WHEN** Linux/macOS 上执行删除命令且未经确认
- **THEN** 返回 `__AWAIT_CONFIRM__` 并暂存参数

#### Scenario: 已确认放行
- **WHEN** 用户已确认删除命令
- **THEN** 本次调用直接执行，不再询问

#### Scenario: 各工具确认范围差异
- **WHEN** Serial 收到含 `git` 的命令
- **THEN** 不触发确认；Terminal 收到同样命令则触发（`git` 出现在任意位置即命中）

### Requirement: Serial 会话上限配置接线（修复项）

现状 Serial 的最大会话数硬编码为 5，配置 `工具.SSH最大会话数` 仅作用于 Terminal。本次重构 SHALL 将 Serial 接上同一配置（默认 5，内部钳制 1-10）——此为 proposal 列明的修复项，须独立可见；除上限来源外，Serial 的编号、占位、用尽报错与状态文案行为 SHALL 保持不变。

#### Scenario: 配置生效
- **WHEN** 配置 `工具.SSH最大会话数` 为 2
- **THEN** Terminal 与 Serial 的最大会话数均为 2，第 3 个 `connect` 报满员错误

#### Scenario: 钳制范围
- **WHEN** 配置值为 20 或 0
- **THEN** 实际生效上限分别为 10 与 1

#### Scenario: 其余行为不变
- **WHEN** 未配置该项（默认 5）
- **THEN** Terminal 与 Serial 上限均为 5，其余会话行为与现状一致

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（用户可感知差异视为回归），若未来修正须作为独立变更处理：Terminal 的 ESC 打断目标为单一「活跃执行会话」引用（多会话并发执行时后者覆盖前者，ESC 仅作用于最近发起者；Serial 已用集合语义，无此覆盖）；`__AWAIT_CONFIRM__` 返回值在 Terminal 侧为字面量、在 Serial 侧为共享常量（对外值相同，重构统一为常量即等价）；Serial 会话摘要串在 `port` 与 `@` 之间有一个空格（`{port} @{baudrate}`）；Serial 非法串口参数静默回落默认值（`databits` 非 5/6/7/8 → 8；`parity` 非 N/E/O/M/S → N；`stopbits` 非 1/1.5/2 → 1），无任何提示，`flow_control` 非 `software`/`hardware`（含 `none` 与非法值）一律按无流控；Serial 超时后不保留后台收敛、迟到输出在下次调用前被丢弃，其繁忙文案不含 `input` 指导（与 SSH 侧不对称）；两侧输出解码固定按 UTF-8 加替换字符处理（无编码探测，非 UTF-8 设备输出可能呈现替换字符）；Terminal 会话层对 `timeout` ≤ 0 视作无限等待（工具层已校验，仅兜底路径）。

#### Scenario: ESC 打断覆盖范围（怪癖）
- **WHEN** 两个 Terminal 会话先后发起执行且用户按 ESC
- **THEN** 中断作用于最近注册的活跃执行会话（现状保持）

#### Scenario: 非法串口参数静默回落（怪癖）
- **WHEN** `connect` 传 `parity` 为 `X`
- **THEN** 实际按 `N` 打开串口，不提示错误

#### Scenario: Serial 超时不收敛（怪癖）
- **WHEN** Serial 命令超时
- **THEN** 返回超时标签且忙状态立即复位（不缓存后台完成输出）

#### Scenario: 非 UTF-8 输出呈现替换字符（怪癖）
- **WHEN** 设备以 GBK 输出中文
- **THEN** 返回文本中相应字符为替换字符（现状保持）
