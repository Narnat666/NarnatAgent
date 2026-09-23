"""Write 工具 —— 创建新文件或完整覆写文件（本地固定 UTF-8 落盘）。

契约来源：specs/tools-file「Write 创建与覆盖行为」「Read/Edit/Write 的设备参数契约」
「编辑类工具的返回形态与差分展示」「diff 生成与着色」。

行为搬运自旧实现 `narnat_agent/tools/write/__init__.py`（目录拦截、父目录自动创建、
旧内容编码探测、字节级变更判定、空写入提示、diff 生成逐字一致）；结构重组：
`execute` 函数 → `WriteTool`、`_detect_text_encoding` → `file.encoding` 单点、
`colorize_diff` → `file.diff_utils`（取色可注入）、`_make_diff` 与 edit 的重复实现
合并为 `diff_utils.make_diff`。

兼容怪癖保持：写盘固定 UTF-8（不保持既有文件编码）；旧内容读取失败的静默忽略
（结果缺失 diff、仍报告 `[已写入]`）。
"""
from __future__ import annotations

import os

from ...contracts.tool import ToolDefinition, ToolEnv, ToolResult
from .devices import DEFAULT_REMOTE, RemoteFiles, device_hint, normalize_device
from .diff_utils import DiffColors, colorize_diff, describe_bytes_only_change, make_diff
from .encoding import detect_text_encoding
from .param_utils import bind_params

__all__ = ["WRITE_DEFINITION", "WriteTool"]

# 读取旧内容做二进制判定/编码探测的块大小。
HEAD_BYTES = 8192

WRITE_PARAMS = ("file_path", "content", "device")

WRITE_DEFINITION: ToolDefinition = {
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


class WriteTool:
    """Write 工具实现（`contracts.tool.Tool` 协议）。

    - `remote`：远程文件访问端口（`device=devN` 分支使用）；
    - `colors`：diff 着色取色对象（装配时注入 output 主题；缺省用内置默认色）。
    """

    name = "Write"

    def __init__(self, remote: RemoteFiles | None = None,
                 colors: DiffColors | None = None) -> None:
        self._remote = remote if remote is not None else DEFAULT_REMOTE
        self._colors = colors

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（与旧实现 DEFINITION 逐字节等价）。"""
        return WRITE_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """创建或覆写文件，返回写入回执与（覆写时的）着色 diff。"""
        params = bind_params(args, WRITE_PARAMS, ("file_path", "content"))
        file_path = params["file_path"]
        content = params["content"]
        device = params.get("device", "")

        device = normalize_device(device)
        if device is None:
            return ToolResult(f"[错误: {device_hint(self._remote)}]")

        if device:
            text, diff = self._remote.write(file_path, content, device)
            return ToolResult(text, ui_text=diff or None)

        abs_path = os.path.abspath(file_path)

        # 目录路径：open() 会报 Permission denied，误导 AI 去查权限而非换路径，提前拦截给出真实原因
        if os.path.isdir(abs_path):
            return ToolResult(f"[错误: {file_path} 是目录，请使用正确的文件路径]")

        # 自动创建父目录
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 覆写已有文件时生成 diff
        color_diff = ""
        old_content = ""
        old_bytes = None
        diff = ""
        if os.path.isfile(abs_path):
            try:
                # 旧内容编码探测（与 Read 一致）：GBK 文件按 utf-8-sig 读会抛异常导致 diff 静默丢失，
                # AI 覆写后看不到差异。此处仅用于展示 diff，errors=replace 不影响写入内容
                with open(abs_path, "rb") as fb:
                    head = fb.read(HEAD_BYTES)
                    if not (head and b"\x00" in head):
                        # 仅文本文件读全文用于字节级变更判定（二进制只取首块，避免大文件占内存）
                        fb.seek(0)
                        old_bytes = fb.read()
                if old_bytes is not None:
                    encoding = detect_text_encoding(head)
                    old_content = old_bytes.decode(encoding, errors="replace")
                    diff = make_diff(old_content, content, file_path)
                    color_diff = colorize_diff(diff, self._colors)
            except Exception:
                pass

        try:
            with open(abs_path, "w", encoding="utf-8", newline='') as f:
                f.write(content)
        except OSError as e:
            return ToolResult(f"[错误: 写入失败: {e}]")

        new_bytes = content.encode("utf-8")
        byte_count = len(new_bytes)

        # 变更判定基于字节比较：make_diff 的 splitlines() 会抹平行尾符与末尾换行差异，
        # 据其报"无实质修改"会在文件已被改写（CRLF 静默变 LF）时给出假报告
        if diff == "[无差异]" and old_bytes is not None:
            if new_bytes == old_bytes:
                # 覆写内容与现有内容相同：文件已照常写盘，但明确告知 AI 本次无实质修改
                # （此前返回"[已写入...]\n[无差异]"，首行误导 AI 以为写入成功）
                return ToolResult("[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]",
                                  ui_text=color_diff or None)
            detail = describe_bytes_only_change(old_bytes, new_bytes)
            return ToolResult(f"[提示: 文件已写入，正文内容相同，但字节层面有变化（{detail}）]",
                              ui_text=colorize_diff(f"[正文相同，字节变化] {detail}", self._colors))
        if diff:
            return ToolResult(f"[已写入: {file_path} ({byte_count}字节)]\n{diff}",
                              ui_text=color_diff or None)
        return ToolResult(f"[已写入: {file_path} ({byte_count}字节)]", ui_text=color_diff or None)
