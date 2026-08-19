"""
主循环 —— 读输入→调度AI→输出→循环

Agent 类作为纯编排者，只做发令和委托：
- 命令分发 → UIInterface / SessionManager
- 对话轮次 → AgentLoop
- 压缩检查 → CompressionCoordinator
- 自动保存 → AutoSaveManager

所有子模块的构造在 Assembly 中完成，Agent 不关心构造细节。
"""

import os
from typing import Optional

from ..assembly import Assembly, AssemblyResult
from ..output import write as _stdout_write, X, R


class Agent:
    """Narnat Agent 主控 — 纯编排者"""

    def __init__(self, project_root: Optional[str] = None, debug: bool = False):
        self._parts: AssemblyResult = Assembly.build(project_root, debug)
        self._config = self._parts.config
        self._logger = self._parts.logger
        self._ui = self._parts.ui
        self._context = self._parts.context
        self._mgr = self._parts.session_mgr
        self._msg_manager = self._parts.msg_manager
        self._stats = self._parts.stats
        self._agent_loop = self._parts.agent_loop
        self._auto_save = self._parts.auto_save_mgr
        self._compression = self._parts.compression_coordinator
        self._round = 0

    def run(self):
        """主循环"""
        self._ui.start()
        self._logger.info("core.agent", f"Agent启动, model={self._config.ai.model}")

        try:
            while True:
                # 1. 读取用户输入
                user_input = self._ui.read_input()
                if user_input is None:
                    continue

                # 同步点：等后台自动保存完成
                self._auto_save.wait()

                stripped = user_input.strip()
                if not stripped:
                    continue

                # 命令分发
                if stripped.startswith("/"):
                    parts = stripped.split(None, 1)
                    cmd = parts[0]
                    args = parts[1] if len(parts) > 1 else ""
                    result = self._ui.dispatch_command(cmd, args)
                    if result == 2:
                        self._auto_save.on_exit()
                        self._logger.info("core.agent", "用户退出")
                        self._logger.close()
                        os._exit(0)
                    if result == 1:
                        continue

                # 2. 轮次计数（余额查询周期用）+ 余额查询
                self._round += 1
                api_key = getattr(self._config.ai, 'api_key', None)
                self._stats.fetch_balance(api_key, self._round)

                # 3. 压缩检查
                compress_ok = False
                if self._context.need_compress():
                    compress_ok = self._compression.compress(stripped)
                    if not compress_ok:
                        continue

                # 4. 追加用户消息
                if not compress_ok:
                    self._msg_manager.repair()
                    self._msg_manager.append_user(stripped)
                    self._logger.info("core.agent", f"用户输入: {stripped[:100]}")

                # 5. 创建流式输出
                stream = self._ui.create_stream()

                try:
                    # 6. 工具调度内循环
                    self._agent_loop.run(stream)
                except KeyboardInterrupt:
                    self._ui.on_interrupted()
                    stream.abort()
                except Exception as e:
                    self._logger.error("core.agent", f"异常: {e}")
                    if hasattr(self._agent_loop, '_last_content_parts') and self._agent_loop._last_content_parts:
                        self._msg_manager.append_assistant("".join(self._agent_loop._last_content_parts))
                    # 异常≠用户打断：显示明确的错误提示，而非"已打断"
                    stream.abort(message=f"  {X}⚠ 程序异常，本轮回复已停止: {e}{R}")
                else:
                    if not stream.aborted:
                        self._mgr.on_auto_save()
                        self._auto_save.try_save()

                # 7. 回复结束：更新窗口占比 + 告警提示（中断/异常时统计沿用上一轮值）
                self._context.update_ratio(self._stats.input_tokens)
                warn = self._context.check_warn()
                if warn:
                    _stdout_write(f"  ⚠ {warn}\n")

        finally:
            self._parts.dispatcher._executor.shutdown(wait=False)
            from ..tools.terminal import cleanup as _terminal_cleanup
            from ..tools.serial import cleanup as _serial_cleanup
            _terminal_cleanup()
            _serial_cleanup()
