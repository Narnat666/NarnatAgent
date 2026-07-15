"""
余额查询和费用计算

纯配置驱动：
- 定价：只从用户配置读取，无代码内置默认值。未配置定价的模型不计算费用。
- 余额查询：通过 BalanceConfig 驱动，使用 JSONPath 通用解析任意厂商的响应格式。
- 统一使用 httpx，无 requests 依赖。
"""

import httpx
from typing import Optional, Dict, Any

# 导入 BalanceConfig 类型（避免循环导入，仅用于类型标注）
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..config.loader import BalanceConfig


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


def _resolve_jsonpath(data: Any, path: str) -> Any:
    """解析简单的 JSONPath（点号分隔，支持数字数组索引）。

    例: "balance_infos.0.total_balance" → data["balance_infos"][0]["total_balance"]
    """
    if not path:
        return None
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def fetch_balance(api_key: str, balance_cfg=None) -> Optional[Dict[str, Any]]:
    """通用余额查询。根据 BalanceConfig 动态构造请求并解析响应。

    Args:
        api_key: API 密钥
        balance_cfg: BalanceConfig 对象（或兼容的 dict/对象）

    Returns:
        {"total": float, "currency": str} 或 None
    """
    if balance_cfg is None:
        return None

    # 兼容 dict / dataclass 两种传入方式
    if isinstance(balance_cfg, dict):
        enabled = bool(balance_cfg.get("enabled", False))
        url = balance_cfg.get("url", "")
        auth_method = balance_cfg.get("auth_method", "bearer")
        value_path = balance_cfg.get("value_path", "")
        currency_path = balance_cfg.get("currency_path", "")
    else:
        enabled = getattr(balance_cfg, "enabled", False)
        url = getattr(balance_cfg, "url", "")
        auth_method = getattr(balance_cfg, "auth_method", "bearer")
        value_path = getattr(balance_cfg, "value_path", "")
        currency_path = getattr(balance_cfg, "currency_path", "")

    if not enabled or not url:
        return None

    try:
        headers = {}
        if auth_method == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=8.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                return None
            data = r.json()

        total = _resolve_jsonpath(data, value_path)
        if total is None:
            return None

        result: Dict[str, Any] = {"total": float(total)}
        if currency_path:
            currency = _resolve_jsonpath(data, currency_path)
            if currency is not None:
                result["currency"] = str(currency)
        return result
    except Exception:
        return None
