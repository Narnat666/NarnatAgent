"""
工具回调实现 —— 删除确认 + TodoWrite UI更新

从agent.py中提取，Agent只负责组装。
"""

import sys

from ..output import write as _stdout_write, B, D, E, G, R, Y


class SafetyCallbacks:
    """安全相关回调：删除命令确认（仅Windows使用）"""

    @staticmethod
    def confirm_delete(command: str) -> bool:
        if sys.platform != "win32":
            return False
        try:
            response = input(f"  确认执行删除命令? [y/N]: ")
            return response.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False


class TodoCallbacks:
    """TodoWrite UI更新回调"""

    @staticmethod
    def on_todo_update(todos):
        for t in todos:
            status = t["status"]
            content = t.get("content", "")
            active_form = t.get("activeForm", content)

            if status == "completed":
                icon = f"{E}✓{R}"
                line = f"  {icon} {D}{content}{R}"
            elif status == "in_progress":
                icon = f"{Y}●{R}"
                line = f"  {icon} {B}{active_form}{R}"
            else:
                icon = f"{G}○{R}"
                line = f"  {icon} {D}{content}{R}"

            _stdout_write(line + "\n")
