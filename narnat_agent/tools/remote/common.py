"""Terminal 与 Serial 的共用实现 —— 六组重复函数的合并单点（design D7）。

契约来源：specs/tools-remote「SSH 会话编号与容量」「Serial 连接与会话编号」
「Terminal 状态查询与关闭」「Serial 状态、关闭与清理」「删除命令安全确认」
「兼容性怪癖保持」。

旧实现 → 本模块的合并对应（详见 docs/recast/reports/T3.6_remote_report.md）：

1. 会话注册表：`terminal/__init__.py::_allocate_session_id`（死会话回收）+
   `serial/__init__.py::_allocate_session_id`（占位槽不可抢占）→ `SessionSlots`；
2. 解析骨架：两族 `_resolve_session_id` 的显式编号与自动选择分支 →
   `resolve_explicit_slot` / `resolve_auto_slot`（键匹配规则留在各族，差异显式）；
3. status 骨架：两族 `_status` 的槽位状态段 → `slot_state_label`（空闲计数 →
   `SessionSlots.free_count`）；
4. close 骨架：两族 `_close`（关闭全部 / 指定编号 / 按引用键）→ `close_sessions`
   （键匹配与文案由各族回调显式参数化）；
5. cleanup：两族 `cleanup`（关全部 + 清注册表 + 清活跃标记）→ `SessionSlots.cleanup`；
6. 删除确认：terminal `_exec`/`_input` 内联分支 + serial `_check_delete_safety`
   → `confirm_delete_command`（Terminal 的 git 附加匹配经 `extra_pattern` 参数化）。

共用文本原语（Terminal/Serial 差异显式参数化）：
- `strip_ansi` / `ANSI_RE`：ANSI 转义剥离；
- `merge_cr_line`：行内 `\\r` 覆盖合并；
- `clean_output`：ANSI + `\\r` 覆盖 + 空行压缩（两族在 CRLF 语义与结尾裁剪上分叉）；
- `truncate_middle`：保留首 2/3 与尾 1/3 的中段截断（Terminal 侧附标签吸附与
  ≈token 估算）。
"""
from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable
from typing import Any

from ...contracts.tool import AWAIT_CONFIRM, ToolEnv
from ..signal import error_line, safe_cut_points
from ..token_estimate import estimate_text_tokens

__all__ = [
    "ANSI_RE",
    "CANCEL_TEXT",
    "MARKER_RE",
    "RE_DELETE",
    "SessionSlots",
    "clean_output",
    "close_sessions",
    "confirm_delete_command",
    "match_slots",
    "merge_cr_line",
    "resolve_auto_slot",
    "resolve_explicit_slot",
    "slot_state_label",
    "strip_ansi",
    "truncate_middle",
]

# 删除命令正则（Terminal 与 Serial 同一正则）：边界后跟空白或 /，覆盖无空格变体
# （rd/s、del/f、rmdir/q）及 erase/format；\b 边界防止误伤 delphi、3rd、formatting。
RE_DELETE = re.compile(
    r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)

# 用户拒绝确认时的统一文案（无框架标签：用户选择而非框架失败）。
CANCEL_TEXT = "[操作已取消: 此命令需用户确认]"

# ANSI 转义/DEC 私有序列（Terminal 与 Serial 同一正则）。
ANSI_RE = re.compile(
    r"\x1b\[\??[0-9;]*[a-zA-Z]"
    r"|\x1b\].*?(?:\x07|\x1b\\)"
    r"|\x1b[()][A-Za-z0-9]"
    r"|\x1b[0-9:;<=>?@[A-Z\[\]^_`]"  # DEC私有序列: ESC 7(保存光标), ESC 8(恢复光标)等
)

# SSH 内部标记（哨兵/工作目录探测）：清洗时删除，不得出现在返回文本中。
MARKER_RE = re.compile(r"__NARNAT_(?:MARKER|CWD|PWD)_\d+__")

END_STYLE_HINT = "...[中间截断: 输出共{total}字符, 已保留首{head}字符+尾{tail}字符{hint}。增大max_output_chars可获取完整输出]"


def strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列。"""
    return ANSI_RE.sub("", text)


def merge_cr_line(line: str) -> str:
    """合并单行内的 `\\r` 覆盖：后段覆盖前段（模拟终端行为）。"""
    if "\r" not in line:
        return line
    segments = line.split("\r")
    result = ""
    for seg in segments:
        if len(seg) >= len(result):
            result = seg
        else:
            result = seg + result[len(seg):]
    return result


def clean_output(raw: str, *, crlf_is_newline: bool, strip_markers: bool = False) -> str:
    """输出清洗（两族共用；差异显式参数化）。

    - `crlf_is_newline=True` → Serial 形态：先把 `\\r\\n` 归一为 `\\n`（CRLF 是行
      结束符，非覆盖符），末尾整体 `strip()`；
    - `crlf_is_newline=False` → Terminal 形态：`\\r` 一律参与行内覆盖合并，末尾仅
      去首尾空行与行尾空白（保留行首空格——`echo '  x'` 的行首空格是真实数据）；
    - `strip_markers=True`（Terminal）：在空行压缩之前删除 SSH 内部标记。
    """
    cleaned = strip_ansi(raw)
    if crlf_is_newline:
        cleaned = cleaned.replace("\r\n", "\n")
    cleaned = "\n".join(merge_cr_line(line) for line in cleaned.split("\n"))
    if strip_markers:
        cleaned = MARKER_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() if crlf_is_newline else cleaned.strip("\n").rstrip()


def truncate_middle(text: str, max_chars: int, *, attach_tags: bool) -> str:
    """保留首 2/3 与尾 1/3 的中段截断（尾部含提示符等关键状态信息）。

    - `attach_tags=True` → Terminal 形态：切点做框架标签吸附（`safe_cut_points`，
      标签不允许被切开）并附 `≈token` 估算；`max_chars<=0` 返回带标签错误行；
    - `attach_tags=False` → Serial 形态：无标签吸附与 token 估算，错误行为字面量
      `[错误: max_output_chars需为正整数]`。
    """
    if max_chars <= 0:
        if attach_tags:
            return error_line("max_output_chars需为正整数")
        return "[错误: max_output_chars需为正整数]"
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    if attach_tags:
        head_end, tail_start = safe_cut_points(text, head, len(text) - (max_chars - head))
        # ≈token（AI预算单位，混合密度估算）
        hint = END_STYLE_HINT.format(
            total=len(text), head=head_end, tail=len(text) - tail_start,
            hint=f"(≈{estimate_text_tokens(text)}token)",
        )
        return f"{text[:head_end]}\n{hint}\n{text[tail_start:]}"
    tail = max_chars - head
    hint = END_STYLE_HINT.format(total=len(text), head=head, tail=tail, hint="")
    return f"{text[:head]}\n{hint}\n{text[-tail:]}"


def slot_state_label(alive: bool, busy: bool) -> str:
    """槽位状态段（两族 status 共用）：`[活跃|闲]` / `[已断开|忙]`。"""
    return f"[{'活跃' if alive else '已断开'}|{'忙' if busy else '闲'}]"


def confirm_delete_command(
    command: str,
    env: ToolEnv | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    extra_pattern: re.Pattern[str] | None = None,
) -> str | None:
    """删除类命令的安全确认门（Terminal 与 Serial 共用）。

    命中规则：`工具.rm跳过确认` 关闭且命中删除动词，或（`extra_pattern` 给定、
    `工具.git跳过确认` 关闭且命中该正则）——Terminal 传 git 正则，Serial 不传。

    返回 None 表示放行；返回文本表示本次调用被拦截（该文本即工具结果）：
    - Windows：经 `env.confirm` 同步询问，用户拒绝 → `CANCEL_TEXT`；无回调放行；
    - 其他平台：已确认的一次性标记 → 放行；否则暂存挂起参数并返回 `AWAIT_CONFIRM`；
    - 无 `env`（headless）→ 直接放行。
    """
    if env is None:
        return None
    settings = env.settings
    hit = not settings.rm_skip_confirm and bool(RE_DELETE.search(command))
    if not hit and extra_pattern is not None and not settings.git_skip_confirm:
        hit = bool(extra_pattern.search(command))
    if not hit:
        return None
    if sys.platform == "win32":
        confirm = env.confirm
        if confirm is not None and not confirm(command):
            return CANCEL_TEXT
        return None
    if env.delete_gate.consume_confirmed():
        return None
    env.delete_gate.pend(tool_name, dict(arguments))
    return AWAIT_CONFIRM


class SessionSlots:
    """会话槽位表 —— 0 起连续编号、并发上限（构造钳制 1-10）、死会话回收。

    状态语义（specs/tools-remote）：
    - 槽值 `None` 表示「连接中占位」（Serial 三阶段连接预留；Terminal 不产生占位）；
    - 分配复用空闲槽并回收已断开的死会话（回收时关闭原会话）；用尽返回 -1；
    - 活跃执行集合用于 Serial 的多会话 ESC 打断；`focus` 单引用用于 Terminal 的
      兼容怪癖（最近注册的活跃执行会话，多会话并发时后者覆盖前者）。

    线程安全：所有操作内部持槽位锁；`entries_unlocked` / `lock` 供共享流程在
    单一临界区内组合多步操作（调用者需自行持锁）。
    """

    def __init__(self, max_sessions: int = 5) -> None:
        self.max_sessions = max(1, min(int(max_sessions), 10))
        self._lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._slots: dict[int, Any] = {}
        self._active_sids: set[int] = set()
        self._focus: Any = None

    @property
    def lock(self) -> threading.Lock:
        """槽位锁（配合 `entries_unlocked` 使用：持锁期间可安全读写槽位本体）。"""
        return self._lock

    # ═══════════════════════════════════════════════════════════
    # 槽位读写
    # ═══════════════════════════════════════════════════════════

    def has(self, sid: int) -> bool:
        """该编号是否存在（含连接中占位）。"""
        with self._lock:
            return sid in self._slots

    def get(self, sid: int) -> Any:
        """取槽值（不存在或占位槽均返回 None，用 `has` 区分）。"""
        with self._lock:
            return self._slots.get(sid)

    def entries(self) -> dict[int, Any]:
        """槽位快照（副本；含占位槽的 None 值）。"""
        with self._lock:
            return dict(self._slots)

    def entries_unlocked(self) -> dict[int, Any]:
        """槽位本体（调用者需持有 `lock`；共享流程在临界区内直接增删）。"""
        return self._slots

    def put(self, sid: int, session: Any) -> None:
        """写入槽位（正式会话或连接中占位）。"""
        with self._lock:
            self._slots[sid] = session

    def pop(self, sid: int) -> Any:
        """移除并返回槽值（不存在返回 None）。"""
        with self._lock:
            return self._slots.pop(sid, None)

    def drop_reserved(self, sid: int) -> None:
        """释放未完成连接的占位槽（构造失败路径；正式会话不受影响）。"""
        with self._lock:
            if self._slots.get(sid) is None:
                self._slots.pop(sid, None)

    def free_count(self) -> int:
        """空闲槽位数（占位槽已占用，不计入空闲）。"""
        with self._lock:
            return self.max_sessions - len(self._slots)

    # ═══════════════════════════════════════════════════════════
    # 分配与回收
    # ═══════════════════════════════════════════════════════════

    def allocate_locked(self, is_dead: Callable[[Any], bool]) -> int:
        """分配空闲编号（调用者需持有 `lock`）；返回 -1 表示已满。"""
        for sid in range(self.max_sessions):
            if sid not in self._slots:
                return sid
            session = self._slots[sid]
            if session is None:
                continue  # 连接中的占位槽不可抢占
            if is_dead(session):
                session.close()
                del self._slots[sid]
                return sid
        return -1

    def allocate(self, is_dead: Callable[[Any], bool]) -> int:
        """分配空闲编号（锁内回收死会话）；返回 -1 表示已满。"""
        with self._lock:
            return self.allocate_locked(is_dead)

    def reserve(self, is_dead: Callable[[Any], bool]) -> int:
        """分配编号并立即置占位（连接的三阶段协议：先占位、锁外建连、失败释放）。"""
        with self._lock:
            sid = self.allocate_locked(is_dead)
            if sid >= 0:
                self._slots[sid] = None
            return sid

    def live_pairs(self, is_alive: Callable[[Any], bool]) -> list[tuple[int, Any]]:
        """存活会话列表（锁内判定，按编号升序）。"""
        with self._lock:
            return [(sid, s) for sid, s in sorted(self._slots.items())
                    if s is not None and is_alive(s)]

    def cleanup(self) -> None:
        """关闭全部会话、清空注册表与活跃标记（程序退出清理）。"""
        with self._lock:
            for session in self._slots.values():
                if session is not None:
                    session.close()
            self._slots.clear()
        with self._active_lock:
            self._active_sids.clear()
        self.clear_focus()

    # ═══════════════════════════════════════════════════════════
    # 活跃执行标记（ESC 打断目标）
    # ═══════════════════════════════════════════════════════════

    def add_active(self, sid: int) -> None:
        """登记活跃执行（串口集合语义；Terminal 侧另用 `set_focus`）。"""
        with self._active_lock:
            self._active_sids.add(sid)

    def discard_active(self, sid: int) -> None:
        """注销活跃执行。"""
        with self._active_lock:
            self._active_sids.discard(sid)

    def active_sids(self) -> list[int]:
        """活跃执行编号快照（ESC 打断遍历用）。"""
        with self._active_lock:
            return list(self._active_sids)

    def clear_active(self) -> None:
        """清空活跃执行集合。"""
        with self._active_lock:
            self._active_sids.clear()

    def set_focus(self, session: Any) -> None:
        """登记「最近注册的活跃执行会话」（Terminal 兼容怪癖：单引用覆盖）。"""
        with self._active_lock:
            self._focus = session

    def focus(self) -> Any:
        """取「最近注册的活跃执行会话」（无则 None）。"""
        with self._active_lock:
            return self._focus

    def clear_focus(self) -> None:
        """清空活跃执行会话引用。"""
        with self._active_lock:
            self._focus = None


def match_slots(
    slots: SessionSlots, predicate: Callable[[Any], bool]
) -> list[tuple[int, Any]]:
    """按谓词匹配非占位会话（锁内快照，保持槽位插入顺序）。

    两族的宽松引用匹配（Terminal: IP / `user@IP`；Serial: 端口名）共用本原语。
    """
    with slots.lock:
        return [(sid, s) for sid, s in slots.entries_unlocked().items()
                if s is not None and predicate(s)]


def resolve_explicit_slot(
    slots: SessionSlots,
    session_id: int,
    *,
    missing_msg: Callable[[], str],
    placeholder_msg: Callable[[], str] | None = None,
) -> tuple[int, Any]:
    """显式编号解析（两族共用骨架，文案由各族回调给出）。

    未连接 → `missing_msg()`；连接中的占位槽 → `placeholder_msg()`（缺省按未连接）。
    """
    with slots.lock:
        entries = slots.entries_unlocked()
        if session_id not in entries:
            raise ValueError(missing_msg())
        session = entries[session_id]
        if session is None:
            raise ValueError(placeholder_msg() if placeholder_msg is not None else missing_msg())
    return session_id, session


def resolve_auto_slot(
    slots: SessionSlots,
    *,
    no_session_msg: str,
    multiple_msg: Callable[[list[tuple[int, Any]]], str],
) -> tuple[int, Any]:
    """唯一会话自动选择（两族共用骨架）。

    恰好 1 个活跃会话 → 用它；0 个 / 多个 → 抛出各族文案的 `ValueError`。
    """
    with slots.lock:
        active = [(sid, s) for sid, s in sorted(slots.entries_unlocked().items())
                  if s is not None]
    if len(active) == 1:
        return active[0]
    if not active:
        raise ValueError(no_session_msg)
    raise ValueError(multiple_msg(active))


def close_sessions(
    slots: SessionSlots,
    session_id: int,
    key: str,
    *,
    all_msg: Callable[[int], str],
    sid_missing_msg: Callable[[int], str],
    sid_done_msg: Callable[[int], str],
    placeholder_msg: Callable[[int], str] | None = None,
    key_flow: Callable[[dict[int, Any]], tuple[int | None, str | None]] | None = None,
    clear_active: bool = False,
) -> str:
    """关闭流程骨架（两族共用）：关闭全部 / 指定编号 / 按引用键匹配。

    - `key_flow`：按引用键解析（Terminal: devN 引用与越界/非法判定；Serial: 端口名
      匹配），在锁内调用，返回 `(sid, 提前返回文本)`——sid 为 None 时用文本作结果；
    - `clear_active=True`：关闭全部时清空活跃集合、关闭单个时丢弃该编号（Serial
      语义；Terminal 侧不传，其活跃引用由执行路径自身管理）。
    """
    with slots.lock:
        entries = slots.entries_unlocked()
        if session_id < 0 and not key:
            count = len(entries)
            for session in entries.values():
                if session is not None:
                    session.close()
            entries.clear()
            if clear_active:
                slots.clear_active()
            return all_msg(count)
        if session_id < 0:
            sid, early = key_flow(entries) if key_flow is not None else (None, None)
            if sid is None:
                return early if early is not None else sid_missing_msg(session_id)
            session_id = sid
        if session_id not in entries:
            return sid_missing_msg(session_id)
        session = entries[session_id]
        if session is None:
            del entries[session_id]
            return (placeholder_msg(session_id) if placeholder_msg is not None
                    else sid_missing_msg(session_id))
        session.close()
        del entries[session_id]
        if clear_active:
            slots.discard_active(session_id)
        return sid_done_msg(session_id)
