"""
主循环 —— 读输入→调度AI→输出→循环

Agent类作为主编排者，委托具体实现给子模块：
- ToolDispatcher: 工具调度+并行策略
- MessageManager: 消息修复+追加+压缩
- StatsTracker: token统计+费用追踪
- NarnatSessionCallbacks: 会话命令回调
- SafetyCallbacks / TodoCallbacks: 工具回调
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from .llm import LLMClient, set_retry_count
from .context import ContextManager
from .compressor import Compressor
from .tool_dispatcher import ToolDispatcher
from .message_manager import MessageManager
from .stats import StatsTracker
from .session_callbacks import NarnatSessionCallbacks
from .tool_callbacks import SafetyCallbacks, TodoCallbacks
from ..tools.tool_context import AWAIT_CONFIRM
from ..config.loader import AppConfig, load_config
from ..tools.terminal import kill_active_exec as _kill_terminal_exec, cleanup as _terminal_cleanup, set_max_sessions
from ..tools.tool_context import ToolContext
from ..ui.ui_design import UIInterface, _interrupt_ctrl, apply_style
from ..output import write as _stdout_write, D, E, R, Y, G, B, C
from ..logger import AgentLogger


class Agent:
    """Narnat Agent 主控"""

    def __init__(self, project_root: Optional[str] = None, debug: bool = False):
        # 加载配置
        self._config = load_config(project_root)

        # 加载自定义配色
        apply_style(self._config)

        # 应用工具配置
        set_max_sessions(self._config.ssh_max_sessions)
        set_retry_count(self._config.llm_retry_count)

        # 初始化日志
        self._logger = AgentLogger(self._config.logs_dir)
        if debug:
            self._logger.start(self._config.logs_dir)

        # 初始化LLM
        self._llm = LLMClient(
            self._config.ai,
            self._logger,
            max_output_tokens=self._config.ui.max_output_tokens,
        )

        # 初始化上下文管理
        self._context = ContextManager(self._logger, self._config.warn_turn_1, self._config.warn_turn_2, self._config.compress_turn)

        # 初始化压缩器
        self._compressor = Compressor(self._config.narnat_dir, self._logger)

        # 崩溃恢复：检查是否有残留的压缩摘要
        recovered_summary = ""
        if self._compressor.verify_summary():
            recovered_summary = self._compressor.read_summary()
            self._compressor.reset_summary()
            if self._logger:
                self._logger.info("core.agent", "崩溃恢复: 已加载残留的压缩摘要")

        # 初始化messages（通过MessageManager管理）
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._config.system_prompt}
        ]
        self._msg_manager = MessageManager(self._messages, self._compressor, self._logger)
        if recovered_summary:
            self._msg_manager.append_system(f"# 上一轮对话成果\n\n{recovered_summary}")

        # 初始化UI
        callbacks = NarnatSessionCallbacks(
            self._config.narnat_dir,
            lambda: self._messages,
            context_manager=self._context,
        )
        self._ui = UIInterface(self._config.ai.model, callbacks)

        # 初始化工具上下文
        # Windows: confirm_callback用input()直接确认（两套输入系统互不干扰）
        # Linux/macOS: 不注入confirm_callback，删除命令通过AWAIT_CONFIRM机制在#提示符下确认
        self._tool_context = ToolContext(
            confirm_callback=SafetyCallbacks.confirm_delete if sys.platform == "win32" else None,
            ui_callback=TodoCallbacks.on_todo_update,
            api_keys=self._config.api_keys,
            ignore_dirs=self._config.ignore_dirs,
        )

        # 初始化子模块
        self._dispatcher = ToolDispatcher(self._tool_context, ThreadPoolExecutor(max_workers=16), self._logger)
        self._stats = StatsTracker(
            self._config.ai.model,
            self._config.pricing.user_pricing,
            self._config.pricing.balance_url,
        )

        # 轮次计数
        self._round = 0

    def _auto_save_on_exit(self):
        """退出时自动保存已命名的会话"""
        saved_name = self._ui.auto_save()
        if saved_name:
            _stdout_write(f"  {D}会话已自动保存: {E}{saved_name}{R}\n")

    def run(self):
        """主循环"""
        self._ui.start()
        self._logger.info("core.agent", f"Agent启动, model={self._config.ai.model}")

        try:
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
                    os._exit(0)

                # 命令分发
                if stripped.startswith("/"):
                    parts = stripped.split(None, 1)
                    cmd = parts[0]
                    args = parts[1] if len(parts) > 1 else ""
                    if self._ui.dispatch_command(cmd, args):
                        continue

                # 2. 轮次计数
                self._round += 1
                warn = self._context.increment()
                if warn:
                    _stdout_write(f"  ⚠ {warn}\n")

                # 3. 余额查询
                api_key = getattr(self._config.ai, 'api_key', None)
                self._stats.fetch_balance(api_key, self._round)

                # 4. 压缩检查
                compress_ok = False
                if self._context.need_compress():
                    compress_ok = self._handle_compress(stripped)
                    if not compress_ok:
                        continue

                # 5. 追加用户消息
                if not compress_ok:
                    self._msg_manager.repair()
                    self._msg_manager.append_user(stripped)
                    self._logger.info("core.agent", f"用户输入: {stripped[:100]}")

                # 6. 创建流式输出
                stream = self._ui.create_stream()

                try:
                    # 7. 工具调度内循环
                    self._agent_loop(stream)
                except KeyboardInterrupt:
                    self._ui.on_interrupted()
                    stream.abort()
                except Exception as e:
                    self._logger.error("core.agent", f"异常: {e}")
                    if hasattr(self, '_last_content_parts') and self._last_content_parts:
                        self._msg_manager.append_assistant("".join(self._last_content_parts))
                    stream.abort()

        finally:
            self._dispatcher._executor.shutdown(wait=False)
            _terminal_cleanup()

    def _agent_loop(self, stream):
        """工具调度内循环"""
        while True:
            # a. 修复messages
            self._msg_manager.repair()

            # b. 调用LLM
            content_parts = []
            self._last_content_parts = content_parts
            tool_calls_result = []
            call_usage = None
            parsed_finish_reason = None

            for chunk in self._llm.chat_stream(self._msg_manager.messages, cancel_check=lambda: stream.cancelled):
                # b. 检查中断
                if stream.cancelled:
                    if content_parts:
                        self._msg_manager.append_assistant("".join(content_parts))
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

                # e. 捕获usage
                if "usage" in chunk:
                    call_usage = chunk["usage"]

                # f. 处理结束
                if "finish_reason" in chunk:
                    parsed_finish_reason = chunk["finish_reason"]
                    if parsed_finish_reason == "error":
                        stream.finish(
                            self._stats.input_tokens,
                            self._stats.output_tokens,
                        )
                        return

            # 中断检查
            if stream.cancelled:
                stream.abort()
                self._ui.on_interrupted()
                return

            # 有tool_call → 执行工具 → 继续内循环
            if tool_calls_result:
                self._msg_manager.append_assistant(
                    "".join(content_parts) or None,
                    tool_calls=tool_calls_result,
                )

                tool_results = self._dispatcher.execute_tool_calls(tool_calls_result, stream)

                # 中断检查
                if stream.cancelled:
                    completed_ids = {tc_id for tc_id, _ in tool_results}
                    self._msg_manager.append_interrupted_tools(tool_calls_result, completed_ids)
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # Linux/macOS: 拦截删除确认标记，在#提示符下等待用户确认
                if self._tool_context.pending_delete is not None:
                    pending = self._tool_context.pending_delete
                    self._tool_context.pending_delete = None
                    # 找到返回AWAIT_CONFIRM的那个tool_call_id
                    confirm_tc_id = None
                    for tc_id, result in tool_results:
                        if result == AWAIT_CONFIRM:
                            confirm_tc_id = tc_id
                            break
                    if confirm_tc_id is not None:
                        # 结束当前流式输出，回到#提示符等用户确认
                        new_stream = self._handle_delete_confirm(stream, confirm_tc_id, pending, tool_results)
                        if new_stream is not None:
                            stream = new_stream
                            continue
                        return

                # 回传工具结果
                for tc_id, result in tool_results:
                    self._msg_manager.append_tool_result(tc_id, result)

                # 更新统计
                if call_usage:
                    self._stats.update(call_usage)

                continue

            # 无tool_call → 纯文本输出完成
            if content_parts:
                self._msg_manager.append_assistant("".join(content_parts))
            else:
                # 空回复
                self._dump_empty_debug(content_parts, tool_calls_result, parsed_finish_reason, call_usage)
                _empty_msgs = {
                    "stop": "⚠ AI 返回了空回复，请尝试缩短对话或稍后重试。",
                    "max_tokens": "⚠ AI 思考超过了最大输出限制，请增大限制或缩短对话。",
                    "content_filter": "⚠ AI 返回被安全策略拦截，请调整提问内容。",
                    "server_busy": "⚠ 服务器繁忙，请稍后重试。",
                    "error": "⚠ AI 调用出错，请查看上方错误信息。",
                }
                reason = parsed_finish_reason or "stop"
                msg = _empty_msgs.get(reason, f"⚠ AI 返回异常（{reason}），请稍后重试。")
                stream.feed(f"\n\n{msg}\n")
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache=self._stats.cache_tokens,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                )
                return

            # 更新统计
            if call_usage:
                self._stats.update(call_usage)

            stream.finish(
                self._stats.input_tokens,
                self._stats.output_tokens,
                cache=self._stats.cache_tokens,
                cost=self._stats.cost,
                balance=self._stats.balance,
            )
            break

    def _handle_delete_confirm(self, stream, confirm_tc_id, pending_delete, tool_results):
        """处理Linux/macOS下的删除确认：结束流式输出，在#提示符下等用户确认。

        Args:
            stream: 当前流式输出会话
            confirm_tc_id: 返回AWAIT_CONFIRM的tool_call_id
            pending_delete: (tool_name, arguments_dict) 暂存的删除命令
            tool_results: 所有工具结果列表 [(tc_id, result), ...]

        Returns:
            新的UIStreamSession - 用户已确认/取消，结果已回传，继续agent_loop
            None - 流已结束，调用方应return
        """
        tool_name, arguments = pending_delete

        # 先回传非确认的工具结果
        for tc_id, result in tool_results:
            if tc_id != confirm_tc_id:
                self._msg_manager.append_tool_result(tc_id, result)

        # 结束当前流式输出
        stream.finish(
            self._stats.input_tokens,
            self._stats.output_tokens,
            cache=self._stats.cache_tokens,
            cost=self._stats.cost,
            balance=self._stats.balance,
        )

        # 在#提示符下显示确认信息，等待用户输入
        user_input = self._ui.read_input_with_prompt("  确认执行删除命令? [y/N]: ")
        if user_input is None:
            user_input = ""

        confirmed = user_input.strip().lower() in ("y", "yes")

        if confirmed:
            # 用户确认：设置标志跳过删除检测，重新执行命令
            self._tool_context._delete_confirmed = True
            from ..tools.registry import execute as tool_execute
            llm_result, color_diff = tool_execute(tool_name, arguments, self._tool_context)
            self._msg_manager.append_tool_result(confirm_tc_id, llm_result)
            if color_diff:
                _stdout_write("\n".join(f"  {line}" for line in color_diff.split("\n")) + "\n\n")
        else:
            # 用户取消
            self._msg_manager.append_tool_result(confirm_tc_id, "操作已取消: 删除命令需用户确认")

        # 创建新的流式输出会话，继续agent_loop
        return self._ui.create_stream()

    def _dump_empty_debug(self, content_parts, tool_calls_result, finish_reason, call_usage):
        """空回复时写调试日志"""
        debug_path = os.path.join(
            self._config.data_dir,
            f"debug_empty_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        debug_data = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": {"messages": self._msg_manager.messages},
            "response": {
                "raw_sse_lines": self._llm.raw_sse or [],
                "parsed_content": "".join(content_parts),
                "parsed_tool_calls": tool_calls_result,
                "parsed_finish_reason": finish_reason,
                "call_usage": call_usage,
            },
        }
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            _stdout_write(f"  ⚠ 调试日志已写入: {debug_path}\n")
        except OSError:
            pass

    def _handle_compress(self, pending_input: str) -> bool:
        """处理上下文压缩"""
        def on_interrupt():
            self._ui.end_compressing()
            self._context.reset()
            self._msg_manager.append_user(pending_input)

        def on_llm_error(msg):
            self._ui.end_compressing()
            self._logger.error("compressor", msg)
            self._context.set_retry_soon()
            self._msg_manager.append_user(pending_input)

        self._ui.begin_compressing()
        result = self._msg_manager.handle_compress(
            pending_input,
            self._config.system_prompt,
            self._llm,
            cancel_check=lambda: _interrupt_ctrl.is_set,
            on_interrupt=on_interrupt,
            on_llm_error=on_llm_error,
        )
        if result:
            self._ui.end_compressing()
            self._context.reset()
            self._tool_context.clear_read_files()
        return result
