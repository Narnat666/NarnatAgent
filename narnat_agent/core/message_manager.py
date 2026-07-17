"""消息管理 —— 消息修复、压缩触发

MessageManager 负责消息的修复逻辑和压缩流程编排，
实际的列表持有和修改委托给 MessageList（唯一所有者）。
"""

import os
from typing import List, Dict, Any, Optional

from .compressor import Compressor
from .message_list import MessageList, MessageView
from ..logger import AgentLogger


class MessageManager:
    """消息管理器 —— 委托 MessageList 执行修改，自身负责 repair 逻辑和压缩编排"""

    def __init__(self, messages: MessageList, compressor: Compressor,
                 logger: Optional[AgentLogger] = None):
        self._messages = messages
        self._compressor = compressor
        self._logger = logger

    @property
    def view(self) -> MessageView:
        """只读视图（供LLM调用等场景）"""
        return self._messages.view()

    # ── 追加方法（委托 MessageList）──

    def append_system(self, content: str) -> None:
        self._messages.append_system(content)

    def append_user(self, content: str) -> None:
        self._messages.append_user(content)

    def append_assistant(self, content: str, tool_calls: Optional[list] = None) -> None:
        self._messages.append_assistant(content, tool_calls)

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        self._messages.append_tool_result(tool_call_id, result)

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        self._messages.append_interrupted_tools(tool_calls, completed_ids)

    # ── 修复方法（repair 逻辑原样保留，通过 view 读取）──

    def repair(self) -> None:
        """修复messages：打断后可能留下不完整的消息序列。

        1. assistant含tool_calls但没有对应的tool消息 → 补上tool("[用户中断]")
        2. 如果第1步修复了，且末尾是tool消息 → 补上assistant（API要求tool后不能直接跟user）
        """
        msgs = self._messages

        # 1. 为未回复的tool_call补上空结果
        replied_ids = set()
        for msg in msgs.view():
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        for msg in msgs.view():
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        msgs.append_tool_result(tc_id, "[用户中断]")
                        replied_ids.add(tc_id)
                        repaired = True

        # 2. 只有在第1步确实修复了未回复的tool_call时，才补assistant
        if repaired and len(msgs) > 0 and msgs.view()[-1].get("role") == "tool":
            msgs.append_assistant("（用户中断了工具执行）")

        if repaired:
            self._logger.info("core.message_manager", "repair: 修复了打断后的消息序列")

    # ── 压缩方法（逻辑原样保留，通过 view/to_list 读取）──

    def handle_compress(self, pending_input: str, system_prompt: str,
                        llm_client, cancel_check, on_interrupt, on_llm_error) -> bool:
        """
        处理上下文压缩。

        Returns:
            True=压缩成功，pending_input已在messages中
            False=压缩失败/中断
        """
        self._logger.info("message_manager", f"压缩触发, messages={len(self._messages)}条")

        # 构建压缩请求
        compress_messages = self._compressor.build_compress_messages(self._messages.view().to_list())

        # 发送压缩请求，收集AI输出
        summary_content = []
        llm_error = False
        for chunk in llm_client.chat_stream(compress_messages, no_tools=True, cancel_check=cancel_check):
            if cancel_check and cancel_check():
                on_interrupt()
                return False
            if "finish_reason" in chunk and chunk["finish_reason"] == "error":
                llm_error = True
                break
            if "content" in chunk and "tool_calls" not in chunk:
                summary_content.append(chunk["content"])

        if llm_error:
            on_llm_error("压缩失败: LLM调用出错")
            return False

        summary = "".join(summary_content)

        # 校验总结结果（内存中直接校验）
        if not summary.strip():
            on_llm_error("压缩失败: 总结为空")
            return False

        # 先完整构建新会话（含用户问题），再原子替换旧会话
        new_messages = self._compressor.build_new_session_messages(system_prompt, summary)
        new_messages.append({"role": "user", "content": pending_input})
        self._messages.replace_all(new_messages)

        self._logger.info("message_manager", "压缩成功，新会话已创建")
        return True

    def clear_and_rebuild(self, system_prompt: str, summary_text: str) -> None:
        """清空并重建消息列表（压缩后使用）"""
        new_messages = self._compressor.build_new_session_messages(system_prompt, summary_text)
        self._messages.replace_all(new_messages)
