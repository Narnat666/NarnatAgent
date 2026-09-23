# Spec Delta

## Purpose

定义 Shell 与后台任务能力的行为契约：本地命令执行（平台自适应、参数归一化、安全确认门禁、cd 持久化、多段执行、python 载荷直执行、输出解码与截断）、后台任务机制（固定槽位、提交/状态/等待/取消、结果文件生命周期与会话隔离）以及框架退出码/错误标签协议。工具名、参数名、输出文案、结果文件命名与路径是 LLM 与宿主共同依赖的对外契约，必须精确保持。

## ADDED Requirements

### Requirement: Shell 工具定义与平台描述

系统 SHALL 以工具名 `Shell` 提供给 LLM，描述文本按当前平台渲染平台标签：Windows 为 `Windows(cmd)`，其余平台为 `Linux/macOS(bash)`；描述 SHALL 为「本地Shell — 在{平台标签}执行命令。前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。」。工具参数 SHALL 为 `command`/`timeout`/`max_output_chars`/`background`/`bg`/`id`，且 `required` SHALL 为空数组（无必填参数）。参数说明 SHALL 至少覆盖：command 在前台执行或 `background=true` 提交时必填、`bg=status/wait/cancel` 时可省略；timeout 前台为超时秒数（默认 120，超时后命令被终止）、`bg=wait` 时为最长等待秒数；max_output_chars 仅前台生效（正整数，默认 4000）；background 为立即返回 `bgN` 编号的不阻塞后台执行（结果写入会话专属临时目录的 `bgN.log`、编号与文件一一对应、用 Read/Grep 读取、输出随任务分块落盘、并发上限 8 个 `bg1~bg8`、终态槽位自动释放复用、复用前旧结果归档为 `bgN.log.N.prev` 仍可读、会话结束自动清理）；bg 为 `status`（编号/状态/退出码/输出大小/结果路径）/`wait`（有完成立即返回，超时返回最新快照）/`cancel`（杀进程树，已产出内容保留可读）三操作；id 为 `bg=cancel` 必填的任务编号。

#### Scenario: 平台标签渲染
- **WHEN** 系统运行于 Windows
- **THEN** 工具描述首句为「本地Shell — 在Windows(cmd)执行命令。前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。」
- **AND** 在 Linux/macOS 上同一句中的平台标签渲染为 `Linux/macOS(bash)`

#### Scenario: 无必填参数
- **WHEN** LLM 仅传 `bg="status"` 调用 Shell（不传 command）
- **THEN** 调用被接受，进入后台状态查询分发，而非参数校验失败

### Requirement: 参数契约与归一化

系统 SHALL 按固定顺序处理调用：数值归一化 → 安全确认 → 后台分发 → 前台参数校验 → 超时上限钳制。数值归一化 SHALL 接受字符串形式的数字（如 `"30"`）；`timeout` 缺省（None）时取 120、`max_output_chars` 缺省时取 4000；`max_output_tokens` SHALL 作为 `max_output_chars` 的字符数语义别名被接受，两者同传时以 `max_output_tokens` 为准并覆盖。归一化失败（非数字，含 `max_output_tokens` 的非法值）SHALL 返回 `[错误: timeout/max_output_chars需为整数]` 且不执行命令。前台执行时 `command` 为空 SHALL 返回 `[错误: command为空：前台执行需提供 command；后台任务用 background=true 提交，管理用 bg=status/wait/cancel]`；`timeout <= 0` SHALL 返回 `[错误: timeout需为正整数（秒）]`。宿主配置的工具超时上限在大于 0 时 SHALL 对前台 timeout 与 `bg=wait` 的等待秒数取较小值钳制。`max_output_chars` 为 0 或负数的处理见「兼容性怪癖保持」。

#### Scenario: 字符串数字容错
- **WHEN** LLM 传 `timeout="30"`、`max_output_chars="9999"`
- **THEN** 归一化为整数 30 与 9999 后正常执行

#### Scenario: 别名覆盖
- **WHEN** 同一次调用同时传 `max_output_chars=4000` 与 `max_output_tokens=500`
- **THEN** 生效的输出上限为 500（字符数语义）

#### Scenario: 非法数值报错文案
- **WHEN** `timeout` 为 `"abc"`
- **THEN** 返回 `[错误: timeout/max_output_chars需为整数]`，命令不执行

#### Scenario: 空命令文案
- **WHEN** 未传 command 且未传 `background=true`、未传 `bg`
- **THEN** 返回 `[错误: command为空：前台执行需提供 command；后台任务用 background=true 提交，管理用 bg=status/wait/cancel]`

#### Scenario: 非正超时文案
- **WHEN** `timeout=0` 或负值
- **THEN** 返回 `[错误: timeout需为正整数（秒）]`

#### Scenario: 超时上限钳制
- **WHEN** 宿主配置的工具超时上限为 1800 秒，调用传 `timeout=3600`
- **THEN** 实际生效超时为 1800 秒

### Requirement: 安全确认门禁

系统 SHALL 在执行（含 `background=true` 提交）前对命令做安全确认判定：命中删除类命令（独立词 `rm`/`del`/`rd`/`rmdir`/`erase`/`format` 后跟空白或 `/`，或 `Remove-Item`，大小写不敏感）且未开启「rm免确认」、或命中 git 命令（出现独立词 `git`，大小写不敏感）且未开启「git免确认」时，SHALL 要求用户确认；`bg` 管理操作（status/wait/cancel，无 command）与未注入宿主上下文时 SHALL 跳过确认。Windows 上确认 SHALL 通过宿主注入的同步回调询问（终端提示 `  确认执行此命令? [y/N]: `，输入 `y`/`yes` 视为确认），用户拒绝时 SHALL 返回 `[操作已取消: 此命令需用户确认]` 且不执行命令；回调缺失时 SHALL 不拦截。

#### Scenario: rm 免确认开关
- **WHEN** 配置 `工具.rm免确认` 为 true，命令为 `rm -rf build`
- **THEN** 不进入确认流程，命令直接执行

#### Scenario: git 命中确认
- **WHEN** 配置 `工具.git免确认` 为 false，命令为 `git status`
- **THEN** 判定为需确认（Windows 走回调、非 Windows 走挂起确认）

#### Scenario: Windows 用户拒绝
- **WHEN** Windows 下确认回调返回 false
- **THEN** 结果为 `[操作已取消: 此命令需用户确认]`，命令不执行

#### Scenario: 无确认回调时不拦截
- **WHEN** 宿主未注入确认回调（如 headless 运行）
- **THEN** 命中删除类/git 的命令不拦截，直接执行

#### Scenario: 后台提交同样先过确认
- **WHEN** `background=true` 提交的命令命中删除类且未免确认
- **THEN** 先完成确认判定，确认通过后才提交后台任务

### Requirement: 非 Windows 挂起确认

在非 Windows 平台，需确认的命令 SHALL 不阻塞执行，而是返回字面量 `__AWAIT_CONFIRM__`，并把本次调用的完整参数（`command`、`timeout`、`max_output_chars`、`background`、`bg`、`id`）暂存于宿主上下文，由主循环在 `#` 提示符下以 `  确认执行此命令? [y/N]: ` 询问用户。用户确认后，主循环 SHALL 以「已确认」标记重新执行同一次调用；该次执行 SHALL 消费标记并跳过挂起，直接执行命令。用户拒绝时 SHALL 回传 `[操作已取消: 此命令需用户确认]`。返回 `__AWAIT_CONFIRM__` 时命令 SHALL 未被执行。

#### Scenario: 首次调用挂起
- **WHEN** 非 Windows 下执行未免确认的 `rm -rf build`
- **THEN** 返回 `__AWAIT_CONFIRM__`，命令未执行，且参数被暂存（含 command/timeout/max_output_chars/background/bg/id）

#### Scenario: 确认后重执行
- **WHEN** 用户对挂起确认输入 `y`
- **THEN** 系统以已确认标记重新执行同一命令，本次不再挂起，结果（含退出码行）正常回传

#### Scenario: 拒绝回传文案
- **WHEN** 用户对挂起确认输入其它内容（含空输入）
- **THEN** 该工具结果回传 `[操作已取消: 此命令需用户确认]`

### Requirement: 前台执行、超时与中断

系统 SHALL 按平台自适应执行前台命令：Windows 经 cmd 解析执行；Linux/macOS 经 `bash -c` 执行（优先 bash，缺失回退 sh；两者皆无时返回 `[错误: 未找到shell，请安装bash或sh后重试]`）。子进程 SHALL 隔离标准输入（不支持交互，交互场景属 Terminal/Serial 能力），并 SHALL 携带 UTF-8 输出编码提示（`PYTHONIOENCODING=utf-8` 语义），使 Python 子进程输出中文/emoji 不因 GBK 代码页报错。到达超时（默认 120 秒）或收到用户中断（ESC）时，系统 SHALL 终止整棵前台进程树（Windows 走 `taskkill /F /T`、Unix 走进程组 SIGKILL），SHALL 保留已产出的输出，且 SHALL 在收尾时给输出读取线程一个有界宽限（被分离的孙进程持有管道时不无限等待，仅保留已读到的部分）。输出解码 SHALL 为：UTF-8 优先；Windows 下失败回退 GBK；仍失败按 UTF-8 替换字符处理（Unix 无 GBK 回退）。命令启动失败 SHALL 返回固定文案：cmd.exe 缺失为 `[错误: cmd.exe未找到: …]`、Unix shell 缺失见上、其它启动异常（含命令含 NUL 等非法字符）为 `[错误: 启动失败: …]`。

#### Scenario: 平台自适应
- **WHEN** Windows 下执行 `echo %TEMP%`（cmd 语法）
- **THEN** 由 cmd 解析并正确展开环境变量
- **AND** 在 Linux/macOS 上命令由 `bash -c` 执行

#### Scenario: 超时终止
- **WHEN** 命令运行超过 timeout 秒
- **THEN** 进程树被终止，已产出输出保留，结果含超时提示与错误标签

#### Scenario: ESC 中断
- **WHEN** 前台命令运行中用户按 ESC
- **THEN** 进程树被终止，结果保留已产出输出并以 `[用户中断]` 行与提示符收尾，且无退出码行

#### Scenario: 输出解码回退
- **WHEN** 子进程输出为 GBK 编码的中文（Windows）
- **THEN** 解码为可读中文而非乱码（UTF-8 失败后回退 GBK）
- **AND** 两种情况都无法解码时按 UTF-8 替换字符兜底输出

#### Scenario: 子进程输出编码
- **WHEN** 前台命令为打印 emoji 的 Python 脚本（Windows）
- **THEN** 子进程以 UTF-8 输出，不因代码页编码失败

#### Scenario: 启动失败文案
- **WHEN** 命令含 NUL 等非法字符导致进程无法启动
- **THEN** 返回 `[错误: 启动失败: …]`

### Requirement: 前台结果格式

系统 SHALL 以固定格式组装前台结果：正常结束为「退出码行 + stdout 段 + stderr 段 + 提示符」，其中退出码行为 `[exit code: {rc}] [{随机标签}]`；stdout 段与 stderr 段 SHALL 在内容去首尾空白后非空时才出现，stderr 段 SHALL 以 `[stderr]` 单独一行开头、内容另起一行；各部分以单个换行连接，提示符 SHALL 恒为末行。被中断的结果 SHALL 为「stdout 段 + stderr 段 + `[用户中断]` + 提示符」，且 SHALL 无退出码行、SHALL 不含错误标签。超时的结果 SHALL 为「stdout 段 + stderr 段 + `[超时: 命令执行超过{timeout}秒，已终止] [错误标签]` + 提示符」，超时提示的秒数为整数格式，且 SHALL 带错误标签（UI 显示失败）。提示符 SHALL 为：Windows `{当前工作目录}>`；Unix 下工作目录等于 `$HOME` 时为 `~$ `、位于 `$HOME` 之下时为 `~/子路径$ `、其它为 `{当前工作目录}$ `。

#### Scenario: 正常结果
- **WHEN** 前台命令以退出码 0 结束且 stdout 为 `hello`
- **THEN** 结果由 `[exit code: 0] [随机标签]` 行、`hello` 行与提示符行按序组成

#### Scenario: 空输出段省略
- **WHEN** 命令无 stdout、仅 stderr 有内容
- **THEN** 结果不产生空行，stderr 段以 `[stderr]` 行开头紧随退出码行

#### Scenario: 中断结果
- **WHEN** 前台命令被 ESC 中断
- **THEN** 结果为已有输出 + `[用户中断]` + 提示符，无退出码行

#### Scenario: 超时结果
- **WHEN** 前台命令超过 120 秒被终止
- **THEN** 结果含 `[超时: 命令执行超过120秒，已终止]` 且带错误标签，位于输出段之后、提示符之前

### Requirement: cd 命令持久化

纯 cd 命令（`cd`/`chdir`，可带参数，且不含 `&`、`|`、`;` 等复合运算符）SHALL 由宿主直接切换自身工作目录，使后续所有工具共享新目录，而非交给子进程执行。简写形式 SHALL 支持：`cd..`/`chdir..` 为父目录、`cd...`/`chdir...` 为祖父目录、`cd\`/`chdir\` 为当前盘根；cmd 的 `/d ` 前缀 SHALL 被剥离，环境变量与 `~` SHALL 被展开，包裹引号 SHALL 被剥离。无参数 cd 的平台语义 SHALL 为：Windows 不切换目录、仅返回退出码 0 与提示符（由提示符体现当前目录，与 cmd 行为一致）；Unix 切换到 `$HOME`。切换成功 SHALL 返回 `[exit code: 0]` + 提示符（无其它输出）；切换失败 SHALL 返回 `cd: {错误信息}` 行 + `[exit code: 1]` 行 + 提示符。

#### Scenario: 单条 cd 持久化
- **WHEN** 执行 `cd subdir` 成功后调用 Read/Glob 等工具
- **THEN** 这些工具以 `subdir` 为当前目录工作

#### Scenario: 无参数 cd 平台差异
- **WHEN** Windows 下执行 `cd`（无参数）
- **THEN** 不切换目录，返回退出码 0 与当前目录提示符
- **AND** 在 Linux/macOS 下执行 `cd` 则切换到 `$HOME`

#### Scenario: 切换失败文案
- **WHEN** 执行 `cd no_such_dir`
- **THEN** 返回 `cd: …` 错误行 + `[exit code: 1]` + 提示符

#### Scenario: 简写与展开
- **WHEN** 执行 `cd..` 或 `cd %TEMP%`
- **THEN** 分别切换到父目录、展开后的临时目录

#### Scenario: 复合运算符不按纯 cd 处理
- **WHEN** 执行 `cd subdir && ls`
- **THEN** 不整体交给 cd 持久化处理，而进入多段执行流程（cd 在其段内生效）

### Requirement: 多段命令执行

系统 SHALL 在引号外、括号组外并跳过 cmd 转义字符 `^` 的前提下，按 `&&`/`||` 将命令切分；段数大于 1 时 SHALL 逐段执行。短路语义 SHALL 为：`&&` 段在前一段退出码非 0 时跳过并输出 `[跳过: 前一命令失败(退出码{prev_rc})] {段}`；`||` 段在前一段退出码为 0 时跳过并输出 `[跳过: 前一命令成功] {段}`。每段预算 SHALL 独立：初始为总 timeout，段结束后剩余预算 = `max(0, 剩余 − 该段耗时)`；预算为 0 时其后各段 SHALL 立即判定超时。段结果 SHALL 为：成功段不输出退出码行；失败段输出各自的退出码行；末尾追加总退出码行 = 最后执行段的退出码（段退出码为 -1 或发生中断时 SHALL 不追加）。段超时 SHALL 输出 `[超时: 命令执行超过{max(段耗时,1.0):.1f}秒，已终止] [错误标签]`，该行 SHALL 位于该段输出之前（stdout、stderr 依次随后），段退出码记为 -1 并终止后续段。段被中断 SHALL 终止后续段、在末尾追加 `[用户中断]` 且不输出总退出码行（该段已产出输出不保留）。段启动失败 SHALL 输出 `[错误: 段{i}启动失败: …]` 并终止后续段。多段中的 cd 段 SHALL 由宿主执行：成功时无任何输出并记录退出码 0，失败时输出 `cd: {错误信息}` 并记录退出码 1，无参数 cd 仅记录退出码 0 不切换目录；cd 段 SHALL 不产生退出码行。结果末尾 SHALL 恒有提示符。

#### Scenario: && 短路跳过
- **WHEN** 执行 `false && echo hi`
- **THEN** 首段因失败输出其退出码行，第二段不执行并输出跳过文案 `[跳过: 前一命令失败(退出码1)] echo hi`，末尾总退出码行为 `[exit code: 1]`

#### Scenario: || 短路跳过
- **WHEN** 执行 `echo hi || echo fallback`
- **THEN** 第二段不执行，结果含 `[跳过: 前一命令成功] echo fallback`

#### Scenario: 段输出与总退出码
- **WHEN** 执行 `echo a && echo b`
- **THEN** 各成功段无退出码行，结果为 `a`、`b` 与末尾唯一的总退出码行 `[exit code: 0]`

#### Scenario: 预算递减与耗尽
- **WHEN** 首段耗时接近总 timeout
- **THEN** 后续段预算为剩余时间；剩余预算为 0 时后续段立即判定超时并以超时提示收尾

#### Scenario: 段超时格式
- **WHEN** 多段中某段超时
- **THEN** 该段结果以超时提示行开头（秒数为「段耗时（下限 1.0）保留 1 位小数」），随后是该段 stdout、stderr，并终止后续段

#### Scenario: 多段中断
- **WHEN** 多段执行中用户按 ESC
- **THEN** 结果为已收集的段输出 + `[用户中断]` + 提示符，无总退出码行

#### Scenario: 多段中的 cd 段
- **WHEN** 执行 `cd subdir && ls`
- **THEN** 宿主直接切换工作目录（cd 段无输出），随后 `ls` 在新目录中执行

### Requirement: python 载荷直执行

系统 SHALL 识别形如「解释器名或含盘符/空格的全路径 + 可选旗标（不得含 `-c`/`-m`）+ `-c` + 以双引号收尾的载荷」的命令段：载荷引号闭合后允许空白与 `2>&1`（SHALL 剥离，语义等价，工具本就合并展示 stdout+stderr），或 `|`/`>` 后缀；解释器 SHALL 能被解析为真实可执行（`.bat` 垫片等回退），其基名 SHALL 匹配 python 解释器形态（防误命中如 `spy.exe`）。命中时 SHALL 不经 shell 解析、直接创建解释器进程，载荷原样送达解释器（多行、`%`、`&`、`|`、`<` 等无需转义）。尾随后缀语义 SHALL 为：`>nul`（大小写不敏感）丢弃 stdout 仅保留 stderr；`>file`/`>>file` 将 stdout 以 UTF-8 写入文件（覆盖/追加），返回结果中 stdout 置空，写文件失败时 SHALL 返回退出码 1 与 `[stderr]\n写入文件失败: …`；`| cmd` 将解释器 stdout 字节喂给该管道命令并在剩余预算 `max(0.1, timeout − 已耗时)` 内收集，解释器与管道命令的 stderr SHALL 合并展示，解释器阶段中断/超时时 SHALL 跳过管道阶段。中断或超时时 SHALL 不写重定向文件。不支持的形态与后缀 SHALL 回退 shell 原路径执行（零回归）。命中路径 SHALL 覆盖 Windows 单段命令及各平台多段中的 python 段。

#### Scenario: Windows 单段直执行
- **WHEN** Windows 下执行含多行与 `%` 的 `python -c "…"` 命令
- **THEN** 载荷原样交给解释器执行（不经 cmd 吞改字符），结果按前台格式返回

#### Scenario: 多段中的 python 段
- **WHEN** 执行 `python -c "print(1)" && echo ok`（任平台）
- **THEN** python 段直执行，`echo ok` 段独立执行并计入总退出码

#### Scenario: 不支持形态回退
- **WHEN** 命令为 `python -c code`（无引号）或后缀为 `| a | b`、`> "a b" extra`
- **THEN** 不走直执行，按 shell 原路径执行

#### Scenario: >nul 丢弃 stdout
- **WHEN** 命令为 `python -c "print('x')" >nul`
- **THEN** 返回结果不含 stdout，stderr 仍展示

#### Scenario: 重定向写文件
- **WHEN** 命令为 `python -c "print('x')" > out.txt`（或 `>> out.txt`）
- **THEN** stdout 以 UTF-8 写入该文件（覆盖/追加），返回结果中 stdout 为空

#### Scenario: 管道后缀
- **WHEN** 命令为 `python -c "print('x')" | findstr x`
- **THEN** 解释器 stdout 被喂给管道命令，结果展示管道命令输出；解释器中断/超时时跳过管道阶段

#### Scenario: 中断不写文件
- **WHEN** 带 `> file` 后缀的直执行命令被 ESC 中断
- **THEN** 不写入目标文件，结果按中断格式返回

### Requirement: 输出截断

前台与多段结果超过 `max_output_chars` 时，系统 SHALL 保留头部 `max_chars*2//3` 与尾部 `max_chars − 头部` 字符，中段插入 `...[中间截断: 输出共{N}字符, 已保留首{X}字符+尾{Y}字符(≈{T}token)。增大max_output_chars可获取完整输出]`（≈T 为字符混合密度估算的 token 数）。截断切点 SHALL 做标签吸附：切点落在框架标签内部时，头部切点吸附到标签结尾、尾部切点吸附到标签开头，吸附后两切点相向越过时取尾部切点收口——框架标签 SHALL 不被切开。结果未超限时 SHALL 原样返回。返回给 LLM 前，结果还 SHALL 受宿主配置的全局输出上限约束（超限时按字符保留首尾并插入 `[全局截断: 输出共{N}字符(≈{T}token), 已达全局上限{K}KB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]`）。

#### Scenario: 首尾保留与提示文案
- **WHEN** 输出长度超过 max_output_chars（默认 4000）
- **THEN** 结果保留首部与尾部，中段为包含「输出共{N}字符」「已保留首{X}字符+尾{Y}字符」「≈{T}token」「增大max_output_chars可获取完整输出」的截断提示

#### Scenario: 未超限原样返回
- **WHEN** 输出长度不超过 max_output_chars
- **THEN** 结果不含任何截断提示

#### Scenario: 标签不被切开
- **WHEN** 切点位置落在退出码行或错误标签内部
- **THEN** 切点被吸附到标签边界，标签完整保留在结果中，可被标签剥离与判定逻辑识别

### Requirement: 后台任务提交与槽位

系统 SHALL 在 `background=true` 或 `bg` 参数非空时把命令交由后台任务机制处理（操作名按去首尾空白、小写归一化），前台逻辑不变；`background=true` 且 command 为空时 SHALL 返回 `[错误: background=true 提交后台任务需提供 command]`。提交 SHALL 立即返回（不阻塞）并给出文本：`bg{N} 已提交（后台运行）`、`结果: {结果文件绝对路径}`、`查状态: Shell(bg="status") · 等待完成: Shell(bg="wait")`；命令 SHALL 以去首尾空白后的形式落盘与执行。槽位 SHALL 固定为 8 个（`bg1~bg8`），终态即释放；全忙时 SHALL 返回 `[错误: 后台并发已达上限8(bg1~bg8)，当前运行: {运行中列表}。请用 Shell(bg="wait") 等待完成，或 Shell(bg="cancel", id=N) 取消不再需要的任务]`。编号复用 SHALL 优先空闲槽位、其次复用完成时间最早的终态槽位；复用终态槽位时 SHALL 先把旧结果归档为 `bg{N}.log.{序号}.prev`（同名 `.tail` 一并改名，序号会话内递增），归档成功时提交结果 SHALL 追加 `提示: 已归档该槽位旧任务结果到 {归档路径}，仍可 Read 读取`；旧任务无输出时不归档。新任务 SHALL 立即写入任务头 `# bg{N} 提交于 HH:MM:SS | 命令: {命令}`（UTF-8）并立即落盘（保证读端立即可见），输出 SHALL 分块（4KB）转码为 UTF-8 后增量落盘（每块即时落盘，运行中即可 Read 增量，小输出任务可能到结束才可见）；子进程原始输出 SHALL 按「UTF-8 优先、Windows 回退 GBK」增量解码（跨读块的多字节字符不得乱码）。后台任务 SHALL 以提交时刻的工作目录与 UTF-8 编码环境启动。启动失败 SHALL 返回 `[错误: 后台任务启动失败: …]` 并释放槽位；结果文件不可写 SHALL 返回 `[错误: 无法写入结果文件: …]`；Unix 缺 bash/sh 时错误文案为 `未找到shell，请安装bash或sh后重试`。

#### Scenario: 提交返回值与句柄
- **WHEN** 以 `background=true` 提交命令
- **THEN** 立即返回含 `bg{N} 已提交（后台运行）`、结果文件绝对路径与查状态/等待提示的文本

#### Scenario: 空命令提交报错
- **WHEN** `background=true` 且未传 command
- **THEN** 返回 `[错误: background=true 提交后台任务需提供 command]`

#### Scenario: 并发上限
- **WHEN** 8 个槽位均为运行中时提交第 9 个任务
- **THEN** 返回含「后台并发已达上限8(bg1~bg8)」「当前运行: bg1, …」「请用 Shell(bg="wait") 等待完成，或 Shell(bg="cancel", id=N) 取消不再需要的任务」的错误文案

#### Scenario: 终态释放与编号复用
- **WHEN** `bg2` 已进入终态后提交新任务（其余槽空）
- **THEN** 新任务优先取空闲槽位；无空闲槽时复用完成时间最早的终态槽位

#### Scenario: 复用归档
- **WHEN** 复用槽位的旧任务有输出
- **THEN** 旧结果被改名为 `bg{N}.log.{序号}.prev`（`.tail` 一并），提交文本追加归档提示，内容仍可读取

#### Scenario: 日志头与增量落盘
- **WHEN** 任务运行中读取结果文件
- **THEN** 首行为 `# bg{N} 提交于 HH:MM:SS | 命令: {命令}`，其后为已收口的分块输出增量

#### Scenario: 编码统一
- **WHEN** 后台任务在 Windows 下输出 GBK 编码中文（且跨读块切分）
- **THEN** 结果文件为单编码 UTF-8，中文完整无乱码

#### Scenario: 启动失败释放槽位
- **WHEN** 后台任务进程启动失败
- **THEN** 返回 `[错误: 后台任务启动失败: …]`，该槽位不保持占用

### Requirement: 后台任务状态与等待

`bg="status"` SHALL 返回全量快照：首行 `[后台任务]`；每个非空闲槽一行 `bg{N} {状态描述} "{命令前40字符（超长加…）}" 输出{大小}` 加一行缩进的 `    结果: {结果文件路径}`；状态描述为 `运行中 已运行{MM:SS}`、`完成 exit={N} (HH:MM:SS)`、`失败 exit={N} (HH:MM:SS)`、`已取消` 之一；输出大小按 B/KB/MB 自适应（1 位小数）；发生截断时行尾追加 `（超限截断，尾部见 {结果文件路径}.tail）`；末尾附带 `空闲槽位: {编号列表}`；无任何任务时 SHALL 返回 `[后台任务] 当前无后台任务`。`bg="wait"` 的等待时长 SHALL 取 `timeout`（为 0 或未提供时取 120 秒，负值返回 `[错误: timeout需为正整数（秒）]`；非数字在参数归一化阶段即被 `[错误: timeout/max_output_chars需为整数]` 拦截），并受宿主超时上限钳制；等待期间任一任务进入终态即提前返回 `[后台任务] bg{N} 完成 exit={N}；…`（多条以 `；` 连接）+ 快照；无运行中任务时 SHALL 立即返回 `[后台任务] 当前无运行中的后台任务` + 快照；超时返回 `[后台任务] 等待{N}秒超时，当前无新完成` + 快照；等待被 ESC 打断时 SHALL 返回 `[后台任务] 等待已被用户打断（后台任务不受影响，继续运行）` + 快照，且 SHALL 不终止任何后台任务。

#### Scenario: 快照格式
- **WHEN** 有 1 个运行中任务与 1 个失败任务时查询状态
- **THEN** 输出含 `bg{N} 运行中 已运行MM:SS "命令" 输出1.2KB` 与 `    结果: …` 两行式条目，以及末尾空闲槽位列表

#### Scenario: 无任务快照
- **WHEN** 无任何后台任务时查询状态
- **THEN** 返回 `[后台任务] 当前无后台任务`

#### Scenario: 完成事件唤醒等待
- **WHEN** 等待期间某任务完成
- **THEN** 立即返回 `[后台任务] bg{N} 完成 exit={N}` + 完整快照

#### Scenario: 无运行任务立即返回
- **WHEN** 等待时没有任何运行中任务
- **THEN** 立即返回 `[后台任务] 当前无运行中的后台任务` + 快照

#### Scenario: 等待超时
- **WHEN** 等待到达指定秒数仍无新完成
- **THEN** 返回 `[后台任务] 等待{N}秒超时，当前无新完成` + 快照

#### Scenario: 打断等待不杀任务
- **WHEN** 等待中用户按 ESC
- **THEN** 返回等待被用户打断的文案 + 快照，后台任务继续运行

#### Scenario: 等待参数校验
- **WHEN** `bg="wait"` 且 `timeout` 为非数字（或负值）
- **THEN** 分别返回 `[错误: timeout/max_output_chars需为整数]` / `[错误: timeout需为正整数（秒）]`
- **AND** `timeout` 缺省或为 0 时按 120 秒等待

### Requirement: 后台任务取消

`bg="cancel"` SHALL 按固定校验顺序返回文案：`id` 缺失 → `[错误: cancel 需指定任务编号 id（如 id=3 取消 bg3）]`；`id` 非整数 → `[错误: id 需为整数: {值}]`；编号越界 → `[错误: bg{N} 编号无效（有效范围 bg1~bg8）]`；槽位空闲 → `[错误: bg{N} 空闲（无任务）]`；已处于终态 → `[错误: bg{N} 已处于终态（{状态}），无需取消]`。合法取消 SHALL 终止该任务进程树（不阻塞调用方），SHALL 保留已产出内容，并返回 `bg{N} 已取消（进程树已终止，已产出内容保留在 {结果文件路径} 仍可 Read 读取；管道中尚未收口的剩余输出随后补落盘）`。未知 `bg` 操作 SHALL 返回 `[错误: 未知 bg 操作: {值}（可用: status / wait / cancel）]`。

#### Scenario: 正常取消
- **WHEN** 对运行中任务执行 `bg="cancel", id=N`
- **THEN** 返回 `bg{N} 已取消（…）` 文案，进程树被终止，结果文件中已产出内容仍可读取

#### Scenario: id 缺失与非整数
- **WHEN** `bg="cancel"` 未传 id / 传 `id="abc"`
- **THEN** 分别返回 `[错误: cancel 需指定任务编号 id（如 id=3 取消 bg3）]` 与 `[错误: id 需为整数: abc]`

#### Scenario: 编号越界与空槽
- **WHEN** `id=9` 或该槽位空闲
- **THEN** 分别返回 `[错误: bg9 编号无效（有效范围 bg1~bg8）]` 与 `[错误: bg{N} 空闲（无任务）]`

#### Scenario: 终态不可取消
- **WHEN** 对已完成任务执行取消
- **THEN** 返回 `[错误: bg{N} 已处于终态（done），无需取消]`

#### Scenario: 未知操作
- **WHEN** `bg="foo"`
- **THEN** 返回 `[错误: 未知 bg 操作: foo（可用: status / wait / cancel）]`

### Requirement: 后台结果文件生命周期与会话隔离

后台结果 SHALL 落在系统临时目录下本进程专属的随机子目录（前缀 `narnat_bg_`）中，文件名约定为 `bg{N}.log`、`bg{N}.log.tail`、`bg{N}.log.{序号}.prev`（及 `.prev.tail`）；目录 SHALL 每进程唯一，使同项目多 agent、父子 agent 互不抢占，且 SHALL 与 AI 的 cd 无关（返回绝对路径）。会话开始预清 SHALL 清空结果目录但不终止任何进程；会话结束硬清理 SHALL 终止全部运行中任务的进程树、清空结果目录并重置槽位表（幂等）。过期清扫 SHALL 仅回收系统临时目录下超过 7 天未活动（目录及其内任一文件 mtime 均超出保活期）的 `narnat_bg_*` 目录，回收前 SHALL 先原子改名再删除，改名失败（存在活跃文件句柄）或任何异常时 SHALL 放弃删除。运行中任务计数与摘要 SHALL 可供宿主软提醒使用：摘要为 `bg{N}({命令前40字符})` 以 `、` 连接，无运行中任务时为空串。单个任务主文件 SHALL 50MB 封顶，超出部分滚动保留最新 1MB 尾部缓冲并在收口时落盘为 `.tail` 文件。

#### Scenario: 目录隔离
- **WHEN** 两个 agent 进程并行运行各自的后台任务
- **THEN** 各自结果落在不同的 `narnat_bg_*` 目录，互不可见

#### Scenario: 预清不杀进程
- **WHEN** 会话开始执行预清
- **THEN** 上次残留结果被清空，且不终止任何进程

#### Scenario: 结束硬清理幂等
- **WHEN** 会话结束执行清理（重复调用）
- **THEN** 全部运行中任务进程树被终止、目录被清空、槽位表重置，重复调用不报错

#### Scenario: 过期清扫
- **WHEN** 临时目录下存在超过 7 天未活动的 `narnat_bg_*` 目录
- **THEN** 该目录被回收；保活期内（或目录内文件近期有更新）的目录保留；改名失败的目录放弃删除

#### Scenario: 超大输出截断
- **WHEN** 后台任务输出超过主文件上限
- **THEN** 主文件封顶，最新约 1MB 尾部输出在收口时落盘为 `{结果文件}.tail`，快照标注超限截断

#### Scenario: 运行摘要格式
- **WHEN** 宿主查询运行中任务摘要（bg2、bg5 运行中）
- **THEN** 返回形如 `bg2(命令前40字符)、bg5(命令前40字符)` 的串；无运行中任务时为空白串

### Requirement: 退出码与错误标签协议

框架 SHALL 为自身生成的行附加进程级随机标签：退出码行为 `[exit code: {rc}] [{随机标签}]`，错误行为 `[错误: {消息}] [{随机错误标签}]`；超时等非「错误」前缀的失败形态 SHALL 通过附加错误标签标记。标签 SHALL 在进程内保持不变、由随机值生成，命令输出无法预知或伪造。结果在交给 LLM 前 SHALL 剥离全部随机标签，LLM 可见文本 SHALL 与不带标签时完全一致（`[exit code: {N}]`、`[错误: …]`、`[超时: …]` 等文本本身保留）。UI 失败判定 SHALL 只依据错误标签（先判定、后剥离），命令输出中出现同款文字 SHALL 不触发失败显示。同一结果含多个退出码行时，「整体退出码」SHALL 取最后一个。截断 SHALL 保证标签不被切开（见「输出截断」）。

#### Scenario: 标签随机且进程内稳定
- **WHEN** 同一进程内多次生成退出码行
- **THEN** 各行的随机标签相同，且无法从命令输出侧预测

#### Scenario: LLM 可见文本等价
- **WHEN** 带标签结果交给 LLM 前
- **THEN** 随机标签被剥离，文本仅余 `[exit code: {N}]` 等可读内容

#### Scenario: 伪造不生效
- **WHEN** 命令自身输出文本 `[exit code: 0]` 或 `[错误: 假消息]`
- **THEN** UI 不据此判定失败（缺随机标签），退出码判定亦不采信

#### Scenario: 判定顺序
- **WHEN** 结果同时含错误标签与普通文本
- **THEN** 先以错误标签判定失败显示，再剥离标签后回传 LLM

#### Scenario: 整体退出码取最后
- **WHEN** 多段结果含多个退出码行
- **THEN** 整体退出码为最后一个退出码行的值

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（避免用户可感知差异）：① `max_output_chars` 为 0 或负数时不早退，命令照常执行完毕后结果仅为 `[错误: max_output_chars需为正整数]`（副作用已发生、只丢输出）；② 多段执行中剩余预算为 0 时后续段立即判定超时，超时提示的秒数保底为 1.0 秒（1 位小数）；③ 多段中任一段被中断时该段已产出的输出不保留（结果仅含 `[用户中断]`）；④ `bg="wait"` 因任务被取消而提前醒来时，文案为「等待{N}秒超时，当前无新完成」（N 为请求的等待时长，即使实际远未等满）；⑤ 中断标志跨调用残留时，下一次执行入口清零，不误伤新命令；⑥ Unix 下单段命令不做 python 直执行（仅多段中的 python 段直执行）；⑦ 多段中的非 python 段在 Unix 由 `/bin/sh` 执行，而单段执行优先 bash；⑧ 无参数 cd 出现在多段中时不切换目录（仅记录成功），与单段行为不同（单段在 Windows 显示当前目录、在 Unix 切换到 `$HOME`）；⑨ 进程树终止失败等极端情况下，异常可能以通用工具执行失败文案（`[错误: 工具执行失败(Shell): …]`）返回，而非超时/中断格式；⑩ 快照中「已取消」条目不带完成时间与退出码；⑪ 返回结果超过宿主全局输出上限时按字符硬截断（首尾保留），该路径不做标签吸附。若未来修正，须作为独立变更处理。

#### Scenario: 输出上限 0 不早退（兼容怪癖）
- **WHEN** `max_output_chars=0` 且命令有副作用（如创建文件）
- **THEN** 命令执行完成、副作用已发生，返回 `[错误: max_output_chars需为正整数]`

#### Scenario: 预算耗尽即超时（兼容怪癖）
- **WHEN** 多段中前段耗尽总预算
- **THEN** 后续段立即以「超过1.0秒，已终止」形态的提示判定超时

#### Scenario: 中断丢弃段输出（兼容怪癖）
- **WHEN** 多段中某段运行中被 ESC 中断
- **THEN** 结果不含该段已产出的输出，仅含 `[用户中断]`

#### Scenario: 取消唤醒报超时（兼容怪癖）
- **WHEN** 等待中的任务被取消使等待提前醒来
- **THEN** 文案为「等待{N}秒超时，当前无新完成」（不宣布取消事件）

#### Scenario: 中断标志入口清零（兼容怪癖）
- **WHEN** 上一次中断未被消费即发起新的前台执行
- **THEN** 新命令不受残留中断标志影响，正常执行

#### Scenario: Unix 单段不直执行（兼容怪癖）
- **WHEN** Unix 下单段命令为 `python -c "…"`
- **THEN** 由 bash 执行（不直执行）；同一命令出现在多段中则直执行

#### Scenario: 多段 sh 与单段 bash 的不一致（兼容怪癖）
- **WHEN** Unix 下单段与多段中执行同一 shell 语义命令
- **THEN** 单段优先 bash、多段交由 `/bin/sh`（平台分支差异保持现状）

#### Scenario: 多段无参数 cd 不切换（兼容怪癖）
- **WHEN** Unix 下执行多段命令 `cd && pwd`
- **THEN** cd 段不切换目录，`pwd` 仍输出切换前的目录（单段 `cd` 会切到 `$HOME`）

#### Scenario: 全局截断不吸附标签（兼容怪癖）
- **WHEN** 返回结果恰好超过宿主全局输出上限且标签落在切点
- **THEN** 按字符硬截断（该路径不保证标签完整），保持现状
