"""交互主循环 —— 输入读取、命令分发、压缩检查、轮次调度与窗口占比。

契约来源：`openspec/changes/recast-v2/specs/app/spec.md`：
- 「交互主循环」：启动预清 → 启动界面 → 启动日志；随后循环：读输入（失败或空回
  仅继续）→ 等待后台自动保存同步点 → 忽略纯空白 → `/` 前缀命令分发 → 非命令输入
  进入调度；调度序为轮次计数加一与余额查询 → 新任务状态复位 → 压缩检查 →
  追加用户消息 → 目标模式解析 → 内层轮次循环 → 轮末占比刷新与告警；
- 「新任务状态复位」：目标完成标记、两类提醒标志与计划内容在压缩检查之前复位；
- 「余额查询与费用统计触发点」：轮次计数与密钥交统计组件，由其按间隔决定查询；
- 「退出清理」退出命令路径：会话落盘、退出日志、MCP 清理与日志关闭后进程立即终止；
- 「兼容性怪癖保持」：未识别命令按对话内容处理、退出走进程立即终止。

结构（design D7/D8）：主循环不摸内循环私有字段（用 `TurnOutcome`）、不摸调度器
线程池（由 `app` 持有并关闭）、不摸会话管理器私有字段（用 `GoalMode` 公开 API）。
"""
from __future__ import annotations

import os

from ..conversation import TURN_ERROR, TURN_INTERRUPTED, TurnOutcome
from ..ui import CommandResult
from .assembly import LOG_MODULE
from .lifecycle import cleanup_exit_command, cleanup_resources

__all__ = ["run_interactive"]


def _reset_task_state(env) -> None:
    """新任务状态复位（目标完成标记、两类提醒标志、当前计划内容）。"""
    env.goal.reset()
    env.reminders.reset()
    env.plan.replace([])


def _prepare_input(parts, text: str) -> bool:
    """压缩检查与消息追加：返回 False 表示本轮不进入对话轮次（回到输入提示）。

    达到压缩阈值时先压缩（成功由压缩流程把本次输入写入历史）；取消或失败时本轮
    不进入轮次；未触发压缩时自行修复消息序列并追加用户输入。
    """
    if parts.context.need_compress():
        return parts.coordinator.compress(text).ok
    parts.store.repair()
    parts.store.append_user(text)
    parts.logger.info(LOG_MODULE, f"用户输入: {text[:100]}")
    return True


def _dispatch_turn(parts, text: str) -> None:
    """进入轮次调度（目标模式按轮后判定续跑 / 收尾；普通模式单轮结束）。"""
    session_mgr = parts.session_mgr
    goal_state = session_mgr.goal
    goal_enabled = goal_state.enabled
    if goal_enabled:
        parts.goal_runner.start(text, goal_state.override_max_rounds)

    while True:
        sink = parts.ui.begin_turn()
        try:
            outcome = parts.conversation.run(sink, goal_mode=goal_enabled)
        except KeyboardInterrupt:
            parts.ui.notify_interrupted()
            sink.abort()
            outcome = TurnOutcome(TURN_INTERRUPTED, parts.conversation.last_content)
        except Exception as exc:
            parts.logger.error(LOG_MODULE, f"异常: {exc}")
            residual = parts.conversation.last_content
            if residual:
                parts.store.append_assistant(residual)
            theme = parts.theme
            # 异常≠用户打断：显示明确的错误提示，而非"已打断"
            sink.abort(message=f"  {theme.x}⚠ 程序异常，本轮回复已停止: {exc}{theme.r}")
            outcome = TurnOutcome(TURN_ERROR, residual)
        else:
            if not sink.aborted:
                session_mgr.persist_current()
                session_mgr.maybe_auto_save(parts.stats.input_tokens)

        if not goal_enabled:
            return

        decision = parts.goal_runner.after_round(outcome)
        if decision.continues:
            parts.logger.info(
                LOG_MODULE,
                f"目标模式自动续跑: 第{parts.goal_runner.rounds}轮完成，继续",
            )
            parts.store.append_user(decision.text)
            continue
        if decision.needs_final_round:
            # 达到轮数上限：注入收尾指令，强制收尾轮正常结束时显示统计栏
            parts.logger.info(
                LOG_MODULE,
                f"目标模式达到轮数上限({parts.goal_runner.limit})，停止续跑",
            )
            parts.store.append_user(decision.text)
            sink = parts.ui.begin_turn()
            parts.conversation.run(sink, goal_mode=goal_enabled, force_final=True)
            if not sink.aborted:
                session_mgr.persist_current()
                session_mgr.maybe_auto_save(parts.stats.input_tokens)
        return


def _refresh_ratio(parts) -> None:
    """轮末刷新窗口占比并检查一次性告警（中断或异常时占比沿用上一轮值）。"""
    parts.context.update_ratio(parts.stats.input_tokens)
    warn = parts.context.check_warn()
    if warn:
        parts.console.write(f"  ⚠ {warn}\n")


def run_interactive(parts) -> None:
    """交互主循环（读到退出命令或异常为止）。

    Args:
        parts: 装配产物（`app.assembly.AppParts`）。
    """
    # 会话开始预清：清空上次异常退出的后台残留（正常退出由 finally 硬清理）
    parts.background.prepare()
    parts.ui.start(parts.config.ai.model)
    parts.logger.info(LOG_MODULE, f"Agent启动, model={parts.config.ai.model}")

    round_num = 0
    try:
        while True:
            user_input = parts.ui.read_input()
            if user_input is None:
                continue

            # 同步点：等后台自动保存完成
            parts.session_mgr.wait_auto_save()

            stripped = user_input.strip()
            if not stripped:
                continue

            # 命令分发（`/` 前缀；三态：已处理 → 回到输入 / 退出 → 立即终止 / 未识别 → 落模型）
            if stripped.startswith("/"):
                words = stripped.split(None, 1)
                cmd = words[0]
                args = words[1] if len(words) > 1 else ""
                result = parts.ui.dispatch_command(cmd, args)
                if result == CommandResult.EXIT:
                    cleanup_exit_command(parts)
                    os._exit(0)
                if result == CommandResult.HANDLED:
                    continue

            # 轮次计数（余额查询周期用）+ 余额查询
            round_num += 1
            api_key = getattr(parts.config.ai, "api_key", None)
            parts.stats.fetch_balance(api_key, round_num)

            # 新任务翻篇：复位目标完成标记、两类提醒标志与当前计划内容
            # （必须在压缩检查与消息追加之前，防上一任务残留误判新任务）
            _reset_task_state(parts.env)

            # 压缩检查与消息追加（取消或失败：本轮不进入对话轮次，回到输入提示）
            if not _prepare_input(parts, stripped):
                continue

            # 轮次调度（普通模式单轮；目标模式按判定续跑与收尾）
            _dispatch_turn(parts, stripped)

            # 轮末：刷新窗口占比并检查一次性告警
            _refresh_ratio(parts)
    finally:
        cleanup_resources(parts)
