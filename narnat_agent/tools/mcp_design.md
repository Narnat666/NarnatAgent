# MCP 客户端实现方案

## 一、概述

MCP（Model Context Protocol）是 AI Agent 与外部工具之间的标准化协议。

- **agent** = 人
- **MCP 协议** = 通道
- **外部工具** = 工具

agent 通过 MCP 协议（JSON-RPC 2.0 over stdio）与本地运行的子进程通信，调度开源工具。

```
NarnatAgent ──MCP Client──→ stdin/stdout ──→ MCP Server（开源工具进程）
```

---

## 二、整体流程

```
agent 启动
  │
  ├─1. 读取 .narnat/mcp.json 配置
  │
  ├─2. 对每个配置的 server：
  │     subprocess.Popen 启动子进程
  │
  ├─3. 初始化握手：发送 initialize 请求
  │
  ├─4. 发现工具：发送 tools/list 请求 → 拿到工具列表
  │
  ├─5. 合并到 TOOL_DEFINITIONS（动态注册）
  │
  └─6. 运行时：LLM 调用工具 → registry 路由 → MCP Client 转发 → 返回结果
```

---

## 三、涉及的文件（改什么）

### 新建文件

| 文件 | 说明 |
|------|------|
| `narnat_agent/tools/mcp_client.py` | MCP 客户端核心：进程管理 + JSON-RPC 通信 |
| `.narnat/mcp.json`（模板） | 用户配置文件，列出要连接的 MCP Server |

### 修改文件

| 文件 | 改动点 | 改动量 |
|------|--------|--------|
| `narnat_agent/config/defaults.py` | 新增 `MCP_JSON = "mcp.json"` 常量 | +1行 |
| `narnat_agent/config/loader.py` | `load_config()` 中加载 mcp.json | +15行 |
| `narnat_agent/tools/registry.py` | `TOOL_DEFINITIONS` 动态化、`execute()` 路由 MCP 工具 | +40行 |
| `narnat_agent/core/agent.py` | `__init__()` 中初始化 MCP、`_execute_tool_calls()` 分类 MCP 工具、退出时清理子进程 | +25行 |

---

## 四、mcp_client.py 设计

### 核心类：MCPClient

```
MCPClient
├── __init__(command, args, env)
├── start()          → 启动子进程、handshake、获取工具列表
├── list_tools()     → 发送 tools/list，返回工具定义列表
├── call_tool(name, arguments)  → 发送 tools/call，返回执行结果
├── stop()           → 关闭子进程
└── _send_request(method, params)  → 内部：构造 JSON-RPC、写 stdin、读 stdout
```

### JSON-RPC 消息格式

MCP 使用 JSON-RPC 2.0，每行为一条完整 JSON，无行分隔符嵌套。

**发送（请求）：**
```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

**接收（响应）：**
```json
{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"create_issue","description":"...","inputSchema":{...}}]}}
```

只需实现 3 个 method：
- `initialize` — 握手
- `tools/list` — 发现工具
- `tools/call` — 调用工具

### 进程管理

```python
import subprocess

proc = subprocess.Popen(
    ["npx", "-y", "@anthropic/mcp-server-github"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
# 通过 proc.stdin.write(json_line) 发送
# 通过 proc.stdout.readline() 接收
```

---

## 五、registry.py 改造

### 当前结构

```python
_TOOL_IMPLEMENTATIONS = {
    "Read": read.execute,
    "Glob": glob.execute,
    ...
}

TOOL_DEFINITIONS = [
    {"type":"function","function":{"name":"Read",...}},
    {"type":"function","function":{"name":"Glob",...}},
    ...
]
```

### 改造后结构

```python
# 内置工具（不变）
_BUILTIN_IMPLEMENTATIONS = { ... }
_BUILTIN_DEFINITIONS = [ ... ]

# MCP 工具（动态）
_MCP_CLIENTS: List[MCPClient] = []
_MCP_TOOL_MAP: Dict[str, MCPClient] = {}  # tool_name → MCPClient

def register_mcp_server(command: str, args: list, env: dict = None):
    """启动一个 MCP Server 并注册其工具"""
    client = MCPClient(command, args, env)
    client.start()
    tools = client.list_tools()
    for tool in tools:
        name = tool["name"]
        _MCP_TOOL_MAP[name] = client
        TOOL_DEFINITIONS.append(tool)  # 追加到总定义列表
    _MCP_CLIENTS.append(client)

def get_tool_definitions():
    return _BUILTIN_DEFINITIONS + [动态MCP工具定义]

def execute(name, arguments):
    # 1. 先查内置工具
    if name in _BUILTIN_IMPLEMENTATIONS:
        return _BUILTIN_IMPLEMENTATIONS[name](**arguments)
    # 2. 再查 MCP 工具
    if name in _MCP_TOOL_MAP:
        client = _MCP_TOOL_MAP[name]
        result = client.call_tool(name, arguments)
        return (result, "")
    return (f"错误: 未知工具: {name}", "")
```

---

## 六、配置文件设计

### .narnat/mcp.json

```json
{
  "mcp_servers": [
    {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-github"],
      "env": {
        "GITHUB_TOKEN": "ghp_xxxxxxxxxxxx"
      }
    },
    {
      "command": "node",
      "args": ["C:/tools/my-custom-mcp-server/index.js"]
    },
    {
      "command": "uvx",
      "args": ["mcp-server-sqlite"]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `command` | 可执行文件（npx / node / python / uvx / 绝对路径） |
| `args` | 命令行参数 |
| `env` | 环境变量（可选，如 API token） |

---

## 七、agent.py 改造点

### 启动时（__init__）

```python
# 在现有初始化之后
self._mcp_clients = []
for server_config in self._config.mcp_servers:
    register_mcp_server(
        server_config["command"],
        server_config["args"],
        server_config.get("env"),
    )
```

### 运行时（_execute_tool_calls）

MCP 工具默认按只读/联网类处理（并行执行），除非工具定义中明确标注了副作用。

### 退出时（run 结束）

```python
for client in self._mcp_clients:
    client.stop()
```

---

## 八、工具命名冲突处理

如果 MCP 工具名和内置工具名冲突（如都叫 `read`）：

方案：MCP 工具名前缀用 `{server_name}_{tool_name}`，如 `github_create_issue`。

在 `mcp.json` 中可配置 `name` 字段作为前缀：

```json
{
  "name": "github",
  "command": "npx",
  "args": ["-y", "@anthropic/mcp-server-github"]
}
```

---

## 九、错误处理

| 场景 | 处理 |
|------|------|
| MCP Server 启动失败 | 打日志 + 跳过该 server，agent 继续运行 |
| 工具调用超时 | 设置 60s 超时，超时返回错误文本给 LLM |
| 子进程崩溃 | 下次调用时检测 `proc.poll()`，自动重启 |
| 初始化握手失败 | 跳过该 server |

---

## 十、实现顺序

1. `mcp_client.py` — MCPClient 类（核心）
2. `defaults.py` + `loader.py` — 配置加载
3. `registry.py` — 动态注册 + 路由
4. `agent.py` — 启动/退出管理
5. 测试：用一个轻量 MCP Server 验证全链路

---

## 十一、需要用户安装的依赖

不需要额外 Python 包。Python 标准库 `subprocess` + `json` 即可。

用户电脑需要：
- `npx`（Node.js 自带）— 如果要运行 npm 发布的 MCP Server
- `uvx`（`pip install uv`）— 如果要运行 Python MCP Server
- 或什么都不装，直接用本地路径指定已下载的工具
