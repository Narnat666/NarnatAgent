"""app 积木 —— 应用层（唯一装配点、交互主循环、headless 运行、退出清理）。

行为契约：`openspec/changes/recast-v2/specs/app/spec.md`（9 Requirement）。
积木构成：
- `assembly`：唯一构造点（design D8 线性装配；构造顺序即依赖顺序，无后置补线），
  产出 `AppParts` 装配产物与 `App` 门面；
- `interactive`：交互主循环（输入 → 命令分发 → 状态复位 → 压缩 → 轮次调度 → 占比）；
- `headless`：一次性任务运行（目标模式续跑、哨兵行与退出原因取值集）；
- `lifecycle`：日志器与两条退出路径的资源清理；
- `summarizer`：模型命名与模型总结（注入 `sessions` 的实现）。

依赖规则（design D1）：本积木位于 L4（最高层），可依赖全部低层积木。
"""
from __future__ import annotations

from .assembly import (
    TOOL_POOL_WORKERS,
    App,
    AppParts,
    DeleteConfirmer,
    DiffColorizer,
    InterruptProbe,
    build,
)
from .headless import (
    DONE_ABORTED,
    DONE_COMPRESS_FAILED,
    DONE_GOAL_COMPLETE,
    DONE_REASONS,
    DONE_ROUND_FAILED,
    DONE_ROUND_LIMIT,
    DONE_UNKNOWN,
    done_line,
    run_headless,
)
from .interactive import run_interactive
from .lifecycle import AgentLogger, cleanup_exit_command, cleanup_resources
from .summarizer import Summarizer

__all__ = [
    # assembly
    "App",
    "AppParts",
    "DeleteConfirmer",
    "DiffColorizer",
    "InterruptProbe",
    "TOOL_POOL_WORKERS",
    "build",
    # interactive
    "run_interactive",
    # headless
    "DONE_ABORTED",
    "DONE_COMPRESS_FAILED",
    "DONE_GOAL_COMPLETE",
    "DONE_REASONS",
    "DONE_ROUND_FAILED",
    "DONE_ROUND_LIMIT",
    "DONE_UNKNOWN",
    "done_line",
    "run_headless",
    # lifecycle
    "AgentLogger",
    "cleanup_exit_command",
    "cleanup_resources",
    # summarizer
    "Summarizer",
]
