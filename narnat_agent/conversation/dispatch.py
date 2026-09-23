"""工具调度 —— 计划优先拦截、三阶段分组执行、结果顺序回传与终端调度行。

契约来源：`openspec/changes/recast-v2/specs/conversation/spec.md`
（「工具调度分组与结果回传」「计划优先拦截」「兼容性怪癖保持」），显示面另对齐
specs/tools-shell「退出码与错误标签协议」、specs/tools-file「编辑类工具的返回形态与
差分展示」、specs/tools-todo「状态同步与终端显示」、specs/ui「headless 纯文本输出」
（调度日志默认静默）。

行为搬运自旧实现 `narnat_agent/core/tool_dispatcher.py`（分组策略、并行/串行编排、
结果顺序与缺失语义、终端调度行文案逐条等价），结构变化（design D1/D6/D7）：
- 取消（`sink.cancelled`）经注入的 `InterruptSignal.raise_()` 广播收敛：杀前台命令 /
  远程会话 / 串口进程由中断订阅者在装配时接线，本模块不再直接 import 各工具族；
- 工具统一经注册表执行入口 `execute(name, args, env)`，取结构化 `ToolResult`
  （框架标签的判定与剥离、全局截断已在注册表完成）；
- 设备显示名解析、颜色主题、日志端口全部构造注入；无模块级可变状态。
"""
from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Protocol

from ..contracts.interrupt import InterruptSignal
from ..contracts.tool import ToolEnv, ToolResult

__all__ = [
    "ERROR_PREFIX",
    "FILE_PATH_TOOLS",
    "MCP_PREFIX",
    "PLAN_BLOCK_HINT",
    "PLAN_BLOCK_PLACEHOLDER",
    "PLAN_TOOL_NAME",
    "READONLY_TOOLS",
    "TAGGED_TOOLS",
    "TOOL_LABELS",
    "WRITE_TOOLS",
    "CancelProbe",
    "ConsolePort",
    "DispatchTheme",
    "LogPort",
    "ToolDispatcher",
    "ToolExecutor",
    "group_tool_calls",
    "local_hostname",
    "parse_tool_call",
    "plan_block_hint",
]

# ═══════════════════════════════════════════════════════════════
# 工具分类与固定文案
# ═══════════════════════════════════════════════════════════════

READONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "WebSearch"})
"""只读类工具（并行执行；specs/conversation「工具调度分组与结果回传」）。"""

WRITE_TOOLS = frozenset({"Edit", "Write"})
"""写入类工具（按目标文件分组，同文件串行、不同文件并行）。"""

PLAN_TOOL_NAME = "TodoWrite"
"""计划工具名（串行组内稳定排序到最前；批内含它则不触发计划优先拦截）。"""

TAGGED_TOOLS = ("Shell", "Terminal")
"""只认框架错误标签的命令类工具（UI 失败判定依据，specs/tools-shell）。"""

MCP_PREFIX = "mcp__"
"""MCP 工具名前缀（工具标签与失败判定同命令类工具口径）。"""

FILE_PATH_TOOLS = frozenset({"Read", "Edit", "Write"})
"""摘要取 `file_path` 的文件类工具。"""

ERROR_PREFIX = "[错误"
"""非命令类工具的失败判定前缀（结果文本前缀，specs/conversation 兼容怪癖）。"""

TOOL_LABELS = {
    "Read": "读取",
    "Glob": "搜索文件",
    "Grep": "搜索内容",
    "Edit": "编辑",
    "Write": "写入",
    "Shell": "执行命令",
    "Terminal": "终端",
    "WebSearch": "联网搜索",
    "TodoWrite": "更新计划",
    "Serial": "串口",
    "MCP": "MCP",
    "GoalComplete": "声明完成",
}
"""工具名 → 终端显示标签（未知工具名原样显示）。"""

PLAN_BLOCK_HINT = (
    "[计划优先模式已开启: 请先使用TodoWrite制定计划（至少1项in_progress），"
    "再执行其他工具。当前尝试调用的工具: {names}]"
)
"""计划优先拦截的首条结果文案（已发布字面量，精确匹配）。"""

PLAN_BLOCK_PLACEHOLDER = "[计划优先拦截，详见上方]"
"""被拦工具的其余结果占位（已发布字面量）。"""


# ═══════════════════════════════════════════════════════════════
# 端口（结构化协议：实现方在装配点注入）
# ═══════════════════════════════════════════════════════════════


class ToolExecutor(Protocol):
    """工具执行入口面（实现方：`tools.registry.ToolRegistry`）。"""

    def execute(self, name: str, arguments: Mapping[str, object] | None,
                env: ToolEnv | None = None) -> ToolResult:
        """执行单个工具；结果文本已剥离框架标签、已按全局上限截断。"""
        ...


class ConsolePort(Protocol):
    """终端写出面（实现方：`output.console.Console`）。"""

    def write(self, text: str) -> None:
        """阻塞写出（串行化、立即刷新）。"""
        ...

    def is_quiet_tools(self) -> bool:
        """工具调度日志是否静默（headless 默认静默，`-l` 恢复全量）。"""
        ...


class DispatchTheme(Protocol):
    """调度行颜色面（实现方：`output.style.Theme`，缺省为无色）。"""

    d: object
    """暗淡色（调用摘要行）。"""

    r: object
    """复位。"""

    c_error: object
    """错误色（失败提示行）。"""


class CancelProbe(Protocol):
    """取消查询面（实现方：`contracts.output.OutputSink`）。"""

    @property
    def cancelled(self) -> bool:
        """是否已取消（进程级中断状态；headless 恒为 False）。"""
        ...


class DisplayProbe(CancelProbe, Protocol):
    """工具行显示协调面（实现方：`contracts.output.OutputSink`）。

    单个工具执行前先 `pause()` 暂停动画、`flush()` 落定渲染缓冲（保证 AI 本轮
    已输出的文本先于工具行落到终端），执行与差异/失败显示完成后 `resume()`
    恢复动画（specs/conversation「工具调度分组与结果回传」；静默模式下同样执行）。
    """

    def pause(self) -> None:
        """暂停动画（并行计数 +1；首个暂停者真正停止动画）。"""
        ...

    def flush(self) -> None:
        """落定渲染缓冲（守卫：从未注入内容时不动作）。"""
        ...

    def resume(self) -> None:
        """恢复动画（计数递减且不为负；归零时才重启；已中止后不再启动）。"""
        ...


class LogPort(Protocol):
    """日志端口（构造注入；未注入则不记录）。"""

    def info(self, module: str, message: str) -> None:
        """记录一条 info 日志。"""
        ...


# ═══════════════════════════════════════════════════════════════
# 解析与分组（纯函数，供单测直接驱动）
# ═══════════════════════════════════════════════════════════════


def parse_tool_call(tool_call: Mapping[str, Any]) -> tuple[str, str, dict]:
    """解析一条工具调用为 `(id, 工具名, 参数)`；参数非法 JSON 时按空参数处理。"""
    function = tool_call["function"]
    name = function["name"]
    try:
        arguments = json.loads(function["arguments"])
    except json.JSONDecodeError:
        arguments = {}
    return tool_call["id"], name, arguments


def group_tool_calls(
    parsed: Sequence[tuple[str, str, dict]],
) -> tuple[list[tuple[int, str, str, dict]], dict[str, list[tuple[int, str, str, dict]]],
           list[tuple[int, str, str, dict]]]:
    """按三类策略分组，保留原始下标（返回 `(只读组, 写入组字典, 串行组)`）。

    - 只读类：按出现顺序进只读组（并行执行）；
    - 写入类：按 `file_path` 分桶（同文件内保持提交顺序、不同文件并行）；
    - 其余全部工具：进串行组，并把计划工具稳定排序到最前（其余保持原始顺序）。
    """
    readonly: list[tuple[int, str, str, dict]] = []
    write_groups: dict[str, list[tuple[int, str, str, dict]]] = {}
    serial: list[tuple[int, str, str, dict]] = []

    for index, (tc_id, name, arguments) in enumerate(parsed):
        entry = (index, tc_id, name, arguments)
        if name in READONLY_TOOLS:
            readonly.append(entry)
        elif name in WRITE_TOOLS:
            write_groups.setdefault(arguments.get("file_path", ""), []).append(entry)
        else:
            serial.append(entry)

    serial.sort(key=lambda item: item[2] != PLAN_TOOL_NAME)
    return readonly, write_groups, serial


def plan_block_hint(names: Sequence[str]) -> str:
    """计划优先拦截的提示文案（首条结果的固定文案）。"""
    return PLAN_BLOCK_HINT.format(names=", ".join(names))


def local_hostname() -> str:
    """本机主机名（用于判断 `dev0` 显示是否需要省略）；取不到回退 `localhost`。"""
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


def _fmt_cmd(raw: str, empty_label: str = "(空命令)") -> str:
    """格式化命令摘要：有内容返回内容，仅换行 → `(换行)`，仅空格 → `(空格)`。"""
    stripped = raw.strip()
    if stripped:
        return stripped
    if "\n" in raw:
        return "(换行)"
    if raw:
        return "(空格)"
    return empty_label


def _mcp_target_hint(config: object) -> str:
    """MCP connect 摘要里的目标程序（脚本或命令的后两段路径，便于人眼核对）。"""
    if not isinstance(config, dict):
        return ""
    args = [str(a) for a in (config.get("args") or [])]
    candidates: list[str] = []
    if args and not args[0].startswith("-"):
        candidates.append(args[0])
    candidates.append(str(config.get("command") or ""))
    for text in candidates:
        parts = [p for p in text.replace("\\", "/").split("/") if p]
        if parts:
            return "/".join(parts[-2:])
    return ""


# ═══════════════════════════════════════════════════════════════
# 调度器
# ═══════════════════════════════════════════════════════════════


class ToolDispatcher:
    """工具调度器 —— 批调用的分组执行、终端调度行与结果回传。

    执行策略（三个阶段串行推进，保证写入看到最新文件状态）：

    1. 只读类（Read/Glob/Grep/WebSearch）并行；
    2. 写入类（Edit/Write）按目标文件分组：同文件内串行、不同文件并行；
    3. 其余全部工具（命令、终端、串口、计划工具、MCP 与未知工具）逐个串行。

    线程池由装配点持有并通过构造注入（生命周期归 app）；取消时以
    `InterruptSignal.raise_()` 广播收敛，返回已完成的部分结果。
    """

    def __init__(
        self,
        registry: ToolExecutor,
        env: ToolEnv,
        executor: ThreadPoolExecutor,
        console: ConsolePort,
        *,
        theme: DispatchTheme | None = None,
        interrupt: InterruptSignal | None = None,
        resolve_device: Callable[[str], str] | None = None,
        logger: LogPort | None = None,
    ) -> None:
        """构造调度器。

        - `registry`：工具执行入口（内置与动态工具统一解析）；
        - `env`：工具运行时门面（只读配置面、计划状态、删除确认挂起门）；
        - `executor`：线程池（装配点持有并负责关闭）；
        - `console`：终端写出与静默开关；
        - `theme`：颜色来源（None 时无色输出）；
        - `interrupt`：中断信号总线（取消时广播，杀进程由订阅者执行）；
        - `resolve_device`：设备引用 → 显示名解析（前端 `TerminalTool.resolve_dev_display`
          绑定方法）；None 时原样显示；
        - `logger`：日志端口（None 时不记录）。
        """
        self._registry = registry
        self._env = env
        self._executor = executor
        self._console = console
        self._theme = theme
        self._interrupt = interrupt
        self._resolve_device = resolve_device
        self._logger = logger

    # ── 主入口 ──

    def execute(self, tool_calls: Sequence[Mapping[str, Any]],
                sink: DisplayProbe) -> list[tuple[str, ToolResult]]:
        """执行一批工具调用，返回 `[(tool_call_id, ToolResult), ...]`。

        - 计划优先命中时整批拦截（不执行任何工具，回传提示与占位结果）；
        - 结果按原始调用顺序排列；未完成的条目不出现在结果中（由上层补记）；
        - 任一检查点发现取消即终止后续工具并返回已完成的部分结果。
        """
        blocked = self._plan_block(tool_calls)
        if blocked is not None:
            return blocked

        parsed = [parse_tool_call(tool_call) for tool_call in tool_calls]
        readonly, write_groups, serial = group_tool_calls(parsed)
        results: dict[int, tuple[str, ToolResult]] = {}

        if readonly:
            self._run_parallel(readonly, results, sink)
        if sink.cancelled:
            self._request_abort()
            return _collect_ordered(results, len(parsed))

        if write_groups:
            self._run_write_groups(write_groups, results, sink)
        if sink.cancelled:
            return _collect_ordered(results, len(parsed))

        if serial:
            self._run_serial(serial, results, sink)
        return _collect_ordered(results, len(parsed))

    def execute_one(self, name: str, arguments: dict) -> ToolResult:
        """执行单个工具调用（删除确认后重新执行同一命令）。

        结果文本已剥离框架标签；调用摘要与失败行不重复显示（与确认流程的
        终端输出约定一致）。
        """
        return self._registry.execute(name, arguments, self._env)

    # ── 计划优先拦截 ──

    def _plan_block(
        self, tool_calls: Sequence[Mapping[str, Any]],
    ) -> list[tuple[str, ToolResult]] | None:
        """计划优先拦截：返回被拦调用的结果列表，或 None 表示放行。

        拦截条件（全部满足）：配置开启、批内不含计划工具、非计划工具数量达到
        门槛、当前计划没有任何进行中项。
        """
        settings = self._env.settings
        if not settings.require_plan:
            return None

        names = [tool_call["function"]["name"] for tool_call in tool_calls]
        if PLAN_TOOL_NAME in names:
            return None

        if not names or len(names) < settings.min_tools:
            return None

        if any(todo.get("status") == "in_progress" for todo in self._env.plan.current()):
            return None

        if self._logger is not None:
            self._logger.info("dispatcher", f"计划优先拦截: {names}")
        hint = plan_block_hint(names)
        blocked: list[tuple[str, ToolResult]] = []
        for index, tool_call in enumerate(tool_calls):
            name = tool_call["function"]["name"]
            try:
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            self._show_tool_call(name, arguments)
            self._show_tool_failed(name)
            text = hint if index == 0 else PLAN_BLOCK_PLACEHOLDER
            blocked.append((tool_call["id"], ToolResult(llm_text=text)))
        return blocked

    # ── 阶段实现 ──

    def _run_parallel(self, group, results, sink: DisplayProbe) -> None:
        """只读阶段：全部并行，结果按原始顺序回传。"""
        if len(group) == 1:
            index, tc_id, name, arguments = group[0]
            if sink.cancelled:
                return
            future = self._executor.submit(self._run_single, name, arguments, sink)
            if not self._wait_future(future, sink):
                return
            self._store_result(future, index, tc_id, name, results, with_name=True)
            return

        futures: dict[Future, tuple[int, str, str]] = {}
        for index, tc_id, name, arguments in group:
            if sink.cancelled:
                break
            future = self._executor.submit(self._run_single, name, arguments, sink)
            futures[future] = (index, tc_id, name)
        remaining = set(futures)
        while remaining:
            if sink.cancelled:
                self._request_abort()
                break
            done, remaining = wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                self._store_result(future, *futures[future], results, with_name=True)

    def _run_write_groups(self, write_groups, results, sink: DisplayProbe) -> None:
        """写入阶段：单文件组主线程逐个串行；多文件组并行、组内串行。"""
        file_groups = list(write_groups.values())
        if len(file_groups) == 1:
            for index, tc_id, name, arguments in file_groups[0]:
                if sink.cancelled:
                    break
                future = self._executor.submit(self._run_single, name, arguments, sink)
                if not self._wait_future(future, sink):
                    break
                self._store_result(future, index, tc_id, name, results, with_name=False)
            return

        futures: dict[Future, list] = {}
        for group in file_groups:
            futures[self._executor.submit(self._run_sequential_group, group, results,
                                          sink)] = group
        remaining = set(futures)
        while remaining:
            if sink.cancelled:
                self._request_abort()
                break
            done, remaining = wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except Exception:
                    # 组内某工具执行时崩溃：未执行到的工具补失败提示（结果语义不变）
                    for _index, _tc_id, name, _arguments in futures[future]:
                        if _index not in results:
                            self._show_tool_failed(name)

    def _run_serial(self, group, results, sink: DisplayProbe) -> None:
        """串行阶段：命令、终端、串口、计划、MCP 与未知工具逐个执行。"""
        for index, tc_id, name, arguments in group:
            if sink.cancelled:
                break
            future = self._executor.submit(self._run_single, name, arguments, sink)
            if not self._wait_future(future, sink):
                break
            self._store_result(future, index, tc_id, name, results, with_name=False)

    def _run_sequential_group(self, group, results, sink: DisplayProbe) -> None:
        """串行执行同一文件组的写入工具（由线程池调度，不同文件组并行）。"""
        for index, tc_id, name, arguments in group:
            result = self._run_single(name, arguments, sink)
            results[index] = (tc_id, result)

    def _run_single(self, name: str, arguments: dict, sink: DisplayProbe) -> ToolResult:
        """执行单个工具调用：停动画并落定文本 → 显示摘要 → 执行 → 差异 / 失败提示 → 恢复动画。

        显示摘要前先 `pause()` + `flush()`（AI 本轮已输出的文本先于工具行落到终端），
        显示完成后 `resume()`（finally 保证异常路径计数平衡）。
        """
        sink.pause()
        sink.flush()
        try:
            self._show_tool_call(name, arguments)
            result = self._registry.execute(name, arguments, self._env)
            if self._logger is not None:
                self._logger.info(
                    f"tools.{name.lower()}",
                    f"调用: {json.dumps(arguments, ensure_ascii=False)[:200]}",
                )
                self._logger.info(
                    f"tools.{name.lower()}",
                    f"结果: {result.llm_text[:200] if result.llm_text else '(空)'}",
                )
            if result.ui_text:
                self._show_diff(result.ui_text)
            elif self._is_failed(name, result):
                self._show_tool_failed(name)
            return result
        finally:
            sink.resume()

    def _wait_future(self, future: Future, sink: CancelProbe) -> bool:
        """等待任务完成并响应取消；返回 False 表示已取消（调用方应终止本阶段）。

        取消时触发中断广播：清理正在运行的命令 / 终端 / 串口进程由中断订阅者执行
        （旧实现在此调用各工具族的 kill，现经 `InterruptSignal` 统一收敛）。
        """
        while not future.done():
            if sink.cancelled:
                break
            time.sleep(0.05)
        if sink.cancelled:
            self._request_abort()
            return False
        return True

    def _store_result(self, future: Future, index: int, tc_id: str, name: str,
                      results: dict, *, with_name: bool) -> None:
        """收下任务结果；执行异常按所在阶段回传对应形态的错误文本并补失败提示。"""
        try:
            results[index] = (tc_id, future.result())
        except Exception as exc:
            results[index] = (tc_id, ToolResult(llm_text=_failure_text(name, exc, with_name)))
            self._show_tool_failed(name)

    def _request_abort(self) -> None:
        """取消时触发中断广播（杀前台命令/远程/串口进程由中断订阅者执行）。"""
        if self._interrupt is not None:
            self._interrupt.raise_()

    # ── 失败判定与显示 ──

    @staticmethod
    def _is_failed(name: str, result: ToolResult) -> bool:
        """UI 失败显示判定：命令类与 MCP 工具只认框架错误标签，其余以文本前缀判定。"""
        if name in TAGGED_TOOLS or name.startswith(MCP_PREFIX):
            return result.is_error
        return result.llm_text.startswith(ERROR_PREFIX)

    def _colors(self) -> tuple[str, str, str]:
        """取调度行颜色 `(摘要色, 复位, 错误色)`；未注入主题时全部为空串。"""
        theme = self._theme
        if theme is None:
            return "", "", ""
        return str(theme.d), str(theme.r), str(theme.c_error)

    def _tool_label(self, name: str) -> str:
        """工具名 → 终端显示标签；MCP 工具显示所属服务器。"""
        if name.startswith(MCP_PREFIX):
            parts = name.split("__", 2)
            if len(parts) == 3:
                return f"MCP:{parts[1]}"
        return TOOL_LABELS.get(name, name)

    def _resolve_display(self, dev: str) -> str:
        """设备引用 → 显示名（未注入解析器时原样返回）。"""
        if self._resolve_device is None:
            return dev
        return self._resolve_device(dev)

    def _show_tool_call(self, name: str, arguments: dict) -> None:
        """在终端显示工具调用摘要（静默模式下跳过）。"""
        if self._console.is_quiet_tools():
            return
        label = self._tool_label(name)
        summary = self._summary(name, arguments)
        dim, reset, _error = self._colors()
        if summary:
            self._console.write(f"  {dim}[{label}] {summary}{reset}\n")
        else:
            self._console.write(f"  {dim}[{label}]{reset}\n")

    def _summary(self, name: str, arguments: dict) -> str:
        """摘要文本（各工具族的固定形态，逐条搬运旧实现）。"""
        if name.startswith(MCP_PREFIX):
            parts = name.split("__", 2)
            if len(parts) == 3:
                summary = parts[2]
                if arguments:
                    args_text = json.dumps(arguments, ensure_ascii=False,
                                           separators=(",", ":"))
                    return f"{summary} {args_text}"
            return ""
        if name in FILE_PATH_TOOLS:
            dev_raw = arguments.get("device", "")
            # 只有远程设备(dev1..devn)才显示设备；dev0/省略=本机不显示
            dev = self._resolve_display(dev_raw) if dev_raw else ""
            if dev == local_hostname():
                dev = ""
            file_path = arguments.get("file_path", "")
            return f"{dev}:{file_path}" if dev else file_path
        if name == "Shell":
            bg_op = arguments.get("bg", "")
            if arguments.get("background"):
                return f"后台提交 {_fmt_cmd(arguments.get('command', ''))}"
            if bg_op == "status":
                return "后台状态"
            if bg_op == "wait":
                timeout = arguments.get("timeout", "")
                return "等待后台任务" + (f"(≤{timeout}s)" if timeout else "")
            if bg_op == "cancel":
                return f"取消后台任务 bg{arguments.get('id', '?')}"
            return _fmt_cmd(arguments.get("command", ""))
        if name == "Terminal":
            return self._terminal_summary(arguments)
        if name == "Grep":
            return arguments.get("pattern", "")
        if name == "Glob":
            return arguments.get("pattern", "")
        if name == "WebSearch":
            return arguments.get("query", "")
        if name == "TodoWrite":
            todos = arguments.get("todos", [])
            return f"{len(todos)}项" if todos else "(空)"
        if name == "MCP":
            return self._mcp_summary(arguments)
        if name == "Serial":
            return self._serial_summary(arguments)
        return ""

    def _terminal_summary(self, arguments: dict) -> str:
        """Terminal 工具摘要（动作 + 目标设备）。"""
        action = arguments.get("action", "")
        if not action and arguments.get("command", ""):
            action = "exec"
        session_id = arguments.get("session_id", -1)
        sid_str = f"[dev{session_id + 1}]" if session_id >= 0 else ""
        if action == "connect":
            host = arguments.get("host", "")
            username = arguments.get("username", "")
            return f"connect{sid_str} {username}@{host}"
        if action == "exec":
            dev_raw = arguments.get("host", "")
            dev = self._resolve_display(dev_raw) if dev_raw else ""
            dev_str = f"[{dev}]" if dev else ""
            return f"exec{dev_str} {_fmt_cmd(arguments.get('command', ''))}"
        if action == "status":
            return "status"
        if action == "close":
            dev = arguments.get("host", "")
            return f"close {self._resolve_display(dev)}" if dev else "close"
        if action == "transfer":
            source_host = self._resolve_display(arguments.get("source_host", ""))
            source_path = arguments.get("source_path", "")
            target_host = self._resolve_display(arguments.get("target_host", ""))
            target_path = arguments.get("target_path", "")
            return f"transfer {source_host}:{source_path} → {target_host}:{target_path}"
        if action == "input":
            dev_raw = arguments.get("host", "")
            dev = self._resolve_display(dev_raw) if dev_raw else ""
            dev_str = f"[{dev}]" if dev else ""
            return f"input{dev_str} {_fmt_cmd(arguments.get('input', ''), '(空)')}"
        return f"{action or '(未知)'}{sid_str}"

    def _mcp_summary(self, arguments: dict) -> str:
        """MCP 工具摘要（动作 + 目标；connect 附启动程序便于人眼核对）。"""
        action = arguments.get("action", "connect")
        target = arguments.get("name", "")
        if action == "connect":
            hint = _mcp_target_hint(arguments.get("config"))
            return f"connect {target}{f' ({hint})' if hint else ''}".strip()
        if action == "disconnect":
            return f"disconnect {target}".strip()
        return str(action)

    def _serial_summary(self, arguments: dict) -> str:
        """Serial 工具摘要（动作 + 会话槽）。"""
        action = arguments.get("action", "exec")
        session_id = arguments.get("session_id", -1)
        sid_str = f"[{session_id}]" if session_id >= 0 else ""
        if action == "scan":
            return "scan"
        if action == "connect":
            port = arguments.get("port", "")
            baud = arguments.get("baudrate", 115200)
            return f"connect{sid_str} {port} @{baud}"
        if action == "exec":
            return f"exec{sid_str} {_fmt_cmd(arguments.get('command', ''))}"
        if action == "raw_exec":
            return f"raw_exec{sid_str} {_fmt_cmd(arguments.get('command', ''))}"
        if action == "input":
            return f"input{sid_str} {_fmt_cmd(arguments.get('input', ''), '(空)')}"
        if action == "status":
            return "status"
        if action == "close":
            return f"close{sid_str}"
        return f"{action}{sid_str}"

    def _show_diff(self, ui_text: str) -> None:
        """展示着色差异（逐行前置两个空格缩进；静默模式下跳过）。"""
        if self._console.is_quiet_tools():
            return
        buffer = "\n".join(f"  {line}" for line in ui_text.split("\n"))
        # 空编辑的"[无差异]"是单行提示，其后不再加空行，直接接后续输出
        tail = "\n" if "\n" not in ui_text and "[无差异]" in ui_text else "\n\n"
        self._console.write(buffer + tail)

    def _show_tool_failed(self, name: str) -> None:
        """工具执行失败：终端显示一行失败提示（静默模式下跳过）。"""
        if self._console.is_quiet_tools():
            return
        _dim, reset, error = self._colors()
        label = self._tool_label(name)
        self._console.write(f"  {error}[{label}失败]{reset}\n")


def _collect_ordered(results: dict[int, tuple[str, ToolResult]],
                     total: int) -> list[tuple[str, ToolResult]]:
    """按原始调用顺序收集已完成结果（缺失条目不出现在结果中）。"""
    return [results[index] for index in range(total) if index in results]


def _failure_text(name: str, exc: Exception, with_name: bool) -> str:
    """调度层捕获执行异常时的错误文本。

    兼容怪癖保持：只读阶段带工具名、写入与串行阶段不带（specs/conversation）。
    """
    if with_name:
        return f"[错误: 工具执行失败({name}): {exc}]"
    return f"[错误: 工具执行失败: {exc}]"
