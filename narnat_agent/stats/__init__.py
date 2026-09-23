"""stats 积木 —— 用量/费用累计、成本日志（CSV 与轮转）、余额查询。

契约来源：`openspec/changes/recast-v2/specs/stats/spec.md`（8 Requirement）。
内部职责：`billing` 纯计算（定价/费用分项/JSONPath/余额查询）；
`tracker` 状态承载（累计、成本日志落盘、余额节奏、统计栏读数）。
"""
from .billing import (
    calculate_cost,
    cost_breakdown,
    fetch_balance,
    get_pricing,
    resolve_jsonpath,
)
from .tracker import (
    COST_LOG_HEADER,
    DEFAULT_COST_LOG_PATH,
    CostLog,
    StatsReadings,
    StatsTracker,
)

__all__ = [
    "COST_LOG_HEADER",
    "DEFAULT_COST_LOG_PATH",
    "CostLog",
    "StatsReadings",
    "StatsTracker",
    "calculate_cost",
    "cost_breakdown",
    "fetch_balance",
    "get_pricing",
    "resolve_jsonpath",
]
