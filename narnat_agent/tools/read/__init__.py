"""Read工具 —— 读取文件内容，带行号

默认上限2000行，超出自动截断并提示。limit需为正整数。最终输出由系统全局上限控制。

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import os
import re

from ..terminal import _normalize_device_for_tools, _file_tool_device_hint


def _apply_global_cap(text: str, _tool_context) -> str:
    """全局输出上限按行截断（保留完整行+行号），并给出offset续读提示。

    注册表的全局截断策略(保留首尾)面向Shell类输出——尾部含提示符，
    对Read不适用：中间截断丢失行号连续性，AI无法判断从哪行续读。
    此处按行截断并明确告知续读offset。
    """
    max_chars = getattr(_tool_context, "max_tool_output_chars", 0) if _tool_context else 0
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    lines = text.split("\n")
    budget = max(max_chars - 200, 300)  # 为提示信息预留空间
    kept = []
    used = 0
    for ln in lines:
        cost = len(ln) + 1
        if kept and used + cost > budget:
            break
        kept.append(ln)
        used += cost

    # 定位最后显示的完整行号（行号格式 "  N→..."），给出精确续读offset
    last_num = None
    for ln in reversed(kept):
        m = re.match(r"^\s*(\d+)→", ln)
        if m:
            last_num = int(m.group(1))
            break
    if last_num is not None:
        hint = (f"... [已达全局输出上限({max_chars}字符)，仅显示前{len(kept)}行。"
                f"使用 offset={last_num + 1} 继续读取其余部分]")
    else:
        hint = f"... [已达全局输出上限({max_chars}字符)，使用 offset 参数继续读取其余部分]"
    return "\n".join(kept) + "\n" + hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "读取纯文本文件内容，返还内容带行号（由1开始）。不可读取二进制文件。"
            "自动识别UTF-8/GBK编码。"
            "支持本地或远程读取文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "offset": {"type": "integer", "description": "起始行（默认1，含本行）"},
                "limit": {"type": "integer", "description": "读取行数（正整数，默认2000）"},
                "device": {"type": "string", "description": "设备dev编号：默认dev0（可省略）读取本机文件，设置dev1..devn则读取被控设备文件（需先Terminal connect被控设备获取dev编号）"},
            },
            "required": ["file_path"],
        },
    },
}


def _detect_text_encoding(head: bytes) -> str:
    """utf-8 严格解码成功 → utf-8-sig；失败 → gbk。

    中文Windows环境GBK文件常见（旧日志/导出文件）。此前固定utf-8+replace解码
    会把GBK内容变成大片U+FFFD乱码，AI读到的是坏数据。此处用首块字节判定编码。

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


def execute(file_path: str, offset: int = 0, limit: int = 2000,
            device: str = "",
            _tool_context=None) -> str:
    """
    读取文件内容。

    Args:
        file_path: 文件绝对路径
        offset: 起始行号(1-based)，0表示从头读
        limit: 最大行数，正整数，默认2000
        device: 设备dev编号：dev0=本机（默认），dev1..devn=被控设备（需先Terminal connect）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        带行号的文件内容字符串，格式 "  行号→内容"
    """
    # AI可能传字符串类型的数值参数，确保类型正确并处理None
    offset = int(offset) if offset is not None else 0
    limit = int(limit) if limit is not None else 2000
    if limit <= 0:
        return "[错误: limit需为正整数]"

    device = _normalize_device_for_tools(device)
    if device is None:
        return f"[错误: {_file_tool_device_hint()}]"

    if device:
        from ..terminal.remote import remote_read
        result = remote_read(file_path, offset, limit, device)
        # 仅错误结果不标记已读；不能用 "错误" in result 判断——文件内容本身
        # 含"错误"字样时会导致已读文件被误判为未读，Write覆写保护误伤
        if not result.startswith("[错误") and _tool_context:
            _tool_context.mark_remote_read(file_path, device)
        # 成功内容结果加设备头：多设备并行Read时AI可区分结果归属
        # （本地Read不带头，与远程结果形态天然区分，避免张冠李戴）
        if result and not result.startswith("["):
            result = f"[{device}] {file_path}\n{result}"
        return _apply_global_cap(result, _tool_context)
        
    if os.path.isdir(file_path):
        return f"[错误: {file_path} 是目录，请用 Glob 匹配或 Shell(eza -la) 查看目录内容]"

    if not os.path.isfile(file_path):
        # 相对路径解析依赖当前目录（Shell cd会改变它），报错时带上cwd帮AI一次定位
        return f"[错误: 文件不存在: {file_path}（当前目录: {os.getcwd()}）]"

    # 统一转为绝对路径，供 _tool_context 标记和校验
    abs_path = os.path.abspath(file_path)

    try:
        # 二进制检测：首块含NUL字节即视为二进制文件（与Grep检测策略一致）
        with open(file_path, "rb") as fb:
            head_bytes = fb.read(8192)
    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    if b"\x00" in head_bytes:
        return "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请使用Shell工具处理]"

    # 编码探测（GBK回退，避免中文Windows下GBK文件读成U+FFFD乱码）
    encoding = _detect_text_encoding(head_bytes)

    try:
        with open(file_path, "r", encoding=encoding, errors="replace") as f:
            start = max(offset - 1, 0) if offset > 0 else 0

            # 流式跳过 offset 行，避免大文件内存溢出；统计实际跳过的行数
            skipped = 0
            for _ in range(start):
                if not f.readline():
                    break
                skipped += 1

            # 按需读取 limit 行
            result = []
            truncated_by_limit = False

            for i in range(limit):
                line = f.readline()
                if not line:
                    break  # 文件结束

                line_num = start + i + 1
                content = line.rstrip("\n\r")
                formatted = f"  {line_num}→{content}"

                result.append(formatted)
            else:
                # for...else: 循环正常结束（没有 break），说明读完了 limit 行
                # 此时再尝试读一行，如果非空，说明文件还有内容，被 limit 截断了
                if f.readline():
                    truncated_by_limit = True

    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # 标记文件已被Read（供Write检查）——读取成功后才标记。
    # 二进制/权限错误路径在此之前已return：若提前标记，
    # "Read报错但Write覆写保护被绕过"，AI未看到内容却能覆写文件
    if _tool_context:
        _tool_context.mark_read(abs_path)

    # 空结果提示（offset超出末尾 / 空文件）
    if not result:
        if offset and offset > 1:
            return f"[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]"
        return "[文件为空]"

    # 截断提示
    if truncated_by_limit:
        result.append(f"  ... [截断: 已显示 {limit} 行。使用 offset={start + limit + 1} 参数可读取其余部分]")

    return _apply_global_cap("\n".join(result), _tool_context)
