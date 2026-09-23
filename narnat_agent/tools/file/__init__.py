"""文件类工具族（file）—— Read / Glob / Grep / Edit / Write。

契约来源：specs/tools-file（25 Requirement / 97 场景）——工具名与定义要点、
五个工具的参数契约与输出格式、编码识别与保持、忽略目录过滤、截断与续读协议、
diff 生成与着色、设备参数契约；实现遵循 `contracts.tool.Tool` 协议。

结构与旧实现的对应（行为零变化，切分可查）：
- `read.py`   ← `tools/read/__init__.py`（含按行全局截断与续读协议）
- `glob.py`   ← `tools/glob/__init__.py`（花括号展开、glob→正则、scandir+top-K）
- `grep.py`   ← `tools/grep/__init__.py`（滚动缓冲扫描、预算降级、并行路径）
- `edit.py`   ← `tools/edit/__init__.py`（严格解码、换行归一、唯一性校验）
- `write.py`  ← `tools/write/__init__.py`（固定 UTF-8 落盘、字节级变更判定）
- `encoding.py` / `diff_utils.py` / `param_utils.py`：旧实现中跨工具重复的
  编码探测、diff 生成、布尔容错收敛为单点；
- `devices.py`：Read/Edit/Write 的 device 语义与远程文件访问端口（构造注入）。

装配约定：`build_file_tools()` 按 Read→Glob→Grep→Edit→Write 顺序构造工具实例，
注册顺序即「工具定义列表最前部」的既定顺序（specs/tools-file 首条 Requirement）。
"""
from __future__ import annotations

from ...contracts.tool import Tool
from .devices import RemoteFiles
from .diff_utils import DiffColors
from .edit import EDIT_DEFINITION, EditTool
from .glob import GLOB_DEFINITION, GlobTool
from .grep import GREP_DEFINITION, GrepTool
from .read import READ_DEFINITION, ReadTool
from .write import WRITE_DEFINITION, WriteTool

__all__ = [
    # 工具实例（`contracts.tool.Tool` 协议）
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ReadTool",
    "WriteTool",
    # 工具定义（与旧实现 DEFINITION 逐字节等价）
    "EDIT_DEFINITION",
    "GLOB_DEFINITION",
    "GREP_DEFINITION",
    "READ_DEFINITION",
    "WRITE_DEFINITION",
    # 构造与端口类型
    "FILE_TOOL_ORDER",
    "build_file_tools",
    "DiffColors",
    "RemoteFiles",
]

FILE_TOOL_ORDER = ("Read", "Glob", "Grep", "Edit", "Write")
"""内置工具定义列表最前部的固定顺序（specs/tools-file 工具名与注册顺序）。"""


def build_file_tools(remote: RemoteFiles | None = None,
                     colors: DiffColors | None = None) -> list[Tool]:
    """构造文件类工具族的五个工具实例（顺序即 `FILE_TOOL_ORDER`）。

    - `remote`：远程文件访问端口（由 `tools/remote` 族实现，装配注入）；
    - `colors`：diff 着色取色对象（由 `output` 主题提供，装配注入）。
    """
    return [
        ReadTool(remote=remote),
        GlobTool(),
        GrepTool(),
        EditTool(remote=remote, colors=colors),
        WriteTool(remote=remote, colors=colors),
    ]
