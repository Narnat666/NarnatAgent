"""命令输出着色端口 —— 会话域向输出层索取颜色的小接口。

行为契约：`openspec/changes/recast-v2/specs/output/spec.md`（颜色体系）与
`specs/sessions`（`/ls` 的当前会话标记与待删除标记着色）。

- `ThemePort`：`output` 积木的 `Theme` 实例即满足——属性为颜色容器，`str()`
  求值为 ANSI 序列、纯文本模式下为空串；
- `PlainColors`：无色替身（未注入主题时使用，全部颜色求值为空串）。
"""
from __future__ import annotations

from typing import Any, Protocol

__all__ = ["PlainColors", "ThemePort"]


class ThemePort(Protocol):
    """命令输出着色端口（属性名同 `output.Theme` 的派生角色/样式常量）。"""

    cmd_success: Any
    cmd_error: Any
    cmd_hint: Any
    cmd_highlight: Any
    cmd_muted: Any
    r: Any


class PlainColors:
    """无色替身：全部颜色求值为空串（headless 与测试友好）。"""

    cmd_success = ""
    cmd_error = ""
    cmd_hint = ""
    cmd_highlight = ""
    cmd_muted = ""
    r = ""
