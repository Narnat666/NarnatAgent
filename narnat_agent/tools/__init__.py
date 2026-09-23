"""tools 积木 —— 工具运行时框架（标签协议 / 注册表 / 运行时环境 / token 估算）。

框架层（本积木的公共面）：
- `tools.signal`：退出码与错误标签协议（进程级随机标签、先判定后剥离、截断切点吸附）；
- `tools.registry`：工具注册表与统一执行入口（内置/动态注册、参数错误文案、全局截断）；
- `tools.env`：`ToolEnv` 协议实现与各状态 Tracker（状态归主：PlanTracker / GoalState /
  ReminderState / DeleteGate / ToolSettings）；
- `tools.token_estimate`：截断提示与压缩估价共用的 token 估算（混合密度启发式）；
- `tools.catalog`：工具目录端口实现（注册表 + LLM 工具表组合，供 MCP 热注册注入）。

工具族（file / shell / remote / websearch / plan）为独立积木任务，各自实现
`contracts.tool.Tool` 协议后向 `ToolRegistry` 注册。

分层规则（design D1，机械化检查见 `tests/check_layering.py`）：本积木只依赖 `contracts`。
"""
from __future__ import annotations

from . import catalog, env, registry, signal, token_estimate
from .catalog import ToolCatalogImpl
from .env import (
    DeleteGateImpl,
    GoalStateImpl,
    PlanTrackerImpl,
    ReminderStateImpl,
    ToolEnvImpl,
    ToolSettingsImpl,
)
from .registry import (
    ToolRegistry,
    friendly_type_error,
    normalize_result,
    valid_param_names,
)
from .signal import (
    error_line,
    has_error,
    parse_rc,
    rc_line,
    safe_cut_points,
    strip_tags,
    tag_error,
)
from .token_estimate import estimate_message_tokens, estimate_text_tokens

__all__ = [
    # 子模块（`from narnat_agent.tools import signal` 等四条路径）
    "catalog",
    "env",
    "registry",
    "signal",
    "token_estimate",
    # signal —— 标签协议
    "error_line",
    "has_error",
    "parse_rc",
    "rc_line",
    "safe_cut_points",
    "strip_tags",
    "tag_error",
    # registry —— 注册表与执行入口
    "ToolRegistry",
    "friendly_type_error",
    "normalize_result",
    "valid_param_names",
    # catalog —— 工具目录端口实现
    "ToolCatalogImpl",
    # env —— ToolEnv 实现与 Trackers
    "DeleteGateImpl",
    "GoalStateImpl",
    "PlanTrackerImpl",
    "ReminderStateImpl",
    "ToolEnvImpl",
    "ToolSettingsImpl",
    # token_estimate
    "estimate_message_tokens",
    "estimate_text_tokens",
]
