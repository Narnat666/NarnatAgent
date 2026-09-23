"""配置树数据类 —— narnat.json / narnat.md 解析后一次性构建的只读配置树。

契约来源：`openspec/changes/recast-v2/specs/config/spec.md`（「磁盘路径布局」
「数值解析与容错」「模型配置归一」「费用日志容量」等 Requirement）。
字段名、默认值与 frozen 语义逐项对齐旧实现数据类（外部契约：其余积木按字段名读取）。

状态约束（design D6/D7）：
- 除 `AIConfig.thinking_effort` / `thinking_passback` / `model`（运行时命令可改）
  与 `UIConfig.raw`（dict 容器内部可变）外，树节点全部冻结；
- 本模块为零逻辑纯定义层，不 import 新包其他积木。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .defaults import (
    DEFAULT_API_KEY,
    DEFAULT_AUTO_SAVE,
    DEFAULT_AUTO_SAVE_TOKENS,
    DEFAULT_BALANCE_AUTH_METHOD,
    DEFAULT_BALANCE_ENABLED,
    DEFAULT_BASE_URL,
    DEFAULT_COMPRESS_RATIO,
    DEFAULT_COMPRESS_RETAIN_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_COST_LOG_ENABLED,
    DEFAULT_COST_LOG_MAX_BYTES,
    DEFAULT_GIT_SKIP,
    DEFAULT_GOAL_MAX_ROUNDS,
    DEFAULT_LLM_RETRY_COUNT,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOOL_OUTPUT_KB,
    DEFAULT_MCP_STARTUP_TIMEOUT,
    DEFAULT_MCP_TOOL_TIMEOUT,
    DEFAULT_MIN_TOOLS,
    DEFAULT_MODEL,
    DEFAULT_PROTOCOL,
    DEFAULT_REQUIRE_PLAN,
    DEFAULT_RM_SKIP,
    DEFAULT_SHOW_BALANCE,
    DEFAULT_SHOW_COST,
    DEFAULT_SHOW_RATIO,
    DEFAULT_THINKING_ENABLED,
    DEFAULT_THINKING_EFFORT,
    DEFAULT_THINKING_OPTIONS,
    DEFAULT_THINKING_PASSBACK,
    DEFAULT_TOOL_MAX_SESSIONS,
    DEFAULT_TOOL_MAX_TRANSFER_MB,
    DEFAULT_UI_MAX_OUTPUT_TOKENS,
    DEFAULT_WARN_RATIO,
)

__all__ = [
    "AIConfig",
    "BalanceConfig",
    "Config",
    "CostLogConfig",
    "McpServerConfig",
    "PathConfig",
    "PlanConfig",
    "PricingConfig",
    "SafetyConfig",
    "SessionConfig",
    "SkillConfig",
    "ToolConfig",
    "UIConfig",
]


@dataclass
class AIConfig:
    """AI 连接配置（非冻结：`thinking_effort`/`thinking_passback`/`model` 由运行时命令修改）。"""

    api_key: str = DEFAULT_API_KEY
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    model_options: list = field(default_factory=lambda: [DEFAULT_MODEL])  # /mode 可切换的模型列表
    protocol: str = DEFAULT_PROTOCOL              # "openai" | "anthropic"
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_enabled: bool = DEFAULT_THINKING_ENABLED
    thinking_effort: str = DEFAULT_THINKING_EFFORT
    thinking_passback: bool = DEFAULT_THINKING_PASSBACK  # 思考回传开关（由 /thinkback 命令修改）
    thinking_options: dict = field(default_factory=lambda: dict(DEFAULT_THINKING_OPTIONS))
    context_window: int = DEFAULT_CONTEXT_WINDOW   # 模型上下文窗口（token数），≤0 视为无效
    retry_count: int = DEFAULT_LLM_RETRY_COUNT
    goal_max_rounds: int = DEFAULT_GOAL_MAX_ROUNDS  # 目标模式单个任务的自动续跑轮数上限


@dataclass(frozen=True)
class PathConfig:
    """磁盘路径配置（只读）。"""

    project_root: str = ""
    narnat_dir: str = ""
    config_dir: str = ""    # .narnat/config/
    data_dir: str = ""      # .narnat/data/
    logs_dir: str = ""      # .narnat/logs/


@dataclass(frozen=True)
class ToolConfig:
    """工具配置（只读）。单位转换在 load 时完成，外部直接用最终单位。"""

    max_sessions: int = DEFAULT_TOOL_MAX_SESSIONS              # SSH/串口最大会话数
    max_transfer_mb: int = DEFAULT_TOOL_MAX_TRANSFER_MB        # 文件传输上限(MB)
    max_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_KB * 1024  # 工具输出上限(字符数)
    max_timeout_seconds: int = DEFAULT_MAX_TIMEOUT_SECONDS     # 工具超时上限(秒)，0=不限制
    ignore_dirs: tuple = ()                                    # 忽略目录（唯一来源 narnat.json"忽略目录"键，空=不忽略）


@dataclass(frozen=True)
class SafetyConfig:
    """安全确认配置（只读）。"""

    git_skip_confirm: bool = DEFAULT_GIT_SKIP
    rm_skip_confirm: bool = DEFAULT_RM_SKIP


@dataclass(frozen=True)
class McpServerConfig:
    """单个 MCP 服务器配置（只读）。

    stdio 服务器：command/args/env/cwd 启动本地进程；命名与字段对标 codex 的
    [mcp_servers.<name>]（startup_timeout_sec / tool_timeout_sec /
    enabled_tools / disabled_tools）。
    """

    name: str = ""
    command: str = ""
    args: tuple = ()                        # 命令行参数
    env: dict[str, str] = field(default_factory=dict)   # 附加环境变量（继承本进程环境后覆盖）
    cwd: str = ""                           # 工作目录，空=本进程当前目录
    enabled: bool = True                    # False=不启动
    startup_timeout: int = DEFAULT_MCP_STARTUP_TIMEOUT   # 启动+握手+列工具超时（秒）
    tool_timeout: int = DEFAULT_MCP_TOOL_TIMEOUT         # 工具调用超时（秒）
    enabled_tools: tuple = ()               # 工具白名单（服务端原始工具名），空=全部
    disabled_tools: tuple = ()              # 工具黑名单（在白名单之后生效）


@dataclass(frozen=True)
class PlanConfig:
    """计划优先配置（只读）。"""

    require_plan: bool = DEFAULT_REQUIRE_PLAN
    min_tools: int = DEFAULT_MIN_TOOLS


@dataclass(frozen=True)
class SessionConfig:
    """会话与上下文配置（只读）。"""

    auto_save: bool = DEFAULT_AUTO_SAVE
    auto_save_tokens: int = DEFAULT_AUTO_SAVE_TOKENS
    show_ratio: bool = DEFAULT_SHOW_RATIO
    warn_ratio: int = DEFAULT_WARN_RATIO
    compress_ratio: int = DEFAULT_COMPRESS_RATIO
    retain_tokens: int = DEFAULT_COMPRESS_RETAIN_TOKENS  # 压缩保留尾部预算（token），0=不保留


@dataclass(frozen=True)
class SkillConfig:
    """技能配置（只读）。"""

    # 项目技能根目录: None=自动发现（扫描工作目录下所有名为 skills 的目录）；
    # 空元组=关闭项目技能扫描；非空元组=仅扫描显式指定目录（相对工作目录，支持绝对路径）
    project_roots: tuple | None = None


@dataclass(frozen=True)
class PricingConfig:
    """定价配置（只读）。

    用户自定义定价（中文 key 映射到英文 key）：
    {"模型名": {"输入": x, "缓存命中": y, "输出": z}} → {"模型名": {"input": …, "cache_hit": …, "output": …}}
    """

    user_pricing: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class BalanceConfig:
    """余额查询配置（只读）。"""

    enabled: bool = DEFAULT_BALANCE_ENABLED
    url: str = ""                    # 查询地址
    auth_method: str = DEFAULT_BALANCE_AUTH_METHOD   # "bearer" | "x-api-key"
    value_path: str = ""             # 余额数值 JSONPath
    currency_path: str = ""          # 货币单位 JSONPath


@dataclass(frozen=True)
class CostLogConfig:
    """费用日志配置（只读）：开启后每次 LLM 请求追加一行到 CSV。

    双文件轮转：活动文件（path）达到 max_bytes 后改名为「主名_bak.扩展名」
    （旧的 _bak 被删除），再新建活动文件从表头开始写。
    磁盘上始终只有 1 个活动文件 + 1 个备份文件。
    max_bytes = 0 表示不限制（单文件无限追加）。
    """

    enabled: bool = DEFAULT_COST_LOG_ENABLED
    path: str = ""                   # CSV输出路径，空=默认 .narnat/data/cost_log.csv
    max_bytes: int = DEFAULT_COST_LOG_MAX_BYTES  # 活动文件容量上限（默认50MB），0=不限制


@dataclass(frozen=True)
class UIConfig:
    """UI 配置（只读）。

    raw: narnat.json 中 "界面" 分组的完整 dict（键已归一为中→英），直接传给显示层。
    结构:
      {
        "colors":    {"accent": "#88C0D0", ...},
        "markdown":  {"heading_h1": "bold accent", ...},
        "codeblock": {"lang_cyan": "#00FFFF", ...},
        "diff":      {"added": "success", ...},
        "ui":        {"header": "accent", ...},
        "cmd":       {"error": "error", ...},
        "prompt":    {"symbol": "bold #00ff00", ...},
        "show_cost": False,
        "show_balance": False,
        "max_output_tokens": 128000,
      }
    """

    raw: dict = field(default_factory=dict)
    show_cost: bool = DEFAULT_SHOW_COST
    show_balance: bool = DEFAULT_SHOW_BALANCE
    max_output_tokens: int = DEFAULT_UI_MAX_OUTPUT_TOKENS


@dataclass
class Config:
    """应用总配置（非冻结：`AIConfig` 中若干字段仍需运行时修改）。"""

    ai: AIConfig = field(default_factory=AIConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    skills: SkillConfig = field(default_factory=SkillConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    balance: BalanceConfig = field(default_factory=BalanceConfig)
    cost_log: CostLogConfig = field(default_factory=CostLogConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    api_keys: dict = field(default_factory=dict)
    system_prompt: str = ""
