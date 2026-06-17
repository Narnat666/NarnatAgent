"""
压缩执行 —— 发送压缩prompt→校验md→切换会话
"""

import os
from typing import List, Dict, Any, Optional

from ..config.defaults import COMPRESS_PROMPT, DATA_SUBDIR, LAST_SESSION_SUMMARY


class Compressor:
    """
    上下文压缩器。

    9步流程：
    1. 拦截用户输入（由agent.py处理）
    2. 发送压缩prompt
    3. 写入磁盘
    4. 校验总结结果
    5. 销毁旧会话
    6. 创建新会话
    7. 重置标记
    8. 恢复用户问题（由agent.py处理）
    9. 停止压缩动画（由agent.py处理）
    """

    def __init__(self, narnat_dir: str, logger=None):
        self._narnat_dir = narnat_dir
        self._logger = logger
        self._summary_path = os.path.join(narnat_dir, DATA_SUBDIR, LAST_SESSION_SUMMARY)

    def build_compress_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        构建压缩请求的messages。

        复用当前对话的全部messages，末尾追加压缩指令。
        """
        compress_messages = list(messages)  # 浅拷贝
        compress_messages.append({
            "role": "user",
            "content": COMPRESS_PROMPT,
        })
        return compress_messages

    def write_summary(self, content: str) -> bool:
        """
        将AI总结写入磁盘。

        Returns:
            True=写入成功
        """
        try:
            with open(self._summary_path, "w", encoding="utf-8") as f:
                f.write(content)
            if self._logger:
                self._logger.info("compressor", f"总结写入: {self._summary_path}")
            return True
        except OSError as e:
            if self._logger:
                self._logger.error("compressor", f"写入失败: {e}")
            return False

    def verify_summary(self) -> bool:
        """
        校验总结结果。

        读取md文件，非空=压缩成功，空=压缩失败。
        """
        try:
            with open(self._summary_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return len(content) > 0
        except OSError:
            return False

    def read_summary(self) -> str:
        """读取总结内容"""
        try:
            with open(self._summary_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def reset_summary(self):
        """重置总结文件为空（继承完毕后调用）"""
        try:
            with open(self._summary_path, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass

    def build_new_session_messages(
        self, system_prompt: str, summary: str
    ) -> List[Dict[str, Any]]:
        """
        创建新会话的messages。

        system_prompt末尾追加总结作为"上一轮对话成果"。
        """
        new_system = system_prompt
        if summary:
            new_system += f"\n\n# 上一轮对话成果\n\n{summary}"
        return [{"role": "system", "content": new_system}]
