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

from .defaults import DATA_SUBDIR, SESSIONS_SUBDIR


def _safe_timestamp(value) -> float:
    """会话 timestamp 类型校验：非数值（字符串/None/对象）一律视作 0。

    手工编辑/半写文件会把 timestamp 写成字符串或 null，此前会让排序比较
    抛 TypeError（str 与 float 不可比），一条坏文件瘫痪整个会话列表。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _format_ts(ts, fmt: str) -> str:
    """时间格式化（安全版）：越界时间戳（如 1e18）会让 time.localtime 抛
    OSError/OverflowError，降级为占位符，避免一条坏文件让整个列表渲染崩溃。
    """
    try:
        return time.strftime(fmt, time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "未知时间"


def _safe_filename(name: str) -> str:
    safe_name = name.replace("/", "_").replace("\\", "_")
    safe_name = safe_name.replace(":", "_").replace("<", "_").replace(">", "_")
    safe_name = safe_name.replace("|", "_").replace("?", "_").replace("*", "_")
    safe_name = safe_name.replace("..", "_")
    if not safe_name:
        safe_name = "unnamed"
    return safe_name


def _sessions_dir(narnat_dir: str) -> str:
    d = os.path.join(narnat_dir, DATA_SUBDIR, SESSIONS_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # 目录不可用（被同名文件占位/权限拒绝/磁盘满）：此处不抛——调用方按
        # 各自错误路径自然降级（保存返回可读错误、列举为空），避免一次目录
        # 异常把整个进程带崩
        pass
    return d


def _load_json_with_shared_retry(path: str, attempts: int = 3):
    """读取会话 JSON：对共享冲突（并发写方 os.replace 的毫秒窗口）短退避重试。

    读端 open 在替换瞬间可能瞬时 Errno 13 / 文件暂时不可见（多进程或后台
    自动保存线程与前台命令并发时实测存在的瞬态失败）；重试后基本消除。
    重试耗尽仍失败时抛出最后一次 OSError，由调用方按既有语义处理
    （列表跳过 / 加载报错）。解码类异常（JSON 损坏等）立即抛出，不重试。
    """
    last_err = None
    for i in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except OSError as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.01 * (i + 1))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise
    raise last_err


def _session_path(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    safe_name = _safe_filename(name)
    if parent:
        safe_parent = _safe_filename(parent)
        child_dir = os.path.join(_sessions_dir(narnat_dir), safe_parent)
        try:
            os.makedirs(child_dir, exist_ok=True)
        except OSError:
            pass
        return os.path.join(child_dir, f"{safe_name}.json")
    return os.path.join(_sessions_dir(narnat_dir), f"{safe_name}.json")


def session_exists(narnat_dir: str, name: str, parent: Optional[str] = None) -> bool:
    return os.path.isfile(_session_path(narnat_dir, name, parent=parent))


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
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
            existing = {}
        # 顶层非对象（手工编辑/半写文件）视作无既有元数据，不能直接 .get()
        if not isinstance(existing, dict):
            existing = {}
    # 归一化碰撞检测：特殊字符被替换后映射到同一文件，静默覆盖会清掉另一会话。
    # 判空用 is not None：空串 name（命名失败写入的文件）也是"已有名称"，
    # falsy 短路会让它被 "unnamed" 静默覆盖（_safe_filename 把两者映射到同一
    # 文件）。老格式（无 name 键）保持放行升级（get 返回 None）。
    if existing.get("name") is not None and existing["name"] != name:
        return (f"保存失败: 会话名 '{name}' 与已有会话 '{existing['name']}' 冲突"
                f"（名称中的 / \\ : < > | ? * 等字符在存储时会被替换，导致两者映射到同一文件），请换一个名称")
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
    # 临时文件名带进程号：同名会话被多进程并发保存时，固定 ".tmp" 名会互相
    # 抢占（Windows WinError 32/5），各进程写自己的临时文件后 os.replace 原子
    # 替换，语义不变
    tmp_path = f"{path}.{os.getpid()}.tmp"
    last_err = None
    # os.replace 仍可能与另一进程的替换瞬间撞车（共享冲突）→ 短暂退避重试
    for attempt in range(3):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_strip_surrogates(data), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return ""
        except UnicodeEncodeError as e:
            # 编码失败与并发无关，重试无意义
            last_err = e
            break
        except RecursionError as e:
            # 深嵌套 messages（实测约 400 层即触发）在 _strip_surrogates/
            # json.dump 递归时抛异常：load 端已按"损坏文件"防御，save 端
            # 同样收敛为可读失败（不冒泡到主循环）
            last_err = e
            break
        except OSError as e:
            last_err = e
            time.sleep(0.02 * (attempt + 1))
    # 失败路径清掉本次的临时文件，不留残骸（成功路径已被 os.replace 消费）
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return f"保存失败: {last_err}"


def load_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> tuple:
    path = _session_path(narnat_dir, name, parent=parent)
    if not os.path.isfile(path):
        return [], f"会话不存在: {name}"
    try:
        data = _load_json_with_shared_retry(path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError) as e:
        # RecursionError：json.load 对超深嵌套（约1000层起）抛递归异常，属"文件损坏"
        return [], f"加载失败: {e}"
    # 字段类型校验：手工编辑/半写文件可能造成顶层非对象或 messages 非列表
    if not isinstance(data, dict):
        return [], f"加载失败: 会话文件格式非法（顶层应为对象）: {name}"
    messages = data.get("messages")
    if not isinstance(messages, list):
        return [], f"加载失败: 会话文件 messages 字段非法（应为数组）: {name}"
    return messages, ""


def list_sessions(narnat_dir: str) -> List[Dict[str, Any]]:
    sdir = _sessions_dir(narnat_dir)
    result = []
    try:
        entries = os.listdir(sdir)
    except OSError:
        return result   # 会话库目录不可用：按空列表降级（与"无会话"同观感）
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(sdir, fname)
        try:
            data = _load_json_with_shared_retry(fpath)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
            # 损坏文件（含超深嵌套）一律跳过，不影响其余会话
            continue
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        result.append({
            "name": data.get("name", fname[:-5]),
            "timestamp": _safe_timestamp(data.get("timestamp", 0)),
            "message_count": len(messages) if isinstance(messages, list) else 0,
        })
    result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return result


def delete_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    if name == "--all":
        sdir = _sessions_dir(narnat_dir)
        try:
            entries = os.listdir(sdir)
        except OSError as e:
            return f"无法访问会话目录: {e}"
        for entry in entries:
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
        sessions_root = _sessions_dir(narnat_dir)
        child_dir = os.path.join(sessions_root, safe_parent)
        # parent="." 时 child_dir 即会话库根自身的别名：rmdir 绝不能触及
        if (os.path.normpath(child_dir) != os.path.normpath(sessions_root)
                and os.path.isdir(child_dir) and not os.listdir(child_dir)):
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
    sessions_root = _sessions_dir(narnat_dir)
    child_dir = os.path.join(sessions_root, safe_name)
    # "." 经文件名映射后仍是 "."（会话库根自身的别名）：rmtree 会连同
    # 其它全部会话清空整个会话库，必须排除
    if (os.path.normpath(child_dir) != os.path.normpath(sessions_root)
            and os.path.isdir(child_dir)):
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
                data = _load_json_with_shared_retry(fpath)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
                # 损坏文件（含超深嵌套）一律跳过，不影响其余会话
                continue
            if not isinstance(data, dict):
                continue
            messages = data.get("messages")
            info = {
                "name": data.get("name", fname[:-5]),
                "timestamp": _safe_timestamp(data.get("timestamp", 0)),
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "parent": data.get("parent"),
                "status": data.get("status", "active"),
                "summary": data.get("summary"),
            }
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
        ts = _format_ts(root["timestamp"], "%m-%d %H:%M")
        is_current_root = (root["name"] == active_name and active_parent is None)
        delete_mark = f"  ✘ 退出后删除" if root.get("_delete_marked") else ""
        current_mark = "  ◀ 当前" if is_current_root else ""
        lines.append(f"  {prefix} {root['name']}  ({ts}, {root['message_count']}条){delete_mark}{current_mark}")
        child_prefix_base = "      " if is_last_root else "│     "
        children = root.get("children", [])
        for j, child in enumerate(children):
            is_last_child = (j == len(children) - 1)
            connector = "└──" if is_last_child else "├──"
            child_ts = _format_ts(child["timestamp"], "%m-%d %H:%M")
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


def format_session_summary(tree: List[Dict[str, Any]],
                           active_name: Optional[str] = None,
                           active_parent: Optional[str] = None) -> str:
    """精简列表：今天全部列出，更早最多3个父会话并提示剩余（/ls --all 查看全部）。

    分组只看父会话 timestamp；子会话挂在父会话下按树形显示，格式与 /ls --all 一致。
    """
    if not tree:
        return ""
    now = time.localtime()
    today_midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  0, 0, 0, 0, 0, -1))

    def _entry(root):
        return {
            "name": root["name"],
            "timestamp": root["timestamp"],
            "count": root["message_count"],
            "active": (root["name"] == active_name and active_parent is None),
            "deleted": bool(root.get("_delete_marked")),
            "children": [
                {
                    "name": child["name"],
                    "timestamp": child["timestamp"],
                    "count": child["message_count"],
                    "status": child.get("status", "active"),
                    "active": (child["name"] == active_name and root["name"] == active_parent),
                    "deleted": bool(child.get("_delete_marked")),
                }
                for child in root.get("children", [])
            ],
        }

    today, earlier = [], []
    for root in tree:
        entry = _entry(root)
        (today if entry["timestamp"] >= today_midnight else earlier).append(entry)
    today.sort(key=lambda e: e["timestamp"], reverse=True)
    earlier.sort(key=lambda e: e["timestamp"], reverse=True)

    def _render(entry, ts_fmt, is_last):
        lines = []
        ts = _format_ts(entry["timestamp"], ts_fmt)
        marks = ""
        if entry["deleted"]:
            marks += "  ✘ 退出后删除"
        if entry["active"]:
            marks += "  ◀ 当前"
        connector = "└──" if is_last else "├──"
        lines.append(f"  {connector} {entry['name']}  ({ts}, {entry['count']}条){marks}")
        child_prefix_base = "      " if is_last else "│     "
        for j, child in enumerate(entry["children"]):
            is_last_child = (j == len(entry["children"]) - 1)
            c_connector = "└──" if is_last_child else "├──"
            child_ts = _format_ts(child["timestamp"], "%m-%d %H:%M")
            if child["status"] == "completed":
                status_str = f"✓ 已完成 ({child_ts})"
            elif child["status"] == "new":
                status_str = f"({child_ts}, {child['count']}条)"
            else:
                status_str = f"⚠ 待完成 ({child_ts}, {child['count']}条)"
            if child["deleted"]:
                status_str += "  ✘ 退出后删除"
            if child["active"]:
                status_str += "  ◀ 当前"
            lines.append(f"  {child_prefix_base}{c_connector} {child['name']}  {status_str}")
        return lines

    lines = []
    if today:
        lines.append(f"  今天 ({len(today)})")
        for i, e in enumerate(today):
            lines.extend(_render(e, "%H:%M", i == len(today) - 1))
        if earlier:
            lines.append("")
    if earlier:
        lines.append(f"  更早 ({len(earlier)})")
        shown = earlier[:3]
        # 当前会话（父或子）若不在前3棵，追加显示，保证"我在哪"可见
        current = next((e for e in earlier
                        if e["active"] or any(c["active"] for c in e["children"])), None)
        if current is not None and current not in shown:
            shown = shown + [current]
        for i, e in enumerate(shown):
            lines.extend(_render(e, "%m-%d", i == len(shown) - 1))
        rest = len(earlier) - len(shown)
        if rest > 0:
            lines.append(f"  … 还有 {rest} 条更早的会话 (/ls --all 查看)")
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
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
        return {}
    # 顶层类型防御（与 load_session 同构）：手工编辑/半写文件可能是数组/字符串
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "messages"}


def format_session_list(sessions: List[Dict[str, Any]]) -> str:
    if not sessions:
        return ""
    lines = []
    for s in sessions:
        ts = _format_ts(s["timestamp"], "%Y-%m-%d %H:%M")
        lines.append(f"  {s['name']}  ({ts}, {s['message_count']}条消息)")
    return "\n".join(lines)
