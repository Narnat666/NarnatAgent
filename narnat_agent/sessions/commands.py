"""会话命令层 —— 命令可用性表、命令实现与分发。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`（各命令 Requirement）
与 `specs/ui`（「命令集与分发语义」：命令名归一、三态分发结果、提示文案）。

- 命令可用性随会话状态变化（命令表由状态对象给出，见 `state.py`）：不在当前
  状态可用集内的命令按未知命令处理、不执行；
- `/clear` 与 `/goal` 不经过状态校验——`/clear` 的清屏动作归 ui（它不依赖会话
  管理器），本层只认领命令名、不落到模型；
- 分发结果三态：`handled`（已处理，不调用模型）、`exit`（退出，清理后结束
  进程）、`unknown`（未识别，该输入按普通用户消息继续处理——不打印错误）；
- 输出文本为已着色形态（用注入的着色端口，未注入时拼出裸文本），调用方直接
  打印 `CommandReply.text`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注（manager 反向引用本模块）
    from .manager import SessionManager

__all__ = ["CommandKind", "CommandReply", "SessionCommands"]


class CommandKind:
    """分发结果语义常量（ui / app 主循环据此决策）。"""

    HANDLED = "handled"
    EXIT = "exit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandReply:
    """命令分发结果。

    - `kind`：三态语义（`CommandKind` 之一）；
    - `text`：完整可打印文本（可多行、已着色、含行首缩进；未识别/无输出时为空串），
      调用方原样输出（空串不输出）。
    """

    kind: str
    text: str = ""


class SessionCommands:
    """会话命令层：可用性表查询、命令实现与分发（持有会话管理器引用）。"""

    _HANDLERS = {
        "save": "_cmd_save",
        "ls": "_cmd_ls",
        "cd": "_cmd_cd",
        "rm": "_cmd_rm",
        "explore": "_cmd_explore",
        "done": "_cmd_done",
        "skill": "_cmd_skill",
        "thinking": "_cmd_thinking",
        "thinkback": "_cmd_thinkback",
        "mode": "_cmd_mode",
        "compact": "_cmd_compact",
        "exit": "_cmd_exit",
        "goal": "_cmd_goal",
    }

    def __init__(self, mgr: "SessionManager") -> None:
        self._mgr = mgr

    # ═══════════════════════════════════════════════════════════
    # 命令表与分发
    # ═══════════════════════════════════════════════════════════

    def available_commands(self) -> dict[str, str]:
        """当前状态的可用命令表（命令 → 说明；Tab 补全的数据源）。"""
        return self._mgr.available_commands()

    def dispatch(self, text: str) -> CommandReply:
        """分发一条命令输入（含前导 `/` 的完整行，如 `/save 名称`）。

        命令名归一（转小写、剥除全部前导斜杠）后：`/clear` 认领不执行、`/goal`
        不校验状态、其余命令须在当前可用集内；未识别返回 `unknown`。
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return CommandReply(CommandKind.UNKNOWN)
        parts = stripped.split(None, 1)
        cmd = parts[0].lower().lstrip("/")
        args = parts[1] if len(parts) > 1 else ""
        if cmd == "clear":
            # 清屏动作归 ui（不依赖会话管理器）；此处认领命令名、不落模型
            return CommandReply(CommandKind.HANDLED)
        if cmd == "goal":
            return self._cmd_goal(args)
        if f"/{cmd}" not in self.available_commands():
            return CommandReply(CommandKind.UNKNOWN)
        handler = self._HANDLERS.get(cmd)
        if handler is None:
            return CommandReply(CommandKind.UNKNOWN)
        return getattr(self, handler)(args)

    # ═══════════════════════════════════════════════════════════
    # 会话命令实现
    # ═══════════════════════════════════════════════════════════

    def _cmd_save(self, args: str) -> CommandReply:
        """`/save [名称]`：保存会话（空名由模型自动命名）。"""
        err = self._mgr.state.save(args)
        if err:
            return self._error(err)
        name = self._mgr.state.session_name() or ""
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED,
                            f"  {t.cmd_success}会话已保存: {t.cmd_highlight}{name}{t.r}")

    def _cmd_ls(self, args: str) -> CommandReply:
        """`/ls [--all]`：列出已保存会话（无结果显示提示）。"""
        text = self._mgr.state.show(args)
        if not text:
            return self._muted("(无已保存会话)")
        return CommandReply(CommandKind.HANDLED, text)

    def _cmd_cd(self, args: str) -> CommandReply:
        """`/cd <名称>`：进入历史会话。"""
        if not args:
            return self._hint("用法: /cd <名称>")
        err = self._mgr.state.enter(args)
        if err:
            return self._error(err)
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED,
                            f"  {t.cmd_success}已进入会话: {t.cmd_highlight}{args}{t.r}")

    def _cmd_rm(self, args: str) -> CommandReply:
        """`/rm <名称|--all>`：标记删除（退出 agent 时生效）。"""
        if not args:
            return self._hint("用法: /rm <名称 | --all>")
        err = self._mgr.state.delete(args)
        if err:
            return self._error(err)
        t = self._mgr.theme
        return CommandReply(
            CommandKind.HANDLED,
            f"  {t.cmd_success}已标记删除: {t.cmd_highlight}{args}{t.r}  "
            f"{t.cmd_muted}(退出agent时生效){t.r}",
        )

    def _cmd_explore(self, args: str) -> CommandReply:
        """`/explore <名称>`：创建探索分支。"""
        if not args:
            return self._hint("用法: /explore <名称>")
        err = self._mgr.state.explore(args)
        if err:
            return self._error(err)
        t = self._mgr.theme
        return CommandReply(
            CommandKind.HANDLED,
            f"  {t.cmd_success}已进入探索分支: {t.cmd_highlight}{args}{t.r}  "
            f"{t.cmd_muted}(/done 合并结论, /exit 暂离){t.r}",
        )

    def _cmd_done(self, args: str) -> CommandReply:
        """`/done`：合并探索分支结论回父会话。"""
        err = self._mgr.state.done()
        if err:
            return self._error(err)
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED,
                            f"  {t.cmd_success}探索分支已完成，结论已合并{t.r}")

    def _cmd_skill(self, args: str) -> CommandReply:
        """`/skill <名称 | 项目目录/文件.md>`：加载技能为 system 消息。"""
        if not args:
            return self._hint("用法: /skill <名称 | 项目目录/文件.md>")
        err = self._mgr.load_skill(args)
        if err:
            return self._error(err)
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED,
                            f"  {t.cmd_success}已加载技能: {t.cmd_highlight}{args}{t.r}")

    def _cmd_thinking(self, args: str) -> CommandReply:
        """`/thinking [强度]`：查询或切换思考强度（写回配置）。"""
        text = self._mgr.thinking(args.strip())
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_highlight}{text}{t.r}")

    def _cmd_thinkback(self, args: str) -> CommandReply:
        """`/thinkback [on|off]`：查询或切换思考回传开关（写回配置）。"""
        text = self._mgr.thinkback(args.strip())
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_highlight}{text}{t.r}")

    def _cmd_mode(self, args: str) -> CommandReply:
        """`/mode [模型]`：查询或切换模型（大小写不敏感匹配、写回配置）。"""
        text = self._mgr.switch_model(args.strip())
        t = self._mgr.theme
        if text.startswith("无效值"):
            return CommandReply(CommandKind.HANDLED, f"  {t.cmd_error}{text}{t.r}")
        if text.startswith("设置成功"):
            return CommandReply(CommandKind.HANDLED, f"  {t.cmd_success}{text}{t.r}")
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_highlight}{text}{t.r}")

    def _cmd_compact(self, args: str) -> CommandReply:
        """`/compact`：手动压缩上下文（无参数；用户取消不视为错误）。"""
        if args.strip():
            return self._hint("用法: /compact（无参数）")
        try:
            status, text = self._mgr.compact()
        except Exception as e:
            # 命令层兜底：压缩内部异常不应把整个进程带崩；此处不宣称历史状态
            # （异常可能发生在压缩生效后的落盘阶段）
            t = self._mgr.theme
            return CommandReply(CommandKind.HANDLED,
                                f"  {t.cmd_error}压缩中断: 内部异常({e}){t.r}")
        t = self._mgr.theme
        if status == "error" and "取消" in text:
            color = t.cmd_muted  # 用户主动取消属正常操作，非错误
        else:
            color = {"ok": t.cmd_success, "empty": t.cmd_hint}.get(status, t.cmd_error)
        return CommandReply(CommandKind.HANDLED, f"  {color}{text}{t.r}")

    def _cmd_exit(self, args: str) -> CommandReply:
        """`/exit`：语义随状态变化（退出程序 / 退出会话 / 暂离探索分支）。"""
        was_child = self._mgr.is_child_session()
        msg, exiting = self._mgr.exit_session()
        t = self._mgr.theme
        lines = []
        if msg:
            lines.append(f"  {t.cmd_error}{msg}{t.r}")
        if exiting:
            return CommandReply(CommandKind.EXIT, "\n".join(lines))
        if was_child:
            lines.append(f"  {t.cmd_success}已暂离探索分支{t.r}  "
                         f"{t.cmd_muted}(/cd 回来继续){t.r}")
        else:
            lines.append(f"  {t.cmd_muted}已退出会话{t.r}")
        return CommandReply(CommandKind.HANDLED, "\n".join(lines))

    def _cmd_goal(self, args: str) -> CommandReply:
        """`/goal [on [N] | off]`：目标模式开关与状态查询（不校验会话状态）。"""
        t = self._mgr.theme
        goal = self._mgr.goal
        arg = args.strip().lower()
        if arg == "on" or arg.startswith("on "):
            rest = arg[2:].strip()
            override = 0
            if rest:
                try:
                    override = int(rest)
                except ValueError:
                    return CommandReply(CommandKind.HANDLED, f"  {t.cmd_error}无效轮数: {rest}{t.r}")
                if override < 1:
                    return CommandReply(CommandKind.HANDLED, f"  {t.cmd_error}轮数必须为正整数{t.r}")
            goal.turn_on(override)
            if override:
                return CommandReply(
                    CommandKind.HANDLED,
                    f"  {t.cmd_success}目标模式已开启{t.r}  "
                    f"{t.cmd_muted}(本轮上限 {t.cmd_highlight}{override}{t.r}{t.cmd_muted} 轮){t.r}",
                )
            return CommandReply(CommandKind.HANDLED, f"  {t.cmd_success}目标模式已开启{t.r}")
        if arg == "off":
            was_enabled = goal.turn_off()
            if was_enabled:
                return CommandReply(CommandKind.HANDLED, f"  {t.cmd_success}目标模式已关闭{t.r}")
            return self._muted("目标模式已是关闭状态")
        if goal.enabled:
            limit = goal.max_rounds or "默认"
            return CommandReply(
                CommandKind.HANDLED,
                f"  {t.cmd_highlight}目标模式: 已开启{t.r}  "
                f"{t.cmd_muted}(轮数上限: {t.cmd_highlight}{limit}{t.r}{t.cmd_muted}){t.r}",
            )
        return self._muted("目标模式: 已关闭")

    # ═══════════════════════════════════════════════════════════
    # 输出辅助
    # ═══════════════════════════════════════════════════════════

    def _error(self, text: str) -> CommandReply:
        """错误色输出（两空格缩进）。"""
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_error}{text}{t.r}")

    def _hint(self, text: str) -> CommandReply:
        """提示色输出（用法提示）。"""
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_hint}{text}{t.r}")

    def _muted(self, text: str) -> CommandReply:
        """灰色输出（正常但非重点的信息）。"""
        t = self._mgr.theme
        return CommandReply(CommandKind.HANDLED, f"  {t.cmd_muted}{text}{t.r}")
