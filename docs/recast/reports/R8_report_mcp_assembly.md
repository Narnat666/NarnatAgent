# R8 现状调研报告：MCP 客户端 / 日志 / 组装层 / 入口

> 调研范围：`narnat_agent/mcp/__init__.py`、`narnat_agent/mcp/client.py`、`narnat_agent/assembly.py`、
> `narnat_agent/logger.py`、`main.py`、`narnat_agent/__init__.py`（共 6 文件，1105 行）。
> 全部逐文件通读；行号以本报告写作时的源码为准（可用 `wc -l` 复核行数：343/342/231/109/73/7，与任务清单一致）。
> 「被依赖」清单在 `narnat_agent/` 包内 + 根目录 `main.py` 精查；`tool_exp/`（历史实验/复现脚本目录）、
> `subagent_test/` 为库外引用，单列标注，不作为生产依赖。
> 标注「未验证」的条目为静态推断，未实际运行代码验证（任务边界禁止运行）。

---

### narnat_agent/mcp/__init__.py（343行）

**职责**：MCP 运行时连接管理——把 AI 给的服务器配置变成可持续的 stdio 通道，负责 connect/disconnect、工具热注册与注销、断线自动重连提示、子进程回收（含 atexit 兜底）。

**对外接口**：
- `__all__ = ["McpManager", "McpError", "TOOL_PREFIX"]`（L26）
- `TOOL_PREFIX = "mcp__"`（L23）；`_MAX_NAME_LEN = 64`（L20）；`_HASH_LEN = 12`（L21）；`_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")`（L22）；`_MAX_SERVERS = 8`（L24）
- `McpManager.__init__(self, logger=None)`（L36）：保存可选 logger，初始化 4 个状态容器 + 锁 + 回调槽
- `cleanup(self) -> None`（L47）：关闭全部连接并清空登记表，程序退出路径调用
- `set_tool_sinks(self, on_add, on_remove) -> None`（L64）：注入工具定义热更新回调（on_add(definitions) / on_remove(tool_names)）
- `connected(self) -> list`（L73）：返回 `[{"name", "tool_count"}]`
- `connect(self, name: str, spec: dict) -> tuple`（L82）：运行时连接服务器，返回 `(注册工具数, [LLM工具名,...])`
- `disconnect(self, name: str) -> int`（L153）：断开并注销工具，返回注销数；name 为 "all" 时断开全部
- `call_tool(self, server_name: str, tool_name: str, arguments: dict, timeout: float) -> str`（L187）：经通道转发一次工具调用（含断线自动重连）
- `_reconnect(self, server_name: str) -> None`（L211）：用连接时记录的配置重建通道（内部）
- `_connect(self, cfg)`（L247）：启动进程 → 握手 → 列工具（内部）
- `_build_entry(self, cfg, client, tool, used_names)`（L266）：MCP 工具 → registry 条目 `(名称, LLM定义, 实现)`，被过滤返回 None（内部）
- `_log(self, msg: str) -> None`（L297）：logger 转发（内部）
- `_sanitize_name_part(name: str) -> str`（L305，模块级）：名称片段清洗，清空时哈希兜底
- `_make_unique_name(server_name: str, tool_name: str, used_names: set) -> str`（L313，模块级）：生成模型可见工具名
- `_make_tool_impl(manager: McpManager, server_name: str, tool_name: str, timeout: float)`（L336，模块级）：生成 registry 工具实现闭包，内含 `_call(**kwargs)`（L338）

**依赖**（本文件 import 的内部模块，附行号）：
- L15 `from .client import McpError, McpStdioClient`
- L16 `from ..config.loader import parse_mcp_server`
- L17 `from ..tools.exec_signal import error_line`
- L117 `from ..tools.registry import get_tool_names, register_dynamic_tools`（函数内延迟导入）
- L172 `from ..tools.registry import unregister_dynamic_tools`（函数内延迟导入）
- 标准库：L10 atexit、L11 hashlib、L12 re、L13 threading

**被依赖**（grep 方及行号）：
- `narnat_agent/assembly.py:59` `from .mcp import McpManager`（L60 实例化 `McpManager(logger)`）
- `narnat_agent/tools/mcp_tool/__init__.py:7` `from ...mcp import McpError`（L69 捕获、L70 组错误行）
- 库外（tool_exp 实验）：`demo_mcp_flow.py:16`、`demo_persistence.py:19`、`test_mcp.py:21`、`test_mcp_runtime.py:22`、`verify_mcp_console_isolate_live.py:135`（均 `from narnat_agent.mcp import McpManager`）
- `TOOL_PREFIX` 无代码引用方（仅 README.md:355 文字描述）；`tool_dispatcher.py:242/404/416` 用字面量 `"mcp__"` 而非本常量（见补丁痕迹）

**状态**：
- 模块级全局：`_MAX_NAME_LEN`、`_HASH_LEN`、`_NAME_SAFE_RE`（编译后不可变）、`TOOL_PREFIX`、`_MAX_SERVERS`——全为只读常量，无可变全局
- 类变量：无
- 实例状态（`McpManager.__init__` L37-45）：`_logger`（L37）、`_clients`（L38，服务器名→McpStdioClient）、`_server_tools`（L39，服务器名→[LLM工具名]）、`_specs`（L40，服务器名→连接时原始 spec）、`_lock`（L41 threading.Lock）、`_closed`（L42 bool）、`_atexit_registered`（L43 bool）、`_tool_sink_add`/`_tool_sink_remove`（L44-45 回调）

**行为要点**：
1. `cleanup()`：锁内置 `_closed=True`、取出并清空 `_clients`（L49-53），锁外逐个 `client.close()`，异常全吞（L54-58）；不注销 registry 中的动态工具、不调用 sink 回调（注销只发生在 disconnect 路径）。
2. `set_tool_sinks(on_add, on_remove)`：直接保存两个回调（L70-71），不校验可调用性；不接回调时仅 registry 生效（文档 L68）。
3. `connected()`：按 `_clients` 插入序返回（dict 保序），tool_count 取 `_server_tools` 长度（L78-80）。
4. `connect()` 输入归一：`name = str(name or "").strip()`，空名抛 `McpError("缺少服务器名（name）")`（L90-92）；`parse_mcp_server(name, dict(spec or {}, 启用=True))` 返回 None 抛 `McpError("配置非法")`（L93-95）——注意强制覆盖 `启用=True`。
5. 并发上限：锁内若该名未连接且 `len(self._clients) >= 8`，抛 `McpError("已达并发上限(8个)，当前已连接: {、连接的名列表}。请先 disconnect 不需要的服务器")`（L97-105）。
6. 幂等复用：同名已存在且 `client.alive` → 锁内直接返回已注册的 `(数量, 工具名列表)`（L106-108）。
7. 死连接清理：同名存在但进程已死 → 先 `self.disconnect(name)`（注销旧工具、关死进程）再重建（L109-112）。
8. 连接动作不持锁：`self._connect(cfg)`（spawn+握手+列工具，可能耗时）在锁外执行（L114-115）。
9. 原子注册（赢家路径）：锁内二次检查 `_closed`（若退出则 `client.close()` + 抛错，L119-121）；`_clients.get(name) is None` 时：取 `get_tool_names()` 为已用名集合 → 逐个 `_build_entry` → 登记 `_clients/_server_tools/_specs` → `register_dynamic_tools(entries)` → `_tool_sink_add([e[1] for e in entries])`（L122-138）。「检查+登记+注册」在同一锁内完成，与 disconnect 的注销互斥。
10. 并发同名落败路径：`_clients.get(name)` 已存在时，记录 `existing_tools`，锁外关闭本次新建的 client，返回已存在工具清单（L139-144）。
11. atexit 兜底：首次成功连接后注册 `atexit.register(self.cleanup)`，`_atexit_registered` 置 True（L147-149）——覆盖"不走 agent finally 的异常退出"。
12. 连接成功日志：`_log(f"{name}: 已连接，注册 {len(entries)} 个工具")`（L150）；返回 `(len(entries), [e[0] for e in entries])`（L151）。
13. `disconnect()` 归一化后，"all"/"*"/"全部"（`name.lower()` 比较，L156）→ 快照 `_clients.keys()` 后逐个递归 disconnect 并累加返回值（L157-162）。
14. 单服 disconnect：锁内 pop `_clients/_server_tools/_specs`（L165-167）；client 为 None 抛 `McpError(f"服务器 {name} 未连接")`（L168-169）；`unregister_dynamic_tools(tool_names)` 与 `_tool_sink_remove(tool_names)` 均在锁内（L170-175）。
15. 锁外 `client.close()`（吞异常，L176-179），日志 `{name}: 已断开，注销 N 个工具`（L180），返回注销数（L181）。
16. `call_tool()`：锁内查 `_closed`（抛 `McpError("程序正在退出")`，L194-196）与 client（None 抛 `服务器 X 未连接`，L198-199）；`not client.alive` → 先 `_reconnect` 再抛 `McpError("连接曾中断，已自动重连，请重试本次调用")`（L200-202）——**不代 AI 重跑调用**。
17. 调用期断线：`client.call_tool` 抛 McpError 时，若 `client.alive` 仍真则原样重抛（服务端自身错误/超时，L205-207）；否则 `_reconnect` 后抛"请重试本次调用"（L208-209）。
18. `_reconnect()`：锁内取 `_specs` 副本（L213-214）；spec 空 → `McpError(f"服务器 X 无配置，无法自动重连")`（L215-216）；`parse_mcp_server` 失败或 command 空 → `McpError(f"服务器 X 配置不完整，无法自动重连")`（L217-219）；`_connect` 新建（L220）；锁内若 `_clients.get(server_name) is None`（重连期间被 disconnect/cleanup）→ 关闭新通道并抛 `McpError(f"服务器 X 已断开，无需重连")`（L221-235）；否则替换 `_clients[name]` 并关旧 client、日志"连接中断，已自动重连"（L236-241）。`_reconnect` 不改 `_server_tools`（工具注册不变，只换进程）。
19. `_connect()`：command 空校验抛 `McpError("未配置启动命令（\"命令\"/\"command\"）")`（L249-250）；构造 `McpStdioClient(cfg.name, cfg.command, cfg.args, env=cfg.env, cwd=cfg.cwd, logger=self._logger)`（L251-254）；`initialize(cfg.startup_timeout)` + `list_tools(cfg.startup_timeout)`（同一超时，L256-257）；McpError 时 `client.close()` 后重抛（L258-260）；成功日志含 serverInfo.name/version（L261-263）；返回 `(client, tools)`（L264）。
20. `_build_entry()` 过滤链：非 dict → None（L268-269）；原始名为空 → None（L270-271）；白名单非空且不在名单 → None（L274-275）；在黑名单 → None（L276-277）。
21. 工具名：`_make_unique_name(cfg.name, raw_name, used_names)`（L279）；description 空时兜底 `f"MCP工具 {raw_name}（来自 {cfg.name}）"`（L280-281）；`inputSchema` 非 dict 或空 → `{"type": "object", "properties": {}}`（L282-284）；产出 OpenAI 风格 `{"type": "function", "function": {name, description, parameters}}`（L286-293）；实现由 `_make_tool_impl(self, cfg.name, raw_name, cfg.tool_timeout)` 生成（L294）——实现闭包绑定**服务端原始工具名**（非清洗后名）。
22. `_sanitize_name_part()`：`[^A-Za-z0-9_-]` 全部替换为 `_` 并 `strip("_")`；清空时 `"srv_" + sha1(utf-8)[:8]`（L307-310）。
23. `_make_unique_name()`：`base = "mcp__" + 清洗(服务器) + "__" + 清洗(工具)`；`len(base) <= 64 且未被占用` 直接返回并登记（L321-323）；否则以 `identity = f"{server}\0{tool}"` 计算 `suffix = "_" + sha1(raw)[:12]`（13 字符），`name = base[:51] + suffix`（即总长 ≤64）循环尝试直到未占用（L324-333）。
24. `_make_tool_impl()`：闭包捕获 manager/server_name（原始名）/tool_name（原始名）/timeout；`_call(**kwargs)` 调 `manager.call_tool(...)`，捕获 `McpError` → `error_line(f"MCP工具 {server_name}/{tool_name} 调用失败: {e}")`（L338-342）——失败转化为带随机标签的错误行，不向上抛。
25. `_log()`：logger 非空时 `logger.info("mcp", msg)`，异常吞（L297-302）。

**边界/异常行为**：
1. 并发上限 8（L24、L101）：上限判定用 `len(self._clients)`，已连接同名服务器不计入（幂等路径先返回）。
2. `connect` 强制 `启用=True`（L93）：spec 里的"启用"字段被覆盖，即使 AI 传 `"启用": false` 也会连接（`parse_mcp_server` 的 enabled 布尔容错在此被绕过——因为传参恒真）。
3. atexit 注册无锁（L147-149）：并发 connect 可能重复注册 `self.cleanup`；重复调用幂等无副作用，但属竞态窗口。
4. `disconnect("all")` 循环内不捕获异常（L160-162）：并发场景下若某服务器已被断开，递归 `disconnect` 抛 `McpError("未连接")` 会中断整批，已断开的生效、后续的跳过。
5. `_make_unique_name` 的 `attempt` 循环无硬上限（L326-333）：理论上依赖 used_names 增长收敛；同 identity 冲突时靠 attempt 递增改变哈希（实际不可能无限冲突）。
6. 工具名与内置工具冲突：registry 侧静默跳过（`tools/registry.py:81-82`），但 `_server_tools` 仍全量记录该名（L134）→ disconnect 时该名会传入 `unregister_dynamic_tools`（对内置无影响，仅动态表清理），登记的"已注册工具数"也可能多于 registry 实际新增数。
7. 平台相关：子进程进程组/控制台隔离在 client.py 实现（见该节），本文件无平台分支。
8. 编码相关：本文件无编码处理；“纯中文服务器名”走 `_sanitize_name_part` 哈希兜底（L308-310）。
9. 超长 spec/args 无长度校验（原样透传至 client）。
10. `call_tool` 的 `timeout` 不校验类型/正负（原样传给 client.request，格式化 `{timeout:g}` 在 client.py:186）。
11. `entries` 变量只在上锁的赢家分支赋值（L128-134），却在 L150-151 使用——现有控制流下若 `existing_tools is not None` 已在 L142-144 返回，故安全；属"跨分支作用域"脆弱写法。
12. 重连语义（L202、L209）：断线只修通道并要求 AI 重试，绝不自动重跑调用（防写操作/仿真重复执行）。

**补丁痕迹**：
1. 【高】`connect()` 单函数 70 行（L82-151）承担 6 项职责：输入归一/配置解析/并发上限/幂等判断/死连接清理/锁内原子注册/atexit 注册——职责混杂，且嵌套 4 处独立锁段与 2 处函数内 import。
2. 【中】mcp ↔ tools 双向依赖：本模块 import registry（L117、L172 延迟导入），而 registry 的 MCP 工具定义（tools/registry.py:60）与 mcp_tool 模块又依赖 mcp 包（tools/mcp_tool/__init__.py:7）——靠函数内 import 规避循环。
3. 【中】工具名前缀常量重复：`TOOL_PREFIX` 仅本模块使用，判定侧（tool_dispatcher.py:242/404/416）硬编码字面量 `"mcp__"`——命名约定跨层复制，改一处易漏。
4. 【中】`_atexit_registered` 无锁读写（L43、L147-149）——并发 connect 下的竞态窗口（重复注册）。
5. 【中】`disconnect("all")` 递归自身且异常中断批处理（L160-162）——批量操作无 per-server 容错。
6. 【中】`_server_tools` 记录与 registry 实际注册可能不一致（L134 vs registry.py:80-84 静默跳过重名）——"登记"与"生效"两个真相源。
7. 【中】魔法值硬编码：哈希长度 12/8（L21、L309）、截断重算 `base[:_MAX_NAME_LEN - len(suffix)]`（L329）、并发上限 8（L24，注释称"与后台任务槽位同源"但实际各自硬编码）。
8. 【低】`_log` 双层 try/except 吞异常（L297-302）——日志失败不可见。
9. 【低】注释中的历史/对标包袱："对标 codex 的 normalize_tools_for_model"（L316-317）、"与 Terminal 同构"（L30）、"与后台任务槽位同源"（L24）。
10. 【低】无类型校验的参数（spec 任意 dict、timeout 任意值）——信任边界全靠 `parse_mcp_server` 兜底（R7 范围）。

**可测性**：
- 可独立单测：`_sanitize_name_part`（纯函数：合法字符/非法字符/纯中文/空串）；`_make_unique_name`（唯一性、64 上限、重名哈希后缀、used_names 副作用——已有 `tool_exp/zz_hash_verify.py` 先例）；`_build_entry`（duck-typing 的 cfg/client 伪对象即可测过滤链与 schema 兜底）；`set_tool_sinks` + 伪造 sink 验证回调转发（`tool_exp/mcp_test/test_mcp_runtime.py:44-52` 已有 FakeSinks 模式）；`connected`/`disconnect` 错误分支（可注入假 client 到 `_clients`）。
- 需要集成测试：`connect`/`call_tool`/`_reconnect` 全链路（真实子进程 + 真实 JSON-RPC；mock 服务端已存在 `tool_exp/mcp_test/mock_mcp_server.py`，`test_mcp_runtime.py` 覆盖热更新/上限/断线提示/并发上限）；atexit 回收（需独立进程观测子进程存活）。
- 无法自动化：并发同名 connect 的锁序竞态（需压测/时序注入）；解释器退出时 atexit 的实际时序；真实服务端（Sysplorer/MWorks）崩溃后的重连时机。

---

### narnat_agent/mcp/client.py（342行）

**职责**：单个 MCP stdio 服务端连接——JSON-RPC 2.0 行帧协议（握手/列工具/调用）、逐行双策略解码、stderr 泵线程、进程组与控制台隔离、优雅关闭→强杀。

**对外接口**：
- 常量：`PROTOCOL_VERSION = "2025-06-18"`（L28）、`CLIENT_NAME = "narnat-agent"`（L29）、`CLIENT_TITLE = "Narnat Agent"`（L30）、`CLIENT_VERSION = "1.0"`（L31）、`_MAX_LIST_PAGES = 50`（L34）
- `McpError(Exception)`（L37）：MCP 传输/协议错误
- `_decode_line(raw: bytes) -> str`（L41，模块级）：行解码（UTF-8 → GBK → replace）
- `McpStdioClient.__init__(self, name, command, args=(), env=None, cwd=None, logger=None)`（L64）：spawn 子进程 + 启动两个 daemon 线程
- `initialize(self, timeout: float) -> dict`（L124）：initialize 握手 + notifications/initialized
- `list_tools(self, timeout: float) -> list`（L138）：列出全部工具（自动翻页 nextCursor，上限 50 页）
- `call_tool(self, tool_name: str, arguments: dict, timeout: float) -> str`（L151）：调用工具，返回格式化文本（isError → 错误行）
- `notify(self, method: str, params: dict = None) -> None`（L163）：发送通知（无 id）
- `request(self, method: str, params: dict, timeout: float) -> dict`（L170）：发送请求并等待响应；超时/服务端错误/进程退出抛 McpError
- `alive` property（L199-202）：进程存活且未置 `_dead`
- `close(self, grace: float = 3.0) -> None`（L204）：先关 stdin 让服务端自退，超时强杀
- `_send(self, msg: dict) -> None`（L224）、`_terminate(self, proc) -> None`（L237）、`_reader_loop(self) -> None`（L255）、`_stderr_loop(self) -> None`（L291）、`_dispatch_response(self, msg: dict) -> None`（L301）、`_reply_error(self, req_id, code: int, message: str) -> None`（L307）、`_log(self, msg: str) -> None`（L314）（内部）
- `_format_content(result: dict) -> str`（L322，模块级）：content 块拼接 → 文本

**依赖**（本文件 import 的内部模块，附行号）：
- L25 `from ..tools.exec_signal import error_line`
- 标准库：L16 json、L17 os、L18 signal、L19 subprocess、L20 sys、L21 threading、L22 `from io import TextIOWrapper`、L23 `from queue import Empty, Queue`
- 内部依赖仅 exec_signal 一处（本模块是包内叶子）

**被依赖**（grep 方及行号）：
- `narnat_agent/mcp/__init__.py:15` `from .client import McpError, McpStdioClient`（L251 构造、L37 用 McpError 定义）
- 库外（tool_exp 实验）：`tool_exp/mcp_test/demo_mcp_flow.py:17`、`demo_persistence.py:20`（`from narnat_agent.mcp import client as mcp_client`）；`tool_exp/verify_mcp_console_isolate_live.py:122`（`import narnat_agent.mcp.client as mc`）

**状态**：
- 模块级全局：上述 5 个常量，全只读，无可变全局
- 类变量：无
- 实例状态：`name`（L65）、`_logger`（L66）、`_proc`（L67）、`_dead`（L68 bool）、`_next_id`（L69 int，从 1 起）、`_pending`（L70 dict：请求 id→Queue）、`_pending_lock`（L71）、`_write_lock`（L72，主线程请求 + reader 线程回服务端请求共用 stdin）、`_stdin`（L110 TextIOWrapper）、`_stdout`（L112 原始字节流）、`_stderr`（L113）
- 线程：`mcp-{name}-reader`（L115-116）、`mcp-{name}-stderr`（L117-118），均 daemon

**行为要点**：
1. 环境合并：`cmd_env = dict(os.environ)`，`env` 非空时 `update({str(k): str(v)})`（L74-76）——父环境全量继承 + 覆盖。
2. 平台隔离（Windows）：`creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（L79-89），理由注释（L80-86）：不接收本进程 Ctrl+C 事件；独立无窗口控制台使服务端原生栈直写（Message(0)/Socket 等）不污染 narnat 终端，且 FreeConsole/AttachConsole 挂回被 `GetConsoleWindow()==0` 守卫跳过。
3. 平台隔离（POSIX）：`start_new_session=True`（L90-92，等同 setsid，Ctrl+C 不波及服务端）。
4. spawn：`subprocess.Popen([command, *[str(a) for a in args]], stdin/stdout/stderr=PIPE, cwd=cwd or None, env=cmd_env)`（L94-103）；`OSError` → `McpError(f"启动失败: {e}")`（L104-105）。
5. 管道包装：写侧 `TextIOWrapper(encoding="utf-8", errors="replace", newline="\n", write_through=True)`——帧严格 `\n` 分隔、不带 `\r\n`（L107-111）；读侧保留原始字节（L112）以支持逐行双策略解码（L108-109 注释说明 TextIOWrapper 无法回退）。
6. 两个 daemon 线程立即启动（L115-118）。
7. `initialize()`：`request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {name, title, version}}, timeout)`（L126-134）→ `notify("notifications/initialized")`（L135）→ 返回服务端 result（L136）。
8. `list_tools()`：循环 ≤50 页；首轮 `params = {}`（cursor 为 None 时不带 cursor 键），后续 `{"cursor": cursor}`（L142-144）；`tools.extend(result.get("tools") or [])`（L145）；`nextCursor` 空/缺省即 break（L146-148）。
9. `call_tool()`：`request("tools/call", {"name": tool_name, "arguments": arguments or {}})`（L153-156）；`text = _format_content(result)`（L157）；`isError` 真 → 返回 `error_line(text or '工具返回错误')`（L158-160）；normal → `text if text else "(工具无输出)"`（L161）。
10. `notify()`：`{"jsonrpc": "2.0", "method": method}`，params 真值时携带（L165-168）。
11. `request()`：新建 Queue → `_pending_lock` 内查 `_dead`（真则抛 `McpError("进程已退出")`）、分配自增 id、登记 pending（L172-178）；发送 `{"jsonrpc": "2.0", "id", "method", "params": params or {}}`（L181-182）；`q.get(timeout=timeout)` → `Empty` 抛 `McpError(f"{method} 超时({timeout:g}s)")`（L183-186）；finally 移除 pending（L187-189）；`resp is None`（EOF 哨兵）→ `McpError("进程已退出")`（L191-192）；含 `"error"` 键 → `McpError(f"{method} 失败: {err.message or err}")`（L193-196）；否则返回 `resp.get("result") or {}`（L197）。
12. `alive`：`_proc is not None and _proc.poll() is None and not _dead`（L202）。
13. `close(grace=3.0)`：进程已退出直接返回（L207-208）；`self._stdin.close()`（OSError 吞，L209-212）；`proc.wait(grace)` 成功即返回（L213-215）；`TimeoutExpired` → `_terminate(proc)`（L216-218）。
14. `_send()`：`json.dumps(msg, ensure_ascii=False) + "\n"`（L226）；`_write_lock` 内查 `_dead`（抛 `McpError("进程已退出")`，L228-229）；写失败（OSError/ValueError）→ `_dead = True` 并抛 `McpError(f"写入失败: {e}")`（L230-235）。
15. `_terminate()`：Windows `proc.kill()`（L240-241）；POSIX `os.killpg(os.getpgid(pid), SIGTERM)` → 2 秒未退 → `SIGKILL`（L242-247）；`OSError` 吞（L248-249）；最后 `proc.wait(2)` 兜底（L250-253）。
16. `_reader_loop()`：`stdout.readline()` 循环（L259-262）；`_decode_line(raw).strip()`，空行跳过（L263-265）；`json.JSONDecodeError` → 日志 `忽略非JSON输出: {line[:200]}` 并继续（L266-270）；非 dict 消息忽略（L271-272）；响应判定 `"id" in msg and ("result" in msg or "error" in msg)` → `_dispatch_response`（L273-274）；`"method" in msg` 且带 id → 回 `-32601 Method not found` + 日志"服务端请求 X 未实现，已回绝"（L276-279）；带 method 无 id（通知）→ 日志 `通知 {method}`（L280-281）；`OSError` → 日志"读取中断"（L282-283）；finally 置 `_dead=True` 并对 pending 快照广播 `None`（EOF 哨兵唤醒全部等待者，L284-289）。
17. `_stderr_loop()`：逐行解码 trim，非空 → 日志 `stderr: {line[:500]}`（L293-297）；OSError/ValueError 吞（L298-299）。
18. `_dispatch_response()`：按 id 查 pending，命中则 `q.put(msg)`（L301-305）。
19. `_reply_error()`：发送 error 响应，McpError 吞（L307-312）。
20. `_log()`：`logger.info(f"mcp.{self.name}", msg)`，异常吞（L314-319）。
21. `_format_content()`：逐 content 项——非 dict → `str(item)`（L326-328）；`type == "text"` → `text`（L330-331）；`"image"` → `[图片: {mimeType or '未知类型'}]`（L332-333）；`"resource"` → `resource.text` 或 `[资源: {uri}]`（L334-337）；其它 → `json.dumps(item, ensure_ascii=False)`（L338-339）；`parts` 为空且有 `structuredContent` → JSON 序列化兜底（L340-341）；`"\n".join(parts)`（L342）。
22. `_decode_line()`：utf-8 严格 → 失败且 `sys.platform == "win32"` 时 gbk → 再失败 utf-8 replace（L49-58）；注释说明与 `tools/bash._decode_output` 双策略一致、GBK 对 ASCII 解码与 UTF-8 相同故整行回退安全（L44-47）。

**边界/异常行为**：
1. 编码回退仅 Windows 走 GBK（L53-57）；Linux 直接 replace。
2. 超时文案 `f"{method} 超时({timeout:g}s)"`（L186）——`%g` 格式，整数超时显示为 `30s`。
3. `tools/list` 分页 50 页静默截断（L34、L142）——超限时返回已收集工具且无任何提示。
4. `readline()` 对超长行无长度限制（L260）——异常服务端可造成内存压力。
5. 无 id 且无 method 的消息（如 `{"id":1}` 裸 id）被静默忽略（L273-275 分支均不命中）。
6. 每条通知都会写 DEBUG 级日志（L281）——服务端高频通知会产生日志噪音（logger 侧无节流）。
7. 请求超时后 pending 由 finally 移除（L189）；reader 的 EOF 广播只覆盖快照时的 pending（L286-289），二者组合无泄漏。
8. `close()` 只关 stdin、不主动 terminate 进程，若服务端不退则 3 秒后强杀（L204-218）；`~grace` 期间 `alive` 仍可能为真。
9. `close()` 幂等：进程已退出时立即返回（L207-208）；重复 close 无异常。
10. `_send` 写失败立即置 `_dead`（L234）——后续请求全部快速失败（`request` L174-175 / `_send` L228-229）。
11. 服务端主动请求一律回 `-32601`（L278）——不实现 sampling/roots 等。
12. 平台分支：进程隔离（L79-92）、kill 策略（L240-247）、解码回退（L53-57）三处。
13. `initialize` 与 `list_tools` 共用启动超时（调用方 mcp/__init__.py L256-257 都传 `cfg.startup_timeout`），单页超时非累积（每页单独计时）。
14. stderr/非 JSON 输出各自截断 500/200 字符（L297、L269）。
15. 构造期若 `Popen` 成功但后续（L110-118）抛异常，无 finally 清理——子进程会残留（现状未发生，构造期无已知抛点）。

**补丁痕迹**：
1. 【中】`_reader_loop` 单函数 35 行兼 4 职责：响应分发/通知日志/服务端请求回绝/EOF 广播（L255-289）。
2. 【中】控制台隔离补丁：`CREATE_NO_WINDOW` 及其 7 行说明注释（L80-86）是针对"服务端原生栈直写控制台杂糅"问题的定点补丁，属平台怪癖知识资产。
3. 【中】`_decode_line` 的 GBK 回退（L53-58）——为 Windows 服务端（Sysplorer 等）打的编码补丁，注释自述对齐 bash 工具。
4. 【中】`_MAX_LIST_PAGES = 50` 静默截断（L34、L142-149）——防无限 cursor 的护栏，但截断不可观测（无日志）。
5. 【低】魔法值：grace 3.0（L204）、`_terminate` 内 2 秒两处（L245、L251）、尾部截断 200/500（L269、L297）。
6. 【低】通知逐条入日志（L281）——无采样/过滤。
7. 【低】`_log` 吞异常（L314-319）。
8. 【低】协议版本与客户端标识硬编码常量（L28-31）——无配置项。

**可测性**：
- 可独立单测：`_decode_line`（3 分支）、`_format_content`（text/image/resource/未知类型/structuredContent/空 content）；`request` 的超时与服务端 error 分支（mock `_send` + 手动穿 pending 队列）；`_terminate` Windows 分支（mock proc）；`alive` 属性（mock `_proc.poll`）。
- 需要集成测试：initialize/list_tools/call_tool 对真实子进程（mock 服务端 `tool_exp/mcp_test/mock_mcp_server.py` 可直接复用）；分页（需构造多页 mock）；优雅关闭→强杀路径；stderr 泵。
- 无法自动化：`CREATE_NO_WINDOW` 对真实 Sysplorer/MWorks 原生栈行为（`tool_exp/verify_mcp_console_isolate_live.py` 为半自动验证）；ESC/Ctrl+C 与子进程的 OS 级交互；服务端真死（kill -9）时 reader 的 EOF 时序。

---

### narnat_agent/assembly.py（231行）

**职责**：唯一组装点——按固定顺序构造全部运行时对象并注入依赖，返回持有所有引用的 `AssemblyResult`。

**对外接口**：
- `Assembly.build(cls, project_root: Optional[str] = None, debug: bool = False, headless: bool = False) -> 'AssemblyResult'`（L35-37，classmethod）：唯一 public 入口
- `AssemblyResult.__init__(self, config, logger, llm, context, message_list, msg_manager, session_mgr, ui, tool_context, dispatcher, stats, auto_save_mgr, compression_coordinator, agent_loop, mcp_manager=None)`（L213-216）：纯数据容器

**依赖**（本文件 import 的内部模块，附行号）：
- L11 `from .config.loader import load_config`
- L12 `from .core.llm import LLMClient`
- L13 `from .core.context import ContextManager`
- L14 `from .core.compressor import Compressor`
- L15 `from .core.tool_dispatcher import ToolDispatcher`
- L16 `from .core.message_manager import MessageManager`
- L17 `from .core.message_list import MessageList`
- L18 `from .core.stats import StatsTracker`
- L19 `from .core.session_callbacks import SessionManager`
- L20 `from .core.tool_callbacks import SafetyCallbacks, TodoCallbacks`
- L21 `from .core.agent_loop import AgentLoop`
- L22 `from .core.summarizer import Summarizer`
- L23 `from .core.auto_save_manager import AutoSaveManager`
- L24 `from .core.compression_coordinator import CompressionCoordinator`
- L25 `from .tools.tool_context import ToolContext`
- L26 `from .tools.terminal import TerminalRuntime`
- L27 `from .ui.ui_design import UIInterface, apply_style`
- L28 `from .ui.interrupt import _interrupt_ctrl`
- L29 `from .logger import AgentLogger`
- 函数内延迟导入：L59 `from .mcp import McpManager`、L61 `from .tools.registry import get_tool_definitions`、L141 `from .ui.headless import HeadlessUI`
- 标准库：L7 sys、L8 `from concurrent.futures import ThreadPoolExecutor`、L9 `from typing import Optional`

**被依赖**（grep 方及行号）：
- `narnat_agent/core/agent.py:16` `from ..assembly import Assembly, AssemblyResult`（L25 `Assembly.build(project_root, debug, headless)`；L26-35 解包各字段）
- 库外（tool_exp 验证脚本）：`tool_exp/verify_assembly.py:8`、`tool_exp/verify_import_cleanup.py:51`、`tool_exp/verify_mode_e2e.py:9`（均 `from narnat_agent.assembly import Assembly`）

**状态**：
- 模块级全局：无（无模块级赋值语句）
- 类变量：无
- AssemblyResult 实例属性（L217-231）：`config`、`logger`、`llm`、`context`、`message_list`、`msg_manager`、`session_mgr`、`ui`、`tool_context`、`dispatcher`、`stats`、`auto_save_mgr`、`compression_coordinator`、`agent_loop`、`mcp_manager`（15 个字段）
- Assembly 类无实例状态（仅 classmethod）

**行为要点**（构造顺序即依赖顺序，L38-207）：
1. L39 `config = load_config(project_root, headless)`。
2. L43-47 UI 样式回填：`config.ui.raw["show_cost"/"show_balance"/"max_output_tokens"/"show_ratio"/"context_window"]` 依次写回（前三个来自 `config.ui` 属性、后两个来自 `config.session`/`config.ai`）。
3. L48 `apply_style(config.ui.raw)`。
4. L51 `TerminalRuntime.set_max_sessions(config.tools.max_sessions)`（写工具模块类状态）。
5. L54-56 `AgentLogger(config.paths.logs_dir)`；`if debug: logger.start(config.paths.logs_dir)`——**非 debug 时日志器不启动**（所有 `logger.*` 调用静默丢弃，见 logger.py L87-88）。
6. L59-62 `McpManager(logger)`；`tool_definitions = get_tool_definitions()`（快照内置工具定义）。
7. L65-70 `LLMClient(config.ai, logger, max_output_tokens=config.ui.max_output_tokens, tool_definitions=tool_definitions)`。
8. L74 `mcp_manager.set_tool_sinks(llm.add_tool_definitions, llm.remove_tool_definitions)`——MCP 工具热更新接线（绑定方法，非 lambda）。
9. L77-82 `ContextManager(logger, context_window=config.ai.context_window, warn_ratio=config.session.warn_ratio, compress_ratio=config.session.compress_ratio)`。
10. L85 `Compressor()`（无参）。
11. L88-89 `MessageList(config.system_prompt)`；`MessageManager(message_list, compressor, logger)`。
12. L92 `Summarizer(llm, config, logger)`。
13. L97-99 `confirm_cb`：`None`，仅当 `not headless and sys.platform == "win32"` 时为 `SafetyCallbacks.confirm_delete`（staticmethod 引用）。
14. L100-113 `ToolContext(...)`：`confirm_callback=confirm_cb`、`ui_callback=TodoCallbacks.on_todo_update`、`api_keys=config.api_keys`、`ignore_dirs=list(config.tools.ignore_dirs)`（拷贝）、`git_skip_confirm`、`rm_skip_confirm`、`max_transfer_mb`、`max_tool_output_chars`、`max_timeout_seconds`、`require_plan`、`min_tools`、`mcp_manager=mcp_manager`——全部来自 config 或上一步对象。
15. L116-137 `SessionManager(...)` 共 20 个实参（详见总表"Assembly 依赖注入全景"）。
16. L140-144 UI：`headless` → `HeadlessUI(config.ai.model)`；否则 `UIInterface(config.ai.model, session_mgr, config.paths.data_dir)`。
17. L147-148 后置接线：`session_mgr.summary_anim_start = lambda: ui.begin_summarizing()`；`summary_anim_stop = lambda: ui.end_summarizing()`（因需 ui 先创建）。
18. L151-155 `ToolDispatcher(tool_context, ThreadPoolExecutor(max_workers=16), logger)`——线程池内联创建，不持有独立变量名。
19. L158-163 `StatsTracker(config.ai.model, config.pricing.user_pricing, config.balance, config.cost_log)`。
20. L166-169 `def _set_model_with_stats(v)`：`config.ai.model = v` 且 `stats._model = v`；`session_mgr._set_model = _set_model_with_stats`（覆写 SessionManager 私有 setter）。
21. L172-174 `AutoSaveManager(config, message_list, session_mgr, summarizer, stats, logger)`。
22. L177-179 `CompressionCoordinator(config, msg_manager, llm, context, ui, logger)`。
23. L182 `session_mgr.compact_func = compression_coordinator.compress_manual`（/compact 接线）。
24. L185-189 `AgentLoop(llm, msg_manager, dispatcher, tool_context, stats, ui, config, logger, compression=compression_coordinator)`。
25. L191-207 返回 `AssemblyResult(...)`，15 个字段逐一赋值。

**边界/异常行为**：
1. headless 差异仅两处：`confirm_cb=None`（L97-99）与 UI 换 `HeadlessUI`（L140-142）；其余构造完全相同（日志、MCP、SessionManager、skill 根等照常）。
2. 非 debug 不启动日志（L55-56）——debug 是日志的开关，`-p` headless 若不带 `-d` 则无日志文件。
3. `config.ui.raw` 手工回填（L43-47）是 `apply_style` 的前置契约：漏写任一键风格静默缺失（无校验）。
4. `_set_model_with_stats` 覆写 `session_mgr._set_model`（L169）——模型切换只有经过此函数才会同步 `stats._model`；`stats` 创建前无法接线。
5. `summary_anim_start/stop` 构造时传 None（L129-130）后置赋值（L147-148）——两次赋值、两个真相源。
6. `ignore_dirs=list(config.tools.ignore_dirs)`（L104）而 L136 `skill_ignore_dirs=config.tools.ignore_dirs` 为原引用——同源数据两种传递（拷贝/引用）不一致。
7. 无 try/except：任一构造失败异常直抛调用方（`Agent.__init__` 无捕获）。
8. `load_config` 会创建 `.narnat/config`、`.narnat/data` 目录并补默认配置文件（loader.py L722-729，R7 范围）——build 有真实文件系统副作用。
9. `McpManager` 构造不连任何服务器（懒连接），build 不发起网络/进程；MCP 子进程仅在 AI 调 MCP connect 后出现。
10. 步骤编号跳跃/重复（见补丁痕迹）不影响行为，但注释与真实顺序不符。

**补丁痕迹**：
1. 【高】L166-169 跨层私有覆写：`session_mgr._set_model = _set_model_with_stats` + 直写 `stats._model`——模型名三处状态（config.ai.model / stats._model / SessionManager._set_model）靠一个闭包同步。
2. 【高】流程编号失真：`# 6. LLM`（L64）后出现 `# 6.5`（L72）、又一个 `# 6.`（L76）、`# 7.`（L84），且 L152 直接 `# 12.` 后跳到 `# 14.`（L157）——无 13 步，多次插入后未重排。
3. 【中】`config.ui.raw[...]` 5 处手工补回（L43-47）——"配置被 loader 弹出后又要塞回"的耦合补丁。
4. 【中】后置属性接线三处：`summary_anim_start/stop`（L147-148）、`compact_func`（L182）——构建顺序倒置（ui 需 session_mgr、session_mgr 需 ui 回调）。
5. 【中】12 个 lambda 闭包注入 SessionManager（L120-132）——配置读取被闭包捕获，外部改 config 属性"生效与否"取决于闭包实现（setter 用 `setattr(config.ai, ...)` 可变）。
6. 【中】`ThreadPoolExecutor(max_workers=16)` 内联魔法值（L153），与 mcp 的 8 上限无关联。
7. 【中】`from .mcp import McpManager`（L59）函数内 import 与文件头风格不一致（其他 19 个 import 都在头部），原因未注释。
8. 【低】`AssemblyResult.__init__` 14 个位置参数 + 1 个带默认（L213-216），字段顺序即契约，增删字段需同步 agent.py 的解包。
9. 【低】注释残留："Python的顺序执行特性使依赖链一目了然"（L4）与实际编号失序不符；"# 11 补充"（L146）非编号；"# 6.5"（L72）。
10. 【低】`api_keys=config.api_keys`、`config.*` 直接引用透传（L103-112），无防御性拷贝（与 ignore_dirs 的 list() 拷贝不一致）。

**可测性**：
- 可独立单测：`AssemblyResult`（构造 + 15 字段断言）；`_set_model_with_stats` 行为（可间接经 build 后调 `session_mgr._set_model("x")` 验证双写）；各 lambda 的 getter/setter 语义（build 后可单独调用 SessionManager 相关命令验证）。
- 需要集成测试：`Assembly.build` 全链路（需临时项目根目录 + 最小 narnat.json；tool_exp/verify_assembly.py、verify_mode_e2e.py、verify_import_cleanup.py 已有先例）；headless 分支的对象类型断言；`apply_style` 无异常。
- 无法自动化：`apply_style` 的主题视觉效果（需人工/截图判定，属 R6 范围）；`Input` 确认回调路径；真实 API 连通性（LLMClient 构造不联网，但 build 后 fetch_balance 会联网）。

---

### narnat_agent/logger.py（109行）

**职责**：统一日志写入接口——四级日志、API key 脱敏、每次 `start()` 按启动时刻创建新日志文件。

**对外接口**：
- `AgentLogger`（L13）
- 类变量 `RE_SECRET`（L23-27）：敏感信息正则（`api_key["\s:=]+...` 或 `(sk-|key-|token-)[a-zA-Z0-9]{4}...`，`re.IGNORECASE`）
- `_redact(text: str) -> str`（L29 装饰器 + L30 def 至 L42，staticmethod）：保留前 4 位、其余 `***`
- `__init__(self, logs_dir: str = "")`（L44）：仅存目录，不打开文件
- `start(self, logs_dir: Optional[str] = None) -> str`（L49）：创建新日志文件，返回文件路径；可重复调用
- `_log(self, level: int, module: str, msg: str)`（L86）：内部统一写入口
- `debug(self, module: str, msg: str)`（L92）、`info(self, module: str, msg: str)`（L95）、`warning(self, module: str, msg: str)`（L98）、`error(self, module: str, msg: str)`（L101）
- `close(self)`（L104）：关闭并移除当前 handler

**依赖**（本文件 import 的内部模块，附行号）：
- 无内部依赖；标准库 L6 logging、L7 os、L8 re、L9 time、L10 `from typing import Optional`

**被依赖**（grep 方及行号）：
- 包内 import：`narnat_agent/assembly.py:29`（构造于 L54）
- `narnat_agent/core/agent_loop.py:22`、`narnat_agent/core/auto_save_manager.py:15`、`narnat_agent/core/compression_coordinator.py:17`、`narnat_agent/core/message_manager.py:11`、`narnat_agent/core/summarizer.py:11`（均为 `from ..logger import AgentLogger`，仅作类型注解/接收依赖）
- 调用方（方法使用）：`narnat_agent/core/agent.py:44/68-71/102/126/151/164/195/217/230/249/259/276`（`_logger.info/error/close`）；`core/agent_loop.py:82` 附近、`core/llm.py`（`self._logger.*` 30+ 处）、`core/compression_coordinator.py:56/87/117/120/144/179`、`core/context.py:65`、`mcp/__init__.py:300`、`mcp/client.py:317`、`core/tool_dispatcher.py`（4 处）
- 库外（tool_exp 历史副本）：`tool_exp/agent_loop_a156ac3.py:20`

**状态**：
- 模块级全局：无可变全局
- 类变量：`RE_SECRET`（L23-27，编译后不可变；静态方法内以类名硬引用，L42、L89）
- 实例状态：`_logger`（L45，`Optional[logging.Logger]`）、`_handler`（L46，`Optional[logging.FileHandler]`）、`_logs_dir`（L47，记住最近一次目录供后续 start 复用）

**行为要点**：
1. `start()`：`logs_dir` 参数非空则更新 `_logs_dir`（L54-55）；`os.makedirs(self._logs_dir, exist_ok=True)`（L57）；文件名 `time.strftime("%Y-%m-%d_%H-%M-%S") + ".log"`（L59-60）。
2. 关闭旧 handler 并从旧 logger 移除（L63-66）。
3. 新 logger：`logging.getLogger(f"narnat_{id(self)}")`（L68）——按实例 id 命名（同进程多实例互不串写）；`setLevel(DEBUG)`（L69）；`handlers.clear()`（L70）；`propagate=False`（L71）。
4. Handler：`FileHandler(filepath, encoding="utf-8")`、级别 DEBUG（L73-74）；格式 `"%(asctime)s [%(name)s]  %(levelname)-5s  %(message)s"`，日期 `"%Y-%m-%d %H:%M:%S"`（L75-79）；`logger.addHandler`（L80）。
5. 返回 filepath（L84）；`_logger/_handler` 就绪（L82-83）。
6. `_log()`：`self._logger` 为假（None）→ 直接 return（L87-88）；`safe_msg = AgentLogger._redact(msg)`（L89）；`self._logger.log(level, f"[{module}] {safe_msg}")`（L90）——模块名进消息前缀。
7. 四个级别方法只做 level 转发（L92-102）。
8. `_redact()`：两个分支——`sk-/key-/token-` 形态（group(5) 存在，L35-37）保留前缀+前4位后 `***`；`api_key=...` 形态（group(2) 存在，L39-40）保留 `group(1)+前4位+***`；否则原样返回（L41）。
9. `close()`：handler 存在时 close → 从 logger 移除 → `_handler=None`（L104-109）；`_logger` 不置空。
10. 文件追加模式（`FileHandler` 默认 "a"）——同一秒内重复 start 会写到同一文件。

**边界/异常行为**：
1. 未调用 `start()`：全部级别静默（L87-88）——debug 关闭时是预期行为，但也会掩盖"忘记 start"的编码错误。
2. `close()` 后 `_logger` 仍非 None 但无 handler（L104-109）：后续调用静默成功（`logging` 对无 handler 的 logger 走 lastResort/丢弃）。
3. `start()` 无参且 `_logs_dir` 为空串 → `os.makedirs("")` 抛 FileNotFoundError（L57）——（静态推断，未运行验证）；assembly 恒传 `config.paths.logs_dir`，实际路径不触及。
4. 无日志轮转/保留策略（L59-60）：文件名含时间戳但 `logs/` 无清理，磁盘无限增长（每次启动一个文件）。
5. 同秒内二次 `start()` 生成同名文件 —— `FileHandler` 追加写，两个 handler 指向同一文件（先 close 旧 handler 所以不双重写）。
6. 脱敏要求至少 4 位（L24-25）：短密钥（<4 字符）不脱敏；`key-` 前缀无词边界，正文中出现 `key-abcd...` 这类文本会被误脱敏（L25 分支）。
7. msg 非 str：`re.sub` 抛 TypeError（L42），`_log` 内无 try ——异常直接抛给调用方（调用方 core 层多处未包 try，如 `core/agent.py:102`）。
8. 编码固定 utf-8（L73），与 Windows 控制台编码无关。
9. 时间取本地时区（`time.strftime`）：文件名与日志时间行为本地时间。
10. `%(levelname)-5s` 只左对齐补空格：`WARNING`/`ERROR` 长度 ≥5，`CRITICAL` 更宽——列对齐在 WARNING+ 后错位（观察；该 logger 只用 4 级，WARNING/ERROR 恰好 7/5 字符，`ERROR` 对齐、`WARNING` 超宽）。
11. `_redact` 输入含多段密钥时 `sub` 全局替换（L42）。
12. 平台分支：无；重试/回退：无。

**补丁痕迹**：
1. 【中】日志轮转仅靠文件名时间戳，无大小/数量上限（L59-60）——`logs/` 目录无人清理。
2. 【中】`close()` 不清 `_logger`（L104-109）——"已关闭"与"未启动"两状态不可区分（无 `_started` 标志），后续调用静默吞。
3. 【中】`_redact` 正则脆弱：两个分支靠 group 编号（group(2)/group(5)）判定，且 `key-` 无词边界（L23-27、L35-40）——改动正则极易破坏分支判定。
4. 【低】静态方法内硬编码类名 `AgentLogger.RE_SECRET`（L42、L89）——子类无法覆盖正则。
5. 【低】`_logs_dir` 仅被 `start` 自身读回（L47、L54-55）——保存态无外部读取方，为"可重复调用"而存。
6. 【低】模块 docstring"按日期时间滚动文件"（L2）与实现不符（滚动=每次 start 一个文件，无轮转逻辑）。
7. 【低】`_log` 无异常兜底（L86-90），而 mcp/logger 使用方普遍做 try 兜底——保护责任外包给调用者，风格不一致。

**可测性**：
- 可独立单测：`_redact`（sk-xxx / key-xxx / token-xxx / api_key=xxx / 短密钥 / 无密钥，纯静态）；`start()` 返回路径与文件创建（tmp_path）；重复 `start()` 关旧开新；`close()` 幂等；未 start 静默；级别方法写入内容与格式（读文件断言）。
- 需要集成测试：与 assembly 的 debug 开/关（build(debug=False) 时无日志文件生成）；（可选）多线程并发 `_log`（logging 自身线程安全，`_redact` 纯函数）。
- 无法自动化：系统时间跨秒/跨分钟边界的文件名（需 mock `time.strftime`，可自动化但非必要）；磁盘满/权限拒绝时 `FileHandler` 异常（会中断 start）。

---

### main.py（73行）

**职责**：CLI 入口——argparse 解析、版本输出、headless（`-p`）与交互两条启动路径、顶层异常兜底。

**对外接口**：
- `__version__ = "16.2.5"`（L9）
- `main()`（L12）：无参数，读 `sys.argv`
- `if __name__ == "__main__":` 顶层块（L62-72）

**依赖**（本文件 import 的内部模块，附行号）：
- L5 argparse、L6 sys、L7 os（标准库）
- L33 `from narnat_agent.core.agent import Agent`（函数内延迟导入，位于 sys.path 处理之后）
- L45 `from narnat_agent.output import set_plain, set_quiet_tools`（函数内，仅 `-p` 分支）
- 注：L33 会先执行 `narnat_agent/__init__.py`（L5 `from .core.agent import Agent`）→ assembly → 全链加载

**被依赖**（grep 方及行号）：
- 无 Python `import` 方（全库 grep `from narnat_agent import Agent` 无命中）
- 命令行/打包调用：README.md:131、165；`tool_exp/build_final.bat`、`build_fixed.bat` 等构建脚本；nuitka 打包入口（`tool_exp/.gv_build_log.txt:2` 记录 `--output-filename=narnat.exe ... main.py`）；`tool_exp/_zz_shell_commands.txt` 中等多处历史调用 `python main.py -p ...`

**状态**：
- 模块级全局：`__version__`（L9，常量，无写入方）
- 无类、无其它模块级可变状态

**行为要点**：
1. argparse 参数（L14-21）：`-d/--debug`（store_true）、`-v/--version`（store_true）、`-p/--prompt`（str，默认 None）、`-g/--goal-rounds`（int，default 0）、`-l/--tool-log`（store_true）。
2. `-v`：打印 `f"narnat {__version__}"` 后 return（L24-26）——先于一切其它分支。
3. `project_root = os.path.dirname(os.path.abspath(__file__))`；不在 `sys.path` 则 insert(0)（L29-31）。
4. `-p` 分支：`args.prompt is not None` 进入（L35）；`not args.prompt.strip()` → 打印 `错误: -p 任务内容不能为空` + `sys.exit(1)`（L36-38）。
5. headless stdout 编码：win32 下 `sys.stdout.reconfigure(encoding="utf-8")`，`AttributeError/OSError` 吞（L40-44）。
6. `set_plain(True)`（L46，全局去色）；`set_quiet_tools(not args.tool_log)`（L47，默认静默工具日志）。
7. `-g` 负数 → 打印 `错误: -g 轮数上限必须为正整数` + `sys.exit(1)`（L48-50）。
8. `agent = Agent(debug=args.debug, headless=True)`；`agent.run_headless(args.prompt, max_rounds=args.goal_rounds)`；return（L54-56）。
9. 交互分支：`agent = Agent(debug=args.debug)`；`agent.run()`（L58-59）。
10. 顶层异常兜底（L62-72）：`SystemExit` 原样重抛；其它 `Exception` 打印 `f"\n程序异常退出: {e}"`，`input("按回车键退出...")`（`KeyboardInterrupt/EOFError` 吞），`sys.exit(1)`。
11. `-d` 与 `-p` 可组合（Assembly 用 debug 开日志）；`-l` 只在 `-p` 分支消费。

**边界/异常行为**：
1. `-g 0`（默认）合法：传 0 给 `run_headless(max_rounds=0)` → 用配置默认轮数（core/agent.py:211 `max_rounds or self._config.ai.goal_max_rounds`）。
2. `-g` 非整数由 argparse 处理：报错并 `exit 2`（与 L48-50 的手工 `exit(1)` 退出码不一致）。
3. `-p ""` 与 `-p "   "` 均判空（L36）。
4. 未传参 → 交互模式（L58）；`-l`/`-g` 在交互模式被静默忽略（不报错）。
5. `-v` 与其它参数组合时 `-v` 优先（L24-26 提前 return）。
6. 无 stdin（管道/父代理）时 `input()` 抛 EOFError 被吞（L71-72）——异常路径不会挂死；但在有 stdin 的自动化调用（子代理）中会真实阻塞等待回车（**headless 本意自动化，异常时才阻塞，属语义冲突点**）。
7. `Agent(...)` 构造失败（配置/依赖缺失）→ 顶层兜底；交互模式下会走 `input()` 等待回车后才退出。
8. `sys.stdout.reconfigure` 仅在 win32 且 `-p` 下执行（L40-44），交互模式不改编码。
9. 无 `--version` 之外的元信息输出；无子命令。
10. 平台分支：仅 L40 的 win32 分支。
11. 退出码：正常 0；`-p` 空任务 / `-g` 负 → 1；argparse 错误 → 2；顶层异常 → 1。

**补丁痕迹**：
1. 【中】顶层异常 `input("按回车键退出...")`（L69-72）——为双击 exe 场景保留报错可见；与 headless/自动化的非交互预期冲突（子代理侧靠 EOF 或 stdin 关闭兜底）。
2. 【中】`__version__` 硬编码在入口（L9），无单一版本源（npm 包/exe/README 多处需对齐；README.md:165 区域说明发版方式）。
3. 【中】`sys.path.insert` 手工处理（L29-31）——源码直跑补丁，打包/安装场景冗余。
4. 【低】两个参数帮助文本重复 "headless模式：" 前缀（L18、L20-21）。
5. 【低】`-p` 空与 `-g` 负的错误走 `print` 而非 `parser.error`（L36-38、L48-50）——错误风格与退出码不统一。
6. 【低】函数体内 import（L33、L45）——为 `-v` 快速返回而延迟导入，但使入口依赖图需读函数体才完整。
7. 【低】`Agent` 实例在 `-p` 分支与交互分支分别构造（L54、L58）——两处参数略异（headless=True），可合并但保持现状。
8. 【低】顶层块把 `SystemExit` 单独重抛（L65-66）——显式但略冗（默认行为即如此）。

**可测性**：
- 可独立单测：参数解析与错误分支（需以 subprocess 方式运行 `python main.py -v` / `-p ""` / `-g -1` 断言 stdout 与退出码；解析逻辑内联在 `main()` 中，无法直接被测函数调用——新架构可拆出 `parse_args`）。
- 需要集成测试：`-p` 冒烟（会真实调用 LLM，需密钥与网络；`tool_exp` 有历史命令先例）；`-d` 下日志文件生成。
- 无法自动化：交互模式（prompt_toolkit 全屏终端）；`input()` 等待回车的真实键盘行为。

---

### narnat_agent/__init__.py（7行）

**职责**：包入口——导入并导出 `Agent`。

**对外接口**：
- `Agent`（L5，re-export 自 `core.agent`）
- `__all__ = ["Agent"]`（L7）
- 模块 docstring（L1-3）：`"""Narnat Agent —— 精简至上的代码智能体"""`

**依赖**（本文件 import 的内部模块，附行号）：
- L5 `from .core.agent import Agent`（唯一 import）

**被依赖**（grep 方及行号）：
- 无直接 `from narnat_agent import Agent` 命中（全库 grep 验证）
- 间接：任何 `import narnat_agent...` 都会先执行本文件（L5）→ 加载 `core.agent` → `assembly` → 全部 core/tools/ui 模块。实际触发点包括 `main.py:33`（`from narnat_agent.core.agent import Agent`）与库外脚本如 `tool_exp/mcp_test/test_mcp.py:21`（`from narnat_agent.mcp import McpManager`）——**连 mcp 独立测试也会连带加载 assembly 链**
- 库外：`subagent_test/attr_experiment.py:11`、`tool_exp/agent_seq_exp.py:66` 等（`import narnat_agent.xxx`，同上触发）

**状态**：
- 模块级全局：无（`__all__` 字面量）
- 类/实例状态：无

**行为要点**：
1. 包导入即加载 `core.agent`（L5），连带 assembly（assembly.py:11-29 的 20 个 import）与 core/tools/ui 全链。
2. 无延迟导入、无 try/except、无版本号导出（`__version__` 只存在于 main.py:9）。
3. `__all__` 仅 `Agent`（L7）。
4. 导入本身不执行 `Assembly.build`，故不创建目录、不读配置、不联网（副作用在 `Agent()` 构造时才发生）。

**边界/异常行为**：
1. 依赖链任一模块 import 失败 → 整个包不可用（无降级）。
2. 导入开销：链式加载较重（可观察：`python -c "import narnat_agent"` 加载大量模块——未运行验证，按 import 图推断）。
3. 无平台分支、无编码处理、无重试。

**补丁痕迹**：
1. 【低】包级 eager import（L5）造成"导入任意子模块=加载全链"的隐含耦合——工具/实验脚本只想用 `mcp` 或 `tools.registry` 时被动加载 assembly/UI 依赖（含 prompt_toolkit 等重依赖的模块对象构建）。
2. 【低】无 `__version__`（与 main.py:9 不一致）——版本元数据分散。

**可测性**：
- 可独立单测：import 冒烟（`import narnat_agent; assert narnat_agent.Agent`）；导入副作用隔离（断言未创建 `.narnat` 目录、未联网——需隔离环境与 monkeypatch）。
- 需要集成测试：导入耗时基线（性能回归用）。
- 无法自动化：无（纯导入语义）。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

**出边（本组 6 文件引用了谁）**：

| 源 | 目标 | 行号 | 备注 |
|---|---|---|---|
| main.py | narnat_agent/core/agent.py | 33 | 函数内 import（`Agent`） |
| main.py | narnat_agent/output.py | 45 | 函数内 import（`set_plain, set_quiet_tools`） |
| main.py | narnat_agent/__init__.py | —（隐式） | 上述任一 `narnat_agent.*` import 触发包初始化 |
| narnat_agent/__init__.py | narnat_agent/core/agent.py | 5 | 包级 eager re-export |
| narnat_agent/assembly.py | narnat_agent/config/loader.py | 11 | `load_config` |
| narnat_agent/assembly.py | narnat_agent/core/llm.py | 12 | `LLMClient` |
| narnat_agent/assembly.py | narnat_agent/core/context.py | 13 | `ContextManager` |
| narnat_agent/assembly.py | narnat_agent/core/compressor.py | 14 | `Compressor` |
| narnat_agent/assembly.py | narnat_agent/core/tool_dispatcher.py | 15 | `ToolDispatcher` |
| narnat_agent/assembly.py | narnat_agent/core/message_manager.py | 16 | `MessageManager` |
| narnat_agent/assembly.py | narnat_agent/core/message_list.py | 17 | `MessageList` |
| narnat_agent/assembly.py | narnat_agent/core/stats.py | 18 | `StatsTracker` |
| narnat_agent/assembly.py | narnat_agent/core/session_callbacks.py | 19 | `SessionManager` |
| narnat_agent/assembly.py | narnat_agent/core/tool_callbacks.py | 20 | `SafetyCallbacks, TodoCallbacks` |
| narnat_agent/assembly.py | narnat_agent/core/agent_loop.py | 21 | `AgentLoop` |
| narnat_agent/assembly.py | narnat_agent/core/summarizer.py | 22 | `Summarizer` |
| narnat_agent/assembly.py | narnat_agent/core/auto_save_manager.py | 23 | `AutoSaveManager` |
| narnat_agent/assembly.py | narnat_agent/core/compression_coordinator.py | 24 | `CompressionCoordinator` |
| narnat_agent/assembly.py | narnat_agent/tools/tool_context.py | 25 | `ToolContext` |
| narnat_agent/assembly.py | narnat_agent/tools/terminal/__init__.py | 26 | `TerminalRuntime`（仅调 `set_max_sessions`） |
| narnat_agent/assembly.py | narnat_agent/ui/ui_design.py | 27 | `UIInterface, apply_style` |
| narnat_agent/assembly.py | narnat_agent/ui/interrupt.py | 28 | `_interrupt_ctrl`（模块级单例） |
| narnat_agent/assembly.py | narnat_agent/logger.py | 29 | `AgentLogger` |
| narnat_agent/assembly.py | narnat_agent/mcp/__init__.py | 59 | 函数内 import（`McpManager`） |
| narnat_agent/assembly.py | narnat_agent/tools/registry.py | 61 | 函数内 import（`get_tool_definitions`） |
| narnat_agent/assembly.py | narnat_agent/ui/headless.py | 141 | 函数内 import（`HeadlessUI`，headless 分支） |
| narnat_agent/mcp/__init__.py | narnat_agent/mcp/client.py | 15 | `McpError, McpStdioClient` |
| narnat_agent/mcp/__init__.py | narnat_agent/config/loader.py | 16 | `parse_mcp_server` |
| narnat_agent/mcp/__init__.py | narnat_agent/tools/exec_signal.py | 17 | `error_line` |
| narnat_agent/mcp/__init__.py | narnat_agent/tools/registry.py | 117, 172 | 函数内 import（`get_tool_names, register_dynamic_tools` / `unregister_dynamic_tools`） |
| narnat_agent/mcp/client.py | narnat_agent/tools/exec_signal.py | 25 | `error_line` |
| narnat_agent/logger.py | （无内部依赖） | — | 仅标准库 logging/os/re/time/typing |

**入边（谁引用了本组文件）**：

| 目标 | 引用方 | 行号 | 形式 |
|---|---|---|---|
| mcp/__init__.py | narnat_agent/assembly.py | 59-60 | `from .mcp import McpManager`；构造于 60 |
| mcp/__init__.py | narnat_agent/tools/mcp_tool/__init__.py | 7 | `from ...mcp import McpError` |
| mcp/__init__.py | tool_exp/mcp_test/demo_mcp_flow.py | 16 | 库外实验 |
| mcp/__init__.py | tool_exp/mcp_test/demo_persistence.py | 19 | 库外实验 |
| mcp/__init__.py | tool_exp/mcp_test/test_mcp.py | 21 | 库外实验 |
| mcp/__init__.py | tool_exp/mcp_test/test_mcp_runtime.py | 22 | 库外实验 |
| mcp/__init__.py | tool_exp/verify_mcp_console_isolate_live.py | 135 | 库外实验 |
| mcp/client.py | narnat_agent/mcp/__init__.py | 15 | 同包内 |
| mcp/client.py | tool_exp/mcp_test/demo_mcp_flow.py | 17 | 库外实验（`import ... as mcp_client`） |
| mcp/client.py | tool_exp/mcp_test/demo_persistence.py | 20 | 库外实验 |
| mcp/client.py | tool_exp/verify_mcp_console_isolate_live.py | 122 | 库外实验 |
| assembly.py | narnat_agent/core/agent.py | 16（用 25） | `Assembly.build(project_root, debug, headless)` |
| assembly.py | tool_exp/verify_assembly.py | 8 | 库外验证脚本 |
| assembly.py | tool_exp/verify_import_cleanup.py | 51 | 库外验证脚本 |
| assembly.py | tool_exp/verify_mode_e2e.py | 9 | 库外验证脚本 |
| logger.py | narnat_agent/assembly.py | 29（用 54） | `AgentLogger(config.paths.logs_dir)` |
| logger.py | narnat_agent/core/agent_loop.py | 22 | `from ..logger import AgentLogger` |
| logger.py | narnat_agent/core/auto_save_manager.py | 15 | 同上 |
| logger.py | narnat_agent/core/compression_coordinator.py | 17 | 同上 |
| logger.py | narnat_agent/core/message_manager.py | 11 | 同上 |
| logger.py | narnat_agent/core/summarizer.py | 11 | 同上 |
| logger.py（方法调用侧） | narnat_agent/core/agent.py | 44, 68, 71, 102, 126, 151, 164, 195, 217, 230, 249, 259, 276 | `_logger.info/error/close` |
| logger.py（方法调用侧） | narnat_agent/mcp/__init__.py | 300 | `logger.info("mcp", msg)` |
| logger.py（方法调用侧） | narnat_agent/mcp/client.py | 317 | `logger.info(f"mcp.{name}", msg)` |
| logger.py | tool_exp/agent_loop_a156ac3.py | 20 | 库外历史副本（文件名带 git 短哈希） |
| main.py | 无 Python import 方 | — | 命令行/打包入口（README.md:131、165；tool_exp/build_*.bat；nuitka：tool_exp/.gv_build_log.txt:2） |
| narnat_agent/__init__.py | 无 `from narnat_agent import Agent` 命中 | — | 隐式：任意 `import narnat_agent.*` 触发（如 main.py:33、tool_exp/mcp_test/test_mcp.py:21） |

**mcp 反向依赖边（用于循环依赖判定）**：
- narnat_agent/tools/registry.py → mcp 包侧：L28 导入 `MCP_DEF`（tools/mcp_tool），L60 收入 `TOOL_DEFINITIONS`
- narnat_agent/tools/mcp_tool/__init__.py → narnat_agent/mcp/__init__.py (L7)；mcp_tool → config/loader (L82，函数内)
- narnat_agent/core/tool_dispatcher.py 使用 `"mcp__"` 字面量（L242, L404, L416，无 import 边但存在语义耦合）
- 结论：`mcp ↔ tools(mcp_tool/registry)` 存在真实的双向引用，靠 mcp/__init__.py 的 L117/L172 函数内延迟导入 + registry 侧只 import 工具定义（非 mcp 运行时）避免循环崩溃。

### 模块级可变状态全清单（含读写方）

**A. 本组 6 文件内部**：

| 文件 | 模块级状态 | 可变性 | 写方 | 读方 |
|---|---|---|---|---|
| mcp/__init__.py | `_MAX_NAME_LEN`(L20)/`_HASH_LEN`(L21)/`_NAME_SAFE_RE`(L22)/`TOOL_PREFIX`(L23)/`_MAX_SERVERS`(L24) | 只读常量 | 无 | 本文件 L307/L309/L319/L328-329/L101 等 |
| mcp/client.py | `PROTOCOL_VERSION`(L28)/`CLIENT_NAME`(L29)/`CLIENT_TITLE`(L30)/`CLIENT_VERSION`(L31)/`_MAX_LIST_PAGES`(L34) | 只读常量 | 无 | 本文件 L127/L129-131/L142 |
| assembly.py | 无 | — | — | — |
| logger.py | `RE_SECRET`(L23-27) 类变量 | 只读（编译正则） | 无 | `_redact` L42、L89 |
| main.py | `__version__`(L9) | 名义可变，实际只读 | 无 | L25 |
| narnat_agent/__init__.py | 无 | — | — | — |

**B. 因本组代码而被写入的跨模块全局/类级状态（新架构必须继承或替换）**：

| 状态 | 定义位置 | 写方（行号） | 读方（行号） |
|---|---|---|---|
| `tools/registry._DYNAMIC_IMPLEMENTATIONS` | tools/registry.py:70 | mcp/__init__.py:136（`register_dynamic_tools`）；mcp/__init__.py:173（`unregister_dynamic_tools`） | tools/registry.py:81-84, 93-94, 122 |
| `tools/registry._DYNAMIC_DEFINITIONS` | tools/registry.py:71 | 同上（经 registry 函数 L84, L94-97） | tools/registry.py:84, 94-97, 195+（`get_tool_definitions`） |
| `output._PLAIN` | output.py:29 | main.py:46（`set_plain(True)`） | output.py:38-40（`is_plain`）；渲染器运行时读取 |
| `output._QUIET_TOOLS` | output.py:46 | main.py:47（`set_quiet_tools(...)`） | output.py:54-55（`is_quiet_tools`）；core/tool_callbacks.py:31 |
| `output._vt_handle` | output.py:59 | output.py 内部（Windows VT 启用，R6/R8 交叉） | output.py 内部重复申领 |
| `core/llm.LLMClient._max_network_retries/_max_rate_retries` | core/llm.py:175-176（类级） | core/llm.py:181-182（`set_retry_count`；由 `__init__` L191 与 core/agent_loop.py:82 调用） | core/llm.py 重试判定处 |
| `core/llm.LLMClient._tool_defs`（实例，被 sink 共享） | core/llm.py 实例 | mcp/__init__.py:138（`_tool_sink_add`→`llm.add_tool_definitions` L228-239）；mcp/__init__.py:175（`_tool_sink_remove`→`llm.remove_tool_definitions` L241-249） | 每轮请求组装工具表处（llm.py:218-219 同列表对象） |
| `ui/interrupt._interrupt_ctrl`（单例） | ui/interrupt.py:278 | —（内部状态机自管） | assembly.py:131（`cancel_check=lambda: _interrupt_ctrl.is_set`）；ui_design.py:294/302-303/314-315/325/331 |
| `TerminalRuntime` 类级会话表/上限 | tools/terminal/__init__.py（类属性） | assembly.py:51（`set_max_sessions`） | tools/terminal（R4 范围） |
| `SessionManager._set_model`（实例属性被覆写） | core/session_callbacks.py:602 | assembly.py:169（覆写为 `_set_model_with_stats`） | session 命令路径（R2 范围） |
| `StatsTracker._model` | core/stats.py:28 | assembly.py:168（直写）；core/agent_loop.py 侧统计更新 | 费用计算/统计栏 |
| `SessionManager.summary_anim_start/stop`、`compact_func` | session_callbacks.py:605-606, 610 | assembly.py:147-148, 182 | session 命令路径 |

**C. 状态生命周期要点**：
- `_DYNAMIC_*`（registry）：进程级、跨会话不清（`cleanup()` 不注销，见 mcp/__init__.py 行为要点 1）；正常路径由 `disconnect` 归零，异常退出残留至进程结束。
- `_PLAIN`/`_QUIET_TOOLS`：进程级一次性开关（main.py 设置后无人复位）。
- LLM `_tool_defs` 与 registry 动态表是**两个平行真相源**，由 `set_tool_sinks` 回调保持同步（llm.py:231-232 注释确认"共享同一列表对象"）。

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

| # | 严重度 | 位置 | 痕迹 | 判据 |
|---|---|---|---|---|
| 1 | 高 | narnat_agent/assembly.py:166-169 | `session_mgr._set_model = _set_model_with_stats` 覆写私有 setter；函数内直写 `stats._model` | 跨层私有访问 + 三处状态同步靠一个闭包 |
| 2 | 高 | narnat_agent/mcp/__init__.py:82-151 | `connect()` 70 行、6 项职责、4 段独立锁、2 处函数内 import、末尾 atexit 注册 | 职责混杂 + 脆弱边界 |
| 3 | 高 | narnat_agent/assembly.py:64-84, 152-157 | 步骤编号重复（`6.` 出现于 L64 与 L76）、`6.5`（L72）、无 `13`（L152→L157 直接 14） | 历史包袱注释：流程注释与真实构建顺序失真 |
| 4 | 中 | narnat_agent/mcp/__init__.py:117, 172 | mcp → tools.registry 的两处函数内延迟导入 | 跨层引用（为规避循环依赖） |
| 5 | 中 | narnat_agent/mcp/__init__.py:134 | `_server_tools[name] = [e[0] for e in entries]` 记录全量，而 registry 对重名静默跳过（tools/registry.py:81-82） | 两个真相源：登记数可能 > 实际注册数 |
| 6 | 中 | narnat_agent/mcp/__init__.py:147-149 | `_atexit_registered` 无锁检查后注册 `atexit.register(self.cleanup)` | 并发竞态（重复注册） |
| 7 | 中 | narnat_agent/mcp/__init__.py:160-162 | `disconnect("all")` 递归自身、循环内不捕获异常 | 批处理无 per-server 容错，一个失败中断整批 |
| 8 | 中 | narnat_agent/mcp/client.py:255-289 | `_reader_loop` 单函数兼响应分发/通知日志/请求回绝/EOF 广播；L34+L142 分页 50 页静默截断 | 职责混杂 + 截断不可观测 |
| 9 | 中 | narnat_agent/logger.py:59-60, 104-109 | 文件按时间戳命名但无轮转/保留/清理；`close()` 不清 `_logger`（"已关闭"与"未启动"不可区分） | 历史包袱 + 状态二义性 |
| 10 | 中 | main.py:9, 69-72 | `__version__` 硬编码于入口；headless 路径顶层异常走 `input()` 等待回车 | 版本多源 + 与自动化入口语义冲突 |

**次席（11-16，低）**：
- assembly.py:43-47 `config.ui.raw[...]` 5 键手工回填（配置层↔UI 层耦合）
- assembly.py:147-148, 182 后置属性接线（`summary_anim_*`、`compact_func`）
- assembly.py:59 函数内 import 与文件头风格不一致
- mcp/__init__.py:122-151 `entries` 跨分支作用域引用（脆弱但当前安全）
- mcp/client.py:53-58（GBK 回退）、80-86（CREATE_NO_WINDOW）——为平台怪癖打的定点补丁（知识资产）
- narnat_agent/__init__.py:5 包级 eager import（`import narnat_agent.mcp` 连带加载 assembly 全链）；main.py:29-31 手工 `sys.path` 插入

### Assembly 依赖注入全景（对象 → 构造参数 → 注入来源）

**构造顺序表（L39 → L207）**：

| 步 | 对象 | 构造实参（源码原文） | 注入来源 | 行号 |
|---|---|---|---|---|
| 1 | Config | `load_config(project_root, headless)` | 入参 | L39 |
| 2 | （副作用）UI 样式 | `apply_style(config.ui.raw)`；前置 5 键回填 | 回填自 `config.ui.*`/`config.session.show_ratio`/`config.ai.context_window` | L43-48 |
| 3 | （副作用）终端上限 | `TerminalRuntime.set_max_sessions(config.tools.max_sessions)` | config | L51 |
| 4 | AgentLogger | `AgentLogger(config.paths.logs_dir)`；`debug` 时 `logger.start(config.paths.logs_dir)` | config + build 入参 debug | L54-56 |
| 5 | McpManager | `McpManager(logger)` | 步骤 4 | L60 |
| 6 | tool_definitions | `get_tool_definitions()` | tools.registry 快照 | L62 |
| 7 | LLMClient | `LLMClient(config.ai, logger, max_output_tokens=config.ui.max_output_tokens, tool_definitions=tool_definitions)` | config + 步骤 4/6 | L65-70 |
| 7.5 | （接线）MCP→LLM | `mcp_manager.set_tool_sinks(llm.add_tool_definitions, llm.remove_tool_definitions)` | 步骤 5 + 7（绑定方法） | L74 |
| 8 | ContextManager | `ContextManager(logger, context_window=config.ai.context_window, warn_ratio=config.session.warn_ratio, compress_ratio=config.session.compress_ratio)` | config + 步骤 4 | L77-82 |
| 9 | Compressor | `Compressor()` | 无参 | L85 |
| 10 | MessageList | `MessageList(config.system_prompt)` | config | L88 |
| 11 | MessageManager | `MessageManager(message_list, compressor, logger)` | 步骤 10/9/4 | L89 |
| 12 | Summarizer | `Summarizer(llm, config, logger)` | 步骤 7 + config + 步骤 4 | L92 |
| 13 | confirm_cb | `SafetyCallbacks.confirm_delete`（仅 `not headless and sys.platform=="win32"`，否则 None） | build 入参 + 平台 | L97-99 |
| 14 | ToolContext | `ToolContext(confirm_callback=confirm_cb, ui_callback=TodoCallbacks.on_todo_update, api_keys=config.api_keys, ignore_dirs=list(config.tools.ignore_dirs), git_skip_confirm=config.safety.git_skip_confirm, rm_skip_confirm=config.safety.rm_skip_confirm, max_transfer_mb=config.tools.max_transfer_mb, max_tool_output_chars=config.tools.max_output_chars, max_timeout_seconds=config.tools.max_timeout_seconds, require_plan=config.plan.require_plan, min_tools=config.plan.min_tools, mcp_manager=mcp_manager)` | config（10 项）+ 步骤 13/5 | L100-113 |
| 15 | SessionManager | 20 个实参，见下方 lambda 清单 | config + 步骤 11/12/7 + UI 后置 | L116-137 |
| 16 | UI | headless → `HeadlessUI(config.ai.model)`；否则 `UIInterface(config.ai.model, session_mgr, config.paths.data_dir)` | config + build 入参 + 步骤 15 | L140-144 |
| 16.5 | （后置接线） | `session_mgr.summary_anim_start = lambda: ui.begin_summarizing()`；`summary_anim_stop = lambda: ui.end_summarizing()` | 步骤 16 | L147-148 |
| 17 | ToolDispatcher | `ToolDispatcher(tool_context, ThreadPoolExecutor(max_workers=16), logger)` | 步骤 14 + 内联线程池 + 步骤 4 | L151-155 |
| 18 | StatsTracker | `StatsTracker(config.ai.model, config.pricing.user_pricing, config.balance, config.cost_log)` | config | L158-163 |
| 18.5 | （后置接线） | `session_mgr._set_model = _set_model_with_stats`（闭包：写 `config.ai.model` + `stats._model`） | 步骤 18 | L166-169 |
| 19 | AutoSaveManager | `AutoSaveManager(config, message_list, session_mgr, summarizer, stats, logger)` | config + 步骤 10/15/12/18/4 | L172-174 |
| 20 | CompressionCoordinator | `CompressionCoordinator(config, msg_manager, llm, context, ui, logger)` | config + 步骤 11/7/8/16/4 | L177-179 |
| 20.5 | （后置接线） | `session_mgr.compact_func = compression_coordinator.compress_manual` | 步骤 20 | L182 |
| 21 | AgentLoop | `AgentLoop(llm, msg_manager, dispatcher, tool_context, stats, ui, config, logger, compression=compression_coordinator)` | 步骤 7/11/17/14/18/16 + config + 步骤 4/20 | L185-189 |
| 22 | AssemblyResult | `AssemblyResult(config=..., logger=..., llm=..., context=..., message_list=..., msg_manager=..., session_mgr=..., ui=..., tool_context=..., dispatcher=..., stats=..., auto_save_mgr=..., compression_coordinator=..., agent_loop=..., mcp_manager=...)` | 全部步骤 | L191-207 |

**SessionManager 实参清单（L116-137，逐条）**：

| 形参 | 值/来源 | 行号 |
|---|---|---|
| `narnat_dir` | `config.paths.narnat_dir` | L117 |
| `messages` | `message_list`（步骤 10） | L118 |
| `config_dir` | `config.paths.config_dir` | L119 |
| `thinking_effort_getter` | **lambda**: `lambda: config.ai.thinking_effort` | L120 |
| `thinking_effort_setter` | **lambda**: `lambda v: setattr(config.ai, 'thinking_effort', v)` | L121 |
| `thinking_options` | `config.ai.thinking_options` | L122 |
| `thinking_passback_getter` | **lambda**: `lambda: config.ai.thinking_passback` | L123 |
| `thinking_passback_setter` | **lambda**: `lambda v: setattr(config.ai, 'thinking_passback', v)` | L124 |
| `model_getter` | **lambda**: `lambda: config.ai.model` | L125 |
| `model_setter` | **lambda**: `lambda v: setattr(config.ai, 'model', v)`（后被 L169 覆写） | L126 |
| `model_options` | `config.ai.model_options` | L127 |
| `summarize_func` | **lambda**: `lambda msgs, cancel: summarizer.summarize(msgs, cancel)` | L128 |
| `summary_anim_start` | `None` → 后置 **lambda**: `lambda: ui.begin_summarizing()` | L129 / L147 |
| `summary_anim_stop` | `None` → 后置 **lambda**: `lambda: ui.end_summarizing()` | L130 / L148 |
| `cancel_check` | **lambda**: `lambda: _interrupt_ctrl.is_set`（读 ui 层全局单例） | L131 |
| `name_func` | **lambda**: `lambda msgs: summarizer.name_session(msgs)` | L132 |
| `goal_tool_setter` | `llm.set_goal_tool`（绑定方法） | L133 |
| `goal_max_rounds` | `config.ai.goal_max_rounds` | L134 |
| `project_skill_roots` | `config.skills.project_roots` | L135 |
| `skill_ignore_dirs` | `config.tools.ignore_dirs`（原引用，非拷贝） | L136 |
| （后置）`_set_model` | `_set_model_with_stats`（def 闭包，L166-169） | L169 |
| （后置）`compact_func` | `compression_coordinator.compress_manual`（绑定方法） | L182 |

**lambda / 闭包接线清单（共 12 个 lambda + 2 个 def 闭包 + 4 个绑定方法/函数引用）**：
- lambda（9 个即时注入）：L120、L121、L123、L124、L125、L126、L128、L131、L132
- lambda（2 个后置）：L147、L148
- def 闭包（1 个后置覆写）：`_set_model_with_stats`（L166-169）
- 绑定方法/函数引用：`llm.set_goal_tool`（L133）、`llm.add_tool_definitions` / `llm.remove_tool_definitions`（L74）、`TodoCallbacks.on_todo_update`（L102）、`SafetyCallbacks.confirm_delete`（L99）、`compression_coordinator.compress_manual`（L182）
- 闭包捕获风险点：所有 getter/setter lambda 捕获 `config` 子对象（`config.ai`），改配置生效依赖 `setattr` 写回同一对象；`_set_model_with_stats` 额外捕获 `stats`。

**headless 分支差异（与交互模式对比）**：

| 项 | 交互（headless=False） | headless=True | 行号 |
|---|---|---|---|
| `load_config` 第二参 | False | True（剥离 narnat.md 的 subagent:hide 区块，R7） | L39 |
| `confirm_cb` | win32 → `SafetyCallbacks.confirm_delete` | 恒 `None`（命令按未确认处理） | L97-99 |
| UI | `UIInterface(model, session_mgr, data_dir)` | `HeadlessUI(model)` | L140-144 |
| 其余对象 | 完全相同（含 McpManager、SessionManager、AutoSaveManager 等） | 完全相同 | — |

**组装层对外契约要点**（写新架构时的兼容项）：
1. `Assembly.build` 返回的 `AssemblyResult` 字段名被 `core/agent.py:26-35` 解包消费（`config/logger/ui/context/session_mgr/msg_manager/stats/agent_loop/auto_save_mgr/compression_coordinator`；`mcp_manager` 于 L70/184/275 使用；`tool_context` 于 L84-91/145/242 使用；`dispatcher` 于 L177/268 使用）——字段重命名将破坏 agent.py。
2. build 具有文件系统副作用（`load_config` 建目录/补默认文件、debug 时创建日志文件）。
3. build 不含网络/子进程副作用（MCP 懒连接；LLMClient 构造不联网）。
4. 无异常兜底；构造失败直接抛给 `Agent.__init__`（core/agent.py:25）。
5. 后置接线（4 处）是新架构需显式化的隐式依赖：`summary_anim_*`（L147-148）、`_set_model`（L169）、`compact_func`（L182）、`set_tool_sinks`（L74）。

---

## 附：验收自查

- 覆盖 6 文件（`###` 节各一）：mcp/__init__.py、mcp/client.py、assembly.py、logger.py、main.py、narnat_agent/__init__.py ✓
- 行为要点/边界/补丁痕迹条条附行号 ✓
- 被依赖清单含文件:行号，可用如下命令复验（抽查 5 条）：
  - `grep -rn "from .mcp import McpManager" narnat_agent/` → 命中 `narnat_agent/assembly.py:59`
  - `grep -rn "from ...mcp import McpError" narnat_agent/` → 命中 `narnat_agent/tools/mcp_tool/__init__.py:7`
  - `grep -rn "from ..assembly import" narnat_agent/` → 命中 `narnat_agent/core/agent.py:16`
  - `grep -rn "from ..logger import AgentLogger" narnat_agent/core/` → 命中 agent_loop.py:22、auto_save_manager.py:15、compression_coordinator.py:17、message_manager.py:11、summarizer.py:11
  - `grep -rn "from .client import" narnat_agent/mcp/` → 命中 `narnat_agent/mcp/__init__.py:15`
- 总表四项齐备（依赖矩阵 / 可变状态全清单 / 补丁 TOP10 / Assembly 注入全景）✓
- 签名逐字誊自源码（复核命令：`grep -n "def build\|def __init__(self, name, command\|def _make_unique_name\|def _redact\|def run_headless" narnat_agent/assembly.py narnat_agent/mcp/client.py narnat_agent/mcp/__init__.py narnat_agent/logger.py`）

