"""Token统计和费用追踪

从agent.py中提取，负责token计数、费用计算、余额查询。
"""

from .billing import calculate_cost, fetch_balance


class StatsTracker:
    """Token统计和费用追踪器"""

    def __init__(self, model: str, user_pricing=None,
                 balance_cfg=None):
        self._model = model
        self._user_pricing = user_pricing
        self._balance_cfg = balance_cfg
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cache_tokens = 0
        self._total_cost = 0.0
        self._balance_to_show = 0.0

    def update(self, usage: dict) -> None:
        """更新token统计（每轮LLM返回后调用）

        usage: {"prompt_tokens": int, "completion_tokens": int, "cached_tokens": int}
        """
        self._total_output_tokens += usage["completion_tokens"]
        self._total_input_tokens = usage["prompt_tokens"]  # 赋值：每轮已含全部历史
        self._total_cache_tokens = usage.get("cached_tokens", 0)  # 赋值：每轮是当前快照
        self._total_cost += calculate_cost(
            self._model,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage.get("cached_tokens", 0),
            self._user_pricing,
        )

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
    def cache_tokens(self) -> int:
        return self._total_cache_tokens

    @property
    def cost(self) -> float:
        return self._total_cost

    @property
    def balance(self) -> float:
        return self._balance_to_show
