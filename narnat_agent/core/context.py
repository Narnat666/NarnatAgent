"""
上下文管理 —— 窗口占比计算、压缩触发、警告提示、会话销毁/创建
"""

from typing import Optional

from ..config.defaults import (
    DEFAULT_CONTEXT_WINDOW, DEFAULT_WARN_RATIO, DEFAULT_COMPRESS_RATIO,
    DEFAULT_COMPRESS_HEADROOM,
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
                 compress_ratio: int = DEFAULT_COMPRESS_RATIO,
                 max_output_tokens: int = 0,
                 headroom_tokens: int = DEFAULT_COMPRESS_HEADROOM):
        self._logger = logger
        self._context_window = context_window
        self._warn_ratio = warn_ratio
        self._compress_ratio = compress_ratio
        self._max_output_tokens = max_output_tokens
        self._headroom_tokens = headroom_tokens
        self._ratio: Optional[float] = None   # 最近一次窗口占比（%），None=无数据/无效
        self._warned = False                  # 本会话是否已告警过

    @property
    def ratio(self) -> Optional[float]:
        """最近一次窗口占比（%），无数据或窗口无效时为 None"""
        return self._ratio

    def safe_threshold_tokens(self) -> Optional[int]:
        """压缩阈值的**安全上限**（token）。

        用户配的 compress_ratio 只是愿望值，这里再夹一道硬上限：

            上限 = 窗口 - 输出预留 - 安全余量

        为什么要夹：压缩请求本身也要吃掉窗口。若只看比例（例如 90%），
        "历史 900k + 压缩输出 128k" 就超过 1M 窗口——压缩请求自己先溢出，
        压缩永远失败，用户只能看到"压缩失败: 压缩请求自身超出模型上下文限制"。

        返回 None 表示窗口无效（不夹取，仍按比例走）。
        """
        if self._context_window <= 0:
            return None
        budget = self._context_window - max(0, self._max_output_tokens) \
            - max(0, self._headroom_tokens)
        return max(1, budget)

    def threshold_tokens(self) -> Optional[int]:
        """实际生效的压缩触发阈值（token）：min(窗口×比例, 安全上限)。"""
        if self._context_window <= 0:
            return None
        by_ratio = self._context_window * self._compress_ratio / 100.0
        cap = self.safe_threshold_tokens()
        if cap is None:
            return int(by_ratio)
        return int(min(by_ratio, cap))

    def config_warnings(self) -> list:
        """启动时的配置体检：把"这套配置数学上不可能成立"提前说出来。

        返回告警文案列表（空=健康）。不抛异常——配置有问题不该让程序起不来，
        但必须在启动时就明确告知，而不是让用户运行中反复撞"压缩失败"。
        """
        warns = []
        if self._context_window <= 0:
            return warns

        reserved = max(0, self._max_output_tokens)
        headroom = max(0, self._headroom_tokens)
        budget = self._context_window - reserved - headroom
        if budget <= 0:
            warns.append(
                "上下文窗口 %d 无法容纳 输出预留 %d + 安全余量 %d（余量已耗尽）。"
                "请调大窗口、或调小最大输出token数/压缩安全余量。"
                % (self._context_window, reserved, headroom))
        elif budget < self._context_window * self._compress_ratio / 100.0:
            warns.append(
                "压缩阈值 %.0f%% 高于安全上限 %d token（窗口 %d - 输出预留 %d - 余量 %d），"
                "实际将按 %d token 触发压缩。"
                % (self._compress_ratio, budget, self._context_window,
                   reserved, headroom, budget))
        return warns

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
        """是否需要压缩：占比 ≥ 压缩阈值，且已用 token 触及安全上限。

        第二个条件是新加的：占比是基于"上次请求的真实 token"算的，
        而压缩阈值可能被夹到安全上限以下。两者取更严的那个。
        """
        if self._ratio is None:
            return False
        if self._ratio >= self._compress_ratio:
            return True
        threshold = self.threshold_tokens()
        if threshold is None:
            return False
        used = self._ratio / 100.0 * self._context_window
        return used >= threshold

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
        """压缩失败后标记"下次输入前重试"：占比保持 ≥ 触发阈值。

        此前置为"阈值-10"会让下一次输入不再触发压缩（need_compress=False），
        重试被推迟到占比重新涨过阈值之后（用户感知为"下下次"才重试），与
        "压缩失败不阻塞对话（下次输入前自动重试）"的承诺不符。
        """
        self._ratio = float(self._compress_ratio)
