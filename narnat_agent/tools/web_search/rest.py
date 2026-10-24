"""通用 REST 联网适配（WebSearch 的 search / fetch 双接口）

不绑定任何厂商：POST 一个 JSON 到用户填的地址，再按用户指定的字段名从返回
JSON 里取值。所以只要某厂商的接口是「POST JSON → 返回 JSON」，填几个字段
就能用 —— 厂商差异是**配置**，不是**代码分支**。换一家服务商只改 narnat.json。

配置（narnat.json「联网搜索」段）:

  key / auth                顶层共用（两个接口共用同一把钥匙）
                            key 空 = 不加认证头
                            auth 空 = 不加认证头；"bearer" = Authorization: Bearer <key>；
                            "x-api-key" = X-API-Key；其它非空串 = 自定义头名
  http.search.url           搜索接口地址；缺失 = 未配置搜索能力（不报错）
  http.search.query_field   请求体里查询词的字段名，默认 "query"
  http.search.count_field   请求体里条数的字段名，默认 "count"；空串 = 不传
  http.search.args          附加固定参数（原样并入请求体）
  http.search.extra_params  模型可填的扩展参数声明（{参数名: {..., "field": 落库字段名}}，
                            翻译在 web_search/__init__.py 里做，本模块只吃翻译后的 extras）
  http.search.rail          响应解析：
                              {"list": "results", "title": "title", "url": "url",
                               "desc": ["snippet", "summary", "content"]}
                            list 支持点号路径（如 "data.items"）；空 = 响应本身是数组
                            desc 可写成列表，按顺序取第一个非空的
  http.fetch.url            抓取接口地址；缺失 = 未配置抓取能力（不报错）
  http.fetch.url_field      请求体里目标链接的字段名，默认 "url"
  http.fetch.args           附加固定参数
  http.fetch.rail           响应解析：{"title": "title", "content": "content"}

两个接口都缺失时才抛错；只配一个也能用，另一个 action 由调用层给出「未配置」提示。
"""

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

from ...mcp.client import McpError


_RAW_LIMIT = 200            # 错误原文/非 JSON 响应截断长度

DEFAULT_QUERY_FIELD = "query"
DEFAULT_COUNT_FIELD = "count"
DEFAULT_URL_FIELD = "url"
# 响应解析的默认值：标准形态 + 描述按 snippet→summary→content 依次兜底
DEFAULT_SEARCH_RAIL = {"list": "results",
                       "title": ("title",),
                       "url": ("url",),
                       "desc": ("snippet", "summary", "content")}
DEFAULT_FETCH_RAIL = {"title": ("title",),
                      "content": ("content",)}


@dataclass(frozen=True)
class SearchApi:
    """搜索接口的完整契约（地址 + 请求体字段名 + 响应解析）"""
    url: str
    query_field: str
    count_field: str                      # 空 = 不传
    extra_args: Dict                      # 附加固定参数
    list_path: str                        # 结果数组在响应里的路径；空 = 响应本身
    title_key: Tuple[str, ...]
    url_key: Tuple[str, ...]
    desc_keys: Tuple[str, ...]


@dataclass(frozen=True)
class FetchApi:
    """抓取接口的完整契约"""
    url: str
    url_field: str
    extra_args: Dict
    title_key: Tuple[str, ...]
    content_key: Tuple[str, ...]


@dataclass(frozen=True)
class Config:
    """一次 execute 解析出的联网配置"""
    headers: Dict[str, str]
    search: Optional[SearchApi]           # None = 未配置搜索
    fetch: Optional[FetchApi]             # None = 未配置抓取


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


def _auth_headers(key: str, auth: str) -> Dict[str, str]:
    """按认证方式构造请求头；key 为空 = 无鉴权服务器（不加认证头）"""
    if not key:
        return {}
    if auth.lower() == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if auth.lower() == "x-api-key":
        return {"X-API-Key": key}
    return {auth: key}          # 其它非空串 = 自定义头名


def _sub_object(node, name: str) -> Dict:
    """取子配置对象；缺失/不是对象 → 空 dict"""
    sub = node.get(name) if isinstance(node, dict) else None
    return sub if isinstance(sub, dict) else {}


def _read_args(sub: Dict, label: str) -> Dict:
    """读附加固定参数（必须是对象）"""
    raw = sub.get("args", {})
    if not isinstance(raw, dict):
        raise McpError(f'「{label}」必须是对象（如 {{"need_content": false}}）')
    return {str(k): v for k, v in raw.items()}


def _read_search(sub: Dict) -> Optional[SearchApi]:
    """search 子配置 → SearchApi；没有 url = 未配置（返回 None，不报错）"""
    url = str(sub.get("url") or "").strip()
    if not url:
        return None

    query_field = str(sub.get("query_field") or "").strip() or DEFAULT_QUERY_FIELD

    # 未配置（None）= 默认字段名；显式空串 = 不传（与 rail.list 同一语义）
    raw_count = sub.get("count_field")
    count_field = DEFAULT_COUNT_FIELD if raw_count is None else str(raw_count).strip()

    rail = _sub_object(sub, "rail")
    raw_list = rail.get("list")
    list_path = DEFAULT_SEARCH_RAIL["list"] if raw_list is None else str(raw_list).strip()

    return SearchApi(
        url=url,
        query_field=query_field,
        count_field=count_field,
        extra_args=_read_args(sub, "搜索·附加参数"),
        list_path=list_path,
        title_key=_as_str_tuple(rail.get("title"), DEFAULT_SEARCH_RAIL["title"]),
        url_key=_as_str_tuple(rail.get("url"), DEFAULT_SEARCH_RAIL["url"]),
        desc_keys=_as_str_tuple(rail.get("desc"), DEFAULT_SEARCH_RAIL["desc"]),
    )


def _read_fetch(sub: Dict) -> Optional[FetchApi]:
    """fetch 子配置 → FetchApi；没有 url = 未配置（返回 None，不报错）"""
    url = str(sub.get("url") or "").strip()
    if not url:
        return None

    rail = _sub_object(sub, "rail")
    return FetchApi(
        url=url,
        url_field=str(sub.get("url_field") or "").strip() or DEFAULT_URL_FIELD,
        extra_args=_read_args(sub, "抓取·附加参数"),
        title_key=_as_str_tuple(rail.get("title"), DEFAULT_FETCH_RAIL["title"]),
        content_key=_as_str_tuple(rail.get("content"), DEFAULT_FETCH_RAIL["content"]),
    )


def read_config(keys: Dict) -> Config:
    """按 narnat.json「联网搜索」段读配置（认证头在这里统一构造）

    只要求「至少配了一个接口」：缺失的那个能力留给调用层给出「未配置」提示，
    这样用户只配搜索也能正常用 search，不会被 fetch 缺失挡住。
    """
    keys = keys if isinstance(keys, dict) else {}
    http = _sub_object(keys, "http")
    headers = _auth_headers(str(keys.get("key") or ""), str(keys.get("auth") or "").strip())
    search = _read_search(_sub_object(http, "search"))
    fetch = _read_fetch(_sub_object(http, "fetch"))
    if search is None and fetch is None:
        raise McpError("联网搜索未配置：narnat.json「联网搜索」→ "
                       "http.search.url 和/或 http.fetch.url")
    return Config(headers=headers, search=search, fetch=fetch)


def build_search_body(api: SearchApi, query: str, num: int, extras: Dict) -> Dict:
    """组装搜索请求体：查询词 + 数量（count_field 为空则不传）+ 附加/扩展参数

    extras 是本次调用要并入的厂商扩展参数（已按 extra_params.field 翻译成最终
    字段名），用 setdefault 语义并入，不覆盖查询词与条数字段。
    """
    body: Dict = {api.query_field: query}
    if api.count_field:
        body[api.count_field] = num
    for k, v in api.extra_args.items():
        body.setdefault(k, v)
    reserved = {api.query_field, api.count_field}
    for k, v in (extras or {}).items():
        if k in reserved:
            continue
        body.setdefault(k, v)
    return body


def build_fetch_body(api: FetchApi, url: str) -> Dict:
    """组装抓取请求体：目标链接 + 附加固定参数"""
    body: Dict = {api.url_field: url}
    for k, v in api.extra_args.items():
        body.setdefault(k, v)
    return body


def _post(url: str, body: Dict, headers: Dict[str, str], timeout: float) -> str:
    """POST JSON，返回响应文本；HTTP/网络/超时错误抛 McpError（两条路径共用）"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        raise McpError(f"请求超时({timeout:g}s)")
    except httpx.HTTPError as e:
        raise McpError(f"请求失败: {e}")
    if not 200 <= resp.status_code < 300:
        raise McpError(_http_error(resp.status_code, resp.text))
    return resp.text


def search(api: SearchApi, headers: Dict[str, str], query: str, num: int,
           extras: Dict, timeout: float) -> str:
    """调用搜索接口，返回响应文本"""
    return _post(api.url, build_search_body(api, query, num, extras), headers, timeout)


def fetch(api: FetchApi, headers: Dict[str, str], url: str,
          timeout: float) -> str:
    """调用抓取接口，返回响应文本"""
    return _post(api.url, build_fetch_body(api, url), headers, timeout)


def server_error(payload) -> str:
    """服务端 error 体 → 可读 message；不是错误体/取不到 message 时返回空串

    防的是「HTTP 200 但正文是 {"error": {...}}」被当搜索结果/正文。
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


def to_results(text: str, api: SearchApi) -> List[Dict]:
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

    node = _dig(data, api.list_path)
    items = node if isinstance(node, list) else None

    results: List[Dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = _pick(item, api.url_key)
        if not url:
            continue
        results.append({"title": _pick(item, api.title_key) or url,
                        "url": url,
                        "description": _pick(item, api.desc_keys)})
    return results


def to_page(text: str, api: FetchApi) -> Tuple[str, str]:
    """响应文本 → (title, content)；非 JSON / 服务端 error 抛 McpError"""
    if not text.strip():
        return ("", "")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise McpError(f"响应非JSON({text[:_RAW_LIMIT]})")

    detail = server_error(data)
    if detail:
        raise McpError(f"服务端错误: {detail}")

    node = data if isinstance(data, dict) else {}
    return _pick(node, api.title_key), _pick(node, api.content_key)
