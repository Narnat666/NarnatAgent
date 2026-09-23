"""token 估算转发壳 —— 唯一实现在 `messages.tokens`，本模块只保持 tools 族既有引用路径。

T3.11 归一：原先本模块与 `compression/compressor.py` 各持一份**同名同逻辑**实现
（分段系数、取整口径与消息框架开销逐字相同），属逻辑双轨；现唯一实现归 messages 积木
（L1，消息领域的所有者）。本模块只做 re-export，tools 族内部既有
`from ..token_estimate import estimate_text_tokens`（file/read、registry、remote/common、
shell/executor）与包级导出（`tools.__init__`）不受影响。

系数、取整口径与基准来源见 `narnat_agent/messages/tokens.py`（基准文件
`v2/tests/baseline/data/token_estimate.json` 与本模块同名两组用例逐字锁定）。
"""
from __future__ import annotations

from ..messages.tokens import estimate_message_tokens, estimate_text_tokens

__all__ = ["estimate_message_tokens", "estimate_text_tokens"]
