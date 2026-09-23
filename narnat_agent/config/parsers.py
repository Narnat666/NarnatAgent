"""配置解析纯函数 —— 把 narnat.json 的原始 dict 解析为配置树节点。

契约来源：`openspec/changes/recast-v2/specs/config/spec.md`
（「配置键名兼容」「数值解析与容错」「模型配置归一」「系统提示词组装」
「MCP 服务器参数解析」），界面键名归一的行为由 `specs/output/spec.md`
「中文键名与英文别名」定义。

解析风格（逐项复刻现状，不做"顺手修正"）：
- 裸转型（`int()`/`bool()`）处保持裸转型——非法值会抛错；
- `coerce_value` 处保持宽松回落——None/空串/非法 → None；
- 字符串布尔按非空字符串为真（`bool("false") is True`）等兼容怪癖保持（spec 末章）。

本模块为纯函数层（仅 `os`/`platform` 用于路径拼接与平台名），无 I/O、无状态。
"""
from __future__ import annotations

import os
import platform
import re

from .defaults import (
    BASE_PROMPT_TEMPLATE,
    COST_LOG_FILENAME,
    DEFAULT_API_KEY,
    DEFAULT_BALANCE_AUTH_METHOD,
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_COST_LOG_MAX_MB,
    DEFAULT_GOAL_MAX_ROUNDS,
    DEFAULT_LLM_RETRY_COUNT,
    DEFAULT_MCP_STARTUP_TIMEOUT,
    DEFAULT_MCP_TOOL_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_PROTOCOL,
    DEFAULT_THINKING_EFFORT,
    DEFAULT_THINKING_ENABLED,
    DEFAULT_THINKING_OPTIONS,
    DEFAULT_THINKING_PASSBACK,
    DEFAULT_UI_MAX_OUTPUT_TOKENS,
)
from .models import (
    AIConfig,
    BalanceConfig,
    CostLogConfig,
    McpServerConfig,
    PricingConfig,
    UIConfig,
)

__all__ = [
    "build_ai_config",
    "build_balance_config",
    "build_cost_log_config",
    "build_pricing_config",
    "build_system_prompt",
    "build_ui_config",
    "coerce_value",
    "parse_mcp_server",
    "parse_model_config",
    "parse_pricing",
    "parse_project_skill_roots",
    "parse_token_amount",
    "strip_subagent_hidden",
]


# ═══════════════════════════════════════════════════════════════
# 「界面」分组键名映射表（中 → 英）
# ═══════════════════════════════════════════════════════════════

# 分组名映射（英文别名同时存在时中文键优先，spec/output「中文键优先于英文」）。
SECTION_MAP = {
    "颜色": "colors", "基础色": "base_colors",
    "标注": "markdown", "标记": "markdown",
    "代码块": "codeblock", "差异": "diff", "对比": "diff",
    "框架": "ui", "命令": "cmd", "提示符": "prompt",
}

# 分组内键名映射（键存在即替换：中文键优先）。
KEY_MAPS = {
    "colors": {
        # 仅保留旧格式兼容项（如 "成功色""警告色" 在配方值中可能出现）
        "成功色": "success", "警告色": "warning",
        "错误色": "error", "链接色": "link",
        "装饰色": "decoration", "强调": "emphasis",
    },
    "base_colors": {
        "用户": "user", "主色": "primary", "次色": "secondary",
        "强调色": "accent", "链接": "link", "链接色": "link",
        "装饰": "decoration", "装饰色": "decoration",
    },
    "markdown": {
        "标题1": "heading_h1", "标题3": "heading_h3", "标题4": "heading_h4",
        "粗体": "bold", "斜体": "italic", "删除线": "strikethrough",
        "行内代码": "code_inline", "链接": "link", "图片": "image",
        "引用": "blockquote", "分隔线": "hr",
        "无序列表": "list_unordered", "有序列表": "list_ordered",
        "任务完成": "task_done", "任务未完成": "task_undone",
        "表格边框": "table_border", "表格内容": "table_content",
    },
    "codeblock": {
        "背景": "background",
        "行号": "line_number", "语言标签": "lang_label",
        "语言青": "lang_cyan", "语言黄": "lang_yellow", "语言绿": "lang_green",
        "语言紫": "lang_magenta", "语言红": "lang_red",
        "语言蓝": "lang_blue", "语言灰": "lang_gray",
    },
    "diff": {
        "头部": "header", "范围": "range", "添加": "added",
        "删除": "removed", "上下文": "context",
    },
    "ui": {
        "标题": "header", "加载动画": "spinner",
        "中断": "interrupted", "中断提示": "interrupted_hint",
        "统计标签": "stats_label", "统计数值": "stats_value", "分隔": "separator",
    },
    "cmd": {
        "成功": "success", "错误": "error", "提示": "hint",
        "高亮": "highlight", "弱化": "muted",
    },
    "prompt": {
        "符号": "symbol", "文字": "text", "自定义": "custom",
    },
}

# 历史扁平旧键 → (新分组, 新角色)（spec/config「旧扁平配色迁移」；
# 映射明细同 specs/output「中文键名与英文别名」）。
# 注意：「colors」段与本节都含 成功色/错误色/链接色/装饰色，落到不同分组——
# 这是已发布行为（两条路径互不影响），不得合并。
OLD_COLOR_MAP = {
    "用户输入色": ("base_colors", "user"),
    "AI输出色": ("base_colors", "primary"),
    "标题色": ("base_colors", "accent"),
    "成功色": ("base_colors", "success"),
    "行内代码色": ("base_colors", "warning"),
    "错误色": ("base_colors", "error"),
    "链接色": ("base_colors", "link"),
    "装饰色": ("base_colors", "decoration"),
    "加载动画色": ("base_colors", "emphasis"),
    "次要文字色": ("base_colors", "secondary"),
    "代码块背景色": ("codeblock", "background"),
}

# 配方值中的中文色名 → 英文（替换顺序 = 本 dict 的键序，见兼容性怪癖：
# "强调" 先于 "强调色" 命中，故 "bold 强调色" → "bold emphasis色"）。
COLOR_ZH_EN = {**KEY_MAPS["colors"], **KEY_MAPS["base_colors"]}

# 参与配方值中文化替换的分组（固定顺序）。
UI_RECIPE_SECTIONS = ("colors", "markdown", "codeblock", "diff", "ui", "cmd", "prompt")


# ═══════════════════════════════════════════════════════════════
# 宽松转型与 Token 量
# ═══════════════════════════════════════════════════════════════

def coerce_value(v, target_type):
    """宽松转型：空串/非法值 → None（spec「数值解析与容错」）。

    等价于旧实现 `_coerce`：
    - `None` 与 `""` 同样视为"缺失"；
    - 不排除 bool（`int(True)` = 1）；
    - 空白串不 trim（`" "` 走转换失败路径 → None）。
    """
    if v in (None, ""):
        return None
    try:
        return target_type(v)
    except (TypeError, ValueError):
        return None


def parse_token_amount(v, default: int = 0) -> int:
    """解析 token 量配置：数字（10000）或 "10k"/"1.5K"（k = ×1000）。

    对应 spec Scenario「字符串 Token 量」与「非法数值回落」：
    `None`/空串/非法内容 → default；bool → default；负数 → 0。
    """
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        try:
            return max(0, int(v))
        except (ValueError, OverflowError):
            return default
    s = str(v).strip().lower()
    if s.endswith("k"):
        try:
            return max(0, int(float(s[:-1]) * 1000))
        except ValueError:
            return default
    try:
        return max(0, int(s))
    except ValueError:
        return default


# ═══════════════════════════════════════════════════════════════
# 模型 / 定价 / 技能目录
# ═══════════════════════════════════════════════════════════════

def parse_model_config(value) -> tuple:
    """解析「模型」配置，返回 (当前模型, 候选列表)（spec「模型配置归一」）。

    - 非 dict → (DEFAULT_MODEL, [DEFAULT_MODEL])；
    - `列表` 非 list → 空；元素仅保留字符串；
    - `当前` 为空 → 列表首项（列表为空时 DEFAULT_MODEL）；
    - `当前` 不在列表中 → 插入列表首位。
    """
    if not isinstance(value, dict):
        return DEFAULT_MODEL, [DEFAULT_MODEL]
    options = value.get("列表")
    if not isinstance(options, list):
        options = []
    options = [m for m in options if isinstance(m, str)]
    current = value.get("当前") or (options[0] if options else DEFAULT_MODEL)
    if current not in options:
        options.insert(0, current)
    return current, options


def parse_pricing(data: dict) -> dict:
    """解析用户定价：中文键 → 英文键（`输入`/`缓存命中`/`输出`）。

    非 dict 的模型条目跳过；缺键补 0；英文键被忽略（现状行为）。
    """
    result = {}
    for model, prices in data.items():
        if not isinstance(prices, dict):
            continue
        result[model] = {
            "input": prices.get("输入", 0),
            "cache_hit": prices.get("缓存命中", 0),
            "output": prices.get("输出", 0),
        }
    return result


def parse_project_skill_roots(data: dict) -> tuple | None:
    """解析「技能.项目技能目录」（spec「项目技能目录三态」）。

    - 键缺失或非列表 → None（自动发现）；
    - 列表 → 取其中非空字符串项作显式目录（空列表 → 空元组 = 关闭扫描）。
    """
    raw = data.get("技能", {}).get("项目技能目录")
    if isinstance(raw, list):
        return tuple(r for r in raw if isinstance(r, str) and r.strip())
    return None


# ═══════════════════════════════════════════════════════════════
# MCP 服务器条目解析
# ═══════════════════════════════════════════════════════════════

def _pick_value(entry: dict, *keys, default=None):
    """取第一个非 None 非空串的别名键值（中英文键兼容，中文键在前 = 中文优先）。"""
    for k in keys:
        v = entry.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_mcp_server(name: str, entry: dict) -> McpServerConfig | None:
    """解析一个 MCP 服务器配置项（spec「MCP 服务器参数解析」）。

    - `name` 空白或 `entry` 非 dict → None；
    - 键别名：参数/args、命令/command、环境变量/env、启动超时秒/startup_timeout_sec、
      工具超时秒/tool_timeout_sec、工具白名单/enabled_tools、工具黑名单/disabled_tools、
      启用/enabled、工作目录/cwd；
    - `command` 为数组时拆为 command + args（数组尾项在前，显式 args 在后）；
    - 字符串布尔按白名单解析（`"false"`/`"0"`/`"no"`/`"off"`/`"否"` → 未启用）；
    - 超时 ≤0 或非法 → 默认值。
    """
    if not isinstance(entry, dict) or not str(name).strip():
        return None

    args = _pick_value(entry, "参数", "args", default=[])
    if not isinstance(args, list):
        args = []

    # command 数组形态（Claude 配置 [python, main.py, ...]）归一为 command+args，
    # 使 AI 把其它客户端的配置原样贴进来也能直接连接
    command = _pick_value(entry, "命令", "command", default="")
    if isinstance(command, list):
        args = list(command[1:]) + list(args)
        command = command[0] if command else ""

    env = _pick_value(entry, "环境变量", "env", default={})
    if not isinstance(env, dict):
        env = {}

    startup_timeout = coerce_value(_pick_value(entry, "启动超时秒", "startup_timeout_sec"), int)
    if not startup_timeout or startup_timeout <= 0:
        startup_timeout = DEFAULT_MCP_STARTUP_TIMEOUT

    tool_timeout = coerce_value(_pick_value(entry, "工具超时秒", "tool_timeout_sec"), int)
    if not tool_timeout or tool_timeout <= 0:
        tool_timeout = DEFAULT_MCP_TOOL_TIMEOUT

    enabled_tools = _pick_value(entry, "工具白名单", "enabled_tools", default=[])
    if not isinstance(enabled_tools, list):
        enabled_tools = []

    disabled_tools = _pick_value(entry, "工具黑名单", "disabled_tools", default=[])
    if not isinstance(disabled_tools, list):
        disabled_tools = []

    # 布尔容错：AI 手写配置可能给字符串（"false"/"0"/"off"），不能 bool("false")=True
    enabled = _pick_value(entry, "启用", "enabled", default=True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("", "0", "false", "no", "off", "否")

    return McpServerConfig(
        name=str(name),
        command=str(command),
        args=tuple(args),
        env={str(k): str(v) for k, v in env.items()},
        cwd=str(_pick_value(entry, "工作目录", "cwd", default="")),
        enabled=bool(enabled),
        startup_timeout=startup_timeout,
        tool_timeout=tool_timeout,
        enabled_tools=tuple(str(t) for t in enabled_tools),
        disabled_tools=tuple(str(t) for t in disabled_tools),
    )


# ═══════════════════════════════════════════════════════════════
# 系统提示词
# ═══════════════════════════════════════════════════════════════

# narnat.md 子代理隐藏区块：<!-- subagent:hide --> ... <!-- /subagent:hide -->
# headless（nn -p）时整块剥离：子代理继承其余全部内容，唯独不感知子代理调度能力。
SUBAGENT_HIDE_RE = re.compile(
    r"<!--\s*subagent:hide\s*-->.*?<!--\s*/subagent:hide\s*-->",
    re.DOTALL,
)


def strip_subagent_hidden(md: str) -> str:
    """移除 narnat.md 中成对出现的 subagent:hide 区块（spec「headless 隐藏区块」）。

    只删除配对区块：单侧标记（只有开始或只有结束）原样保留。
    """
    return SUBAGENT_HIDE_RE.sub("", md)


def build_system_prompt(model: str, user_md: str, cwd: str = "", os_name: str = "") -> str:
    """拼接系统提示词 = 基础模板（模型/目录/平台）+ 换行 + 用户指令（非空时）。

    对应 spec Scenario「用户指令追加」。
    """
    parts = [BASE_PROMPT_TEMPLATE.format(
        model=model,
        cwd=cwd or os.getcwd(),
        platform=os_name or platform.system(),
    )]
    if user_md:
        parts.append(user_md)
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 分组构建
# ═══════════════════════════════════════════════════════════════

def build_ai_config(data: dict) -> AIConfig:
    """由「智能体」分组构建 AIConfig（含 retry_count，无二次重建）。

    解析风格逐项保持：`协议` 无白名单校验；`思考.启用/回传` 用 `bool()`；
    `上下文窗口大小` 显式 ≤0 保留原值；`目标模式最大轮数` 为 0/非法 → 默认、
    负值保留；`LLM重试次数` 裸 `int()`（非法值抛错）。
    """
    ai = data.get("智能体", {})

    protocol = ai.get("协议", DEFAULT_PROTOCOL)
    base_url = ai.get("接口地址", DEFAULT_BASE_URL)
    model, model_options = parse_model_config(ai.get("模型"))

    thinking_cfg = ai.get("思考", {})
    thinking_enabled = bool(thinking_cfg.get("启用", DEFAULT_THINKING_ENABLED))
    thinking_effort = thinking_cfg.get("强度", DEFAULT_THINKING_EFFORT)
    thinking_passback = bool(thinking_cfg.get("回传", DEFAULT_THINKING_PASSBACK))
    thinking_options = thinking_cfg.get("强度选项", dict(DEFAULT_THINKING_OPTIONS))

    # 上下文窗口：缺失/非法 → 默认；显式 ≤0 → 保留原值（下游视为无效，占比显示 --）
    parsed_cw = coerce_value(ai.get("上下文窗口大小"), int)
    context_window = DEFAULT_CONTEXT_WINDOW if parsed_cw is None else parsed_cw

    return AIConfig(
        api_key=ai.get("接口密钥", DEFAULT_API_KEY),
        base_url=base_url,
        model=model,
        model_options=model_options,
        protocol=protocol,
        temperature=coerce_value(ai.get("温度"), float),
        max_tokens=coerce_value(ai.get("最大输出token数"), int),
        thinking_enabled=thinking_enabled,
        thinking_effort=thinking_effort,
        thinking_passback=thinking_passback,
        thinking_options=thinking_options,
        context_window=context_window,
        retry_count=int(ai.get("LLM重试次数", DEFAULT_LLM_RETRY_COUNT)),
        goal_max_rounds=coerce_value(ai.get("目标模式最大轮数"), int) or DEFAULT_GOAL_MAX_ROUNDS,
    )


def _pop_bool_any(d: dict, *keys) -> bool:
    """弹出第一个命中的开关键并按 `bool()` 定型（keys 顺序即别名优先级）。"""
    for k in keys:
        if k in d:
            return bool(d.pop(k))
    return False


def _pop_int_any(d: dict, *, keys: tuple = (), default: int = 0) -> int:
    """弹出第一个命中的整数键（裸 `int()`：非法值抛错，保持现状）。"""
    for k in keys:
        if k in d:
            return int(d.pop(k))
    return default


def build_ui_config(data: dict, max_output_tokens: int = DEFAULT_UI_MAX_OUTPUT_TOKENS) -> UIConfig:
    """由「界面」分组构建 UIConfig，支持全中文键（spec/output「中文键名与英文别名」）。

    处理顺序（保持现状）：
    1. 顶层开关弹出（中英均可；同时存在时英文键优先）；
    2. 分组名中→英（仅当英文键不存在时迁移；已存在时中文键原样保留，中文别名先到者胜）；
    3. 分组内键名中→英（同键直赋，英文键已存在时覆盖）；
    4. 旧扁平键迁移到新分组（直赋迁入，目标位置已有值时覆盖）；
    5. 配方值中的中文色名逐项替换（键序固定，见 COLOR_ZH_EN 注释）。
    """
    ui = data.get("界面", {})
    raw = dict(ui)

    # 1. 顶层开关字段（中英均可，提取后从 raw 清理；同时存在时英文键优先）
    show_cost = _pop_bool_any(raw, "show_cost", "显示费用")
    show_balance = _pop_bool_any(raw, "show_balance", "显示余额")
    max_tokens = _pop_int_any(raw, keys=("max_output_tokens", "最大输出token数"),
                              default=max_output_tokens)

    # 2. 分组名称中→英（仅当英文键不存在时迁移；英文键已存在时中文键原样保留）
    for zh, en in SECTION_MAP.items():
        if zh in raw and en not in raw:
            raw[en] = raw.pop(zh)

    # 3. 每个分组内部键名中→英
    for section, key_map in KEY_MAPS.items():
        if section not in raw:
            continue
        sec = raw[section]
        if not isinstance(sec, dict):
            continue
        for zh, en in key_map.items():
            if zh in sec:
                sec[en] = sec.pop(zh)

    # 4. 兼容旧扁平键（"用户输入色" 等；直接赋值迁入——目标位置已有值时覆盖）
    for old_key, (section, new_key) in OLD_COLOR_MAP.items():
        if old_key in raw:
            raw.setdefault(section, {})[new_key] = raw.pop(old_key)

    # 5. 配方值中的中文色名 → 英文（如 "bold 强调色" → "bold emphasis色"，见怪癖）
    for section_name in UI_RECIPE_SECTIONS:
        sec = raw.get(section_name)
        if not isinstance(sec, dict):
            continue
        for k, v in list(sec.items()):
            if isinstance(v, str):
                for zh, en in COLOR_ZH_EN.items():
                    v = v.replace(zh, en)
                sec[k] = v

    return UIConfig(raw=raw, show_cost=show_cost, show_balance=show_balance,
                    max_output_tokens=max_tokens)


def build_pricing_config(data: dict) -> PricingConfig:
    """由「定价」分组构建 PricingConfig（空/缺失 → 默认）。"""
    pricing_group = data.get("定价", {})
    if not pricing_group:
        return PricingConfig()
    raw_pricing = pricing_group.get("模型", {})
    user_pricing = parse_pricing(raw_pricing) if raw_pricing else {}
    return PricingConfig(user_pricing=user_pricing)


def build_balance_config(data: dict) -> BalanceConfig:
    """由「余额查询」分组构建 BalanceConfig（`启用` 用 `bool()`，字符串布尔怪癖保持）。"""
    bal = data.get("余额查询", {})
    return BalanceConfig(
        enabled=bool(bal.get("启用", False)),
        url=bal.get("查询地址", ""),
        auth_method=bal.get("认证方式", DEFAULT_BALANCE_AUTH_METHOD),
        value_path=bal.get("响应路径", ""),
        currency_path=bal.get("货币路径", ""),
    )


def build_cost_log_config(data: dict, data_dir: str) -> CostLogConfig:
    """由「费用日志」分组构建 CostLogConfig（spec Scenario「费用日志容量」）。

    非 dict 分组按空处理；`输出文件` 为空 → `<data_dir>/cost_log.csv`；
    `最大容量MB` 非法 → 50MB；负值 → 0（不轮转）；换算 = MB×1024×1024。
    """
    cfg = data.get("费用日志", {})
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = bool(cfg.get("启用", False))
    path = cfg.get("输出文件") or os.path.join(data_dir, COST_LOG_FILENAME)
    max_mb = cfg.get("最大容量MB")
    try:
        max_mb = int(max_mb)
    except (TypeError, ValueError):
        max_mb = DEFAULT_COST_LOG_MAX_MB
    if max_mb < 0:
        max_mb = 0
    max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0
    return CostLogConfig(enabled=enabled, path=path, max_bytes=max_bytes)
