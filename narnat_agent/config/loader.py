"""
配置加载器 —— 读取 narnat.json + narnat.md，拼接系统prompt
"""

import json
import os
import sys
import platform
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from .defaults import (
    BASE_PROMPT_TEMPLATE, COMPRESS_PROMPT,
    NARNAT_DIR, NARNAT_JSON, NARNAT_MD, LAST_SESSION_SUMMARY,
    CONFIG_SUBDIR, DATA_SUBDIR, LOGS_SUBDIR,
    DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL,
    DEFAULT_PROTOCOL, DEFAULT_THINKING_ENABLED, DEFAULT_THINKING_EFFORT,
    WARN_TURN_1, WARN_TURN_2, COMPRESS_TURN,
    DEFAULT_GIT_SKIP, DEFAULT_RM_SKIP,
    DEFAULT_REQUIRE_PLAN, DEFAULT_MIN_TOOLS,
    DEFAULT_MAX_TOOL_OUTPUT_KB,
    DEFAULT_AUTO_SAVE,
)


# ── 默认忽略目录 ──
_DEFAULT_IGNORE_DIRS = [".git", "__pycache__", "node_modules", ".svn", ".hg", "venv", ".venv", ".pytest_cache"]


@dataclass
class AIConfig:
    """AI连接配置"""
    api_key: str = DEFAULT_API_KEY
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    protocol: str = DEFAULT_PROTOCOL              # "openai" | "anthropic"
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    thinking_enabled: bool = DEFAULT_THINKING_ENABLED
    thinking_effort: str = DEFAULT_THINKING_EFFORT
    thinking_options: dict = field(default_factory=lambda: {"high": "高", "max": "全开"})


@dataclass
class PricingConfig:
    """定价配置"""
    # 用户自定义定价（中文key映射到英文key）
    # 格式: {"模型名": {"输入": x, "缓存命中": y, "输出": z}}
    user_pricing: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class BalanceConfig:
    """余额查询配置"""
    enabled: bool = False
    url: str = ""                    # 查询地址
    auth_method: str = "bearer"      # "bearer" | "x-api-key"
    value_path: str = ""             # 余额数值 JSONPath
    currency_path: str = ""          # 货币单位 JSONPath


@dataclass
class UIConfig:
    """UI配置（原style.json内容）"""
    max_output_tokens: int = 128000
    show_cost: bool = False
    show_balance: bool = False
    # 颜色配置（hex字符串）
    colors: Dict[str, str] = field(default_factory=lambda: {
        "用户输入色": "#FFFFFF",
        "AI输出色": "#D8DEE9",
        "标题色": "#5EEAD4",
        "成功色": "#A3BE8C",
        "行内代码色": "#EBCB8B",
        "错误色": "#BF616A",
        "链接色": "#81A1C1",
        "装饰色": "#B48EAD",
        "加载动画色": "#D08770",
        "次要文字色": "#4C566A",
        "代码块背景色": "#161821",
    })


@dataclass
class AppConfig:
    """应用总配置"""
    ai: AIConfig = field(default_factory=AIConfig)
    api_keys: dict = field(default_factory=dict)
    system_prompt: str = ""
    narnat_dir: str = ""
    project_root: str = ""
    # 子目录路径（由 load_config 计算）
    config_dir: str = ""    # .narnat/config/
    data_dir: str = ""      # .narnat/data/
    logs_dir: str = ""      # .narnat/logs/
    # 新增配置项
    pricing: PricingConfig = field(default_factory=PricingConfig)
    balance: BalanceConfig = field(default_factory=BalanceConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    compress_turn: int = COMPRESS_TURN
    warn_turn_1: int = WARN_TURN_1
    warn_turn_2: int = WARN_TURN_2
    ignore_dirs: List[str] = field(default_factory=lambda: list(_DEFAULT_IGNORE_DIRS))
    ssh_max_sessions: int = 5
    max_transfer_mb: int = 100
    llm_retry_count: int = 3
    git_skip_confirm: bool = DEFAULT_GIT_SKIP
    rm_skip_confirm: bool = DEFAULT_RM_SKIP
    require_plan: bool = DEFAULT_REQUIRE_PLAN
    min_tools: int = DEFAULT_MIN_TOOLS
    max_tool_output_kb: int = DEFAULT_MAX_TOOL_OUTPUT_KB
    auto_save: bool = DEFAULT_AUTO_SAVE


def _is_nuitka_onefile() -> bool:
    """检测是否运行在 Nuitka onefile 模式下。
    
    Nuitka onefile 不设置 sys.frozen，sys.executable 指向临时解压目录的 python.exe，
    临时目录路径通常包含 "onefile_" 。
    """
    exe_dir = os.path.dirname(sys.executable)
    exe_name = os.path.basename(sys.executable).lower()
    if "onefile_" in exe_dir and exe_name in ("python.exe", "python", "python3"):
        return True
    if "__compiled__" in dir(sys.modules.get("__main__", type(None))):
        return True
    return False


def _find_narnat_exe_dir() -> Optional[str]:
    """通过 PATH 查找 narnat.exe 所在目录。"""
    import shutil
    for name in ("narnat.exe", "narnat"):
        found = shutil.which(name)
        if found:
            return os.path.dirname(os.path.abspath(found))
    return None


def _find_project_root() -> str:
    # 1. 环境变量优先级最高，允许用户显式指定
    env_home = os.environ.get("NARNAT_HOME")
    if env_home and os.path.isdir(os.path.join(env_home, NARNAT_DIR)):
        return env_home

    # 2. Nuitka onefile 模式
    if _is_nuitka_onefile():
        exe_dir = _find_narnat_exe_dir()
        if exe_dir and os.path.isdir(os.path.join(exe_dir, NARNAT_DIR)):
            return exe_dir
        if exe_dir:
            return exe_dir
        return os.path.dirname(sys.executable)

    # 3. PyInstaller / Nuitka standalone 模式
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(exe_dir, NARNAT_DIR)):
            return exe_dir
        return exe_dir

    # 4. 开发模式：从 cwd 向上查找
    cwd = os.getcwd()
    candidate = cwd
    for _ in range(10):
        if os.path.isdir(os.path.join(candidate, NARNAT_DIR)):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return cwd


def _coerce(v, target_type):
    """字符串/数字 → target_type，空串/非法值 → None"""
    if v in (None, ""):
        return None
    try:
        return target_type(v)
    except (TypeError, ValueError):
        return None


def _parse_pricing(data: dict) -> Dict[str, Dict[str, float]]:
    """解析用户定价配置，中文key映射到英文key

    用户配置格式: {"模型名": {"输入": x, "缓存命中": y, "输出": z}}
    内部格式: {"模型名": {"input": x, "cache_hit": y, "output": z}}
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


def _load_json(config_dir: str) -> dict:
    """读取 narnat.json，返回原始数据字典。解析失败返回空字典"""
    path = os.path.join(config_dir, NARNAT_JSON)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}



def _build_ai_config(data: dict) -> AIConfig:
    """从narnat.json的"智能体"分组构建AIConfig"""
    ai = data.get("智能体", {})

    protocol = ai.get("协议", DEFAULT_PROTOCOL)

    thinking_cfg = ai.get("思考", {})
    thinking_enabled = bool(thinking_cfg.get("启用", DEFAULT_THINKING_ENABLED))
    thinking_effort = thinking_cfg.get("强度", DEFAULT_THINKING_EFFORT)
    thinking_options = thinking_cfg.get("强度选项", {"high": "高", "max": "全开"})

    return AIConfig(
        api_key=ai.get("接口密钥", DEFAULT_API_KEY),
        base_url=ai.get("接口地址", DEFAULT_BASE_URL),
        model=ai.get("模型", DEFAULT_MODEL),
        protocol=protocol,
        temperature=_coerce(ai.get("温度"), float),
        max_tokens=_coerce(ai.get("最大输出token数"), int),
        thinking_enabled=thinking_enabled,
        thinking_effort=thinking_effort,
        thinking_options=thinking_options,
    )


def _build_ui_config(data: dict, max_output_tokens: int = 128000) -> UIConfig:
    """从narnat.json的"界面"分组构建UIConfig"""
    ui = data.get("界面", {})

    colors = {}
    color_keys = [
        "用户输入色", "AI输出色", "标题色", "成功色", "行内代码色",
        "错误色", "链接色", "装饰色", "加载动画色", "次要文字色", "代码块背景色",
    ]
    for key in color_keys:
        if key in ui:
            colors[key] = ui[key]

    return UIConfig(
        max_output_tokens=max_output_tokens,
        show_cost=bool(ui.get("显示费用", False)),
        show_balance=bool(ui.get("显示余额", False)),
        colors=colors,
    )


def _build_pricing_config(data: dict) -> PricingConfig:
    """从narnat.json的"定价"分组构建PricingConfig"""
    pricing_group = data.get("定价", {})
    if not pricing_group:
        return PricingConfig()
    raw_pricing = pricing_group.get("模型", {})
    user_pricing = _parse_pricing(raw_pricing) if raw_pricing else {}
    return PricingConfig(user_pricing=user_pricing)


def _build_balance_config(data: dict) -> BalanceConfig:
    """从narnat.json的"余额查询"分组构建BalanceConfig"""
    bal = data.get("余额查询", {})
    return BalanceConfig(
        enabled=bool(bal.get("启用", False)),
        url=bal.get("查询地址", ""),
        auth_method=bal.get("认证方式", "bearer"),
        value_path=bal.get("响应路径", ""),
        currency_path=bal.get("货币路径", ""),
    )


def _load_user_md(config_dir: str) -> str:
    """读取 narnat.md 用户自定义指令，不存在或为空返回空串"""
    path = os.path.join(config_dir, NARNAT_MD)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _build_system_prompt(model: str, user_md: str, cwd: str = "", os_name: str = "", shell_name: str = "") -> str:
    """拼接系统prompt：基础prompt + 用户自定义"""
    parts = [BASE_PROMPT_TEMPLATE.format(
        model=model,
        cwd=cwd or os.getcwd(),
        platform=os_name or platform.system(),
        shell=shell_name or ("PowerShell" if sys.platform == "win32" else "bash"),
    )]
    if user_md:
        parts.append(user_md)
    return "\n".join(parts)


def load_config(project_root: Optional[str] = None) -> AppConfig:
    """
    加载全部配置。

    1. 定位项目根目录（含 .narnat 的目录）
    2. 创建 .narnat 子目录结构（config/ data/ logs/）
    3. 读取 narnat.json → 全部配置
    4. 读取 narnat.md → 用户自定义指令
    5. 拼接系统prompt
    """
    root = os.path.abspath(project_root or _find_project_root())
    narnat_dir = os.path.join(root, NARNAT_DIR)
    config_dir = os.path.join(narnat_dir, CONFIG_SUBDIR)
    data_dir = os.path.join(narnat_dir, DATA_SUBDIR)
    logs_dir = os.path.join(narnat_dir, LOGS_SUBDIR)

    # 确保 .narnat 及子目录存在
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    # 确保关键配置文件存在
    for fname in (NARNAT_JSON, NARNAT_MD):
        fpath = os.path.join(config_dir, fname)
        if not os.path.isfile(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                if fname == NARNAT_JSON:
                    json.dump({
                        "智能体": {
                            "接口密钥": DEFAULT_API_KEY,
                            "接口地址": DEFAULT_BASE_URL,
                            "模型": DEFAULT_MODEL,
                            "协议": "anthropic",
                            "温度": None,
                            "最大输出token数": 128000,
                            "思考": {
                                "启用": True,
                                "强度": "high",
                                "强度选项": {"high": "高", "max": "全开"},
                            },
                            "LLM重试次数": 3,
                        },
                        "余额查询": {
                            "启用": True,
                            "查询地址": "https://api.deepseek.com/user/balance",
                            "认证方式": "bearer",
                            "响应路径": "balance_infos.0.total_balance",
                            "货币路径": "balance_infos.0.currency",
                        },
                        "接口密钥组": {"websearch": "", "websearch_url": "https://api.anysearch.com/mcp"},
                        "定价": {"模型": {}},
                        "界面": {},
                        "工具": {"输出上限KB": DEFAULT_MAX_TOOL_OUTPUT_KB},
                        "会话": {},
                        "压缩": {},
                        "计划": {},
                        "忽略目录": _DEFAULT_IGNORE_DIRS,
                    }, f, indent=2, ensure_ascii=False)
                else:
                    f.write("")

    # 确保压缩摘要文件存在
    summary_path = os.path.join(data_dir, LAST_SESSION_SUMMARY)
    if not os.path.isfile(summary_path):
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write("")

    # 读取配置
    data = _load_json(config_dir)

    # 构建各子配置
    ai_config = _build_ai_config(data)
    api_keys = data.get("接口密钥组", {})
    pricing_config = _build_pricing_config(data)
    balance_config = _build_balance_config(data)
    ui_config = _build_ui_config(data, ai_config.max_tokens or 128000)

    # 读取用户自定义指令
    user_md = _load_user_md(config_dir)
    system_prompt = _build_system_prompt(
        model=ai_config.model,
        user_md=user_md,
        cwd=os.getcwd(),
        os_name=platform.system(),
        shell_name="PowerShell" if sys.platform == "win32" else "bash",
    )

    return AppConfig(
        ai=ai_config,
        api_keys=api_keys,
        system_prompt=system_prompt,
        narnat_dir=narnat_dir,
        project_root=root,
        config_dir=config_dir,
        data_dir=data_dir,
        logs_dir=logs_dir,
        pricing=pricing_config,
        balance=balance_config,
        ui=ui_config,
        compress_turn=int(data.get("压缩", {}).get("压缩轮次", COMPRESS_TURN)),
        warn_turn_1=int(data.get("压缩", {}).get("警告轮次1", WARN_TURN_1)),
        warn_turn_2=int(data.get("压缩", {}).get("警告轮次2", WARN_TURN_2)),
        ignore_dirs=data.get("忽略目录", list(_DEFAULT_IGNORE_DIRS)),
        ssh_max_sessions=int(data.get("工具", {}).get("SSH最大会话数", 5)),
        max_transfer_mb=int(data.get("工具", {}).get("最大传输文件MB", 100)),
        llm_retry_count=int(data.get("智能体", {}).get("LLM重试次数", 3)),
        git_skip_confirm=bool(data.get("工具", {}).get("git免确认", DEFAULT_GIT_SKIP)),
        rm_skip_confirm=bool(data.get("工具", {}).get("rm免确认", DEFAULT_RM_SKIP)),
        require_plan=bool(data.get("计划", {}).get("计划优先", DEFAULT_REQUIRE_PLAN)),
        min_tools=int(data.get("计划", {}).get("计划最低工具数", DEFAULT_MIN_TOOLS)),
        max_tool_output_kb=int(data.get("工具", {}).get("输出上限KB", DEFAULT_MAX_TOOL_OUTPUT_KB)),
        auto_save=bool(data.get("会话", {}).get("自动保存", DEFAULT_AUTO_SAVE)),
    )
