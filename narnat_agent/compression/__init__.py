"""compression 积木 —— 上下文压缩：占比触发、摘要编排、尾部保留重建、溢出恢复、手动压缩。

行为契约：`openspec/changes/recast-v2/specs/compression/spec.md`（10 Requirement）。

对外面（design D1：跨积木只经显式接口）：
- `CompressionContext`：窗口占比基准（占比刷新、压缩触发、一次性告警、重置）——
  消费方（会话层）直接持有本对象，原协调器的纯转发层已合并删除；
- `CompressionCoordinator`：压缩入口（用户轮边界自动压缩 / 无输入压缩（运行中自查与
  溢出恢复共用）/ 手动压缩）与运行中自查；
- `CompressResult`：一次压缩执行的结果归因（成功统计与失败原因）；
- `OVERFLOW_CONTINUE_MESSAGE`：压缩重建后的内部继续指令文本（以字面 `[]` 开头）；
- `MANUAL_FAIL_TEXT`：手动压缩失败归因 → 用户提示映射；
- `LogSink`：日志端口协议（构造注入，缺省不记录）。

依赖规则（design D1）：本积木位于 L2，只依赖 `contracts`（L0）与 `config` / `messages`
（L1）；`llm` 流经构造注入（`LLMStreamSource` 契约承载），压缩动画经 `Animator` 端口
注入，中断经 `InterruptSignal` 注入。
"""
from __future__ import annotations

from .context import CompressionContext, LogSink
from .coordinator import (
    MANUAL_FAIL_TEXT,
    OVERFLOW_CONTINUE_MESSAGE,
    CompressResult,
    CompressionCoordinator,
)

__all__ = [
    "MANUAL_FAIL_TEXT",
    "OVERFLOW_CONTINUE_MESSAGE",
    "CompressResult",
    "CompressionContext",
    "CompressionCoordinator",
    "LogSink",
]
