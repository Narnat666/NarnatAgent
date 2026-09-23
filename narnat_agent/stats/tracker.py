"""统计追踪 —— 用量/费用累计、成本日志写入与轮转、余额查询节奏（状态承载方）。

契约来源：
- `openspec/changes/recast-v2/specs/stats/spec.md`「用量累计口径」：输出 token 全程
  累加；输入 token 为最近一轮的值（当前上下文快照）；累计输入与累计缓存命中用于
  token 加权缓存命中率；对外读数 = 最近输入 / 累计输出 / 加权命中率 / 累计费用；
- 「成本日志写入」：启用时每次 LLM 请求后向 CSV 追加一行（默认
  `.narnat/data/cost_log.csv`，配置非空时以配置为准），列序与「未命中 = 输入 −
  缓存命中」口径固定；新文件（大小 0）先写表头；UTF-8-sig 追加写；费用保留
  9 位小数；写失败（OSError）不丢行——失败行入内存队列，下次写入前先补写积压行、
  成功一行出队一行、失败即停；
- 「成本日志容量与轮转」：活动文件达到上限（负值按 0 = 不轮转）时执行双文件
  轮转（旧 `<主名>_bak<扩展名>` 被替换；新活动文件从表头开始），磁盘上始终只留
  1 活动 + 1 备份；轮转失败不中断写入（本次不写表头、继续追加）；
- 「余额查询节奏」：仅当轮次号为间隔（10）的整数倍且接口密钥非空时发起查询；
  成功刷新显示余额、失败保持上次值；其余情形显示余额重置为 0.0（查询调用点归
  app 主循环，本模块提供 `fetch_balance` 轮次语义）；
- 「统计栏数据供给」：`snapshot()` 给出输入 token / 输出 token / 缓存命中率 /
  费用 / 余额五项读数（显示开关、格式与着色归界面）；
- 「兼容性怪癖保持」：余额 0.0 双关（未到查询轮与余额为 0 不可区分）；缓存命中
  大于输入时「缓存未命中tokens」为负且不钳制；用量数据缺输入/输出 token 键时
  抛错（不做防御性取值）；失败行仅驻留内存、只在下次请求时补写；运行中切换
  模型后已累计费用不重算。

设计决策（design D7/D8）：模型名经 `set_model()` 公开切换（消除外部直改私有字段）；
费用与余额的纯计算在 `billing`，本模块只承载状态与落盘副作用。
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from .billing import cost_breakdown, fetch_balance

__all__ = [
    "COST_LOG_HEADER",
    "DEFAULT_COST_LOG_PATH",
    "CostLog",
    "StatsReadings",
    "StatsTracker",
]

# 费用日志 CSV 表头（已发布列序契约）
COST_LOG_HEADER = [
    "时间", "模型",
    "输入tokens", "缓存命中tokens", "缓存未命中tokens",
    "输出tokens(含思考)",
    "输入费用(元)", "缓存费用(元)", "输出费用(元)",
    "本次费用(元)", "累计费用(元)",
]

# 费用日志默认输出文件（相对当前工作目录）
DEFAULT_COST_LOG_PATH = os.path.join(".narnat", "data", "cost_log.csv")

# 费用数值落盘精度（小数位）
COST_DECIMALS = 9


class CostLog:
    """成本日志落盘器：CSV 追加、空文件先写表头、容量轮转、写失败补写队列。

    行格式（11 列，与 `COST_LOG_HEADER` 对应）：时间（本地时区）、模型、输入
    tokens、缓存命中 tokens、缓存未命中 tokens（= 输入 − 缓存命中，可为负）、
    输出 tokens（含思考）、输入/缓存/输出分项费用、本次费用、累计费用；
    费用数值按 `round(x, 9)` 落盘。写失败行驻留 `_pending` 内存队列，下一次
    写入前按序补写（不丢行不重复写，此后无请求则永久驻留）。
    """

    def __init__(self, enabled: bool = False, path: str = "", max_bytes: int = 0):
        self._enabled = bool(enabled)
        self._path = path
        self._max_bytes = max_bytes
        self._pending: list[list[Any]] = []

    @classmethod
    def from_config(cls, cfg: Any = None) -> "CostLog":
        """由「费用日志」配置构造（None → 关闭）。

        容量上限（mb 换算后的字节数）解析失败（TypeError/ValueError）按 0
        处理（= 不轮转），与现状一致。
        """
        enabled = bool(getattr(cfg, "enabled", False))
        path = getattr(cfg, "path", "")
        try:
            max_bytes = int(getattr(cfg, "max_bytes", 0) or 0)
        except (TypeError, ValueError):
            max_bytes = 0
        return cls(enabled=enabled, path=path, max_bytes=max_bytes)

    def append(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int,
        breakdown: tuple[float, float, float],
        total_cost: float,
    ) -> None:
        """追加一行费用记录（每次 LLM 请求调用一次；未启用时不动作）。

        先补写积压行再写当前行；任一 `OSError` 时把**当前行**入队并返回。
        """
        if not self._enabled:
            return
        input_cost, cache_cost, output_cost = breakdown
        row = [
            time.strftime("%Y-%m-%d %H:%M:%S"),
            model,
            prompt_tokens, cached_tokens, prompt_tokens - cached_tokens,
            completion_tokens,
            round(input_cost, COST_DECIMALS),
            round(cache_cost, COST_DECIMALS),
            round(output_cost, COST_DECIMALS),
            round(input_cost + cache_cost + output_cost, COST_DECIMALS),
            round(total_cost, COST_DECIMALS),
        ]
        try:
            while self._pending:
                self._write_row(self._pending[0])
                self._pending.pop(0)
            self._write_row(row)
        except OSError:
            self._pending.append(row)

    def _write_row(self, row: list[Any]) -> None:
        """写一行到 CSV；文件不存在/为空先写表头；写满先轮转。失败抛 OSError。"""
        path = self._path or DEFAULT_COST_LOG_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        need_header = False
        if size == 0:
            need_header = True
        elif self._max_bytes > 0 and size >= self._max_bytes:
            # 写满 → 当前文件转 _bak（旧备份被顶掉），新活动文件从表头开始
            need_header = self._rotate_to_bak(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            if need_header:
                writer.writerow(COST_LOG_HEADER)
            writer.writerow(row)

    def _rotate_to_bak(self, path: str) -> bool:
        """双文件轮转：写满的当前文件改名为「主名_bak.扩展名」（旧备份先删）。

        返回是否轮转成功（成功才为新文件写表头）；失败（如文件被办公软件
        独占）时文件保持原位，本次行继续追加到已超限的活动文件（尽力而为）。
        """
        try:
            root, ext = os.path.splitext(path)
            bak = f"{root}_bak{ext}"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(path, bak)
            return True
        except OSError:
            return False


@dataclass(frozen=True)
class StatsReadings:
    """统计栏读数（五项口径，供界面/自动保存/占比刷新消费）。"""

    input_tokens: int
    output_tokens: int
    cache_hit_ratio: float
    cost: float
    balance: float


class StatsTracker:
    """Token 统计与费用追踪器（模型、定价表、余额配置、成本日志配置构造注入）。"""

    def __init__(
        self,
        model: str,
        user_pricing: Optional[dict[str, dict[str, float]]] = None,
        balance_cfg: Any = None,
        cost_log_cfg: Any = None,
    ):
        self._model = model
        self._user_pricing = user_pricing
        self._balance_cfg = balance_cfg
        self._cost_log = CostLog.from_config(cost_log_cfg)
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_prompt_tokens = 0  # 累计输入（全程求和；快照输入另见 _total_input_tokens）
        self._total_cache_tokens = 0   # 累计缓存命中
        self._total_cost = 0.0
        self._balance_to_show = 0.0

    def set_model(self, model: str) -> None:
        """切换当前模型（费用按新模型定价查表；已累计费用不重算）。"""
        self._model = model

    def update(self, usage: dict) -> None:
        """更新一轮用量（每轮 LLM 请求返回后调用）。

        `usage`：`{"prompt_tokens": int, "completion_tokens": int, "cached_tokens": int}`。
        输入/输出 token 键缺失时抛错（不做防御性取值）；缓存命中键缺失按 0。
        """
        prompt = usage["prompt_tokens"]
        completion = usage["completion_tokens"]
        cached = usage.get("cached_tokens", 0)
        self._total_output_tokens += completion
        self._total_input_tokens = prompt  # 赋值：每轮已含全部历史（当前上下文快照）
        self._total_prompt_tokens += prompt
        self._total_cache_tokens += cached
        breakdown = cost_breakdown(
            self._model, prompt, completion, cached, self._user_pricing
        )
        self._total_cost += sum(breakdown)
        self._cost_log.append(
            self._model, prompt, completion, cached, breakdown, self._total_cost
        )

    def fetch_balance(self, api_key: str, round_num: int, interval: int = 10) -> None:
        """按轮次节奏查询余额（每 `interval` 轮一次；调用点归 app 主循环）。

        轮次号为间隔的整数倍且密钥非空时发起查询：成功刷新显示余额、失败保持
        上次值；其余情形（非整数倍或密钥为空）显示余额重置为 0.0。
        """
        if api_key and round_num % interval == 0:
            result = fetch_balance(api_key, self._balance_cfg)
            if result:
                self._balance_to_show = result["total"]
        else:
            self._balance_to_show = 0.0

    @property
    def input_tokens(self) -> int:
        """输入 token（最近一轮的值）。"""
        return self._total_input_tokens

    @property
    def output_tokens(self) -> int:
        """输出 token（全程累计）。"""
        return self._total_output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        """缓存命中率（0~1）：累计命中 / 累计输入，token 加权；分母 0 取 0.0。"""
        if self._total_prompt_tokens == 0:
            return 0.0
        return self._total_cache_tokens / self._total_prompt_tokens

    @property
    def cost(self) -> float:
        """费用（全程累计，人民币元）。"""
        return self._total_cost

    @property
    def balance(self) -> float:
        """余额（最近一次成功查询的值；0.0 同时表示未查询，兼容怪癖保持）。"""
        return self._balance_to_show

    def snapshot(self) -> StatsReadings:
        """统计栏读数快照（输入 / 输出 / 缓存命中率 / 费用 / 余额）。"""
        return StatsReadings(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_hit_ratio=self.cache_hit_ratio,
            cost=self.cost,
            balance=self.balance,
        )
