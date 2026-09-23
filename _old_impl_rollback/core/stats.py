"""Token统计和费用追踪

从agent.py中提取，负责token计数、费用计算、余额查询。
配置"费用日志"开启时，每次LLM请求追加一行到CSV（与界面费用同口径）。
"""

import csv
import os
import time

from .billing import calculate_cost, cost_breakdown, fetch_balance

# 费用日志CSV表头
COST_LOG_HEADER = [
    "时间", "模型",
    "输入tokens", "缓存命中tokens", "缓存未命中tokens",
    "输出tokens(含思考)",           # 服务端usage不分拆思考token，思考与正文合并计输出
    "输入费用(元)", "缓存费用(元)", "输出费用(元)",
    "本次费用(元)", "累计费用(元)",
]


class StatsTracker:
    """Token统计和费用追踪器"""

    def __init__(self, model: str, user_pricing=None,
                 balance_cfg=None, cost_log_cfg=None):
        self._model = model
        self._user_pricing = user_pricing
        self._balance_cfg = balance_cfg
        # cost_log_cfg 缺省(None)时各字段取默认值 → 功能关闭，与原行为一致
        self._cost_log_enabled = bool(getattr(cost_log_cfg, "enabled", False))
        self._cost_log_path = getattr(cost_log_cfg, "path", "")
        try:
            self._cost_log_max_bytes = int(getattr(cost_log_cfg, "max_bytes", 0) or 0)
        except (TypeError, ValueError):
            self._cost_log_max_bytes = 0
        self._cost_log_pending = []  # 写失败时缓存的行，下次写入前自动补写
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_prompt_tokens = 0  # 累计输入token（所有轮次求和）
        self._total_cache_tokens = 0   # 累计缓存命中token
        self._total_cost = 0.0
        self._balance_to_show = 0.0

    def update(self, usage: dict) -> None:
        """更新token统计（每轮LLM返回后调用）

        usage: {"prompt_tokens": int, "completion_tokens": int, "cached_tokens": int}
        """
        prompt = usage["prompt_tokens"]
        self._total_output_tokens += usage["completion_tokens"]
        self._total_input_tokens = prompt  # 赋值：每轮已含全部历史（当前上下文快照）
        # 累计：用于计算全程 token 加权缓存命中率（小轮次不会被等权放大）
        self._total_prompt_tokens += prompt
        self._total_cache_tokens += usage.get("cached_tokens", 0)
        self._total_cost += calculate_cost(
            self._model,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage.get("cached_tokens", 0),
            self._user_pricing,
        )
        # 费用日志（配置开启时）：与上面同一分项口径落盘
        if self._cost_log_enabled:
            self._append_cost_log(
                usage["prompt_tokens"],
                usage["completion_tokens"],
                usage.get("cached_tokens", 0),
            )

    def _append_cost_log(self, prompt: int, completion: int, cached: int) -> None:
        """追加一行费用记录到CSV（每次LLM请求调用一次）。

        写入失败（如文件被 WPS/Excel 独占）不丢行：缓存到内存队列，
        下次写入前自动补写；失败期间累计费用仍准确（按内存累计）。
        """
        uncached = prompt - cached
        inp_cost, cache_cost, out_cost = cost_breakdown(
            self._model, prompt, completion, cached, self._user_pricing
        )
        row = [
            time.strftime("%Y-%m-%d %H:%M:%S"),
            self._model,
            prompt, cached, uncached, completion,
            round(inp_cost, 9), round(cache_cost, 9), round(out_cost, 9),
            round(inp_cost + cache_cost + out_cost, 9),
            round(self._total_cost, 9),
        ]
        # 先补写积压行，再写当前行。写成功一行立即出队，失败即停：
        # 未写出的积压行与当前行留到下次再试，保证不丢行也不重复写。
        try:
            while self._cost_log_pending:
                self._write_cost_log_row(self._cost_log_pending[0])
                self._cost_log_pending.pop(0)
            self._write_cost_log_row(row)
        except OSError:
            self._cost_log_pending.append(row)

    def _write_cost_log_row(self, row: list) -> None:
        """写一行到CSV；文件不存在/为空写表头；写满自动轮转 _bak。失败抛OSError。"""
        path = self._cost_log_path or os.path.join(".narnat", "data", "cost_log.csv")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        need_header = False
        if size == 0:
            # 新文件（含轮转后新建）→ 写表头
            need_header = True
        elif self._cost_log_max_bytes > 0 and size >= self._cost_log_max_bytes:
            # 写满 → 当前文件转 _bak（旧 _bak 删除），新建活动文件继续写
            need_header = self._rotate_to_bak(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if need_header:
                writer.writerow(COST_LOG_HEADER)
            writer.writerow(row)

    def _rotate_to_bak(self, path: str) -> bool:
        """双文件轮转：写满的当前文件改名为「主名_bak.扩展名」，并删除旧的 _bak。

        磁盘上始终只有 1 个活动文件 + 1 个备份文件：
        活动文件写满 → 转备份（旧的备份被顶掉），再新建活动文件从表头开始写。
        返回是否轮转成功（成功才需为新文件写表头）。
        """
        try:
            root, ext = os.path.splitext(path)
            bak = f"{root}_bak{ext}"
            if os.path.exists(bak):
                os.remove(bak)      # 删除旧的 _bak
            os.replace(path, bak)   # 当前写满的文件 → _bak
            return True
        except OSError:
            # 轮转失败（如文件被 WPS/Excel 独占）不中断写入：
            # 本次行继续追加到已超限的活动文件（尽力而为，不再写表头）
            return False

    def fetch_balance(self, api_key: str, round_num: int, interval: int = 10) -> None:
        """每N轮查询一次余额，其余轮次保留上次查询结果"""
        if api_key and round_num % interval == 0:
            bal = fetch_balance(api_key, self._balance_cfg)
            if bal:
                self._balance_to_show = bal["total"]
        else:
            self._balance_to_show = 0.0

    @property
    def input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def output_tokens(self) -> int:
        return self._total_output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        """全程累计缓存命中率（0~1）：累计命中token / 累计输入token，token加权"""
        if self._total_prompt_tokens == 0:
            return 0.0
        return self._total_cache_tokens / self._total_prompt_tokens

    @property
    def cost(self) -> float:
        return self._total_cost

    @property
    def balance(self) -> float:
        return self._balance_to_show
