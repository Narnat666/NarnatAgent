"""WebSearch工具 —— 通用 MCP 搜索客户端

依赖:
  - 任意 MCP 搜索服务器（默认 AnySearch）

经 MCP Streamable HTTP 协议调用：McpHttpClient 握手(initialize) → 列工具(tools/list)
→ 按配置解析工具与参数 → 调用(tools/call)。JSON/SSE 响应、会话头、超时均由
McpHttpClient 承担，本模块不碰 HTTP 细节。

各服务器的工具名/参数名不同，一律由配置指定（不猜）；参数类型（数组/标量）查工具
inputSchema 决定。配置（narnat.json "联网搜索"，缺项即报错；旧字段名不识别）:
  - key:          API key；空 = 无鉴权服务器
  - url:          MCP 端点 URL
  - auth:         认证方式：x-api-key / bearer / 自定义头名
  - tool:         搜索工具名；空 = 连接后报错列出服务器工具表
  - query_params: 查询词填入的参数名列表（可多个，数组型参数传 [query]）
  - count_param:  结果数量参数名；空串 = 不传
  - args:         可选，附加固定参数，原样并入调用（如 {"zone": "cn"}）
"""

import json
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple

from ...mcp.client import McpError
from ...mcp.http_client import McpHttpClient
from ..exec_signal import has_error


# ── 搜索配置（原模块级全局收敛为类成员）──

class SearchConfig:
    """WebSearch 固定参数。

    连接/工具/参数契约全部来自 narnat.json "联网搜索"，由 execute() 每次调用时经
    tool_context.api_keys 读取（不缓存到模块级全局，避免跨调用残留脏状态）；
    握手建立的连接按配置指纹缓存，配置变化自然重建。
    """
    timeout = 15          # 工具调用超时（秒）；握手/列工具为 timeout + _CONNECT_TIMEOUT_EXTRA

DEFINITION = {
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "网页搜索（用于查找API文档、解决方案、技术文章等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "num": {"type": "integer", "description": "返回结果数量（正整数，默认5，上限20）"},
            },
            "required": ["query"],
        },
    },
}


# ── 工具函数 ──

def _extract_query_words(query: str) -> set:
    """提取查询词用于相关性排序"""
    words = set()
    for w in re.findall(r"[a-zA-Z0-9]{2,}", query):
        words.add(w.lower())
    cn_chars = re.findall(r"[\u4e00-\u9fff]+", query)
    for seg in cn_chars:
        for i in range(len(seg) - 1):
            words.add(seg[i:i + 2])
        if len(seg) == 1:
            words.add(seg)
    return words


def _relevance_score(result: Dict, query: str) -> float:
    """按标题和摘要中的查询词命中数打分"""
    title = result.get("title", "").lower()
    desc = result.get("description", "").lower()
    q_words = _extract_query_words(query)
    if not q_words:
        return 0.5
    title_hits = sum(1 for w in q_words if w in title)
    desc_hits = sum(1 for w in q_words if w in desc)
    return (title_hits * 3 + desc_hits) / (len(q_words) * 4)


def _format_results(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本（描述压缩空白并截断，避免大段噪音灌给LLM）"""
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "") or ""
        if len(title) > 120:
            title = title[:120] + "…"
        lines.append(f"{i}. {title}")
        lines.append(f"   URL: {r['url']}")
        desc = r.get("description", "")
        if desc:
            # 压缩换行/连续空白为单空格，截断到300字符
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 300:
                desc = desc[:300] + "…"
            lines.append(f"   {desc}")
    return "\n".join(lines)


# ── 结果解析 ──

def _parse_anysearch_markdown(text: str) -> List[Dict]:
    """解析 AnySearch 返回的 markdown 文本为结构化结果"""
    results = []
    # 格式: ### N. Title\n- **URL**: url\n- description\n...
    blocks = re.split(r"\n(?=### \d+\.)", text)
    for block in blocks:
        title_m = re.search(r"^### \d+\.\s*(.+)$", block, re.MULTILINE)
        url_m = re.search(r"-\s*\*\*URL\*\*:\s*(https?://[^\s\n]+)", block)
        if title_m and url_m:
            title = title_m.group(1).strip()
            url = url_m.group(1).strip()
            # 提取描述：URL之后的文本，去掉前缀标记
            desc_start = url_m.end()
            rest = block[desc_start:].strip()
            # 去掉可能的 "**Description**: " 等前缀
            desc = re.sub(r"^[-*]+\s*(\*\*Description\*\*:\s*)?", "", rest).strip()
            results.append({"title": title, "url": url, "description": desc})
    return results


# markdown 链接 [标题](url)；裸 URL 行（可带列表前缀）
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_BARE_URL_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]\s*|\d+[.)]\s*)?(https?://\S+?)[ \t]*$",
                               re.MULTILINE)


def _line_tail(text: str, pos: int) -> str:
    """取 pos 所在行从 pos 起的剩余文本"""
    end = text.find("\n", pos)
    return text[pos:] if end < 0 else text[pos:end]


def _parse_links(text: str) -> List[Dict]:
    """通用兜底：提取「标题 + URL」对 —— markdown 链接优先，无则取裸 URL 行（标题用 URL）"""
    results = []
    for m in _MD_LINK_RE.finditer(text):
        desc = _line_tail(text, m.end())
        results.append({"title": m.group(1).strip(),
                        "url": m.group(2).strip(),
                        "description": re.sub(r"^[)\s:：|\-–—]+", "", desc)})
    if results:
        return results
    for m in _BARE_URL_LINE_RE.finditer(text):
        url = m.group(1)
        results.append({"title": url, "url": url, "description": ""})
    return results


# JSON 结果常见键名（不同搜索服务器的字段命名）
_JSON_URL_KEYS = ("url", "link", "href")
_JSON_TITLE_KEYS = ("title", "name", "headline", "text")
_JSON_DESC_KEYS = ("description", "snippet", "content", "summary")
_JSON_MAX_RESULTS = 50


def _first_text(node: Dict, keys: Tuple[str, ...]) -> str:
    """取 node 中第一个非空字符串字段"""
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _walk_json(node, out: List[Dict]) -> None:
    """递归遍历 JSON，收集带 URL 字段的对象"""
    if len(out) >= _JSON_MAX_RESULTS:
        return
    if isinstance(node, list):
        for item in node:
            _walk_json(item, out)
    elif isinstance(node, dict):
        url = _first_text(node, _JSON_URL_KEYS)
        if url.lower().startswith(("http://", "https://")):
            out.append({"title": _first_text(node, _JSON_TITLE_KEYS) or url,
                        "url": url,
                        "description": _first_text(node, _JSON_DESC_KEYS)})
        else:
            for value in node.values():
                _walk_json(value, out)


def _parse_json_results(text: str) -> List[Dict]:
    """通用兜底：整段文本为 JSON 时递归提取结果对象（深度异常时放弃解析）"""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return []
    results: List[Dict] = []
    try:
        _walk_json(data, results)
    except RecursionError:
        pass
    return results


# "标签: 值" 块（Exa 等返回格式：Title: xxx / URL: https://...）
_LABEL_TITLE_RE = re.compile(r"^[ \t]*(?:Title|标题)[ \t]*[:：][ \t]*(.+?)[ \t]*$")
_LABEL_URL_RE = re.compile(r"^[ \t]*(?:URL|Link|链接)[ \t]*[:：][ \t]*(https?://\S+)[ \t]*$")


def _parse_labeled_blocks(text: str) -> List[Dict]:
    """通用兜底：'Title: xxx' + 'URL: https://...' 标签块解析

    URL 行收尾一个结果，标题取紧邻其上的 Title 行（无/无效则用 URL）；
    描述取 URL 行之后到下一个标签行之间的文本。
    """
    results: List[Dict] = []
    lines = text.splitlines()
    title = ""
    i, n = 0, len(lines)
    while i < n:
        tm = _LABEL_TITLE_RE.match(lines[i])
        if tm:
            value = tm.group(1).strip()
            title = "" if value in ("N/A", "-", "null", "None") else value
            i += 1
            continue
        um = _LABEL_URL_RE.match(lines[i])
        if um:
            url = um.group(1).strip()
            desc: List[str] = []
            j = i + 1
            while j < n and not (_LABEL_TITLE_RE.match(lines[j])
                                 or _LABEL_URL_RE.match(lines[j])):
                if lines[j].strip():
                    desc.append(lines[j].strip())
                j += 1
            results.append({"title": title or url, "url": url,
                            "description": re.sub(r"\s+", " ", " ".join(desc)).strip()})
            title = ""
            i = j
            continue
        i += 1
    return results


_RAW_LIMIT = 2000           # 解析不出结构化结果时的原文截断长度


def _truncate_raw(text: str) -> str:
    """原文兜底输出（截断到 _RAW_LIMIT，不丢内容优先于丢格式）"""
    if len(text) <= _RAW_LIMIT:
        return text
    return f"{text[:_RAW_LIMIT]}\n...[已截断: 原文共{len(text)}字符]"


def _render(text: str, query: str, num: int) -> str:
    """结果文本 → AI 可读输出：结构化解析优先，全解析不出则返回原文"""
    if not text.strip():
        return "[无搜索结果]"
    results = (_parse_anysearch_markdown(text)
               or _parse_json_results(text)
               or _parse_labeled_blocks(text)
               or _parse_links(text))
    if not results:
        return _truncate_raw(text)
    results.sort(key=lambda r: _relevance_score(r, query), reverse=True)
    return _format_results(results[:num])


# ── MCP 连接（惰性握手 + 按配置解析契约，缓存）──

_CONNECT_TIMEOUT_EXTRA = 5      # initialize/list_tools 相对调用超时多给的余量（秒）

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, "_Connection"] = {}


@dataclass(frozen=True)
class _Config:
    """一次 execute 解析出的搜索配置（含认证头）"""
    url: str
    headers: Dict[str, str]
    tool: str                 # 搜索工具名；空 = 服务器仅一个工具时采用
    query_params: tuple       # 查询词填入的参数名
    count_param: str          # 结果数量参数名；空 = 不传
    extra_args: Dict          # 附加固定参数


class _Connection:
    """一次握手 + 契约解析得到的连接与调用参数（缓存单元）"""

    def __init__(self, fingerprint: str, client: McpHttpClient, tool_name: str,
                 query_fills: tuple, count_param: str, extra_args: Dict):
        self.fingerprint = fingerprint
        self.client = client
        self.tool_name = tool_name
        self.query_fills = query_fills    # ((参数名, 是否数组), ...)：查询词填入这些参数
        self.count_param = count_param    # 已过滤：仅在 schema 中存在才非空
        self.extra_args = extra_args      # 已过滤：仅保留 schema 中声明的参数


def _fingerprint(cfg: _Config) -> str:
    """连接缓存键：全部影响请求的配置（url/头/工具/参数契约）序列化"""
    return json.dumps([cfg.url, sorted(cfg.headers.items()), cfg.tool,
                       list(cfg.query_params), cfg.count_param, cfg.extra_args],
                      sort_keys=True, ensure_ascii=False)


def _read_config(api_keys) -> _Config:
    """按 narnat.json "联网搜索" 读配置；字段缺失/非法即报错并指出缺什么

    只认当前字段名（旧字段名如 websearch_url 一律不识别）：
    url/auth/tool/query_params/count_param 必填；key（空=无鉴权）与 args 可选。
    """
    keys = api_keys or {}
    missing = [name for name in ("url", "auth", "tool",
                                 "query_params", "count_param")
               if name not in keys]
    if missing:
        raise McpError(f"联网搜索缺少配置项: {', '.join(missing)}"
                       f"（按 README「联网搜索」补全 narnat.json）")

    url = str(keys["url"] or "").strip()
    auth = str(keys["auth"] or "").strip()
    if not url:
        raise McpError("url 不能为空")
    if not auth:
        raise McpError("auth 不能为空（x-api-key / bearer / 自定义头名）")

    raw_query = keys["query_params"]
    if isinstance(raw_query, str):
        query_params = (raw_query.strip(),) if raw_query.strip() else ()
    elif isinstance(raw_query, list):
        query_params = tuple(s for x in raw_query if (s := str(x).strip()))
    else:
        query_params = ()
    if not query_params:
        raise McpError('query_params 不能为空（查询词参数名，如 ["query"]）')

    count_param = str(keys["count_param"] or "").strip()

    raw_args = keys.get("args", {})
    if not isinstance(raw_args, dict):
        raise McpError('args 必须是对象（如 {"zone": "cn"}）')
    extra_args = {str(k): v for k, v in raw_args.items()}

    key = str(keys.get("key") or "")
    return _Config(url, _auth_headers(key, auth), str(keys["tool"] or "").strip(),
                   query_params, count_param, extra_args)


def _auth_headers(key: str, auth: str) -> Dict[str, str]:
    """按认证方式构造请求头；key 为空 = 无鉴权服务器（不加认证头）"""
    if not key:
        return {}
    if auth.lower() == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if auth.lower() == "x-api-key":
        return {"X-API-Key": key}
    return {auth: key}          # 其它非空串 = 自定义头名


def _schema_props(tool: Dict) -> Dict:
    """取工具 inputSchema.properties（无/非法时返回空 dict）"""
    schema = tool.get("inputSchema") if isinstance(tool, dict) else None
    props = schema.get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


def _describe_tools(specs: List[Dict], max_params: int = 6) -> str:
    """工具名(参数: a, b) 一览，报错时提示用户配置什么"""
    parts = []
    for spec in specs:
        params = list(_schema_props(spec))[:max_params]
        parts.append(f"{spec['name']}({', '.join(params)})" if params else spec["name"])
    return "、".join(parts) or "(无)"


def _resolve_tool_name(tools: list, configured: str) -> str:
    """确定搜索工具：配置名命中服务器工具表即用；否则报错列出工具表（不猜）"""
    specs = [t for t in tools if isinstance(t, dict) and t.get("name")]
    names = [str(t["name"]) for t in specs]
    if configured and configured in names:
        return configured
    raise McpError(f"搜索工具未确定（tool={configured or '空'}）；"
                   f"服务器工具表: {_describe_tools(specs)}；"
                   f"请在 narnat.json「联网搜索」配置 tool")


def _resolve_params(tool: Dict, tool_name: str, query_params: tuple,
                    count_param: str, extra_args: Dict) -> tuple:
    """按配置 + inputSchema 解析调用契约：(query_fills, count_param, extra_args)

    query 参数查 schema 决定标量/数组，不在 schema 中即报错（列可用参数名）；
    count 与附加参数不在 schema 中时静默不传（换服务器后旧配置不会误传给其它工具）。
    """
    if not query_params:
        raise McpError("query_params 为空；请在 narnat.json「联网搜索」配置查询词参数名")
    props = _schema_props(tool)
    query_fills = []
    for name in query_params:
        spec = props.get(name)
        if props and spec is None:
            raise McpError(f"参数 {name} 不在工具 {tool_name} 的参数表: "
                           f"{', '.join(props) or '(无)'}；请在 narnat.json「联网搜索」配置 query_params")
        is_array = isinstance(spec, dict) and spec.get("type") == "array"
        query_fills.append((name, is_array))
    if not props:
        # 无 schema 可查：照配置传（标量），附加参数原样并入
        return tuple(query_fills), count_param, dict(extra_args)
    return (tuple(query_fills),
            count_param if count_param in props else "",
            {k: v for k, v in extra_args.items() if k in props})


def _connect(cfg: _Config, timeout: float, fingerprint: str) -> _Connection:
    """握手 → 列工具 → 契约解析。失败抛 McpError 并关闭连接（不留半初始化状态）"""
    client = McpHttpClient("websearch", cfg.url, headers=cfg.headers)
    try:
        client.initialize(timeout)
        tools = client.list_tools(timeout)
        tool_name = _resolve_tool_name(tools, cfg.tool)
        tool_spec = next((t for t in tools if isinstance(t, dict)
                          and str(t.get("name")) == tool_name), {})
        query_fills, count_param, extra_args = _resolve_params(
            tool_spec, tool_name, cfg.query_params, cfg.count_param, cfg.extra_args)
    except Exception:
        client.close()
        raise
    return _Connection(fingerprint, client, tool_name, query_fills, count_param, extra_args)


def _get_connection(cfg: _Config) -> _Connection:
    """取连接（缓存命中直接复用）。首调持锁构建：并发首调只握手一次"""
    fingerprint = _fingerprint(cfg)
    with _CACHE_LOCK:
        conn = _CACHE.get(fingerprint)
        if conn is None:
            conn = _connect(cfg, SearchConfig.timeout + _CONNECT_TIMEOUT_EXTRA, fingerprint)
            _CACHE[fingerprint] = conn
        return conn


def _drop_connection(conn: _Connection) -> None:
    """调用失败后清缓存（下次自然重建）。锁外 close，不长时间持锁"""
    with _CACHE_LOCK:
        dropped = _CACHE.get(conn.fingerprint) is conn
        if dropped:
            _CACHE.pop(conn.fingerprint, None)
    if dropped:
        conn.client.close()


def _build_arguments(conn: _Connection, query: str, num: int) -> Dict:
    """按解析出的契约组装调用参数（数组参数传 [query]；附加参数原样并入）"""
    args = {name: ([query] if is_array else query)
            for name, is_array in conn.query_fills}
    if conn.count_param:
        args[conn.count_param] = num
    for name, value in conn.extra_args.items():
        args.setdefault(name, value)
    return args


# ── 主入口 ──

def execute(query: str, num: int = 5, _tool_context=None) -> str:
    """
    联网搜索。

    Args:
        query: 搜索关键词
        num: 返回结果数，默认5
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        格式化的搜索结果，每条含标题、URL、描述
    """
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    try:
        num = int(num) if num is not None else 5
    except (TypeError, ValueError, OverflowError):
        return "[错误: num需为正整数]"
    if num <= 0:
        return "[错误: num需为正整数]"
    # 上限20：结果逐条灌给LLM，超大num既拖慢响应又浪费配额，且全局截断会砍掉尾部
    num = min(num, 20)
    # 每次调用从 tool_context 读取配置（不缓存到全局，避免跨调用残留）
    try:
        cfg = _read_config(_tool_context.api_keys if _tool_context else None)
        conn = _get_connection(cfg)
    except Exception as e:
        return f"[错误: 搜索失败: {e}]"

    try:
        text = conn.client.call_tool(conn.tool_name,
                                     _build_arguments(conn, query, num),
                                     SearchConfig.timeout)
    except McpError as e:
        _drop_connection(conn)      # 连接可能已损坏：清缓存，下次自然重建
        return f"[错误: 搜索失败: {e}]"
    except Exception as e:
        return f"[错误: 搜索失败: {e}]"

    # 服务端 isError：call_tool 已包成框架错误行（带不可伪造标签）。
    # 必须原样透传——若进 _render 解析，错误文本里的链接会被当"搜索结果"，
    # 失败语义丢失（UI 不显示失败、AI 可能把错误文档链接当结果）。
    if has_error(text):
        return text
    return _render(text, query, num)
