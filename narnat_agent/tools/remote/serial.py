"""Serial 工具 —— 多会话串口终端。

核心设计（搬运自旧 `narnat_agent/tools/serial/__init__.py`，结构重组）:
- 支持最多 max_sessions 个并发串口会话，每个会话有唯一 session_id；
- `scan` 扫描本机串口，`connect` 三阶段连接（锁内占位 → 锁外 open → 锁内落库）；
- 提示符检测: 字符集匹配 + 稳定性采样；`raw_exec` 纯超时（含空命令纯监听）；
- 超时默认 120s，超时返回已收集数据且忙状态立即复位（不保留后台收敛）。

结构差异（design D7，行为不变）:
- 会话注册表/解析/status/close/cleanup/确认六组重复函数收敛到 `remote.common`
  （与 Terminal 共用，两族差异显式参数化）；
- 会话数由构造注入：**明示修复项**——配置 `工具.SSH最大会话数` 对 Serial 生效
  （旧实现硬编码 5 且 `set_max_sessions` 无调用方；默认值场景无差异）；
- `"__AWAIT_CONFIRM__"` 统一走 `contracts.tool.AWAIT_CONFIRM` 常量。

契约来源：specs/tools-remote（Serial 工具参数契约 / 扫描 / 连接与会话编号 /
命令执行与提示符检测 / 裸执行与纯监听 / 交互输入 / 输出清洗与截断 /
状态、关闭与清理 / 中断传播 / 会话上限配置接线 / 兼容性怪癖保持）。
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Optional

from ...contracts.tool import ToolDefinition, ToolEnv, ToolResult
from .common import (
    SessionSlots,
    close_sessions,
    confirm_delete_command,
    match_slots,
    resolve_auto_slot,
    resolve_explicit_slot,
)
from .serial_session import SerialSession

__all__ = ["DEFINITION", "SerialTool"]

DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "Serial",
        "description": "多设备持久串口：连接后保持，同一串口重复调用复用同一会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["scan", "connect", "exec", "raw_exec", "input", "status", "close"],
                    "description": (
                        "操作类型（默认exec）。"
                        "scan 扫描本机可用串口；"
                        "connect 打开串口连接（自动分配或指定session_id）；"
                        "exec 发送命令，等待提示符或超时返回；"
                        "raw_exec 发送命令，纯超时返回（不检测提示符，适合裸机/AT固件等无标准提示符设备）；"
                        "input 发送交互输入；"
                        "status 查看所有串口会话状态；"
                        "close 关闭指定会话（省略session_id/port时关闭全部会话）"
                    ),
                },
                "port": {
                    "type": "string",
                    "description": (
                        "串口设备名，如COM1、/dev/ttyUSB0（connect时必填；"
                        "其他action可直接填已连接的串口名指定会话）"
                    ),
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
                    "description": "自定义提示符正则（给定时替代默认字符集 $#%>:❯=@~）",
                },
                "command": {
                    "type": "string",
                    "description": (
                        "发送的命令（action=exec/raw_exec时使用）；"
                        "raw_exec时为空则纯监听：不发送，仅在timeout内收集设备主动输出"
                    ),
                },
                "input": {
                    "type": "string",
                    "description": (
                        "交互输入内容（action=input时使用，如密码、y/n确认）。"
                        "发送 ^C 可中断设备上仍在运行的命令；"
                        "其他文本追加行结束符发送，空闲时等同 exec 执行命令"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "超时秒数（正整数，默认120）。"
                        "超时返回已收集输出，设备上命令仍在运行，可用input发送^C中断"
                    ),
                },
                "session_id": {
                    "type": "integer",
                    "description": (
                        "终端ID（默认自动分配；0=第一个串口终端）。"
                        "exec/raw_exec/input 省略时自动选择唯一会话（多个会话时需指定），"
                        "close 省略时关闭全部会话"
                    ),
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


class SerialTool:
    """Serial 工具 —— 多会话串口终端的对外入口与会话注册表属主。

    - `execute(args, env)`：LLM 工具调用入口（7 种 action，见 `DEFINITION`）；
    - `kill_active_exec()`：ESC 中断订阅点（覆盖所有活跃执行中的会话）；
    - `cleanup()`：程序退出清理。
    """

    # connect 默认波特率与 exec 默认超时（DEFINITION 文案同源，保持单一默认）
    DEFAULT_BAUDRATE = 115200
    DEFAULT_TIMEOUT = 120
    DEFAULT_MAX_OUTPUT_CHARS = 8000
    # "\\n" 等转义字符串的行结束符还原表
    ESCAPED_LINE_ENDINGS = {"\\n": "\n", "\\r\\n": "\r\n", "\\r": "\r"}

    def __init__(self, max_sessions: int = 5) -> None:
        """构造：`max_sessions` 由装配点按配置 `工具.SSH最大会话数` 注入（内部钳制 1-10）。

        明示修复项：旧实现 `SerialRuntime.max_sessions` 硬编码 5（配置未接线），
        本实现与 Terminal 共用同一配置来源（默认值场景无差异）。
        """
        self._slots = SessionSlots(max_sessions)

    # ═══════════════════════════════════════════════════════════
    # Tool 协议面
    # ═══════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """工具名（LLM 可见名与注册表键）。"""
        return "Serial"

    def definition(self) -> ToolDefinition:
        """LLM 工具定义（与旧包 `Serial.DEFINITION` 逐字等价）。"""
        return DEFINITION

    def execute(self, args: dict[str, Any], env: Optional[ToolEnv] = None) -> ToolResult:
        """Serial 工具入口：参数强转 → action 分发（参数说明见 `DEFINITION`）。"""
        action = args.get("action")
        action = "exec" if action is None else action
        port = self._arg_text(args, "port")
        parity = self._arg_text(args, "parity", "N")
        flow_control = self._arg_text(args, "flow_control", "none")
        line_ending = self._arg_text(args, "line_ending", "\n")
        prompt_pattern = self._arg_text(args, "prompt_pattern")
        command = self._arg_text(args, "command")
        input_text = self._arg_text(args, "input")

        # AI可能传字符串类型的数值参数，统一转int/float（与Grep/Read容错风格一致）
        raw_baudrate = args.get("baudrate", self.DEFAULT_BAUDRATE)
        raw_databits = args.get("databits", 8)
        raw_stopbits = args.get("stopbits", 1)
        raw_timeout = args.get("timeout", self.DEFAULT_TIMEOUT)
        raw_session_id = args.get("session_id", -1)
        raw_max_output = args.get("max_output_chars", self.DEFAULT_MAX_OUTPUT_CHARS)
        try:
            baudrate = int(raw_baudrate) if raw_baudrate is not None else self.DEFAULT_BAUDRATE
            databits = int(raw_databits) if raw_databits is not None else 8
            stopbits = float(raw_stopbits) if raw_stopbits is not None else 1
            timeout = int(raw_timeout) if raw_timeout is not None else self.DEFAULT_TIMEOUT
            session_id = int(raw_session_id) if raw_session_id is not None else -1
            max_output_chars = (int(raw_max_output) if raw_max_output is not None
                                else self.DEFAULT_MAX_OUTPUT_CHARS)
        except (TypeError, ValueError):
            return ToolResult(llm_text="[错误: baudrate/databits/stopbits/timeout/session_id/max_output_chars需为数值]")

        if action == "scan":
            return ToolResult(llm_text=self._scan())
        if action == "connect":
            return ToolResult(llm_text=self._connect(
                port, baudrate, databits, parity, stopbits, flow_control,
                line_ending, prompt_pattern, session_id))
        if action == "exec":
            return ToolResult(llm_text=self._exec(
                session_id, port, command, timeout, max_output_chars, env))
        if action == "raw_exec":
            return ToolResult(llm_text=self._raw_exec(
                session_id, port, command, timeout, max_output_chars, env))
        if action == "input":
            return ToolResult(llm_text=self._input(
                session_id, port, input_text, timeout, max_output_chars, env))
        if action == "status":
            return ToolResult(llm_text=self._status())
        if action == "close":
            return ToolResult(llm_text=self._close(session_id, port))
        return ToolResult(llm_text=f"[错误: 未知action '{action}'，可选: scan/connect/exec/raw_exec/input/status/close]")

    # ═══════════════════════════════════════════════════════════
    # ESC 中断与清理
    # ═══════════════════════════════════════════════════════════

    def kill_active_exec(self) -> None:
        """ESC 打断：设置中断标志让所有活跃读取线程退出（集合语义，覆盖多会话）。"""
        for sid in self._slots.active_sids():
            session = self._slots.get(sid)
            if session is not None:
                session.kill_active()

    def cleanup(self) -> None:
        """程序退出时清理所有串口会话（关全部 + 清注册表与活跃集合）。"""
        self._slots.cleanup()

    # ═══════════════════════════════════════════════════════════
    # action 实现
    # ═══════════════════════════════════════════════════════════

    def _scan(self) -> str:
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
        for p in ports:
            desc = p.description or "(无描述)"
            hwid = p.hwid or ""
            info = f"  {p.device}"
            if desc != "n/a" and desc != p.device:
                info += f"  — {desc}"
            if hwid and hwid != "n/a":
                info += f"  [{hwid}]"
            lines.append(info)
        return "\n".join(lines)

    def _connect(self, port: str, baudrate: int = 115200, databits: int = 8,
                 parity: str = "N", stopbits: float = 1, flow_control: str = "none",
                 line_ending: str = "\n", prompt_pattern: str = "",
                 session_id: int = -1) -> str:
        """打开串口连接（三阶段：锁内占位 → 锁外 open → 锁内落库）"""
        if not port:
            return "[错误: connect 需要提供 port（串口设备名）]"

        # 规范化 line_ending（LLM 可能传 "\\n" 转义字符串）；非法值回落 \n
        le = self.ESCAPED_LINE_ENDINGS.get(line_ending, line_ending)
        if le not in ("\n", "\r\n", "\r"):
            le = "\n"

        # 端口名归一（Windows 大小写不敏感）
        port_key = self._port_key(port)

        # ── 阶段1: 锁内分配 slot（避免 TOCTOU）──
        slots = self._slots
        with slots.lock:
            entries = slots.entries_unlocked()
            # 同端口防重复：串口被独占，同一端口不能开两个会话
            for sid, s in entries.items():
                if s is None:
                    continue
                if self._port_key(s.port) == port_key and s.is_alive:
                    return f"[错误: {port} 已被终端{sid}占用，请先 close 终端{sid}]"

            if session_id >= 0:
                if session_id >= slots.max_sessions:
                    return f"[错误: session_id 范围 0-{slots.max_sessions - 1}]"
                if session_id in entries:
                    old = entries[session_id]
                    if old is not None and old.is_alive:
                        return f"[串口终端{session_id}已连接: {old.prompt_info}]"
                    if old is not None:
                        old.close()
                    del entries[session_id]
                alloc_id = session_id
            else:
                alloc_id = slots.allocate_locked(lambda s: not s.is_alive)
                if alloc_id < 0:
                    active = list(entries.keys())
                    return f"[错误: 已达最大会话数({slots.max_sessions})，当前终端: {active}，请先 close 释放]"

            # 预留 slot（置 None），防止锁外构造期间其他线程抢占同一 alloc_id
            entries[alloc_id] = None

        # ── 阶段2: 锁外构造 SerialSession（串口 open 可能阻塞，不持锁）──
        try:
            session = SerialSession(
                port=port, baudrate=baudrate, databits=databits,
                parity=parity, stopbits=stopbits, flow_control=flow_control,
                line_ending=le, prompt_pattern=prompt_pattern,
            )
        except Exception as e:
            # 构造失败 → 释放预留 slot
            slots.drop_reserved(alloc_id)
            return f"[错误: 无法打开串口 {port}: {e}]"

        # ── 阶段3: 锁内存储正式 session ──
        slots.put(alloc_id, session)

        parts = [f"[已连接终端{alloc_id}: {session.prompt_info}]"]
        if session.initial_output:
            parts.append(session.initial_output)
        return "\n".join(parts)

    def _check_delete_safety(self, command: str, session_id: int, port: str, timeout: int,
                             max_output_chars: int, action_name: str,
                             env: Optional[ToolEnv]) -> Optional[str]:
        """删除命令安全确认。返回 None 表示放行，返回 str 表示被拦截的提示。

        Serial 侧无 git 确认项（与 Terminal 的差异经 `extra_pattern` 参数化）。
        """
        return confirm_delete_command(
            command, env, tool_name=self.name,
            arguments={
                "action": action_name,
                "session_id": session_id,
                "port": port,
                "command": command,
                "timeout": timeout,
                "max_output_chars": max_output_chars,
            },
        )

    def _exec(self, session_id: int, port: str, command: str, timeout: int = 120,
              max_output_chars: int = 8000, env: Optional[ToolEnv] = None) -> str:
        """在指定会话中发送命令"""
        if not command:
            return "[错误: exec 需要提供 command]"
        if timeout <= 0:
            return "[错误: timeout 需为正整数（秒）]"

        settings = env.settings if env is not None else None
        if settings is not None and settings.max_timeout_seconds > 0:
            timeout = min(timeout, settings.max_timeout_seconds)

        blocked = self._check_delete_safety(command, session_id, port, timeout,
                                            max_output_chars, "exec", env)
        if blocked is not None:
            return blocked

        def sender(session: SerialSession) -> str:
            return session.execute(command, timeout=timeout, max_output_chars=max_output_chars)

        return self._run_on_session(session_id, port, sender, "命令执行失败")

    def _raw_exec(self, session_id: int, port: str, command: str, timeout: int = 120,
                  max_output_chars: int = 8000, env: Optional[ToolEnv] = None) -> str:
        """在指定会话中发送命令，纯超时返回，不检测提示符。

        适用场景:
        - 设备无标准提示符（裸机串口、AT 固件、bootloader 启动日志）
        - 输出中含大量提示符字符导致 exec 误判
        - command 为空 → 纯监听模式：不发送任何内容，仅在 timeout 内收集
          设备主动输出（boot日志、登录提示、刷屏日志等）
        """
        if timeout <= 0:
            return "[错误: timeout 需为正整数（秒）]"

        settings = env.settings if env is not None else None
        if settings is not None and settings.max_timeout_seconds > 0:
            timeout = min(timeout, settings.max_timeout_seconds)

        # 安全检查（与 _exec 保持一致）：空命令（纯监听）无内容可查，直接跳过
        if command:
            blocked = self._check_delete_safety(command, session_id, port, timeout,
                                                max_output_chars, "raw_exec", env)
            if blocked is not None:
                return blocked

        def sender(session: SerialSession) -> str:
            return session.raw_execute(command, timeout=timeout, max_output_chars=max_output_chars)

        return self._run_on_session(session_id, port, sender, "命令执行失败")

    def _input(self, session_id: int, port: str, text: str, timeout: int = 120,
               max_output_chars: int = 8000, env: Optional[ToolEnv] = None) -> str:
        """向串口发送交互输入（密码、y/n 确认、^C 中断等）"""
        if not text:
            return "[错误: input 需要提供 input 内容]"
        if timeout <= 0:
            return "[错误: timeout 需为正整数（秒）]"

        settings = env.settings if env is not None else None
        if settings is not None and settings.max_timeout_seconds > 0:
            timeout = min(timeout, settings.max_timeout_seconds)

        blocked = self._check_delete_safety(text, session_id, port, timeout,
                                           max_output_chars, "input", env)
        if blocked is not None:
            return blocked

        def sender(session: SerialSession) -> str:
            return session.send_input(text, timeout=timeout, max_output_chars=max_output_chars)

        return self._run_on_session(session_id, port, sender, "输入发送失败")

    def _run_on_session(self, session_id: int, port: str,
                        sender: Callable[[SerialSession], str],
                        failure_label: str) -> str:
        """会话解析 → 存活校验（断线回收槽位）→ 注册活跃执行 → 发送（exec/raw_exec/input 共用）"""
        try:
            sid, session = self._resolve_session_id(session_id, port)
        except ValueError as e:
            return f"[错误: {e}]"

        if not session.is_alive:
            # 串口已断开（如被拔出）：回收槽位并提示重新 connect
            self._slots.pop(sid)
            return f"[错误: 终端{sid}串口已断开，请重新 connect]"

        try:
            self._slots.add_active(sid)
            try:
                result = sender(session)
            finally:
                self._slots.discard_active(sid)
            return f"[终端{sid}] {result}"
        except Exception as e:
            return f"[错误: 终端{sid}{failure_label}: {e}]"

    def _status(self) -> str:
        """查看所有会话状态"""
        slots = self._slots
        with slots.lock:
            entries = slots.entries_unlocked()
            if not entries:
                return f"[无活跃串口会话，最多支持{slots.max_sessions}个并发终端]"

            lines = []
            for sid in sorted(entries.keys()):
                session = entries[sid]
                if session is None:
                    lines.append(f"  终端{sid}: [连接中...]")
                    continue
                alive = "活跃" if session.is_alive else "已断开"
                busy = "忙" if session.busy else "闲"
                lines.append(f"  终端{sid}: {session.prompt_info} [{alive}|{busy}]")
            free = slots.max_sessions - len(entries)
            if free > 0:
                lines.append(f"  [{free}个空闲]")
            return "[串口会话]\n" + "\n".join(lines)

    def _close(self, session_id: int, port: str = "") -> str:
        """关闭会话。session_id=-1 且无 port 关闭全部；port 可直引已连接的串口"""

        def key_flow(entries: dict) -> tuple[Optional[int], Optional[str]]:
            port_key = self._port_key(port, strip=True)
            matched = [sid for sid, s in entries.items()
                       if s is not None and self._port_key(s.port) == port_key]
            if not matched:
                return None, f"[错误: 端口 {port} 未连接]"
            return matched[0], None

        return close_sessions(
            self._slots, session_id, port,
            all_msg=lambda count: f"[已关闭{count}个串口会话]",
            sid_missing_msg=lambda sid: f"[终端{sid}未连接]",
            sid_done_msg=lambda sid: f"[已关闭终端{sid}]",
            placeholder_msg=lambda sid: f"[终端{sid}连接中，已取消]",
            key_flow=key_flow,
            clear_active=True,
        )

    # ═══════════════════════════════════════════════════════════
    # 会话解析与端口名归一
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _port_key(port: str, *, strip: bool = False) -> str:
        """端口名归一（Windows 大小写不敏感）。

        `strip=True` 用于 close/exec 的端口引用（旧实现先 strip 再比较）；
        连接路径直接使用原名（旧实现行为一致）。
        """
        text = port.strip() if strip else port
        return text.upper() if sys.platform == "win32" else text

    def _resolve_session_id(self, session_id: int, port: str = "") -> tuple[int, SerialSession]:
        """解析 session_id，返回 (sid, session) 或抛出 ValueError。

        解析顺序:
        1. session_id >= 0: 直接查找（占位槽报"正在连接中"）
        2. session_id < 0 且指定 port: 按端口名匹配（Windows 大小写不敏感）
        3. session_id < 0 且无 port: 只有一个会话时自动选择，多个时报清单
        """
        slots = self._slots
        if session_id >= 0:
            return resolve_explicit_slot(
                slots, session_id,
                missing_msg=lambda: f"终端{session_id}未连接，请先 connect",
                placeholder_msg=lambda: f"终端{session_id}正在连接中，请稍候",
            )
        if port:
            port_key = self._port_key(port, strip=True)
            matched = [sid for sid, s in match_slots(
                slots, lambda s: self._port_key(s.port) == port_key)]
            if matched:
                sid = matched[0]
                return sid, slots.get(sid)
            raise ValueError(f"端口 {port} 未连接，请先 connect 或 status 查看已连接的串口")
        return resolve_auto_slot(
            slots,
            no_session_msg="无活跃会话，请先 connect",
            multiple_msg=lambda active: (
                f"有{len(active)}个会话，请指定 session_id 或 port（串口设备名，如COM1、/dev/ttyUSB0）。\n"
                + "\n".join(f"终端{sid}: {s.prompt_info}" for sid, s in active)
            ),
        )

    @staticmethod
    def _arg_text(args: dict, key: str, default: str = "") -> str:
        """取文本参数：缺失或 None 时用默认值（空串语义与旧实现一致）。"""
        value = args.get(key, default)
        return default if value is None else value
