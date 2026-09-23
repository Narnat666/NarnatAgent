"""headless 一次性运行 —— 任务注入、目标模式续跑、哨兵输出与退出清理。

契约来源：`openspec/changes/recast-v2/specs/app/spec.md`「headless 一次性运行」：
- 任务注入历史（修复后追加）、强制开启目标模式并注入目标完成工具、轮数上限取
  `-g` 覆盖值（大于 0 时）或配置默认值；
- 不读取用户输入、不自动保存会话、不查询余额、不显示统计栏；
- 每轮先做压缩检查（失败则终止，`compress_failed`）→ 建流执行内循环（异常时以
  提示结束流并终止，`aborted`）→ 轮计数加一 → 固定顺序判定（被中断 → `aborted`；
  非正常完成 → `round_failed`；已声明完成 → `goal_complete`；达到轮数上限 →
  注入收尾指令并执行强制收尾轮 → `round_limit`；否则注入续跑提示继续）；
- 清理前无条件输出哨兵行 `\\n[NN_DONE] reason={原因} rounds={轮数}\\n`（正常、
  异常、中断路径都必须出现）。

结构（design D7）：判定只读 `TurnOutcome` 与 `GoalState.consume()`，不摸内循环与
会话管理器私有字段；`GoalMode` 开关状态经会话管理器公开 API 设置。
"""
from __future__ import annotations

from ..conversation import CONTINUE_TEMPLATE, FINAL_ROUND_TEMPLATE
from .assembly import LOG_MODULE
from .lifecycle import cleanup_resources

__all__ = [
    "DONE_ABORTED",
    "DONE_COMPRESS_FAILED",
    "DONE_GOAL_COMPLETE",
    "DONE_REASONS",
    "DONE_ROUND_FAILED",
    "DONE_ROUND_LIMIT",
    "DONE_UNKNOWN",
    "run_headless",
]

# 退出原因取值集（哨兵契约；未进入判定即退出为 unknown）
DONE_GOAL_COMPLETE = "goal_complete"
DONE_ROUND_LIMIT = "round_limit"
DONE_ROUND_FAILED = "round_failed"
DONE_ABORTED = "aborted"
DONE_COMPRESS_FAILED = "compress_failed"
DONE_UNKNOWN = "unknown"

DONE_REASONS = (
    DONE_GOAL_COMPLETE,
    DONE_ROUND_LIMIT,
    DONE_ROUND_FAILED,
    DONE_ABORTED,
    DONE_COMPRESS_FAILED,
    DONE_UNKNOWN,
)


def done_line(reason: str, rounds: int) -> str:
    """哨兵行文本（父代理以此判定子代理结束与结束原因）。"""
    return f"\n[NN_DONE] reason={reason} rounds={rounds}\n"


def run_headless(parts, task: str, max_rounds: int = 0) -> None:
    """headless 一次性任务执行（`nn -p` 入口）。

    Args:
        parts: 装配产物（`app.assembly.AppParts`）；
        task: 任务内容（已由入口校验非空）；
        max_rounds: 续跑轮数上限覆盖值（大于 0 时优先，否则用配置默认值）。
    """
    parts.logger.info(LOG_MODULE, f"Agent启动(headless), model={parts.config.ai.model}")

    # 会话开始预清：清空上次异常退出的后台残留（与交互主循环一致）
    parts.background.prepare()

    # 哨兵变量在 try 外初始化：finally 无条件引用，任何异常路径都必须能打印 [NN_DONE]
    goal_round = 0
    end_reason = DONE_UNKNOWN

    try:
        # 开启目标模式：注入 GoalComplete 工具（max_rounds>0 临时覆盖，否则用配置默认值）
        goal_state = parts.session_mgr.goal
        goal_state.turn_on(max_rounds)
        goal_limit = goal_state.max_rounds
        goal_task = task.strip()

        # 注入任务（修复后追加）
        parts.store.repair()
        parts.store.append_user(goal_task)
        parts.logger.info(LOG_MODULE, f"任务注入: {goal_task[:100]}")

        while True:
            # 压缩检查（与交互模式对齐：上下文超限时先压缩再继续）
            if parts.context.need_compress():
                if not parts.coordinator.compress(goal_task).ok:
                    end_reason = DONE_COMPRESS_FAILED
                    break

            sink = parts.ui.begin_turn()
            try:
                outcome = parts.conversation.run(sink, goal_mode=True)
            except Exception as exc:
                parts.logger.error(LOG_MODULE, f"异常: {exc}")
                sink.abort(message=f"⚠ 程序异常，本轮回复已停止: {exc}")
                end_reason = DONE_ABORTED
                break

            goal_round += 1
            if sink.aborted or outcome.interrupted:
                end_reason = DONE_ABORTED
                break
            if not outcome.completed:
                end_reason = DONE_ROUND_FAILED
                break
            if parts.env.goal.consume():
                # AI 已调用 GoalComplete 声明完成：复位标记，结束续跑
                end_reason = DONE_GOAL_COMPLETE
                break
            if goal_round >= goal_limit:
                # 达到轮数上限：注入收尾指令，让 AI 总结后结束
                parts.logger.info(
                    LOG_MODULE, f"目标模式达到轮数上限({goal_limit})，停止续跑"
                )
                parts.store.append_user(FINAL_ROUND_TEMPLATE.format(limit=goal_limit))
                sink = parts.ui.begin_turn()
                parts.conversation.run(sink, goal_mode=True, force_final=True)
                end_reason = DONE_ROUND_LIMIT
                break
            # 任务未完成：注入续跑提示，再跑一轮
            parts.logger.info(
                LOG_MODULE, f"目标模式自动续跑: 第{goal_round}轮完成，继续"
            )
            parts.store.append_user(
                CONTINUE_TEMPLATE.format(rounds=goal_round, task=goal_task)
            )
    finally:
        # 完成信号哨兵：所有退出路径必经此处；先于清理输出（父代理轮询结果文件）
        parts.console.write(done_line(end_reason, goal_round))
        cleanup_resources(parts, close_log=True)
