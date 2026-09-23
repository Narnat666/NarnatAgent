# R7 现状报告：配置层（loader / defaults / session_store / skill_store）

- 代码基线：`D:\desktop\NarnatAgent`，HEAD = `7d075a2`（[更新] 新增上下文主动压缩功能）
- 调研方式：4 个源文件**逐行完整通读**（Read 全量），依赖关系用 Grep 全仓复验（`*.py`，含 `narnat_agent/**` 与仓库根 `main.py`）
- 行数核对（`find /c /v ""` 实测）：loader.py 872 / session_store.py 363 / defaults.py 328 / skill_store.py 318，与任务清单一致
- 本报告为只读调研产物，除本文件外未创建/修改/删除任何文件，未执行任何 git 写操作
- 标注「未验证」的条目表示仅静态阅读所得，未运行验证

---

### narnat_agent/config/loader.py（872 行）

**职责**：把 `narnat.json`（中文键）+ `narnat.md`（用户自定义指令）解析为**一次性构建**的 `Config` 数据类对象树（含系统 prompt 拼接与单位换算），并负责 `.narnat` 目录定位/创建与首次运行时的默认配置文件生成；另提供运行时 MCP 服务器配置项的独立解析函数 `parse_mcp_server`。

**对外接口**（public 类/函数/方法，逐个列签名；类行号取 `class` 关键字所在行，`@dataclass` 装饰器在其上一行）：

- `class AIConfig`（36-55，非 frozen dataclass）：AI 连接配置。
  - `api_key: str = DEFAULT_API_KEY`（42）
  - `base_url: str = DEFAULT_BASE_URL`（43）
  - `model: str = DEFAULT_MODEL`（44）
  - `model_options: list = field(default_factory=lambda: [DEFAULT_MODEL])`（45）
  - `protocol: str = DEFAULT_PROTOCOL`（46）
  - `temperature: Optional[float] = None`（47）
  - `max_tokens: Optional[int] = None`（48）
  - `thinking_enabled: bool = DEFAULT_THINKING_ENABLED`（49）
  - `thinking_effort: str = DEFAULT_THINKING_EFFORT`（50）
  - `thinking_passback: bool = DEFAULT_THINKING_PASSBACK`（51）
  - `thinking_options: dict = field(default_factory=lambda: {"high": "高", "max": "全开"})`（52）
  - `context_window: int = DEFAULT_CONTEXT_WINDOW`（53）
  - `retry_count: int = 3`（54）
  - `goal_max_rounds: int = DEFAULT_GOAL_MAX_ROUNDS`（55）
- `class PathConfig`（59-65，frozen）：`project_root: str = ""`（61）、`narnat_dir: str = ""`（62）、`config_dir: str = ""`（63）、`data_dir: str = ""`（64）、`logs_dir: str = ""`（65）
- `class ToolConfig`（69-75，frozen）：`max_sessions: int = 5`（71）、`max_transfer_mb: int = 100`（72）、`max_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_KB * 1024`（73）、`max_timeout_seconds: int = DEFAULT_MAX_TIMEOUT_SECONDS`（74）、`ignore_dirs: tuple = ()`（75）
- `class SafetyConfig`（79-82，frozen）：`git_skip_confirm: bool = DEFAULT_GIT_SKIP`（81）、`rm_skip_confirm: bool = DEFAULT_RM_SKIP`（82）
- `class McpServerConfig`（86-102，frozen）：`name`（93）、`command`（94）、`args: tuple`（95）、`env: Dict[str, str]`（96）、`cwd`（97）、`enabled: bool`（98）、`startup_timeout: int = DEFAULT_MCP_STARTUP_TIMEOUT`（99）、`tool_timeout: int = DEFAULT_MCP_TOOL_TIMEOUT`（100）、`enabled_tools: tuple`（101）、`disabled_tools: tuple`（102）
- `class PlanConfig`（106-109，frozen）：`require_plan: bool = DEFAULT_REQUIRE_PLAN`（108）、`min_tools: int = DEFAULT_MIN_TOOLS`（109）
- `class SessionConfig`（113-120，frozen）：`auto_save: bool = DEFAULT_AUTO_SAVE`（115）、`auto_save_tokens: int = DEFAULT_AUTO_SAVE_TOKENS`（116）、`show_ratio: bool = DEFAULT_SHOW_RATIO`（117）、`warn_ratio: int = DEFAULT_WARN_RATIO`（118）、`compress_ratio: int = DEFAULT_COMPRESS_RATIO`（119）、`retain_tokens: int = DEFAULT_COMPRESS_RETAIN_TOKENS`（120）
- `class SkillConfig`（124-128，frozen）：`project_roots: Optional[tuple] = None`（128）
- `class PricingConfig`（132-136，frozen）：`user_pricing: Dict[str, Dict[str, float]]`（136）
- `class BalanceConfig`（140-146，frozen）：`enabled: bool = False`（142）、`url: str = ""`（143）、`auth_method: str = "bearer"`（144）、`value_path: str = ""`（145）、`currency_path: str = ""`（146）
- `class CostLogConfig`（150-160，frozen）：`enabled: bool = False`（158）、`path: str = ""`（159）、`max_bytes: int = 50 * 1024 * 1024`（160）
- `class UIConfig`（164-185，frozen）：`raw: Dict`（182）、`show_cost: bool = False`（183）、`show_balance: bool = False`（184）、`max_output_tokens: int = 128000`（185）
- `class Config`（189-207，非 frozen）：`ai`（195）、`paths`（196）、`tools`（197）、`safety`（198）、`plan`（199）、`session`（200）、`skills`（201）、`pricing`（202）、`balance`（203）、`cost_log`（204）、`ui`（205）、`api_keys: dict`（206，无默认工厂外的类型校验）、`system_prompt: str`（207）
- `def parse_mcp_server(name: str, entry: dict) -> Optional[McpServerConfig]`（318）：解析单个 MCP 服务器配置项（中英文键别名兼容；`entry` 非 dict 或 `name` 空白 → None）
- `def load_config(project_root: Optional[str] = None, headless: bool = False) -> Config`（704）：定位/创建 `.narnat`，读 JSON/MD，返回 `Config`；`headless=True` 时剥离 narnat.md 的 subagent:hide 区块

私有但被本报告引用的模块级函数：`_is_nuitka_onefile`（210）、`_find_narnat_exe_dir`（225）、`_find_project_root`（261）、`_coerce`（296）、`_parse_project_skill_roots`（306）、`_parse_token_amount`（385）、`_parse_pricing`（408）、`_load_json`（426）、`_parse_model_config`（439）、`_build_ai_config`（456）、`_build_ui_config`（491）、`_pop_bool_any`（611）、`_pop_int_any`（618）、`_build_pricing_config`（625）、`_build_balance_config`（635）、`_build_cost_log_config`（647）、`_load_user_md`（666）、`_strip_subagent_hidden`（686）、`_build_system_prompt`（691）。注意 `_parse_model_config` 被测试脚本 `tool_exp/verify_mode_cmd.py:11` 从外部 import（私有函数越界使用）。

**依赖**（本文件 import 的内部模块）：

- `from .defaults import (...)`（13-32）：`BASE_PROMPT_TEMPLATE, COMPRESS_PROMPT, NARNAT_DIR, NARNAT_JSON, NARNAT_MD, CONFIG_SUBDIR, DATA_SUBDIR, LOGS_SUBDIR, DEFAULT_IGNORE_DIRS, DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_PROTOCOL, DEFAULT_THINKING_ENABLED, DEFAULT_THINKING_EFFORT, DEFAULT_THINKING_PASSBACK, DEFAULT_CONTEXT_WINDOW, DEFAULT_SHOW_RATIO, DEFAULT_WARN_RATIO, DEFAULT_COMPRESS_RATIO, DEFAULT_COMPRESS_RETAIN_TOKENS, DEFAULT_GIT_SKIP, DEFAULT_RM_SKIP, DEFAULT_REQUIRE_PLAN, DEFAULT_MIN_TOOLS, DEFAULT_MAX_TOOL_OUTPUT_KB, DEFAULT_MAX_TIMEOUT_SECONDS, DEFAULT_MCP_STARTUP_TIMEOUT, DEFAULT_MCP_TOOL_TIMEOUT, DEFAULT_AUTO_SAVE, DEFAULT_AUTO_SAVE_TOKENS, DEFAULT_GOAL_MAX_ROUNDS`
- 标准库：`json, os, re, sys, platform, dataclasses, typing`（5-11）；局部 import：`ctypes`（237）、`shutil`（252）
- 反向无：`config/loader.py` 不被 `defaults.py/session_store.py/skill_store.py` import（无循环）

**被依赖**（谁 import 本文件；含 import 符号与行号，可用 grep `config\.loader` 复验）：

- `narnat_agent/assembly.py:11` `from .config.loader import load_config`（调用点 39）
- `narnat_agent/core/agent_loop.py:19` `from ..config.loader import Config`（类型注解）
- `narnat_agent/core/auto_save_manager.py:10` `from ..config.loader import Config`（构造注入）
- `narnat_agent/core/billing.py:16` `from ..config.loader import BalanceConfig`（在 `if TYPE_CHECKING:` 内，15-16，仅类型标注）
- `narnat_agent/core/compression_coordinator.py:11` `from ..config.loader import Config`
- `narnat_agent/core/llm.py:22` `from ..config.loader import AIConfig`（运行时消费全部 AI 字段，见 llm.py:191-197、271-272、287-341、563-607、1029-1030）
- `narnat_agent/core/summarizer.py:10` `from ..config.loader import Config`
- `narnat_agent/mcp/__init__.py:16` `from ..config.loader import parse_mcp_server`（调用 93、217）
- `narnat_agent/tools/mcp_tool/__init__.py:82` `from ...config.loader import parse_mcp_server`（函数内局部 import，调用 83）
- 仓库根 `main.py` **不**直接 import config（经由 `narnat_agent.core.agent.Agent` → `assembly.load_config`，main.py:33、54/58）
- 非生产代码引用（tool_exp 脚本）：`tool_exp/api_contract_test.py:15`、`tool_exp/verify_goal_e2e.py:21`、`tool_exp/verify_goal_mode.py:33,56`、`tool_exp/verify_import_cleanup.py:18`、`tool_exp/verify_mode_cmd.py:11`、`tool_exp/verify_stream_fix.py:13`、`tool_exp/retry_unify_probe.py:15`、`tool_exp/stall_test.py:21`、`tool_exp/repair_seq_test.py:20`、`tool_exp/mcp_test/demo_persistence.py:17`、`tool_exp/mcp_test/test_mcp.py:20`、`tool_exp/mcp_test/test_mcp_runtime.py:21,232`

**状态**：

- 模块级全局
  - `_SUBAGENT_HIDE_RE`（680-683）：编译后的 `re.Pattern`，只读；唯一读者 `_strip_subagent_hidden`（688）
  - 无其它模块级可写全局；映射表 `_SECTION_MAP`/`_KEY_MAPS`/`_OLD_COLOR_MAP`/`_COLOR_ZH_EN` 均为函数内局部（498-548、575-587、593-595），每次调用重建
- 类变量：无
- 实例状态（`Config` 树，`load_config` 一次性赋值；frozen 情况见上）
  - 运行时**可变**字段（详见总表"模块级可变状态全清单"）：`Config.ai.{thinking_effort, thinking_passback, model}`、`Config.ui.raw`（dict 容器内部可变）
  - 构造函数签名即全部实例状态；无隐藏属性

**行为要点**（编号；行号；可观察行为）：

1. 项目根定位顺序（`_find_project_root`，261-293）：① 环境变量 `NARNAT_HOME` 且 `<NARNAT_HOME>/.narnat` 存在目录 → 返回该值（263-265）；② Nuitka onefile（`_is_nuitka_onefile` 为真，详见 6）→ `_find_narnat_exe_dir()`；该目录含 `.narnat` → 返回之，否则**仍返回该目录**，再次拿不到则返回 `os.path.dirname(sys.executable)`（268-274）；③ `sys.frozen` 为真（PyInstaller/standalone）→ `os.path.dirname(sys.executable)`（有/无 `.narnat` 都返回该目录，277-281）；④ 开发模式：从 `os.getcwd()` 起**最多向上查 10 层**找含 `.narnat` 的目录（286-292），找不到返回 `cwd`（293）
2. `load_config` 入口把根转为绝对路径：`root = os.path.abspath(project_root or _find_project_root())`（715）；随后固定拼出 `narnat_dir = root/.narnat`、`config_dir = .narnat/config`、`data_dir = .narnat/data`、`logs_dir = .narnat/logs`（716-719）
3. 目录副作用：`os.makedirs(config_dir, exist_ok=True)` 与 `os.makedirs(data_dir, exist_ok=True)`（722-723）；**logs 目录此处不创建**（注释 721 说明由 `logger.start()` 在 debug 模式按需创建），但 `PathConfig.logs_dir` 始终填值（839）
4. 关键文件首次生成（726-778）：对 `narnat.json` 与 `narnat.md` 逐个检查 `os.path.isfile`，缺失则写文件——`narnat.json` 写入固定 JSON（731-776，`indent=2, ensure_ascii=False`），`narnat.md` 写入空字符串（778）。生成的 JSON 顶层键为：`智能体`（接口密钥/接口地址/模型{当前,列表}/协议/温度/最大输出token数/上下文窗口大小/目标模式最大轮数/思考{启用,强度,强度选项}/LLM重试次数）、`余额查询`（启用=True/查询地址/认证方式/响应路径/货币路径）、`接口密钥组`（websearch/websearch_url）、`定价`（模型={}）、`费用日志`（启用=False/输出文件=""/最大容量MB=50）、`界面`（show_cost/show_balance/max_output_tokens，全用英文键）、`工具`（输出上限KB/超时上限秒）、`会话`（自动保存Token量）、`压缩`（占比显示/告警/压缩/保留尾部）、`计划`（空 dict）、`忽略目录`（DEFAULT_IGNORE_DIRS 列表）——注意其中 `协议`、`最大输出token数`、`思考.*`、`LLM重试次数`、`余额查询.启用` 等为**硬编码值**而非 defaults 常量（详见补丁痕迹 2）
5. JSON 读取（`_load_json`，426-435）：只在 `config_dir/narnat.json` 读取；文件不存在 → `{}`（429-430）；`json.JSONDecodeError` 或 `OSError` → `{}`（434-435，静默吞掉，无日志/提示）
6. Nuitka onefile 判定（`_is_nuitka_onefile`，210-222）：满足任一即真——(a) `sys.executable` 所在目录路径含 `onefile_` 且可执行名属于 `("python.exe","python","python3")`（216-219）；(b) `__main__` 模块有 `__compiled__` 属性（220-221）
7. exe 目录定位（`_find_narnat_exe_dir`，225-258）：① win32 下 `GetModuleFileNameW`（1024 缓冲）取模块真实路径，文件存在则返回其目录（235-245，异常静默忽略）；② `sys.argv[0]` 是存在的文件 → 返回 `os.path.dirname(os.path.abspath(argv0))`（247-249）；③ `argv[0]` 是裸名（无目录部分）→ `shutil.which` 解析（250-257）；④ 都不行返回 `None`（258）
8. `AIConfig` 构建（`_build_ai_config`，456-488）：
   - `协议` 直接取值无白名单校验（460）；`接口地址` 460-461；`模型` 走 `_parse_model_config`（462）
   - `_parse_model_config`（439-453）：非 dict → `(DEFAULT_MODEL, [DEFAULT_MODEL])`；`列表` 非 list → `[]`；元素过滤非 str（449）；`当前` 为空时取 `列表[0]` 或 `DEFAULT_MODEL`（450）；`当前` 不在列表中则 **insert(0)** 到列表首位（451-452）
   - `思考.启用/强度/回传/强度选项` 取默认（464-468）；`回传` 键名是"回传"，与 narnat.json 首次生成模板（744-748，只有 启用/强度/强度选项）不同——即首次生成的配置没有"回传"键
   - `上下文窗口大小` 与 `温度`/`最大输出token数` 用 `_coerce`（471、480-481）：`_coerce` 语义 = 值为 `None`/`""` 或转换抛异常时返回 `None`（296-303）；`context_window` 在解析结果为 None 时用 `DEFAULT_CONTEXT_WINDOW`，显式 ≤0 保留原值（470-472）
   - `目标模式最大轮数`：`_coerce(...) or DEFAULT_GOAL_MAX_ROUNDS`（487）——0/None/非法都变 100，**负值被保留**（负数真值）
9. `retry_count` 单独二次赋值：`load_config` 里用 `int(data.get("智能体", {}).get("LLM重试次数", 3))` 覆盖（828），并在 815-830 **重建整个 AIConfig**（13 个字段逐一复制）
10. UI 配置构建（`_build_ui_config`，491-608）：
    - 顶层开关先弹出：`show_cost`/`显示费用`、`show_balance`/`显示余额`（554-555，`_pop_bool_any` 用 `bool(...)`）、`max_output_tokens`/`最大输出token数`（556，`_pop_int_any` 默认值 = 传入的 `max_output_tokens`）
    - Section 名中→英（`_SECTION_MAP`，498-503、559-561）：颜色→colors、基础色→base_colors、标注/标记→markdown、代码块→codeblock、差异/对比→diff、框架→ui、命令→cmd、提示符→prompt；**仅当英文键不存在时**才替换（560）
    - Section 内键中→英（`_KEY_MAPS`，504-548、563-572）
    - 旧扁平键迁移（`_OLD_COLOR_MAP`，574-590）：`用户输入色/标题色/成功色/…代码块背景色` 等 11 个键按 `setdefault` 写入对应 section（不覆盖已有值）
    - 配方值中文色名→英文（592-605）：对 colors/markdown/codeblock/diff/ui/cmd/prompt 七段的字符串值做**逐色名 replace**（无词边界，子串也会命中）
    - 返回 `UIConfig(raw=raw, show_cost=..., show_balance=..., max_output_tokens=...)`（607-608）；`raw` 是原 dict 的浅拷贝（551 `dict(ui)`），弹掉的三个键不在 raw 中（由 assembly.py:43-45 补回）
11. 其余分组构建：定价（625-632，仅当 `data["定价"]` 非空时解析 `定价.模型`）、余额查询（635-644，5 字段直取）、费用日志（647-663，见边界 10）
12. 用户指令读取（`_load_user_md`，666-675）：读 `config_dir/narnat.md`（UTF-8），`strip()` 后返回；不存在或 `OSError` → `""`
13. headless 剥离（793-794 + 678-688）：正则 `<!--\s*subagent:hide\s*-->.*?<!--\s*/subagent:hide\s*-->`（DOTALL、非贪婪）把所有配对区块整体删除；`load_config(headless=True)` 才会调用
14. 系统 prompt 拼接（`_build_system_prompt`，691-701）：`BASE_PROMPT_TEMPLATE.format(model=…, cwd=cwd or os.getcwd(), platform=os_name or platform.system(), shell=shell_name or …)`（693-698），`user_md` 非空则 `"\n".join([模板, user_md])`（699-701）。调用点显式传入 `cwd=os.getcwd()`、`os_name=platform.system()`、`shell_name="cmd.exe" if sys.platform == "win32" else "bash"`（795-803）；模板（defaults.py:41-47）只引用 `{model}/{cwd}/{platform}`，`{shell}` 未被模板引用（注释 800-801 自认此值为"备将来使用"）
15. 单位换算（805-812）：`工具.输出上限KB` → `max_output_chars = kb * 1024`，`kb <= 0` → 0（806-807）；`压缩.保留尾部` 用 `_coerce` 判 None → 默认 16000，否则 `max(0, 值)`（810-812，显式 0 合法）
16. 最终组装（832-872）：`Config(ai=重建后的 ai_config, paths=..., tools=..., safety=..., plan=..., session=..., skills=SkillConfig(project_roots=_parse_project_skill_roots(data)), pricing=..., balance=..., cost_log=..., ui=..., api_keys=data.get("接口密钥组", {}), system_prompt=...)`
17. `parse_mcp_server`（318-382）契约：`name` 去空白为空或 `entry` 非 dict → `None`（324-325）；键别名依次 `参数|args`（335）、`命令|command`（341）、`环境变量|env`（346）、`启动超时秒|startup_timeout_sec`（350）、`工具超时秒|tool_timeout_sec`（354）、`工具白名单|enabled_tools`（358）、`工具黑名单|disabled_tools`（362）、`启用|enabled`（367）、`工作目录|cwd`（376）；取值规则"第一个非 None 非空串"（327-333）
18. `parse_mcp_server` 归一化：`command` 为数组（Claude 客户端风格）时拆成 `command=数组[0]`、`args=数组[1:] + 已有 args`（339-344）；`env` 全部字符串化（375）；`args/enabled_tools/disabled_tools` 转 tuple 且字符串化（374、380-381）；`enabled` 字符串容错（366-369，见边界 13）；超时 ≤0/非数值 → 各自默认（350-356）
19. `_parse_project_skill_roots`（306-315）：`技能.项目技能目录` 是 list → `tuple(非空字符串项)`（可为空元组）；否则 `None`
20. `api_keys` 直取 `data.get("接口密钥组", {})`（785），无类型校验，原样放入 `Config.api_keys`

**边界/异常行为**（编号；行号）：

1. `NARNAT_HOME` 指向的目录**不含** `.narnat` 子目录时被忽略、继续走后续分支（264）；即"仅当目标已是 narnat 项目才生效"
2. onefile 分支在 exe 目录没有 `.narnat` 时**也返回 exe 目录**（272-273），与 frozen 分支（279-281）行为一致；而开发模式找不到 `.narnat` 时返回 cwd（293）
3. 向上查找上限 10 层（286），硬编码；路径极深且 `.narnat` 在 10 层之外 → 退回 cwd
4. `narnat.json` 内容损坏（非法 JSON）→ 全默认配置静默生效（434-435），用户无任何提示；文件存在但顶层是 JSON 数组/字符串/数字 → `_load_json` 返回非 dict，随后 `data.get(...)`（如 458）抛 `AttributeError`，且不在捕获范围（`load_config` 无 try）
5. `数据JSON` 中 `"智能体": []` 之类字段类型错（非 dict）→ `ai.get` 抛 `AttributeError`（458）；`"思考": "high"` → `thinking_cfg.get` 抛 `AttributeError`（465）
6. `_coerce` 对 `int("10.5")`、`int("abc")` 返回 None（300-303）；对布尔 `True` → `int(True)=1`（未显式排除 bool，与 `_parse_token_amount` 的 bool 排除策略不同，387-390）
7. `温度` 字符串 `"abc"` → None（不传温度）；`最大输出token数` 字符串非法 → None（不传 max_tokens）。注意 `ai.max_tokens=None` 时 `_build_ui_config` 用 `ai_config.max_tokens or 128000` → 128000（789）
8. `目标模式最大轮数` 为 0/空/非法 → 100；为负数（如 -5）→ 保留 -5（487，真值绕过 `or`）
9. `上下文窗口大小` 为 0 或负 → 原值保留（472），下游 `ContextManager` 视为无效（core/context.py:48）；为 `true`（布尔）→ `_coerce(True,int)=1`（边界偶发）
10. 费用日志（647-663）：`费用日志` 非 dict → 按空 dict 处理（650-651）；`最大容量MB` 非数值/None → 50MB（655-659）；负值 → 0（=不限制，660-661）；`max_bytes = max_mb * 1024 * 1024` 当 `max_mb>0`，否则 0（662）
11. `界面.max_output_tokens`（中文键 `最大输出token数`）为非法字符串 → `_pop_int_any` 的 `int(...)` 抛 `ValueError` 且无捕获（618-622）→ 启动失败
12. `工具.输出上限KB`/`超时上限秒`/`SSH最大会话数`/`最大传输文件MB`/`计划.计划最低工具数`/`LLM重试次数` 走裸 `int(...)`（806、842-845、854、828），非法字符串 → `ValueError` 未捕获 → 启动失败；浮点值则截断（如 10.9→10）
13. `工具.输出上限KB` ≤0 → `max_output_chars = 0`（807，0=不限制语义）
14. `忽略目录` 为字符串（如 `"output"`）→ `tuple("output")` 得到 `('o','u','t','p','u','t')`（846）；为 dict → 取键元组；为 None/缺失 → `()`
15. `压缩.告警`/`压缩.压缩` 为 0 或非法 → 回落到默认 50/95（861-862，`or` 使 0 不可用；负值保留）
16. `会话.自动保存Token量` 支持数字或 `"10k"/"1.5K"`（`_parse_token_amount`，385-405）：`k` 后缀 ×1000 后取整；bool → 默认；负数 → 0（`max(0, …)`）
17. `界面` 中字符串布尔（`"show_cost": "false"`）→ `bool("false")` = True（611-615）；同样缺容错见"思考.启用/回传"（465、467）
18. `_build_ui_config` 的 `_KEY_MAPS["colors"]` 只有 6 个旧键（505-510），而 `_OLD_COLOR_MAP` 会把 `成功色/错误色/链接色/装饰色` 写进 `base_colors`（575-587）——同一中文键"成功色"在 `颜色` 段与旧扁平键两条路径下落到不同 section（`colors.success` vs `base_colors.success`）
19. 配方值替换为纯字符串 `replace`（600-605）：值 `"bold 强调色"` → `"bold accent"`；但用户自定义色名（当前 narnat.json 中的 `红/月光白/次文字` 等，见 `.narnat/config/narnat.json:57-70`）不在映射表中，保持原样交由 UI 层解析
20. `narnat.md` 读取失败（权限/编码）→ `""`（674-675）；内容含 BOM 时 `strip()` 后 BOM 仍保留在首行（UTF-8 非 sig 读取，672-673）
21. `_strip_subagent_hidden` 只删除**配对**区块（681-682）：只有开始标记或只有结束标记时原样保留；非贪婪匹配意味着同段落多个区块逐个替换
22. `parse_mcp_server` 中 `enabled` 非字符串且非 None → `bool(value)`（仅字符串走白名单式解析，367-369）；`env` 值为 None → `str(None)`=`"None"`（375）
23. onefile 检测在 Windows 之外的平台也可由 `__compiled__` 触发（220-221）；`GetModuleFileNameW` 分支仅在 win32 执行（235），异常（如 ctypes 不可用）静默忽略（244-245）
24. `_find_narnat_exe_dir` 中 `shutil.which` 失败/异常 → 静默忽略（256-257）；全部失败返回 None → onefile 分支回退 `os.path.dirname(sys.executable)`（274）
25. 生成的 `narnat.md` 为空文件（778）：首次运行时 `user_md=""`，系统 prompt 只有模板段（699-701 的 `if user_md` 分支不进入）

**补丁痕迹**（编号；行号 + 证据；严重度）：

1. **（高）`AIConfig` 在 `load_config` 内被整体重建以补 `retry_count`**：815-830 逐字段复制 13 个字段，注释自认"补充 AIConfig 的 retry_count（从JSON读取，不在 _build_ai_config 中处理）"（814）；新增字段必须同时改 `AIConfig`、`_build_ai_config`、此处三处
2. **（高）首次生成 narnat.json 的默认值硬编码且与其他默认值来源不一致**：`"协议": "anthropic"`（740，未用 `DEFAULT_PROTOCOL`）、`"最大输出token数": 128000`（741）、`"思考": {...}`（744-748）、`"LLM重试次数": 3`（749）、`"余额查询"."启用": True`（752）——而 `BalanceConfig.enabled` 默认 False（142），`DEFAULT_THINKING_PASSBACK`/`DEFAULT_AUTO_SAVE`/`DEFAULT_REQUIRE_PLAN` 等键根本未生成
3. **（高）`_load_json` 吞异常**：434-435 `except (json.JSONDecodeError, OSError): return {}`，配置写错静默回退全默认；且返回类型未校验顶层为 dict（426-435），非 dict 时下游 458 行 `AttributeError` 崩溃
4. **（高）`session_store._strip_surrogates` 与 `core/llm.py:28-35` 完全重复**（session_store.py:12-19 vs llm.py:28-35，逐行相同），两份实现各自演化风险高
5. **（中）解析风格双轨制**：`_coerce`（296-303）容错，但 `工具/安全/计划/会话.自动保存/LLM重试次数/界面.max_output_tokens` 走裸 `int()`/`bool()`（806、828、842-845、849-850、853-854、857、618-622）；导致 `bool("false")==True`、非法字符串直接崩溃两类不同故障
6. **（中）`_pop_int_any` 无容错**（618-622）：与同区域 `_pop_bool_any`（611-615）并列，但前者可让进程启动失败
7. **（中）忽略目录解析脆弱**：`tuple(data.get("忽略目录") or [])`（846），字符串按字符拆解、dict 取键，无类型校验
8. **（中）UI 键映射双源**：`_KEY_MAPS["colors"]`（505-510）与 `_OLD_COLOR_MAP`（575-587）都处理"成功色/错误色/链接色/装饰色"，落到不同 section；`_SECTION_MAP` 还有别名（标注/标记、差异/对比，500-502）
9. **（中）`_build_ui_config` 每次调用重建两张映射表**（498-548）+ 函数内 100 行的键转换过程；`max_output_tokens` 默认值 128000 在本文件出现 5 次（185、491、743、764、789）无常量
10. **（中）`_MAX_DIR_DEPTH`/扫描深度双常量**见 skill_store 节；loader 侧对应问题是向上查找 10 层硬编码（286）
11. **（中）职责混杂：narnat.md 的 subagent 隐藏协议解析放在 loader**（678-688）与配置解析同文件；这是 prompt 组装策略而非配置项
12. **（低）`shell_name` 死参数**：`_build_system_prompt` 传入（697、802）但模板无 `{shell}`，注释自认"备将来模板使用"（800-801）
13. **（低）历史包袱注释**：`AIConfig` 类注释"后续 Phase 拆分 LLMClient 后将移出此字段，届时本类将改为 frozen"（39-41）、`Config` 同类注释（190-194）——标记了未完成的重构
14. **（低）`if not startup_timeout or startup_timeout <= 0`**（351、355）双条件冗余（`not 0` 即 `<=0`）
15. **（低）`_coerce` 空串语义**：`v in (None, "")`（298）把"显式空串"与"缺失"等同，无法表达"显式清空"，且不 trim 空白串（`" "` 会走转换后失败→None，行为等价但路径不同）
16. **（低）平台分支不可测**：`_is_nuitka_onefile`/`_find_narnat_exe_dir` 直接读 `sys.executable/sys.argv/sys.platform` 与 `ctypes.windll`（210-258），无注入点
17. **（低）`mcp` 参数解析放在 config 层但只服务运行时 MCP 工具**（318-382）：`parse_mcp_server` 唯一目的是给 `mcp/__init__.py`、`tools/mcp_tool` 用，属"运行时对象构造"而非静态配置
18. **（低）`Config` 非 frozen**（189-207）导致"配置不可变"契约名不副实（3 个字段运行时可变，见总表）

**可测性**：

- 可独立单测（纯函数、无 I/O 或可注入路径）：
  - `_coerce`（296-303）：None/空串/非法/正常值矩阵
  - `_parse_token_amount`（385-405）：数字/bool/`"10k"`/`"1.5K"`/负数/非法
  - `_parse_pricing`（408-423）：中文键映射、非 dict 值跳过、缺键补 0
  - `_parse_model_config`（439-453）：非 dict、空列表、当前不在列表、元素非 str
  - `_parse_project_skill_roots`（306-315）：缺失/空列表/混合项
  - `parse_mcp_server`（318-382）：别名、command 数组归一、字符串布尔、超时非法值
  - `_strip_subagent_hidden`（686-688）：配对/单边标记/多区块
  - `_build_system_prompt`（691-701）：显式传参（不依赖平台）
  - `_build_ui_config`/`_build_ai_config`/`_build_pricing_config`/`_build_balance_config`/`_build_cost_log_config`（491、456、625、635、647）：全部只需构造 dict 入参 + `data_dir` 字符串
- 需要集成测试（文件系统行为）：
  - `load_config`（704-872）：需临时目录验证首次生成文件、目录创建、narnat.md 读取、单位换算合并结果
  - `_find_project_root`（261-293）：需构造含/不含 `.narnat` 的临时目录树、设置 `NARNAT_HOME`（可通过 `monkeypatch.setenv` + `os.chdir` 覆盖 3 个真实分支）
  - `_load_json`/`_load_user_md`（426、666）：需真实文件（含损坏 JSON、BOM）
- 无法自动化/难自动化：
  - `_is_nuitka_onefile`、`_find_narnat_exe_dir`（210-258）：依赖 Nuitka/PyInstaller 运行态（`__compiled__`、`onefile_` 临时目录、`GetModuleFileNameW`），常规单测只能用 `sys.frozen/sys.argv` 打桩近似；exe 实机行为「未验证」
  - 首次生成的 `narnat.json` 与真实用户配置的"漂移"（键增删）只能靠集成测试对比快照

---

### narnat_agent/config/session_store.py（363 行）

**职责**：会话（messages 列表 + 元数据）的 JSON 持久化：原子保存、加载、扁平列举、树形列举、删除、以及三种面向终端展示的格式化函数。

**对外接口**：

- `def save_session(narnat_dir: str, name: str, messages: List[Dict[str, Any]], parent: Optional[str] = None, status: str = "active", summary: Optional[str] = None, parent_msg_count: Optional[int] = None, last_summarized_at: Optional[int] = None) -> str`（50-56）：保存会话，成功返回 `""`，失败返回 `"保存失败: {e}"`
- `def load_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> tuple`（85）：返回 `(messages, error)`；不存在 → `([], "会话不存在: {name}")`
- `def list_sessions(narnat_dir: str) -> List[Dict[str, Any]]`（97）：仅列**顶层**会话（不递归子目录），元素 `{name, timestamp, message_count}`，按 timestamp 倒序
- `def delete_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> str`（118）：`name == "--all"` 删除整个 sessions 目录下的条目（保留目录本身）；否则删单个文件（父会话连同同名子目录一起删）
- `def list_sessions_tree(narnat_dir: str) -> List[Dict[str, Any]]`（161）：递归扫描 `sessions/`，按 JSON 内 `parent` 字段组装两层树
- `def format_session_tree(tree: List[Dict[str, Any]], active_name: Optional[str] = None, active_parent: Optional[str] = None) -> str`（216-218）：`/ls --all` 全量树渲染
- `def format_session_summary(tree: List[Dict[str, Any]], active_name: Optional[str] = None, active_parent: Optional[str] = None) -> str`（251-253）：默认 `/ls` 精简渲染（今天全列 + 更早最多 3 棵）
- `def load_session_meta(narnat_dir: str, name: str, parent: Optional[str] = None) -> dict`（344）：返回会话 JSON 中**除 messages 外**的全部键
- `def format_session_list(sessions: List[Dict[str, Any]]) -> str`（356）：简单平铺渲染；**全仓无调用方（死代码）**

私有：`_strip_surrogates`（12）、`_safe_filename`（24）、`_sessions_dir`（34）、`_session_path`（40）。

**依赖**：

- `from .defaults import DATA_SUBDIR, SESSIONS_SUBDIR`（21）
- 标准库：`json, os, shutil, time, typing`（5-9）
- 注意 import 位置异常：`_strip_surrogates`（12-19）定义在 import 语句（21）之前

**被依赖**（grep `config.session_store` 复验）：

- `narnat_agent/core/session_callbacks.py:16-20`：`save_session, load_session, delete_session, list_sessions_tree, format_session_tree, format_session_summary, load_session_meta`（调用点：120、128、131、133、153、168、179、223、236、242、245、247、266、281、295、313、316、398、425、434、437、439、458、505、514、519、521、533、648、664、666、671、850、877）
- `narnat_agent/core/auto_save_manager.py:61`（函数内局部 import：`from ..config.session_store import save_session`），调用 62
- `narnat_agent/core/summarizer.py:36`（函数内局部 import：`from ..config.session_store import list_sessions`），调用 37
- `narnat_agent/ui/session_commands.py` **不**直接 import session_store，经 `self._mgr`（SessionManager，core/session_callbacks.py）间接触达（如 `ui/session_commands.py:114` 调 `on_list_skill_tree`）
- `format_session_list`、`list_sessions`（除 summarizer 外）无其它调用方；`format_session_list` 为死代码

**状态**：

- 模块级全局：无（只有 import 名与函数定义）
- 类变量：无
- 实例状态：无（全函数式）；唯一"状态"是文件系统布局：
  - 顶层会话文件：`<narnat_dir>/data/sessions/<safe_name>.json`
  - 子会话文件：`<narnat_dir>/data/sessions/<safe_parent>/<safe_name>.json`
  - 保存临时文件：`<path>.tmp`（写入后 `os.replace` 覆盖目标）
  - 会话 JSON 结构：`{name, timestamp, messages, parent, status, summary, parent_msg_count, last_summarized_at}`（65-74）

**行为要点**：

1. `_safe_filename`（24-31）：依次把 `/ \ : < > | ? *` 与 `..` 替换为 `_`；结果为空 → `"unnamed"`。注意替换顺序：`"a/../b"` 先变 `a_.._b` 再变 `a___b`（24-28）
2. `_sessions_dir`（34-37）：`<narnat_dir>/data/sessions`，**每次调用都 `os.makedirs(exist_ok=True)`**——即读操作也有建目录副作用
3. `_session_path`（40-47）：有 `parent` 时为子会话路径并**同时创建父目录**（44-45）；无 parent 时直接顶层路径（47）
4. `save_session`（50-82）：先读旧文件取 `existing`（仅用于 `parent_msg_count`/`last_summarized_at` 的继承，57-64、72-73）；写入结构见"状态"节；`timestamp=time.time()` 每次保存刷新（67）；`_strip_surrogates` 清洗后 `json.dump(..., ensure_ascii=False, indent=2)` 写 `.tmp` 再 `os.replace`（75-79，原子替换）
5. `save_session` 对 `parent_msg_count`/`last_summarized_at` 的语义：显式传值优先，`None` 时继承旧值（72-73）——调用方无法通过传 `None` 清除这两个字段
6. `load_session`（85-94）：返回 `data.get("messages", [])`；文件不存在与解析失败分别返回不同 error 串（88、93-94）
7. `list_sessions`（97-115）：`os.listdir` 仅顶层，`.json` 结尾过滤（101）；损坏文件跳过（112-113）；`name` 优先取文件内字段，否则文件名去掉 `.json`（108）；按 `timestamp` 倒序（114）
8. `delete_session`（118-158）三种模式：
   - `"--all"`：遍历 sessions 目录，子目录 `shutil.rmtree`、`.json` 文件 `os.remove`，异常静默（119-133）
   - 指定 `parent`：删子会话文件，若父目录随后为空则 `os.rmdir`（134-146）
   - 无 `parent`：删顶层文件，并**递归删除同名子目录**（147-158，即"删父连带删全部子会话"）
   - 文件不存在时返回 `"会话不存在: {name}"`（137、149）
9. `list_sessions_tree`（161-213）：`os.walk` 递归（165）；每个 JSON 提取 `{name, timestamp, message_count, parent, status, summary}`（173-180）；**按 `parent` 字段而非目录结构**分组（183-191）；父不存在的孤儿子会话会**合成占位根**（timestamp=0、message_count=0、children=[child]，203-209）
10. 树排序：子节点按 timestamp **升序**（211），根节点按 timestamp **倒序**（212）
11. `list_sessions_tree` 的真实根节点 children 元素含 `status/summary`（195-201），占位根的 children 元素是完整 info（含 `parent` 键，207）——两种子节点结构不一致
12. `format_session_tree`（216-248）渲染规则：
    - 空树 → `""`（219-220）
    - 根行：`  ├── name  (MM-DD HH:MM, N条)  ✘ 退出后删除  ◀ 当前`（224-229）；末根用 `└──`（223-224）
    - 子行前缀随父是否末根取 `"      "` 或 `"│     "`（230）
    - 子状态文案：`completed` → `✓ 已完成 (时间)`；`new` → `(时间, N条)`；其他 → `⚠ 待完成 (时间, N条)`（236-241）
    - `_delete_marked`（树节点上的注入键）→ 追加 `"  ✘ 退出后删除"`（227、242-243）
    - 无 active 会话时末尾追加 `"   ◉  ◀ 当前"`（246-247）
    - **`_delete_marked` 键由调用方注入**：`core/session_callbacks.py:655、658` 设置，本模块只读（227、242、270、278）
13. `format_session_summary`（251-341）渲染规则：
    - 今天分界：`time.mktime((年,月,日,0,0,0,0,0,-1))`（261-262）
    - 分组标题 `"  今天 (N)"`（321）、`"  更早 (N)"`（327）；today 时间格式 `%H:%M`，earlier 用 `%m-%d`（323、335）
    - earlier 默认只显示前 3 棵（328）；若当前会话（含子会话 active）不在其中则追加显示（329-333）；剩余提示 `"  … 还有 N 条更早的会话 (/ls --all 查看)"`（336-338）
    - 无 active 会话时末尾追加 `"   ◉  ◀ 当前"`（339-340）
14. `load_session_meta`（344-353）：返回除 `messages` 的所有键（351）；文件缺失/损坏 → `{}`（347、352-353）
15. `format_session_list`（356-363）：`  name  (YYYY-MM-DD HH:MM, N条消息)`；空列表 → `""`；调用方缺失（死代码）
16. `_strip_surrogates`（12-19）：递归处理 str/dict/list，`encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")`——把孤立代理对替换为 U+FFFD，避免 `json.dump` 抛 `UnicodeEncodeError`

**边界/异常行为**：

1. `_safe_filename("")` → `"unnamed"`（29-30）；全为非法字符（如 `"***"`）→ `"___"`（非空，不触发 unnamed）
2. Windows 保留名（`CON`、`NUL`）、尾随点/空格不处理（24-31）→ 极端会话名可能创建失败，错误以 `"保存失败: {e}"` 返回（81-82）
3. 会话名含 `..` → 变 `_`，防止目录穿越（28）；但 `_safe_filename` 不做长度截断（超长名依赖 OS 报错）
4. `save_session` 捕获范围 `(OSError, UnicodeEncodeError)`（81）：`messages` 含不可 JSON 序列化对象（如自定义类）→ `TypeError` **逃逸**给调用方（78）
5. `save_session` 旧文件损坏时静默以 `existing={}` 继续（63-64）→ 覆盖旧文件成功，元数据丢失
6. `.tmp` 残留：进程在 `os.replace` 前崩溃会留下 `<name>.json.tmp`（76-79）；`list_sessions`/`list_sessions_tree` 只认 `.json` 后缀，`.json.tmp` 不会被列举（101、167），也不会被 `delete_session("--all")` 清理（128-130 仅 `.json`/目录）
7. `delete_session("--all")` 不清理非 `.json` 的文件与 `.tmp`（121-132）；删除失败静默（126、131）
8. `delete_session` 顶层删除会 `shutil.rmtree` 同名子目录（151-157），失败静默；而删子会话只尝试 `rmdir` **空**父目录（141-145）——不对称
9. `list_sessions_tree`：同名根（`name` 相同）互相覆盖（186）；孙层（三级）不建树，只按"一层 parent"关系挂载（183-209）；`parent` 指向不存在会话的占位根不可 `cd`（下游行为，见 R2 范围）
10. 子节点排序按 timestamp 升序（211）而 `status=="completed"` 的子会话 `timestamp` 是最后一次保存时间——排序语义与"创建顺序"可能不同
11. 时间显示全部走 `time.localtime`（225、235、293、305），无时区/夏令时处理
12. `format_session_summary` 的"当前会话追加显示"用 `current not in shown`（332）——**dict 等值比较**，内容相同的不同会话可能被误判为已显示
13. `format_session_tree`/`format_session_summary` 对 `active_name`/`active_parent` 的判定：根当前 = `name` 匹配且 `active_parent is None`（226、269）；子当前 = `child.name + root.name == active_parent`（244、277）
14. 树的 `roots` 只有在文件内 `parent` 为空时才算根；若文件内 `parent` 字段有值但实际文件在顶层目录，会被当子会话（183-184）——目录结构与元数据不一致时显示混乱
15. `load_session` 不校验 `messages` 类型（92）：文件里 `"messages": "abc"` 会原样返回字符串

**补丁痕迹**：

1. **（高）`_strip_surrogates` 与 `core/llm.py:28-35` 完全重复**（12-19）；且定义在 import 语句之前（21），属补丁插入未整理
2. **（中）存储模块承担终端展示职责**：`format_session_tree`（216-248）、`format_session_summary`（251-341）内含 emoji（`✘ ◀ ◉ ✓ ⚠`）、缩进空格、中文文案，与文件读写同文件
3. **（中）树构建双数据源**：`list_sessions_tree` 用 JSON `parent` 字段（183-209）而非目录结构决定父子关系，而文件布局（40-47）同样编码了父子关系——两套真相
4. **（中）两种子节点结构**：真实根 children（195-201）与占位根 children（207）字段不一致，渲染函数对两者用同一套读取代码（235-245）
5. **（中）`list_sessions` 与 `list_sessions_tree` 重复实现**（97-115 vs 161-213）：都是"遍历 → 读 JSON → 提取元数据 → 排序"
6. **（中）`_session_path` 的建目录副作用**（44-45、36）使 `load_session`/`load_session_meta` 也会创建目录（86、345 间接调用）
7. **（中）删除逻辑不对称**：顶层删除连带 `rmtree` 子目录（151-157），子删除只清空目录（141-145）；`"--all"` 魔法字符串（119）无常量
8. **（中）`_safe_filename` 脆弱边界**：未处理 Windows 保留名/尾随点空格（24-31）；`".."` 替换顺序依赖前序替换（26-28）
9. **（中）`save_session` 异常捕获不全**（81）：`TypeError`（不可序列化对象）会逃逸，与"返回错误串"的接口契约不一致
10. **（低）`format_session_list` 死代码**（356-363），全仓无调用方
11. **（低）读取失败静默**：`existing` 读取（63-64）、`list_sessions`（112-113）、`list_sessions_tree`（181-182）、`load_session_meta`（352-353）都吞异常
12. **（低）魔数 3**（更早会话最多显示 3 棵，328）与文案硬编码
13. **（低）`next(iter(...))`/dict 比较等实现细节**（265、332）对结构稳定性有隐含要求

**可测性**：

- 可独立单测：
  - `_safe_filename`（24-31）：全字符矩阵、空、`".."`、Windows 保留名（仅断言字符串结果，不落盘）
  - `_strip_surrogates`（12-19）：孤立代理对、嵌套 dict/list
  - `format_session_tree` / `format_session_summary` / `format_session_list`（216、251、356）：纯函数，喂手工构造的 tree 断言精确文本（含 `_delete_marked`）
- 需要集成测试（临时目录）：
  - `save_session`/`load_session`/`list_sessions`/`list_sessions_tree`/`delete_session`/`load_session_meta`（50-213、344）：验证路径布局（父/子）、原子写、元数据继承、删除连带行为、损坏 JSON 容错
- 无法自动化：
  - `os.replace` 原子性、跨进程并发写同一会话（无锁）属平台行为，常规测试不做；并发覆盖行为「未验证」

---

### narnat_agent/config/defaults.py（328 行）

**职责**：全项目默认值与 prompt 模板的唯一常量源，并提供 thinking 参数/回传格式按（协议, 模型前缀）的查表解析。

**对外接口**：

常量（逐条，值逐字来自源码）：

- `DEFAULT_AUTO_SAVE = False`（6）
- `DEFAULT_AUTO_SAVE_TOKENS = 0`（7）
- `DEFAULT_GIT_SKIP = False`（10）
- `DEFAULT_RM_SKIP = False`（11）
- `DEFAULT_MAX_TOOL_OUTPUT_KB = 64`（14）
- `DEFAULT_MAX_TIMEOUT_SECONDS = 1800`（17）
- `DEFAULT_MCP_STARTUP_TIMEOUT = 30`（20）
- `DEFAULT_MCP_TOOL_TIMEOUT = 300`（21）
- `DEFAULT_REQUIRE_PLAN = False`（24）
- `DEFAULT_MIN_TOOLS = 2`（25）
- `DEFAULT_GOAL_MAX_ROUNDS = 100`（28）
- `DEFAULT_CONTEXT_WINDOW = 1000000`（31）
- `DEFAULT_SHOW_RATIO = False`（32）
- `DEFAULT_WARN_RATIO = 50`（33）
- `DEFAULT_COMPRESS_RATIO = 95`（34）
- `DEFAULT_COMPRESS_RETAIN_TOKENS = 16000`（38）
- `BASE_PROMPT_TEMPLATE`（41-47，三引号字符串，5 行表格，占位符 `{model}`/`{cwd}`/`{platform}`）
- `COMPRESS_PROMPT`（52-85，压缩引擎结构化检查点模板，8 个小节 + 5 条规则）
- `DEFAULT_SKILL_SCAN_DEPTH = 4`（93）
- `NARNAT_DIR = ".narnat"`（96）
- `CONFIG_SUBDIR = "config"`（99）
- `DATA_SUBDIR = "data"`（100）
- `LOGS_SUBDIR = "logs"`（101）
- `SESSIONS_SUBDIR = "sessions"`（102）
- `DEFAULT_IGNORE_DIRS = [".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".idea", ".vscode", ".tox", ".nox"]`（107，共 15 项）
- `NARNAT_JSON = "narnat.json"`（110）
- `NARNAT_MD = "narnat.md"`（111）
- `DEFAULT_API_KEY = ""`（114）
- `DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"`（115）
- `DEFAULT_MODEL = "deepseek-v4-flash"`（116）
- `DEFAULT_PROTOCOL = "anthropic"`（117）
- `DEFAULT_THINKING_ENABLED = True`（118）
- `DEFAULT_THINKING_EFFORT = "high"`（119）
- `DEFAULT_THINKING_PASSBACK = True`（120）
- `THINKING_PARAM_MAP`（140-255，见下 3）——含 9 个条目：`("anthropic","deepseek")`（142）、`("openai","deepseek")`（153）、`("openai","glm")`（166）、`("openai","kimi")`（181）、`("openai","mimo")`（194）、`("anthropic","mimo")`（208）、`("openai","qwen")`（218）、`("openai","gpt")`（233）、`("anthropic","claude")`（246）

函数：

- `def resolve_thinking_passback(protocol: str, model: str) -> str`（258）：查表的回传格式；未匹配 → `"none"`
- `def resolve_thinking_params(protocol: str, model: str, thinking_enabled: bool, effort: str)`（275-276）：返回 `(body_top: dict, extra_body: dict)`

**依赖**：无内部模块 import（仅本文件自身；文件内 `resolve_*` 读 `THINKING_PARAM_MAP`，269、287）。

**被依赖**（grep 常量名/函数名复验）：

- `narnat_agent/config/loader.py:13-32`（大批常量，见 loader 节）
- `narnat_agent/config/session_store.py:21`（`DATA_SUBDIR, SESSIONS_SUBDIR`）
- `narnat_agent/config/skill_store.py:22`（`CONFIG_SUBDIR, DEFAULT_SKILL_SCAN_DEPTH`）
- `narnat_agent/core/compressor.py:7`（`COMPRESS_PROMPT`），使用点 87
- `narnat_agent/core/context.py:7-9`（`DEFAULT_CONTEXT_WINDOW, DEFAULT_WARN_RATIO, DEFAULT_COMPRESS_RATIO`），使用点 27-29
- `narnat_agent/core/llm.py:23`（`resolve_thinking_params, resolve_thinking_passback`），使用点 289、318、511、585、1030
- `narnat_agent/output.py:17`（`DEFAULT_CONTEXT_WINDOW`），使用点 493、510
- `BASE_PROMPT_TEMPLATE`、`DEFAULT_IGNORE_DIRS`、`DEFAULT_GOAL_MAX_ROUNDS`、`DEFAULT_MAX_TOOL_OUTPUT_KB`、`DEFAULT_MAX_TIMEOUT_SECONDS`、`DEFAULT_MCP_*`、`DEFAULT_AUTO_SAVE*`、`DEFAULT_SHOW_RATIO`、`DEFAULT_COMPRESS_RETAIN_TOKENS`、`NARNAT_JSON/MD`、`LOGS_SUBDIR` 等**仅由 loader.py 消费**（除 loader 外无引用）
- `DEFAULT_CONTEXT_WINDOW` 的第三处消费点：loader.py:742（写入首次生成的 narnat.json）
- tool_exp 脚本：`tool_exp/verify_goal_mode.py:22`（`DEFAULT_GOAL_MAX_ROUNDS`）

**状态**：

- 模块级全局（全部只读语义，但类型可变的两个）：
  - `DEFAULT_IGNORE_DIRS`（107，**list**，可变容器；无写入方，只被 loader.py:775 读取写入默认 JSON）
  - `THINKING_PARAM_MAP`（140-255，**dict 嵌套 dict**；无写入方，只被本文件 269、287 读取）
  - 其余常量为 `str/int/bool`，天然不可变
- 类变量：无
- 实例状态：无

**行为要点**：

1. `resolve_thinking_passback`（258-272）：`model_lower = model.lower()`；**按 `THINKING_PARAM_MAP` 的插入顺序**遍历，第一个 `proto == protocol and model_lower.startswith(prefix)` 命中者返回 `mapping.get("passback", "none")`；未命中 → `"none"`（266-272）
2. `resolve_thinking_params`（275-328）流程：同样按插入顺序前缀匹配找 `matched`（285-290）；未命中 → `({}, {})`（292-293）；`thinking_enabled=True` → 用 `enable` 段，`False` → 用 `disable` 段，**无 `disable` 段则返回 `({}, {})`**（299-304）；按 `location` 把参数并入 `body_top`（location=="body_top"）或 `extra_body`（其余，含 `"body"`/`"extra_body"`）（306-308）
3. effort 注入只在此处生效且**仅当 thinking_enabled**（311-326）：`effort_path=(location, *keys)` 决定写入位置（314-316）；`effort_map` 存在时 `effort_map.get(effort, effort)` 做语义→实际值映射，否则原样写入（319-320）；`keys` 长度 1 → 直接赋值；长度 2 → `setdefault` 后写二级键（322-326）。**长度 3+ 或 0 静默不写入**
4. 各厂商映射明细（写规格用，逐条核对 140-255）：
   - `("anthropic","deepseek")`：enable `body: {"thinking": {"type": "enabled"}}` + `body_top: {}` 占位（142-147）；`effort_path=("body_top","output_config","effort")`（147）；`passback="thinking_block"`（149）
   - `("openai","deepseek")`：enable `extra_body: {"thinking":{"type":"enabled"}}` + `body_top: {}`（153-157）；`effort_path=("body_top","reasoning_effort")`（158）；无 disable；`passback="reasoning_content"`（160）
   - `("openai","glm")`：enable `extra_body: {"thinking":{"type":"enabled","clear_thinking":False}}`（166-170）；disable `extra_body: {"thinking":{"type":"disabled"}}`（171-173）；`effort_path=("body_top","reasoning_effort")`（174）；`passback="reasoning_content"`（177）
   - `("openai","kimi")`：enable/disable 同上样式（181-187）；`effort_path=None`（188）；`passback="reasoning_content"`（190）
   - `("openai","mimo")`：enable/disable（194-200）；`effort_path=None`（201）；`passback="reasoning_content"`（204）
   - `("anthropic","mimo")`：enable `body`（208-211）；`effort_path=None`（212）；`passback="thinking_block"`（214）；**无 disable**
   - `("openai","qwen")`：enable `extra_body: {"enable_thinking": True}`（218-220）；disable `{"enable_thinking": False}`（221-223）；`effort_path=("extra_body","thinking_budget")`（225）；`effort_map={"max":32000,"xhigh":24000,"high":16000,"medium":8000,"low":4000,"minimal":1000,"none":0}`（226-227）；`passback="none"`（229）
   - `("openai","gpt")`：enable `body_top: {}`（233-235）；disable `body_top: {"reasoning_effort": "none"}`（237-239）；`effort_path=("body_top","reasoning_effort")`（240）；`passback="none"`（242）
   - `("anthropic","claude")`：enable `body: {"thinking":{"type":"adaptive"}}`（246-249）；`effort_path=("body_top","effort")`（250）；`passback="thinking_block_signed"`（253）；无 disable
5. `BASE_PROMPT_TEMPLATE` 实际内容（41-47，逐字）：
   - `| 你的身份 | 你是一位严谨、克制且极具专业素养的 {model} 智能体。 |`
   - `| 所处环境 | narnat agent 框架内，作为自主代理运行。 |`
   - `| 当前工作目录 | {cwd} |`
   - `| 所处平台 | {platform} |`
   - `| 核心任务 | 尽你所能帮助用户，为用户解难。 |`
   - 无 `{shell}` 占位符（loader.py:800-801 注释已自认）
6. `COMPRESS_PROMPT`（52-85）结构：角色声明 + "严格按下面 Markdown 结构输出" + 8 个小节（原始请求与意图 / 关键技术概念 / 文件与代码 / 错误与修复 / 待办任务 / 当前工作 / 下一步 / 关键上下文）+ 5 条规则（中文工程语言、忠实记录用户反馈、不提摘要请求、只输出检查点、遇旧检查点须"合并而非照抄"）
7. 常量注释里记录外部事实：`DEFAULT_THINKING_PASSBACK` 注释"DeepSeek思考模式契约"（120）；`THINKING_PARAM_MAP` 头部注释逐条说明 passback 四种取值含义（130-134）与三种 location 语义（136-139）；glm 注释记录外部 issue「zai-org/GLM-5#92 回传无效」（175-176）；mimo 注释记录"深度思考+历史含工具调用时…否则 400"（202-203）

**边界/异常行为**：

1. `model=""` → `startswith(prefix)` 全不命中 → `"none"` / `({}, {})`（268-272、285-293）
2. 模型名前缀匹配**顺序敏感**（dict 插入序，269、287）：如将来出现 `("openai","deepseek-v4")` 与 `("openai","deepseek")` 并存，则先插入者优先；同协议下前缀互相包含时行为取决于条目顺序（当前不存在该情况）
3. `protocol` 取值非 `"openai"/"anthropic"`（如 `"azure"`）→ 全不命中 → 回退 `"none"`/空参数（无异常）
4. `resolve_thinking_params` 在 `thinking_enabled=False` 且该 provider 无 `disable` 段时 **不传任何 thinking 参数**（303-304）——即"无法显式关闭"，由服务端默认决定
5. `effort` 为空串/None → 不写 effort（313 `if effort_path and effort`）；未命中 `effort_map` 的语义值被**原样透传**给 provider（320 `effort_map.get(effort, effort)`）
6. `effort_path` 为 2 元组以上（`len(keys)>=3`）时静默丢弃（322-326 无 else 分支）
7. `("anthropic","deepseek")` 的 `enable.body_top: {}` 与 `("openai","deepseek")` 的 `body_top: {}` 是空占位（145、156）——若未来有人删掉 `effort_path` 定义，此处不产生任何参数（静默）
8. `THINKING_PARAM_MAP` 条目缺 `"passback"` 键时 `resolve_thinking_passback` 返回 `"none"`（271）
9. 常量被 `loader.py` 用于默认 JSON 生成时，`DEFAULT_IGNORE_DIRS` 是**共享 list 对象**：`json.dump` 只序列化不修改，但若未来代码 `append` 该 list 会污染全局默认（107 与 loader.py:775）

**补丁痕迹**：

1. **（中）`THINKING_PARAM_MAP` 一处总表承担 5 类厂商差异**（140-255）：`enable/disable/effort_path/effort_map/passback` 五元结构交织，`body` 与 `extra_body` 的区分靠单一字符串 `location` 与 `location=="body_top"` 的隐式判定（307、316）
2. **（中）前缀匹配隐含顺序依赖**（269、287）无注释警示；`startswith` 无法表达精确模型 ID 匹配
3. **（低）`DEFAULT_THINKING_EFFORT="high"` 与 `thinking_options` 默认 `{"high": "高", "max": "全开"}`（loader.py:52、468）分散在两个文件**，无关联约束
4. **（低）`DEFAULT_IGNORE_DIRS` 为可变 list、`THINKING_PARAM_MAP` 为可变 dict**（107、140）：常量命名但无 `tuple`/冻结保护
5. **（低）`DEFAULT_MAX_TIMEOUT_SECONDS = 1800`（17）与 `tools/tool_context.py:41` 的 `max_timeout_seconds: int = 1800` 重复默认值**，两处独立演化
6. **（低）`DEFAULT_COMPRESS_RATIO=95`（34）与 `core/context.py:76` 的 `self._ratio = max(0.0, self._compress_ratio - 10)`** 构成跨文件隐式耦合（"压缩后占比按阈值减 10 估算"），10 这个偏移量不在本文件
7. **（低）`DEFAULT_AUTO_SAVE_TOKENS = 0` 语义为"0=无门槛"**（7）——0 同时是"关闭"与"最敏感"两种直觉，注释解释成本高
8. **（低）`DEFAULT_SKILL_SCAN_DEPTH = 4`（93）与 `skill_store._MAX_DIR_DEPTH = 8`（skill_store.py:26）并存**，两个深度概念无交叉说明
9. **（低）`resolve_thinking_params` 无返回类型注解**（275-276），与同文件另一函数 `-> str`（258）风格不一致
10. **（低）注释密集记录外部 issue 编号/厂商行为**（131-135、175-176、202-203、213）——属"历史包袱注释"，但同时也是关键领域知识，重构须保留为文档

**可测性**：

- 可独立单测（纯函数/纯常量，本文件几乎全部可测）：
  - `resolve_thinking_passback`（258）：9 个条目 × 命中/未命中/大小写
  - `resolve_thinking_params`（275）：enable/disable 分支、effort_map 命中/未命中、`effort_path` 长度 1/2/3、无 disable 返回空
  - 常量快照断言（防重构漂移）
- 需要集成测试：`COMPRESS_PROMPT` 的实际压缩效果属 R1/R2 范围，本组只做文本快照
- 无法自动化：无

---

### narnat_agent/config/skill_store.py（318 行）

**职责**：技能（Markdown 文件）加载与技能树枚举：系统技能（`<narnat_dir>/config/skills/`）+ 项目技能（工作目录下自动发现的名为 `skills` 的目录），含路径穿越防护与编码回退。

**对外接口**：

- `def load_skill(narnat_dir: str, name: str, project_roots=None, cwd: str = "", ignore_dirs: tuple = (), scan_depth: int = DEFAULT_SKILL_SCAN_DEPTH) -> tuple`（29-33）：返回 `(content, error, path)`；`path` 为实际读取文件的绝对路径（未命中为 `""`）
- `def list_skill_tree(narnat_dir: str, project_roots=None, cwd: str = "", ignore_dirs: tuple = (), scan_depth: int = DEFAULT_SKILL_SCAN_DEPTH) -> List[dict]`（68-72）：技能树节点列表（供 `/skill` 层级补全）
- 私有：`_project_roots`（111）、`_discover_skill_roots`（129）、`_load_system_skill`（165）、`_load_project_skill`（182）、`_resolve_project_dir`（200）、`_scan_dir`（227）、`_is_single`（263）、`_merge_nodes`（268）、`_nodes_to_list`（278）、`_unsafe_name`（292）、`_within`（300）、`_read`（305）

节点结构（docstring 77-82 + 代码）：`{"name": str, "type": "file", "origin": "system"|"project"}`；目录节点 `{"name","type":"dir","children":[...],"single":bool,"origin":"project"}`。

**依赖**：

- `from .defaults import CONFIG_SUBDIR, DEFAULT_SKILL_SCAN_DEPTH`（22）
- 标准库：`os, re, typing.List`（18-20）
- 模块级常量：`_MAX_DIR_DEPTH = 8`（26）

**被依赖**：

- `narnat_agent/core/session_callbacks.py:21` `from ..config.skill_store import load_skill, list_skill_tree`；调用点：`load_skill` → 748（`/skill` 加载，参数 `project_roots=self._project_skill_roots, ignore_dirs=self._skill_ignore_dirs`）、`list_skill_tree` → 888-890（`on_list_skill_tree`）
- 间接：`narnat_agent/ui/session_commands.py:114` 调 `self._mgr.on_list_skill_tree()`（Tab 补全），`ui` 不直接 import 本模块
- 无其它 import 方（grep `skill_store` 全仓仅上述 + 本文件）

**状态**：

- 模块级全局：`_MAX_DIR_DEPTH = 8`（26，只读，唯一读者 233）
- 类变量：无
- 实例状态：无（全函数式）；调用的持久状态仅"文件系统上的技能目录"

**行为要点**：

1. `load_skill` 前置校验（47-51）：`name` 空白 → `("", "技能不存在: ", "")`；`name = name.strip().replace("\\","/").rstrip("/")`（49）；`_unsafe_name(name)` 为真 → `("", f"技能不存在: {name}", "")`
2. `_unsafe_name`（292-297）：`\\`→`/` 归一后，空串、以 `/` 开头、匹配 `^[A-Za-z]:`（盘符）→ 不安全；任何 `/` 分割出的组件属于 `("", ".", "..")` → 不安全
3. 查找顺序（43-46 docstring、53-65 代码）：
   - ① 系统技能：**仅当 `"/" not in name`**（54）→ `_load_system_skill`；若返回非空内容或非空错误则直接返回（55-57）
   - ② 项目技能：逐个 `_project_roots(...)` 根调 `_load_project_skill`，返回非 None 即返回（61-64）
   - ③ 都不中 → `("", f"技能不存在: {name}", "")`（65）
4. `_project_roots` 三态语义（111-126）：`project_roots is not None` → 按显式列表解析（相对路径拼 `cwd`，绝对路径原样；**只保留 `os.path.isdir` 存在的**，119-125）；`None` → `_discover_skill_roots`（126）
5. `_discover_skill_roots`（129-162）：从 `cwd` 起 DFS，深度从 1 开始，`depth > max_depth` 停止（142-144）；`os.listdir` **已排序**（146）；跳过 `ignore_dirs` 精确名（150）；命中名为 `skills`（**小写比较 `entry.lower()`**，155）的目录则加入 roots（除非 realpath 等于系统技能目录，156），且**不再进入其内部**（158）；其它目录递归（159）
6. `_load_system_skill`（165-179）：`<skills_dir>/<name>.md` 存在 → `_read(realpath)`（168-170）；否则 `<skills_dir>/<name>/` 目录 → **遍历 `os.listdir` 取第一个 `.md`**（171-178）；都没有 → `("", "", "")`
7. `_load_project_skill`（182-197）：候选依次 `name`、`name + ".md"`（若不以 `.md` 结尾，186-188）；要求 `isfile` 且 `p.lower().endswith(".md")` 且 `_within(base_real, p)`（191）→ `_read(p)`；否则视 `name` 为目录，`isdir` 且 `_within` → `_resolve_project_dir`（194-196）；都没有 → `None`（197，表示"该根下不存在，继续下一个根"）
8. `_resolve_project_dir`（200-224）：`listdir` 排序（203）；直接子文件中的 `.md` 列表（206-207）；无 `.md` 但有子目录 → 提示前 5 个子目录 `name/sub`（209-214）；无 `.md` 无子目录 → `技能 '{name}' 目录下没有技能文件`（215）；优先 `skill.md`（小写比较，216-218）；恰好 1 个 `.md` → 读取（219-220）；多个 → 提示前 5 个文件、超 5 个加 `等N个`（221-224）
9. `list_skill_tree` 系统部分（84-99）：`<narnat_dir>/config/skills` 下**排序**遍历；`.md` 文件 → `{"name": entry[:-3], "type":"file", "origin":"system"}`（91，去掉 `.md` 后缀）；目录且直接含 `.md` → `{"name": entry, "type":"file", "origin":"system"}`（94-95，用目录名当可加载技能名）
10. `list_skill_tree` 项目部分（101-106）：所有根先 `_scan_dir` 得节点 dict，再 `_merge_nodes` 合并（103-104），`_nodes_to_list` 展平（105）
11. `_scan_dir`（227-260）：`visited` realpath 集合防环（235-240）；`depth > _MAX_DIR_DEPTH(8)` 停止（233-234）；排序遍历（243）；含 `.md` 的子目录才建节点（249-257）；`.md` 文件节点用 `setdefault` 防同名覆盖（258-259）
12. `_is_single`（263-265）：`len(children)==1` 且唯一子项是 file → 目录视作"单技能"，补全显示裸名（docstring 81-82）
13. `_merge_nodes`（268-275）：同名目录递归合并子项、**先出现者保留**（270-275），合并后重算 `single`（275）
14. `_read`（305-318）：二进制读取；编码回退顺序 `utf-8-sig` → `gbk`（313）；成功后 `strip()`；全部失败 → `("", "读取失败: 无法识别的文件编码", "")`；`OSError` → `("", f"读取失败: {e}", "")`（310-311）
15. `_within`（300-302）：`path_real == base_real or path_real.startswith(base_real + os.sep)`（realpath 已解析，防 `..`/符号链接穿越）

**边界/异常行为**：

1. `name` 为 `"dir/file.md"` 时跳过系统技能查找（54），只查项目技能；系统技能**不支持**层级路径
2. `name` 带尾斜杠 `"xxx/"` → `rstrip("/")` 归一为 `"xxx"`（49）；`name="a//b"` → 组件含空串 → `_unsafe_name` 判不安全（297）
3. 系统技能目录下同名 `.md` 与目录并存时**文件优先**（169-176）
4. 系统技能目录内 `.md` 的选取依赖 `os.listdir` **未排序**（174-176）→ 多 `.md` 时读取结果不确定（与 `list_skill_tree` 的排序行为不一致）
5. 系统技能文件内容为空白（`strip()` 后为空）→ `content=""`、`err=""` → `load_skill` 的 `if content or err` 判假（56），**继续查找项目技能**（空技能被静默跳过）
6. 项目技能同名文件优先于目录（185-196）；显式 `.md` 后缀与省略后缀等价（186-188）
7. 非 `.md` 后缀文件即便存在也被拒（191 `p.lower().endswith(".md")`）：`name="a.txt"` 且 `base/a.txt` 存在 → 视为不存在
8. 目录名以 `.md` 结尾时 `_load_project_skill` 第 191 行条件对目录分支不适用（目录走 194 → `_resolve_project_dir`）；但 `load_skill` 传入 `"x.md"` 且 `x.md` 是目录 → `_resolve_project_dir` 正常处理
9. `_resolve_project_dir` 的 `SKILL.md` 判定不区分大小写（216 `e.lower() == "skill.md"`）；多 `.md` 且无 `SKILL.md` → 返回**错误提示**而非部分加载（221-224）
10. `_discover_skill_roots`：`ignore_dirs` 精确匹配（大小写敏感，150）——`Node_Modules` 不会被跳过；`skills` 目录名比较用 `lower()`（155），但 `ignore_dirs` 不 lower
11. `_discover_skill_roots` 不影响 `_scan_dir`：技能的**内部**目录不受 `ignore_dirs` 约束（_scan_dir 无 ignore 参数，227-260）
12. 自动发现深度 > `scan_depth`（默认 4）的 `skills` 目录被忽略（142-144）；显式 `project_roots` 不受该深度与 `ignore_dirs` 约束（119-125）
13. 显式 `project_roots` 中不存在的目录被静默丢弃（123-124）；全部丢弃 → `_project_roots` 返回 `[]` → 无项目技能（等同 `()`）
14. `project_roots=()` → `bases=[]`（119-125）→ 只加载系统技能（与 docstring 40-41 一致）
15. 编码：UTF-8 BOM 被 `utf-8-sig` 吸收（313）；非法 UTF-8 但可被 GBK 解码的字节（几乎总能）→ 返回 GBK 解码结果，**可能静默乱码**而不是报错（313-317）
16. 环（junction/symlink）防护：`_scan_dir` 用 realpath visited（235-240）；`_discover_skill_roots` **无** visited 集合，靠 `max_depth` 兜底（142-144）——junction 指向祖先目录时会在 4 层内重复枚举（不无线循环，但可能重复 roots 与耗时）
17. 深度上限 `_MAX_DIR_DEPTH=8`（26）与 `scan_depth=4` 是两个不同维度的限制（技能树内部 vs 自动发现），`list_skill_tree` 只用 `_MAX_DIR_DEPTH`
18. `_merge_nodes` 同名目录合并时，`single` 只在双方都是 dir 时重算（273-275）；同名 file 与 dir 冲突 → 先到者保留（270-272）
19. `list_skill_tree` 系统目录判断 `any(f.endswith(".md") for f in os.listdir(path))`（94）会把名为 `"x.md"` 的**子目录**也算作可加载技能（95）；`_load_system_skill` 侧则取"第一个 `.md`"，两侧判定不完全一致
20. 路径安全：`_within` 用 realpath 前缀比较（300-302）——在 Windows 上大小写不敏感但本比较**大小写敏感**（`os.sep` 拼接），存在大小写不同导致的误判（低概率，「未验证」实机行为）
21. `name` 为绝对路径（`C:\x` 或 `/x`）→ `_unsafe_name` 拒绝（295）
22. `name` 长度超限/含空字符串 → 由 OS 报错，`_read` 返回 `读取失败: {e}`（310-311）

**补丁痕迹**：

1. **（中）系统技能与项目技能两条加载路径能力不对称**（165-179 vs 182-224）：系统侧不支持省略 `.md`、不支持 `SKILL.md` 优先级、多 `.md` 取第一个（未排序）；项目侧支持。同一"技能"概念两套语义
2. **（中）两套遍历实现**：`_discover_skill_roots`（129-162）与 `_scan_dir`（227-260）都在递归枚举目录树，各自的排序/忽略/防环策略不同
3. **（中）两个深度常量**：`DEFAULT_SKILL_SCAN_DEPTH=4`（defaults.py:93）与 `_MAX_DIR_DEPTH=8`（26），注释各自解释但无总览
4. **（中）`load_skill` 以"空 content + 空 err"表示未找到**（65），且系统技能空文件也会被 `content or err` 判为未找到（56）——用返回值真假表达控制流
5. **（中）显式 `project_roots` 与自动发现的规则不一致**（119-125 vs 129-162）：前者不校验深度/忽略目录、静默丢弃不存在项
6. **（中）`os.listdir` 未排序取第一个 `.md`**（174-176）→ 非确定性加载
7. **（中）`_scan_dir` 无 ignore_dirs、`_discover_skill_roots` 无 visited**（对称缺口）
8. **（低）提示文案中的魔法数 5**（211-213、221-223）与 `…`/`等N个` 文案硬编码
9. **（低）`_unsafe_name` 与 `load_skill` 重复做 `\`→`/` 归一**（49 与 294）
10. **（低）`list_skill_tree` 与 `_load_system_skill` 对系统技能目录的判定不一致**（94-95 vs 171-178）
11. **（低）`_is_single` 的语义描述在 docstring**（81-82），实现只依赖 children 结构（263-265）——渲染语义与数据结构耦合
12. **（低）模块 docstring 描述与实现细节漂移风险**：docstring 声称"取目录下第一个 .md"（7），未提"未排序"

**可测性**：

- 可独立单测（纯函数，无 I/O）：
  - `_unsafe_name`（292-297）：矩阵（空、`/`开头、盘符、`.`/`..`/空组件、正常层级）
  - `_within`（300）：相等/子路径/前缀相似（`base2` vs `base`）/大小写
  - `_is_single`（263）：单 file / 单 dir / 多子项 / 空
  - `_merge_nodes` + `_nodes_to_list`（268、278）：同名合并优先级、single 重算、顺序保持
  - `_resolve_project_dir`（200）：需构造临时目录（可归入集成）
- 需要集成测试（临时目录树）：
  - `load_skill`（29）：系统/项目两路径、层级名、省略后缀、`SKILL.md` 优先、多 `.md` 提示、空文件旁路、编码回退（UTF-8/GBK/二进制）
  - `list_skill_tree`（68）：system+project 合并、`single` 判定、深度截断
  - `_discover_skill_roots`（129）：ignore_dirs、深度、skills 目录不递归、系统目录排除
- 无法自动化：
  - junction/symlink 环（Windows 需管理员或开发者模式创建，环境相关）——「未验证」
  - Windows 路径大小写不敏感对 `_within` 的影响——「未验证」

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

本组内部：

- `narnat_agent/config/loader.py` → `narnat_agent/config/defaults.py` (13)
- `narnat_agent/config/session_store.py` → `narnat_agent/config/defaults.py` (21)
- `narnat_agent/config/skill_store.py` → `narnat_agent/config/defaults.py` (22)
- `narnat_agent/config/defaults.py` → （无内部依赖，叶子）
- 反向：`loader.py`/`session_store.py`/`skill_store.py` 之间**无**互相 import（无环）

外部 → 本组：

- `assembly.py` → `config/loader.py` (11)
- `core/agent_loop.py` → `config/loader.py` (19)
- `core/auto_save_manager.py` → `config/loader.py` (10)、`config/session_store.py` (61, 函数内)
- `core/billing.py` → `config/loader.py` (16, `TYPE_CHECKING`)
- `core/compression_coordinator.py` → `config/loader.py` (11)
- `core/compressor.py` → `config/defaults.py` (7)
- `core/context.py` → `config/defaults.py` (7)
- `core/llm.py` → `config/loader.py` (22)、`config/defaults.py` (23)
- `core/session_callbacks.py` → `config/session_store.py` (16)、`config/skill_store.py` (21)
- `core/summarizer.py` → `config/loader.py` (10)、`config/session_store.py` (36, 函数内)
- `mcp/__init__.py` → `config/loader.py` (16)
- `output.py` → `config/defaults.py` (17)
- `tools/mcp_tool/__init__.py` → `config/loader.py` (82, 函数内)
- 另有 `ui/*`、`tools/*` 通过 `Config` 对象间接消费（不 import config 包）：`assembly.py:43-48、51、54、66-146、158-192` 是唯一装配点

### 模块级可变状态全清单（含读写方）

| 状态 | 类型/可变性 | 声明位置 | 写入方 | 读取方 |
|---|---|---|---|---|
| `defaults.DEFAULT_IGNORE_DIRS` | list（容器可变，无写者） | defaults.py:107 | — | loader.py:775；loader.py:775 生成的 JSON 供后续人工编辑（`.narnat/config/narnat.json:163-180`） |
| `defaults.THINKING_PARAM_MAP` | 嵌套 dict（容器可变，无写者） | defaults.py:140 | — | defaults.py:269、defaults.py:287 |
| `loader._SUBAGENT_HIDE_RE` | `re.Pattern`（只读） | loader.py:680 | — | loader.py:688 |
| `skill_store._MAX_DIR_DEPTH` | int（只读） | skill_store.py:26 | — | skill_store.py:233 |
| `session_store.*` | 无模块级状态 | — | — | — |
| `Config.ai.thinking_effort` | 实例字段（可变，容器为非 frozen dataclass） | loader.py:50 | `assembly.py:121`（setter lambda）；触发点 `core/session_callbacks.py:771-772`（`/thinking`） | `core/llm.py:321、588`；`core/agent_loop.py:47` |
| `Config.ai.thinking_passback` | 实例字段（可变） | loader.py:51 | `assembly.py:124`；触发点 `core/session_callbacks.py:801-802`（`/thinkback`） | `core/llm.py:1029` |
| `Config.ai.model` | 实例字段（可变） | loader.py:44 | `assembly.py:126`（随后被 `assembly.py:166-169` 覆盖为 `_set_model_with_stats`，同时写 `stats._model`）；触发点 `core/session_callbacks.py:832-833`（`/mode`） | `core/llm.py:289、324、591、1030`；`core/stats.py`（经 assembly.py:159 注入） |
| `Config.ui.raw` | dict（frozen dataclass 内的可变容器） | loader.py:182 | `assembly.py:43-47`（补回 show_cost/show_balance/max_output_tokens/show_ratio/context_window） | `output.py` 的 `apply_style`（output.py:487-493 读取这些键） |
| `Config`（整体） | 非 frozen | loader.py:189-207 | 上述字段 | 全局注入（assembly.py） |
| 其余子配置（Path/Tool/Safety/Mcp/Plan/Session/Skill/Pricing/Balance/CostLog/UIConfig 属性） | frozen=True | loader.py:59、69、79、86、106、113、124、132、140、150、164 | 构造期一次性写入 | 见配置项全清单 |

说明：`AIConfig` 与 `Config` 是**唯一**两个非 frozen 配置类（loader.py:36、189，注释 39-41、190-194 自认属过渡状态）。

### 补丁痕迹 TOP10（按严重度排序）

1. （高）`loader.py:815-830` —— `AIConfig` 在 `load_config` 中被整体重建（13 字段逐字复制）只为补 `retry_count`；新增字段需三处同步（36-55、474-488、815-830）
2. （高）`loader.py:731-776` —— 首次生成的 narnat.json 默认值硬编码且与 dataclass 默认不一致：`740 "协议":"anthropic"`、`741 "最大输出token数":128000`、`744-748 "思考"`、`749 "LLM重试次数":3`、`752 "余额查询"."启用":True`（对比 `BalanceConfig.enabled=False`，loader.py:142）
3. （高）`loader.py:426-435` —— `_load_json` 吞掉 JSON 语法错误静默回退全默认；且不校验顶层为 dict，非 dict 时下游 `data.get`（loader.py:458）抛 `AttributeError` 崩溃
4. （高）`session_store.py:12-19` ↔ `core/llm.py:28-35` —— `_strip_surrogates` 两份完全相同的实现；且 session_store 里它定义在 import 语句（21）之前，补丁插入痕迹明显
5. （中）`loader.py:806、828、842-845、849-850、853-854、857、618-622` —— 解析风格双轨：`_coerce` 容错 vs 裸 `int()`/`bool()`（`bool("false")` 为 True、非法字符串导致启动失败）
6. （中）`loader.py:846` —— `tuple(data.get("忽略目录") or [])`：字符串按字符拆解、dict 取键，无类型校验
7. （中）`session_store.py:161-213` —— 树用 JSON `parent` 字段（而非目录结构）建树；真实根 children（195-201）与占位根 children（207）字段不一致
8. （中）`session_store.py:216-248、251-341` —— emoji/缩进/状态文案硬编码在存储模块；搭配 `core/session_callbacks.py:655、658` 注入的 `_delete_marked` 隐式键契约
9. （中）`skill_store.py:165-179` —— 系统技能加载用未排序 `os.listdir` 取第一个 `.md`（非确定性），且与项目技能加载器（182-224）能力不对称
10. （中）`defaults.py:269、287` + `skill_store.py:26/129-162` + `loader.py:286` —— 前缀匹配顺序敏感、三处各不相同的深度/层数硬编码（`scan_depth=4`、`_MAX_DIR_DEPTH=8`、向上 10 层），配置扫描边界分散

其余（完整清单见各文件"补丁痕迹"节）：loader 第 8-18 条、session_store 第 10-13 条、defaults 第 3-10 条、skill_store 第 8-12 条。

### 本组对外契约清单（新架构必须保持的行为面）

1. `load_config(project_root=None, headless=False) -> Config`（loader.py:704）
   - 副作用：创建 `.narnat/config`、`.narnat/data`；首次缺失时生成 `narnat.json`（固定 JSON 结构）与空 `narnat.md`
   - 返回值：`Config` 12 个字段名与子配置字段名（外部直接按属性访问，字段名即契约）
   - `headless=True` 时对 `narnat.md` 应用 subagent:hide 剥离
2. `Config.system_prompt` 内容契约：`BASE_PROMPT_TEMPLATE.format(model,cwd,platform)` 与 `narnat.md` 内容以 `"\n"` 拼接（loader.py:691-701、795-803）
3. `parse_mcp_server(name, entry) -> Optional[McpServerConfig]`（loader.py:318）：中英文键别名、command 数组归一、字符串布尔容错、超时下限回落、字段字符串化
4. 首次生成 `narnat.json` 的键集合（loader.py:731-776）——已发布给用户，向后兼容解析必须继续接受这些中文键
5. `session_store` 文件布局与命名契约：`<narnat_dir>/data/sessions/<name>.json`、子会话 `<parent>/<child>.json`、`_safe_filename` 替换规则、`<path>.tmp` + `os.replace` 原子写
6. `session_store` 8 个函数签名与返回约定（`save_session`→`""`/错误串；`load_session`→`(messages, error)`；`delete_session` 的 `--all`、父子连带规则；`format_*` 的精确文案与缩进）
7. 树节点契约：`{name, timestamp, message_count, children[]}`（根）+ `{name, timestamp, message_count, status, summary}`（子）；`_delete_marked` 由调用方注入（core/session_callbacks.py:655、658）
8. `skill_store.load_skill(...) -> (content, error, path)` 三元组语义（`path` 供模型相对路径基准）；`project_roots` 三态（`None`=自动发现 / `()`=关闭 / 非空元组=显式）；`list_skill_tree` 节点结构与 `single` 语义
9. `defaults.resolve_thinking_params / resolve_thinking_passback` 返回值契约（`core/llm.py:289、318、511、585、1030` 依赖）
10. `defaults` 常量：`COMPRESS_PROMPT`（core/compressor.py:87）、`DEFAULT_CONTEXT_WINDOW`（output.py:493、510）、`DEFAULT_WARN_RATIO`/`DEFAULT_COMPRESS_RATIO`（core/context.py:27-29）必须保持名称与语义
11. `.narnat` 目录名与子目录名（`NARNAT_DIR/CONFIG_SUBDIR/DATA_SUBDIR/LOGS_SUBDIR/SESSIONS_SUBDIR`）——磁盘上已存在的用户数据依赖
12. `.narnat/config/narnat.md` 的 subagent:hide 标记语法（`<!-- subagent:hide --> … <!-- /subagent:hide -->`）

### 配置项全清单（键名/默认值/单位/解析函数/生效位置）

"默认值"含义：JSON 键缺失（或非法且被容错）时的结果值。"生效位置"指运行时消费该值的对象（行号为本报告已核实的位置）。

**`智能体` 分组**（解析：`_build_ai_config` loader.py:456-488；`LLM重试次数` 在 828）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `接口密钥` | `""`（DEFAULT_API_KEY） | str 直取（475） | — | `AIConfig.api_key` → llm.py:271、566（x-api-key/Bearer） |
| `接口地址` | `https://api.deepseek.com/anthropic` | str 直取（460-461） | — | llm.py:272、563 |
| `模型.当前` | `deepseek-v4-flash`（缺省或被列表修正，439-453） | 非 str → 走规则；不在列表则 insert(0) | — | llm.py:289、324、591、1030；stats |
| `模型.列表` | `[DEFAULT_MODEL]`（非 list → `[]`，非 str 元素过滤，446-449） | list of str | — | `/mode` 候选（assembly.py:127 → session_callbacks.py:819-847） |
| `协议` | `anthropic` | str 直取，**无白名单**（460） | — | llm.py:194-197（`== "anthropic"` 走 Anthropic 后端，否则 OpenAI 后端） |
| `温度` | `None`（不传） | `_coerce(float)`（480） | — | llm.py:338-339、604-605（None 时不写 kwargs/body） |
| `最大输出token数` | `None`（不传）；界面组缺省时作 128000 | `_coerce(int)`（481） | — | llm.py:340-341、606-607；loader.py:789 作为 UI 默认 |
| `上下文窗口大小` | `1000000`（缺失/非法）；显式 ≤0 保留 | `_coerce(int)`（471-472） | — | core/context.py:48（占比分母）；assembly.py:47、79；ui_design.py:149 |
| `目标模式最大轮数` | `100`（0/非法→100；负值保留） | `_coerce(int) or DEFAULT`（487） | — | core/agent.py:107、211 |
| `思考.启用` | `True` | `bool(...)`（465，无字符串容错） | — | llm.py:287、319-320、509、586-587、1029 |
| `思考.强度` | `high` | str 直取（466） | — | `resolve_thinking_params(effort=…)`（llm.py:321、588）；`/thinking` 展示（agent_loop.py:47） |
| `思考.回传` | `True`（DEFAULT_THINKING_PASSBACK） | `bool(...)`（467） | — | llm.py:1029 |
| `思考.强度选项` | `{"high":"高","max":"全开"}` | dict 直取（468） | — | `/thinking` 列表（session_callbacks.py:762、785-786） |
| `LLM重试次数` | `3` | 裸 `int()`（828，无容错） | — | llm.py:191 `set_retry_count` |

**`界面` 分组**（解析：`_build_ui_config` loader.py:491-608）

| 键名（中/英） | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `显示费用` / `show_cost` | `False` | `bool()`（554、611-615） | — | `UIConfig.show_cost` → output.py:489（DisplayState） |
| `显示余额` / `show_balance` | `False` | `bool()`（555） | — | output.py:490 |
| `最大输出token数` / `max_output_tokens` | `ai.max_tokens or 128000` | 裸 `int()`（556、618-622） | — | output.py:491；llm.py `max_output_tokens`（assembly.py:68） |
| `颜色`（或旧扁平色键） | `{}`（不合并任何色） | 中→英键映射 + `_OLD_COLOR_MAP`（498-548、574-590） | — | output.py `apply_style`（assembly.py:48） |
| `基础色`/`标注`/`代码块`/`差异`/`框架`/`命令`/`提示符` | 同上 | section 名映射（498-503）+ 段内键映射（504-548） | — | apply_style；配方值内中文色名→英文（592-605） |

**`工具` 分组 / 顶层 `忽略目录` / `安全` 键**（解析：loader.py:806-807、841-851）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `工具.输出上限KB` | `64` | 裸 `int()`（806） | **KB→字符数 ×1024**，≤0 → 0（807） | `ToolConfig.max_output_chars` → assembly.py:108 → `ToolContext.max_tool_output_chars`（tool_context.py:38） |
| `工具.超时上限秒` | `1800` | 裸 `int()`（845） | — | assembly.py:109 → `ToolContext.max_timeout_seconds`（tool_context.py:41）；bash/serial/background 取 `min()`（bash:507-508、serial:348-349、background:691-692） |
| `工具.SSH最大会话数` | `5` | 裸 `int()`（842） | — | assembly.py:51 → `TerminalRuntime.set_max_sessions`（serial/__init__.py:49-51，内部 clamp 1..10） |
| `工具.最大传输文件MB` | `100` | 裸 `int()`（843） | — | assembly.py:107 → `ToolContext.max_transfer_mb`（tool_context.py:35） |
| `工具.git免确认` | `False` | `bool()`（849） | — | assembly.py:105 → `ToolContext.git_skip_confirm` → bash:471 |
| `工具.rm免确认` | `False` | `bool()`（850） | — | assembly.py:106 → `ToolContext.rm_skip_confirm` → bash:469、serial:316 |
| `忽略目录`（顶层） | `()`（缺失/空） | `tuple(... or [])`（846，无类型校验） | — | `ToolConfig.ignore_dirs` → assembly.py:104 → `ToolContext.ignore_dirs` → glob:485-486、grep:195；skill 扫描 assembly.py:136 |

**`计划` 分组**（解析：loader.py:852-855）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `计划优先` | `False` | `bool()`（853） | — | assembly.py:110 → `ToolContext.require_plan` → tool_dispatcher.py:334 |
| `计划最低工具数` | `2` | 裸 `int()`（854） | — | assembly.py:111 → `ToolContext.min_tools` → tool_dispatcher.py:354 |

**`会话` / `压缩` 分组**（解析：loader.py:809-812、856-863）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `会话.自动保存` | `False` | `bool()`（857） | — | auto_save_manager.py:40 |
| `会话.自动保存Token量` | `0`（0=无门槛） | `_parse_token_amount`（858-859、385-405） | 支持 `"10k"`→10000、`"1.5K"`→1500 | auto_save_manager.py:47（阈值比较） |
| `压缩.占比显示` | `False` | `bool()`（860） | — | assembly.py:46（ui.raw）→ output.py:492 → ui 统计栏 |
| `压缩.告警` | `50`（0/非法→50） | `_coerce(int) or DEFAULT`（861） | — | assembly.py:80 → `ContextManager._warn_ratio`（context.py:32、61-63） |
| `压缩.压缩` | `95`（0/非法→95） | `_coerce(int) or DEFAULT`（862） | — | assembly.py:81 → `ContextManager._compress_ratio`（context.py:33、55）；压缩后估算 context.py:76 |
| `压缩.保留尾部` | `16000`（显式 0 合法；负→0） | `_coerce(int)` + `max(0,…)`（810-812） | — | `SessionConfig.retain_tokens` → compression_coordinator.py:68、98、132 → compressor.select_cut_index（compressor.py:33-59） |

**`技能` 分组**（解析：`_parse_project_skill_roots` loader.py:306-315）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `技能.项目技能目录` | `None`（自动发现） | 非 list → None；list → 非空 str 项 tuple（可为 `()`=关闭） | — | `SkillConfig.project_roots` → assembly.py:135 → session_callbacks.py:749、889-890 → skill_store `_project_roots`（119-126） |

**`定价` / `余额查询` / `费用日志` / `接口密钥组`**（解析：loader.py:625-663、785、788）

| 键名 | 默认值 | 类型/解析 | 单位换算 | 生效位置 |
|---|---|---|---|---|
| `定价.模型` | `{}` | `_parse_pricing`（408-423）：中文键→英文（输入/缓存命中/输出 → input/cache_hit/output），值不做数值校验 | — | assembly.py:160 → `StatsTracker` → billing.get_pricing（billing.py:19-23） |
| `余额查询.启用` | `False` | `bool()`（639） | — | assembly.py:161 → Stats/余额显示 |
| `余额查询.查询地址` | `""` | str（640） | — | 同上（httpx 请求） |
| `余额查询.认证方式` | `bearer` | str（641） | — | 同上（`bearer` / `x-api-key`） |
| `余额查询.响应路径` | `""` | str（642） | — | JSONPath 解析余额值 |
| `余额查询.货币路径` | `""` | str（643） | — | JSONPath 解析货币 |
| `费用日志.启用` | `False` | `bool()`（652） | — | assembly.py:162 → stats.py:32-38 |
| `费用日志.输出文件` | `""` → `<data_dir>/cost_log.csv` | str or 默认（653） | — | stats.py:102（`_cost_log_path`，空时回落 `.narnat/data/cost_log.csv`） |
| `费用日志.最大容量MB` | `50`（非数值→50；负→0） | 裸 `int()` + clamp（655-661） | **MB→字节 ×1024×1024**，0=不轮转 | stats.py:33-37、109（双文件轮转） |
| `接口密钥组` | `{}` | dict 直取（785） | — | `Config.api_keys` → assembly.py:103 → `ToolContext.get_api_key`（tool_context.py:82-84）；web_search 用 `websearch`/`websearch_url` |

**路径/文件名常量（非 narnat.json 键）**：`NARNAT_DIR=".narnat"`、`CONFIG_SUBDIR="config"`、`DATA_SUBDIR="data"`、`LOGS_SUBDIR="logs"`、`SESSIONS_SUBDIR="sessions"`、`NARNAT_JSON="narnat.json"`、`NARNAT_MD="narnat.md"`（defaults.py:96-111）；skill 相关固定名 `skills`（系统技能目录，skill_store.py:85、138、167）与 `"skills"` 目录名发现规则（skill_store.py:155）、`SKILL.md`（216）。

---

## 附：核对与未验证事项

- 被依赖清单可用以下命令复验（抽查任一即应一致）：
  - `grep -rn "config.loader" narnat_agent` → 9 处 import（含 tool_exp 除外）
  - `grep -rn "config.session_store" narnat_agent` → 3 处（session_callbacks:16、auto_save_manager:61、summarizer:36）
  - `grep -rn "config.skill_store" narnat_agent` → 1 处（session_callbacks:21）
  - `grep -rn "config.defaults" narnat_agent` → 7 处（loader:13、session_store:21、skill_store:22、compressor:7、context:7、llm:23、output:17）
- 未验证（静态阅读无法确认，需运行验证）：
  1. Nuitka onefile / PyInstaller 运行态下的项目根定位实际结果（loader.py:210-281）
  2. Windows 下 `_within` 的大小写敏感性对技能路径校验的实际影响（skill_store.py:300）
  3. junction/symlink 环在 `_discover_skill_roots` 中的实际表现（无 visited 集合，skill_store.py:129-162）
  4. `save_session` 的 `os.replace` 在 Windows 上对已打开文件的原子替换语义（session_store.py:79）
  5. `ui` 层对 `UIConfig.raw` 全部键的消费细节（属 R6 范围，本报告只确认 assembly.py:43-48 的注入路径）
