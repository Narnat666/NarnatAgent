# Narnat RPC 协议 v1

VSCode 扩展与 `narnat.exe` 之间的通信协议。**JSON-RPC 2.0 over stdio，一行一条消息**（NDJSON 分帧）。

> 设计参照 `openai/codex` 的 app-server：核心逻辑留在本地进程，客户端只做渲染。
> 本项目 `narnat_agent/mcp/client.py` 已有同款分帧实现（作为客户端），可直接复用其序列化思路。

**规模**：12 个请求方法（§3）+ 18 类通知（§4）。
契约审计脚本可一键核对文档 ↔ 后端实现 ↔ 前端渲染三者的覆盖度（输出 `CONTRACT_ALL_COVERED`）。

---

## 1. 进程模型

```
VSCode 扩展 (Node)                narnat.exe (Python)
      │                                  │
      ├── spawn("narnat.exe", ["--rpc"]) ─┤  启动一次，常驻
      │                                  │
      ├──── stdin: 请求/通知 ───────────→ │
      │←─── stdout: 响应/通知 ────────────┤
      │←─── stderr: 日志（不解析）─────────┤
```

- 扩展 activate 时 spawn 一次，之后所有交互复用同一进程
- 进程存活期间保持会话状态（当前会话、模型、上下文），重启代价约 1 秒
- 扩展 deactivate 时发 `shutdown` 再 kill，超时 2 秒强制 kill
- **stdout 只输出协议消息**；任何调试输出必须走 stderr

### 进程退出的三条路径

| 触发 | 机制 | 实测 |
|---|---|---|
| 客户端发 `shutdown` | 收尾后 `os._exit(0)` | 208ms 内退出 |
| stdin 被关闭（父进程正常退出） | `readline()` 返回空 → 收尾退出 | 0.0s |
| **父进程被强杀，管道仍被持有** | 看门狗每 5s 检查父进程存活，消失即收尾退出 | 父进程死后 ≤5s |

看门狗是为最后一种场景加的：VSCode 扩展宿主被 `taskkill /F` 杀掉时 `deactivate`
不执行，且 Windows 上 stdin 管道可能被其他进程继承持有、不产生 EOF。
没有它时实测留下过孤儿 `python main.py --rpc` 进程。

> 实现：`narnat_agent/rpc/server.py` 的 `_ParentWatchdog`；
> Windows 用 `ctypes` 调 `OpenProcess` + `GetExitCodeProcess`，POSIX 用 `os.kill(pid, 0)`；
> 判断不出时一律视为「父进程还活着」（宁可多活，不误杀）。

### 启动参数

```
narnat.exe --rpc [--projects-root <路径>]
```

- `--rpc`：进入 RPC 模式（不启动 prompt_toolkit、不打印 banner）
- 工作目录由扩展的 `cwd` 决定（等于 VSCode 工作区根）

---

## 2. 消息格式

严格遵循 JSON-RPC 2.0：

```jsonc
// 请求（有 id，需响应）
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}

// 响应（成功）
{"jsonrpc":"2.0","id":1,"result":{...}}

// 响应（失败）
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found"}}

// 通知（无 id，不响应）—— 服务端推给客户端的事件都走这个
{"jsonrpc":"2.0","method":"stream/delta","params":{"text":"你好"}}
```

**错误码**

| code | 含义 |
|---|---|
| -32700 | JSON 解析失败 |
| -32600 | 请求格式非法 |
| -32601 | 方法不存在 |
| -32602 | 参数非法 |
| -32603 | 内部错误 |
| -32000 | 业务错误（如会话不存在，`data` 里带人类可读原因） |

---

## 3. 客户端 → 服务端（请求）

### 3.1 `initialize`

握手，返回服务端能力。**必须是第一个请求**。

```jsonc
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
← {"jsonrpc":"2.0","id":1,"result":{
     "version":"16.7.9",
     "protocol":1,
     "model":{"current":"deepseek-flash","options":["deepseek-v4-pro","deepseek-flash"]},
     "thinking":{"effort":"max","options":{"low":"低","medium":"中","high":"高","max":"最大"},"passback":false},
     "goal":{"enabled":false,"maxRounds":10},
     "plugins":[{"name":"terminal","label":"多终端持久 SSH","enabled":true}],
     "session":{"current":"grep-ignore-case","children":["try-regex"]},
     "workdir":"<启动后端时的当前工作目录>"
   }}
```

`thinking.passback`（boolean）：思考内容是否随请求**回传给模型**（对应配置 `智能体.思考.回传`，由 `/thinkback` 或 `config/set` 切换，**默认 `false`**）。它只决定"发不发"——思考内容本身始终记录在会话里，`thinking/*` 事件也照常下发（见 §4.3）。客户端可用它渲染开关状态。

### 3.2 `chat/send`

发起一轮对话。**立即返回** `{"accepted":true}`，真正的产出通过通知推送，结束时发 `turn/end`。

```jsonc
→ {"jsonrpc":"2.0","id":2,"method":"chat/send","params":{"text":"给 grep 加个参数"}}
← {"jsonrpc":"2.0","id":2,"result":{"accepted":true}}
```

- 若上一轮仍在跑，返回 `-32000` 错误，`message` 为 `"busy"`（实测）
- `text` 缺失 / 非字符串 / 全空白 → `-32602`（参数非法）
- busy 在 `turn/end` 之后自动解除，无需客户端干预

### 3.3 `chat/cancel`

打断当前轮（等价终端里的 ESC）。

```jsonc
→ {"jsonrpc":"2.0","id":3,"method":"chat/cancel","params":{}}
← {"jsonrpc":"2.0","id":3,"result":{"cancelled":true}}
```

- `cancelled:true` 表示成功打断了正在跑的轮次；`false` 表示当前没有可打断的轮次
- 两种情况下服务端都会补一条 `turn/end`（`reason:"cancelled"`），客户端只需照常收尾
- 打断后进程仍可用，可立即发下一轮 `chat/send`（实测）

### 3.4 `command/exec`

执行斜杠命令，等价于终端里输入 `/xxx args`。

```jsonc
→ {"jsonrpc":"2.0","id":4,"method":"command/exec","params":{"command":"cd","args":"ui-webview"}}
← {"jsonrpc":"2.0","id":4,"result":{
     "handled":true,
     "ok":true,
     "lines":["已进入会话: ui-webview"],
     "stateChanged":["session"]
   }}
```

- `command` 不带前导 `/`
- `handled:false` 表示不是已知命令（调用方应把它当普通对话发给 `chat/send`）
- `stateChanged` 列出因此变化的顶层状态（`session` / `model` / `thinking` / `goal` / `plugin` / `context`），客户端据此决定要不要重新拉 `session/list` 或更新状态栏

### 3.5 `session/list`

返回结构化会话树（**不是格式化字符串**）。

```jsonc
→ {"jsonrpc":"2.0","id":5,"method":"session/list","params":{"all":false}}
← {"jsonrpc":"2.0","id":5,"result":{
     "current":"grep-ignore-case/try-regex",
     "groups":[
       {"label":"今天","sessions":[
         {"name":"grep-ignore-case","time":"14:32","count":28,"group":"today",
          "current":false,"deleted":false,
          "children":[
            {"name":"try-regex","status":"completed","time":"14:20","current":true,"deleted":false},
            {"name":"alt-approach","status":"active","time":"14:25","count":12,"current":false,"deleted":false}
          ]},
         {"name":"ui-webview","time":"11:05","count":64,"group":"today","current":false,"deleted":false,"children":[]}
       ]},
       {"label":"更早","sessions":[...],"hidden":3}
     ]
   }}
```

`status` 取值：`new` / `active`（进行中，UI 显示 ⚠ 待完成）/ `completed`（UI 显示 ✓ 已完成）

> `groups[].label` 只是后端的传输容器（按今天/更早切段）；客户端**不必**按分组显示，
> 扁平列表 + 一个「展开其余 N 条」更省空间。`hidden` 表示该组被截断的条数。

### 3.6 `session/new`

新建对话：回到游离态并清空上下文。

```jsonc
→ {"method":"session/new"}
← {"result":{"current":null,"messageCount":1}}
```

- 用于「打断后不想继续这段，直接开新的」——**不需要先 `/save`**
- 当前若在已保存的会话/分支里，先落盘（之后仍能从列表进入）
- 游离态（未保存）的对话内容如用户所愿被丢弃
- 只在消息列表里保留 system prompt，因此 `messageCount` 为 1
- 若有轮次正在运行，服务端会先打断再新建

### 3.7 `session/switch`

```jsonc
→ {"method":"session/switch","params":{"name":"ui-webview"}}
← {"result":{"current":"ui-webview","messageCount":64}}
```

子会话用 `父/子` 形式。裸名且唯一重复时会返回 `-32000` 并带候选列表。

**`messages` 载荷**（`session/switch`、`session/new`、`session/done` 与 `command/exec` 的 `cd` 都会返回）：

```jsonc
"messages":[
  {"role":"user","text":"给 grep 加个参数"},
  {"role":"assistant","text":"参数定义在...","thinking":"先确认参数在哪一层"},
  {"role":"assistant","text":"已新增 ignore_case"}
]
```

- `role` 只有 `user` / `assistant`（子会话标题那条 system 以 `# 子会话 [` 开头时会原样出现）；`text` 恒为正文字段，**思考不算正文**
- `assistant` 项的 `thinking`（string）：该条回复的模型思考内容，**可选**——**没有思考内容时该键不出现**（不是空串），客户端据此决定是否渲染思考块；思考与回传开关无关，关闭回传后历史里仍有思考
- 工具调用、空正文消息不下发

### 3.8 `session/delete`

**立即删除**（删完即落盘）。CLI 的 `/rm` 才是「标记删除、退出时生效」；
面板后端是长驻进程，没有退出时机，所以 RPC 这条路径直接删文件。

```jsonc
→ {"method":"session/delete","params":{"name":"old-1"}}
← {"result":{"deleted":true,"name":"old-1","parent":null,
             "wasCurrent":false,"current":"grep-ignore-case"}}
```

返回字段：

| 字段 | 含义 |
| --- | --- |
| `deleted` | 恒为 `true`（失败走 JSON-RPC 错误对象，见下） |
| `name` | 实际删掉的会话名（子会话时是子名） |
| `parent` | 子会话的父会话名；根会话为 `null` |
| `wasCurrent` | 删的是否会掏空当前所在位置（删的是当前会话、或其父）。`true` 时服务端已复位到游离态，`current` 为 `null` |
| `current` | 删除后的当前路径：`父/子` / 名称 / `null` |
| `ghost` | 可选。`true` 表示删的是幽灵父会话（父 json 已丢、只剩 `<父>/` 下的子会话文件） |
| `childrenRemoved` | 可选。仅 `ghost`：随父一起清掉的子会话文件个数 |
| `broken` | 可选。`true` 表示删掉的是列表里看不到的损坏 JSON 文件（界面应提示「清理了损坏文件」而不是「删除了会话」） |

- 可删**任意**会话，不受当前状态限制；删当前会话（或当前会话的父）会先打断在跑的轮次再复位状态
- 幽灵父会话同样可删：`<父>/` 下的子会话文件与空目录一并清掉，删完名字可复用
- 失败返回 `-32000`，`message` 是可读原因（文件被占用 / 无权限 / 目标不存在）

### 3.9 `session/explore` / `session/done`

**前置条件**：`session/explore` 要求当前处于**已保存的根会话**（RootSession）。
游离态（还没 `/save` 过）调用会返回 `-32000 "当前状态不可用 /explore"`；
子会话内再开分支同样被拒（源码 `ChildSession` 没有 explore）。
调用方应先确保 `initialize` 返回的 `session.current` 非 null。

```jsonc
→ {"method":"session/explore","params":{"name":"try-regex"}}
← {"result":{"current":"grep-ignore-case/try-regex"}}

→ {"method":"session/done"}
← {"result":{"merged":true,"current":"grep-ignore-case","bytes":1120}}

// 分支没有任何增量内容时的**成功**响应（不是错误，UI 应显示灰提示而非红错误）
→ {"method":"session/done"}
← {"result":{"merged":false,"reason":"no-new-content",
             "message":"没有新的讨论内容需要总结","current":"grep-ignore-case","bytes":0}}
```

子会话内再 explore 会返回 `-32000` `"子会话内不能再开分支"`（与源码 `ChildSession` 一致）。

**`done` 的两个后续行为**（照源码，客户端需知）：
- `merged:false`（无增量）时**不会退出分支**，`session.current` 仍含 `/`，用户可继续在分支里工作；
- `merged:true` 时自动退回父会话，`session.current` 不再含 `/`。

**`session/delete` 的可用范围**：子会话内不可删除（源码 `ChildSession` 没有 delete），
调用会返回 `-32000 "当前状态不可用 /rm"`；需先 `session/switch` 回父会话。

### 3.10 `config/set`

```jsonc
→ {"method":"config/set","params":{"key":"model","value":"deepseek-v4-pro"}}
← {"result":{"ok":true,"current":"deepseek-v4-pro"}}
```

`key` 取值：`model` / `thinking` / `thinkback` / `goal` / `plugin`
- `goal` 的 value 是 `{"enabled":true,"maxRounds":10}`
- `plugin` 的 value 是 `{"name":"serial","enabled":false}`

**错误码区分**（实测）：
- 未知 `key`、缺 `value`、value 类型不对 → `-32602`（参数非法）
- `key` 合法但 value 内容不存在（如 `thinking` 档位不存在、`plugin` 名字不存在）→ `-32000`（业务错误）

### 3.11 `confirm/reply`

回答后端的确认请求（删除命令 / git 命令的安全检查，见 §4.13 `confirm/request`）。

```jsonc
→ {"jsonrpc":"2.0","id":9,"method":"confirm/reply","params":{"text":"y"}}
← {"jsonrpc":"2.0","id":9,"result":{"accepted":true,"text":"y"}}
```

- `text` 必须是 `"y"`（确认）或 `"n"`（取消）；后端按 `y`/`yes`（忽略大小写）判定确认
- **不能用 `chat/send` 代替**：确认发生在轮次中途，此时 `chat/send` 会被 `-32000 busy` 拒绝
- 若当前没有待确认操作，投递的文本会留在输入队列里，等下一次 `read_input` 被消费

### 3.12 `shutdown`

```jsonc
→ {"method":"shutdown"}
← {"result":{"bye":true}}
```

响应后进程自行退出。

---

## 4. 服务端 → 客户端（通知）

所有事件都是 `{"jsonrpc":"2.0","method":"...","params":{...}}`。

一轮对话的事件顺序：

```
turn/begin
  thinking/begin? …thinking/delta × N …thinking/end?
  stream/delta × N
  tool/call → tool/result（可多个，工具间可并行）
  todo/update?
  compress/begin → compress/end?
  verify/start → verify/result?
  stats
turn/end
（turn/end 之后可能来 session/saved × 2：自动保存的后台线程发出，见 §4.14）
```

### 4.1 `turn/begin`

```jsonc
{"method":"turn/begin","params":{"round":13,"goal":{"enabled":true,"current":2,"max":10}}}
```

### 4.2 `stream/delta`

模型正文的增量文本。客户端直接追加到当前助手消息。

```jsonc
{"method":"stream/delta","params":{"text":"参数定义在 "}}
```

### 4.3 `thinking/begin` / `thinking/delta` / `thinking/end`

思考内容的流式推送（**终端形态不显示，仅 RPC 下发**）。

```jsonc
{"method":"thinking/begin","params":{}}
{"method":"thinking/delta","params":{"text":"先确认参数在哪一层"}}
{"method":"thinking/end","params":{"chars":616,"durationMs":3200}}
```

> **出事件的条件**（实测结论）：
> 1. 当前模型确实会产出思考块。实测 `deepseek-v4-pro` 有（28 个 delta / 91 字），
>    `deepseek-flash` 没有——那是模型侧行为，不是协议或实现缺陷。
> 2. **思考的「捕获」与「回传」已解耦**：`智能体.思考.回传`（即 `initialize.thinking.passback`，
>    默认 `false`）只决定请求里要不要把思考发回模型，**不影响**这三个事件——关闭回传时思考照样
>    下发、照样存进会话 json（见 §3.7 `messages[].thinking`）。
>
> 客户端不要把「没有 thinking 事件」当成异常，也不要把它当作「回传已开启」的依据。

### 4.4 `tool/call`

工具开始执行。

```jsonc
{"method":"tool/call","params":{
   "id":"tc_1",
   "name":"Read",
   "label":"读取",
   "dispatch":"readonly",
   "summary":"narnat_agent/tools/grep/__init__.py",
   "arguments":{"file_path":"narnat_agent/tools/grep/__init__.py"}
 }}
```

`label` / `dispatch` / `summary` 全部来自源码 `CAPABILITY`，客户端不自行推导。

### 4.5 `tool/result`

```jsonc
{"method":"tool/result","params":{
   "id":"tc_1",
   "ok":true,
   "durationMs":11,
   "output":"226→...",
   "failed":false,
   "diff":{"added":4,"removed":1,"lines":[
     {"type":"del","no":229,"text":"\"type\": \"string\""},
     {"type":"add","no":229,"text":"\"type\": \"string\","}
   ]}
 }}
```

- `failed:true` 表示工具执行失败（`_show_tool_failed` 的触发条件），`ok` 在失败时为 `false`
- `diff` 仅 Edit/Write 有
- `output` 可能被截断，完整内容已在服务端落盘

### 4.6 `todo/update`

```jsonc
{"method":"todo/update","params":{"todos":[
   {"content":"读 grep 参数定义","status":"completed"},
   {"content":"新增 ignore_case 参数","status":"in_progress"}
 ]}}
```

### 4.7 `verify/start` / `verify/result`

```jsonc
{"method":"verify/start","params":{}}
{"method":"verify/result","params":{
   "verdict":"pass","blocks":3,"limit":3,"durationMs":6500,"gaps":[]
 }}
```

`verdict` 取值：`pass` / `uncertain` / `fail` / `forced` / `skip`（与 `show_verify_result` / `show_verify_skip` 一一对应）

> 仅**目标模式**（`config/set` 把 `goal.enabled` 设 true）下，AI 调 `GoalComplete` 后才会触发。
> 本轮无文件改动时 `verdict=skip`、`blocks=0`、`durationMs=0`；
> 有文件改动时会真调验证器，实测 `verdict=pass, blocks=2, limit=2, durationMs=3108`。

### 4.8 `compress/begin` / `compress/end`

```jsonc
{"method":"compress/begin","params":{}}
{"method":"compress/end","params":{"ok":null,"before":null,"after":null,"durationMs":12400}}
```

> `ok` / `before` / `after` **当前恒为 null**：源码 `begin_compressing()` /
> `end_compressing()` 无参数，拿不到压缩前后条数与成败，只有墙钟耗时可用。
> 要拿到真实数值需要改 `narnat_agent/ui` 的接口，属于后续可选项。

### 4.9 `bg/submit`

后台任务提交（Shell 的 `background` 参数）。

```jsonc
{"method":"bg/submit","params":{"slot":"bg1","logPath":"C:\\...\\bg1.log"}}
```

> 实现方式：Shell 的 background 提交没有专用回调，服务端从 `tool/result.output`
> 文本里正则识别 `bgN 已提交` 与 `结果: …/bgN.log` 后补发。实测可拿到
> `{"slot":"bg1","logPath":"C:\\Users\\...\\narnat_bg_3kjizl9m\\bg1.log"}`。
> 若上游改动了这段文案，`slot` 匹配不到就不会发此事件、`logPath` 可能为 null。

### 4.10 `stats`

每轮结束时下发，字段与 `ui_design.show_stats` 一致。

```jsonc
{"method":"stats","params":{
   "inputTokens":12300,"outputTokens":456,"cacheRatio":0.8,
   "thinkingEffort":"高","contextRatio":0.12,"maxOutputTokens":8000,
   "cost":0.0123,"balance":9.99
 }}
```

### 4.11 `turn/end`

一轮结束，客户端据此把「停止」按钮切回「发送」。

```jsonc
{"method":"turn/end","params":{"reason":"done"}}
```

`reason` 取值：`done`（正常结束）/ `cancelled`（被打断）/ `error` / `goal_complete` / `round_limit`

### 4.12 `error`

非致命的运行时错误（致命错误走 `turn/end` + `reason:"error"`）。

```jsonc
{"method":"error","params":{"message":"余额查询失败","detail":"timeout"}}
```

### 4.13 `confirm/request`

后端需要用户确认才能继续（`rm` 等删除命令、`git` 命令，受
`智能体.Git免确认` / `rm免确认` 控制）。**轮次没有结束**，后端在等 `confirm/reply`。

```jsonc
{"method":"confirm/request","params":{"prompt":"  确认执行此命令? [y/N]: "}}
```

- 客户端应渲染成可点的确认条（确认 / 取消），分别回 `confirm/reply {"text":"y"}` / `{"text":"n"}`
- 用户不答则后端一直等待；`chat/send` 在此期间会被 `busy` 拒绝
- 超时不做自动处理：取消由用户显式操作（安全侧 fail-closed）

### 4.14 `session/saved`

自动保存落盘 / 会话状态切换的通知（方向：后端 → 客户端）。只在 `会话.自动保存` 开启且过了
`自动保存Token量` 门槛时出现，由自动保存的后台线程发出，因此时间点在 `turn/end` **之后**，
不在 §4 开头的轮次序列里。

```jsonc
{"method":"session/saved","params":{"name":"简短确认回复","current":null,"switched":false}}
{"method":"session/saved","params":{"name":"简短确认回复","current":"简短确认回复","switched":true}}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | string | 自动保存的会话名（后端自动命名） |
| `current` | string \| null | 切换后的当前会话路径（子会话为 `父/子`）；未切换时为 `null` |
| `switched` | boolean | 是否为「状态已切换」的那一次：`false` = 已写盘、进程内状态尚未切换；`true` = 状态已切到该会话 |

> **同一份自动保存会发两次**：先 `switched:false`（写盘完成），后 `switched:true`（状态切换完成），
> 实测两条几乎同时到达（录制流里时间戳相同，见 `nn-tasks/reqA-backend/probe.jsonl`）。客户端应**幂等**
> 处理——按 `name`（必要时加 `switched`）去重，重复收到不要重复插系统提示、不要重复拉 `session/list`。
> 状态切换被跳过时（写盘后用户已 `session/new` 或切到别的会话）只发 `switched:false` 那一条；
> 写盘失败则两条都不发。

---

## 5. 与终端形态的差异（有意为之）

| 行为 | 终端 | RPC |
|---|---|---|
| 思考内容 | 不显示（无法折叠） | `thinking/*` 事件下发，UI 折叠显示 |
| 工具调用 | 打印一行 `[label] summary` | `tool/call` + `tool/result` 结构化下发 |
| 会话列表 | 格式化字符串带 `├──` | `session/list` 返回结构化树 |
| spinner | 终端动画 | 不发事件，客户端自己做「思考中…」 |
| 统计栏 | 每轮打印一行 | `stats` 事件，客户端渲染到状态行 |
| `/exit` | `os._exit(0)` | 走 `shutdown`，不退出进程 |
| ANSI 颜色 | 有 | **完全不下发**（客户端自己上色） |

---

## 6. 兼容性

- `initialize` 返回的 `protocol` 是协议版本号，当前为 `1`
- 客户端发现 `protocol` 大于自己支持的版本时，应提示用户升级扩展
- 新增字段向后兼容（客户端忽略未知字段）；删除/改名字段必须提升 `protocol`

---

## 7. 事件验证对照表

每个事件都有可复跑的验证脚本（`vscode-extension/test/`），避免「文档写了但没实现」。

| 事件 | 验证脚本 | 实测证据 |
|---|---|---|
| `turn/begin`、`turn/end` | `e2e-real.js` | `reason=done` / `cancelled` |
| `stream/delta` | `e2e-real.js`、宿主 `chat.js` | 拼接文本非空；webview 回执含 assistant |
| `thinking/*` | `e2e-thinking.js`（三工况） | v4-pro：28 delta / 91 字；flash：0（模型侧） |
| `tool/call`、`tool/result` | `e2e-real.js`、`e2e-events.js` | id 配对；diff `added=1 removed=1` |
| `todo/update` | `e2e-events.js` | 3 条计划（alpha/beta/gamma） |
| `verify/start`、`verify/result` | `e2e-verify.js` | skip；改文件后 `pass, blocks=2, durationMs=3108` |
| `compress/begin`、`compress/end` | `nn-tasks/verify-compress.py` | durationMs 为数字；ok/before/after 为 null |
| `bg/submit` | `e2e-events.js` | `{"slot":"bg1","logPath":"…bg1.log"}` |
| `stats` | `e2e-real.js` | `inputTokens/outputTokens/cacheRatio/cost` |
| `error` | `nn-tasks/verify-compress.py` | `{"message":"测试错误"}` |
| `confirm/request` | `nn-tasks/verify-confirm.py`、宿主 `ui.js` | 通知含 prompt；点取消后 `confirm/reply{yes:false}` 且退出运行态 |
| `session/saved` | `nn-tasks/reqA-backend/rpc_drive.py` | `{"name":"简短确认回复","current":null,"switched":false}` → `{"…","current":"简短确认回复","switched":true}`（见 `nn-tasks/reqA-backend/probe.jsonl`） |
