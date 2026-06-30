"""
会话管理命令 ── /save /show /enter /delete /skill /clear

包含：
- SessionCallbacks: 后端回调接口
- _CommandCompleter: Tab补全
- _dispatch_command: 命令分发
"""

import os
import sys
from typing import Optional

from prompt_toolkit.completion import Completer, Completion

from .colors import R, G, C, E, Y, X, _stdout_write


# ═══════════════════════════════════════════════════════════════
# 会话管理回调接口 ── 后端实现
# ═══════════════════════════════════════════════════════════════

class SessionCallbacks:
    """
    后端实现此类的四个方法，传入 UIInterface。
    方法返回字符串：空串表示成功，非空串为给用户的错误提示。
    """

    def on_save(self, name: str) -> str:
        """保存当前会话，参数为用户输入的名称"""
        return ""

    def on_show(self) -> str:
        """列出所有已保存会话，返回给用户的展示文本"""
        return ""

    def on_enter(self, name: str) -> str:
        """进入指定历史会话，返回会话文本或错误提示"""
        return ""

    def on_delete(self, name: str) -> str:
        """删除指定会话(name不为空)或全部(name为空或--all)，返回结果"""
        return ""

    def on_list_names(self) -> list:
        """返回所有已保存会话的名称列表，供Tab补全使用"""
        return []

    def on_exit(self) -> str:
        """退出时自动保存，返回保存的会话名或空串"""
        return ""

    def on_skill(self, name: str) -> str:
        """加载指定技能，返回错误提示或空串"""
        return ""

    def on_list_skill_names(self) -> list:
        """返回所有可用技能的名称列表，供Tab补全使用"""
        return []

    def on_thinking(self, effort: str) -> str:
        """切换思考强度（high/max 或空查询），返回提示文本"""
        return ""

    def on_list_thinking_options(self) -> list:
        """返回所有可用的思考强度选项，供Tab补全使用"""
        return []


# ═══════════════════════════════════════════════════════════════
# Tab 补全
# ═══════════════════════════════════════════════════════════════

class _CommandCompleter(Completer):
    """命令补全：/enter /delete 动态补全会话名，/skill 动态补全技能名，其余命令静态补全"""

    _COMMANDS = {
        "/clear":    "清理屏幕",
        "/save":     "保存当前会话",
        "/show":     "显示所有会话",
        "/enter":    "进入历史会话",
        "/delete":   "删除会话",
        "/skill":    "加载技能",
        "/thinking": "切换思考强度",
        "/exit":     "退出程序",
    }
    # 命令→动态补全回调方法名
    _NAME_COMMANDS = {
        "/enter":    "on_list_names",
        "/delete":   "on_list_names",
        "/skill":    "on_list_skill_names",
        "/thinking": "on_list_thinking_options",
    }

    def __init__(self, callbacks: SessionCallbacks):
        self._cb = callbacks

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        # 不以/开头的不补全
        if not text.startswith("/"):
            return

        parts = text.split()
        num_parts = len(parts)

        # 输入中光标前无空格 → 补全命令名
        if num_parts == 1 and not text.endswith(" "):
            word = parts[0].lower()
            for cmd, meta in self._COMMANDS.items():
                if cmd.startswith(word):
                    yield Completion(
                        cmd[len(word):],
                        start_position=0,
                        display_meta=meta,
                    )
            return

        # 命令后有空格 → 补全参数
        if num_parts >= 1:
            cmd = parts[0].lower()
            if cmd in self._NAME_COMMANDS:
                names = getattr(self._cb, self._NAME_COMMANDS[cmd])()
                if num_parts == 1 and text.endswith(" "):
                    # 刚输入完命令+空格，补全所有会话名
                    for name in names:
                        yield Completion(name, start_position=0)
                elif num_parts == 2 and not text.endswith(" "):
                    # 正在输入会话名，按前缀过滤
                    prefix = parts[1]
                    for name in names:
                        if name.startswith(prefix):
                            yield Completion(
                                name[len(prefix):],
                                start_position=0,
                            )


def _dispatch_command(cmd: str, args: str, cb: SessionCallbacks) -> bool:
    cmd = cmd.lower().lstrip("/")
    if cmd == "clear":
        os.system("cls" if sys.platform == "win32" else "clear")
        return True
    if cmd == "save":
        if not args:
            _stdout_write(f"  {Y}用法: /save <名称>{R}\n")
            return True
        result = cb.on_save(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}会话已保存: {C}{args}{R}\n")
        return True
    if cmd == "show":
        result = cb.on_show()
        if result:
            _stdout_write(result + "\n")
        else:
            _stdout_write(f"  {G}(无已保存会话){R}\n")
        return True
    if cmd == "enter":
        if not args:
            _stdout_write(f"  {Y}用法: /enter <名称>{R}\n")
            return True
        result = cb.on_enter(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已进入会话: {C}{args}{R}\n")
        return True
    if cmd == "skill":
        if not args:
            _stdout_write(f"  {Y}用法: /skill <名称>{R}\n")
            return True
        result = cb.on_skill(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已加载技能: {C}{args}{R}\n")
        return True
    if cmd == "delete":
        if not args:
            _stdout_write(f"  {Y}用法: /delete <名称 | --all>{R}\n")
            return True
        result = cb.on_delete(args)
        if result:
            _stdout_write(f"  {X}{result}{R}\n")
        else:
            _stdout_write(f"  {E}已删除: {C}{args}{R}\n")
        return True
    if cmd == "thinking":
        result = cb.on_thinking(args.strip() if args else "")
        _stdout_write(f"  {C}{result}{R}\n")
        return True
    return False
