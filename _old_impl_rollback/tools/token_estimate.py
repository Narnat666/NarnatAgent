"""统一 token 估算 —— 截断提示与压缩估价共用的混合密度启发式

对齐官方 harness token-meter 的思路：启发式只负责短距离估算，长距离
由 API 真实 usage 锚定（narnat 侧：stats 每轮 input_tokens 即锚点）。
官方固定 4 字符/token 对中文低估约 3 倍（中文实测约 0.7 token/字），
故采用分段系数：CJK ≈ 0.7、其余 ≈ 0.25，并对用户可见处标注 "≈"。

用途（两处消费者）：
1. 压缩器 select_cut_index 的尾部保留估价（core/compressor.py）
2. 工具输出截断提示的 "≈N token" 标注（registry / bash / read）
"""

import re

# CJK 统一表意 + 全角符号 + 假名 + 谚文（全角空格 U+3000 计入）
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\uff00-\uffef\uac00-\ud7af]")

# 实测密度：中文约 0.7 token/字（DeepSeek BPE 常用词 2 字 1 token）；
# 其余字符按官方 4 字符/token 基准（英文 BPE 平均约 0.25~0.3）。
_TOKEN_PER_CJK = 0.7
_TOKEN_PER_OTHER = 0.25

# 消息级 JSON 框架开销（对齐官方 harness 的 BLOCK_OVERHEAD/ROLE_OVERHEAD 思路）
_MSG_OVERHEAD = 8


def estimate_text_tokens(text: str) -> int:
    """估算一段文本的 token 数（向上取整）。空文本返回 0。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    est = cjk * _TOKEN_PER_CJK + other * _TOKEN_PER_OTHER
    return int(est + 0.999)  # ceil，避免小文本归 0


def estimate_message_tokens(msg: dict) -> int:
    """估算一条消息占用的 token 数（content/thinking/tool_calls 汇总）。

    content 覆盖 str/list 两种形态；tool_calls 按 name+arguments 估价。
    """
    tokens = _MSG_OVERHEAD
    content = msg.get("content")
    if isinstance(content, str):
        tokens += estimate_text_tokens(content)
    elif content:
        # 防御性：块列表形态按 JSON 文本长度估价
        tokens += estimate_text_tokens(str(content))
    thinking = msg.get("thinking")
    if thinking:
        tokens += estimate_text_tokens(thinking)
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        tokens += estimate_text_tokens(fn.get("name", "") or "")
        tokens += estimate_text_tokens(fn.get("arguments", "") or "")
    return tokens
