"""文件传输 —— 本机 ↔ 被控设备 ↔ 被控设备三种路径（逐行搬运自旧
`narnat_agent/tools/terminal/__init__.py` 的 `_transfer` 系列，仅重组结构）。

- 三路：本机→被控设备（`sftp.put`）、被控设备→本机（`sftp.get`）、
  被控设备→被控设备（固定块流式中转，文件不落本机磁盘）；
- 传输前按 `工具.最大传输文件MB` 校验大小上限（0 或负值表示不限制）；
- 源为目录一律拒绝并给出手工打包指引；
- SFTP 句柄经 `SSHSession.open_sftp()` 获取（design D7：不跨层直取 `_client`）。

契约来源：specs/tools-remote「文件传输」。
"""
from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable
from typing import Optional

from ..signal import error_line
from .ssh_session import SSHSession

__all__ = ["FileTransfer", "check_transfer_size", "ensure_remote_dir", "format_size",
           "remote_file_size"]

# 流式传输块大小（远程→远程中转；文件不落本机磁盘）
TRANSFER_BUFFER_SIZE = 65536


def remote_file_size(session: SSHSession, path: str) -> Optional[tuple]:
    """获取远程文件 (大小, 是否目录)。目录也返回而非None——
    区分"不存在"与"是目录"，让transfer给出明确指引而非paramiko的"Failure"。
    返回None表示无法访问（不存在/权限）。"""
    try:
        sftp = session.open_sftp()
        try:
            st = sftp.stat(path)
            return st.st_size, stat_module.S_ISDIR(st.st_mode)
        finally:
            sftp.close()
    except Exception:
        return None


def ensure_remote_dir(session: SSHSession, remote_path: str) -> bool:
    """逐级创建远程父目录（paramiko 的 mkdir 不递归）；失败返回 False。"""
    parent = remote_path.rsplit("/", 1)[0]
    if not parent or parent == remote_path:
        return True
    try:
        sftp = session.open_sftp()
        try:
            parts = parent.strip("/").split("/")
            cur = ""
            for p in parts:
                cur += "/" + p
                try:
                    sftp.stat(cur)
                except IOError:
                    try:
                        sftp.mkdir(cur)
                    except IOError as e:
                        # 并发创建/已存在可接受；仍不存在才是真失败（如权限不足）
                        try:
                            sftp.stat(cur)
                        except IOError:
                            raise e
        finally:
            sftp.close()
        return True
    except Exception:
        return False


def format_size(size_bytes: int) -> str:
    """按 1024 进制格式化文件大小（B/KB/MB/GB，保留 1 位小数）。"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def check_transfer_size(size_bytes: int, max_transfer_mb: int) -> Optional[str]:
    """校验传输大小上限；超限返回错误文案，未超限返回 None。"""
    if max_transfer_mb <= 0:
        return None
    max_bytes = max_transfer_mb * 1024 * 1024
    if size_bytes > max_bytes:
        return f"文件大小 {format_size(size_bytes)} 超过传输上限 {format_size(max_bytes)}"
    return None


class FileTransfer:
    """文件传输服务 —— 三路传输与目录/大小校验的合并实现。

    构造注入两个端口：`session_lookup(host) -> SSHSession | None`（断线自动重连，
    失败返回 None）与 `devices_summary() -> str`（已连接设备清单，供错误文案）。
    """

    def __init__(
        self,
        session_lookup: Callable[[str], Optional[SSHSession]],
        devices_summary: Callable[[], str],
        buffer_size: int = TRANSFER_BUFFER_SIZE,
    ) -> None:
        self._session_lookup = session_lookup
        self._devices_summary = devices_summary
        self._buffer_size = buffer_size

    def transfer(
        self,
        source_host: str,
        source_path: str,
        target_host: str,
        target_path: str,
        *,
        max_transfer_mb: int,
    ) -> str:
        """按四象限分派传输（空设备引用=本机；双本机报错引导本地文件工具）。"""
        source_is_local = not source_host
        target_is_local = not target_host
        if source_is_local and target_is_local:
            return error_line("源和目标都是本机(dev0)，请使用本地文件操作工具")
        if source_is_local:
            return self._local_to_remote(source_path, target_host, target_path, max_transfer_mb)
        if target_is_local:
            return self._remote_to_local(source_host, source_path, target_path, max_transfer_mb)
        return self._remote_to_remote(source_host, source_path, target_host, target_path,
                                      max_transfer_mb)

    # ═══════════════════════════════════════════════════════════
    # 三路传输
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _ensure_local_dir(local_path: str) -> bool:
        """创建本地目标文件的父目录；失败返回 False。"""
        parent = os.path.dirname(local_path)
        if not parent:
            return True
        try:
            os.makedirs(parent, exist_ok=True)
            return True
        except OSError:
            return False

    def _local_to_remote(self, source_path: str, target_host: str,
                         target_path: str, max_transfer_mb: int) -> str:
        if os.path.isdir(source_path):
            return error_line(f"源是目录，transfer仅支持文件传输。目录请先用 Shell 打包为单个文件（如 tar/zip）再传输: {source_path}")
        if not os.path.isfile(source_path):
            return error_line(f"源文件不存在: {source_path}")

        size = os.path.getsize(source_path)
        err = check_transfer_size(size, max_transfer_mb)
        if err:
            return error_line(f"{err}")

        session = self._session_lookup(target_host)
        if session is None:
            return error_line(f"目标设备 {target_host} 未连接或已断开，请先connect。当前已连接: {self._devices_summary()}")

        if not ensure_remote_dir(session, target_path):
            parent = target_path.rsplit("/", 1)[0] or "/"
            return error_line(f"无法创建远程目标目录: {parent}（可能无写权限）")

        try:
            sftp = session.open_sftp()
            try:
                sftp.put(source_path, target_path)
            finally:
                sftp.close()
        except Exception as e:
            return error_line(f"传输失败: {e}")

        return f"[已传输: 本机:{source_path} → {target_host}:{target_path} ({format_size(size)})]"

    def _remote_to_local(self, source_host: str, source_path: str,
                         target_path: str, max_transfer_mb: int) -> str:
        session = self._session_lookup(source_host)
        if session is None:
            return error_line(f"源设备 {source_host} 未连接或已断开，请先connect。当前已连接: {self._devices_summary()}")

        st = remote_file_size(session, source_path)
        if st is None:
            return error_line(f"源文件不存在或无法访问: {source_host}:{source_path}")
        size, is_dir = st
        if is_dir:
            return error_line(f"源是目录，transfer仅支持文件传输。目录请先用 exec 打包为单个文件（如 tar/zip）再传输: {source_host}:{source_path}")

        err = check_transfer_size(size, max_transfer_mb)
        if err:
            return error_line(f"{err}")

        if not self._ensure_local_dir(target_path):
            return error_line(f"无法创建本地目标目录: {target_path}")

        try:
            sftp = session.open_sftp()
            try:
                sftp.get(source_path, target_path)
            finally:
                sftp.close()
        except Exception as e:
            return error_line(f"传输失败: {e}")

        return f"[已传输: {source_host}:{source_path} → 本机:{target_path} ({format_size(size)})]"

    def _remote_to_remote(self, source_host: str, source_path: str,
                          target_host: str, target_path: str,
                          max_transfer_mb: int) -> str:
        src_session = self._session_lookup(source_host)
        if src_session is None:
            return error_line(f"源设备 {source_host} 未连接或已断开，请先connect。当前已连接: {self._devices_summary()}")

        tgt_session = self._session_lookup(target_host)
        if tgt_session is None:
            return error_line(f"目标设备 {target_host} 未连接或已断开，请先connect。当前已连接: {self._devices_summary()}")

        st = remote_file_size(src_session, source_path)
        if st is None:
            return error_line(f"源文件不存在或无法访问: {source_host}:{source_path}")
        size, is_dir = st
        if is_dir:
            return error_line(f"源是目录，transfer仅支持文件传输。目录请先在源设备 exec 打包为单个文件（如 tar/zip）再传输: {source_host}:{source_path}")

        err = check_transfer_size(size, max_transfer_mb)
        if err:
            return error_line(f"{err}")

        if not ensure_remote_dir(tgt_session, target_path):
            return error_line(f"无法创建远程目标目录: {target_path}")

        transferred = 0
        try:
            src_sftp = src_session.open_sftp()
            tgt_sftp = tgt_session.open_sftp()
            try:
                src_file = src_sftp.open(source_path, "rb")
                tgt_file = tgt_sftp.open(target_path, "wb")
                try:
                    while True:
                        chunk = src_file.read(self._buffer_size)
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
            return error_line(f"传输中断，已传输 {format_size(transferred)}/{format_size(size)}: {e}")

        return f"[已传输: {source_host}:{source_path} → {target_host}:{target_path} ({format_size(size)})]"
