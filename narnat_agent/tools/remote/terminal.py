"""Terminal 工具 —— 多设备持久 SSH：连接管理、命令执行、交互输入、文件传输。

核心设计（搬运自旧 `narnat_agent/tools/terminal/__init__.py`，结构重组）:
- 支持最多 max_sessions 个并发 SSH 会话，内部用 session_id(0..N-1) 标识；
- AI 通过 dev 编号引用设备: dev0=本机，dev1..devn=被控设备（终端 N-1）；
- 会话持久化，多次调用复用同一连接；
- timeout 默认 120 秒，超时告知 AI 命令仍在运行（AI 可去其他终端继续工作）；
- transfer: 在任意设备间传输文件（本机↔设备、设备↔设备），远程间流式中转不落盘。

结构差异（design D7，行为不变）:
- 会话注册表/解析/status/close/cleanup/确认六组重复函数收敛到 `remote.common`
  （与 Serial 共用，两族差异显式参数化）；
- 跨层直取 `SSHSession._channel/_client` 改为会话公开方法
  （`is_closed()` / `send_interrupt()` / `open_sftp()` / `initialize()`）；
- 会话数由构造注入（取代 `set_max_sessions` 类属性全局设置）；
- `"__AWAIT_CONFIRM__"` 字面量统一用 `contracts.tool.AWAIT_CONFIRM` 常量。

契约来源：specs/tools-remote（Terminal 工具参数契约 / SSH 连接建立与认证 /
会话编号与容量 / 状态查询与关闭 / 命令执行与完成检测 / 输出清洗与截断 /
超时与后台收敛 / 交互输入语义 / sudo 密码自动注入 / 断线自动重连 / 文件传输 /
设备引用约定 / 删除命令安全确认 / 兼容性怪癖保持）。
"""
from __future__ import annotations

import os
import re
import socket
from typing import Any, Optional

import paramiko

from ...contracts.tool import ToolDefinition, ToolEnv, ToolResult
from ..signal import error_line
from .common import (
    SessionSlots,
    close_sessions,
    confirm_delete_command,
    match_slots,
    resolve_auto_slot,
    resolve_explicit_slot,
)
from .ssh_session import SSHSession
from .transfer import FileTransfer

__all__ = ["DEFINITION", "TerminalTool", "local_host"]

DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "Terminal",
        "description": (
            "多设备持久SSH：远程执行命令、设备间传输文件。"
            "连接后保持，同一设备重复调用复用同一连接。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["connect", "exec", "input", "status", "close", "transfer"],
                    "description": (
                        "操作类型（默认exec）。"
                        "connect 建立SSH会话（成功后返回该设备的dev编号）；"
                        "exec 在指定设备执行命令；"
                        "input 向设备发送交互输入；"
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
                        "exec/input/close 填dev编号：exec/input 省略时自动选择唯一会话（多个会话时需指定），"
                        "close 省略时关闭全部会话"
                    ),
                },
                "username": {"type": "string", "description": "SSH用户名（connect时使用）"},
                "port": {"type": "integer", "description": "SSH端口（默认22）"},
                "password": {
                    "type": "string",
                    "description": (
                        "认证凭据（connect时使用）：登录密码或私钥路径（如~/.ssh/id_rsa），"
                        "不填则自动尝试默认密钥。填密码时sudo密码自动用同登录密码注入"
                    ),
                },
                "command": {"type": "string", "description": "执行的命令（action=exec时使用）"},
                "input": {
                    "type": "string",
                    "description": (
                        "交互输入内容（action=input时使用，如sudo密码、y/n确认）。"
                        "仅当有命令在等待输入时有效（通常是上个命令超时仍在后台运行）；"
                        "发送 ^C 可中断仍在运行的命令；空闲终端发送会被拒绝"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "命令超时秒数（正整数）。exec/input默认120，超时后命令继续后台运行，"
                        "可用input应答其交互提示或^C中断；connect默认15，连不上时快速报错"
                    ),
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "最大输出字符数（正整数，默认8000，超出截断并提示）",
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


def local_host() -> str:
    """本机显示名：优先取主机名，失败回退 localhost。"""
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


class TerminalTool:
    """Terminal 工具 —— 多设备持久 SSH 会话的对外入口与会话注册表属主。

    - `execute(args, env)`：LLM 工具调用入口（action 分发见 `DEFINITION`）；
    - `session_for(host)` / `devices_summary()`：供文件工具与文件传输复用的端口；
    - `kill_active_exec()`：ESC 中断订阅点（app 装配时接入中断总线）；
    - `cleanup()`：程序退出清理；
    - `resolve_dev_display(dev)` / `normalize_device_for_tools(device)`：
      UI 摘要与文件工具设备标识的归一化面。
    """

    # dev编号正则: dev0=本机(当前设备), devN(N>=1)=第N台被控设备(终端N-1)
    RE_DEV = re.compile(r"^dev(\d+)$", re.IGNORECASE)

    # 匹配 git 命令的简单正则（出现 git 即命中；Serial 侧无此项）
    RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

    # connect 默认超时 15 秒（连错IP/黑洞IP时快速失败）；exec/input 默认 120 秒
    CONNECT_TIMEOUT = 15
    COMMAND_TIMEOUT = 120
    DEFAULT_MAX_OUTPUT_CHARS = 8000
    DEFAULT_MAX_TRANSFER_MB = 100

    def __init__(self, max_sessions: int = 5) -> None:
        """构造：`max_sessions` 由装配点按配置 `工具.SSH最大会话数` 注入（内部钳制 1-10）。"""
        self._slots = SessionSlots(max_sessions)
        self._transfer = FileTransfer(self.session_for, self.devices_summary)

    # ═══════════════════════════════════════════════════════════
    # Tool 协议面
    # ═══════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """工具名（LLM 可见名与注册表键）。"""
        return "Terminal"

    def definition(self) -> ToolDefinition:
        """LLM 工具定义（与旧包 `Terminal.DEFINITION` 逐字等价）。"""
        return DEFINITION

    def execute(self, args: dict[str, Any], env: Optional[ToolEnv] = None) -> ToolResult:
        """Terminal 工具入口：参数强转 → action 分发（参数说明见 `DEFINITION`）。

        `session_id` / `key_path` / `sudo_password` 为内部参数：可被接受但不在
        `DEFINITION` 中列出（设备引用一律走 `host`）。
        """
        action = args.get("action")
        action = "exec" if action is None else action
        host = self._arg_text(args, "host")
        username = self._arg_text(args, "username")
        key_path = self._arg_text(args, "key_path")
        password = self._arg_text(args, "password")
        sudo_password = self._arg_text(args, "sudo_password")
        command = self._arg_text(args, "command")
        input_text = self._arg_text(args, "input")
        source_host = self._arg_text(args, "source_host")
        source_path = self._arg_text(args, "source_path")
        target_host = self._arg_text(args, "target_host")
        target_path = self._arg_text(args, "target_path")

        # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
        raw_port = args.get("port", 22)
        raw_timeout = args.get("timeout")
        raw_session_id = args.get("session_id", -1)
        raw_max_output = args.get("max_output_chars", self.DEFAULT_MAX_OUTPUT_CHARS)
        try:
            port = int(raw_port) if raw_port is not None else 22
            timeout = int(raw_timeout) if raw_timeout is not None else None
            session_id = int(raw_session_id) if raw_session_id is not None else -1
            max_output_chars = (int(raw_max_output) if raw_max_output is not None
                                else self.DEFAULT_MAX_OUTPUT_CHARS)
        except (TypeError, ValueError):
            return ToolResult(llm_text=error_line("port/timeout/session_id/max_output_chars需为整数"))

        settings = env.settings if env is not None else None

        if action == "connect":
            # connect默认超时15秒（exec/input默认120秒）：连错IP/黑洞IP时快速失败，
            # 避免OS默认TCP重试阻塞数十秒。同时受全局超时上限约束
            connect_timeout = self.CONNECT_TIMEOUT if timeout is None else timeout
            if settings is not None and settings.max_timeout_seconds > 0:
                connect_timeout = min(connect_timeout, settings.max_timeout_seconds)
            return ToolResult(llm_text=self._connect(
                host, username, port, key_path, password, sudo_password,
                session_id, connect_timeout))
        if action == "exec":
            exec_timeout = self.COMMAND_TIMEOUT if timeout is None else timeout
            return ToolResult(llm_text=self._exec(
                session_id, host, command, exec_timeout, max_output_chars, env))
        if action == "input":
            input_timeout = self.COMMAND_TIMEOUT if timeout is None else timeout
            return ToolResult(llm_text=self._input(
                session_id, host, input_text, input_timeout, max_output_chars, env))
        if action == "status":
            return ToolResult(llm_text=self._status())
        if action == "close":
            return ToolResult(llm_text=self._close(session_id, host))
        if action == "transfer":
            return ToolResult(llm_text=self._transfer_files(
                source_host, source_path, target_host, target_path, env))
        return ToolResult(llm_text=error_line(
            f"未知action '{action}'，可选: connect/exec/input/status/close/transfer"))

    # ═══════════════════════════════════════════════════════════
    # 设备引用（devN）与清单
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _dev_label(sid: int) -> str:
        """终端session_id → dev编号标签: 终端0=dev1, 终端1=dev2 ..."""
        return f"dev{sid + 1}(终端{sid})"

    @staticmethod
    def _devices_summary_of(entries: dict) -> str:
        """已连接设备清单（形如 `dev1(user@host)`，多个以 `、` 连接，无设备为 `(无)`）。"""
        devs = [f"dev{sid + 1}({s.username}@{s.host})"
                for sid, s in sorted(entries.items()) if s is not None]
        return "、".join(devs) if devs else "(无)"

    def devices_summary(self) -> str:
        """当前已连接设备清单（供错误文案与文件工具提示）。"""
        return self._devices_summary_of(self._slots.entries())

    def _dev_hint(self) -> str:
        """设备标识错误时的统一指导：说明 devN 用法 + 列出当前可用设备。"""
        return self._dev_hint_of(self._slots.entries())

    def _dev_hint_of(self, entries: dict) -> str:
        base = "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备；本机执行命令请用Shell工具)"
        devs = self._devices_summary_of(entries)
        if devs == "(无)":
            return f"{base}。当前无已连接设备，请先connect"
        return f"{base}。当前已连接: {devs}"

    @staticmethod
    def _normalize_device(host: str) -> str:
        """设备标识归一化: 空/dev0 → 空字符串(本机)；devN(N>=1) → 原样保留；其余原样交由校验报错"""
        if not host:
            return ""
        h = host.strip()
        m = TerminalTool.RE_DEV.match(h)
        return "" if m and int(m.group(1)) == 0 else h

    def normalize_device_for_tools(self, device: str) -> Optional[str]:
        """文件工具(Read/Edit/Write)的设备标识归一化: 合法返回规范值(本机为""), 非法返回None"""
        if not device:
            return ""
        h = device.strip()
        m = self.RE_DEV.match(h)
        if not m:
            return None
        return "" if int(m.group(1)) == 0 else h

    def file_tool_device_hint(self) -> str:
        """文件工具(Read/Edit/Write)设备标识错误时的统一指导：
        附当前已连接设备清单，减少AI回查status的往返"""
        base = "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)"
        devs = self.devices_summary()
        if devs == "(无)":
            return f"{base}。当前无已连接设备，请先Terminal connect"
        return f"{base}。当前已连接: {devs}"

    def device_error(self, host: str) -> Optional[str]:
        """校验设备标识是否合法devN（dev0~devN），非法返回错误信息，合法返回None"""
        if not host:
            return None
        if self.RE_DEV.match(host.strip()):
            return None
        return error_line(self._dev_hint())

    def resolve_dev_display(self, dev: str) -> str:
        """把设备引用翻译成UI显示名（供终端摘要使用）：
        - 空/dev0/本机 → 本机host（主机名）
        - devN已连接 → "host"（IP）
        - devN未连接 → 原样devN
        - 其他 → 原样
        """
        if not dev:
            return local_host()
        h = dev.strip()
        m = self.RE_DEV.match(h)
        if not m:
            return h
        d = int(m.group(1))
        if d == 0:
            return local_host()
        session = self._slots.get(d - 1)
        if session is not None and not session.is_closed():
            return session.host
        return h

    # ═══════════════════════════════════════════════════════════
    # 会话解析与端口
    # ═══════════════════════════════════════════════════════════

    def _resolve_session_id(self, session_id: int, host: str = "") -> tuple[int, SSHSession]:
        """解析session_id，返回 (session_id, session) 或抛出ValueError

        逻辑:
        1. session_id >= 0: 直接查找
        2. session_id == -1 且指定host: 按host匹配（devN，或IP/用户名@IP宽松引用）
        3. session_id == -1 且无host: 只有一个会话时自动选择
        """
        slots = self._slots
        if session_id >= 0:
            return resolve_explicit_slot(
                slots, session_id,
                missing_msg=lambda: (f"{self._dev_label(session_id)}未连接，请先connect。"
                                     f"当前已连接: {self.devices_summary()}"),
            )
        if host:
            h = host.strip()
            m = self.RE_DEV.match(h)
            if not m:
                # 宽松匹配: 允许直接用IP或用户名@IP引用已连接设备，
                # 避免AI回查dev编号（如 exec host=192.168.1.213）
                matched = match_slots(
                    slots, lambda s: s.host == h or f"{s.username}@{s.host}" == h)
                if len(matched) == 1:
                    return matched[0]
                if len(matched) > 1:
                    raise ValueError(
                        f"多个会话匹配 '{h}'，请用dev编号区分: "
                        + "、".join(f"dev{sid + 1}({s.username}@{s.host})"
                                    for sid, s in matched)
                    )
                raise ValueError(self._dev_hint())
            d = int(m.group(1))
            if d == 0:
                raise ValueError("dev0是本机，无SSH会话。本机执行命令请用 Shell 工具（Terminal 的 exec 用于SSH远程设备，dev0仅用于transfer）")
            sid = d - 1
            if sid >= slots.max_sessions or not slots.has(sid):
                raise ValueError(f"dev{d}未连接，请先connect。当前已连接: {self.devices_summary()}")
            return sid, slots.get(sid)
        return resolve_auto_slot(
            slots,
            no_session_msg="无活跃会话，请先connect",
            multiple_msg=lambda active: (
                "有多个会话，请指定host=dev编号，当前已连接: "
                + "、".join(f"dev{sid + 1}({s.username}@{s.host})" for sid, s in active)
            ),
        )

    def session_for(self, host: str = "", session_id: int = -1) -> Optional[SSHSession]:
        """取指定设备的 SSH 会话（供SFTP等内部使用）。断线时自动重连，失败返回None。"""
        try:
            _, session = self._resolve_session_id(session_id, host)
        except ValueError:
            return None
        if not self._try_reconnect(session):
            return None
        return session

    # ═══════════════════════════════════════════════════════════
    # ESC 中断与清理
    # ═══════════════════════════════════════════════════════════

    def kill_active_exec(self) -> None:
        """ESC打断：发Ctrl+C终止远程进程，设中断标志让本地读取线程退出。

        同时覆盖后台运行中的命令（超时后仍在运行的busy会话），
        使ESC能自愈busy状态：watcher检测到中断标志后退出并清除busy。
        兼容怪癖：活跃执行会话为单一引用（多会话并发执行时后者覆盖前者，
        ESC 仅作用于最近发起者；Serial 已用集合语义，无此覆盖）。
        """
        session = self._slots.focus()
        if session is not None:
            session.send_interrupt()

        # 后台运行中的busy会话（无活跃exec时也打断）
        for _, busy in self._slots.live_pairs(lambda s: s.busy):
            if busy is not session:
                busy.send_interrupt()

    def cleanup(self) -> None:
        """程序退出时清理所有会话。channel立即关闭，transport后台回收。"""
        self._slots.cleanup()

    def _try_reconnect(self, session: Optional[SSHSession]) -> bool:
        """断线会话尝试重连（复用原凭据，保留dev编号与cwd）。失败返回False。

        重连是阻塞操作（TCP连接最长等满 connect timeout）：注册为活跃会话后，
        ESC(kill_active_exec) 能置位中断标志，重连会尽快放弃而不是卡满超时。
        """
        if session is None or not session.is_closed():
            return session is not None
        self._slots.set_focus(session)
        try:
            session.reconnect()
            return True
        except Exception:
            return False
        finally:
            self._slots.clear_focus()

    # ═══════════════════════════════════════════════════════════
    # action 实现
    # ═══════════════════════════════════════════════════════════

    def _connect(self, host: str, username: str, port: int = 22,
                 key_path: str = "", password: str = "",
                 sudo_password: str = "",
                 session_id: int = -1,
                 timeout: int = 15) -> str:
        """建立SSH会话。timeout为连接超时（默认15秒），黑洞IP快速失败。"""
        if not host or not username:
            return error_line("connect需要提供host和username")

        # timeout非正整数时兜底默认15秒（LLM可能传0/负数）
        if not timeout or timeout <= 0:
            timeout = self.CONNECT_TIMEOUT

        slots = self._slots
        with slots.lock:
            entries = slots.entries_unlocked()
            # 指定了session_id
            if session_id >= 0:
                if session_id >= slots.max_sessions:
                    return error_line(f"session_id范围0-{slots.max_sessions - 1}")
                if session_id in entries:
                    session = entries[session_id]
                    if session is not None and not session.is_closed():
                        return (f"[{self._dev_label(session_id)}已连接: "
                                f"{session.username}@{session.host}]\n{session.prompt}")
                    if session is not None:
                        session.close()
                    del entries[session_id]
                alloc_id = session_id
            else:
                # 自动分配（锁内回收死会话；槽位用尽时报错并提示释放）
                alloc_id = slots.allocate_locked(lambda s: s.is_closed())
                if alloc_id < 0:
                    active = [self._dev_label(s) for s in sorted(entries.keys())]
                    return error_line(f"已达最大会话数({slots.max_sessions})，当前已连接: {active}，请先close释放")

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
            slots.set_focus(session)
            try:
                session.initialize()
            finally:
                slots.clear_focus()

            slots.put(alloc_id, session)

            # 重复连接提示: 相同设备已有会话时提醒AI直接用现有dev编号，
            # 避免无谓地多开会话占用槽位、造成多终端选择歧义
            # 示例引用用纯devN（AI可照抄host参数），展示名devN(终端N)括号含内部槽位号，照抄会报错
            with slots.lock:
                dup_sids = [
                    sid for sid, s in slots.entries_unlocked().items()
                    if s is not None and sid != alloc_id
                    and not s.is_closed()
                    and s.host == host and s.username == username
                ]
                dup_devs = [self._dev_label(sid) for sid in dup_sids]

            parts = [f"[已连接 {self._dev_label(alloc_id)}: {username}@{host}]"]
            if dup_devs:
                dup_refs = "、".join(f"dev{sid + 1}" for sid in dup_sids)
                parts.append(
                    f"[注意: 相同设备已有连接: {'、'.join(dup_devs)}。"
                    f"如非必要请勿重复连接，可直接用 host={dup_refs} 执行命令]"
                )
            if session.initial_output:
                # 折叠登录横幅噪音：>4行时只保留首行(系统版本)+末两行(last login/prompt)
                banner_lines = session.initial_output.rstrip().split("\n")
                if len(banner_lines) > 4:
                    parts.append(banner_lines[0])
                    parts.append(f"...(已省略{len(banner_lines) - 3}行登录横幅)")
                    parts.append("\n".join(banner_lines[-2:]))
                else:
                    parts.append(session.initial_output)
            else:
                parts.append(session.prompt)
            return "\n".join(parts)

        except paramiko.AuthenticationException:
            hint = ""
            if not password and not key_path:
                hint = "。未提供password，大多数设备需要密码认证，请在password参数填登录密码后重试"
            elif looks_like_path and not os.path.isfile(os.path.expanduser(password)):
                hint = "。password疑似私钥路径但本地文件不存在，请确认路径或改填登录密码"
            return error_line(f"认证失败({username}@{host})，请检查password{hint}")
        except paramiko.SSHException as e:
            return error_line(f"SSH连接失败({username}@{host}): {e}")
        except Exception as e:
            return error_line(f"连接失败({username}@{host}): {e}")

    def _exec(self, session_id: int, host: str, command: str, timeout: int = 120,
              max_output_chars: int = 8000, env: Optional[ToolEnv] = None) -> str:
        """在指定会话中执行命令"""
        if not command:
            return error_line("exec需要提供command")
        if timeout <= 0:
            return error_line("timeout需为正整数（秒）")

        settings = env.settings if env is not None else None
        if settings is not None and settings.max_timeout_seconds > 0:
            timeout = min(timeout, settings.max_timeout_seconds)

        # 安全检查：删除命令和git命令根据配置决定是否需要确认
        blocked = confirm_delete_command(
            command, env, tool_name=self.name,
            arguments={
                "action": "exec",
                "session_id": session_id,
                "host": host,
                "command": command,
                "timeout": timeout,
                "max_output_chars": max_output_chars,
            },
            extra_pattern=self.RE_GIT,
        )
        if blocked is not None:
            return blocked

        try:
            sid, session = self._resolve_session_id(session_id, host)
        except ValueError as e:
            return error_line(f"{e}")

        # 断线自动重连（设备关机/重启后恢复，保留dev编号与cwd）。
        # 重连失败不丢弃会话：凭据保留，设备恢复后重试可自动连上
        if session.is_closed() and not self._try_reconnect(session):
            return error_line(f"{self._dev_label(sid)}连接中断，自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连")

        try:
            # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
            self._slots.set_focus(session)
            try:
                result = session.execute(command, timeout=timeout, max_output_chars=max_output_chars)
            finally:
                self._slots.clear_focus()
            # 在结果前标注dev编号
            return f"[{self._dev_label(sid)}] {result}"
        except Exception as e:
            # 连接在操作中死掉（设备重启/网络断）：先自动重连，再让AI重试
            if session.is_closed():
                if self._try_reconnect(session):
                    return error_line(f"{self._dev_label(sid)}连接曾中断，已自动重连，请重试命令")
                return error_line(f"{self._dev_label(sid)}连接中断，自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连")
            return error_line(f"{self._dev_label(sid)}命令执行失败: {e}")

    def _input(self, session_id: int, host: str, input_text: str, timeout: int = 120,
               max_output_chars: int = 8000, env: Optional[ToolEnv] = None) -> str:
        """向终端发送交互输入"""
        if not input_text:
            return error_line("input需要提供input内容")
        if timeout <= 0:
            return error_line("timeout需为正整数（秒）")

        settings = env.settings if env is not None else None
        if settings is not None and settings.max_timeout_seconds > 0:
            timeout = min(timeout, settings.max_timeout_seconds)

        # 安全检查：input内容与exec一致走删除/git确认（防止通过input绕过安全确认）
        blocked = confirm_delete_command(
            input_text, env, tool_name=self.name,
            arguments={
                "action": "input",
                "session_id": session_id,
                "host": host,
                "input": input_text,
                "timeout": timeout,
                "max_output_chars": max_output_chars,
            },
            extra_pattern=self.RE_GIT,
        )
        if blocked is not None:
            return blocked

        try:
            sid, session = self._resolve_session_id(session_id, host)
        except ValueError as e:
            return error_line(f"{e}")

        # 断线自动重连（设备关机/重启后恢复）。重连失败不丢弃会话
        if session.is_closed() and not self._try_reconnect(session):
            return error_line(f"{self._dev_label(sid)}连接中断，自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连")

        try:
            # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
            self._slots.set_focus(session)
            try:
                result = session.send_input(input_text, timeout=timeout, max_output_chars=max_output_chars)
            finally:
                self._slots.clear_focus()
            return f"[{self._dev_label(sid)}] {result}"
        except Exception as e:
            return error_line(f"{self._dev_label(sid)}输入发送失败: {e}")

    def _status(self) -> str:
        """查看所有会话状态"""
        slots = self._slots
        with slots.lock:
            entries = slots.entries_unlocked()
            lines = ["dev0: 本机(当前设备)"]
            for sid in range(slots.max_sessions):
                if sid in entries:
                    session = entries[sid]
                    alive = "活跃" if not session.is_closed() else "已断开"
                    busy = "忙" if session.busy else "闲"
                    # 只显示cwd（prompt会重复user@host，纯噪音；cwd才是AI关心的状态信息）
                    lines.append(f"  {self._dev_label(sid)}: {session.username}@{session.host} "
                                 f"[{alive}|{busy}] 目录:{session.cwd}")
                else:
                    lines.append(f"  {self._dev_label(sid)}: [未连接]")
            if len(entries) == 0:
                return ("[SSH会话]\n" + "\n".join(lines)
                        + f"\n(无已连接设备，最多支持{slots.max_sessions}个并发终端)")
            return "[SSH会话]\n" + "\n".join(lines)

    def _close(self, session_id: int, host: str) -> str:
        """关闭会话（未指定 host 与 session_id 时关闭全部）"""

        def key_flow(entries: dict) -> tuple[Optional[int], Optional[str]]:
            m = self.RE_DEV.match(host.strip())
            if not m:
                return None, error_line(self._dev_hint_of(entries))
            d = int(m.group(1))
            if d == 0:
                return None, "[dev0是当前设备(本机)，无需关闭]"
            sid = d - 1
            if sid >= self._slots.max_sessions or sid not in entries:
                return None, f"[dev{d}未连接]"
            return sid, None

        return close_sessions(
            self._slots, session_id, host,
            all_msg=lambda count: f"[已关闭{count}个会话]",
            sid_missing_msg=lambda sid: f"[{self._dev_label(sid)}未连接]",
            sid_done_msg=lambda sid: f"[已关闭 {self._dev_label(sid)}]",
            key_flow=key_flow,
        )

    def _transfer_files(self, source_host: str, source_path: str,
                        target_host: str, target_path: str,
                        env: Optional[ToolEnv] = None) -> str:
        """在任意设备间传输文件（本机↔被控设备、被控设备↔被控设备）"""
        if not source_path:
            return error_line("transfer需要提供source_path（源文件路径）")
        if not target_path:
            return error_line("transfer需要提供target_path（目标文件路径）")

        # 设备标识校验（只认devN: dev0=本机可省略，dev1..devN=被控设备）
        source_host = self._normalize_device(source_host)
        target_host = self._normalize_device(target_host)
        err = self.device_error(source_host) or self.device_error(target_host)
        if err:
            return err

        if source_host == target_host and source_path == target_path:
            return error_line("源和目标相同，无需传输")

        settings = env.settings if env is not None else None
        max_transfer_mb = (settings.max_transfer_mb if settings is not None
                           else self.DEFAULT_MAX_TRANSFER_MB)
        return self._transfer.transfer(
            source_host, source_path, target_host, target_path,
            max_transfer_mb=max_transfer_mb,
        )

    @staticmethod
    def _arg_text(args: dict, key: str, default: str = "") -> str:
        """取文本参数：缺失或 None 时用默认值（空串语义与旧实现一致）。"""
        value = args.get(key, default)
        return default if value is None else value
