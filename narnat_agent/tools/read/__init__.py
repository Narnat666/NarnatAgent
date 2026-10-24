"""Read工具 —— 读取文件内容，带行号；图片文件自动转为可视图

默认上限2000行，超出自动截断并提示。limit需为正整数。最终输出由系统全局上限控制。

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。

图片语义: 读到图片文件时不再是文本，返回句柄文本（文件名、格式、尺寸、副本路径、
内容指纹）并附一张归一化副本。副本落在 <data_dir>/attachments/，与用户原文件解耦——
原文件之后被删/改名都不影响对话。图片引用挂在返回值的 .images 属性上（见 ImageBearingStr），
由 agent_loop 追加一条 user 消息携带；文本文件的返回值与旧实现逐字节一致。
"""

import errno
import os
import re

from ..terminal import _normalize_device_for_tools, _file_tool_device_hint
from ..token_estimate import estimate_text_tokens
from ...core import attachment as _attachment


class ImageBearingStr(str):
    """带图片引用的工具结果字符串。

    继承 str，所以 registry.execute 的 len() 硬截断、dispatcher 透传、
    append_tool_result、json.dump 全部照常工作，零改动；
    只有 agent_loop 回传结果时检查 .images，有图就额外追加一条 user 消息。

    images 为图片引用 list（见 attachment.make_ref），空 list 表示纯文本结果。
    """

    __slots__ = ("images",)

    def __new__(cls, value, images=None):
        obj = super().__new__(cls, value)
        obj.images = list(images or [])
        return obj


# ── 读取窗口常量 ──
# 单行显示上限（对齐官方 harness readMaxLineLength 默认值）：
# 一条几 MB 的无换行日志会撑爆整次输出，截断后 AI 至少能看到"行的轮廓"。
READ_MAX_LINE_CHARS = 2000
# 截断时统计总行数的文件大小上限：超过则跳过计数（避免秒级阻塞），提示不显示总行数。
READ_MAX_COUNT_BYTES = 20 * 1024 * 1024

# 图片文件按扩展名快速识别（真正是不是图片由 attachment.normalize 全解码判定，
# 扩展名只用来决定"要不要走图分支"——避免把每个文本文件都拿去试探解码）
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _apply_global_cap(text: str, _tool_context, total_lines: int = None) -> str:
    """全局输出上限按行截断（保留完整行+行号），并给出offset续读提示。

    注册表的全局截断策略(保留首尾)面向Shell类输出——尾部含提示符，
    对Read不适用：中间截断丢失行号连续性，AI无法判断从哪行续读。
    此处按行截断并明确告知续读offset（有总行数时同时给出剩余行数，
    让AI从"盲续"变"可规划"）。
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
        est = estimate_text_tokens(text)  # ≈token（AI预算单位，混合密度估算）
        if total_lines is not None:
            remain = f"，剩余{total_lines - last_num}行"
            hint = (f"... [已达全局输出上限({max_chars}字符≈{est}token)，仅显示前{len(kept)}行。"
                    f"文件共{total_lines}行{remain}。"
                    f"使用 offset={last_num + 1} 继续读取其余部分]")
        else:
            hint = (f"... [已达全局输出上限({max_chars}字符≈{est}token)，仅显示前{len(kept)}行。"
                    f"使用 offset={last_num + 1} 继续读取其余部分]")
    else:
        hint = f"... [已达全局输出上限({max_chars}字符)，使用 offset 参数继续读取其余部分]"
    return "\n".join(kept) + "\n" + hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "读取文件内容。文本文件返还带行号的内容（由1开始）；"
            "支持图片文件读取（png/jpg/jpeg/webp/gif）。"
            "支持本地或远程读取文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始行（默认1，含本行）"},
                "limit": {"type": "integer", "description": "读取行数（正整数，默认2000）"},
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即读取本地指定文件，设置dev1..devn则读取指定远程设备文件",
                },
            },
            "required": ["file_path"],
        },
    },
}


CAPABILITY = {
    "label": "读取",
    "dispatch": "readonly",
    "summary": "file_path",
    "trusted_output": True,
}


def _sniff_bom_encoding(head: bytes):
    """BOM 嗅探：返回 "utf-16"（Python 解码时按 BOM 自动定字节序）或 None。

    必须在二进制判定之前调用：UTF-16 文本的 ASCII 字符含 NUL 字节，会被
    "首块含 NUL"误判为二进制（Windows 生态常见：PowerShell 5.1 重定向输出、
    部分导出文件为 UTF-16LE）。无 BOM 的 UTF-16 不猜（与 ripgrep 一致）。
    """
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    return None


def _detect_text_encoding(head: bytes) -> str:
    """先嗅探BOM（命中 → utf-16）；否则 utf-8 严格解码成功 → utf-8-sig，失败 → gb18030。

    中文Windows环境GBK文件常见（旧日志/导出文件）。此前固定utf-8+replace解码
    会把GBK内容变成大片U+FFFD乱码，AI读到的是坏数据。此处用首块字节判定编码。

    fallback 用 gb18030 而非 gbk：GB18030 是 GBK 的超集（同一字符的两字节编码
    完全相同），而 gbk 编解码器解不了国标四字节序列（CJK 扩展 B 区生僻字，如
    b"\\x95\\x32\\x82\\x36"）——用 gbk 会把它们读成 U+FFFD，回写即永久丢失原文。

    尾部窗口重试: 首块8KB可能恰好多字节序列边界截断，utf-8严格解码在截断处
    抛错会误判为GBK。UnicodeDecodeError.start位于末尾3字节内时切除重试。
    """
    encoding = _sniff_bom_encoding(head)
    if encoding:
        return encoding
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
    return "gb18030"


def _write_encoding_for(head: bytes, encoding: str) -> tuple:
    """原文件编码 → (写回编解码器, 需前置的 BOM 字符)。

    读一遍再写回时必须保持原文件编码形态：固定按 UTF-8 写会把 UTF-16 文件静默
    改写成 UTF-8、把带 BOM 的 UTF-8 的 BOM 丢掉，按原编码读取的下游工具链拿到
    坏数据。utf-16-le/be 编解码器自身不写 BOM，需调用方在正文前置 U+FEFF 还原
    原 BOM 字节（LE=FF FE / BE=FE FF）。
    """
    if encoding == "utf-16":
        return ("utf-16-be" if head.startswith(b"\xfe\xff") else "utf-16-le"), "\ufeff"
    if encoding == "utf-8-sig":
        return ("utf-8-sig", "") if head.startswith(b"\xef\xbb\xbf") else ("utf-8", "")
    return encoding, ""


def _first_undecodable_offset(head: bytes, encoding: str) -> int:
    """首块按 encoding 严格解码，返回第一个无法解码字节的偏移；-1 = 全部可解码。

    连探测出的编码也解不了的字节此前被静默替换成 U+FFFD（AI 拿到的是坏数据还
    以为读到了原文）。末尾被 8KB 窗口切开的多字节序列不算无法解码：与
    _detect_text_encoding 的尾部边界截断重试同一判据（逐次切除尾部 1~3 字节）。
    """
    for cut in (0, 1, 2, 3):
        trial = head[: len(head) - cut] if cut else head
        if not trial:
            return -1
        try:
            trial.decode(encoding)
            return -1
        except UnicodeDecodeError as e:
            if e.start < len(trial) - 3:
                return e.start
    return -1


# Windows 传统路径长度上限（MAX_PATH）：超过后普通 API 一律报"找不到路径"，
# 无法与"确实不存在"区分，需按长度识别并给出可执行的提示。
_MAX_LEGACY_PATH_LEN = 260


def _isfile_miss_reason(file_path: str) -> str:
    """os.path.isfile 为 False 时细分真实原因（Read/Edit 共用）。

    返回 ""=确实不存在（调用方保持原文案）；非空=替代错误文案。
    isdir/isfile 内部的 stat 异常被吞成 False，系统保护文件（权限不足）、
    超长路径都会误报"文件不存在"；此处用 os.stat 探测按异常分类区分。
    """
    try:
        os.stat(file_path)
    except PermissionError as e:
        return f"[错误: 权限不足或路径无法访问: {file_path}（{e}）]"
    except OSError as e:
        if getattr(e, "errno", None) in (errno.ENOENT, errno.ENOTDIR):
            try:
                over_len = (os.name == "nt"
                            and len(os.path.abspath(file_path)) >= _MAX_LEGACY_PATH_LEN)
            except (OSError, ValueError):
                over_len = False
            if over_len:
                return (f"[错误: 路径过长无法访问: {file_path}"
                        f"（绝对路径达到 Windows {_MAX_LEGACY_PATH_LEN} 字符传统上限，"
                        f"可缩短路径，或用 Shell 以 \\\\?\\ 前缀访问）]")
            return ""
        return f"[错误: 权限不足或路径无法访问: {file_path}（{e}）]"
    except ValueError:
        return ""
    return ""


def _format_size(n: int) -> str:
    """字节数 → 人类可读（AI 也用得上：判断这张图是不是高清截图）"""
    if n >= 1024 * 1024:
        return "%.1fMB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%dKB" % int(n / 1024)
    return "%dB" % n


def _image_handle_text(file_path: str, ref: dict, root: str, token_est: int) -> str:
    """图片句柄文本 —— 模型看到的"这张图是什么"的唯一依据。

    这段文本会永久留在会话 history 的 content 里（图片字节只在发送时挂到
    images 字段），所以它必须自包含：文件名、格式、尺寸、体积、指纹、副本路径。
    模型据此知道：看的是缩放版还是原图、原图多大、想重新读该读哪里。
    """
    name = ref.get("name") or os.path.basename(file_path)
    w, h = ref.get("width") or 0, ref.get("height") or 0
    sw, sh = ref.get("source_width"), ref.get("source_height")

    lines = [
        "[图片] %s — %s %dx%d，约 %s token" % (
            name, ref.get("media_type") or "未知格式", w, h, token_est),
    ]
    if sw and sh:
        lines.append("原图 %dx%d，已缩放至长边 %d 发送（细节以副本为准）" % (
            sw, sh, max(w, h)))
    lines.append("附件 %s" % ref["id"])
    lines.append("副本（只读）：%s" % _attachment.object_path(root, ref["id"][7:]))
    lines.append("源文件：%s" % file_path)
    return "\n".join(lines)


def _read_image(file_path: str, _tool_context) -> str:
    """图片分支：归一化 + 落附件库 + 返回带 .images 的句柄文本。

    任何失败都返回普通错误字符串（不挂图），与文本分支的错误风格一致，
    AI 拿到的是一条可读的失败原因，对话继续。
    """
    if not _attachment.pil_available():
        return ("[错误: 当前环境缺少 Pillow，无法读取图片。"
                "请安装 Pillow，或改用 Shell 调用外部工具处理此文件]")

    data_dir = getattr(_tool_context, "data_dir", "") if _tool_context else ""
    if not data_dir:
        return "[错误: 附件库路径未配置，无法读取图片]"
    root = _attachment.attachment_root(data_dir)

    try:
        with open(file_path, "rb") as f:
            source = f.read(_attachment.MAX_SOURCE_BYTES + 1)
    except PermissionError:
        return "[错误: 权限不足: %s" % file_path + "]"
    except OSError as e:
        return "[错误: 读取失败: %s" % e + "]"

    if len(source) > _attachment.MAX_SOURCE_BYTES:
        return ("[错误: 图片文件超过 %s 上限，无法读取。"
                "请先用 Shell 缩小后再读]" % _format_size(_attachment.MAX_SOURCE_BYTES))

    ref = _attachment.store(root, source)
    if ref is None:
        return ("[错误: 无法解码图片（文件损坏、格式不支持或尺寸过大）。"
                "支持 PNG/JPEG/WebP/GIF；若确为图片请用 Shell 检查]")

    ref["name"] = os.path.basename(file_path)

    # token 估价（DeepSeek 视觉网格口径，见 core/vision_tokens.py）
    from ..vision_tokens import estimate_image_tokens
    token_est = estimate_image_tokens(ref.get("width") or 0, ref.get("height") or 0)

    text = _image_handle_text(file_path, ref, root, token_est)
    return ImageBearingStr(text, [ref])


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
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Shell容错风格一致）
    # OverflowError：LLM 输出 JSON 里的 1e999 会被 json.loads 解析成 float('inf')，
    # int(inf) 抛 OverflowError（ArithmeticError 子类），漏捕会逃出工具层
    try:
        offset = int(offset) if offset is not None else 0
        limit = int(limit) if limit is not None else 2000
    except (TypeError, ValueError, OverflowError):
        return "[错误: offset/limit需为整数]"
    if limit <= 0:
        return "[错误: limit需为正整数]"

    device = _normalize_device_for_tools(device)
    if device is None:
        return f"[错误: {_file_tool_device_hint()}]"

    if device:
        from ..terminal.remote import remote_read
        result = remote_read(file_path, offset, limit, device)
        # 成功内容结果加设备头：多设备并行Read时AI可区分结果归属
        # （本地Read不带头，与远程结果形态天然区分，避免张冠李戴）
        if result and not result.startswith("["):
            result = f"[{device}] {file_path}\n{result}"
        return _apply_global_cap(result, _tool_context)
        
    if os.path.isdir(file_path):
        return f"[错误: {file_path} 是目录，请用 Glob 匹配或 Shell 查看目录内容]"

    if not os.path.isfile(file_path):
        # 先细分真实原因：权限不足/超长路径会被 isfile 吞成 False 误报"不存在"
        miss_reason = _isfile_miss_reason(file_path)
        if miss_reason:
            return miss_reason
        # 相对路径解析依赖当前目录（Shell cd会改变它），报错时带上cwd帮AI一次定位
        return f"[错误: 文件不存在: {file_path}（当前目录: {os.getcwd()}）]"

    # ── 图片分支 ──
    # 按扩展名判定，真正是不是图片由 PIL 全解码决定（失败即返回错误文本）。
    # 位置必须在二进制检测之前：图片首块必然含 NUL，走到下面会被误判成"二进制文件"。
    # 远程设备（device 非空）不走此分支——图片归一化与附件库都在本机，
    # 远程读图仍按原有文本路径处理（返回二进制错误提示）。
    if (not device) and file_path.lower().endswith(_IMAGE_EXTS):
        return _read_image(file_path, _tool_context)

    try:
        # 二进制检测：首块含NUL字节即视为二进制文件（与Grep检测策略一致）
        with open(file_path, "rb") as fb:
            head_bytes = fb.read(8192)
    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # BOM 嗅探优先于二进制判定：UTF-16 文本的 ASCII 字符含 NUL 字节，
    # 会被"首块含 NUL"误判为二进制（判定顺序与 Grep 一致）
    encoding = _sniff_bom_encoding(head_bytes)
    if encoding is None:
        if b"\x00" in head_bytes:
            return "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请使用Shell工具处理]"
        # 编码探测（GB18030回退，避免中文Windows下GBK/GB18030文件读成U+FFFD乱码）
        encoding = _detect_text_encoding(head_bytes)

    # 编码探测本身也可能解不了某些字节：此时下方 errors="replace" 会静默填 U+FFFD，
    # AI 拿到坏数据却以为读到了原文（回写即永久丢失），必须先给出可读提示
    decode_hint = ""
    bad_at = _first_undecodable_offset(head_bytes, encoding)
    if bad_at >= 0:
        decode_hint = (
            f"[提示: 文件编码无法完全识别——第{bad_at}字节起无法按 {encoding} 解码，"
            f"无法解码的部分以 \ufffd 显示，内容可能不完整；"
            f"如需原样处理请用 Shell 工具]\n"
        )

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
                # 单行超长截断：保留前段 + 后缀，避免一条无换行巨行撑爆输出
                if len(content) > READ_MAX_LINE_CHARS:
                    content = (
                        content[:READ_MAX_LINE_CHARS]
                        + f"...[单行截断: 本行共{len(content)}字符,"
                          f"仅显示前{READ_MAX_LINE_CHARS}字符]"
                    )
                formatted = f"  {line_num}→{content}"

                result.append(formatted)
            else:
                # for...else: 循环正常结束（没有 break），说明读完了 limit 行
                # 此时再尝试读一行，如果非空，说明文件还有内容，被 limit 截断了
                if f.readline():
                    truncated_by_limit = True

            # limit 截断时统计文件总行数（供提示规划续读）。
            # 超大文件跳过计数（>20MB 扫描成本秒级）；异常放弃统计，提示降级。
            total_lines = None
            if truncated_by_limit:
                try:
                    if os.path.getsize(file_path) <= READ_MAX_COUNT_BYTES:
                        # 探测行已读掉，从下一行计起
                        total_lines = skipped + limit + 1 + sum(1 for _ in f)
                except OSError:
                    total_lines = None
            elif offset and offset > 1:
                # 未截断（读到EOF）：总行数 = 实际跳过 + 实际读出，零成本精确
                total_lines = skipped + len(result)

    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # 空结果提示（offset超出末尾 / 空文件）
    if not result:
        if offset and offset > 1:
            return f"[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]"
        return "[文件为空]"

    # 截断提示（含总行数：AI 可算剩余页数，从"盲续"变"可规划"）
    if truncated_by_limit:
        if total_lines is not None:
            remain = total_lines - (skipped + limit)
            result.append(
                f"  ... [截断: 已显示 {limit} 行。文件共{total_lines}行，剩余{remain}行。"
                f"使用 offset={skipped + limit + 1} 参数可读取其余部分]"
            )
        else:
            result.append(
                f"  ... [截断: 已显示 {limit} 行。使用 offset={skipped + limit + 1} 参数可读取其余部分]"
            )

    return _apply_global_cap(decode_hint + "\n".join(result), _tool_context, total_lines)
