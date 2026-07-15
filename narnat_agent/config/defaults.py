"""
默认配置常量 —— 压缩prompt模板、阈值等
"""

# ── 自动保存默认值 ──
DEFAULT_AUTO_SAVE = False   # 默认不自动保存，需用户手动 /save

# ── 安全确认默认值 ──
DEFAULT_GIT_SKIP = False   # git 命令默认不免确认（即需要二次确认）
DEFAULT_RM_SKIP = False     # rm 命令默认不免确认（即需要二次确认）

# ── 工具输出全局上限（KB），0=不限制 ──
DEFAULT_MAX_TOOL_OUTPUT_KB = 64

# ── 计划优先默认值 ──
DEFAULT_REQUIRE_PLAN = False  # 是否强制AI先制定计划再执行工具
DEFAULT_MIN_TOOLS = 2         # 单轮工具调用数≥此值时才强制要求先写计划

# ── 上下文压缩阈值 ──
WARN_TURN_1 = 50    # 提示对话已50轮
WARN_TURN_2 = 80   # 提示已80轮，建议开新对话
COMPRESS_TURN = 100  # 强制压缩

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

# ── .narnat 目录名 ──
NARNAT_DIR = ".narnat"

# ── .narnat 内部子目录 ──
CONFIG_SUBDIR = "config"       # 配置层：静态、用户可编辑
DATA_SUBDIR = "data"           # 数据层：运行时持久化
LOGS_SUBDIR = "logs"           # 日志层：可清理

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


# ── Thinking 参数映射表 ──
# 每个条目定义了一组 (协议, 模型前缀) 的 thinking 参数构造规则。
# 结构:
#   "enable":   {地点: {参数: 值}}  — 启用 thinking 时写入
#   "disable":  {地点: {参数: 值}}  — 禁用 thinking 时写入（可选）
#   "effort_path": tuple | None      — effort 值的写入位置
#   "effort_map": dict | None        — 语义 effort → provider 实际值的映射
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
    },

    # ── DeepSeek (OpenAI 协议) ──
    ("openai", "deepseek"): {
        "enable": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "body_top":   {},  # reasoning_effort 由 effort_path 注入
        },
        "effort_path": ("body_top", "reasoning_effort"),
    },

    # ── GLM (OpenAI 协议) ──
    ("openai", "glm"): {
        "enable": {
            "body_top": {"thinking": {"type": "enabled"}},
        },
        "disable": {
            "body_top": {"thinking": {"type": "disabled"}},
        },
        "effort_path": ("body_top", "reasoning_effort"),
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
    },

    # ── Claude (Anthropic 协议，新版 adaptive) ──
    ("anthropic", "claude"): {
        "enable": {
            "body": {"thinking": {"type": "adaptive"}},
        },
        "effort_path": ("body_top", "effort"),
    },
}


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
