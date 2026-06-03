"""远程文件操作 —— 通过SFTP在远程Linux上读写文件

当AI在Terminal会话中时，Read/Edit/Write可通过SFTP操作远程文件。
AI通过 remote=True 参数显式指定远程操作。
"""

import io
import os
import difflib
from typing import Optional

from .terminal import get_session, SSHSession
from ..config.defaults import MAX_FILE_LINES, MAX_LINE_CHARS
from ..ui.ui_design import colorize_diff


def _get_sftp(session: SSHSession):
    """从SSH会话获取SFTP客户端"""
    return session._client.open_sftp()


# ── 远程Read ──

def remote_read(file_path: str, offset: int = 0, limit: int = 0,
                host: str = "") -> str:
    """通过SFTP读取远程文件"""
    session = get_session(host)
    if session is None:
        return "错误: 无活跃SSH会话，请先Terminal connect"

    try:
        sftp = _get_sftp(session)
        with sftp.open(file_path, "r") as f:
            raw = f.read()
        sftp.close()
    except IOError:
        return f"错误: 远程文件不存在: {file_path}"
    except Exception as e:
        return f"错误: 远程读取失败: {e}"

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")

    lines = content.splitlines(keepends=True)
    total = len(lines)

    if total > MAX_FILE_LINES and offset == 0 and limit == 0:
        lines = lines[:MAX_FILE_LINES]
        hint = f"\n... 文件共{total}行，仅显示前{MAX_FILE_LINES}行，可用offset/limit分段读取 ..."
    else:
        hint = ""

    start = max(offset - 1, 0) if offset > 0 else 0
    if limit > 0:
        lines = lines[start:start + limit]
    else:
        lines = lines[start:]

    result = []
    for i, line in enumerate(lines):
        line_num = start + i + 1
        text = line.rstrip("\n\r")
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "...(截断)"
        result.append(f"  {line_num}→{text}")

    return "\n".join(result) + hint


# ── 远程Write ──

# 跟踪已Read过的远程文件
_read_remote_files: set = set()


def mark_remote_read(file_path: str, host: str = ""):
    """标记远程文件已被Read"""
    key = f"{host}:{file_path}" if host else file_path
    _read_remote_files.add(key)


def remote_write(file_path: str, content: str, host: str = "") -> tuple:
    """通过SFTP写入远程文件"""
    session = get_session(host)
    if session is None:
        return ("错误: 无活跃SSH会话，请先Terminal connect", "")

    key = f"{host}:{file_path}" if host else file_path

    # 覆写已有文件前检查是否Read过
    try:
        sftp = _get_sftp(session)
        try:
            sftp.stat(file_path)
            file_exists = True
        except IOError:
            file_exists = False

        if file_exists and key not in _read_remote_files:
            sftp.close()
            return ((f"错误: 覆写已有远程文件前必须先Read确认当前内容。"
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

        # 写入
        data = content.encode("utf-8")
        with sftp.open(file_path, "w") as f:
            f.write(data)
        sftp.close()

    except Exception as e:
        return (f"错误: 远程写入失败: {e}", "")

    _read_remote_files.add(key)
    byte_count = len(content.encode("utf-8"))
    return (f"已写入(远程): {file_path} ({byte_count}字节)", color_diff)


# ── 远程Edit ──

def remote_edit(file_path: str, old_string: str = "", new_string: str = "",
                replace_all: bool = False,
                line_start: int = 0, line_end: int = 0,
                host: str = "") -> tuple:
    """通过SFTP修改远程文件"""
    session = get_session(host)
    if session is None:
        return ("错误: 无活跃SSH会话，请先Terminal connect", "")

    try:
        sftp = _get_sftp(session)
        with sftp.open(file_path, "r") as f:
            raw = f.read()
        sftp.close()
    except IOError:
        return (f"错误: 远程文件不存在: {file_path}，如需创建请用Write工具", "")
    except Exception as e:
        return (f"错误: 远程读取失败: {e}", "")

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")

    # ── 行号模式 ──
    if line_start > 0:
        return _remote_edit_by_lines(content, file_path, line_start, line_end,
                                     new_string, session)

    # ── 字符串模式 ──
    if not old_string:
        return ("错误: old_string不能为空（或使用line_start行号模式）", "")

    count = content.count(old_string)
    if count == 0:
        return ("错误: 未找到匹配文本。请先Read确认远程文件内容。", "")

    if count > 1 and not replace_all:
        return ((f"错误: 找到{count}处匹配，old_string不唯一。"
                 f"请扩大上下文使其唯一，或设置replace_all=True"), "")

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    return _remote_write_and_diff(content, new_content, file_path,
                                  session, count if replace_all else 1)


def _remote_edit_by_lines(content: str, file_path: str,
                          line_start: int, line_end: int, new_string: str,
                          session: SSHSession) -> tuple:
    """远程行号范围替换"""
    lines = content.splitlines(keepends=True)
    total = len(lines)

    if line_end <= 0:
        line_end = line_start

    if line_start < 1 or line_start > total:
        return (f"错误: line_start={line_start} 超出范围（1-{total}）", "")
    if line_end < line_start:
        return (f"错误: line_end={line_end} < line_start={line_start}", "")
    if line_end > total:
        return (f"错误: line_end={line_end} 超出范围（1-{total}）", "")

    new_lines = new_string.splitlines(keepends=True)
    if new_string and not new_string.endswith("\n"):
        new_lines[-1] = new_lines[-1] + "\n"

    new_content_lines = lines[:line_start - 1] + new_lines + lines[line_end:]
    new_content = "".join(new_content_lines)

    replaced_count = line_end - line_start + 1
    return _remote_write_and_diff(content, new_content, file_path,
                                  session, replaced_count,
                                  f"行{line_start}-{line_end}")


def _remote_write_and_diff(old_content: str, new_content: str, file_path: str,
                           session: SSHSession, count: int,
                           range_desc: str = "") -> tuple:
    """写回远程文件并生成diff"""
    try:
        sftp = _get_sftp(session)
        data = new_content.encode("utf-8")
        with sftp.open(file_path, "w") as f:
            f.write(data)
        sftp.close()
    except Exception as e:
        return (f"错误: 远程写入失败: {e}", "")

    diff = _make_diff(old_content, new_content, file_path)
    if range_desc:
        llm_result = f"已替换{range_desc}（{count}行）\n{diff}"
    else:
        llm_result = f"已替换{count}处\n{diff}"

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
    return result if result else "(无差异)"
