"""远程文件操作 —— 通过SFTP在远程Linux上读写文件

当AI在Terminal会话中时，Read/Edit/Write可通过SFTP操作远程文件。
AI通过 device 参数（dev1..devn）指定远程设备。
"""

import difflib
import errno

from . import get_session, SSHSession
from ..diff_utils import colorize_diff, describe_bytes_only_change


def _no_session_msg(host: str = "") -> str:
    """无目标会话时的提示：区分「无任何会话」与「指定设备未连接」两种情况。

    注意: 必须以"[错误"开头，终端UI据此显示失败提示。
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
        import stat as _stat_mod
        try:
            info = sftp.stat(file_path)
        except IOError:
            info = None
        # 目录路径: SFTP open 目录抛原始IOError被误报为"文件不存在"，
        # 提前判定给出准确指引（与本地Read的目录提示对齐）
        if info is not None and _stat_mod.S_ISDIR(info.st_mode):
            return ("[错误: 远程路径是目录: "
                    f"{file_path}，请用 Terminal exec 查看目录内容]")

        with sftp.open(file_path, "rb") as f:
            # 二进制检测: 仅读首块8KB（与本地Read检测策略一致）
            head = f.read(8192)
            if b"\x00" in head:
                return "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请用 Terminal exec 处理]"

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


# ── 远程Write ──


def remote_write(file_path: str, content: str, host: str = "") -> tuple:
    """通过SFTP写入远程文件"""
    session = get_session(host=host)
    if session is None:
        return (_no_session_msg(host), "")

    # 探测目标是否存在（目录判定与diff生成共用）
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

        # 读取旧内容生成diff
        diff = ""
        color_diff = ""
        old_bytes = None
        if file_exists:
            try:
                with sftp.open(file_path, "r") as f:
                    old_bytes = f.read()  # paramiko无文本模式，read()返回原始字节
                old_content = old_bytes.decode("utf-8", errors="replace")
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
                colorize_diff(f"[正文相同，字节变化] {detail}"))
    if diff:
        return (f"{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]\n{diff}", color_diff)
    return (f"{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]", color_diff)


# ── 远程Edit ──

def remote_edit(file_path: str, old_string: str = "", new_string: str = "",
                replace_all: bool = False,
                host: str = "") -> tuple:
    """通过SFTP修改远程文件"""
    session = get_session(host=host)
    if session is None:
        return (_no_session_msg(host), "")

    try:
        sftp = _get_sftp(session)
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
                                  session, count if replace_all else 1, host=host)


def _remote_write_and_diff(old_content: str, new_content: str, file_path: str,
                           session: SSHSession, count: int,
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

    diff = _make_diff(old_content, new_content, file_path)
    dev_tag = f"[{host}] " if host else ""
    if old_content == new_content:
        # 空编辑提醒（与本地Edit行为一致）：此前返回"[已替换1处]\n[无差异]"，
        # 首行误导AI以为编辑成功。文件已照常写盘，但明确告知本次无实质修改
        return (f"{dev_tag}[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]",
                colorize_diff("[无差异]"))
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
