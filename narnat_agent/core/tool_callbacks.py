"""
工具回调实现 —— TodoWrite UI更新

从agent.py中提取，Agent只负责组装。
"""

from ..output import write as _stdout_write, B, D, E, G, R, Y, is_quiet_tools


class TodoCallbacks:
    """TodoWrite UI更新回调"""

    @staticmethod
    def on_todo_update(todos):
        if is_quiet_tools():
            return
        for t in todos:
            status = t["status"]
            content = str(t.get("content", ""))

            if status == "completed":
                icon = f"{E}✓{R}"
                line = f"  {icon} {D}{content}{R}"
            elif status == "in_progress":
                icon = f"{Y}●{R}"
                # AI 有时自己写了"正在"前缀；此时不再叠加，避免"正在正在…"
                prefix = "" if content.startswith("正在") else "正在"
                line = f"  {icon} {B}{prefix}{content}{R}"
            else:
                icon = f"{G}○{R}"
                line = f"  {icon} {D}{content}{R}"

            _stdout_write(line + "\n")
