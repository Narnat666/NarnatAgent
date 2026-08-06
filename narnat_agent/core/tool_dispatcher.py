"""工具调度器 —— 并行/串行执行策略

从agent.py中提取，负责工具调用的分组、调度和执行。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import List, Dict, Any, Tuple, Optional

from ..tools.registry import execute as tool_execute
from ..tools.bash import kill_active as _kill_bash
from ..tools.terminal import kill_active_exec as _kill_terminal_exec
from ..tools.terminal import resolve_dev_display as _dev_display
from ..tools.serial import kill_active_exec as _kill_serial_exec
from ..tools.tool_context import ToolContext
from ..output import write as _stdout_write, D, E, R, Y, G, B, C


def _local_hostname() -> str:
    """本机主机名（用于UI判断dev0显示）"""
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "localhost"


# ── 工具分类 ──
_READONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch"}
_WRITE_TOOLS = {"Edit", "Write"}
_SERIAL_TOOLS = {"Shell", "Terminal", "TodoWrite", "Serial"}

# 工具名→简短描述映射
_TOOL_LABELS = {
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
}

# 工具摘要提取：文件类工具取file_path
_FILE_PATH_TOOLS = {"Read", "Edit", "Write"}


class ToolDispatcher:
    """工具调度器"""

    def __init__(self, tool_context: ToolContext, executor: ThreadPoolExecutor, logger=None):
        self._tool_context = tool_context
        self._executor = executor
        self._logger = logger

    def execute_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
        stream,
    ) -> List[Tuple[str, str]]:
        """
        并行执行工具调用，返回 [(tool_call_id, result), ...] 按原始顺序。

        调度策略：
        1. 只读工具（Read/Glob/Grep/WebSearch）→ 全部并行
        2. 写入工具（Edit/Write）→ 按file_path分组，同文件串行，不同文件并行
        3. 串行工具（Bash/TodoWrite）→ 逐个串行

        三组之间串行执行：只读 → 写入 → 串行，保证写入看到最新文件状态。
        """
        # 计划优先拦截
        blocked = self._check_plan_required(tool_calls)
        if blocked is not None:
            return blocked

        # 解析所有tool_call
        parsed: List[Tuple[str, str, dict]] = []
        for tc in tool_calls:
            func = tc["function"]
            name = func["name"]
            try:
                arguments = json.loads(func["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            parsed.append((tc["id"], name, arguments))

        # 按分类分组，保留原始索引
        readonly_group = []
        write_group = {}
        serial_group = []

        for idx, (tc_id, name, arguments) in enumerate(parsed):
            if name in _READONLY_TOOLS:
                readonly_group.append((idx, tc_id, name, arguments))
            elif name in _WRITE_TOOLS:
                fp = arguments.get("file_path", "")
                write_group.setdefault(fp, []).append((idx, tc_id, name, arguments))
            else:
                serial_group.append((idx, tc_id, name, arguments))

        # 结果容器
        results: Dict[int, Tuple[str, str]] = {}

        # ── 阶段1：只读工具并行 ──
        if readonly_group:
            self._run_parallel(readonly_group, results, stream)

        if stream.cancelled:
            _kill_bash()
            _kill_terminal_exec()
            _kill_serial_exec()
            return [results[i] for i in range(len(parsed)) if i in results]

        # ── 阶段2：写入工具按文件分组 ──
        if write_group:
            file_groups = list(write_group.values())
            if len(file_groups) == 1:
                for item in file_groups[0]:
                    idx, tc_id, name, arguments = item
                    if stream.cancelled:
                        break
                    fut = self._executor.submit(self._run_single, tc_id, name, arguments, stream)
                    while not fut.done():
                        if stream.cancelled:
                            _kill_bash()
                            _kill_terminal_exec()
                            _kill_serial_exec()
                            break
                        time.sleep(0.2)
                    if stream.cancelled:
                        break
                    try:
                        result = fut.result()
                        results[idx] = (tc_id, result)
                    except Exception as e:
                        results[idx] = (tc_id, f"[错误: 工具执行失败: {e}]")
            else:
                futures = {}
                for group in file_groups:
                    fut = self._executor.submit(
                        self._run_sequential_group, group, results, stream
                    )
                    futures[fut] = group
                remaining = set(futures.keys())
                while remaining:
                    if stream.cancelled:
                        _kill_bash()
                        _kill_terminal_exec()
                        _kill_serial_exec()
                        break
                    done, remaining = wait(remaining, timeout=0.2, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            fut.result()
                        except Exception:
                            pass

        if stream.cancelled:
            return [results[i] for i in range(len(parsed)) if i in results]

        # ── 阶段3：串行工具 ──
        if serial_group:
            for item in serial_group:
                idx, tc_id, name, arguments = item
                if stream.cancelled:
                    break
                fut = self._executor.submit(self._run_single, tc_id, name, arguments, stream)
                while not fut.done():
                    if stream.cancelled:
                        _kill_bash()
                        _kill_terminal_exec()
                        _kill_serial_exec()
                        break
                    time.sleep(0.2)
                if stream.cancelled:
                    break
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败: {e}]")

        return [results[i] for i in range(len(parsed)) if i in results]

    def _run_single(self, tc_id: str, name: str, arguments: dict, stream) -> str:
        """执行单个工具调用，处理UI和日志"""
        # UI: 暂停spinner，flush渲染缓冲区，显示工具调用摘要
        stream.pause_spinner()
        stream.flush_renderer()
        self._show_tool_call(name, arguments)

        # 执行工具
        llm_result, color_diff = tool_execute(name, arguments, self._tool_context)
        self._logger.info(
            f"tools.{name.lower()}",
            f"调用: {json.dumps(arguments, ensure_ascii=False)[:200]}",
        )
        self._logger.info(
            f"tools.{name.lower()}",
            f"结果: {llm_result[:200] if llm_result else '(空)'}",
        )

        # 展示着色diff
        if color_diff:
            self._show_diff(color_diff)

        stream.resume_spinner()
        return llm_result

    def _run_parallel(self, group, results, stream) -> None:
        """并行执行一组工具调用"""
        if not group:
            return
        if len(group) == 1:
            idx, tc_id, name, arguments = group[0]
            if not stream.cancelled:
                fut = self._executor.submit(self._run_single, tc_id, name, arguments, stream)
                while not fut.done():
                    if stream.cancelled:
                        _kill_bash()
                        _kill_terminal_exec()
                        _kill_serial_exec()
                        break
                    time.sleep(0.2)
                if stream.cancelled:
                    return
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败({name}): {e}]")
            return

        futures = {}
        for idx, tc_id, name, arguments in group:
            if stream.cancelled:
                break
            fut = self._executor.submit(self._run_single, tc_id, name, arguments, stream)
            futures[fut] = (idx, tc_id, name)
        remaining = set(futures.keys())
        while remaining:
            if stream.cancelled:
                _kill_bash()
                _kill_terminal_exec()
                _kill_serial_exec()
                break
            done, remaining = wait(remaining, timeout=0.2, return_when=FIRST_COMPLETED)
            for fut in done:
                idx, tc_id, name = futures[fut]
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败({name}): {e}]")

    def _run_sequential_group(self, group, results, stream) -> None:
        """串行执行同一文件组的写入工具"""
        for idx, tc_id, name, arguments in group:
            if stream.cancelled:
                break
            result = self._run_single(tc_id, name, arguments, stream)
            results[idx] = (tc_id, result)

    def _check_plan_required(self, tool_calls: List[Dict[str, Any]]) -> Optional[List[Tuple[str, str]]]:
        """计划优先拦截：require_plan开启时，非TodoWrite工具需先有in_progress的todo"""
        ctx = self._tool_context
        if not ctx.require_plan:
            return None

        non_todo_names = []
        non_todo_ids = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            if name != "TodoWrite":
                non_todo_names.append(name)
                non_todo_ids.append(tc["id"])

        if not non_todo_names:
            return None

        if len(non_todo_names) < ctx.min_tools:
            return None

        has_active = any(
            t.get("status") == "in_progress"
            for t in ctx.current_todos
        )
        if has_active:
            return None

        if self._logger:
            self._logger.info("dispatcher", f"计划优先拦截: {non_todo_names}")
        hint = (
            f"[计划优先模式已开启: 请先使用TodoWrite制定计划（至少1项in_progress），"
            f"再执行其他工具。当前尝试调用的工具: {', '.join(non_todo_names)}]"
        )
        results = []
        for i, tc_id in enumerate(non_todo_ids):
            results.append((tc_id, hint if i == 0 else "[计划优先拦截，详见上方]"))
        return results

    @staticmethod
    def _fmt_cmd(raw: str, empty_label: str = "(空命令)") -> str:
        """格式化命令摘要：有内容返回内容，仅换行→(换行)，仅空格→(空格)，空→默认标签"""
        stripped = raw.strip()
        if stripped:
            return stripped
        if "\n" in raw:
            return "(换行)"
        if raw:  # 非空但 strip 后为空 → 纯空格
            return "(空格)"
        return empty_label

    def _show_tool_call(self, name: str, arguments: dict):
        """在终端显示工具调用摘要"""
        label = _TOOL_LABELS.get(name, name)
        summary = ""
        if name in _FILE_PATH_TOOLS:
            dev_raw = arguments.get("device", "")
            # 只有远程设备(dev1..devn)才显示设备；dev0/省略=本机不显示
            dev = _dev_display(dev_raw) if dev_raw else ""
            if dev == _local_hostname():
                dev = ""
            fp = arguments.get("file_path", "")
            summary = f"{dev}:{fp}" if dev else fp
        elif name == "Shell":
            summary = self._fmt_cmd(arguments.get("command", ""))
        elif name == "Terminal":
            action = arguments.get("action", "")
            if not action and arguments.get("command", ""):
                action = "exec"
            sid = arguments.get("session_id", -1)
            sid_str = f"[dev{sid + 1}]" if sid >= 0 else ""
            if action == "connect":
                host = arguments.get("host", "")
                username = arguments.get("username", "")
                summary = f"connect{sid_str} {username}@{host}"
            elif action == "exec":
                dev_raw = arguments.get("host", "")
                dev = _dev_display(dev_raw) if dev_raw else ""
                dev_str = f"[{dev}]" if dev else ""
                summary = f"exec{dev_str} {self._fmt_cmd(arguments.get('command', ''))}"
            elif action == "status":
                summary = "status"
            elif action == "close":
                dev = arguments.get("host", "")
                summary = f"close {_dev_display(dev)}" if dev else "close"
            elif action == "transfer":
                src_h = _dev_display(arguments.get("source_host", ""))
                src_p = arguments.get("source_path", "")
                tgt_h = _dev_display(arguments.get("target_host", ""))
                tgt_p = arguments.get("target_path", "")
                summary = f"transfer {src_h}:{src_p} → {tgt_h}:{tgt_p}"
            elif action == "input":
                dev_raw = arguments.get("host", "")
                dev = _dev_display(dev_raw) if dev_raw else ""
                dev_str = f"[{dev}]" if dev else ""
                summary = f"input{dev_str} {self._fmt_cmd(arguments.get('input', ''), '(空)')}"
            else:
                summary = f"{action or '(未知)'}{sid_str}"
        elif name == "Grep":
            summary = arguments.get("pattern", "")
        elif name == "Glob":
            summary = arguments.get("pattern", "")
        elif name == "WebSearch":
            summary = arguments.get("query", "")
        elif name == "TodoWrite":
            todos = arguments.get("todos", [])
            summary = f"{len(todos)}项" if todos else "(空)"
        elif name == "Serial":
            action = arguments.get("action", "exec")
            sid = arguments.get("session_id", -1)
            sid_str = f"[{sid}]" if sid >= 0 else ""
            if action == "scan":
                summary = "scan"
            elif action == "connect":
                port = arguments.get("port", "")
                baud = arguments.get("baudrate", 115200)
                summary = f"connect{sid_str} {port} @{baud}"
            elif action == "exec":
                summary = f"exec{sid_str} {self._fmt_cmd(arguments.get('command', ''))}"
            elif action == "raw_exec":
                summary = f"raw_exec{sid_str} {self._fmt_cmd(arguments.get('command', ''))}"
            elif action == "input":
                summary = f"input{sid_str} {self._fmt_cmd(arguments.get('input', ''), '(空)')}"
            elif action == "status":
                summary = "status"
            elif action == "close":
                summary = f"close{sid_str}"
            else:
                summary = f"{action}{sid_str}"

        if summary:
            _stdout_write(f"  {D}[{label}] {summary}{R}\n")
        else:
            _stdout_write(f"  {D}[{label}]{R}\n")

    def _show_diff(self, color_diff: str):
        """在终端展示着色diff"""
        buf = "\n".join(f"  {line}" for line in color_diff.split("\n"))
        _stdout_write(buf + "\n\n")
