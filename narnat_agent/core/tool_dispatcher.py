"""工具调度器 —— 并行/串行执行策略

从agent.py中提取，负责工具调用的分组、调度和执行。
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import List, Dict, Any, Tuple

from ..tools.registry import (
    execute as tool_execute, get_capability, capability_names,
)
from ..tools.bash import kill_active as _kill_bash
from ..tools.terminal import kill_active_exec as _kill_terminal_exec
from ..tools.terminal import resolve_dev_display as _dev_display
from ..tools.serial import kill_active_exec as _kill_serial_exec
from ..tools.tool_context import ToolContext
from ..tools.exec_signal import has_error, strip_tags
from ..tools.param_utils import to_bool
from ..output import write as _stdout_write, D, R, X, is_quiet_tools


def _local_hostname() -> str:
    """本机主机名（用于UI判断dev0显示）"""
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "localhost"


def _mcp_target_hint(config) -> str:
    """MCP connect 摘要里的目标程序（脚本或命令的后两段路径，便于人眼核对）

    sysplorer_mcp/main.py / npx 这种；取不到返回空串。
    """
    if not isinstance(config, dict):
        return ""
    args = [str(a) for a in (config.get("args") or [])]
    candidates = []
    if args and not args[0].startswith("-"):
        candidates.append(args[0])
    candidates.append(str(config.get("command") or ""))
    for text in candidates:
        parts = [p for p in text.replace("\\", "/").split("/") if p]
        if parts:
            return "/".join(parts[-2:])
    return ""


# ═══════════════════════════════════════════════════════════════
# 工具调用行摘要风格表
#
# 键 = 工具 CAPABILITY["summary"]（见 tools/registry.py），生成函数收
# (工具名, 参数) 返回摘要文本；空串表示该调用不显示摘要。
# ═══════════════════════════════════════════════════════════════

def _style_file_path(name: str, arguments: dict) -> str:
    dev_raw = arguments.get("device", "")
    # 只有远程设备(dev1..devn)才显示设备；dev0/省略=本机不显示
    dev = _dev_display(dev_raw) if dev_raw else ""
    if dev == _local_hostname():
        dev = ""
    fp = arguments.get("file_path", "")
    return f"{dev}:{fp}" if dev else fp


def _style_command(name: str, arguments: dict) -> str:
    bg_op = arguments.get("bg", "")
    # 字符串布尔归一化（"false" 不能显示为后台提交；与 bash 执行层同一判定）
    if to_bool(arguments.get("background")):
        return f"后台提交 {ToolDispatcher._fmt_cmd(arguments.get('command', ''))}"
    if bg_op == "status":
        return "后台状态"
    if bg_op == "wait":
        t = arguments.get("timeout", "")
        return "等待后台任务" + (f"(≤{t}s)" if t else "")
    if bg_op == "cancel":
        return f"取消后台任务 bg{arguments.get('id', '?')}"
    return ToolDispatcher._fmt_cmd(arguments.get("command", ""))


def _style_device_host(name: str, arguments: dict) -> str:
    action = arguments.get("action", "")
    if not action and arguments.get("command", ""):
        action = "exec"
    sid = arguments.get("session_id", -1)
    sid_str = f"[dev{sid + 1}]" if sid >= 0 else ""
    if action == "connect":
        host = arguments.get("host", "")
        username = arguments.get("username", "")
        return f"connect{sid_str} {username}@{host}"
    if action == "exec":
        dev_raw = arguments.get("host", "")
        dev = _dev_display(dev_raw) if dev_raw else ""
        dev_str = f"[{dev}]" if dev else ""
        return f"exec{dev_str} {ToolDispatcher._fmt_cmd(arguments.get('command', ''))}"
    if action == "status":
        return "status"
    if action == "close":
        dev = arguments.get("host", "")
        return f"close {_dev_display(dev)}" if dev else "close"
    if action == "transfer":
        src_h = _dev_display(arguments.get("source_host", ""))
        src_p = arguments.get("source_path", "")
        tgt_h = _dev_display(arguments.get("target_host", ""))
        tgt_p = arguments.get("target_path", "")
        return f"transfer {src_h}:{src_p} → {tgt_h}:{tgt_p}"
    if action == "input":
        dev_raw = arguments.get("host", "")
        dev = _dev_display(dev_raw) if dev_raw else ""
        dev_str = f"[{dev}]" if dev else ""
        return f"input{dev_str} {ToolDispatcher._fmt_cmd(arguments.get('input', ''), '(空)')}"
    return f"{action or '(未知)'}{sid_str}"


def _style_pattern(name: str, arguments: dict) -> str:
    return arguments.get("pattern", "")


def _style_pattern_path(name: str, arguments: dict) -> str:
    return arguments.get("pattern", "")


def _style_query(name: str, arguments: dict) -> str:
    return arguments.get("query", "")


def _style_todos(name: str, arguments: dict) -> str:
    todos = arguments.get("todos", [])
    return f"{len(todos)}项" if todos else "(空)"


def _style_mcp(name: str, arguments: dict) -> str:
    if name.startswith("mcp__"):
        # MCP 动态工具（mcp__<服务器>__<工具>）：摘要显示工具名+参数（紧凑全量）
        parts = name.split("__", 2)
        if len(parts) != 3:
            return ""
        summary = parts[2]
        if arguments:
            args_text = json.dumps(arguments, ensure_ascii=False,
                                   separators=(",", ":"))
            summary += f" {args_text}"
        return summary
    # 内置 MCP：与 Terminal 同风格，动作 + 目标（括号内为启动程序，便于人眼核对连的是什么）
    action = arguments.get("action", "connect")
    target = arguments.get("name", "")
    if action == "connect":
        hint = _mcp_target_hint(arguments.get("config"))
        return f"connect {target}{f' ({hint})' if hint else ''}".strip()
    if action == "disconnect":
        return f"disconnect {target}".strip()
    return str(action)


def _style_serial(name: str, arguments: dict) -> str:
    action = arguments.get("action", "exec")
    sid = arguments.get("session_id", -1)
    sid_str = f"[{sid}]" if sid >= 0 else ""
    if action == "scan":
        return "scan"
    if action == "connect":
        port = arguments.get("port", "")
        baud = arguments.get("baudrate", 115200)
        return f"connect{sid_str} {port} @{baud}"
    if action == "exec":
        return f"exec{sid_str} {ToolDispatcher._fmt_cmd(arguments.get('command', ''))}"
    if action == "raw_exec":
        return f"raw_exec{sid_str} {ToolDispatcher._fmt_cmd(arguments.get('command', ''))}"
    if action == "input":
        return f"input{sid_str} {ToolDispatcher._fmt_cmd(arguments.get('input', ''), '(空)')}"
    if action == "status":
        return "status"
    if action == "close":
        return f"close{sid_str}"
    return f"{action}{sid_str}"


def _style_none(name: str, arguments: dict) -> str:
    return ""


_SUMMARY_STYLES = {
    "file_path": _style_file_path,
    "command": _style_command,
    "device_host": _style_device_host,
    "pattern": _style_pattern,
    "pattern_path": _style_pattern_path,
    "query": _style_query,
    "todos": _style_todos,
    "mcp": _style_mcp,
    "serial": _style_serial,
    "none": _style_none,
}


class ToolDispatcher:
    """工具调度器"""

    # ── 工具分类/标签（由各工具 CAPABILITY 派生，见 tools/registry.py）──
    READONLY_TOOLS = {n for n in capability_names()
                      if get_capability(n)["dispatch"] == "readonly"}
    WRITE_TOOLS = {n for n in capability_names()
                   if get_capability(n)["dispatch"] == "write"}

    # 工具名→简短描述映射
    TOOL_LABELS = {n: get_capability(n)["label"] for n in capability_names()}

    # 工具摘要提取：文件类工具取file_path（摘要风格为 file_path 的工具）
    FILE_PATH_TOOLS = {n for n in capability_names()
                       if get_capability(n)["summary"] == "file_path"}

    def __init__(self, tool_context: ToolContext, executor: ThreadPoolExecutor, logger=None):
        self._tool_context = tool_context
        self._executor = executor
        self._logger = logger

    @staticmethod
    def _write_group_key(arguments: dict) -> str:
        """写入工具分组键：file_path 规范化（相对/绝对、大小写、斜杠别名归一）+ device。
        不归一化会让同一文件的多次 Edit 并发执行，写入互相覆盖（丢更新）。"""
        raw = str(arguments.get("file_path", "") or "")
        if raw:
            try:
                raw = os.path.normcase(os.path.abspath(raw))
            except (OSError, ValueError):
                pass
        dev = str(arguments.get("device", "") or "").strip().lower()
        if dev in ("", "dev0"):
            dev = ""   # dev0 与缺省同为本机
        return dev + "\x00" + raw

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
            if name in ToolDispatcher.READONLY_TOOLS:
                readonly_group.append((idx, tc_id, name, arguments))
            elif name in ToolDispatcher.WRITE_TOOLS:
                fp = ToolDispatcher._write_group_key(arguments)
                write_group.setdefault(fp, []).append((idx, tc_id, name, arguments))
            else:
                # 未分类工具默认按串行执行（保守策略）
                serial_group.append((idx, tc_id, name, arguments))

        # 批内 TodoWrite 须在其他串行工具前执行：UI 先呈现计划，再展示后续工具执行。
        # 稳定排序：TodoWrite 提到最前，其余保持原始顺序。
        serial_group.sort(key=lambda item: item[2] != "TodoWrite")

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
                        time.sleep(0.05)
                    if stream.cancelled:
                        break
                    try:
                        result = fut.result()
                        results[idx] = (tc_id, result)
                    except Exception as e:
                        results[idx] = (tc_id, f"[错误: 工具执行失败: {e}]")
                        self._show_tool_failed(name)
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
                    done, remaining = wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            fut.result()
                        except Exception as e:
                            # 组内某工具执行时崩溃：未执行到的工具补错误行结果
                            # （与单文件分支一致——此前只补 UI 失败提示，会让
                            # 该组 tool_call 结果缺失，下一轮 repair 以
                            # "[用户中断]"占位，误导 LLM）
                            for _idx, _tc_id, _name, _args in futures[fut]:
                                if _idx not in results:
                                    results[_idx] = (_tc_id, f"[错误: 工具执行失败: {e}]")
                                    self._show_tool_failed(_name)

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
                    time.sleep(0.05)
                if stream.cancelled:
                    break
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败: {e}]")
                    self._show_tool_failed(name)

        return [results[i] for i in range(len(parsed)) if i in results]

    def _run_single(self, tc_id: str, name: str, arguments: dict, stream) -> str:
        """执行单个工具调用，处理UI和日志"""
        # UI: 暂停spinner，flush渲染缓冲区，显示工具调用摘要
        stream.pause_spinner()
        try:
            stream.flush_renderer()
            self._show_tool_call(name, arguments)

            # 执行工具
            llm_result, color_diff = tool_execute(name, arguments, self._tool_context)
            # ── UI失败显示判定（从根上杜绝误判）──
            # 命令类工具(Shell/Terminal): 只认框架错误标签(has_error)。
            #   标签是进程级随机值，只有框架自身生成的错误消息携带，命令输出
            #   无法伪造 → 100%确定才显示失败。
            #   - 非零退出码: 不显示（grep无匹配/diff有差异/测试脚本exit 1等
            #     都是合法命令结果，退出码信息本身已进AI上下文）
            #   - 输出文本含"[错误"/"[超时"字样: 不显示（那是数据不是框架错误）
            #   - Shell超时杀进程/工具层错误(连接失败/参数非法): 带标签 → 显示
            #   - Terminal设计内超时(仍在后台运行)/密码提示/繁忙提示: 不带标签 → 不显示
            # 其他工具(Read/Edit/Write/Serial等): 结果纯框架文本、绝无命令输出，
            #   startswith("[错误")即100%确定，沿用。
            # MCP 工具(mcp__*)结果含"服务端任意文本"（可能自带"[错误"开头的中文文本），
            #   与命令类工具同源：只认框架不可伪造标签，避免服务端文本被误判为工具失败。
            # 框架标签只服务于判定，不给AI看（strip_tags剥离后AI看到的内容与无标签一致）
            # 判定来源：CAPABILITY["trusted_output"]（输出含外部文本的工具声明 False）
            tagged_judge = (name.startswith("mcp__")
                            or not get_capability(name)["trusted_output"])
            exec_failed = (
                tagged_judge
                and isinstance(llm_result, str)
                and has_error(llm_result)
            )
            if isinstance(llm_result, str):
                llm_result = strip_tags(llm_result)
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
            elif exec_failed:
                # Shell/Terminal框架错误（带不可伪造标签）：终端补一行失败提示，原因只进AI上下文
                self._show_tool_failed(name)
            elif (not tagged_judge
                  and isinstance(llm_result, str)
                  and llm_result.startswith("[错误")):
                # 非命令类工具(Read/Edit/Write/Serial等)：结果纯框架文本、绝无命令输出，
                # startswith("[错误")即100%确定的工具错误
                self._show_tool_failed(name)

            return llm_result
        finally:
            # 工具抛异常也必须成对恢复，否则 pause 计数泄漏 → spinner 永久不再显示
            stream.resume_spinner()

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
                    time.sleep(0.05)
                if stream.cancelled:
                    return
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败({name}): {e}]")
                    self._show_tool_failed(name)
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
            done, remaining = wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)
            for fut in done:
                idx, tc_id, name = futures[fut]
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"[错误: 工具执行失败({name}): {e}]")
                    self._show_tool_failed(name)

    def _run_sequential_group(self, group, results, stream) -> None:
        """串行执行同一文件组的写入工具"""
        for idx, tc_id, name, arguments in group:
            if stream.cancelled:
                break
            result = self._run_single(tc_id, name, arguments, stream)
            results[idx] = (tc_id, result)

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

    @staticmethod
    def _tool_label(name: str) -> str:
        """工具名→终端显示标签。MCP 工具显示所属服务器，其余查静态标签表。

        调用行与失败行共用，避免两处各算一套导致显示不一致。
        """
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) == 3:
                return f"MCP:{parts[1]}"
        return ToolDispatcher.TOOL_LABELS.get(name, name)

    def _show_tool_call(self, name: str, arguments: dict):
        """在终端显示工具调用摘要（静默模式跳过）

        摘要风格由工具 CAPABILITY["summary"] 指定（查 _SUMMARY_STYLES 分发）。
        """
        if is_quiet_tools():
            return
        label = self._tool_label(name)
        style = _SUMMARY_STYLES.get(get_capability(name)["summary"])
        summary = style(name, arguments) if style else ""

        if summary:
            _stdout_write(f"  {D}[{label}] {summary}{R}\n")
        else:
            _stdout_write(f"  {D}[{label}]{R}\n")

    def _show_diff(self, color_diff: str):
        """在终端展示着色diff（静默模式跳过）"""
        if is_quiet_tools():
            return
        buf = "\n".join(f"  {line}" for line in color_diff.split("\n"))
        # 空编辑的"[无差异]"是单行提示，其后不再加空行，直接接后续输出
        tail = "\n" if "\n" not in color_diff and "[无差异]" in color_diff else "\n\n"
        _stdout_write(buf + tail)

    def _show_tool_failed(self, name: str):
        """工具执行失败：终端显示一行红色失败提示（静默模式跳过）"""
        if is_quiet_tools():
            return
        label = self._tool_label(name)
        _stdout_write(f"  {X}[{label}失败]{R}\n")
