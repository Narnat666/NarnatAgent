"""摘要器 —— 模型命名会话与模型总结探索分支（sessions 命令层的注入实现）。

行为搬运自旧实现 `narnat_agent/core/summarizer.py`（提示词文案、收集口径、名称
清洗与截断逐条等价），结构按新包协议重组：

- `summarize(messages, cancel_check)`：探索分支合并时的模型总结（`no_tools=True`；
  取消即返回空串，历史不变）；
- `name_session(messages)`：`/save` 空名与自动保存的模型命名（列出已占用会话名
  作为提示；带工具、禁思考；结果剥引号类字符并截断 30 字）。

设计（design D8）：装配点构造后以绑定方法注入 `sessions`（`name_func` /
`summarize_func`），不残留后置补线；命名请求的「不可取消」语义以静态方法表达，
不落散落 lambda。

依赖规则（design D1）：本积木位于 L4，只依赖低层积木（llm 流、消息历史、
会话存储经构造注入的鸭子协议）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..contracts.llm_events import KEY_CONTENT, is_text_delta

__all__ = ["Summarizer"]

NAME_PROMPT_SUFFIX = (
    "\n【重要】不要调用任何工具，直接输出标题文本。"
)
"""命名请求的末尾固定指令（已发布文案）。"""

NAME_NAME_LIMIT = 30
"""会话名长度上限（超出截断）。"""

QUOTE_CHARS = "\"'“”‘’《》「」"
"""命名结果需剥除的首尾引号类字符（含中文引号与书名号）。"""


class Summarizer:
    """LLM 摘要器（构造注入 LLM 流与已占用会话名来源）。

    - `llm`：LLM 流式接口（`chat_stream`，形态见 `contracts.llm_events`）；
    - `store`：会话存储门面（`SessionStore.list_tree()` 提供已占用根会话名）；
    - `logger`：日志端口（缺省不记录）。
    """

    def __init__(self, llm, store, logger: Optional[Any] = None) -> None:
        self._llm = llm
        self._store = store
        self._logger = logger

    @staticmethod
    def _never_cancel() -> bool:
        """命名请求的取消查询：恒为假（命名沿用旧实现的不可取消语义）。"""
        return False

    def summarize(self, messages: list[dict[str, Any]],
                  cancel_check: Callable[[], bool]) -> str:
        """用 LLM 总结消息内容（探索分支合并时使用）；取消或失败返回空串。"""
        summary_parts: list[str] = []
        for chunk in self._llm.chat_stream(messages, no_tools=True,
                                           cancel_check=cancel_check):
            if cancel_check():
                return ""
            if is_text_delta(chunk):
                summary_parts.append(chunk[KEY_CONTENT])
        return "".join(summary_parts)

    def name_session(self, messages: list[dict[str, Any]]) -> str:
        """用 LLM 生成会话名称；返回空串表示命名失败。"""
        taken_names = [root["name"] for root in self._store.list_tree()]
        hint = ""
        if taken_names:
            hint = f"\n注意：以下名称已被占用，请勿使用：{', '.join(taken_names)}"
        name_messages = list(messages)
        name_messages.append({
            "role": "user",
            "content": ("请为以上对话起一个简短标题（15字以内），直接输出标题，"
                        f"不要引号不要解释。{hint}{NAME_PROMPT_SUFFIX}"),
        })
        parts: list[str] = []
        for chunk in self._llm.chat_stream(name_messages, no_tools=False, no_thinking=True,
                                           cancel_check=self._never_cancel):
            if is_text_delta(chunk):
                parts.append(chunk[KEY_CONTENT])
        name = "".join(parts).strip()
        if not name:
            return ""
        name = name.strip(QUOTE_CHARS)
        if len(name) > NAME_NAME_LIMIT:
            name = name[:NAME_NAME_LIMIT]
        return name
