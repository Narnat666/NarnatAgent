"""
会话管理命令 ── /save /show /enter /delete /skill /clear

命令可用性由当前会话状态决定：
  NoSession:   /save /enter /delete /show /exit
  RootSession: /save /enter /delete(仅儿子) /explore /show /exit
  ChildSession: /enter /done /show /exit

Tab 补全只显示当前状态拥有的命令。
"""

import os
import sys
from typing import Dict, Optional

from prompt_toolkit.completion import Completer, Completion

from .colors import R, G, C, D, E, Y, X, _stdout_write


# ═══════════════════════════════════════════════════════════════
# Tab 补全
# ═══════════════════════════════════════════════════════════════

class _CommandCompleter(Completer):
    """命令补全：从当前状态获取可用命令，/enter /delete 动态补全会话名"""

    _NAME_COMMANDS = {
        "/enter":    "on_list_names_tree",
        "/delete":   "on_list_names_tree",
        "/skill":    "on_list_skill_names",
        "/thinking": "on_list_thinking_options",
    }

    def __init__(self, mgr):
        self._mgr = mgr

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        commands = self._mgr.available_commands()
        parts = text.split()
        num_parts = len(parts)

        if num_parts == 1 and not text.endswith(" "):
            word = parts[0].lower()
            for cmd, meta in commands.items():
                if cmd.startswith(word):
                    yield Completion(
                        cmd[len(word):],
                        start_position=0,
                        display_meta=meta,
                    )
            return

        if num_parts >= 1:
            cmd = parts[0].lower()
            if cmd in self._NAME_COMMANDS:
                names = getattr(self._mgr, self._NAME_COMMANDS[cmd])()
                if num_parts == 1 and text.endswith(" "):
                    for name in names:
                        yield Completion(name, start_position=0)
                elif num_parts == 2 and not text.endswith(" "):
                    prefix = parts[1]
                    if "/" in prefix:
                        slash_pos = prefix.rfind("/")
                        parent_part = prefix[:slash_pos + 1]
                        child_prefix = prefix[slash_pos + 1:]
                        for name in names:
                            if name.startswith(prefix) and "/" in name:
                                child_name = name[len(parent_part):]
                                if child_name.startswith(child_prefix):
                                    yield Completion(
                                        child_name,
                                        start_position=-len(child_prefix),
                                    )
                    else:
                        for name in names:
                            if name.startswith(prefix):
                                yield Completion(
                                    name[len(prefix):],
                                    start_position=0,
                                )


# ═══════════════════════════════════════════════════════════════
# 命令分发
# ═══════════════════════════════════════════════════════════════

def _dispatch_command(cmd: str, args: str, mgr) -> int:
    """分发命令。返回值：0=未知命令 1=已处理 2=退出agent"""
    cmd = cmd.lower().lstrip("/")
    available = mgr.available_commands()
    cmd_slash = f"/{cmd}"

    if cmd == "clear":
        os.system("cls" if sys.platform == "win32" else "clear")
        return 1

    if cmd_slash not in available:
        return 0

    if cmd == "explore":
        if not args:
            _stdout_write(f"  {Y}用法: /explore <名称>{R}\n")
            return 1
        result = mgr.on_explore(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已进入探索分支: {C}{args}{R}  {D}(/done 合并结论, /exit 暂离){R}\n")
        return 1

    if cmd == "done":
        result = mgr.on_done()
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}探索分支已完成，结论已合并{R}\n")
        return 1

    if cmd == "save":
        if not args:
            _stdout_write(f"  {Y}用法: /save <名称>{R}\n")
            return 1
        result = mgr.on_save(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}会话已保存: {C}{args}{R}\n")
        return 1

    if cmd == "show":
        result = mgr.on_show()
        if result:
            _stdout_write(result + "\n")
        else:
            _stdout_write(f"  {G}(无已保存会话){R}\n")
        return 1

    if cmd == "enter":
        if not args:
            _stdout_write(f"  {Y}用法: /enter <名称>{R}\n")
            return 1
        result = mgr.on_enter(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已进入会话: {C}{args}{R}\n")
        return 1

    if cmd == "skill":
        if not args:
            _stdout_write(f"  {Y}用法: /skill <名称>{R}\n")
            return 1
        result = mgr.on_skill(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已加载技能: {C}{args}{R}\n")
        return 1

    if cmd == "delete":
        if not args:
            _stdout_write(f"  {Y}用法: /delete <名称 | --all>{R}\n")
            return 1
        result = mgr.on_delete(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已标记删除: {C}{args}{R}  {D}(退出agent时生效){R}\n")
        return 1

    if cmd == "thinking":
        result = mgr.on_thinking(args.strip() if args else "")
        _stdout_write(f"  {C}{result}{R}\n")
        return 1

    if cmd == "exit":
        was_child = mgr.is_child_session()
        result = mgr.on_exit()
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        if mgr.should_exit_agent():
            return 2
        if was_child:
            _stdout_write(f"  {E}已暂离探索分支{R}  {D}(/enter 回来继续){R}\n")
        else:
            _stdout_write(f"  {D}已退出会话{R}\n")
        return 1

    return 0
