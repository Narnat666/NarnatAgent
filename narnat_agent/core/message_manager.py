"""消息管理 —— 消息修复、追加、压缩触发

从agent.py中提取，负责维护messages列表的完整性。
所有对messages的修改都通过此模块进行，agent不再直接操作列表。
"""

import os
from typing import List, Dict, Any, Optional

from .compressor import Compressor
from ..config.loader import AppConfig
from ..logger import AgentLogger


class MessageManager:
    """消息列表管理器 —— messages的唯一修改入口"""

    def __init__(self, messages: List[Dict[str, Any]], compressor: Compressor,
                 logger: Optional[AgentLogger] = None):
        self._messages = messages
        self._compressor = compressor
        self._logger = logger

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """只读访问messages列表（供LLM调用等场景）"""
        return self._messages

    # ── 追加方法 ──

    def append_system(self, content: str) -> None:
        """追加系统消息"""
        self._messages.append({"role": "system", "content": content})

    def append_user(self, content: str) -> None:
        """追加用户消息"""
        self._messages.append({"role": "user", "content": content})

    def append_assistant(self, content: str, tool_calls: Optional[list] = None) -> None:
        """追加assistant消息"""
        msg = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        """追加工具结果消息"""
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        """为未完成的tool_call追加中断结果"""
        for tc in tool_calls:
            if tc["id"] not in completed_ids:
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "[用户中断]",
                })

    # ── 修复方法 ──

    def repair(self) -> None:
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
        if repaired and self._messages and self._messages[-1].get("role") == "tool":
            self._messages.append({"role": "assistant", "content": "（用户中断了工具执行）"})

        if repaired:
            self._logger.info("core.message_manager", "repair: 修复了打断后的消息序列")

    # ── 压缩方法 ──

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
        compress_messages = self._compressor.build_compress_messages(self._messages)

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

        # 写入磁盘
        if not self._compressor.write_summary(summary):
            on_llm_error("压缩失败: 写入失败")
            return False

        # 校验总结结果
        if not self._compressor.verify_summary():
            on_llm_error("压缩失败: 总结为空")
            return False

        # 销毁旧会话，创建新会话
        self._messages.clear()
        summary_text = self._compressor.read_summary()
        self._messages.extend(
            self._compressor.build_new_session_messages(system_prompt, summary_text)
        )
        self._compressor.reset_summary()

        self._logger.info("message_manager", "压缩成功，新会话已创建")

        # 恢复用户问题
        self._messages.append({"role": "user", "content": pending_input})
        return True

    def clear_and_rebuild(self, system_prompt: str, summary_text: str) -> None:
        """清空并重建消息列表（压缩后使用）"""
        self._messages.clear()
        self._messages.extend(
            self._compressor.build_new_session_messages(system_prompt, summary_text)
        )
