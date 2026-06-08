# Open WebSearch 环境配置指南

为 narnat AI agent 提供免费的多引擎联网搜索能力。

## 简介

Open WebSearch 是一个开源的本地搜索 daemon，支持 Bing / DuckDuckGo / Baidu / Sogou 等 8 个搜索引擎，纯 HTTP 请求方式，不需要浏览器或 API Key。

- GitHub: https://github.com/Aas-ee/open-webSearch
- 协议: MIT
- 内存占用: ~188MB
- 响应速度: 0.2-0.4s

## 前提条件

- Node.js >= 18（推荐 20+）
- npm >= 9
- 国内网络可正常访问百度、Sogou、cn.bing.com

## 安装

```bash
# 全局安装（一次即可）
npm install -g open-websearch
```

如果 npm 下载慢，用国内镜像：

```bash
npm install -g open-websearch --registry=https://registry.npmmirror.com
```

## 启动 daemon

```bash
# 以 HTTP 模式启动（Baidu 为默认引擎，适合国内环境）
# Windows PowerShell:
$env:DEFAULT_SEARCH_ENGINE="baidu"
$env:MODE="http"
open-websearch serve

# Linux/Mac:
DEFAULT_SEARCH_ENGINE=baidu MODE=http open-websearch serve
```

启动后会监听 `http://127.0.0.1:3210`，端口被占用时会自动递增。

## 验证

```bash
# 健康检查
curl http://127.0.0.1:3210/health
# 返回: {"status":"ok","message":"open-websearch daemon running"}

# 搜索测试
curl -X POST http://127.0.0.1:3210/search \
  -H "Content-Type: application/json" \
  -d '{"query":"Python asyncio","maxResults":3,"engine":"bing"}'
```

## Python 调用

只用标准库 `urllib`，零依赖：

```python
import json
import urllib.request

def search(query, engine="bing", max_results=5):
    data = json.dumps({
        "query": query,
        "maxResults": max_results,
        "engine": engine,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:3210/search",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    return result["data"]["results"]

# 使用
results = search("Python asyncio", "bing")
for r in results:
    print(r["title"], r["url"])
```

## 支持的搜索引擎

| 引擎 | 代码 | 国内可用 | 说明 |
|------|------|----------|------|
| Bing | `bing` | cn.bing.com | 英文/技术搜索 |
| DuckDuckGo | `duckduckgo` | 国内直连 | 隐私搜索引擎 |
| Baidu | `baidu` | 国内直连 | 中文搜索 |
| Sogou | `sogou` | 国内直连 | 中文搜索 |
| Brave | `brave` | 需代理 | 独立索引 |
| CSDN | `csdn` | 国内直连 | 中文技术社区 |
| Exa | `exa` | 需 Key | AI 语义搜索 |
| Startpage | `startpage` | 需代理 | Google 匿名代理 |

## 多引擎并发

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {
        pool.submit(search, "query", "bing"): "bing",
        pool.submit(search, "query", "baidu"): "baidu",
        pool.submit(search, "query", "duckduckgo"): "ddg",
    }
    all_results = []
    for f in futures:
        all_results.extend(f.result())
```

## 后台运行

```bash
# Linux/Mac (nohup)
nohup DEFAULT_SEARCH_ENGINE=baidu MODE=http open-websearch serve > /tmp/ows.log 2>&1 &

# Windows PowerShell (后台进程)
Start-Process -NoNewWindow -FilePath "open-websearch" -ArgumentList "serve"
```

## 故障排查

| 问题 | 解决 |
|------|------|
| 端口被占用 | daemon 会自动递增端口，查看日志确认实际端口 |
| 搜索返回空 | 检查网络，尝试切换引擎（Bing → Baidu） |
| npm install 失败 | 换国内镜像 `--registry=https://registry.npmmirror.com` |
| daemon 进程消失 | 重新执行 `open-websearch serve` |
