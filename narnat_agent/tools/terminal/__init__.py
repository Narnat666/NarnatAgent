"""
Terminal工具 ── 多终端可持续SSH

核心设计:
- 支持最多MAX_SESSIONS(5)个并发SSH会话，每个会话有唯一session_id(0-4)
- AI通过session_id指定在哪个终端操作，实现多终端并行
- 会话持久化，多次调用复用同一连接
- timeout默认120秒，超时告知AI命令仍在运行（AI可去其他终端继续工作）
"""

import re
import sys
import threading
from typing import Optional

import paramiko

from .ssh_session import SSHSession, _truncate_output

# 公开接口：供remote.py和其他模块使用
__all__ = ["execute", "DEFINITION", "get_session", "SSHSession", "kill_active_exec", "cleanup", "set_max_sessions"]


# 删除命令正则
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# git 只读子命令白名单（不在白名单中的 git 命令需确认）
# 只匹配第一个子命令词，如 git log --oneline 匹配 log
_GIT_READONLY = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "blame", "shortlog", "rev-parse", "ls-tree", "describe",
    "reflog", "name-rev", "ls-remote", "show-ref", "merge-base",
    "cherry", "var",
})

# 匹配 git 子命令的正则（只捕获第一个子命令词）
_RE_GIT_CMD = re.compile(r"\bgit\s+(\S+)", re.IGNORECASE)

MAX_SESSIONS = 5  # 默认5个并发SSH会话，可通过set_max_sessions修改


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
        "description": "Multi-terminal persistent SSH, max 5 concurrent terminals. connect to establish session (with optional or auto session_id), exec to run commands on a terminal, input to send interactive input (e.g. sudo password), enabling parallel multi-terminal control. Output truncated to 2000 chars by default, truncation notice returned when exceeded, increase max_output_chars for full output",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "exec", "input", "status", "close"],
                    "description": "Action, default exec: connect=establish SSH session, exec=execute command, input=send interactive input (e.g. sudo password), status=view sessions, close=close session",
                },
                "host": {"type": "string", "description": "Remote host IP or domain"},
                "username": {"type": "string", "description": "SSH username"},
                "port": {"type": "integer", "description": "SSH port, default 22"},
                "key_path": {"type": "string", "description": "SSH private key path, e.g. ~/.ssh/id_rsa"},
                "password": {"type": "string", "description": "SSH password. Auto-tries key-based auth from ~/.ssh/ if not provided"},
                "sudo_password": {"type": "string", "description": "Sudo password (set at connect time, auto-injected when subsequent exec encounters sudo prompt)"},
                "command": {"type": "string", "description": "Command to execute (required when action=exec)"},
                "input": {"type": "string", "description": "Interactive input (required when action=input, e.g. sudo password, y/n confirmation)"},
                "timeout": {"type": "integer", "description": "Command timeout in seconds, default 120s. Auto-returns timeout notification. Set a positive integer to customize"},
                "session_id": {"type": "integer", "description": "Terminal ID 0-4, default -1 auto-select. Specified or auto-assigned during connect, specifies which terminal on exec"},
                "max_output_chars": {"type": "integer", "description": "Max output chars, default 2000. Must be a positive integer"},
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
    _tool_context=None,
) -> str:
    """
    Terminal工具：多终端可持续SSH。

    action:
      connect  - 建立SSH会话（首次连接或重连），自动分配或使用指定session_id
      exec     - 在指定会话中执行命令
      input    - 向终端发送交互输入（如sudo密码、确认提示等）
      status   - 查看当前所有会话状态
      close    - 关闭指定会话

    session_id:
      0-4  - 指定终端编号
      -1   - 自动选择（connect时自动分配，exec时选唯一活跃会话）

    sudo_password:
      connect时设置，后续exec遇到sudo密码提示自动注入

    max_output_chars:
      返回内容最大字符数，默认2000。设为0或负数表示不限制
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
    else:
        return f"错误: 未知action '{action}'，可选: connect/exec/input/status/close"


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

    # 安全检查：删除命令和非只读git命令需用户确认
    need_confirm = False
    if _RE_DELETE.search(command):
        need_confirm = True
    else:
        m = _RE_GIT_CMD.search(command)
        if m:
            subcmd = m.group(1).lower()
            if subcmd not in _GIT_READONLY:
                need_confirm = True
            # branch/tag 虽然是只读子命令，但 -d/-D 是删除操作
            elif subcmd in ("branch", "tag") and re.search(r"\s-[dD]\b", command):
                need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            # Windows: prompt_toolkit和input()用不同的输入系统，直接用input()确认
            if _tool_context and _tool_context.confirm_callback and not _tool_context.confirm_callback(command):
                return "操作已取消: 此命令需用户确认"
        else:
            # Linux/macOS: 终端被prompt_toolkit占用，无法在子线程中读取输入
            # 用户已确认过（_delete_confirmed=True），直接执行
            if _tool_context and _tool_context._delete_confirmed:
                _tool_context._delete_confirmed = False
            else:
                # 暂存命令，返回AWAIT_CONFIRM标记，由agent主循环在#提示符下等待用户确认
                if _tool_context is not None:
                    _tool_context.pending_delete = ("Terminal", {
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
