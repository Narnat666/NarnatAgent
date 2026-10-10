"""工具调度内循环 —— LLM调用 ↔ 工具执行的循环

从 Agent._agent_loop() / _handle_delete_confirm() / _dump_empty_debug() 提取。
算法逻辑原样保留。
"""

import json
import os
import re
import time

from .llm import LLMClient, retry_sleep
from .message_manager import MessageManager
from .tool_dispatcher import ToolDispatcher
from ..tools.tool_context import ToolContext, AWAIT_CONFIRM
from ..tools.goal_complete import has_declared_changes
from ..tools.background import running_count as _bg_running_count
from ..tools.background import running_summary as _bg_running_summary
from .stats import StatsTracker
from ..ui.ui_design import UIInterface
from ..ui.ui_design import (show_verify_start as _ui_verify_start,
                            show_verify_result as _ui_verify_result,
                            show_verify_skip as _ui_verify_skip)
from ..config.loader import Config
from ..output import write as _stdout_write
from ..tools.exec_signal import strip_tags
from ..logger import AgentLogger


# 迟到提醒触发阈值：同一任务累计工具轮数达到此值仍无计划 → 温和提醒一次
TODO_NUDGE_TOOL_ROUNDS = 6

# 请求前尾部守卫补的 user 收尾内容（异常形态兜底：/done 结论注入尾部 system 后
# 转换层抽出 system 会以无 thinking 的 assistant 收尾，被 API 拒；正常交互不触发）
TAIL_GUARD_MESSAGE = "[]请基于当前上下文继续推进任务。"

# 验证器自报覆盖计数（如 "任务原文要求 3 项，清单覆盖 3 项"）的提取：
# 两段分别取"最后一次出现"，不限制中间隔多少字符——验证器常把逐条核对明细
# 插在两句之间（实测一句可长到上百字），限制宽度会漏掉计数
_RE_REQ_N = re.compile(r"要求\s*(\d+)\s*项")
_RE_COV_N = re.compile(r"覆盖\s*(\d+)\s*项")


def _parse_coverage(summary: str):
    """从验证器摘要里提取 (M, K) 覆盖计数；取不到返回 (0, 0)。"""
    s = summary or ""
    reqs = _RE_REQ_N.findall(s)
    covs = _RE_COV_N.findall(s)
    if not reqs or not covs:
        return 0, 0
    return int(covs[-1]), int(reqs[-1])


class AgentLoop:
    """工具调度内循环"""

    def __init__(self, llm: LLMClient, msg_manager: MessageManager,
                 dispatcher: ToolDispatcher, tool_context: ToolContext,
                 stats: StatsTracker, ui: UIInterface,
                 config: Config, logger: AgentLogger,
                 compression=None, goal_verifier=None):
        self._llm = llm
        self._msg_manager = msg_manager
        self._dispatcher = dispatcher
        self._tool_context = tool_context
        self._stats = stats
        self._ui = ui
        self._config = config
        self._logger = logger
        # 溢出恢复压缩协调器（assembly 注入；None=禁用溢出恢复）
        self._compression = compression
        # 目标完成验证器（assembly 注入；None=不复核完成声明）
        self._goal_verifier = goal_verifier

    @property
    def _thinking_label(self) -> str:
        """思考强度中文标签（从配置读取）"""
        return self._config.ai.thinking_options.get(
            self._config.ai.thinking_effort, self._config.ai.thinking_effort
        )

    def _sync_ratio(self):
        """把最新输入token同步进窗口占比（每轮 usage 到达后调用）。

        占比的唯一更新点原先在用户轮末（agent.py），AI 自我运行期间循环
        顶部自查会一直读到陈旧值；刷新后 mid_run_guard 才能看到真实压力。
        """
        if self._compression is not None:
            self._compression.refresh_ratio(self._stats.input_tokens)

    def run(self, stream, goal_mode: bool = False, force_final: bool = False,
            goal_task: str = "", round_budget_left: int = -1):
        """工具调度内循环

        Args:
            stream: 当前流式输出会话
            goal_mode: 是否目标模式。目标模式下中间轮纯文本结束时不显示统计栏，
                只有AI声明完成（GoalComplete已置位）或强制收尾轮（force_final）才显示，
                使统计栏成为"结案信号"。
            force_final: 强制收尾轮（轮数上限注入收尾指令的那一轮），正常完成时显示统计栏。
            goal_task: 目标模式任务原文（供独立验证器核对任务要求）
            round_budget_left: 剩余续跑预算（组装方 agent.py 传入：limit - 已消耗轮数），
                <0 = 不限制。验证 fail 打回前据此判断：本次打回后本轮消耗（1+k）
                超出剩余预算即无力再跑 → 强制放行，避免"打回却无预算"的空转。
        """
        # 本轮是否正常完成（无tool_call纯文本输出结束）。目标模式续跑据此判断，
        # 出错/中断/空回复等非正常结束不触发自动续跑。
        self._last_round_ok = False
        # 本轮验证打回次数k（每轮清零；预算结算上移到 agent.py：未完成时本轮消耗 1+k）
        self._last_round_blocks = 0
        stream_interrupted_retries = 0  # 响应流中断（无完成标记）的自动重试计数
        overflow_compacted = False      # 本次 run 内是否已做过溢出恢复压缩（重发再溢出则放弃）
        # 流中断重试上限 = 配置的"LLM重试次数"（每轮独立：收到正常完成标记后重置，
        # 与连接层重试语义一致，仅累计同一次中断后的连续重试）
        try:
            stream_retry_max = max(1, min(int(self._config.ai.retry_count), 10))
        except (TypeError, ValueError, AttributeError):
            stream_retry_max = 3
        # 同步LLM层重试次数：网络/5xx/429 与流中断重试统一由同一配置值控制
        self._llm.set_retry_count(stream_retry_max)
        while True:
            # a0. 运行中自查：占比超阈值时先压缩再发本次请求
            #     （AI 自我运行期间不经过用户轮边界；占比由每轮 usage 到达时刷新）
            #     提示先于压缩落定到屏幕：压缩要调一次 LLM，用户需知情（headless 进日志）
            if self._compression is not None and self._compression.need_compress():
                stream.feed("\n⚠ 上下文占比超阈值，正在自动压缩历史…\n")
                stream.flush_renderer()
                if self._compression.mid_run_guard():
                    stream.feed("  压缩完成，继续任务…\n")
                else:
                    stream.feed("  未执行压缩（无可压缩历史或压缩失败），继续本次请求…\n")
                stream.flush_renderer()
                stream.begin()  # 压缩动画已停止，重启"思考中"spinner

            # a. 修复messages
            self._msg_manager.repair()

            # b. 调用LLM
            content_parts = []
            self._last_content_parts = content_parts
            tool_calls_result = []
            call_usage = None
            parsed_finish_reason = None
            stream_interrupted_info = None  # LLM层上报的流中断信息（kind/detail）
            thinking_text = None            # 本轮思考内容（仅工具轮回传，学官方 harness 规则）
            thinking_signature = None       # 本轮思考签名（Claude 回传思考块必需）

            # 请求前尾部守卫：转换层会把 system 消息抽出并入 API system prompt，
            # 若抽除后请求以"无 thinking 的 assistant"收尾（如 /done 结论注入为
            # 尾部 system 之后），thinking 模式下会被 API 拒（400: must be
            # passed back）。正常交互不触发（请求总由 user 输入/续跑消息收尾）；
            # 仅本次请求副本补一条 user，不改动内存与持久化。
            request_msgs = self._msg_manager.view.to_list()
            if request_msgs:
                _tail = request_msgs[-1]
                _tail_role = _tail.get("role")
                if _tail_role == "system" or (
                        _tail_role == "assistant"
                        and not _tail.get("tool_calls") and not _tail.get("thinking")):
                    request_msgs.append({"role": "user", "content": TAIL_GUARD_MESSAGE})

            for chunk in self._llm.chat_stream(request_msgs, cancel_check=lambda: stream.cancelled):
                # b. 检查中断
                if stream.cancelled:
                    # 已完成调用照常记账（与循环外取消分支同一口径：
                    # usage 已到达而残余 chunk 被消费时，此前两条路径漏记不一致）
                    if call_usage:
                        self._stats.update(call_usage)
                        self._sync_ratio()
                    if content_parts:
                        # 中断残留是纯文本轮：不回传思考（学官方：仅工具轮回传）
                        self._msg_manager.append_assistant("".join(content_parts))
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # c. 处理tool_call
                if "tool_calls" in chunk:
                    tool_calls_result = chunk["tool_calls"]

                # c2. 捕获思考内容（assistant消息回传API用）
                if "thinking" in chunk:
                    thinking_text = chunk["thinking"]
                if "thinking_signature" in chunk:
                    thinking_signature = chunk["thinking_signature"]

                # d. 处理纯文本
                if "content" in chunk and "tool_calls" not in chunk:
                    stream.feed(chunk["content"])
                    content_parts.append(chunk["content"])

                # e. 捕获usage
                if "usage" in chunk:
                    call_usage = chunk["usage"]

                # f. 流中断上报（LLM层流读取异常，无完成标记）
                if "stream_interrupted" in chunk:
                    stream_interrupted_info = chunk["stream_interrupted"]
                    continue

                # g. 重试通知（LLM层网络/429重试的用户提示，非AI内容，不入历史）
                if "retry_notice" in chunk:
                    stream.feed(chunk["retry_notice"])
                    continue

                # h. 处理结束
                if "finish_reason" in chunk:
                    parsed_finish_reason = chunk["finish_reason"]
                    if parsed_finish_reason == "context_overflow":
                        # 上下文超限：跳出chunk循环，走压缩恢复分支
                        break
                    if parsed_finish_reason == "error":
                        stream.finish(
                            self._stats.input_tokens,
                            self._stats.output_tokens,
                            cache_ratio=self._stats.cache_hit_ratio,
                            cost=self._stats.cost,
                            balance=self._stats.balance,
                            thinking_effort=self._thinking_label,
                        )
                        return

            # ── 上下文超限溢出恢复：压缩历史 → 重发请求 ──
            # 此时 assistant 消息尚未写入历史，压缩+重发幂等安全。
            # 每次 run 内最多恢复一次：重发仍溢出说明单条消息/系统提示词
            # 本身超限，压不压都救不了，报错收尾防死循环。
            if parsed_finish_reason == "context_overflow":
                if overflow_compacted or self._compression is None:
                    stream.feed(
                        "\n\n⚠ 请求超出模型上下文限制。"
                        "请尝试缩减本次输入，或 /save 保存会话后开启新对话。\n"
                    )
                    stream.finish(
                        self._stats.input_tokens,
                        self._stats.output_tokens,
                        cache_ratio=self._stats.cache_hit_ratio,
                        cost=self._stats.cost,
                        balance=self._stats.balance,
                        thinking_effort=self._thinking_label,
                    )
                    return
                overflow_compacted = True
                if self._logger:
                    self._logger.warning("agent_loop", "请求被拒绝: 上下文超限，执行压缩恢复")
                stream.reset_renderer()
                stream.feed("\n⚠ 上下文超限，正在自动压缩历史…\n")
                if self._compression.compress_no_input():
                    if self._logger:
                        self._logger.info("agent_loop", "压缩恢复完成，重发请求")
                    stream.feed("  压缩完成，重试请求…\n")
                    continue
                stream.feed(
                    "\n\n⚠ 自动压缩失败，请求仍超出模型上下文限制。"
                    "请尝试缩减本次输入，或 /save 保存会话后开启新对话。\n"
                )
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache_ratio=self._stats.cache_hit_ratio,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                    thinking_effort=self._thinking_label,
                )
                return

            # 中断检查
            if stream.cancelled:
                # 已完成调用照常记账（"流完成瞬间取消"窄窗此前会漏记；
                # 流中途取消时 usage 通常尚未到达，自然不记）
                if call_usage:
                    self._stats.update(call_usage)
                    self._sync_ratio()
                stream.abort()
                self._ui.on_interrupted()
                return

            # 本轮收到正常完成标记（含工具轮）→ 重置流中断重试预算（每轮独立）
            if parsed_finish_reason is not None:
                stream_interrupted_retries = 0

            # 本轮 usage 统一落账（放在工具执行/收尾分支之前）：工具执行中被
            # 终止（taskkill 等）时，已完成的 LLM 调用照常记账（README 承诺
            # "每次 API 调用照常记账"）；原"工具轮后/文本轮后"两处记账点已
            # 合并至此，保证每轮响应恰好记一次
            if call_usage:
                self._stats.update(call_usage)
                self._sync_ratio()

            # 有tool_call → 执行工具 → 继续内循环
            if tool_calls_result:
                self._msg_manager.append_assistant(
                    "".join(content_parts) or None,
                    tool_calls=tool_calls_result,
                    thinking=thinking_text,
                    thinking_signature=thinking_signature,
                )

                tool_results = self._dispatcher.execute_tool_calls(tool_calls_result, stream)

                # 中断检查
                if stream.cancelled:
                    completed_ids = {tc_id for tc_id, _ in tool_results}
                    self._msg_manager.append_interrupted_tools(tool_calls_result, completed_ids)
                    stream.abort()
                    self._ui.on_interrupted()
                    return

                # 拦截待确认命令标记（删除/git）：在#提示符下等用户确认后按序逐条重放
                if self._tool_context.pending_delete:
                    pendings = list(self._tool_context.pending_delete)
                    self._tool_context.pending_delete.clear()
                    # 收集本批全部待确认命令的 tool_call_id；
                    # 工具执行器串行执行（Shell/Terminal/Serial 均在串行组），
                    # 收集顺序与 pendings 追加顺序一致，可一一配对。
                    confirm_ids = [tc_id for tc_id, result in tool_results
                                   if result == AWAIT_CONFIRM]
                    if confirm_ids:
                        # 结束当前流式输出，回到#提示符等用户确认
                        new_stream = self._handle_delete_confirm(stream, confirm_ids, pendings, tool_results)
                        if new_stream is not None:
                            stream = new_stream
                            continue
                        return

                # 回传工具结果
                for tc_id, result in tool_results:
                    self._msg_manager.append_tool_result(tc_id, result)

                # （本轮的 usage 落账已上移至工具执行前统一处理）

                # ── 迟到提醒：多轮工具后仍未建立计划 → 温和提醒一次 ──
                # 不阻塞、不重复；文案自带"可忽略"出口，单步任务/探索阶段
                # 由AI自行判断。判据"未建立计划"= current_todos 为空，与
                # 收尾软提醒/GoalComplete 交叉校验对计划的口径一致。
                self._tool_context.tool_rounds_used += 1
                if (not self._tool_context.current_todos
                        and not self._tool_context.todo_nudge_sent
                        and self._tool_context.tool_rounds_used >= TODO_NUDGE_TOOL_ROUNDS):
                    self._tool_context.todo_nudge_sent = True
                    self._msg_manager.append_user(
                        "[系统提醒] 若这是多步任务且你尚未梳理步骤，可考虑用 TodoWrite 建立计划；"
                        "单步任务或探索阶段可忽略本条。"
                    )

                continue

            # ── 响应流中断（无完成标记）→ 整轮重试（指数退避）──
            # 正常完成必有finish_reason；None表示服务端在响应中途断开。
            # 此时assistant消息尚未写入历史（部分内容/不完整tool_calls被丢弃），
            # 重发幂等无副作用。重试上限=配置的LLM重试次数，网络波动时可连续自救。
            if parsed_finish_reason is None:
                if stream_interrupted_retries < stream_retry_max:
                    stream_interrupted_retries += 1
                    kind = (stream_interrupted_info or {}).get("kind", "unknown")
                    base = LLMClient.RETRY_BACKOFF_BASE[
                        min(stream_interrupted_retries - 1, len(LLMClient.RETRY_BACKOFF_BASE) - 1)
                    ]
                    if self._logger:
                        self._logger.warning(
                            "agent_loop",
                            f"响应流中断({kind})，{base}s后自动重试"
                            f"(第{stream_interrupted_retries}/{stream_retry_max}次)",
                        )
                    # 清空渲染器缓冲：重试流会从开头重播，上一轮残留的半行文字/
                    # 表格行/代码块若不清除，会与重播内容拼接重复或错乱。
                    stream.reset_renderer()
                    stream.feed(
                        f"\n⚠ 服务端响应流中断，约{base}s后自动重试"
                        f"（第{stream_interrupted_retries}/{stream_retry_max}次）…\n"
                    )
                    # 退避等待期间用户可ESC取消
                    if not retry_sleep(stream_interrupted_retries - 1, lambda: stream.cancelled):
                        stream.abort()
                        self._ui.on_interrupted()
                        return
                    continue
                # 重试耗尽 → 报错结束（截断内容不写入历史，避免污染上下文）
                stream.feed(
                    f"\n\n⚠ 服务端响应流中断，已自动重试{stream_retry_max}次仍失败，请稍后重试。\n"
                )
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache_ratio=self._stats.cache_hit_ratio,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                    thinking_effort=self._thinking_label,
                )
                return

            # 无tool_call → 纯文本输出完成
            if content_parts:
                # 纯文本轮不回传思考（学官方 harness：reasoning_content 仅工具轮回传，
                # 纯文本轮被忽略，省 token）
                self._msg_manager.append_assistant("".join(content_parts))
            else:
                # 空回复
                self._dump_empty_debug(content_parts, tool_calls_result, parsed_finish_reason, call_usage)
                _empty_msgs = {
                    "stop": "⚠ AI 返回了空回复，请尝试缩短对话或稍后重试。",
                    "max_tokens": "⚠ AI 思考超过了最大输出限制，请增大限制或缩短对话。",
                    "length": "⚠ AI 思考超过了最大输出限制，请增大限制或缩短对话。",
                    "content_filter": "⚠ AI 返回被安全策略拦截，请调整提问内容。",
                    "server_busy": "⚠ 服务器繁忙，请稍后重试。",
                    "error": "⚠ AI 调用出错，请查看上方错误信息。",
                    "stream_interrupted": "⚠ 服务端响应流中断，请稍后重试。",
                }
                reason = parsed_finish_reason or "stream_interrupted"
                msg = _empty_msgs.get(reason, f"⚠ AI 返回异常（{reason}），请稍后重试。")
                stream.feed(f"\n\n{msg}\n")
                stream.finish(
                    self._stats.input_tokens,
                    self._stats.output_tokens,
                    cache_ratio=self._stats.cache_hit_ratio,
                    cost=self._stats.cost,
                    balance=self._stats.balance,
                    thinking_effort=self._thinking_label,
                )
                return

            # （本轮的 usage 落账已上移至工具执行前统一处理）

            # ── 收尾软提醒：计划未全部勾选时提醒一次，仅一次 ──
            # AI漏勾选就输出总结时，注入一条提醒让它补勾；无论下一轮结果如何
            # 都放行（todo_reminded置True后不再触发），不循环、不复读。
            # 提醒仅注入AI上下文，终端不展示（内部纠偏，用户无需感知）。
            unfinished = [
                t for t in self._tool_context.current_todos
                if t.get("status") != "completed"
            ]
            if unfinished and not self._tool_context.todo_reminded:
                self._tool_context.todo_reminded = True
                names = "、".join(str(t.get("content", "")) for t in unfinished[:5])
                tail = f"等共{len(unfinished)}项" if len(unfinished) > 5 else ""
                self._msg_manager.append_user(
                    f"[系统提醒] 你的计划仍有未勾选完成的项: {names}{tail}。"
                    "若这些任务实际已完成，请先调用TodoWrite勾选它们（并把仍需继续的项标记为进行中）"
                    "再输出最终总结；若确实未完成或刻意跳过，请在最终总结中向用户说明原因；"
                    "若你有需要用户澄清的困惑或依赖用户决策，可直接向用户提问，无需强行收尾。"
                )
                # 复用同一stream继续内循环：先把上一轮文字落定到屏幕，
                # 再重启"思考中"spinner，避免LLM等待期界面无动画静默卡住
                stream.flush_renderer()
                stream.begin()
                continue

            # ── 后台任务软提醒：结束回合时仍有 running → 提醒一次，AI 不处理则放行 ──
            # 不强杀：wait 挂住回合才是默认姿态；用户ESC插话后 AI 带着 running
            # 结束本轮是常态，强杀会杀掉用户正在等的任务（硬兜底在 GoalComplete/会话结束）。
            bg_summary = _bg_running_summary()
            if bg_summary and not self._tool_context.bg_reminded:
                self._tool_context.bg_reminded = True
                self._msg_manager.append_user(
                    f"[系统提醒] 后台仍有任务在运行: {bg_summary}。"
                    "若需等待其完成，请调用 Shell(bg=\"wait\")（任一任务完成会立即返回）；"
                    "若不再需要这些任务，请调用 Shell(bg=\"cancel\", id=N) 清理后结束回复。"
                )
                # 复用同一stream继续内循环：先把上一轮文字落定到屏幕，
                # 再重启"思考中"spinner，避免LLM等待期界面无动画静默卡住
                stream.flush_renderer()
                stream.begin()
                continue

            # ── 完成验证：AI 声明完成（goal_complete 置位）→ 独立验证器复核 ──
            # 诚实收尾（含未完成/受阻项）、已强制放行、收尾轮均不验证，直接走结案。
            # 触发门槛：清单未申报任何文件改动（纯查询/推导类任务）时无文件可核对 →
            # 跳过独立复核直接结案（省掉一次无据可查的验证往返）。
            if (goal_mode
                    and self._tool_context.goal_complete
                    and not self._tool_context.goal_honest       # 诚实收尾（含未完成/受阻项）不验证
                    and not self._tool_context.goal_forced       # 已强制放行
                    and not force_final                          # 收尾轮不验证
                    and self._config.ai.goal_verify              # 配置开关
                    and self._goal_verifier is not None):
                if not has_declared_changes(self._tool_context.goal_checklist):
                    _ui_verify_skip()
                else:
                    final_answer = "".join(content_parts)
                    _ui_verify_start()
                    stream.flush_renderer()
                    # 验证动画为可选 UI 能力（轻量 UI/测试替身可缺省）→ 缺失时静默跳过
                    begin_verify = getattr(self._ui, "begin_verifying", None)
                    end_verify = getattr(self._ui, "end_verifying", None)
                    if begin_verify:
                        begin_verify()
                    _v_t0 = time.time()
                    try:
                        result = self._goal_verifier.verify(
                            goal_task, self._tool_context.goal_checklist, final_answer,
                            cancel_check=lambda: stream.cancelled)
                    finally:
                        if end_verify:
                            end_verify()
                    _v_secs = time.time() - _v_t0
                    if stream.cancelled or result.verdict == "interrupted":
                        stream.abort()
                        self._ui.on_interrupted()
                        return
                    # 覆盖计数（K/M）：从裁决摘要里取验证器自报的计数，取不到则不展示
                    _covered = _parse_coverage(result.summary)
                    if result.verdict == "pass":
                        _ui_verify_result("pass", result.summary, secs=_v_secs,
                                          blocks=_covered[0], limit=_covered[1])
                        # 保持 goal_complete → 走正常结案收尾
                    elif result.verdict == "uncertain":
                        self._tool_context.goal_suspect = True
                        _ui_verify_result("uncertain", result.summary, secs=_v_secs)
                    else:  # fail
                        # 记录本轮一次验证打回（本轮内第 k 次）；预算结算统一在 agent.py：
                        # 未声明完成时本轮消耗 = 1（续跑）+ k（验证打回次数）
                        self._last_round_blocks += 1
                        # 打回计数展示分母：本任务续跑总预算（剩余预算 + 已消耗轮数）
                        limit = round_budget_left + self._tool_context.goal_rounds_used
                        # 预算耗尽判定：本次打回后本轮消耗（1+k）将超出剩余预算 →
                        # 打回已无力再跑（没有下一轮空间）→ 强制放行（未通过质检），不空转。
                        # round_budget_left < 0 表示不限制（未提供预算）
                        if 0 <= round_budget_left < 1 + self._last_round_blocks:
                            # 拟议打回无力再跑：本次打回未实际发生（不注入返工提示、
                            # 不产生新一轮），撤回其计数——否则预算结算多记 1，
                            # rounds 虚超 -g N，父代理误判预算消耗
                            self._last_round_blocks -= 1
                            self._tool_context.goal_forced = True
                            _ui_verify_result("forced", result.summary,
                                              gaps=result.gaps, secs=_v_secs)
                        else:
                            self._tool_context.goal_complete = False
                            _ui_verify_result("fail", result.summary,
                                              gaps=result.gaps, secs=_v_secs,
                                              blocks=self._last_round_blocks,
                                              limit=limit)
                            self._msg_manager.append_user(result.continue_prompt)
                            stream.begin()
                            continue

            # ── 结案点：goal_complete 置位（验证通过/存疑/强制/诚实收尾）→ 清理受管后台任务 ──
            # 声明完成≠结案：验证可能打回继续工作，故清理从 GoalComplete 工具移到本处，
            # 返工期间后台依赖保持存活；不要求 goal_mode（普通模式误调 GoalComplete 同样清理）
            if self._tool_context.goal_complete:
                try:
                    from ..tools.background import cleanup_all
                    cleanup_all()
                except Exception:
                    pass

            self._last_round_ok = True  # 正常完成：无tool_call纯文本输出
            # 统计栏 = 结案信号：普通模式每轮结束都显示；
            # 目标模式只有AI声明完成（goal_complete已置位）或强制收尾轮才显示，
            # 中间轮静默结束（AI文字照常显示，仅不打印统计栏）。
            show_stats = (not goal_mode) or self._tool_context.goal_complete or force_final
            # 后台仍有运行中任务：不显示统计栏（结案信号），终端提示仍在运行，
            # 避免用户误以为 agent 已全部完成而离开
            bg_running = _bg_running_count()
            if bg_running:
                show_stats = False
                _stdout_write(
                    f"  ⚠ 后台 {bg_running} 个任务仍在运行"
                    f"（Shell(bg=\"wait\") 等待 / Shell(bg=\"cancel\", id=N) 清理）\n"
                )
            stream.finish(
                self._stats.input_tokens,
                self._stats.output_tokens,
                cache_ratio=self._stats.cache_hit_ratio,
                cost=self._stats.cost,
                balance=self._stats.balance,
                thinking_effort=self._thinking_label,
                with_stats=show_stats,
            )
            break

    def _handle_delete_confirm(self, stream, confirm_ids, pendings, tool_results):
        """处理待确认命令（删除/git）：结束流式输出，在#提示符下等用户确认后按序逐条重放。

        Args:
            stream: 当前流式输出会话
            confirm_ids: 返回AWAIT_CONFIRM的tool_call_id列表（按原始顺序）
            pendings: 暂存的待确认命令 [(tool_name, arguments_dict), ...]（与 confirm_ids 一一对应）
            tool_results: 所有工具结果列表 [(tc_id, result), ...]

        Returns:
            新的UIStreamSession - 用户已确认/取消，结果已回传，继续agent_loop
            None - 流已结束，调用方应return
        """
        confirm_set = set(confirm_ids)

        # 先回传非确认的工具结果
        for tc_id, result in tool_results:
            if tc_id not in confirm_set:
                self._msg_manager.append_tool_result(tc_id, result)

        # 结束当前流式输出（本轮还没结束，不显示stats，等确认后最终finish再显示）
        stream.finish(
            self._stats.input_tokens,
            self._stats.output_tokens,
            cache_ratio=self._stats.cache_hit_ratio,
            cost=self._stats.cost,
            balance=self._stats.balance,
            thinking_effort=self._thinking_label,
            with_stats=False,
        )

        # 在#提示符下显示确认信息，等待用户输入
        prompt = ("  确认执行此命令? [y/N]: " if len(confirm_ids) == 1
                  else f"  确认执行这{len(confirm_ids)}条命令? [y/N]: ")
        user_input = self._ui.read_input_with_prompt(prompt)
        if user_input is None:
            user_input = ""

        confirmed = user_input.strip().lower() in ("y", "yes")

        if confirmed and len(pendings) == len(confirm_ids):
            # 用户确认：逐条重放。每条重放前设置确认标志，工具消费该标志后跳过
            # 确认检测直接执行（标志一次性）；结果回填到各自的 tool_call_id。
            from ..tools.registry import execute as tool_execute
            for tc_id, (tool_name, arguments) in zip(confirm_ids, pendings):
                self._tool_context._delete_confirmed = True
                llm_result, color_diff = tool_execute(tool_name, arguments, self._tool_context)
                # 框架退出码标签只服务于判定，不给AI看
                llm_result = strip_tags(llm_result)
                self._msg_manager.append_tool_result(tc_id, llm_result)
                if color_diff:
                    _stdout_write("\n".join(f"  {line}" for line in color_diff.split("\n")) + "\n\n")
            # 重放结束无条件复位：防止异常输入（未消费标志的命令）把"跳过确认"
            # 标志残留到后续命令，导致删除命令跳过确认直接执行（fail-open）
            self._tool_context._delete_confirmed = False
        else:
            # 用户取消（或配对异常，安全侧按取消处理）：全部按取消回填
            for tc_id in confirm_ids:
                self._msg_manager.append_tool_result(tc_id, "[操作已取消: 此命令需用户确认]")

        # 创建新的流式输出会话，继续agent_loop
        return self._ui.create_stream()

    def _dump_empty_debug(self, content_parts, tool_calls_result, finish_reason, call_usage):
        """空回复时写调试日志"""
        debug_path = os.path.join(
            self._config.paths.data_dir,
            f"debug_empty_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        debug_data = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": {"messages": self._msg_manager.view.to_list()},
            "response": {
                "raw_sse_lines": self._llm.raw_sse or [],
                "parsed_content": "".join(content_parts),
                "parsed_tool_calls": tool_calls_result,
                "parsed_finish_reason": finish_reason,
                "call_usage": call_usage,
            },
        }
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            _stdout_write(f"  ⚠ 调试日志已写入: {debug_path}\n")
        except OSError:
            pass
