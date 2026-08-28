"""远程文件操作 —— 通过SFTP在远程Linux上读写文件

当AI在Terminal会话中时，Read/Edit/Write可通过SFTP操作远程文件。
AI通过 device 参数（dev1..devn）指定远程设备。
"""

import io
import os
import difflib
from typing import Optional

from . import get_session, SSHSession
from ..diff_utils import colorize_diff


def _no_session_msg(host: str = "") -> str:
    """无目标会话时的提示：区分「无任何会话」与「指定设备未连接」两种情况。

    注意: 必须以"[错误"开头——Read工具靠startswith("[错误")判断失败读不标记已读，
    若失败消息无此前缀，一次失败的Read会被误标为已读，绕过Write/Edit覆写保护。
    """
    try:
        from . import _list_devices
        devs = _list_devices()
        if devs == "(无)":
            return "[错误: 无可用SSH会话，请先Terminal connect建立连接]"
        target = f"指定设备 {host} 未连接。" if host else "无法确定目标设备。"
        return f"[错误: {target}设备标识只支持devN编号(dev1..devn)。当前已连接: {devs}]"
    except Exception:
        return "[错误: 无可用SSH会话，请先Terminal connect]"


def _get_sftp(session: SSHSession):
    """从SSH会话获取SFTP客户端"""
    return session._client.open_sftp()


# ── 远程Read ──

def remote_read(file_path: str, offset: int = 0, limit: int = 2000,
                host: str = "") -> str:
    """通过SFTP读取远程文件（流式，与本地Read行为一致）

    - 仅读取首8KB做二进制检测，二进制文件不再被完整下载后拒绝
    - 按行流式跳过offset/读取limit，大文本文件不会整体载入内存
    """
    if limit <= 0:
        return "[错误: limit需为正整数]"

    session = get_session(host=host)
    if session is None:
        return _no_session_msg(host)

    try:
        sftp = _get_sftp(session)
    except Exception as e:
        return f"[错误: 远程SFTP打开失败: {e}]"

    try:
        # "rb"模式: read/readline均返回bytes（"r"模式的readline会按UTF-8强解码，
        # 遇到GBK等非UTF-8文件直接抛UnicodeDecodeError）
        with sftp.open(file_path, "rb") as f:
            # 二进制检测: 仅读首块8KB（与本地Read检测策略一致）
            head = f.read(8192)
            if b"\x00" in head:
                return "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请使用Shell工具处理]"

            # 编码探测（与本地Read一致）：GBK文件按utf-8+replace读是乱码
            from ..read import _detect_text_encoding
            encoding = _detect_text_encoding(head)

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
    except IOError:
        return f"[错误: 远程文件不存在: {file_path}]"
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


# ── 远程Write ──


def remote_write(file_path: str, content: str, host: str = "", _tool_context=None) -> tuple:
    """通过SFTP写入远程文件"""
    session = get_session(host=host)
    if session is None:
        return (_no_session_msg(host), "")

    # 覆写已有文件前检查是否Read过
    import stat as _stat_mod
    try:
        sftp = _get_sftp(session)
        try:
            info = sftp.stat(file_path)
            file_exists = True
        except IOError:
            file_exists = False

        # 目录路径：SFTP写入会报原始IOError，误导AI去查权限，提前拦截给出真实原因
        if file_exists and _stat_mod.S_ISDIR(info.st_mode):
            sftp.close()
            return (f"[错误: 远程路径是目录: {file_path}，请使用正确的文件路径]", "")

        if file_exists and _tool_context and not _tool_context.is_remote_read(file_path, host):
            sftp.close()
            return ((f"[错误: 覆写已有远程文件前必须先Read确认当前内容。"
                     f"请先Read {file_path}，再决定用Edit还是Write。"), "")

        # 读取旧内容生成diff
        color_diff = ""
        if file_exists:
            try:
                with sftp.open(file_path, "r") as f:
                    old_raw = f.read()
                old_content = old_raw.decode("utf-8", errors="replace")
                diff = _make_diff(old_content, content, file_path)
                color_diff = colorize_diff(diff)
            except Exception:
                pass

        # 自动创建远程父目录（与本地Write行为一致）。仅处理绝对路径：
        # _ensure_remote_dir 按 / 分段创建，相对路径无法定位正确的远程基目录
        if not file_exists and file_path.startswith("/"):
            from . import _ensure_remote_dir
            if not _ensure_remote_dir(session, file_path):
                sftp.close()
                return (f"[错误: 无法创建远程目标目录: {file_path}]", "")

        # 写入
        data = content.encode("utf-8")
        with sftp.open(file_path, "w") as f:
            f.write(data)
        sftp.close()

    except Exception as e:
        return (f"[错误: 远程写入失败: {e}]", "")

    if _tool_context:
        _tool_context.mark_remote_read(file_path, host)
    byte_count = len(content.encode("utf-8"))
    dev_tag = f"[{host}] " if host else ""
    return (f"{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]", color_diff)


# ── 远程Edit ──

def remote_edit(file_path: str, old_string: str = "", new_string: str = "",
                replace_all: bool = False,
                host: str = "", _tool_context=None) -> tuple:
    """通过SFTP修改远程文件"""
    session = get_session(host=host)
    if session is None:
        return (_no_session_msg(host), "")

    # 编辑前必须Read（与本地Edit行为一致：防止AI盲改未确认的远程文件）
    if _tool_context and not _tool_context.is_remote_read(file_path, host):
        return (f"[错误: 编辑远程文件前必须先Read该文件: {file_path}]", "")

    try:
        sftp = _get_sftp(session)
        with sftp.open(file_path, "r") as f:
            raw = f.read()
        sftp.close()
    except IOError:
        return (f"[错误: 远程文件不存在: {file_path}，如需创建请用Write工具]", "")
    except Exception as e:
        return (f"[错误: 远程读取失败: {e}]", "")

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 非UTF-8文件（GBK/Latin-1等）拒绝编辑：decode失败时若降级errors="replace"，
        # 写回会把原文件字节永久替换为U+FFFD，造成静默数据损坏
        return (f"[错误: 远程文件非UTF-8编码，为防止内容损坏已拒绝编辑: {file_path}。"
                f"请用Terminal exec处理（如iconv转码后再编辑）]", "")

    # 字符串模式
    if not old_string:
        return ("[错误: old_string不能为空]", "")

    # 换行符归一化（与本地Edit行为一致）：远程文件为CRLF（如Windows上传）时，
    # AI传\n风格old_string也能匹配，避免"明明内容存在却匹配失败"
    has_crlf = '\r\n' in content
    if has_crlf:
        _normalize = lambda s: s.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
    else:
        _normalize = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')

    old_string_normalized = _normalize(old_string)
    new_string_normalized = _normalize(new_string)

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

    return _remote_write_and_diff(content, new_content, file_path,
                                  session, count if replace_all else 1,
                                  _tool_context=_tool_context, host=host)


def _remote_write_and_diff(old_content: str, new_content: str, file_path: str,
                           session: SSHSession, count: int, _tool_context=None,
                           host: str = "") -> tuple:
    """写回远程文件并生成diff"""
    try:
        sftp = _get_sftp(session)
        data = new_content.encode("utf-8")
        with sftp.open(file_path, "w") as f:
            f.write(data)
        sftp.close()
    except Exception as e:
        return (f"[错误: 远程写入失败: {e}]", "")

    # 标记已读：连续编辑无需重复Read（与本地Edit的mark_read行为一致）
    if _tool_context:
        _tool_context.mark_remote_read(file_path, host)

    diff = _make_diff(old_content, new_content, file_path)
    dev_tag = f"[{host}] " if host else ""
    llm_result = f"{dev_tag}[已替换{count}处]\n{diff}"

    color_diff = colorize_diff(diff)
    return (llm_result, color_diff)


def _make_diff(old_content: str, new_content: str, file_path: str) -> str:
    """生成unified diff"""
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
