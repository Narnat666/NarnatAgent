"""Write工具 —— 创建新文件或完整覆写文件

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import os
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
    diff = ""
    if os.path.isfile(abs_path):
        try:
            # 旧内容编码探测（与Read一致）：GBK文件按utf-8-sig读会抛异常导致diff静默丢失，
            # AI 覆写后看不到差异。此处仅用于展示diff，errors=replace 不影响写入内容
            from ..read import _detect_text_encoding
            with open(abs_path, "rb") as fb:
                head = fb.read(8192)
                if not (head and b"\x00" in head):
                    # 仅文本文件读全文用于字节级变更判定（二进制只取首块，避免大文件占内存）
                    fb.seek(0)
                    old_bytes = fb.read()
            if old_bytes is not None:
                encoding = _detect_text_encoding(head)
                old_content = old_bytes.decode(encoding, errors="replace")
                diff = _make_diff(old_content, content, file_path)
                color_diff = colorize_diff(diff)
        except Exception:
            pass

    try:
        with open(abs_path, "w", encoding="utf-8", newline='') as f:
            f.write(content)
    except OSError as e:
        return (f"[错误: 写入失败: {e}]", "")

    new_bytes = content.encode("utf-8")
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
