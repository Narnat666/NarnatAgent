"""
日志模块 —— 统一写入接口，按日期时间滚动文件
API key等敏感数据脱敏
"""

import logging
import os
import re
import sys
import time
from typing import Optional


class AgentLogger:
    """
    Agent统一日志器。

    - 每次start()创建新日志文件：logs/YYYY-MM-DD_HH-MM-SS.log
    - 四级：DEBUG / INFO / WARNING / ERROR
    - 自动脱敏
    """

    # 敏感信息脱敏：匹配 sk-xxx / as_sk_xxx / api_key=xxx / key=xxx / password=xxx 等
    # （password/passwd/pwd/secret/token 键名形态统一按"键名+分隔符+值"脱敏；_replace 分组编号不变）
    # 密钥主体字符类含连字符：现代格式 sk-ant-api03-... / sk-or-v1-... /
    # sk-proj-... 的中间段含 '-'，不含则整串匹配失败、密钥原样落日志
    # 键名表含 x-api-key（MCP 远程头）/bearer（Authorization: Bearer <token>
    # 形态从 "Bearer" 起匹配到 token 值）/裸 key（含 URL ?key= 形态）
    RE_SECRET = re.compile(
        r'((?:api_key|apikey|x-api-key|api-key|bearer|key|password|passwd|pwd|secret|token)["\s:=]+["\s]*)'
        r'([^\s",\}]{4,})([^\s",\}]*?)'
        r'|((?:as_sk_|sk[-_]|key-|token-)([a-zA-Z0-9-]{4})[a-zA-Z0-9-]*)',
        re.IGNORECASE,
    )

    @staticmethod
    def _redact(text: str) -> str:
        """脱敏：保留前4位，其余用***替代"""
        def _replace(m):
            full = m.group(0)
            # as_sk_/sk-/key-/token- 前缀形态：保留前缀+值前4位（如 "sk-13e4***"）
            if m.group(5):
                head = m.group(4)
                keep = 4
                for p in ("as_sk_", "token-", "key-", "sk-", "sk_"):
                    if head.lower().startswith(p):
                        keep = len(p) + 4
                        break
                return head[:keep] + "***"
            # api_key=xxx 格式
            if m.group(2):
                return m.group(1) + m.group(2)[:4] + "***"
            return full
        return AgentLogger.RE_SECRET.sub(_replace, text)

    def __init__(self, logs_dir: str = ""):
        self._logger: Optional[logging.Logger] = None
        self._handler: Optional[logging.FileHandler] = None
        self._logs_dir = logs_dir

    def start(self, logs_dir: Optional[str] = None) -> str:
        """
        初始化日志，创建新文件。返回日志文件路径。
        可重复调用（先关闭旧handler再创建新的）。
        """
        if logs_dir:
            self._logs_dir = logs_dir

        try:
            os.makedirs(self._logs_dir, exist_ok=True)
        except OSError:
            pass   # 目录不可用：留给下方占位/FileHandler 报错并降级

        # 用 O_EXCL 原子占位探测占用：检查-创建两步存在竞态，同刻启动的多实例
        # 会都通过 os.path.exists 判断而撞名（混写同一文件）
        base = time.strftime("%Y-%m-%d_%H-%M-%S")
        filepath = os.path.join(self._logs_dir, base + ".log")
        try:
            fd = os.open(filepath, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                         | getattr(os, "O_BINARY", 0))
            os.close(fd)   # 原子占位成功（空文件留给 FileHandler 追加写）
        except FileExistsError:
            # 同刻启动的多实例：文件名追加 pid 区分
            filepath = os.path.join(self._logs_dir, f"{base}_{os.getpid()}.log")
        except OSError as e:
            # 目录不可写（权限/磁盘满）等：降级为无文件日志
            return self._disable(e)

        # 关闭旧handler
        if self._handler:
            self._handler.close()
            if self._logger:
                self._logger.removeHandler(self._handler)

        logger = logging.getLogger(f"narnat_{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.propagate = False

        try:
            handler = logging.FileHandler(filepath, encoding="utf-8")
        except OSError as e:
            return self._disable(e)
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

    def _disable(self, err: OSError) -> str:
        """日志基础设施不可用（目录无权限/磁盘满/路径被占位）：降级为无文件日志。

        日志仅作辅助，不应让主程序不可用；降级后 _log 自动静默（_logger=None）。
        """
        if self._handler:
            try:
                self._handler.close()
            except Exception:
                pass
        self._logger = None
        self._handler = None
        try:
            print(f"  ⚠ 日志目录不可用，调试日志已禁用: {err}", file=sys.stderr)
        except Exception:
            pass
        return ""

    def _log(self, level: int, module: str, msg: str):
        if not self._logger:
            return
        safe_msg = AgentLogger._redact(msg)
        self._logger.log(level, f"[{module}] {safe_msg}")

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
