"""Open WebSearch vs AnySearch 暴力对比测试"""
import json
import time
import re
import urllib.request

QUERIES = [
    ("技术文档", "DeepSeek API pricing documentation"),
    ("GitHub代码", "BM3803 processor China space chip"),
    ("前端框架", "vue3 composition api setup script"),
    ("中文产品", "vivo s60 参数配置详细"),
    ("通用问题", "Python asyncio best practices 2025"),
    ("官方文档", "React 19 new features official docs"),
    ("手机", "Redmi K80 Pro specs"),
]

def search_ows(query, engine="bing"):
    data = json.dumps({"query": query, "maxResults": 5, "engine": engine}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:3210/search",
        data=data, headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())["data"]["results"]

def search_any(query):
    data = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "max_results": 5, "zone": "cn"}}
    }).encode()
    req = urllib.request.Request(
        "https://api.anysearch.com/mcp",
        data=data, headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    raw = json.loads(resp.read())
    # AnySearch returns markdown: ### N. Title\n- **URL**: url\n- description
    results = []
    for item in raw.get("result", {}).get("content", []):
        if isinstance(item, dict) and "text" in item:
            # Extract URLs from markdown
            for m in re.finditer(r'- \*\*URL\*\*:\s*(https?://[^\s\n]+)', item["text"]):
                results.append({"url": m.group(1)})
    return results

def url_domain(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except:
        return url

print("=" * 70)
print("  Open WebSearch (Bing)  vs  AnySearch  暴力对比")
print("=" * 70)

rows = []

for category, query in QUERIES:
    print(f"\n{'─'*60}")
    print(f"  [{category}] {query}")
    print(f"{'─'*60}")

    # Open WebSearch
    t0 = time.time()
    try:
        ows = search_ows(query, "bing")
        ows_time = time.time() - t0
        ows_domains = [url_domain(r["url"]) for r in ows[:5]]
        ows_title = ows[0].get("title", "")[:50] if ows else ""
        print(f"  OWS-Bing  {ows_time:.1f}s | {ows_domains[0] if ows_domains else '空'}")
    except Exception as e:
        ows_time = 99
        ows_domains = []
        print(f"  OWS-Bing  FAIL: {type(e).__name__}")

    # AnySearch
    t0 = time.time()
    try:
        any_r = search_any(query)
        any_time = time.time() - t0
        any_domains = [url_domain(r["url"]) for r in any_r[:5]]
        print(f"  AnySearch {any_time:.1f}s | {any_domains[0] if any_domains else '空'}")
    except Exception as e:
        any_time = 99
        any_domains = []
        print(f"  AnySearch FAIL: {type(e).__name__}: {e}")

    rows.append((category, ows_time, ows_domains, any_time, any_domains))

print(f"\n{'='*70}")
print(f"  总结")
print(f"{'='*70}")
print(f"{'查询':<12} {'OWS速度':<10} {'OWS首结果':<35} {'AnySearch速度':<14} {'AnySearch首结果'}")
print("-" * 70)
ows_total = 0
any_total = 0
for cat, ot, od, at, ad in rows:
    ows_first = od[0][:33] if od else "N/A"
    any_first = ad[0][:33] if ad else "N/A"
    print(f"{cat:<12} {ot if ot<99 else 'FAIL':.1f}s{'':>6} {ows_first:<35} {at if at<99 else 'FAIL':.1f}s{'':>10} {any_first}")
    if ot < 99: ows_total += ot
    if at < 99: any_total += at

ows_ok = sum(1 for _, ot, _, _, _ in rows if ot < 99)
any_ok = sum(1 for _, _, _, at, _ in rows if at < 99)
print("-" * 70)
print(f"  成功率: OWS {ows_ok}/{len(QUERIES)} | AnySearch {any_ok}/{len(QUERIES)}")
print(f"  总耗时: OWS {ows_total:.1f}s | AnySearch {any_total:.1f}s")
