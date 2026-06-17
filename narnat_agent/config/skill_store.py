"""
技能加载 —— 从 .narnat/config/skills/ 目录读取技能文件
"""

import os
from typing import List

from .defaults import CONFIG_SUBDIR


def load_skill(narnat_dir: str, name: str) -> tuple:
    """加载技能内容。返回 (content, error)。
    
    查找顺序:
    1. config/skills/<name>.md       (扁平结构)
    2. config/skills/<name>/*.md      (目录结构，取目录下任意 .md 文件)
    """
    if ".." in name or "/" in name or "\\" in name:
        return "", f"技能不存在: {name}"
    skills_dir = os.path.join(narnat_dir, CONFIG_SUBDIR, "skills")
    path = os.path.join(skills_dir, f"{name}.md")
    if os.path.isfile(path):
        return _read(os.path.realpath(path))
    subdir = os.path.join(skills_dir, name)
    if os.path.isdir(subdir):
        try:
            for f in os.listdir(subdir):
                if f.endswith(".md"):
                    return _read(os.path.realpath(os.path.join(subdir, f)))
        except OSError:
            pass
    return "", f"技能不存在: {name}"


def list_skill_names(narnat_dir: str) -> List[str]:
    """列出所有可用技能名。
    
    返回 config/skills/ 下:
    - *.md 文件名（不含后缀）
    - 含 .md 文件的子目录名
    """
    skills_dir = os.path.join(narnat_dir, CONFIG_SUBDIR, "skills")
    if not os.path.isdir(skills_dir):
        return []
    names = set()
    try:
        for entry in os.listdir(skills_dir):
            path = os.path.join(skills_dir, entry)
            if os.path.isfile(path) and entry.endswith(".md"):
                names.add(entry[:-3])
            elif os.path.isdir(path):
                try:
                    if any(f.endswith(".md") for f in os.listdir(path)):
                        names.add(entry)
                except OSError:
                    pass
    except OSError:
        return []
    return sorted(names)


def _read(path: str) -> tuple:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip(), ""
    except OSError as e:
        return "", f"读取失败: {e}"
