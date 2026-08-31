"""工具调度内循环 —— LLM调用 ↔ 工具执行的循环

从 Agent._agent_loop() / _handle_delete_confirm() / _dump_empty_debug() 提取。
算法逻辑原样保留。
"""

import json
import os
import time
from typing import List, Dict, Any, Optional

from .llm import LLMClient, retry_sleep, set_retry_count, RETRY_BACKOFF_BASE
from .message_manager import MessageManager
from .tool_dispatcher import ToolDispatcher
from ..tools.tool_context import ToolContext, AWAIT_CONFIRM
from .stats import StatsTracker
from ..ui.ui_design import UIInterface
from ..config.loader import Config
from ..output import write as _stdout_write, D, E, R, Y, G, B, C
from ..logger import AgentLogger


class AgentLoop:
    """工具调度内循环"""

    def __init__(self, llm: LLMClient, msg_manager: MessageManager,
                 dispatcher: ToolDispatcher, tool_context: ToolContext,
                 stats: StatsTracker, ui: UIInterface,
                 config: Config, logger: AgentLogger):
        self._llm = llm
        self._msg_manager = msg_manager
        self._dispatcher = dispatcher
        self._tool_context = tool_context
        self._stats = stats
        self._ui = ui
        self._config = config
        self._logger = logger

    @property
    def _thinking_label(self) -> str:
        """思考强度中文标签（从配置读取）"""
        return self._config.ai.thinking_options.get(
            self._config.ai.thinking_effort, self._config.ai.thinking_effort
        )

    def run(self, stream):
        """工具调度内循环"""
        # 本轮是否正常完成（无tool_call纯文本输出结束）。目标模式续跑据此判断，
        # 出错/中断/空回复等非正常结束不触发自动续跑。
        self._last_round_ok = False
        stream_interrupted_retries = 0  # 响应流中断（无完成标记）的自动重试计数
        # 流中断重试上限 = 配置的"LLM重试次数"（每轮独立：收到正常完成标记后重置，
        # 与连接层重试语义一致，仅累计同一次中断后的连续重试）
        try:
            stream_retry_max = max(1, min(int(self._config.ai.retry_count), 10))
        except (TypeError, ValueError, AttributeError):
            stream_retry_max = 3
        # 同步LLM层重试次数：网络/5xx/429 与流中断重试统一由同一配置值控制
        set_retry_count(stream_retry_max)
        while True:
            # a. 修复messages
            self._msg_manager.repair()

            # b. 调用LLM
            content_parts = []
            self._last_content_parts = content_parts
            tool_calls_result = []
            call_usage = None
            parsed_finish_reason = None
            stream_interrupted_info = None  # LLM层上报的流中断信息（kind/detail）

            for chunk in self._llm.chat_stream(self._msg_manager.view.to_list(), cancel_check=lambda: stream.cancelled):
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

                # f. 流中断上报（LLM层流读取异常，无完成标记）
                if "stream_interrupted" in chunk:
                    stream_interrupted_info = chunk["stream_interrupted"]
                    continue

                # g. 重试通知（LLM层网络/429重试的用户提示，非AI内容，不入历史）
                if "retry_notice" in chunk:
                    stream.feed(chunk["retry_notice"])
                    continue

                # h. 处理结束
                if "finish_reason" in chunk:
                    parsed_finish_reason = chunk["finish_reason"]
                    if parsed_finish_reason == "error":
                        stream.finish(
                            self._stats.input_tokens,
                            self._stats.output_tokens,
                            cache_ratio=self._stats.cache_hit_ratio,
                            cost=self._stats.cost,
                            balance=self._stats.balance,
                            thinking_effort=self._thinking_label,
                        )
                        return

            # 中断检查
            if stream.cancelled:
                stream.abort()
                self._ui.on_interrupted()
                return

            # 本轮收到正常完成标记（含工具轮）→ 重置流中断重试预算（每轮独立）
            if parsed_finish_reason is not None:
                stream_interrupted_retries = 0

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

            # ── 响应流中断（无完成标记）→ 整轮重试（指数退避）──
            # 正常完成必有finish_reason；None表示服务端在响应中途断开。
            # 此时assistant消息尚未写入历史（部分内容/不完整tool_calls被丢弃），
            # 重发幂等无副作用。重试上限=配置的LLM重试次数，网络波动时可连续自救。
            if parsed_finish_reason is None:
                if stream_interrupted_retries < stream_retry_max:
                    stream_interrupted_retries += 1
                    kind = (stream_interrupted_info or {}).get("kind", "unknown")
                    base = RETRY_BACKOFF_BASE[
                        min(stream_interrupted_retries - 1, len(RETRY_BACKOFF_BASE) - 1)
                    ]
                    if self._logger:
                        self._logger.warning(
                            "agent_loop",
                            f"响应流中断({kind})，{base}s后自动重试"
                            f"(第{stream_interrupted_retries}/{stream_retry_max}次)",
                        )
                    # 清空渲染器缓冲：重试流会从开头重播，上一轮残留的半行文字/
                    # 表格行/代码块若不清除，会与重播内容拼接重复或错乱。
                    stream.reset_renderer()
                    stream.feed(
                        f"\n⚠ 服务端响应流中断，约{base}s后自动重试"
                        f"（第{stream_interrupted_retries}/{stream_retry_max}次）…\n"
                    )
                    # 退避等待期间用户可ESC取消
                    if not retry_sleep(stream_interrupted_retries - 1, lambda: stream.cancelled):
                        stream.abort()
                        self._ui.on_interrupted()
                        return
                    continue
                # 重试耗尽 → 报错结束（截断内容不写入历史，避免污染上下文）
                stream.feed(
                    f"\n\n⚠ 服务端响应流中断，已自动重试{stream_retry_max}次仍失败，请稍后重试。\n"
                )
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache_ratio=self._stats.cache_hit_ratio,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                    thinking_effort=self._thinking_label,
                )
                return

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
                    "stream_interrupted": "⚠ 服务端响应流中断，请稍后重试。",
                }
                reason = parsed_finish_reason or "stream_interrupted"
                msg = _empty_msgs.get(reason, f"⚠ AI 返回异常（{reason}），请稍后重试。")
                stream.feed(f"\n\n{msg}\n")
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache_ratio=self._stats.cache_hit_ratio,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                    thinking_effort=self._thinking_label,
                )
                return

            # 更新统计
            if call_usage:
                self._stats.update(call_usage)

            self._last_round_ok = True  # 正常完成：无tool_call纯文本输出
            stream.finish(
                self._stats.input_tokens,
                self._stats.output_tokens,
                cache_ratio=self._stats.cache_hit_ratio,
                cost=self._stats.cost,
                balance=self._stats.balance,
                thinking_effort=self._thinking_label,
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

        # 结束当前流式输出（本轮还没结束，不显示stats，等确认后最终finish再显示）
        stream.finish(
            self._stats.input_tokens,
            self._stats.output_tokens,
            cache_ratio=self._stats.cache_hit_ratio,
            cost=self._stats.cost,
            balance=self._stats.balance,
            thinking_effort=self._thinking_label,
            with_stats=False,
        )

        # 在#提示符下显示确认信息，等待用户输入
        user_input = self._ui.read_input_with_prompt("  确认执行此命令? [y/N]: ")
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
            self._msg_manager.append_tool_result(confirm_tc_id, "[操作已取消: 此命令需用户确认]")

        # 创建新的流式输出会话，继续agent_loop
        return self._ui.create_stream()

    def _dump_empty_debug(self, content_parts, tool_calls_result, finish_reason, call_usage):
        """空回复时写调试日志"""
        debug_path = os.path.join(
            self._config.paths.data_dir,
            f"debug_empty_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        debug_data = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": {"messages": self._msg_manager.view.to_list()},
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
