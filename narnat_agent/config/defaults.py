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

# ── 计划优先默认值 ──
DEFAULT_REQUIRE_PLAN = False  # 是否强制AI先制定计划再执行工具
DEFAULT_MIN_TOOLS = 2         # 单轮工具调用数≥此值时才强制要求先写计划

# ── 目标模式默认值 ──
DEFAULT_GOAL_MAX_ROUNDS = 100  # /goal 开启后单个任务的自动续跑轮数上限

# ── 上下文窗口占比阈值 ──
DEFAULT_CONTEXT_WINDOW = 1000000  # 模型上下文窗口（token数），≤0 视为无效
DEFAULT_SHOW_RATIO = False        # 统计栏是否显示 窗口占比:x%
DEFAULT_WARN_RATIO = 50           # 窗口占比 ≥ 此百分比时提示一次
DEFAULT_COMPRESS_RATIO = 95       # 窗口占比 ≥ 此百分比时先压缩再请求

# ── 基础Prompt模板 ──
BASE_PROMPT_TEMPLATE = """\
| 你的身份 | 你是一位严谨、克制且极具专业素养的 {model} 智能体。 |
| 所处环境 | narnat agent 框架内，作为自主代理运行。 |
| 当前工作目录 | {cwd} |
| 所处平台 | {platform} |
| 核心任务 | 尽你所能帮助用户，为用户解难。 |
"""

# ── 压缩Prompt模板 ──
COMPRESS_PROMPT = """直接输出本轮对话核心经验总结，作为下一新会话的基础。必须包含：

1. 用户原始请求与当前目标
2. 已完成工作及结果（涉及文件、执行命令、实际产出）
3. 未完成任务及下一步计划
4. 关键决策及原因
5. 核心技术细节（文件路径、关键代码段）
6. 遇到的错误及解决方案"""

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
# 仅用于首次生成 narnat.json 时写入"忽略目录"键（把常见噪音目录作为可见建议写入文件，用户可自行删改）。
# 运行时以 narnat.json 为准：键缺失或为空 → 不忽略任何目录。
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
DEFAULT_THINKING_PASSBACK = True      # 思考回传：捕获并回传思考内容（DeepSeek思考模式契约）


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
        "effort_path": ("body_top", "effort"),
        # 官方支持回传思考块（工具轮次建议带上），但必须携带加密签名；
        # 流式下签名经 signature_delta 事件送达，捕获后原样回传
        "passback": "thinking_block_signed",
    },
}


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
