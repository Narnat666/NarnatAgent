"""
上下文管理 —— 轮次计数、压缩触发、会话销毁/创建
"""

from typing import Dict, Any, Optional

from ..config.defaults import WARN_TURN_1, WARN_TURN_2, COMPRESS_TURN


class ContextManager:
    """
    上下文管理器。

    职责：
    - 轮次计数
    - 压缩触发判断
    - 警告提示
    """

    def __init__(self, logger=None, warn_turn_1: int = WARN_TURN_1,
                 warn_turn_2: int = WARN_TURN_2, compress_turn: int = COMPRESS_TURN):
        self._turn_count = 0
        self._logger = logger
        self._warn_turn_1 = warn_turn_1
        self._warn_turn_2 = warn_turn_2
        self._compress_turn = compress_turn
        self._warned_1 = False
        self._warned_2 = False

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def increment(self) -> str:
        """
        轮次+1，返回警告提示（空串表示无警告）。
        """
        self._turn_count += 1

        if self._turn_count == self._warn_turn_1 and not self._warned_1:
            self._warned_1 = True
            msg = f"对话已达{self._warn_turn_1}轮，注意上下文长度"
            if self._logger:
                self._logger.warning("core.context", msg)
            return msg

        if self._turn_count == self._warn_turn_2 and not self._warned_2:
            self._warned_2 = True
            msg = f"对话已达{self._warn_turn_2}轮，建议开启新对话"
            if self._logger:
                self._logger.warning("core.context", msg)
            return msg

        return ""

    def need_compress(self) -> bool:
        """是否需要压缩"""
        return self._turn_count >= self._compress_turn

    def reset(self):
        """重置（新会话开始时调用）"""
        self._turn_count = 0
        self._warned_1 = False
        self._warned_2 = False

    def sync_from_messages(self, messages):
        """
        根据messages同步轮次计数（恢复会话时调用）。

        increment()在每次用户输入时+1，因此user消息数等于轮次数。
        同时根据轮次设置警告标记，避免重复触发警告。
        """
        user_count = sum(1 for m in messages if m.get("role") == "user")
        self._turn_count = user_count
        self._warned_1 = user_count >= self._warn_turn_1
        self._warned_2 = user_count >= self._warn_turn_2

    def set_retry_soon(self):
        """压缩失败后设置近期重试（10轮后再次触发压缩）"""
        self._turn_count = max(0, self._compress_turn - 10)

    def get_summary(self) -> Dict[str, Any]:
        """获取上下文摘要"""
        return {
            "turn_count": self._turn_count,
            "warned_1": self._warned_1,
            "warned_2": self._warned_2,
        }
