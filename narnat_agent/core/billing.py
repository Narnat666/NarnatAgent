"""DeepSeek 余额查询和费用计算"""
import requests
import json
from typing import Optional, Dict, Any


# 定价（元/百万tokens）
PRICING = {
    "deepseek-v4-pro": {
        "input": 3.0,       # 输入（缓存未命中）
        "cache_hit": 0.025, # 输入（缓存命中）
        "output": 6.0,      # 输出
    },
    "deepseek-v4-flash": {
        "input": 1.0,
        "cache_hit": 0.02,
        "output": 2.0,
    },
    # 旧模型别名
    "deepseek-chat": {
        "input": 1.0,
        "cache_hit": 0.02,
        "output": 2.0,
    },
    "deepseek-reasoner": {
        "input": 1.0,
        "cache_hit": 0.02,
        "output": 2.0,
    },
}


def get_pricing(model: str) -> Dict[str, float]:
    """获取指定模型的定价，找不到则用 v4-pro 默认"""
    return PRICING.get(model, PRICING["deepseek-v4-pro"])


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
) -> float:
    """计算本轮费用（元）

    prompt_tokens 包含缓存部分，实际计费:
      - (prompt_tokens - cached_tokens) * input_price
      - cached_tokens * cache_hit_price
      - completion_tokens * output_price
    """
    p = get_pricing(model)
    uncached = prompt_tokens - cached_tokens
    cost = (
        uncached * p["input"] +
        cached_tokens * p["cache_hit"] +
        completion_tokens * p["output"]
    ) / 1_000_000
    return cost


def fetch_balance(api_key: str) -> Optional[Dict[str, Any]]:
    """查询 DeepSeek 账户余额

    返回:
        {
            "total": float,         # 总可用余额
            "granted": float,       # 赠金余额
            "topped_up": float,     # 充值余额
            "currency": str,        # CNY 或 USD
        }
        失败返回 None
    """
    try:
        r = requests.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(3, 5),  # (connect, read) 超时
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("is_available"):
            return None
        infos = data.get("balance_infos", [])
        if not infos:
            return None
        info = infos[0]
        return {
            "total": float(info["total_balance"]),
            "granted": float(info["granted_balance"]),
            "topped_up": float(info["topped_up_balance"]),
            "currency": info["currency"],
        }
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None
