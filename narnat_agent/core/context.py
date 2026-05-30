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

    def __init__(self, logger=None):
        self._turn_count = 0
        self._logger = logger
        self._warned_50 = False
        self._warned_100 = False

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def increment(self) -> str:
        """
        轮次+1，返回警告提示（空串表示无警告）。

        Returns:
            警告提示字符串，空串表示无警告
        """
        self._turn_count += 1

        if self._turn_count == WARN_TURN_1 and not self._warned_50:
            self._warned_50 = True
            msg = f"对话已达{WARN_TURN_1}轮，注意上下文长度"
            if self._logger:
                self._logger.warning("core.context", msg)
            return msg

        if self._turn_count == WARN_TURN_2 and not self._warned_100:
            self._warned_100 = True
            msg = f"对话已达{WARN_TURN_2}轮，建议开启新对话"
            if self._logger:
                self._logger.warning("core.context", msg)
            return msg

        return ""

    def need_compress(self) -> bool:
        """是否需要压缩"""
        return self._turn_count >= COMPRESS_TURN

    def reset(self):
        """重置（新会话开始时调用）"""
        self._turn_count = 0
        self._warned_50 = False
        self._warned_100 = False

    def get_summary(self) -> Dict[str, Any]:
        """获取上下文摘要"""
        return {
            "turn_count": self._turn_count,
            "warned_50": self._warned_50,
            "warned_100": self._warned_100,
        }
