"""
配置加载器 —— 读取 narnat.json + narnat.md，拼接系统prompt
"""

import json
import os
import sys
import platform
from dataclasses import dataclass, field
from typing import Optional

from .defaults import (
    BASE_PROMPT_TEMPLATE, IRON_RULES, COMPRESS_PROMPT,
    NARNAT_DIR, NARNAT_JSON, NARNAT_MD, LAST_SESSION_SUMMARY,
    DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL,
)


@dataclass
class AIConfig:
    """AI连接配置"""
    api_key: str = DEFAULT_API_KEY
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL


@dataclass
class AppConfig:
    """应用总配置"""
    ai: AIConfig = field(default_factory=AIConfig)
    api_keys: dict = field(default_factory=dict)
    system_prompt: str = ""
    narnat_dir: str = ""
    project_root: str = ""


def _is_nuitka_onefile() -> bool:
    """检测是否运行在 Nuitka onefile 模式下。
    
    Nuitka onefile 不设置 sys.frozen，sys.executable 指向临时解压目录的 python.exe，
    临时目录路径通常包含 "onefile_" 。
    """
    exe_dir = os.path.dirname(sys.executable)
    # Nuitka onefile 临时目录特征：路径含 "onefile_" 且 sys.executable 是 python 解释器
    exe_name = os.path.basename(sys.executable).lower()
    if "onefile_" in exe_dir and exe_name in ("python.exe", "python", "python3"):
        return True
    # 也检查 __compiled__ 属性（Nuitka 设置的）
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

    # 2. Nuitka onefile 模式：sys.executable 指向临时目录，需要通过 PATH 定位真实 exe
    if _is_nuitka_onefile():
        exe_dir = _find_narnat_exe_dir()
        if exe_dir and os.path.isdir(os.path.join(exe_dir, NARNAT_DIR)):
            return exe_dir
        # 找不到 .narnat 也返回 exe_dir，避免在 cwd 下创建配置
        if exe_dir:
            return exe_dir
        # PATH 也找不到，fallback 到临时目录
        return os.path.dirname(sys.executable)

    # 3. PyInstaller / Nuitka standalone 模式：sys.frozen=True，sys.executable 指向真实 exe
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


def _load_json(narnat_dir: str) -> tuple[AIConfig, dict]:
    """读取 narnat.json，返回 (AIConfig, api_keys)。解析失败返回默认配置"""
    path = os.path.join(narnat_dir, NARNAT_JSON)
    if not os.path.isfile(path):
        return AIConfig(), {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ai_config = AIConfig(
            api_key=data.get("api_key", DEFAULT_API_KEY),
            base_url=data.get("base_url", DEFAULT_BASE_URL),
            model=data.get("model", DEFAULT_MODEL),
        )
        api_keys = data.get("api_keys", {})
        return ai_config, api_keys
    except (json.JSONDecodeError, OSError):
        return AIConfig(), {}


def _load_user_md(narnat_dir: str) -> str:
    """读取 narnat.md 用户自定义指令，不存在或为空返回空串"""
    path = os.path.join(narnat_dir, NARNAT_MD)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _build_system_prompt(model: str, user_md: str, cwd: str = "", os_name: str = "", shell_name: str = "") -> str:
    """拼接系统prompt：基础prompt + 铁律 + 用户自定义"""
    parts = [BASE_PROMPT_TEMPLATE.format(
        model=model,
        cwd=cwd or os.getcwd(),
        platform=os_name or platform.system(),
        shell=shell_name or ("PowerShell" if sys.platform == "win32" else "bash"),
    ), IRON_RULES]
    if user_md:
        parts.append(user_md)
    return "\n".join(parts)


def load_config(project_root: Optional[str] = None) -> AppConfig:
    """
    加载全部配置。

    1. 定位项目根目录（含 .narnat 的目录）
    2. 读取 narnat.json → AIConfig
    3. 读取 narnat.md → 用户自定义指令
    4. 拼接系统prompt
    """
    root = os.path.abspath(project_root or _find_project_root())
    narnat_dir = os.path.join(root, NARNAT_DIR)

    # 确保 .narnat 目录存在
    os.makedirs(narnat_dir, exist_ok=True)

    # 确保关键文件存在
    for fname in (NARNAT_JSON, NARNAT_MD, LAST_SESSION_SUMMARY):
        fpath = os.path.join(narnat_dir, fname)
        if not os.path.isfile(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                if fname == NARNAT_JSON:
                    json.dump({
                        "api_key": DEFAULT_API_KEY,
                        "base_url": DEFAULT_BASE_URL,
                        "model": DEFAULT_MODEL,
                        "api_keys": {
                            "anysearch": ""
                        },
                    }, f, indent=2, ensure_ascii=False)
                else:
                    f.write("")

    ai_config, api_keys = _load_json(narnat_dir)
    user_md = _load_user_md(narnat_dir)
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
    )
