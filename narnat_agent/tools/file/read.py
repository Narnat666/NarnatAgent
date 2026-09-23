"""Read 工具 —— 读取本地/远程纯文本文件内容，返还带行号文本。

契约来源：specs/tools-file「Read 参数契约」「Read 输出格式与行号」「Read 空结果与
文件级错误文案」「Read 编码识别与二进制拒绝」「Read 截断与续读协议」
「Read/Edit/Write 的设备参数契约」。

行为搬运自旧实现 `narnat_agent/tools/read/__init__.py`（参数归一、行格式化、单行截断、
编码探测调用、limit 截断与总行数统计、全局按行截断与续读提示逐字一致）；
结构重组：模块级 `execute` 函数 → `ReadTool`（`contracts.tool.Tool` 协议），
`_tool_context` → `env.settings`，`_detect_text_encoding` → `file.encoding` 共用单点，
设备语义与远程分支 → `file.devices` 端口。
"""
from __future__ import annotations

import os
import re

from ...contracts.tool import ToolDefinition, ToolEnv, ToolResult
from ..token_estimate import estimate_text_tokens
from .devices import DEFAULT_REMOTE, RemoteFiles, device_hint, normalize_device
from .encoding import detect_text_encoding
from .param_utils import bind_params, settings_of

__all__ = ["READ_DEFINITION", "READ_MAX_COUNT_BYTES", "READ_MAX_LINE_CHARS", "ReadTool", "apply_global_cap"]

# ── 读取窗口常量 ──
# 单行显示上限（对齐官方 harness readMaxLineLength 默认值）：
# 一条几 MB 的无换行日志会撑爆整次输出，截断后 AI 至少能看到"行的轮廓"。
READ_MAX_LINE_CHARS = 2000
# 截断时统计总行数的文件大小上限：超过则跳过计数（避免秒级阻塞），提示不显示总行数。
READ_MAX_COUNT_BYTES = 20 * 1024 * 1024

# 载入文件内容前的探测块大小（二进制判定 + 编码探测共用）。
READ_HEAD_BYTES = 8192

# 行号前缀格式（输出与全局截断的续读定位互为隐式契约）。
LINE_NUMBER_RE = re.compile(r"^\s*(\d+)→")

READ_PARAMS = ("file_path", "offset", "limit", "device")

READ_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": (
            "读取纯文本文件内容，返还内容带行号（由1开始）。"
            "自动识别UTF-8/GBK编码。"
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


def apply_global_cap(text: str, env: ToolEnv | None, total_lines: int | None = None) -> str:
    """全局输出上限按行截断（保留完整行+行号），并给出 offset 续读提示。

    注册表的全局截断策略（保留首尾）面向 Shell 类输出——尾部含提示符，
    对 Read 不适用：中间截断丢失行号连续性，AI 无法判断从哪行续读。
    此处按行截断并明确告知续读 offset（有总行数时同时给出剩余行数，
    让 AI 从"盲续"变"可规划"）。上下文缺失或上限 ≤0 时不截断。
    """
    settings = settings_of(env)
    max_chars = getattr(settings, "max_tool_output_chars", 0) if settings is not None else 0
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

    # 定位最后显示的完整行号（行号格式 "  N→..."），给出精确续读 offset
    last_num = None
    for ln in reversed(kept):
        m = LINE_NUMBER_RE.match(ln)
        if m:
            last_num = int(m.group(1))
            break
    if last_num is not None:
        est = estimate_text_tokens(text)  # ≈token（AI 预算单位，混合密度估算）
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


class ReadTool:
    """Read 工具实现（`contracts.tool.Tool` 协议）。

    `remote` 为远程文件访问端口（`device=devN` 分支使用；缺省为未装配退化实现）。
    """

    name = "Read"

    def __init__(self, remote: RemoteFiles | None = None) -> None:
        self._remote = remote if remote is not None else DEFAULT_REMOTE

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（与旧实现 DEFINITION 逐字节等价）。"""
        return READ_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """读取文件内容（本机或远程），返回带行号文本。

        - `offset`：起始行（0 或负数表示从头读，正数 N 表示从第 N 行开始且含该行）；
        - `limit`：最大行数（正整数，默认 2000）；
        - `device`：dev0=本机（默认），dev1..devn=被控设备（需先 Terminal connect）。
        """
        params = bind_params(args, READ_PARAMS, ("file_path",))
        file_path = params["file_path"]
        offset = params.get("offset")
        limit = params.get("limit")
        device = params.get("device", "")
        # AI 可能传字符串类型的数值参数，确保类型正确并处理 None
        offset = int(offset) if offset is not None else 0
        limit = int(limit) if limit is not None else 2000
        if limit <= 0:
            return ToolResult("[错误: limit需为正整数]")

        device = normalize_device(device)
        if device is None:
            return ToolResult(f"[错误: {device_hint(self._remote)}]")

        if device:
            result = self._remote.read(file_path, offset, limit, device)
            # 成功内容结果加设备头：多设备并行 Read 时 AI 可区分结果归属
            # （本地 Read 不带头，与远程结果形态天然区分，避免张冠李戴）
            if result and not result.startswith("["):
                result = f"[{device}] {file_path}\n{result}"
            return ToolResult(apply_global_cap(result, env))

        if os.path.isdir(file_path):
            return ToolResult(f"[错误: {file_path} 是目录，请用 Glob 匹配或 Shell 查看目录内容]")

        if not os.path.isfile(file_path):
            # 相对路径解析依赖当前目录（Shell cd 会改变它），报错时带上 cwd 帮 AI 一次定位
            return ToolResult(f"[错误: 文件不存在: {file_path}（当前目录: {os.getcwd()}）]")

        try:
            # 二进制检测：首块含 NUL 字节即视为二进制文件（与 Grep 检测策略一致）
            with open(file_path, "rb") as fb:
                head_bytes = fb.read(READ_HEAD_BYTES)
        except PermissionError:
            return ToolResult(f"[错误: 权限不足: {file_path}]")
        except OSError as e:
            return ToolResult(f"[错误: 读取失败: {e}]")

        if b"\x00" in head_bytes:
            return ToolResult("[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请使用Shell工具处理]")

        # 编码探测（GBK 回退，避免中文 Windows 下 GBK 文件读成 U+FFFD 乱码）
        encoding = detect_text_encoding(head_bytes)

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
                    # 未截断（读到 EOF）：总行数 = 实际跳过 + 实际读出，零成本精确
                    total_lines = skipped + len(result)

        except PermissionError:
            return ToolResult(f"[错误: 权限不足: {file_path}]")
        except OSError as e:
            return ToolResult(f"[错误: 读取失败: {e}]")

        # 空结果提示（offset 超出末尾 / 空文件）
        if not result:
            if offset and offset > 1:
                return ToolResult(f"[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]")
            return ToolResult("[文件为空]")

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

        return ToolResult(apply_global_cap("\n".join(result), env, total_lines))
