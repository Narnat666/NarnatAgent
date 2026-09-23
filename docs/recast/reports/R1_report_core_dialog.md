# R1 现状报告：core 对话链路（LLM 客户端 / 主循环 / 上下文压缩）

> 调研对象：`narnat_agent/core/{llm,agent_loop,agent,context,compressor,compression_coordinator,summarizer,interrupt}.py`
> 调研方式：逐文件完整通读（Read 全文）+ grep 复核依赖 + `python -m pyflakes` 复核静态问题 + `git log` 只读查历史。
> 只读任务：本报告之外未创建/修改/删除任何文件，未执行任何 git 写操作。
> 基线版本：git HEAD `7d075a2 [更新] 新增上下文主动压缩功能`（工作区 core/ 无未提交改动，`git status --short` 无 core 相关输出）。

## 阅读约定

- 所有行号均为**该文件内行号**，格式 `文件:行`；跨文件引用标明文件名。
- 行数与任务清单的差异（实测值）：`llm.py 1107`、`agent_loop.py 488`、`agent.py 276`、`context.py **76**（任务清单写 80）`、`compressor.py 107`、`compression_coordinator.py 180`、`summarizer.py **58**（清单写 50）`、`interrupt.py **22**（清单写 25）`。本报告一律用实测行数。
- 标注「未验证」= 静态阅读可得但不能从代码确证（需运行/外部服务才能判定）的事项。
- `tool_exp/` 目录下的脚本是**实验/验证脚本**（不在 `narnat_agent` 包内，不随产品分发），在「被依赖」中单独归类，不影响产品契约，但反映历史的 API 面变化。
- 本组 8 个文件的公共依赖（非本组，但被反复引用）：`core/message_manager.py`（压缩编排 + `CompressResult`）、`core/message_list.py`（`MessageList`/`MessageView`/`SYNTHETIC_THINKING`）、`config/defaults.py`（阈值常量、`COMPRESS_PROMPT`、thinking 查表）、`ui/ui_design.py`（`UIInterface`/`UIStreamSession`）、`ui/interrupt.py`（`_interrupt_ctrl` 全局单例）。

---

### narnat_agent/core/llm.py（1107行）

**职责**：LLM 调用层——按 `AIConfig.protocol` 在 OpenAI 兼容与 Anthropic 兼容两个后端间二选一，统一对外产出 OpenAI 格式的流式 chunk，内含重试、思考参数构造、思考回传、工具表热更新、中断挂点与空转看门狗。

**对外接口**（public 类/函数/方法，逐个列签名）：

- `def _strip_surrogates(obj)`：递归剥离字符串中的代理字符（非法 UTF-8 序列），防止发送请求时编码失败。
- `def retry_sleep(attempt: int, cancel_check=None) -> bool`：指数退避休眠（含 jitter 与中断检查），返回 False 表示用户取消。
- `def _retry_notice(attempt: int, max_retries: int, reason: str = "网络连接失败") -> Dict[str, str]`：构造重试提示事件 `{"retry_notice": "..."}`（供上层渲染、不入历史）。
- `def _classify_stream_error(e: Exception) -> Dict[str, str]`：把流读取异常归类为 `{"kind": "timeout"|"network"|"unknown", "detail": str(e)[:200]}`。
- `def _is_retryable_http(status: int) -> bool`：`408/409/>=500` 视为可重试。
- `def _is_context_overflow(text: str) -> bool`：判断 400 响应文本是否含上下文超限特征。
- `def _user_has_tool_result(user_msg: Dict[str, Any]) -> bool`：转换后的 user 消息内容块中是否含 `tool_result`。
- `def abort_active_llm_request()`：关闭当前活跃的 HTTP 连接/流（ESC 中断挂点）。
- `def _iter_to_queue(iterator, q, err_box=None, data_ts=None)`：后台线程函数，把迭代器元素逐个入队，末尾放哨兵 `LLMClient._STREAM_END`；异常记入 `err_box`。
- `class LLMClient`（类级状态见「状态」）：
  - `@classmethod def set_retry_count(cls, n: int) -> None`：设置网络与 429 重试次数上限（钳制 1–10）。
  - `def __init__(self, config: AIConfig, logger=None, max_output_tokens: int = 128000, tool_definitions: list = None)`：按协议实例化后端，复制工具定义列表。
  - `def chat_stream(self, messages: List[Dict[str, Any]], no_tools: bool = False, no_thinking: bool = False, cancel_check=None) -> Iterator`：剥代理字符后转发给后端流式接口。
  - `def set_goal_tool(self, enabled: bool) -> None`：动态注入/移除 GoalComplete 工具定义（幂等）。
  - `def add_tool_definitions(self, definitions: list) -> None`：按工具名去重热追加工具定义（MCP 连接时用）。
  - `def remove_tool_definitions(self, names) -> None`：按名热移除工具定义（MCP 断开时用）。
  - `@property def raw_sse(self)`：返回后端最近一次流的原始 SSE data 行（无则 None）。
- `class _OpenAIBackend`：
  - `def __init__(self, config, tool_defs, logger)`：构造 `openai.OpenAI` 客户端（`max_retries=0`，重试自管）。
  - `def _prepare_messages(self, messages)`：内部消息 → OpenAI 请求消息（剥离 thinking，必要时写回 `reasoning_content`）。
  - `def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None)`：OpenAI 流式请求 + 解析（生成器）。
- `class _AnthropicBackend`：
  - `def __init__(self, config, tool_defs, logger, max_output_tokens=128000)`：构造 httpx 请求参数（URL/头/原始 SSE 缓冲）。
  - `def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None)`：Anthropic 流式请求 + SSE 解析（生成器）。
  - `def _convert_messages(self, messages)`：OpenAI → Anthropic 消息转换，返回 `(system_text, anthropic_msgs)`。
  - `def _convert_tools(self, tool_defs)`：OpenAI 工具定义 → Anthropic `input_schema` 形式。

**依赖**（本文件 import 的内部模块，附行号）：

- `..config.loader.AIConfig`（22）
- `..config.defaults.resolve_thinking_params, resolve_thinking_passback`（23）
- `.interrupt.register_abort`（24）
- `.message_list.SYNTHETIC_THINKING`（25）
- 函数内延迟导入：`openai.{APIConnectionError, APITimeoutError}`（79）、`openai.OpenAI`（266）、`openai.{APIStatusError, APIConnectionError, APITimeoutError}`（308）、`..tools.goal_complete.DEFINITION`（214）
- 第三方：`httpx`（19，模块级，两个后端共用）

**被依赖**（谁 import 本文件；grep 可复验）：

产品代码（`narnat_agent/`）：
- `narnat_agent/assembly.py:12` `from .core.llm import LLMClient`（65 构造、74 接线 MCP 工具热更新、133 注入 `goal_tool_setter`）
- `narnat_agent/core/agent_loop.py:11` `from .llm import LLMClient, retry_sleep`（110 调 `chat_stream`、82 调 `set_retry_count`、275-276 读 `RETRY_BACKOFF_BASE`、292 调 `retry_sleep`、476 读 `raw_sse`）
- `narnat_agent/core/compression_coordinator.py:13` `from .llm import LLMClient`（仅类型标注，37；实际调用经 `message_manager.handle_compress` 的 `llm_client` 入参）
- `narnat_agent/core/summarizer.py:9` `from .llm import LLMClient`（17 构造参数、26/48 调 `chat_stream`）
- 间接消费方（鸭子类型，按 import 图不到本模块，但契约相关）：`core/message_manager.py:152` `llm_client.chat_stream(compress_messages, no_tools=True, cancel_check=cancel_check)`

实验脚本（`tool_exp/`，非产品）：
- `tool_exp/api_contract_test.py:14`、`tool_exp/stall_test.py:20`、`tool_exp/repair_seq_test.py:19`、`tool_exp/retry_unify_probe.py:12`、`tool_exp/verify_retry_resilience.py:16,17`、`tool_exp/verify_stream_fix.py:14,15`、`tool_exp/mcp_test/demo_persistence.py:18`、`tool_exp/mcp_test/test_mcp_runtime.py:233`、`tool_exp/retry_notice_probe.py:20`、`tool_exp/verify_goal_e2e.py:22`、`tool_exp/verify_goal_mode.py:57`、`tool_exp/verify_import_cleanup.py:17`

**状态**：

- 模块级全局（可变）：
  - 无模块级可变变量；但存在**导入副作用**：`register_abort(abort_active_llm_request)`（140）在 import 时把回调写入 `core.interrupt._abort_callback`。
- 类变量（`LLMClient`，全部进程级共享）：
  - `_active_response = None`（171）：读 132；写 315、346、384、401、409、538、617、631、653、656、670、673、683、694、712、721、982。
  - `_STREAM_END = object()`（172）：哨兵身份常量，读 445、769（写入队列：160、747）。
  - `RETRY_BACKOFF_BASE = [1, 2, 4, 8, 8]`（173）：读 45、61、`agent_loop.py:275-276`；无可变写点（列表内容理论上可被外部改写，未验证存在此类调用）。
  - `_STREAM_STALL_SECONDS = 180.0`（174）：读 437、439、761、763；生产代码无写点，外部测试改写（`tool_exp/stall_test.py:111,146`）。
  - `_max_network_retries = 3`（175）、`_max_rate_retries = 5`（176）：读 362、366、370、374、387、391、647、651、657、664、668、674、697、701；写 181-182。
- 实例状态：
  - `LLMClient`：`_config`（186）、`_logger`（187）、`_tool_defs`（189，**与后端共享同一列表对象**，198/200 传入）、`_max_output_tokens`（190，写入后无读取=死状态）、`_protocol`（195，写入后无读取=死状态）、`_backend`（198/200）。
  - `_OpenAIBackend`：`_config`、`_tool_defs`、`_logger`、`_client`（267-274）。
  - `_AnthropicBackend`：`_config`、`_tool_defs`、`_logger`、`_max_output_tokens`（562）、`_url`（563）、`_last_raw_sse`（564，读 572/779 与 `raw_sse` 属性）、`_headers`（565-569）。

**行为要点**（编号；每条附行号）：

1. 工具定义列表的复制语义：`LLMClient.__init__` 用 `list(tool_definitions or [])` 复制（189），随后把**该内部列表的引用**交给后端（198/200）；三个热更新方法都原地改这个列表（`append` 221、`__setitem__` 切片赋值 223-226、238、246-249）→ AI 下一轮请求即可见新工具，且不改动调用方原列表。
2. 协议分派是"只有 anthropic 是特例"：`if protocol == "anthropic": ... else: _OpenAIBackend`（197-200），未知/拼写错误的协议值会静默走 OpenAI 后端。
3. `LLMClient.chat_stream` 是**普通函数**（无 yield，202-205）：调用即返回后端的生成器对象，因此"真正发请求"的副作用延迟到首次迭代（后端 `chat_stream` 是生成器函数）。`_strip_surrogates(messages)` 在调用时执行，返回新结构，不改动调用方数据。
4. 重试预算来自类变量并在两处被写：`__init__` 里 `set_retry_count(config.retry_count)`（191）与 `agent_loop.run` 每轮 `set_retry_count(...)`（`agent_loop.py:82`）。钳制区间 1–10（181-182），网络与 429 同值同源。
5. `retry_sleep`：`base = RETRY_BACKOFF_BASE[min(attempt, len-1)]`（45）→ attempt≥4 起固定 8s；jitter = ±25%（46）；`sleep_time = max(0, base+jitter)`（47）；以 ≤0.2s 分片轮询 `cancel_check`（49-52），取消返回 False（51）。
6. `_retry_notice` 的等待秒数用 `attempt-1` 索引（61），与 `retry_sleep` 使用的基数一致 → 提示文案"约Ns后自动重试"与真实退避相等；文案格式固定为 `"\n⚠ {reason}，约{base}s后自动重试（第{attempt}/{max_retries}次）…\n"`（63-66）。
7. OpenAI 后端重试矩阵（345-407）：
   - 400/401/403/404/422 → 不重试；其中 400 且命中超限特征 → `yield {"finish_reason": "context_overflow"}` 并 return（350-355）；其余 → `yield {"content": "[错误: API调用失败({type}): {e}]", "finish_reason": "error"}` 并 return（356-359）。
   - 重试前先判取消：`if cancel_check and cancel_check(): return`（360-361）。
   - 429 → `rate_retries` 上限 `_max_rate_retries`，先 yield 重试提示再退避（362-369）。
   - `_is_retryable_http(status)` → `network_retries` 上限 `_max_network_retries`（370-377）。
   - 耗尽 → `[错误: API调用失败({type}，重试{network_retries}次): {e}]`（378-381）。
   - `APIConnectionError/APITimeoutError` → 计入 `network_retries`，提示 reason 用默认"网络连接失败"（383-398）。
   - 其他异常 → 不重试，直接报错（400-407）。
8. OpenAI 请求参数（323-341）：`stream=True`、`stream_options={"include_usage": True}`、`timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=30.0)`；thinking 参数由 `resolve_thinking_params("openai", model, thinking_enabled and not no_thinking, thinking_effort)` 动态构造，body_top 合并进 kwargs、extra_body 放 `extra_body`（332-334）；`no_tools=True` 时不传 `tools`（335-336）；`temperature`/`max_tokens` 仅在配置非 None 时传入（338-341）。
9. OpenAI 中断挂点：发请求前 `_active_response = self._client`（315），拿到流后改为 stream（409），生成器 `finally` 置 None（538）；ESC 关闭的是"client 或 stream"二者之一（`abort_active_llm_request`，130-137）。
10. OpenAI 流解析在后台线程里做（`_iter_to_queue`，422-426），主线程用 `queue.get(timeout=0.05)` 轮询（430）：每 50ms 有一次取消检查（432-433）。
11. 看门狗：仅当"已有异常? 否 且 已收到过首个 chunk（`last_data_ts[0] is not None`）且静默 > 180s"时判定挂死 → 写入 `stream_err=[httpx.ReadTimeout(...)]` 并 `stream.close()`（436-444）。首字节前静默不在此判（注释 435），由 read=300s 兜底。
12. usage 归一化：`{"prompt_tokens", "completion_tokens", "cached_tokens"}`；`cached` 三级回退 `prompt_tokens_details.cached_tokens` → `usage.prompt_cache_hit_tokens` → `model_extra['prompt_cache_hit_tokens']`（448-465）。
13. 内容与思考：`delta.content` 即时 yield `{"content": ...}`（472-474）；`reasoning_content` 从 delta 属性或 `model_extra` 双路取（478-480），只累积不外发。
14. tool_calls 增量聚合：以 `tc.id` 为键；收到新 id 时记录 `index → id` 映射（487-490）；只有 index 无 id 时用映射补 id（491-492）；无任何依据时造 `f"_tc_{n}"` 占位 id（493-495）；name/arguments 用字符串累加（499-502）。
15. 完成事件：`finish_reason` 非空 → `received_finish=True`（504-505）；有 tool_calls 缓冲 → `{"tool_calls":[...], "finish_reason": ..., "thinking": ...}`（514-523）；无 → `{"finish_reason": ..., "thinking": ...}`（524-525）。`thinking` 仅在 `thinking_enabled 且 thinking_passback 且 resolve_thinking_passback("openai", model) == "reasoning_content"` 时为拼接结果，否则 None（508-513）。
16. 流中断上报：迭代器异常且未收到完成标记 → `yield {"stream_interrupted": _classify_stream_error(...)}`（533-536），不伪造 finish_reason；生成器 `finally` 清空 `_active_response`（537-538）。
17. Anthropic 后端请求前做双向转换（577-582）：转换异常 → `[错误: 消息格式转换失败: {e}]` + `finish_reason=error` 并 return。
18. Anthropic 请求体：`{model, messages, max_tokens: self._max_output_tokens, stream: True}`；thinking 的 body_top 与 extra_body **都并入 body 顶层**（596-598）；`system` 仅在非空时写入（599-600）；`tools` 仅在非空且 `not no_tools` 时写入（601-602）；`temperature`/`max_tokens` 的覆盖规则同 OpenAI（604-607）。
19. Anthropic 重试矩阵（613-718）：400/401/403/404/422 → `resp.read()` 后取 text（必须先 read，否则 ResponseNotRead，注释 626）、关闭 resp+client；其中 400+超限特征 → `context_overflow`（632-637）；429 上限 `_max_rate_retries`（644-658，耗尽文案 `[错误: API返回429速率限制(已重试N次)]`）；`_is_retryable_http` 上限 `_max_network_retries`（661-675，耗尽文案 `[错误: API返回{status}错误(已重试N次)]`）；其他非 200 → 报错（678-687）；`httpx.TransportError/TimeoutException` → 计数重试（692-708）；其他异常 → 报错（710-718）。所有失败路径都显式 `_active_response = None`。
20. Anthropic SSE 解析：只处理 `data:` 前缀行（773-775），data 文本追加到 `_last_raw_sse`（779，每次调用开头清空 572），JSON 解析失败静默跳过（781-784）。
21. Anthropic 事件处理：`message_start` 取初始 usage 存 `_start_usage`（788-800，cached 双字段兼容，`prompt_tokens = input_tokens + cached`）；`content_block_start` 建 `tool_use`/`thinking` 块（802-814）；`content_block_delta` 四类：`text_delta` 即时 yield、`thinking_delta` 累积到 `thinking_buffer` 与 `thinking_blocks[idx]`、`input_json_delta` 累积、`signature_delta` 记 `thinking_sigs[idx]`（816-843）。
22. Anthropic `message_delta`：置 `_msg_delta_seen`（846）；`stop_reason` 缺省 `"end_turn"`（847）；映射表 `end_turn→stop`、`tool_use→tool_calls`、`max_tokens|length→max_tokens`、`content_filter→content_filter`、`insufficient_system_resource→server_busy`、其他原样（859-871）；有 tool_use 块 → 按 index 升序 yield 全部 `tool_calls`（873-886）；无 tool_use 且无文本但有 thinking → **thinking-only 兜底**：把 thinking 当正式文本 yield，随后 finish 事件的 `thinking=""`（888-898）；否则正常 yield（899-901）；usage 单独一个 chunk（908-913）。
23. Anthropic `error` 事件 → `[错误: {msg}]` + `finish_reason=error` 并立即 return（915-918）。
24. 流结束后的两条兜底（922-979）：`stream_err` 且未收到 `message_delta` → `stream_interrupted`（922-925）；无错误且未收到 `message_delta` → 依次尝试：补 usage（930-931）→ 有 tool_use 块（且 id/name 非空）则 `finish_reason="tool_calls"`（941-961）→ 否则 thinking-only 当文本 + `stop`（962-971）→ 否则已有文本则 `stop`（972-979）。`finally` 清空 `_active_response` 并关闭 resp 与 client（981-984）。
25. Anthropic 消息转换（988-1090）：system 消息内容抽到 `system_parts`（996-999）；连续纯文本 user 合并为一条（1001-1018）；**含 tool_result 的 user 不合并**（1005、注释 1006-1009）；assistant 的 thinking 回传按 `resolve_thinking_passback("anthropic", model)` 判定，且只在"有 tool_calls 或 `thinking == SYNTHETIC_THINKING`"时回传（1029-1044，注释 1034-1036）；`thinking_block_signed` 无签名则整块省略（1038-1042）；构造块顺序为 [thinking, text, tool_use…]（1046-1066）；无 tool_calls 但有 thinking 时同样带块（1067-1073）；tool 消息合并进前一条 user 的 content 列表（1077-1087）；system 以 `"\n\n"` 连接（1089）。
26. Anthropic 工具转换（1094-1107）：`t.get("function", t)` 兼容裸定义；`input_schema` 缺省 `{"type": "object", "properties": {}}`。

**边界/异常行为**（编号；附行号）：

1. `_strip_surrogates` 只处理 `str/dict/list`，其他类型原样返回（28-35）——元组内的非法字符不会被处理（未被本模块调用路径触达，未验证是否可发生）。
2. `_is_retryable_http`：`status in (408, 409) or status >= 500`（91-92）——任何 ≥500 状态都重试，含 599 等非标准码。
3. `_is_context_overflow`：13 条中英特征子串匹配且 `lower()`（96-120），包含极宽的 `"too long"`、`"超限"`——非超限的 400 也可能被误判为 context_overflow（未验证真实误报率）。
4. `_classify_stream_error`：`httpx.TimeoutException`→timeout（含 ReadTimeout，即看门狗伪造的异常）、`httpx.TransportError`→network；其他异常尝试 `from openai import APIConnectionError, APITimeoutError`，ImportError 时把两者置 `()`（81）使 `isinstance` 恒 False → `"unknown"`；`detail` 截断 200 字符（88）。
5. `abort_active_llm_request`：句柄为 None 时直接返回（133）；`resp.close()` 的所有异常被吞（136-137）。
6. 空 choices 的 chunk 被跳过（467-468）——usage-only chunk（OpenAI 在末尾单独发 usage）能正常处理。
7. 队列哨兵是类级 `object()`（172），两个后端共用同一哨兵对象但队列各自独立（418、732），不会互相误判。
8. `stream_err` 只保留第一个异常：`_iter_to_queue` 里 `if err_box is not None and not err_box`（157）、Anthropic `_read_lines` 里 `if not stream_err`（744）——看门狗已记录的错误不会被覆盖。
9. 看门狗在 `queue.Empty` 分支内每次轮询都会重新判断（436-444/760-768），一旦命中即置 `stream_err` 并关流；后续轮询因 `not stream_err` 为假不再重复记录，读线程随关闭而退出并投递哨兵（445/769）。
10. Anthropic `read=300.0`（616）与 OpenAI `read=300.0`（330）一致，但 connect 超时不同：OpenAI 5.0s（330），Anthropic 10.0s（616）。
11. Anthropic 非 200 且状态码不在 400/401/403/404/422/429/可重试集合内（例：3xx）→ 走 678-687 报错路径。
12. `no_thinking=True` 时按 `resolve_thinking_params(..., thinking_enabled=False, ...)` 查 `disable` 分支；表项无 `disable` 则返回 `({}, {})`（`config/defaults.py:302-304`）→ 不传任何 thinking 参数（而非显式关闭）。
13. tool_call 的 `arguments` 非法 JSON → Anthropic 转换时 `input = {}`（1056-1059）；OpenAI 侧不解析，原样透传字符串（502）。
14. Anthropic 流式 `finally` 无条件 `resp.close()` + `client.close()`（982-984）；所有重试路径在分支内各自 close（629-630、645-646、663、681-682、693、711），无重复关闭保护（未验证 httpx 二次 close 是否静默）。
15. OpenAI 侧 tool_calls 空缓冲但 `delta.tool_calls` 曾到达且 name/arguments 均为空串时，仍会在 finish 时输出一个空名工具调用（496-502 + 514-521）——未做 name 非空过滤（对比 Anthropic 兜底路径有 `tu["id"] and tu["name"]` 过滤，945）。
16. `raw_sse` 对 OpenAI 后端返回 None（251-255，OpenAI 无 `_last_raw_sse` 属性）——协议相关能力差异。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【高】类级可变全局 `LLMClient._active_response`（171；写点 315/346/384/401/409/538/617/631/653/656/670/673/683/694/712/721/982，读点 132）：进程内所有实例共享一个中断句柄，多客户端/多会话并存时互相覆盖（有线程但无锁）。
2. 【高】`set_retry_count` 为 `@classmethod` 写类变量（178-182），且被 `__init__`（191）与 `agent_loop.py:82` 反复写：配置变成全局副作用，实例级隔离不可能；注释自述"均为类成员"（37-40）即承认这是收敛后的折中。
3. 【中】模块级函数依赖尚未定义的类：`retry_sleep`（43-53）与 `_retry_notice`（56-67）在文件 167 行之前引用 `LLMClient.RETRY_BACKOFF_BASE`——顺序脆弱，import 期调用即 NameError。
4. 【中】`_strip_surrogates` 与 `config/session_store.py:12-19` 是逐字相同的 8 行重复实现（两处独立维护的编码兜底）。
5. 【中】两个后端的重试/提示/错误处理是镜像复制：OpenAI 345-407 vs Anthropic 644-718（同样四段式：不可重试 / 429 / 可重试状态 / 传输异常），且文案风格不一致（`API调用失败(重试N次)` vs `API返回N错误(已重试N次)`）。
6. 【中】看门狗代码块逐字重复：436-444 与 760-768（9 行相同，仅 `stream.close()` 与 `resp.close()` 之差）。
7. 【中】过期 API 面（静态可判）：`set_retry_count` 现为类方法（178），而 `tool_exp/retry_notice_probe.py:20`、`tool_exp/verify_retry_resilience.py:17` 仍 `from narnat_agent.core.llm import set_retry_count`（模块级符号不存在 → ImportError），`tool_exp/retry_unify_probe.py:29` 调 `llm_mod.set_retry_count(...)`（AttributeError）。此结论由 grep 静态得出，**未运行验证**。
8. 【中】`raw_sse` 只对 Anthropic 后端有效（251-255），而唯一消费者是空回复取证（`agent_loop.py:476`）——OpenAI 协议下调试信息静默为 `[]`。
9. 【中】core → tools 反向依赖：`set_goal_tool` 函数内导 `..tools.goal_complete.DEFINITION`（213-215），注释自述"避免模块顶层循环依赖"。
10. 【中】运行期依赖第三方 `openai`：`_classify_stream_error` 内 import（78-81）、`_OpenAIBackend.__init__` 内 import（266）、`chat_stream` 内 import（308）——不改用 openai 的部署必须仍安装该包（未验证是否有 requirements 约束）。
11. 【中】`max_output_tokens` 参数不对称：Anthropic 后端接收（198、558、593），OpenAI 后端不接收（200、265）——同名参数的语义只在一种协议下生效。
12. 【低】死状态：`self._protocol`（195）写入后全文件无读取；`LLMClient.self._max_output_tokens`（190）写入后无读取（真正生效的是后端 562）。
13. 【低】未使用 import：`re`（16）、`typing.Optional`（20）。`python -m pyflakes narnat_agent\core\llm.py` 输出：`16:1: 're' imported but unused`、`20:1: 'typing.Optional' imported but unused`。
14. 【低】魔法值集中且分散：退避表 `[1,2,4,8,8]`（173）、静默阈值 180.0（174）、队列轮询 0.05（430、754）、超时元组（330、616）、错误摘要截断 200（88、639、640、686、707 等）、重试钳制 1–10（181-182）。
15. 【低】吞异常点密集：`abort_active_llm_request` 的 `except Exception: pass`（136-137）、看门狗关流 `except Exception: pass`（441-443、764-767）、`_iter_to_queue` 的总捕获（156-158）、`_read_lines` 的总捕获（742-745）。
16. 【低】历史包袱注释引用外部不可核查物：`学官方 harness 规则（serialize.ts）`（281）、`学官方 harness`（1034）——规格化时无法据注释确定对错，只能以行为为准。

**可测性**：

- 可独立单测（纯函数、无 IO）：
  - `_is_retryable_http`、`_is_context_overflow`、`_user_has_tool_result`、`_strip_surrogates`、`_classify_stream_error`（需构造 httpx/openai 异常实例）、`_retry_notice`（只查文案与秒数）。
  - `retry_sleep`：可用 `cancel_check=lambda: True` 走取消分支即时返回；正时延需 monkeypatch `time.sleep`/`time.time`。
  - `_iter_to_queue`：喂一个抛出异常的迭代器 + 队列，断言 `err_box` 与哨兵。
- 可独立单测（构造对象即可，无需网络）：
  - `_AnthropicBackend._convert_messages` / `_convert_tools` / `_OpenAIBackend._prepare_messages`：纯数据变换，覆盖角色交替、tool_result 合并规则、thinking 回传查表、签名缺失省略。
  - `LLMClient.set_goal_tool` / `add_tool_definitions` / `remove_tool_definitions`：断言幂等与"后端可见"（`_backend._tool_defs is llm._tool_defs`）。
  - `raw_sse`：Anthropic 后端直接写 `_last_raw_sse` 后取值；OpenAI 后端断言 None。
- 需要集成测试（需假 HTTP 层：monkeypatch `openai` 客户端或 `httpx.Client`）：
  - 两个 `chat_stream` 的全部分支：重试矩阵、`retry_notice` 序列、看门狗挂死、流中断上报、usage 提取（含 cached 三级回退）、Anthropic SSE 兜底路径（无 message_delta、thinking-only）。
  - `_active_response` 的置位/清空时序（并发观察，含 ESC 关闭连接）。
  - 已有参考：`tool_exp/verify_retry_resilience.py`、`tool_exp/verify_stream_interrupt.py`、`tool_exp/stall_test.py`、`tool_exp/api_contract_test.py`。
- 无法自动化：真实服务端的 400 措辞是否命中 `_CONTEXT_OVERFLOW_HINTS`；真实网络抖动下的退避墙钟时序；`base_url` 各兼容网关（DeepSeek/Claude 兼容层）对 thinking 回传校验的真实行为。

---

### narnat_agent/core/agent_loop.py（488行）

**职责**：工具调度内循环——单轮对话内"发请求 → 收 chunk → 执行工具 → 回填结果 → 再发请求"的循环控制，并负责流中断整轮重试、上下文溢出恢复、收尾软提醒与统计栏落定。

**对外接口**：

- `class AgentLoop`：
  - `def __init__(self, llm: LLMClient, msg_manager: MessageManager, dispatcher: ToolDispatcher, tool_context: ToolContext, stats: StatsTracker, ui: UIInterface, config: Config, logger: AgentLogger, compression=None)`：注入全部协作者；`compression=None` 表示禁用溢出恢复与运行中自查。
  - `@property def _thinking_label(self) -> str`：思考强度中文标签（配置映射，缺失回退原值）。
  - `def _sync_ratio(self)`：把 `stats.input_tokens` 推给压缩协调器刷新窗口占比。
  - `def run(self, stream, goal_mode: bool = False, force_final: bool = False)`：工具调度内循环主入口；`goal_mode` 控制中间轮是否显示统计栏，`force_final` 强制显示统计栏。
  - `def _handle_delete_confirm(self, stream, confirm_tc_id, pending_delete, tool_results)`：Linux/macOS 删除确认——结束本轮流式输出、在 `#` 提示符下等用户 y/N，随后回填结果并返回新 stream（或 None）。
  - `def _dump_empty_debug(self, content_parts, tool_calls_result, finish_reason, call_usage)`：空回复时把请求/响应/原始 SSE 落盘为 JSON 调试文件。
- 公开的隐式状态契约（外部读取，非方法）：`self._last_round_ok`（`agent.py:143/239`）、`self._last_content_parts`（`agent.py:127-128`）。

**依赖**：

- `.llm.LLMClient, retry_sleep`（11）
- `.message_manager.MessageManager`（12）
- `.tool_dispatcher.ToolDispatcher`（13）
- `..tools.tool_context.ToolContext, AWAIT_CONFIRM`（14）
- `..tools.background.running_count as _bg_running_count`（15）、`..tools.background.running_summary as _bg_running_summary`（16）
- `.stats.StatsTracker`（17）
- `..ui.ui_design.UIInterface`（18）
- `..config.loader.Config`（19）
- `..output.write as _stdout_write`（20）
- `..tools.exec_signal.strip_tags`（21）
- `..logger.AgentLogger`（22）
- 函数内延迟导入：`..tools.registry.execute`（452）

**被依赖**：

产品代码：
- `narnat_agent/assembly.py:21` `from .core.agent_loop import AgentLoop`（185-189 构造，注入 `compression=compression_coordinator`）
- `narnat_agent/core/agent.py:121`（`run(stream, goal_mode=...)`）、`158`（`force_final=True`）、`228`、`127-128`（读 `_last_content_parts`）、`143/239`（读 `_last_round_ok`）——**按对象属性访问，非 import**

实验脚本（`tool_exp/`）：
- `tool_exp/retry_notice_probe.py:51`、`tool_exp/retry_unify_probe.py:13`、`tool_exp/verify_renderer_reset_race.py:126`、`tool_exp/verify_retry_resilience.py:18,19`、`tool_exp/verify_stream_fix.py:150,151`、`tool_exp/verify_stream_interrupt.py:8,9`、`tool_exp/verify_ui_showcase.py:153`

**状态**：

- 模块级全局：无（无模块级变量）。
- 类变量：无。
- 实例状态：`_llm`、`_msg_manager`、`_dispatcher`、`_tool_context`、`_stats`、`_ui`、`_config`、`_logger`、`_compression`（33-42）；以及每次 `run()` 设置的 `_last_round_ok`（72/386）、`_last_content_parts`（102）。
- 跨模块共享状态（本类写入）：`tool_context._delete_confirmed`（451，私有字段直写）。

**行为要点**（编号；附行号）：

1. `run()` 起始状态：`_last_round_ok = False`（72）、`stream_interrupted_retries = 0`（73）、`overflow_compacted = False`（74）。
2. 流中断重试预算：`max(1, min(int(config.ai.retry_count), 10))`，异常（TypeError/ValueError/AttributeError）兜底 3（77-80）；随后 `self._llm.set_retry_count(stream_retry_max)` 把同一值同步给连接层（82）。
3. 循环顶部"运行中自查"（87-95）：条件为 `self._compression is not None and self._compression.need_compress()`；先 `feed("\n⚠ 上下文占比超阈值，正在自动压缩历史…\n")` + `flush_renderer()`（88-89，注释 85-86 说明"提示先于压缩"是为了让用户知情），再 `mid_run_guard()`，成功 feed "  压缩完成，继续任务…\n"、失败 feed "  自动压缩失败，继续尝试本次请求…\n"（90-94），最后 `stream.begin()` 重启"思考中"spinner（95）。
4. 每轮固定先 `msg_manager.repair()`（98）再请求（110）；请求入参是 `self._msg_manager.view.to_list()` 与 `cancel_check=lambda: stream.cancelled`。
5. chunk 分派优先级（按代码顺序）：中断（112-118）→ `tool_calls`（121-122）→ `thinking`/`thinking_signature`（125-128）→ 纯文本（131-133）→ `usage`（136-137）→ `stream_interrupted`（140-142，continue）→ `retry_notice`（145-147，continue）→ `finish_reason`（150-164）。
6. 纯文本只在不含 `tool_calls` 键时累计：`if "content" in chunk and "tool_calls" not in chunk`（131）——即"同 chunk 带 tool_calls 的 content 不落屏、不入 buffer"。
7. 中断处理（112-118）：若已累计文本，先 `append_assistant("".join(content_parts))` 落历史（注释 114：纯文本轮不回传思考）→ `stream.abort()` → `ui.on_interrupted()` → return。
8. `finish_reason == "context_overflow"`：`break` 出 chunk 循环走恢复分支（152-154）；`finish_reason == "error"`：直接 `stream.finish(...)` 后 return（155-164）。
9. 溢出恢复（170-207）：`overflow_compacted or compression is None` → 提示"请尝试缩减本次输入，或 /save 保存会话后开启新对话" + finish + return（171-184）；否则置 `overflow_compacted=True`、日志、`stream.reset_renderer()`、feed 提示、`compress_no_input()`；成功 → feed "  压缩完成，重试请求…\n" + `continue`（重发）；失败 → 提示 + finish + return（195-207）。注释 166-169 说明 assistant 尚未入历史故重发幂等、每次 run 只允许一次恢复。
10. 收到任何正常完成标记即重置流中断预算：`if parsed_finish_reason is not None: stream_interrupted_retries = 0`（216-217）。
11. 有 tool_calls 的分支（220-265）：`append_assistant("".join(content_parts) or None, tool_calls=..., thinking=..., thinking_signature=...)`（221-226）→ `dispatcher.execute_tool_calls(tool_calls_result, stream)`（228）→ 中止检查：`append_interrupted_tools(tool_calls_result, completed_ids)` + abort + `ui.on_interrupted()` + return（231-236）→ 删除确认分支（239-254）→ 回填所有 `(tc_id, result)`（257-258）→ `stats.update(call_usage)` + `_sync_ratio()`（261-263）→ continue。
12. 删除确认触发条件：`tool_context.pending_delete is not None`（239）；取出后立即清空字段（240-241）；在工具结果里找第一个 `result == AWAIT_CONFIRM` 的 tc_id（243-247）；找不到则不进入确认流程，照常回填（248 的 `if confirm_tc_id is not None`）。`_handle_delete_confirm` 返回新 stream 时 `stream = new_stream; continue`（251-253），返回 None 则 `return`（254）。
13. 流中断整轮重试（267-309）：条件 `parsed_finish_reason is None`（271）；预算内 → 计数 +1、`kind = (stream_interrupted_info or {}).get("kind", "unknown")`（274）、基数查表（275-277）、日志（278-283）、`stream.reset_renderer()`（286，注释 284-285：避免与重播内容拼接重复）、feed 提示（287-290）、`retry_sleep(..., lambda: stream.cancelled)`；取消 → abort + on_interrupted + return（292-295）；预算耗尽 → 提示"已自动重试N次仍失败" + finish + return（297-309，注释 297：截断内容不写历史）。
14. 纯文本完成（311-338）：有内容 → `append_assistant("".join(content_parts))`（315，注释 313-314：纯文本轮不回传思考）；无内容 → `_dump_empty_debug(...)`（318）+ 6 键提示表（319-326）按 `parsed_finish_reason or "stream_interrupted"` 取文案（327-328），未命中用 `⚠ AI 返回异常（{reason}），请稍后重试。`（328）；两种情况之后都 `stream.finish(...)` + return（330-338）。
15. 收尾软提醒——计划未勾选（345-367）：未完成项 = `tool_context.current_todos` 中 `status != "completed"`（349-352）；条件为"有未完成项 且 `todo_reminded` 为假"（353）；置 `todo_reminded = True`（354）；拼接前 5 项名称（355）+ `等共N项`（356）；`append_user("[系统提醒] …")`（357-362，注释 348：仅注入 AI 上下文，终端不展示）；随后 `flush_renderer()` + `begin()`（365-366，注释 363-364）→ continue。
16. 收尾软提醒——后台任务（369-384）：`_bg_running_summary()` 非空且 `bg_reminded` 为假（373）→ 置真（374）→ `append_user("[系统提醒] 后台仍有任务在运行: …")`（375-379）→ flush + begin（382-383）→ continue；注释 369-371 说明"不强杀，硬兜底在 GoalComplete/会话结束"。
17. 正常收尾（386-409）：`_last_round_ok = True`（386）；`show_stats = (not goal_mode) or tool_context.goal_complete or force_final`（390）；`bg_running = _bg_running_count()` 非 0 → `show_stats = False` 并 `_stdout_write("  ⚠ 后台 N 个任务仍在运行…")`（393-399）；`stream.finish(input, output, cache_ratio, cost, balance, thinking_effort, with_stats=show_stats)`（400-408）→ `break`（409）。
18. `_handle_delete_confirm`（411-464）：先回填所有非确认工具结果（427-429）→ `stream.finish(..., with_stats=False)` 结束本轮流式输出（432-440，注释 431）→ `ui.read_input_with_prompt("  确认执行此命令? [y/N]: ")`（443），None 视为空串（444-445）→ `confirmed = user_input.strip().lower() in ("y", "yes")`（447）→ 确认路径：置 `tool_context._delete_confirmed = True`（451）、函数内 import `..tools.registry.execute`（452）、重新执行工具（453）、`strip_tags` 去框架标签再回填（455）、`color_diff` 存在则逐行缩进打印（457-458）；取消路径：回填 `"[操作已取消: 此命令需用户确认]"`（461）→ 返回 `self._ui.create_stream()`（464）。
19. `_dump_empty_debug`（466-488）：文件名 `{config.paths.data_dir}/debug_empty_{YYYYmmdd_HHMMSS}.json`（468-471）；内容含 `time`、`request.messages`、`response.raw_sse_lines`（来自 `self._llm.raw_sse or []`）、`parsed_content`、`parsed_tool_calls`、`parsed_finish_reason`、`call_usage`（472-482）；写入成功打印"⚠ 调试日志已写入: …"（486）；`OSError` 静默（483-488）。
20. `_sync_ratio`（51-58）：仅当 `_compression is not None` 时调用 `refresh_ratio(stats.input_tokens)`；注释 53-56 说明这是"占比唯一更新点原在用户轮末"的补丁。
21. `_thinking_label`（44-49）：`config.ai.thinking_options.get(effort, effort)`——映射缺失时直接回退原始 effort 字符串。

**边界/异常行为**（编号；附行号）：

1. `retry_count` 为非数字/缺失 → 3（79-80）；配置 0 或负 → 钳到 1；>10 → 10（78）。
2. `parsed_finish_reason` 为 None 且预算耗尽后才走"中断重试耗尽"分支；该分支的提示文案硬编码"已自动重试{stream_retry_max}次仍失败"（298-300）。
3. 未注入 compression（None）时：运行中自查（87）与溢出恢复（171）整体禁用，400 超限直接报错收尾。
4. `_last_content_parts` 每轮被重新赋值为当轮 list（102）——`agent.py` 异常路径读到的永远是"最近一轮"的内容。
5. 空回复调试文件名只精确到秒（470），同一秒内两次空回复会互相覆盖（未验证实际发生概率）。
6. `stream.finish(...)` 在 8 处调用（156-163、176-183、199-206、301-308、330-337、400-408、432-440），其中只有正常收尾（400-408）与删除确认（432-440）显式传 `with_stats`，其余走默认 True。
7. `_handle_delete_confirm` 在 `confirm_tc_id is None` 时不会被调用（248），因此"pending_delete 非空但无 AWAIT_CONFIRM 结果"的情况按普通工具轮回填（257-258）——不会阻塞。
8. `tool_context._delete_confirmed` 是私有字段直写（451），与 `tools/tool_context.py` 的公开契约不一致（清单见总表）。
9. `usage` 只影响统计与占比刷新（261-263、341-343），不参与任何控制流；无 usage 时占比不刷新。
10. `finish_reason="error"` 分支不写任何 assistant 消息（155-164）——错误轮在历史中不留痕。
11. `_dump_empty_debug` 只在 `content_parts` 为空时调用（316-318），即"有 finish_reason 但零文本"；此时若 `raw_sse` 为 None（OpenAI 协议）则记为 `[]`（476）。
12. 所有"提前 return"路径都不会设置 `_last_round_ok = True`（386 是唯一置 True 点），因此目标模式续跑判定把它们统一当作"非正常结束"。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【高】跨模块私有字段直写：`self._tool_context._delete_confirmed = True`（451）——绕过 ToolContext 公开接口。
2. 【高】反向依赖被外部读到内部状态：`agent.py:127-128` 读 `self._agent_loop._last_content_parts`（本文件 102 赋值）、`agent.py:143/239` 读 `self._last_round_ok`（72/386）——循环状态被当作对外契约。
3. 【中】统计栏参数 8 处重复：同一组 `(input_tokens, output_tokens, cache_ratio, cost, balance, thinking_effort)` 在 156-163、176-183、199-206、301-308、330-337、400-408、432-440 反复展开（含缩进差异）。
4. 【中】两个软提醒分支逐字重复的收尾样板：`stream.flush_renderer(); stream.begin(); continue`（365-366 与 382-383）。
5. 【中】函数内延迟 import 规避循环依赖：`from ..tools.registry import execute`（452）——core 运行时依赖 tools 注册表。
6. 【中】空回复提示文案以 6 键 dict 内联（319-326），与其他错误文案（88、93、172-175、195-198、287-290、298-300）各自散落，没有单点。
7. 【中】`_dump_empty_debug` 把磁盘 IO + 终端输出混入对话循环（466-488），仅 `except OSError` 兜底（487-488）。
8. 【中】提醒状态位（`todo_reminded`/`bg_reminded`）由本类写、`agent.py:86/88` 复位——提醒逻辑与其状态分居两层。
9. 【低】魔法值/硬编码：`unfinished[:5]`（355）、文件名时间格式（470）、截断阈值 0.05s 等来自 llm 层。
10. 【低】`_handle_delete_confirm` 用 `None | stream` 二值表达"是否继续"（420-422 文档块、464），调用方需按类型判空（251-254）。

**可测性**：

- 可独立单测（全部依赖可替换为 fake，无需网络/终端）：
  - `run()` 分支矩阵：中断、`context_overflow` 恢复成功/失败、流中断重试与耗尽、空回复 6 种 reason、纯文本收尾、tool_calls 循环、两种软提醒、`show_stats` 组合（goal_mode × goal_complete × force_final × bg_running）。可参考现有可行做法：`tool_exp/verify_stream_interrupt.py`（fake llm/mm）、`tool_exp/verify_retry_resilience.py`（fake 网络 + 真实 AgentLoop）、`tool_exp/retry_notice_probe.py`、`tool_exp/verify_renderer_reset_race.py`。
  - `_dump_empty_debug`：临时目录 + fake 数据，断言 JSON 内容。
  - `_thinking_label`：构造 `thinking_options` 覆盖命中/未命中。
- 需要集成测试：溢出恢复（真实 `CompressionCoordinator` + `MessageManager` + 假 LLM）、删除确认（真实 `tools.registry.execute` + ToolContext + UI 管道，验证 y/N 两路与结果回填）。
- 无法自动化：`#` 提示符下的真实键入确认（`read_input_with_prompt`，依赖 prompt_toolkit 会话）、ESC 打断与 spinner 视觉时序、`_stdout_write` 终端渲染效果。

---

### narnat_agent/core/agent.py（276行）

**职责**：主循环编排者——读用户输入 → 命令分发 → 阈值压缩检查 → 调 AgentLoop 跑轮次 → 目标模式自动续跑 → 退出清理；另提供 headless 一次性任务执行入口。

**对外接口**：

- `class Agent`：
  - `def __init__(self, project_root: Optional[str] = None, debug: bool = False, headless: bool = False)`：构造即调用 `Assembly.build(...)`，此后仅经由 `AssemblyResult` 各字段取协作者。
  - `def run(self)`：交互主循环（读输入/命令分发/压缩/轮次/续跑/清理）。
  - `def run_headless(self, task: str, max_rounds: int = 0)`：headless 一次性任务（`nn -p` 入口），目标模式强制开启 + 自动续跑 + `[NN_DONE]` 完成信号。
- 实例状态被外部读取的情况：无（只有 `main.py` / `narnat_agent/__init__.py` 使用 `Agent`）。

**依赖**：

- `..assembly.Assembly, AssemblyResult`（16）
- `..output.write as _stdout_write, X, R`（17）
- 函数内延迟导入：`..tools.background.prepare as _bg_prepare`（41、198）、`..tools.terminal.cleanup`（178、269）、`..tools.serial.cleanup`（179、270）、`..tools.background.cleanup_all`（180、271）
- 标准库：`os`（13，用于 `os._exit`）、`typing.Optional`（14）

**被依赖**：

产品代码：
- `main.py:33` `from narnat_agent.core.agent import Agent`
- `narnat_agent/__init__.py:5` `from .core.agent import Agent`

（无其他产品模块 import 本文件；`tool_exp/` 无 import 本文件的脚本。）

**状态**：

- 模块级全局：无。
- 类变量：无。
- 实例状态：`_parts`（25，AssemblyResult，含全部子模块引用）、`_config`、`_logger`、`_ui`、`_context`、`_mgr`（SessionManager）、`_msg_manager`、`_stats`、`_agent_loop`、`_auto_save`、`_compression`（26-35）、`_round`（36）。
- 跨模块共享状态（本类写入）：`tool_context.goal_complete`（84、147、244）、`tool_context.todo_reminded`（86）、`tool_context.bg_reminded`（88）、`tool_context.current_todos`（91）、`session_mgr._goal_enabled`（105 读 / 207 写）、`session_mgr._goal_max_rounds`（107 读 / 208 写）。

**行为要点**（编号；附行号）：

1. `run()` 启动序列：`..tools.background.prepare()` 预清上次异常退出的后台残留（41-42）→ `ui.start()`（43）→ 记日志 `Agent启动, model=...`（44）。
2. 主循环读输入：`ui.read_input()`；返回 None → `continue`（49-51）；随后 `auto_save.wait()` 作为后台保存同步点（54）；`stripped = user_input.strip()` 为空 → `continue`（56-58）。
3. 命令分发（61-74）：`stripped.startswith("/")` → `split(None, 1)` 得 `cmd`/`args`（默认 ""）→ `ui.dispatch_command(cmd, args)`；返回值 `2`（`CommandResult.EXIT`，`ui/session_commands.py:29`）→ `auto_save.on_exit()` → 日志 → `mcp_manager.cleanup()` → `logger.close()` → `os._exit(0)`（66-72）；返回值 `1`（HANDLED）→ `continue`（73-74）；`0`（UNKNOWN）继续按普通输入处理（隐含行为：命令不识别时会当作对话内容发送，未验证是否有额外保护）。
4. 每轮计数与余额查询：`_round += 1`（77）；`api_key = getattr(self._config.ai, 'api_key', None)`（78）；`stats.fetch_balance(api_key, self._round)`（79）。
5. 每次用户新输入都复位 4 项任务态：`goal_complete = False`（84）、`todo_reminded = False`（86）、`bg_reminded = False`（88）、`current_todos = []`（91）；注释 82-90 给出各复位理由。
6. 阈值压缩：`if self._context.need_compress(): compress_ok = self._compression.compress(stripped)`；失败 → `continue`（92-96）——不追加消息、不进入对话轮次（失败路径的消息追加由协调器的 `on_llm_error`/`on_interrupt` 回调完成）。
7. 追加用户消息仅在未压缩时执行：`if not compress_ok: repair(); append_user(stripped); log(用户输入: stripped[:100])`（99-102）——压缩成功时用户输入由 `handle_compress(pending_input=stripped)` 写入历史。
8. 目标模式参数：`goal_enabled = self._mgr._goal_enabled`（105）；`goal_limit = self._mgr._goal_max_rounds or self._config.ai.goal_max_rounds`（107）；否则 `goal_limit = 0 / goal_task = "" / goal_round = 0`（110-113）。
9. 内层轮次循环（115-168）：`stream = ui.create_stream()`（117）→ `agent_loop.run(stream, goal_mode=goal_enabled)`（121）。
10. 异常处理（122-134）：`KeyboardInterrupt` → `ui.on_interrupted()` + `stream.abort()`（122-124）；其他异常 → 记日志 + 若 `agent_loop._last_content_parts` 非空则 `append_assistant`（127-128）+ `stream.abort(message=f"  {X}⚠ 程序异常，本轮回复已停止: {e}{R}")`（130）；无异常 → `mgr.on_auto_save()` + `auto_save.try_save()`（131-134）。
11. 续跑判定顺序（139-168，仅目标模式）：`not goal_enabled` → break（139-140）；`stream.aborted` → break（141-142）；`not self._agent_loop._last_round_ok` → break（143-144）；`tool_context.goal_complete` → 复位为 False 并 break（145-148）；`goal_round >= goal_limit` → 注入收尾指令（152-155）+ 新建 stream（156）+ `run(..., force_final=True)`（158）+ 保存（159-161）+ break；否则注入【自动续跑】消息（164-168）继续内层 while。
12. 轮末（170-174）：`context.update_ratio(stats.input_tokens)`（171）、`warn = context.check_warn()`（172）、非空则 `_stdout_write(f"  ⚠ {warn}\n")`（173-174）。注释 170 说明中断/异常时占比沿用上一轮值。
13. `run()` 的 `finally`（176-184）：`dispatcher._executor.shutdown(wait=False)`（177）→ 导入并执行 `terminal.cleanup()`、`serial.cleanup()`、`background.cleanup_all()`（178-183）→ `mcp_manager.cleanup()`（184）。
14. `run_headless(task, max_rounds)` 启动序列（195-217）：记日志（195）→ `_bg_prepare()`（198-199）→ 哨兵变量 `goal_round = 0`、`end_reason = "unknown"` 在 try 之外初始化（201-203，注释：finally 无条件引用）→ 强制目标模式：`mgr._goal_enabled = True`（207）、`mgr._goal_max_rounds = max_rounds`（208）、`if getattr(self._mgr, '_set_goal_tool', None): self._mgr._set_goal_tool(True)`（209-210）→ `goal_limit = max_rounds or config.ai.goal_max_rounds`（211）→ `goal_task = task.strip()`（212）→ `repair()` + `append_user(goal_task)`（215-217）。
15. headless 循环（219-263）：每轮先做阈值压缩（与 run 对齐），失败 → `end_reason = "compress_failed"` + break（221-224）；`stream = ui.create_stream()` + `run(stream, goal_mode=True)`，异常 → 日志 + `stream.abort(message=f"⚠ 程序异常，本轮回复已停止: {e}")` + `end_reason="aborted"` + break（226-233）；随后依次判：`stream.aborted`→"aborted"（236-238）、`not _last_round_ok`→"round_failed"（239-241）、`goal_complete`→复位并置"goal_complete"（242-246）、`goal_round >= goal_limit`→注入收尾指令 + 新建 stream + `run(..., force_final=True)` + `end_reason="round_limit"`（247-257）；否则注入【自动续跑】继续（259-263）。
16. headless `finally`（264-276）：先 `_stdout_write(f"\n[NN_DONE] reason={end_reason} rounds={goal_round}\n")`（267，注释 265-266：父代理以此行判定子代理结束及原因）→ 同样的 shutdown + 三 cleanup（268-274）→ `logger.close()`（276）。
17. headless 与 run 的差异（docstring 189-190 + 代码）：不读用户输入、不自动保存会话、不查余额、不显统计栏、出口打印 `[NN_DONE]`。

**边界/异常行为**（编号；附行号）：

1. `read_input()` 返回 None（Ctrl-C/Ctrl-D 或空回）不退出主循环（50-51）；唯一退出路径是命令返回码 2（66-72）。
2. `os._exit(0)` 绕过 `finally`（注释 69-70），因此退出前显式做 MCP 清理与日志关闭（70-71）——两条清理路径互斥且都不可省略。
3. `goal_limit = self._mgr._goal_max_rounds or config.ai.goal_max_rounds`（107）：`_goal_max_rounds = 0` 时用配置默认（默认 100，`config/defaults.py:28`）；若配置值也为 0 → 首轮就满足 `goal_round >= goal_limit`（149）并注入收尾指令（未验证是否可能出现 0 配置）。
4. 异常分支用 `hasattr(self._agent_loop, '_last_content_parts')` 防御（127）——AgentLoop 在 102 行赋值前抛错时不会二次异常。
5. `KeyboardInterrupt` 只在 `run()` 捕获（122-124），`run_headless` 只捕 `Exception`（229）→ 中断异常会冒泡但 `finally` 仍打印 `[NN_DONE] reason=unknown`（202-203、267）。
6. headless 的轮数上限分支（247-257）不检查 `stream.aborted`，也不在 try 内保存会话——与 run 的对应分支（158-162）行为不一致。
7. `finally` 内直接取 `self._parts.dispatcher._executor`（177、268）——若装配失败（`self._parts` 未建成）不进入 finally，故不会 NameError；但清理函数任一抛错会顶替主异常（未验证是否发生过）。
8. 阈值压缩失败时用户输入已由回调写入历史，但本轮不产生 AI 回复，控制权回到输入提示（92-96）。
9. `run()` 注释中步骤编号重复（"3. 压缩检查"在 81 行、"4. 追加用户消息"在 98 行，而内层循环里注释又写 "5. 创建流式输出" 116 与 "5. 目标模式…" 104）——仅注释层面。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【高】跨层私有字段访问成网：`self._mgr._goal_enabled`（105、207）、`self._mgr._goal_max_rounds`（107、208）、`self._mgr._set_goal_tool`（209-210）、`self._parts.dispatcher._executor`（177、268）、`self._parts.tool_context.*`（84-91、145-147）；其中 `_goal_enabled/_goal_max_rounds` 同样被 `ui/session_commands.py:242-256` 直接读写。
2. 【高】续跑算法双份实现：`run()` 139-168 与 `run_headless()` 236-263 逻辑同构（判定顺序一致、收尾注入一致），差异仅保存/日志/end_reason。
3. 【高】反射式读取循环内部状态：`hasattr(self._agent_loop, '_last_content_parts')` + `self._agent_loop._last_content_parts`（127-128）、`self._agent_loop._last_round_ok`（143、239）。
4. 【中】目标模式文案硬编码两处：收尾指令（152-155 = 250-253）、续跑提示（165-168 = 260-263）逐字重复。
5. 【中】`os._exit(0)` 硬退出（72）与 finally 清理路径形成两套退出流程（66-72 vs 176-184），清理责任被拆散。
6. 【中】`finally` 内延迟 import 三个 cleanup（178-180、269-275）——每轮启动/退出都走一遍 import 机制（缓存命中，非性能问题，但把依赖关系藏在函数体里）。
7. 【中】`_goal_max_rounds or config...` 与 headless 的 `max_rounds or config...`（107、211）表达同一语义却各写一遍。
8. 【低】`X`/`R` 颜色常量被拼进异常提示字符串（130）——UI 细节渗入编排层。
9. 【低】注释步骤编号重复（104、116 两处"5."），docstring 与实现描述存在漂移风险。
10. 【低】`run_headless` 收尾分支不复用 run 的保存逻辑（254-257）→ 该路径下压缩/续跑后的会话不会落盘（行为差异，未验证是否有意为之）。

**可测性**：

- 可独立单测（前提是能替换装配）：当前 `__init__` 里 `Assembly.build(...)` 是硬编码类方法调用（25），无注入点——单测需要在测试中 monkeypatch `narnat_agent.core.agent.Assembly.build`（可行，`tool_exp/` 无先例）。
  - 可验证的分支：命令返回码 0/1/2 三条路径（2 需 patch `os._exit`）、目标模式 6 个 break 条件、headless 的 end_reason 取值集合（goal_complete/round_limit/round_failed/aborted/compress_failed/unknown）、`[NN_DONE]` 输出格式。
- 需要集成测试：真装配（`Assembly.build`，`tool_exp/verify_assembly.py` 已有先例）+ 假 LLM/假 UI 跑完整 turn；压缩触发与 `context.update_ratio` 的组合。
- 无法自动化：真实终端输入与 `/` 命令交互（prompt_toolkit）、`os._exit` 之后的进程状态、MCP 子进程回收。

---

### narnat_agent/core/context.py（76行）

**职责**：上下文窗口占比状态机——占比更新、压缩触发判断、一次性告警、压缩后/失败后的状态重置。

**对外接口**：

- `class ContextManager`：
  - `def __init__(self, logger=None, context_window: int = DEFAULT_CONTEXT_WINDOW, warn_ratio: int = DEFAULT_WARN_RATIO, compress_ratio: int = DEFAULT_COMPRESS_RATIO)`：注入日志器与三个阈值。
  - `@property def ratio(self) -> Optional[float]`：最近一次窗口占比（%），无数据/窗口无效为 None。
  - `def update_ratio(self, input_tokens: int) -> None`：按服务端 prompt_tokens 快照更新占比。
  - `def need_compress(self) -> bool`：占比 ≥ 压缩阈值。
  - `def check_warn(self) -> str`：本会话仅一次的超阈值告警文案（无告警返回空串）。
  - `def reset(self)`：占位置 None + 清除告警标记（压缩成功后调用）。
  - `def set_retry_soon(self)`：把占比设为"压缩阈值 − 10"，表达"稍后重试"（压缩失败后调用）。

**依赖**：

- `..config.defaults.DEFAULT_CONTEXT_WINDOW, DEFAULT_WARN_RATIO, DEFAULT_COMPRESS_RATIO`（7-9）
- 标准库：`typing.Optional`（5）

**被依赖**：

- `narnat_agent/assembly.py:13` `from .core.context import ContextManager`（77-82 构造，窗口取 `config.ai.context_window`、阈值取 `config.session.warn_ratio/compress_ratio`）
- `narnat_agent/core/compression_coordinator.py:14` `from .context import ContextManager`（37 类型标注；51/57/72/83/88/102/143 调 `reset`/`set_retry_soon`；158/166/177 调 `need_compress`/`update_ratio`）
- `narnat_agent/core/agent.py`（93 `need_compress`、171 `update_ratio`、172 `check_warn`）——按对象属性访问，非 import

（`tool_exp/` 无直接 import 本文件的脚本。）

**状态**：

- 模块级全局：无可变全局（仅 import 三个常量）。
- 类变量：无。
- 实例状态：`_logger`（30）、`_context_window`（31）、`_warn_ratio`（32）、`_compress_ratio`（33）、`_ratio: Optional[float]`（34，写 49/51/71/76；读 40/55/59/61）、`_warned`（35，写 62/72；读 59）。
- 无锁保护：`_ratio`/`_warned` 可被 agent_loop 轮内刷新（经协调器）与用户轮末（agent.py:171）交叉写。

**行为要点**（编号；附行号）：

1. 占比公式：`input_tokens / context_window * 100`，仅在 `context_window > 0 and input_tokens > 0` 时计算，否则置 None（48-51）。
2. `ratio` 是纯读取属性（37-40）。
3. `need_compress()`：`self._ratio is not None and self._ratio >= self._compress_ratio`（53-55）——None 一律视为"不需要压缩"。
4. `check_warn()`：`_ratio is None or _warned` → `""`（59-60）；`_ratio >= warn_ratio` → 置 `_warned = True`、写 warning 日志（分类 "core.context"）、返回 `f"窗口占比已达{self._warn_ratio}%，建议开启新对话"`（61-66）；否则空串（67）——单会话仅一次（35、62）。
5. `reset()`：`_ratio = None`、`_warned = False`（69-72）。
6. `set_retry_soon()`：`_ratio = max(0.0, self._compress_ratio - 10)`（74-76）——使 `need_compress()` 立即为假（阈值以下），但当再次增长 10% 内即可再触发压缩；由"压缩失败"路径调用（`compression_coordinator.py:57/88`）。

**边界/异常行为**（编号；附行号）：

1. `context_window <= 0` → 占比恒 None（48-51）→ 不告警（59）不压缩（55），与类 docstring 声明一致（18 行）。
2. `input_tokens == 0`（首轮/恢复会话未收到 usage）→ 占比置 None（48-51）——注意这是"清零"而非"保持上次值"。
3. 负数 `input_tokens` → 同一判断为假 → None（48）。
4. `set_retry_soon` 在 `compress_ratio < 10` 时得到 0.0（74 的 `max`）。
5. `warn_ratio > compress_ratio` 的配置组合会导致"先压缩、后告警"，两者语义倒置（未验证配置加载处是否校验；`config/loader.py:861-862` 只是取整兜底）。
6. `reset()` 的 docstring 写"占比归 0"但实现是置 None（70-71）——文档与实现不一致。
7. 阈值比较是闭区间（`>=`，55/61）——恰好等于阈值即触发压缩/告警。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【中】用数值伪造状态：`set_retry_soon` 通过"把占比设成阈值−10"表达流程意图（74-76），调用方读不出"上次压缩失败"这一事实（无独立标志位）。
2. 【中】docstring 与实现不符：`reset()` 声称"占比归 0"，实现为 `None`（70-71）；且 `update_ratio` 在无数据时也置 None，语义被复用为"无数据/已重置/无效"三种含义（34 注释、48-51）。
3. 【低】`reset`（69）与 `set_retry_soon`（74）缺返回类型注解，与同文件其它方法（`-> None`/`-> bool`/`-> str`）风格不一致。
4. 【低】魔法数 `10`（"留 10% 余量"，75）与阈值常量未同处定义，仅靠注释 75 说明。
5. 【低】无并发保护：占比在"用户轮末"与"工具轮 usage 到达"两个时机被写（agent.py:171、agent_loop.py:58 经协调器），读写无锁（未验证是否真实发生交叉）。

**可测性**：

- 可完全独立单测（纯计算 + 两枚状态位，无 IO、无时钟）：
  - `update_ratio`（正数/0/负数/窗口 0/窗口负 → 占比与 None 的映射）；
  - `need_compress`（None/阈值下/阈值上/恰好等于）；
  - `check_warn`（首次返回文案 + 二次返回空 + None 返回空 + 日志调用次数）；
  - `reset` / `set_retry_soon`（含 `compress_ratio < 10` 边界）；
  - `ratio` 属性。
- 需要集成测试：无（本文件无外部副作用）。
- 无法自动化：无。

---

### narnat_agent/core/compressor.py（107行）

**职责**：压缩的三个纯数据步骤——构建压缩请求消息、按 token 预算选择"逐字保留"的尾部切点、用摘要重建新会话消息列表（全程内存操作，不落盘）。

**对外接口**：

- `def estimate_tokens(msg: Dict[str, Any]) -> int`：估算单条消息 token 数（转发 `tools/token_estimate.py`）。
- `def _cut_balanced(messages: List[Dict[str, Any]], cut: int) -> bool`：判断切点 `cut` 处是否安全（保留尾部首条必须是 user）。
- `def select_cut_index(messages: List[Dict[str, Any]], retain_tokens: int) -> Optional[int]`：返回保留尾部起点下标；None = 不保留（全量压缩）。
- `class Compressor`（无 `__init__`，无实例状态）：
  - `def build_compress_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]`：全部历史 + 末尾追加压缩指令。
  - `def build_new_session_messages(self, system_prompt: str, summary: str, tail_messages: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]`：system + 摘要 system + 尾部消息。

**依赖**：

- `..config.defaults.COMPRESS_PROMPT`（7）
- `..tools.token_estimate.estimate_message_tokens`（8）
- 标准库：`typing.{List, Dict, Any, Optional}`（5）

**被依赖**：

产品代码：
- `narnat_agent/assembly.py:14` `from .core.compressor import Compressor`（85 构造 `Compressor()`，注入 MessageManager 89）
- `narnat_agent/core/message_manager.py:9` `from .compressor import Compressor, select_cut_index, estimate_tokens`（33 构造参数；130 调 `select_cut_index`；139/197 调 `estimate_tokens`；147 调 `build_compress_messages`；198/218 调 `build_new_session_messages`）

实验脚本（`tool_exp/`）：
- `tool_exp/repair_seq_test.py:18`（`MessageManager(ml, Compressor(), logger=None)` 等，56/76/118）

**状态**：

- 模块级全局：无可变全局。
- 类变量：无。
- 实例状态：`Compressor` 无任何实例属性（未定义 `__init__`）；`estimate_tokens` 等为模块级纯函数。

**行为要点**（编号；附行号）：

1. `estimate_tokens` 是薄封装（16-18），真正的估价实现在 `tools/token_estimate.py:38-57`（混合密度：CJK 0.7/字、其余 0.25/字符，加 8 的 JSON 框架开销；`content` 覆盖 str/list，`thinking` 与 `tool_calls` 也计入）。
2. 模块顶部注释声明精度立场：估价仅用于切点选择，精确压力判断以服务端 `prompt_tokens` 为准（10-13）。
3. `_cut_balanced`：`cut >= len(messages)` → True（28-29）；否则要求 `messages[cut]["role"] == "user"`（30）——注释 24-27 说明该约束同时满足 Anthropic 首条/交替要求并天然避免拆散 `assistant(tool_calls)/tool` 对。
4. `select_cut_index` 返回 None 的三种情形：`retain_tokens <= 0`（45）、`messages` 为空（45）、全部为 system 消息（51-52）、以及回退后首条仍非 user（63-64）。返回 `start`（对话区首条下标，可能为 0）表示"预算不足，但全部非 system 消息都可保留"（65）。
5. 切点算法（48-62）：先跳过开头连续 system 得到 `start`（48-52）；从尾部向前累加估价，`keep_from` 逐条前移直到累计 ≥ `retain_tokens` 或走完（54-60）；再向前回退直到切点是 user 起头或到达 `start`（61-62）。
6. system 消息不进保留区（双保险）：`select_cut_index` 从 `start` 开始搜（48-52），`build_new_session_messages` 再过滤一次 `role != "system"`（104-106）；注释 41-43 说明重建时由新的 system_prompt + 摘要承担。
7. `build_compress_messages`：`list(messages)` 浅拷贝后追加 `{"role": "user", "content": COMPRESS_PROMPT}`（84-89）——不改动调用方 list 结构（元素仍是同一 dict 引用）。
8. `build_new_session_messages` 结构（101-107）：`[{"role":"system","content":system_prompt}]` + （`summary` 非空时）`{"role":"system","content": f"# 上一轮对话成果\n\n{summary}"}` + 逐个追加非 system 的 tail。
9. `COMPRESS_PROMPT`（`config/defaults.py:52-85`）固定 8 个小节结构与"不照抄旧摘要"规则——压缩摘要的格式契约在此定义（不在本文件）。

**边界/异常行为**（编号；附行号）：

1. `retain_tokens` 为负/0 → None（全量压缩，45）。
2. 空列表 → None（45）。
3. 全 system 消息 → None（51-52）。
4. 回退到 `start` 后首条仍非 user（异常序列，例如对话区首条是 assistant）→ None（63-64）。
5. 尾部首条是 tool 结果（保留了 assistant 但丢了 tool_use）→ 回退（61-62）；`_cut_balanced` 只看 role 字符串，不校验 `tool_call_id` 一致性（未验证异常序列下的完整性）。
6. `build_new_session_messages(summary="")` → 不注入摘要 system 消息（102）。
7. `tail_messages=None` → 仅 system + 摘要（104）。
8. 尾部的 system 消息被静默丢弃（105）——即使调用方显式传入。
9. 估价对 `content` 为 list（Anthropic 块形态）时按 `str(content)` 近似（`tools/token_estimate.py:47-49`）——含块结构字符，偏保守。
10. 迭代方向依赖列表顺序假设（尾部即最新），不做时间戳校验。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【低】`estimate_tokens` 为转发壳（16-18），而真正消费者（`message_manager.py:139/197`）可直接调用 `token_estimate`——多一层无增值间接。
2. 【低】命名与语义不符：`_cut_balanced` 名为"balanced"，实际只判定 `role == "user"`（21-30）。
3. 【低】注释保留历史包袱：说明"此前固定 4 字符/token 对中文低估约 3 倍"（10-12）+ 引用外部"官方 harness token-meter 思路"（11）——变更理由留在注释里，规格化时无法核对。
4. 【低】`Compressor` docstring 强调"纯函数"（71-74），但它是无状态类（无 `__init__`），构造点仍有 `Compressor()`（`assembly.py:85`）——语义上是命名空间而非对象。
5. 【低】摘要格式（`# 上一轮对话成果`）以 f-string 内联（103），而消费方（`COMPRESS_PROMPT` 的"若对话中已存在…"规则，`config/defaults.py:85`）在另一文件硬编码同一标题——格式契约分居两处。

**可测性**：

- 可完全独立单测（纯函数，无 IO、无网络）：
  - `select_cut_index`：预算充足/不足/恰好、尾部非 user 回退、全 system、空列表、`retain_tokens=0/负`、返回 0 的语义；
  - `_cut_balanced`：边界 `cut == len`；
  - `build_compress_messages`：断言"原列表未被改写 + 末尾指令内容 == COMPRESS_PROMPT"；
  - `build_new_session_messages`：空摘要/带摘要/尾部含 system/None 尾部；
  - `estimate_tokens`：与 `token_estimate.estimate_message_tokens` 等值（转发一致性）。
- 需要集成测试：与 `MessageManager.handle_compress` 的联合行为（裁切 + 原子替换 + pending_input 收尾），可参考 `tool_exp/repair_seq_test.py`。
- 无法自动化：无（除估价精度本身需要真实 API usage 对照）。

---

### narnat_agent/core/compression_coordinator.py（180行）

**职责**：压缩流程编排——三个入口（用户轮边界自动压缩 / 400 溢出恢复压缩 / `/compact` 手动压缩）、占比刷新与阈值查询，并统一压缩前后的 UI 动画与状态复位。

**对外接口**：

- 模块常量：
  - `OVERFLOW_CONTINUE_MESSAGE`（20-23）：溢出恢复压缩后追加的内部继续指令文本。
  - `_MANUAL_FAIL_TEXT`（26-30）：手动压缩失败原因 → 用户提示的映射表。
- `class CompressionCoordinator`：
  - `def __init__(self, config: Config, msg_manager: MessageManager, llm: LLMClient, context: ContextManager, ui: UIInterface, logger: AgentLogger)`：依赖注入。
  - `def compress(self, pending_input: str) -> bool`：用户轮边界的自动压缩（成功携带新输入收尾）。
  - `def compress_no_input(self) -> bool`：溢出恢复压缩（无新输入，以内部继续指令收尾）。
  - `def compress_manual(self) -> Tuple[str, str]`：手动压缩，返回 `(status, text)`，status ∈ `ok|empty|error`。
  - `def need_compress(self) -> bool`：占比是否已达压缩阈值。
  - `def refresh_ratio(self, input_tokens: int) -> None`：刷新窗口占比。
  - `def mid_run_guard(self) -> bool`：运行中自查并压缩，返回 True 表示"执行了压缩且成功"。

**依赖**：

- `..config.loader.Config`（11）
- `.message_manager.MessageManager`（12）
- `.llm.LLMClient`（13）
- `.context.ContextManager`（14）
- `..ui.ui_design.UIInterface`（15）
- `..ui.interrupt._interrupt_ctrl`（16）
- `..logger.AgentLogger`（17）
- 标准库：`typing.Tuple`（9）

**被依赖**：

产品代码：
- `narnat_agent/assembly.py:24` `from .core.compression_coordinator import CompressionCoordinator`（177-179 构造；182 `session_mgr.compact_func = compression_coordinator.compress_manual`；188 注入 AgentLoop 的 `compression=`）
- `narnat_agent/core/session_callbacks.py:609`（注释说明接线来源）、`610`（`self.compact_func: Optional[Callable[[], Tuple[str, str]]] = None`）、`730-736`（`on_compact` 转发并处理 `status == "ok"`）
- `narnat_agent/core/agent_loop.py:42`（构造参数 `compression=None`）、`57-58/87/90/171/190`（按对象调用 `refresh_ratio`/`need_compress`/`mid_run_guard`/`compress_no_input`）
- `narnat_agent/core/agent.py:92-94/222`（`need_compress`/`compress`）

实验脚本（`tool_exp/`）：无直接 import（`tool_exp/verify_assembly.py:17` 仅打印提及类名的断言文案）。

**状态**：

- 模块级全局：无可变全局；两个模块级常量（上述）。
- 类变量：无。
- 实例状态：`_config`、`_msg_manager`、`_llm`、`_context`、`_ui`、`_logger`（40-45）。
- 跨模块共享状态（读/写）：`_interrupt_ctrl`（ui 层全局单例，见 `ui/interrupt.py:278`）——本文件由它读 `is_set`（65/95/129）并写运行/输入模式（123/140）；`_context` 的 `_ratio`（51/57/72/83/88/102/143/166）。

**行为要点**（编号；附行号）：

1. `compress(pending_input)` 流程（47-73）：定义两个回调——`on_interrupt`（结束压缩动画 + `context.reset()` + `append_user(pending_input)`，49-52）与 `on_llm_error(msg)`（结束动画 + error 日志 + `context.set_retry_soon()` + `append_user(pending_input)`，54-58）；`ui.begin_compressing()`（60）→ `msg_manager.handle_compress(pending_input, config.system_prompt, llm, cancel_check=lambda: _interrupt_ctrl.is_set, on_interrupt=..., on_llm_error=..., retain_tokens=config.session.retain_tokens)`（61-69）；`res.ok` 为真 → `ui.end_compressing()` + `context.reset()`（70-73）；返回 `res.ok`。
2. `compress_no_input()`（75-103）：与 `compress` 同构，但 `pending_input=OVERFLOW_CONTINUE_MESSAGE`（92），且两个回调都不追加用户消息（81-88）；注释 77-79 说明"成功后消息必须以内部继续指令收尾（OpenAI 协议要求 user/assistant 收尾，system 兜底不可行）"。
3. `compress_manual()`（105-154）：`_interrupt_ctrl.enter_run_mode()`（123，注释 110-111：命令在输入模式执行，临时切运行模式让 Esc 取消手感一致）→ `ui.begin_compressing()`（124）→ `handle_compress(None, ...)`（125-133）；`finally` 内两重保护：先 `ui.end_compressing()` 再 `_interrupt_ctrl.enter_input_mode()`（134-140，注释 135-136：避免 ESC 轮询线程滞留运行态）。
4. `compress_manual` 返回契约（142-154）：成功 → `context.reset()` + info 日志（含 replaced/tokens）+ `("ok", f"已压缩 {res.replaced} 条历史消息（约 {res.tokens} tokens）")`；`reason == "empty"` → `("empty", "没有可压缩的历史对话")`；`"interrupted"` → `("error", "压缩已取消，历史未变更")`；其他 → `("error", _MANUAL_FAIL_TEXT.get(res.reason, "压缩失败: 未识别的失败原因"))`（注释 153：未知 reason 不宣称历史状态）。
5. `_MANUAL_FAIL_TEXT` 三条映射（26-30）：`llm_error` → "压缩失败: LLM调用出错，历史未变更"；`empty_summary` → "压缩失败: 总结为空，历史未变更"；`overflow` → "压缩失败: 压缩请求自身超出模型上下文限制，历史未变更"。
6. `need_compress()` 纯转发（156-158）；`refresh_ratio(input_tokens)` 转发到 `context.update_ratio`（160-166，注释 161-165：占比原先只在用户轮末更新，运行中自查因此永不触发）。
7. `mid_run_guard()`（168-180）：未超阈值 → False（177-178）；超阈值 → `logger.warning("compressor", "运行中占比超阈值，主动压缩历史")`（179）→ 返回 `compress_no_input()`（180）。
8. 三个入口的差异（对照表，用于写规格）：
   | 入口 | 触发方 | pending_input | 失败时是否写用户输入 | 是否动 ESC 模式 |
   |---|---|---|---|---|
   | `compress` | agent.py:94（阈值） | 用户新输入 | 是（回调 52/58） | 否 |
   | `compress_no_input` | agent_loop.py:90/190 | 内部继续指令 | 不写 | 否 |
   | `compress_manual` | /compact（session_callbacks.py:732） | None | 不写 | 是（123/140） |
9. 压缩成功一律 `context.reset()`（72/101/143）→ 占比置 None，下一次 `need_compress()` 为假。

**边界/异常行为**（编号；附行号）：

1. `handle_compress` 抛异常时 `compress_manual` 的 `finally` 先恢复输入模式，异常继续向上冒泡（无捕获，134-140）；此时 `res` 未定义也不会被读到（异常已传播）。
2. `compression` 的取消语义取决于 `_interrupt_ctrl.is_set`（65/95/129）——用户按 Esc 后压缩请求会被放弃并把输入写回历史（52）。
3. `handle_compress` 返回 `reason="interrupted"` 时 `compress()` 返回 False（`res.ok` 为假），`agent.py:95-96` 因此 `continue` 回输入提示；用户输入已由 `on_interrupt` 写入历史（52），不会重复写入（`agent.py:99` 的 `if not compress_ok` 守卫）。
4. `reason="empty"` 只在手动路径可达（`message_manager.py:131-134` 的 `pending_input is None` 条件），自动路径不会提前返回。
5. `mid_run_guard` 内部再次判 `need_compress()`（177）——调用方已判一次（`agent_loop.py:87`），是冗余但幂等的双检。
6. 失败路径不改占比的例外：`compress_no_input` 的 `on_llm_error` 会 `set_retry_soon()`（88）；`compress` 同理（57）——即"自动/溢出失败"会压低占比，而"手动失败"完全不动（142-154 只在 ok 时 reset）。
7. 硬编码文案：`OVERFLOW_CONTINUE_MESSAGE` 以 `"[]"` 开头（21）——前缀用途无注释说明（未验证是否为标记约定）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【中】core → ui 反向依赖全局单例：`from ..ui.interrupt import _interrupt_ctrl`（16），并直接切模式（123/140）与取 `is_set`（65/95/129）——与 `core/interrupt.py` 声称的"解耦 ui↔llm"设计（`core/interrupt.py:1-5`）形成两套中断通道。
2. 【中】`compress` 与 `compress_no_input` 逐字重复（47-73 vs 75-103）：回调定义、begin/end 动画、`handle_compress` 的 7 个实参、`context.reset()` 全部镜像，差异仅"pending_input 常量 + 是否 append 输入"。
3. 【中】返回值三形态（`bool` / `bool` / `(status, text)`，47/75/105）→ 三个调用方各写一套适配（`agent.py:94`、`agent_loop.py:90/190`、`session_callbacks.py:732`）。
4. 【中】失败原因枚举分散：`CompressResult.reason` 在 `message_manager.py:20-22` 定义 5 值，本文件在 149-154 与 26-30 分两处各自映射。
5. 【低】`_MANUAL_FAIL_TEXT` 未覆盖 `reason="empty_summary"` 的"历史未变更"表述冲突（26 行措辞如此，若压缩在替换后失败不可能出现）——未验证是否可能。
6. 【低】魔法前缀 `"[]"`（21）无语义说明。
7. 【低】`mid_run_guard` 的阈值判断与 `agent_loop` 重复（177 vs `agent_loop.py:87`）。

**可测性**：

- 可独立单测（fake msg_manager/ui/context/llm）：三个入口的返回值与状态副作用、失败回调路径（`on_interrupt`/`on_llm_error` 是否写输入与 `set_retry_soon`）、`_MANUAL_FAIL_TEXT` 映射、`mid_run_guard` 的真/假分支。**阻碍**：`_interrupt_ctrl` 是模块级全局单例，测试需 `monkeypatch` `compression_coordinator._interrupt_ctrl`（并注意 `enter_run_mode` 会启动真实 ESC 轮询线程，涉及终端状态）。
- 需要集成测试：真实 `MessageManager.handle_compress` + `Compressor` + 假 LLM（验证"历史原子替换 + pending_input 收尾 + 无历史可压缩"的三方协同），可参考 `tool_exp/repair_seq_test.py` 与 `tool_exp/verify_assembly.py`。
- 无法自动化：压缩动画（`ui.begin_compressing`）的终端视觉；`_interrupt_ctrl` 真实 ESC 时序（msvcrt/termios 轮询）。

---

### narnat_agent/core/summarizer.py（58行）

**职责**：两处 LLM 文案生成——探索分支合并时的对话总结（`summarize`）与自动保存时的会话命名（`name_session`）。

**对外接口**：

- `class Summarizer`：
  - `def __init__(self, llm: LLMClient, config: Config, logger: AgentLogger)`：依赖注入。
  - `def summarize(self, messages: List[Dict[str, Any]], cancel_check: Callable[[], bool]) -> str`：用 LLM 总结消息内容（取消时返回空串）。
  - `def name_session(self, messages: List[Dict[str, Any]]) -> str`：用 LLM 生成会话名（失败返回空串）。

**依赖**：

- `.llm.LLMClient`（9）
- `..config.loader.Config`（10）
- `..logger.AgentLogger`（11）
- 函数内延迟导入：`..config.session_store.list_sessions`（36）
- 标准库：`typing.{List, Dict, Any, Callable, Optional}`（7）

**被依赖**：

- `narnat_agent/assembly.py:22` `from .core.summarizer import Summarizer`（92 构造；128 `summarize_func=lambda msgs, cancel: summarizer.summarize(msgs, cancel)`；132 `name_func=lambda msgs: summarizer.name_session(msgs)`——两者注入 SessionManager）
- `narnat_agent/core/auto_save_manager.py:14` `from .summarizer import Summarizer`（23 构造参数；58 `name = self._summary.name_session(...)`）

（`tool_exp/` 无直接 import 本文件的脚本。）

**状态**：

- 模块级全局：无可变全局。
- 类变量：无。
- 实例状态：`_llm`、`_config`、`_logger`（18-20）——其中 `_logger` 在本文件内**无任何使用点**。

**行为要点**（编号；附行号）：

1. `summarize`（22-32）：以 `chat_stream(messages, no_tools=True, cancel_check=cancel_check)` 流式请求（26-27）；每次循环先判 `cancel_check()`，真则立即返回空串（28-29）；只累计"含 `content` 且不含 `tool_calls` 键"的 chunk（30-31）；返回拼接字符串（32）。
2. `name_session` 流程（34-58）：`list_sessions(config.paths.narnat_dir)` 取已有会话（36-37）→ 提取 `taken_names`（38）→ 若非空拼提示 `\n注意：以下名称已被占用，请勿使用：{...}`（39-41）→ 复制消息并追加 user 指令（42-46）：`"请为以上对话起一个简短标题（15字以内），直接输出标题，不要引号不要解释。{hint}\n【重要】不要调用任何工具，直接输出标题文本。"` → `chat_stream(name_messages, no_tools=False, no_thinking=True, cancel_check=lambda: False)`（48-49）。
3. 名称清洗链（52-57）：拼接 → `.strip()` → 空则返回 ""（52-54）→ `.strip('"\'""''《》「」')` 去掉常见包裹引号/书名号（55）→ `len(name) > 30` 截断到 30 字符（56-57）→ 返回。
4. 两个方法对 chunk 的选择规则一致（`"content" in chunk and "tool_calls" not in chunk`，30/50）——与 `agent_loop` 的纯文本判定（`agent_loop.py:131`）同构。

**边界/异常行为**（编号；附行号）：

1. `summarize` 的取消只在循环内检查（28）：若 `chat_stream` 零输出（例如网络失败提前 return），返回空串而非报错——调用方无法区分"取消"与"总结为空"。
2. `name_session` 的 `cancel_check=lambda: False`（49）——命名请求不可取消。
3. 命名长度两个阈值不一致：提示词要求"15字以内"（45），代码截断阈值为 30 字符（56）。
4. `no_tools=False` + 提示词"不要调用任何工具"（45、48）矛盾组合：模型可能返回 tool_call chunk，此时该 chunk 无 `content` 被忽略（50），严重情况下名称流可能全无 content → 返回空串（53-54）。
5. `list_sessions` 异常未捕获（36-37）→ 冒泡到调用方（`auto_save_manager._do_save` 后台线程 或 会话命令）。
6. `summarize` 无长度限制/后处理（对比 `name_session` 有截断）——总结长度完全由模型与 `COMPRESS_PROMPT` 之外的上下文决定。
7. 清洗用的字符集合同时含半角与全角引号（55：`"`、`'`、`“`、`”`、`‘`、`’`、`《`、`》`、`「`、`」`），其中 U+201C/U+201D 在源码字符串里各出现一次（`strip` 语义下重复无害）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【中】行为靠提示词约束而非参数：`name_session` 用 `no_tools=False`（48）却写"【重要】不要调用任何工具"（45）——与 `summarize` 用 `no_tools=True`（26）不一致。
2. 【中】阈值双写且不一致：提示"15字以内"（45）vs 代码截断 30（56）。
3. 【中】函数内延迟 import `list_sessions`（36）——规避 import 环的补丁式写法。
4. 【低】持而未用的 `self._logger`（20）：命名/总结路径的失败无日志痕迹（调用方 `auto_save_manager` 也不记录返回空串的原因）。
5. 【低】历史包袱注释：模块 docstring 声明"从 Agent._do_summarize() 和 Agent._do_name_session() 提取。算法逻辑原样保留。"（3-4）——指向已不存在的旧实现。
6. 【低】未使用 import：`typing.Optional`（7）。`python -m pyflakes narnat_agent\core\summarizer.py` 输出：`7:1: 'typing.Optional' imported but unused`。

**可测性**：

- 可独立单测（fake LLM 即可，无网络）：
  - `summarize`：chunk 过滤规则（带 tool_calls 的 content 不入结果）、取消即时返回空串、零输出返回空串；
  - `name_session`：断言传给 `chat_stream` 的实参（`no_tools=False`、`no_thinking=True`、`cancel_check` 恒 False）、去引号/书名号、31 字截断、空名返回 ""、已占用名称提示拼接。
- 需要集成测试：`list_sessions` 依赖磁盘目录（`.narnat/data/sessions`），需临时目录；`AutoSaveManager._do_save` 的后台线程与状态切换（`tool_exp/` 无现成用例）。
- 无法自动化：真实模型对"15 字标题"与"不调用工具"提示的遵从度。

---

### narnat_agent/core/interrupt.py（22行）

**职责**：中断回调的注册/触发单点——ui 层注册"关闭当前 LLM 请求"的回调，llm 层通过本模块触发，以解耦 ui↔llm 的循环依赖。

**对外接口**：

- `def register_abort(callback: Callable[[], None]) -> None`：注册 LLM 请求中断回调（由 ui/llm 层调用）。
- `def abort_request() -> None`：触发中断回调（由 ui 层 ESC 轮询调用）；未注册时为 no-op。

**依赖**：

- 无内部模块依赖（仅标准库 `typing.{Optional, Callable}`，7）。

**被依赖**：

- `narnat_agent/core/llm.py:24` `from .interrupt import register_abort`（140 在 import 期执行 `register_abort(abort_active_llm_request)`）
- `narnat_agent/ui/interrupt.py:37` 函数内 `from ..core.interrupt import abort_request`（41 在 `_on_esc_detected` 中调用，位于 `try` 内、`except Exception: pass` 兜底）

（无其他引用点；`tool_exp/` 无 import 本文件的脚本。）

**状态**：

- 模块级全局（可变）：`_abort_callback: Optional[Callable[[], None]] = None`（10）
  - 写：`register_abort`（15）
  - 读：`abort_request`（21-22）
- 类变量：无。实例状态：无（纯模块函数）。

**行为要点**（编号；附行号）：

1. 单槽位注册：`register_abort` 无条件覆盖 `_abort_callback`（13-16）——进程内"当前中断目标"只有一个，最后一次注册生效。
2. 触发：`abort_request` 回调为 None 时静默返回（19-22）；非 None 时直接调用（22），无异常处理。
3. 实际链路（跨文件）：`ui/interrupt.py:35-44` 的 `_on_esc_detected` 在 ESC 按下时 `ctrl._interrupt.set()` → `abort_request()` → `core/llm.py:130-137` 的 `abort_active_llm_request` 关闭 `LLMClient._active_response`。
4. 与另一条中断通道的关系：`ui/interrupt.py:16` 的 `InterruptController` 是**另一套**机制（`_interrupt_ctrl.is_set`），被 `compression_coordinator`（65/95/129）与 `ui_design.UIStreamSession.cancelled`（`ui/ui_design.py:205-206`）使用；两套并行存在。

**边界/异常行为**（编号；附行号）：

1. 未注册时 `abort_request()` 是 no-op（21）——例如在纯库使用场景（未 import `core.llm`）中不会报错。
2. 回调自身异常不被捕获（19-22）；唯一调用点外面有 `except Exception: pass`（`ui/interrupt.py:45-47`）兜底，因此异常会被静默吞掉。
3. 注册语义是"替换"而非"追加/注销"（13-16）——多客户端场景下后注册者覆盖先注册者；无 `unregister` API。
4. 无锁、无线程亲和性要求（10、15、22）——注册与触发通常发生在不同线程（ESC 轮询线程触发）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【中】单槽位全局回调（10）：`LLMClient` 实例不参与中断寻址，多实例/多会话只能有一个中断目标；同时它是**导入期副作用**的载体（`llm.py:140`）。
2. 【中】两套中断通道并存：本模块（回调式，供 llm 关闭连接）+ `ui/interrupt.py` 的 `InterruptController`（事件式，供 core 读 `is_set`）——core 侧同时依赖两者（`compression_coordinator.py:16` 与 `llm.py:24`），解耦目标只完成了一半。
3. 【低】无注销/清理接口（13-22）：进程生命周期内回调常驻，测试之间需要手工改 `_abort_callback` 才能隔离。

**可测性**：

- 可完全独立单测（无 IO、无第三方）：
  - 注册后触发 → 回调被调用一次；
  - 重复注册 → 只有最后注册者被调用；
  - 未注册触发 → 无异常；
  - 回调抛异常 → 异常向调用方传播（当前语义如此），便于为"调用方是否兜底"写契约测试。
  - 注意：测试后需复位 `core.interrupt._abort_callback`（无公开 reset）。
- 需要集成测试：与 `core/llm.py:140` 的注册副作用联动（import `core.llm` 后 `abort_request()` 应能关闭活跃连接）。
- 无法自动化：ESC 真实按键 → `abort_request` 的端到端时序。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

本组内部互依赖（`core/` 内）：

| 源 | 目标 | 行号 | 导入内容 |
|---|---|---|---|
| core/llm.py | core/interrupt.py | 24 | `register_abort` |
| core/llm.py | core/message_list.py | 25 | `SYNTHETIC_THINKING` |
| core/compression_coordinator.py | core/llm.py | 13 | `LLMClient`（类型标注） |
| core/compression_coordinator.py | core/context.py | 14 | `ContextManager`（类型标注） |
| core/compression_coordinator.py | core/message_manager.py | 12 | `MessageManager`（类型标注） |
| core/summarizer.py | core/llm.py | 9 | `LLMClient` |
| core/agent_loop.py | core/llm.py | 11 | `LLMClient, retry_sleep` |
| core/agent_loop.py | core/message_manager.py | 12 | `MessageManager` |
| core/agent_loop.py | core/tool_dispatcher.py | 13 | `ToolDispatcher` |
| core/agent_loop.py | core/stats.py | 17 | `StatsTracker` |
| core/agent.py | （无 core 内 import；经 `..assembly`（16）间接获得全部协作者） | — | — |
| core/context.py | （无 core 内 import） | — | — |
| core/compressor.py | （无 core 内 import） | — | — |
| core/interrupt.py | （无 core 内 import） | — | — |

本组 → 其它层（出边）：

| 源 | 目标 | 行号 | 备注 |
|---|---|---|---|
| core/llm.py | config/loader.py | 22 | `AIConfig` |
| core/llm.py | config/defaults.py | 23 | `resolve_thinking_params, resolve_thinking_passback` |
| core/llm.py | tools/goal_complete | 214 | 函数内延迟 import |
| core/agent_loop.py | tools/tool_context.py | 14 | `ToolContext, AWAIT_CONFIRM` |
| core/agent_loop.py | tools/background | 15,16 | `running_count, running_summary` |
| core/agent_loop.py | tools/exec_signal.py | 21 | `strip_tags` |
| core/agent_loop.py | tools/registry.py | 452 | 函数内延迟 import `execute` |
| core/agent_loop.py | ui/ui_design.py | 18 | `UIInterface` |
| core/agent_loop.py | config/loader.py | 19 | `Config` |
| core/agent_loop.py | output | 20 | `write` |
| core/agent_loop.py | logger.py | 22 | `AgentLogger` |
| core/agent.py | assembly.py | 16 | `Assembly, AssemblyResult` |
| core/agent.py | output | 17 | `write, X, R` |
| core/agent.py | tools/background.py | 41,198 | 函数内 `prepare` |
| core/agent.py | tools/{terminal,serial,background} | 178-180,269-271 | 函数内 `cleanup/cleanup_all` |
| core/context.py | config/defaults.py | 7-9 | 三个阈值常量 |
| core/compressor.py | config/defaults.py | 7 | `COMPRESS_PROMPT` |
| core/compressor.py | tools/token_estimate.py | 8 | `estimate_message_tokens` |
| core/compression_coordinator.py | ui/ui_design.py | 15 | `UIInterface` |
| core/compression_coordinator.py | ui/interrupt.py | 16 | `_interrupt_ctrl`（全局单例） |
| core/compression_coordinator.py | config/loader.py | 11 | `Config` |
| core/compression_coordinator.py | logger.py | 17 | `AgentLogger` |
| core/summarizer.py | config/loader.py | 10 | `Config` |
| core/summarizer.py | config/session_store.py | 36 | 函数内 `list_sessions` |
| core/summarizer.py | logger.py | 11 | `AgentLogger` |

其它层 → 本组（入边，被依赖）：

| 源 | 目标 | 行号 | 导入内容 |
|---|---|---|---|
| main.py | core/agent.py | 33 | `Agent` |
| narnat_agent/__init__.py | core/agent.py | 5 | `Agent` |
| assembly.py | core/llm.py | 12 | `LLMClient` |
| assembly.py | core/context.py | 13 | `ContextManager` |
| assembly.py | core/compressor.py | 14 | `Compressor` |
| assembly.py | core/agent_loop.py | 21 | `AgentLoop` |
| assembly.py | core/summarizer.py | 22 | `Summarizer` |
| assembly.py | core/compression_coordinator.py | 24 | `CompressionCoordinator` |
| core/message_manager.py | core/compressor.py | 9 | `Compressor, select_cut_index, estimate_tokens` |
| core/auto_save_manager.py | core/summarizer.py | 14 | `Summarizer` |
| ui/interrupt.py | core/interrupt.py | 37 | 函数内 `abort_request` |

按对象引用（非 import，但在新架构中必须保留的行为面）：`core/agent.py` → `AgentLoop.run/_last_round_ok/_last_content_parts`；`core/session_callbacks.py:610/732` ← `CompressionCoordinator.compress_manual`（经 `compact_func` 注入）；`assembly.py:74/133/182` → `LLMClient.add_tool_definitions/remove_tool_definitions/set_goal_tool`、`CompressionCoordinator.compress_manual`。

实验脚本（`tool_exp/`，非产品分发）对本组的 import：`core/llm.py` ← `api_contract_test.py:14`、`stall_test.py:20`、`repair_seq_test.py:19`、`retry_unify_probe.py:12`、`verify_retry_resilience.py:16,17`、`verify_stream_fix.py:14,15`、`mcp_test/demo_persistence.py:18`、`mcp_test/test_mcp_runtime.py:233`、`retry_notice_probe.py:20`、`verify_goal_e2e.py:22`、`verify_goal_mode.py:57`、`verify_import_cleanup.py:17`；`core/agent_loop.py` ← `retry_notice_probe.py:51`、`retry_unify_probe.py:13`、`verify_renderer_reset_race.py:126`、`verify_retry_resilience.py:18,19`、`verify_stream_fix.py:150,151`、`verify_stream_interrupt.py:8,9`、`verify_ui_showcase.py:153`；`core/compressor.py` ← `repair_seq_test.py:18`。

### 模块级可变状态全清单（含读写方）

| 状态 | 定义 | 读 | 写 | 隔离性 |
|---|---|---|---|---|
| `core.llm.LLMClient._active_response` | `llm.py:171`（类变量） | `llm.py:132` | `llm.py:315,346,384,401,409,538,617,631,653,656,670,673,683,694,712,721,982` | 进程级共享；跨实例互相覆盖 |
| `core.llm.LLMClient._max_network_retries` | `llm.py:175` | `llm.py:370,374,387,391,664,668,674,697,701` | `llm.py:181`（`set_retry_count`） | 进程级共享；写点来自 `llm.py:191`、`agent_loop.py:82` |
| `core.llm.LLMClient._max_rate_retries` | `llm.py:176` | `llm.py:362,366,647,651,657` | `llm.py:182`（`set_retry_count`） | 同上 |
| `core.llm.LLMClient._STREAM_END` | `llm.py:172` | `llm.py:445,769` | 不写入（哨兵身份不变；入队：`llm.py:160,747`） | 进程级共享常量 |
| `core.llm.LLMClient.RETRY_BACKOFF_BASE` | `llm.py:173` | `llm.py:45,61`；`agent_loop.py:275,276` | 生产代码无写点 | 进程级共享常量（列表可变） |
| `core.llm.LLMClient._STREAM_STALL_SECONDS` | `llm.py:174` | `llm.py:437,439,761,763` | 生产代码无写点；外部测试改写 `tool_exp/stall_test.py:111,146` | 进程级共享常量 |
| `core.interrupt._abort_callback` | `interrupt.py:10` | `interrupt.py:21` | `interrupt.py:15`（`register_abort`，由 `llm.py:140` 调用） | 进程级单槽位；无注销 API |
| `ui.interrupt._interrupt_ctrl`（本组参与读写） | `ui/interrupt.py:278` | `compression_coordinator.py:65,95,129`；`ui_design.py:205-206` | `compression_coordinator.py:123,140`（模式切换）；`ui_design.py:257/267/294/303/315/325/331` | 进程级单例 |
| `core.context.ContextManager._ratio`（实例） | `context.py:34` | `context.py:40,55,59,61` | `context.py:49,51,71,76`；间接写：`agent.py:171`、`agent_loop.py:58` | 实例级；无锁，跨线程/跨阶段写 |
| `core.context.ContextManager._warned`（实例） | `context.py:35` | `context.py:59` | `context.py:62,72` | 实例级 |
| `LLMClient._tool_defs`（实例，被后端共享引用） | `llm.py:189` | `llm.py:336`（OpenAI 传参）、`llm.py:579`（Anthropic 转换） | `llm.py:221,223-226,238,246-249` | 实例级但**两个对象共享同一列表** |
| `AgentLoop._last_round_ok`（实例，外部可读） | `agent_loop.py:72,386` | `agent.py:143,239` | `agent_loop.py:72,386` | 实例级，被编排层读 |
| `AgentLoop._last_content_parts`（实例，外部可读） | `agent_loop.py:102` | `agent.py:127-128` | `agent_loop.py:102` | 实例级，被编排层读 |
| `ToolContext.{goal_complete,todo_reminded,bg_reminded,current_todos}`（本组参与写） | `tools/tool_context.py:55-69` | `agent_loop.py:350-353,373,390`；`tools/goal_complete/__init__.py:45-55` | `agent.py:84,86,88,91`、`agent.py:147,244`；`agent_loop.py:354,374`；`tools/goal_complete/__init__.py:46,55` | 实例级；读写方跨 3 个模块 |
| `ToolContext.pending_delete` / `_delete_confirmed`（本组参与写） | `tools/tool_context.py:55`、`_delete_confirmed`（同文件） | `agent_loop.py:239`、`tools/{bash,terminal,serial}` 内部 | `agent_loop.py:240-241,451`；`tools/bash/__init__.py:483`、`tools/terminal/__init__.py:559,626`、`tools/serial/__init__.py:329` | 实例级；确认状态由工具层写、循环层清 |
| `SessionManager._goal_enabled / _goal_max_rounds`（本组参与读写） | `core/session_callbacks.py:622-623` | `agent.py:105,107`；`ui/session_commands.py:254,266,268` | `agent.py:207,208`；`ui/session_commands.py:242,243,255,256` | 实例级；读写方跨 core/ui 两层 |
| `Dispatcher._executor`（本组参与读） | `core/tool_dispatcher.py:78` | `agent.py:177,268` | 构造时（`assembly.py:153`） | 实例级，私有字段被外部关闭 |

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

1. 【高】`core/llm.py:171` — 类级可变全局 `_active_response` 充当进程唯一中断句柄（写点 17 处），实例间不可隔离，无锁。
2. 【高】`core/llm.py:178-182`（+ `llm.py:191`、`agent_loop.py:82`）— 重试次数是类变量，构造与每轮 run 双写，属配置的全局副作用。
3. 【高】`core/agent.py:105,107,207-210` — 直接读写 `SessionManager._goal_enabled/_goal_max_rounds/_set_goal_tool` 私有成员（`ui/session_commands.py:242-256` 同病）。
4. 【高】`core/agent_loop.py:451`（+ `core/agent.py:127-128,143,239`）— 跨模块私有字段直写（`tool_context._delete_confirmed`）与反射式读循环内部状态（`_last_content_parts`/`_last_round_ok`）。
5. 【高】`core/agent.py:139-168` 与 `core/agent.py:236-263` — 目标模式自动续跑算法两份实现（含收尾注入与续跑文案）。
6. 【中】`core/compression_coordinator.py:16,65,95,123,129,140` — core 直接依赖 ui 层全局 `_interrupt_ctrl` 并切模式，与 `core/interrupt.py` 的回调解耦设计并行成两套中断通道。
7. 【中】`core/llm.py:43-67` — 模块级函数引用文件后部才定义的 `LLMClient`（前向依赖），把类逻辑摊到模块层。
8. 【中】`core/llm.py:330,436-444` 与 `core/llm.py:616,760-768` — 两个后端的超时/看门狗/重试是镜像复制（看门狗 9 行逐字相同），429/5xx 耗尽文案风格不一致。
9. 【中】`core/llm.py:251-255`（+ `core/agent_loop.py:476`）— `raw_sse` 仅 Anthropic 后端可用，OpenAI 协议下静默为 None，而空回复取证依赖它。
10. 【中】`core/llm.py:213-215`（+ `core/agent_loop.py:452`）— core → tools 的反向依赖藏在函数体内（goal_complete 定义、registry 执行）。

紧随其后（第 11–18 位，各文件节内有完整清单）：`core/summarizer.py:45/48` 用提示词代替 `no_tools` 参数、`core/summarizer.py:45/56` 长度阈值双写、`core/compressor.py:16-18` 转发壳、`core/context.py:70-71` docstring 与实现不符、`core/context.py:74-76` 用数值伪造状态、`core/llm.py:190/195` 死状态（`_max_output_tokens`/`_protocol`）、`core/llm.py:16/20` 与 `core/summarizer.py:7` 未使用 import、`tool_exp/retry_notice_probe.py:20` 等过期 `set_retry_count` 导入面（静态判定，未运行验证）。

### 本组对外契约清单（被 core 之外模块依赖的 public API，即新架构必须保持的行为面）

1. `core.agent.Agent(project_root=None, debug=False, headless=False)`、`Agent.run()`、`Agent.run_headless(task, max_rounds=0)`
   - 消费方：`main.py:33`、`narnat_agent/__init__.py:5`
   - 行为面：主循环交互语义、`/` 命令返回码 0/1/2 的处理、headless 的 `[NN_DONE] reason=... rounds=...` 输出行（父代理据此判定结束与原因，`agent.py:267`）。
2. `core.llm.LLMClient` 构造 `(config: AIConfig, logger=None, max_output_tokens: int = 128000, tool_definitions: list = None)`
   - 消费方：`assembly.py:65-70`
   - 行为面：从 `config` 取 protocol/api_key/base_url/model/temperature/max_tokens/thinking_*/retry_count。
3. `LLMClient.chat_stream(messages, no_tools=False, no_thinking=False, cancel_check=None) -> Iterator`
   - 消费方：`agent_loop.py:110`、`summarizer.py:26/48`、`message_manager.py:152`（鸭子类型）
   - **chunk 事件协议（新架构必须逐字保持）**：
     - `{"content": str}`：文本增量（Anthropic thinking-only 兜底时也会以 content 形式下发，`llm.py:890-893/962-965`）；
     - `{"tool_calls": [{"id","type":"function","function":{"name","arguments"}}], "finish_reason": str, "thinking": str|None[, "thinking_signature": str|None]}`；
     - `{"finish_reason": str[, "thinking": ...]}`：无工具轮的完成事件；
     - `{"usage": {"prompt_tokens","completion_tokens","cached_tokens"}}`（可多次，Anthropic 另可能来自 `message_start` 兜底）；
     - `{"retry_notice": str}`：网络/429/5xx 重试提示（`agent_loop.py:145-147` 只渲染不入历史）；
     - `{"stream_interrupted": {"kind": "timeout"|"network"|"unknown", "detail": str}}`：流中断（`agent_loop.py:140-142`）；
     - `{"finish_reason": "context_overflow"}`：400 超限归类（`agent_loop.py:152-154`、`message_manager.py:157-161`）；
     - `{"content": "[错误: ...]", "finish_reason": "error"}`：各类失败文案（`agent_loop.py:155-164`）。
   - finish_reason 取值集合（下游分支依赖）：`stop / tool_calls / max_tokens / content_filter / server_busy / error / context_overflow`（+ Anthropic 未知 stop_reason 原样透传，`llm.py:870-871`）。
4. `LLMClient.set_retry_count(n)`、`LLMClient.RETRY_BACKOFF_BASE`
   - 消费方：`agent_loop.py:82`、`agent_loop.py:275-276`；外部测试改写（`tool_exp/stall_test.py:111,146`）
   - 行为面：钳制 1–10（`llm.py:181-182`）；退避表 `[1,2,4,8,8]` 同时被连接层与循环层使用（提示文案的秒数必须一致）。
5. `LLMClient.set_goal_tool/add_tool_definitions/remove_tool_definitions/raw_sse`
   - 消费方：`assembly.py:74`（MCP 热更新接线）、`assembly.py:133`（/goal 注入）、`agent_loop.py:476`（空回复取证）
   - 行为面：幂等、按名去重、原地改列表使后端立即可见；`raw_sse` 仅 Anthropic 协议有值。
6. `core.llm.retry_sleep(attempt, cancel_check=None) -> bool`
   - 消费方：`agent_loop.py:292`；外部测试 monkeypatch（`tool_exp/verify_retry_resilience.py:100/293/330/346/364/391/393`、`verify_stream_fix.py:18/152`、`verify_stream_interrupt.py:12`）
   - 行为面：返回 False = 用户取消（循环层据此 abort + `ui.on_interrupted()`）。
7. `core.interrupt.register_abort(callback)` / `abort_request()`
   - 消费方：`llm.py:140`（注册）、`ui/interrupt.py:41`（触发）
   - 行为面：单槽位、未注册 no-op、触发即关闭活跃连接。
8. `core.agent_loop.AgentLoop` 构造 `(llm, msg_manager, dispatcher, tool_context, stats, ui, config, logger, compression=None)` 与 `run(stream, goal_mode=False, force_final=False)`
   - 消费方：`assembly.py:185-189`、`agent.py:121/158/228/255`
   - 行为面：统计栏显示条件（`show_stats`，`agent_loop.py:390-399`）、提前 return 不置 `_last_round_ok`、`_last_content_parts` 每轮重置语义。
9. `core.compression_coordinator.CompressionCoordinator` 构造与 `compress/compress_no_input/compress_manual/need_compress/refresh_ratio/mid_run_guard` + `OVERFLOW_CONTINUE_MESSAGE`
   - 消费方：`assembly.py:177-182/188`、`session_callbacks.py:610/730-736`、`agent.py:92-94`、`agent_loop.py:57-58/87/90/190`
   - 行为面：三入口的返回值形态（bool/bool/(status,text)）、status ∈ {ok, empty, error} 与文案、成功即 `context.reset()`、`compress_manual` 必须恢复输入模式。
10. `core.message_manager.CompressResult` 契约（本组依赖它）：`reason ∈ {empty, interrupted, llm_error, empty_summary, overflow}`（`message_manager.py:14-27`）；失败/中断不改动历史，`ok=True` 时 `pending_input` 已在历史中（`message_manager.py:114-116`）。
11. `core.summarizer.Summarizer` 构造与 `summarize(messages, cancel_check)` / `name_session(messages)`
    - 消费方：`assembly.py:92/128/132`（注入 SessionManager 的 `summarize_func`/`name_func`）、`auto_save_manager.py:23/58`
    - 行为面：失败返回空串（调用方据此静默跳过保存/合并）；`name_session` 的名称清洗与长度截断。
12. `core.context.ContextManager` 全部方法
    - 消费方：`assembly.py:77-82`、`agent.py:93/171/172`、`compression_coordinator.py`（全部转发）
    - 行为面：占比定义（prompt_tokens / 配置窗口 × 100）、窗口 ≤ 0 时无数据、`need_compress` 闭区间、告警单会话一次、`reset` 置 None、`set_retry_soon` 使占比 = 阈值 − 10。
13. `core.compressor.Compressor.build_compress_messages/build_new_session_messages` 与 `select_cut_index/estimate_tokens`
    - 消费方：`assembly.py:85`、`message_manager.py:130/139/147/197/198/218`
    - 行为面：尾部保留切点必须落在 user 消息上；切点只落非 system 消息；新会话结构 = system + （摘要 system）+ 尾部（过滤 system）；摘要标题字面量为 `# 上一轮对话成果`。

### 验收自检对照（本报告）

1. 覆盖清单全部 8 个文件，各一个 `###` 节：`llm.py`、`agent_loop.py`、`agent.py`、`context.py`、`compressor.py`、`compression_coordinator.py`、`summarizer.py`、`interrupt.py`。
2. 「行为要点」「边界/异常行为」「补丁痕迹」每条均带行号（含跨文件行号）。
3. 「被依赖」清单由 grep 得出，可用 `grep -rn "core\.\(llm\|agent_loop\|context\|compressor\|compression_coordinator\|summarizer\|interrupt\)"` 复验（抽查项：`assembly.py:12-14/21-24`、`core/message_manager.py:9`、`core/auto_save_manager.py:14`、`ui/interrupt.py:37`、`main.py:33`、`narnat_agent/__init__.py:5`）。
4. 总表四项齐备：依赖关系矩阵（出边/入边两张表）、模块级可变状态全清单、补丁痕迹 TOP10、本组对外契约清单。
5. 所有签名逐字取自源码（可用 grep 对照 `def ` 行）；行数用 `find /c /v ""` 实测。
6. 不确定事项已标注「未验证」（正文共 15 处）或「未运行验证」（静态判定但未执行，共 2 处：`core/llm.py` 节补丁痕迹第 7 条、TOP10 第 11–18 位说明）——主要集中在真实服务端措辞、并发交叉、`os._exit` 之后的行为与外部测试脚本的可运行性。
