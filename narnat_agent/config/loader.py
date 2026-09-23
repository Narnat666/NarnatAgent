"""配置加载主流程 —— `.narnat` 目录定位、首次生成默认文件、读取并组装配置树。

契约来源：`openspec/changes/recast-v2/specs/config/spec.md`
（「`.narnat` 目录发现」「首次运行初始化」「磁盘路径布局」）。
解析细节在 `parsers`：本模块只负责"找目录 → 生成文件 → 读文件 → 组装"，
单位换算在组装时一次完成（外部直接用最终单位）。

目录发现优先级（spec「`.narnat` 目录发现」）：
① 环境变量 `NARNAT_HOME`（仅当其下存在 `.narnat/` 时生效）；
② 打包运行态（Nuitka onefile / PyInstaller）用可执行文件所在目录；
③ 开发态从当前工作目录向上最多 10 层查找含 `.narnat/` 的目录，找不到退回 cwd。
"""
from __future__ import annotations

import json
import os
import platform
import sys
from typing import Optional

from .defaults import (
    CONFIG_SUBDIR,
    DATA_SUBDIR,
    DEFAULT_AUTO_SAVE,
    DEFAULT_AUTO_SAVE_TOKENS,
    DEFAULT_COMPRESS_RATIO,
    DEFAULT_COMPRESS_RETAIN_TOKENS,
    DEFAULT_GIT_SKIP,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOOL_OUTPUT_KB,
    DEFAULT_MIN_TOOLS,
    DEFAULT_REQUIRE_PLAN,
    DEFAULT_RM_SKIP,
    DEFAULT_SHOW_RATIO,
    DEFAULT_TOOL_MAX_SESSIONS,
    DEFAULT_TOOL_MAX_TRANSFER_MB,
    DEFAULT_UI_MAX_OUTPUT_TOKENS,
    DEFAULT_WARN_RATIO,
    LOGS_SUBDIR,
    NARNAT_DIR,
    NARNAT_JSON,
    NARNAT_MD,
    build_first_run_config,
)
from .models import (
    Config,
    PathConfig,
    PlanConfig,
    SafetyConfig,
    SessionConfig,
    SkillConfig,
    ToolConfig,
)
from .parsers import (
    build_ai_config,
    build_balance_config,
    build_cost_log_config,
    build_pricing_config,
    build_system_prompt,
    build_ui_config,
    coerce_value,
    parse_project_skill_roots,
    parse_token_amount,
    strip_subagent_hidden,
)

__all__ = [
    "find_narnat_exe_dir",
    "find_project_root",
    "is_nuitka_onefile",
    "load_config",
]

# 开发态向上查找 `.narnat` 的最大层数（spec：向上最多 10 层）。
PROJECT_ROOT_MAX_UP = 10


def is_nuitka_onefile() -> bool:
    """检测是否运行在 Nuitka onefile 模式下（spec「`.narnat` 目录发现」第②分支的判定）。

    Nuitka onefile 不设置 sys.frozen，sys.executable 指向临时解压目录的 python.exe，
    临时目录路径通常包含 "onefile_"。
    """
    exe_dir = os.path.dirname(sys.executable)
    exe_name = os.path.basename(sys.executable).lower()
    if "onefile_" in exe_dir and exe_name in ("python.exe", "python", "python3"):
        return True
    if "__compiled__" in dir(sys.modules.get("__main__", type(None))):
        return True
    return False


def find_narnat_exe_dir() -> Optional[str]:
    """获取真实 exe 所在目录（Nuitka onefile 下指打包前的原始 exe 位置）。

    定位顺序（由可靠到不可靠）：
    1. Windows: GetModuleFileNameW —— 内核返回模块真实路径，
       不受 argv[0] 影响。PATH 裸名调用（`nn`）时 argv[0]="nn"，
       仅靠它会把项目根错定位到 onefile 临时解压目录。
    2. argv[0] 为存在的完整路径（直接 `D:\\x\\nn.exe` 调用）。
    3. argv[0] 为裸名：用 PATH 搜索解析（shutil.which）。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 1024)
            if n and buf.value:
                path = buf.value
                if os.path.isfile(path):
                    return os.path.dirname(path)
        except Exception:
            pass

    argv0 = sys.argv[0]
    if argv0 and os.path.isfile(argv0):
        return os.path.dirname(os.path.abspath(argv0))
    if argv0 and not os.path.dirname(argv0):
        try:
            import shutil
            resolved = shutil.which(argv0)
            if resolved and os.path.isfile(resolved):
                return os.path.dirname(os.path.abspath(resolved))
        except Exception:
            pass
    return None


def find_project_root() -> str:
    """按固定优先级定位项目根（spec「`.narnat` 目录发现」）。

    ① `NARNAT_HOME`（其下须存在 `.narnat/`，否则忽略该变量继续回退）；
    ② Nuitka onefile / PyInstaller 打包运行态 → 可执行文件所在目录；
    ③ 开发态：cwd 向上最多 10 层找含 `.narnat/` 的目录，找不到退回 cwd。
    """
    # 1. 环境变量优先级最高，允许用户显式指定
    env_home = os.environ.get("NARNAT_HOME")
    if env_home and os.path.isdir(os.path.join(env_home, NARNAT_DIR)):
        return env_home

    # 2. Nuitka onefile 模式
    if is_nuitka_onefile():
        exe_dir = find_narnat_exe_dir()
        if exe_dir and os.path.isdir(os.path.join(exe_dir, NARNAT_DIR)):
            return exe_dir
        if exe_dir:
            return exe_dir
        return os.path.dirname(sys.executable)

    # 3. PyInstaller / Nuitka standalone 模式
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(exe_dir, NARNAT_DIR)):
            return exe_dir
        return exe_dir

    # 4. 开发模式：从 cwd 向上查找
    cwd = os.getcwd()
    candidate = cwd
    for _ in range(PROJECT_ROOT_MAX_UP):
        if os.path.isdir(os.path.join(candidate, NARNAT_DIR)):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return cwd


def _ensure_default_files(config_dir: str) -> None:
    """首次运行时生成默认 `narnat.json` / 空 `narnat.md`（spec「首次运行初始化」）。

    文件已存在则不覆盖、不补写任何键。
    """
    json_path = os.path.join(config_dir, NARNAT_JSON)
    if not os.path.isfile(json_path):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(build_first_run_config(), f, indent=2, ensure_ascii=False)
    md_path = os.path.join(config_dir, NARNAT_MD)
    if not os.path.isfile(md_path):
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("")


def _load_json(config_dir: str) -> dict:
    """读取 narnat.json 原始数据；文件缺失/非法 JSON/读取失败 → 空字典（静默回退全默认）。

    与现状一致：顶层非 dict 的内容不做类型校验（下游按 dict 使用）。
    """
    path = os.path.join(config_dir, NARNAT_JSON)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_user_md(config_dir: str) -> str:
    """读取 narnat.md 用户自定义指令，不存在或读取失败返回空串（strip 后返回）。"""
    path = os.path.join(config_dir, NARNAT_MD)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def load_config(project_root: Optional[str] = None, headless: bool = False) -> Config:
    """加载全部配置，返回一次性构建的 Config 对象树。

    1. 定位项目根目录（含 .narnat 的目录）
    2. 创建 .narnat/config/ 与 .narnat/data/（logs 目录由日志模块在调试模式下按需创建）
    3. 缺失时生成默认 narnat.json 与空 narnat.md
    4. 读取 narnat.json → 全部配置（含单位换算）
    5. 读取 narnat.md → 用户自定义指令（headless 时剥离 subagent:hide 区块）
    6. 拼接系统提示词
    """
    root = os.path.abspath(project_root or find_project_root())
    narnat_dir = os.path.join(root, NARNAT_DIR)
    config_dir = os.path.join(narnat_dir, CONFIG_SUBDIR)
    data_dir = os.path.join(narnat_dir, DATA_SUBDIR)
    logs_dir = os.path.join(narnat_dir, LOGS_SUBDIR)

    # 确保 .narnat 及子目录存在（logs 目录由 logger.start() 在 debug 模式下按需创建）
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    # 确保关键配置文件存在
    _ensure_default_files(config_dir)

    # 读取配置
    data = _load_json(config_dir)

    # 构建各子配置
    ai_config = build_ai_config(data)
    api_keys = data.get("接口密钥组", {})
    pricing_config = build_pricing_config(data)
    balance_config = build_balance_config(data)
    cost_log_config = build_cost_log_config(data, data_dir)
    ui_config = build_ui_config(data, ai_config.max_tokens or DEFAULT_UI_MAX_OUTPUT_TOKENS)

    # 读取用户自定义指令（headless 时剥离 subagent:hide 区块）
    user_md = _load_user_md(config_dir)
    if headless:
        user_md = strip_subagent_hidden(user_md)
    system_prompt = build_system_prompt(
        model=ai_config.model,
        user_md=user_md,
        cwd=os.getcwd(),
        os_name=platform.system(),
    )

    # ── 单位转换在此完成 ──
    max_output_kb = int(data.get("工具", {}).get("输出上限KB", DEFAULT_MAX_TOOL_OUTPUT_KB))
    max_output_chars = max_output_kb * 1024 if max_output_kb > 0 else 0

    # 压缩保留尾部：0 是合法值（关闭保留），不能用 `or` 兜底；负数按 0 处理
    retain_raw = coerce_value(data.get("压缩", {}).get("保留尾部"), int)
    compress_retain = (DEFAULT_COMPRESS_RETAIN_TOKENS if retain_raw is None
                       else max(0, retain_raw))

    return Config(
        ai=ai_config,
        paths=PathConfig(
            project_root=root,
            narnat_dir=narnat_dir,
            config_dir=config_dir,
            data_dir=data_dir,
            logs_dir=logs_dir,
        ),
        tools=ToolConfig(
            max_sessions=int(data.get("工具", {}).get("SSH最大会话数", DEFAULT_TOOL_MAX_SESSIONS)),
            max_transfer_mb=int(data.get("工具", {}).get("最大传输文件MB", DEFAULT_TOOL_MAX_TRANSFER_MB)),
            max_output_chars=max_output_chars,
            max_timeout_seconds=int(data.get("工具", {}).get("超时上限秒", DEFAULT_MAX_TIMEOUT_SECONDS)),
            ignore_dirs=tuple(data.get("忽略目录") or []),
        ),
        safety=SafetyConfig(
            git_skip_confirm=bool(data.get("工具", {}).get("git免确认", DEFAULT_GIT_SKIP)),
            rm_skip_confirm=bool(data.get("工具", {}).get("rm免确认", DEFAULT_RM_SKIP)),
        ),
        plan=PlanConfig(
            require_plan=bool(data.get("计划", {}).get("计划优先", DEFAULT_REQUIRE_PLAN)),
            min_tools=int(data.get("计划", {}).get("计划最低工具数", DEFAULT_MIN_TOOLS)),
        ),
        session=SessionConfig(
            auto_save=bool(data.get("会话", {}).get("自动保存", DEFAULT_AUTO_SAVE)),
            auto_save_tokens=parse_token_amount(
                data.get("会话", {}).get("自动保存Token量"), DEFAULT_AUTO_SAVE_TOKENS),
            show_ratio=bool(data.get("压缩", {}).get("占比显示", DEFAULT_SHOW_RATIO)),
            warn_ratio=coerce_value(data.get("压缩", {}).get("告警"), int) or DEFAULT_WARN_RATIO,
            compress_ratio=coerce_value(data.get("压缩", {}).get("压缩"), int) or DEFAULT_COMPRESS_RATIO,
            retain_tokens=compress_retain,
        ),
        skills=SkillConfig(project_roots=parse_project_skill_roots(data)),
        pricing=pricing_config,
        balance=balance_config,
        cost_log=cost_log_config,
        ui=ui_config,
        api_keys=api_keys,
        system_prompt=system_prompt,
    )
