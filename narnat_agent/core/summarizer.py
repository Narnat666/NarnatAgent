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

    def __init__(self, llm: LLMClient, config: Config, logger: AgentLogger, stats=None):
        self._llm = llm
        self._config = config
        self._logger = logger
        # 总结/命名调用的 token 与费用计入本会话消耗（同压缩/验证器口径），
        # 并即时刷新状态栏 —— 见 stats.log_external_usage
        self._stats = stats

    def _log_usage(self, usage: dict) -> None:
        try:
            self._stats.log_external_usage(usage)
        except Exception:
            pass

    def summarize(self, messages: List[Dict[str, Any]],
                  cancel_check: Callable[[], bool]) -> str:
        """用 LLM 总结消息内容（探索分支合并时使用）"""
        summary_parts = []
        failed = False
        for chunk in self._llm.chat_stream(messages, no_tools=True,
                                           cancel_check=cancel_check):
            if cancel_check():
                return ""
            # LLM 出错时正文就是一整段错误文案（如「[错误: API调用失败(401): …]」）。
            # 不判 finish_reason 会把它当成"总结结论"返回，/done 于是把这段
            # 错误原文合并进父会话、还把分支标成 completed —— 用户以为结论
            # 已经合并，实际父会话里多了一条假的结论（实测，数据被污染）。
            if chunk.get("finish_reason") == "error":
                failed = True
            if "content" in chunk and "tool_calls" not in chunk:
                summary_parts.append(chunk["content"])
            if "usage" in chunk and self._stats is not None:
                self._log_usage(chunk["usage"])
        if failed:
            return ""
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
            "content": f"请为以上对话起一个简短标题（15字以内），直接输出标题，不要引号不要解释。{hint}\n[重要]不要调用任何工具，直接输出标题文本。"
        })
        parts = []
        failed = False
        for chunk in self._llm.chat_stream(name_messages, no_tools=False, no_thinking=True,
                                           cancel_check=lambda: False):
            # 同 summarize：出错时正文是错误文案，不能拿它当会话名
            # （否则会话列表里会出现一个叫「[错误: API调用失败(401)…]」的会话）
            if chunk.get("finish_reason") == "error":
                failed = True
            if "content" in chunk and "tool_calls" not in chunk:
                parts.append(chunk["content"])
            if "usage" in chunk and self._stats is not None:
                self._log_usage(chunk["usage"])
        if failed:
            return ""
        name = "".join(parts).strip()
        if not name:
            return ""
        name = name.strip('"\'""''《》「」')
        if len(name) > 30:
            name = name[:30]
        return name
