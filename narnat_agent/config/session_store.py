"""
会话持久化 —— 序列化/反序列化messages，供commands/调用
"""

import json
import os
import time
from typing import List, Dict, Any, Optional

from .defaults import NARNAT_DIR

# 会话存储子目录
_SESSIONS_SUBDIR = "sessions"


def _sessions_dir(narnat_dir: str) -> str:
    """获取会话存储目录，不存在则创建"""
    d = os.path.join(narnat_dir, _SESSIONS_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _session_path(narnat_dir: str, name: str) -> str:
    """获取指定会话的文件路径"""
    # 安全化：替换路径分隔符和Windows禁止字符
    safe_name = name.replace("/", "_").replace("\\", "_")
    safe_name = safe_name.replace(":", "_").replace("<", "_").replace(">", "_")
    safe_name = safe_name.replace("|", "_").replace("?", "_").replace("*", "_")
    # 防止..逃逸
    safe_name = safe_name.replace("..", "_")
    if not safe_name:
        safe_name = "unnamed"
    return os.path.join(_sessions_dir(narnat_dir), f"{safe_name}.json")


def save_session(narnat_dir: str, name: str,
                 messages: List[Dict[str, Any]]) -> str:
    """
    保存会话。返回空串表示成功，非空串为错误提示。
    """
    path = _session_path(narnat_dir, name)
    data = {
        "name": name,
        "timestamp": time.time(),
        "messages": messages,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return ""
    except OSError as e:
        return f"保存失败: {e}"


def load_session(narnat_dir: str, name: str) -> tuple:
    """
    加载会话。返回 (messages, error)。
    messages非空且error为空串表示成功。
    """
    path = _session_path(narnat_dir, name)
    if not os.path.isfile(path):
        return [], f"会话不存在: {name}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", []), ""
    except (json.JSONDecodeError, OSError) as e:
        return [], f"加载失败: {e}"


def list_sessions(narnat_dir: str) -> List[Dict[str, Any]]:
    """
    列出所有已保存会话的摘要信息。
    返回列表，每项含 name, timestamp, message_count。
    """
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


def delete_session(narnat_dir: str, name: str) -> str:
    """
    删除指定会话。返回空串表示成功。
    name为"--all"时删除全部。
    """
    if name == "--all":
        sdir = _sessions_dir(narnat_dir)
        for fname in os.listdir(sdir):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(sdir, fname))
                except OSError:
                    pass
        return ""
    path = _session_path(narnat_dir, name)
    if not os.path.isfile(path):
        return f"会话不存在: {name}"
    os.remove(path)
    return ""


def format_session_list(sessions: List[Dict[str, Any]]) -> str:
    """格式化会话列表为可读文本"""
    if not sessions:
        return ""
    lines = []
    for s in sessions:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["timestamp"]))
        lines.append(f"  {s['name']}  ({ts}, {s['message_count']}条消息)")
    return "\n".join(lines)
