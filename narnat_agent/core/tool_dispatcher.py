"""工具调度器 —— 并行/串行执行策略

从agent.py中提取，负责工具调用的分组、调度和执行。
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import List, Dict, Any, Optional, Tuple

from ..tools.registry import (
    execute as tool_execute, get_capability, capability_names,
)
from ..tools.bash import kill_active as _kill_bash
from ..tools.terminal import kill_active_exec as _kill_terminal_exec
from ..tools.terminal import resolve_dev_display as _dev_display
from ..tools.serial import kill_active_exec as _kill_serial_exec
from ..tools.tool_context import ToolContext, AWAIT_CONFIRM
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


# ── 着色diff → 结构化diff（RPC tool/result.diff 用；终端展示不受影响）──
_ANSI_SEQ = re.compile(r"\x1b\[[0-9;]*m")
_HUNK_HEAD = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# 结构化 diff 最多带多少条变更行（新建大文件时防止 133KB payload）
_DIFF_LINES_MAX = 200


def _struct_diff(color_diff: str) -> Optional[dict]:
    """把着色 unified diff 解析成协议要求的结构。

    仅保留变更行（add/del）及其行号：add 用新文件行号，del 用旧文件行号；
    "无差异"返回空行列表，无 diff（空串）返回 None。上下文行只推进行号不输出。
    """
    if not color_diff:
        return None
    plain = _ANSI_SEQ.sub("", color_diff)
    if plain.strip() == "[无差异]":
        return {"added": 0, "removed": 0, "lines": []}
    added = removed = 0
    old_no = new_no = 0
    lines = []
    seen_hunk = False
    for raw in plain.split("\n"):
        # 文件头只在第一个 @@ 之前出现。用「见过 hunk 了吗」做判据而不是
        # 纯前缀：删掉一行内容为 `--` 的代码时，unified diff 里那一行是
        # `---`，与文件头 `--- a/xxx` 同前缀，按前缀跳过会把这一行内容吃掉
        #（实测：代码里的分隔线 `---` 在 diff 里凭空消失，用户以为没删）。
        head = _HUNK_HEAD.match(raw)
        if head:
            seen_hunk = True
            old_no, new_no = int(head.group(1)), int(head.group(2))
            continue
        if not seen_hunk and (raw.startswith("--- ") or raw.startswith("+++ ")):
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            # 还没进 hunk 但也没空格（极短的伪头）：同样是头，跳过
            if not seen_hunk:
                continue
        if raw.startswith("-"):
            removed += 1
            lines.append({"type": "del", "no": old_no, "text": raw[1:]})
            old_no += 1
        elif raw.startswith("+"):
            added += 1
            lines.append({"type": "add", "no": new_no, "text": raw[1:]})
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    # 变更行太多时只带前若干行进协议：一个 1000 行的新建文件会产生 1000 条
    # 结构化行、133KB 的 tool/result，前端要铺 1000 个 DOM 节点（实测渲染明显变慢）。
    # 行头仍有 +N −M，用户能看出规模；要看全文去编辑器。
    total_add, total_del = added, removed
    if len(lines) > _DIFF_LINES_MAX:
        lines = lines[:_DIFF_LINES_MAX]
    return {"added": total_add, "removed": total_del, "lines": lines,
            "truncated": (total_add + total_del) > len(lines)}


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
    fp = str(arguments.get("file_path", "") or "")
    return f"{dev}:{fp}" if dev else fp


# 工具行里路径的显示上限（字符）。
#
# 曾经是 46，超长路径缩成「…\上级目录\文件名」。改回完整显示的理由：
# 文件类工具和 Shell 是同一类东西——用户得能对照自己给的路径确认
# 「它读/改的正是我指定的那个文件」。折叠后 138 字符的微信临时路径只剩
# `…\6088c8ae\c180d0….jpg`，用户无法与自己输入的那串对上（实测反馈）。
# 行头拥挤的代价由终端软换行承担，比"看不出是哪个文件"轻。
_PATH_SHOW_MAX = 0


def _shorten_path(fp: str) -> str:
    """把过长的路径缩成「上级目录\文件名」，中间省略。

    仅在 _PATH_SHOW_MAX > 0 时生效（当前为 0 = 不折叠，原样返回）。

    保留这个函数是为了保留那条踩过的坑：如果将来要恢复折叠，不能简单截尾——
    两个不同文件可能在行头显示成同一个字符串（`rect_area.py` 与
    `rect_perimeter.py` 截尾后都是 `…\rect_…`），必须保留「上级目录\文件名」。
    """
    if not _PATH_SHOW_MAX or not fp or len(fp) <= _PATH_SHOW_MAX:
        return fp
    # 两种分隔符都要认：模型给的路径可能混用（`E:\a/b\c.py`）。
    # 只用第一个出现的分隔符 split 会漏掉另一种，导致拼出来的 tail 丢字符。
    parts = [p for p in re.split(r"[\\/]", fp) if p]
    if not parts:
        return fp
    sep = "\\" if "\\" in fp else "/"
    base = parts[-1]
    parent = parts[-2] if len(parts) > 1 else ""
    tail = (parent + sep + base) if parent else base
    if len(tail) >= _PATH_SHOW_MAX:
        # 文件名本身就很长：保留前若干字符，至少让不同文件的首部能区分
        return "…" + sep + tail[-(_PATH_SHOW_MAX - 1):]
    return "…" + sep + tail


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
        started = time.time()
        try:
            stream.flush_renderer()
            self._show_tool_call(name, arguments)
            self._notify_tool_call(stream, tc_id, name, arguments)

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
                stripped = strip_tags(llm_result)
                # Read 读到图片时结果携带 .images（ImageBearingStr 子类）。
                # strip_tags 返回普通 str 会把图片引用丢掉——那等于"工具报告
                # 有图但图永远发不出去"，模型只能看到句柄文本却看不到画面。
                # 原位回填原字符串内容，保留子类与附带引用。
                images = getattr(llm_result, "images", None)
                if images:
                    llm_result = type(llm_result)(stripped, images)
                else:
                    llm_result = stripped
            self._logger.info(
                f"tools.{name.lower()}",
                f"调用: {json.dumps(arguments, ensure_ascii=False)[:200]}",
            )
            self._logger.info(
                f"tools.{name.lower()}",
                f"结果: {llm_result[:200] if llm_result else '(空)'}",
            )

            # 展示着色diff
            tool_failed = (
                exec_failed
                # 非命令类工具(Read/Edit/Write/Serial等)：结果纯框架文本、绝无命令输出，
                # startswith("[错误")即100%确定的工具错误
                or (not tagged_judge
                    and isinstance(llm_result, str)
                    and llm_result.startswith("[错误"))
            )
            if color_diff:
                self._show_diff(color_diff)
            elif tool_failed:
                # Shell/Terminal框架错误（带不可伪造标签）：终端补一行失败提示，原因只进AI上下文
                self._show_tool_failed(name)

            self._notify_tool_result(stream, tc_id, name, started,
                                     llm_result, color_diff, tool_failed)
            return llm_result
        except BaseException:
            # 工具抛异常同样要成对下发结果事件，否则 RPC 侧 tool/call 悬空
            self._notify_tool_result(stream, tc_id, name, started, "", None, True)
            raise
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

    @staticmethod
    def _tool_label_summary(name: str, arguments: dict) -> Tuple[str, str]:
        """工具名→(终端标签, 摘要文本)。终端行与结构化钩子共用，避免两处各算一套。"""
        label = ToolDispatcher._tool_label(name)
        style = _SUMMARY_STYLES.get(get_capability(name)["summary"])
        summary = style(name, arguments) if style else ""
        return label, summary

    @staticmethod
    def _notify_tool_call(stream, tc_id: str, name: str, arguments: dict) -> None:
        """把工具调用下发到 stream 的可选钩子（终端形态无此方法 → 什么都不做）"""
        hook = getattr(stream, "on_tool_call", None)
        if hook is None:
            return
        label, summary = ToolDispatcher._tool_label_summary(name, arguments)
        hook(tool_call_id=tc_id, name=name, label=label,
             dispatch=get_capability(name)["dispatch"], summary=summary,
             arguments=arguments)

    @staticmethod
    def _notify_tool_result(stream, tc_id: str, name: str, started: float,
                            output, color_diff, failed: bool) -> None:
        """把工具结果下发到 stream 的可选钩子（终端形态无此方法 → 什么都不做）"""
        hook = getattr(stream, "on_tool_result", None)
        if hook is None:
            return
        # AWAIT_CONFIRM 是内部哨兵（工具还没执行，等用户确认）。
        # 原样下发会让界面显示 "__AWAIT_CONFIRM__" 这串无意义文本；
        # 换成用户看得懂的「等待确认」，确认后的真实结果由主循环补发。
        text = output if isinstance(output, str) else ""
        if text == AWAIT_CONFIRM:
            text = "等待确认…"
        hook(tool_call_id=tc_id, name=name, ok=not failed,
             duration_ms=int(max(0.0, time.time() - started) * 1000),
             output=text,
             failed=failed, diff=_struct_diff(color_diff))

    def _show_tool_call(self, name: str, arguments: dict):
        """在终端显示工具调用摘要（静默模式跳过）

        摘要风格由工具 CAPABILITY["summary"] 指定（查 _SUMMARY_STYLES 分发）。
        """
        if is_quiet_tools():
            return
        label, summary = self._tool_label_summary(name, arguments)

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
