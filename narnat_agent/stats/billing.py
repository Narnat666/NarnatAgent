"""费用计算与余额查询 —— 纯计算（无状态），配置以鸭子类型传入。

契约来源：
- `openspec/changes/recast-v2/specs/stats/spec.md`「费用计算（定价表）」：仅按用户
  配置的定价表（每百万 token 单价，模型名为键，分项为输入/缓存命中/输出）计算；
  本轮费用 = (输入 − 缓存命中)×输入单价 + 缓存命中×缓存单价 + 输出×输出单价；
  未配置定价的模型计为 0；
- 「余额查询」：配置驱动（仅当启用且查询地址非空时发起）；`bearer`（默认）走
  `Authorization: Bearer <密钥>`、`x-api-key` 走 `x-api-key` 头；HTTP GET、超时
  8 秒；非 200、JSON 解析失败、路径取值失败、数值转换失败等任何异常静默为
  「无结果」；响应/货币路径为点号分隔的 JSONPath（支持数字数组索引）；
- 「兼容性怪癖保持」：定价表缺分项键时抛错传播（不静默兜底）；缓存命中大于
  输入时的负数不钳制（口径由调用方使用，见 tracker 的日志列）。

职责边界（design D7）：本模块只做纯计算与单次 HTTP 调用，不承载任何状态；
配置对象按属性（或字典）读取，不依赖 config 积木的具体类型。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

__all__ = [
    "calculate_cost",
    "cost_breakdown",
    "fetch_balance",
    "get_pricing",
    "resolve_jsonpath",
]

# 余额查询超时（秒）——已发布口径
BALANCE_TIMEOUT_SECONDS = 8.0


def get_pricing(
    model: str,
    user_pricing: Optional[dict[str, dict[str, float]]] = None,
) -> Optional[dict[str, float]]:
    """取模型定价；未配置（或定价表为空）返回 None（该模型不计费用）。"""
    if user_pricing and model in user_pricing:
        return user_pricing[model]
    return None


def cost_breakdown(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    user_pricing: Optional[dict[str, dict[str, float]]] = None,
) -> tuple[float, float, float]:
    """本轮费用分项（元）：(输入费用, 缓存费用, 输出费用)。

    未配置定价的模型返回全 0；`prompt_tokens` 含缓存部分，输入费用按
    `(输入 − 缓存命中)` 计。定价表缺分项键时 KeyError 传播（兼容怪癖）。
    """
    pricing = get_pricing(model, user_pricing)
    if pricing is None:
        return (0.0, 0.0, 0.0)
    uncached = prompt_tokens - cached_tokens
    return (
        uncached * pricing["input"] / 1_000_000,
        cached_tokens * pricing["cache_hit"] / 1_000_000,
        completion_tokens * pricing["output"] / 1_000_000,
    )


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    user_pricing: Optional[dict[str, dict[str, float]]] = None,
) -> float:
    """本轮费用（元）= 三个分项之和；未配置定价则为 0。"""
    return sum(
        cost_breakdown(model, prompt_tokens, completion_tokens, cached_tokens, user_pricing)
    )


def resolve_jsonpath(data: Any, path: str) -> Any:
    """解析点号分隔的简易 JSONPath（支持数字数组索引）；取不到返回 None。

    例：`balance_infos.0.total_balance` → `data["balance_infos"][0]["total_balance"]`。
    空路径、键缺失、索引越界/非数字索引、标量中途取值均返回 None（静默）。
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


def _read_field(cfg: Any, name: str, default: Any) -> Any:
    """读配置字段：兼容对象属性与字典两种传入形态（缺字段取默认值）。"""
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def fetch_balance(api_key: str, balance_cfg: Any = None) -> Optional[dict[str, Any]]:
    """通用余额查询：按配置构造请求并解析响应；任何失败静默为 None。

    - 未配置（None）、未启用、查询地址为空 → 不发起请求，返回 None；
    - 认证：`auth_method == "x-api-key"` 用 `x-api-key` 头，其余（含缺省
      `bearer`）用 `Authorization: Bearer <密钥>`；
    - 取值：响应路径命中且可转 float 时返回 `{"total": float[, "currency": str]}`
      （货币路径命中时附带）；其余情形（非 200、JSON 解析失败、路径未命中、
      数值转换失败、网络异常）一律返回 None。
    """
    if balance_cfg is None:
        return None
    enabled = bool(_read_field(balance_cfg, "enabled", False))
    url = _read_field(balance_cfg, "url", "")
    if not enabled or not url:
        return None
    auth_method = _read_field(balance_cfg, "auth_method", "bearer")
    value_path = _read_field(balance_cfg, "value_path", "")
    currency_path = _read_field(balance_cfg, "currency_path", "")

    try:
        headers: dict[str, str] = {}
        if auth_method == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=BALANCE_TIMEOUT_SECONDS) as client:
            response = client.get(url, headers=headers)
            if response.status_code != 200:
                return None
            data = response.json()

        total = resolve_jsonpath(data, value_path)
        if total is None:
            return None

        result: dict[str, Any] = {"total": float(total)}
        if currency_path:
            currency = resolve_jsonpath(data, currency_path)
            if currency is not None:
                result["currency"] = str(currency)
        return result
    except Exception:
        return None
