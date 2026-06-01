"""
主循环 —— 读输入→调度AI→输出→循环
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

from .llm import LLMClient
from .context import ContextManager
from .compressor import Compressor
from ..config.loader import AppConfig, load_config
from ..config.session_store import save_session, load_session, list_sessions, delete_session, format_session_list
from ..tools.registry import execute as tool_execute
from ..tools import write as write_tool
from ..tools import bash as bash_tool
from ..tools import todo_write as todo_tool
from ..ui.ui_design import UIInterface, SessionCallbacks, _interrupt_ctrl, D, E, R, Y, G, B, C
from ..logger import AgentLogger

# ── 工具分类 ──
_READONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch"}
_WRITE_TOOLS = {"Edit", "Write"}
_SERIAL_TOOLS = {"Shell", "TodoWrite"}

# 工具名→简短描述映射
_TOOL_LABELS = {
    "Read": "读取",
    "Glob": "搜索文件",
    "Grep": "搜索内容",
    "Edit": "编辑",
    "Write": "写入",
    "Shell": "执行命令",
    "WebSearch": "联网搜索",
    "TodoWrite": "更新计划",
}

# 工具摘要提取：文件类工具取file_path
_FILE_PATH_TOOLS = {"Read", "Edit", "Write"}


class NarnatSessionCallbacks(SessionCallbacks):
    """会话命令回调实现"""

    def __init__(self, narnat_dir: str, get_messages_func, set_messages_func):
        self._narnat_dir = narnat_dir
        self._get_messages = get_messages_func
        self._set_messages = set_messages_func
        self._active_name: Optional[str] = None  # 当前已保存的会话名

    def on_save(self, name: str) -> str:
        err = save_session(self._narnat_dir, name, self._get_messages())
        if not err:
            self._active_name = name
        return err

    def on_show(self) -> str:
        sessions = list_sessions(self._narnat_dir)
        return format_session_list(sessions)

    def on_enter(self, name: str) -> str:
        messages, err = load_session(self._narnat_dir, name)
        if err:
            return err
        self._set_messages(messages)
        self._active_name = name
        return ""

    def on_delete(self, name: str) -> str:
        err = delete_session(self._narnat_dir, name)
        if not err and name == self._active_name:
            self._active_name = None
        return err

    def on_exit(self) -> str:
        """退出时自动保存（仅当会话曾被/save过）。返回保存的会话名或空串。"""
        if self._active_name is None:
            return ""
        name = self._active_name
        err = save_session(self._narnat_dir, name, self._get_messages())
        if err:
            return ""  # 保存失败，不返回名字
        self._active_name = None
        return name


class Agent:
    """Narnat Agent 主控"""

    def __init__(self, project_root: Optional[str] = None, debug: bool = False):
        # 加载配置
        self._config = load_config(project_root)
        # 初始化日志（仅debug模式启用）
        self._logger = AgentLogger(self._config.project_root)
        if debug:
            self._logger.start(self._config.project_root)
        # 初始化LLM
        self._llm = LLMClient(self._config.ai, self._logger)
        # 初始化上下文管理
        self._context = ContextManager(self._logger)
        # 初始化压缩器
        self._compressor = Compressor(self._config.narnat_dir, self._logger)
        # 初始化messages
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._config.system_prompt}
        ]
        # 初始化UI
        callbacks = NarnatSessionCallbacks(
            self._config.narnat_dir,
            lambda: self._messages,
            lambda msgs: setattr(self, '_messages', msgs),
        )
        self._ui = UIInterface(self._config.ai.model, callbacks)
        # 注入工具回调
        bash_tool.set_confirm_callback(self._confirm_delete)
        bash_tool.set_interrupt_check(lambda: _interrupt_ctrl.is_set)
        todo_tool.set_ui_callback(self._on_todo_update)
        # 统计
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _auto_save_on_exit(self):
        """退出时自动保存已命名的会话"""
        saved_name = self._ui.auto_save()
        if saved_name:
            print(f"  {D}会话已自动保存: {E}{saved_name}{R}")

    def _confirm_delete(self, command: str) -> bool:
        """删除命令确认回调"""
        try:
            response = input(f"  确认执行删除命令? [y/N]: ")
            return response.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _on_todo_update(self, todos):
        """TodoWrite UI更新回调 — 展示工作计划和进度"""
        # 打印任务列表
        for t in todos:
            status = t["status"]
            content = t.get("content", "")
            active_form = t.get("activeForm", content)

            if status == "completed":
                icon = f"{E}✓{R}"
                line = f"  {icon} {D}{content}{R}"
            elif status == "in_progress":
                icon = f"{Y}●{R}"
                line = f"  {icon} {B}{active_form}{R}"
            else:  # pending
                icon = f"{G}○{R}"
                line = f"  {icon} {D}{content}{R}"

            print(line)

    def _show_tool_call(self, name: str, arguments: dict):
        """在终端显示工具调用摘要，让用户看到AI正在做什么"""
        label = _TOOL_LABELS.get(name, name)

        # 提取关键参数用于摘要
        summary = ""
        if name in _FILE_PATH_TOOLS:
            summary = arguments.get("file_path", "")
        elif name == "Shell":
            summary = arguments.get("command", "")
        elif name == "Grep":
            summary = arguments.get("pattern", "")
        elif name == "Glob":
            summary = arguments.get("pattern", "")
        elif name == "WebSearch":
            summary = arguments.get("query", "")

        if summary:
            # 截断过长的摘要
            if len(summary) > 100:
                summary = summary[:97] + "..."
            print(f"  {D}[{label}] {summary}{R}")
        else:
            print(f"  {D}[{label}]{R}")

    def _show_diff(self, color_diff: str):
        """在终端展示着色diff"""
        # 每行缩进2空格，与工具调用摘要对齐
        for line in color_diff.split("\n"):
            print(f"  {line}")
        print()  # diff后空一行，与后续输出分隔

    def run(self):
        """主循环"""
        self._ui.start()
        self._logger.info("core.agent", f"Agent启动, model={self._config.ai.model}")

        while True:
            # 1. 读取用户输入
            user_input = self._ui.read_input()
            if user_input is None:
                continue

            stripped = user_input.strip()
            if not stripped:
                continue

            # /exit 退出
            if stripped == "/exit":
                self._auto_save_on_exit()
                self._logger.info("core.agent", "用户退出")
                self._logger.close()
                return

            # 命令分发
            if stripped.startswith("/"):
                parts = stripped.split(None, 1)
                cmd = parts[0]
                args = parts[1] if len(parts) > 1 else ""
                if self._ui.dispatch_command(cmd, args):
                    continue

            # 2. 轮次计数
            warn = self._context.increment()
            if warn:
                print(f"  ⚠ {warn}")

            # 3. 压缩检查
            compress_ok = False
            if self._context.need_compress():
                compress_ok = self._handle_compress(stripped)
                if not compress_ok:
                    continue
                # 压缩成功：pending_input已在messages中，跳过追加，直接走AI调度

            # 4. 追加用户消息（压缩成功时已追加，跳过）
            if not compress_ok:
                # 修复messages：打断可能留下不完整的tool_call
                self._repair_messages()
                self._messages.append({"role": "user", "content": stripped})
                self._logger.info("core.agent", f"用户输入: {stripped[:100]}")

            # 5. 创建流式输出
            stream = self._ui.create_stream()

            try:
                # 6. 工具调度内循环
                self._agent_loop(stream)
            except KeyboardInterrupt:
                self._ui.on_interrupted()
                stream.abort()
            except Exception as e:
                self._logger.error("core.agent", f"异常: {e}")
                stream.abort()

    def _agent_loop(self, stream):
        """工具调度内循环"""
        while True:
            # a. 修复messages：如果有未回复的tool_call，补上空结果
            self._repair_messages()

            # b. 调用LLM
            content_parts = []
            tool_calls_result = []

            for chunk in self._llm.chat_stream(self._messages):
                # b. 检查中断
                if stream.cancelled:
                    # 将已收到的部分内容追加为assistant消息，保持messages完整
                    if content_parts:
                        self._messages.append({
                            "role": "assistant",
                            "content": "".join(content_parts),
                        })
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # c. 处理tool_call
                if "tool_calls" in chunk:
                    tool_calls_result = chunk["tool_calls"]

                # d. 处理纯文本
                if "content" in chunk and "tool_calls" not in chunk:
                    stream.feed(chunk["content"])
                    content_parts.append(chunk["content"])

                # e. 处理结束
                if "finish_reason" in chunk:
                    reason = chunk["finish_reason"]
                    if reason == "error":
                        stream.finish(
                            self._total_input_tokens,
                            self._total_output_tokens,
                        )
                        return

            # 有tool_call → 执行工具 → 继续内循环
            if tool_calls_result:
                # 追加assistant消息（含tool_calls）
                self._messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": tool_calls_result,
                })

                # 并行执行工具调用
                tool_results = self._execute_tool_calls(tool_calls_result, stream)

                # 中断检查：并行执行中可能被打断
                if stream.cancelled:
                    # 为未完成的tool_call补上空结果，避免API 400错误
                    completed_ids = {tc_id for tc_id, _ in tool_results}
                    for tc in tool_calls_result:
                        if tc["id"] not in completed_ids:
                            self._messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": "[用户中断]",
                            })
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # 按原始顺序回传所有工具结果
                for tc_id, result in tool_results:
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result,
                    })

                # 更新token统计（tool_call轮次也需统计）
                self._total_output_tokens += sum(len(p) for p in content_parts)
                self._total_input_tokens = self._llm.count_tokens(self._messages)

                # 继续内循环
                continue

            # 无tool_call → 纯文本输出完成，退出内循环
            # 追加assistant消息
            if content_parts:
                self._messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts),
                })

            # 更新token统计（仅统计本轮新增的输出token）
            self._total_output_tokens += sum(len(p) for p in content_parts)
            self._total_input_tokens = self._llm.count_tokens(self._messages)

            stream.finish(
                self._total_input_tokens,
                self._total_output_tokens,
            )
            break

    def _repair_messages(self):
        """修复messages：打断后可能留下不完整的消息序列。
        
        1. assistant含tool_calls但没有对应的tool消息 → 补上tool("[用户中断]")
        2. 如果第1步修复了，且末尾是tool消息 → 补上assistant（API要求tool后不能直接跟user）
        """
        # 1. 为未回复的tool_call补上空结果
        replied_ids = set()
        for msg in self._messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        for msg in self._messages:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[用户中断]",
                        })
                        replied_ids.add(tc_id)
                        repaired = True

        # 2. 只有在第1步确实修复了未回复的tool_call时，才补assistant
        # 正常流程中末尾是tool消息是正常的（下一轮LLM调用会处理）
        if repaired and self._messages and self._messages[-1].get("role") == "tool":
            self._messages.append({"role": "assistant", "content": "（用户中断了工具执行）"})

        if repaired:
            self._logger.info("core.agent", "_repair_messages: 修复了打断后的消息序列")

    def _execute_tool_calls(
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
        parsed: List[Tuple[str, str, dict]] = []  # (tc_id, name, arguments)
        for tc in tool_calls:
            func = tc["function"]
            name = func["name"]
            try:
                arguments = json.loads(func["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            parsed.append((tc["id"], name, arguments))

        # 按分类分组，保留原始索引
        readonly_group = []   # (index, tc_id, name, arguments)
        write_group = {}      # file_path → [(index, tc_id, name, arguments)]
        serial_group = []     # (index, tc_id, name, arguments)

        for idx, (tc_id, name, arguments) in enumerate(parsed):
            if name in _READONLY_TOOLS:
                readonly_group.append((idx, tc_id, name, arguments))
            elif name in _WRITE_TOOLS:
                fp = arguments.get("file_path", "")
                write_group.setdefault(fp, []).append((idx, tc_id, name, arguments))
            else:
                serial_group.append((idx, tc_id, name, arguments))

        # 结果容器：index → (tc_id, result)
        results: Dict[int, Tuple[str, str]] = {}

        # ── 阶段1：只读工具并行 ──
        if readonly_group:
            self._run_parallel(readonly_group, results, stream)

        # ── 阶段2：写入工具按文件分组 ──
        # 同文件串行，不同文件并行
        if write_group:
            # 将每个文件组作为一个并行单元
            file_groups = list(write_group.values())
            if len(file_groups) == 1:
                # 单文件组，直接串行
                for item in file_groups[0]:
                    idx, tc_id, name, arguments = item
                    if stream.cancelled:
                        break
                    result = self._run_single(tc_id, name, arguments, stream)
                    results[idx] = (tc_id, result)
            else:
                # 多文件组并行，组内串行
                with ThreadPoolExecutor(max_workers=len(file_groups)) as executor:
                    futures = {}
                    for group in file_groups:
                        fut = executor.submit(
                            self._run_sequential_group, group, results, stream
                        )
                        futures[fut] = group
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except Exception:
                            pass  # _run_sequential_group内部已处理

        # ── 阶段3：串行工具 ──
        for item in serial_group:
            idx, tc_id, name, arguments = item
            if stream.cancelled:
                break
            result = self._run_single(tc_id, name, arguments, stream)
            results[idx] = (tc_id, result)

        # 按原始顺序返回
        return [results[i] for i in range(len(parsed)) if i in results]

    def _run_single(
        self, tc_id: str, name: str, arguments: dict, stream
    ) -> str:
        """执行单个工具调用，处理UI和日志"""
        # Read后标记（供Write检查）
        if name == "Read":
            file_path = arguments.get("file_path", "")
            if file_path:
                write_tool.mark_read(file_path)

        # UI: 暂停spinner，flush渲染缓冲区，显示工具调用摘要
        stream.pause_spinner()
        stream.flush_renderer()
        self._show_tool_call(name, arguments)

        # 执行工具 → 返回 (llm_result, color_diff)
        llm_result, color_diff = tool_execute(name, arguments)
        self._logger.info(
            f"tools.{name.lower()}",
            f"调用: {json.dumps(arguments, ensure_ascii=False)[:200]}",
        )
        self._logger.info(
            f"tools.{name.lower()}",
            f"结果: {llm_result[:200] if llm_result else '(空)'}",
        )

        # 展示着色diff（Edit/Write编辑文件后）
        if color_diff:
            self._show_diff(color_diff)

        # UI: 恢复spinner
        stream.resume_spinner()
        return llm_result

    def _run_parallel(
        self,
        group: List[Tuple[int, str, str, dict]],
        results: Dict[int, Tuple[str, str]],
        stream,
    ) -> None:
        """并行执行一组工具调用，结果写入results字典"""
        if not group:
            return
        if len(group) == 1:
            idx, tc_id, name, arguments = group[0]
            if not stream.cancelled:
                result = self._run_single(tc_id, name, arguments, stream)
                results[idx] = (tc_id, result)
            return

        with ThreadPoolExecutor(max_workers=len(group)) as executor:
            futures = {}
            for idx, tc_id, name, arguments in group:
                if stream.cancelled:
                    break
                fut = executor.submit(self._run_single, tc_id, name, arguments, stream)
                futures[fut] = (idx, tc_id, name)
            for fut in as_completed(futures):
                if stream.cancelled:
                    break
                idx, tc_id, name = futures[fut]
                try:
                    result = fut.result()
                    results[idx] = (tc_id, result)
                except Exception as e:
                    results[idx] = (tc_id, f"错误: 工具执行失败({name}): {e}")

    def _run_sequential_group(
        self,
        group: List[Tuple[int, str, str, dict]],
        results: Dict[int, Tuple[str, str]],
        stream,
    ) -> None:
        """串行执行同一文件组的写入工具，结果写入results字典"""
        for idx, tc_id, name, arguments in group:
            if stream.cancelled:
                break
            result = self._run_single(tc_id, name, arguments, stream)
            results[idx] = (tc_id, result)

    def _handle_compress(self, pending_input: str) -> bool:
        """
        处理上下文压缩。

        Returns:
            True=压缩成功，pending_input已在messages中，调用方应走AI调度
            False=压缩失败/中断，调用方应continue
        """
        self._logger.info("compressor", f"压缩触发, 轮次={self._context.turn_count}")

        # 1. 暂存用户输入（pending_input已传入）
        # 2. 启动压缩动画
        self._ui.begin_compressing()

        # 3. 构建压缩请求
        compress_messages = self._compressor.build_compress_messages(self._messages)

        # 4. 发送压缩请求，收集AI输出
        summary_content = []
        llm_error = False
        for chunk in self._llm.chat_stream(compress_messages, no_tools=True):
            # 压缩过程中也检查中断
            if _interrupt_ctrl.is_set:
                self._ui.end_compressing()
                self._context.reset()
                self._messages.append({"role": "user", "content": pending_input})
                return False
            # 检测LLM错误（避免错误信息被当作总结）
            if "finish_reason" in chunk and chunk["finish_reason"] == "error":
                llm_error = True
                break
            if "content" in chunk and "tool_calls" not in chunk:
                summary_content.append(chunk["content"])

        # LLM报错 → 走失败分支
        if llm_error:
            self._ui.end_compressing()
            self._logger.error("compressor", "压缩失败: LLM调用出错")
            self._context.set_retry_soon()
            self._messages.append({"role": "user", "content": pending_input})
            return False

        summary = "".join(summary_content)

        # 5. 写入磁盘
        if not self._compressor.write_summary(summary):
            self._ui.end_compressing()
            self._logger.error("compressor", "压缩失败: 写入失败")
            self._context.set_retry_soon()
            self._messages.append({"role": "user", "content": pending_input})
            return False

        # 6. 校验总结结果
        if not self._compressor.verify_summary():
            self._ui.end_compressing()
            self._logger.error("compressor", "压缩失败: 总结为空")
            self._context.set_retry_soon()
            self._messages.append({"role": "user", "content": pending_input})
            return False

        # 7. 销毁旧会话
        self._messages.clear()
        self._context.reset()
        write_tool.clear_read_files()

        # 8. 创建新会话（注入总结）
        summary_text = self._compressor.read_summary()
        self._messages = self._compressor.build_new_session_messages(
            self._config.system_prompt, summary_text
        )

        # 9. 重置总结文件
        self._compressor.reset_summary()

        # 10. 停止压缩动画
        self._ui.end_compressing()

        self._logger.info("compressor", "压缩成功，新会话已创建")

        # 11. 恢复用户问题（追加到messages，交给AI调度处理）
        self._messages.append({"role": "user", "content": pending_input})
        return True
