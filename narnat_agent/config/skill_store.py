"""
技能加载 —— 系统技能(.narnat/config/skills/) + 项目技能(工作目录下自动发现的 skills 目录)

技能来源（加载时按此顺序）:
  1. 系统技能: <narnat_dir>/config/skills/  （exe 同目录，原有行为不变）
     - <name>.md 扁平文件
     - <name>/ 目录（取目录下第一个 .md）
  2. 项目技能: 当前工作目录下所有名为 skills 的目录（自动发现，无需写死列表），
     如 .agents/skills、.kimi-code/skills、xxx/skills、顶层 skills/。
     - 支持层级路径: /skill 目录1/XXX.md
     - 目录内含 SKILL.md 或恰好一个 .md 时，可直接用目录名加载
     - 目录内多个 .md 且无 SKILL.md 时，用目录名加载会提示可选文件
"""

import os
import re
from typing import List

from .defaults import CONFIG_SUBDIR, DEFAULT_SKILL_SCAN_DEPTH

# 技能树目录递归深度上限（兜底；防环主要靠 realpath 已访问集合）。
# 正常技能目录嵌套不超过 2-3 层，8 层足够宽裕。
_MAX_DIR_DEPTH = 8


def load_skill(narnat_dir: str, name: str,
               project_roots=None,
               cwd: str = "",
               ignore_dirs: tuple = (),
               scan_depth: int = DEFAULT_SKILL_SCAN_DEPTH) -> tuple:
    """加载技能内容。返回 (content, error)。

    project_roots:
      None      → 自动发现（扫描工作目录下所有名为 skills 的目录）
      ()        → 关闭项目技能，只加载系统技能
      非空元组  → 仅使用显式指定的技能根目录（相对工作目录，支持绝对路径）

    查找顺序:
    1. 系统技能 config/skills/<name>.md / <name>/（原有行为）
    2. 项目技能（各技能根目录下，支持 目录/文件.md 层级路径）
    """
    if not name or not name.strip():
        return "", "技能不存在: "
    name = name.strip().replace("\\", "/").rstrip("/")
    if _unsafe_name(name):
        return "", f"技能不存在: {name}"

    # 1. 系统技能（名称不含 "/"，原有逻辑不变）
    if "/" not in name:
        content, err = _load_system_skill(narnat_dir, name)
        if content or err:
            return content, err

    # 2. 项目技能
    cwd = cwd or os.getcwd()
    for root in _project_roots(narnat_dir, project_roots, cwd, ignore_dirs, scan_depth):
        result = _load_project_skill(root, name)
        if result is not None:
            return result
    return "", f"技能不存在: {name}"


def list_skill_tree(narnat_dir: str,
                    project_roots=None,
                    cwd: str = "",
                    ignore_dirs: tuple = (),
                    scan_depth: int = DEFAULT_SKILL_SCAN_DEPTH) -> List[dict]:
    """技能树（供 /skill 按层级 Tab 补全）。

    project_roots 语义同 load_skill：None=自动发现；()=关闭项目技能；非空=显式指定。

    节点结构:
      {"name": str, "type": "file", "origin": "system"|"project"}  — 可加载叶子
      {"name": str, "type": "dir", "children": [...], "single": bool,
       "origin": "project"}
      - single=True 的目录（恰含一个直接 .md 且无含 .md 的子目录）显示为裸名，可直接加载；
        其余目录显示为 "name/" 形式供逐层进入。
    """
    tree = []
    skills_dir = os.path.join(narnat_dir, CONFIG_SUBDIR, "skills")
    if os.path.isdir(skills_dir):
        try:
            for entry in sorted(os.listdir(skills_dir)):
                path = os.path.join(skills_dir, entry)
                if os.path.isfile(path) and entry.endswith(".md"):
                    tree.append({"name": entry[:-3], "type": "file", "origin": "system"})
                elif os.path.isdir(path):
                    try:
                        if any(f.endswith(".md") for f in os.listdir(path)):
                            tree.append({"name": entry, "type": "file", "origin": "system"})
                    except OSError:
                        pass
        except OSError:
            pass

    cwd = cwd or os.getcwd()
    merged = {}
    for root in _project_roots(narnat_dir, project_roots, cwd, ignore_dirs, scan_depth):
        _merge_nodes(merged, _scan_dir(root))
    tree.extend(_nodes_to_list(merged))
    return tree


# ── 内部工具 ──

def _project_roots(narnat_dir: str, project_roots, cwd: str,
                   ignore_dirs: tuple, scan_depth: int) -> list:
    """解析项目技能根目录列表。

    - None → 自动发现（扫描工作目录下所有名为 skills 的目录，排除系统技能目录）
    - ()  → 空列表
    - 非空元组 → 按显式列表解析（相对 cwd）
    """
    if project_roots is not None:
        bases = []
        for root in project_roots:
            base = root if os.path.isabs(root) else os.path.join(cwd, root)
            if os.path.isdir(base):
                bases.append(base)
        return bases
    return _discover_skill_roots(cwd, narnat_dir, ignore_dirs, scan_depth)


def _discover_skill_roots(cwd: str, narnat_dir: str,
                          ignore_dirs: tuple, max_depth: int) -> list:
    """在 cwd 下递归查找名为 skills 的目录作为项目技能根。

    - 跳过忽略目录（默认含 node_modules/.git 等噪音目录）
    - 深度超过 max_depth（根算第1层）不再深入
    - 发现 skills 目录后不再进入其内部（技能内容本身不再找技能根）
    - 排除系统技能目录本身（避免与系统技能重复列出）
    """
    system_skills = os.path.realpath(os.path.join(narnat_dir, CONFIG_SUBDIR, "skills"))
    ignores = set(ignore_dirs or ())
    roots = []

    def walk(path: str, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(path))
        except OSError:
            return
        for entry in entries:
            if entry in ignores:
                continue
            p = os.path.join(path, entry)
            if not os.path.isdir(p):
                continue
            if entry.lower() == "skills":
                if os.path.realpath(p) != system_skills:
                    roots.append(p)
                continue
            walk(p, depth + 1)

    walk(cwd, 1)
    return roots


def _load_system_skill(narnat_dir: str, name: str) -> tuple:
    """原有系统技能查找逻辑。未找到返回 ("", "")。"""
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
    return "", ""


def _load_project_skill(base: str, name: str):
    """在单个项目技能根 base 下解析 name。返回 (content, error)；该根下不存在返回 None。"""
    base_real = os.path.realpath(base)
    # 1) 直接文件（支持省略 .md 后缀）
    candidates = [name]
    if not name.lower().endswith(".md"):
        candidates.append(name + ".md")
    for rel in candidates:
        p = os.path.realpath(os.path.join(base, rel))
        if os.path.isfile(p) and p.lower().endswith(".md") and _within(base_real, p):
            return _read(p)
    # 2) 目录
    d = os.path.realpath(os.path.join(base, name))
    if os.path.isdir(d) and _within(base_real, d):
        return _resolve_project_dir(d, name)
    return None


def _resolve_project_dir(d: str, name: str) -> tuple:
    """解析项目技能目录：SKILL.md 优先，其次唯一 .md，多个则提示可选文件。"""
    try:
        entries = sorted(os.listdir(d))
    except OSError:
        return "", f"技能不存在: {name}"
    mds = [e for e in entries
           if os.path.isfile(os.path.join(d, e)) and e.lower().endswith(".md")]
    if not mds:
        subdirs = [e for e in entries if os.path.isdir(os.path.join(d, e))]
        if subdirs:
            hint = "、".join(f"{name}/{s}" for s in subdirs[:5])
            if len(subdirs) > 5:
                hint += " …"
            return "", f"技能 '{name}' 目录下没有直接技能文件，可指定子目录: {hint}"
        return "", f"技能 '{name}' 目录下没有技能文件"
    skill = next((e for e in mds if e.lower() == "skill.md"), None)
    if skill:
        return _read(os.path.join(d, skill))
    if len(mds) == 1:
        return _read(os.path.join(d, mds[0]))
    hint = "、".join(f"{name}/{e}" for e in mds[:5])
    if len(mds) > 5:
        hint += f" 等{len(mds)}个"
    return "", f"技能 '{name}' 目录下有多个技能文件，请指定具体文件，例如: {hint}"


def _scan_dir(abs_dir: str, visited=None, depth: int = 0) -> dict:
    """扫描目录，返回 {名字: 节点}（仅 .md 文件与含 .md 的子目录）。

    visited: 已访问 realpath 集合 —— 防 junction/symlink 环导致无限递归；
    depth: 相对技能根的深度，超过 _MAX_DIR_DEPTH 停止深入（兜底，正常技能目录远达不到）。
    """
    if depth > _MAX_DIR_DEPTH:
        return {}
    real = os.path.realpath(abs_dir)
    if visited is None:
        visited = set()
    if real in visited:
        return {}
    visited.add(real)
    nodes = {}
    try:
        entries = sorted(os.listdir(abs_dir))
    except OSError:
        return nodes
    for entry in entries:
        p = os.path.join(abs_dir, entry)
        if os.path.isdir(p):
            children = _scan_dir(p, visited, depth + 1)
            if children:
                nodes[entry] = {
                    "name": entry,
                    "type": "dir",
                    "children": children,
                    "single": _is_single(children),
                    "origin": "project",
                }
        elif os.path.isfile(p) and entry.lower().endswith(".md"):
            nodes.setdefault(entry, {"name": entry, "type": "file", "origin": "project"})
    return nodes


def _is_single(children: dict) -> bool:
    """目录是否"单技能"：恰含一个直接 .md 且无含 .md 的子目录 → 补全显示为裸名。"""
    return len(children) == 1 and next(iter(children.values()))["type"] == "file"


def _merge_nodes(dst: dict, src: dict):
    """跨根合并：同名目录递归合并子项，先出现（优先级更高的根）的节点保留。"""
    for name, node in src.items():
        if name not in dst:
            dst[name] = node
        elif dst[name]["type"] == "dir" and node["type"] == "dir":
            _merge_nodes(dst[name]["children"], node["children"])
            dst[name]["single"] = _is_single(dst[name]["children"])


def _nodes_to_list(d: dict) -> list:
    result = []
    for n in d.values():
        if n["type"] == "file":
            result.append(dict(n))
        else:
            result.append({
                "name": n["name"], "type": "dir",
                "children": _nodes_to_list(n["children"]),
                "single": n["single"], "origin": n["origin"],
            })
    return result


def _unsafe_name(name: str) -> bool:
    """路径穿越/绝对路径检测。合法名称：相对路径、无 ".." 组件、非绝对路径。"""
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return True
    return any(part in ("", ".", "..") for part in n.split("/"))


def _within(base_real: str, path_real: str) -> bool:
    """path_real 是否位于 base_real 之内（防路径穿越的兜底校验）。"""
    return path_real == base_real or path_real.startswith(base_real + os.sep)


def _read(path: str) -> tuple:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return "", f"读取失败: {e}"
    # 先 UTF-8（含 BOM）严格解码，失败回退 GBK（中文Windows常见），都失败报错不崩溃
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding).strip(), ""
        except UnicodeDecodeError:
            continue
    return "", "读取失败: 无法识别的文件编码"
