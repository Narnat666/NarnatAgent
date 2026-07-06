"""
会话持久化 —— 序列化/反序列化messages，供commands/调用
"""

import json
import os
import shutil
import time
from typing import List, Dict, Any, Optional


def _strip_surrogates(obj):
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_surrogates(v) for v in obj]
    return obj

from .defaults import DATA_SUBDIR

_SESSIONS_SUBDIR = "sessions"


def _safe_filename(name: str) -> str:
    safe_name = name.replace("/", "_").replace("\\", "_")
    safe_name = safe_name.replace(":", "_").replace("<", "_").replace(">", "_")
    safe_name = safe_name.replace("|", "_").replace("?", "_").replace("*", "_")
    safe_name = safe_name.replace("..", "_")
    if not safe_name:
        safe_name = "unnamed"
    return safe_name


def _sessions_dir(narnat_dir: str) -> str:
    d = os.path.join(narnat_dir, DATA_SUBDIR, _SESSIONS_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _session_path(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    safe_name = _safe_filename(name)
    if parent:
        safe_parent = _safe_filename(parent)
        child_dir = os.path.join(_sessions_dir(narnat_dir), safe_parent)
        os.makedirs(child_dir, exist_ok=True)
        return os.path.join(child_dir, f"{safe_name}.json")
    return os.path.join(_sessions_dir(narnat_dir), f"{safe_name}.json")


def save_session(narnat_dir: str, name: str,
                 messages: List[Dict[str, Any]],
                 parent: Optional[str] = None,
                 status: str = "active",
                 summary: Optional[str] = None,
                 parent_msg_count: Optional[int] = None,
                 last_summarized_at: Optional[int] = None) -> str:
    path = _session_path(narnat_dir, name, parent=parent)
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    data = {
        "name": name,
        "timestamp": time.time(),
        "messages": messages,
        "parent": parent,
        "status": status,
        "summary": summary,
        "parent_msg_count": parent_msg_count if parent_msg_count is not None else existing.get("parent_msg_count"),
        "last_summarized_at": last_summarized_at if last_summarized_at is not None else existing.get("last_summarized_at"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_strip_surrogates(data), f, ensure_ascii=False, indent=2)
        return ""
    except (OSError, UnicodeEncodeError) as e:
        return f"保存失败: {e}"


def load_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> tuple:
    path = _session_path(narnat_dir, name, parent=parent)
    if not os.path.isfile(path):
        return [], f"会话不存在: {name}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", []), ""
    except (json.JSONDecodeError, OSError) as e:
        return [], f"加载失败: {e}"


def list_sessions(narnat_dir: str) -> List[Dict[str, Any]]:
    sdir = _sessions_dir(narnat_dir)
    result = []
    for fname in os.listdir(sdir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(sdir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({
                "name": data.get("name", fname[:-5]),
                "timestamp": data.get("timestamp", 0),
                "message_count": len(data.get("messages", [])),
            })
        except (json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return result


def delete_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    if name == "--all":
        sdir = _sessions_dir(narnat_dir)
        for entry in os.listdir(sdir):
            entry_path = os.path.join(sdir, entry)
            if os.path.isdir(entry_path):
                try:
                    shutil.rmtree(entry_path)
                except OSError:
                    pass
            elif entry.endswith(".json"):
                try:
                    os.remove(entry_path)
                except OSError:
                    pass
        return ""
    if parent:
        path = _session_path(narnat_dir, name, parent=parent)
        if not os.path.isfile(path):
            return f"会话不存在: {name}"
        os.remove(path)
        safe_parent = _safe_filename(parent)
        child_dir = os.path.join(_sessions_dir(narnat_dir), safe_parent)
        if os.path.isdir(child_dir) and not os.listdir(child_dir):
            try:
                os.rmdir(child_dir)
            except OSError:
                pass
        return ""
    path = _session_path(narnat_dir, name)
    if not os.path.isfile(path):
        return f"会话不存在: {name}"
    os.remove(path)
    safe_name = _safe_filename(name)
    child_dir = os.path.join(_sessions_dir(narnat_dir), safe_name)
    if os.path.isdir(child_dir):
        try:
            shutil.rmtree(child_dir)
        except OSError:
            pass
    return ""


def list_sessions_tree(narnat_dir: str) -> List[Dict[str, Any]]:
    sdir = _sessions_dir(narnat_dir)
    roots = {}
    orphans = []
    for dirpath, dirnames, filenames in os.walk(sdir):
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                info = {
                    "name": data.get("name", fname[:-5]),
                    "timestamp": data.get("timestamp", 0),
                    "message_count": len(data.get("messages", [])),
                    "parent": data.get("parent"),
                    "status": data.get("status", "active"),
                    "summary": data.get("summary"),
                }
            except (json.JSONDecodeError, OSError):
                continue
            if info["parent"]:
                orphans.append(info)
            else:
                roots[info["name"]] = {
                    "name": info["name"],
                    "timestamp": info["timestamp"],
                    "message_count": info["message_count"],
                    "children": [],
                }
    for child in orphans:
        parent_name = child["parent"]
        if parent_name in roots:
            roots[parent_name]["children"].append({
                "name": child["name"],
                "timestamp": child["timestamp"],
                "message_count": child["message_count"],
                "status": child["status"],
                "summary": child["summary"],
            })
        else:
            root_entry = {
                "name": parent_name,
                "timestamp": 0,
                "message_count": 0,
                "children": [child],
            }
            roots[parent_name] = root_entry
    for root in roots.values():
        root["children"].sort(key=lambda c: c.get("timestamp", 0))
    result = sorted(roots.values(), key=lambda r: r.get("timestamp", 0), reverse=True)
    return result


def format_session_tree(tree: List[Dict[str, Any]],
                        active_name: Optional[str] = None,
                        active_parent: Optional[str] = None) -> str:
    if not tree:
        return ""
    lines = []
    for i, root in enumerate(tree):
        is_last_root = (i == len(tree) - 1)
        prefix = "└──" if is_last_root else "├──"
        ts = time.strftime("%m-%d %H:%M", time.localtime(root["timestamp"]))
        is_current_root = (root["name"] == active_name and active_parent is None)
        delete_mark = f"  ✘ 退出后删除" if root.get("_delete_marked") else ""
        current_mark = "  ◀ 当前" if is_current_root else ""
        lines.append(f"  {prefix} {root['name']}  ({ts}, {root['message_count']}条){delete_mark}{current_mark}")
        child_prefix_base = "      " if is_last_root else "│     "
        children = root.get("children", [])
        for j, child in enumerate(children):
            is_last_child = (j == len(children) - 1)
            connector = "└──" if is_last_child else "├──"
            child_ts = time.strftime("%m-%d %H:%M", time.localtime(child["timestamp"]))
            if child.get("status") == "completed":
                status_str = f"✓ 已完成 ({child_ts})"
            elif child.get("status") == "new":
                status_str = f"({child_ts}, {child['message_count']}条)"
            else:
                status_str = f"⚠ 待完成 ({child_ts}, {child['message_count']}条)"
            if child.get("_delete_marked"):
                status_str += "  ✘ 退出后删除"
            current_mark = "  ◀ 当前" if (child["name"] == active_name and root["name"] == active_parent) else ""
            lines.append(f"  {child_prefix_base}{connector} {child['name']}  {status_str}{current_mark}")
    if active_name is None and active_parent is None:
        lines.append(f"   ◉  ◀ 当前")
    return "\n".join(lines)


def load_session_meta(narnat_dir: str, name: str, parent: Optional[str] = None) -> dict:
    path = _session_path(narnat_dir, name, parent=parent)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k != "messages"}
    except (json.JSONDecodeError, OSError):
        return {}


def format_session_list(sessions: List[Dict[str, Any]]) -> str:
    if not sessions:
        return ""
    lines = []
    for s in sessions:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["timestamp"]))
        lines.append(f"  {s['name']}  ({ts}, {s['message_count']}条消息)")
    return "\n".join(lines)
