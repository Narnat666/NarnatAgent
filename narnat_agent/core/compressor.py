"""
压缩执行 —— 构建压缩prompt、构建新会话messages
"""

from typing import List, Dict, Any

from ..config.defaults import COMPRESS_PROMPT


class Compressor:
    """
    上下文压缩器。

    纯函数：不在磁盘留下任何中间文件，
    build_compress_messages → LLM总结 → build_new_session_messages 全部在内存中完成。
    """

    def build_compress_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        构建压缩请求的messages。

        复用当前对话的全部messages，末尾追加压缩指令。
        """
        compress_messages = list(messages)  # 浅拷贝
        compress_messages.append({
            "role": "user",
            "content": COMPRESS_PROMPT,
        })
        return compress_messages

    def build_new_session_messages(
        self, system_prompt: str, summary: str
    ) -> List[Dict[str, Any]]:
        """
        创建新会话的messages。

        压缩摘要作为独立的system消息注入，与skill格式一致。
        """
        messages = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": f"# 上一轮对话成果\n\n{summary}"})
        return messages
