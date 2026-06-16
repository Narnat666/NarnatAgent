# Narnat Agent 设计文档

## 1. 核心宗旨

### 1.0 纯管道原则 — 一切的基石

**Agent是通道，不是决策者。**

Agent的唯一职责：
1. **送达**：将AI的命令完整、无修改地传达到目标设备
2. **返还**：将设备的输出完整、无裁剪地返还给AI

**绝对不做的事**：
- 不翻译AI的命令（AI自己知道平台语法）
- 不裁剪设备的输出（AI自己决定看多少）
- 不替AI杀进程（超时只告知，AI自己决定）
- 不替AI注入密码（AI自己写 `echo PASS | sudo -S`）
- 不替AI过滤结果（AI自己判断相关性）
- 不做任何"帮AI做决定"的事

**AI是人，不是傻子。** 我们只负责传话，其他事情AI自己斟酌。

**管道基础设施（允许保留）**：
- Shell的UTF8编码设置（管道编码，不是翻译）
- Terminal的哨兵marker（检测命令结束，不是翻译）
- Terminal的PTY噪声清洗（剥离回显/ANSI/\\r，不是AI的输出）

### 1.1 工具设计原则

**工具、上下文即核心，精简至上。**

1.  **AI工具设计**：数量严控，拒绝冗余。9个工具覆盖所有开发场景。单次调用必须返回全量有效信息，禁止碎片化交互。
2.  **上下文策略**：默认零压缩。仅在用户编辑+回车提问累计达 **120次** 时触发强制压缩。
    -   ⚠️ **50次**：提示对话已经50轮。
    -   ⚠️ **100次**：提示对话已经到达100轮，建议开启新对话。
    -   🛑 **120次**：强制执行上下文压缩（不可继续，必须压缩后开新会话）。
3.  **权限管控**：仅 `rm` 高危删除操作强制用户二次确认；其余所有操作对 AI 全权开放，允许自主调度。
4.  **代码准则**：设计优雅、精简，绝对不要设计屎山。

## 2. AI工具设计

 **按照agent_design\tool_design\tool_design.md设计**

## 3. 上下文策略

 **压缩策略**（120轮触发时的完整流程）：

1. **拦截用户输入**：Agent检测到第121次提问，暂存该问题，冻结当前会话，UI显示"正在压缩..."动态界面（调用`ui.begin_compressing()`）
2. **发送压缩prompt**：Agent向当前AI发送压缩指令（见第6节压缩prompt模板），**复用当前对话的全部messages**（AI需要完整上下文才能总结），在messages末尾追加一条user消息作为压缩指令
3. **写入磁盘**：Agent将AI输出写入 `.narnat/last_session_summary.md`
4. **校验总结结果**：读取 `.narnat/last_session_summary.md`，若内容为空，说明AI总结失败，**不销毁旧会话**，报错"压缩失败"并恢复用户输入
5. **销毁旧会话**：校验通过（md中有实际内容），Agent清除当前会话的全部上下文
6. **创建新会话**：Agent创建新会话，将md内容直接追加到系统prompt末尾（作为"上一轮对话成果"注入），无需额外让AI调Read工具
7. **重置标记**：继承完毕后，将 `.narnat/last_session_summary.md` 清空（写入空字符串），作为下次压缩的初始状态
8. **恢复用户问题**：将第121次暂存的问题发送给新会话的AI，继续处理
9. **停止压缩动画**：调用`ui.end_compressing()`

**初始状态**：`.narnat/last_session_summary.md` 初始为空文件。每次压缩成功并继承后重置为空文件。校验逻辑：文件非空=压缩成功，文件为空=未压缩/压缩失败。

## 4. UI设计

- narnat_agent\ui\ui_design.py已经实现完毕完整的ui界面和接口详情看API.md

## 5. 系统Prompt

系统prompt由三部分拼接：**基础prompt + 铁律（代码内置） + 用户自定义（narnat.md）**

### 5.1 基础Prompt

```
You are {model}, a code agent that helps users with software engineering tasks.

# Professional Objectivity

Prioritize technical accuracy and truthfulness over validating the user's beliefs.
Provide direct, objective technical info without unnecessary praise or emotional validation.
When there is uncertainty, investigate to find the truth first rather than instinctively confirming.
Avoid over-the-top validation like 'You are absolutely right'.

# Tool Usage Policy

## File Operations
- Read: Read file content. MUST read entire file at once (omit offset/limit), unless file >500 lines.
  NEVER read same file in segments, wastes tool calls.
- Write: Create or overwrite file. MUST provide full content, NEVER partial content.
  ALWAYS prefer Edit for modifying existing files, NEVER rewrite entire file just to change a few lines.
- Edit: Exact string replacement. old_string→new_string. NEVER guess content, Read first to confirm.
- Glob: Search files by name pattern (e.g. **/*.py). For content search, use Grep.
- Grep: Search file content by regex. MUST use regex syntax, NEVER glob syntax.

## Command Execution
- Bash: Execute shell command. For git/pip/npm/docker etc.
  NEVER use for file operations (read/write/search), use dedicated tools instead.
  NEVER use interactive commands (vim/top). Max timeout 600000ms.

## Web Search
- WebSearch: Search the internet for API docs, solutions, tech articles.
  Use sparingly — frequent searches hurt user experience and add cost.
  Applicable: real-time info, knowledge AI absolutely lacks, user correction.
  NEVER use for local code search (that's Grep's job).
  Do NOT blindly trust web information — verify with objective judgment before implementing.

## Progress Tracking
- TodoWrite: MUST use for multi-step tasks. NEVER write progress in plain text, MUST call this tool.
  Exactly ONE task MUST be in_progress at any time.
  Mark tasks completed IMMEDIATELY after finishing, do NOT batch completions.

# Task Management

- If you encounter errors, blockers, or cannot finish, keep the task as in_progress.
  When blocked, create a new task describing what needs to be resolved.
- NEVER mark a task as completed if tests are failing, implementation is partial, or you encountered unresolved errors.

# Avoid Over-Engineering

- Only make changes that are directly requested or clearly necessary. Keep solutions simple and focused.
- Do NOT add features, refactor code, or make improvements beyond what was asked.
- Do NOT add docstrings, comments, or type annotations to code you did not change.
- Do NOT add error handling, fallbacks, or validation for scenarios that cannot happen.
  Only validate at system boundaries (user input, external APIs).
- Do NOT create helpers, utilities, or abstractions for one-time operations.
- If something is unused, delete it completely. Do NOT add backwards-compatibility hacks.

# Behavioral Guidelines

1. Plan steps first, call TodoWrite to create progress list, then execute step by step.
2. MUST Read to confirm current content before modifying, NEVER guess file content from memory.
3. Change one thing at a time, verify after each change before moving to next.
4. Only make requested changes, NEVER refactor/add comments/add type annotations as side changes.
5. Avoid repeating similar thinking text before each tool call, just call the tool directly.
6. After all tool calls complete, provide a comprehensive summary in final response.
7. On error, analyze root cause first before deciding next step. If same approach fails twice, try a different approach.
8. Long conversations may trigger context compression. If you notice earlier information is lost, re-read critical files rather than guessing.
9. Keep final responses concise. Use markdown for formatting. NEVER use emojis unless user explicitly requests them.
10. NEVER create files unless absolutely necessary. ALWAYS prefer editing existing files.
11. Be careful not to introduce security vulnerabilities (command injection, XSS, SQL injection, etc).
```

### 5.2 铁律（代码内置 + prompt告知，双重保障）

以下规则同时出现在5.1节基础prompt中（告知AI遵守）和代码中（兜底拦截，用户无法通过narnat.md覆盖）：

- Edit前必须Read
- 改一处验一处
- 优先Edit而非Write
- Bash仅用于执行，文件操作用专用工具
- Grep定位→Read确认→Edit修改

### 5.3 用户自定义（narnat.md）

用户在 `narnat.md` 中写入自定义指令，追加到系统prompt末尾。用于调教AI的行为风格、项目特定规范等。

## 6. 压缩Prompt模板

120轮触发时，Agent向AI发送的专用压缩指令：

```
请总结本轮对话的全部经验和成果，写入经验成果.md，确保新对话能继承当前对话的全部经验成果。
```

## 7. 配置管理

### 7.1 narnat.json — 调度配置

```json
{
  "api_key": "sk-xxx",
  "base_url": "https://api.deepseek.com/anthropic",
  "model": "deepseek-v4-pro"
}
```

AI连接参数，用户在此调度AI。

### 7.2 narnat.md — 用户调教

Markdown格式，内容追加到系统prompt末尾。示例：

```markdown
# 项目规范
- 使用Python 3.10+语法
- 测试框架用pytest
- 代码风格遵循PEP8
```

## 8. 工具调度格式与主循环

### 8.1 LLM API协议

采用OpenAI兼容格式（DeepSeek/通义/智谱等均兼容）。使用`openai`SDK，配置base_url指向对应服务。

**请求格式**：
```python
client = OpenAI(api_key=config.api_key, base_url=config.base_url)
response = client.chat.completions.create(
    model=config.model,
    messages=messages,          # 对话历史
    tools=tool_definitions,     # 工具定义列表
    stream=True                 # 流式输出
)
```

**工具定义格式**（传给LLM的tools参数）：
```python
tool_definitions = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "读取文件内容，带行号",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "offset": {"type": "integer", "description": "起始行号(1-based)，省略则从头读"},
                    "limit": {"type": "integer", "description": "最大行数，省略则读全文"}
                },
                "required": ["file_path"]
            }
        }
    },
    # ... 其余7个工具同理
]

# TodoWrite工具定义（第9个工具，结构较复杂，单独列出）
tool_definitions.append({
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": "创建和管理结构化任务列表，用于跟踪当前编码会话的进度。复杂多步骤任务必须使用。任何时刻恰好只有1个任务处于in_progress状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述，祈使语气，如'Run tests'"
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "进行时形式，执行时显示，如'Running tests'"
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态"
                            }
                        },
                        "required": ["content", "status", "activeForm"]
                    },
                    "description": "任务列表"
                }
            },
            "required": ["todos"]
        }
    }
})
```

**LLM返回tool_call**（AI决定调工具）：
```python
delta.tool_calls = [{
    "id": "call_abc123",
    "type": "function",
    "function": {
        "name": "Grep",
        "arguments": "{\"pattern\": \"class Foo\", \"path\": \"src/\", \"output_mode\": \"content\"}"
    }
}]
```

**工具结果回传**（agent执行后塞回messages）：
```python
messages.append({
    "role": "tool",
    "tool_call_id": "call_abc123",
    "content": "src/main.py:42:class Foo:\nsrc/utils.py:15:class FooBar:"
})
```

**LLM返回纯文本**（AI输出给用户，无tool_call）：
```python
delta.content = "问题在第42行，修复方案是..."
```

### 8.2 主循环数据流

```
┌─────────────────────────────────────────────────────┐
│  agent.py 主循环                                     │
│                                                      │
│  while True:                                         │
│    1. user_input = ui.read_input()                   │
│    2. context.turn_count += 1                        │
│    3. if context.need_compress():                    │
│         → 暂存user_input，执行compressor              │
│         → 压缩完成后在新会话中处理user_input           │
│         → continue                                   │
│    4. messages.append({"role":"user","content":...}) │
│    5. stream = ui.create_stream()                    │
│    6. while True:  ← 工具调度内循环                  │
│       a. response = llm.chat(messages, tools)        │
│       b. if stream.cancelled: break ← 用户ESC中断   │
│       c. if response有tool_call:                     │
│            - result = registry.execute(tool_call)    │
│            - messages.append(tool_call结果)           │
│            - logger记录工具调用                       │
│            - continue  ← 继续内循环                  │
│       d. else:  ← 纯文本输出                        │
│            - stream.feed(response.content)            │
│            - break  ← 退出内循环                     │
│    7. stream.finish(tokens, cost)                    │
│    8. logger记录本轮统计                             │
└─────────────────────────────────────────────────────┘
```

**关键数据结构**：

- `messages`：对话历史列表，包含system/user/assistant/tool四种role
- `tool_definitions`：9个工具的JSON Schema定义，每次请求都传给LLM
- `tool_call`：LLM返回的工具调用请求，含name+arguments
- `tool_result`：工具执行结果，以role=tool回传

**流式处理**：

- **纯文本**：逐token喂给`stream.feed()`
- **tool_call**：流式返回时arguments分块到达，需累积拼接完整JSON后再执行工具。实现方式：
  ```python
  # 累积tool_call
  tool_calls_buffer = {}  # id → {name, arguments_str}
  for chunk in stream:
      if chunk.tool_calls:
          for tc in chunk.tool_calls:
              buf = tool_calls_buffer.setdefault(tc.id, {"name": "", "arguments": ""})
              buf["name"] += tc.function.name or ""       # 首块有name，后续为None
              buf["arguments"] += tc.function.arguments or ""  # 逐块拼接
      if chunk.content:
          stream.feed(chunk.content)
  # 流结束后，tool_calls_buffer中每个entry的arguments已是完整JSON，可json.loads解析执行
  ```
- **中断**：每次chunk后检查`stream.cancelled`，为True则break内循环

### 8.3 Web工具调度格式

WebSearch的LLM tool_call格式：

**WebSearch工具定义**：
```python
{
    "type": "function",
    "function": {
        "name": "WebSearch",
        "description": "联网搜索，获取实时信息或在线文档",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "num": {"type": "integer", "description": "返回结果数，默认5"},
                "lr": {"type": "string", "description": "语言限制，如lang_en/lang_zh-CN"}
            },
            "required": ["query"]
        }
    }
}
```

**WebSearch实现**：搜索引擎降级链 百度→Bing→DuckDuckGo，用requests请求，解析HTML提取标题+摘要+URL。

## 9. 项目结构

采用DDD分层，各模块各司其职，问题精准定位：

```
narnat_agent/
├── core/                       # 后端调度层（应用服务层）
│   ├── agent.py                # 主循环：读输入→调度AI→输出→循环
│   ├── llm.py                  # LLM调用：API连接、流式输出、token计数
│   ├── context.py              # 上下文管理：轮次计数、压缩触发、会话销毁/创建
│   └── compressor.py           # 压缩执行：发送压缩prompt→校验md→切换会话
│
├── tools/                      # 工具层（基础设施层）
│   ├── read.py                 # Read工具
│   ├── glob.py                 # Glob工具
│   ├── grep.py                 # Grep工具
│   ├── edit.py                 # Edit工具
│   ├── write.py                # Write工具
│   ├── bash.py                 # Bash工具
│   ├── web_search.py           # WebSearch工具
│   ├── todo_write.py           # TodoWrite工具
│   └── registry.py             # 工具注册表：名称→实现映射
│
├── ui/                         # UI层（表现层）
│   └── ui_design.py            # 流式渲染+中断+输入（已实现）
│
├── commands/                   # 命令层（表现层子模块）
│   └── session.py              # /save /show /enter /delete /clear 实现
│
├── config/                     # 配置层（基础设施层）
│   ├── loader.py               # 读取narnat.json + narnat.md，拼接系统prompt
│   ├── defaults.py             # 默认配置常量（铁律、压缩prompt模板等）
│   └── session_store.py        # 会话持久化：序列化/反序列化messages，供commands/调用
│
├── logs/                       # 日志目录（运行时生成）
│
├── logger.py                   # 日志模块：统一写入接口，按日期时间滚动文件
│
└── __init__.py

.narnat/                        # 运行时数据（项目根目录下）
├── last_session_summary.md     # 压缩总结（初始为空文件）
├── narnat.json                 # 用户调度配置
└── narnat.md                   # 用户调教指令
```

### 分层职责

| 层 | 模块 | 职责 | 依赖 |
|---|---|---|---|
| 表现层 | ui/ | 用户输入/流式输出/中断 | 无 |
| 表现层 | commands/ | /save等命令实现 | config/, logger |
| 应用服务层 | core/ | 主循环/LLM调度/上下文/压缩 | tools/, ui/, config/, logger |
| 基础设施层 | tools/ | 9个工具实现 | logger |
| 基础设施层 | config/ | 配置读取/prompt拼接/会话持久化 | 无 |
| 基础设施层 | logger | 日志记录 | 无 |

**原则**：上层依赖下层，下层不依赖上层。问题定位：哪个模块出错改哪个模块。

## 10. 日志系统

**目标**：将AI调度黑盒透明化——工具调用链、AI调度决策、压缩过程全记录，开发者可排查。

### 日志格式

```
2026-05-30 14:30:01 [core.agent]     INFO  用户输入: "帮我重构这个模块"
2026-05-30 14:30:01 [core.llm]       INFO  发送请求, tokens_in=1200
2026-05-30 14:30:02 [tools.grep]     INFO  调用: pattern="class Foo", path="src/", output_mode="content"
2026-05-30 14:30:02 [tools.grep]     INFO  结果: 3个文件匹配
2026-05-30 14:30:02 [tools.read]     INFO  调用: file_path="src/main.py"
2026-05-30 14:30:03 [core.llm]       INFO  响应完成, tokens_out=450, cost=$0.0023
2026-05-30 14:30:03 [core.agent]     INFO  AI决策: 调用Edit修改src/main.py
2026-05-30 14:30:05 [compressor]     INFO  压缩触发, 轮次=120
2026-05-30 14:30:06 [compressor]     INFO  压缩成功, 总结写入.narnat/last_session_summary.md
```

### 日志规则

- 每次启动创建新日志文件：`logs/YYYY-MM-DD_HH-MM-SS.log`
- 记录内容：工具调用（参数+结果）、AI调度决策（调哪个工具+为什么）、压缩过程（触发/成功/失败）、LLM交互（token数/耗时/费用）
- 级别：DEBUG / INFO / WARNING / ERROR
- 不记录用户敏感数据（API key等脱敏）

## 11. 测试要求

### 测试框架

pytest，所有测试放在项目根目录 `tests/` 下。

### 测试目录

```
tests/
├── test_tools/                 # 工具暴力测试（每个工具一个文件）
│   ├── test_read.py            # 正常读/大文件/不存在/权限不足/二进制
│   ├── test_glob.py            # 各种pattern/空结果/深层目录/忽略目录
│   ├── test_grep.py            # 正则/非法正则/大小写/上下文/多模式/大结果集
│   ├── test_edit.py            # 精确匹配/多处匹配/未找到/相似行/空old_string
│   ├── test_write.py           # 新建/覆写/自动建目录/空内容
│   ├── test_bash.py            # 正常命令/超时/删除确认/交互式拦截/长输出截断
│   ├── test_web_search.py      # 正常搜索/降级链/无结果
│   └── test_todo_write.py      # 正常创建/状态转换/多任务/边界校验
│
├── test_core/                  # 核心调度测试
│   ├── test_llm.py             # API连接/流式输出/token计数/错误重试
│   ├── test_context.py         # 轮次计数/阈值触发/会话销毁创建
│   └── test_compressor.py      # 压缩成功/压缩失败(未压缩标记)/校验逻辑
│
├── test_commands/              # 命令测试
│   └── test_session.py         # save/show/enter/delete各场景
│
└── test_config/                # 配置测试
    └── test_loader.py          # json解析/md读取/prompt拼接
```

### 测试标准

1. **暴力测试**：每个模块必须覆盖正常路径+边界条件+异常场景，全绿才算通过
2. **算法优雅**：O(n²)→O(n)、冗余遍历→直接查找，算法不优雅不算完成
3. **AI自测**：AI实现完毕后必须自己写Python测试脚本并执行，确认全绿
4. **准入门槛**：`pytest tests/ -v` 全部PASS，方可认为模块开发完成