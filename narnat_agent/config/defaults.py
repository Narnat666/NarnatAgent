"""
默认配置常量 —— 压缩prompt模板、阈值等
"""

# ── 自动保存默认值 ──
DEFAULT_AUTO_SAVE = False   # 默认不自动保存，需用户手动 /save
DEFAULT_AUTO_SAVE_TOKENS = 0  # 自动保存输入量：服务器输入token > 此值才自动保存，0=无门槛（用户输入即保存）

# ── 安全确认默认值 ──
DEFAULT_GIT_SKIP = False   # git 命令默认不免确认（即需要二次确认）
DEFAULT_RM_SKIP = False     # rm 命令默认不免确认（即需要二次确认）

# ── 工具输出全局上限（KB），0=不限制 ──
DEFAULT_MAX_TOOL_OUTPUT_KB = 64

# ── 工具超时全局上限（秒），0=不限制 ──
DEFAULT_MAX_TIMEOUT_SECONDS = 1800

# ── 插件工具（可开关分组）默认名单 ──
# 常驻工具（Read/Glob/Grep/Edit/Write/Shell/TodoWrite）不可开关；
# 下列工具可在 narnat.json "工具"."插件" 中用 "on"/"off" 开关（缺项默认 on）。
DEFAULT_PLUGIN_TOOLS = ("Terminal", "WebSearch", "Serial", "MCP")

# ── MCP 服务器默认值（对标 codex：DEFAULT_STARTUP_TIMEOUT / DEFAULT_TOOL_TIMEOUT）──
DEFAULT_MCP_STARTUP_TIMEOUT = 30   # 启动 + 握手 + 列工具超时（秒）
DEFAULT_MCP_TOOL_TIMEOUT = 300     # MCP 工具调用超时（秒）

# ── 目标模式默认值 ──
# 单一预算：续跑轮数与验证打回合并计数（每轮续跑消耗1；轮内验证打回每次额外消耗1）
DEFAULT_GOAL_MAX_ROUNDS = 10   # /goal 开启后单个任务的自动续跑轮数上限（总预算）
DEFAULT_GOAL_VERIFY = True          # 完成验证：GoalComplete提交清单后由独立AI验证器复核

# ── 目标模式完成审计提示（续跑消息中拼接，供AI自查完成度）──
GOAL_AUDIT_HINT = """\
[完成审计] 声明完成前，先从任务原文列出全部要求，逐条核对各自的证据（文件路径/命令输出/测试结果/实际现象）。
把完成当作未证实：证据不直接或不确定，就继续执行；状态复述、未执行的计划、空泛的声称，都不算证据。
完成清单必须覆盖任务的全部要求，未完成或受阻的项也要列出并写明原因，不得只报做过的部分。
确实无法完成或被阻塞时，如实标注「未完成」或「受阻」并写明原因，不要谎报完成。"""

# ── 上下文窗口占比阈值 ──
DEFAULT_CONTEXT_WINDOW = 1000000  # 模型上下文窗口（token数），≤0 视为无效
DEFAULT_SHOW_RATIO = False        # 统计栏是否显示 窗口占比:x%
DEFAULT_WARN_RATIO = 50           # 窗口占比 ≥ 此百分比时提示一次
DEFAULT_COMPRESS_RATIO = 95       # 窗口占比 ≥ 此百分比时先压缩再请求

# 压缩安全余量：压缩阈值被夹在「窗口 - 输出预留 - 本余量」以内。
# 为什么需要：压缩请求本身要吃掉窗口（全量历史 + 压缩指令 + 压缩输出），
# 只看比例会出现"历史 90% + 压缩输出 128k > 100% 窗口"，压缩请求自己先溢出，
# 压缩永远失败。留出余量后，压缩请求自身超窗在数学上不可能。
# 65536 取自 deepseek-harness 的 headroomTokens 默认值。
DEFAULT_COMPRESS_HEADROOM = 65536

# ── 压缩保留尾部预算（token）──
# 压缩时逐字保留的近期消息预算（启发式估价）；0=不保留（压缩后仅剩摘要，即旧行为）
DEFAULT_COMPRESS_RETAIN_TOKENS = 16000

# ── 回话风格提醒（narnat.json "风格"）──
# 通道一（轮次）：AI 连续若干轮纯讨论（未调用任何工具）后，在下一轮请求末尾追加
# 一条提醒（仅发给AI，终端不显示）；AI 一旦调用工具即视为在干活，计数清零。
# 通道二（token 间隔）：上下文距上次注入的 token 增量达到间隔值时，在下一轮请求
# 末尾追加一条提醒；不看 AI 在讨论还是干活，到点即注入。压缩后上下文变小，
# 基准惰性重置（重新计时）。
# 两通道共用"提醒语"；"间隔轮数"/"间隔token数"= 各自触发间隔，0=关闭对应通道。
DEFAULT_STYLE_NOTICE_ENABLED = False
DEFAULT_STYLE_NOTICE_INTERVAL = 5
DEFAULT_STYLE_NOTICE_TOKEN_INTERVAL = 0
STYLE_NOTICE_TEXT = ("回复内容简洁且抓住核心")
# 提醒语由用户配置原文，程序只加统一前缀（与 TodoWrite 迟到提醒同形）
STYLE_NOTICE_TEMPLATE = "[系统提醒] {text}"

# ── 内部注入的统一前缀 ──
# 后端为解决「模型跑偏 / 上下文丢失 / 计划没勾」而临时插进对话的 user 消息，
# 全部用这个前缀开头。它们是**给模型看的纠偏指令**，不是用户说的话，
# 不应该出现在界面历史里（用户会以为自己在某个时刻真的打过这句话）。
#
# 为什么前缀要单独定义：注入点散落在 agent_loop / compression_coordinator /
# agent 等多处，而过滤发生在 rpc/server.py —— 两处各写一份字面量必然漂移
#（改了一处忘了另一处，注入就会漏到界面上）。这里是唯一真源。
INTERNAL_NOTICE_PREFIX = "[系统提醒]"

# 溢出恢复压缩后追加的内部继续消息（compression_coordinator.OVERFLOW_CONTINUE_MESSAGE）。
# 它不以 [系统提醒] 开头（历史遗留的 `[]` 前缀），所以单独登记。
# 这里只放「开头片段」用于识别，完整文案仍由 compression_coordinator 定义。
INTERNAL_NOTICE_MARKERS = (
    INTERNAL_NOTICE_PREFIX,
    "[]对话历史过长已自动压缩",
)

# ── 基础Prompt模板 ──
BASE_PROMPT_TEMPLATE = """\
| 你的身份 | 你是 {model} 智能体。 |
| 所处环境 | narnat agent 框架内，作为自主代理运行。 |
| 当前工作目录 | {cwd} |
| 所处平台 | {platform} |
| 核心任务 | 尽你所能帮助用户，为用户解难。 |
"""

# ── 压缩Prompt模板 ──
# 结构化检查点：固定小节顺序，防止摘要漏掉续跑最关键的信息（下一步/关键上下文）；
# 合并规则防止多次压缩后旧摘要照抄叠加、信息过时膨胀。
COMPRESS_PROMPT = """你现在是压缩引擎。把以上对话浓缩成一个结构化检查点，使另一个模型可以无损接续工作。

严格按下面的 Markdown 结构输出：保持每一节的顺序，用简练的要点而非散文。空节写"(无)"，不得省略任何一节。

## 原始请求与意图
- [用户最初及演进中的目标；措辞重要处原文引用]

## 关键技术概念
- [涉及的技术、框架、模式与约定]

## 文件与代码
- [精确路径：其重要性、关键改动或代码片段]

## 错误与修复
- [错误：如何解决的，以及相关用户反馈]

## 待办任务
- [已明确要求但尚未完成的工作]

## 当前工作
- [检查点时刻正在进行的精确工作]

## 下一步
- [与最近请求直接衔接的单一动作，或"(无)"]

## 关键上下文
- [决策及其理由、约束、用户偏好、未决问题、继续所需的数据]

规则：
- 用简洁的中文工程语言。保留精确的文件路径、命令、报错串、标识符、数值、函数签名与语法片段。
- 忠实记录用户反馈与明确指令，尤其是纠正。
- 不要提及本次摘要请求，也不要提及上下文被压缩。
- 只输出检查点文本：不要调用任何工具或采取其他行动。
- 若对话中已存在"# 上一轮对话成果"摘要，它是旧检查点。不要原文照抄：保留仍成立的事实、删除过时内容、把新信息合并进同一结构下的单一整合摘要。"""

# ── 项目技能扫描默认值 ──
# /skill 除系统技能（.narnat/config/skills/）外，还会自动扫描当前工作目录下所有名为
# skills 的目录作为项目技能根（如 .agents/skills、.kimi-code/skills、顶层 skills 等），
# 无需写死目录列表。
# 可在 narnat.json 的 "技能"."项目技能目录" 显式指定目录列表（覆盖自动发现）；
# 写 [] 表示关闭项目技能扫描。
DEFAULT_SKILL_SCAN_DEPTH = 4  # 自动发现 skills 目录的最大递归深度（根目录算第1层）

# ── .narnat 目录名 ──
NARNAT_DIR = ".narnat"

# ── .narnat 内部子目录 ──
CONFIG_SUBDIR = "config"       # 配置层：静态、用户可编辑
DATA_SUBDIR = "data"           # 数据层：运行时持久化
LOGS_SUBDIR = "logs"           # 日志层：可清理
SESSIONS_SUBDIR = "sessions"   # 会话存档（相对 data/）

# ── 忽略目录种子列表 ──
# 只服务于「项目技能发现」的扫描：首次生成 narnat.json 时写入"忽略目录"键，
# 用户可自行删改。Grep/Glob 不读这个清单（它们走各目录的 .gitignore）。
# 键缺失或为空 → 用本列表。
DEFAULT_IGNORE_DIRS = [".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".idea", ".vscode", ".tox", ".nox"]

# ── 配置文件名（相对于 config/ 子目录） ──
NARNAT_JSON = "narnat.json"
NARNAT_MD = "narnat.md"

# ── 默认AI配置 ──
DEFAULT_API_KEY = ""
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROTOCOL = "anthropic"          # "openai" | "anthropic"
DEFAULT_THINKING_ENABLED = True
DEFAULT_THINKING_EFFORT = "high"      # high / max
DEFAULT_THINKING_PASSBACK = False     # 思考回传：默认不回传（思考内容仍记录到会话，只是不发给模型）


# ── Thinking 参数映射表 ──
# 每个条目定义了一组 (协议, 模型前缀) 的 thinking 参数构造规则。
# 结构:
#   "enable":   {地点: {参数: 值}}  — 启用 thinking 时写入
#   "disable":  {地点: {参数: 值}}  — 禁用 thinking 时写入（可选）
#   "effort_path": tuple | None      — effort 值的写入位置
#   "effort_map": dict | None        — 语义 effort → provider 实际值的映射
#   "passback": str                  — 思考回传格式（官方要求为依据）：
#       "thinking_block"        → anthropic 协议 thinking 内容块，无需签名（DeepSeek 强制，否则400）
#       "thinking_block_signed" → anthropic 协议 thinking 内容块，必须携带签名（Claude）
#       "reasoning_content"     → OpenAI 协议顶层 reasoning_content 字段（DeepSeek/Kimi 强制；GLM 可选支持）
#       "none"                  → 不回传（官方不要求/不支持/回传被忽略：GPT/Qwen 等）
#
# 地点说明:
#   "body_top"  → OpenAI: 顶层 kwargs  /  Anthropic: 合并到 body
#   "body"      → OpenAI: 不用       /  Anthropic: 合并到 body
#   "extra_body"→ OpenAI: extra_body  /  Anthropic: 合并到 body
THINKING_PARAM_MAP = {
    # ── DeepSeek (Anthropic 协议) ──
    ("anthropic", "deepseek"): {
        "enable": {
            "body":      {"thinking": {"type": "enabled"}},
            "body_top":  {},  # output_config 由 effort_path 动态构造
        },
        "effort_path": ("body_top", "output_config", "effort"),
        # 官方强制：请求尾部 assistant 必须携带 thinking 块，否则 400
        "passback": "thinking_block",
    },

    # ── DeepSeek (OpenAI 协议) ──
    ("openai", "deepseek"): {
        "enable": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "body_top":   {},  # reasoning_effort 由 effort_path 注入
        },
        "effort_path": ("body_top", "reasoning_effort"),
        # 官方强制：工具调用轮次的 reasoning_content 必须回传，否则 400
        "passback": "reasoning_content",
    },

    # ── GLM (OpenAI 协议) ──
    # thinking 必须走 extra_body，OpenAI SDK 不接受 thinking 作为顶层 kwargs
    # reasoning_effort 走 body_top（OpenAI SDK 原生支持）
    ("openai", "glm"): {
        "enable": {
            # clear_thinking=false：官方建议值，历史 reasoning_content 作为上下文保留
            "extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}},
        },
        "disable": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "effort_path": ("body_top", "reasoning_effort"),
        # 官方：assistant 消息携带 reasoning_content 时作为上下文输入（可选回传，
        # 不回传不报错）；注：GLM-5.2 coding 端点曾报回传无效（zai-org/GLM-5#92）
        "passback": "reasoning_content",
    },

    # ── Kimi (OpenAI 协议) ──
    ("openai", "kimi"): {
        "enable": {
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        "disable": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "effort_path": None,  # Kimi 无强度概念
        # 官方强制：多轮和工具调用必须原样回传 reasoning_content
        "passback": "reasoning_content",
    },

    # ── 小米 MiMo (OpenAI 协议) ──
    ("openai", "mimo"): {
        "enable": {
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        "disable": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "effort_path": None,  # 官方未提供思考强度参数
        # 官方强制：深度思考+历史含工具调用时，assistant 含工具调用必须完整回传
        # reasoning_content，否则 400（mimo-v2.5-pro / mimo-v2.5）
        "passback": "reasoning_content",
    },

    # ── 小米 MiMo (Anthropic 协议) ──
    ("anthropic", "mimo"): {
        "enable": {
            "body": {"thinking": {"type": "enabled"}},
        },
        "effort_path": None,
        # 官方：Anthropic 兼容协议同样要求回传思考块（受影响的 Agent 产品列表含 Anthropic 兼容协议）
        "passback": "thinking_block",
    },

    # ── Qwen (OpenAI 协议) ──
    ("openai", "qwen"): {
        "enable": {
            "extra_body": {"enable_thinking": True},
        },
        "disable": {
            "extra_body": {"enable_thinking": False},
        },
        "effort_path": ("extra_body", "thinking_budget"),
        "effort_map": {"max": 32000, "xhigh": 24000, "high": 16000,
                       "medium": 8000, "low": 4000, "minimal": 1000, "none": 0},
        # 官方：可选回传，且明确禁止拼入 content；不回传不报错
        "passback": "none",
    },

    # ── GPT (OpenAI 协议) ──
    ("openai", "gpt"): {
        "enable": {
            "body_top": {},  # reasoning_effort 自己就是开关+强度
        },
        "disable": {
            "body_top": {"reasoning_effort": "none"},
        },
        "effort_path": ("body_top", "reasoning_effort"),
        # 官方：每轮推理 token 自动丢弃，服务端生成摘要延续上下文，无需回传
        "passback": "none",
    },

    # ── Claude (Anthropic 协议，新版 adaptive) ──
    ("anthropic", "claude"): {
        "enable": {
            "body": {"thinking": {"type": "adaptive"}},
        },
        # 官方契约：effort 位于 output_config 对象内（Claude Platform Docs
        # 「Effort is set at output_config.effort」），不可写在请求体顶层
        "effort_path": ("body_top", "output_config", "effort"),
        # 官方支持回传思考块（工具轮次建议带上），但必须携带加密签名；
        # 流式下签名经 signature_delta 事件送达，捕获后原样回传
        "passback": "thinking_block_signed",
    },
}


# ── 识图能力表 ──
# 与 THINKING_PARAM_MAP 同一范式：(协议, 模型前缀) → 是否接受图片输入。
#
# 缺省方向是**保守的**：未列出的模型一律视为不支持识图。
# 理由是 narnat 允许用户自填 base_url（中转站、自建网关、各种兼容端点），
# 未编目模型占多数；猜"支持"会直接吃服务端 400 打断对话，
# 猜"不支持"只是少发一张图（模型仍能看到句柄文本并据此提示用户切模型）。
# 两个方向的代价不对等，故取保守值。
#
# 新增支持识图的模型只需在此追加一条，无需改任何判定代码。
IMAGE_CAPABLE_MODELS = {
    # DeepSeek：V4 Flash 线自 2026-08 起原生识图，Anthropic 兼容端点接受
    # {"type":"image","source":{"type":"base64",...}}（deepseek-harness 同一端点实证）
    ("anthropic", "deepseek-flash"): True,
    ("openai", "deepseek-flash"): True,

    # Claude 全系原生支持
    ("anthropic", "claude"): True,

    # GPT / Qwen-VL / GLM-V 等视觉模型
    ("openai", "gpt-4o"): True,
    ("openai", "gpt-4.1"): True,
    ("openai", "qwen-vl"): True,
}

# 显式不支持：名单里写了 False 的优先于前缀匹配（用于"前缀像但实际不支持"的型号）
IMAGE_MODEL_EXCLUSIONS = {
    # V4 Pro 是纯文本模型（deepseek-harness 默认目录条目 inputModalities=['text']）
    ("anthropic", "deepseek-v4-pro"): False,
    ("openai", "deepseek-v4-pro"): False,
}


def resolve_image_support(protocol: str, model: str) -> bool:
    """查表：该 (协议, 模型) 是否接受图片输入。

    匹配顺序：排除名单 → 能力表前缀匹配 → 缺省 False（保守）。
    """
    if not model:
        return False
    model_lower = model.lower()

    for (proto, prefix), supported in IMAGE_MODEL_EXCLUSIONS.items():
        if proto == protocol and model_lower.startswith(prefix):
            return supported

    for (proto, prefix), supported in IMAGE_CAPABLE_MODELS.items():
        if proto == protocol and model_lower.startswith(prefix):
            return supported

    return False


def resolve_thinking_passback(protocol: str, model: str) -> str:
    """查表：该 (协议, 模型前缀) 组合的思考回传格式。

    Returns:
        "thinking_block"        — anthropic 协议 thinking 内容块回传（无需签名）
        "thinking_block_signed" — anthropic 协议 thinking 内容块回传（必须携带签名）
        "reasoning_content"     — OpenAI 协议顶层 reasoning_content 字段回传
        "none"                  — 不回传
    未匹配到表项 → "none"（安全默认：不回传不会踩强制校验以外的错误）。
    """
    model_lower = model.lower()
    for (proto, prefix), mapping in THINKING_PARAM_MAP.items():
        if proto == protocol and model_lower.startswith(prefix):
            return mapping.get("passback", "none")
    return "none"


def resolve_thinking_params(protocol: str, model: str,
                            thinking_enabled: bool, effort: str):
    """根据协议+模型查找 thinking 参数映射。

    Returns:
        (body_top: dict, extra_body: dict)
        - OpenAI 后端: body_top → kwargs, extra_body → extra_body
        - Anthropic 后端: 两者合并到 body
        - thinking_enabled=False 且无 disable 映射 → 返回空
    """
    model_lower = model.lower()
    matched = None
    for (proto, prefix), mapping in THINKING_PARAM_MAP.items():
        if proto == protocol and model_lower.startswith(prefix):
            matched = mapping
            break

    if matched is None:
        return {}, {}

    body_top = {}
    extra_body = {}

    # 选择 enable 或 disable
    if thinking_enabled:
        source = matched.get("enable", {})
    else:
        source = matched.get("disable")
        if source is None:
            return {}, {}  # 不支持禁用 → 不传任何 thinking 参数

    for location, params in source.items():
        target = body_top if location == "body_top" else extra_body
        target.update(params)

    # 注入 effort 值（仅在启用思考时）
    if thinking_enabled:
        effort_path = matched.get("effort_path")
        if effort_path and effort:
            location = effort_path[0]
            keys = effort_path[1:]
            target = body_top if location == "body_top" else extra_body

            # 可选：通过 effort_map 转换语义 effort → provider 实际值
            effort_map = matched.get("effort_map")
            actual_effort = effort_map.get(effort, effort) if effort_map else effort

            if len(keys) == 1:
                target[keys[0]] = actual_effort
            elif len(keys) == 2:
                d = target.setdefault(keys[0], {})
                d[keys[1]] = actual_effort

    return body_top, extra_body
