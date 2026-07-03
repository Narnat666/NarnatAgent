"""
Terminal工具 ── 多终端可持续SSH + 文件传输

核心设计:
- 支持最多MAX_SESSIONS(5)个并发SSH会话，每个会话有唯一session_id(0-4)
- AI通过session_id指定在哪个终端操作，实现多终端并行
- 会话持久化，多次调用复用同一连接
- timeout默认120秒，超时告知AI命令仍在运行（AI可去其他终端继续工作）
- transfer: 在已连接的设备间传输文件，远程间流式中转不落盘
"""

import os
import re
import sys
import threading
from typing import Optional

import paramiko

from .ssh_session import SSHSession, _truncate_output

__all__ = ["execute", "DEFINITION", "get_session", "SSHSession", "kill_active_exec", "cleanup", "set_max_sessions"]


# 删除命令正则
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# 匹配 git 命令的简单正则（出现 git 即命中）
_RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

MAX_SESSIONS = 5  # 默认5个并发SSH会话，可通过set_max_sessions修改

_TRANSFER_BUFFER_SIZE = 65536  # 64KB 流式传输buffer


def set_max_sessions(n: int) -> None:
    """设置最大SSH会话数（由Agent初始化时从配置读取）"""
    global MAX_SESSIONS
    MAX_SESSIONS = max(1, min(n, 10))  # 限制1-10

# session_id(0-4) → SSHSession
_sessions: dict[int, "SSHSession"] = {}
_sessions_lock = threading.Lock()

# 当前正在执行命令的SSH会话（agent层ESC打断后调用kill_active_exec杀死远程进程）
_active_exec_session: Optional["SSHSession"] = None
_active_exec_lock = threading.Lock()

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Terminal",
        "description": "多终端持久SSH，最多5个并发。connect建立会话，exec执行命令，input发送交互输入，status查看会话，close关闭会话，transfer在所有已连接设备（含本机）之间自由传输文件（需先connect）。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "exec", "input", "status", "close", "transfer"],
                    "description": "操作类型（默认exec）",
                },
                "host": {"type": "string", "description": "远程主机IP或域名（action=connect时使用）"},
                "username": {"type": "string", "description": "SSH用户名"},
                "port": {"type": "integer", "description": "SSH端口（默认22）"},
                "key_path": {"type": "string", "description": "SSH私钥路径（如~/.ssh/id_rsa）"},
                "password": {"type": "string", "description": "SSH密码（不填则自动尝试密钥认证）"},
                "sudo_password": {"type": "string", "description": "sudo密码（connect时设置，后续exec遇sudo自动注入）"},
                "command": {"type": "string", "description": "执行的命令（需先设action=exec）"},
                "input": {"type": "string", "description": "交互输入内容（需先设action=input，如sudo密码、y/n确认）"},
                "timeout": {"type": "integer", "description": "命令超时秒数（默认120，超时自动返回通知）"},
                "session_id": {"type": "integer", "description": "终端ID 0-4（默认自动分配，exec时需指定目标终端）"},
                "max_output_chars": {"type": "integer", "description": "最大输出字符数（默认2000，超出截断并提示）"},
                "source_host": {"type": "string", "description": "传输源设备（IP或域名），默认本机（action=transfer时使用）"},
                "source_path": {"type": "string", "description": "源文件在源设备上的绝对路径（action=transfer时使用）"},
                "target_host": {"type": "string", "description": "传输目标设备（IP或域名），默认本机（action=transfer时使用）"},
                "target_path": {"type": "string", "description": "目标文件在目标设备上的绝对路径（action=transfer时使用）"},
            },
            "required": [],
        },
    },
}


def kill_active_exec():
    """ESC打断：发Ctrl+C终止远程进程，设中断标志让本地读取线程退出。"""
    with _active_exec_lock:
        session = _active_exec_session
    if session is not None:
        session._interrupt.set()
        try:
            session._channel.send("\x03")
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
    timeout: int = 120,
    session_id: int = -1,
    max_output_chars: int = 2000,
    source_host: str = "",
    source_path: str = "",
    target_host: str = "",
    target_path: str = "",
    _tool_context=None,
) -> str:
    """
    Terminal工具：多终端可持续SSH + 文件传输。

    action:
      connect  - 建立SSH会话（首次连接或重连），自动分配或使用指定session_id
      exec     - 在指定会话中执行命令
      input    - 向终端发送交互输入（如sudo密码、确认提示等）
      status   - 查看当前所有会话状态
      close    - 关闭指定会话
      transfer - 在已连接的设备间传输文件

    session_id:
      0-4  - 指定终端编号
      -1   - 自动选择（connect时自动分配，exec时选唯一活跃会话）

    sudo_password:
      connect时设置，后续exec遇到sudo密码提示自动注入

    max_output_chars:
      返回内容最大字符数，默认2000。设为0或负数表示不限制

    transfer参数:
      source_host  - 源设备host，空字符串表示本机
      source_path  - 源文件在源设备上的绝对路径
      target_host  - 目标设备host，空字符串表示本机
      target_path  - 目标文件在目标设备上的绝对路径
    """
    if action == "connect":
        return _connect(host, username, port, key_path, password, sudo_password, session_id)
    elif action == "exec":
        return _exec(session_id, host, command, timeout, max_output_chars, _tool_context)
    elif action == "input":
        return _input(session_id, host, input, timeout, max_output_chars)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(session_id, host)
    elif action == "transfer":
        return _transfer(source_host, source_path, target_host, target_path, _tool_context)
    else:
        return f"错误: 未知action '{action}'，可选: connect/exec/input/status/close/transfer"


def _allocate_session_id() -> int:
    """分配一个空闲的session_id，返回-1表示已满"""
    for i in range(MAX_SESSIONS):
        if i not in _sessions:
            return i
    return -1


def _resolve_session_id(session_id: int, host: str = "") -> tuple[int, "SSHSession"]:
    """解析session_id，返回 (session_id, session) 或抛出ValueError

    逻辑:
    1. session_id >= 0: 直接查找
    2. session_id == -1 且指定host: 按host模糊匹配
    3. session_id == -1 且无host: 只有一个会话时自动选择
    """
    with _sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id not in _sessions:
                raise ValueError(f"终端{session_id}未连接，请先connect")
            return session_id, _sessions[session_id]

        # 未指定session_id，按host匹配
        if host:
            for sid, session in _sessions.items():
                if host in session.host or host in f"{session.username}@{session.host}":
                    return sid, session
            raise ValueError(f"未找到host={host}的会话，请先connect")

        # 未指定session_id和host，自动选择唯一会话
        if len(_sessions) == 1:
            sid = list(_sessions.keys())[0]
            return sid, _sessions[sid]
        elif len(_sessions) == 0:
            raise ValueError("无活跃会话，请先connect")
        else:
            keys = list(_sessions.keys())
            raise ValueError(f"有多个会话，请指定session_id，当前终端: {keys}")


def _connect(host: str, username: str, port: int = 22,
             key_path: str = "", password: str = "",
             sudo_password: str = "",
             session_id: int = -1) -> str:
    """建立SSH会话"""
    if not host or not username:
        return "错误: connect需要提供host和username"

    with _sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id >= MAX_SESSIONS:
                return f"错误: session_id范围0-{MAX_SESSIONS - 1}"
            if session_id in _sessions:
                session = _sessions[session_id]
                if not session._channel.closed:
                    return f"终端{session_id}已连接: {session.username}@{session.host}\n{session.prompt}"
                else:
                    session.close()
                    del _sessions[session_id]
            alloc_id = session_id
        else:
            # 自动分配
            alloc_id = _allocate_session_id()
            if alloc_id < 0:
                active = list(_sessions.keys())
                return f"错误: 已达最大会话数({MAX_SESSIONS})，当前终端: {active}，请先close释放"

    try:
        kwargs = {"host": host, "username": username, "port": port}
        if key_path:
            kwargs["key_path"] = key_path
        if password:
            kwargs["password"] = password
        if sudo_password:
            kwargs["sudo_password"] = sudo_password

        session = SSHSession(**kwargs)

        # 注册活跃会话，让 ESC 能在 connect 的阻塞初始化阶段打断
        with _active_exec_lock:
            global _active_exec_session
            _active_exec_session = session
        try:
            session._initialize()
        finally:
            with _active_exec_lock:
                _active_exec_session = None

        with _sessions_lock:
            _sessions[alloc_id] = session

        parts = [f"已连接终端{alloc_id}: {username}@{host}"]
        if session._initial_output:
            parts.append(session._initial_output)
        else:
            parts.append(session.prompt)
        return "\n".join(parts)

    except paramiko.AuthenticationException:
        hint = ""
        if not password and not key_path:
            hint = "。未提供password或key_path，大多数设备需要password认证，请提供password参数后重试"
        return f"错误: 认证失败({username}@{host})，请检查key_path或password{hint}"
    except paramiko.SSHException as e:
        return f"错误: SSH连接失败({username}@{host}): {e}"
    except Exception as e:
        return f"错误: 连接失败({username}@{host}): {e}"


def _exec(session_id: int, host: str, command: str, timeout: int = 120, max_output_chars: int = 2000, _tool_context=None) -> str:
    """在指定会话中执行命令"""
    if not command:
        return "错误: exec需要提供command"
    if timeout <= 0:
        return "错误: timeout必须为正整数（秒），默认120秒"

    # 安全检查：删除命令和git命令根据配置决定是否需要确认
    need_confirm = False
    tc = _tool_context
    if tc and not tc.rm_skip_confirm and _RE_DELETE.search(command):
        need_confirm = True
    elif tc and not tc.git_skip_confirm and _RE_GIT.search(command):
        need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            # Windows: prompt_toolkit和input()用不同的输入系统，直接用input()确认
            if tc and tc.confirm_callback and not tc.confirm_callback(command):
                return "操作已取消: 此命令需用户确认"
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
        return f"错误: {e}"

    if session._channel.closed:
        with _sessions_lock:
            _sessions.pop(sid, None)
        session.close()
        return f"错误: 终端{sid}会话已断开，请重新connect"

    try:
        # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
        with _active_exec_lock:
            global _active_exec_session
            _active_exec_session = session
        try:
            result = session.execute(command, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with _active_exec_lock:
                _active_exec_session = None
        # 在结果前标注终端编号
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"错误: 终端{sid}命令执行失败: {e}"


def _input(session_id: int, host: str, input: str, timeout: int = 120, max_output_chars: int = 2000) -> str:
    """向终端发送交互输入"""
    if not input:
        return "错误: input需要提供input内容"
    if timeout <= 0:
        return "错误: timeout必须为正整数（秒），默认120秒"

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return f"错误: {e}"

    if session._channel.closed:
        with _sessions_lock:
            _sessions.pop(sid, None)
        session.close()
        return f"错误: 终端{sid}会话已断开，请重新connect"

    try:
        result = session.send_input(input, timeout=timeout, max_output_chars=max_output_chars)
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"错误: 终端{sid}输入发送失败: {e}"


def _status() -> str:
    """查看所有会话状态"""
    with _sessions_lock:
        if not _sessions:
            return f"(无活跃SSH会话，最多支持{MAX_SESSIONS}个并发终端)"

        lines = []
        for sid in range(MAX_SESSIONS):
            if sid in _sessions:
                session = _sessions[sid]
                alive = "活跃" if not session._channel.closed else "已断开"
                busy = "忙" if session._busy else "闲"
                lines.append(f"  终端{sid}: {session.username}@{session.host} [{alive}|{busy}] {session.prompt}")
            else:
                lines.append(f"  终端{sid}: (空闲)")
        return "SSH会话:\n" + "\n".join(lines)


def _close(session_id: int, host: str) -> str:
    """关闭会话"""
    with _sessions_lock:
        if session_id < 0 and not host:
            # 关闭所有
            for session in _sessions.values():
                session.close()
            count = len(_sessions)
            _sessions.clear()
            return f"已关闭{count}个会话"

        # 指定了session_id
        if session_id >= 0:
            if session_id not in _sessions:
                return f"终端{session_id}未连接"
            _sessions[session_id].close()
            del _sessions[session_id]
            return f"已关闭终端{session_id}"

        # 按host匹配
        matched = None
        for sid, session in _sessions.items():
            if host in session.host or host in f"{session.username}@{session.host}":
                matched = sid
                break

        if matched is None:
            return f"未找到host={host}的会话"

        _sessions[matched].close()
        del _sessions[matched]
        return f"已关闭终端{matched}"


def cleanup():
    """程序退出时清理所有会话。channel立即关闭，transport后台回收。"""
    with _sessions_lock:
        for session in _sessions.values():
            session.close()
        _sessions.clear()


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


def _get_remote_file_size(session: "SSHSession", path: str) -> Optional[int]:
    try:
        sftp = session._client.open_sftp()
        try:
            stat = sftp.stat(path)
            return stat.st_size
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
    if not os.path.isfile(source_path):
        return f"错误: 源文件不存在: {source_path}"

    size = os.path.getsize(source_path)
    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"错误: {err}"

    session = get_session(host=target_host)
    if session is None:
        return f"错误: 目标设备 {target_host} 未连接，请先connect"

    if not _ensure_remote_dir(session, target_path):
        return f"错误: 无法创建远程目标目录: {target_path}"

    try:
        sftp = session._client.open_sftp()
        try:
            sftp.put(source_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return f"错误: 传输失败: {e}"

    return f"已传输: 本机:{source_path} → {target_host}:{target_path} ({_format_size(size)})"


def _transfer_remote_to_local(source_host: str, source_path: str, target_path: str, max_transfer_mb: int) -> str:
    session = get_session(host=source_host)
    if session is None:
        return f"错误: 源设备 {source_host} 未连接，请先connect"

    size = _get_remote_file_size(session, source_path)
    if size is None:
        return f"错误: 源文件不存在或无法访问: {source_host}:{source_path}"

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"错误: {err}"

    if not _ensure_local_dir(target_path):
        return f"错误: 无法创建本地目标目录: {target_path}"

    try:
        sftp = session._client.open_sftp()
        try:
            sftp.get(source_path, target_path)
        finally:
            sftp.close()
    except Exception as e:
        return f"错误: 传输失败: {e}"

    return f"已传输: {source_host}:{source_path} → 本机:{target_path} ({_format_size(size)})"


def _transfer_remote_to_remote(source_host: str, source_path: str, target_host: str, target_path: str, max_transfer_mb: int) -> str:
    src_session = get_session(host=source_host)
    if src_session is None:
        return f"错误: 源设备 {source_host} 未连接，请先connect"

    tgt_session = get_session(host=target_host)
    if tgt_session is None:
        return f"错误: 目标设备 {target_host} 未连接，请先connect"

    size = _get_remote_file_size(src_session, source_path)
    if size is None:
        return f"错误: 源文件不存在或无法访问: {source_host}:{source_path}"

    err = _check_transfer_size(size, max_transfer_mb)
    if err:
        return f"错误: {err}"

    if not _ensure_remote_dir(tgt_session, target_path):
        return f"错误: 无法创建远程目标目录: {target_path}"

    transferred = 0
    try:
        src_sftp = src_session._client.open_sftp()
        tgt_sftp = tgt_session._client.open_sftp()
        try:
            src_file = src_sftp.open(source_path, "rb")
            tgt_file = tgt_sftp.open(target_path, "wb")
            try:
                while True:
                    chunk = src_file.read(_TRANSFER_BUFFER_SIZE)
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
        return f"错误: 传输中断，已传输 {_format_size(transferred)}/{_format_size(size)}: {e}"

    return f"已传输: {source_host}:{source_path} → {target_host}:{target_path} ({_format_size(size)})"


def _transfer(source_host: str, source_path: str, target_host: str, target_path: str, _tool_context=None) -> str:
    if not source_path:
        return "错误: transfer需要提供source_path（源文件路径）"
    if not target_path:
        return "错误: transfer需要提供target_path（目标文件路径）"
    if source_host == target_host and source_path == target_path:
        return "错误: 源和目标相同，无需传输"

    max_transfer_mb = 100
    if _tool_context and hasattr(_tool_context, "max_transfer_mb"):
        max_transfer_mb = _tool_context.max_transfer_mb

    source_is_local = not source_host
    target_is_local = not target_host

    if source_is_local and target_is_local:
        return "错误: 源和目标都是本机，请使用本地文件操作工具"
    elif source_is_local:
        return _transfer_local_to_remote(source_path, target_host, target_path, max_transfer_mb)
    elif target_is_local:
        return _transfer_remote_to_local(source_host, source_path, target_path, max_transfer_mb)
    else:
        return _transfer_remote_to_remote(source_host, source_path, target_host, target_path, max_transfer_mb)
