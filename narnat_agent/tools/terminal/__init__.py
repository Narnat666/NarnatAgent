"""Terminal 工具 —— 多设备 SSH 调度 + 单通道命令执行（v2）

数据通道: 每命令一条独立 SSH 通道（exec_command），完成判定 = SSH 协议 exit_status。
无 PTY 常驻 shell、无哨兵注入、无提示符检测。

结构:
- __init__.py  调度面: DEFINITION + 5 action 路由 + dev 注册表（本文件）
- session.py   数据通道: SSHSession（每命令独立通道执行器）
- remote.py    SFTP: Read/Write/Edit 的远程文件操作 + transfer 内部
"""

import os
import re
import socket
import threading
from typing import Optional

import paramiko

from ..exec_signal import error_line
from ..tool_context import AWAIT_CONFIRM
from .session import SSHSession

__all__ = ["execute", "DEFINITION", "get_session", "SSHSession", "kill_active_exec",
           "cleanup", "TerminalRuntime", "resolve_dev_display"]


class TerminalRuntime:
    """Terminal 工具全部模块级状态与参数。

    - sessions/active_exec_session: 跨调用共享的可变状态
    - max_sessions: assembly 启动时按配置写入（set_max_sessions）
    """
    # dev编号正则: dev0=本机(当前设备), devN(N>=1)=第N台被控设备(终端N-1)
    RE_DEV = re.compile(r"^dev(\d+)$", re.IGNORECASE)

    # 删除命令正则
    # 边界后跟空白或/：覆盖无空格变体（rd/s、del/f、rmdir/q）及erase/format；
    # \b边界防止误伤 delphi、3rd、formatting 等普通词
    RE_DELETE = re.compile(
        r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
        r"|\bRemove-Item\b",
        re.IGNORECASE,
    )

    # 匹配 git 命令的简单正则（出现 git 即命中）
    RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

    # 纯 cd 命令（无 &&/||/管道/重定向/换行/分号）：单通道执行下 cd 不影响后续命令，
    # 结果后附提示引导。空白用 [ \t] 而非 \s：\s 允许换行会让 `cd\npwd` 被误判为纯 cd
    RE_CD_ONLY = re.compile(r"^(?:cd|chdir)(?:[ \t]+/d)?(?:[ \t]+[^&|<>;\n\r]*)?$", re.IGNORECASE)

    max_sessions = 5  # 默认5个并发SSH会话，assembly 启动时按配置覆盖

    # session_id(0-4) → SSHSession
    sessions: dict = {}
    sessions_lock = threading.Lock()

    # 当前正在执行命令的SSH会话（agent层ESC打断后据此关闭飞行中通道）
    active_exec_session = None
    active_exec_lock = threading.Lock()

    @classmethod
    def set_max_sessions(cls, n: int) -> None:
        """设置最大SSH会话数（由 assembly 初始化时从配置读取），限制1-10"""
        cls.max_sessions = max(1, min(n, 10))


DEFINITION = {
    "type": "function",
    "function": {
        "name": "Terminal",
        "description": (
            "多设备SSH：远程执行命令、设备间传输文件。"
            "连接后保持（复用SSH连接），每条命令独立执行完毕返回退出码与输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "exec", "status", "close", "transfer"],
                    "description": (
                        "操作类型（默认exec）。"
                        "connect 建立SSH会话（成功后返回该设备的dev编号）；"
                        "exec 在指定设备执行命令；"
                        "status 查看所有设备状态；"
                        "close 关闭指定设备会话（省略host时关闭全部会话）；"
                        "transfer 在任意设备间传输文件（本机↔被控设备、被控设备↔被控设备）"
                    ),
                },
                "host": {
                    "type": "string",
                    "description": (
                        "设备引用，统一用dev编号：dev0=本机（本机执行命令请用Shell工具），"
                        "dev1..devn=已connect的被控设备。"
                        "connect时填被控设备IP/域名（连接成功后返回其dev编号）；"
                        "exec/close 填dev编号：exec 省略时自动选择唯一会话（多个会话时需指定），"
                        "close 省略时关闭全部会话"
                    ),
                },
                "username": {"type": "string", "description": "SSH用户名（connect时使用）"},
                "port": {"type": "integer", "description": "SSH端口（默认22）"},
                "password": {
                    "type": "string",
                    "description": (
                        "认证凭据（connect时使用）：登录密码或私钥路径（如~/.ssh/id_rsa），"
                        "不填则自动尝试默认密钥"
                    ),
                },
                "command": {"type": "string", "description": "执行的命令（action=exec时使用）"},
                "timeout": {
                    "type": "integer",
                    "description": (
                        "命令超时秒数（正整数）。exec默认120，超时后终止命令通道并明确报错；"
                        "connect默认15，连不上时快速报错"
                    ),
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "最大输出字符数（默认8000，超出截断并提示）",
                },
                "source_host": {
                    "type": "string",
                    "description": (
                        "传输源设备：dev0=本机（默认，可省略），"
                        "dev1..devn=被控设备（action=transfer时使用）"
                    ),
                },
                "source_path": {
                    "type": "string",
                    "description": "源文件在源设备上的绝对路径（action=transfer时使用）",
                },
                "target_host": {
                    "type": "string",
                    "description": (
                        "传输目标设备：dev0=本机（默认，可省略），"
                        "dev1..devn=被控设备（action=transfer时使用）"
                    ),
                },
                "target_path": {
                    "type": "string",
                    "description": "目标文件在目标设备上的绝对路径（action=transfer时使用）",
                },
            },
            "required": [],
        },
    },
}


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

def execute(
    action: str = "exec",
    host: str = "",
    username: str = "",
    port: int = 22,
    key_path: str = "",
    password: str = "",
    sudo_password: str = "",
    command: str = "",
    cwd: str = "",
    input: str = "",
    timeout: Optional[int] = None,
    session_id: int = -1,
    max_output_chars: int = 8000,
    source_host: str = "",
    source_path: str = "",
    target_host: str = "",
    target_path: str = "",
    _tool_context=None,
) -> str:
    """Terminal工具：多设备SSH + 文件传输（参数说明见 DEFINITION）。

    session_id: 内部参数，AI无需使用（设备引用统一用host=devN）
    sudo_password/input/cwd: 兼容占位（保留签名，不再使用；cwd 请改用 'cd X && 命令'）
    """
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    try:
        port = int(port) if port is not None else 22
        timeout = int(timeout) if timeout is not None else None
        session_id = int(session_id) if session_id is not None else -1
        max_output_chars = int(max_output_chars) if max_output_chars is not None else 8000
    except (TypeError, ValueError):
        return error_line("port/timeout/session_id/max_output_chars需为整数")

    if action == "connect":
        # connect默认超时15秒（exec默认120秒）：连错IP/黑洞IP时快速失败
        connect_timeout = 15 if timeout is None else timeout
        if _tool_context and _tool_context.max_timeout_seconds > 0:
            connect_timeout = min(connect_timeout, _tool_context.max_timeout_seconds)
        return _connect(host, username, port, key_path, password, session_id, connect_timeout)
    elif action == "exec":
        exec_timeout = 120 if timeout is None else timeout
        if _tool_context and _tool_context.max_timeout_seconds > 0:
            exec_timeout = min(exec_timeout, _tool_context.max_timeout_seconds)
        return _exec(session_id, host, command, exec_timeout, max_output_chars, _tool_context)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(session_id, host)
    elif action == "transfer":
        return _transfer(source_host, source_path, target_host, target_path, _tool_context)
    else:
        return error_line(f"未知action '{action}'，可选: connect/exec/status/close/transfer")


# ═══════════════════════════════════════════════════════════════
# dev 编号解析与管理
# ═══════════════════════════════════════════════════════════════

def _dev_label(sid: int) -> str:
    """终端session_id → dev编号标签: 终端0=dev1, 终端1=dev2 ..."""
    return f"dev{sid + 1}(终端{sid})"


def _list_devices_locked() -> str:
    """当前已连接设备的dev清单（调用者需持有TerminalRuntime.sessions_lock）"""
    devs = [f"dev{sid + 1}({s.username}@{s.host})"
            for sid, s in sorted(TerminalRuntime.sessions.items()) if s is not None]
    return "、".join(devs) if devs else "(无)"


def _list_devices() -> str:
    """当前已连接设备的dev清单（用于报错提示）"""
    with TerminalRuntime.sessions_lock:
        return _list_devices_locked()


def _dev_hint_locked() -> str:
    """设备标识错误时的统一指导（调用者需持有TerminalRuntime.sessions_lock）"""
    base = "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备；本机执行命令请用Shell工具)"
    devs = _list_devices_locked()
    if devs == "(无)":
        return f"{base}。当前无已连接设备，请先connect"
    return f"{base}。当前已连接: {devs}"


def _normalize_device(host: str) -> str:
    """设备标识归一化: 空/dev0 → 空字符串(本机)；devN(N>=1) → 原样保留；其余原样交由校验报错"""
    if not host:
        return ""
    h = host.strip()
    m = TerminalRuntime.RE_DEV.match(h)
    return "" if m and int(m.group(1)) == 0 else h


def _normalize_device_for_tools(device: str) -> Optional[str]:
    """文件工具(Read/Edit/Write)的设备标识归一化: 合法返回规范值(本机为""), 非法返回None"""
    if not device:
        return ""
    h = device.strip()
    m = TerminalRuntime.RE_DEV.match(h)
    if not m:
        return None
    return "" if int(m.group(1)) == 0 else h


def _file_tool_device_hint() -> str:
    """文件工具(Read/Edit/Write)设备标识错误时的统一指导"""
    base = "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)"
    devs = _list_devices()
    if devs == "(无)":
        return f"{base}。当前无已连接设备，请先Terminal connect"
    return f"{base}。当前已连接: {devs}"


def _device_error(host: str) -> Optional[str]:
    """校验设备标识是否合法devN（dev0~devN），非法返回错误信息，合法返回None"""
    if not host:
        return None
    if TerminalRuntime.RE_DEV.match(host.strip()):
        return None
    with TerminalRuntime.sessions_lock:
        return error_line(f"{_dev_hint_locked()}")


def _local_host() -> str:
    """本机显示名：优先取主机名，失败回退localhost"""
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


def resolve_dev_display(dev: str) -> str:
    """把设备引用翻译成UI显示名（供tool_dispatcher终端摘要使用）：
    - 空/dev0/本机 → 本机host（主机名）
    - devN已连接 → "host"（IP）
    - devN未连接 → 原样devN
    - 其他 → 原样
    """
    if not dev:
        return _local_host()
    h = dev.strip()
    m = TerminalRuntime.RE_DEV.match(h)
    if not m:
        return h
    d = int(m.group(1))
    if d == 0:
        return _local_host()
    sid = d - 1
    with TerminalRuntime.sessions_lock:
        session = TerminalRuntime.sessions.get(sid)
        if session is not None and session.alive:
            return session.host
    return h


def _allocate_session_id_locked() -> int:
    """分配一个空闲的session_id，返回-1表示已满（调用者需持有sessions_lock）。

    死会话（连接已断开）自动回收槽位：否则连接意外断开后槽位被死会话占用，
    AI重连时报"已达最大会话数"却无法释放。
    预留占位（值 None 且键存在，见 _connect）视为已占用：跳过它，否则并发
    connect 会拿到同一槽位，后完成的会话覆盖前一个（连接泄漏）。
    """
    for i in range(TerminalRuntime.max_sessions):
        if i not in TerminalRuntime.sessions:
            return i
        session = TerminalRuntime.sessions[i]
        if session is None:
            continue
        if not session.alive:
            try:
                session.close()
            except Exception:
                pass
            del TerminalRuntime.sessions[i]
            return i
    return -1


def _resolve_session_id(session_id: int, host: str = "") -> tuple:
    """解析session_id，返回 (session_id, session) 或抛出ValueError

    逻辑:
    1. session_id >= 0: 直接查找
    2. session_id == -1 且指定host: 按host模糊匹配（devN优先，其次IP/用户名@IP）
    3. session_id == -1 且无host: 只有一个会话时自动选择
    """
    with TerminalRuntime.sessions_lock:
        if session_id >= 0:
            if session_id not in TerminalRuntime.sessions:
                raise ValueError(
                    f"{_dev_label(session_id)}未连接，请先connect。当前已连接: {_list_devices_locked()}")
            return session_id, TerminalRuntime.sessions[session_id]

        if host:
            h = host.strip()
            m = TerminalRuntime.RE_DEV.match(h)
            if m:
                d = int(m.group(1))
                if d == 0:
                    raise ValueError(
                        "dev0是本机，无SSH会话。本机执行命令请用 Shell 工具"
                        "（Terminal 的 exec 用于SSH远程设备，dev0仅用于transfer）")
                sid = d - 1
                if sid >= TerminalRuntime.max_sessions or sid not in TerminalRuntime.sessions:
                    raise ValueError(f"dev{d}未连接，请先connect。当前已连接: {_list_devices_locked()}")
                return sid, TerminalRuntime.sessions[sid]

            # 宽松匹配: 允许直接用IP或用户名@IP引用已连接设备，避免AI回查dev编号
            matched = [sid for sid, s in sorted(TerminalRuntime.sessions.items())
                       if s is not None and (s.host == h or f"{s.username}@{s.host}" == h)]
            if len(matched) == 1:
                return matched[0], TerminalRuntime.sessions[matched[0]]
            if len(matched) > 1:
                detail = "、".join(f"dev{s + 1}({TerminalRuntime.sessions[s].username}"
                                   f"@{TerminalRuntime.sessions[s].host})" for s in matched)
                raise ValueError(f"多个会话匹配 '{h}'，请用dev编号区分: {detail}")
            raise ValueError(_dev_hint_locked())

        if len(TerminalRuntime.sessions) == 1:
            sid = list(TerminalRuntime.sessions.keys())[0]
            return sid, TerminalRuntime.sessions[sid]
        elif len(TerminalRuntime.sessions) == 0:
            raise ValueError("无活跃会话，请先connect")
        keys = [f"dev{s + 1}({TerminalRuntime.sessions[s].username}@{TerminalRuntime.sessions[s].host})"
                for s in sorted(TerminalRuntime.sessions.keys())]
        raise ValueError(f"有多个会话，请指定host=dev编号，当前已连接: {'、'.join(keys)}")


def get_session(session_id: int = -1, host: str = "") -> Optional["SSHSession"]:
    """获取指定SSH会话（供SFTP等内部使用）。断线时自动重连，失败返回None。"""
    try:
        _, session = _resolve_session_id(session_id, host)
    except ValueError:
        return None
    if not _try_reconnect(session):
        return None
    return session


def _try_reconnect(session) -> bool:
    """断线会话尝试重连（复用原凭据，保留dev编号）。失败返回False。"""
    if session is None:
        return False
    if session.alive:
        return True
    with TerminalRuntime.active_exec_lock:
        TerminalRuntime.active_exec_session = session
    try:
        session.reconnect()
        return True
    except Exception:
        return False
    finally:
        with TerminalRuntime.active_exec_lock:
            TerminalRuntime.active_exec_session = None


# ═══════════════════════════════════════════════════════════════
# ESC 打断 / 清理
# ═══════════════════════════════════════════════════════════════

def kill_active_exec():
    """ESC打断：关闭飞行中的命令通道，设中断标志让本地读取线程退出。

    幂等：无活跃执行时也不报错。
    """
    with TerminalRuntime.active_exec_lock:
        session = TerminalRuntime.active_exec_session
    if session is not None:
        try:
            session.interrupt()
        except Exception:
            pass

    # 其他会话若有飞行中命令，一并打断
    with TerminalRuntime.sessions_lock:
        others = [s for s in TerminalRuntime.sessions.values()
                  if s is not None and s is not session]
    for s in others:
        try:
            if s.busy:
                s.interrupt()
        except Exception:
            pass


def cleanup():
    """程序退出时清理所有会话。"""
    with TerminalRuntime.sessions_lock:
        for session in TerminalRuntime.sessions.values():
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        TerminalRuntime.sessions.clear()


# ═══════════════════════════════════════════════════════════════
# action 实现
# ═══════════════════════════════════════════════════════════════

def _looks_like_key_path(p: str) -> bool:
    """password 参数疑似私钥路径（含路径分隔符或以 ~ 开头）"""
    return "/" in p or "\\" in p or p.startswith("~")


def _auth_hint(password: str) -> str:
    """认证失败时按 password 形态给出排查提示（两种文案，其余留空）"""
    p = (password or "").strip()
    if not p:
        return "。未提供password，大多数设备需要密码认证，请在password参数填登录密码后重试"
    if _looks_like_key_path(p) and not os.path.isfile(os.path.expanduser(p)):
        return "。password疑似私钥路径但本地文件不存在，请确认路径或改填登录密码"
    return ""


def _fold_banner(text: str) -> str:
    """登录横幅折叠: >4 行时为首行 + 省略提示 + 末两行，避免 banner 刷屏"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) <= 4:
        return "\n".join(lines)
    return "\n".join([lines[0], f"...(已省略{len(lines) - 3}行登录横幅)",
                      lines[-2], lines[-1]])


def _release_slot(alloc_id: int) -> None:
    """释放 connect 失败后的预留占位（仅当占位仍为 None，不误删他人会话）"""
    with TerminalRuntime.sessions_lock:
        if (alloc_id in TerminalRuntime.sessions
                and TerminalRuntime.sessions[alloc_id] is None):
            del TerminalRuntime.sessions[alloc_id]


def _connect(host: str, username: str, port: int = 22,
             key_path: str = "", password: str = "",
             session_id: int = -1, timeout: int = 15) -> str:
    """建立SSH会话。timeout为连接超时（默认15秒），黑洞IP快速失败。"""
    if not host or not username:
        return error_line("connect需要提供host和username")
    if timeout <= 0:
        timeout = 15  # timeout<=0 兜底回默认值（保持旧契约）

    with TerminalRuntime.sessions_lock:
        if session_id >= 0:
            if session_id >= TerminalRuntime.max_sessions:
                return error_line(f"session_id范围0-{TerminalRuntime.max_sessions - 1}")
            if session_id in TerminalRuntime.sessions:
                existing = TerminalRuntime.sessions[session_id]
                if existing is None:
                    return error_line(f"{_dev_label(session_id)}正在连接中，请稍后重试")
                if existing.alive:
                    return f"[{_dev_label(session_id)}已连接: {existing.username}@{existing.host}]"
                # 死会话: 关闭回收后复用该槽位
                try:
                    existing.close()
                except Exception:
                    pass
                del TerminalRuntime.sessions[session_id]
            alloc_id = session_id
        else:
            alloc_id = _allocate_session_id_locked()
            if alloc_id < 0:
                return error_line(
                    f"已达最大会话数({TerminalRuntime.max_sessions})，当前已连接: "
                    f"{_list_devices_locked()}，请先close释放")

        # 锁内预留占位（防锁外构造期间另一个 connect 抢同一槽位）
        TerminalRuntime.sessions[alloc_id] = None

        # 相同设备重复连接提示（仅提示，不阻止）
        dup_refs, dup_devs = [], []
        for sid, s in sorted(TerminalRuntime.sessions.items()):
            if s is None or sid == alloc_id:
                continue
            if s.host == host and s.username == username:
                dup_refs.append(f"dev{sid + 1}")
                dup_devs.append(_dev_label(sid))

    # 锁外建连（可能阻塞数秒，不持锁）
    try:
        session = SSHSession(host=host, username=username, port=port,
                             key_path=key_path, password=password, timeout=timeout)
    except paramiko.AuthenticationException:
        _release_slot(alloc_id)
        return error_line(f"认证失败({username}@{host})，请检查password{_auth_hint(password)}")
    except (paramiko.SSHException, OSError) as e:
        _release_slot(alloc_id)
        return error_line(f"SSH连接失败({username}@{host}): {e}")
    except Exception as e:
        _release_slot(alloc_id)
        return error_line(f"连接失败({username}@{host}): {e}")

    with TerminalRuntime.sessions_lock:
        TerminalRuntime.sessions[alloc_id] = session

    parts = [f"[已连接 {_dev_label(alloc_id)}: {username}@{host}]"]
    if dup_devs:
        parts.append(f"[注意: 相同设备已有连接: {'、'.join(dup_devs)}。"
                     f"如非必要请勿重复连接，可直接用 host={'、'.join(dup_refs)} 执行命令]")
    banner = _fold_banner(session.banner)
    if banner:
        parts.append(banner)
    return "\n".join(parts)


def _check_exec_safety(command: str, session_id: int, host: str,
                       timeout: int, max_output_chars: int,
                       _tool_context) -> Optional[str]:
    """删除/git 命令安全确认。返回 None 表示放行，返回 str 表示被拦截的提示。"""
    tc = _tool_context
    if not (command and tc):
        return None

    need_confirm = False
    if not tc.rm_skip_confirm and TerminalRuntime.RE_DELETE.search(command):
        need_confirm = True
    elif not tc.git_skip_confirm and TerminalRuntime.RE_GIT.search(command):
        need_confirm = True
    if not need_confirm:
        return None

    # 终端被 prompt_toolkit 占用或本无交互终端：暂存命令由 agent 主循环
    # 在 # 提示符下等用户确认后重放；headless 下主循环读不到输入 → 取消执行
    # （fail-closed）
    if tc._delete_confirmed:
        tc._delete_confirmed = False
        return None

    tc.pending_delete.append(("Terminal", {
        "action": "exec",
        "session_id": session_id,
        "host": host,
        "command": command,
        "timeout": timeout,
        "max_output_chars": max_output_chars,
    }))
    return AWAIT_CONFIRM


def _exec(session_id: int, host: str, command: str,
          timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str:
    """在指定会话中执行命令（单通道）。"""
    if not command:
        return error_line("exec需要提供command")
    if timeout <= 0:
        return error_line("timeout需为正整数（秒）")

    blocked = _check_exec_safety(command, session_id, host, timeout,
                                 max_output_chars, _tool_context)
    if blocked is not None:
        return blocked

    # 纯 cd 形态：命令照常执行，仅在结果后附提示。
    # 不拦截：误判的代价（命令不执行）远大于收益，提示足以引导 AI 改用 cd X && 命令
    hint_cd_only = bool(TerminalRuntime.RE_CD_ONLY.match(command.strip()))

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return error_line(str(e))

    if session is None:
        return error_line(f"{_dev_label(sid)}正在连接中，请稍后重试")

    # 断线自愈：凭据保留，设备恢复后重试可自动连上
    if not session.alive and not _try_reconnect(session):
        return error_line(
            f"{_dev_label(sid)}连接中断，自动重连失败（设备可能未开机/网络不通）。"
            f"设备恢复后重试将自动重连")

    try:
        with TerminalRuntime.active_exec_lock:
            TerminalRuntime.active_exec_session = session
        try:
            result = session.execute(command, timeout=timeout,
                                     max_output_chars=max_output_chars)
        finally:
            with TerminalRuntime.active_exec_lock:
                TerminalRuntime.active_exec_session = None
        out = f"[{_dev_label(sid)}] {result}"
        if hint_cd_only:
            out += ("\n[提示: 单通道执行下 cd 不影响后续命令，"
                    "请用 'cd X && 命令'；Windows 跨盘符请用 'cd /d X && 命令']")
        return out
    except Exception as e:
        # 执行中连接掉线：重连成功让 AI 直接重试（原命令结果未知，不能当成功）
        if not session.alive and _try_reconnect(session):
            return error_line(f"{_dev_label(sid)}连接曾中断，已自动重连，请重试命令")
        return error_line(f"{_dev_label(sid)}命令执行失败: {e}")


def _status() -> str:
    """查看所有会话状态"""
    with TerminalRuntime.sessions_lock:
        lines = ["[SSH会话]", "dev0: 本机(当前设备)"]
        if not TerminalRuntime.sessions:
            lines.append(f"(无已连接设备，最多支持{TerminalRuntime.max_sessions}个并发终端)")
            return "\n".join(lines)
        for sid in range(TerminalRuntime.max_sessions):
            if sid not in TerminalRuntime.sessions:
                lines.append(f"  {_dev_label(sid)}: [未连接]")
                continue
            session = TerminalRuntime.sessions[sid]
            if session is None:
                lines.append(f"  {_dev_label(sid)}: [连接中...]")
                continue
            alive = "活跃" if session.alive else "已断开"
            lines.append(f"  {_dev_label(sid)}: {session.username}@{session.host} [{alive}]")
        return "\n".join(lines)


def _close(session_id: int, host: str) -> str:
    """关闭会话。session_id=-1 且无 host → 关闭全部；host 为 devN 形式"""
    with TerminalRuntime.sessions_lock:
        if session_id < 0 and not host:
            count = len(TerminalRuntime.sessions)
            for session in TerminalRuntime.sessions.values():
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
            TerminalRuntime.sessions.clear()
            return f"[已关闭{count}个会话]"

        if session_id >= 0:
            if session_id not in TerminalRuntime.sessions:
                return f"{_dev_label(session_id)}未连接"
            session = TerminalRuntime.sessions.pop(session_id)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            return f"[已关闭 {_dev_label(session_id)}]"

        m = TerminalRuntime.RE_DEV.match(host.strip())
        if m:
            d = int(m.group(1))
            if d == 0:
                return "[dev0是当前设备(本机)，无需关闭]"
            sid = d - 1
            if sid not in TerminalRuntime.sessions:
                return f"dev{d}未连接"
            session = TerminalRuntime.sessions.pop(sid)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            return f"[已关闭 {_dev_label(sid)}]"

        return error_line(f"{_dev_hint_locked()}")


def _get_transfer_session(dev: str) -> Optional["SSHSession"]:
    """devN（已归一化）→ 已连接的 SSHSession; 未连接/已断开且重连失败返回 None"""
    m = TerminalRuntime.RE_DEV.match(dev)
    if not m:
        return None
    sid = int(m.group(1)) - 1
    with TerminalRuntime.sessions_lock:
        session = TerminalRuntime.sessions.get(sid)
    if session is None:
        return None
    if not session.alive and not _try_reconnect(session):
        return None
    return session


def _transfer_local_to_remote(local_path: str, session, target_ref: str,
                              target_path: str, max_transfer_mb: int) -> str:
    """本机 → 被控设备"""
    if not os.path.exists(local_path):
        return error_line(f"源文件不存在: {local_path}")
    if os.path.isdir(local_path):
        return error_line(
            f"源是目录，transfer仅支持文件传输。请先打包为文件再传输: {local_path}")

    size = os.path.getsize(local_path)
    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return error_line(err)

    try:
        if not _ensure_remote_dir(session, target_path):
            parent = target_path.rsplit("/", 1)[0] or "/"
            return error_line(f"无法创建远程目标目录: {parent}（可能无写权限）")
        sftp = session.open_sftp()
        try:
            sftp.put(local_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return error_line(f"传输失败: {e}")
    return (f"[已传输: 本机:{local_path} → {target_ref}:{target_path} "
            f"({_format_size(size)})]")


def _transfer_remote_to_local(session, source_ref: str, source_path: str,
                              target_path: str, max_transfer_mb: int) -> str:
    """被控设备 → 本机"""
    info = _get_remote_file_size(session, source_path)
    if info is None:
        return error_line(f"源文件不存在或无法访问: {source_ref}:{source_path}")
    size, is_dir = info
    if is_dir:
        return error_line(
            f"源是目录，transfer仅支持文件传输。请先打包为文件再传输: {source_path}")

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return error_line(err)

    parent = os.path.dirname(target_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            return error_line(f"无法创建本地目标目录: {target_path}")

    try:
        sftp = session.open_sftp()
        try:
            sftp.get(source_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return error_line(f"传输失败: {e}")
    return (f"[已传输: {source_ref}:{source_path} → 本机:{target_path} "
            f"({_format_size(size)})]")


def _transfer_remote_to_remote(src_session, source_ref: str, source_path: str,
                               dst_session, target_ref: str, target_path: str,
                               max_transfer_mb: int) -> str:
    """被控设备 → 被控设备（经本机分块中转，中断时给出已传输量）"""
    info = _get_remote_file_size(src_session, source_path)
    if info is None:
        return error_line(f"源文件不存在或无法访问: {source_ref}:{source_path}")
    size, is_dir = info
    if is_dir:
        return error_line(
            f"源是目录，transfer仅支持文件传输。请先打包为文件再传输: {source_path}")

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return error_line(err)

    copied = 0
    try:
        if not _ensure_remote_dir(dst_session, target_path):
            parent = target_path.rsplit("/", 1)[0] or "/"
            return error_line(f"无法创建远程目标目录: {parent}（可能无写权限）")
        src_sftp = src_session.open_sftp()
        try:
            dst_sftp = dst_session.open_sftp()
            try:
                with src_sftp.open(source_path, "rb") as fsrc:
                    with dst_sftp.open(target_path, "wb") as fdst:
                        while True:
                            data = fsrc.read(_TRANSFER_CHUNK)
                            if not data:
                                break
                            fdst.write(data)
                            copied += len(data)
            finally:
                dst_sftp.close()
        finally:
            src_sftp.close()
    except Exception as e:
        return error_line(
            f"传输中断，已传输 {_format_size(copied)}/{_format_size(size)}: {e}")
    return (f"[已传输: {source_ref}:{source_path} → {target_ref}:{target_path} "
            f"({_format_size(size)})]")


def _transfer(source_host: str, source_path: str, target_host: str,
              target_path: str, _tool_context=None) -> str:
    """设备间文件传输: 本机↔被控设备、被控设备↔被控设备"""
    if not source_path:
        return error_line("transfer需要提供source_path（源文件路径）")
    if not target_path:
        return error_line("transfer需要提供target_path（目标文件路径）")

    for ref in (source_host, target_host):
        err = _device_error(ref)
        if err:
            return err

    src_dev = _normalize_device(source_host)
    dst_dev = _normalize_device(target_host)

    if src_dev == dst_dev and source_path == target_path:
        return error_line("源和目标相同，无需传输")
    if not src_dev and not dst_dev:
        return error_line("源和目标都是本机(dev0)，请使用本地文件操作工具")

    max_transfer_mb = _tool_context.max_transfer_mb if _tool_context is not None else 100

    src_session = None
    dst_session = None
    if src_dev:
        src_session = _get_transfer_session(src_dev)
        if src_session is None:
            return error_line(f"源设备 {source_host} 未连接或已断开，请先connect。"
                              f"当前已连接: {_list_devices()}")
    if dst_dev:
        dst_session = _get_transfer_session(dst_dev)
        if dst_session is None:
            return error_line(f"目标设备 {target_host} 未连接或已断开，请先connect。"
                              f"当前已连接: {_list_devices()}")

    if src_session is None:
        return _transfer_local_to_remote(source_path, dst_session, target_host,
                                         target_path, max_transfer_mb)
    if dst_session is None:
        return _transfer_remote_to_local(src_session, source_host, source_path,
                                         target_path, max_transfer_mb)
    return _transfer_remote_to_remote(src_session, source_host, source_path,
                                      dst_session, target_host, target_path,
                                      max_transfer_mb)


# ═══════════════════════════════════════════════════════════════
# transfer 辅助
# ═══════════════════════════════════════════════════════════════

# 远程→远程中转的分块大小
_TRANSFER_CHUNK = 1024 * 1024


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def _check_transfer_size(size_bytes: int, max_transfer_mb: int) -> Optional[str]:
    if max_transfer_mb <= 0:
        return None
    max_bytes = max_transfer_mb * 1024 * 1024
    if size_bytes > max_bytes:
        return f"文件大小 {_format_size(size_bytes)} 超过传输上限 {_format_size(max_bytes)}"
    return None


def _get_remote_file_size(session, path: str) -> Optional[tuple]:
    """获取远程文件 (大小, 是否目录)。返回None表示无法访问（不存在/权限）。"""
    try:
        sftp = session.open_sftp()
        try:
            st = sftp.stat(path)
            import stat as _stat
            return st.st_size, _stat.S_ISDIR(st.st_mode)
        finally:
            sftp.close()
    except Exception:
        return None


def _ensure_remote_dir(session, path: str) -> bool:
    """确保远程目录存在（按 / 分段 mkdir），返回是否成功。"""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent or parent == "/":
        return True
    try:
        sftp = session.open_sftp()
        try:
            parts = [p for p in parent.split("/") if p]
            cur = ""
            for p in parts:
                cur += "/" + p
                try:
                    sftp.stat(cur)
                except IOError:
                    try:
                        sftp.mkdir(cur)
                    except IOError:
                        pass
            return True
        finally:
            sftp.close()
    except Exception:
        return False
