"""
会话状态机 —— 三态模型：NoSession / RootSession / ChildSession

每个状态类封装自己的行为，命令可用性由类型决定：
  NoSession:   /save(诞生) /cd /rm(任意) /ls /exit(退出agent)
  RootSession: /save(持久化) /cd /rm(仅删儿子) /explore /ls /exit(→NoSession)
  ChildSession: /cd /done /ls /exit(→RootSession)

SessionManager 持有共享资源，状态对象通过 manager 引用访问。
"""

import hashlib
import json
import os
import threading
import time
from typing import Optional, List, Dict, Any, Callable, Set, Tuple

from ..config.session_store import (
    save_session, load_session, delete_session,
    list_sessions_tree, format_session_tree,
    format_session_summary, load_session_meta, session_exists,
    validate_session_name,
    # 文件指纹 = 冲突检测的唯一口径。实现放在 config/session_store（save_session
    # 自己也要用它，顶层反向 import 会成环），这里作为模块级函数对外暴露：
    # 全仓凡是要签名的地方都从这里取，别各写一套。
    file_signature, session_signature,
)
from ..config.defaults import DEFAULT_THINKING_PASSBACK, DEFAULT_THINKING_OPTIONS
from ..config.skill_store import load_skill, list_skill_tree
from ..output import write as _stdout_write, D, R
from ..tools.registry import (
    PLUGIN_TOOL_NAMES as _PLUGIN_TOOL_NAMES, get_capability as _get_capability,
)
from .message_list import MessageList, split_loaded_history


def messages_fingerprint(msgs) -> str:
    """消息列表的内容指纹（全仓唯一的摘要口径，稳定且与键序无关）。

    用途：判断「这次要保存的内容」与「carry_over 已经存下的那一份」是不是
    同一份。不能用布尔标记——它是跨段泄漏的根源：carry_over 置位后没人复位，
    新一段对话（内容明明不同）被误判成"已存过"，整段永不落盘。
    用 sha256(json.dumps(sort_keys=True)) 而非 id/hash()：进程重启后仍可比较，
    且与字典键插入顺序无关（同一份消息即使键序不同也判为相同）。
    """
    try:
        payload = json.dumps(msgs, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(msgs)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _format_messages_text(messages: list) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            if content:
                lines.append(f"AI：{content}")
        elif role == "tool":
            tc_id = m.get("tool_call_id", "")
            label = f"工具返回 [{tc_id}]" if tc_id else "工具返回"
            lines.append(f"{label}：{content}")
        elif role == "system":
            lines.append(f"系统：{content}")
    return "\n".join(lines)


# 「需要让用户看见的提示」通道。终端形态用不上（stderr 就在眼前），
# 面板形态必须注册：_report_save_error 只写 stderr 的话，RPC 模式下
# 那句话只落到扩展的 console.log，用户在界面上完全看不到保存失败——
# 数据丢了都不知道（实测：文件被占用导致写入失败，界面一片安静）。
_NOTIFY_HOOK: Optional[Callable[[str], None]] = None

# 「会话已另存到新名字」通道（P0-1 冲突另存）。core 不 import rpc，
# 由 RPC 层注册后补发一条 session/saved{name, current, switched:true}，
# 让界面把当前会话指到内容实际所在的会话。
_RESAVED_HOOK: Optional[Callable[[str], None]] = None


def set_notify_hook(fn: Optional[Callable[[str], None]]) -> None:
    """注册界面提示通道（RPC 层启动时注册，把提示推成 notice 通知）。"""
    global _NOTIFY_HOOK
    _NOTIFY_HOOK = fn


def set_resaved_hook(fn: Optional[Callable[[str], None]]) -> None:
    """注册「会话因冲突另存」通道（RPC 层启动时注册，推成 session/saved）。"""
    global _RESAVED_HOOK
    _RESAVED_HOOK = fn


# Windows MAX_PATH=260；这里留出会话目录与 ".<pid>.tmp" 后缀的余量
_MAX_PATH_HINT = 240


def readable_save_error(err: str) -> str:
    """把「会话名过长撞 Windows MAX_PATH」的 OSError 翻成用户能看懂的一句话。

    Windows 上 open() 对超长路径报 [Errno 2] No such file or directory
    （WinError 3 被映射成 ENOENT），与「目录不存在」同一个 errno 号——
    用户只看到一句无从下手的系统错误，不知道该改什么。原始错误串保留在
    末尾供排查（errno 与完整路径都在里面）。
    """
    if not err or "errno 2" not in err.lower():
        return err
    paths = err.split("'")[1::2]
    if not paths or max(len(p) for p in paths) < _MAX_PATH_HINT:
        return err
    return ("会话名过长，超出 Windows 路径长度上限，请换短一些的名字后重试"
            f"（原始错误: {err}）")


def _report_save_error(err: str) -> None:
    """会话落盘失败提示：打印到终端，避免保存失败被静默吞掉（数据丢失无感知）"""
    if not err:
        return
    err = readable_save_error(err)
    _stdout_write(f"  {D}⚠ 会话保存失败: {err}{R}\n")
    hook = _NOTIFY_HOOK
    if hook is not None:
        try:
            hook(f"⚠ 会话保存失败: {err}")
        except Exception:
            pass


def _report_conflict_notice(name: str) -> None:
    """「已另存」提示：内容没丢，但不在用户以为的那个会话里了，必须说出来。

    两个窗口切到同一个会话各聊各的时，后写的一方不覆盖对方，而是另存成
    "X (2)"。不说的话用户回到 X 只看到对方的内容，会以为自己刚聊的那轮被
    吞了——正是这个 bug 的原始观感。终端打一行，面板额外推 notice 与
    session/saved（界面据此把当前会话指到新名字）。
    """
    text = f'[提示: 检测到另一个实例正在写该会话，本次内容已另存为 "{name}"]'
    _stdout_write(f"  {D}{text}{R}\n")
    _fire_hook(_NOTIFY_HOOK, text)
    _fire_hook(_RESAVED_HOOK, name)


def _report_exit_save_error(err: str) -> None:
    """退出落盘失败提示。

    退出时用户可能已经看不到界面了，但必须留痕：RPC 模式下 stdout 已被
    改道到 stderr（见 rpc/protocol.py 的 _StdoutProxy），终端形态直接打在
    屏幕上，两种形态都能看到这一行。
    """
    _stdout_write(f"  {D}⚠ 退出前保存会话失败: {readable_save_error(err)}{R}\n")


def _fire_hook(hook, *args) -> None:
    """回调自身异常不能影响保存/退出流程"""
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:
        pass


# 插件工具名称 → 说明（/plugin 状态表展示用；名称与顺序来源 registry.PLUGIN_TOOL_NAMES，
# 说明文案来源工具自身 CAPABILITY 的 plugin_label，缺省回退 label）
_PLUGIN_TOOL_LABELS = {
    name: (_get_capability(name).get("plugin_label") or _get_capability(name)["label"])
    for name in _PLUGIN_TOOL_NAMES
}


def _persist_config(config_dir: str, mutate) -> str:
    """读 narnat.json → mutate(data) → 原子写回。返回 ""=成功；非空=错误信息（改动仅本次会话有效）。

    原子性：写临时文件（含 pid 防并发互撞）后 os.replace——并发写不会产生半个 JSON。
    Windows 下 replace 偶发瞬态拒绝访问（并发写回/外部读取者的共享窗口），
    按 60ms 退避重试 2 次，仍失败才回报错误。
    """
    if not config_dir:
        return ""
    config_path = os.path.join(config_dir, "narnat.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return f"配置读取失败({e})"
    mutate(data)
    tmp_path = f"{config_path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        for attempt in range(3):
            try:
                os.replace(tmp_path, config_path)
                return ""
            except OSError:
                if attempt >= 2:
                    raise
                time.sleep(0.06)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"配置写回失败({e})"


# 会话名保留字：与 /rm 的"全部删除"哨兵值同名（见 session_store.delete_session）。
# 一旦允许保存/改名为该名称，删除它就会清空整个会话库——入口处直接拒绝（大小写不敏感）
_RESERVED_SESSION_NAMES = frozenset(["--all"])

# 会话名长度上限。Windows 单段文件名上限约 255 字符，而名字还要拼上会话库
# 的绝对路径前缀与 ".json"/".json.tmp" 后缀，光看名字本身看不出会超；
# 超了之后报的是 `[Errno 2] No such file or directory: 'E:\...\<300 字>.json'`
# ——一句带完整磁盘路径的系统错误，用户既看不懂也改不动。
# 取 120：留足前缀与后缀余量，又宽于任何正常命名（LLM 命名与首句截断都远短于此）。
_MAX_SESSION_NAME_LEN = 120


def _check_reserved_session_name(name: str) -> str:
    """会话名校验；命中返回错误文案，否则返回""。

    这是所有「写会话名」入口的唯一校验点：/save（NoSession/RootSession）、
    /explore、session/rename、自动命名（AutoSaveManager）、
    新建对话的兜底保存（carry_over / exit_flush）都经这里。别在别处再写一套——
    少堵一处就会在磁盘上留下删不掉的 .tmp 残骸（实测）。
    非法字符、过长、保留字分开报原因，用户才知道该改什么。
    """
    if isinstance(name, str) and len(name) > _MAX_SESSION_NAME_LEN:
        # 长度先判：超长名往下走只会在建文件时炸出带绝对路径的 errno
        return (f"[错误: 会话名过长（最多 {_MAX_SESSION_NAME_LEN} 字，"
                f"当前 {len(name)} 字），请缩短后重试]")
    bad = validate_session_name(name)
    if bad:
        return bad
    if name.strip().lower() in _RESERVED_SESSION_NAMES:
        return f"[错误: '{name}' 为保留名称（/rm --all 表示全部删除），请换一个名称]"
    return ""


def _quick_session_name(msgs, limit: int = 10) -> str:
    """用首条用户消息截断成会话名，不调用 LLM。

    「新建对话」时的兜底保存必须**立刻返回**：走 LLM 命名要同步等一次网络，
    慢的时候「新建」按钮会转很久（实测宿主里连做几段后表现为卡住）。
    后台自动保存仍用 LLM 命名——那是异步线程，不影响交互。
    """
    for m in msgs or []:
        if m.get("role") != "user":
            continue
        text = " ".join(str(m.get("content") or "").split())
        if text:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def unique_session_name(narnat_dir: str, base: str,
                        parent: Optional[str] = None) -> str:
    """把 base 变成会话库里未被占用的名字：撞名就追加 " (2)"、" (3)"…

    全仓**唯一**的撞名兜底，两条自动命名路径都必须走这里：
    `NoSession.carry_over()`（新建对话/切走前的快速名）与
    `AutoSaveManager._do_save()`（轮末的 LLM 命名）。
    此前两处各写一套，弱的那套（重名直接放弃保存）正好落在更关键的
    「新建对话」路径上：默认阈值下第二段整段不落盘，磁盘上无文件、
    界面还显示「上下文已清空」。
    parent 非空 = 按子会话命名空间判重（子会话目录下同名才算撞名）。
    序号到 999 仍撞名时返回最后一个候选，由 save_session 的重名检测
    兜底——绝不静默覆盖既有会话。
    """
    name, i = base, 2
    while session_exists(narnat_dir, name, parent=parent) and i < 1000:
        name, i = f"{base} ({i})", i + 1
    return name


# ── 命令元数据（单表）：{命令: (默认说明, {状态覆盖说明})} ──
# 状态键：no_session / root / child；无覆盖时用默认说明
_COMMAND_META = {
    "/clear":     ("清理屏幕", {}),
    "/compact":   ("压缩上下文", {}),
    "/save":      ("保存当前会话", {}),
    "/ls":        ("显示所有会话", {}),
    "/cd":        ("进入历史会话", {}),
    "/rm":        ("删除会话", {"root": "删除子会话"}),
    "/skill":     ("加载技能", {}),
    "/thinking":  ("切换思考强度", {}),
    "/thinkback": ("思考回传开关", {}),
    "/mode":      ("切换模型", {}),
    "/goal":      ("目标模式开关", {}),
    "/plugin":    ("插件开关", {}),
    "/explore":   ("创建探索分支", {}),
    "/done":      ("完成探索分支", {}),
    "/exit":      ("退出程序", {"root": "退出会话", "child": "暂离探索分支"}),
}

# 各状态可用命令清单（列表顺序 = Tab 补全展示顺序，须稳定）
_STATE_COMMANDS = {
    "no_session": ["/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
                   "/thinking", "/thinkback", "/mode", "/goal", "/plugin", "/exit"],
    "root":       ["/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
                   "/thinking", "/thinkback", "/mode", "/goal", "/plugin",
                   "/explore", "/exit"],
    "child":      ["/clear", "/compact", "/ls", "/cd", "/skill", "/thinking",
                   "/thinkback", "/mode", "/goal", "/plugin", "/done", "/exit"],
}


def _commands_for(state: str) -> Dict[str, str]:
    """由元数据表生成某状态的可用命令表（键序 = 清单顺序）"""
    return {
        cmd: _COMMAND_META[cmd][1].get(state, _COMMAND_META[cmd][0])
        for cmd in _STATE_COMMANDS[state]
    }


class SessionState:
    """状态基类 —— 定义可用命令接口"""

    def available_commands(self) -> Dict[str, str]:
        raise NotImplementedError

    def save(self, name: str) -> str:
        return "当前状态不可用 /save"

    def show(self) -> str:
        raise NotImplementedError

    def enter(self, name: str) -> str:
        raise NotImplementedError

    def delete(self, name: str) -> str:
        return "当前状态不可用 /rm"

    def explore(self, name: str) -> str:
        return "当前状态不可用 /explore"

    def done(self) -> str:
        return "当前状态不可用 /done"

    def exit(self) -> Tuple[str, Optional['SessionState']]:
        """返回 (消息, 新状态)。新状态为 None 表示 agent 应退出。"""
        raise NotImplementedError

    def auto_save(self):
        """落盘当前内容。返回本次自动保存出的会话名（没存则 None）。

        游离态这里什么都不做：轮末每轮都会调它，若在此处保存，未保存的
        对话会「每轮刷出一个新会话」。游离态的兜底保存在
        NoSession.carry_over()，只由「要离开游离态」的动作显式触发
        （新建对话 / `/cd` 切走，两条入口都走 NoSession.enter 或 on_new）。
        """
        return None

    def exit_flush(self) -> Optional[str]:
        """退出前的同步落盘。返回实际写入的会话名（没写则 None）。

        与 auto_save 的区别：**不依赖 LLM 命名**、必须是同步的纯本地写。
        进程马上要 os._exit，后台命名线程等不到，必须先把手上的内容落盘
        （P0-3：轮末自动保存没跑完就关窗口，最后一段整段丢失且退出码为 0）。
        默认无事可做，由各状态自己实现。
        """
        return None

    def reset_after_compact(self):
        """上下文压缩后修正会话基准（默认无基准可修正，no-op）"""
        pass

    def session_name(self) -> Optional[str]:
        return None

    def session_parent(self) -> Optional[str]:
        return None

    def is_child(self) -> bool:
        return False


class NoSession(SessionState):
    """游离态 —— 不在任何会话中，可创建根会话、删除任意会话"""

    def __init__(self, mgr: 'SessionManager'):
        self._mgr = mgr

    def available_commands(self) -> Dict[str, str]:
        return _commands_for("no_session")

    def save(self, name: str) -> str:
        # 完整历史（归档 + 当前）：与 _persist / carry_over / exit_flush 同一口径。
        # 用 get_messages()（压缩后的上下文）另存会让新文件丢掉压缩前的原文。
        msgs = self._mgr.get_full_messages()
        if not any(m.get("role") == "user" for m in msgs):
            return "没有对话内容，无需保存"
        if not name:
            if self._mgr.name_func:
                name = self._mgr.name_func(msgs)
            if not name:
                return "自动命名失败，请手动指定: /save <名称>"
        err = _check_reserved_session_name(name)
        if err:
            return err
        if session_exists(self._mgr.narnat_dir, name):
            return (f"[错误: 会话 '{name}' 已存在，请换名保存，或 /cd {name} 进入已有会话"
                    f"（如需覆盖请先 /rm {name} 删除）]")
        # 用「占名」而不是「预检 + 直接写」（P0，第五轮 R40 实测）：
        # 原来这里是 session_exists 预检通过后 save_session(name)（无 expected_signature），
        # 于是两个后端进程（两个 VSCode 窗口）同时建同名会话时**都通过预检、
        # 都收到成功回执**，后 os.replace 的那个把先写的整段覆盖掉 ——
        # 用户在窗口 A 存的东西在窗口 B 保存后凭空消失，而且两边都显示「已保存」。
        # 传 expected_signature="" 表示「期望这个名字此刻还不存在」：
        # save_session 会在写盘前校验，已被别人抢先就 conflict → 改落 "X (2)"，
        # 谁的内容都不会丢。这是该函数本来就有的语义，只是这里没用。
        out: Dict[str, Any] = {}
        err = save_session(self._mgr.narnat_dir, name, msgs,
                           expected_signature="", result=out)
        if err:
            return readable_save_error(err)
        saved = out.get("name") or name
        if out.get("conflict") and saved != name:
            # 抢名失败但内容已另存：必须告诉用户实际存到了哪个名字，
            # 否则他之后按原名 /cd 会进到别人的会话里。
            _report_conflict_notice(saved)
        new_state = self._mgr.create_root_state(saved)
        self._mgr.switch_state(new_state)
        return ""

    def carry_over(self):
        """离开游离态前的兜底保存：把游离态这段内容存成一条会话。

        由「要离开游离态」的动作显式调用：`on_new`（新建对话）与
        `NoSession.enter`（`/cd` 切走）；`session/switch` 经 on_enter 走同一条
        `enter`，不再单独调一次（两处都调会存出两份）。**不**覆盖 auto_save：
        轮末的 on_auto_save 也会调 auto_save，若把逻辑放进 auto_save，
        游离态就变成「每轮结束都存一个新会话」，会刷出一堆碎会话
        （实测过：「记忆蓝莓」「蓝莓记忆测试」并存）。

        返回存出的会话名（没存则 None），供界面告诉用户存到哪了。
        不切状态：调用方随后自己把状态换成新的 NoSession / 目标会话。
        """
        # 完整历史（归档 + 当前）：与 _persist / AutoSaveManager 的写盘口径
        # 一致——三处任一少了归档，压缩过的历史就会漏一段；指纹也必须基于
        # 同一份内容，否则 carry_over 与后台自动保存互相认不出、重复写一份。
        msgs = self._mgr.get_full_messages()
        if not any(m.get("role") == "user" for m in msgs):
            return None  # 只有 system prompt，没什么可存
        if self._mgr._auto_save_done and self._mgr._pending_auto_save_name:
            # 后台自动保存已经落盘（名字就绪，等 wait 消费）→ 不必再存一份
            return None
        # 注意这里不能只判断 _auto_save_done：
        # 它只是「线程已启动」，线程可能还在跑 LLM 命名、甚至已失败。
        # 此时若跳过保存，用户点「新建」就丢了这一段（实测宿主里连做几段后复现）。
        # 用内容指纹通知后台线程：这段已由这里存下，别重复写（比布尔标记严格——
        # 只有后台线程要存的内容与这里存的完全相同才算"重复"）。
        # 用快速截断命名，不走 LLM：这个调用发生在「新建对话」的同步路径上，
        # 走 LLM 会让按钮转很久（实测连做几段后像卡死）。
        base = _quick_session_name(msgs)
        if not base or _check_reserved_session_name(base):
            return None
        # 撞名（同一句开头连着开好几段时必然发生）必须追加序号照样存下，
        # 不能像以前那样直接放弃——那是整段对话永久不落盘。
        # expected_signature=""：这段内容本该落到一个还不存在的文件，由
        # save_session 用 O_EXCL 原子占名字——两个实例同时给同一句开头保存时，
        # 后到的一方不会覆盖先到的，而是自动另存成 "X (2)"。
        name = unique_session_name(self._mgr.narnat_dir, base)
        out: Dict[str, Any] = {}
        try:
            err = save_session(self._mgr.narnat_dir, name, msgs,
                               expected_signature="", result=out)
        except Exception:
            return None
        if err:
            _report_save_error(err)
            return None
        name = out.get("name") or name
        # 通知后台自动保存线程：这段已经被这里存下，别再写一份
        self._mgr._carried_over_fingerprint = messages_fingerprint(msgs)
        self._mgr._last_saved_fingerprint = self._mgr._carried_over_fingerprint
        # 记下「存成了哪个名字」：后台线程指纹命中（内容与这份完全相同）时
        # 要把会话状态切到这份会话，否则用户停在游离态、后续内容谁都不管
        self._mgr._carried_over_name = name
        return name

    def exit_flush(self) -> Optional[str]:
        """退出兜底：用快速截断名把这一段同步存下来（不调 LLM）。

        与 carry_over 同一套逻辑（快速名 + unique_session_name + save_session），
        多一条判据「这段内容已经在磁盘上了」：轮末自动保存的后台线程可能已经
        写过同一个指纹而状态还没切过来（切状态在写盘之后），此时再存一份就会
        凭空多出一个会话文件——正常退出不该产生多余文件。
        """
        msgs = self._mgr.get_full_messages()
        if not any(m.get("role") == "user" for m in msgs):
            return None   # 只有 system prompt，没什么可存
        # 「已在磁盘上」的判据用内容指纹，两种口径都比一次：轮末自动保存线程
        # 写的是当前上下文（get_messages），这里写的是完整历史（含压缩归档），
        # 压缩过的会话两者不同，只比一种会把已落盘的这一段再存一份。
        saved_fp = self._mgr._last_saved_fingerprint
        fingerprint = messages_fingerprint(msgs)
        if saved_fp and saved_fp in (fingerprint,
                                     messages_fingerprint(self._mgr.get_messages())):
            return None
        base = _quick_session_name(msgs)
        if not base or _check_reserved_session_name(base):
            return None
        name = unique_session_name(self._mgr.narnat_dir, base)
        out: Dict[str, Any] = {}
        err = save_session(self._mgr.narnat_dir, name, msgs,
                           expected_signature="", result=out)
        if err:
            _report_exit_save_error(err)
            return None
        name = out.get("name") or name
        self._mgr._last_saved_fingerprint = fingerprint
        # 通知可能还在跑的后台命名线程：这段已存下，别再写一份（与 carry_over 同一机制）
        self._mgr._carried_over_fingerprint = fingerprint
        self._mgr._carried_over_name = name
        return name

    def show(self, args: str = "") -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        if args == "--all":
            result = format_session_tree(tree, None, None)
        else:
            result = format_session_summary(tree, None, None)
        if not result:
            return ""
        from ..ui.colors import C, R, X
        result = result.replace("◀ 当前", f"{C}◀ 当前{R}")
        if self._mgr.pending_deletes:
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "[错误: 请指定会话名称]"
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        # 切走之前先把游离态这一段兜底存下来：与 on_new / session/switch 完全一致，
        # 复用同一个 carry_over（不复制逻辑）。必须赶在 replace_messages 之前——
        # 之后 MessageList 已经是目标会话的内容，刚聊的这段再也取不回来
        # （P0：默认 自动保存=false 时状态长期停在 NoSession，敲 /cd 就整段丢）。
        saved = None
        try:
            saved = self.carry_over()
        except Exception:
            saved = None
        # 存成了哪个名字要如实告诉用户：终端直接打这一行（RPC 形态下同一次
        # print 被 exec_command 捕获进 /cd 回执的 lines），名字另存进
        # _last_auto_saved，供 RPC 回执的 autoSaved 字段用（core 不 import rpc）。
        self._mgr._last_auto_saved = saved
        if saved:
            _stdout_write(f"  {D}上一段对话已自动保存为: {saved}{R}\n")
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def delete(self, name: str) -> str:
        if not name:
            return "[错误: 请指定会话名称]"
        if name == "--all":
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                self._mgr.pending_deletes.add((root["name"], None))
                for child in root.get("children", []):
                    self._mgr.pending_deletes.add((child["name"], root["name"]))
            return ""
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        self._mgr.pending_deletes.add((target_name, target_parent))
        if target_parent is None:
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                if root["name"] == target_name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], target_name))
                    break
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:

        return ("", None)


class RootSession(SessionState):
    """根会话 —— 可创建子会话、删除自己的儿子"""

    def __init__(self, mgr: 'SessionManager', name: str):
        self._mgr = mgr
        self._name = name
        self._status: Optional[str] = None
        self._summary: Optional[str] = None
        # 基线与 _persist 的比较口径必须一致（全量历史）：压缩过的会话载入后
        # 「当前上下文」只是摘要 + 保留尾部，按它取基线会让下一次落盘把
        # 「历史本来就更多」误判成「有新内容」，把 completed 状态刷成 active
        self._msg_count: int = len(mgr.get_full_messages())
        # 本进程最近一次读/写这个会话文件时的指纹：_persist 用它判断磁盘是否
        # 已被另一个实例改过（两个窗口切到同一会话各聊各的 → 后写者整段覆盖）。
        self._sig: str = session_signature(mgr.narnat_dir, name)

    def available_commands(self) -> Dict[str, str]:
        return _commands_for("root")

    def _persist(self) -> str:
        """落盘当前会话，返回 ""=成功 / 非空=可读失败原因。

        带上「上次读/写该文件时的指纹」写盘：磁盘指纹与之不符说明另一个实例
        （另一个窗口/后端）也在这个会话里写，直接覆盖会让对方刚写的那一整段
        静默消失。此时 save_session 改为另存成 "X (2)"，这里把状态与提示
        一并切到新名字（否则下一轮又会另存一份）。

        ## 「自己人」不能判成「另一个实例」（实测过的假警报）

        同一进程里有两条保存路径：轮末自动保存（`agent.py` 的 `on_auto_save`）
        和用户 `/save`（RPC 主线程）。两者在**同一个旧 `_sig`** 上先后进入
        `save_session`，中间只隔一次 `json.dump`（2MB ≈16ms）。后落盘的那次
        看到指纹已变，`_write_session_json` 报 `raced=True`，于是被判成
        「另一个实例正在写」→ 另存 `X (2)` + 弹假警报。实测确定性复现 3/3，
        自然时序 6/6；等 400ms 再保存则 0/3（窗口就那么大）。

        所以这里做的是**串行化**：`_persist` 整段（读 `_sig` → 写盘 → 写回 `_sig`）
        放进一把按会话名分片的锁里。后进的那次看到的 `_sig` 已经是前一次写完后
        更新过的值，指纹自然对得上，不会再误判，也不会另存。

        锁按会话名分片而不是全局：保存 A 不能阻塞保存 B。锁内只有一次
        `json.dump`（2MB ≈16ms），不碰 LLM / 压缩 / 命名。

        跨进程保护一点不退：真·另一个窗口写进来的指纹不会等于本进程的 `_sig`，
        该冲突的照旧冲突、该另存的照旧另存。
        """
        # 落盘用完整历史（归档 + 当前）：压缩只替换「发给模型的上下文」，
        # 用户看得见的会话文件必须保留压缩前的原文，否则翻历史就少了内容。
        msgs = self._mgr.get_full_messages()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        # 整段放进按会话名分片的锁里：并发的两次 _persist（轮末自动保存 +
        # /save）必须串行，后进的那次才能看到前一次刚更新的 _sig，
        # 从而不会把「自己人刚写的」判成「外部改动」。
        with self._mgr.persist_lock(self._name):
            return self._persist_locked(msgs)

    def _persist_locked(self, msgs: List[Dict[str, Any]]) -> str:
        """`_persist` 的锁内部分（调用方必须已持锁）。"""
        out: Dict[str, Any] = {}
        err = save_session(self._mgr.narnat_dir, self._name, msgs,
                           status=self._status or "active",
                           summary=self._summary,
                           expected_signature=self._sig,
                           result=out)
        self._msg_count = len(msgs)
        if err:
            _report_save_error(err)
            self._mgr._last_save_error = err
            return err
        self._mgr._last_save_error = ""
        # 写完立刻更新签名：自己紧接着再写一次不能算「被外部改动」（同进程不误判）
        self._sig = out.get("signature") or self._sig
        if out.get("conflict"):
            self._adopt_saved_name(out.get("name"))
        return ""

    def _adopt_saved_name(self, name: Optional[str]) -> None:
        """冲突另存后把状态指到实际写入的那个会话，并告诉用户内容去了哪。"""
        if not name or name == self._name:
            return
        self._name = name
        _report_conflict_notice(name)

    def save(self, name: str) -> str:
        # _persist 的失败原因必须如实回给调用方：吞掉就等于「写盘失败仍回
        # ok:true + 会话已保存」（P1，实测 WinError 5 时依然报成功）。
        if not name:
            return readable_save_error(self._persist())
        if name == self._name:
            return readable_save_error(self._persist())
        else:
            err = _check_reserved_session_name(name)
            if err:
                return err
            if session_exists(self._mgr.narnat_dir, name):
                return (f"[错误: 会话 '{name}' 已存在，请换名保存，或 /cd {name} 进入已有会话"
                        f"（如需覆盖请先 /rm {name} 删除）]")
            # 完整历史（归档 + 当前）：与 _persist 同一口径。用压缩后的上下文
            # 另存会让新文件丢掉压缩前的原文（P1，实测）。
            msgs = self._mgr.get_full_messages()
            # 同 NoSession.save：用「占名」防跨进程抢名后互相覆盖（P0，R40 实测）。
            out_new: Dict[str, Any] = {}
            err = save_session(self._mgr.narnat_dir, name, msgs,
                               expected_signature="", result=out_new)
            if err:
                return readable_save_error(err)
            saved = out_new.get("name") or name
            if out_new.get("conflict") and saved != name:
                _report_conflict_notice(saved)
            new_state = self._mgr.create_root_state(saved)
            self._mgr.switch_state(new_state)
        return ""

    def show(self, args: str = "") -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        if args == "--all":
            result = format_session_tree(tree, self._name, None)
        else:
            result = format_session_summary(tree, self._name, None)
        from ..ui.colors import C, R, X
        result = result.replace("◀ 当前", f"{C}◀ 当前{R}")
        if self._mgr.pending_deletes:
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "[错误: 请指定会话名称]"
        self._persist()
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def delete(self, name: str) -> str:
        if not name:
            return "[错误: 请指定会话名称]"
        if name == "--all":
            tree = list_sessions_tree(self._mgr.narnat_dir)
            for root in tree:
                if root["name"] == self._name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], self._name))
                    return ""
            return "会话不存在"
        # 处理 parent/child 路径格式（tab 补全可能产生此格式）
        if "/" in name:
            parts = name.split("/", 1)
            parent_name, child_name = parts[0], parts[1]
            if parent_name != self._name:
                return f"'{name}' 不是当前会话的子会话"
            name = child_name
        tree = list_sessions_tree(self._mgr.narnat_dir)
        for root in tree:
            if root["name"] == self._name:
                for child in root.get("children", []):
                    if child["name"] == name:
                        self._mgr.pending_deletes.add((name, self._name))
                        return ""
                return f"'{name}' 不是当前会话的子会话"
        return f"会话不存在: {name}"

    def explore(self, name: str) -> str:
        """创建探索分支 / 进入已有分支。

        分支文件已存在时按状态分流，**绝不**再用父会话快照覆盖它：
        completed 一律拒绝，active/new 走「继续之前的分支」（读回分支自己
        的内容）。此前 completed 被当作「允许重开」而直接覆盖，一次同名
        /explore 就把已完成分支的原文从磁盘上永久抹掉（P0，实测重开
        「分支甲」后 C02 全库零命中）。
        """
        name = (name or "").strip()
        if not name:
            # 不带名：用父会话内容自动命名；撞名由 unique_session_name 换序号
            name = self._auto_branch_name()
            if not name:
                return ("[错误: 父会话还没有对话内容，无法自动命名分支，"
                        "请手动指定: /explore <名称>]")
        err = _check_reserved_session_name(name)
        if err:
            return err
        if name == self._name:
            return f"[错误: 分支名不可与父会话同名（'{name}'），请换一个名称]"
        if session_exists(self._mgr.narnat_dir, name, parent=self._name):
            return self._reenter_branch(name)
        return self._create_branch(name)

    def _auto_branch_name(self) -> str:
        """自动命名分支：父会话首条用户消息截断，在子会话命名空间内取未占用序号。"""
        base = _quick_session_name(self._mgr.get_full_messages())
        # 首句为空、与父会话同名（子命名空间里也会撞名）、或本身非法时才换种子名
        if not base or base == self._name or _check_reserved_session_name(base):
            base = "分支"
        return unique_session_name(self._mgr.narnat_dir, base, parent=self._name)

    def _create_branch(self, name: str) -> str:
        """全新分支：以父会话当前快照为起点建文件（仅在此名字还不存在时调用）。"""
        self._persist()
        # 分支文件写父会话的完整历史（归档 + 当前）：父会话压缩过时
        # get_messages() 只剩「摘要 + 保留尾部」，拿它当起点等于把父会话压缩前
        # 的原文从分支里删掉（与 done() 同一个 P0 类缺陷）
        msgs = self._mgr.get_full_messages()
        # 增量基准必须是当前上下文的下标（done() 在上下文上切片，压缩后由
        # reset_after_compact 重置到摘要边界）；用全量条数会让 target 恒为空
        parent_msg_count = len(self._mgr.get_messages())
        err = save_session(self._mgr.narnat_dir, name, msgs, parent=self._name,
                           status="new", parent_msg_count=parent_msg_count,
                           last_summarized_at=parent_msg_count)
        if err:
            return readable_save_error(err)
        new_msgs, err = load_session(self._mgr.narnat_dir, name, parent=self._name)
        if err:
            return err
        self._mgr.replace_messages(new_msgs)
        new_state = self._mgr.create_child_state(name, self._name)
        self._mgr.switch_state(new_state)
        return ""

    def _reenter_branch(self, name: str) -> str:
        """进入已存在的分支（继续之前那次探索）。

        completed = 已完成并合并：拒绝。拿父会话快照覆盖它，等于把那整段
        探索原文从磁盘上抹掉，用户再也找不到（P0）。
        active / new = 还活着：读回分支文件自己的内容重建子会话状态，
        不写分支文件一个字节。
        """
        meta = load_session_meta(self._mgr.narnat_dir, name, parent=self._name)
        if meta.get("status") == "completed":
            return (f"[错误: 分支 '{name}' 已完成并合并，重新创建会覆盖它的原文。"
                    f"请换个名字，或先 /rm {self._name}/{name} 删除它]")
        self._persist()
        new_msgs, err = load_session(self._mgr.narnat_dir, name, parent=self._name)
        if err:
            return err
        self._mgr.replace_messages(new_msgs)
        new_state = self._mgr.create_child_state(name, self._name)
        self._mgr.switch_state(new_state)
        # 回执必须说清这是「重进」而不是新建：终端打这一行（RPC 形态下同一行
        # 被 exec_command 收进回执 lines），名字与条数照实给，用户才知道
        # 自己看到的是之前那段探索
        _stdout_write(f"  {D}已重新进入已有分支: {name}"
                      f"（{len(new_msgs)} 条消息，/done 合并结论）{R}\n")
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:
        # 退出会话前先把当前内存落盘：打断轮（aborted 不触发轮末自动保存）
        # 的消息此前会静默丢失（/cd 回来不可见、无提示）
        self._persist()
        return ("", NoSession(self._mgr))

    def auto_save(self):
        """落盘并回传会话名（供 on_new 告诉界面「上一段已存为 X」）。

        已在会话里时这一段是写回原文件而非新建，但界面同样需要知道内容落在
        哪个会话：返回 None 会让 session/new 的 autoSaved 恒为 null（阈值 0
        时轮末自动保存已先切到会话，carry_over 不再发生），用户就以为这段
        没保存。空对话不回报名字，与 carry_over 的「没什么可存」同一口径。
        落盘失败时必须回 None（失败原因已由 _persist → _report_save_error 推给
        界面）：回名字就等于告诉用户「已自动保存为 X」，而磁盘上什么都没有（实测）。
        """
        if not any(m.get("role") == "user" for m in self._mgr.get_messages()):
            return None
        if self._persist():
            return None
        return self._name

    def exit_flush(self) -> Optional[str]:
        """退出兜底：直接写回自己这个文件（不新建会话，重复退出也只多一次覆盖写）。"""
        err = self._persist()
        return None if err else self._name

    def session_name(self) -> Optional[str]:
        return self._name

    def session_parent(self) -> Optional[str]:
        return None


class ChildSession(SessionState):
    """子会话 —— 可合并结论回父会话"""

    # ── 探索分支合并常量（原模块级常量收敛于此，仅本类 done() 使用）──
    BOUNDARY_MARKER_PREFIX = "━━━ 探索分支开始"
    # 结构化检查点模板（对齐 COMPRESS_PROMPT 的固定小节骨架，见 config/defaults.py）：
    # 固定小节顺序防止合并结论漏掉续跑最关键的信息；每条小节只写相对主分支的增量。
    SUMMARY_TASK_TEMPLATE = """# 任务：探索分支增量合并

你现在是压缩引擎。把子分支的探索过程浓缩成一个结构化检查点，只将**有价值的增量信息**合并回主分支，使另一个模型可以无损接续工作，同时保持主会话简洁。

## 1. 主分支当前状态
这是主分支已知的上下文，视为“基准真理”，增量判定以它为基准。
{memory}

## 2. 子分支探索日志
这是在子分支中进行的调试、验证、试错过程。其中若包含主分支已知的内容（尤其是与上方摘要重复的部分），视为已知，不计入增量。
{target}

## 3. 输出结构
严格按下面的 Markdown 结构输出：保持每一节的顺序，用简练的要点而非散文。空节写“(无)”，不得省略任何一节。
除“原始请求与意图”外，每节只记录**相对主分支已知信息的新增量**：与主分支重复的内容一律不写；主分支已记录的事实不要原文照抄，只写新增、变化或修正的部分。

## 原始请求与意图
- [子分支本次探索的目标（即使与主分支相同也写明，供接续定位）；措辞重要处原文引用]

## 关键技术概念
- [本次探索新涉及的技术、框架、模式与约定]

## 文件与代码
- [精确路径：其重要性、关键改动或代码片段]

## 错误与修复
- [错误：如何解决的，以及相关用户反馈；失败的尝试一句话带过（如“排除了方案A，因为…”）]

## 待办任务
- [本次探索暴露的、尚未完成的工作]

## 当前工作
- [子分支结束时正在进行的精确工作]

## 下一步
- [与主分支衔接的单一动作，或“(无)”]

## 关键上下文
- [决策及其理由、约束、用户偏好、未决问题、继续所需的数据]

规则：
- 用简洁的中文工程语言。保留精确的文件路径、命令、报错串、标识符、数值、函数签名与语法片段。
- 忠实记录用户反馈与明确指令，尤其是纠正。
- 不要提及本次合并请求，也不要提及这是子分支的探索过程。
- 只输出检查点文本：不要调用任何工具或采取其他行动。
- 总长不超过 1500 字：宁可少写，不可废话。
- 若所有小节均为“(无)”——即探索没有产生任何新结论、只是重复或确认了主分支已知信息——则整段输出仅一行：`[无实质性更新]`（不要输出任何小节）。
"""

    def __init__(self, mgr: 'SessionManager', name: str, parent: str):
        self._mgr = mgr
        self._name = name
        self._parent = parent
        meta = load_session_meta(self._mgr.narnat_dir, self._name, parent=self._parent)
        self._status: str = meta.get("status", "active")
        self._summary: Optional[str] = meta.get("summary")
        self._parent_msg_count: int = meta.get("parent_msg_count") or 0
        self._last_summarized_at: int = (meta.get("last_summarized_at")
                                         or self._parent_msg_count)
        # 同 RootSession._msg_count：基线取全量历史，与 ChildSession._persist 同口径
        self._msg_count: int = len(mgr.get_full_messages())
        # 本进程最近一次读/写该分支文件时的指纹（同 RootSession._sig）
        self._sig: str = session_signature(mgr.narnat_dir, name, parent=parent)

    def available_commands(self) -> Dict[str, str]:
        return _commands_for("child")

    def _persist(self):
        # 落盘用完整历史（归档 + 当前），理由同 RootSession._persist
        msgs = self._mgr.get_full_messages()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        # 同 RootSession：串行化「读指纹 → 写盘 → 写回指纹」，避免自己人误判。
        # 锁键带父会话名，避免不同父下的同名分支互相阻塞。
        with self._mgr.persist_lock((self._parent or "") + "/" + self._name):
            return self._persist_locked(msgs)

    def _persist_locked(self, msgs):
        out: Dict[str, Any] = {}
        err = save_session(self._mgr.narnat_dir, self._name, msgs,
                           parent=self._parent,
                           status=self._status or "active",
                           summary=self._summary,
                           parent_msg_count=self._parent_msg_count,
                           last_summarized_at=self._last_summarized_at,
                           expected_signature=self._sig,
                           result=out)
        self._msg_count = len(msgs)
        if err:
            _report_save_error(err)
            self._mgr._last_save_error = err
            return err
        self._mgr._last_save_error = ""
        self._sig = out.get("signature") or self._sig
        if out.get("conflict"):
            self._adopt_saved_name(out.get("name"))
        return ""

    def _adopt_saved_name(self, name: Optional[str]) -> None:
        """冲突另存后把状态指到实际写入的分支名（仍在同一个父会话下）。"""
        if not name or name == self._name:
            return
        self._name = name
        _report_conflict_notice(name)

    def show(self, args: str = "") -> str:
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        if args == "--all":
            result = format_session_tree(tree, self._name, self._parent)
        else:
            result = format_session_summary(tree, self._name, self._parent)
        from ..ui.colors import C, R, X
        result = result.replace("◀ 当前", f"{C}◀ 当前{R}")
        if self._mgr.pending_deletes:
            result = result.replace("✘ 退出后删除", f"{X}✘ 退出后删除{R}")
        return result

    def enter(self, name: str) -> str:
        if not name:
            return "[错误: 请指定会话名称]"
        self._persist()
        target_name, target_parent, err = self._mgr.resolve_session_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.load_child_with_boundary(target_name, target_parent)
            if load_err:
                return load_err
        else:
            new_msgs, load_err = load_session(self._mgr.narnat_dir, target_name)
            if load_err:
                return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            new_state = self._mgr.create_child_state(target_name, target_parent)
        else:
            new_state = self._mgr.create_root_state(target_name)
        self._mgr.switch_state(new_state)
        return ""

    def done(self) -> str:
        # 不做"completed 一律拒绝"的提前守卫：是否有可合并增量统一由下方
        # target 为空判断（_persist 已把有续聊消息的 completed 恢复为 active，
        # 提前守卫会与"续聊后第二轮合并"语义自相矛盾，并在无新消息时误报
        # "不可重复 /done"）
        # 两条口径分工必须分清：
        #   msgs      = 当前上下文（发给模型的那份），parent_msg_count /
        #               last_summarized_at 都是它的下标（压缩后由
        #               reset_after_compact 重置到摘要边界），切片只能用它；
        #   full_msgs = 完整历史（归档 + 当前），写分支文件只能用它——用压缩后
        #               的上下文写盘会把分支文件从 15 条截成 4 条，压缩前原文
        #               永久消失（P0，自动压缩与手动 /compact 各复现一次）
        msgs = list(self._mgr.get_messages())
        full_msgs = self._mgr.get_full_messages()
        parent_msg_count = self._parent_msg_count
        if parent_msg_count == 0:
            for i, m in enumerate(msgs):
                if m.get("role") == "system" and ChildSession.BOUNDARY_MARKER_PREFIX in m.get("content", ""):
                    parent_msg_count = i
                    break

        last_summarized_at = self._last_summarized_at
        if last_summarized_at < parent_msg_count:
            last_summarized_at = parent_msg_count

        memory = msgs[:parent_msg_count]
        target = msgs[last_summarized_at:]

        if not target:
            return "没有新的讨论内容需要总结"

        memory_text = _format_messages_text(memory)
        target_text = _format_messages_text(target)
        task_content = ChildSession.SUMMARY_TASK_TEMPLATE.format(memory=memory_text, target=target_text)
        summary_msgs = [{"role": "user", "content": task_content}]

        if self._mgr.summary_anim_start:
            self._mgr.summary_anim_start()
        summary = ""
        if self._mgr.summarize_func:
            summary = self._mgr.summarize_func(summary_msgs, self._mgr.cancel_check)
        if self._mgr.summary_anim_stop:
            self._mgr.summary_anim_stop()
        if not summary:
            return "总结取消或失败"

        parent_msgs, err = load_session(self._mgr.narnat_dir, self._parent)
        if err:
            return f"无法加载父会话: {err}"
        round_num = sum(1 for m in parent_msgs
                        if m.get("role") == "system"
                        and f"子会话 [{self._name}]" in m.get("content", "")) + 1
        round_label = f" (第{round_num}轮)" if round_num > 1 else ""
        parent_msgs.append({"role": "system",
            "content": f"# 子会话 [{self._name}]{round_label} 结论\n\n{summary}"})
        err = save_session(self._mgr.narnat_dir, self._parent, parent_msgs)
        if err:
            # 父会话没写成就绝不能把分支标成 completed：结论没落到父会话里，
            # 标了之后「已完成」与实际内容不符，用户还收到「结论已合并」的假成功
            # （P1）。这里如实回错误，分支状态与增量基准原样保留，可以重试 /done。
            _report_save_error(err)
            return (f"合并失败：父会话「{self._parent}」写入失败"
                    f"（{readable_save_error(err)}）；分支未标记完成，可重试 /done")

        self._last_summarized_at = len(msgs)
        self._status = "completed"
        self._summary = summary
        # 分支文件写内存全量（权威版本）：磁盘版可能落后于内存（消息未持久化
        # 时读取会把它回退为旧版，而 last_summarized_at 按内存长度记，内容与
        # 基准错位导致后续增量被静默跳过）。全量 = 归档 + 当前（压缩前的原文
        # 在归档里），与 _persist / carry_over / exit_flush 同一口径
        err = save_session(self._mgr.narnat_dir, self._name, full_msgs,
                           parent=self._parent, status="completed", summary=summary,
                           parent_msg_count=self._parent_msg_count,
                           last_summarized_at=self._last_summarized_at)
        _report_save_error(err)
        # 这次写盘不经过 _persist，签名要跟着更新，否则后续 _persist 会把「自己
        # 刚写的」判成被外部改动，又另存一份 "X (2)"
        self._sig = session_signature(self._mgr.narnat_dir, self._name,
                                      parent=self._parent)

        self._mgr.replace_messages(parent_msgs)
        new_state = self._mgr.create_root_state(self._parent)
        self._mgr.switch_state(new_state)
        return ""

    def exit(self) -> Tuple[str, Optional[SessionState]]:
        # 暂离分支前先把分支当前状态落盘（同上：打断轮消息不丢），
        # 再切回父会话内存
        self._persist()
        parent_msgs, err = load_session(self._mgr.narnat_dir, self._parent)
        if err:
            return (err, None)
        self._mgr.replace_messages(parent_msgs)
        new_state = self._mgr.create_root_state(self._parent)
        self._mgr.switch_state(new_state)
        return ("", new_state)

    def auto_save(self):
        """落盘并回传会话名（与 RootSession.auto_save 同一口径）。

        返回 None 会让分支内「新建对话」的 `autoSaved` 恒为 null：内容其实已经
        写进 `父/分支.json`，界面却说不出存在哪（实测 E1 FAIL）。名字是
        session_name() 的裸名，父会话由 session_parent() 提供，与 RootSession
        回裸名、上层用 `父/子` 拼 current 的既有约定一致。
        空对话不回报名字；落盘失败必须回 None（原因已由 _persist →
        _report_save_error 推给界面）：回名字等于谎报「已自动保存为 X」。
        """
        if not any(m.get("role") == "user" for m in self._mgr.get_messages()):
            return None
        if self._persist():
            return None
        return self._name

    def exit_flush(self) -> Optional[str]:
        """退出兜底：直接写回自己的分支文件（不新建会话、不切状态）。"""
        err = self._persist()
        return None if err else self._name

    def reset_after_compact(self):
        """压缩后重设增量合并基准。

        压缩把消息列表整体替换为 [system_prompt, 摘要, 保留尾部...]，
        而 _parent_msg_count / _last_summarized_at 是压缩前的消息下标，
        失配会让 /done 静默取不到增量（或把分支讨论计入父基准）。
        重置到摘要边界：memory=压缩摘要（含父基准与早期分支内容），
        target=压缩后逐字保留的分支近期讨论。
        """
        msgs = self._mgr.get_messages()
        boundary = 0
        while boundary < len(msgs) and msgs[boundary].get("role") == "system":
            boundary += 1
        self._parent_msg_count = boundary
        self._last_summarized_at = boundary

    def session_name(self) -> Optional[str]:
        return self._name

    def session_parent(self) -> Optional[str]:
        return self._parent

    def is_child(self) -> bool:
        return True


class SessionManager:
    """会话管理器 —— 持有共享资源，管理状态切换和延迟删除"""

    def __init__(self, narnat_dir: str,
                 messages: 'MessageList',
                 config_dir: str = "",
                 thinking_effort_getter: Callable[[], str] = None,
                 thinking_effort_setter: Callable[[str], None] = None,
                 thinking_options: dict = None,
                 thinking_passback_getter: Callable[[], bool] = None,
                 thinking_passback_setter: Callable[[bool], None] = None,
                 model_getter: Callable[[], str] = None,
                 model_setter: Callable[[str], None] = None,
                 model_options: list = None,
                 summarize_func: Callable[[List[Dict[str, Any]], Callable[[], bool]], str] = None,
                 summary_anim_start: Callable[[], None] = None,
                 summary_anim_stop: Callable[[], None] = None,
                 cancel_check: Callable[[], bool] = None,
                 name_func: Callable[[List[Dict[str, Any]]], str] = None,
                 goal_tool_setter: Callable[[bool], None] = None,
                 plugin_setter: Callable[[str, bool], bool] = None,
                 goal_max_rounds: int = 0,
                 project_skill_roots=None,

                 context_reset_func: Callable[[], None] = None,
                 logger=None):
        self.narnat_dir = narnat_dir
        self._messages = messages
        # 同一个会话的「读指纹 → 写盘 → 更新指纹」必须串行。
        # 谁在并发：轮末自动保存（agent.py 的 on_auto_save，跑在 narnat-agent
        # 线程）与用户 /save（RPC 主线程）打在同一个 RootSession 上，两者读到
        # 同一个旧 _sig；先写的那次换掉文件后，后写的就把「自己人刚写的」
        # 判成「另一个实例改的」→ 另存 X (2) + 弹假警报（实测确定性 3/3）。
        # 锁按会话名分片（不能用全局锁，否则保存 A 会阻塞保存 B），
        # 并且**必须把读 _sig 一起圈进来**，只锁写没用。
        self._persist_locks: Dict[str, Any] = {}
        self._persist_locks_guard = threading.Lock()
        # 日志器：只为「载入会话时清掉了 N 条孤儿工具结果」这类**删了用户数据**
        # 的动作留痕。没有它时 replace_messages 里那行 warning 永远打不出来
        # （线上用户报「历史里少了几条工具结果」将无从追查）。
        self._logger = logger
        self._config_dir = config_dir
        self._context_reset = context_reset_func
        # 后追加的会话复位回调（见 add_context_reset）。单独存一个列表而不是拼
        # 成一个闭包：构造时注入那个必须原样保留在首位，追加方无权替换它。
        self._context_resets = []
        self._get_thinking_effort = thinking_effort_getter
        self._set_thinking_effort = thinking_effort_setter
        self._thinking_options = thinking_options or dict(DEFAULT_THINKING_OPTIONS)
        self._get_thinking_passback = thinking_passback_getter or (lambda: DEFAULT_THINKING_PASSBACK)
        self._set_thinking_passback = thinking_passback_setter
        self._get_model = model_getter
        self._set_model = model_setter
        self._model_options = model_options or []
        self.summarize_func = summarize_func
        self.summary_anim_start = summary_anim_start
        self.summary_anim_stop = summary_anim_stop
        self.cancel_check = cancel_check or (lambda: False)
        self.name_func = name_func
        # 手动压缩（/compact）：由 Assembly 注入 CompressionCoordinator.compress_manual
        self.compact_func: Optional[Callable[[], Tuple[str, str]]] = None
        self._set_goal_tool = goal_tool_setter
        # 插件工具开关（/plugin）：由 Assembly 注入 llm.set_plugin_enabled（注册表 + LLM工具表同步）
        self._set_plugin_enabled = plugin_setter
        # 项目技能根目录: None=自动发现（扫描所有名为 skills 的目录）；空元组=关闭；非空=显式
        self._project_skill_roots = project_skill_roots

        self._auto_save_done: bool = False
        self._pending_auto_save_name: Optional[str] = None
        # 最近一次「离开游离态」（新建对话 / /cd 切走）时兜底存下的会话名
        # （None=没存），供界面回执告诉用户内容落到哪个会话
        self._last_auto_saved: Optional[str] = None
        # carry_over 已把这一段落盘 → 后台自动保存线程写盘前据此跳过，
        # 避免同一个对话出现两份记录。存的是「已落盘那份消息的指纹」而非布尔：
        # 布尔会跨段泄漏（新一段内容不同却被跳过，永不落盘）。
        self._carried_over_fingerprint: Optional[str] = None
        # 指纹对应的会话名（carry_over 存成了哪个名字）。指纹命中时后台线程
        # 靠它把状态切到这份会话——只跳过写盘会把用户留在游离态。
        self._carried_over_name: Optional[str] = None
        # 最近一次成功落盘的那份内容指纹。退出兜底（exit_flush）据此判断
        # 「这一段已经在磁盘上了」——不判就会在正常退出时多存出一个会话文件。
        self._last_saved_fingerprint: Optional[str] = None
        # 最近一次 _persist 的失败原因（""=上次写盘成功）。退出提示要说出
        # 「为什么没存下」，而 auto_save() 只回 None，原因只能从这里带走。
        self._last_save_error: str = ""
        self.pending_deletes: Set[Tuple[str, Optional[str]]] = set()
        self._state: SessionState = NoSession(self)
        # 目标模式状态：_goal_enabled=开关；_goal_max_rounds=临时轮数覆盖（0=用配置默认值）；
        # _goal_default_rounds=配置默认轮数上限（供 /goal 查询显示具体值）
        self._goal_enabled: bool = False
        self._goal_max_rounds: int = 0
        self._goal_default_rounds: int = goal_max_rounds

    def get_messages(self) -> List[Dict[str, Any]]:
        """返回 messages 的浅拷贝列表（供状态类读取/保存使用）"""
        return self._messages.view().to_list()

    def persist_lock(self, key: str):
        """取某个会话的写盘锁（按名分片；同一把锁跨线程共享）。

        只包住 `_persist` 的「读 _sig → save_session → 写回 _sig」这一段，
        **绝不能**在锁内调 LLM / 压缩 / 命名：那会把 /save 卡在长耗时操作上，
        而锁的目的只是防止两次覆盖写互相把对方误判成外部改动。
        """
        with self._persist_locks_guard:
            lock = self._persist_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._persist_locks[key] = lock
            return lock

    def get_full_messages(self) -> List[Dict[str, Any]]:
        """返回完整历史（归档 + 当前）的浅拷贝列表，供展示/落盘使用。

        与 get_messages() 的区别：压缩把当前上下文换成「摘要 + 保留尾部」后，
        被替换掉的原文只在归档里——展示/落盘必须走这里，否则用户翻历史看不
        到压缩前的原文。给模型的上下文仍走 get_message_list().view()。
        """
        return self._messages.full_history()

    def get_message_list(self):
        """返回 MessageList 引用（供 Agent 直接调用 LLM 等场景）"""
        return self._messages

    def add_context_reset(self, func: Callable[[], None]) -> None:
        """追加一个「会话上下文已更换」回调，排在构造时注入的那个之后。

        为什么只能在 replace_messages 这一条链上追加：新建/切换//cd//explore/
        /done/删除当前会话/headless 任务复位全在这里汇合，去 rpc/server.py 的
        各个 _m_session_* 里分别补一遍只会「补一处漏一处」。

        为什么必须追加而不能替换 _context_reset：那里挂着压缩协调器的占比与
        告警复位，被顶掉后新加载的会话会被上一段的残留占比直接判成该压缩。
        """
        self._context_resets.append(func)

    def replace_messages(self, new_msgs: List[Dict[str, Any]]):
        # 切换/载入会话：归档属于上一段会话，必须先清空，否则完整历史会把两个
        # 会话的内容拼在一起。
        self._messages.clear_archive()
        # 落盘文件是全量历史（提示词 + 归档 + 当前），载入时拆回两半：
        # 摘要边界之前的历史进归档（展示/落盘保留，不发给模型），这样重进压缩过的
        # 会话不会把上下文从摘要态膨胀回全量、也不会让摘要夹在历史中游被再次摘要。
        # 首条系统提示词由拆分解法保证存在且只有一条（缺了就补 config 的）。
        current, archived = split_loaded_history(new_msgs, self._messages.system_prompt)
        self._messages.replace_all(current)
        # 载入即修结构：会话文件可能是**被劈开**的状态（工具还在跑时切了会话，
        # assistant 声明与 tool 结果分别落在两个文件里 —— 实测 b.json 有 4 条
        # 结果、b/b_g.json 有那条声明）。这种状态一旦发出去必然 400
        # （`tool_result` 找不到对应 `tool_use`），而用户此时还什么都没做。
        # 在这里修比等下一次发消息再修好：用户切回来就能正常继续，不会先撞一个
        # 「内容审核拦截」的假警报。
        dropped = self._messages.drop_orphan_tool_results()
        if dropped:
            # SessionManager 没有 _logger（日志在 AutoSaveManager 那侧），
            # 用 getattr 兜住：这里绝不能因为「打日志」把切换会话搞崩。
            _lg = getattr(self, "_logger", None) or getattr(self, "logger", None)
            if _lg is not None:
                _lg.warning(
                    "core.session_callbacks",
                    f"载入会话时清理了 {len(dropped)} 条无对应声明的工具结果"
                    f"（声明与结果被劈到了不同会话文件）")
        if archived:
            self._messages.archive(archived)
        # 恢复历史会话时占比从 0 开始（历史 token 数未持久化），第一轮回复结束后恢复真实值
        # 会话切换：上一会话的占比/告警标记必须复位，否则新加载会话被残留
        # 占比误触发压缩、首次告警不再提示（coordinator 经 assembly 注入复位回调）
        if self._context_reset is not None:
            self._context_reset()
        for reset in self._context_resets:
            reset()

    def switch_state(self, new_state: SessionState):
        self._state = new_state

    def create_root_state(self, name: str) -> RootSession:
        return RootSession(self, name)

    def create_child_state(self, name: str, parent: str) -> ChildSession:
        return ChildSession(self, name, parent)

    def load_child_with_boundary(self, name: str, parent: str) -> Tuple[List[Dict[str, Any]], str]:
        new_msgs, err = load_session(self.narnat_dir, name, parent=parent)
        return new_msgs, err


    def apply_delete_marks(self, tree: List[Dict[str, Any]]):
        for root in tree:
            if (root["name"], None) in self.pending_deletes:
                root["_delete_marked"] = True
            for child in root.get("children", []):
                if (child["name"], root["name"]) in self.pending_deletes:
                    child["_delete_marked"] = True

    def cleanup_deletes(self):
        children = [(n, p) for n, p in self.pending_deletes if p is not None]
        roots = [(n, p) for n, p in self.pending_deletes if p is None]
        for name, parent in children:
            delete_session(self.narnat_dir, name, parent=parent)
        for name, parent in roots:
            delete_session(self.narnat_dir, name, parent=parent)
        self.pending_deletes.clear()

    def resolve_session_name(self, name: str) -> Tuple[Optional[str], Optional[str], str]:
        """解析会话名。返回 (target_name, target_parent, error_msg)"""
        tree = list_sessions_tree(self.narnat_dir)
        # 1) 完整名匹配：根会话名（可能本身含 "/"）或 "父/子" 完整路径
        for root in tree:
            if root["name"] == name:
                return root["name"], None, ""
            for child in root.get("children", []):
                if f"{root['name']}/{child['name']}" == name:
                    return child["name"], root["name"], ""
        # 2) 含 "/" 的名字按 parent/child 拆分（"父/子" 快捷写法）
        if "/" in name:
            parent, child = name.split("/", 1)
            return child, parent, ""
        # 3) 裸名：唯一子会话匹配
        matches = []
        for root in tree:
            for child in root.get("children", []):
                if child["name"] == name:
                    matches.append((child["name"], root["name"]))
        if len(matches) == 1:
            return matches[0][0], matches[0][1], ""
        if len(matches) > 1:
            paths = "\n".join(
                f"      {c}/{p}" if c else f"      {p}" for p, c in matches
            )
            return None, None, f"'{name}' 有多个，请用完整路径指定：\n{paths}"
        return None, None, f"会话不存在: {name}"

    @property
    def state(self) -> SessionState:
        return self._state

    # ── 代理方法，供外部直接调用 ──

    def on_save(self, name: str) -> str:
        return self._state.save(name)

    def on_show(self, args: str = "") -> str:
        return self._state.show(args)

    def on_enter(self, name: str) -> str:
        # 「上一段兜底保存名」只对本条命令有效：逐次复位，免得上一次的旧名字
        # 被当成这次的结果回给用户（NoSession.enter 会重新填）。
        self._last_auto_saved = None
        return self._state.enter(name)

    def on_delete(self, name: str) -> str:
        return self._state.delete(name)

    def on_explore(self, name: str) -> str:
        return self._state.explore(name)

    def on_done(self) -> str:
        return self._state.done()

    def on_compact(self) -> Tuple[str, str]:
        """手动压缩上下文（/compact 命令）——转发给注入的 compact_func。

        成功后：先按压缩后的新列表修正会话基准（子会话的增量合并下标），
        再落盘——命令不经过轮末自动保存，不落盘的话压缩结果会在退出时丢失。

        返回 (status, text)：status="ok" 成功 / "empty" 无历史可压缩 / "error" 失败。
        """
        if self.compact_func is None:
            return "error", "压缩不可用"
        status, text = self.compact_func()
        if status == "ok":
            self._state.reset_after_compact()
            self._state.auto_save()
        return status, text

    def on_exit(self) -> str:
        msg, new_state = self._state.exit()
        if new_state is not None:
            self._state = new_state
        return msg

    def on_auto_save(self):
        """落盘当前内容，返回实际写入的会话名（没存或写盘失败则 None）。

        返回值是「是否真的落盘成功」的唯一判据：退出提示必须据此打印，
        不能再拿 state.session_name() 当成功依据——写盘失败时它照样有名字，
        于是出现「会话已自动保存: X」而磁盘上什么都没有（P1）。
        """
        return self._state.auto_save()

    def exit_flush(self) -> Optional[str]:
        """退出前的同步落盘（不依赖 LLM 命名、毫秒级纯本地写）。

        必须在 os._exit 之前由调用方（rpc/server._finish、agent 退出分支）
        在当前线程同步调用：轮末自动保存是后台线程里的网络请求，退出时等不到
        它，原来那一段就整段随进程消失（P0-3，退出码还是 0）。两种状态都覆盖：
        游离态用快速截断名另存成一条会话，已在会话/分支里则写回自己那个文件。
        返回实际写入的会话名（没写则 None）。
        """
        try:
            return self._state.exit_flush()
        except Exception as e:
            _report_exit_save_error(f"{type(e).__name__}: {e}")
            return None

    def on_new(self) -> str:
        """新建对话：回到游离态并清空上下文。

        与 /exit 的区别：/exit 只切状态、消息还留在内存里（再聊就接上了上一段）。
        这里明确把消息重置为只剩 system prompt，用户才能真的「不要这个对话」。

        当前内容先落盘再清空——已聊内容不丢，之后仍可从会话列表进入。
        已在会话/分支里时走 auto_save（写回原文件）；游离态时走
        NoSession.carry_over()——否则「未保存的对话点新建」就是静默丢数据，
        磁盘上连痕迹都没有。
        存下的名字记在 _last_auto_saved 里，好让界面告诉用户存到哪了。
        """
        self._last_auto_saved = None
        # 清上一段可能遗留的「已由 carry_over 存下」指纹（连同它的会话名）
        self._carried_over_fingerprint = None
        self._carried_over_name = None
        try:
            saved = self._state.auto_save()
            if saved is None and isinstance(self._state, NoSession):
                saved = self._state.carry_over()   # 存下时会记下那份消息的指纹
            self._last_auto_saved = saved
        except Exception:
            # 落盘失败不该挡住「新建」这个动作本身
            pass
        # 复位自动保存标志：它表示「本段对话已由 AutoSaveManager 处理过」，
        # 新的一段开始后必须清零。不清会怎样：第二次及以后「新建对话」时
        # try_save 与 carry_over 都因这个标志跳过，那一段内容静默丢失
        # （实测：聊一句→新建，内容既不落盘也不提示）。
        self._auto_save_done = False
        self._pending_auto_save_name = None
        self._state = NoSession(self)
        msgs = self.get_messages()
        system = msgs[0] if msgs and msgs[0].get("role") == "system" else None
        # replace_messages 会顺带复位上下文占比（与会话切换同语义）
        self.replace_messages([system] if system else [])
        return ""

    def on_skill(self, name: str) -> str:
        content, err, path = load_skill(self.narnat_dir, name,
                                        project_roots=self._project_skill_roots)

        if err:
            return err
        if path:
            # 带上技能目录：正文里的相对路径以此为基准（技能可能装在任意层级）
            content = (f"本技能目录: {os.path.dirname(path)}\n"
                       "技能正文里提到的相对路径，请按上面的目录解析；引用的文件按需读取。\n\n"
                       f"{content}")
        self._messages.append_system(content)
        return ""

    def on_thinking(self, effort: str) -> str:
        options = self._thinking_options
        if not effort:
            current = (self._get_thinking_effort or (lambda: "high"))()
            current_label = options.get(current, current)
            return f"当前思考强度: {current_label}"
        effort_lower = effort.strip().lower()
        if effort_lower not in options:
            available = " / ".join(options.keys())
            return f"无效值: {effort_lower}（可用: {available}）"
        if self._set_thinking_effort:
            self._set_thinking_effort(effort_lower)

        def _mutate(data):
            data.setdefault("智能体", {}).setdefault("思考", {})["强度"] = effort_lower

        err = _persist_config(self._config_dir, _mutate)
        text = f"思考强度已切换为: {options[effort_lower]}"
        return f"{text}（{err}，仅本次会话有效）" if err else text

    def on_list_thinking_options(self) -> list:
        return list(self._thinking_options.keys())

    def on_thinkback(self, action: str) -> str:
        """思考回传开关：/thinkback 查状态，/thinkback on|off 切换并持久化。

        只控制「是否把思考内容随请求回传给模型」，与「是否记录思考」无关：
        无论开关状态，模型思考内容都会写入会话记录（供界面查看/回放）。
        关闭（默认）：思考不回传（省上下文）；repair 合成的安全空块仍始终
        回传（400兜底，零上下文占用）。
        """
        current = self._get_thinking_passback()
        act = action.strip().lower()
        if not act:
            state = "开" if current else "关"
            return f"思考回传: {state}（/thinkback on|off 切换；关闭时思考仍记录到会话，只是不发给模型）"
        if act in ("on", "off"):
            target = act == "on"
            if self._set_thinking_passback:
                self._set_thinking_passback(target)

            def _mutate(data):
                data.setdefault("智能体", {}).setdefault("思考", {})["回传"] = target

            err = _persist_config(self._config_dir, _mutate)
            suffix = f"（{err}，仅本次会话有效）" if err else ""
            if current == target:
                return f"思考回传已是{'开启' if target else '关闭'}状态{suffix}"
            if target:
                return f"思考回传已开启：思考内容将随请求发给模型{suffix}"
            return f"思考回传已关闭：思考内容仍会记录到会话，只是不再发给模型{suffix}"
        return f"无效值: {act}（可用: on / off）"

    def on_mode(self, name: str) -> str:
        options = list(self._model_options or [])
        if not name:
            current = (self._get_model or (lambda: ""))()
            return f"当前模型: {current}"
        target = name.strip()
        matched = None
        for opt in options:
            if opt.lower() == target.lower():
                matched = opt
                break
        if matched is None:
            available = " / ".join(options)
            return f"无效值: {target}（可用: {available}）"
        if self._set_model:
            self._set_model(matched)

        def _mutate(data):
            data.setdefault("智能体", {}).setdefault("模型", {})["当前"] = matched

        err = _persist_config(self._config_dir, _mutate)
        if err:
            # 写回失败时不得显示"设置成功"误导：本次会话已切换，但配置未持久化
            return f"已切换为 {matched}，但{err}，仅本次会话有效"
        return f"设置成功：{matched}"

    def on_list_model_names(self) -> list:
        return list(self._model_options or [])

    def on_plugin(self, action: str) -> Tuple[str, str]:
        """插件工具开关：/plugin 查看状态，/plugin <名称> <on|off> 切换并持久化。

        关闭 = 该工具定义下一轮请求起不再发给 LLM（省token）；不做执行层拦截，
        已建立的 SSH/串口/MCP 连接保持存活。
        返回 (status, text)：status ∈ {"info","ok","hint","error"}，命令层据此着色。
        """
        from ..tools import registry as _registry
        if not action:
            states = _registry.get_plugin_states()
            on_count = sum(1 for enabled in states.values() if enabled)
            lines = [f"  插件开关: {on_count}/{len(states)} 开启  用法: /plugin <名称> <on|off>"]
            for name, enabled in states.items():
                lines.append(f"    {name.ljust(9)} {'on ' if enabled else 'off'}  "
                             f"{_PLUGIN_TOOL_LABELS.get(name, '')}")
            return "info", "\n".join(lines)
        parts = action.split()
        name = _registry.resolve_plugin_name(parts[0])
        if name is None:
            available = " / ".join(_registry.PLUGIN_TOOL_NAMES)
            return "error", f"  无效插件名: {parts[0]}（可用: {available}）"
        value = " ".join(parts[1:]).strip().lower()
        if not value:
            return "error", f"  参数不完整（用法: /plugin {name} on|off）"
        if value not in ("on", "off"):
            return "error", f"  无效值: {value}（可用: on / off，例如 /plugin {name} off）"
        target = value == "on"
        current = _registry.get_plugin_states()[name]
        if self._set_plugin_enabled is None or not self._set_plugin_enabled(name, target):
            return "error", "  插件开关不可用"
        err = self._persist_plugin_states()
        suffix = f"（{err}，仅本次会话有效）" if err else ""
        if current == target:
            return "hint", f"  {name} 已是{'开启' if target else '关闭'}状态{suffix}"
        if target:
            return "ok", f"  {name} 已开启  (下轮请求起恢复向 AI 提供该工具){suffix}"
        return "ok", f"  {name} 已关闭  (下轮请求起不再向 AI 提供该工具){suffix}"

    def _persist_plugin_states(self) -> str:
        """把全部插件开关状态原子写回 narnat.json；返回 ""=成功，非空=错误信息"""
        if not self._config_dir:
            return ""
        from ..tools import registry as _registry

        def _mutate(data):
            data.setdefault("工具", {})["插件"] = {
                name: "on" if enabled else "off"
                for name, enabled in _registry.get_plugin_states().items()
            }

        return _persist_config(self._config_dir, _mutate)

    def on_list_plugin_names(self) -> list:
        from ..tools import registry as _registry
        return list(_registry.PLUGIN_TOOL_NAMES)

    def on_list_names(self) -> list:
        tree = list_sessions_tree(self.narnat_dir)
        names = []
        for root in tree:
            names.append(root["name"])
            for child in root.get("children", []):
                names.append(f"{root['name']}/{child['name']}")
        return names

    def on_list_names_tree(self) -> list:
        return self.on_list_names()

    def on_list_rm_names(self) -> list:
        """返回当前状态下可删除的会话名列表（供 /rm tab 补全使用）"""
        if isinstance(self._state, NoSession):
            all_names = self.on_list_names()
            result = []
            for name in all_names:
                if "/" in name:
                    parent, child = name.split("/", 1)
                    if (child, parent) in self.pending_deletes:
                        continue
                else:
                    if (name, None) in self.pending_deletes:
                        continue
                result.append(name)
            return result
        if isinstance(self._state, RootSession):
            tree = list_sessions_tree(self.narnat_dir)
            current_name = self._state.session_name()
            for root in tree:
                if root["name"] == current_name:
                    return [f"{root['name']}/{child['name']}"
                            for child in root.get("children", [])
                            if (child["name"], current_name) not in self.pending_deletes]
        return []

    def on_list_skill_tree(self) -> list:
        """技能树（供 /skill 按层级 Tab 补全）。"""
        return list_skill_tree(self.narnat_dir,
                               project_roots=self._project_skill_roots)


    def is_child_session(self) -> bool:
        return self._state.is_child()

    def has_active_session(self) -> bool:
        return self._state.session_name() is not None

    def should_exit_agent(self) -> bool:
        return isinstance(self._state, NoSession)

    def available_commands(self) -> Dict[str, str]:
        return self._state.available_commands()
