"""
余额查询和费用计算

纯配置驱动：
- 定价：只从用户配置读取，无代码内置默认值。未配置定价的模型不计算费用。
- 余额查询URL：只从用户配置读取，未配置则不查询。
- 统一使用 httpx，无 requests 依赖。
"""

import httpx
from typing import Optional, Dict, Any


def get_pricing(model: str, user_pricing: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[Dict[str, float]]:
    """获取指定模型的定价。未配置则返回None（不计算费用）。"""
    if user_pricing and model in user_pricing:
        return user_pricing[model]
    return None


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    user_pricing: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    """计算本轮费用（元）。未配置定价则返回0。

    prompt_tokens 包含缓存部分，实际计费:
      - (prompt_tokens - cached_tokens) * input_price
      - cached_tokens * cache_hit_price
      - completion_tokens * output_price
    """
    p = get_pricing(model, user_pricing)
    if p is None:
        return 0.0
    uncached = prompt_tokens - cached_tokens
    return (
        uncached * p["input"] +
        cached_tokens * p["cache_hit"] +
        completion_tokens * p["output"]
    ) / 1_000_000


def fetch_balance(api_key: str, balance_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """查询账户余额。未配置balance_url则返回None。

    返回:
        {"total": float, "granted": float, "topped_up": float, "currency": str}
        失败或未配置返回 None
    """
    if not balance_url:
        return None
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(
                balance_url,
                headers={"Authorization": f"Bearer {api_key}"},
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
    except Exception:
        return None
