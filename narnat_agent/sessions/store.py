"""会话持久化 —— 会话文件的读写、删除、树形枚举与列表文本格式化。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`
（「会话列表（/ls）」的树形/摘要格式、「会话文件布局与原子写」、「延迟删除执行
与展示标记」）。

文件布局（已发布契约，跨版本兼容）：
- 根会话 `<narnat_dir>/data/sessions/<名称>.json`；
- 子会话 `<narnat_dir>/data/sessions/<父名>/<子名>.json`；
- 文件字段：name / timestamp（每次保存刷新）/ messages / parent / status /
  summary / parent_msg_count / last_summarized_at（子会话的合并基准与已合并位置
  显式传值优先、缺省时继承旧文件已有值）；
- 写入为原子写（先写 `<路径>.tmp` 再改名替换）；写入前清洗孤立代理字符
  （替换为 U+FFFD）；文件名做安全替换（`/ \\ : < > | ? *` → `_`、`..` → `_`、
  空 → `unnamed`）。

文本格式的兼容基准：`v2/tests/baseline/data/session_store.json`
（`safe_filename` / `format_session_tree` / `format_session_summary` /
`format_session_list` 的输出逐字节保持；`format_session_list` 当前无消费方，
作为已发布格式化契约保留）。

依赖规则（design D1）：本积木位于 L3，只依赖 contracts 与下层积木
（本模块只用到 config 的目录常量）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Optional

from ..config.defaults import DATA_SUBDIR, SESSIONS_SUBDIR

__all__ = [
    "SessionStore",
    "clean_surrogates",
    "format_session_list",
    "format_session_summary",
    "format_session_tree",
    "safe_filename",
]


def safe_filename(name: str) -> str:
    """会话名 → 安全文件名（spec「文件名安全替换」）。

    `/ \\ : < > | ? *` 替换为 `_`、`..` 替换为 `_`、结果为空时使用 `unnamed`。
    """
    safe = name.replace("/", "_").replace("\\", "_")
    safe = safe.replace(":", "_").replace("<", "_").replace(">", "_")
    safe = safe.replace("|", "_").replace("?", "_").replace("*", "_")
    safe = safe.replace("..", "_")
    return safe or "unnamed"


def clean_surrogates(obj: Any) -> Any:
    """清洗文本中的孤立代理字符（替换为 U+FFFD），递归处理 dict / list。

    写入前的编码异常防护（spec「孤立代理字符清洗」）：孤立代理字符无法按
    UTF-8 编码，未清洗会让整次保存失败。
    """
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: clean_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_surrogates(v) for v in obj]
    return obj


class SessionStore:
    """会话持久化门面 —— 文件布局、读写、枚举与删除。

    构造注入 `narnat_dir`（`.narnat` 目录）；会话文件位于其
    `<narnat_dir>/data/sessions/` 子目录。路径拼接（含安全替换）全部在本类内
    完成，调用方只以会话名（与父名）寻址。

    读取失败文案（spec）：文件缺失 → `会话不存在: {名称}`；解析失败 →
    `加载失败: {错误}`。删除失败静默忽略（延迟删除执行不因单条失败中断）。
    """

    def __init__(self, narnat_dir: str) -> None:
        self._narnat_dir = narnat_dir

    @property
    def narnat_dir(self) -> str:
        """数据目录的父目录（`.narnat`）。"""
        return self._narnat_dir

    # ── 路径 ──

    def sessions_dir(self) -> str:
        """会话目录（不存在时创建）。"""
        path = os.path.join(self._narnat_dir, DATA_SUBDIR, SESSIONS_SUBDIR)
        os.makedirs(path, exist_ok=True)
        return path

    def path(self, name: str, parent: Optional[str] = None) -> str:
        """会话文件路径（子会话为 `<sessions>/<父名>/<子名>.json`）。"""
        safe_name = safe_filename(name)
        if parent:
            child_dir = os.path.join(self.sessions_dir(), safe_filename(parent))
            os.makedirs(child_dir, exist_ok=True)
            return os.path.join(child_dir, f"{safe_name}.json")
        return os.path.join(self.sessions_dir(), f"{safe_name}.json")

    # ── 读写 ──

    def save(self, name: str, messages: list[dict[str, Any]],
             parent: Optional[str] = None, status: str = "active",
             summary: Optional[str] = None,
             parent_msg_count: Optional[int] = None,
             last_summarized_at: Optional[int] = None) -> str:
        """保存会话（原子写）。返回错误文本，空串表示成功。

        timestamp 每次刷新为当前时间；`parent_msg_count` / `last_summarized_at`
        显式传值优先，缺省（None）时继承旧文件已有值（子会话增量合并基准）。
        """
        path = self.path(name, parent=parent)
        existing: dict = {}
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
            "parent_msg_count": (parent_msg_count if parent_msg_count is not None
                                 else existing.get("parent_msg_count")),
            "last_summarized_at": (last_summarized_at if last_summarized_at is not None
                                   else existing.get("last_summarized_at")),
        }
        try:
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(clean_surrogates(data), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return ""
        except (OSError, UnicodeEncodeError) as e:
            return f"保存失败: {e}"

    def load(self, name: str, parent: Optional[str] = None) -> tuple[list, str]:
        """加载会话消息列表。返回 `(messages, 错误文本)`。"""
        path = self.path(name, parent=parent)
        if not os.path.isfile(path):
            return [], f"会话不存在: {name}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("messages", []), ""
        except (json.JSONDecodeError, OSError) as e:
            return [], f"加载失败: {e}"

    def load_meta(self, name: str, parent: Optional[str] = None) -> dict:
        """读取会话元数据（除 messages 外的全部字段）；缺失/损坏返回空 dict。"""
        path = self.path(name, parent=parent)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k != "messages"}
        except (json.JSONDecodeError, OSError):
            return {}

    # ── 枚举与删除 ──

    def list_tree(self) -> list[dict]:
        """会话树：根按时间倒序、子按时间升序。

        根会话文件缺失的子会话（孤儿）以父名合成占位根（时间与条数为 0）挂载
        （spec「游离子会话的占位父行」）。节点结构：根 `{name, timestamp,
        message_count, children}`，子 `{name, timestamp, message_count, status,
        summary}`；`_delete_marked` 由调用方（会话管理器）按待删除标记注入。
        """
        sdir = self.sessions_dir()
        roots: dict[str, dict] = {}
        orphans: list[dict] = []
        for dirpath, _dirnames, filenames in os.walk(sdir):
            for fname in filenames:
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(dirpath, fname), "r", encoding="utf-8") as f:
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
                roots[parent_name] = {
                    "name": parent_name,
                    "timestamp": 0,
                    "message_count": 0,
                    "children": [child],
                }
        for root in roots.values():
            root["children"].sort(key=lambda c: c.get("timestamp", 0))
        return sorted(roots.values(), key=lambda r: r.get("timestamp", 0), reverse=True)

    def list_root_names(self) -> list[str]:
        """全部根会话名（按时间倒序；供会话自动命名避开已占用名称）。"""
        sdir = self.sessions_dir()
        entries: list[tuple[float, str]] = []
        for fname in os.listdir(sdir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(sdir, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries.append((data.get("timestamp", 0), data.get("name", fname[:-5])))
            except (json.JSONDecodeError, OSError):
                continue
        entries.sort(key=lambda e: e[0], reverse=True)
        return [name for _ts, name in entries]

    def delete(self, name: str, parent: Optional[str] = None) -> str:
        """删除一个会话文件。返回错误文本，空串表示成功。

        根会话（parent 为空）连带删除其子会话目录（全部子会话）；子会话删除后
        父目录为空时移除该目录。删除失败静默忽略（spec「延迟删除执行与展示标记」）。
        """
        if parent:
            path = self.path(name, parent=parent)
            if not os.path.isfile(path):
                return f"会话不存在: {name}"
            try:
                os.remove(path)
            except OSError:
                return ""
            child_dir = os.path.join(self.sessions_dir(), safe_filename(parent))
            if os.path.isdir(child_dir) and not os.listdir(child_dir):
                try:
                    os.rmdir(child_dir)
                except OSError:
                    pass
            return ""
        path = self.path(name)
        if not os.path.isfile(path):
            return f"会话不存在: {name}"
        try:
            os.remove(path)
        except OSError:
            return ""
        child_dir = os.path.join(self.sessions_dir(), safe_filename(name))
        if os.path.isdir(child_dir):
            try:
                shutil.rmtree(child_dir)
            except OSError:
                pass
        return ""


# ═══════════════════════════════════════════════════════════════
# 列表文本格式化（纯函数；输出为已发布契约，见模块 docstring 的基准对照）
# ═══════════════════════════════════════════════════════════════


def format_session_tree(tree: list[dict[str, Any]],
                        active_name: Optional[str] = None,
                        active_parent: Optional[str] = None) -> str:
    """全量树形文本（`/ls --all`）：根行 `├──`/`└──` 前缀、子行再缩进。

    根行格式「名称  (MM-DD HH:MM, N条)」；子行状态文案按 status 字段：
    completed →「✓ 已完成 (MM-DD HH:MM)」、new →「(MM-DD HH:MM, N条)」、
    其余（active）→「⚠ 待完成 (MM-DD HH:MM, N条)」。待删除标记（`_delete_marked`）
    与当前会话标记（`◀ 当前`）按已有约定追加；游离态（两个 active 参数均空）在
    末尾追加「   ◉  ◀ 当前」行。空树返回空串。
    """
    if not tree:
        return ""
    lines = []
    for i, root in enumerate(tree):
        is_last_root = (i == len(tree) - 1)
        prefix = "└──" if is_last_root else "├──"
        ts = time.strftime("%m-%d %H:%M", time.localtime(root["timestamp"]))
        is_current_root = (root["name"] == active_name and active_parent is None)
        delete_mark = "  ✘ 退出后删除" if root.get("_delete_marked") else ""
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
            current_mark = "  ◀ 当前" if (
                child["name"] == active_name and root["name"] == active_parent
            ) else ""
            lines.append(f"  {child_prefix_base}{connector} {child['name']}  {status_str}{current_mark}")
    if active_name is None and active_parent is None:
        lines.append("   ◉  ◀ 当前")
    return "\n".join(lines)


def format_session_summary(tree: list[dict[str, Any]],
                           active_name: Optional[str] = None,
                           active_parent: Optional[str] = None) -> str:
    """精简列表文本（`/ls` 无参数）：今天全部列出，更早最多 3 个父会话。

    分组只看父会话 timestamp；子会话挂在父会话下按树形显示（格式与
    `format_session_tree` 一致，仅根行时间格式不同：今天 HH:MM、更早 MM-DD）。
    更早组剩余时追加「… 还有 N 条更早的会话 (/ls --all 查看)」；当前会话
    （父或子）不在前 3 棵时追加显示，保证「我在哪」可见（比较用整条记录内容，
    内容相同的不同会话可能被误判为已显示——兼容怪癖保持）。
    """
    if not tree:
        return ""
    now = time.localtime()
    today_midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  0, 0, 0, 0, 0, -1))

    def _entry(root: dict) -> dict:
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

    today: list[dict] = []
    earlier: list[dict] = []
    for root in tree:
        entry = _entry(root)
        (today if entry["timestamp"] >= today_midnight else earlier).append(entry)
    today.sort(key=lambda e: e["timestamp"], reverse=True)
    earlier.sort(key=lambda e: e["timestamp"], reverse=True)

    def _render(entry: dict, ts_fmt: str, is_last: bool) -> list[str]:
        lines = []
        ts = time.strftime(ts_fmt, time.localtime(entry["timestamp"]))
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
            child_ts = time.strftime("%m-%d %H:%M", time.localtime(child["timestamp"]))
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
        lines.append("   ◉  ◀ 当前")
    return "\n".join(lines)


def format_session_list(sessions: list[dict[str, Any]]) -> str:
    """平铺列表文本（`名称  (YYYY-MM-DD HH:MM, N条消息)`）。

    当前无消费方，作为已发布格式化契约保留（`v2/tests/baseline/data/
    session_store.json` 对照）。空列表返回空串。
    """
    if not sessions:
        return ""
    lines = []
    for s in sessions:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["timestamp"]))
        lines.append(f"  {s['name']}  ({ts}, {s['message_count']}条消息)")
    return "\n".join(lines)
