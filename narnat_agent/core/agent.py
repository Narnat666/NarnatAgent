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
from ..config.defaults import GOAL_AUDIT_HINT
from ..output import write as _stdout_write, X, R


class Agent:
    """Narnat Agent 主控 — 纯编排者"""

    def __init__(self, project_root: Optional[str] = None, debug: bool = False,
                 headless: bool = False):
        self._parts: AssemblyResult = Assembly.build(project_root, debug, headless)
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
        self._debug = debug

    def run(self):
        """主循环"""
        # 会话开始预清：清空上次异常退出的后台残留（正常退出由 finally 硬清理）
        from ..tools.background import prepare as _bg_prepare
        _bg_prepare()
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
                        # os._exit 不走 finally：MCP 服务端子进程在此显式回收
                        self._parts.mcp_manager.cleanup()
                        self._logger.close()
                        os._exit(0)
                    if result == 1:
                        continue

                # 2. 轮次计数（余额查询周期用）+ 余额查询
                self._round += 1
                api_key = getattr(self._config.ai, 'api_key', None)
                self._stats.fetch_balance(api_key, self._round)

                # 3. 压缩检查
                # 任务级状态复位：防止上一任务的完成标记/清单/预算/计划残留泄漏到新任务
                self._reset_goal_state()
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

                # 5. 目标模式：解析轮数上限（临时覆盖优先，否则用配置默认值）
                goal_enabled = self._mgr._goal_enabled
                if goal_enabled:
                    goal_limit = self._mgr._goal_max_rounds or self._config.ai.goal_max_rounds
                    goal_task = stripped
                    goal_round = 0
                else:
                    goal_limit = 0
                    goal_task = ""
                    goal_round = 0

                while True:
                    # 5. 创建流式输出
                    stream = self._ui.create_stream()

                    try:
                        # 6. 工具调度内循环（目标模式传入goal_mode：中间轮不显示统计栏；
                        #    剩余预算供验证打回前判断是否已耗尽）
                        self._agent_loop.run(stream, goal_mode=goal_enabled, goal_task=goal_task,
                                             round_budget_left=(goal_limit - goal_round)
                                             if goal_enabled else -1)
                    except KeyboardInterrupt:
                        self._ui.on_interrupted()
                        # 与 ESC（agent_loop 内）及异常路径一致：已产出的半截
                        # 输出落为 assistant 消息——否则本轮内容丢失、历史以
                        # 未回复的 user 结尾（下次输入形成连续 user）；
                        # 此前 Ctrl+C 与 ESC 两种打断的上下文行为不一致
                        if hasattr(self._agent_loop, '_last_content_parts') and self._agent_loop._last_content_parts:
                            self._msg_manager.append_assistant("".join(self._agent_loop._last_content_parts))
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

                    # ── 统一预算结算：本轮续跑消耗1 + 本轮验证打回次数k ──
                    goal_round += 1 + self._agent_loop._last_round_blocks
                    self._parts.tool_context.goal_rounds_used = goal_round

                    # ── 目标模式自动续跑判断 ──
                    if not goal_enabled:
                        break
                    if stream.aborted:
                        break  # 用户中断/程序异常：保持现状退出
                    if not self._agent_loop._last_round_ok:
                        break  # 出错/空回复等非正常结束：保持现状退出
                    if self._parts.tool_context.goal_complete:
                        # AI已调用GoalComplete声明完成：复位标记，结束续跑
                        self._parts.tool_context.goal_complete = False
                        break
                    if goal_round >= goal_limit:
                        # 达到轮数上限：注入收尾指令，让AI总结后结束（不再续跑）
                        self._logger.info("core.agent", f"目标模式达到轮数上限({goal_limit})，停止续跑")
                        self._msg_manager.append_user(
                            f"[系统提示]目标模式已达到轮数上限（{goal_limit}轮），任务尚未完成。"
                            "请向用户总结当前进度、已完成工作和未完成原因，无需继续执行新任务。"
                        )
                        stream = self._ui.create_stream()
                        # 强制收尾轮：无论AI是否声明完成，正常结束时都显示统计栏
                        self._agent_loop.run(stream, goal_mode=goal_enabled, force_final=True,
                                             goal_task=goal_task,
                                             round_budget_left=goal_limit - goal_round)
                        if not stream.aborted:
                            self._mgr.on_auto_save()
                            self._auto_save.try_save()
                        break
                    # 任务未完成：注入继续消息（含完成审计提示），再跑一轮
                    self._logger.info("core.agent", f"目标模式自动续跑: 第{goal_round}轮完成，继续")
                    self._msg_manager.append_user(
                        f"[自动续跑]已完成{goal_round}轮，任务：{goal_task}\n"
                        "请继续推进任务。\n"
                        + GOAL_AUDIT_HINT
                    )

                # 7. 回复结束：更新窗口占比 + 告警提示（中断/异常时统计沿用上一轮值）
                self._context.update_ratio(self._stats.input_tokens)
                warn = self._context.check_warn()
                if warn:
                    _stdout_write(f"  ⚠ {warn}\n")

        finally:
            # 线程池不做任务级 shutdown：Agent 支持同进程复用（批处理/多次运行），
            # 池一旦停用，下次运行的工具提交会抛 cannot schedule new futures；
            # 进程退出时由解释器统一回收线程池（threading._register_atexit）。
            from ..tools.terminal import cleanup as _terminal_cleanup
            from ..tools.serial import cleanup as _serial_cleanup
            from ..tools.background import cleanup_all as _bg_cleanup
            _terminal_cleanup()
            _serial_cleanup()
            _bg_cleanup()
            # MCP 连接不做任务级 cleanup：cleanup 会把 McpManager 置为终态（_closed），
            # 复用后 connect/call_tool 抛"程序正在退出"；/exit 路径显式回收，
            # 异常退出由 atexit 兜底（连接成功时已注册）。

    def _reset_goal_state(self):
        """复位任务级状态：完成标记/清单/预算计数/提醒标志/计划。

        普通模式下 AI 误调 GoalComplete 的残留、子代理同进程复用时的上一任务
        残留，都会让新任务被误判（跳过验证/误拒清单），故每个任务起手必须复位。
        实际复位字段集由 ToolContext 的 task_scoped 标记决定（reset_for_task），
        新增任务级字段只需在字段定义处标记，不再维护本处的手工清单。
        """
        self._parts.tool_context.reset_for_task()

    def run_headless(self, task: str, max_rounds: int = 0):
        """headless 一次性任务执行（nn -p 入口）。

        带 -g N（N≥1）→ 目标模式：注入 GoalComplete、自动续跑 + 完成验证，
        预算 N 轮；不带 -g → 普通模式：单轮执行（AI 一口气干到停手），
        不注入 GoalComplete、无验证，跑完即退。
        与 run() 的区别：不读用户输入、不自动保存会话、不查余额、不显示统计栏。
        输出经 HeadlessStream（纯文本，无颜色）。

        max_rounds: 续跑预算轮数（nn -g 参数）；>0 才开启目标模式。
        """
        self._logger.info("core.agent", f"Agent启动(headless), model={self._config.ai.model}")

        # 会话开始预清：清空上次异常退出的后台残留（与 run() 一致）
        from ..tools.background import prepare as _bg_prepare
        _bg_prepare()

        # 哨兵变量在 try 外初始化：finally 无条件引用，任何异常路径都必须能打印 [NN_DONE]
        goal_round = 0
        end_reason = "unknown"

        goal_enabled = max_rounds > 0

        try:
            # 任务级状态复位：同进程复用（批处理/测试脚手架）时上一任务残留会跳过验证
            self._reset_goal_state()
            # 消息列表复位（+上下文占比复位）：headless 每次调用是独立任务，
            # 复用同一 Agent 对象时上一任务的完整对话（含工具结果/GoalComplete
            # 交互）不得带入本任务——此前仅 goal/todo 复位，消息未复位，第 2 个
            # 任务起请求携带全部历史并持续增长（跨任务污染与成本膨胀）
            self._mgr.replace_messages(
                [{"role": "system", "content": self._config.system_prompt}])
            # debug 日志重建：上一任务 finally 已 logger.close()（移除 handler），
            # 复用重跑时重新 start 创建新日志文件（否则第 2 个任务起零日志）
            if self._debug:
                self._logger.start(self._config.paths.logs_dir)
            if goal_enabled:
                # 开启目标模式：注入 GoalComplete 工具（预算 = -g N）
                self._mgr._goal_enabled = True
                self._mgr._goal_max_rounds = max_rounds
                if getattr(self._mgr, '_set_goal_tool', None):
                    self._mgr._set_goal_tool(True)
            goal_limit = max_rounds if goal_enabled else 0
            goal_task = task.strip()

            # 注入任务
            self._msg_manager.repair()
            self._msg_manager.append_user(goal_task)
            self._logger.info("core.agent", f"任务注入: {goal_task[:100]}")

            while True:
                # 压缩检查（与 run() 对齐：上下文超限时先压缩再继续）
                if self._context.need_compress():
                    if not self._compression.compress(goal_task):
                        end_reason = "compress_failed"
                        break

                stream = self._ui.create_stream()
                try:
                    self._agent_loop.run(
                        stream, goal_mode=goal_enabled, goal_task=goal_task,
                        round_budget_left=(goal_limit - goal_round) if goal_enabled else -1)
                except Exception as e:
                    self._logger.error("core.agent", f"异常: {e}")
                    stream.abort(message=f"⚠ 程序异常，本轮回复已停止: {e}")
                    end_reason = "aborted"
                    break

                # ── 统一预算结算：本轮续跑消耗1 + 本轮验证打回次数k ──
                goal_round += 1 + self._agent_loop._last_round_blocks
                self._parts.tool_context.goal_rounds_used = goal_round
                if stream.aborted:
                    end_reason = "aborted"
                    break  # 程序异常等非正常结束：保持现状退出
                if not self._agent_loop._last_round_ok:
                    end_reason = "round_failed"
                    break  # 出错/空回复等非正常结束：保持现状退出
                if not goal_enabled:
                    # 普通模式（不带 -g）：单轮执行完毕即退出（不续跑、不验证）
                    end_reason = "done"
                    break
                if self._parts.tool_context.goal_complete:
                    # AI已调用GoalComplete声明完成：复位标记，结束续跑。
                    # 结束原因细分（优先级）：强制放行 > 诚实收尾 > 验证存疑 > 正常完成
                    self._parts.tool_context.goal_complete = False
                    if self._parts.tool_context.goal_forced:
                        end_reason = "goal_forced"
                    elif self._parts.tool_context.goal_honest:
                        end_reason = "goal_honest"
                    elif self._parts.tool_context.goal_suspect:
                        end_reason = "goal_suspect"
                    else:
                        end_reason = "goal_complete"
                    break
                if goal_round >= goal_limit:
                    # 达到轮数上限：注入收尾指令，让AI总结后结束
                    self._logger.info("core.agent", f"目标模式达到轮数上限({goal_limit})，停止续跑")
                    self._msg_manager.append_user(
                        f"[系统提示]目标模式已达到轮数上限（{goal_limit}轮），任务尚未完成。"
                        "请向用户总结当前进度、已完成工作和未完成原因，无需继续执行新任务。"
                    )
                    stream = self._ui.create_stream()
                    self._agent_loop.run(stream, goal_mode=True, force_final=True,
                                         goal_task=goal_task,
                                         round_budget_left=goal_limit - goal_round)
                    end_reason = "round_limit"
                    break
                # 任务未完成：注入继续消息（含完成审计提示），再跑一轮
                self._logger.info("core.agent", f"目标模式自动续跑: 第{goal_round}轮完成，继续")
                self._msg_manager.append_user(
                    f"[自动续跑]已完成{goal_round}轮，任务：{goal_task}\n"
                    "请继续推进任务。\n"
                    + GOAL_AUDIT_HINT
                )
        except Exception as e:
            # 本层未捕获异常（消息注入/流创建/压缩编排等；agent_loop 内的
            # 异常已归因为 aborted/round_failed）：保持 end_reason=unknown，
            # 不走冒泡——异常冒泡到 main.py 顶层会被归为"启动异常"exit 1，
            # 而 README 约定 unknown（任务级失败）退出码为 2。此处收敛后
            # 经哨兵+返回路径，main.py 据此 sys.exit(2)
            self._logger.error("core.agent", f"headless 未捕获异常: {e}")
            _stdout_write(f"\n程序异常退出: {e}\n")
        finally:
            # 完成信号哨兵：所有退出路径必经此处。父代理轮询结果文件时
            # 以该行为准判定"子代理已结束"及结束原因（goal_complete/round_limit/aborted等）。
            _stdout_write(f"\n[NN_DONE] reason={end_reason} rounds={goal_round}\n")
            # 目标模式状态复位（无条件）：同进程复用（批处理/测试脚手架）时，
            # 上一任务 -g 残留的 _goal_enabled/GoalComplete 工具会污染后续任务
            # （普通模式任务被注入 GoalComplete 并触发验证器）
            self._mgr._goal_enabled = False
            self._mgr._goal_max_rounds = 0
            if getattr(self._mgr, '_set_goal_tool', None):
                self._mgr._set_goal_tool(False)
            # 线程池不做任务级 shutdown（同 run()：Agent 支持同进程复用）
            from ..tools.terminal import cleanup as _terminal_cleanup
            from ..tools.serial import cleanup as _serial_cleanup
            from ..tools.background import cleanup_all as _bg_cleanup
            _terminal_cleanup()
            _serial_cleanup()
            _bg_cleanup()
            # MCP 连接不做任务级 cleanup（同 run()：终态不可恢复，atexit 兜底回收）
            self._logger.close()
        return end_reason
