"""摘要器 —— LLM 命名会话 + LLM 总结探索分支

从 Agent._do_summarize() 和 Agent._do_name_session() 提取。
算法逻辑原样保留。
"""

from typing import List, Dict, Any, Callable, Optional

from .llm import LLMClient
from ..config.loader import Config
from ..logger import AgentLogger


class Summarizer:
    """LLM 摘要器"""

    def __init__(self, llm: LLMClient, config: Config, logger: AgentLogger):
        self._llm = llm
        self._config = config
        self._logger = logger

    def summarize(self, messages: List[Dict[str, Any]],
                  cancel_check: Callable[[], bool]) -> str:
        """用 LLM 总结消息内容（探索分支合并时使用）"""
        summary_parts = []
        for chunk in self._llm.chat_stream(messages, no_tools=True,
                                           cancel_check=cancel_check):
            if cancel_check():
                return ""
            if "content" in chunk and "tool_calls" not in chunk:
                summary_parts.append(chunk["content"])
        return "".join(summary_parts)

    def name_session(self, messages: List[Dict[str, Any]]) -> str:
        """用 LLM 生成会话名称。返回空串表示失败。"""
        from ..config.session_store import list_sessions
        existing = list_sessions(self._config.paths.narnat_dir)
        taken_names = [s["name"] for s in existing]
        hint = ""
        if taken_names:
            hint = f"\n注意：以下名称已被占用，请勿使用：{', '.join(taken_names)}"
        name_messages = list(messages)
        name_messages.append({
            "role": "user",
            "content": f"请为以上对话起一个简短标题（15字以内），直接输出标题，不要引号不要解释。{hint}\n【重要】不要调用任何工具，直接输出标题文本。"
        })
        parts = []
        for chunk in self._llm.chat_stream(name_messages, no_tools=False, no_thinking=True,
                                           cancel_check=lambda: False):
            if "content" in chunk and "tool_calls" not in chunk:
                parts.append(chunk["content"])
        name = "".join(parts).strip()
        if not name:
            return ""
        name = name.strip('"\'""''《》「」')
        if len(name) > 30:
            name = name[:30]
        return name
