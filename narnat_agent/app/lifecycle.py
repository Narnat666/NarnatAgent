"""应用生命周期 —— 日志器与进程级资源清理（两条退出路径的唯一实现点）。

契约来源：`openspec/changes/recast-v2/specs/app/spec.md`「退出清理」
（①退出命令路径自行完成会话落盘、退出日志、MCP 清理与日志关闭；②主循环结束或
异常传播路径关闭共享线程池、清理终端/串口/后台/MCP，headless 另须关闭日志）
与「装配次序与依赖注入」的日志器步骤（仅调试模式启动文件日志，未启动时日志
调用静默丢弃）。

- `AgentLogger`：按日期时间命名的文件日志器（四级、写入前密钥脱敏）；
  实例承载状态（无模块级可变状态），可注入给全部消费方（`LogSink`/`LogPort`
  等鸭子协议）。
- `cleanup_resources`：常规清理（主循环结束 / 异常传播路径）。
- `cleanup_exit_command`：退出命令路径清理（进程随后立即终止，不经常规清理）。

依赖规则（design D1）：本积木位于 L4，只依赖标准库与低层积木的类型（运行时
经装配产物 duck-typed 访问，不 import 其它 app 模块）。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

__all__ = ["AgentLogger", "cleanup_exit_command", "cleanup_resources"]


class AgentLogger:
    """Agent 统一日志器（按日期时间滚动文件）。

    - `start()` 每次创建新日志文件 `logs/YYYY-MM-DD_HH-MM-SS.log`（可重复调用）；
    - 四级：DEBUG / INFO / WARNING / ERROR；
    - 写入前对 API key 类敏感串脱敏（保留前 4 位）；
    - 未 `start()` 时全部日志调用静默丢弃（非调试模式语义）。
    """

    # 敏感信息脱敏：匹配 sk-xxx / api_key=xxx / key=xxx 等
    RE_SECRET = re.compile(
        r'(api_key["\s:=]+["\s]*)([^\s",\}]{4,})([^\s",\}]*?)'
        r'|((?:sk-|key-|token-)([a-zA-Z0-9]{4})[a-zA-Z0-9]*)',
        re.IGNORECASE,
    )

    @staticmethod
    def _redact(text: str) -> str:
        """脱敏：保留前 4 位，其余用 *** 替代。"""

        def _replace(match: "re.Match[str]") -> str:
            full = match.group(0)
            # sk-xxxx... 格式
            if match.group(5):
                prefix = match.group(4)[:len(match.group(4)) - len(match.group(5)) + 4]
                return prefix + "***"
            # api_key=xxx 格式
            if match.group(2):
                return match.group(1) + match.group(2)[:4] + "***"
            return full

        return AgentLogger.RE_SECRET.sub(_replace, text)

    def __init__(self, logs_dir: str = ""):
        """构造日志器（尚未启动文件日志）。

        `logs_dir`：日志目录（调试模式 `start()` 时按需创建）。
        """
        self._logger: Optional[logging.Logger] = None
        self._handler: Optional[logging.FileHandler] = None
        self._logs_dir = logs_dir

    def start(self, logs_dir: Optional[str] = None) -> str:
        """启动文件日志（创建新文件），返回日志文件路径；可重复调用。"""
        if logs_dir:
            self._logs_dir = logs_dir

        os.makedirs(self._logs_dir, exist_ok=True)

        filename = time.strftime("%Y-%m-%d_%H-%M-%S") + ".log"
        filepath = os.path.join(self._logs_dir, filename)

        # 关闭旧 handler
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

    def _log(self, level: int, module: str, msg: str) -> None:
        """写一条日志（未启动时静默丢弃；写入前脱敏）。"""
        if not self._logger:
            return
        safe_msg = AgentLogger._redact(msg)
        self._logger.log(level, f"[{module}] {safe_msg}")

    def debug(self, module: str, msg: str) -> None:
        """记录 DEBUG 日志。"""
        self._log(logging.DEBUG, module, msg)

    def info(self, module: str, msg: str) -> None:
        """记录 INFO 日志。"""
        self._log(logging.INFO, module, msg)

    def warning(self, module: str, msg: str) -> None:
        """记录 WARNING 日志。"""
        self._log(logging.WARNING, module, msg)

    def error(self, module: str, msg: str) -> None:
        """记录 ERROR 日志。"""
        self._log(logging.ERROR, module, msg)

    def close(self) -> None:
        """关闭文件日志（幂等；关闭后日志调用静默丢弃）。"""
        if self._handler:
            self._handler.close()
            if self._logger:
                self._logger.removeHandler(self._handler)
            self._handler = None


def cleanup_resources(parts, *, close_log: bool = False) -> None:
    """常规退出清理（主循环结束或异常传播路径）。

    固定次序：关闭共享线程池（不等待在途任务）→ 终端会话 → 串口会话 → 后台任务
    → MCP 子进程；`close_log=True`（headless 路径）时最后关闭文件日志。
    """
    parts.thread_pool.shutdown(wait=False)
    parts.terminal.cleanup()
    parts.serial.cleanup()
    parts.background.cleanup_all()
    parts.mcp_manager.cleanup()
    if close_log:
        parts.logger.close()


def cleanup_exit_command(parts) -> None:
    """退出命令路径清理（进程随后立即终止，绕过常规清理流程）。

    自行完成：会话落盘与延迟删除执行 → 退出日志 → MCP 清理 → 日志关闭；
    会话已有名称时输出「会话已自动保存: {名称}」（形态逐字对齐旧实现，
    名称取收尾等待与落盘之后的当前会话名）。
    """
    parts.session_mgr.exit_cleanup()
    name = parts.session_mgr.session_name()
    if name:
        theme = parts.theme
        parts.console.write(f"  {theme.d}会话已自动保存: {theme.e}{name}{theme.r}\n")
    parts.logger.info("app", "用户退出")
    # os._exit 不走 finally：MCP 服务端子进程在此显式回收
    parts.mcp_manager.cleanup()
    parts.logger.close()
