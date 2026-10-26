"""Read工具 —— 读取文件内容，带行号；图片文件自动转为可视图

默认上限2000行，超出自动截断并提示。limit需为正整数。最终输出由系统全局上限控制。

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。

图片语义: 读到图片文件时不再是文本，返回句柄文本（文件名、格式、尺寸、副本路径、
内容指纹）并附一张归一化副本。副本落在 <data_dir>/attachments/，与用户原文件解耦——
原文件之后被删/改名都不影响对话。图片引用挂在返回值的 .images 属性上（见 ImageBearingStr），
由 agent_loop 追加一条 user 消息携带；文本文件的返回值与旧实现逐字节一致。
"""

import errno
import io
import os
import re

from ..terminal import _normalize_device_for_tools, _file_tool_device_hint
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
    此处按行截断并明确告知续读offset。

    提示只报事实：已显示到第几行（行号来自正文，可验证）、文件共几行。
    不写 token 估算（那是指数级近似，不是事实）、不写剩余行数（总行数减去
    已显示行号即可得出，重复）。
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

    # 定位正文首/末行的行号（格式 "  N→..."），给出准确区间与续读 offset
    first_num = last_num = None
    for ln in kept:
        m = re.match(r"^\s*(\d+)→", ln)
        if m:
            first_num = int(m.group(1))
            break
    for ln in reversed(kept):
        m = re.match(r"^\s*(\d+)→", ln)
        if m:
            last_num = int(m.group(1))
            break
    if last_num is not None:
        # 区间起点取自正文，不写死 1：offset=500 时正文从 500 行起，
        # 写「1-535」会报错的区间，AI 据此续读会漏掉 500-534。
        head = f"已显示 {first_num}-{last_num} 行"
        if total_lines is not None:
            head += f"，文件共 {total_lines} 行"
        hint = f"... [{head}。用 offset={last_num + 1} 继续]"
    else:
        hint = "... [已达输出上限。用 offset 继续]"
    return "\n".join(kept) + "\n" + hint

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "读取UTF-8文本文件，返回带行号的内容。"
            "支持读取图片（png/jpg/jpeg/webp/gif）。"
            "支持本地或远程读取文件。"
            "offset/limit 仅对文本有效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径。"},
                "offset": {"type": "integer", "description": "起始行，从1开始。默认1。"},
                "limit": {"type": "integer", "description": "最多返回的行数。默认2000。"},
                "device": {
                    "type": "string",
                    "description": "设备编号。默认dev0读取指定的本地文件，设置dev1..devn则读取指定的远程设备文件。",
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
    """BOM 嗅探：返回 "utf-16" 或 None。

    只用于「识别出这是 UTF-16 并拒绝」，不用于正常读取（Read 只读 UTF-8）。
    Grep/Write 仍需要它：Grep 用它避免把 UTF-16 误判成二进制，Write 用它
    避免把含 NUL 的 UTF-16 当二进制而丢 diff。
    """
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    return None


def _detect_text_encoding(head: bytes) -> str:
    """严格 UTF-8 解码成功 → "utf-8-sig"；否则 None。

    只认能百分百确定的一件事：严格解码通过的 UTF-8（BOM 由 utf-8-sig 吸收）。
    其余一律返回 None，由调用方报「不是 UTF-8」。

    不回退 gb18030：GB18030 是 GBK 的超集，几乎任何字节序列都能解出字符
    （连随机二进制都能解成一串合法汉字），因此「解得出」不构成「是这个编码」的
    证据。实测同一串字节（UTF-16LE「中文内容测试」与 GBK「-N噀匭筟Km諎」）
    两种读法都能严格通过 —— 判据根本不存在。把猜测当结论输出，AI 会拿错字
    当原文回写，比读不出来严重得多。

    不嗅探 UTF-16 BOM：Read 只读 UTF-8。虽然 BOM 能确定字节序，但 Write/Edit
    都只写 UTF-8，单方面支持读取会造成「读得到、改不了」的不对称 —— AI 会以为
    文件可改，实际每次编辑都失败。

    尾部窗口重试: 首块8KB可能恰好多字节序列边界截断，utf-8严格解码在截断处
    抛错会误判。**仅当首块读满 8KB**（确认是被窗口切开而非文件本身如此）时，
    才在 UnicodeDecodeError.start 位于末尾 3 字节内切除重试。不加这个前置条件
    会误判短文件：实测 4 字节的 UTF-16LE「中文」= 2D 4E 87 65，e.start=2 落在
    末尾 3 字节内 → 切成「-N」解码成功 → 整个文件被当成 UTF-8，读出来是
    「-N\uFFFD e」这种半截乱码。
    """
    trial = head
    full_window = len(head) >= _HEAD_SNIFF_BYTES
    for _ in range(3):
        try:
            trial.decode("utf-8")
            return "utf-8-sig"
        except UnicodeDecodeError as e:
            if full_window and e.start >= len(trial) - 3:
                trial = head[: e.start]  # 边界截断 → 切掉错误起点后重试
                continue
            break
    return None


def _write_encoding_for(head: bytes, encoding: str) -> tuple:
    """写回形态 → (编解码器, 需前置的 BOM 字符)。

    Edit/Write 都只处理 UTF-8，所以这里只需决定「原文件有 BOM 要不要保留」：
    读一遍再写回时丢掉 BOM，会让按原形态读取的下游工具链拿到不同的内容。
    """
    if encoding == "utf-8-sig":
        return ("utf-8-sig", "") if head.startswith(b"\xef\xbb\xbf") else ("utf-8", "")
    return encoding, ""


# 首块嗅探字节数：二进制判定、编码探测、BOM 嗅探共用这一个窗口（唯一真源）
_HEAD_SNIFF_BYTES = 8192


def _isfile_miss_reason(file_path: str) -> str:
    """os.path.isfile 为 False 时细化真实原因（Read/Edit 共用）。

    返回 ""=确实不存在（调用方保持原文案）；非空=替代错误文案。
    isdir/isfile 内部的 stat 异常被吞成 False，系统保护文件（权限不足）
    会误报"文件不存在"；此处用 os.stat 探测按异常分类区分。

    ENOENT/ENOTDIR 一律返回 ""：这两个 errno 无法区分"不存在"与"路径过长"，
    断言任一种都会误导 AI。原来按长度 >=260 判"路径过长"是推断（文件真的
    不存在、路径恰好也长会走同一分支），且实测本环境 259 字符路径可正常
    读写，该分支本身可能永不触发 —— 已删除，改由调用方报「文件不存在」，
    那是 errno 的直译，属可确定的事实。
    """
    try:
        os.stat(file_path)
    except PermissionError as e:
        return f"[错误: 权限不足: {file_path}（{e}）]"
    except OSError as e:
        if getattr(e, "errno", None) in (errno.ENOENT, errno.ENOTDIR):
            return ""
        return f"[错误: 权限不足: {file_path}（{e}）]"
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


def _image_handle_text(file_path: str, ref: dict, root: str,
                       device: str = "") -> str:
    """图片句柄文本 —— 模型看到的"这张图是什么"的唯一依据。

    这段文本会永久留在会话 history 的 content 里（图片字节只在发送时挂到
    images 字段），所以它必须自包含：文件名、格式、尺寸、指纹、副本路径。
    模型据此知道：看的是缩放版还是原图、原图多大、想重新读该读哪里。

    不写 token 估算：那是指数级近似（见 core/vision_tokens.py），不是事实。

    device: 远程设备编号。远程图片的"源文件"在远端，本地只有归一化副本，
    必须标出来，否则模型会以为源路径在本机。
    """
    name = ref.get("name") or os.path.basename(file_path)
    w, h = ref.get("width") or 0, ref.get("height") or 0
    sw, sh = ref.get("source_width"), ref.get("source_height")

    lines = [
        "[图片] %s — %s %dx%d" % (
            name, ref.get("media_type") or "未知格式", w, h),
    ]
    if sw and sh:
        lines.append("原图 %dx%d，已缩放至长边 %d" % (sw, sh, max(w, h)))
    if device:
        lines.append("来源: 远程设备 %s" % device)
    lines.append("附件 %s" % ref["id"])
    lines.append("副本（只读）：%s" % _attachment.object_path(root, ref["id"][7:]))
    lines.append("源文件：%s%s" % (device + ":" if device else "", file_path))
    return "\n".join(lines)


def _read_image(file_path: str, _tool_context, device: str = "") -> str:
    """图片分支：归一化 + 落附件库 + 返回带 .images 的句柄文本。

    本地与远程走同一条归一化管线，区别只在"字节从哪来"：
    本地 open()，远程走 SFTP 把整份文件取回。之后的解码、缩放、入库、
    句柄文本完全一致——图片不需要分页，取回整份是正确的做法。

    任何失败都返回普通错误字符串（不挂图），与文本分支的错误风格一致，
    AI 拿到的是一条可读的失败原因，对话继续。
    """
    if not _attachment.pil_available():
        return "[错误: 缺少 Pillow，无法读取图片]"

    data_dir = getattr(_tool_context, "data_dir", "") if _tool_context else ""
    if not data_dir:
        return "[错误: 附件库路径未配置，无法读取图片]"
    root = _attachment.attachment_root(data_dir)

    if device:
        from ..terminal.remote import remote_read_bytes
        source, err = remote_read_bytes(file_path, _attachment.MAX_SOURCE_BYTES, device)
        if err:
            return err
    else:
        try:
            with open(file_path, "rb") as f:
                source = f.read(_attachment.MAX_SOURCE_BYTES + 1)
        except PermissionError:
            return "[错误: 权限不足: %s" % file_path + "]"
        except OSError as e:
            return "[错误: 读取失败: %s" % e + "]"
        if len(source) > _attachment.MAX_SOURCE_BYTES:
            return ("[错误: 图片 %s，超过 %s 上限: %s]"
                    % (_format_size(len(source)),
                       _format_size(_attachment.MAX_SOURCE_BYTES), file_path))

    ref = _attachment.store(root, source)
    if ref is None:
        # store 返回 None 的原因有五种（PIL 不可用 / 空或超字节上限 / 超像素上限 /
        # 格式不在 SUPPORTED_FORMATS / 解码异常），代码里无法区分是哪一种。
        # 只报可确定的事实：这个文件没解出图。罗列原因等于猜测。
        return f"[错误: 无法解码图片: {file_path}]"

    ref["name"] = os.path.basename(file_path)

    text = _image_handle_text(file_path, ref, root, device)
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

    # ── 图片分支（必须放在远程/本地分派之前）──
    # 按扩展名判定，真正是不是图片由 PIL 全解码决定（失败即返回错误文本）。
    # 位置有两层要求：
    #   1. 在二进制检测之前——图片首块必然含 NUL，走到那里会被误判成"二进制文件"；
    #   2. 在 device 分派之前——远程读文本那一路是行切分管线，走不到这里。
    # 本地与远程共用同一条归一化管线，区别只在字节从哪来（见 _read_image）。
    if file_path.lower().endswith(_IMAGE_EXTS):
        return _read_image(file_path, _tool_context, device)

    if device:
        from ..terminal.remote import remote_read
        result = remote_read(file_path, offset, limit, device)
        # 成功内容结果加设备头：多设备并行Read时AI可区分结果归属
        # （本地Read不带头，与远程结果形态天然区分，避免张冠李戴）
        if result and not result.startswith("["):
            result = f"[{device}] {file_path}\n{result}"
        return _apply_global_cap(result, _tool_context)

    if os.path.isdir(file_path):
        return f"[错误: {file_path} 是目录]"

    if not os.path.isfile(file_path):
        # 先细分真实原因：权限不足会被 isfile 吞成 False 误报"不存在"
        miss_reason = _isfile_miss_reason(file_path)
        if miss_reason:
            return miss_reason
        # 相对路径解析依赖当前目录（Shell cd会改变它），报错时带上cwd帮AI一次定位
        return f"[错误: 文件不存在: {file_path}（当前目录: {os.getcwd()}）]"

    try:
        # 二进制检测：首块含NUL字节即视为二进制文件（与Grep检测策略一致）
        with open(file_path, "rb") as fb:
            head_bytes = fb.read(_HEAD_SNIFF_BYTES)
    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # 判定顺序（与 Edit 一致）：先识别 BOM 声明的 UTF-16，再判二进制，再判编码。
    # 顺序不是随意定的：UTF-16 文本的 ASCII 字符含 NUL，先判 NUL 会把它报成
    # 「二进制文件」—— 而它确实是文本，只是编码不支持。两处报同一件事时必须
    # 用同一句话，否则 AI 对同一个文件会得到两个结论。
    if _sniff_bom_encoding(head_bytes):
        return f"[错误: 不是 UTF-8: {file_path}]"
    if b"\x00" in head_bytes:
        return f"[错误: 二进制文件: {file_path}]"
    encoding = _detect_text_encoding(head_bytes)
    if encoding is None:
        # 严格 UTF-8 解码失败：只报这个确定的事实。
        # 不猜是哪一种编码（判据不存在，见 _detect_text_encoding）。
        return f"[错误: 不是 UTF-8: {file_path}]"

    try:
        # 文本解码必须**全程严格**（errors="strict"），只有二进制判定才只看首块。
        # 两者范围不同，曾经混用过一次（判定只看 8KB、解码却 errors="replace"），
        # 结果：坏字节落在 8KB 之后就静默变成 U+FFFD —— AI 拿到坏数据却以为
        # 读到了原文，比读不出来严重得多。dsh 的写法是同一套：
        # BINARY_SAMPLE_BYTES 只作用于 NUL 采样，TextDecoder 始终 fatal: true。
        with open(file_path, "rb") as fb:
            f = io.TextIOWrapper(fb, encoding=encoding, errors="strict")
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
                except (OSError, UnicodeDecodeError):
                    # 尾部含坏字节 → 无法统计总行数；按「未知」降级，
                    # 不因此否定已读出的内容（那部分是按严格解码验证过的）
                    total_lines = None
            elif offset and offset > 1:
                # 未截断（读到EOF）：总行数 = 实际跳过 + 实际读出，零成本精确
                total_lines = skipped + len(result)

    except PermissionError:
        return f"[错误: 权限不足: {file_path}]"
    except UnicodeDecodeError as e:
        # 严格解码在读取途中失败：坏字节不在首块（否则前面就报了「不是 UTF-8」）。
        # 位置报块内偏移而不是猜绝对偏移 —— 只说「这份文件含坏字节」这个事实。
        return f"[错误: 不是 UTF-8: {file_path}（读取中途解码失败，块内偏移 {e.start}）]"
    except OSError as e:
        return f"[错误: 读取失败: {e}]"

    # 空结果提示（offset超出末尾 / 空文件）
    if not result:
        if offset and offset > 1:
            return f"[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]"
        return "[文件为空]"

    # 截断提示（只报事实：显示到第几行、文件共几行；剩余行数可算，不写）
    if truncated_by_limit:
        end_line = skipped + limit
        if total_lines is not None:
            result.append(
                f"  ... [已显示 {skipped + 1}-{end_line} 行，文件共 {total_lines} 行。"
                f"用 offset={end_line + 1} 继续]"
            )
        else:
            result.append(
                f"  ... [已显示 {skipped + 1}-{end_line} 行。用 offset={end_line + 1} 继续]"
            )

    return _apply_global_cap("\n".join(result), _tool_context, total_lines)
