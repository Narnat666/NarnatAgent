"""
主循环 —— 读输入→调度AI→输出→循环
"""

import json
import os
from typing import List, Dict, Any, Optional

from .llm import LLMClient
from .context import ContextManager
from .compressor import Compressor
from ..config.loader import AppConfig, load_config
from ..config.session_store import save_session, load_session, list_sessions, delete_session, format_session_list
from ..tools.registry import execute as tool_execute
from ..tools import write as write_tool
from ..tools import bash as bash_tool
from ..tools import todo_write as todo_tool
from ..ui.ui_design import UIInterface, SessionCallbacks, _interrupt_ctrl
from ..logger import AgentLogger


class NarnatSessionCallbacks(SessionCallbacks):
    """会话命令回调实现"""

    def __init__(self, narnat_dir: str, get_messages_func, set_messages_func):
        self._narnat_dir = narnat_dir
        self._get_messages = get_messages_func
        self._set_messages = set_messages_func

    def on_save(self, name: str) -> str:
        return save_session(self._narnat_dir, name, self._get_messages())

    def on_show(self) -> str:
        sessions = list_sessions(self._narnat_dir)
        return format_session_list(sessions)

    def on_enter(self, name: str) -> str:
        messages, err = load_session(self._narnat_dir, name)
        if err:
            return err
        self._set_messages(messages)
        return ""

    def on_delete(self, name: str) -> str:
        return delete_session(self._narnat_dir, name)


class Agent:
    """Narnat Agent 主控"""

    def __init__(self, project_root: Optional[str] = None, debug: bool = False):
        # 加载配置
        self._config = load_config(project_root)
        # 初始化日志（仅debug模式启用）
        self._logger = AgentLogger(self._config.project_root)
        if debug:
            self._logger.start(self._config.project_root)
        # 初始化LLM
        self._llm = LLMClient(self._config.ai, self._logger)
        # 初始化上下文管理
        self._context = ContextManager(self._logger)
        # 初始化压缩器
        self._compressor = Compressor(self._config.narnat_dir, self._logger)
        # 初始化messages
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._config.system_prompt}
        ]
        # 初始化UI
        callbacks = NarnatSessionCallbacks(
            self._config.narnat_dir,
            lambda: self._messages,
            lambda msgs: setattr(self, '_messages', msgs),
        )
        self._ui = UIInterface(self._config.ai.model, callbacks)
        # 注入工具回调
        bash_tool.set_confirm_callback(self._confirm_delete)
        todo_tool.set_ui_callback(self._on_todo_update)
        # 统计
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _confirm_delete(self, command: str) -> bool:
        """删除命令确认回调"""
        try:
            response = input(f"  确认执行删除命令? [y/N]: ")
            return response.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _on_todo_update(self, todos):
        """TodoWrite UI更新回调 — 展示工作计划和进度"""
        from ..ui.ui_design import C, E, Y, G, B, R, D

        # 找到当前 in_progress 的任务
        in_progress_item = None
        for t in todos:
            if t["status"] == "in_progress":
                in_progress_item = t
                break

        # 打印任务列表
        for t in todos:
            status = t["status"]
            content = t.get("content", "")
            active_form = t.get("activeForm", content)

            if status == "completed":
                icon = f"{E}✓{R}"
                line = f"  {icon} {D}{content}{R}"
            elif status == "in_progress":
                icon = f"{Y}●{R}"
                line = f"  {icon} {B}{active_form}{R}"
            else:  # pending
                icon = f"{G}○{R}"
                line = f"  {icon} {D}{content}{R}"

            print(line)

    def _show_tool_call(self, name: str, arguments: dict):
        """在终端显示工具调用摘要，让用户看到AI正在做什么"""
        from ..ui.ui_design import C, D, R

        # 工具名→简短描述映射
        _TOOL_LABELS = {
            "Read": "读取",
            "Glob": "搜索文件",
            "Grep": "搜索内容",
            "Edit": "编辑",
            "Write": "写入",
            "Bash": "执行命令",
            "WebSearch": "联网搜索",
            "TodoWrite": "更新计划",
        }

        label = _TOOL_LABELS.get(name, name)

        # 提取关键参数用于摘要
        summary = ""
        if name == "Read":
            summary = arguments.get("file_path", "")
        elif name == "Edit":
            summary = arguments.get("file_path", "")
        elif name == "Write":
            summary = arguments.get("file_path", "")
        elif name == "Bash":
            summary = arguments.get("command", "")[:60]
        elif name == "Grep":
            summary = arguments.get("pattern", "")
        elif name == "Glob":
            summary = arguments.get("pattern", "")
        elif name == "WebSearch":
            summary = arguments.get("query", "")

        if summary:
            # 截断过长的摘要
            if len(summary) > 80:
                summary = summary[:77] + "..."
            print(f"  {D}[{label}] {summary}{R}")
        else:
            print(f"  {D}[{label}]{R}")

    def run(self):
        """主循环"""
        self._ui.start()
        self._logger.info("core.agent", f"Agent启动, model={self._config.ai.model}")

        while True:
            # 1. 读取用户输入
            user_input = self._ui.read_input()
            if user_input is None:
                continue

            stripped = user_input.strip()
            if not stripped:
                continue

            # /exit 退出
            if stripped == "/exit":
                self._logger.info("core.agent", "用户退出")
                self._logger.close()
                return

            # 命令分发
            if stripped.startswith("/"):
                parts = stripped.split(None, 1)
                cmd = parts[0]
                args = parts[1] if len(parts) > 1 else ""
                if self._ui.dispatch_command(cmd, args):
                    continue

            # 2. 轮次计数
            warn = self._context.increment()
            if warn:
                print(f"  ⚠ {warn}")

            # 3. 压缩检查
            if self._context.need_compress():
                self._handle_compress(stripped)
                continue

            # 4. 追加用户消息
            self._messages.append({"role": "user", "content": stripped})
            self._logger.info("core.agent", f"用户输入: {stripped[:100]}")

            # 5. 创建流式输出
            stream = self._ui.create_stream()

            try:
                # 6. 工具调度内循环
                self._agent_loop(stream)
            except KeyboardInterrupt:
                self._ui.on_interrupted()
                stream.abort()
            except Exception as e:
                self._logger.error("core.agent", f"异常: {e}")
                stream.abort()

    def _agent_loop(self, stream):
        """工具调度内循环"""
        while True:
            # a. 调用LLM
            content_parts = []
            tool_calls_result = []

            for chunk in self._llm.chat_stream(self._messages):
                # b. 检查中断
                if stream.cancelled:
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # c. 处理tool_call
                if "tool_calls" in chunk:
                    tool_calls_result = chunk["tool_calls"]

                # d. 处理纯文本
                if "content" in chunk and "tool_calls" not in chunk:
                    stream.feed(chunk["content"])
                    content_parts.append(chunk["content"])

                # e. 处理结束
                if "finish_reason" in chunk:
                    reason = chunk["finish_reason"]
                    if reason == "error":
                        stream.finish(
                            self._total_input_tokens,
                            self._total_output_tokens,
                        )
                        return

            # 有tool_call → 执行工具 → 继续内循环
            if tool_calls_result:
                # 追加assistant消息（含tool_calls）
                self._messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": tool_calls_result,
                })

                # 逐个执行tool_call
                for tc in tool_calls_result:
                    # 工具执行前也检查中断
                    if stream.cancelled:
                        stream.abort()
                        self._ui.on_interrupted()
                        return

                    func = tc["function"]
                    name = func["name"]
                    try:
                        arguments = json.loads(func["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}

                    # Read后标记（供Write检查）
                    if name == "Read":
                        file_path = arguments.get("file_path", "")
                        if file_path:
                            write_tool.mark_read(file_path)

                    # UI: 暂停spinner，显示工具调用摘要，避免闪烁
                    stream.pause_spinner()
                    self._show_tool_call(name, arguments)

                    # 执行工具
                    result = tool_execute(name, arguments)
                    self._logger.info(f"tools.{name.lower()}", f"调用: {json.dumps(arguments, ensure_ascii=False)[:200]}")
                    self._logger.info(f"tools.{name.lower()}", f"结果: {result[:200] if result else '(空)'}")

                    # UI: 恢复spinner（AI可能继续思考）
                    stream.resume_spinner()

                    # 回传工具结果
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                # 更新token统计（tool_call轮次也需统计）
                self._total_output_tokens += sum(len(p) for p in content_parts)
                self._total_input_tokens = self._llm.count_tokens(self._messages)

                # 继续内循环
                continue

            # 无tool_call → 纯文本输出完成，退出内循环
            # 追加assistant消息
            if content_parts:
                self._messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts),
                })

            # 更新token统计（仅统计本轮新增的输出token）
            self._total_output_tokens += sum(len(p) for p in content_parts)
            self._total_input_tokens = self._llm.count_tokens(self._messages)

            stream.finish(
                self._total_input_tokens,
                self._total_output_tokens,
            )
            break

    def _handle_compress(self, pending_input: str):
        """
        处理上下文压缩。

        9步流程实现。
        """
        self._logger.info("compressor", f"压缩触发, 轮次={self._context.turn_count}")

        # 1. 暂存用户输入（pending_input已传入）
        # 2. 启动压缩动画
        self._ui.begin_compressing()

        # 3. 构建压缩请求
        compress_messages = self._compressor.build_compress_messages(self._messages)

        # 4. 发送压缩请求，收集AI输出
        summary_content = []
        for chunk in self._llm.chat_stream(compress_messages):
            # 压缩过程中也检查中断
            if _interrupt_ctrl.is_set:
                self._ui.end_compressing()
                self._context.reset()
                self._messages.append({"role": "user", "content": pending_input})
                return
            if "content" in chunk and "tool_calls" not in chunk:
                summary_content.append(chunk["content"])

        summary = "".join(summary_content)

        # 5. 写入磁盘
        if not self._compressor.write_summary(summary):
            self._ui.end_compressing()
            self._logger.error("compressor", "压缩失败: 写入失败")
            # 压缩失败：重置轮次计数防止无限重试，恢复用户输入
            self._context.reset()
            self._messages.append({"role": "user", "content": pending_input})
            return

        # 6. 校验总结结果
        if not self._compressor.verify_summary():
            self._ui.end_compressing()
            self._logger.error("compressor", "压缩失败: 总结为空")
            # 压缩失败：重置轮次计数防止无限重试，恢复用户输入
            self._context.reset()
            self._messages.append({"role": "user", "content": pending_input})
            return

        # 7. 销毁旧会话
        self._messages.clear()
        self._context.reset()
        write_tool.clear_read_files()

        # 8. 创建新会话（注入总结）
        summary_text = self._compressor.read_summary()
        self._messages = self._compressor.build_new_session_messages(
            self._config.system_prompt, summary_text
        )

        # 9. 重置总结文件
        self._compressor.reset_summary()

        # 10. 停止压缩动画
        self._ui.end_compressing()

        self._logger.info("compressor", "压缩成功，新会话已创建")

        # 11. 恢复用户问题（追加到messages，下一轮主循环处理）
        self._messages.append({"role": "user", "content": pending_input})
