"""
压缩执行 —— 构建压缩prompt、选择保留尾部切点、构建新会话messages
"""

from typing import List, Dict, Any, Optional

from ..config.defaults import COMPRESS_PROMPT
from ..tools.token_estimate import estimate_message_tokens

# 估价统一走 tools/token_estimate.py 的混合密度启发式（CJK≈0.7/字、其余≈0.25/字符，
# 对齐官方 harness token-meter 思路）。此前固定 4 字符/token 对中文低估约 3 倍，
# 尾部保留估价失真会直接影响压缩后残留体积。仅用于切点选择，不追求精确；
# 精确压力判断仍由服务端 prompt_tokens 负责。


def estimate_tokens(msg: Dict[str, Any]) -> int:
    """启发式估价一条消息占用的 token 数（薄封装，见 token_estimate.py）"""
    return estimate_message_tokens(msg)


def _cut_balanced(messages: List[Dict[str, Any]], cut: int) -> bool:
    """切点 cut 处是否安全：保留尾部首条必须是 user 且尾部内无孤儿 tool。

    Anthropic 协议强制 messages 首条为 user 且角色交替；要求尾部首条是
    user 同时天然避免拆散 assistant(tool_calls)/tool 对（工具对永远以
    assistant 开头）。OpenAI 协议同样受益（只是少保留一两条）。
    非良构序列（repair() 把补的 tool 结果追加在列表末尾等异常路径）会让
    工具对跨越切点：其 tool_call 声明留在切点前、tool 结果落入尾部——
    此类切点视为不安全（回退找更早的 user 切点，或全量兜底）。
    """
    if cut >= len(messages):
        return True
    if messages[cut].get("role") != "user":
        return False
    declared_before = set()
    for m in messages[:cut]:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                tc_id = tc.get("id")
                if tc_id:
                    declared_before.add(tc_id)
    if not declared_before:
        return True
    for m in messages[cut:]:
        if m.get("role") == "tool" and m.get("tool_call_id") in declared_before:
            return False
    return True


def select_cut_index(messages: List[Dict[str, Any]], retain_tokens: int) -> Optional[int]:
    """选择压缩切点：返回保留尾部起点下标；None=不保留（全量压缩）。

    - retain_tokens <= 0 → None（不保留，全量压缩，旧行为）
    - 从尾部向前累计估价至预算 → 得保留起点
    - 回退到尾部首条为 user 的切点（Anthropic 首条/交替约束，且不拆工具对）
    - 预算充足但对话区首条不是 user（异常序列）→ None 全量压缩兜底
    - 预算充足但回退至对话区起点（单用户轮无中间 user）→ None 全量压缩兜底
    - 预算不足 → 返回对话区起点（0 合法，表示保留全部非 system 消息）

    注：切点只落在非 system 消息上，system 消息（含技能注入）不进保留区，
    重建时由新的 system_prompt + 摘要承担。
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
        accumulated += estimate_tokens(messages[i])
        keep_from = i
        if accumulated >= retain_tokens:
            break
    cut_pos = keep_from  # 预算落点（回退前）
    while keep_from > start and not _cut_balanced(messages, keep_from):
        keep_from -= 1
    if messages[keep_from].get("role") != "user":
        return None  # 对话区首条不是 user：无安全保留切点，全量压缩兜底
    if keep_from == start and cut_pos > start:
        # 预算够但唯一 user 切点在对话区起点（单用户轮长工具往返无中间 user）：
        # 返回起点会被判定"无可压缩历史"而永久跳过压缩（占比超阈后上下文
        # 持续膨胀直至溢出失败）；退为全量压缩兜底（与保留尾部=0 同路径）
        return None
    return keep_from


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
        self, system_prompt: str, summary: str,
        tail_messages: List[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        创建新会话的messages。

        压缩摘要作为独立的system消息注入，与skill格式一致；
        tail_messages 为逐字保留的近期尾部（不含 system 消息）。
        """
        messages = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": f"# 上一轮对话成果\n\n{summary}"})
        for m in (tail_messages or []):
            if m.get("role") != "system":
                messages.append(m)
        return messages
