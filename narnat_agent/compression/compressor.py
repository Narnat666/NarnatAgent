"""压缩器 —— 摘要请求构建、保留尾部切点选择、会话重建与 token 估算。

行为契约：`openspec/changes/recast-v2/specs/compression/spec.md`
（Requirement：摘要请求构造 / 保留尾部切点选择 / 压缩后会话重建 / token 估算口径）。

结构（design D1）：本模块为 compression 积木的纯函数层——无 IO、无状态；接口面与旧实现
`narnat_agent/core/compressor.py` 逐条对应，行为搬运零改动（旧 `estimate_tokens` 转发壳
已按报告「补丁痕迹」删除；T3.11 起估算的唯一实现在 `messages` 积木，本模块按新架构引用）。

token 估算的唯一实现归 `messages.tokens`（L1）：`specs/compression` 与 `specs/tools-file`
的两条「token 估算口径」同源，原先 compression 与 tools 各持一份同名同逻辑副本（逻辑
双轨），按 T3.11 归一为 `v2/narnat_agent/messages/tokens.py`。本模块只转发（re-export）
该实现，`compression.compressor.estimate_*` 的既有引用路径与基准对照面保持不变，消费方
（`coordinator` 等）无需改动。

依赖方向（design D1）：compression（L2）→ messages（L1）为向下引用，无分层障碍。
"""
from __future__ import annotations

from typing import Any

from ..config.defaults import COMPRESS_PROMPT
from ..messages.tokens import estimate_message_tokens, estimate_text_tokens

__all__ = [
    "Compressor",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "select_cut_index",
]

# estimate_text_tokens / estimate_message_tokens 为 `messages.tokens` 的转发导出
# （实现与系数口径见该模块；本模块不再持有副本），保留同名导出即保持调用面不变。


def _cut_balanced(messages: list[dict[str, Any]], cut: int) -> bool:
    """切点 `cut` 处是否安全：保留尾部首条必须是 `user` 消息。

    Anthropic 协议强制 messages 首条为 user 且角色交替；要求尾部首条是 user
    同时天然避免拆散 assistant(tool_calls)/tool 对（工具对永远以 assistant 开头）。
    OpenAI 协议同样受益（只是少保留一两条）。

    注：本函数是基准对照面（`v2/tests/baseline/data/compressor.json` 的
    `compressor._cut_balanced` 组），更名须同步基准与测试。
    """
    if cut >= len(messages):
        return True
    return messages[cut].get("role") == "user"


def select_cut_index(messages: list[dict[str, Any]],
                     retain_tokens: int) -> int | None:
    """选择压缩切点：返回保留尾部起点下标；None = 不保留（全量压缩）。

    - `retain_tokens <= 0` → None（不保留，全量压缩）
    - 从尾部向前按启发式估价累计至预算 → 得保留起点
    - 回退到尾部首条为 user 的切点（Anthropic 首条/交替约束，且不拆工具对）
    - 预算充足但对话区首条不是 user（异常序列）→ None 全量压缩兜底
    - 预算不足 → 返回对话区起点（表示保留全部非 system 消息）

    注：切点只可能落在非 system 消息上（首部连续 system 被跳过；更靠后的 system
    由重建时过滤），system 消息（含技能注入）不进保留区，重建时由新的
    system_prompt + 摘要承担。
    """
    if retain_tokens <= 0 or not messages:
        return None

    start = 0
    while start < len(messages) and messages[start].get("role") == "system":
        start += 1
    if start >= len(messages):
        return None

    accumulated = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, start - 1, -1):
        accumulated += estimate_message_tokens(messages[i])
        keep_from = i
        if accumulated >= retain_tokens:
            break
    while keep_from > start and not _cut_balanced(messages, keep_from):
        keep_from -= 1
    if messages[keep_from].get("role") != "user":
        return None  # 对话区首条不是 user：无安全保留切点，全量压缩兜底
    return keep_from


class Compressor:
    """压缩器的两个纯数据步骤（无实例状态）。

    全程在内存中完成（不留中间文件）：构建摘要请求 → 重建新会话消息列表。
    """

    def build_compress_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """构建摘要请求消息：全部历史 + 末尾一条 `user` 角色的压缩指令。

        浅拷贝列表后追加指令，不改动调用方列表结构（元素仍与调用方共享）。
        指令为结构化检查点模板（`config.defaults.COMPRESS_PROMPT`）。
        """
        compress_messages = list(messages)  # 浅拷贝
        compress_messages.append({
            "role": "user",
            "content": COMPRESS_PROMPT,
        })
        return compress_messages

    def build_new_session_messages(
        self, system_prompt: str, summary: str,
        tail_messages: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """以「系统提示词 + 摘要 + 保留尾部」重建会话消息列表。

        摘要以独立 `system` 消息承载，标题固定为 `# 上一轮对话成果`（与压缩指令中
        的旧摘要识别规则配套，不得单侧改动）；`summary` 为空时不注入摘要消息。
        尾部中（非首位）的 system 消息被静默丢弃，不进入新会话。
        """
        messages = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": f"# 上一轮对话成果\n\n{summary}"})
        for m in (tail_messages or []):
            if m.get("role") != "system":
                messages.append(m)
        return messages
