"""DeepSeek 官方联网搜索适配（provider="deepseek"）

POST <url>（默认 DEFAULT_URL），鉴权固定 `Authorization: Bearer <key>`。
请求体只放 DeepSeek 支持的字段（query / count / need_content / ori_question /
localization），字段名 count 可由配置 count_param 改写；响应
`{"results": [{title, url, snippet, summary, content, rerank_score}]}` 转成与
MCP 路径渲染入参同构的 [{title, url, description}]，交回 __init__ 的 _render
系列统一出最终文本（格式/相关性排序/条数控制与 MCP 路径一致）。

配置（narnat.json「联网搜索」，provider="deepseek" 时）:
  - key:         必填，DeepSeek API Key；空则报错（auth 字段在此模式下忽略，
                 因为 DeepSeek 只支持 Bearer）
  - url:         可选，默认 DEFAULT_URL
  - count_param: 可选，请求体里的结果数量字段名，默认 "count"；空串 = 不传
  - args:        可选，need_content(bool，默认 false；JSON 里写成 "false"/"0"/"off"
                 等字符串也按语义解析，与 config.loader 的布尔解析同源) /
                 ori_question / localization
"""

import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

import httpx

from ...config.loader import _coerce_bool
from ...mcp.client import McpError


DEFAULT_URL = "https://api.deepseek.com/v1/tools/search"
DEFAULT_COUNT_PARAM = "count"
COUNT_MIN = 1
COUNT_MAX = 50              # DeepSeek count 取值范围
_RAW_LIMIT = 200            # 错误原文/非JSON响应截断长度（与 McpHttpClient 同量级）

# 摘要优先顺序：snippet 最贴查询，summary 次之，content 仅在前两者都空时兜底
_DESC_KEYS = ("snippet", "summary", "content")

# args 布尔开关的真词表：与 config.loader._coerce_bool 的真词表逐项一致（同源）
_TRUTHY_WORDS = frozenset(("true", "1", "yes", "on", "t", "y", "是"))


@dataclass(frozen=True)
class Config:
    """一次 execute 解析出的 DeepSeek 搜索配置"""
    url: str
    key: str
    count_param: str          # 请求体数量字段名；空 = 不传
    need_content: bool
    ori_question: str
    localization: str


def _coerce_flag(value, default: bool = False) -> bool:
    """args 布尔开关（need_content）解析：复用 config.loader._coerce_bool 的语义

    真/假词表与 loader 同源（真词表 _TRUTHY_WORDS 逐项对齐，假词表即「不在真词表
    里的字符串」），非字符串值直接交 _coerce_bool，故 bool/数字/None 的结果与
    loader 完全相同。唯一差别：loader 对认不出的值兜底 bool(value)——"abc" 与非空
    容器都会变成 True，那正是 need_content 写成 "abc" 反而打开正文抓取的病灶
    （既费配额又把网页正文灌进上下文）。开关默认是关，故此处认不出的字符串一律
    fail-closed 到 False（不看 default）；只有非字符串值（bool/数字/None）以及
    非标量容器才走 default。
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_WORDS
    if isinstance(value, (bool, int, float)) or value is None:
        return _coerce_bool(value, default)
    return default


def _text_arg(raw_args: Dict, name: str) -> str:
    """args 文本字段（ori_question/localization）：只认字符串/数字，其它类型丢弃

    原写法 str(raw_args.get(name) or "") 会把 {"a": 1}、["x"] 这类误配 str() 成
    "{'a': 1}" 原样塞进请求体（与 need_content 同一类「类型没校验」问题）；
    认不出的类型按未配置处理（不传该字段）。
    """
    value = raw_args.get(name)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def read_config(keys: Dict) -> Config:
    """按 narnat.json「联网搜索」读 DeepSeek 配置；字段缺失/非法即报错并指出缺什么"""
    key = str(keys.get("key") or "").strip()
    if not key:
        raise McpError('缺少配置项 key：provider="deepseek" 需要 DeepSeek API 密钥；'
                       "请在 narnat.json「联网搜索」段的 key 填入"
                       "（可直接复用「智能体」段的接口密钥）")

    url = str(keys.get("url") or "").strip() or DEFAULT_URL

    raw_count = keys.get("count_param")
    count_param = DEFAULT_COUNT_PARAM if raw_count is None else str(raw_count).strip()

    raw_args = keys.get("args", {})
    if not isinstance(raw_args, dict):
        raise McpError('args 必须是对象（如 {"need_content": true}）')

    return Config(url, key, count_param, _coerce_flag(raw_args.get("need_content")),
                  _text_arg(raw_args, "ori_question"),
                  _text_arg(raw_args, "localization"))


def _first_text(node: Dict, keys: Tuple[str, ...]) -> str:
    """取 node 中第一个非空字符串字段"""
    for name in keys:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def server_error(payload) -> str:
    """服务端 error 体 → 可读 message；不是错误体/取不到 message 时返回空串

    防的是「HTTP 200 但正文是 {"error": {...}}」被当搜索结果：服务端不守 isError
    规范时（error 体混在 content 文本块里），渲染成结果就丢了失败语义（UI 不显示
    失败、AI 会把错误信息当搜索结果）。MCP 与 DeepSeek 两条解析路径共用此判定，
    全仓只有这一处实现。payload 可以是响应文本，也可以是 json 解析后的对象。
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
    """非 200 响应的错误文本：优先服务端 error.message，无则原文截断"""
    detail = server_error(text) or text.strip()[:_RAW_LIMIT]
    return f"HTTP {status}: {detail}" if detail else f"HTTP {status}"


def build_body(cfg: Config, query: str, num: int) -> Dict:
    """组装请求体：num → count 字段（clamp 到 1~50；count_param 为空则不传）"""
    body: Dict = {"query": query}
    if cfg.count_param:
        body[cfg.count_param] = max(COUNT_MIN, min(int(num), COUNT_MAX))
    body["need_content"] = cfg.need_content
    if cfg.ori_question:
        body["ori_question"] = cfg.ori_question
    if cfg.localization:
        body["localization"] = cfg.localization
    return body


def search(cfg: Config, query: str, num: int, timeout: float) -> str:
    """调 DeepSeek Search，返回响应文本；HTTP/网络/超时错误抛 McpError"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(cfg.url, json=build_body(cfg, query, num),
                               headers={"Authorization": f"Bearer {cfg.key}"})
    except httpx.TimeoutException:
        raise McpError(f"请求超时({timeout:g}s)")
    except httpx.HTTPError as e:
        raise McpError(f"请求失败: {e}")
    if resp.status_code != 200:
        raise McpError(_http_error(resp.status_code, resp.text))
    return resp.text


def to_results(text: str) -> List[Dict]:
    """响应文本 → [{title, url, description}]；非 JSON / 服务端 error 抛 McpError

    只有在服务端真返回 200 且无 error 时才算「搜索结果」——error 体绝不能被
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

    items = data.get("results") if isinstance(data, dict) else None
    results: List[Dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append({"title": _first_text(item, ("title",)) or url,
                        "url": url,
                        "description": _first_text(item, _DESC_KEYS)})
    return results
