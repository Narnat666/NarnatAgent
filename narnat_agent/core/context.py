"""
上下文管理 —— 窗口占比计算、压缩触发、警告提示、会话销毁/创建
"""

from typing import Optional

from ..config.defaults import (
    DEFAULT_CONTEXT_WINDOW, DEFAULT_WARN_RATIO, DEFAULT_COMPRESS_RATIO,
)


class ContextManager:
    """
    上下文管理器（窗口占比基准）。

    占比(%) = 服务端返回的 prompt_tokens / 用户配置的上下文窗口大小 × 100
    - 分子：最近一次请求的真实 token（含全部历史 + 缓存命中），本地不估算
    - 分母：静态配置；≤ 0 视为无效，占比为 None（显示 --，不告警、不压缩）

    职责：
    - 占比更新（回复结束后）
    - 压缩触发判断（下次输入前）
    - 警告提示（本会话仅一次）
    """

    def __init__(self, logger=None,
                 context_window: int = DEFAULT_CONTEXT_WINDOW,
                 warn_ratio: int = DEFAULT_WARN_RATIO,
                 compress_ratio: int = DEFAULT_COMPRESS_RATIO):
        self._logger = logger
        self._context_window = context_window
        self._warn_ratio = warn_ratio
        self._compress_ratio = compress_ratio
        self._ratio: Optional[float] = None   # 最近一次窗口占比（%），None=无数据/无效
        self._warned = False                  # 本会话是否已告警过

    @property
    def ratio(self) -> Optional[float]:
        """最近一次窗口占比（%），无数据或窗口无效时为 None"""
        return self._ratio

    def update_ratio(self, input_tokens: int) -> None:
        """回复结束后更新窗口占比。

        input_tokens 为服务端返回的 prompt_tokens（快照赋值）。
        窗口 ≤ 0 或 input_tokens 为 0（首轮/恢复会话无数据）→ 占比置 None。
        """
        if self._context_window > 0 and input_tokens > 0:
            self._ratio = input_tokens / self._context_window * 100
        else:
            self._ratio = None

    def need_compress(self) -> bool:
        """是否需要压缩（占比 ≥ 压缩阈值）"""
        return self._ratio is not None and self._ratio >= self._compress_ratio

    def check_warn(self) -> str:
        """检查是否触发告警，返回提示文案（空串表示无告警）。本会话仅提示一次。"""
        if self._ratio is None or self._warned:
            return ""
        if self._ratio >= self._warn_ratio:
            self._warned = True
            msg = f"窗口占比已达{self._warn_ratio}%，建议开启新对话"
            if self._logger:
                self._logger.warning("core.context", msg)
            return msg
        return ""

    def reset(self):
        """重置（压缩成功后调用）：占比归 0、告警标记清除。"""
        self._ratio = None
        self._warned = False

    def set_retry_soon(self):
        """压缩失败后设置近期重试（占比设为 压缩阈值-10，留 10% 余量）"""
        self._ratio = max(0.0, self._compress_ratio - 10)
