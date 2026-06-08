"""查看 AnySearch 原始响应"""
import json
import urllib.request

data = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "search", "arguments": {"query": "BM3803 processor", "max_results": 3, "zone": "cn"}}
}).encode()
req = urllib.request.Request("https://api.anysearch.com/mcp", data=data,
                              headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req, timeout=15)
raw = resp.read()
print(raw.decode()[:2000])
