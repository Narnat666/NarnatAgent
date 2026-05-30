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
    system_prompt: str = ""
    narnat_dir: str = ""
    project_root: str = ""


def _find_project_root() -> str:
    """从当前目录向上查找包含 .narnat 的目录，找不到则用当前目录"""
    cwd = os.getcwd()
    candidate = cwd
    for _ in range(10):  # 最多向上查10层
        if os.path.isdir(os.path.join(candidate, NARNAT_DIR)):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return cwd


def _load_json(narnat_dir: str) -> AIConfig:
    """读取 narnat.json，解析失败返回默认配置"""
    path = os.path.join(narnat_dir, NARNAT_JSON)
    if not os.path.isfile(path):
        return AIConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return AIConfig(
            api_key=data.get("api_key", DEFAULT_API_KEY),
            base_url=data.get("base_url", DEFAULT_BASE_URL),
            model=data.get("model", DEFAULT_MODEL),
        )
    except (json.JSONDecodeError, OSError):
        return AIConfig()


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
                    }, f, indent=2, ensure_ascii=False)
                else:
                    f.write("")

    ai_config = _load_json(narnat_dir)
    user_md = _load_user_md(narnat_dir)
    system_prompt = _build_system_prompt(
        model=ai_config.model,
        user_md=user_md,
        cwd=root,
        os_name=platform.system(),
        shell_name="PowerShell" if sys.platform == "win32" else "bash",
    )

    return AppConfig(
        ai=ai_config,
        system_prompt=system_prompt,
        narnat_dir=narnat_dir,
        project_root=root,
    )
