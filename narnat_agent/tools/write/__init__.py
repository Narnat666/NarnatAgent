"""Write工具 —— 创建新文件或完整覆写文件

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import itertools
import os
import shutil
import time
import difflib

from ..diff_utils import colorize_diff, describe_bytes_only_change
from ..terminal import _normalize_device_for_tools, _file_tool_device_hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": (
            "创建新文件或全量覆盖文件。支持本地或远程写入文件。"
            "覆写已存在的文件时返回diff。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即写入本地指定文件，设置dev1..devn则写入指定远程设备文件",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}


CAPABILITY = {
    "label": "写入",
    "dispatch": "write",
    "summary": "file_path",
    "trusted_output": True,
}


# Windows 保留设备名（写这些名字等价于写设备，要么假成功要么抛 WinError）
# CONIN$/CONOUT$ 是控制台输入/输出缓冲区设备名（同属微软保留名清单）：
# CONOUT$ 写入假成功（内容进控制台后丢失）、CONIN$ 抛 ValueError 而非 OSError
_NT_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def _is_nt_reserved_name(name: str) -> bool:
    """basename 是否命中 Windows 保留设备名。

    判据：取第一个 '.' 之前的部分、再去掉尾随点/空格后大写 ∈ 保留名集合
    （CON/NUL/COM1 等含扩展名写法同样命中，如 con.txt、work\\PRN.txt；
    注意先切首段再剥尾：'nul .txt' 的 stem 是 'nul '，剥尾后才等于 NUL——
    顺序颠倒会漏拦，写入被送进 NUL 设备且报"已写入"假成功）。
    """
    stem = name.split(".", 1)[0].rstrip(" .")
    return stem.upper() in _NT_RESERVED_NAMES


def _has_nt_ads_colon(file_path: str) -> bool:
    """Windows 路径是否含 NTFS 备用数据流（ADS）冒号语法：去掉盘符后仍含 ':'。

    判据精确：仅"首2字符为盘符"（C:\\...）算合法前缀，其后出现的 ':' 是 ADS
    分隔符——`a.txt:notes` 会被 open 写进 a.txt 的隐藏数据流（报"已写入"但
    目录/Glob 看不见，对既有文件还会静默附加内容）。\\\\?\\ 与 \\.\\ 显式前缀
    路径先剥离前缀再判（与保留名拦截的豁免保持一致）。
    盘符相对形式（C:a.txt）不误伤：去掉盘符后无 ':'。
    """
    if os.name != "nt":
        return False
    p = file_path
    if p.startswith(("\\\\?\\", "\\\\.\\")):
        p = p[4:]
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    return ":" in p


# 临时文件名的候选上限：tempfile 模块的默认临时文件助手在 ACL 拒绝写入的目录里
# 挂死——Windows 的 os.access(W_OK) 只看只读属性、不看 ACL → 认为可写 → os.open
# 恒失败 → 换随机名重试到 TMP_MAX（2147483647 轮，实测单轮 0.08ms ≈ 17 万秒）。
# 只按"文件已存在"重试，且限定次数。
_TMP_NAME_MAX_ATTEMPTS = 20
_tmp_seq = itertools.count()


def _create_tmp_file(target_path: str) -> tuple:
    """在目标文件所在目录独占创建临时文件，返回 (tmp_path, fd)。

    与 tempfile 模块默认实现（重试到 TMP_MAX）的差别见 _TMP_NAME_MAX_ATTEMPTS 注释：
    重试次数受控，且"目录不可写"这类 OSError 立即抛出、不换名重试。目录不可写时
    PermissionError 直接上抛，由调用方转成可读错误。

    O_EXCL 保证独占：候选名撞车（FileExistsError）才换下一个序号。
    """
    directory = os.path.dirname(os.path.abspath(target_path)) or "."
    # 名字取目标名前 32 字符（唯一性由 pid+计数保证），避免长文件名把拼接后的
    # 临时文件名顶过单层文件名长度上限
    stem = os.path.basename(target_path)[:32]
    for _ in range(_TMP_NAME_MAX_ATTEMPTS):
        tmp_path = os.path.join(
            directory, f".{stem}.narnat_tmp_{os.getpid()}_{next(_tmp_seq)}")
        try:
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        return tmp_path, fd
    raise OSError(
        f"无法创建临时文件（{_TMP_NAME_MAX_ATTEMPTS} 个候选名均已被占用）: {directory}")


def _write_oserror_message(file_path: str, e: OSError, creating_tmp: bool = False) -> str:
    """写盘期 OSError → 可读错误文案（Write/Edit 共用）。

    creating_tmp=True 表示临时文件都没能创建出来：此时 PermissionError 只可能是
    目录不可写（ACL 拒写），裸 WinError 13 会被 AI 当成"文件被占用"反复重试。
    其余阶段的 OSError 保持原有文案不变。
    """
    if creating_tmp and isinstance(e, PermissionError):
        return f"[错误: 无法写入 {file_path}: 权限不足（目录不可写）]"
    return f"[错误: 写入失败: {e}]"


def execute(file_path: str, content: str,
            device: str = "") -> tuple:
    """
    创建或覆写文件。

    Args:
        file_path: 文件路径
        content: 完整文件内容
        device: 设备dev编号：dev0=本机（默认），dev1..devn=被控设备（需先Terminal connect）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示新建文件无需diff
    """
    device = _normalize_device_for_tools(device)
    if device is None:
        return (f"[错误: {_file_tool_device_hint()}]", "")

    # 路径含 NUL（JSON \u0000）：Win32 文件 API 抛未捕获 ValueError 逃出工具层，
    # 提前拒绝给出可读错误（本地与 device=devN 远程路径同一层拦截）
    if "\x00" in file_path:
        return ("[错误: 路径含 NUL 字符（\\0），非法文件路径]", "")

    if device:
        from ..terminal.remote import remote_write
        return remote_write(file_path, content, device)
    abs_path = os.path.abspath(file_path)

    # Windows 保留设备名（NUL/CON/PRN/COM1 等）：open() 会写进设备假成功或抛
    # 未捕获的 WinError，提前拒绝并说明真实原因。判据必须用原始路径的 basename
    # ——abspath 不可用：GetFullPathNameW 把保留名转成设备路径（\\.\NUL）并吞掉
    # basename；显式 \\?\ / \\.\ 前缀路径本身跳过保留名解析，不拦。
    name = os.path.basename(file_path)
    if (os.name == "nt" and not file_path.startswith(("\\\\?\\", "\\\\.\\"))
            and _is_nt_reserved_name(name)):
        return (f"[错误: '{name}' 是 Windows 保留设备名，不能作为普通文件写入: {file_path}]", "")

    # NTFS 备用数据流（ADS）：`a.txt:notes` 会被 open 写进 a.txt 的隐藏数据流，
    # 报"已写入"但资源管理器/Glob 都看不见，Edit 对该路径也必然失败——提前拒绝
    if _has_nt_ads_colon(file_path):
        return ("[错误: 路径含非法字符 ':'（Windows 中冒号后的部分会被写入备用数据流，"
                "目录不可见），请使用普通文件名]", "")

    # 目录路径：open()会报Permission denied，误导AI去查权限而非换路径，提前拦截给出真实原因
    if os.path.isdir(abs_path):
        return (f"[错误: {file_path} 是目录，请使用正确的文件路径]", "")

    # 自动创建父目录（盘不存在/超 MAX_PATH 等失败给出明确错误，不做未捕获抛出）
    parent = os.path.dirname(abs_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return (f"[错误: 无法创建父目录: {e}]", "")

    # 覆写已有文件时生成diff
    color_diff = ""
    old_content = ""
    old_bytes = None
    old_encoding = None
    write_encoding = "utf-8"    # 写回编码：默认 UTF-8，覆写时沿用原文件编码
    write_bom_prefix = ""       # 需前置的 BOM 字符（UTF-16 用 LE/BE 编解码器时手工补回）
    diff = ""
    if os.path.isfile(abs_path):
        try:
            # 旧内容编码探测（与Read一致）：GBK文件按utf-8-sig读会抛异常导致diff静默丢失，
            # AI 覆写后看不到差异。此处仅用于展示diff，errors=replace 不影响写入内容
            from ..read import (_detect_text_encoding, _sniff_bom_encoding,
                                _write_encoding_for)
            with open(abs_path, "rb") as fb:
                head = fb.read(8192)
                # BOM 命中的 UTF-16 文本同样含 NUL 字节，但它确实是文本：必须按文本
                # 读全文，否则既没有 diff 也拿不到原编码，写回只能退回 UTF-8
                if _sniff_bom_encoding(head) or not (head and b"\x00" in head):
                    # 仅文本文件读全文用于字节级变更判定（二进制只取首块，避免大文件占内存）
                    fb.seek(0)
                    old_bytes = fb.read()
            if old_bytes is not None:
                old_encoding = _sniff_bom_encoding(head) or _detect_text_encoding(head)
                # 写回保持原文件编码：固定按 UTF-8 写会把 UTF-16/BOM/GBK 文件静默
                # 改写编码形态（下游按原编码读取的工具链拿到坏数据，且无任何提示）
                write_encoding, write_bom_prefix = _write_encoding_for(head, old_encoding)
                old_content = old_bytes.decode(old_encoding, errors="replace")
                diff = _make_diff(old_content, content, file_path)
                color_diff = colorize_diff(diff)
        except Exception:
            pass
    else:
        # 新建文件也要出 diff。只给「(N字节)」会怎样（实测）：AI 写了 866 行、
        # 31KB 的文件，面板上只显示「已写入 X (31954字节)」，一行代码都看不到——
        # 用户必须去编辑器打开才知道它写了什么，面板等于没有呈现改动的能力。
        # 新建等价于「空文件 → 全部新增」，直接按这个语义生成 diff。
        try:
            diff = _make_diff("", content, file_path)
            color_diff = colorize_diff(diff)
        except Exception:
            pass

    try:
        data = (write_bom_prefix + content).encode(write_encoding)
    except UnicodeEncodeError:
        # 原编码表示不了新内容（GB18030 覆盖全 Unicode，实践上只剩孤立代理项这类
        # 连 UTF-8 也编不了的情况）：退回 UTF-8 写盘并由 _describe_form_drift
        # 明确告知编码变更，不静默改编码；UTF-8 也编不了则直接报错、不碰原文件
        write_encoding, write_bom_prefix = "utf-8", ""
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as e:
            return (f"[错误: 写入失败: {e}]", "")
    tmp_path = ""
    try:
        # 临时文件 + 原子替换：并发读端（其它进程/编辑器重载）不会读到
        # "截断到一半"的文件（open("w") 的 truncate+write 非原子，读写并发
        # 实测 11.5% 撕裂；Edit 已用本方案，Write 对齐）
        tmp_path, fd = _create_tmp_file(abs_path)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # 保持原文件权限（与 Edit 同款；Windows 跳过——只读属性复制到
            # tmp 会让 replace 与清理双双失败）
            if os.name != "nt":
                try:
                    shutil.copymode(abs_path, tmp_path)
                except OSError:
                    pass
            last_err = None
            for i in range(3):
                try:
                    os.replace(tmp_path, abs_path)
                    break
                except OSError as e:
                    # 读端句柄持续持有目标时 replace 需要删除权限，可能瞬时被拒
                    last_err = e
                    time.sleep(0.01 * (i + 1))
            else:
                # 3 次均被拒（持续并发读端持有）：回退直写保证本次写入成功——
                # 原子保护在常规场景仍生效，极端并发下退化为直写（写成功优先，
                # 撕裂窗口仅存在于该读写竞态场景）
                try:
                    with open(abs_path, "wb") as f:
                        f.write(data)
                except OSError:
                    raise last_err
        finally:
            # 未消费的 tmp 一律清理（replace 成功时 tmp 已被移走，unlink 静默失败）
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except OSError as e:
        # tmp_path 仍为空 = 卡在临时文件创建（目录不可写），文案据此区分
        return (_write_oserror_message(file_path, e, creating_tmp=not tmp_path), "")

    new_bytes = data
    byte_count = len(new_bytes)

    # 变更判定基于字节比较：_make_diff的splitlines()会抹平行尾符与末尾换行差异，
    # 据其报"无实质修改"会在文件已被改写（CRLF静默变LF）时给出假报告
    if diff == "[无差异]" and old_bytes is not None:
        if new_bytes == old_bytes:
            # 覆写内容与现有内容相同：文件已照常写盘，但明确告知AI本次无实质修改
            # （此前返回"[已写入...]\n[无差异]"，首行误导AI以为写入成功）
            return ("[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]", color_diff)
        detail = describe_bytes_only_change(old_bytes, new_bytes)
        return (f"[提示: 文件已写入，正文内容相同，但字节层面有变化（{detail}）]",
                colorize_diff(f"[正文相同，字节变化] {detail}"))
    if diff:
        tip = ""
        if old_bytes is not None:
            drift = _describe_form_drift(old_bytes, old_encoding, old_content, content,
                                         write_encoding)
            if drift:
                tip = f"（注意: {drift}）"
        return (f"[已写入: {file_path} ({byte_count}字节)]{tip}\n{diff}", color_diff)
    return (f"[已写入: {file_path} ({byte_count}字节)]", color_diff)


def _describe_form_drift(old_bytes: bytes, old_encoding, old_content: str, content: str,
                         write_encoding: str = "utf-8") -> str:
    """正文有变化时，描述"正文之外"的文件形态（编码/BOM/行尾符）。

    写回默认沿用原文件编码：原编码表示不了新内容时退回 UTF-8（编码形态被改变），
    行尾也可能由 CRLF 变为 LF。"正文相同"分支已有字节变化提示，正文不同时此前
    只回 diff、对形态漂移零提示——AI/用户会误以为"只改了那几行"，按原编码读取的
    下游工具链静默拿到坏数据。无形态变化时返回空串。
    """
    notes = []
    if old_encoding == "utf-16" and write_encoding not in ("utf-16-le", "utf-16-be"):
        notes.append("编码 UTF-16→UTF-8（新内容含原编码无法表示的字符）")
    elif old_encoding in ("gbk", "gb18030") and write_encoding == "utf-8":
        notes.append("编码 GBK→UTF-8（新内容含原编码无法表示的字符）")
    if old_bytes.startswith(b"\xef\xbb\xbf") and write_encoding != "utf-8-sig":
        notes.append("UTF-8 BOM 已移除")
    if old_encoding == "utf-16" and write_encoding in ("utf-16-le", "utf-16-be"):
        # 编码保持是最容易被误判为"改了"的形态（面板 diff 只显示文本），显式告知
        notes.append(f"编码保持 {write_encoding.upper()}，BOM 保留")
    if "\r\n" in old_content and "\r\n" not in content and "\n" in content:
        notes.append("行尾符 CRLF→LF")
    elif "\r\n" in content and "\r\n" not in old_content and "\n" in old_content:
        notes.append("行尾符 LF→CRLF")
    return "、".join(notes)


def _make_diff(old_content: str, new_content: str, file_path: str) -> str:
    """生成unified diff"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{os.path.basename(file_path)}",
        tofile=f"b/{os.path.basename(file_path)}",
        lineterm="",
    )
    result = "\n".join(diff)
    return result if result else "[无差异]"
