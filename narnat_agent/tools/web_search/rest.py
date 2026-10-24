"""通用 REST 搜索适配（provider="http"）

不绑定任何厂商：POST 一个 JSON 到用户填的地址，从返回 JSON 里按用户指定的
字段名取出 {title, url, 描述} 列表。所以只要某厂商的搜索接口是「POST JSON →
返回 JSON」，在界面上填几个字段就能用，不需要动代码。

## 为什么不再按厂商写死

原实现有 deepseek.py，把「DeepSeek 官方搜索」的端点、字段名、鉴权方式全写在
代码里。后果是：换一家厂商就得改代码、发版；而且 provider 枚举里只有
mcp / deepseek 两个值，用户没有第三种选择。MCP 的价值就在于「适配任意厂商」，
REST 侧也应如此 —— 厂商差异是**配置**，不是**代码分支**。

配置（narnat.json「联网搜索」的 http 子对象）:
  url          必填，接口地址
  auth         认证方式：bearer / x-api-key / 自定义头名；空 = 不加认证头
  key          密钥（在顶层，两种接入方式共用）
  query_field  请求体里查询词的字段名，默认 "query"
  count_field  请求体里结果数量的字段名，默认 "count"；空串 = 不传
  args         附加固定参数（原样并入请求体）
  rail         响应解析：数组路径 + 三个字段名
                 {"list": "results", "title": "title", "url": "url", "desc": "snippet"}
               list 支持点号路径（如 "data.items"）；空 = 响应本身是数组
               desc 可写成列表，按顺序取第一个非空的（如 ["snippet","summary","content"]）
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import httpx

from ...mcp.client import McpError


_RAW_LIMIT = 200            # 错误原文/非 JSON 响应截断长度

DEFAULT_QUERY_FIELD = "query"
DEFAULT_COUNT_FIELD = "count"
# 响应解析的默认值：标准形态 + 描述按 snippet→summary→content 依次兜底
DEFAULT_RAIL = {"list": "results",
                "title": ("title",),
                "url": ("url",),
                "desc": ("snippet", "summary", "content")}


@dataclass(frozen=True)
class Config:
    """一次 execute 解析出的 REST 搜索配置"""
    url: str
    headers: Dict[str, str]
    query_field: str
    count_field: str                      # 空 = 不传
    extra_args: Dict = field(default_factory=dict)
    list_path: str = ""                   # 结果数组在响应里的路径；空 = 响应本身
    title_key: Tuple[str, ...] = ("title",)
    url_key: Tuple[str, ...] = ("url",)
    desc_keys: Tuple[str, ...] = DEFAULT_RAIL["desc"]


def _as_str_tuple(value, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    """字段名归一化成字符串元组：接受字符串或列表，空/非法回退默认

    用元组是因为描述字段要「按顺序取第一个非空」（snippet→summary→content）。
    """
    if isinstance(value, str):
        v = value.strip()
        return (v,) if v else fallback
    if isinstance(value, list):
        items = tuple(str(x).strip() for x in value if str(x).strip())
        return items or fallback
    return fallback


def read_config(keys: Dict, headers: Dict[str, str]) -> Config:
    """按 narnat.json「联网搜索」读 REST 配置；只读 `http` 子对象

    headers 由调用方按 auth/key 构造好传进来（认证逻辑与 MCP 路径共用一份）。
    """
    sub = keys.get("http")
    if not isinstance(sub, dict):
        sub = {}

    url = str(sub.get("url") or "").strip()
    if not url:
        raise McpError('接入方式为 REST 时必须在「接口地址」填入地址'
                       '（narnat.json「联网搜索」→ http.url）')

    query_field = str(sub.get("query_field") or "").strip() or DEFAULT_QUERY_FIELD

    raw_count = sub.get("count_field")
    count_field = DEFAULT_COUNT_FIELD if raw_count is None else str(raw_count).strip()

    raw_args = sub.get("args", {})
    if not isinstance(raw_args, dict):
        raise McpError('「附加参数」必须是对象（如 {"need_content": false}）')

    rail = sub.get("rail")
    if not isinstance(rail, dict):
        rail = {}

    # 未配置（None）= 用默认数组路径；显式空串 = 响应本身就是数组。
    # 与 count_field 同一语义：缺省取默认值，写空才表示「不传」。
    raw_list = rail.get("list")
    list_path = DEFAULT_RAIL["list"] if raw_list is None else str(raw_list).strip()

    return Config(
        url=url,
        headers=headers,
        query_field=query_field,
        count_field=count_field,
        extra_args={str(k): v for k, v in raw_args.items()},
        list_path=list_path,
        title_key=_as_str_tuple(rail.get("title"), DEFAULT_RAIL["title"]),
        url_key=_as_str_tuple(rail.get("url"), DEFAULT_RAIL["url"]),
        desc_keys=_as_str_tuple(rail.get("desc"), DEFAULT_RAIL["desc"]),
    )


def build_body(cfg: Config, query: str, num: int) -> Dict:
    """组装请求体：查询词 + 数量（count_field 为空则不传）+ 附加参数"""
    body: Dict = {cfg.query_field: query}
    if cfg.count_field:
        body[cfg.count_field] = num
    for k, v in cfg.extra_args.items():
        body.setdefault(k, v)
    return body


def search(cfg: Config, query: str, num: int, timeout: float) -> str:
    """POST 到用户配置的地址，返回响应文本；HTTP/网络/超时错误抛 McpError"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(cfg.url, json=build_body(cfg, query, num),
                               headers=cfg.headers)
    except httpx.TimeoutException:
        raise McpError(f"请求超时({timeout:g}s)")
    except httpx.HTTPError as e:
        raise McpError(f"请求失败: {e}")
    if not 200 <= resp.status_code < 300:
        raise McpError(_http_error(resp.status_code, resp.text))
    return resp.text


def server_error(payload) -> str:
    """服务端 error 体 → 可读 message；不是错误体/取不到 message 时返回空串

    防的是「HTTP 200 但正文是 {"error": {...}}」被当搜索结果。MCP 与 REST
    两条解析路径共用此判定，全仓只有这一处实现。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "").strip()
    if isinstance(err, str):
        return err.strip()
    return ""


def _http_error(status: int, text: str) -> str:
    """非 2xx 响应的错误文本：优先服务端 error.message，无则原文截断"""
    detail = server_error(text) or text.strip()[:_RAW_LIMIT]
    return f"HTTP {status}: {detail}" if detail else f"HTTP {status}"


def _dig(node, path: str):
    """按点号路径取子节点（"data.items"）；路径为空返回自身"""
    if not path:
        return node
    cur = node
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _pick(node: Dict, keys: Tuple[str, ...]) -> str:
    """按顺序取第一个非空字符串字段（字段名可配，故不写死 key 名）"""
    for name in keys:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def to_results(text: str, cfg: Config) -> List[Dict]:
    """响应文本 → [{title, url, description}]；非 JSON / 服务端 error 抛 McpError

    只有在服务端真返回 2xx 且无 error 时才算「搜索结果」——error 体绝不能被
    渲染成结果正文（失败语义必须保留）。
    """
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise McpError(f"响应非JSON({text[:_RAW_LIMIT]})")

    detail = server_error(data)
    if detail:
        raise McpError(f"服务端错误: {detail}")

    node = _dig(data, cfg.list_path)
    items = node if isinstance(node, list) else None

    results: List[Dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = _pick(item, cfg.url_key)
        if not url:
            continue
        results.append({"title": _pick(item, cfg.title_key) or url,
                        "url": url,
                        "description": _pick(item, cfg.desc_keys)})
    return results
