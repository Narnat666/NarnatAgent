"""Serial 工具 —— 多会话串口终端

核心设计:
- 支持最多 MAX_SESSIONS(5) 个并发串口会话，每个会话有唯一 session_id(0-4)
- AI 通过 session_id 指定在哪个串口操作
- 提示符检测: 字符集匹配 + 稳定性采样
- 超时默认 60s，超时返回已收集数据
"""

import re
import sys
import threading
from typing import Optional

from .serial_session import SerialSession
from ..tool_context import AWAIT_CONFIRM

__all__ = ["execute", "DEFINITION", "kill_active_exec", "cleanup", "set_max_sessions"]


MAX_SESSIONS = 5

# 删除命令正则（串口设备误删更危险）
# 边界后跟空白或/：覆盖无空格变体（rd/s、del/f、rmdir/q）及erase/format；
# \b边界防止误伤 delphi、3rd、formatting 等普通词
_RE_DELETE = re.compile(
    r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)

# session_id(0-4) → SerialSession
_sessions: dict[int, "SerialSession"] = {}
_sessions_lock = threading.Lock()

# 当前正在执行命令的串口会话 ID 集合（ESC 打断时遍历）
# 使用集合而非单变量，避免多会话并发 exec 时后者覆盖前者导致 ESC 打错会话
_active_exec_sids: set[int] = set()
_active_exec_lock = threading.Lock()


def set_max_sessions(n: int) -> None:
    """设置最大串口会话数"""
    global MAX_SESSIONS
    MAX_SESSIONS = max(1, min(n, 10))


DEFINITION = {
    "type": "function",
    "function": {
        "name": "Serial",
        "description": "多终端持久串口，最多可连5个不同串口设备。scan扫描串口，connect连接设备，exec执行命令等待提示符，raw_exec纯超时返回（不检测提示符），input发送交互输入，status查看会话，close关闭会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["scan", "connect", "exec", "raw_exec", "input", "status", "close"],
                    "description": "操作类型（默认exec）",
                },
                "port": {
                    "type": "string",
                    "description": "串口设备名，如COM1、/dev/ttyUSB0（connect时必填）",
                },
                "baudrate": {
                    "type": "integer",
                    "description": "波特率，默认115200",
                },
                "databits": {
                    "type": "integer",
                    "description": "数据位5/6/7/8，默认8",
                },
                "parity": {
                    "type": "string",
                    "description": "校验位N/E/O/M/S，默认N",
                },
                "stopbits": {
                    "type": "number",
                    "description": "停止位1/1.5/2，默认1",
                },
                "flow_control": {
                    "type": "string",
                    "description": "流控none/hardware/software，默认none",
                },
                "line_ending": {
                    "type": "string",
                    "description": "行结束符\\n/\\r\\n/\\r，默认\\n",
                },
                "prompt_pattern": {
                    "type": "string",
                    "description": "自定义提示符正则（默认匹配$#%>:❯=@~，不匹配时覆盖）",
                },
                "command": {
                    "type": "string",
                    "description": "发送的命令（action=exec/raw_exec时使用）",
                },
                "input": {
                    "type": "string",
                    "description": "交互输入内容（action=input时使用，如密码、y/n确认）；发送 ^C 可中断设备上仍在运行的命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数（正整数，默认120）",
                },
                "session_id": {
                    "type": "integer",
                    "description": "终端ID 0-4（默认自动分配；0=第一个串口终端）",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "最大输出字符数（正整数，默认8000）",
                },
            },
            "required": [],
        },
    },
}


def kill_active_exec():
    """ESC 打断：设置中断标志让所有活跃读取线程退出"""
    with _active_exec_lock:
        sids = list(_active_exec_sids)
    for sid in sids:
        with _sessions_lock:
            session = _sessions.get(sid)
        if session is not None:
            session.kill_active()


# ── 公开接口 ──

def execute(
    action: str = "exec",
    port: str = "",
    baudrate: int = 115200,
    databits: int = 8,
    parity: str = "N",
    stopbits: float = 1,
    flow_control: str = "none",
    line_ending: str = "\n",
    prompt_pattern: str = "",
    command: str = "",
    input: str = "",  # 参数名 "input" 与 DEFINITION 对齐，不可改名（LLM 通过 **arguments 传参）
    timeout: int = 120,
    session_id: int = -1,
    max_output_chars: int = 8000,
    _tool_context=None,
) -> str:
    """
    Serial 工具：多会话串口终端。

    action:
      scan     - 扫描本机可用串口
      connect  - 打开串口连接，自动分配或使用指定 session_id
      exec     - 在指定会话中发送命令，等待提示符或超时返回（默认action，与Terminal一致）
      raw_exec - 在指定会话中发送命令，纯超时返回（不检测提示符，适合裸机/AT固件等无标准提示符设备）
      input    - 向串口发送交互输入（如密码、确认等）；"^C"/"\\x03" 发送原始Ctrl+C中断
      status   - 查看当前所有串口会话状态
      close    - 关闭指定会话
    """
    # AI可能传字符串类型的数值参数，统一转int/float（与Grep/Read容错风格一致）
    try:
        baudrate = int(baudrate) if baudrate is not None else 115200
        databits = int(databits) if databits is not None else 8
        stopbits = float(stopbits) if stopbits is not None else 1
        timeout = int(timeout) if timeout is not None else 120
        session_id = int(session_id) if session_id is not None else -1
        max_output_chars = int(max_output_chars) if max_output_chars is not None else 8000
    except (TypeError, ValueError):
        return "[错误: baudrate/databits/stopbits/timeout/session_id/max_output_chars需为数值]"

    if action == "scan":
        return _scan()
    elif action == "connect":
        return _connect(port, baudrate, databits, parity, stopbits, flow_control, line_ending, prompt_pattern, session_id)
    elif action == "exec":
        return _exec(session_id, port, command, timeout, max_output_chars, _tool_context)
    elif action == "raw_exec":
        return _raw_exec(session_id, port, command, timeout, max_output_chars, _tool_context)
    elif action == "input":
        return _input(session_id, port, input, timeout, max_output_chars, _tool_context)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(session_id, port)
    else:
        return f"[错误: 未知action '{action}'，可选: scan/connect/exec/raw_exec/input/status/close]"


# ── 内部实现 ──

def _scan() -> str:
    """扫描本机可用串口"""
    try:
        from serial.tools.list_ports import comports
        ports = list(comports())
    except ImportError:
        return "[错误: 无法导入 pyserial，请确认已安装]"
    except Exception as e:
        return f"[错误: 扫描串口失败: {e}]"
    if not ports:
        return "[未检测到串口设备]"

    lines = ["可用串口:"]
    for i, p in enumerate(ports):
        desc = p.description or "(无描述)"
        hwid = p.hwid or ""
        info = f"  {p.device}"
        if desc != "n/a" and desc != p.device:
            info += f"  — {desc}"
        if hwid and hwid != "n/a":
            info += f"  [{hwid}]"
        lines.append(info)
    return "\n".join(lines)


def _connect(port: str, baudrate: int = 115200, databits: int = 8,
             parity: str = "N", stopbits: float = 1, flow_control: str = "none",
             line_ending: str = "\n", prompt_pattern: str = "",
             session_id: int = -1) -> str:
    """打开串口连接"""
    if not port:
        return "[错误: connect 需要提供 port（串口设备名）]"

    # 规范化 line_ending（LLM 可能传 "\\n" 转义字符串）
    _LE_ESCAPED = {"\\n": "\n", "\\r\\n": "\r\n", "\\r": "\r"}
    le = _LE_ESCAPED.get(line_ending, line_ending)
    if le not in ("\n", "\r\n", "\r"):
        le = "\n"

    # 规范化端口名（Windows 大小写不敏感）
    port_key = port.upper() if sys.platform == "win32" else port

    # ── 阶段1: 锁内分配 slot（避免 TOCTOU）──
    with _sessions_lock:
        # 同端口防重复：串口被独占，同一端口不能开两个会话
        for sid, s in _sessions.items():
            if s is None:
                continue
            existing_port = s.port.upper() if sys.platform == "win32" else s.port
            if existing_port == port_key and s.is_alive:
                return f"[错误: {port} 已被终端{sid}占用，请先 close 终端{sid}]"


        if session_id >= 0:
            if session_id >= MAX_SESSIONS:
                return f"[错误: session_id 范围 0-{MAX_SESSIONS - 1}]"
            if session_id in _sessions:
                old = _sessions[session_id]
                if old is not None and old.is_alive:
                    return f"[串口终端{session_id}已连接: {old.prompt_info}]"
                else:
                    if old is not None:
                        old.close()
                    del _sessions[session_id]
            alloc_id = session_id
        else:
            alloc_id = _allocate_session_id()
            if alloc_id < 0:
                active = list(_sessions.keys())
                return f"[错误: 已达最大会话数({MAX_SESSIONS})，当前终端: {active}，请先 close 释放]"

        # 预留 slot（置 None），防止锁外构造期间其他线程抢占同一 alloc_id
        _sessions[alloc_id] = None

    # ── 阶段2: 锁外构造 SerialSession（串口 open 可能阻塞，不持锁）──
    try:
        session = SerialSession(
            port=port, baudrate=baudrate, databits=databits,
            parity=parity, stopbits=stopbits, flow_control=flow_control,
            line_ending=le, prompt_pattern=prompt_pattern,
        )
    except Exception as e:
        # 构造失败 → 释放预留 slot
        with _sessions_lock:
            if _sessions.get(alloc_id) is None:
                del _sessions[alloc_id]
        return f"[错误: 无法打开串口 {port}: {e}]"

    # ── 阶段3: 锁内存储正式 session ──
    with _sessions_lock:
        _sessions[alloc_id] = session

    parts = [f"[已连接终端{alloc_id}: {session.prompt_info}]"]
    if session.initial_output:
        parts.append(session.initial_output)
    return "\n".join(parts)


def _check_delete_safety(command: str, session_id: int, port: str, timeout: int,
                         max_output_chars: int, action_name: str,
                         _tool_context) -> Optional[str]:
    """删除命令安全确认。返回 None 表示放行，返回 str 表示被拦截的提示。"""
    if not (_tool_context and not _tool_context.rm_skip_confirm and _RE_DELETE.search(command)):
        return None

    if sys.platform == "win32":
        if _tool_context.confirm_callback and not _tool_context.confirm_callback(command):
            return "[操作已取消: 此命令需用户确认]"
        return None

    # Linux/macOS: 终端被 prompt_toolkit 占用，无法在子线程读取输入
    if _tool_context._delete_confirmed:
        _tool_context._delete_confirmed = False
        return None

    _tool_context.pending_delete = ("Serial", {
        "action": action_name,
        "session_id": session_id,
        "port": port,
        "command": command,
        "timeout": timeout,
        "max_output_chars": max_output_chars,
    })
    return AWAIT_CONFIRM


def _exec(session_id: int, port: str, command: str, timeout: int = 120,
          max_output_chars: int = 8000, _tool_context=None) -> str:
    """在指定会话中发送命令"""
    if not command:
        return "[错误: exec 需要提供 command]"
    if timeout <= 0:
        return "[错误: timeout 需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    blocked = _check_delete_safety(command, session_id, port, timeout,
                                   max_output_chars, "exec", _tool_context)
    if blocked is not None:
        return blocked

    try:
        sid, session = _resolve_session_id(session_id, port)
    except ValueError as e:
        return f"[错误: {e}]"

    if not session.is_alive:
        with _sessions_lock:
            _sessions.pop(sid, None)
        return f"[错误: 终端{sid}串口已断开，请重新 connect]"

    try:
        with _active_exec_lock:
            _active_exec_sids.add(sid)
        try:
            result = session.execute(command, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with _active_exec_lock:
                _active_exec_sids.discard(sid)
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"[错误: 终端{sid}命令执行失败: {e}]"


def _raw_exec(session_id: int, port: str, command: str, timeout: int = 120,
              max_output_chars: int = 8000, _tool_context=None) -> str:
    """在指定会话中发送命令，纯超时返回，不检测提示符。

    适用场景:
    - 设备无标准提示符（裸机串口、AT 固件、bootloader 启动日志）
    - 输出中含大量提示符字符导致 exec 误判
    """
    if not command:
        return "[错误: raw_exec 需要提供 command]"
    if timeout <= 0:
        return "[错误: timeout 需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    # 安全检查（与 _exec 保持一致）
    blocked = _check_delete_safety(command, session_id, port, timeout,
                                   max_output_chars, "raw_exec", _tool_context)
    if blocked is not None:
        return blocked

    try:
        sid, session = _resolve_session_id(session_id, port)
    except ValueError as e:
        return f"[错误: {e}]"

    if not session.is_alive:
        with _sessions_lock:
            _sessions.pop(sid, None)
        return f"[错误: 终端{sid}串口已断开，请重新 connect]"

    try:
        with _active_exec_lock:
            _active_exec_sids.add(sid)
        try:
            result = session.raw_execute(command, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with _active_exec_lock:
                _active_exec_sids.discard(sid)
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"[错误: 终端{sid}命令执行失败: {e}]"


def _input(session_id: int, port: str, text: str, timeout: int = 120,
           max_output_chars: int = 8000, _tool_context=None) -> str:
    """向串口发送交互输入（密码、y/n 确认、^C 中断等）"""
    if not text:
        return "[错误: input 需要提供 input 内容]"
    if timeout <= 0:
        return "[错误: timeout 需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    blocked = _check_delete_safety(text, session_id, port, timeout,
                                   max_output_chars, "input", _tool_context)
    if blocked is not None:
        return blocked

    try:
        sid, session = _resolve_session_id(session_id, port)
    except ValueError as e:
        return f"[错误: {e}]"

    if not session.is_alive:
        with _sessions_lock:
            _sessions.pop(sid, None)
        return f"[错误: 终端{sid}串口已断开，请重新 connect]"

    try:
        with _active_exec_lock:
            _active_exec_sids.add(sid)
        try:
            result = session.send_input(text, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with _active_exec_lock:
                _active_exec_sids.discard(sid)
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"[错误: 终端{sid}输入发送失败: {e}]"


def _status() -> str:
    """查看所有会话状态"""
    with _sessions_lock:
        if not _sessions:
            return f"[无活跃串口会话，最多支持{MAX_SESSIONS}个并发终端]"

        lines = []
        for sid in sorted(_sessions.keys()):
            session = _sessions[sid]
            if session is None:
                lines.append(f"  终端{sid}: [连接中...]")
                continue
            alive = "活跃" if session.is_alive else "已断开"
            busy = "忙" if session.busy else "闲"
            lines.append(f"  终端{sid}: {session.prompt_info} [{alive}|{busy}]")
        free = MAX_SESSIONS - len(_sessions)
        if free > 0:
            lines.append(f"  [{free}个空闲]")
        return "[串口会话]\n" + "\n".join(lines)


def _close(session_id: int, port: str = "") -> str:
    """关闭会话。session_id=-1 且无 port 关闭全部；port 可直引已连接的串口"""
    with _sessions_lock:
        if session_id < 0 and not port:
            count = len(_sessions)
            for sid, session in list(_sessions.items()):
                if session is not None:
                    session.close()
            _sessions.clear()
            with _active_exec_lock:
                _active_exec_sids.clear()
            return f"[已关闭{count}个串口会话]"

        if session_id < 0 and port:
            # 按端口名匹配（Windows 大小写不敏感）
            port_key = port.strip().upper() if sys.platform == "win32" else port.strip()
            matched = [
                sid for sid, s in _sessions.items()
                if s is not None and (
                    (s.port.upper() if sys.platform == "win32" else s.port) == port_key
                )
            ]
            if not matched:
                return f"[错误: 端口 {port} 未连接]"
            session_id = matched[0]

        if session_id not in _sessions:
            return f"[终端{session_id}未连接]"

        session = _sessions[session_id]
        if session is None:
            del _sessions[session_id]
            return f"[终端{session_id}连接中，已取消]"

        session.close()
        del _sessions[session_id]
        with _active_exec_lock:
            _active_exec_sids.discard(session_id)
        return f"[已关闭终端{session_id}]"


def cleanup():
    """程序退出时清理所有串口会话"""
    with _active_exec_lock:
        _active_exec_sids.clear()
    with _sessions_lock:
        for session in _sessions.values():
            if session is not None:
                session.close()
        _sessions.clear()


def _allocate_session_id() -> int:
    """分配空闲 session_id，返回 -1 表示已满。

    死会话（is_alive=False，如串口被拔出）自动回收槽位：
    否则设备断开后槽位被死会话占用，AI重连时报"已达最大会话数"却无法释放。
    调用者需持有_sessions_lock。
    """
    for i in range(MAX_SESSIONS):
        if i not in _sessions:
            return i
        session = _sessions[i]
        # None=正在连接中的占位槽，不可抢占；只回收已断开的死会话
        if session is not None and not session.is_alive:
            session.close()
            del _sessions[i]
            return i
    return -1


def _resolve_session_id(session_id: int, port: str = "") -> tuple[int, "SerialSession"]:
    """解析 session_id，返回 (sid, session) 或抛出 ValueError。

    解析顺序（与 Terminal 的 _resolve_session_id 宽松引用对齐）:
    1. session_id >= 0: 直接查找
    2. session_id < 0 且指定 port: 按端口名匹配（Windows 大小写不敏感），
       减少 AI 回查终端编号的往返（连的哪个口它最清楚）
    3. session_id < 0 且无 port: 只有一个会话时自动选择，多个时报清单
    """
    with _sessions_lock:
        if session_id >= 0:
            if session_id not in _sessions:
                raise ValueError(f"终端{session_id}未连接，请先 connect")
            session = _sessions[session_id]
            if session is None:
                raise ValueError(f"终端{session_id}正在连接中，请稍候")
            return session_id, session

        if port:
            port_key = port.strip().upper() if sys.platform == "win32" else port.strip()
            matched = [
                sid for sid, s in _sessions.items()
                if s is not None and (
                    (s.port.upper() if sys.platform == "win32" else s.port) == port_key
                )
            ]
            if matched:
                sid = matched[0]
                return sid, _sessions[sid]
            raise ValueError(f"端口 {port} 未连接，请先 connect 或 status 查看已连接的串口")

        # 自动选择：跳过 None 预留 slot
        active = {k: v for k, v in _sessions.items() if v is not None}
        if len(active) == 1:
            sid = list(active.keys())[0]
            return sid, active[sid]
        elif len(active) == 0:
            raise ValueError("无活跃会话，请先 connect")
        else:
            summaries = [f"终端{k}: {v.prompt_info}" for k, v in sorted(active.items())]
            raise ValueError(
                f"有{len(active)}个会话，请指定 session_id 或 port（如 port=COM3）。\n"
                + "\n".join(summaries)
            )
