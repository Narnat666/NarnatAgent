"""WebSearch 工具 —— MCP 风格搜索接口（默认 AnySearch）的调用与结果格式化。

契约来源：`openspec/changes/recast-v2/specs/tools-websearch/spec.md`
（工具名与参数契约、接口密钥与地址来源、调用协议与超时、相关性排序与结果格式化、
错误与空结果文案、兼容性怪癖保持）。行为搬运自旧实现
`narnat_agent/tools/web_search/__init__.py`（分组/正则/权重/截断口径逐字一致），
结构改为 `contracts.tool.Tool` 协议实现。

结构要点：
- 无模块级可变状态：密钥与接口地址每次调用从 `env.settings` 读取，不跨调用缓存；
- 纯标准库（json / re / urllib.request），不依赖新包其它积木（tools 只依赖 contracts）；
- 失败（接口异常、无密钥、无结果）一律作为普通工具结果文本返回，不向上抛异常。
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Mapping, Sequence

from ..contracts.tool import ToolDefinition, ToolEnv, ToolResult

__all__ = [
    "DEFAULT_NUM",
    "DEFAULT_URL",
    "MAX_NUM",
    "TIMEOUT_SECONDS",
    "WEBSEARCH_DEFINITION",
    "ZONE",
    "WebSearchTool",
    "call_search_api",
    "extract_query_words",
    "format_results",
    "normalize_num",
    "parse_markdown",
    "relevance_score",
]

DEFAULT_URL = "https://api.anysearch.com/mcp"
"""接口地址默认值（未配置 `websearch_url` 时使用）。"""

TIMEOUT_SECONDS = 15
"""单次调用超时（秒）。"""

DEFAULT_NUM = 5
"""`num` 缺省结果数。"""

MAX_NUM = 20
"""`num` 上限（超出静默裁剪）。"""

ZONE = "cn"
"""请求的地域参数（固定值、无配置入口——兼容性怪癖保持）。"""

TITLE_LIMIT = 120
"""标题显示上限（超出静默截断加 `…`）。"""

DESCRIPTION_LIMIT = 300
"""摘要显示上限（超出静默截断加 `…`）。"""

NO_KEY_ERROR = "[错误: 搜索失败]"
"""未配置密钥的固定文案（不带原因——兼容性怪癖保持）。"""

EMPTY_RESULT_TEXT = "[无搜索结果]"
"""接口成功但无可解析结果的固定文案。"""

BLOCK_SPLIT_RE = re.compile(r"\n(?=### \d+\.)")
TITLE_RE = re.compile(r"^### \d+\.\s*(.+)$", re.MULTILINE)
URL_RE = re.compile(r"-\s*\*\*URL\*\*:\s*(https?://[^\s\n]+)")
DESC_PREFIX_RE = re.compile(r"^[-*]+\s*(\*\*Description\*\*:\s*)?")
QUERY_WORD_RE = re.compile(r"[a-zA-Z0-9]{2,}")
CJK_SEGMENT_RE = re.compile(r"[\u4e00-\u9fff]+")
WHITESPACE_RE = re.compile(r"\s+")


WEBSEARCH_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "网页搜索（用于查找API文档、解决方案、技术文章等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "num": {"type": "integer",
                        "description": "返回结果数量（正整数，默认5，上限20）"},
            },
            "required": ["query"],
        },
    },
}


# ═══════════════════════════════════════════════════════════════
# 相关性排序
# ═══════════════════════════════════════════════════════════════


def extract_query_words(query: str) -> set[str]:
    """提取查询词集合：英文与数字词（长度 ≥2、小写）+ 中文连续段的相邻双字组合。

    单字段落取该字本身；无英文数字词也无中文字符时集合为空（打分走中性值）。
    """
    words: set[str] = set()
    for word in QUERY_WORD_RE.findall(query):
        words.add(word.lower())
    for segment in CJK_SEGMENT_RE.findall(query):
        for i in range(len(segment) - 1):
            words.add(segment[i:i + 2])
        if len(segment) == 1:
            words.add(segment)
    return words


def relevance_score(result: Mapping[str, str], query: str) -> float:
    """按标题与摘要中的查询词命中数打分：`(标题命中×3 + 摘要命中) / (查询词数×4)`。

    查询词集合为空时返回中性值 0.5（保持接口返回的原始顺序）。
    """
    title = (result.get("title", "") or "").lower()
    description = (result.get("description", "") or "").lower()
    words = extract_query_words(query)
    if not words:
        return 0.5
    title_hits = sum(1 for word in words if word in title)
    description_hits = sum(1 for word in words if word in description)
    return (title_hits * 3 + description_hits) / (len(words) * 4)


# ═══════════════════════════════════════════════════════════════
# 结果格式化
# ═══════════════════════════════════════════════════════════════


def format_results(results: Sequence[Mapping[str, str]]) -> str:
    """格式化搜索结果（每条至多三行：标题行、链接行、非空摘要行）。

    标题超 120 字符截断加 `…`；摘要把连续空白（含换行）压缩为单空格并去首尾空白、
    超 300 字符截断加 `…`；摘要为空时不输出摘要行。
    """
    lines: list[str] = []
    for index, result in enumerate(results, 1):
        title = result.get("title", "") or ""
        if len(title) > TITLE_LIMIT:
            title = title[:TITLE_LIMIT] + "…"
        lines.append(f"{index}. {title}")
        lines.append(f"   URL: {result['url']}")
        description = result.get("description", "")
        if description:
            description = WHITESPACE_RE.sub(" ", description).strip()
            if len(description) > DESCRIPTION_LIMIT:
                description = description[:DESCRIPTION_LIMIT] + "…"
            lines.append(f"   {description}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 接口调用
# ═══════════════════════════════════════════════════════════════


def parse_markdown(text: str) -> list[dict[str, str]]:
    """解析接口返回的 Markdown 文本为 `{title, url, description}` 结果列表。

    结果块以 `### N.` 序号标题起始；标题取标题行文本、链接取 `- **URL**: …` 行中的
    http/https 地址、摘要取链接之后的文本并剥离 `- `/`* `/`**Description**: ` 前缀；
    标题或链接缺失的块被丢弃。
    """
    results: list[dict[str, str]] = []
    for block in BLOCK_SPLIT_RE.split(text):
        title_match = TITLE_RE.search(block)
        url_match = URL_RE.search(block)
        if not (title_match and url_match):
            continue
        rest = block[url_match.end():].strip()
        results.append({
            "title": title_match.group(1).strip(),
            "url": url_match.group(1).strip(),
            "description": DESC_PREFIX_RE.sub("", rest).strip(),
        })
    return results


def call_search_api(query: object, max_results: int, api_key: str,
                    url: str) -> list[dict[str, str]]:
    """调用搜索接口（JSON-RPC 2.0 `tools/call`）并返回解析出的结果列表。

    请求参数固定为 `name=search`、`arguments={query, max_results, zone}`（zone 恒为
    `cn`）；请求头 `Content-Type: application/json`，已配置密钥时附 `X-API-Key`；
    单次调用超时 15 秒；响应取 `result.content` 中全部 `text` 块拼接后按 Markdown 解析。
    网络/HTTP/JSON 解析异常向上抛出，由调用方按固定文案包装（不吞异常在此层）。
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {"query": query, "max_results": max_results, "zone": ZONE},
        },
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        raw = json.loads(response.read())
    texts = [
        item["text"]
        for item in raw.get("result", {}).get("content", [])
        if isinstance(item, dict) and "text" in item
    ]
    return parse_markdown("\n".join(texts))


def normalize_num(raw: object) -> int | None:
    """把 `num` 容错为正整数：None 回落默认 5，超上限静默裁剪为 20。

    字符串等形态按整数转换；转换失败或非正数返回 None（调用方按固定文案返回且
    不发起网络调用）。
    """
    try:
        num = int(raw) if raw is not None else DEFAULT_NUM
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return min(num, MAX_NUM)


# ═══════════════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════════════


class WebSearchTool:
    """WebSearch 工具实现（`contracts.tool.Tool` 协议）。"""

    name = "WebSearch"

    def definition(self) -> ToolDefinition:
        """返回 LLM 工具定义（与旧实现 DEFINITION 逐字节等价）。"""
        return WEBSEARCH_DEFINITION

    def execute(self, args: dict[str, object], env: ToolEnv | None) -> ToolResult:
        """执行搜索。

        参数校验（`num`）先于密钥读取与网络调用；密钥与接口地址每次从
        `env.settings` 读取（未配置密钥返回 `[错误: 搜索失败]` 且不发起网络调用）；
        全部失败形态均作为普通结果文本返回，不向上抛异常。
        """
        if "query" not in args:
            # 缺必填参数：与旧实现（实现函数签名缺参）等价，经执行入口转友好提示
            raise TypeError("execute() missing 1 required positional argument: 'query'")
        query = args["query"]

        num = normalize_num(args.get("num"))
        if num is None:
            return ToolResult(llm_text="[错误: num需为正整数]")

        api_key = self._api_key(env)
        if not api_key:
            return ToolResult(llm_text=NO_KEY_ERROR)

        try:
            results = call_search_api(query, num, api_key, self._api_url(env))
        except Exception as exc:
            return ToolResult(llm_text=f"[错误: 搜索失败: {exc}]")

        if not results:
            return ToolResult(llm_text=EMPTY_RESULT_TEXT)

        # 先对全部返回结果排序，再截取前 num 条（兼容性怪癖保持）
        results.sort(key=lambda result: relevance_score(result, query), reverse=True)
        return ToolResult(llm_text=format_results(results[:num]))

    @staticmethod
    def _api_key(env: ToolEnv | None) -> str:
        """密钥表的 `websearch` 键（上下文缺失按未配置处理）。"""
        return env.settings.get_api_key("websearch") if env is not None else ""

    @staticmethod
    def _api_url(env: ToolEnv | None) -> str:
        """密钥表的 `websearch_url` 键；该键缺失或未配置时回落默认地址。"""
        if env is None:
            return DEFAULT_URL
        return env.settings.get_api_key("websearch_url") or DEFAULT_URL
