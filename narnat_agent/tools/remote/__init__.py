"""tools/remote 工具族 —— Terminal（多设备持久 SSH）与 Serial（多会话串口）。

- `TerminalTool` / `SerialTool`：`contracts.tool.Tool` 协议实现，注册进 `ToolRegistry`；
- `SSHSession` / `SerialSession`：单会话封装（提示符检测、哨兵完成检测、sudo 注入、
  后台收敛、断线重连等）；
- `FileTransfer`：Terminal 的 transfer 三路实现（本机↔被控设备↔被控设备）；
- `RemoteFileAccess`：文件工具（Read/Edit/Write）的 devN 路径后端；
- `SessionProvider`：`RemoteFileAccess` 所需的会话端口（`TerminalTool` 满足）；
- `common`：Terminal 与 Serial 的六组共用实现（会话槽位状态机、解析/关闭/清理骨架、
  删除确认、文本清洗与截断）。

装配（app 积木）：构造两个工具（`max_sessions` 由配置 `工具.SSH最大会话数` 注入，
明示修复项：Terminal 与 Serial 共用同一来源）、注册进 `ToolRegistry`、把
`kill_active_exec` 订阅到中断总线（ESC 打断）、程序退出时调用 `cleanup`。

契约来源：specs/tools-remote（26 Requirement / 107 场景）。
"""
from __future__ import annotations

from . import common
from .common import SessionSlots
from .remote_files import (
    RemoteFileAccess,
    SessionProvider,
    colorize_diff,
    describe_bytes_only_change,
    detect_text_encoding,
)
from .serial import DEFINITION as SERIAL_DEFINITION
from .serial import SerialTool
from .serial_session import SerialSession
from .ssh_session import SSHSession
from .terminal import DEFINITION as TERMINAL_DEFINITION
from .terminal import TerminalTool, local_host
from .transfer import FileTransfer

__all__ = [
    # 子模块（`from narnat_agent.tools.remote import common` 等）
    "common",
    # 工具（Tool 协议实现）
    "TerminalTool",
    "SerialTool",
    # 工具定义（LLM 侧契约；与旧包 DEFINITION 逐字等价）
    "TERMINAL_DEFINITION",
    "SERIAL_DEFINITION",
    # 会话
    "SSHSession",
    "SerialSession",
    # 传输与远程文件
    "FileTransfer",
    "RemoteFileAccess",
    "SessionProvider",
    # 共享实现与纯函数
    "SessionSlots",
    "colorize_diff",
    "describe_bytes_only_change",
    "detect_text_encoding",
    "local_host",
]
