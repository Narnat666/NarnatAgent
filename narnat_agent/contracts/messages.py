"""消息序列的跨积木共享字面量 —— 契约来源：`specs/messages/spec.md`（repair 合成消息）。

这些字面量是**可被消费方识别的行为标记**（不只文本本身）：
- `SYNTHETIC_THINKING`：repair 为"已修复且尾部为 tool"的序列补的合成 assistant 的思考占位
  （`specs/messages/spec.md`「中断修复」）。llm 转换层依据它识别"合成占位"并决定
  思考回传；messages 依据它构造合成消息。两侧必须共用同一定义。
- `INTERRUPTED_TOOL_RESULT`：为未回复 tool_call 补的合成工具结果文本。

本模块为零逻辑纯定义层：只依赖标准库；不 import 新包其他积木。
"""
from __future__ import annotations

__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "SYNTHETIC_THINKING",
]

# 未完成 tool_call 的中断回执（specs/messages「中断补齐」）
INTERRUPTED_TOOL_RESULT = "[用户中断]"

# repair 合成 assistant 的思考占位（specs/messages「中断修复」；
# DeepSeek 思考模式校验要求非空思考块，llm 转换层按此值识别合成占位）
SYNTHETIC_THINKING = "（用户中断了工具执行）"
