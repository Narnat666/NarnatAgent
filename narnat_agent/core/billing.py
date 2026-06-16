"""余额查询和费用计算

定价支持从配置读取，代码中保留默认定价表作为fallback。
余额查询URL支持从配置读取。
"""
import requests
import json
from typing import Optional, Dict, Any


# 默认定价（元/百万tokens）—— 代码内置fallback
_DEFAULT_PRICING = {
    "deepseek-v4-pro": {
        "input": 3.0,
        "cache_hit": 0.025,
        "output": 6.0,
    },
    "deepseek-v4-flash": {
        "input": 1.0,
        "cache_hit": 0.02,
        "output": 2.0,
    },
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

# 默认余额查询地址
_DEFAULT_BALANCE_URL = "https://api.deepseek.com/user/balance"


def get_pricing(model: str, user_pricing: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, float]:
    """获取指定模型的定价

    优先使用用户配置的定价，fallback到代码默认值。
    """
    if user_pricing and model in user_pricing:
        return user_pricing[model]
    return _DEFAULT_PRICING.get(model, _DEFAULT_PRICING["deepseek-v4-pro"])


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    user_pricing: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    """计算本轮费用（元）

    prompt_tokens 包含缓存部分，实际计费:
      - (prompt_tokens - cached_tokens) * input_price
      - cached_tokens * cache_hit_price
      - completion_tokens * output_price
    """
    p = get_pricing(model, user_pricing)
    uncached = prompt_tokens - cached_tokens
    cost = (
        uncached * p["input"] +
        cached_tokens * p["cache_hit"] +
        completion_tokens * p["output"]
    ) / 1_000_000
    return cost


def fetch_balance(api_key: str, balance_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """查询账户余额

    Args:
        api_key: API密钥
        balance_url: 余额查询地址，不传则用默认DeepSeek地址

    返回:
        {
            "total": float,
            "granted": float,
            "topped_up": float,
            "currency": str,
        }
        失败返回 None
    """
    url = balance_url or _DEFAULT_BALANCE_URL
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(3, 5),
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
