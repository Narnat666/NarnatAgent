"""Edit工具 —— 精确修改文件内容

字符串精确替换: Edit(file_path, old_string, new_string)
- old_string 必须精确匹配文件内容
- replace_all=True 替换所有匹配，默认只替换第一处（多处匹配且未设replace_all时报错）
- 自动兼容 \r\n 和 \n 换行符

设备语义: device=dev0或省略 → 本机(dev0)；device=dev1..devn → 被控设备(需先Terminal connect)。
"""

import os
import shutil
import time
import difflib

from typing import Optional

from ..diff_utils import colorize_diff
from ..param_utils import to_bool
from ..terminal import _normalize_device_for_tools, _file_tool_device_hint
from ..write import _create_tmp_file, _has_nt_ads_colon, _write_oserror_message

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": (
            "编辑UTF-8文本文件（字符串精确替换）。支持本地或远程编辑文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径。"},
                "old_string": {"type": "string", "description": "待替换的原文，须与文件内容精确匹配。"},
                "new_string": {"type": "string", "description": "替换后的新文本。"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换全部匹配。默认否。",
                },
                "device": {
                    "type": "string",
                    "description": "设备编号。默认dev0编辑指定的本地文件，设置dev1..devn则编辑指定的远程设备文件。",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
}


CAPABILITY = {
    "label": "编辑",
    "dispatch": "write",
    "summary": "file_path",
    "trusted_output": True,
}


def _read_for_edit(file_path: str) -> tuple:
    """读取文件内容用于编辑，返回 (content, 写回编码)。

    只处理 UTF-8 文本（原文件有 BOM 就保留 BOM）。Edit 会把内容写回文件，
    编码判断一旦出错就是静默数据损坏（错字写回、原字节永久丢失），所以只
    接受能严格验证的 UTF-8。与 Write 的能力声明一致：两者都只做 UTF-8。

    Raises:
        ValueError: 二进制文件（含 NUL 字节）
        UnicodeDecodeError: 不是 UTF-8（含 UTF-16 等其它编码）
    """
    from ..read import (_HEAD_SNIFF_BYTES, _detect_text_encoding,
                        _sniff_bom_encoding, _write_encoding_for)
    with open(file_path, "rb") as fb:
        head = fb.read(_HEAD_SNIFF_BYTES)
    # BOM 优先于 NUL 判定：UTF-16 文本的 ASCII 字符含 NUL，先判 NUL 会把它
    # 报成「二进制」。先识别出来，再按「不是 UTF-8」拒绝——这句更准确，
    # 因为文件确实是文本，只是编码不支持。
    if _sniff_bom_encoding(head):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "utf-16 not supported")
    if b"\x00" in head:
        raise ValueError("binary")
    encoding = _detect_text_encoding(head)
    if encoding is None:
        # 探测不出编码：猜一种（如 GB18030）会把错字写回文件，原字节永久丢失
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "encoding not determined")
    with open(file_path, "r", encoding=encoding, newline="") as f:
        content = f.read()
    # 写回形态：原文件有 BOM 就保留，没有就不加
    write_encoding, bom_prefix = _write_encoding_for(head, encoding)
    return bom_prefix + content, write_encoding


def execute(file_path: str, old_string: str = "", new_string: Optional[str] = None,
            replace_all: bool = False,
            device: str = "") -> tuple:
    """
    修改文件内容。

    字符串模式: Edit(file_path, old_string="...", new_string="...")

    Args:
        file_path: 文件路径
        old_string: 要替换的原文（必须精确匹配）
        new_string: 替换后的新文本（必填；删除匹配文本需显式传空字符串 ""）
        replace_all: 替换所有匹配（默认只替换第一个）
        device: 设备dev编号：dev0=本机（默认），dev1..devn=被控设备（需先Terminal connect）

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示
    """
    # new_string 缺参/显式 null 防御：默认空串会把"漏传"静默变成"删除匹配文本"
    # 且返回成功文案，造成不可回溯的数据破坏（LLM 偶发漏参/压缩后参数丢失）。
    # 删除语义必须显式传空字符串。
    if new_string is None:
        return ('[错误: 缺少参数 new_string]', "")

    # 归一化布尔参数: LLM 偶发传 "false" 字符串，bool("false") 恒为 True
    # 会导致全量替换（与意图相反的危险行为），必须按字符串语义解析
    replace_all = to_bool(replace_all)

    device = _normalize_device_for_tools(device)
    if device is None:
        return (f"[错误: {_file_tool_device_hint()}]", "")

    if device:
        from ..terminal.remote import remote_edit
        return remote_edit(file_path, old_string, new_string, replace_all, device)

    # 含 ':' 的路径是 NTFS 备用数据流语法（非普通文件）：原子替换必然 WinError 123，
    # 此前只回原始系统错误、不说明原因（与 Write 的 ADS 拦截同款判据）
    if _has_nt_ads_colon(file_path):
        return (f"[错误: 路径含非法字符 ':': {file_path}]", "")

    if not os.path.isfile(file_path):
        # 目录路径单独给出准确文案（与 Read/Write 对齐）：报"不存在"会误导 AI
        # 改用 Write 创建（Write 对目录同样拒绝），陷入二次错误
        if os.path.isdir(file_path):
            return (f"[错误: {file_path} 是目录]", "")
        # 先细分真实原因：权限不足会被 isfile 吞成 False 误报"不存在"
        from ..read import _isfile_miss_reason
        miss_reason = _isfile_miss_reason(file_path)
        if miss_reason:
            return (miss_reason, "")
        return (f"[错误: 文件不存在: {file_path}（当前目录: {os.getcwd()}）]", "")

    try:
        content, write_encoding = _read_for_edit(file_path)
    except PermissionError:
        return (f"[错误: 权限不足: {file_path}]", "")
    except OSError as e:
        return (f"[错误: 读取失败: {e}]", "")
    except UnicodeDecodeError:
        return (f"[错误: 不是 UTF-8: {file_path}]", "")
    except ValueError:
        return (f"[错误: 二进制文件: {file_path}]", "")

    return _edit_by_string(content, old_string, new_string, replace_all, file_path,
                           write_encoding)


def _edit_by_string(content: str, old_string: str, new_string: str,
                    replace_all: bool, file_path: str,
                    write_encoding: str = "utf-8") -> tuple:
    """字符串精确替换，自动兼容换行符"""
    if not old_string:
        return ("[错误: old_string不能为空]", "")

    # 检测文件换行符风格，转换 old_string/new_string 以匹配。
    # 顺序回退：原样 → 文件全局风格归一化 → 全 LF。混合换行文件（CRLF 与
    # LF 混存）中纯 LF 段落必须靠"原样/全 LF"命中：此前按"文件含 CRLF"一刀
    # 切把整段匹配请求转成 CRLF 形态，导致 LF 段落永远"未找到匹配"（AI 从
    # Read 看到的正是 LF 形态，出现相似度 100% 却报未找到的诡异现场）。
    has_crlf = '\r\n' in content
    if has_crlf:
        _normalize = lambda s: s.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
    else:
        _normalize = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')
    _to_lf = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')

    old_string_normalized = None
    new_string_normalized = None
    count = 0
    for cand_old, cand_new in (
        (old_string, new_string),
        (_normalize(old_string), _normalize(new_string)),
        (_to_lf(old_string), _to_lf(new_string)),
    ):
        c = content.count(cand_old)
        if c:
            old_string_normalized, new_string_normalized, count = cand_old, cand_new, c
            break

    if count == 0:
        hint = _find_similar(content, old_string)
        return (f"[错误: 未找到匹配文本]\n{hint}", "")

    if count > 1 and not replace_all:
        return (f"[错误: 找到{count}处匹配，old_string 不唯一]", "")

    if replace_all:
        new_content = content.replace(old_string_normalized, new_string_normalized)
    else:
        new_content = content.replace(old_string_normalized, new_string_normalized, 1)

    return _write_and_diff(content, new_content, file_path, count if replace_all else 1,
                           write_encoding=write_encoding)


def _write_and_diff(old_content: str, new_content: str, file_path: str,
                    count: int, write_encoding: str = "utf-8") -> tuple:
    """写回文件并生成diff。

    Returns:
        (llm_result, color_diff) 元组:
        - llm_result: 纯文本确认信息+diff，传给LLM
        - color_diff: 着色diff，传给终端展示；空串表示无差异
    """
    try:
        data = new_content.encode(write_encoding)
    except UnicodeEncodeError:
        # 新内容含文件编码无法表示的字符（原编码写入失败时）：先编码后写盘，
        # 编码失败时文件未被触碰，不静默改成 UTF-8
        return (f"[错误: 新内容包含文件编码({write_encoding})无法表示的字符，写入失败: {file_path}]", "")
    tmp_path = ""
    try:
        # 临时文件 + 原子替换：任何写入中途的异常都不破坏原文件
        tmp_path, fd = _create_tmp_file(file_path)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # 保持原文件权限：临时文件在 POSIX 下创建为 0600，直接替换
            # 会把受限权限带给原文件（如编辑可执行脚本后丢失 +x）。
            # Windows 不做：只读属性复制到 tmp 会让 replace 与清理双双失败，
            # 在"目标只读"场景残留只读 tmp（且 replace 失败语义与原 open("w") 一致）
            if os.name != "nt":
                try:
                    shutil.copymode(file_path, tmp_path)
                except OSError:
                    pass  # 目标不存在或权限查询失败：按临时文件默认权限替换
            last_err = None
            for i in range(3):
                try:
                    os.replace(tmp_path, file_path)
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
                    with open(file_path, "wb") as f:
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

    if old_content == new_content:
        # 空编辑提醒：文件已照常写盘，但明确告知AI本次无实质修改
        # （此前返回"[已替换1处]\n[无差异]"，首行误导AI以为编辑成功）
        return ("[提示: 新旧内容相同，文件无实质修改]", _make_color_diff("[无差异]"))

    diff = _make_diff(old_content, new_content, file_path)
    llm_result = f"[已替换{count}处]\n{diff}"

    color_diff = _make_color_diff(diff)
    return (llm_result, color_diff)


def _find_similar(content: str, old_string: str) -> str:
    """查找相似行，帮助LLM定位"""
    content_lines = content.splitlines()
    old_lines = old_string.strip().splitlines()
    if not old_lines:
        return ""

    target = old_lines[0].strip()
    similarities = []
    for i, line in enumerate(content_lines):
        ratio = difflib.SequenceMatcher(None, target, line.strip()).ratio()
        if ratio > 0.5:
            similarities.append((ratio, i + 1, line))

    if not similarities:
        return ""

    similarities.sort(reverse=True)
    hints = ["相似行:"]
    for _ratio, line_num, line in similarities[:3]:
        hints.append(f"  行{line_num}: {line.strip()}")
    return "\n".join(hints)


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


def _make_color_diff(diff_text: str) -> str:
    """对着色diff调用ui层着色函数，返回ANSI着色文本"""
    return colorize_diff(diff_text)
