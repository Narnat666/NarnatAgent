"""WebSearch —— 联网搜索与网页抓取（纯 REST 双接口）

## 只有一个工具形态：action 判别

  action=search  按关键词搜索 → 返回编号列表（标题 / URL / 摘要）
  action=fetch   抓取指定网页 → 返回 Markdown 正文（不自行截断）

两个动作走同一个 narnat.json「联网搜索」段里的两套子配置（http.search /
http.fetch）。换服务商只改 json，不改代码。

## MCP 联网请用 MCP 工具

本工具**不再自带 MCP 接入**。以前那层「MCP 握手 → 选单个工具 → 映射参数名」
是在项目自带的 MCP 框架之上又包了一层，且会丢信息（正文抓取拿不到）。
需要任意 MCP 联网服务器时，用 `MCP` 工具的 connect 接上（工具名
`mcp__<服务器>__<工具>`），schema 原样来自服务器。

## 配置示例

```jsonc
"联网搜索": {
  "key": "sk-...",
  "auth": "bearer",                          // 空=不认证 / bearer / 自定义头名
  "http": {
    "search": {
      "url": "https://api.deepseek.com/v1/tools/search",
      "query_field": "query",
      "count_field": "count",                // 空串 = 不传条数
      "args": {},
      "rail": {"list": "results", "title": "title", "url": "url",
               "desc": ["snippet", "summary", "content"]},
      "extra_params": {}
    },
    "fetch": {
      "url": "https://api.deepseek.com/v1/tools/fetch",
      "url_field": "url",
      "args": {},
      "rail": {"title": "title", "content": "content"}
    }
  }
}
```

`rail` 是响应解析规则（数组路径 + 字段名，全部可配）；`args` 是固定附加参数；
两个 `url` 至少填一个（缺哪个就只是那个 action 报「未配置」，另一个照常可用）。

## extra_params —— 让模型能填厂商扩展参数

`http.search.extra_params` 声明「模型可填的参数」，形如：

```jsonc
"extra_params": {
  "domains": {"type": "array", "items": {"type": "string"},
              "description": "域名过滤", "field": "include_domains"}
}
```

模块加载时把去掉 `field` 的副本并入工具定义的 properties（与 action/query/url/num
冲突的条目跳过，`required` 不变），运行时把模型填的值按 `{参数名 → 字段名}`
翻译进请求体。所以给某家厂商加一个参数不需要改代码，也不需要多一轮发版。
"""

import re
from typing import Any, Dict, List

from ..tool_context import _narnat_json_path, _read_search_section
from . import rest


# ── 搜索配置（超时）──

class SearchConfig:
    """WebSearch 固定参数。

    接口地址/字段名/认证契约全部来自 narnat.json 的「联网搜索」段，
    由 execute() 每次调用时经 tool_context.api_keys 读取（不缓存到模块级全局，
    避免跨调用残留脏状态）。
    """
    timeout = 15          # 单次 HTTP 调用超时（秒）

DEFINITION = {
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "联网搜索或抓取网页。search 按关键词搜索，返回标题、"
                       "URL 与摘要；fetch 抓取指定网页，返回正文。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "fetch"],
                           "description": "search=按关键词搜索；fetch=抓取网页正文。"},
                "query": {"type": "string",
                          "description": "搜索词。action=search 时必填。"},
                "url": {"type": "string",
                        "description": "网页地址（http/https）。action=fetch 时必填。"},
                "num": {"type": "integer",
                        "description": "返回条数。默认 10，上限 20。"},
            },
            "required": ["action"],
        },
    },
}


CAPABILITY = {
    "label": "联网搜索",
    "dispatch": "readonly",
    "summary": "query",
    "trusted_output": True,
}


# ── 扩展参数（narnat.json 声明 → 工具定义 properties + 运行时字段翻译）──

# {工具参数名: 请求体字段名}，运行时据此把模型填的值翻译成厂商字段
_EXTRA_FIELDS: Dict[str, str] = {}


def _apply_extra_params() -> None:
    """把「联网搜索」→ http.search.extra_params 声明的扩展参数并入 DEFINITION

    模块加载时调一次；重复调用是幂等的（先撤掉上一次注入的项）。
    读盘失败/段缺失/声明非法一律静默跳过：扩展参数是可选增强，
    读不到不能影响工具本身的可用性。
    """
    props = DEFINITION["function"]["parameters"]["properties"]
    for name in list(_EXTRA_FIELDS):
        props.pop(name, None)
    _EXTRA_FIELDS.clear()

    declared: Dict = {}
    try:
        path = _narnat_json_path()
        section = _read_search_section(path) if path else None
        search = (section or {}).get("http", {}).get("search", {})
        raw = search.get("extra_params") if isinstance(search, dict) else None
        if isinstance(raw, dict):
            declared = raw
    except Exception:
        return

    for name, spec in declared.items():
        name = str(name).strip()
        if not name or not isinstance(spec, dict) or name in props:
            # 与 action/query/url/num 冲突（或已注入过）的条目一律跳过，不许覆盖固定参数
            continue
        field = str(spec.get("field") or name).strip() or name
        _EXTRA_FIELDS[name] = field
        props[name] = {k: v for k, v in spec.items() if k != "field"}


_apply_extra_params()


# ── 工具函数 ──

def _format_results(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本：只做排版，不排序、不截断。

    不排序：服务商返回的顺序就是它自己的相关性判断，我们手上没有比它更准的
    依据。曾经在这里用「中文二元切词 + 命中计数」重排过，实测「南京天气」
    会切出跨词边界的「京天」，让「北京天气预报」也拿到 0.500 分——用一套更
    粗的猜测去覆盖服务商的排序，AI 拿到的是一个看不出被改过的假事实。

    不截断：标题和摘要原样返回。截断由全局输出上限统一负责（超限会落盘并
    给出路径），这里再切一刀只会让 AI 看不到摘要的后半句而不知道有东西被丢。
    """
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "") or ""
        lines.append(f"{i}. {title}")
        lines.append(f"   URL: {r['url']}")
        desc = r.get("description", "")
        if desc:
            # 仅压缩换行/连续空白为单空格：这是排版（多行摘要会破坏列表结构），
            # 不是内容裁剪
            flat = re.sub(r"\s+", " ", desc).strip()
            lines.append(f"   {flat}")
    return "\n".join(lines)


def _translate_extras(values: Dict) -> Dict:
    """本次调用填的扩展参数 → 请求体字段名（跳过空值/空数组）"""
    extras: Dict = {}
    for name, value in values.items():
        field = _EXTRA_FIELDS.get(name)
        if not field or value is None or value == "" or value == [] or value == {}:
            continue
        extras[field] = value
    return extras


def _do_fetch(cfg: rest.Config, url: str) -> str:
    """action=fetch：抓取网页正文（不截断，体积由全局输出上限控制）"""
    target = str(url or "").strip()
    if not target:
        return "[错误: action=fetch 需要 url]"
    if not target.lower().startswith(("http://", "https://")):
        return "[错误: url 必须是 http/https 地址]"
    if cfg.fetch is None:
        return "[错误: 未配置抓取接口（narnat.json「联网搜索」→ http.fetch.url）]"
    try:
        text = rest.fetch(cfg.fetch, cfg.headers, target, SearchConfig.timeout)
        title, content = rest.to_page(text, cfg.fetch)
    except Exception as e:
        return f"[错误: 抓取失败: {e}]"
    if not content:
        return f"[错误: 抓取失败: 服务端未返回正文（title: {title}）]"
    return f"标题: {title or target}\nURL: {target}\n\n{content}"


def _do_search(cfg: rest.Config, query: str, num: Any, extra_values: Dict) -> str:
    """action=search：搜索 → 编号列表（保持服务商顺序，不重排）"""
    q = str(query or "").strip()
    if not q:
        return "[错误: action=search 需要 query]"
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    try:
        num = int(num) if num is not None else 10
    except (TypeError, ValueError, OverflowError):
        return "[错误: num需为正整数]"
    if num <= 0:
        return "[错误: num需为正整数]"
    # 上限20：结果逐条灌给LLM，超大 num 既拖慢响应又浪费配额。
    # 这是我们主动收缩请求条数（发请求时就带这个值），不是拿到后再裁剪。
    num = min(num, 20)
    if cfg.search is None:
        return "[错误: 未配置搜索接口（narnat.json「联网搜索」→ http.search.url）]"
    try:
        text = rest.search(cfg.search, cfg.headers, q, num,
                           _translate_extras(extra_values), SearchConfig.timeout)
        results = rest.to_results(text, cfg.search)
    except Exception as e:
        return f"[错误: 搜索失败: {e}]"
    if not results:
        return "[无搜索结果]"
    # 按服务商返回顺序原样输出，不重排（理由见 _format_results）
    return _format_results(results)


# ── 主入口 ──

def execute(action: str = "", query: str = "", num: int = 10, url: str = "",
            _tool_context=None, **kwargs) -> str:
    """
    联网搜索 / 网页抓取。

    Args:
        action: "search" 或 "fetch"（必填）
        query: 搜索词（action=search 时必填）
        num: 返回条数（action=search 时有效，默认 10，上限 20）
        url: 要抓取正文的网页地址（action=fetch 时必填）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）
        kwargs: narnat.json「extra_params」声明的厂商扩展参数

    Returns:
        search: 编号列表（标题 / URL / 摘要）；fetch: 标题 + URL + Markdown 正文
    """
    act = str(action or "").strip().lower()
    if act not in ("search", "fetch"):
        return "[错误: action 必须是 search 或 fetch]"
    try:
        cfg = rest.read_config(_tool_context.api_keys if _tool_context else None)
    except Exception as e:
        return f"[错误: 联网配置无效: {e}]"
    if act == "fetch":
        return _do_fetch(cfg, url)
    unknown = [k for k in kwargs if k not in _EXTRA_FIELDS]
    if unknown:
        raise TypeError(f"got an unexpected keyword argument '{unknown[0]}'")
    return _do_search(cfg, query, num, kwargs)
