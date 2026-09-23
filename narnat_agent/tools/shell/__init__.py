"""shell 工具族 —— Shell 工具（前台执行 + 后台任务管理）。

对外契约（specs/tools-shell）：工具名 `Shell`，参数 `command`/`timeout`/
`max_output_chars`/`background`/`bg`/`id` 且无必填项；返回文本为「退出码行 /
stdout 段 / stderr 段 / 提示符」等固定形态（框架随机标签由注册表判定后剥离）。

处理顺序（specs/tools-shell「参数契约与归一化」）：数值归一化 → 安全确认 →
后台分发 → 前台参数校验 → 超时上限钳制。

积木内切分：
- `process.py`：进程/编码原语（杀树、收尾宽限、解码、可执行查找、UTF-8 环境）
  与前台运行态 `ShellRuntime`（当前进程 + 中断标志的唯一属主）；
- `executor.py`：前台执行器（平台自适应、cd 持久化、多段执行、python 载荷直执行）；
- `background.py`：后台槽位管理器（提交/状态/等待/取消/清理）；
- 本模块：`Tool` 协议实现与对外门面（装配点构造，注册进 `ToolRegistry`）。
"""
from __future__ import annotations

import sys

from ...contracts.interrupt import InterruptSignal
from ...contracts.tool import AWAIT_CONFIRM, ToolDefinition, ToolEnv, ToolResult
from ..signal import error_line
from .background import BackgroundManager
from .executor import PLATFORM_LABEL, RE_DELETE, RE_GIT, ShellExecutor
from .process import ShellRuntime

__all__ = ["BackgroundManager", "ShellRuntime", "ShellTool"]


class ShellTool:
    """Shell 工具 —— 本地命令执行与后台任务管理（`Tool` 协议实现）。

    - 前台：同步执行并返回「退出码行 + 输出段 + 提示符」（详见 `executor.py`）；
    - 后台：`background=true` 提交、`bg=status/wait/cancel` 管理（详见 `background.py`）；
    - 安全确认：删除类/git 命令的门禁（Windows 走宿主回调、非 Windows 挂起确认）；
    - 中断：`runtime.interrupt` 为 ESC 打断入口（可经 `InterruptSignal.subscribe` 接线）。

    构造参数留给装配点注入：共享同一 `ShellRuntime` 的实例拥有同一中断语义；
    `signal` 给定时本工具自动订阅中断（未给定则不接线，由装配点自行接线）。
    """

    def __init__(
        self,
        runtime: ShellRuntime | None = None,
        background: BackgroundManager | None = None,
        signal: InterruptSignal | None = None,
    ) -> None:
        self._runtime = runtime if runtime is not None else ShellRuntime()
        self._background = (
            background if background is not None else BackgroundManager(self._runtime)
        )
        self._executor = ShellExecutor(self._runtime)
        if signal is not None:
            signal.subscribe(self._runtime.interrupt)

    # ═══════════════════════════════════════════════════════════
    # Tool 协议
    # ═══════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """工具名（LLM 可见名与注册表键）。"""
        return "Shell"

    @property
    def runtime(self) -> ShellRuntime:
        """前台运行态（中断入口与当前进程属主；宿主 ESC 打断经此接线）。"""
        return self._runtime

    @property
    def background(self) -> BackgroundManager:
        """后台槽位管理器（宿主会话生命周期与软提醒经此读取）。"""
        return self._background

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义：描述按当前平台渲染平台标签，参数表无必填项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    f"本地Shell — 在{PLATFORM_LABEL}执行命令。"
                    "前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": (
                                "命令（前台执行或 background=true 提交时必填；"
                                "bg=status/wait/cancel 时可省略）"
                            ),
                        },
                        "timeout": {
                            "type": "integer",
                            "description": (
                                "前台=超时秒数（正整数，默认120，超时后命令被终止）；"
                                "bg=wait时=最长等待秒数"
                            ),
                        },
                        "max_output_chars": {
                            "type": "integer",
                            "description": "最大输出字符数（正整数，默认4000，仅前台命令生效）",
                        },
                        "background": {
                            "type": "boolean",
                            "description": (
                                "true=命令在后台执行，立即返回 bgN 编号（不阻塞）；"
                                "结果写入会话专属临时目录的 bgN.log（提交时返回绝对路径，编号与文件一一对应），"
                                "用 Read/Grep 读取该文件；"
                                "输出随任务运行分块落盘（输出量小的任务可能到结束才可见）；"
                                "并发上限8个（bg1~bg8，终态槽位自动释放复用，"
                                "复用前旧结果归档为 bgN.log.N.prev，仍可 Read 读取、不丢失），"
                                "会话结束自动清理全部后台任务"
                            ),
                        },
                        "bg": {
                            "type": "string",
                            "description": (
                                "后台任务管理操作："
                                "status 查看所有后台任务状态（编号/状态/退出码/输出大小/结果路径）；"
                                "wait 挂起等待任意后台任务完成（有完成立即返回，超时返回最新状态快照）；"
                                "cancel 取消 bgN（杀进程树，已产出内容保留可读，配合 id）"
                            ),
                        },
                        "id": {
                            "type": "integer",
                            "description": "后台任务编号（bg=cancel 时必填，如 id=3 取消 bg3）",
                        },
                    },
                    "required": [],
                },
            },
        }

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """执行一次 Shell 调用（前台执行或后台任务管理）。

        Args:
            args: LLM 提交的参数（command/timeout/max_output_chars/max_output_tokens/
                background/bg/id；缺失按默认值处理）。
            env: 运行时门面（只读配置 + 确认回调 + 删除确认挂起门）；None 表示未注入，
                此时不做安全确认拦截、不做超时上限钳制。

        Returns:
            `ToolResult`：文本仍带框架标签（由注册表判定失败并剥离后交 LLM）；
            挂起确认时 `await_confirm=True` 且文本为 `__AWAIT_CONFIRM__`。
        """
        # ── 数值归一化：AI可能传字符串类型的数值参数，统一转int ──
        try:
            timeout = int(args["timeout"]) if args.get("timeout") is not None else 120
            if args.get("max_output_tokens") is not None:
                max_output_chars = args.get("max_output_tokens")  # 别名：归一化后统一走字符语义
            else:
                max_output_chars = args.get("max_output_chars")
            max_output_chars = int(max_output_chars) if max_output_chars is not None else 4000
        except (TypeError, ValueError):
            return self._text(error_line("timeout/max_output_chars需为整数"))

        command = args.get("command")
        command = "" if command is None else str(command)
        background = bool(args.get("background"))
        bg = args.get("bg")
        task_id = args.get("id")

        # ── 安全检查：删除命令和git命令根据配置决定是否需要确认 ──
        # 后台提交与前台同一套确认（bg 管理操作无 command 自然跳过）
        pending = self._confirm_gate(
            command, timeout, max_output_chars, background, bg, task_id, env
        )
        if pending is not None:
            return pending

        # ── 后台任务分发（bg 参数）：前台逻辑不变 ──
        if background or bg:
            limit = env.settings.max_timeout_seconds if env is not None else None
            return self._text(
                self._background.dispatch(command, timeout, background, bg, task_id, limit)
            )

        if not command:
            return self._text(error_line(
                "command为空：前台执行需提供 command；"
                "后台任务用 background=true 提交，管理用 bg=status/wait/cancel"
            ))

        if timeout <= 0:
            return self._text(error_line("timeout需为正整数（秒）"))

        if env is not None and env.settings.max_timeout_seconds > 0:
            timeout = min(timeout, env.settings.max_timeout_seconds)

        return self._text(self._executor.run(command, timeout, max_output_chars))

    # ═══════════════════════════════════════════════════════════
    # 内部：结果包装与安全确认门禁
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _text(text: str) -> ToolResult:
        """包装纯文本结果（框架标签保留，交注册表统一判定/剥离）。"""
        return ToolResult(llm_text=text)

    @staticmethod
    def _confirm_gate(command: str, timeout: int, max_output_chars: int, background: bool,
                      bg: object, task_id: object, env: ToolEnv | None) -> ToolResult | None:
        """安全确认门禁：需确认时返回结果对象（拒绝文案或挂起标记），无需确认返回 None。

        - 命中删除类命令且未开启「rm免确认」、或命中 git 命令且未开启「git免确认」
          时才需要确认（大小写不敏感）；
        - Windows：经宿主注入的同步回调询问，拒绝返回 `[操作已取消: 此命令需用户确认]`，
          回调缺失时不拦截；
        - 非 Windows：暂存本次调用的完整参数并返回 `__AWAIT_CONFIRM__`（命令未执行），
          主循环确认后带「已确认」标记重执行，该标记消费后直接放行；
        - 未注入宿主上下文（env 为 None）时不拦截，直接执行。
        """
        if not command or env is None:
            return None
        settings = env.settings
        if not settings.rm_skip_confirm and RE_DELETE.search(command):
            need_confirm = True
        elif not settings.git_skip_confirm and RE_GIT.search(command):
            need_confirm = True
        else:
            need_confirm = False
        if not need_confirm:
            return None

        if sys.platform == "win32":
            if env.confirm is not None and not env.confirm(command):
                return ShellTool._text("[操作已取消: 此命令需用户确认]")
            return None

        gate = env.delete_gate
        if gate.consume_confirmed():
            return None
        gate.pend("Shell", {
            "command": command,
            "timeout": timeout,
            "max_output_chars": max_output_chars,
            "background": background,
            "bg": bg,
            "id": task_id,
        })
        return ToolResult(llm_text=AWAIT_CONFIRM, await_confirm=True)
