"""文件工具的 `device` 语义与远程文件访问端口。

契约来源：
- specs/tools-file「Read/Edit/Write 的设备参数契约」（归一规则与统一错误文案）；
- specs/tools-remote「设备引用约定（devN）」（本族取同一归一约定）与
  「远程文件访问（Read/Edit/Write 的 devN 路径）」（远程分支的行为归余）。

结构（design D7/D8）：设备清单与远程读写由 `tools/remote` 族实现、经构造注入
（`RemoteFiles` 端口）；未装配时使用 `MissingRemote`（设备清单为空、读写返回
与「无任何 SSH 会话」一致的错误文案——与远程族无会话路径的文案逐字相同）。
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

__all__ = [
    "DEFAULT_REMOTE",
    "DEVICE_GUIDE",
    "NO_SESSION_ERROR",
    "MissingRemote",
    "RemoteFiles",
    "device_hint",
    "normalize_device",
]

# dev编号正则: dev0=本机(当前设备), devN(N>=1)=第N台被控设备(终端N-1)
DEV_RE = re.compile(r"^dev(\d+)$", re.IGNORECASE)

DEVICE_GUIDE = "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)"
"""文件工具设备标识错误时的统一指导语前缀（specs/tools-file 已发布文案）。"""

NO_SESSION_ERROR = "[错误: 无可用SSH会话，请先Terminal connect建立连接]"
"""远程文件访问无会话时的错误文案（specs/tools-remote「远程文件访问」）。"""


@runtime_checkable
class RemoteFiles(Protocol):
    """远程文件访问端口（由 `tools/remote` 族实现，装配时注入）。

    - `device_list()`：已连接设备清单（形如 `dev1(user@host)`、多个以 `、` 连接，
      无设备时返回 `(无)`）；
    - `read`：远程读取（返回带行号文本或以 `[` 开头的错误行）；
    - `write` / `edit`：返回 `(文本结果, 着色 diff)` 二元组。
    """

    def device_list(self) -> str: ...

    def read(self, file_path: str, offset: int, limit: int, device: str) -> str: ...

    def write(self, file_path: str, content: str, device: str) -> tuple[str, str]: ...

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool, device: str) -> tuple[str, str]: ...


class MissingRemote:
    """缺省端口：未装配远程文件实现（`tools/remote` 接线前）时的退化实现。

    行为取「无任何 SSH 会话」语义：设备清单为空、读写返回统一无会话错误行——
    与远程族真实实现的无会话路径一致，故装配前/后同一调用的可观察结果相同。
    """

    def device_list(self) -> str:
        return "(无)"

    def read(self, file_path: str, offset: int, limit: int, device: str) -> str:
        return NO_SESSION_ERROR

    def write(self, file_path: str, content: str, device: str) -> tuple[str, str]:
        return (NO_SESSION_ERROR, "")

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool, device: str) -> tuple[str, str]:
        return (NO_SESSION_ERROR, "")


DEFAULT_REMOTE = MissingRemote()


def normalize_device(device: str) -> str | None:
    """文件工具（Read/Edit/Write）的设备标识归一化：合法返回规范值（本机为 ""），非法返回 None。

    - 空串/缺省 → 本机（""）；
    - `dev0`（大小写不敏感、容忍首尾空白）→ 本机（""）；
    - `devN`（N≥1，大小写不敏感、容忍首尾空白）→ 原样保留的规范值；
    - 其他值（如 `host1`）→ None（调用方报统一错误）。
    """
    if not device:
        return ""
    h = device.strip()
    m = DEV_RE.match(h)
    if not m:
        return None
    return "" if int(m.group(1)) == 0 else h


def device_hint(remote: RemoteFiles | None = None) -> str:
    """设备标识错误时的统一指导：附当前已连接设备清单，减少 AI 回查 status 的往返。"""
    base = DEVICE_GUIDE
    devs = (remote if remote is not None else DEFAULT_REMOTE).device_list()
    if devs == "(无)":
        return f"{base}。当前无已连接设备，请先Terminal connect"
    return f"{base}。当前已连接: {devs}"
