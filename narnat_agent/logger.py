"""
日志模块 —— 统一写入接口，按日期时间滚动文件
API key等敏感数据脱敏
"""

import logging
import os
import re
import time
from typing import Optional

from .config.defaults import LOG_DIR


# 敏感信息脱敏：匹配 sk-xxx / api_key=xxx / key=xxx 等
_RE_SECRET = re.compile(
    r'(api_key["\s:=]+["\s]*)([^\s",\}]{4,})([^\s",\}]*?)'
    r'|((?:sk-|key-|token-)([a-zA-Z0-9]{4})[a-zA-Z0-9]*)',
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """脱敏：保留前4位，其余用***替代"""
    def _replace(m):
        full = m.group(0)
        # sk-xxxx... 格式
        if m.group(5):
            prefix = m.group(4)[:len(m.group(4)) - len(m.group(5)) + 4]
            return prefix + "***"
        # api_key=xxx 格式
        if m.group(2):
            return m.group(1) + m.group(2)[:4] + "***"
        return full
    return _RE_SECRET.sub(_replace, text)


class AgentLogger:
    """
    Agent统一日志器。

    - 每次start()创建新日志文件：logs/YYYY-MM-DD_HH-MM-SS.log
    - 四级：DEBUG / INFO / WARNING / ERROR
    - 自动脱敏
    """

    def __init__(self, project_root: str = ""):
        self._logger: Optional[logging.Logger] = None
        self._handler: Optional[logging.FileHandler] = None
        self._project_root = project_root

    def start(self, project_root: Optional[str] = None) -> str:
        """
        初始化日志，创建新文件。返回日志文件路径。
        可重复调用（先关闭旧handler再创建新的）。
        """
        if project_root:
            self._project_root = project_root

        log_dir = os.path.join(self._project_root, LOG_DIR)
        os.makedirs(log_dir, exist_ok=True)

        filename = time.strftime("%Y-%m-%d_%H-%M-%S") + ".log"
        filepath = os.path.join(log_dir, filename)

        # 关闭旧handler
        if self._handler:
            self._handler.close()
            if self._logger:
                self._logger.removeHandler(self._handler)

        logger = logging.getLogger(f"narnat_{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.propagate = False

        handler = logging.FileHandler(filepath, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s [%(name)s]  %(levelname)-5s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)

        self._logger = logger
        self._handler = handler
        return filepath

    def _log(self, level: int, module: str, msg: str):
        if not self._logger:
            return
        safe_msg = _redact(msg)
        # 用子logger名模拟模块名
        child = logging.getLogger(f"narnat_{id(self)}.{module}")
        child.handlers.clear()
        child.propagate = True
        child.parent = self._logger
        child.log(level, safe_msg)

    def debug(self, module: str, msg: str):
        self._log(logging.DEBUG, module, msg)

    def info(self, module: str, msg: str):
        self._log(logging.INFO, module, msg)

    def warning(self, module: str, msg: str):
        self._log(logging.WARNING, module, msg)

    def error(self, module: str, msg: str):
        self._log(logging.ERROR, module, msg)

    def close(self):
        if self._handler:
            self._handler.close()
            if self._logger:
                self._logger.removeHandler(self._handler)
            self._handler = None
