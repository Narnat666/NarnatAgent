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
            "创建或全量覆盖UTF-8文本文件。支持本地或远程写入文件。"
            "覆写时保留原文件的 UTF-8 BOM，并返回 diff。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径。"},
                "content": {"type": "string", "description": "完整文件内容。"},
                "device": {
                    "type": "string",
                    "description": "设备编号。默认dev0写入指定的本地文件，设置dev1..devn则写入指定的远程设备文件。",
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
        return (f"[错误: 路径含非法字符 ':': {file_path}]", "")

    # 目录路径：open()会报Permission denied，误导AI去查权限而非换路径，提前拦截给出真实原因
    if os.path.isdir(abs_path):
        return (f"[错误: {file_path} 是目录]", "")

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
    write_bom = False       # 原文件带 UTF-8 BOM → 写回也带（与 Edit 一致）
    diff = ""
    if os.path.isfile(abs_path):
        try:
            # 旧内容探测：只用于生成 diff 与决定「BOM 要不要保留」，
            # 不参与编码决策 —— 写盘永远 UTF-8（见下方 content.encode("utf-8")）。
            from ..read import _detect_text_encoding, _sniff_bom_encoding
            with open(abs_path, "rb") as fb:
                head = fb.read(8192)
                # BOM 命中的 UTF-16 文本同样含 NUL 字节，但它确实是文本：
                # 必须按文本读全文，否则没有 diff
                if _sniff_bom_encoding(head) or not (head and b"\x00" in head):
                    fb.seek(0)
                    old_bytes = fb.read()
            if old_bytes is not None:
                # 探测不出编码时按 utf-8+replace 尽力展示 diff：
                # 这只影响面板/diff 的呈现，不改变写入字节
                old_encoding = _sniff_bom_encoding(head) or _detect_text_encoding(head)
                old_content = old_bytes.decode(old_encoding or "utf-8", errors="replace")
                diff = _make_diff(old_content, content, file_path)
                color_diff = colorize_diff(diff)
                # 原文件是「带 BOM 的 UTF-8」→ 写回也带 BOM。
                # 不保留会怎样（实测）：Edit 保留 BOM、Write 丢弃，同一文件
                # 走两条路得到两种字节形态，之后按原形态读取的下游工具链
                # 拿到的东西不一样；用户也会看到「我只改了内容，BOM 怎么没了」。
                if head.startswith(b"\xef\xbb\xbf"):
                    write_bom = True
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
        # BOM 保留：原文件是「带 BOM 的 UTF-8」时写回同样带 BOM
        data = (b"\xef\xbb\xbf" + content.encode("utf-8")) if write_bom \
            else content.encode("utf-8")
    except UnicodeEncodeError as e:
        # 内容含孤立代理项（连 UTF-8 也编不了）：直接报错、不碰原文件
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
            return ("[提示: 新旧内容字节级一致，文件无实质修改]", color_diff)
        detail = describe_bytes_only_change(old_bytes, new_bytes)
        return (f"[提示: 文件已写入，正文内容相同，但字节层面有变化（{detail}）]",
                colorize_diff(f"[正文相同，字节变化] {detail}"))
    if diff:
        return (f"[已写入: {file_path} ({byte_count}字节)]\n{diff}", color_diff)
    return (f"[已写入: {file_path} ({byte_count}字节)]", color_diff)


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
