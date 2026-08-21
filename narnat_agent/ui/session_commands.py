"""
会话管理命令 ── /save /ls /cd /rm /skill /clear

命令可用性由当前会话状态决定：
  NoSession:   /save /cd /rm /ls /exit
  RootSession: /save /cd /rm(仅儿子) /explore /ls /exit
  ChildSession: /cd /done /ls /exit

Tab 补全只显示当前状态拥有的命令。

命令注册：用 @_register("name") 装饰器替代 if/elif 链，
新增命令只需添加一个装饰函数，无需修改 _dispatch_command。
"""

from enum import IntEnum
from typing import Callable, Dict, Optional

from prompt_toolkit.completion import Completer, Completion

from .colors import (R,
    CMD_SUCCESS, CMD_ERROR, CMD_HINT, CMD_HIGHLIGHT, CMD_MUTED,
    _stdout_write)


class CommandResult(IntEnum):
    """命令分发返回值。继承 int，与旧代码完全兼容。"""
    UNKNOWN = 0  # 未知命令
    HANDLED = 1  # 已处理，继续主循环
    EXIT = 2     # 退出 agent 进程


# ═══════════════════════════════════════════════════════════════
# Tab 补全
# ═══════════════════════════════════════════════════════════════

class _CommandCompleter(Completer):
    """命令补全：从当前状态获取可用命令，/cd /rm 动态补全会话名"""

    _NAME_COMMANDS = {
        "/cd":       "on_list_names_tree",
        "/rm":       "on_list_rm_names",
        "/skill":    "on_list_skill_names",
        "/thinking": "on_list_thinking_options",
        "/mode":     "on_list_model_names",
    }

    _STATIC_OPTIONS = {
        "/ls": ["--all"],
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
            elif num_parts == 1 and text.endswith(" "):
                for opt in self._STATIC_OPTIONS.get(cmd, []):
                    yield Completion(opt, start_position=0)


# ═══════════════════════════════════════════════════════════════
# 命令注册表 — @_register("name") 装饰器替代 if/elif 链
# ═══════════════════════════════════════════════════════════════

_commands: Dict[str, Callable] = {}


def _register(name: str):
    """装饰器：将函数注册为命令处理器。"""
    def decorator(fn: Callable) -> Callable:
        _commands[name] = fn
        return fn
    return decorator


def _require_args(args: str, hint: str) -> Optional[str]:
    """通用参数检查：无参数时打印用法提示并返回错误消息。"""
    if not args:
        _stdout_write(f"  {CMD_HINT}{hint}{R}\n")
        return "missing_args"
    return None


@_register("clear")
def _cmd_clear(args: str, mgr) -> CommandResult:
    _stdout_write("\033[2J\033[H")
    return CommandResult.HANDLED


@_register("explore")
def _cmd_explore(args: str, mgr) -> CommandResult:
    if _require_args(args, "用法: /explore <名称>"):
        return CommandResult.HANDLED
    result = mgr.on_explore(args)
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_SUCCESS}已进入探索分支: {CMD_HIGHLIGHT}{args}{R}  "
                      f"{CMD_MUTED}(/done 合并结论, /exit 暂离){R}\n")
    return CommandResult.HANDLED


@_register("done")
def _cmd_done(args: str, mgr) -> CommandResult:
    result = mgr.on_done()
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_SUCCESS}探索分支已完成，结论已合并{R}\n")
    return CommandResult.HANDLED


@_register("goal")
def _cmd_goal(args: str, mgr) -> CommandResult:
    """目标模式：/goal on [N] 开启（N=临时轮数上限），/goal off 关闭，/goal 查看状态"""
    arg = args.strip().lower()
    if arg == "on" or arg.startswith("on "):
        # /goal on [N]：可选临时轮数上限
        override = 0
        rest = arg[2:].strip()
        if rest:
            try:
                override = int(rest)
            except ValueError:
                _stdout_write(f"  {CMD_ERROR}无效轮数: {rest}{R}\n")
                return CommandResult.HANDLED
            if override < 1:
                _stdout_write(f"  {CMD_ERROR}轮数必须为正整数{R}\n")
                return CommandResult.HANDLED
        mgr._goal_enabled = True
        mgr._goal_max_rounds = override
        # 目标模式开启：向 LLM 动态注入 GoalComplete 工具
        if getattr(mgr, '_set_goal_tool', None):
            mgr._set_goal_tool(True)
        if override:
            _stdout_write(f"  {CMD_SUCCESS}目标模式已开启{R}  "
                          f"{CMD_MUTED}(本轮上限 {CMD_HIGHLIGHT}{override}{R}{CMD_MUTED} 轮){R}\n")
        else:
            _stdout_write(f"  {CMD_SUCCESS}目标模式已开启{R}\n")
        return CommandResult.HANDLED
    if arg == "off":
        was_enabled = mgr._goal_enabled
        mgr._goal_enabled = False
        mgr._goal_max_rounds = 0  # 关闭时清除临时轮数覆盖
        # 目标模式关闭：从 LLM 工具定义中移除 GoalComplete
        if getattr(mgr, '_set_goal_tool', None):
            mgr._set_goal_tool(False)
        if was_enabled:
            _stdout_write(f"  {CMD_SUCCESS}目标模式已关闭{R}\n")
        else:
            _stdout_write(f"  {CMD_MUTED}目标模式已是关闭状态{R}\n")
        return CommandResult.HANDLED
    # 无参数：查看状态
    if mgr._goal_enabled:
        # 实际生效轮数：临时覆盖 > 配置默认值 > 兜底"默认"
        limit = mgr._goal_max_rounds or getattr(mgr, '_goal_default_rounds', 0) or "默认"
        _stdout_write(f"  {CMD_HIGHLIGHT}目标模式: 已开启{R}  "
                      f"{CMD_MUTED}(轮数上限: {CMD_HIGHLIGHT}{limit}{R}{CMD_MUTED}){R}\n")
    else:
        _stdout_write(f"  {CMD_MUTED}目标模式: 已关闭{R}\n")
    return CommandResult.HANDLED


@_register("save")
def _cmd_save(args: str, mgr) -> CommandResult:
    result = mgr.on_save(args)
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        if args:
            _stdout_write(f"  {CMD_SUCCESS}会话已保存: {CMD_HIGHLIGHT}{args}{R}\n")
        else:
            saved_name = mgr.state.session_name()
            label = saved_name if saved_name else ""
            _stdout_write(f"  {CMD_SUCCESS}会话已保存: {CMD_HIGHLIGHT}{label}{R}\n")
    return CommandResult.HANDLED


@_register("ls")
def _cmd_ls(args: str, mgr) -> CommandResult:
    result = mgr.on_show(args)
    if result:
        _stdout_write(result + "\n")
    else:
        _stdout_write(f"  {CMD_MUTED}(无已保存会话){R}\n")
    return CommandResult.HANDLED


@_register("cd")
def _cmd_cd(args: str, mgr) -> CommandResult:
    if _require_args(args, "用法: /cd <名称>"):
        return CommandResult.HANDLED
    result = mgr.on_enter(args)
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_SUCCESS}已进入会话: {CMD_HIGHLIGHT}{args}{R}\n")
    return CommandResult.HANDLED


@_register("skill")
def _cmd_skill(args: str, mgr) -> CommandResult:
    if _require_args(args, "用法: /skill <名称>"):
        return CommandResult.HANDLED
    result = mgr.on_skill(args)
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_SUCCESS}已加载技能: {CMD_HIGHLIGHT}{args}{R}\n")
    return CommandResult.HANDLED


@_register("rm")
def _cmd_rm(args: str, mgr) -> CommandResult:
    if _require_args(args, "用法: /rm <名称 | --all>"):
        return CommandResult.HANDLED
    result = mgr.on_delete(args)
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_SUCCESS}已标记删除: {CMD_HIGHLIGHT}{args}{R}  "
                      f"{CMD_MUTED}(退出agent时生效){R}\n")
    return CommandResult.HANDLED


@_register("thinking")
def _cmd_thinking(args: str, mgr) -> CommandResult:
    result = mgr.on_thinking(args.strip() if args else "")
    _stdout_write(f"  {CMD_HIGHLIGHT}{result}{R}\n")
    return CommandResult.HANDLED


@_register("mode")
def _cmd_mode(args: str, mgr) -> CommandResult:
    result = mgr.on_mode(args.strip() if args else "")
    if result.startswith("无效值"):
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    elif result.startswith("设置成功"):
        _stdout_write(f"  {CMD_SUCCESS}{result}{R}\n")
    else:
        _stdout_write(f"  {CMD_HIGHLIGHT}{result}{R}\n")
    return CommandResult.HANDLED


@_register("exit")
def _cmd_exit(args: str, mgr) -> CommandResult:
    was_child = mgr.is_child_session()
    result = mgr.on_exit()
    if result:
        _stdout_write(f"  {CMD_ERROR}{result}{R}\n")
    if mgr.should_exit_agent():
        return CommandResult.EXIT
    if was_child:
        _stdout_write(f"  {CMD_SUCCESS}已暂离探索分支{R}  "
                      f"{CMD_MUTED}(/cd 回来继续){R}\n")
    else:
        _stdout_write(f"  {CMD_MUTED}已退出会话{R}\n")
    return CommandResult.HANDLED


# ═══════════════════════════════════════════════════════════════
# _dispatch_command — 统一入口
# ═══════════════════════════════════════════════════════════════

def _dispatch_command(cmd: str, args: str, mgr) -> CommandResult:
    """分发命令。查注册表 → 可用性校验 → 调用处理器。

    /clear 始终可用（不依赖会话状态），其余命令校验 available_commands()。
    """
    cmd = cmd.lower().lstrip("/")
    if cmd == "clear":
        return _cmd_clear(args, mgr)
    if mgr is None:
        return CommandResult.UNKNOWN
    if cmd == "goal":
        return _cmd_goal(args, mgr)
    available = mgr.available_commands()


    cmd_slash = f"/{cmd}"
    if cmd_slash not in available:
        return CommandResult.UNKNOWN

    handler = _commands.get(cmd)
    if handler is None:
        return CommandResult.UNKNOWN
    return handler(args, mgr)
