# Narnat Agent 架构重构总结

> 重构日期: 2025-07 | 策略: 纯 OOP，算法零改动

---

## 一、重构目标

```
稳定 — 算法逻辑逐行搬运，行为完全一致
可维护 — Bug 定位到单一对象，打开 ~200 行文件
可扩展 — 新增模块像插拔积木，不改核心代码
```

---

## 二、文件结构（重构后）

```
narnat_agent/
├── assembly.py                  ← 唯一组装点（NEW）
├── config/
│   ├── defaults.py              ← 默认常量
│   └── loader.py                ← JSON解析 → Config（重构）
├── core/
│   ├── agent.py                 ← 编排者 ~100行（重构）
│   ├── agent_loop.py            ← LLM↔工具内循环（NEW）
│   ├── auto_save_manager.py     ← 后台自动保存（NEW）
│   ├── billing.py
│   ├── compression_coordinator.py ← 上下文压缩（NEW）
│   ├── compressor.py
│   ├── context.py
│   ├── interrupt.py
│   ├── llm.py                   ← 消除跨层依赖（重构）
│   ├── message_list.py          ← 消息唯一所有者（NEW）
│   ├── message_manager.py       ← 委托 MessageList（重构）
│   ├── session_callbacks.py     ← 直接持有对象引用（重构）
│   ├── stats.py
│   ├── summarizer.py            ← LLM摘要命名（NEW）
│   ├── tool_callbacks.py
│   └── tool_dispatcher.py
├── logger.py
├── output.py
├── tools/
│   ├── bash/
│   ├── diff_utils.py
│   ├── edit/
│   ├── glob/
│   ├── grep/
│   ├── read/
│   ├── registry.py
│   ├── terminal/
│   ├── todo_write/
│   ├── tool_context.py
│   ├── web_search/
│   └── write/
└── ui/
    ├── colors.py                ← 文档更新（重构）
    ├── interrupt.py
    ├── renderer.py
    ├── session_commands.py
    └── ui_design.py
```

---

## 三、五个阶段

### Phase 1 — 配置统一

**问题:** `AppConfig` 扁平 30 字段，单位转换散落在 `Agent.__init__`，新增配置需改 4-5 个文件。

**解决:**

```
AppConfig(扁平30字段)
  → Config(分组只读)
      ├── ai: AIConfig          # 模型/密钥/protocol/retry_count
      ├── paths: PathConfig     # 所有路径（frozen）
      ├── tools: ToolConfig     # SSH会话/传输上限/输出上限/忽略目录（frozen）
      ├── safety: SafetyConfig  # git/rm确认开关（frozen）
      ├── plan: PlanConfig      # 计划优先开关（frozen）
      ├── session: SessionConfig# 自动保存/压缩阈值（frozen）
      ├── pricing, balance, ui  # 保持原结构（frozen）
      ├── api_keys: dict
      └── system_prompt: str
```

**关键改进:**
- 单位转换（KB→字符数）移到 `load_config()` 内部
- `tools.ignore_dirs` 改为 `tuple`（不可变）
- `llm_retry_count` → `ai.retry_count`
- 新增配置项只需改 2 个文件

### Phase 2 — 消息所有权

**问题:** `messages` 列表被 Agent / MessageManager / SessionManager 三处通过闭包共享引用。

**解决:**

```
MessageList（唯一所有者）
  ├── _messages: list[dict]     ← 私有属性
  ├── view() → MessageView      ← 只读视图（零拷贝）
  ├── append_user/assistant/tool/...  ← 受控修改接口
  └── replace_all()             ← 原子替换（会话切换）

SessionManager 不再通过 lambda 获取引用
  → 直接持有 MessageList 对象
```

**数据流:** `LLMClient` 调用 → `MessageList.view().to_list()`（浅拷贝）→ HTTP 请求。外部无法获取内部列表引用。

### Phase 3 — Agent 拆分

**问题:** Agent 承担 8 项职责，`__init__` 80 行手工 DI，12 个 lambda 回调。

**解决:**

```
Agent 526行 → 98行纯编排者

提取的对象:
  Assembly              ← 唯一组装点，依赖顺序就是代码顺序
  AgentLoop             ← LLM↔工具调度内循环
  Summarizer            ← LLM 命名会话 + 总结探索分支
  AutoSaveManager       ← 后台自动保存
  CompressionCoordinator ← 上下文压缩流程
```

### Phase 4 — 消除跨层依赖

**问题:** `core/llm.py` 通过 `from ..tools.registry import get_tool_definitions` 反向依赖工具层。

**解决:** 工具定义通过构造参数注入：

```python
# Before (llm.py)
from ..tools.registry import get_tool_definitions
self._tool_defs = get_tool_definitions()

# After (llm.py)
def __init__(self, ..., tool_definitions: list = None):
    self._tool_defs = tool_definitions or []

# Assembly 负责桥接
from .tools.registry import get_tool_definitions
llm = LLMClient(..., tool_definitions=get_tool_definitions())
```

### Phase 5 — 中断系统评估

**结论:** 保留原样。中断系统涉及多线程竞争（poll线程 + 主线程 + 后台线程），任何时序变化都可能导致中断失效。当前稳定实现不改动。

---

## 四、完整流程图

### 4.1 总览 — 对象与依赖关系

```
                        ┌─────────────┐
                        │   Assembly   │
                        │  (唯一组装点) │
                        └──────┬──────┘
                               │ 构建并注入依赖
                               ▼
    ┌──────────────────────────────────────────────────┐
    │                    Agent                          │
    │               (编排者 ~98行)                       │
    │  读输入 → 判断命令/对话 → 委托 → 循环              │
    └───┬──────────────┬──────────────┬────────────────┘
        │              │              │
        ▼              ▼              ▼
  ┌───────────┐  ┌───────────┐  ┌───────────────┐
  │ UIInterface│  │  Session  │  │    Agent      │
  │           │  │  Manager  │  │     Loop      │
  │ 输入/输出  │  │ (状态机)  │  │ (对话内循环)   │
  │ 渲染/中断  │  │ 会话切换   │  │ LLM↔工具调度  │
  └─────┬─────┘  └─────┬─────┘  └──────┬────────┘
        │               │               │
        │          ┌────┴────┐          │
        │          ▼         ▼          │
        │   ┌──────────┐┌──────────┐   │
        │   │ Context  ││ Context  │   │
        │   │Compressor││ Tracker  │   │
        │   │ (压缩)   ││ (轮次)   │   │
        │   └────┬─────┘└──────────┘   │
        │        │                      │
        │        ▼                      ▼
        │   ┌─────────────────────────────────┐
        │   │         MessageList              │
        │   │      (消息唯一所有者)             │
        │   │                                 │
        │   │  .view() → MessageView(只读)     │
        │   │  .append_user/assistant/tool()  │
        │   │  .repair() / .replace_all()     │
        │   └─────────────────────────────────┘
        │                      ▲    ▲
        │                      │    │
        │                      │    │
        │   ┌──────────┐       │    │     ┌──────────────┐
        │   │   LLM    │───────┘    │     │ToolDispatcher │
        │   │  Client  │  读视图     │     │  (调度策略)   │
        │   │          │            │     └──────┬───────┘
        │   │ 持有可变  │←───────────┘            │
        │   │ thinking │              ┌──────────┴──────────┐
        │   │ _effort  │              │                     │
        │   └──────────┘         ┌────▼─────┐        ┌──────▼──────┐
        │                        │DeleteGuard│       │ToolRegistry  │
        │                        │(删除确认) │       │ name→Tool    │
        │                        └──────────┘        └──────┬──────┘
        │                                                   │
        │                                    ┌──────┬───────┼───────┬──────┐
        │                                    ▼      ▼       ▼       ▼      ▼
        │                               ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐
        │                               │ Read ││ Glob ││ Grep ││ Edit ││Write │
        │                               └──┬───┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘
        │                                  │       │       │       │       │
        │                                  └───────┴───┬───┴───────┘       │
        │                                              │                   │
        │                                    ┌────────┴────────┐          │
        │                                    ▼                 ▼          ▼
        │                              ┌──────────┐     ┌──────────┐┌──────────┐
        │                              │FileTracker│    │SafetyPolicy││BashTool │
        │                              │(已读追踪) │     │(确认策略)  ││Terminal │
        │                              └──────────┘     └──────────┘│WebSearch│
        │                                                          │TodoWrite│
        │                                                          └──────────┘
        │
        ▼
  ┌───────────┐
  │InterruptBus│ ← ESC键触发（保留现有中断系统）
  └─────┬─────┘
        │ 通知所有注册者
        ├─→ LLMClient.interrupt()    (关HTTP连接)
        ├─→ BashTool.interrupt()     (kill子进程)
        └─→ TerminalTool.interrupt() (kill远程执行)
```

### 4.2 主循环 — Agent.run()

```
                        ┌─────────────┐
                        │ Agent.run() │
                        └──────┬──────┘
                               ▼
                    ┌──────────────────┐
                    │  UI.read_input() │◄─────────────────────┐
                    └────────┬─────────┘                      │
                             ▼                                │
                      ┌────────────┐                         │
                      │  空输入?   │──Yes──→ 跳过 ──────────→│
                      └─────┬──────┘                         │
                            │ No                              │
                            ▼                                │
                   ┌─────────────────┐                      │
                   │ 以 "/" 开头?     │                      │
                   └────┬───────┬────┘                      │
                   Yes  │       │  No                        │
                        ▼       ▼                           │
              ┌──────────────┐  ┌────────────────────┐      │
              │UI.dispatch   │  │ ContextTracker     │      │
              │_command()    │  │ .increment()       │      │
              └──────┬───────┘  └────────┬───────────┘      │
                     │                   │                  │
                     ▼                   ▼                  │
              ┌─────────────┐    ┌───────────────┐         │
              │ result==EXIT?│   │need_compress? │         │
              └──┬──────┬───┘    └──┬────────┬───┘         │
              Yes│      │No      Yes│        │No           │
                 ▼      │           ▼        │             │
          ┌──────────┐  │   ┌──────────────┐ │             │
          │SessionMgr│  │   │Compression   │ │             │
          │.on_exit()│  │   │Service       │ │             │
          └────┬─────┘  │   │.compress()   │ │             │
               ▼        │   └──┬───────┬───┘ │             │
          ┌────────┐    │      │成功?   │失败  │             │
          │ return │    │      ▼       ▼     │             │
          └────────┘    │  继续   │     跳过──→│─────────────│
                        ▼        │           │             │
              ┌──────────────┐  │           │             │
              │ AgentLoop    │  │           │             │
              │ .run_turn()  │  │           │             │
              └────────┬─────┘  │           │             │
                       │        │           │             │
                       ▼        │           │             │
                 (见下方详细图)  │           │             │
                       │        │           │             │
                       └────────┴───────────┴─────────────┘
                                          (下一轮)
```

### 4.3 对话内循环 — AgentLoop.run_turn()

```
              ┌─────────────────────────┐
              │ AgentLoop.run(stream)   │
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │ MessageManager.repair() │     ← 修复中断后的不完整序列
              └───────────┬─────────────┘
                          ▼
         ┌────────────────────────────────────────┐
         │            内循环开始                     │
         └───────────┬─────────────────────────────┘
                     ▼
         ┌────────────────────────────────────────┐
         │     LLMClient.chat_stream(             │
         │         MessageManager.view.to_list()  │     ← 浅拷贝传入
         │     )                                  │
         └───────────┬────────────────────────────┘
                     │  stream chunks
                     ▼
         ┌───────────────────────────────────────────┐
         │           遍历 chunks                      │
         └───┬──────────┬──────────┬────────┬────────┘
             │          │          │        │
     content │   tool_calls     usage   finish_reason
             │          │          │        │
             ▼          │          │        │
    ┌──────────────┐    │          │        │
    │UI.feed(text)│    │          │        │   ← 渲染AI输出
    │content_parts │    │          │        │     追加到缓冲区
    │.append(text) │    │          │        │
    └──────────────┘    │          │        │
                        │          │        │
    ┌───────────────────┴──────────┴────────┴───────┐
    │              chunk遍历结束                      │
    └───────────┬───────────────────────────────────┘
                ▼
        ┌───────────────┐
        │ 有tool_calls? │
        └──┬───────┬────┘
       Yes │       │ No
           ▼       ▼
  ┌────────────┐  ┌───────────────────────────────┐
  │MessageMgr  │  │ 有 content?                   │
  │.append_    │  └──┬──────────────────────────┬─┘
  │ assistant( │  Yes│                          │No
  │  content,  │     ▼                          ▼
  │  tool_calls│  ┌──────────┐           ┌──────────────┐
  │ )          │  │MessageMgr│           │空回复处理     │
  └─────┬──────┘  │.append_  │           │(stop/max_    │
        │         │assistant │           │tokens/error) │
        ▼         │(content) │           └──────┬───────┘
  ┌──────────────────────────┐                   │
  │ ToolDispatcher.execute() │                   │
  │ (tool_calls, stream)     │                   │
  └──────────┬───────────────┘                   │
             ▼                                   │
  ┌──────────────────────────────────┐           │
  │     工具调度（见下方详细图）       │           │
  └──────────┬───────────────────────┘           │
             │                                   │
             ▼                                   │
  ┌──────────────────────┐                       │
  │ 有await_confirm?     │                       │
  └──┬──────────────┬────┘                       │
   No│              │Yes                         │
     │              ▼                            │
     │    ┌──────────────────┐                   │
     │    │ _handle_delete   │                   │
     │    │ _confirm()       │                   │
     │    │                  │                   │
     │    │ 1.结束当前流      │                   │
     │    │ 2.UI读用户确认    │                   │
     │    │ 3.确认→重新执行   │                   │
     │    │ 4.创建新流        │                   │
     │    └────────┬─────────┘                   │
     │             │                             │
     ▼             ▼                             │
  ┌──────────────────────────┐                   │
  │ 对每个tool_result:       │                   │
  │ MessageManager.append_   │                   │
  │   tool_result(id, text)  │   ← 唯一修改入口   │
  └──────────┬───────────────┘                   │
             │                                   │
             ▼                                   │
  ┌──────────────────┐                           │
  │ StatsTracker     │                           │
  │ .update(usage)   │                           │
  └──────────┬───────┘                           │
             │                                   │
             ▼                                   │
     ┌───────────────┐                           │
     │ 有tool_call？  │                           │
     │ 继续内循环 ──→│ (回到LLM调用)              │
     └───────────────┘                           │
                                                 │
     ┌───────────────────────────────────────────┘
     │
     ▼
  ┌──────────────────────────────┐
  │ UI.stream.finish(            │
  │   input_tokens,              │
  │   output_tokens,             │
  │   cost, balance,             │
  │   thinking_effort)           │
  └──────────────┬───────────────┘
                 ▼
  ┌──────────────────┐
  │ 内循环结束        │
  └──────────┬───────┘
             ▼
  ┌──────────────────────────────┐
  │ SessionManager.on_auto_save()│
  │ (如有活跃会话则持久化)         │
  └──────────────────────────────┘
```

### 4.4 工具调度 — ToolDispatcher.execute()

```
         ┌─────────────────────────────────────┐
         │ ToolDispatcher.execute(tool_calls)  │
         └────────────────┬────────────────────┘
                          ▼
         ┌─────────────────────────────────────┐
         │ 1. 计划优先检查                       │
         │    PlanConfig.require_plan == True?  │
         │    且 工具数 >= min_tools?            │
         │    且 TodoTracker.has_active == False?│
         └────────┬────────────────────────────┘
                  │
            需要   │   不需要
        拦截 ◄────┴────► 继续
            │                │
            ▼                ▼
     ┌────────────┐  ┌──────────────────────────┐
     │返回拦截提示 │  │ 2. 解析tool_calls         │
     │给所有工具   │  │    提取 id, name, args    │
     └────────────┘  └────────────┬─────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │ 3. 按分类分组                  │
                    └──┬──────────┬────────────┬───┘
                       │          │            │
              只读工具  │   写入工具 │    串行工具 │
            (Read,Glob,│ (Edit,    │(Shell,    │
             Grep,Web) │  Write)   │ Terminal, │
                       │           │ TodoWrite)│
                       ▼           ▼            ▼
              ┌────────────┐┌────────────┐┌────────────┐
              │ 阶段1:     ││ 阶段2:     ││ 阶段3:     │
              │ 全部并行   ││ 按文件分组  ││ 逐个串行   │
              │            ││ 同文件串行  ││            │
              │ ThreadPool ││ 不同文件并行││            │
              │ submit     ││            ││            │
              └──────┬─────┘└──────┬─────┘└──────┬─────┘
                     │             │             │
                     ▼             ▼             ▼
              ┌─────────────────────────────────────┐
              │  每个工具执行:                        │
              │                                      │
              │  stream.pause_spinner()              │
              │  stream.flush_renderer()             │
              │  显示工具调用摘要                     │
              │                                      │
              │  ToolRegistry.get(name)              │
              │      .execute(args)                  │
              │           │                          │
              │           ▼                          │
              │      ┌─────────┐                     │
              │      │ToolResult│                    │
              │      │.llm_text│                     │
              │      │.display │                     │
              │      │.await_  │                     │
              │      │ confirm │                     │
              │      └─────────┘                     │
              │                                      │
              │  显示diff（如有）                     │
              │  stream.resume_spinner()             │
              └──────────────────────────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────────┐
              │ 返回 [(tool_call_id, ToolResult)]    │
              │ 按原始顺序排列                        │
              └─────────────────────────────────────┘
```

### 4.5 关键场景

#### 4.5.1 ESC 中断

```
用户按 ESC
    │
    ▼
┌─────────────────────────┐
│ InterruptHandler        │
│ (UI层键盘事件)           │
│ _interrupt_ctrl.set()   │
└───────────┬─────────────┘
            │
            ├─→ interrupt.py: abort_request()
            │       └─→ llm.py: abort_active_llm_request()
            │           └─→ _active_llm_response.close()
            │               (HTTP连接断开)
            │
            └─→ ToolDispatcher 检测 _interrupt_ctrl.is_set
                    ├─→ bash.kill_active()      (kill子进程)
                    └─→ terminal.kill_active_exec() (kill远程执行)

    ┌───────────────────────────────────────────┐
    │ AgentLoop 检测到 stream.cancelled          │
    │  ├─→ MessageManager.append_interrupted_tools│
    │  │     (为未完成的tool补"[用户中断]")      │
    │  ├─→ stream.abort()                       │
    │  ├─→ UI.on_interrupted()                  │
    │  └─→ 退出内循环，回到 Agent.run() 主循环   │
    └───────────────────────────────────────────┘
```

#### 4.5.2 上下文压缩

```
Agent.run() 主循环
    │
    ▼
ContextTracker.need_compress() == True
    │
    ▼
┌─────────────────────────────────────┐
│ CompressionCoordinator.compress(input)│
└──────────────────┬──────────────────┘
                   ▼
        ┌────────────────────┐
        │ UI.begin_compressing│
        │ (显示压缩动画)      │
        └──────────┬─────────┘
                   ▼
        ┌────────────────────────────┐
        │ MessageManager.handle_     │
        │   compress()                │
        │                            │
        │ 构建压缩messages:           │
        │  MessageList.view().to_list│
        │  + 追加 COMPRESS_PROMPT     │
        └────────────┬───────────────┘
                     ▼
        ┌────────────────────────────┐
        │ LLMClient.chat_stream(     │
        │   compress_msgs,           │
        │   no_tools=True            │
        │ )                          │
        └────────────┬───────────────┘
                     │
                     ▼
              ┌──────────┐
              │ 收集summary│
              └─────┬────┘
                    │
              ┌─────┴──────┐
              │ summary为空?│
              └──┬──────┬───┘
              Yes│      │No
                 ▼      ▼
          ┌──────────┐ ┌────────────────────────┐
          │失败处理  │ │MessageList.replace_all( │
          │重试10轮后│ │  system + summary +    │
          │再压缩    │ │  user_input)           │
          │ContextTra│ └───────────┬────────────┘
          │cker.     │            │
          │set_retry_│            ▼
          │soon()    │   ┌────────────────┐
          └──────────┘   │ContextTracker  │
                         │.reset()        │
                         ├────────────────┤
                         │ToolContext     │
                         │.clear_read()   │
                         ├────────────────┤
                         │UI.end_         │
                         │ compressing()  │
                         └────────────────┘
                                 │
                                 ▼
                        ┌────────────────┐
                        │ return True    │
                        │ → 继续对话轮    │
                        └────────────────┘
```

#### 4.5.3 会话切换（/cd 命令）

```
用户输入: /cd feature-refactor
    │
    ▼
┌──────────────────────────────────────────┐
│ UI.dispatch_command → SessionManager     │
│   .enter("feature-refactor")             │
└──────────────────┬───────────────────────┘
                   ▼
         ┌────────────────────┐
         │ 当前状态._persist() │  ← 先保存当前会话
         └────────┬───────────┘
                  ▼
         ┌────────────────────────┐
         │ resolve_session_name() │
         │ 解析名称→找到目标会话   │
         └────────┬───────────────┘
                  ▼
         ┌────────────────────────┐
         │ session_store.load()   │
         │ 从磁盘读取目标会话      │
         └────────┬───────────────┘
                  ▼
         ┌────────────────────────────────┐
         │ MessageList.replace_all(       │  ← 唯一批量替换入口
         │   new_messages)                │
         └────────┬───────────────────────┘
                  │
                  ├─→ ContextTracker.sync_from_messages()
                  │     (根据消息数同步轮次)
                  │
                  └─→ ToolContext.clear_read_files()
                        (清空已读文件追踪)
                  ▼
         ┌────────────────────────┐
         │ 创建新状态对象          │
         │ switch_state(new_state)│
         └────────┬───────────────┘
                  ▼
              返回给用户:
              "已进入会话: feature-refactor"
```

#### 4.5.4 删除确认（Linux/macOS）

```
工具执行: Shell("rm -rf /tmp/test")
    │
    ▼
┌─────────────────────────────────────┐
│ BashTool.execute(args)              │
│                                     │
│ rm_skip_confirm == False → 需确认   │
│ 设置 ToolContext.pending_delete     │
│ 返回 AWAIT_CONFIRM                  │
└────────────────┬────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────┐
│ AgentLoop 检测到 pending_delete     │
│                                     │
│ 1. 先回传其他工具结果               │
│ 2. 结束当前流式输出                 │
│ 3. UI 读用户确认                    │
│    "确认执行此命令? [y/N]: "       │
└────────────────┬────────────────────┘
                 │
          ┌──────┴──────┐
          ▼             ▼
       用户确认       用户取消
          │             │
          ▼             ▼
┌──────────────┐ ┌──────────────────┐
│_delete_      │ │返回: "操作已取消" │
│ confirmed    │ └────────┬─────────┘
│ = True       │          │
│              │          ▼
│重新执行工具   │ ┌──────────────────┐
│tool_execute  │ │ MessageManager   │
│(跳过下次检测) │ │ .append_tool_    │
└──────┬───────┘ │  result(id,取消)  │
       │         └──────────────────┘
       ▼
┌──────────────┐
│ MessageManager│
│ .append_tool_ │
│ result(id,    │
│  执行结果)     │
└──────┬───────┘
       │
       ▼
┌──────────────────────┐
│ UI.create_stream()   │
│ (新流式输出)          │
└──────────────────────┘
       │
       ▼
   继续内循环
```

#### 4.5.5 Assembly 构建顺序

```
Assembly.build()
  │
  ├─ 1. Config.load()          ← 读取JSON + 拼接prompt + 单位转换
  ├─ 2. apply_style(config)    ← UI配色
  ├─ 3. set_max_sessions()     ← 终端会话数（过渡期全局setter）
  │    set_retry_count()       ← LLM重试次数（过渡期全局setter）
  ├─ 4. Logger                ← 日志
  ├─ 5. get_tool_definitions()← 工具定义（桥接点）
  ├─ 6. LLMClient             ← LLM客户端
  ├─ 7. ContextTracker        ← 轮次计数
  ├─ 8. Compressor            ← 压缩器
  ├─ 9. MessageList           ← 消息唯一所有者
  ├─ 10. MessageManager       ← 消息管理
  ├─ 11. Summarizer           ← LLM摘要命名
  ├─ 12. SessionManager       ← 会话状态机
  ├─ 13. UIInterface          ← 输入/输出/渲染
  │     └─ 补充 session_mgr 的 UI 回调
  ├─ 14. ToolContext          ← 工具运行时上下文
  ├─ 15. ToolDispatcher       ← 工具调度器
  ├─ 16. StatsTracker         ← 费用追踪
  ├─ 17. AutoSaveManager      ← 自动保存
  ├─ 18. CompressionCoordinator ← 压缩协调器
  ├─ 19. AgentLoop            ← 对话内循环
  └─ 20. Agent                ← 编排者
         └─ Agent.run()       ← 启动主循环
```

---

## 五、架构核心原则

```
1. 单一职责 — 每个对象做一件事
2. 封装 — 数据有明确所有者，修改通过所有者接口
3. 依赖注入 — 对象通过构造参数声明依赖，不在内部new
4. 组装点单一 — Assembly 是唯一知道全部依赖关系的地方
5. 接口抽象 — 为未来扩展预留（Tool Protocol）
```

---

## 六、数据所有权

| 数据 | 所有者 | 谁能读 |
|------|--------|--------|
| messages列表 | MessageList | 通过 view() 获取只读视图 |
| thinking_effort | AIConfig（过渡）/ 未来归 LLMClient | 通过属性读取 |
| 已读文件集合 | ToolContext.read_files | 通过 ToolContext |
| todo状态 | ToolContext.current_todos | 通过 ToolContext |
| 轮次计数 | ContextTracker | 通过属性读取 |
| 费用统计 | StatsTracker | 通过属性读取 |
| 会话状态 | SessionManager._state | 通过 state 属性 |
| 配置 | Config（除 AIConfig 外 frozen） | 所有人可读 |

---

## 七、关键数据

```
新增文件: 6 个（assembly, agent_loop, auto_save_manager,
                  compression_coordinator, summarizer, message_list）
重构文件: 6 个（loader, agent, message_manager, session_callbacks, llm, colors）
算法改动: 0 行
Agent 代码: 526行 → 98行（-81%）
SessionManager 回调: 12个 lambda → 5个对象引用
```

---

## 八、场景验证

### 场景 A：新增一个 CodeSearch 工具

```
1. 创建 tools/code_search/__init__.py
2. Assembly 中加一行: registry.register(CodeSearchTool(依赖...))
→ 不修改 Agent、AgentLoop、ToolDispatcher
```

### 场景 B：新增一个配置项 max_file_size_mb

```
1. defaults.py: 加默认值
2. loader.py: ToolConfig 加字段 + JSON 解析
→ 不修改 Agent、Assembly
```

### 场景 C：支持第三种 LLM 协议

```
1. LLMClient 内部加 backend 分支
→ 不影响其他对象
```

### 场景 D：Bug 定位

```
对话中断后行为异常 → 打开 message_manager.py（repair 逻辑）
工具调度结果乱序 → 打开 tool_dispatcher.py（并行策略）
会话切换后轮次不对 → 打开 session_callbacks.py（sync_from_messages）
```
