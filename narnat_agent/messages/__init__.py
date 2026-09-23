"""messages 积木 —— 对话历史的唯一所有者、只读视图与请求前序列修复。

行为契约：`openspec/changes/recast-v2/specs/messages/spec.md`（Requirement：消息序列与角色
契约 / 只读视图 / 受控修改接口 / 中断补齐 / 请求前序列修复 / 兼容性怪癖保持）。

对外面：
- 读：`MessageStore.view()` → `MessageView`（实时只读；导出用 `to_list()`）；
- 写：`MessageStore` 的受控接口（追加各角色消息、批量补齐中断结果、整体替换）；
- 修复：`MessageStore.repair()` 在发出请求前修复中断留下的不完整序列；
- 共享字面量：`INTERRUPTED_TOOL_RESULT`、`SYNTHETIC_THINKING`（跨模块可感知识别依据，
  禁止改动其文本；模型请求构造层按后者判断思考块是否回传）。

依赖规则（design D1）：本积木位于 L1，只依赖标准库；跨积木共享面若需新增须经 contracts。
"""
from .store import (
    INTERRUPTED_TOOL_RESULT,
    SYNTHETIC_THINKING,
    LogSink,
    MessageStore,
    MessageView,
)

__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "SYNTHETIC_THINKING",
    "LogSink",
    "MessageStore",
    "MessageView",
]
