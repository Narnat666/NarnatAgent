"""
Terminal工具 ── 多终端可持续SSH + 文件传输

核心设计:
- 支持最多max_sessions(5)个并发SSH会话，内部用session_id(0-4)标识
- AI通过dev编号引用设备: dev0=本机，dev1..devn=被控设备(终端N-1)
- 会话持久化，多次调用复用同一连接
- timeout默认120秒，超时告知AI命令仍在运行（AI可去其他终端继续工作）
- transfer: 在任意设备间传输文件（本机↔设备、设备↔设备），远程间流式中转不落盘
"""

import os
import re
import stat
import sys
import threading
from typing import Optional

import paramiko

from .ssh_session import SSHSession, _truncate_output

__all__ = ["execute", "DEFINITION", "get_session", "SSHSession", "kill_active_exec", "cleanup", "TerminalRuntime", "resolve_dev_display"]


class TerminalRuntime:
    """Terminal 工具全部模块级状态与参数（原散落的模块级全局收敛于此）。

    - sessions/active_exec_session: 跨调用共享的可变状态
    - max_sessions: assembly 启动时按配置写入（set_max_sessions）
    - 正则/缓冲常量: 仅本模块使用
    """
    # 删除命令正则
    # 边界后跟空白或/：覆盖无空格变体（rd/s、del/f、rmdir/q）及erase/format；
    # \b边界防止误伤 delphi、3rd、formatting 等普通词
    RE_DELETE = re.compile(
        r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
        r"|\bRemove-Item\b",
        re.IGNORECASE,
    )

    # dev编号正则: dev0=本机(当前设备), devN(N>=1)=第N台被控设备(终端N-1)
    RE_DEV = re.compile(r"^dev(\d+)$", re.IGNORECASE)

    # 匹配 git 命令的简单正则（出现 git 即命中）
    RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

    max_sessions = 5  # 默认5个并发SSH会话，assembly 启动时按配置覆盖

    TRANSFER_BUFFER_SIZE = 65536  # 64KB 流式传输buffer

    # session_id(0-4) → SSHSession
    sessions: dict = {}
    sessions_lock = threading.Lock()

    # 当前正在执行命令的SSH会话（agent层ESC打断后调用kill_active_exec杀死远程进程）
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
        "description": "多终端持久SSH，最多5个并发。connect建立会话，exec执行命令，input发送交互输入，status查看所有设备，close关闭会话，transfer在任意设备间传输文件(本机↔设备、设备↔设备)。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "exec", "input", "status", "close", "transfer"],
                    "description": "操作类型（默认exec）",
                },
                "host": {"type": "string", "description": "connect时填被控设备IP/域名（建立连接，连接成功后每个设备返回唯一dev编号）；exec/input/close时填dev编号（dev1..devn）"},
                "username": {"type": "string", "description": "SSH用户名（connect时使用）"},
                "port": {"type": "integer", "description": "SSH端口（默认22）"},
                "password": {"type": "string", "description": "认证凭据（connect时使用）：登录密码或私钥路径（如~/.ssh/id_rsa），不填则自动尝试默认密钥。填密码时sudo密码自动用同登录密码注入"},
                "command": {"type": "string", "description": "执行的命令（需先设action=exec）"},
                "input": {"type": "string", "description": "交互输入内容（需先设action=input，如sudo密码、y/n确认）。仅当有命令在等待输入时有效（通常是上个命令超时仍在后台运行）；发送 ^C 可中断仍在运行的命令；空闲终端发送会被拒绝"},
                "timeout": {"type": "integer", "description": "命令超时秒数（正整数）。exec/input默认120，超时后命令继续后台运行，可用input应答其交互提示或^C中断；connect默认15，连不上时快速报错"},
                "max_output_chars": {"type": "integer", "description": "最大输出字符数（正整数，默认8000，超出截断并提示）"},
                "source_host": {"type": "string", "description": "传输源设备：默认dev0即本机（可省略），设置dev1..devn选择被控设备（action=transfer时使用）"},
                "source_path": {"type": "string", "description": "源文件在源设备上的绝对路径（action=transfer时使用）"},
                "target_host": {"type": "string", "description": "传输目标设备：默认dev0即本机（可省略），设置dev1..devn选择被控设备（action=transfer时使用）"},
                "target_path": {"type": "string", "description": "目标文件在目标设备上的绝对路径（action=transfer时使用）"},
            },
            "required": [],
        },
    },
}


def kill_active_exec():
    """ESC打断：发Ctrl+C终止远程进程，设中断标志让本地读取线程退出。

    同时覆盖后台运行中的命令（超时后仍在运行的busy会话），
    使ESC能自愈busy状态：watcher检测到中断标志后退出并清除busy。
    """
    with TerminalRuntime.active_exec_lock:
        session = TerminalRuntime.active_exec_session
    if session is not None:
        session._interrupt.set()
        try:
            session._channel.send("\x03")
        except Exception:
            pass

    # 后台运行中的busy会话（无活跃exec时也打断）
    with TerminalRuntime.sessions_lock:
        busy_sessions = [
            s for s in TerminalRuntime.sessions.values()
            if s is not None and s._busy and s is not session
        ]
    for s in busy_sessions:
        s._interrupt.set()
        try:
            s._channel.send("\x03")
        except Exception:
            pass


# 公开接口

def execute(
    action: str = "exec",
    host: str = "",
    username: str = "",
    port: int = 22,
    key_path: str = "",
    password: str = "",
    sudo_password: str = "",
    command: str = "",
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
    """
    Terminal工具：多终端可持续SSH + 文件传输。

    action:
      connect  - 建立SSH会话（首次连接或重连），连接成功后返回该设备的dev编号
      exec     - 在指定设备（host=devN）执行命令
      input    - 向设备发送交互输入（如sudo密码、确认提示等）
      status   - 查看所有设备状态（dev0本机 + dev1..devn）
      close    - 关闭指定设备（host=devN）会话
      transfer - 在任意设备间传输文件（dev0本机 ↔ dev1..devn被控设备）

    device标识:
      dev0=本机(无需connect)，dev1..devn=已connect的被控设备
      设备引用统一用dev编号（host参数），不填host时自动选唯一会话

    session_id:
      内部参数，AI无需使用（设备引用统一用host=devN）

    sudo_password:
      connect时设置，后续exec遇到sudo密码提示自动注入

    max_output_chars:
      返回内容最大字符数，正整数，默认8000

    transfer参数:
      source_host  - 传输源设备，dev0=本机(可省略)，dev1..devn=被控设备
      source_path  - 源文件在源设备上的绝对路径
      target_host  - 传输目标设备，dev0=本机(可省略)，dev1..devn=被控设备
      target_path  - 目标文件在目标设备上的绝对路径
    """
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    try:
        port = int(port) if port is not None else 22
        timeout = int(timeout) if timeout is not None else None
        session_id = int(session_id) if session_id is not None else -1
        max_output_chars = int(max_output_chars) if max_output_chars is not None else 8000
    except (TypeError, ValueError):
        return "[错误: port/timeout/session_id/max_output_chars需为整数]"

    if action == "connect":
        # connect默认超时15秒（exec/input默认120秒）：连错IP/黑洞IP时快速失败，
        # 避免OS默认TCP重试阻塞数十秒。同时受全局超时上限约束
        connect_timeout = 15 if timeout is None else timeout
        if _tool_context and _tool_context.max_timeout_seconds > 0:
            connect_timeout = min(connect_timeout, _tool_context.max_timeout_seconds)
        return _connect(host, username, port, key_path, password, sudo_password, session_id, connect_timeout)
    elif action == "exec":
        exec_timeout = 120 if timeout is None else timeout
        return _exec(session_id, host, command, exec_timeout, max_output_chars, _tool_context)
    elif action == "input":
        input_timeout = 120 if timeout is None else timeout
        return _input(session_id, host, input, input_timeout, max_output_chars, _tool_context)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(session_id, host)
    elif action == "transfer":
        return _transfer(source_host, source_path, target_host, target_path, _tool_context)
    else:
        return f"[错误: 未知action '{action}'，可选: connect/exec/input/status/close/transfer]"


def _allocate_session_id() -> int:
    """分配一个空闲的session_id，返回-1表示已满。

    死会话（channel已关闭，如网络断开）自动回收槽位：
    否则连接意外断开后槽位被死会话占用，AI重连时报"已达最大会话数"却无法释放。
    调用者需持有TerminalRuntime.sessions_lock。
    """
    for i in range(TerminalRuntime.max_sessions):
        session = TerminalRuntime.sessions.get(i)
        if session is None:
            return i
        if session._channel.closed:
            session.close()
            del TerminalRuntime.sessions[i]
            return i
    return -1


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
    """文件工具(Read/Edit/Write)设备标识错误时的统一指导：
    附当前已连接设备清单，减少AI回查status的往返"""
    base = "设备标识使用devN编号(dev0=本机, dev1..devn=被控设备)"
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
        return f"[错误: {_dev_hint_locked()}]"


def _dev_label(sid: int) -> str:
    """终端session_id → dev编号标签: 终端0=dev1, 终端1=dev2 ..."""
    return f"dev{sid + 1}(终端{sid})"


def _list_devices_locked() -> str:
    """当前已连接设备的dev清单（调用者需持有TerminalRuntime.sessions_lock）"""
    devs = [f"dev{sid + 1}({s.username}@{s.host})" for sid, s in sorted(TerminalRuntime.sessions.items())]
    return "、".join(devs) if devs else "(无)"


def _list_devices() -> str:
    """当前已连接设备的dev清单（用于报错提示）"""
    with TerminalRuntime.sessions_lock:
        return _list_devices_locked()


def _local_host() -> str:
    """本机显示名：优先取主机名，失败回退localhost"""
    try:
        import socket
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
        if session is not None and not session._channel.closed:
            return session.host
    return h


def _dev_hint_locked() -> str:
    """设备标识错误时的统一指导：说明devN用法 + 列出当前可用设备（调用者需持有TerminalRuntime.sessions_lock）"""
    devs = _list_devices_locked()
    if devs == "(无)":
        return "设备标识使用devN编号(dev0=本机, dev1..devn=被控设备)。当前无已连接设备，请先connect"
    return f"设备标识使用devN编号(dev0=本机, dev1..devn=被控设备)。当前已连接: {devs}"


def _resolve_session_id(session_id: int, host: str = "") -> tuple[int, "SSHSession"]:
    """解析session_id，返回 (session_id, session) 或抛出ValueError

    逻辑:
    1. session_id >= 0: 直接查找
    2. session_id == -1 且指定host: 按host模糊匹配
    3. session_id == -1 且无host: 只有一个会话时自动选择
    """
    with TerminalRuntime.sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id not in TerminalRuntime.sessions:
                raise ValueError(f"{_dev_label(session_id)}未连接，请先connect。当前已连接: {_list_devices_locked()}")
            return session_id, TerminalRuntime.sessions[session_id]

        # 未指定session_id，按host匹配（优先devN，其次IP/用户名@IP宽松匹配）
        if host:
            h = host.strip()
            m = TerminalRuntime.RE_DEV.match(h)
            if not m:
                # 宽松匹配: 允许直接用IP或用户名@IP引用已连接设备，
                # 减少AI回查dev编号的往返（如 exec host=192.168.1.213）
                matched = [
                    sid for sid, s in TerminalRuntime.sessions.items()
                    if s is not None and (s.host == h or f"{s.username}@{s.host}" == h)
                ]
                if len(matched) == 1:
                    return matched[0], TerminalRuntime.sessions[matched[0]]
                if len(matched) > 1:
                    raise ValueError(
                        f"多个会话匹配 '{h}'，请用dev编号区分: "
                        f"{'、'.join(f'dev{s + 1}({TerminalRuntime.sessions[s].username}@{TerminalRuntime.sessions[s].host})' for s in matched)}"
                    )
                raise ValueError(_dev_hint_locked())
            d = int(m.group(1))
            if d == 0:
                raise ValueError("dev0是当前设备(本机)，无SSH会话，仅transfer可用")
            sid = d - 1
            if sid >= TerminalRuntime.max_sessions or sid not in TerminalRuntime.sessions:
                raise ValueError(f"dev{d}未连接，请先connect。当前已连接: {_list_devices_locked()}")
            return sid, TerminalRuntime.sessions[sid]

        # 未指定session_id和host，自动选择唯一会话
        if len(TerminalRuntime.sessions) == 1:
            sid = list(TerminalRuntime.sessions.keys())[0]
            return sid, TerminalRuntime.sessions[sid]
        elif len(TerminalRuntime.sessions) == 0:
            raise ValueError("无活跃会话，请先connect")
        else:
            keys = [f"dev{s + 1}({TerminalRuntime.sessions[s].username}@{TerminalRuntime.sessions[s].host})" for s in sorted(TerminalRuntime.sessions.keys())]
            raise ValueError(f"有多个会话，请指定host=dev编号，当前已连接: {'、'.join(keys)}")


def _connect(host: str, username: str, port: int = 22,
             key_path: str = "", password: str = "",
             sudo_password: str = "",
             session_id: int = -1,
             timeout: int = 15) -> str:
    """建立SSH会话。timeout为连接超时（默认15秒），黑洞IP快速失败。"""
    if not host or not username:
        return "[错误: connect需要提供host和username]"

    # timeout非正整数时兜底默认15秒（LLM可能传0/负数）
    if not timeout or timeout <= 0:
        timeout = 15

    with TerminalRuntime.sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id >= TerminalRuntime.max_sessions:
                return f"[错误: session_id范围0-{TerminalRuntime.max_sessions - 1}]"
            if session_id in TerminalRuntime.sessions:
                session = TerminalRuntime.sessions[session_id]
                if not session._channel.closed:
                    return f"[{_dev_label(session_id)}已连接: {session.username}@{session.host}]\n{session.prompt}"
                else:
                    session.close()
                    del TerminalRuntime.sessions[session_id]
            alloc_id = session_id
        else:
            # 自动分配
            alloc_id = _allocate_session_id()
            if alloc_id < 0:
                active = [_dev_label(s) for s in sorted(TerminalRuntime.sessions.keys())]
                return f"[错误: 已达最大会话数({TerminalRuntime.max_sessions})，当前已连接: {active}，请先close释放]"

    try:
        # password 三合一：私钥路径（~或路径分隔符开头/包含 + 文件存在）→ 密钥认证；
        # 否则视为密码；空 → 由 paramiko 自动尝试默认密钥（look_for_keys/agent）
        resolved_key_path = key_path or ""
        resolved_password = password or ""
        looks_like_path = False
        if not resolved_key_path and resolved_password:
            looks_like_path = resolved_password.startswith(("~", "/", "\\", ".")) or "/" in resolved_password or "\\" in resolved_password
            if looks_like_path and os.path.isfile(os.path.expanduser(resolved_password)):
                resolved_key_path = resolved_password
                resolved_password = ""

        kwargs = {"host": host, "username": username, "port": port}
        if resolved_key_path:
            kwargs["key_path"] = resolved_key_path
        if resolved_password:
            kwargs["password"] = resolved_password
        # sudo密码默认与登录密码相同；显式传入的sudo_password优先（隐藏兼容参数）
        kwargs["sudo_password"] = sudo_password or resolved_password

        kwargs["timeout"] = timeout
        session = SSHSession(**kwargs)

        # 注册活跃会话，让 ESC 能在 connect 的阻塞初始化阶段打断
        with TerminalRuntime.active_exec_lock:
            TerminalRuntime.active_exec_session = session
        try:
            session._initialize()
        finally:
            with TerminalRuntime.active_exec_lock:
                TerminalRuntime.active_exec_session = None

        with TerminalRuntime.sessions_lock:
            TerminalRuntime.sessions[alloc_id] = session

        # 重复连接提示: 相同设备已有会话时提醒AI直接用现有dev编号，
        # 避免无谓地多开会话占用槽位、造成多终端选择歧义
        # 示例引用用纯devN（AI可照抄host参数），展示名devN(终端N)括号含内部槽位号，照抄会报错
        with TerminalRuntime.sessions_lock:
            dup_sids = [
                sid for sid, s in TerminalRuntime.sessions.items()
                if s is not None and sid != alloc_id
                and not s._channel.closed
                and s.host == host and s.username == username
            ]
            dup_devs = [_dev_label(sid) for sid in dup_sids]

        parts = [f"[已连接 {_dev_label(alloc_id)}: {username}@{host}]"]
        if dup_devs:
            dup_refs = "、".join(f"dev{sid + 1}" for sid in dup_sids)
            parts.append(
                f"[注意: 相同设备已有连接: {'、'.join(dup_devs)}。"
                f"如非必要请勿重复连接，可直接用 host={dup_refs} 执行命令]"
            )
        if session._initial_output:
            # 折叠登录横幅噪音：>4行时只保留首行(系统版本)+末两行(last login/prompt)
            banner_lines = session._initial_output.rstrip().split("\n")
            if len(banner_lines) > 4:
                parts.append(banner_lines[0])
                parts.append(f"...(已省略{len(banner_lines) - 3}行登录横幅)")
                parts.append("\n".join(banner_lines[-2:]))
            else:
                parts.append(session._initial_output)
        else:
            parts.append(session.prompt)
        return "\n".join(parts)

    except paramiko.AuthenticationException:
        hint = ""
        if not password and not key_path:
            hint = "。未提供password，大多数设备需要密码认证，请在password参数填登录密码后重试"
        elif looks_like_path and not os.path.isfile(os.path.expanduser(password)):
            hint = "。password疑似私钥路径但本地文件不存在，请确认路径或改填登录密码"
        return f"[错误: 认证失败({username}@{host})，请检查password{hint}]"
    except paramiko.SSHException as e:
        return f"[错误: SSH连接失败({username}@{host}): {e}]"
    except Exception as e:
        return f"[错误: 连接失败({username}@{host}): {e}]"


def _exec(session_id: int, host: str, command: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str:
    """在指定会话中执行命令"""
    if not command:
        return "[错误: exec需要提供command]"
    if timeout <= 0:
        return "[错误: timeout需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    # 安全检查：删除命令和git命令根据配置决定是否需要确认
    need_confirm = False
    tc = _tool_context
    if tc and not tc.rm_skip_confirm and TerminalRuntime.RE_DELETE.search(command):
        need_confirm = True
    elif tc and not tc.git_skip_confirm and TerminalRuntime.RE_GIT.search(command):
        need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            # Windows: prompt_toolkit和input()用不同的输入系统，直接用input()确认
            if tc and tc.confirm_callback and not tc.confirm_callback(command):
                return "[操作已取消: 此命令需用户确认]"
        else:
            # Linux/macOS: 终端被prompt_toolkit占用，无法在子线程中读取输入
            # 用户已确认过（_delete_confirmed=True），直接执行
            if tc and tc._delete_confirmed:
                tc._delete_confirmed = False
            else:
                # 暂存命令，返回AWAIT_CONFIRM标记，由agent主循环在#提示符下等待用户确认
                if tc is not None:
                    tc.pending_delete = ("Terminal", {
                        "action": "exec",
                        "session_id": session_id,
                        "host": host,
                        "command": command,
                        "timeout": timeout,
                        "max_output_chars": max_output_chars,
                    })
                return "__AWAIT_CONFIRM__"

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return f"[错误: {e}]"

    if session._channel.closed:
        with TerminalRuntime.sessions_lock:
            TerminalRuntime.sessions.pop(sid, None)
        session.close()
        return f"[错误: {_dev_label(sid)}会话已断开，请重新connect]"

    try:
        # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
        with TerminalRuntime.active_exec_lock:
            TerminalRuntime.active_exec_session = session
        try:
            result = session.execute(command, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with TerminalRuntime.active_exec_lock:
                TerminalRuntime.active_exec_session = None
        # 在结果前标注dev编号
        return f"[{_dev_label(sid)}] {result}"
    except Exception as e:
        return f"[错误: {_dev_label(sid)}命令执行失败: {e}]"


def _input(session_id: int, host: str, input: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str:
    """向终端发送交互输入"""
    if not input:
        return "[错误: input需要提供input内容]"
    if timeout <= 0:
        return "[错误: timeout需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    # 安全检查：input内容与exec一致走删除/git确认（防止通过input绕过安全确认）
    need_confirm = False
    tc = _tool_context
    if tc and not tc.rm_skip_confirm and TerminalRuntime.RE_DELETE.search(input):
        need_confirm = True
    elif tc and not tc.git_skip_confirm and TerminalRuntime.RE_GIT.search(input):
        need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            if tc and tc.confirm_callback and not tc.confirm_callback(input):
                return "[操作已取消: 此命令需用户确认]"
        else:
            if tc and tc._delete_confirmed:
                tc._delete_confirmed = False
            else:
                if tc is not None:
                    tc.pending_delete = ("Terminal", {
                        "action": "input",
                        "session_id": session_id,
                        "host": host,
                        "input": input,
                        "timeout": timeout,
                        "max_output_chars": max_output_chars,
                    })
                return "__AWAIT_CONFIRM__"

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return f"[错误: {e}]"

    if session._channel.closed:
        with TerminalRuntime.sessions_lock:
            TerminalRuntime.sessions.pop(sid, None)
        session.close()
        return f"[错误: {_dev_label(sid)}会话已断开，请重新connect]"

    try:
        # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
        with TerminalRuntime.active_exec_lock:
            TerminalRuntime.active_exec_session = session
        try:
            result = session.send_input(input, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with TerminalRuntime.active_exec_lock:
                TerminalRuntime.active_exec_session = None
        return f"[{_dev_label(sid)}] {result}"
    except Exception as e:
        return f"[错误: {_dev_label(sid)}输入发送失败: {e}]"


def _status() -> str:
    """查看所有会话状态"""
    with TerminalRuntime.sessions_lock:
        lines = ["dev0: 本机(当前设备)"]
        for sid in range(TerminalRuntime.max_sessions):
            if sid in TerminalRuntime.sessions:
                session = TerminalRuntime.sessions[sid]
                alive = "活跃" if not session._channel.closed else "已断开"
                busy = "忙" if session._busy else "闲"
                # 只显示cwd（prompt会重复user@host，纯噪音；cwd才是AI关心的状态信息）
                lines.append(f"  {_dev_label(sid)}: {session.username}@{session.host} [{alive}|{busy}] 目录:{session._cwd}")
            else:
                lines.append(f"  {_dev_label(sid)}: [未连接]")
        if len(TerminalRuntime.sessions) == 0:
            return "[SSH会话]\n" + "\n".join(lines) + f"\n(无已连接设备，最多支持{TerminalRuntime.max_sessions}个并发终端)"
        return "[SSH会话]\n" + "\n".join(lines)


def _close(session_id: int, host: str) -> str:
    """关闭会话"""
    with TerminalRuntime.sessions_lock:
        if session_id < 0 and not host:
            # 关闭所有
            for session in TerminalRuntime.sessions.values():
                session.close()
            count = len(TerminalRuntime.sessions)
            TerminalRuntime.sessions.clear()
            return f"[已关闭{count}个会话]"

        # 指定了session_id
        if session_id >= 0:
            if session_id not in TerminalRuntime.sessions:
                return f"[{_dev_label(session_id)}未连接]"
            TerminalRuntime.sessions[session_id].close()
            del TerminalRuntime.sessions[session_id]
            return f"[已关闭 {_dev_label(session_id)}]"

        # 按host匹配（只认devN）
        if host:
            m = TerminalRuntime.RE_DEV.match(host.strip())
            if not m:
                return f"[错误: {_dev_hint_locked()}]"
            d = int(m.group(1))
            if d == 0:
                return "[dev0是当前设备(本机)，无需关闭]"
            sid = d - 1
            if sid >= TerminalRuntime.max_sessions or sid not in TerminalRuntime.sessions:
                return f"[dev{d}未连接]"
            TerminalRuntime.sessions[sid].close()
            del TerminalRuntime.sessions[sid]
            return f"[已关闭 {_dev_label(sid)}]"

        return "[错误: close需要指定dev编号(如host=dev1)或session_id]"


def cleanup():
    """程序退出时清理所有会话。channel立即关闭，transport后台回收。"""
    with TerminalRuntime.sessions_lock:
        for session in TerminalRuntime.sessions.values():
            session.close()
        TerminalRuntime.sessions.clear()


def get_session(session_id: int = -1, host: str = "") -> Optional["SSHSession"]:
    """获取指定SSH会话（供SFTP等内部使用）"""
    try:
        _, session = _resolve_session_id(session_id, host)
        return session
    except ValueError:
        return None


# ── 文件传输 ──

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


def _get_remote_file_size(session: "SSHSession", path: str) -> Optional[tuple]:
    """获取远程文件 (大小, 是否目录)。目录也返回而非None——
    区分"不存在"与"是目录"，让transfer给出明确指引而非paramiko的"Failure"。
    返回None表示无法访问（不存在/权限）。"""
    try:
        sftp = session._client.open_sftp()
        try:
            st = sftp.stat(path)
            return st.st_size, stat.S_ISDIR(st.st_mode)
        finally:
            sftp.close()
    except Exception:
        return None


def _ensure_remote_dir(session: "SSHSession", remote_path: str) -> bool:
    parent = remote_path.rsplit("/", 1)[0]
    if not parent or parent == remote_path:
        return True
    try:
        sftp = session._client.open_sftp()
        try:
            # 逐级创建，paramiko sftp.mkdir 不递归
            parts = parent.strip("/").split("/")
            cur = ""
            for p in parts:
                cur += "/" + p
                try:
                    sftp.stat(cur)
                except IOError:
                    sftp.mkdir(cur)
        finally:
            sftp.close()
        return True
    except Exception:
        return False


def _ensure_local_dir(local_path: str) -> bool:
    parent = os.path.dirname(local_path)
    if not parent:
        return True
    try:
        os.makedirs(parent, exist_ok=True)
        return True
    except OSError:
        return False


def _transfer_local_to_remote(source_path: str, target_host: str, target_path: str, max_transfer_mb: int) -> str:
    if os.path.isdir(source_path):
        return (f"[错误: 源是目录，transfer仅支持文件传输。"
                f"目录请先用 Shell 打包（如 tar czf x.tar.gz 目录）再传输: {source_path}]")
    if not os.path.isfile(source_path):
        return f"[错误: 源文件不存在: {source_path}]"

    size = os.path.getsize(source_path)
    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"[错误: {err}]"

    session = get_session(host=target_host)
    if session is None:
        return f"[错误: 目标设备 {target_host} 未连接，请先connect。当前已连接: {_list_devices()}]"

    if not _ensure_remote_dir(session, target_path):
        return f"[错误: 无法创建远程目标目录: {target_path}]"

    try:
        sftp = session._client.open_sftp()
        try:
            sftp.put(source_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return f"[错误: 传输失败: {e}]"

    return f"[已传输: 本机:{source_path} → {target_host}:{target_path} ({_format_size(size)})]"


def _transfer_remote_to_local(source_host: str, source_path: str, target_path: str, max_transfer_mb: int) -> str:
    session = get_session(host=source_host)
    if session is None:
        return f"[错误: 源设备 {source_host} 未连接，请先connect。当前已连接: {_list_devices()}]"

    st = _get_remote_file_size(session, source_path)
    if st is None:
        return f"[错误: 源文件不存在或无法访问: {source_host}:{source_path}]"
    size, is_dir = st
    if is_dir:
        return (f"[错误: 源是目录，transfer仅支持文件传输。"
                f"目录请先用 exec 打包（如 tar czf /tmp/x.tar.gz 目录）再传输: {source_host}:{source_path}]")

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"[错误: {err}]"

    if not _ensure_local_dir(target_path):
        return f"[错误: 无法创建本地目标目录: {target_path}]"

    try:
        sftp = session._client.open_sftp()
        try:
            sftp.get(source_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return f"[错误: 传输失败: {e}]"

    return f"[已传输: {source_host}:{source_path} → 本机:{target_path} ({_format_size(size)})]"


def _transfer_remote_to_remote(source_host: str, source_path: str, target_host: str, target_path: str, max_transfer_mb: int) -> str:
    src_session = get_session(host=source_host)
    if src_session is None:
        return f"[错误: 源设备 {source_host} 未连接，请先connect。当前已连接: {_list_devices()}]"

    tgt_session = get_session(host=target_host)
    if tgt_session is None:
        return f"[错误: 目标设备 {target_host} 未连接，请先connect。当前已连接: {_list_devices()}]"

    st = _get_remote_file_size(src_session, source_path)
    if st is None:
        return f"[错误: 源文件不存在或无法访问: {source_host}:{source_path}]"
    size, is_dir = st
    if is_dir:
        return (f"[错误: 源是目录，transfer仅支持文件传输。"
                f"目录请先在源设备 exec 打包（如 tar czf /tmp/x.tar.gz 目录）再传输: {source_host}:{source_path}]")

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"[错误: {err}]"

    if not _ensure_remote_dir(tgt_session, target_path):
        return f"[错误: 无法创建远程目标目录: {target_path}]"

    transferred = 0
    try:
        src_sftp = src_session._client.open_sftp()
        tgt_sftp = tgt_session._client.open_sftp()
        try:
            src_file = src_sftp.open(source_path, "rb")
            tgt_file = tgt_sftp.open(target_path, "wb")
            try:
                while True:
                    chunk = src_file.read(TerminalRuntime.TRANSFER_BUFFER_SIZE)
                    if not chunk:
                        break
                    tgt_file.write(chunk)
                    transferred += len(chunk)
            finally:
                src_file.close()
                tgt_file.close()
        finally:
            src_sftp.close()
            tgt_sftp.close()
    except Exception as e:
        return f"[错误: 传输中断，已传输 {_format_size(transferred)}/{_format_size(size)}: {e}]"

    return f"[已传输: {source_host}:{source_path} → {target_host}:{target_path} ({_format_size(size)})]"


def _transfer(source_host: str, source_path: str, target_host: str, target_path: str, _tool_context=None) -> str:
    if not source_path:
        return "[错误: transfer需要提供source_path（源文件路径）]"
    if not target_path:
        return "[错误: transfer需要提供target_path（目标文件路径）]"

    # 设备标识校验（只认devN: dev0=本机可省略，dev1..devN=被控设备）
    source_host = _normalize_device(source_host)
    target_host = _normalize_device(target_host)
    err = _device_error(source_host) or _device_error(target_host)
    if err:
        return err

    if source_host == target_host and source_path == target_path:
        return "[错误: 源和目标相同，无需传输]"

    max_transfer_mb = 100
    if _tool_context and hasattr(_tool_context, "max_transfer_mb"):
        max_transfer_mb = _tool_context.max_transfer_mb

    source_is_local = not source_host
    target_is_local = not target_host

    if source_is_local and target_is_local:
        return "[错误: 源和目标都是本机(dev0)，请使用本地文件操作工具]"
    elif source_is_local:
        return _transfer_local_to_remote(source_path, target_host, target_path, max_transfer_mb)
    elif target_is_local:
        return _transfer_remote_to_local(source_host, source_path, target_path, max_transfer_mb)
    else:
        return _transfer_remote_to_remote(source_host, source_path, target_host, target_path, max_transfer_mb)
