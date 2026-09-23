# R5 调研报告：文件类工具与工具基建现状

> 调研对象：narnat agent（`D:\desktop\NarnatAgent`）中 `narnat_agent/tools/` 下的文件类工具与工具基建。
> 调研方法：逐文件完整通读（Read 工具，带行号）+ grep 交叉核对 import 边与引用点；全部行号以当前工作区文件为准（未提交修改的前提下）。
> 只读约束：本报告是唯一产出物；未创建/修改/删除任何源文件，未执行任何 git 写操作，未发起网络调用。
> 行数核验命令：`bash -l -c "wc -l <files>"`（结果见下表，与任务书预估行数不同的文件已标注）。

## 0. 范围与行数基线

| # | 文件 | 实际行数 | 任务书预估 | 差异说明 |
|---|------|---------|-----------|---------|
| 1 | narnat_agent/tools/__init__.py | 0 | ? | 空文件 |
| 2 | narnat_agent/tools/tool_context.py | 84 | 120 | 实际更短 |
| 3 | narnat_agent/tools/registry.py | 197 | 197 | 一致 |
| 4 | narnat_agent/tools/token_estimate.py | 57 | 40 | 实际更长 |
| 5 | narnat_agent/tools/param_utils.py | 25 | 60 | 实际更短 |
| 6 | narnat_agent/tools/diff_utils.py | 55 | 80 | 实际更短 |
| 7 | narnat_agent/tools/read/__init__.py | 247 | 247 | 一致 |
| 8 | narnat_agent/tools/glob/__init__.py | 576 | 576 | 一致 |
| 9 | narnat_agent/tools/grep/__init__.py | 591 | 591 | 一致 |
| 10 | narnat_agent/tools/edit/__init__.py | 230 | 230 | 一致 |
| 11 | narnat_agent/tools/write/__init__.py | 129 | 129 | 一致 |
| 12 | narnat_agent/tools/web_search/__init__.py | 178 | 178 | 一致 |
| 13 | narnat_agent/tools/todo_write/__init__.py | 104 | 60 | 实际更长 |
| 14 | narnat_agent/tools/goal_complete/__init__.py | 63 | 40 | 实际更长 |
| 15 | narnat_agent/tools/mcp_tool/__init__.py | 111 | 111 | 一致 |

同组外的关联文件（本组依赖或依赖本组，报告中被引用但不在 15 个调研清单内，未逐节展开）：
`narnat_agent/tools/exec_signal.py`（错误/退出码标签协议）、`narnat_agent/tools/terminal/__init__.py`、`narnat_agent/tools/terminal/remote.py`、`narnat_agent/tools/bash/__init__.py`、`narnat_agent/tools/serial/__init__.py`、`narnat_agent/tools/background/__init__.py`、`narnat_agent/core/{agent,agent_loop,tool_dispatcher,llm,compressor,tool_callbacks}.py`、`narnat_agent/mcp/__init__.py`、`narnat_agent/config/loader.py`、`narnat_agent/assembly.py`。

---

### narnat_agent/tools/__init__.py（0行）

**职责**：空包标记文件，使 `narnat_agent.tools` 成为 Python 包。

**对外接口**：无（无 export、无初始化代码、`wc -l` = 0）。

**依赖**：无 internal import。

**被依赖**：所有 `from ..tools.<子模块> import ...` 的相对导入都会先执行本文件（空）：
- `narnat_agent/tools/read/__init__.py:11`、`glob` 同级、`grep/__init__.py:18-19`、`edit/__init__.py:14-16`、`write/__init__.py:9-10`、`terminal/remote.py:11`、`registry.py:11-29`、`assembly.py:25`、`core/agent_loop.py:14`、`core/tool_dispatcher.py:11-17`、`core/compressor.py:8`、`core/llm.py:214`
- 绝对路径导入方（示例）：`tool_exp/mcp_test/test_mcp.py:22-23`、`tool_exp/audit_def_cost.py:7`

**状态**：无模块级全局、无类变量、无实例状态。

**行为要点**：
1. 文件为 0 行空文件：import 它无任何副作用（无 re-export、无版本声明、无 `__all__`）。

**边界/异常行为**：
1. 无（无代码）。

**补丁痕迹**：
1. 低：包 `__init__` 不做任何聚合，所有消费方均深引用具体子模块（如 `registry.py:11-29`、`read/__init__.py:11-12`），重构时任何模块搬迁都会牵动全部引用点（证据：`core/tool_dispatcher.py:11-17` 一次 import 4 个不同子模块）。

**可测性**：
- 可独立单测的部分：可做"import 无副作用"断言（`import narnat_agent.tools` 后 `dir()` 为空），价值低。
- 需要集成测试的部分：无。
- 无法自动化的部分：无。

---

### narnat_agent/tools/tool_context.py（84行）

**职责**：工具运行时上下文 dataclass，统一承载跨工具的共享状态与回调（替代"模块级全局变量 + setter 注入"）。

**对外接口**：
- `AWAIT_CONFIRM = "__AWAIT_CONFIRM__"`（L12）：删除确认哨兵值（bash/terminal/serial 检测到删除命令时返回给主循环）。
- `@dataclass class ToolContext`（L15-16）字段（全部有默认值，可裸构造）：
  - `confirm_callback: Optional[Callable[[str], bool]] = None`（L20）
  - `ui_callback: Optional[Callable[[Any], None]] = None`（L23）
  - `api_keys: Dict[str, str] = field(default_factory=dict)`（L26）
  - `ignore_dirs: List[str] = field(default_factory=list)`（L29）
  - `git_skip_confirm: bool = False`（L32）
  - `rm_skip_confirm: bool = False`（L33）
  - `max_transfer_mb: int = 100`（L35）
  - `max_tool_output_chars: int = 65536`（L38）
  - `max_timeout_seconds: int = 1800`（L41）
  - `require_plan: bool = False`（L44）
  - `min_tools: int = 2`（L45）
  - `mcp_manager: Any = None`（L48）
  - `current_todos: list = field(default_factory=list)`（L51）
  - `pending_delete: Optional[Tuple[str, dict]] = None`（L55）
  - `_delete_confirmed: bool = field(default=False, repr=False)`（L58）
  - `goal_complete: bool = field(default=False, repr=False)`（L61）
  - `todo_reminded: bool = field(default=False, repr=False)`（L65）
  - `bg_reminded: bool = field(default=False, repr=False)`（L69）
- `def confirm_delete(self, command: str) -> bool`（L71）：调用 `confirm_callback`，无回调返回 False。
- `def on_todo_update(self, todos)`（L77）：调用 `ui_callback`。
- `def get_api_key(self, name: str) -> str`（L82）：`api_keys.get(name, "")`。

**依赖**（本文件 import 的内部模块）：无内部模块；仅 `dataclasses`（L7）、`typing`（L8）。

**被依赖**（谁 import 本文件）：
- `narnat_agent/assembly.py:25`（`from .tools.tool_context import ToolContext`），L100-113 是生产环境唯一构造点（注入 confirm_callback/ui_callback/api_keys/ignore_dirs/各上限/require_plan/min_tools/mcp_manager）。
- `narnat_agent/core/agent_loop.py:14`（`ToolContext, AWAIT_CONFIRM`）：L29 构造签名、L239-254 处理 `pending_delete`+`AWAIT_CONFIRM`、L350-374 读 `current_todos`/写 `todo_reminded`/`bg_reminded`、L411-453 删除确认流程（L451 写 `_delete_confirmed = True`）。
- `narnat_agent/core/tool_dispatcher.py:16`（ToolContext 类型注解）、L76 构造。
- `narnat_agent/tools/registry.py:12`（类型注解）。
- `narnat_agent/tools/serial/__init__.py:16`（`from ..tool_context import AWAIT_CONFIRM`）。
- 不 import 但按 duck-typing 消费字段的模块（同一契约的隐式实现方）：`tools/bash/__init__.py:469-491`、`tools/terminal/__init__.py:541-567,612-634,959-960`、`core/agent.py:84-91`（复位 4 个字段）。
- 实验/回归脚本：`tool_exp/_obsolete_20260910/regression_test.py:74`、`regression_test_v2.py:76`、`verify_patch.py:8`、`verify_tool_ux_fixes.py:9`、`verify_ux_round3.py:11`、`tool_exp/mcp_test/{demo_mcp_flow.py:19,demo_persistence.py:22,test_mcp.py:23,test_mcp_runtime.py:25}`、`tool_exp/repair_seq_test.py:15`、`tool_exp/repro_mcp_fail_label.py:16`、`tool_exp/test_esc_interrupt_regression.py:253`。

**状态**：
- 模块级全局：`AWAIT_CONFIRM`（L12，不可变字符串常量；值被 `tools/bash/__init__.py:491`、`tools/terminal/__init__.py:567,634` 以字面量复制）。
- 类变量：无。
- 实例状态：上述 18 个字段即全部实例状态；可变字段（`api_keys`/`ignore_dirs`/`current_todos`/`pending_delete`）由外部就地读写。

**行为要点**：
1. `confirm_delete`（L71-75）：无 `confirm_callback` 时返回 False；有则透传其返回值（不捕获异常）。
2. `on_todo_update`（L77-80）：无 `ui_callback` 时静默；有则调用（注意 `todo_write/__init__.py:82-83` 未走此方法而直接调 `ui_callback`）。
3. `get_api_key`（L82-84）：未配置的键返回空串 `""`（不抛 KeyError）。
4. 全部字段有默认值（L20-69）：`ToolContext()` 可零参构造（对测试友好）；`repr=False` 用于 3 个标志位，避免日志噪音（L58/61/65/69）。
5. `_delete_confirmed` 语义（L57-58 注释）：置 True 后下一次删除命令跳过确认；由 `tools/bash:479-480`、`tools/terminal:554-555,622-623`、`tools/serial:325-326` 消费后立即复位为 False。
6. `pending_delete` 语义（L53-55 注释）：Linux/macOS 下暂存 `(工具名, 参数字典)`，主循环在 `#` 提示符下等用户确认后重执行（`core/agent_loop.py:239-254,411-453`）。
7. `goal_complete`/`todo_reminded`/`bg_reminded` 三个标志的"每轮复位"契约由消费方实现：`core/agent.py:84-91` 每次用户新输入时复位 `goal_complete`/`todo_reminded`/`bg_reminded`/`current_todos`（L82-91 注释说明原因）。
8. 字段注释显式标注平台差异（L19）：删除确认回调"仅 Windows 使用"，Linux/macOS 走 AWAIT_CONFIRM 机制。
9. `max_tool_output_chars` 默认 65536 与配置换算点分离：`config/loader.py:806-807` 把配置"输出上限KB"换算成字符数，`assembly.py:108` 注入。

**边界/异常行为**：
1. `confirm_delete` 不捕获回调异常（L73-74）：回调抛错会把异常冒泡到工具执行层（`registry.execute` 的 `except Exception`，registry.py:155-156）→ 表现为"工具执行失败"。
2. `max_transfer_mb=0` 表示不限制（L35 注释）；`max_tool_output_chars=0` 表示不限制（L37）；`max_timeout_seconds=0` 表示不限制（L40）——三个"0 = 关"语义在 registry/terminal/background 分别解释（registry.py:138、terminal:226-227、background:691-692）。
3. `_delete_confirmed` 若被外部置 True 而删除命令未到来，标志会残留到下一次删除（消费者只在命中删除检测时复位：bash:479-480）——潜在跨轮残留（低概率，需外部误置位才会发生）。
4. 未验证：`mcp_manager: Any` 无类型约束，`mcp_tool` 用 `getattr(_tool_context, "mcp_manager", None)` 读取（mcp_tool/__init__.py:58）。

**补丁痕迹**：
1. 高：`AWAIT_CONFIRM` 契约靠字符串字面量对齐而不 import 常量——`tools/bash/__init__.py:491`、`tools/terminal/__init__.py:567,634` 直接写 `"__AWAIT_CONFIRM__"`；改常量值会静默破坏确认流程（判据：跨层引用 + 魔法值）。
2. 中：三个 `__dict__` 方法与 18 字段枢纽共存，其中 3 个方法在生产代码中**无调用方**（死代码）：`confirm_delete`（L71-75，消费方直接用 `tc.confirm_callback(command)`：`tools/bash:476`、`tools/terminal:549,619`、`tools/serial:320`）、`on_todo_update`（L77-80，`todo_write:82-83` 直调 `ui_callback`）、`get_api_key`（L82-84，`web_search:163-164` 直读 `api_keys`）。grep 复验：`confirm_delete|get_api_key|.on_todo_update(` 仅命中外层调用点与 tool_exp 脚本。
3. 中：职责混杂（判据：职责混杂）——删除确认、UI 回调、密钥、忽略目录、7 类上限/开关、MCP 管理器、todo 状态、目标完成标记同宿一个 dataclass（L19-69）；状态机分散在三处（工具写、agent.py 复位、agent_loop 读）。
4. 中：字段"存在但无写者/无读者"不对称：`require_plan`/`min_tools` 由 assembly 注入、仅在 `core/tool_dispatcher.py:334,354` 读；`max_transfer_mb` 由 assembly 注入、仅在 `terminal:959-960` 读；`ignore_dirs` 只有 glob/grep 读（glob:485-486、grep:195）——上下文对象成为事实上的"全局配置袋"。
5. 低：`pending_delete` 用裸元组 `Tuple[str, dict]`（L55）承载跨模块协议，字段协议未类型化。

**可测性**：
- 可独立单测的部分：`confirm_delete`/`on_todo_update`/`get_api_key`（注入 stub 回调断言调用与返回值）；dataclass 默认值断言（含 `field(default_factory=...)` 不共享可变默认）。
- 需要集成测试的部分：`pending_delete` + `_delete_confirmed` 的跨工具确认流程（涉及 agent_loop 主循环与 bash/terminal/serial）。
- 无法自动化的部分：Windows 交互式确认（`core/tool_callbacks.py:16-23` 用 `input()`，需 TTY）。

---

### narnat_agent/tools/registry.py（197行）

**职责**：工具注册表 —— 工具名→实现映射、LLM 工具定义收集、动态工具增删、统一 `execute` 入口（上下文注入、异常包装为中文提示、全局输出硬截断）。

**对外接口**：
- `TOOL_DEFINITIONS: List[Dict]`（L57-61）：内置工具定义列表（9 个工具 DEFINITION 顺序为 READ/GLOB/GREP/EDIT/WRITE/BASH/TERMINAL/WEBSEARCH/TODOWRITE/SERIAL/MCP）。
- `def register_dynamic_tools(entries: List[tuple]) -> None`（L74）：注册运行时动态工具（当前来源：MCP 服务器 tools/list）；与内置或已注册动态工具重名时跳过。
- `def unregister_dynamic_tools(names: List[str]) -> None`（L87）：注销动态工具（内置不受影响）。
- `def execute(name: str, arguments: Dict[str, Any], tool_context: Optional[ToolContext] = None) -> tuple`（L108）：执行工具，返回 `(llm_result, color_diff)`。
- `def get_tool_names() -> List[str]`（L190）：全部工具名（内置 + 动态）。
- `def get_tool_definitions() -> List[Dict]`（L195）：`TOOL_DEFINITIONS + _DYNAMIC_DEFINITIONS`。
- 私有：`def _friendly_type_error(name: str, impl: Callable, err: TypeError) -> str`（L159）。
- 模块私有数据：`_TOOL_IMPLEMENTATIONS: Dict[str, Callable]`（L36-50，12 个工具名→实现：Read/Glob/Grep/Edit/Write/Shell/Terminal/WebSearch/TodoWrite/Serial/MCP/GoalComplete）、`_DYNAMIC_IMPLEMENTATIONS: Dict[str, Callable]`（L70）、`_DYNAMIC_DEFINITIONS: List[Dict]`（L71）、`_CONTEXT_TOOLS: set`（L105）。

**依赖**（行号）：
- `re`（L8）、`typing`（L9）
- `.exec_signal.error_line`（L11）
- `.tool_context.ToolContext`（L12）
- `.token_estimate.estimate_text_tokens`（L13）
- 各工具模块（L16-29）：`background`（L16）、`read`（L17）、`glob`（L18）、`grep`（L19）、`edit`（L20）、`write`（L21）、`bash`（L22）、`terminal`（L23）、`web_search`（L25）、`todo_write`（L26）、`serial`（L27）、`mcp_tool`（L28）、`goal_complete`（L29，只取 execute 不取 DEFINITION）。

**被依赖**：
- `narnat_agent/assembly.py:61`（`from .tools.registry import get_tool_definitions`）；L62 调用。
- `narnat_agent/core/tool_dispatcher.py:11`（`from ..tools.registry import execute as tool_execute`）；L227 调用。
- `narnat_agent/core/agent_loop.py:452`（函数内 `from ..tools.registry import execute as tool_execute`）；L453 调用。
- `narnat_agent/mcp/__init__.py:117`（`get_tool_names, register_dynamic_tools`，L127/L136 调用）、L172（`unregister_dynamic_tools`，L173 调用）。
- 实验脚本：`tool_exp/audit_def_cost.py:7`、`audit_params_surface.py:7`、`verify_assembly.py:41,47`、`verify_goal_e2e.py:23,87`、`verify_goal_mode.py:40`、`verify_import_cleanup.py:7`、`verify_write_free.py:10`、`repro_tool_block_undercount.py:11`、`replay_real_turnB.py:11`、`_obsolete_20260910/…`。

**状态**：
- 模块级全局（可变）：`_DYNAMIC_IMPLEMENTATIONS`（写：L83、L93；读：L81、L122、L192）、`_DYNAMIC_DEFINITIONS`（写：L84、L94-97；读：L197）；`TOOL_DEFINITIONS`（L57，无写入者，但为可变 list 且被上游 `core/llm.py:189` 拷贝后共享语义）。
- 类变量：无（本文件无 class）。
- 实例状态：无（纯模块级函数）。
- 隐含进程级状态：无（连接态由 MCP 管理器持有）。

**行为要点**：
1. 名称解析：内置优先，其次动态（L122）。
2. 上下文注入条件：`tool_context` 非 None 且 `name in _CONTEXT_TOOLS`（L127-130）；`_CONTEXT_TOOLS = {"Shell","Terminal","TodoWrite","WebSearch","Read","Glob","Grep","Serial","MCP","GoalComplete"}`（L105）。
3. 返回值归一：工具返回 tuple → `(llm_result, color_diff)`；非 tuple → `(result, "")`（L131-135）。
4. 全局输出硬截断（L137-150）：仅当 `tool_context.max_tool_output_chars > 0` 且 `len(llm_result) > 上限`；保留前 2/3（`head`）+ 后 1/3（`tail`），中间插入提示 `...[全局截断: 输出共N字符(≈Mtoken), 已达全局上限KKB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]`（L146-150）。
5. 截断提示里的 `KKB` 用整除：`limit_kb = max_tool_output_chars // 1024`（L141）。
6. 未知工具 → `(error_line("未知工具: X"), "")`（L123-124）。
7. `TypeError` → `(error_line("工具参数错误(name): ..."), "")`（L153-154）；其他异常 → `(error_line("工具执行失败(name): e"), "")`（L155-156）。
8. `_friendly_type_error`：`re.search(r"unexpected keyword argument '([^']+)'", msg)` 命中 → `"收到未知参数 'x'。<工具> 的有效参数: a, b, ..."`（L176-179）；`"missing"` 命中 → `"缺少必填参数: ...。<工具> 的有效参数: ..."`（L180-186）；都不命中 → 原样返回（L187）。
9. 有效参数表来自 `inspect.signature(impl)`，排除 `_tool_context` 与 `kwargs`（L167-174）；签名获取失败 → `params = []`（L173-174）→ 提示降级为原文。
10. `register_dynamic_tools`：重名跳过（内置 L81 前半 / 已注册动态 L81 后半）；重复调用幂等（L80-84）。
11. `unregister_dynamic_tools`：空/None 名单直接返回（L89-91）；实现按名 pop（L92-93）；定义按 `d.get("function", {}).get("name")` 过滤（L94-97）。
12. `get_tool_names` 顺序：内置在前，动态在后（L192）。
13. GoalComplete 的执行能力**始终**注册（L29/L49），但定义不进 `TOOL_DEFINITIONS`（L62-63 注释）；由 `/goal` 开启时经 `core/llm.py:207-226` 的 `set_goal_tool(True)` 动态注入到 LLM 请求（定义来源 `tools/goal_complete/__init__.py:7-25`，见 `core/llm.py:214`）。

**边界/异常行为**：
1. `tool_context=None` → 不注入上下文、不做截断（L127、L138），适用于直接调用工具函数的测试场景。
2. `max_tool_output_chars <= 0` → 不截断（L138）。
3. 上限 < 1024 时截断提示显示 "0KB"（L141 整除）。
4. 工具内部的 `TypeError`（代码 bug）也会被归类为"工具参数错误"（L153-154）——误分类。
5. 工具内部 `SystemExit`/`KeyboardInterrupt`（BaseException）不会被捕获（L155 只 `except Exception`）。
6. 截断按字符数（非字节/token），`est` 只是提示用估算（L145）。
7. 动态工具定义若缺 `function.name`，`unregister` 过滤时不会命中（L96 用 `.get` 链，安全）。
8. `impl(**arguments)` 用关键字展开：LLM 传多余参数或错名 → TypeError → 中文提示（L128-130）。

**补丁痕迹**：
1. 高：全局截断存在**两套并行实现**且策略不同——registry 保留"首尾"面向 Shell 输出（L137-150，L137 注释"尾部常含提示符等关键状态信息"），Read 自建按行截断并给 offset（`read/__init__.py:22-64`）。两处都写 `≈Ntoken` 提示，格式不完全一致（判据：跨层重复 + 职责混杂）。
2. 中：`_TOOL_IMPLEMENTATIONS`（L36-50）与 `TOOL_DEFINITIONS`（L57-61）是两份需人工同步的清单，且导入 12 个工具模块（L16-29）使本文件成为聚合中枢；新增工具需改多行（L5-6 注释自承"新增工具：新建目录 + 在此处加2行导入"）。
3. 中：`_friendly_type_error` 依赖 `inspect` + 正则解析 CPython 英文异常文本（L167-186）——脆弱边界（Python 版本/措辞变化即失效），且单/多参数缺失用 `re.findall(r"'([^']+)'")` 抽取（L183）会把消息中其它引号内容一并当作参数名。
4. 中：`except TypeError` 把工具实现内部 bug 归类为参数错误（L153-154，判据：吞异常/误分类）。
5. 低：`import inspect` 放在函数体（L168），每次调用买单。
6. 低：模块级可变容器无锁保护（L70-71）：`register/unregister` 由 `mcp/__init__.py:118,164` 的锁内调用间接串行化，但 registry 本身不设防。
7. 低：`TOOL_DEFINITIONS` 暴露为可变公共 list（L57），外部可原地改（当前无写入者，靠约定保护）。

**可测性**：
- 可独立单测的部分：`_friendly_type_error`（构造 TypeError 断言中英文案）；`register_dynamic_tools`/`unregister_dynamic_tools`（含重名跳过、幂等、空名单）；`get_tool_names`/`get_tool_definitions`（顺序与合并语义）；`execute` 的错误分支（未知工具、参数错名、工具异常）——可用动态工具注入假实现（`register_dynamic_tools([(name, defn, impl)])`）来测截断与 tuple 归一。
- 需要集成测试的部分：真实 12 个工具的注册完整性（数量/名称映射）；全局截断对长输出的效果；GoalComplete 的"执行存在但定义不暴露"契约（配合 `core/llm.py:set_goal_tool`）。
- 无法自动化的部分：无（模块级可变状态使测试间需手工清理 `_DYNAMIC_*`，无公开重置 API → 建议重构时补 reset）。

---

### narnat_agent/tools/token_estimate.py（57行）

**职责**：统一 token 估算的混合密度启发式（CJK≈0.7 token/字、其余≈0.25），供截断提示与压缩估价共用。

**对外接口**：
- `def estimate_text_tokens(text: str) -> int`（L28）：估算一段文本 token 数（向上取整，空文本 0）。
- `def estimate_message_tokens(msg: dict) -> int`（L38）：估算一条消息（content/thinking/tool_calls + 固定开销）。

**依赖**（行号）：`re`（L13）；无内部模块 import。

**被依赖**：
- `narnat_agent/tools/registry.py:13`（L145 使用）。
- `narnat_agent/tools/read/__init__.py:12`（L53 使用）。
- `narnat_agent/core/compressor.py:8`（`estimate_message_tokens`，L18 使用）。
- `narnat_agent/tools/bash/__init__.py:17`（L400 使用）。
- `narnat_agent/tools/terminal/ssh_session.py:39`（L59 使用）。
- `tool_exp/verify_shell_three_fixes.py:84`。

**状态**：
- 模块级全局（全部不可变）：`_CJK_RE`（L16-17，正则，字符类含 CJK 统一表意/全角/假名/谚文）、`_TOKEN_PER_CJK = 0.7`（L21）、`_TOKEN_PER_OTHER = 0.25`（L22）、`_MSG_OVERHEAD = 8`（L25）。
- 类变量：无。实例状态：无。

**行为要点**：
1. `estimate_text_tokens`：`if not text: return 0`（L30-31）；`cjk = len(_CJK_RE.findall(text))`（L32）；`other = len(text) - cjk`（L33）；`est = cjk*0.7 + other*0.25`（L34）；`int(est + 0.999)` 向上取整（L35）。
2. `estimate_message_tokens`：起始 `_MSG_OVERHEAD = 8`（L43）；`content` 为 str → 文本估算（L45-46）；`content` 为真值的非 str（块列表）→ `str(content)` 估算（L47-49）；`thinking` 计入（L50-52）；每个 `tool_calls[i].function.name` 与 `arguments` 分别计入（L53-56）。
3. 浮点 `+0.999` 而不是 `math.ceil`（L35 注释"ceil，避免小文本归 0"）。

**边界/异常行为**：
1. 极小文本：1 个 ASCII 字符 → `0.25 + 0.999 = 1.249 → int = 1`（L34-35）。
2. 空字符串/None：`None` 会落到 `not text` → 返回 0（L30）；但 `estimate_message_tokens` 传入的 content 若为 `0`/`False` 等假值也会被跳过（L45-47）。
3. `tool_calls` 为 None → `msg.get("tool_calls") or []` 兜底（L53）。
4. `fn.get("arguments", "") or ""` 兜底 None（L56）。
5. 未验证：与真实 API usage 的误差（文档 L19-20 称"中文约 0.7 token/字"，无仓库内实测基准）。
6. CJK 判定覆盖 `\u3040-\u30ff`（假名）、`\u3400-\u4dbf`、`\u4e00-\u9fff`、`\uf900-\ufaff`、`\uff00-\uffef`（全角）、`\uac00-\ud7af`（谚文）（L16-17）；emoji、西里尔等按 other 计 0.25。

**补丁痕迹**：
1. 中：本模块被 4 处消费（registry/read/bash/ssh_session）而文件头注释 L8-10 只列 2 处（compressor + "registry / bash / read"），未提 `terminal/ssh_session.py:59`——注释与现状漂移（判据：历史包袱注释）。
2. 低：魔数 `0.999`（L35）、`8`（L25）、`0.7/0.25`（L21-22）均无可配入口；系数变更影响所有截断提示文案（跨模块隐式耦合）。
3. 低：`estimate_message_tokens` 对"块列表"形态用 `str(content)`（L48-49）估算，误差大（自带自认"防御性"）。

**可测性**：
- 可独立单测的部分：两个函数全部——纯函数、零状态；表驱动断言（空、纯中文、纯英文、混合、单字符、超长、None 兜底、tool_calls 结构）。
- 需要集成测试的部分：与真实 `input_tokens` 的校准（需要 API 调用，本任务禁止；属"未验证"）。
- 无法自动化的部分：无。

---

### narnat_agent/tools/param_utils.py（25行）

**职责**：把 LLM 可能传成字符串的布尔参数归一化为 bool（避免 `bool("false") == True` 的反意图行为）。

**对外接口**：
- `def to_bool(v) -> bool`（L9）：bool 原样；字符串按白名单解析；其他类型 `bool(v)`。

**依赖**（行号）：无 import（零依赖，纯函数）。

**被依赖**：
- `narnat_agent/tools/edit/__init__.py:15`（L97 使用，注释 L95-96 说明"全量替换（与意图相反的危险行为）"）。
- `narnat_agent/tools/grep/__init__.py:18`（L175 使用，`re.IGNORECASE` 开关）。

**状态**：无模块级全局、无类变量、无实例状态。

**行为要点**：
1. `isinstance(v, bool)` → 原样返回（L17-18）。
2. 字符串：`s = v.strip().lower()`（L20）；`s in ("false","0","no","off","f","n","")` → False（L21-22）；`s in ("true","1","yes","on","t","y")` → True（L23-24）。
3. 其他类型（含 None、数字、列表等）→ `bool(v)`（L25）。

**边界/异常行为**：
1. 未列入白名单的字符串落入 `bool(v)`（L25）→ 非空串恒 True：如 `"2"`、`"maybe"`、`"disabled"` 都是 True；`"否"`、`"假"` 不识别（潜在反意图）。
2. 空白串 `""`/`"  "` → False（L21 白名单含空串，`strip` 后判空）。
3. None → False（L25）。
4. 数字 `0` → False，`0.0` → False，其他数字 → True（L25）。
5. 数字字符串 `" 1 "` → strip 后命中 → True（L20/L23-24）。

**补丁痕迹**：
1. 中：容错模式**不成体系**——同一问题（LLM 传错类型）在别处各自手写：`read/__init__.py:131-132`（int 转换）、`grep/__init__.py:163-169`（int + 错误文案）、`web_search/__init__.py:150-153`、`glob/__init__.py:473-476`；而 `edit/grep` 的布尔归一才走本模块（判据：重复代码 + 职责混杂）。
2. 低：白名单外的字符串回退 `bool(v)`（L25）不是"拒绝"，无错误提示路径（AI 传 `"enable"` 会被静默当 True）。

**可测性**：
- 可独立单测的部分：`to_bool` 全部分支（表驱动：白名单两集合、大小写、空白、未知串、数字、None、bool）。
- 需要集成测试的部分：无。
- 无法自动化的部分：无。

---

### narnat_agent/tools/diff_utils.py（55行）

**职责**：对 unified diff 文本加 ANSI 颜色（终端展示用）；以及"正文相同但字节不同"的差异摘要（Write 变更判定用）。

**对外接口**：
- `def colorize_diff(diff_text: str) -> str`（L9）：`-` 行红、`+` 行绿、`@@` 行暗青、`---/+++` 加粗青、其余灰。
- `def describe_bytes_only_change(old_bytes: bytes, new_bytes: bytes) -> str`（L32）：描述行尾符/末尾换行/字节表示变化。

**依赖**（行号）：`..output` 的颜色常量（L6）：`RST as R, BLD as B, DIM as D, GRY as G, CYN as C, GRN as E, RED as X`（无内部逻辑依赖，仅常量）。

**被依赖**：
- `narnat_agent/tools/edit/__init__.py:14`（L230 使用 `colorize_diff`）。
- `narnat_agent/tools/write/__init__.py:9`（L90、L112 使用两函数）。
- `narnat_agent/tools/terminal/remote.py:11`（L166、L196-198、L289、L292 使用两函数）。

**状态**：无模块级全局；颜色常量 import 自 `output`（不可变 str）。

**行为要点**：
1. `colorize_diff` 空输入或 `"[无差异]"` → 返回 `{G}[无差异]{R}`（L14-15）；`None` 也命中 `not diff_text`（L14）。
2. 逐行前缀判定顺序（L19-28）：`---`/`+++` → `B+C`；`@@` → `D+C`；`-` → `X`（红）；`+` → `E`（绿）；其余 → `G`（灰）。
3. 行以 `"\n"` 重新拼接（L29）——保留空行、不改文本结构（L11-12 注释）。
4. `describe_bytes_only_change`：内嵌 `_style(b)` 统计 `\r\n`/`\n`/`\r` 并合成 `"CRLF+LF"` 类描述，无换行符时返回 `"无换行符"`（L39-43）。
5. 输出片段组合：行尾符不同 → `"行尾符 {old}→{new}"`（L48-49）；末尾换行不同 → `"末尾换行已添加/已移除"`（L50-51）；都不满足（字节仍不同）→ `"换行符以外的字节表示变化（N→M字节，如编码或BOM差异）"`（L52-54）；多片段用 `"、"` 连接（L55）。

**边界/异常行为**：
1. `diff_text` 为 None → 返回 `[无差异]` 着色（L14）——签名声明 str，实际容错。
2. `---foo`（普通上下文行恰以 `---` 开头）会被误判为 header（L19）——对 unified diff 输入无影响（header 总是 `---`），但输入非标准 diff 时着色可能不准。
3. `_style` 对纯 CR 换行（旧 Mac）→ `"CR"`（L43）。
4. 两文件都无换行符且字节不同 → `_style` 相同（`"无换行符"`）、末尾换行同为 False → 落入 L52-54 的兜底描述。
5. 只在"行尾符/末尾换行均一致"时才输出兜底文案（L52），字节数差异嵌在文案里（L54）。

**补丁痕迹**：
1. 中：与 `narnat_agent/ui/renderer.py:300-316` 存在**重复实现**的同名函数 `colorize_diff`（UI 层注释 L296-298 自承"与 tools/diff_utils.py 逻辑一致，此处供 UI 层使用"）——两套颜色常量（`ui/renderer.py` 用 `DIFF_HEADER/DIFF_RANGE/...`）、两份需同步的判定逻辑（判据：重复代码）。
2. 低：颜色单字母别名映射 `BLD as B`、`GRN as E`、`RED as X`（L6）语义不透明；`E` 实为绿、`X` 实为红，易与 `ui/colors` 的命名体系混淆。
3. 低：`_style` 对同一 bytes 做 3 次 `count` 全扫描（L40-42），大文件下 6 次全扫描。
4. 低：颜色常量通过 `..output` 中转（L6），使 tools 层依赖 UI 输出层模块（跨层引用）。

**可测性**：
- 可独立单测的部分：`colorize_diff`（各前缀行、空、`[无差异]`、None）；`describe_bytes_only_change`（CRLF↔LF、末尾换行增减、BOM 增减、同风格不同字节、空 twins）。
- 需要集成测试的部分：无。
- 无法自动化的部分：ANSI 实际渲染效果（可由 `aichat -f 截图` 人工确认，非自动）。

---
### narnat_agent/tools/read/__init__.py（247行）

**职责**：读取本地/远程纯文本文件内容，输出带行号文本；自动识别 UTF-8/GBK 编码；超出单行/行数/全局字符上限时按行截断并给出 `offset` 续读提示。

**对外接口**：
- `READ_MAX_LINE_CHARS = 2000`（L17）：单行显示上限（对齐官方 harness readMaxLineLength 默认值，L15-16 注释）。
- `READ_MAX_COUNT_BYTES = 20 * 1024 * 1024`（L19）：截断时统计总行数的文件大小上限。
- `def _apply_global_cap(text: str, _tool_context, total_lines: int = None) -> str`（L22）：按行做全局输出上限截断 + offset 续读提示。
- `DEFINITION`（L66-89，见下"数据契约"）。
- `def _detect_text_encoding(head: bytes) -> str`（L92）：首块字节判编码，返回 `"utf-8-sig"` 或 `"gbk"`。
- `def execute(file_path: str, offset: int = 0, limit: int = 2000, device: str = "", _tool_context=None) -> str`（L114-116）：主入口，返回带行号内容字符串。

**数据契约（DEFINITION 全文，L66-89）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "读取纯文本文件内容，返还内容带行号（由1开始）。"
            "自动识别UTF-8/GBK编码。"
            "支持本地或远程读取文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始行（默认1，含本行）"},
                "limit": {"type": "integer", "description": "读取行数（正整数，默认2000）"},
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即读取本地指定文件，设置dev1..devn则读取指定远程设备文件",
                },
            },
            "required": ["file_path"],
        },
    },
}
```

**依赖**（行号）：
- `os`（L8）、`re`（L9）
- `..terminal` 的 `_normalize_device_for_tools`, `_file_tool_device_hint`（L11）
- `..token_estimate.estimate_text_tokens`（L12）
- 函数内延迟导入：`..terminal.remote.remote_read`（L141）

**被依赖**：
- `narnat_agent/tools/registry.py:17`（`from .read import execute as read_execute, DEFINITION as READ_DEF`）。
- `narnat_agent/tools/edit/__init__.py:59`（函数内 `from ..read import _detect_text_encoding`；L64 使用）。
- `narnat_agent/tools/write/__init__.py:79`（函数内 import；L87 使用）。
- `narnat_agent/tools/terminal/remote.py:77`（函数内 import；L78 使用）。
- 实验脚本：`tool_exp/_obsolete_20260910/regression_test_v2.py:75`、`verify_ai_ux_fixes.py:11`、`verify_tool_ux_fixes.py:7`、`verify_ux_round3.py:8`、`tool_exp/verify_input_silent_and_race.py:18`、`tool_exp/verify_remote_file_reconnect.py:8`。

**状态**：
- 模块级全局（不可变）：`READ_MAX_LINE_CHARS`（L17）、`READ_MAX_COUNT_BYTES`（L19）。
- 类变量：无。实例状态：无（纯函数 + 每次调用局部变量）。

**行为要点**：
1. 参数归一（L131-132）：`offset = int(offset) if offset is not None else 0`；`limit` 同理（默认 2000）；`limit <= 0` → `"[错误: limit需为正整数]"`（L133-134）。
2. 设备归一（L136-138）：调用 `_normalize_device_for_tools(device)`；返回 None（非法 devN）→ `"[错误: {_file_tool_device_hint()}]"`（附当前已连接设备清单）。
3. 远程分支（L140-147）：`remote_read(file_path, offset, limit, device)`；成功结果（不以 `[` 开头）加设备头 `"[{device}] {file_path}\n{result}"`（L145-146）；随后仍走 `_apply_global_cap`（L147）。
4. 目录检测（L149-150）→ `"[错误: {path} 是目录，请用 Glob 匹配或 Shell 查看目录内容]"`。
5. 文件不存在（L152-154）→ 报错含 `os.getcwd()`。
6. 二进制检测（L156-166）：首 8192 字节含 `b"\x00"` → 拒绝；`PermissionError` → `"[错误: 权限不足: ...]"`（L160-161）；`OSError` → `"[错误: 读取失败: ...]"`（L162-163）。
7. 编码探测（L169）→ `_detect_text_encoding(head_bytes)`；读取用 `errors="replace"`（L172）。
8. 编码探测算法（L92-111）：首块 utf-8 严格解码成功 → `"utf-8-sig"`；`UnicodeDecodeError.start >= len(trial)-3`（疑似边界截断）→ 切掉错误起点重试，最多 3 次（L102-110）；其他失败 → `"gbk"`（L111）。
9. offset 语义（L173）：`start = max(offset - 1, 0) if offset > 0 else 0`（offset≤0 = 从头读）；跳过用流式 `readline` 并统计实际跳过行数 `skipped`（L176-180）。
10. 行格式化（L191-200）：行号 `start + i + 1`；内容 `rstrip("\n\r")`；格式 `f"  {line_num}→{content}"`；超长行截断为前 2000 字符 + `"...[单行截断: 本行共N字符,仅显示前2000字符]"`（L194-199）。
11. `limit` 截断判定（L186-207）：`for...else` 正常读满 limit 行后再读一行探测是否还有内容 → `truncated_by_limit`。
12. 截断时总行数统计（L211-218）：文件 ≤ 20MB 时 `total_lines = skipped + limit + 1 + sum(1 for _ in f)`；`OSError` → None；未截断且 `offset > 1` 时 `total_lines = skipped + len(result)`（L219-221）。
13. 空结果（L229-232）：`offset > 1` → `"[无内容: offset=N 已超出文件末尾（文件共M行）]"`；否则 `"[文件为空]"`。
14. limit 截断提示（L235-245）：有总行数 → `"  ... [截断: 已显示 {limit} 行。文件共{total}行，剩余{remain}行。使用 offset={skipped+limit+1} 参数可读取其余部分]"`；无总行数 → 去剩余行数版本。
15. `_apply_global_cap`（L22-64）：`max_chars <= 0` 或未超限直接返回（L30-32）；预算 `budget = max(max_chars - 200, 300)`（L35）；逐行累加到预算（首行无条件保留，L39-43）；正则 `^\s*(\d+)→` 找最后完整行号（L46-51）；带 `total_lines` 时提示剩余行数与 `offset=last_num+1`（L54-58），否则只给 offset（L59-61），完全找不到行号则降级提示（L62-63）。

**边界/异常行为**：
1. `offset` 传字符串数字/None（L131）；`limit` 传 `0`/负数 → 立即错误（L133-134）。
2. 空文件 → `"[文件为空]"`（L232）。
3. `offset` 超出末尾 → 提示含"文件共{skipped}行"（L231）——注意此处行数是"实际跳过行数"，对 offset 超界的文件即真实总行数（可能少于 offset 值）。
4. 超 20MB 且被 limit 截断 → 不统计总行数，提示降级（L214-218、L243-245）。
5. 单行超 2000 字符 → 只截前段（无尾部保留）（L194-199）。
6. 编码探测误判窗口：仅首 8KB；首块全 ASCII 而后续含 GBK 字节的文件会被按 `utf-8-sig` + `errors="replace"` 读出 U+FFFD（理论上可能；L158-159 只读 8192）。
7. `device` 非法值（如 `"host1"`）→ 错误 + 设备清单（L136-138，提示字符串见 `terminal/__init__.py:283-290`）。
8. 远程结果为错误（以 `[` 开头）不加设备头（L145）——用 `startswith("[")` 判定，若远程文件正文恰以 `[` 开头（错误分支外的成功结果）会被误判为错误结果而不加头（潜在边界）。
9. `_apply_global_cap` 的 `_tool_context=None` → `max_chars = 0` → 不截断（L30）。
10. 平台差异：无（本地/远程统一逻辑；远程经 SFTP，见 `terminal/remote.py:37-129`）。

**补丁痕迹**：
1. 高：工具自建全局截断，与 registry 的"保留首尾"策略并存（L22-64 vs `registry.py:137-150`）；L25-28 注释自承"注册表的全局截断策略(保留首尾)面向Shell类输出……对Read不适用"——同一"输出上限"配置项在两条路径产生不同截断形态（判据：跨层重复 + 职责混杂）。
2. 中：`_detect_text_encoding` 与 `grep/__init__.py:109-126` 是**逐字重复**的两份实现（同算法、同 3 字节窗口、同注释）——任一处修 bug 需同步另一处（判据：重复代码）。
3. 中：工具层依赖 terminal 层两个私有函数（L11）与远程实现（L141）；设备语义/提示文案由 `terminal/__init__.py:272-290` 定义，read/edit/write 三个工具都靠它（判据：跨层引用）。
4. 低：魔法值集中：8192（L159）、3（重试次数 L102）、2000（L197-198）、200/300（L35）、20MB（L19）。
5. 低：行号格式 `f"  {line_num}→{content}"`（L200）与续读定位正则 `^\s*(\d+)→`（L48）互为隐式契约，格式串出现在两处字面量。
6. 低：`_apply_global_cap(text, _tool_context, total_lines=None)` 对上下文用 `getattr(..., 0)` 容错（L30），宽松契约。

**可测性**：
- 可独立单测的部分：`_detect_text_encoding`（UTF-8/BOM/GBK/边界截断/空 bytes）；`_apply_global_cap`（max_chars 边界、续读 offset 正确性、total_lines 有无、无行号降级）；`execute` 本地分支（临时文件：空文件、目录、缺失、二进制 NUL、超长单行、offset/limit 组合、截断提示文案与 offset 数值）。
- 需要集成测试的部分：远程读取路径（需 SSH 会话或 mock `terminal.remote.remote_read`）；设备非法提示依赖 `terminal._list_devices()` 的会话状态。
- 无法自动化的部分：真实设备/网络异常（可 mock，但端到端需真机）。

---

### narnat_agent/tools/glob/__init__.py（576行）

**职责**：按 glob 模式匹配文件与目录（scandir 遍历 + pattern 预编译正则 + 堆维护 top-K），返回按修改时间倒序的路径列表。

**对外接口**：
- `class GlobLimits`（L18-21）：`MAX_BRACE_EXPANSIONS = 100`（L20）、`MAX_HARD_LIMIT = 50_000`（L21）。
- `def _expand_braces(pattern: str) -> list[str]`（L26）：花括号展开（支持 `\{ \} \,` 转义；无逗号/`..` 不展开）。
- `def _unescape_braces(pattern: str) -> str`（L111）：去花括号相关转义。
- `def _pattern_has_uppercase(pattern: str) -> bool`（L128）：smart case 判定（仅 POSIX 使用）。
- `@lru_cache(maxsize=256) def _compile_pattern(pattern: str) -> re.Pattern[str]`（L133-134）：glob→正则（带缓存）。
- `def _collect(root: str, regexes: list[re.Pattern[str]], ignore_dirs: set[str], max_results: int, skip_hidden_files: bool) -> tuple[list[tuple[str, float]], int]`（L265-271）：遍历+匹配，返回（mtime 降序结果, 总匹配数）。
- `def _norm_path(path: str, is_nt: bool) -> str`（L375）：Windows 下 `\`→`/`。
- `def _pattern_has_dot_component(pattern: str) -> bool`（L382）：pattern 任一组件以 `.` 开头。
- `def _hidden_files_hint(skip_hidden_files: bool) -> str`（L395）：隐藏文件提示文案。
- `def _split_static_prefix(pattern: str) -> tuple`（L404）：拆"静态目录 + 剩余 pattern"。
- `DEFINITION`（L430-458，见下"数据契约"）。
- `def execute(pattern: str, path: str = "", max_results: int = 50, _tool_context=None) -> str`（L461）：主入口。

**数据契约（DEFINITION 全文，L430-458）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": (
            "按模式匹配文件和目录。返回匹配路径，按修改时间倒序。"
            "（仅支持本机文件，不支持远程设备文件）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        '匹配路径任意位置，如 "*.py" 递归匹配所有层的.py文件，'
                        '"dir/*.ext" 递归匹配 dir 下所有层的.ext（含子目录）；'
                        '例："**/*.h"、"src/**/*.cpp"、"*.{docx,pdf}"。'
                        '支持绝对路径（如 win:"D:\\work\\**\\*.py"、'
                        'linux/macOS:"/home/user/**/*.py"），绝对与相对写法语义一致。'
                        '默认跳过隐藏文件；含以.开头的路径组件时匹配隐藏文件（".*"匹配点开头的项）'
                    ),
                },
                "path": {"type": "string", "description": "搜索目录（默认当前目录）"},
                "max_results": {"type": "integer", "description": "最大结果数（正整数，默认50）"},
            },
            "required": ["pattern"],
        },
    },
}
```

**依赖**（行号）：`heapq`（L13）、`os`（L14）、`re`（L15）、`functools.lru_cache`（L16）、`__future__.annotations`（L11）。**无内部模块 import**。

**被依赖**：
- `narnat_agent/tools/registry.py:18`（execute/DEFINITION）。
- `narnat_agent/tools/grep/__init__.py:19`（`from ..glob import _expand_braces, _unescape_braces`；L581、L584 使用）。
- 实验脚本：`tool_exp/glob_regression_check.py:8`、`tool_exp/_obsolete_20260910/verify_ai_ux_fixes.py:7`、`verify_ux_round3.py:64`。
- 直接消费 `_compile_pattern`/`_expand_braces` 的还有 `tool_exp/glob_regression_check.py:8`。

**状态**：
- 模块级全局（可变、隐式）：`_compile_pattern` 的 `lru_cache(maxsize=256)` 缓存（L133）——进程级缓存，无可清接口（只能 `_compile_pattern.cache_clear()`）。
- 类变量：`GlobLimits.MAX_BRACE_EXPANSIONS`/`MAX_HARD_LIMIT`（L20-21，可被赋值，实际无写入者）。
- 实例状态：无。

**行为要点**：
1. 花括号展开（L26-108）：无 `{` 直接原样（L28-29）；转义 `\{`/`\}`/`\,` 跳过不参与深度与切割（L38-40、L58-60、L86-88）；仅顶层逗号或 `..` 触发展开（L52-77）；否则原样返回含字面花括号（L75-77）；展开结果超过 100 个 → 提前截断返回（L102-103）；`}` 使深度不为负（L45）防 `}abc{` 类输入。
2. glob→正则语义（L134-260）：`**/` → `(?:[^/]+/)*` 零回溯跨层（L198-200）；`**` 独立组件 → `.*`（L201-203）；`a**b` → `[^/]*`（L204-206）；`*` → `[^/]*`（L191-209）；`?` → `[^/]`（L210-212）；`[!x]`/`[^x]` → `[^x]`（L213-238）；非法字符类（如 `[z-a]`）降级为逐字转义（L229-233）；未闭合 `[` → 字面 `[`（L224-227）。
3. 锚定（L145-147、L254-260）：结尾 `\Z` 恒锚定；起点默认不锚定（`search` 语义）；`.` 开头 + 通配（`.*`/`.?`/`.[`）→ 起点 `^` 锚定（L257-258），避免 `.*` 命中普通文件扩展名的点。
4. 平台分支（L149-153、L247-252）：Windows 下 `\`→`/`（L150-152）且恒定 `re.IGNORECASE`（L249-250）；POSIX 下 smart case（有字面大写则不忽略大小写，L252）。
5. 路径预处理（L155-160）：去前导 `./` 与首尾 `/`；空 → `"*"`。
6. `_collect` 遍历（L265-372）：迭代 DFS + `os.scandir`（L280-284）；符号链接目录跳过（防死循环）、符号链接文件按普通文件处理（L289-297）；`entry.is_dir()` OSError → 跳过（L299-302）；隐藏项（`skip_hidden_files` 且名字以 `.` 开头）目录与文件均跳过（L304-310）；`name in ignore_dirs` 的目录不进入也不匹配（L312-314）；**目录也参与匹配**（L316-336），`dir_rel` 无尾斜杠（L317）；匹配用 `single_regex.search` 或逐 regex（L319-322、L342-349）。
7. top-K 堆（L330-333、L357-360）：`len(heap) >= max_results` 时 `heappushpop`（保持最近 mtime 的 K 个）；`total` 无条件累加（L329、L356）。
8. 结果排序（L368-371）：`sorted(heap, key=lambda x: (-x[0], x[1]))`（mtime 降序、路径升序），`/`→`os.sep`。
9. `execute` 流程（L461-576）：pattern 空/空白 → `"[错误: pattern不能为空]"`（L463-465）；`root = path or os.getcwd()`（L467）；root 非目录 → 错误含 cwd（L468-470）；`max_results` 转 int（L473-476）、`<= 0` 错误（L478-479）、硬上限 50000（L482）；忽略目录来自 `_tool_context.ignore_dirs`（L485-486）；花括号展开+去转义（L489-490）；隐藏文件策略（L494）；无通配 pattern 走 `os.stat` 存在性检查（L506-519）；有静态前缀 → 拼接 root 后 `_collect`（L520-546）；无静态前缀的 pattern 合并为一次遍历（L548-553）。
10. 输出（L555-576）：空结果 → `"[无匹配（搜索目录: {root}）]{隐藏文件提示}"` 或目录不存在错误（L555-561）；结果 `[:max_results]` 拼接（L563-568）；`total > max_results` → 追加 `"...[已截断: 共{total}个匹配项, 当前显示按修改时间最近的{max_results}个。增大max_results可获取完整列表]"`（L570-574）。
11. 结果合并去重（L500、L517、L545、L552）：`merged.setdefault(key, mtime)` 先到先得。
12. 输出路径前缀保留 AI 写法（L541-543）：静态前缀原样保留（相对/绝对），Windows 下大小写跟随 pattern 字面量。

**边界/异常行为**：
1. `pattern` 为 None/空白 → 错误（L463-465）；非 str 会被 `str(pattern)` 转（L463、L465）。
2. `max_results` 字符串 → int 容错（L473-476）；`0`/负数 → 错误（L478-479）；超大 → 截到 50000（L482）。
3. `path` 为不存在目录 → 立即错误（不遍历）（L468-470）。
4. 权限错误目录 → `_collect` 内 `except (PermissionError, OSError): continue`（L365-366）静默跳过整棵子树。
5. `entry.stat()` 失败 → mtime 记 0.0（L326-328、L352-355）→ 排序靠后。
6. 静态前缀不存在 → `missing_dirs` 记录，最终在无结果时报 `"[错误: 目录不存在: {missing_dirs[0]}（当前目录: ...）]"`（L536、L557-558）——注意只报第一个缺失目录。
7. 盘符单独作前缀时补 `\`：`D:\*.py` → `static_dir = "D:\\"`（L422-424）。
8. 相对静态目录一律相对 `root` 解析（L524-538，L524-526 注释记录了"搜索范围逃逸"的历史修复）。
9. `total` 是各次 `_collect` 计数之和（可能含多 pattern 重复计数），而展示列表按 `merged` 去重（L546、L553、L563）——截断提示中的 `total` 与可见条数口径不同。
10. 目录与同名文件可同时进入结果（二者都参与匹配，L316-336 vs L338-360）。
11. Windows 下 `_norm_path` 仅在含 `\` 时替换（L377-378）。
12. `lru_cache` 缓存键未含平台标志，但 `_compile_pattern` 内部读 `os.name`（同进程内恒定，安全；跨进程无影响）。
13. 隐藏文件提示仅在无匹配时出现（L556）。

**补丁痕迹**：
1. 中：`_collect` 可能对同一 root 遍历两次——无静态前缀的 pattern 合并走一次（L548-553），静态前缀 pattern 各自 `_collect`（L539-540）；多个静态前缀指向同一目录时重复遍历（判据：重复计算，性能）。
2. 中：`merged.setdefault` 的"先到先得 mtime"（L500/517/545/552）使多 pattern 重叠时结果 mtime 取首个 pattern 的采集值，而非最新（语义含糊）。
3. 中：`GlobLimits`（L18-21）注释"原模块级常量收敛为类成员"——历史重构痕迹；同类痕迹见 `GrepLimits`（grep L21-45）、`SearchConfig`（web_search L17-24），说明曾做过一轮"全局→类属性"的机械迁移（判据：历史包袱注释）。
4. 低：`_pattern_has_dot_component`（L382-392）与 `_compile_pattern` 各自实现"剥离前导 ./ 与 /"（L155-158 vs L388-391），逻辑重复。
5. 低：魔法值 100（L20、L102）、50_000（L21）、256（L133）、3 次探测无关（本文件无）。
6. 低：`_split_static_prefix` 用 `pattern.rfind("/")` 与 `rfind("\\")` 取较大者（L418），对 Windows 混合分隔符容错，但返回值混用两种分隔符（L420、L425）。
7. 低：`execute` 里 `_hidden_files_hint` 只覆盖"无匹配"路径（L556），有条目时不提示跳过规则（信息不对称）。

**可测性**：
- 可独立单测的部分：`_expand_braces`/`_unescape_braces`（转义、嵌套、不展开、上限、畸形深度）；`_compile_pattern`（`**` 各位置、字符类降级、锚定、Windows/POSIX 大小写）；`_split_static_prefix`；`_pattern_has_dot_component`；`_norm_path`。
- 需要集成测试的部分：`execute`/`_collect`（临时目录树 + `os.utime` 控制 mtime 验证排序与截断；ignore_dirs 注入；隐藏文件策略；符号链接）。
- 无法自动化的部分：无（lru_cache 需在测试间 `cache_clear()`）；已有回归脚本 `tool_exp/glob_regression_check.py`。

---

### narnat_agent/tools/grep/__init__.py（591行）

**职责**：正则搜索文件内容（多路径、上下文行、head_limit 预算降级、并行扫描、二进制/超大文件/超长行防护），输出 ripgrep heading 形态结果。

**对外接口**：
- `class GrepLimits`（L21-45）：`BUFFER_SIZE = 64 * 1024`（L24）、`MAX_PATTERN_LENGTH = 4096`（L27）、`MAX_FILE_SIZE = 100 * 1024 * 1024`（L30）、`MAX_LINE_LENGTH = 1 * 1024 * 1024`（L33）、`RE_META_CHARS = set(r".*+?[]{}()\|^$")`（L36）、`BINARY_NUL_THRESHOLD = 1`（L39）、`DEFAULT_HEAD_LIMIT = 30`（L42）、`PARALLEL_MIN_FILES = 10`（L45）。
- `DEFINITION`（L47-106，见下"数据契约"）。
- `def _detect_text_encoding(head: bytes) -> str`（L109）：与 read 同款的编码探测（重复实现）。
- `def execute(pattern: str, path="", glob: str = "", i: bool = False, A: int = 0, B: int = 0, C: int = 0, head_limit: int = GrepLimits.DEFAULT_HEAD_LIMIT, _tool_context=None, **kwargs) -> str`（L129-140）：主入口（含 CLI 风格别名兼容）。
- `def _has_re_meta(pattern: str) -> bool`（L265）：是否含正则元字符/反斜杠。
- `def _make_fast_searcher(pattern: str, ignore_case: bool)`（L281）：纯文本快路径闭包。
- `def _check_binary_first_chunk(first_chunk: bytes) -> bool`（L301）：NUL 检测。
- `def _scan_file(file_path, regex, fast_searcher, A, B, collect_budget)`（L310）：单文件全扫，返回 `(count, blocks, in_file_trunc)` 或 None。
- `def _emit_file(label, count, blocks, in_file_trunc, head_limit, results, ctx)`（L446）：按预算输出单文件。
- `def _search_target(target, label, is_file, regex, fast_searcher, glob_filter, A, B, head_limit, ignore_dirs, results, ctx)`（L492）：文件/目录统一搜索入口。
- `def _remaining_budget(head_limit, ctx)`（L558）：剩余可展开匹配数。
- `def _match_glob(fname: str, rel: str, glob_filter: str) -> bool`（L569）：glob 过滤（含花括号展开）。

**数据契约（DEFINITION 全文，L47-106）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": (
            "正则搜索文件内容（仅支持本机文件，不支持远程设备文件）。"
            "默认返回每个命中文件的分组结果：文件表头（含匹配计数）+ 带行号的匹配行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式",
                },
                "path": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "搜索路径（默认当前目录）；可填目录、单个文件，或多个路径的数组"
                        "（文件/目录可混合，按给定顺序输出），如 [\"src/a.c\", \"include\"]"
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "文件过滤，如*.py、src/*.c、**/*.c、*.{c,h}、src/**/*.{py,md}"
                        "（花括号多模式，与Glob工具语法一致，默认空）"
                    ),
                },
                "i": {
                    "type": "boolean",
                    "description": "是否忽略大小写（默认否）",
                },
                "A": {
                    "type": "integer",
                    "description": "额外带后续N行（默认0）",
                },
                "B": {
                    "type": "integer",
                    "description": "额外带前面N行（默认0）",
                },
                "C": {
                    "type": "integer",
                    "description": "额外带前后各N行（默认0）",
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "最大返回匹配行数（正整数，默认30）；达到后剩余文件仅列文件名（含计数），"
                        "增大可展开更多匹配行"
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
}
```

**依赖**（行号）：`fnmatch`（L12）、`os`（L13）、`re`（L14）、`collections.deque`（L15）、`concurrent.futures.{ThreadPoolExecutor, as_completed}`（L16）；`..param_utils.to_bool`（L18）；`..glob._expand_braces, _unescape_braces`（L19）。

**被依赖**：
- `narnat_agent/tools/registry.py:19`（execute/DEFINITION）。
- 实验脚本：`tool_exp/verify_grep_contract.py:11`、`tool_exp/_obsolete_20260910/verify_ai_ux_fixes.py:10`、`verify_tool_ux_fixes.py:8`、`verify_ux_round3.py:66`、`verify_ux_round4.py:12`（后者还 import 了已不存在的 `_format_results`，见补丁痕迹）。
- 注意：`grep` 不 import read（自带编码探测副本）。

**状态**：
- 模块级全局：无（常量收敛进 `GrepLimits` 类）。
- 类变量：`GrepLimits` 8 个常量（L24-45，可被外部赋值，无写入者）。
- 实例状态：无。
- 每次调用的可变上下文：`ctx = {"expanded": 0, "limit_hit": False, "seen_files": set()}`（L231），以参数在 `_search_target`/`_emit_file` 之间传递（L235、L543、L551、L562）。

**行为要点**：
1. CLI 别名兼容（L141-160）：`-i/-A/-B/-C/-head_limit` → 去横杠；未知横杠参数保留原名 → TypeError（L148-149）→ 由 registry 转成"有效参数"中文提示；显式传参优先（`if "X" in aliases and not X`，L151-158）；`head_limit` 别名直接覆盖（L159-160，注释 L150 自承无法区分"未传"与"传默认值"）。
2. 数值参数转 int（L163-169）：`A/B/C` None → 0；`head_limit` None → 保持 None（=无限）；失败 → `"[错误: A/B/C/head_limit需为整数]"`。
3. ReDoS 防护（L172-173）：`len(pattern) > 4096` 拒绝。
4. 正则编译（L176-179）：`re.error` → `"[错误: 非法正则: {e}]"`。
5. `i` 经 `to_bool` 归一（L175）。
6. `C > 0` 时覆盖 A、B（L181-183）。
7. `head_limit <= 0`（非 None）→ `"[错误: head_limit需为正整数]"`（L185-186）。
8. 快慢双路径（L188、L265-294）：pattern 无元字符且无反斜杠 → 快路径（大小写敏感用 `pattern in line`；不敏感用 `re.escape`+IGNORECASE）；否则正则 `search`。
9. path 归一化（L190-193）：单值/数组均可；空或全 None → `[""]`（→ cwd）。
10. 目标收集（L197-228）：文件与目录分别 `normcase(abspath)` 去重（L209-212、215-218）；不存在路径进 `warnings`（L220-221）；无有效目标 → `"[无匹配]"` 前缀带跳过提示（L223-228）。
11. 忽略目录（L195、L503）：来自 `_tool_context.ignore_dirs`；`os.walk` 里 `dirnames[:] = [d for d in dirnames if d not in ignore_dirs]`（L503）——按**目录名**匹配（非路径）。
12. 目录内文件排序（L510）：`file_items.sort(key=lambda t: t[1])`（相对路径字典序，确定性输出）。
13. glob 过滤（L507-508、L569-590）：`_match_glob(fname, rel, glob_filter)`；花括号展开（L581-584）；`**/` 前缀额外生成零层变体（L586-587）；Windows 下 `\`→`/` 且 fnmatch 内部 normcase（L578-580）；文件名或相对路径任一命中即通过（L588-590）。
14. 单文件扫描 `_scan_file`（L310-439）：`open(rb)` → 大小 > 100MB 跳过（L327-329）→ 首块 64KB NUL 检测（L332-334）→ 编码探测（L335）→ 文本模式重开（L344）→ 64KB 块循环（L359-414）。
15. 行处理（L364-379）：`chunk.replace("\r\n", "\n")`（防缓冲区边界分裂，L365）；`leftover` 行尾残留 `\r` 剥离（L378）；超长 leftover > 1MB → `aborted`（L372-374）。
16. 匹配与上下文（L387-414）：命中即 `count += 1`；新命中打断未收满 after 的块（L389-393）；`budget` 内新建块并回收 before 窗口（L394-397）；超出预算仅计数（L398-401）；after 行收集（L402-408）；before 窗口 deque 上限 `B`（L409-414）。
17. 文件末尾不完整行（L416-430）：非 aborted 且长度合法时也参与匹配与计数。
18. 返回值（L439）：`(count, blocks, count > len(blocks))` —— `in_file_trunc` 由"总命中数 > 已收集块数"推断。
19. 输出 `_emit_file`（L446-485）：`count == 0` 直接跳过（L454-455）；预算耗尽 → 清单行 `f"{label} ({count}处)"` 并置 `limit_hit = True`（L457-462）；否则展开：组间空行（L464-465）、表头 `f"{label} ({count}处):"`（L467）、块间 `"--"` 分隔（L471-472）、before 行 `f"{bnum}-{btext}"`、命中行 `f"{mnum}:{mtext}"`、after 行 `f"{anum}-{atext}"`（L473-480）；`expanded += 1`（L481）；文件内截断提示（L483-485）。
20. 并行路径（L515-543）：文件数 ≥ 10 → 线程池（`min(cpu_count() or 4, 12)`，L522）；每个文件按 `head_limit` 收集（L520）；结果按原顺序消费预算（L538-543）；单文件异常 → None 跳过（L534-537）。
21. 串行路径（L544-555）：按 `_remaining_budget(head_limit, ctx)` 收集（L550-551）。
22. 跨目标去重：`ctx["seen_files"]`（L526-529、L546-549），同一文件被多个 path 项覆盖时只扫一次。
23. 汇总输出（L252-258）：`limit_hit` 追加 `"...[已截断: 达到head_limit({head_limit})，剩余文件仅列文件名（含计数）。增大head_limit可展开更多匹配行]"`（L253-254）；`warnings` 前置（L255-257）。
24. 无匹配文案随搜索范围变化（L238-250）：单文件 → `"[无匹配]"`；单目录 → `"[无匹配（搜索目录: X）]"`；多目标 → `"[无匹配（搜索范围: a、b、c等）]"`。

**边界/异常行为**：
1. 空 pattern `""` → 编译成功，匹配所有行（无拦截）。
2. `head_limit=None` → 无限展开（L167、L558-562）。
3. 二进制文件（首块含 NUL）→ 静默跳过（L333-334）。
4. 不可读/打不开文件 → 静默跳过（L322-324、L336-337、L345-346）。
5. 超 100MB 文件 → 静默跳过（L327-329）。
6. 超 1MB 单行 → 视异常文件提前结束（`aborted`），已计入的 count 保留（L372-374、L417）。
7. 文件末尾 after 不足 → 正常入列（L432-435）。
8. 并行与串行结果可能不同：并行按每文件 head_limit 收集（L520-521 注释自承"收集超量部分丢弃，代价可控"）。
9. `i` 显式传 False + `-i` 别名 True 时，别名会覆盖（`not i` 为真，L151）——仅在 AI 传矛盾参数时出现。
10. `A/B/C` 传入非数字字符串 → 错误（L168-169）。
11. `sort` 只对文件列表排序，不改变多 path 项的输出顺序（L233-235 循环按 AI 给定顺序）（L10 模块注释）。
12. `_match_glob` 的 fnmatch 语义：`**/*.c` 中 `**` 不跨 `/`（与 Glob 工具的自研正则语义不同）——注释 L566 声称"与 Glob 工具语义对齐"。

**补丁痕迹**：
1. 中：`_detect_text_encoding`（L109-126）与 `read/__init__.py:92-111` 完全重复（判据：重复代码）。
2. 中：并行（L515-543）/串行（L544-555）两条路径预算语义不同 → 同一查询因文件数跨过 10 而结果详略不同（判据：脆弱边界 + 行为不一致）。
3. 中：`_match_glob` 用 fnmatch（L588-590），Glob 用自研正则（glob L134-260）——"与 Glob 工具语义对齐"仅是部分对齐（花括号共用，`**` 语义不同）。
4. 中：`head_limit` 别名无条件覆盖（L159-160），L150 注释自承局限（判据：历史包袱注释）。
5. 中：`_scan_file` 单函数 130 行，混合 I/O、编码、分块、上下文、预算五种职责（L310-439）（判据：职责混杂）。
6. 低：`aborted`（L357、372-374）只抑制末尾遗留行处理，不改变返回值语义，容易被误读为"整文件跳过"。
7. 低：`GrepLimits` 的"原模块级常量收敛为类成员"注释（L22）——同 glob 的历史迁移痕迹；`DEFAULT_HEAD_LIMIT = 30` 附经验数据注释（L41"514次中132次传30"）（判据：魔法值 + 历史包袱）。
8. 低：`RE_META_CHARS` 包含 `\\`（L36 字符串含反斜杠），而 `_has_re_meta` 又单独短路反斜杠（L272-274）——双重表达。
9. 低：已废弃函数引用残留：`tool_exp/_obsolete_20260910/verify_ux_round4.py:12` import `_format_results`（grep 中已不存在）——历史脚本未随重构清理（死引用）。

**可测性**：
- 可独立单测的部分：`_has_re_meta`（元字符/反斜杠/纯文本）；`_make_fast_searcher`（大小写两态语义等价正则路径）；`_check_binary_first_chunk`；`_match_glob`（花括号、`**/`、Windows 分隔符、大小写）；`_remaining_budget`；`_detect_text_encoding`。
- 需要集成测试的部分：`execute` 端到端（临时目录：多路径数组顺序与去重、head_limit 降级为清单行、ignore_dirs 剪枝、GBK 中文命中、二进制跳过、超长行、上下文 A/B/C、limit_hit 提示文案）；并行/串行一致性（构造 ≥10 文件目录对比）。
- 无法自动化的部分：无（已有 `tool_exp/verify_grep_contract.py`）。

---
### narnat_agent/tools/edit/__init__.py（230行）

**职责**：字符串精确替换编辑文件（本地/远程），保持原文件编码与换行符风格；生成 diff 供 LLM 与终端展示。

**对外接口**：
- `DEFINITION`（L18-44，见下"数据契约"）。
- `def _read_for_edit(file_path: str) -> tuple`（L47）：返回 `(content, 写回编码)`；二进制抛 ValueError、解码失败抛 UnicodeDecodeError。
- `def execute(file_path: str, old_string: str = "", new_string: str = "", replace_all: bool = False, device: str = "") -> tuple`（L75-77）：主入口，返回 `(llm_result, color_diff)`。
- `def _edit_by_string(content: str, old_string: str, new_string: str, replace_all: bool, file_path: str, write_encoding: str = "utf-8") -> tuple`（L126-128）：替换核心。
- `def _write_and_diff(old_content: str, new_content: str, file_path: str, count: int, write_encoding: str = "utf-8") -> tuple`（L160-161）：写回 + 生成 diff。
- `def _find_similar(content: str, old_string: str) -> str`（L190）：未匹配时的相似行提示。
- `def _make_diff(old_content: str, new_content: str, file_path: str) -> str`（L214）：unified diff。
- `def _make_color_diff(diff_text: str) -> str`（L228）：转调 `diff_utils.colorize_diff`。

**数据契约（DEFINITION 全文，L18-44）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": (
            "编辑文件（字符串精确替换）。支持本地或远程编辑文件。"
            "自动识别并保持原编码（UTF-8/GBK）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "待替换的原文（必须与文件内容精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换全部匹配（默认否，即只替换唯一匹配处）",
                },
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即编辑本地指定文件，设置dev1..devn则编辑指定远程设备文件",
                },
            },
            "required": ["file_path", "old_string"],
        },
    },
}
```
> 注：`new_string` 无默认值要求（`required` 只有 file_path/old_string），但代码层 `execute` 的 `new_string` 默认 `""`（L75-76）。

**依赖**（行号）：
- `os`（L11）、`difflib`（L12）
- `..diff_utils.colorize_diff`（L14）
- `..param_utils.to_bool`（L15）
- `..terminal._normalize_device_for_tools, _file_tool_device_hint`（L16）
- 函数内延迟导入：`..read._detect_text_encoding`（L59）、`..terminal.remote.remote_edit`（L104）

**被依赖**：
- `narnat_agent/tools/registry.py:20`（execute/DEFINITION；不在 `_CONTEXT_TOOLS` 中，故不注入上下文）。
- 实验脚本：`tool_exp/verify_edit_no_guard.py:11`、`tool_exp/verify_edit_remote.py:8`、`tool_exp/verify_write_free.py:8`、`tool_exp/_obsolete_20260910/verify_ai_ux_fixes.py:8`、`verify_ux_round3.py:43`（`__import__` 动态导入）。

**状态**：无模块级可变全局、无类变量、无实例状态（全部为函数局部）。

**行为要点**：
1. `replace_all = to_bool(replace_all)`（L97，注释 L95-96 说明 `bool("false")` 风险）。
2. 设备归一（L99-101）：非法 → `(f"[错误: {_file_tool_device_hint()}]", "")`。
3. 远程分支（L103-105）：`remote_edit(file_path, old_string, new_string, replace_all, device)` 直接返回其 tuple。
4. 本地文件不存在（L107-108）→ 提示"如需创建请用Write工具"。
5. `_read_for_edit`（L47-72）：首 8192 字节二进制判定（L60-63）；`_detect_text_encoding` 探测（L64）；读取用 `encoding=encoding, newline=""`（L65，**保持原始换行符不解译**）；严格解码（无 `errors=`）；写回编码规则：`utf-8-sig` 且头有 BOM → `"utf-8-sig"`，否则 `"utf-8"`；GBK → `"gbk"`（L67-71）。
6. 异常映射（L110-120）：`PermissionError` → 权限不足；`OSError` → 读取失败；`ValueError`（binary）→ 二进制拒绝；`UnicodeDecodeError` → `"[错误: 文件非UTF-8/GBK编码，为防止内容损坏已拒绝编辑: ...。请用Shell工具处理（如转码为UTF-8后再编辑）]"`。
7. `_edit_by_string` 换行符归一（L133-141）：`has_crlf = '\r\n' in content`；CRLF 文件：`\r\n`→`\x00` → `\n`→`\r\n` → `\x00`→`\r\n`（把孤立 `\n` 与既有 `\r\n` 统一为 `\r\n`）；LF 文件：`\r\n`→`\n`、`\r`→`\n`。
8. 匹配语义（L143-154）：`count = content.count(old_string_normalized)`；`count == 0` → 错误 + 相似行提示（L144-146）；`count > 1 and not replace_all` → 错误提示"扩大上下文使其唯一，或设置replace_all=True"（L148-149）；`replace_all` → `str.replace` 全部（L151-152）；否则 `replace(..., 1)`（L153-154）。
9. `_find_similar`（L190-211）：按 `old_string` 首行 strip 后与文件每行做 `difflib.SequenceMatcher.ratio()`；`> 0.5` 收集；降序取前 3；输出 `"相似行（供参考）:"` + `"  行N: text (相似度X%)"`（L208-211）。
10. `_write_and_diff`（L160-187）：写回用 `open(file_path, "w", encoding=write_encoding, newline='')`（L170，newline='' 保证内容里的 `\r\n` 原样写出）；`OSError` → 写入失败（L172-173）；`UnicodeEncodeError`（如 GBK 文件写入 emoji）→ 明确提示（L174-176）。
11. 空编辑提示（L178-181）：`old_content == new_content` → `"[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]"` + `[无差异]` 着色（**文件已照常写盘**）。
12. 成功返回（L183-187）：`f"[已替换{count}处]\n{diff}"` + 着色 diff；`count` 为全部匹配数（replace_all 时）或 1。
13. `_make_diff`（L214-225）：`difflib.unified_diff(..., fromfile="a/{basename}", tofile="b/{basename}", lineterm="")`；空结果 → `"[无差异]"`。
14. `_make_color_diff`（L228-230）：转调 `diff_utils.colorize_diff`（L14 导入）。

**边界/异常行为**：
1. `old_string=""` → `"[错误: old_string不能为空]"`（L130-131，在归一化之后判断）。
2. `new_string` 省略 → 默认 `""` → 删除匹配文本（L75-76、L154）。
3. 仅 1 行无末尾换行的文件：LF 分支 `\r`→`\n` 归一（L138），提交内容带 `\r` 会被转换。
4. GBK 文件写入无法表示的字符 → UnicodeEncodeError 分支（L174-176）：因 `open(..., "w")` 在 L170 已先截断文件，此分支报错时磁盘上的原内容已被清空（残留空文件）——"先截断后写入"的失败语义风险（同 L170-171）。
5. 二进制文件 → 拒绝（L62-63、L116-117）。
6. 非 UTF-8/GBK（如 UTF-16）→ 拒绝编辑（L118-120），不做转码。
7. 多匹配且 `replace_all=False` → 拒绝（L148-149）。
8. 未匹配 → 拒绝 + 相似行提示（L144-146）。
9. CRLF 文件里若 `old_string` 用 LF 写（AI 常见），会被归一为 CRLF 后匹配成功（L136）——跨换行符风格容错。
10. `_read_for_edit` 的 `head` 只读 8192 字节用于判编码，全量读取用探测到的编码（L60-66）。
11. 远程分支的返回值/行为由 `terminal/remote.py:206-293` 定义（本报告不展开）。

**补丁痕迹**：
1. 中：与 `write/__init__.py` 的**编码策略不对称**：Edit 保持原编码（L64-71），Write 固定 UTF-8（write L95）；`write` 的 DEFINITION 未声明编码行为，AI 无从区分（判据：行为不一致 + 文档缺口）。
2. 中：`_make_diff`（L214-225）与 `write/__init__.py:118-129` 是逐行重复的实现（仅调用方不同）。
3. 中：`_write_and_diff` 先 `open(...,"w")`（截断文件）再 write（L170-171）：写入抛错时原文件已丢失（L174-176 的 UnicodeEncodeError 分支同理）——"先截断后写入"的脆弱边界（判据：脆弱边界）。
4. 低：`\x00` 哨兵换行归一技巧（L136）可读性差且含魔法字节。
5. 低：`_find_similar` 对每行做 SequenceMatcher（L199-202）：大文件未命中时 O(N×len) 成本。
6. 低：设备错误文案 `"[错误: {_file_tool_device_hint()}]"` 与 read/write 重复手写（L101、read L138、write L54）。
7. 低：`new_string` 在 DEFINITION 中未列入 required 也未给 default 提示，与代码默认 `""` 的组合语义靠 L81 文档字符串传达。

**可测性**：
- 可独立单测的部分：`_edit_by_string`（0/1/多匹配、replace_all、CRLF/LF 归一）；`_find_similar`（阈值、top3、空输入）；`_make_diff`（同/异、`[无差异]`）；`_read_for_edit`（UTF-8/BOM/GBK/二进制/不可解码）。
- 需要集成测试的部分：`execute` 端到端（临时文件：编码保持、BOM 保持、CRLF 保持、空编辑提示、写入失败路径）；远程编辑（mock 或真机）。
- 无法自动化的部分：真实 SSH 远程编辑（已有 `tool_exp/verify_edit_remote.py`）。

---

### narnat_agent/tools/write/__init__.py（129行）

**职责**：创建新文件或整体覆写文件（本地以 UTF-8 写入；远程 SFTP），覆写已有文件时返回 diff。

**对外接口**：
- `DEFINITION`（L12-33，见下"数据契约"）。
- `def execute(file_path: str, content: str, device: str = "") -> tuple`（L37-38）：主入口，返回 `(llm_result, color_diff)`。
- `def _make_diff(old_content: str, new_content: str, file_path: str) -> str`（L118）：unified diff。

**数据契约（DEFINITION 全文，L12-33）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": (
            "创建新文件或全量覆盖文件。支持本地或远程写入文件。"
            "覆写已存在的文件时返回diff。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即写入本地指定文件，设置dev1..devn则写入指定远程设备文件",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}
```

**依赖**（行号）：
- `os`（L6）、`difflib`（L7）
- `..diff_utils.colorize_diff, describe_bytes_only_change`（L9）
- `..terminal._normalize_device_for_tools, _file_tool_device_hint`（L10）
- 函数内延迟导入：`..terminal.remote.remote_write`（L57）、`..read._detect_text_encoding`（L79）

**被依赖**：
- `narnat_agent/tools/registry.py:21`（execute/DEFINITION；不在 `_CONTEXT_TOOLS`）。
- 实验脚本：`tool_exp/verify_write_free.py:7`、`tool_exp/verify_remote_file_reconnect.py:9`、`tool_exp/_obsolete_20260910/verify_ai_ux_fixes.py:9`、`verify_ux_round3.py:7`、`tool_exp/verify_write_line_ending.py`、`verify_write_remote.py`、`verify_write_line_ending_remote.py`。

**状态**：无模块级可变全局、无类变量、无实例状态。

**行为要点**：
1. 设备归一（L52-55）：非法 → 错误。
2. 远程分支（L56-58）：`remote_write(file_path, content, device)` 直接返回其 tuple。
3. `abs_path = os.path.abspath(file_path)`（L59）。
4. 目录路径提前拦截（L61-63）→ `"[错误: {file_path} 是目录，请使用正确的文件路径]"`（注释 L61：避免 `open()` 报 Permission denied 误导）。
5. 自动创建父目录（L65-68）：`os.makedirs(parent, exist_ok=True)`。
6. 旧内容读取（L75-92）：文件存在时读首 8192 字节；含 NUL → 视为二进制、`old_bytes = None`（L82）；否则回读全量 `old_bytes`（L83-85）；编码探测后 `decode(errors="replace")` 得 `old_content`（L87-88）；`diff = _make_diff(...)`、`color_diff = colorize_diff(diff)`（L89-90）；整个块 `except Exception: pass`（L91-92）。
7. 写回（L94-98）：`open(abs_path, "w", encoding="utf-8", newline='')`——**固定 UTF-8、无 BOM、不保持原编码**；`OSError` → `"[错误: 写入失败: {e}]"`。
8. 字节级变更判定（L100-112）：`new_bytes = content.encode("utf-8")`、`byte_count = len(new_bytes)`；当 `diff == "[无差异]"` 且 `old_bytes is not None`：
   - `new_bytes == old_bytes` → `"[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]"`（L106-109）；
   - 否则 `describe_bytes_only_change(old_bytes, new_bytes)` → `"[提示: 文件已写入，正文内容相同，但字节层面有变化（{detail}）]"` + 着色提示（L110-112）。
9. 成功返回（L113-115）：`f"[已写入: {file_path} ({byte_count}字节)]\n{diff}"`；新文件无 diff 时只有首行。
10. `_make_diff`（L118-129）：与 edit 相同实现，空结果 → `"[无差异]"`。

**边界/异常行为**：
1. 新文件：`os.path.isfile` 为假 → 跳过旧内容分支（L75），`old_bytes = None` → 不进入字节比较（L105）→ 返回 `[已写入: ...]`（L113-115）。
2. 覆写二进制旧文件 → 只读首块、不读全文（L82-85）→ `old_bytes=None` → 无"无实质修改"判定，直接报 `[已写入]`。
3. 覆写 GBK 文件 → 写入 UTF-8 → 字节变化提示（L110-112，`describe_bytes_only_change` 输出编码/BOM 差异说明）。
4. 覆写 CRLF 文件（内容相同但换行不同）→ 字节比较检出 → 提示行尾符变化（L103-104 注释 + L110-112）。
5. 旧内容读取异常（权限/编码/其它）→ 静默 pass（L91-92）：diff 为空、`old_bytes` 可能为 None；写入仍继续，返回无 diff 的 `[已写入]`。
6. `content` 为空串 → 写空文件，`byte_count = 0`。
7. 父目录创建失败（权限）→ 异常冒泡到 registry → `"[工具执行失败(Write): ...]"`（registry L155-156）。
8. 覆写既有文件且 `diff` 非 `[无差异]` 但字节相同（例如行尾差异被 splitlines 抹平后仍有 hunk？）——逻辑上不冲突：只要 `diff != "[无差异]"` 就按成功路径返回 diff（L113-114）。
9. 远程写入行为见 `terminal/remote.py:135-201`（touple 返回，含字节数）。

**补丁痕迹**：
1. 高：编码策略"只写 UTF-8"（L95）与 Edit "保持原编码"（edit L64-71）不一致，且 DEFINITION 未声明（L16-19）——同一批文件工具的隐性行为差异，AI 覆写 GBK 文件会静默改变编码（判据：行为不一致 + 隐藏契约）。
2. 中：`except Exception: pass`（L91-92）静默吞异常——diff 丢失无任何提示（判据：吞异常）。
3. 中：`_make_diff`（L118-129）与 edit 的 `_make_diff`（edit L214-225）完全重复（判据：重复代码）。
4. 中：`old_bytes = fb.read()` 全量读入（L83-85）无大小上限——超大文件覆写时内存尖峰（对比 read 的 20MB 计数保护）（判据：脆弱边界）。
5. 低：`os.makedirs` 无 try（L66-68），异常直接走 registry 兜底文案。
6. 低：`byte_count` 只按 UTF-8 编码计数（L100），若目标文件原为 GBK 且内容未变，计数仍按 UTF-8 展示（信息与磁盘实际可能不一致）。

**可测性**：
- 可独立单测的部分：`_make_diff`；`execute` 本地分支（临时目录：新建/覆写同内容/覆写不同内容/CRLF→LF/GBK 覆写/目录路径/父目录创建/空内容/二进制旧文件）。
- 需要集成测试的部分：远程写入（mock 或真机）；与 registry 的返回契约（tuple）。
- 无法自动化的部分：真实 SSH/SFTP 写入（已有 `tool_exp/verify_write_remote.py`、`verify_write_line_ending_remote.py`）。

---

### narnat_agent/tools/web_search/__init__.py（178行）

**职责**：通过 MCP 风格 HTTP 接口（默认 AnySearch）执行网页搜索，解析 markdown 响应、按查询词相关性排序后格式化返回。

**对外接口**：
- `class SearchConfig`（L17-24）：`default_url = "https://api.anysearch.com/mcp"`（L23）、`timeout = 15`（L24）。
- `DEFINITION`（L26-40，见下"数据契约"）。
- `def _extract_query_words(query: str) -> set`（L45）：提取查询词集合（英文单词小写 + 中文 bigram）。
- `def _relevance_score(result: Dict, query: str) -> float`（L59）：相关性打分。
- `def _format_results(results: List[Dict]) -> str`（L71）：格式化输出。
- `def _parse_anysearch_markdown(text: str) -> List[Dict]`（L92）：解析 markdown 结果为 `{title,url,description}`。
- `def _search_anysearch(query: str, max_results: int, api_key: str, url: str) -> List[Dict]`（L112）：HTTP 调用。
- `def execute(query: str, num: int = 5, _tool_context=None) -> str`（L137）：主入口。

**数据契约（DEFINITION 全文，L26-40）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "网页搜索（用于查找API文档、解决方案、技术文章等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "num": {"type": "integer", "description": "返回结果数量（正整数，默认5，上限20）"},
            },
            "required": ["query"],
        },
    },
}
```

**依赖**（行号）：`json`（L9）、`re`（L10）、`urllib.request`（L11）、`typing.{List, Dict}`（L12）。**无内部模块 import**（密钥经 `_tool_context` 传递，无 import 依赖）。

**被依赖**：
- `narnat_agent/tools/registry.py:25`（execute/DEFINITION；在 `_CONTEXT_TOOLS` 中，L105）。
- `narnat_agent/core/tool_dispatcher.py:53`（读作只读工具并行执行）、L66（标签）、L483（摘要）。
- 实验脚本：`tool_exp/analyze_tool_pain.py:72`（离线分析日志，不 import）。

**状态**：
- 模块级全局：无（配置收敛进 `SearchConfig` 类）。
- 类变量：`SearchConfig.default_url`、`SearchConfig.timeout`（L23-24，可被外部赋值，无写入者）；L18-22 文档串说明"api_key/api_url 属运行时配置，由 execute() 每次调用时从 tool_context.api_keys 读取，不再缓存到模块级全局"。
- 实例状态：无（每次调用局部变量）。

**行为要点**：
1. `num` 转 int（L150-153）；`num <= 0` → `"[错误: num需为正整数]"`（L154-155）；上限裁剪 `num = min(num, 20)`（L157，注释 L156 说明理由）。
2. 密钥读取（L158-164）：`api_keys = _tool_context.api_keys if _tool_context else None`；`api_key = api_keys.get("websearch", "")`；`api_url = api_keys.get("websearch_url", SearchConfig.default_url)`。
3. 无密钥（L166-167）→ `"[错误: 搜索失败]"`（**不含原因**）。
4. 调用（L169-172）：异常统一 `"[错误: 搜索失败: {e}]"`。
5. 空结果（L174-175）→ `"[无搜索结果]"`。
6. 排序（L177）：`results.sort(key=lambda r: _relevance_score(r, query), reverse=True)`（Python 稳定排序，同分保持原序）。
7. 截取（L178）：`_format_results(results[:num])`。
8. 相关性打分（L59-68）：标题命中权重 3、描述权重 1，除以 `len(q_words) * 4`；无查询词 → 0.5 中性值。
9. 查询词提取（L45-56）：`[a-zA-Z0-9]{2,}` 小写词；中文段取相邻两字 bigram，单字段落额外加原文。
10. HTTP 调用（L112-132）：JSON-RPC `tools/call`，`params.name = "search"`、`arguments = {query, max_results, zone: "cn"}`（L116-118）；头 `Content-Type: application/json`（L120）、可选 `X-API-Key`（L121-122）；`urlopen(..., timeout=SearchConfig.timeout)`（L124）；响应取 `result.content[].text` 拼接（L126-131）。
11. markdown 解析（L92-109）：按 `\n(?=### \d+\.)` 分块；标题 `^### \d+\.\s*(.+)$`；URL `-\s*\*\*URL\*\*:\s*(https?://[^\s\n]+)`；描述取 URL 之后文本并剥离 `- ` / `**Description**: ` 前缀。
12. 输出格式（L71-87）：`"{i}. {title}"` + `"   URL: {url}"` + 描述（换行/连续空白压成单空格，超 300 截断加 `…`）；标题超 120 截断加 `…`。

**边界/异常行为**：
1. `num` 传 None/字符串（L151-153）。
2. `num` 超大（如 100）→ 静默裁到 20（L157）。
3. 无 `_tool_context` 或无 `api_keys` → 走"无密钥"分支（L159-167）。
4. 网络/HTTP/JSON 异常 → 统一错误（L171-172）。
5. `_format_results` 用 `r['url']` 硬索引（L79）——若上游解析出的 dict 缺 url 会 KeyError（当前解析器保证存在，L99-108）。
6. 描述为空 → 不输出描述行（L80-86）。
7. `zone` 固定 `"cn"`（L117，不可配置）。
8. 排序后按 `num` 截断（L178）——服务端多返回的结果会被排序用上（`max_results` 直接传给服务端，未额外放大）。

**补丁痕迹**：
1. 中：无密钥时返回 `"[错误: 搜索失败]"`（L167）——丢失关键诊断（AI 无法区分"没配密钥"与"接口挂了"）（判据：吞异常/信息丢失）。
2. 中：`zone="cn"` 硬编码（L117）、`_relevance_score` 的 `3`/`4`/`0.5` 权重（L65、L68）、`120`/`300` 截断（L76、L84）——魔法值密集且无配置入口。
3. 低：自建 MCP JSON-RPC 调用（L114-124），与项目内 `narnat_agent/mcp` 客户端概念重复（此工具不 import mcp 包）。
4. 低：`SearchConfig` 注释（L18-22）是一段"为什么不缓存到全局"的历史说明（判据：历史包袱注释）。
5. 低：密钥名 `"websearch"`/`"websearch_url"`（L163-164）与配置键 `"接口密钥组"`（`config/loader.py:785`、默认写入 L758）耦合，拼写错误只能得到"搜索失败"。

**可测性**：
- 可独立单测的部分：`_extract_query_words`（英文/中文/单字）；`_relevance_score`（命中/无命中/空词）；`_parse_anysearch_markdown`（标准块/缺 URL/描述前缀清洗）；`_format_results`（截断、空白压缩、缺 description）。
- 需要集成测试的部分：`execute` 的参数分支与错误分支（可用 monkeypatch 替换 `_search_anysearch`）；真实网络调用**禁止**（本任务明令）。
- 无法自动化的部分：真实搜索质量/接口可用性（需网络）。

---

### narnat_agent/tools/todo_write/__init__.py（104行）

**职责**：创建/整体替换任务列表，校验字段、自动修正多个 in_progress、通知 UI、同步上下文并回显未完成清单。

**对外接口**：
- `DEFINITION`（L5-39，见下"数据契约"）。
- `def execute(todos: List[Dict[str, Any]], _tool_context=None) -> str`（L43）：主入口。

**数据契约（DEFINITION 全文，L5-39）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": (
            "创建并管理任务列表。"
            "多步任务开始前先与用户同步计划。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述（祈使句，如'运行测试'）",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态（同时刻最多1个in_progress，多余的自动调整为待处理）",
                            },
                        },
                        "required": ["content", "status"],
                    },
                    "description": "任务列表（每次提交完整列表，整体替换）",
                },
            },
            "required": ["todos"],
        },
    },
}
```

**依赖**（行号）：`typing.{List, Dict, Any}`（L3）。**无内部模块 import**（UI 与上下文经 `_tool_context` 回调注入）。

**被依赖**：
- `narnat_agent/tools/registry.py:26`（execute/DEFINITION；在 `_CONTEXT_TOOLS` 中）。
- `narnat_agent/core/tool_dispatcher.py:55`（归入 SERIAL_TOOLS 串行执行）、L340（计划优先豁免判断按工具名 `"TodoWrite"`）、L347-349。
- 实验脚本：`tool_exp/verify_todo_no_activeform.py:12`。

**状态**：无模块级可变全局、无类变量、无实例状态。副作用全部写向 `_tool_context`（`ui_callback`、`current_todos`）与传入的 `todos` 列表本身。

**行为要点**：
1. 空校验（L54-56）：`if not todos` → `"[错误: todos不能为空]"`。
2. 逐项校验（L59-66）：非 dict → `"[错误: 第{i+1}项不是对象]"`；缺 `content`/`status` → `"[错误: 第{i+1}项缺少必填字段: {field}]"`；status 非三值之一 → `"[错误: 第{i+1}项status非法: {status}]"`。
3. in_progress 容错（L68-79）：保留第一个 in_progress，其余就地改为 `pending` 并计数 `demoted`（L76、L77）；注释 L68-70 说明"硬报错会导致计划整体丢失"。
4. UI 回调（L81-83）：`_tool_context.ui_callback(todos)`（等价于 `core/tool_callbacks.py:30-49` 的逐行打印）。
5. 上下文同步（L85-87）：`_tool_context.current_todos = list(todos)`（**浅拷贝列表**，元素仍是同一 dict 对象）。
6. 修正提示（L90-92）：`demoted > 0` → 前缀 `"[已自动修正: 检测到多个in_progress，保留第一个，其余{N}项调整为待处理]\n"`。
7. 返回未完成清单（L94-104）：空 → `fix_note + "[任务全部完成]"`（L96-97）；否则 `fix_note + "[你有未完成的任务，请继续:]\n\n1. [进行中|待处理] {content}..."`（L99-104）。

**边界/异常行为**：
1. `todos` 为 None/空列表/`[]` → 错误（L55）。
2. `todos` 非列表（如字符串）→ `for i, todo in enumerate(...)` 会逐字符迭代 → 每项非 dict → 报"第1项不是对象"（L60-61 兜底）。
3. 多项 in_progress → 只保留第一个（L73-79），其余降级（返回文案告知）。
4. 全部 completed → `[任务全部完成]`（L96-97）。
5. 缺 status/content 的字段类型不做校验（如 status 为非字符串但恰好等于 `"pending"` 亦可）。
6. `_tool_context=None` → 跳过 UI 与上下文同步（L82、L86），仅返回文本（可用于纯函数式测试）。
7. DEFINITION 中项 schema 只声明 `content`/`status`；额外字段（如历史 `activeForm`）会被保留在 dict 中并被 UI 回调忽略（`core/tool_callbacks.py:35` 只读 content）——证据：`tool_exp/verify_todo_no_activeform.py:65` 专门回归"activeForm 旧值"场景。

**补丁痕迹**：
1. 中：就地修改传入的 `todos` 元素 dict（L76）——若调用方（消息历史）持有同一对象，历史内容被静默改写（判据：副作用泄漏/脆弱边界）。
2. 中：`current_todos` 浅拷贝（L87）导致上下文与传入列表共享元素 dict：后续外部改元素会同时改上下文（与上一条同源）。
3. 低：状态中文标签硬编码（L101-102），与 `core/tool_dispatcher.py:58-71` 的 `TOOL_LABELS`、UI 层图标体系各自维护一套展示语义。
4. 低：错误消息用 `f"第{i+1}项..."` 位置语（L61、L64、L66）——对 0-based 的 AI 语料无碍，但基准是"第1项"起始，需在 specs 中固化。

**可测性**：
- 可独立单测的部分：`execute` 全部（纯函数 + 注入 stub `_tool_context`：校验分支、降级分支、返回文案、`current_todos` 与回调调用断言）。
- 需要集成测试的部分：与 `core/tool_dispatcher.py:331-374` 计划优先拦截的联动（`current_todos` 中 in_progress 判定）；UI 回调实际输出（`core/tool_callbacks.py`）。
- 无法自动化的部分：终端渲染效果（颜色/图标）。

---

### narnat_agent/tools/goal_complete/__init__.py（63行）

**职责**：目标模式（/goal）下声明任务完成：收尾软提醒（计划未勾选时提醒一次）、置 `goal_complete` 标记、清理受管后台任务。

**对外接口**：
- `DEFINITION`（L7-25，见下"数据契约"）。
- `def execute(_tool_context=None) -> str`（L28）：主入口。

**数据契约（DEFINITION 全文，L7-25）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "GoalComplete",
        "description": (
            "声明当前目标任务已完成——任务执行的最后一步调用。\n"
            "调用前必须全部满足：\n"
            "1. 任务目标已实际达成，关键结果已经真实验证，而非仅凭推理判断；\n"
            "2. 最终答复已完整写出。\n"
            "调用方式：在输出最终答复后调用本工具。\n"
            "收到工具结果后只用一两句话简短收尾，不要重复完整答复内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}
```

**依赖**（行号）：无模块级 import；函数内延迟导入 `..background.cleanup_all`（L59）。

**被依赖**：
- `narnat_agent/tools/registry.py:29`（`from .goal_complete import execute as goal_complete_execute`；**不导入 DEFINITION**——定义不进默认工具表，L62-63 注释）。
- `narnat_agent/core/llm.py:214`（函数内 `from ..tools.goal_complete import DEFINITION as _GOAL_DEF`；L215-226 动态注入/移除）。
- 实验脚本：`tool_exp/audit_params_surface.py:9`、`tool_exp/verify_goal_e2e.py`、`verify_goal_mode.py`（间接）。

**状态**：无模块级可变全局、无类变量、无实例状态。产生的外部状态写入：`_tool_context.goal_complete`（L55）、`_tool_context.todo_reminded`（L46）。

**行为要点**：
1. `_tool_context is None` → 跳过全部状态写入，直接返回固定文本（L38）。
2. 收尾软提醒（L41-54）：未完成 todos 非空且 `todo_reminded` 为 False → 置 `todo_reminded = True` 并返回提醒（**不置 goal_complete**，让 AI 补勾选后再调）。
3. 提醒文案（L49-54）：列前 5 项内容，`> 5` 时追加 `"等共N项"`（L47-48）；给出两条出路（先 TodoWrite 勾选再调 / 再次调用并说明原因）。
4. 正常路径（L55-62）：`_tool_context.goal_complete = True`（L55）；`cleanup_all()` 清理受管后台任务（L58-62）；返回 `"[GOAL_COMPLETE] 目标任务已声明完成，自动续跑将停止。请向用户总结完成情况。"`（L63）。
5. 与主循环的联动（消费方）：`core/agent_loop.py:390` 统计展示条件；`core/agent.py:145-147`、`242-245` 据此结束目标模式并记 `end_reason = "goal_complete"`；`core/agent.py:84` 每轮复位。

**边界/异常行为**：
1. 无 `_tool_context` → 只返回文本，运行语义上的"完成"不被记录（L38）。
2. 计划未勾选 + 已提醒过 → 直接置 `goal_complete`（L45 条件 `not _tool_context.todo_reminded` 为假）——"提醒仅一次"。
3. 未完成项内容为空或缺 `content` → `t.get("content", "")` 兜底空串（L47）。
4. `cleanup_all` 抛异常 → `except Exception: pass`（L61-62）静默忽略（注释 L56-57 说明"硬兜底"）。
5. `current_todos` 元素非 dict → `t.get` 会 AttributeError（L42-43）；实际由 TodoWrite 校验保证 dict（契约耦合）。

**补丁痕迹**：
1. 中：工具层直接调用后台任务域的 `background.cleanup_all`（L59）——跨层引用（files/goal 工具 → background 模块），且延迟导入 + 吞异常（L58-62）（判据：跨层引用 + 吞异常）。
2. 中："收尾软提醒"逻辑与 `core/agent_loop.py:347-354` 的同名机制重复实现（两处各自判 `todo_reminded` 并各写提醒文案）（判据：重复代码）。
3. 低：返回文本 `[GOAL_COMPLETE]` 前缀是面向 LLM 的魔法标记（L63），与 `AWAIT_CONFIRM` 同类的跨模块字符串契约。
4. 低：`execute(_tool_context=None)` 的无上下文行为与有上下文行为差异大（不置标记），但文档串（L28-37）未提示。

**可测性**：
- 可独立单测的部分：`execute`（注入 `ToolContext`：无上下文 / 有未完成计划首次调用 / 再次调用 / 计划全完成）；`cleanup_all` 可 monkeypatch 断言被调用。
- 需要集成测试的部分：与 `core/agent.py`（复位、结束原因）和 `core/llm.py:set_goal_tool`（定义注入）的联动——已有 `tool_exp/verify_goal_e2e.py`、`verify_goal_mode.py`。
- 无法自动化的部分：无。

---

### narnat_agent/tools/mcp_tool/__init__.py（111行）

**职责**：MCP 连接通道工具——用 AI 给出的配置连接本地 stdio MCP 服务器（工具热注册）或断开连接（注销工具）。

**对外接口**：
- `DEFINITION`（L10-50，见下"数据契约"）。
- `_MAX_TOOLS_SHOWN = 20`（L52）：connect 结果里工具名展示上限。
- `def execute(action: str = "connect", name: str = "", config: dict = None, _tool_context=None) -> str`（L55-56）：主入口。
- `def _connect(manager, name: str, config: dict) -> str`（L75）：连接分支。
- `def _disconnect(manager, name: str) -> str`（L96）：断开分支。

**数据契约（DEFINITION 全文，L10-50）**：
```python
DEFINITION = {
    "type": "function",
    "function": {
        "name": "MCP",
        "description": "MCP 服务器连接通道（仅支持本地 stdio 型服务器）。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "disconnect"],
                    "description": (
                        "操作类型（默认connect）。"
                        "connect 连接一个本地 MCP（stdio）服务器，连接后其工具以 "
                        "mcp__<服务器名>__<工具名> 注册，后续轮次可直接调用；"
                        "disconnect 断开并注销其工具"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "服务器名（成为工具名前缀 mcp__<name>__）；"
                        "disconnect 用已连接的服务器名或 all（断开全部）"
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "connect 的启动配置：\n"
                        "{\"command\": \"python\", \"args\": [\"main.py\"], "
                        "\"env\": {}, \"cwd\": \"\"}\n"
                        "（中英文键均可；可选 startup_timeout_sec 启动超时、"
                        "tool_timeout_sec 工具调用超时、"
                        "enabled_tools/disabled_tools 工具白/黑名单）"
                    ),
                },
            },
            "required": [],
        },
    },
}
```

**依赖**（行号）：`...mcp.McpError`（L7）；`..exec_signal.error_line`（L8）；函数内延迟导入 `...config.loader.parse_mcp_server`（L82）。

**被依赖**：
- `narnat_agent/tools/registry.py:28`（execute/DEFINITION；在 `_CONTEXT_TOOLS` 中）。
- 实验脚本：`tool_exp/mcp_test/test_mcp_runtime.py:24`（`execute as mcp_execute`）。

**状态**：无模块级可变全局、无类变量；常量 `_MAX_TOOLS_SHOWN = 20`（L52）。MCP 连接态存储在注入的 `manager`（`narnat_agent/mcp/__init__.py` 的 `McpManager`）中，本模块无状态。

**行为要点**：
1. manager 获取（L58-60）：`getattr(_tool_context, "mcp_manager", None)`；None → `error_line("MCP 管理器未初始化")`。
2. action 归一（L62）：`(action or "connect").strip().lower()`。
3. 分发（L64-68）：`connect` / `disconnect` / 其他 → `"未知 action: {action}（可用: connect/disconnect）"`。
4. 异常分层（L69-72）：`McpError` → `"MCP {action} 失败: {e}"`；其他异常 → `"MCP {action} 异常: {e}"`。
5. `_connect`（L75-93）：name 去空白必填（L77-78）；config 必须为非空 dict（L79-80）；`parse_mcp_server(name, config)`（L82-83），`None` 或缺 command → `"config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器"`（L84-85）；`manager.connect(name, config)` 返回 `(count, tool_names)`（L87）；输出首行 `"[已连接 MCP服务器 {name}：注册 {count} 个工具，后续轮次可直接调用]"`（L88）+ 每行一个工具名，超过 20 个时末尾 `"  …共{N}个"`（L89-92）。
6. `_disconnect`（L96-110）：name 必填（L99）；`all`/`*`/`全部`（小写比较）→ `manager.disconnect("all")` → `"[已断开全部 MCP服务器，注销 {removed} 个工具]"`（L100-102）；否则大小写不敏感解析目标名（L104-106）；未连接 → 列出当前已连接（L107-109）；成功 → `"[已断开 MCP服务器 {target}，注销 {removed} 个工具]"`（L110-111）。
7. 注销链路：`manager.disconnect` 内部调 `registry.unregister_dynamic_tools`（`mcp/__init__.py:172-173`）并从 LLM 工具表移除（L174-175）。

**边界/异常行为**：
1. `action` 传 None/空 → 默认 connect（L62）。
2. `name` 传空白串 → 对应分支的必填错误（L77-78、L98-99）。
3. `config` 为 None/`{}`/非 dict → 错误（L79-80）。
4. `config` 无 `command` 或 parse 失败 → 错误（L84-85）。
5. 同名重复 connect → 由 manager 的锁内检查处理（`mcp/__init__.py:118-144`），返回既有工具数（L142-144）。
6. `disconnect` 名字大小写不匹配 → 走小写匹配（L105-106）；仍不匹配 → 错误 + 已连接清单（L107-109）。
7. 工具名超过 20 个 → 只展示前 20 + 总数（L89-92）。
8. `_tool_context=None` → "MCP 管理器未初始化"（L58-60）。
9. 未验证：`manager.connect` 的实际超时/失败分支文案（属 `mcp` 包与 `config.loader.parse_mcp_server` 范围，本报告未展开）。

**补丁痕迹**：
1. 中：`getattr(_tool_context, "mcp_manager", None)`（L58）——上下文字段访问不走类型化接口（ToolContext 已声明类型 `Any`，见 tool_context L48），耦合度靠约定（判据：脆弱边界）。
2. 低：`_MAX_TOOLS_SHOWN = 20`（L52）与 `disconnect` 的 `("all", "*", "全部")` 别名集合（L100）均为硬编码魔法值。
3. 低：`parse_mcp_server` 被调用两次可能（本模块 L82-83 校验一次，manager 内部 `mcp/__init__.py:93,217` 再解析）——同配置双解析（重复校验）。
4. 低：工具名展示被硬截断为 20 个（L89-92）：超出部分 AI 只能从 `mcp__<name>__<tool>` 命名规则自行推断，是否可调用需试错。
5. 低：模块文档串（L1-5）是"只做两件事/不查配置/不教 AI 去哪找"的定位声明（判据：历史包袱注释——记录了此前的功能裁剪决策）。

**可测性**：
- 可独立单测的部分：`execute` 全部分支（fake manager 注入：未初始化 / 未知 action / connect 参数缺失 / 展示上限 / disconnect all / 未连接 / 大小写匹配）；`_connect`/`_disconnect` 直接调用。
- 需要集成测试的部分：真实 MCP stdio 服务器连接与工具热注册（`tool_exp/mcp_test/test_mcp_runtime.py` 已覆盖）。
- 无法自动化的部分：外部 MCP 服务器进程行为（需真实子进程/网络）。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

本组（15 文件）之间的内部边：

| 起点 | 终点 | 位置 | 说明 |
|------|------|------|------|
| tools/grep | tools/glob | grep/__init__.py:19 | 复用 `_expand_braces`/`_unescape_braces` |
| tools/grep | tools/param_utils | grep/__init__.py:18 | `to_bool` |
| tools/edit | tools/diff_utils | edit/__init__.py:14 | `colorize_diff` |
| tools/edit | tools/param_utils | edit/__init__.py:15 | `to_bool` |
| tools/edit | tools/read | edit/__init__.py:59（函数内） | `_detect_text_encoding` |
| tools/write | tools/diff_utils | write/__init__.py:9 | `colorize_diff`、`describe_bytes_only_change` |
| tools/write | tools/read | write/__init__.py:79（函数内） | `_detect_text_encoding` |
| tools/registry | tools/tool_context | registry.py:12 | 类型注解 |
| tools/registry | tools/token_estimate | registry.py:13 | `estimate_text_tokens` |
| tools/registry | tools/{read,glob,grep,edit,write,bash,terminal,web_search,todo_write,serial,mcp_tool,goal_complete} | registry.py:16-29 | 聚合导入（含 `background` 显式导入 L16） |
| tools/read | tools/token_estimate | read/__init__.py:12 | `estimate_text_tokens` |
| tools/todo_write | （无本组内依赖） | — | 仅 typing |
| tools/goal_complete | tools/background | goal_complete/__init__.py:59（函数内） | `cleanup_all`（跨出本组） |
| tools/mcp_tool | tools/exec_signal | mcp_tool/__init__.py:8 | `error_line`（跨出本组） |

本组指向组外（被本组依赖的）边：

| 起点 | 终点 | 位置 |
|------|------|------|
| tools/read, tools/edit, tools/write | tools/terminal | read:11、edit:16、write:10（`_normalize_device_for_tools`、`_file_tool_device_hint`） |
| tools/read, tools/edit, tools/write | tools/terminal.remote | read:141、edit:104、write:57（函数内，`remote_read/remote_edit/remote_write`） |
| tools/diff_utils | output | diff_utils.py:6（颜色常量） |
| tools/registry, tools/mcp_tool | tools/exec_signal | registry.py:11、mcp_tool:8 |
| tools/mcp_tool | mcp | mcp_tool:7（`McpError`）、mcp_tool:82（`parse_mcp_server`） |

组外指向本组（被依赖）的边：

| 起点 | 终点 | 位置 |
|------|------|------|
| assembly | tools/tool_context | assembly.py:25、100-113 |
| assembly | tools/registry | assembly.py:61-62 |
| core/agent_loop | tools/tool_context | agent_loop.py:14、29、239-254、350-374、411-453 |
| core/agent_loop | tools/registry | agent_loop.py:452-453 |
| core/tool_dispatcher | tools/tool_context | tool_dispatcher.py:16、76 |
| core/tool_dispatcher | tools/registry | tool_dispatcher.py:11、227 |
| core/compressor | tools/token_estimate | compressor.py:8、18 |
| core/llm | tools/goal_complete | llm.py:214 |
| core/agent | tools/tool_context | agent.py:84-91、145-147、242-245 |
| mcp | tools/registry | mcp/__init__.py:117、172-173 |
| mcp, mcp/client | tools/exec_signal | mcp/__init__.py:17、client.py:25 |
| tools/bash, tools/terminal, tools/serial, tools/background, tools/terminal/ssh_session | tools/exec_signal、tools/token_estimate、tools/tool_context(duck-typing) | bash:16-17,469-491；terminal:22,226-227,541-567,612-634,959-960；serial:16,325-329；background:45,691-692；ssh_session:38-39 |
| ui/renderer | （无 import，但复制了 colorize_diff 逻辑） | renderer.py:300-316（重复实现） |

### 模块级可变状态全清单（含读写方）

| # | 状态 | 位置 | 写方 | 读方 |
|---|------|------|------|------|
| 1 | `ToolContext` 实例字段（18 个） | tool_context.py:20-69 | assembly.py:100-113（构造）；agent.py:84-91（复位 4 个）；bash:479-483；terminal:554-559,622-626；serial:325-329；todo_write:46,55,87；goal_complete:46,55；agent_loop:241,451 | registry:138-147；glob:485-486；grep:195；read:30；todo_write:82-87；goal_complete:41-55；tool_dispatcher:334-359；agent_loop:239-245,350-374,390；web_search:159-164；mcp_tool:58；terminal:226,535,606,959-960；bash:469-483,507；serial:316-349,392-434；background:691-692 |
| 2 | `registry._DYNAMIC_IMPLEMENTATIONS` | registry.py:70 | register_dynamic_tools L83（mcp/__init__.py:136 调用）；unregister L93（mcp:173） | registry execute L122；get_tool_names L192 |
| 3 | `registry._DYNAMIC_DEFINITIONS` | registry.py:71 | register L84；unregister L94-97 | get_tool_definitions L197 → assembly:62 → LLM |
| 4 | `registry.TOOL_DEFINITIONS` | registry.py:57-61 | 无（模块加载时构造） | get_tool_definitions L197；`core/llm.py:189` 拷贝一份（GoalComplete 动态增删只改副本） |
| 5 | `glob._compile_pattern` 的 `lru_cache(256)` | glob/__init__.py:133 | 框架（functools）在调用时写 | glob execute L539,549 |
| 6 | `GlobLimits.*` / `GrepLimits.*` / `SearchConfig.*` 类常量 | glob:20-21；grep:24-45；web_search:23-24 | 无（可被外部赋值，当前无写入者） | glob:102,482,515；grep:172,328,332,372,515,522；web_search:124,164 |
| 7 | `exec_signal._TAG` / `_ERR_TAG` | exec_signal.py:18,22 | 无（进程启动时生成） | 全框架判定侧（registry → error_line） |
| 8 | `AWAIT_CONFIRM` 常量 + 三处字符串字面量副本 | tool_context.py:12；bash:491；terminal:567,634 | 无 | agent_loop:245 |
| 9 | `ToolContext` 内嵌可变容器：`api_keys`/`ignore_dirs`/`current_todos`/`pending_delete` | tool_context.py:26,29,51,55 | assembly（前两者）；todo_write:87；bash:483、terminal:559,626、serial:329；agent_loop:241 | web_search:159-164；glob:485-486；grep:195；goal_complete:41-43；tool_dispatcher:357-360；agent_loop:239-245 |
| 10 | `terminal` 会话表 `_sessions`（本组经 `_normalize_device_for_tools`/`_file_tool_device_hint` 间接读） | terminal/__init__.py（组外） | terminal connect/close | read:136-138、edit:99-101、write:52-54（经 `_list_devices`） |

> 注：本组的 `read`/`glob`/`grep`/`edit`/`write`/`web_search`/`todo_write`/`goal_complete`/`mcp_tool` 除上述外均**无模块级可变状态**；`goal_complete`/`mcp_tool` 的副作用全部外化到 `ToolContext` 与注入的 manager。

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

| # | 严重度 | 位置 | 判据 | 摘要 |
|---|-------|------|------|------|
| 1 | 高 | read/__init__.py:22-64 vs registry.py:137-150 | 跨层重复 + 职责混杂 | "输出上限"两套截断实现（Read 按行给 offset；registry 保留首尾），同一配置产生两种语义 |
| 2 | 高 | tool_context.py:12 vs bash/__init__.py:491、terminal/__init__.py:567,634 | 跨层引用 + 魔法值 | `AWAIT_CONFIRM` 哨兵被三处字符串字面量复制，改常量即静默失效 |
| 3 | 高 | write/__init__.py:95 vs edit/__init__.py:64-71 | 行为不一致 + 隐藏契约 | Write 固定 UTF-8（不保持原编码），Edit 保持原编码；Write 的 DEFINITION 未声明编码行为 |
| 4 | 中 | read/__init__.py:92-111 vs grep/__init__.py:109-126 | 重复代码 | 编码探测（utf-8/gbk + 3 字节窗口重试）两份逐字重复实现 |
| 5 | 中 | edit/__init__.py:214-225 vs write/__init__.py:118-129 | 重复代码 | unified diff 生成函数完全重复（仅 file_path 来源不同） |
| 6 | 中 | diff_utils.py:9-29 vs ui/renderer.py:300-316 | 重复代码 | `colorize_diff` 两套实现 + 两套颜色常量（UI 注释自承"逻辑一致"） |
| 7 | 中 | grep/__init__.py:515-555 | 脆弱边界 + 行为不一致 | 并行（≥10 文件）与串行路径预算语义不同，同一查询结果详略随文件数变化 |
| 8 | 中 | registry.py:159-187 | 脆弱边界 + 误分类 | `_friendly_type_error` 解析 CPython 英文异常文本；且 `TypeError` 分类把工具内部 bug 误报为"参数错误"（L153-154） |
| 9 | 中 | write/__init__.py:91-92、goal_complete/__init__.py:61-62 | 吞异常 | 两处 `except Exception: pass`（Write 旧内容读取失败→diff 静默丢失；GoalComplete 后台清理失败→无痕） |
| 10 | 中 | tool_context.py:71-84（3 个方法） | 死代码 | `confirm_delete`/`on_todo_update`/`get_api_key` 生产代码无调用方（消费方直取字段），grep 可复验 |

次级（低）痕迹汇总（含行号，供重构时批量清理）：
- 魔法值：glob 100/50_000/256（glob:20,21,133）、grep 4096/100MB/1MB/30/10（grep:27,30,33,42,45）、read 8192/2000/200/300/20MB（read:159,17,35,19）、web_search 120/300 截断与 `zone="cn"`（web_search:76,84,117）、mcp_tool 20（mcp_tool:52）、edit `\x00` 哨兵（edit:136）、token_estimate `0.999`（token_estimate:35）。
- 历史包袱注释：glob:19、grep:22,41,150、web_search:18-22、read:15-16,25-28、registry:62-63、mcp_tool:1-5。
- 死引用/过期脚本：`tool_exp/_obsolete_20260910/verify_ux_round4.py:12`（import 已删除的 `grep._format_results`）。
- 双份"剥离前导 ./ 与 /"：glob:155-158 vs glob:388-391。
- 别名单字母化：diff_utils.py:6（`BLD as B`、`GRN as E`、`RED as X`）。

### 本组对外契约清单（被本组之外模块依赖的 public API）

新架构必须保持的行为面（组外引用点以行号列明）：

1. `tools.registry.execute(name, arguments, tool_context) -> (llm_result, color_diff)`（core/tool_dispatcher.py:11,227；core/agent_loop.py:452-453）。
2. `tools.registry.get_tool_definitions() -> List[Dict]`（assembly.py:61-62；tool_exp/verify_asm 等）。
3. `tools.registry.get_tool_names() -> List[str]`（mcp/__init__.py:117,127）。
4. `tools.registry.register_dynamic_tools(entries)` / `unregister_dynamic_tools(names)`（mcp/__init__.py:136,173）。
5. `tools.registry.TOOL_DEFINITIONS`（被 `core/llm.py:189` 拷贝；GoalComplete 不入表的契约见 registry.py:62-63 与 llm.py:207-226）。
6. `tools.tool_context.ToolContext` 全部字段与语义（assembly.py:100-113 构造；agent_loop.py:239-254,350-374,411-453；agent.py:84-91；bash/terminal/serial/todo_write/goal_complete/web_search/glob/grep/read/tool_dispatcher/background 消费）。
7. `tools.tool_context.AWAIT_CONFIRM` 值契约（agent_loop.py:14,245；bash:491、terminal:567,634 的字面量副本）。
8. `tools.token_estimate.estimate_text_tokens`（registry.py:145；read:53；bash:400；ssh_session:59）/ `estimate_message_tokens`（compressor.py:8,18）。
9. `tools.diff_utils.colorize_diff`（edit:230；write:90,112；terminal/remote.py:166,292）/ `describe_bytes_only_change`（write:110；terminal/remote.py:196）。
10. `tools.param_utils.to_bool`（edit:97；grep:175）。
11. `tools.read._detect_text_encoding(head) -> "utf-8-sig"|"gbk"`（edit:59-64；write:79-87；terminal/remote.py:77-78）。
12. `tools.glob._expand_braces` / `_unescape_braces`（grep:19,581,584）。
13. 各工具 `DEFINITION`（read/glob/grep/edit/write/web_search/todo_write/mcp_tool 经 registry 进 LLM；`goal_complete.DEFINITION` 由 `core/llm.py:214-221` 单独注入）。
14. 工具返回形态契约：Read/Glob/Grep/WebSearch/TodoWrite 返回 `str`；Edit/Write 返回 `(llm_result, color_diff)`；文本以 `"[错误: ...]"` 开头表示工具错误（core/tool_dispatcher.py:265-270 判定非命令类工具失败）。
15. `Read` 的行号格式 `"  {N}→{内容}"` 与 `/[无差异]`、`[已写入: ...]`、`[已替换N处]`、`[无匹配...]`、`[截断...]` 等返回文案前缀（AI 侧事实约定，UI 侧不解析；建议 specs 固化为字面量族）。

