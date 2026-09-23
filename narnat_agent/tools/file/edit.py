"""Edit 工具 —— 字符串精确替换编辑文件（本地/远程），保持原编码与换行符形态。

契约来源：specs/tools-file「Edit 参数契约与唯一性校验」「Edit 编码与换行符保持」
「编辑类工具的返回形态与差分展示」「diff 生成与着色」「Read/Edit/Write 的设备参数契约」。

行为搬运自旧实现 `narnat_agent/tools/edit/__init__.py`（严格解码读取、二进制拒绝、
换行符归一、唯一性校验、相似行提示、写回与编码失败文案、空编辑提示、diff 生成
逐字一致）；结构重组：`execute` 函数 → `EditTool`、`_detect_text_encoding` →
`file.encoding` 单点、`colorize_diff` → `file.diff_utils`（取色可注入）、
`_make_diff` 与 write 的重复实现合并为 `diff_utils.make_diff`。
"""
from __future__ import annotations

import difflib
import os

from ...contracts.tool import ToolDefinition, ToolEnv, ToolResult
from .devices import DEFAULT_REMOTE, RemoteFiles, device_hint, normalize_device
from .diff_utils import DiffColors, colorize_diff, make_diff
from .encoding import detect_text_encoding
from .param_utils import bind_params, to_bool

__all__ = [
    "EDIT_DEFINITION",
    "EditTool",
    "edit_by_string",
    "find_similar",
    "read_for_edit",
    "write_and_diff",
]

# 载入文件内容前的探测块大小（二进制判定 + 编码探测共用）。
HEAD_BYTES = 8192

# 相似行提示的两条阈值（相似度下限与最多条数）。
SIMILAR_RATIO_MIN = 0.5
SIMILAR_MAX_HINTS = 3

EDIT_PARAMS = ("file_path", "old_string", "new_string", "replace_all", "device")

EDIT_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": (
            "编辑文件（字符串精确替换）。支持本地或远程编辑文件。"
            "自动识别并保持原编码（UTF-8/GBK）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "待替换的原文（必须与文件内容精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换全部匹配（默认否，即只替换唯一匹配处）",
                },
                "device": {
                    "type": "string",
                    "description": "设备dev编号：默认dev0即编辑本地指定文件，设置dev1..devn则编辑指定远程设备文件",
                },
            },
            "required": ["file_path", "old_string"],
        },
    },
}


def read_for_edit(file_path: str) -> tuple[str, str]:
    """读取文件内容用于编辑，返回 (content, 写回编码)。

    编码策略与 Read 一致（首块探测 utf-8-sig / gbk），但必须严格解码：
    Edit 会把内容写回文件，errors="replace" 会以 U+FFFD 永久替换原字节，
    造成静默数据损坏。探测结果无法严格解码时抛 UnicodeDecodeError，
    由调用方拒绝编辑（对齐远程 Edit 行为）。

    Raises:
        ValueError: 二进制文件（含NUL字节）
        UnicodeDecodeError: 无法按 UTF-8/GBK 严格解码
    """
    with open(file_path, "rb") as fb:
        head = fb.read(HEAD_BYTES)
    if b"\x00" in head:
        raise ValueError("binary")
    encoding = detect_text_encoding(head)
    with open(file_path, "r", encoding=encoding, newline="") as f:
        content = f.read()
    # 写回编码保持原文件形态：UTF-8 有 BOM 保留 BOM、无 BOM 不添加，GBK 写回 GBK
    if encoding == "utf-8-sig":
        write_encoding = "utf-8-sig" if head.startswith(b"\xef\xbb\xbf") else "utf-8"
    else:
        write_encoding = "gbk"
    return content, write_encoding


def find_similar(content: str, old_string: str) -> str:
    """查找相似行，帮助 LLM 定位（对 old_string 首行与文件各行做相似度比较）。

    仅返回相似度 >0.5 的前三条，格式 `相似行（供参考）:` + `  行{N}: {行内容} (相似度{P}%)`。
    """
    content_lines = content.splitlines()
    old_lines = old_string.strip().splitlines()
    if not old_lines:
        return ""

    target = old_lines[0].strip()
    similarities = []
    for i, line in enumerate(content_lines):
        ratio = difflib.SequenceMatcher(None, target, line.strip()).ratio()
        if ratio > SIMILAR_RATIO_MIN:
            similarities.append((ratio, i + 1, line))

    if not similarities:
        return ""

    similarities.sort(reverse=True)
    hints = ["相似行（供参考）:"]
    for ratio, line_num, line in similarities[:SIMILAR_MAX_HINTS]:
        hints.append(f"  行{line_num}: {line.strip()} (相似度{ratio:.0%})")
    return "\n".join(hints)


def edit_by_string(content: str, old_string: str, new_string: str,
                   replace_all: bool, file_path: str,
                   write_encoding: str = "utf-8",
                   colors: DiffColors | None = None) -> ToolResult:
    """字符串精确替换（自动兼容换行符），随后写回并生成 diff。"""
    if not old_string:
        return ToolResult("[错误: old_string不能为空]")

    # 检测文件换行符风格，转换 old_string/new_string 以匹配
    has_crlf = '\r\n' in content
    if has_crlf:
        _normalize = lambda s: s.replace('\r\n', '\x00').replace('\n', '\r\n').replace('\x00', '\r\n')
    else:
        _normalize = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')

    old_string_normalized = _normalize(old_string)
    new_string_normalized = _normalize(new_string)

    count = content.count(old_string_normalized)
    if count == 0:
        hint = find_similar(content, old_string)
        return ToolResult(f"[错误: 未找到匹配文本。请先Read确认文件内容。]\n{hint}")

    if count > 1 and not replace_all:
        return ToolResult(f"[错误: 找到{count}处匹配，old_string不唯一。请扩大上下文使其唯一，或设置replace_all=True]")

    if replace_all:
        new_content = content.replace(old_string_normalized, new_string_normalized)
    else:
        new_content = content.replace(old_string_normalized, new_string_normalized, 1)

    return write_and_diff(content, new_content, file_path, count if replace_all else 1,
                          write_encoding=write_encoding, colors=colors)


def write_and_diff(old_content: str, new_content: str, file_path: str,
                   count: int, write_encoding: str = "utf-8",
                   colors: DiffColors | None = None) -> ToolResult:
    """写回文件并生成 diff（着色 diff 进 `ui_text`，供终端展示）。"""
    try:
        with open(file_path, "w", encoding=write_encoding, newline='') as f:
            f.write(new_content)
    except OSError as e:
        return ToolResult(f"[错误: 写入失败: {e}]")
    except UnicodeEncodeError:
        # 新内容含文件编码无法表示的字符（如 GBK 文件中写入 emoji）
        return ToolResult(f"[错误: 新内容包含文件编码({write_encoding})无法表示的字符，写入失败: {file_path}]")

    if old_content == new_content:
        # 空编辑提醒：文件已照常写盘，但明确告知 AI 本次无实质修改
        # （此前返回"[已替换1处]\n[无差异]"，首行误导 AI 以为编辑成功）
        return ToolResult("[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]",
                          ui_text=colorize_diff("[无差异]", colors))

    diff = make_diff(old_content, new_content, file_path)
    llm_result = f"[已替换{count}处]\n{diff}"
    return ToolResult(llm_result, ui_text=colorize_diff(diff, colors))


class EditTool:
    """Edit 工具实现（`contracts.tool.Tool` 协议）。

    - `remote`：远程文件访问端口（`device=devN` 分支使用）；
    - `colors`：diff 着色取色对象（装配时注入 output 主题；缺省用内置默认色）。
    """

    name = "Edit"

    def __init__(self, remote: RemoteFiles | None = None,
                 colors: DiffColors | None = None) -> None:
        self._remote = remote if remote is not None else DEFAULT_REMOTE
        self._colors = colors

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（与旧实现 DEFINITION 逐字节等价）。"""
        return EDIT_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """编辑文件（唯一匹配替换 / replace_all 全量替换），返回确认信息与着色 diff。"""
        params = bind_params(args, EDIT_PARAMS, ("file_path",))
        file_path = params["file_path"]
        old_string = params.get("old_string", "")
        new_string = params.get("new_string", "")
        replace_all = params.get("replace_all", False)
        device = params.get("device", "")

        # 归一化布尔参数: LLM 偶发传 "false" 字符串，bool("false") 恒为 True
        # 会导致全量替换（与意图相反的危险行为），必须按字符串语义解析
        replace_all = to_bool(replace_all)

        device = normalize_device(device)
        if device is None:
            return ToolResult(f"[错误: {device_hint(self._remote)}]")

        if device:
            text, diff = self._remote.edit(file_path, old_string, new_string, replace_all, device)
            return ToolResult(text, ui_text=diff or None)

        if not os.path.isfile(file_path):
            return ToolResult(f"[错误: 文件不存在: {file_path}，如需创建请用Write工具]")

        try:
            content, write_encoding = read_for_edit(file_path)
        except PermissionError:
            return ToolResult(f"[错误: 权限不足: {file_path}]")
        except OSError as e:
            return ToolResult(f"[错误: 读取失败: {e}]")
        except ValueError:
            # 顺序与旧实现逐行一致：UnicodeDecodeError 是 ValueError 子类，
            # 故此分支吸收「无法严格解码」的文本文件（返回二进制文案），
            # 其后的 UnicodeDecodeError 分支不可达——等价保持，不"顺手修正"
            # （design D11：除明示修复项外零差异；偏差已登记）。
            return ToolResult("[错误: 检测到二进制文件（含NUL字节），Edit仅支持文本文件。请使用Shell工具处理]")
        except UnicodeDecodeError:
            return ToolResult(f"[错误: 文件非UTF-8/GBK编码，为防止内容损坏已拒绝编辑: {file_path}。"
                              f"请用Shell工具处理（如转码为UTF-8后再编辑）]")

        return edit_by_string(content, old_string, new_string, replace_all, file_path,
                              write_encoding, colors=self._colors)
