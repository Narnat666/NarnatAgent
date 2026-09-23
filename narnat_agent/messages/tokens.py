"""token 估算（唯一实现）—— 截断提示与压缩估价共用的混合密度启发式。

行为契约：`specs/compression` 与 `specs/tools-file` 各自的「token 估算口径」Requirement
（两条口径同源，均以本模块为实现的唯一落点）。原 `compression/compressor.py` 与
`tools/token_estimate.py` 各持一份同名同逻辑实现（逻辑双轨），按 T3.11 归一：唯一实现归
messages 积木（L1）——messages 是消息领域的所有者，token 估算是消息/文本度量工具；
两个 L2 消费方（compression / tools）向下引用本模块，不再各自持有副本。

对齐官方 harness token-meter 的思路：启发式只负责短距离估算，长距离
由 API 真实 usage 锚定（narnat 侧：stats 每轮 input_tokens 即锚点）。
官方固定 4 字符/token 对中文低估约 3 倍（中文实测约 0.7 token/字），
故采用分段系数：CJK ≈ 0.7、其余 ≈ 0.25，并对用户可见处标注 "≈"。

基准来源（双向锁定，禁止单侧调整系数与舍入口径）：
- `v2/tests/baseline/data/token_estimate.json`：`token_estimate.estimate_text_tokens`（9 例）
  与 `token_estimate.estimate_message_tokens`（6 例）两组直接对照；
- `v2/tests/baseline/data/compressor.json`：同源组 `token_estimate.json` 的
  `compressor.estimate_tokens`（6 例，旧转发壳组）逐字一致；另 `compressor.json` 的
  `select_cut_index` 组经「尾部逐条累计估价」间接锁定同一口径。

用途（消费方，均经转发路径引用）：
1. 压缩器切点选择的尾部保留估价（compression 积木，经 `compressor` 转发）；
2. 工具输出截断提示的 "≈N token" 标注（tools 族的 `token_estimate` 转发壳）；
3. 会话链路的压缩统计估价（coordinator 等，引用路径不变）。
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["estimate_message_tokens", "estimate_text_tokens"]

# CJK 统一表意 + 全角符号 + 假名 + 谚文（全角空格 U+3000 计入）
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                    r"\uf900-\ufaff\uff00-\uffef\uac00-\ud7af]")

# 实测密度：中文约 0.7 token/字（DeepSeek BPE 常用词 2 字 1 token）；
# 其余字符按官方 4 字符/token 基准（英文 BPE 平均约 0.25~0.3）。
TOKEN_PER_CJK = 0.7
TOKEN_PER_OTHER = 0.25

# 消息级 JSON 框架开销（对齐官方 harness 的 BLOCK_OVERHEAD/ROLE_OVERHEAD 思路）
MSG_OVERHEAD = 8


def estimate_text_tokens(text: str) -> int:
    """估算一段文本的 token 数（CJK 按 0.7/字、其余按 0.25/字符，向上取整）。

    空文本返回 0。仅用于切点选择与结果统计展示；精确的窗口压力判断以服务端
    返回的输入 token 为准（specs/compression「token 估算口径」）。
    """
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    other = len(text) - cjk
    est = cjk * TOKEN_PER_CJK + other * TOKEN_PER_OTHER
    return int(est + 0.999)  # ceil，避免小文本归 0


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """估算一条消息占用的 token 数（8 框架开销 + content/thinking/tool_calls 汇总）。

    content 覆盖字符串与块列表两种形态（块列表按文本近似）；tool_calls 按函数名
    与参数字符串估价；无内容的消息只计框架开销。
    """
    tokens = MSG_OVERHEAD
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
