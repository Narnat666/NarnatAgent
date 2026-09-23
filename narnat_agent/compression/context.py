"""窗口占比跟踪 —— 上下文窗口占比状态机（占比计算、压缩触发、一次性告警、重置）。

行为契约：`openspec/changes/recast-v2/specs/compression/spec.md`
（Requirement：窗口占比与触发判定 / 兼容性怪癖保持）。

结构（design D1/D7）：占比状态归属本积木且只此一处；消费方（会话层）直接持有本对象
读取与刷新，原协调器对 `ContextManager` 的纯转发层已合并删除。日志经 `LogSink`
端口调用（构造注入），未注入时静默。
"""
from __future__ import annotations

from typing import Protocol

from ..config.defaults import (
    DEFAULT_COMPRESS_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_WARN_RATIO,
)

__all__ = ["CompressionContext", "LogSink"]


class LogSink(Protocol):
    """日志端口（构造注入）：`info/warning/error(模块名, 消息)`。

    实现方（app 侧日志器）仅在调试模式写文件、未启动时日志调用静默丢弃；
    本积木不感知其实现。
    """

    def info(self, module: str, message: str) -> None:
        """记录一条 info 日志。"""
        ...

    def warning(self, module: str, message: str) -> None:
        """记录一条 warning 日志。"""
        ...

    def error(self, module: str, message: str) -> None:
        """记录一条 error 日志。"""
        ...


class CompressionContext:
    """上下文窗口占比基准（占比、触发判定、告警与重置状态）。

    占比(%) = 最近一次服务端返回的输入 token 数 / 配置的上下文窗口大小 × 100
    - 分子：最近一次请求的真实 token（含全部历史 + 缓存命中），本地不估算；
    - 分母：静态配置；≤ 0 视为无效，占比为「无数据」。

    职责：占比刷新（回复结束后 / 工具轮 usage 到达时）、压缩触发判断（请求前）、
    一次性告警（每次轮次结束后检查）、压缩成功后重置、摘要请求失败后延后重试。
    """

    def __init__(self, context_window: int = DEFAULT_CONTEXT_WINDOW,
                 warn_ratio: int = DEFAULT_WARN_RATIO,
                 compress_ratio: int = DEFAULT_COMPRESS_RATIO,
                 logger: LogSink | None = None) -> None:
        self._context_window = context_window
        self._warn_ratio = warn_ratio
        self._compress_ratio = compress_ratio
        self._logger = logger
        self._ratio: float | None = None   # 最近一次窗口占比（%），None=无数据/无效
        self._warned = False               # 本会话是否已告警过

    @property
    def ratio(self) -> float | None:
        """最近一次窗口占比（%），无数据或窗口无效时为 None。"""
        return self._ratio

    def update_ratio(self, input_tokens: int) -> None:
        """按服务端返回的输入 token 数快照更新占比。

        窗口 ≤ 0 或输入 token ≤ 0（首轮/恢复会话无数据）→ 占比置「无数据」
        （不告警、不压缩）。
        """
        if self._context_window > 0 and input_tokens > 0:
            self._ratio = input_tokens / self._context_window * 100
        else:
            self._ratio = None

    def need_compress(self) -> bool:
        """是否需要压缩（占比 ≥ 压缩阈值，闭区间比较；无数据一律为否）。"""
        return self._ratio is not None and self._ratio >= self._compress_ratio

    def check_warn(self) -> str:
        """检查是否触发告警，返回提示文案（空串表示无告警）。本会话仅提示一次。

        文案固定为 `窗口占比已达{N}%，建议开启新对话`（N 为告警阈值数字）；
        压缩成功后经 `reset()` 清除告警状态，此后可再次告警。
        """
        if self._ratio is None or self._warned:
            return ""
        if self._ratio >= self._warn_ratio:
            self._warned = True
            message = f"窗口占比已达{self._warn_ratio}%，建议开启新对话"
            if self._logger is not None:
                self._logger.warning("compression", message)
            return message
        return ""

    def reset(self) -> None:
        """重置占比数据与告警状态（压缩成功后调用）。"""
        self._ratio = None
        self._warned = False

    def set_retry_soon(self) -> None:
        """摘要请求失败后的占比处理：置为「压缩阈值 − 10」（下限 0）以延后重试。"""
        self._ratio = max(0.0, self._compress_ratio - 10)
