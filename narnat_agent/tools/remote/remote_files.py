"""远程文件访问 —— 文件工具（Read/Edit/Write）的 devN 路径后端（逐行搬运自旧
`narnat_agent/tools/terminal/remote.py`，仅重组结构）。

- 经 SFTP 复用 Terminal 会话：无会话/设备未连接的错误文案与 Terminal 同源；
- 断线自动重连（`session_for` 内部处理），重连失败视为无会话；
- 读取与本地 Read 对齐（行号前缀、二进制检测、编码探测、offset/limit 截断提示）；
- 写入生成 unified diff（AI 视角纯文本 + UI 视角着色），字节级变更判定；
- 编辑拒绝非 UTF-8 文件、换行风格归一化、唯一性校验。

契约来源：specs/tools-remote「远程文件访问（Read/Edit/Write 的 devN 路径）」。

结构说明：diff 着色（`colorize_diff`）与编码探测（`detect_text_encoding`）此前分别
位于 `tools/diff_utils.py` 与 `tools/read/__init__.py`；本模块自包含这两个纯函数
（行为逐字搬运），供 T3.4 文件工具族复用或按 output 主题注入替换。
"""
from __future__ import annotations

import difflib
import errno
import stat
from collections.abc import Callable
from typing import Optional, Protocol, runtime_checkable

from .ssh_session import SSHSession
from .transfer import ensure_remote_dir

__all__ = ["RemoteFileAccess", "SessionProvider", "colorize_diff",
           "describe_bytes_only_change", "detect_text_encoding"]

# ── 默认 diff 着色（真彩 + 默认色板；与旧 `diff_utils.colorize_diff` 逐字等价）──
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_DIM = "\x1b[2m"
ANSI_SECONDARY = "\x1b[38;2;100;116;139m"   # #64748B
ANSI_ACCENT = "\x1b[38;2;94;234;212m"       # #5EEAD4
ANSI_SUCCESS = "\x1b[38;2;52;211;153m"      # #34D399
ANSI_ERROR = "\x1b[38;2;248;113;113m"       # #F87171


def colorize_diff(diff_text: str) -> str:
    """对 unified diff 文本着色：-行红色、+行绿色、@@行青色暗淡，其余灰色。

    保留行首的 +/- 符号，仅对内容着色，不改变文本结构。
    """
    if not diff_text or diff_text == "[无差异]":
        return f"{ANSI_SECONDARY}[无差异]{ANSI_RESET}"

    out = []
    for line in diff_text.split("\n"):
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{ANSI_BOLD}{ANSI_ACCENT}{line}{ANSI_RESET}")
        elif line.startswith("@@"):
            out.append(f"{ANSI_DIM}{ANSI_ACCENT}{line}{ANSI_RESET}")
        elif line.startswith("-"):
            out.append(f"{ANSI_ERROR}{line}{ANSI_RESET}")
        elif line.startswith("+"):
            out.append(f"{ANSI_SUCCESS}{line}{ANSI_RESET}")
        else:
            out.append(f"{ANSI_SECONDARY}{line}{ANSI_RESET}")
    return "\n".join(out)


def describe_bytes_only_change(old_bytes: bytes, new_bytes: bytes) -> str:
    """正文相同、字节却不同时的差异摘要（Write变更判定用）。

    unified diff基于splitlines()，会抹平CRLF/LF与末尾换行差异，不能据此判断
    "文件未变化"——需按字节比较，并说明差异来源：行尾符/末尾换行/字节表示
    （如GBK重写为UTF-8、BOM增减）。
    """
    def _style(b: bytes) -> str:
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        cr = b.count(b"\r") - crlf
        return "+".join(n for n, c in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if c) or "无换行符"

    old_style, new_style = _style(old_bytes), _style(new_bytes)
    old_nl, new_nl = old_bytes.endswith(b"\n"), new_bytes.endswith(b"\n")
    parts = []
    if old_style != new_style:
        parts.append(f"行尾符 {old_style}→{new_style}")
    if old_nl != new_nl:
        parts.append("末尾换行" + ("已添加" if new_nl else "已移除"))
    if not parts:
        # 行尾符与末尾换行都一致，字节却不同 → 换行以外的字节表示变化（编码/BOM）
        parts.append(f"换行符以外的字节表示变化（{len(old_bytes)}→{len(new_bytes)}字节，如编码或BOM差异）")
    return "、".join(parts)


def detect_text_encoding(head: bytes) -> str:
    """utf-8 严格解码成功 → utf-8-sig；失败 → gbk。

    中文Windows环境GBK文件常见（旧日志/导出文件）。此前固定utf-8+replace解码
    会把GBK内容变成大片U+FFFD乱码，AI读到的是坏数据。此处用首块字节判定编码。
    行为搬运自旧 `tools/read/__init__.py::_detect_text_encoding`。

    尾部窗口重试: 首块8KB可能恰好多字节序列边界截断，utf-8严格解码在截断处
    抛错会误判为GBK。UnicodeDecodeError.start位于末尾3字节内时切除重试。
    """
    trial = head
    for _ in range(3):
        try:
            trial.decode("utf-8")
            return "utf-8-sig"
        except UnicodeDecodeError as e:
            if e.start >= len(trial) - 3:
                trial = head[: e.start]  # 疑似边界截断 → 切掉错误起点后重试
                continue
            break
    return "gbk"


@runtime_checkable
class SessionProvider(Protocol):
    """远程文件访问所需的会话端口（由 Terminal 工具实现）。"""

    def session_for(self, host: str = "", session_id: int = -1) -> Optional[SSHSession]:
        """取指定设备的会话（断线自动重连）；解析失败或重连失败返回 None。"""
        ...

    def devices_summary(self) -> str:
        """当前已连接设备清单（`dev1(user@host)`，无设备为 `(无)`）。"""
        ...


class RemoteFileAccess:
    """远程文件读写编辑 —— 文件工具族的 devN 后端。

    构造注入 `SessionProvider`（Terminal 工具）与可选 `colorize`（UI diff 着色；
    默认取本模块 `colorize_diff`）。
    """

    def __init__(
        self,
        sessions: SessionProvider,
        colorize: Callable[[str], str] = colorize_diff,
    ) -> None:
        self._sessions = sessions
        self._colorize = colorize

    # ═══════════════════════════════════════════════════════════
    # 公共辅助
    # ═══════════════════════════════════════════════════════════

    def _no_session_msg(self, host: str = "") -> str:
        """无目标会话时的提示：区分「无任何会话」与「指定设备未连接」两种情况。

        注意: 必须以"[错误"开头，终端UI据此显示失败提示。
        """
        try:
            devs = self._sessions.devices_summary()
            if devs == "(无)":
                return "[错误: 无可用SSH会话，请先Terminal connect建立连接]"
            target = f"指定设备 {host} 未连接。" if host else "无法确定目标设备。"
            return f"[错误: {target}设备标识只支持devN编号(dev1..devn)。当前已连接: {devs}]"
        except Exception:
            return "[错误: 无可用SSH会话，请先Terminal connect]"

    # ═══════════════════════════════════════════════════════════
    # 远程 Read
    # ═══════════════════════════════════════════════════════════

    def read(self, file_path: str, offset: int = 0, limit: int = 2000,
             host: str = "") -> str:
        """通过SFTP读取远程文件（流式，与本地Read行为一致）

        - 仅读取首8KB做二进制检测，二进制文件不再被完整下载后拒绝
        - 按行流式跳过offset/读取limit，大文本文件不会整体载入内存
        """
        if limit <= 0:
            return "[错误: limit需为正整数]"

        session = self._sessions.session_for(host)
        if session is None:
            return self._no_session_msg(host)

        try:
            sftp = session.open_sftp()
        except Exception as e:
            return f"[错误: 远程SFTP打开失败: {e}]"

        try:
            # "rb"模式: read/readline均返回bytes（"r"模式的readline会按UTF-8强解码，
            # 遇到GBK等非UTF-8文件直接抛UnicodeDecodeError）
            try:
                info = sftp.stat(file_path)
            except IOError:
                info = None
            # 目录路径: SFTP open 目录抛原始IOError被误报为"文件不存在"，
            # 提前判定给出准确指引（与本地Read的目录提示对齐）
            if info is not None and stat.S_ISDIR(info.st_mode):
                return ("[错误: 远程路径是目录: "
                        f"{file_path}，请用 Terminal exec 查看目录内容]")

            with sftp.open(file_path, "rb") as f:
                # 二进制检测: 仅读首块8KB（与本地Read检测策略一致）
                head = f.read(8192)
                if b"\x00" in head:
                    return "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请用 Terminal exec 处理]"

                # 编码探测（与本地Read一致）：GBK文件按utf-8+replace读是乱码
                encoding = detect_text_encoding(head)

                f.seek(0)
                start = max(offset - 1, 0) if offset > 0 else 0

                # 流式跳过 offset 行（与本地Read一致，避免大文件整体载入内存）
                skipped = 0
                for _ in range(start):
                    if not f.readline():
                        break
                    skipped += 1

                result = []
                truncated = False
                for i in range(limit):
                    line = f.readline()
                    if not line:
                        break
                    line_num = start + i + 1
                    content = line.rstrip(b"\n\r").decode(encoding, errors="replace")
                    result.append(f"  {line_num}→{content}")
                else:
                    # for...else: 读完limit行后还有剩余内容
                    if f.readline():
                        truncated = True
        except OSError as e:
            # SFTP把EACCES(errno=13)也抛成IOError：一律报"不存在"会把排查方向
            # 引向路径拼写，实际问题通常是权限（chmod/属主/父目录缺x位）
            if e.errno == errno.ENOENT:
                return f"[错误: 远程文件不存在: {file_path}]"
            if e.errno == errno.EACCES:
                return f"[错误: 远程文件权限不足（EACCES）: {file_path}]"
            return f"[错误: 远程读取失败: {file_path} ({e})]"
        except Exception as e:
            return f"[错误: 远程读取失败: {e}]"
        finally:
            try:
                sftp.close()
            except Exception:
                pass

        # 空结果提示（offset超出末尾 / 空文件）
        if not result:
            if start > 0:
                return f"[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]"
            return "[文件为空]"

        # 截断提示（与本地Read一致: 给出精确续读offset，AI无需自己计算）
        if truncated:
            result.append(f"  ... [截断: 已显示 {limit} 行。使用 offset={start + limit + 1} 参数可读取其余部分]")

        return "\n".join(result)

    # ═══════════════════════════════════════════════════════════
    # 远程 Write
    # ═══════════════════════════════════════════════════════════

    def write(self, file_path: str, content: str, host: str = "") -> tuple:
        """通过SFTP写入远程文件（返回 `(AI文本, UI着色diff)`）"""
        session = self._sessions.session_for(host)
        if session is None:
            return (self._no_session_msg(host), "")

        # 探测目标是否存在（目录判定与diff生成共用）
        try:
            sftp = session.open_sftp()
            try:
                info = sftp.stat(file_path)
                file_exists = True
            except IOError:
                file_exists = False

            # 目录路径：SFTP写入会报原始IOError，误导AI去查权限，提前拦截给出真实原因
            if file_exists and stat.S_ISDIR(info.st_mode):
                sftp.close()
                return (f"[错误: 远程路径是目录: {file_path}，请使用正确的文件路径]", "")

            # 读取旧内容生成diff
            diff = ""
            color_diff = ""
            old_bytes = None
            if file_exists:
                try:
                    with sftp.open(file_path, "r") as f:
                        old_bytes = f.read()  # paramiko无文本模式，read()返回原始字节
                    old_content = old_bytes.decode("utf-8", errors="replace")
                    diff = self._make_diff(old_content, content, file_path)
                    color_diff = self._colorize(diff)
                except Exception:
                    pass

            # 自动创建远程父目录（与本地Write行为一致）。仅处理绝对路径：
            # _ensure_remote_dir 按 / 分段创建，相对路径无法定位正确的远程基目录
            if not file_exists and file_path.startswith("/"):
                if not ensure_remote_dir(session, file_path):
                    sftp.close()
                    parent = file_path.rsplit("/", 1)[0] or "/"
                    return (f"[错误: 无法创建远程目标目录: {parent}（可能无写权限）]", "")

            # 写入
            data = content.encode("utf-8")
            with sftp.open(file_path, "w") as f:
                f.write(data)
            sftp.close()

        except Exception as e:
            return (f"[错误: 远程写入失败: {e}]", "")

        new_bytes = content.encode("utf-8")
        byte_count = len(new_bytes)
        dev_tag = f"[{host}] " if host else ""
        # 变更判定基于字节比较：_make_diff的splitlines()会抹平行尾符与末尾换行差异，
        # 据其报"无实质修改"会在文件已被改写（CRLF静默变LF）时给出假报告
        if diff == "[无差异]" and old_bytes is not None:
            if new_bytes == old_bytes:
                return (f"{dev_tag}[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]", color_diff)
            detail = describe_bytes_only_change(old_bytes, new_bytes)
            return (f"{dev_tag}[提示: 文件已写入，正文内容相同，但字节层面有变化（{detail}）]",
                    self._colorize(f"[正文相同，字节变化] {detail}"))
        if diff:
            return (f"{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]\n{diff}", color_diff)
        return (f"{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]", color_diff)

    # ═══════════════════════════════════════════════════════════
    # 远程 Edit
    # ═══════════════════════════════════════════════════════════

    def edit(self, file_path: str, old_string: str = "", new_string: str = "",
             replace_all: bool = False, host: str = "") -> tuple:
        """通过SFTP修改远程文件（返回 `(AI文本, UI着色diff)`）"""
        session = self._sessions.session_for(host)
        if session is None:
            return (self._no_session_msg(host), "")

        try:
            sftp = session.open_sftp()
            with sftp.open(file_path, "r") as f:
                raw = f.read()
            sftp.close()
        except OSError as e:
            # EACCES与ENOENT必须区分：文件存在但无权限时若提示"请用Write创建"，
            # 会诱导AI覆盖一个真实存在的文件
            if e.errno == errno.ENOENT:
                return (f"[错误: 远程文件不存在: {file_path}，如需创建请用Write工具]", "")
            if e.errno == errno.EACCES:
                return (f"[错误: 远程文件权限不足（EACCES），未做任何修改: {file_path}]", "")
            return (f"[错误: 远程文件无法读取: {file_path} ({e})]", "")
        except Exception as e:
            return (f"[错误: 远程读取失败: {e}]", "")

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 非UTF-8文件（GBK/Latin-1等）拒绝编辑：decode失败时若降级errors="replace"，
            # 写回会把原文件字节永久替换为U+FFFD，造成静默数据损坏
            return (f"[错误: 远程文件非UTF-8编码，为防止内容损坏已拒绝编辑: {file_path}。"
                    f"请用Terminal exec处理（如转码为UTF-8后再编辑）]", "")

        # 字符串模式
        if not old_string:
            return ("[错误: old_string不能为空]", "")

        # 换行符归一化（与本地Edit行为一致）：远程文件为CRLF（如Windows上传）时，
        # AI传\n风格old_string也能匹配，避免"明明内容存在却匹配失败"
        has_crlf = '\r\n' in content
        if has_crlf:
            normalize = lambda s: s.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
        else:
            normalize = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')

        old_string_normalized = normalize(old_string)
        new_string_normalized = normalize(new_string)

        count = content.count(old_string_normalized)
        if count == 0:
            return ("[错误: 未找到匹配文本。请先Read确认远程文件内容。]", "")

        if count > 1 and not replace_all:
            return ((f"[错误: 找到{count}处匹配，old_string不唯一。"
                     f"请扩大上下文使其唯一，或设置replace_all=True"), "")

        if replace_all:
            new_content = content.replace(old_string_normalized, new_string_normalized)
        else:
            new_content = content.replace(old_string_normalized, new_string_normalized, 1)

        return self._write_and_diff(content, new_content, file_path,
                                    session, count if replace_all else 1, host=host)

    def _write_and_diff(self, old_content: str, new_content: str, file_path: str,
                        session: SSHSession, count: int, host: str = "") -> tuple:
        """写回远程文件并生成diff"""
        try:
            sftp = session.open_sftp()
            data = new_content.encode("utf-8")
            with sftp.open(file_path, "w") as f:
                f.write(data)
            sftp.close()
        except Exception as e:
            return (f"[错误: 远程写入失败: {e}]", "")

        diff = self._make_diff(old_content, new_content, file_path)
        dev_tag = f"[{host}] " if host else ""
        if old_content == new_content:
            # 空编辑提醒（与本地Edit行为一致）：此前返回"[已替换1处]\n[无差异]"，
            # 首行误导AI以为编辑成功。文件已照常写盘，但明确告知本次无实质修改
            return (f"{dev_tag}[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]",
                    self._colorize("[无差异]"))
        llm_result = f"{dev_tag}[已替换{count}处]\n{diff}"

        color_diff = self._colorize(diff)
        return (llm_result, color_diff)

    @staticmethod
    def _make_diff(old_content: str, new_content: str, file_path: str) -> str:
        """生成unified diff（无差异返回 `[无差异]`）"""
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        basename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{basename}",
            tofile=f"b/{basename}",
            lineterm="",
        )
        result = "\n".join(diff)
        return result if result else "[无差异]"
