# Spec Delta

## Purpose

定义 LLM 客户端能力的双协议行为契约：OpenAI 兼容与 Anthropic 兼容两种协议下的流式请求构建、统一事件流输出、重试与退避、思考参数与回传的厂商适配、工具定义动态管理、上下文超限判定与中断取消语义。本能力的对外契约是统一事件流的字段与取值（上层对话循环按此消费），必须逐字保持。余额查询不属本能力（归 `stats`）。

## ADDED Requirements

### Requirement: 协议选择与请求构建

系统 SHALL 按配置的协议值选择协议：值为 `anthropic` 时走 Anthropic 兼容协议，其余任意值（含未知值与拼写错误）一律走 OpenAI 兼容协议。发送前系统 SHALL 递归清理消息中的非法 UTF-8 代理字符（字典与列表递归处理，其他类型原样），且 SHALL NOT 改动调用方持有的消息对象。OpenAI 兼容请求 SHALL 以流式发起，携带模型名、消息数组、`stream` 与流式用量统计选项 `stream_options={"include_usage": true}`，超时固定为连接 5 秒、读 300 秒、写 60 秒、连接池 30 秒；底层客户端自身的自动重试 SHALL 关闭（重试完全自管）。`temperature` 与 `max_tokens` 仅在配置的 `温度`、`最大输出token数` 非空时携带。

#### Scenario: 协议选择
- **WHEN** 配置协议值为 `anthropic`
- **THEN** 走 Anthropic 兼容协议；值为其他任意值（如拼写错误）时静默走 OpenAI 兼容协议，不报错

#### Scenario: 非法代理字符清理
- **WHEN** 待发送消息文本含非法 UTF-8 代理字符
- **THEN** 发送前被替换清理，且调用方持有的消息内容不变

#### Scenario: 可选参数缺省
- **WHEN** `温度` 与 `最大输出token数` 均未配置
- **THEN** 请求中不出现 `temperature` 与 `max_tokens`（而非传 0 或默认值）

### Requirement: Anthropic 兼容请求体组装

Anthropic 兼容请求 SHALL 发往 `<接口地址>/v1/messages`（接口地址末尾斜杠先去除），携带 `x-api-key`、`anthropic-version: 2023-06-01` 与 JSON 内容类型请求头。请求体 SHALL 含模型名、转换后的消息数组、`max_tokens`（默认 128000）与 `stream: true`；思考参数的两类位置（请求顶层与扩展体）SHALL 都合入请求体顶层；`system` 仅在系统文本非空时写入；`tools` 仅在工具列表非空且未裁剪时写入；配置的 `最大输出token数` 非空时 SHALL 覆盖默认 `max_tokens`；请求超时为连接 10 秒、读 300 秒、写 60 秒、连接池 30 秒。

#### Scenario: 端点与认证
- **WHEN** 协议为 Anthropic 兼容且接口地址为 `https://api.example.com/anthropic/`
- **THEN** 请求发往 `https://api.example.com/anthropic/v1/messages`，并携带 `x-api-key` 与版本请求头

#### Scenario: 可选字段缺省
- **WHEN** 消息不含 `system` 且工具列表为空
- **THEN** 请求体不含 `system` 与 `tools` 字段

#### Scenario: 最大输出覆盖
- **WHEN** 配置了 `最大输出token数`
- **THEN** 请求体的 `max_tokens` 取配置值（未配置时取默认 128000）

### Requirement: Thinking 参数映射规则

系统 SHALL 以（协议，模型名小写前缀）匹配厂商映射表，按表内固定顺序取首个命中；未命中时 SHALL 不传任何思考参数。参数位置规则：OpenAI 兼容协议的"请求顶层"项合入请求参数、"扩展体"项放入 `extra_body`；Anthropic 兼容协议的两类项均合入请求体顶层。强度值 SHALL 仅在启用思考且强度非空时注入，且存在厂商强度映射时 SHALL 先做语义强度到实际值的转换再写入指定路径。关闭思考（思考开关关闭或请求裁剪）时 SHALL 走禁用分支：有禁用映射时传显式关闭参数，无禁用映射时 SHALL 完全不传任何思考参数（而非显式关闭）。

#### Scenario: 未匹配模型
- **WHEN** 模型名不匹配任何厂商前缀
- **THEN** 请求中不出现任何 thinking 相关参数

#### Scenario: 禁用映射缺失
- **WHEN** 关闭思考且该厂商条目没有禁用映射
- **THEN** 不传任何思考参数

#### Scenario: 强度注入
- **WHEN** 启用思考、强度为 `high` 且厂商定义了强度映射
- **THEN** 强度经映射转换后写入该厂商指定的参数路径

### Requirement: Thinking 厂商参数表

启用思考时，系统 SHALL 构造以下厂商参数——Anthropic 兼容协议：`deepseek` → 请求体 `thinking={"type": "enabled"}`、强度经 `output_config.effort`；`mimo` → 请求体 `thinking={"type": "enabled"}`（无强度参数）；`claude` → 请求体 `thinking={"type": "adaptive"}`、强度经 `effort`。OpenAI 兼容协议：`deepseek` → `extra_body` 内 `thinking={"type": "enabled"}`、强度经 `reasoning_effort`；`glm` → `extra_body` 内 `thinking={"type": "enabled", "clear_thinking": false}`、强度经 `reasoning_effort`；`kimi` → `extra_body` 内 `thinking={"type": "enabled"}`（无强度参数）；`mimo` → `extra_body` 内 `thinking={"type": "enabled"}`（无强度参数）；`qwen` → `extra_body` 内 `enable_thinking=true`、强度经 `thinking_budget` 映射（`max`/`xhigh`/`high`/`medium`/`low`/`minimal`/`none` → 32000/24000/16000/8000/4000/1000/0）；`gpt` → 不附加开关参数（`reasoning_effort` 即开关与强度）。关闭思考时：`glm`/`kimi`/`mimo`（OpenAI）→ `extra_body` 内 `thinking={"type": "disabled"}`；`qwen` → `extra_body` 内 `enable_thinking=false`；`gpt` → `reasoning_effort="none"`。

#### Scenario: DeepSeek 双协议差异
- **WHEN** 模型为 DeepSeek 且分别使用 Anthropic 兼容协议与 OpenAI 兼容协议并启用思考
- **THEN** 前者把思考参数合入请求体顶层、强度走 `output_config.effort`；后者把思考参数放入扩展体、强度走 `reasoning_effort`

#### Scenario: Qwen 强度映射
- **WHEN** 模型为 Qwen、启用思考、强度为 `max`
- **THEN** 扩展体含 `enable_thinking=true` 与 `thinking_budget=32000`

#### Scenario: GPT 关闭思考
- **WHEN** 模型为 GPT 且关闭思考
- **THEN** 请求携带 `reasoning_effort="none"`

#### Scenario: Kimi 无强度参数
- **WHEN** 模型为 Kimi、启用思考且配置了强度
- **THEN** 请求只含思考开关参数，不注入任何强度参数

### Requirement: 思考回传格式与开关

系统 SHALL 以（协议，模型前缀）查表决定思考回传格式：`thinking_block`（Anthropic 协议思考内容块，无需签名；DeepSeek/MiMo 的 Anthropic 组合）、`thinking_block_signed`（Anthropic 协议思考块且必须携带签名；Claude）、`reasoning_content`（OpenAI 兼容协议顶层 `reasoning_content` 字段；DeepSeek/GLM/Kimi/MiMo）、`none`（不回传；Qwen/GPT 及未匹配）。回传开关关闭时 SHALL 一律不回传。OpenAI 兼容协议 SHALL 仅在"思考已启用 + 回传开关开启 + 回传格式为 `reasoning_content` + 消息为含工具调用的 assistant + 思考非空"时把思考写入 `reasoning_content`；其余情况（含纯文本轮）SHALL 剥离 `thinking` 与 `thinking_signature` 字段。Anthropic 兼容协议 SHALL 仅在"思考已启用 + 回传开关开启 + 回传格式为思考块类"时回传，且仅当该 assistant 含工具调用或思考为中断修复写入的合成占位值时回传（纯文本轮的思考不回传）；格式为 `thinking_block_signed` 且签名缺失时 SHALL 省略该思考块。完成事件中的 `thinking` 输出：OpenAI 兼容协议仅在"思考已启用 + 回传开关开启 + 格式为 `reasoning_content`"时给出拼接结果，否则为空值；Anthropic 兼容协议仅由回传开关决定。

#### Scenario: 工具轮回传（OpenAI 协议）
- **WHEN** DeepSeek（OpenAI 兼容协议）的工具调用轮携带思考，思考开关与回传开关均开启
- **THEN** 该 assistant 消息以顶层 `reasoning_content` 回传思考

#### Scenario: 纯文本轮不回传
- **WHEN** assistant 消息无工具调用但携带思考（OpenAI 兼容协议）
- **THEN** 思考被剥离，请求中不含 `reasoning_content`

#### Scenario: 签名缺失省略（Anthropic 协议）
- **WHEN** Claude 的思考块没有可回传的签名
- **THEN** 该思考块整体省略（不报错）

#### Scenario: 回传开关关闭
- **WHEN** 回传开关关闭
- **THEN** 所有协议均不回传思考，且完成事件中 `thinking` 为空值

### Requirement: 消息格式转换（OpenAI → Anthropic）

转换 SHALL 遵循以下规则：`system` 角色消息的内容抽为独立系统文本（多条以空行连接、空内容跳过）且不进入消息数组；连续的纯文本 `user` 消息合并为一条（第二条起追加为文本块），前一条 `user` 含工具结果块时 SHALL NOT 合并；`assistant` 转为内容块序列，顺序为 [思考块, 文本块, 工具调用块…]，工具调用块形态为 `{"type": "tool_use", "id": …, "name": …, "input": …}`，其中 `arguments` 字符串 SHALL 解析为对象（解析失败取空对象）；无工具调用但有思考时同样携带思考块；`tool` 角色消息转为 `{"type": "tool_result", "tool_use_id": …, "content": …}` 并追加到前一条 `user` 消息的内容列表（前一条不是 `user` 或内容非列表时新建 `user` 消息承载）；其余 `assistant` 以纯文本承载。

#### Scenario: 系统消息提升
- **WHEN** 消息数组含两条 `system` 消息
- **THEN** 两条内容以空行连接为顶层系统文本，消息数组不含 `system` 角色

#### Scenario: 连续用户消息合并
- **WHEN** 出现连续两条纯文本 `user` 消息
- **THEN** 合并为一条消息、两个文本块

#### Scenario: 工具结果不合并
- **WHEN** 前一条 `user` 消息含工具结果块，其后来了一条新的 `user` 文本
- **THEN** 新消息保持独立（不合并进工具结果消息）

#### Scenario: 工具结果映射
- **WHEN** `tool` 角色消息紧随含工具调用的 `assistant`
- **THEN** 转为工具结果块并追加进其后的 `user` 消息内容列表

#### Scenario: 转换失败
- **WHEN** 消息转换过程抛出异常
- **THEN** 下发 `{"content": "[错误: 消息格式转换失败: <详情>]", "finish_reason": "error"}` 并结束本轮

### Requirement: 工具定义传递与转换

OpenAI 兼容协议 SHALL 原样传递工具定义列表。Anthropic 兼容协议 SHALL 逐条转换为 `{"name": …, "description": …, "input_schema": …}`：名称与描述缺省为空串，`input_schema` 取定义中的 `parameters`，缺失时用 `{"type": "object", "properties": {}}`；定义没有外层 `function` 包装时 SHALL 直接取定义本身的字段。

#### Scenario: 参数模式缺省
- **WHEN** 工具定义没有参数模式（`parameters`）
- **THEN** 转换结果使用空对象模式 `{"type": "object", "properties": {}}`

#### Scenario: 裸定义
- **WHEN** 工具定义未包 `function` 层（直接含 `name`/`parameters`）
- **THEN** 转换按定义自身字段取值

### Requirement: 文本与思考事件流

系统 SHALL 把两种协议统一为同一事件流（OpenAI 形态）。文本增量 SHALL 在到达时立即以 `{"content": …}` 下发并同时累积。OpenAI 兼容协议的思考增量（`reasoning_content`，从增量属性或扩展字段两路读取）SHALL 只累积、不单独下发。Anthropic 兼容协议的思考增量 SHALL 累积为思考块；结束事件（`message_delta`）到达时若无任何文本而有思考，系统 SHALL 把思考内容作为正式文本以 `{"content": …}` 下发，且该轮完成事件的 `thinking` 使用空串（避免重复占用上下文）。同一轮流中 `content` 事件可出现多次。

#### Scenario: 文本增量即时下发
- **WHEN** 流中出现文本增量
- **THEN** 每个增量即时以 `{"content": …}` 下发

#### Scenario: 思考不回显
- **WHEN** 流中出现思考增量
- **THEN** 不单独下发思考事件（仅累积，随完成事件回传）

#### Scenario: 仅思考回复兜底
- **WHEN** 结束事件到达时没有任何文本但已累积思考内容
- **THEN** 思考内容以 `{"content": …}` 作为正式文本下发，完成事件的 `thinking` 为空串

### Requirement: 工具调用流式聚合

系统 SHALL 以工具调用 id 为键聚合流式增量：增量带 id 时记录该 id（含位置索引到 id 的映射）；只有位置索引且此前出现过映射时 SHALL 用映射补 id；无任何依据时 SHALL 生成 `_tc_<序号>` 形式的占位 id（序号自增）。名称与参数 SHALL 按到达顺序字符串拼接（参数不做解析）。完成标记到达时：有工具调用缓冲 SHALL 下发 `{"tool_calls": [{"id": …, "type": "function", "function": {"name": …, "arguments": …}}…], "finish_reason": …, "thinking": …}`；无缓冲 SHALL 下发 `{"finish_reason": …, "thinking": …}`。Anthropic 兼容协议的工具调用 SHALL 按内容块索引升序输出，完成事件额外含 `thinking_signature`（恰一个思考块且签名非空时取签名值，否则为空值）。

#### Scenario: 增量聚合
- **WHEN** 同一次工具调用的名称与参数分多个增量到达
- **THEN** 完成事件中 `name` 为完整拼接结果、`arguments` 为原始拼接字符串

#### Scenario: 索引补 id
- **WHEN** 增量只有位置索引而无 id，且此前同索引已出现过 id
- **THEN** 该增量归属到已记录的 id

#### Scenario: 占位 id
- **WHEN** 增量既无 id 也无可用索引映射
- **THEN** 生成 `_tc_<序号>` 占位 id 并自增序号

#### Scenario: 无工具轮完成事件
- **WHEN** 流以完成标记结束且无工具调用缓冲
- **THEN** 下发 `{"finish_reason": …, "thinking": …}`（不带 `tool_calls` 键）

### Requirement: 用量事件归一

用量 SHALL 以 `{"usage": {"prompt_tokens": …, "completion_tokens": …, "cached_tokens": …}}` 形态下发，同一轮流中可出现多次。OpenAI 兼容协议的缓存命中数 SHALL 三级回退：`prompt_tokens_details.cached_tokens` → 顶层 `prompt_cache_hit_tokens` → 扩展字段中的 `prompt_cache_hit_tokens`，均缺失时取 0；不含选项（`choices`）的仅用量块 SHALL 正常处理。Anthropic 兼容协议的输入用量 SHALL 为 `input_tokens + 缓存命中数`（缓存取 `cache_read_input_tokens` 或 `prompt_cache_hit_tokens`），输出用量取 `output_tokens`；流首事件（`message_start`）的初始用量 SHALL 仅保存备用，在结束事件（`message_delta`）缺失时补发一次（`completion_tokens` 为 0）。

#### Scenario: 缓存字段回退
- **WHEN** 用量含顶层 `prompt_cache_hit_tokens`
- **THEN** `cached_tokens` 取该值；`prompt_tokens_details.cached_tokens` 存在时优先取之

#### Scenario: 仅用量块
- **WHEN** 收到不含选项的仅用量块
- **THEN** 正常下发用量事件（不被跳过）

#### Scenario: 结束事件缺失补用量
- **WHEN** Anthropic 兼容流正常结束但未收到结束事件
- **THEN** 以流首事件保存的初始用量补发一次用量事件

### Requirement: 完成原因取值与协议映射

OpenAI 兼容协议 SHALL 原样透传服务端给出的完成原因（取值集合含 `stop`、`tool_calls`、`max_tokens`、`content_filter`、`server_busy`）。Anthropic 兼容协议 SHALL 映射结束原因：`end_turn`→`stop`、`tool_use`→`tool_calls`、`max_tokens`/`length`→`max_tokens`、`content_filter`→`content_filter`、`insufficient_system_resource`→`server_busy`，其余值原样透传；结束原因缺失时按 `end_turn` 处理。异常与超限路径的取值 SHALL 为 `error` 与 `context_overflow`；流被中断的轮次 SHALL NOT 伪造完成原因。

#### Scenario: 未知结束原因透传
- **WHEN** Anthropic 兼容流返回映射表外的结束原因
- **THEN** 该值原样出现在完成事件的 `finish_reason`

#### Scenario: 结束原因缺失
- **WHEN** 结束事件未携带结束原因
- **THEN** 按 `end_turn` 映射为 `stop`

### Requirement: 重试、退避与重试通知

系统 SHALL 自管重试：状态 400/401/403/404/422 不重试（400 命中超限特征除外）；429 按限流类重试；408、409 与全部 ≥500 状态按服务端类重试；连接与超时异常按网络类重试；其他异常不重试。重试上限 SHALL 取自配置 `智能体.LLM重试次数`（默认 3），在构造与每轮对话开始时同步，钳制到 1–10；限流类与服务端/网络类上限同值。退避 SHALL 取序列 `[1, 2, 4, 8, 8]` 秒的第 `<重试次数>-1` 项（超出末项后固定 8 秒），叠加 ±25% 抖动；等待期间 SHALL 以不大于 0.2 秒的分片轮询取消标记。每次重试前 SHALL 先下发 `{"retry_notice": …}`，文案为 `\n⚠ <原因>，约<基数>s后自动重试（第<次数>/<上限>次）…\n`，原因取值为 `网络连接失败`（网络类）、`请求被限流(429)`（限流类）、`服务端错误(<状态码>)`（服务端类），`<基数>` 与该次真实退避基数一致；该事件仅供渲染，不进入对话历史。

#### Scenario: 429 重试
- **WHEN** 连续收到 429 且未达上限
- **THEN** 每次先下发重试通知（原因 `请求被限流(429)`、秒数为退避序列对应值）再退避重发

#### Scenario: 服务端错误重试
- **WHEN** 收到 503 且未达上限
- **THEN** 下发重试通知（原因 `服务端错误(503)`）后退避重发

#### Scenario: 重试上限钳制
- **WHEN** 配置的重试次数为 0、负值或大于 10
- **THEN** 实际生效上限被钳制在 1–10 区间内

#### Scenario: 退避期间取消
- **WHEN** 退避等待中取消标记被置位
- **THEN** 不再重发请求，静默结束本轮

### Requirement: 错误事件与文案

请求最终失败时系统 SHALL 下发 `{"content": …, "finish_reason": "error"}` 并结束本轮，文案按协议与失败原因固定：OpenAI 兼容协议——不可重试失败 `[错误: API调用失败(<异常类型>): <详情>]`、重试耗尽 `[错误: API调用失败(<异常类型>，重试<次数>次): <详情>]`；Anthropic 兼容协议——429 耗尽 `[错误: API返回429速率限制(已重试<次数>次)]`、可重试状态耗尽 `[错误: API返回<状态码>错误(已重试<次数>次)]`、其他非 200 状态 `[错误: API调用失败(<状态码>): <响应文本前 200 字符>]`、流内错误事件 `[错误: <服务端错误消息>]`（消息缺失时为 `未知错误`）、连接类耗尽与其他异常同 OpenAI 文案。用户取消 SHALL NOT 产生任何错误事件。

#### Scenario: 不可重试状态（OpenAI 协议）
- **WHEN** 响应状态为 401
- **THEN** 下发 `[错误: API调用失败(<异常类型>): <详情>]`，`finish_reason` 为 `error`，不重试

#### Scenario: 429 重试耗尽（Anthropic 协议）
- **WHEN** Anthropic 兼容协议 429 已达上限
- **THEN** 下发 `[错误: API返回429速率限制(已重试<次数>次)]`

#### Scenario: 流内错误事件
- **WHEN** Anthropic 兼容流下发 `error` 事件
- **THEN** 立即下发 `[错误: <服务端错误消息>]` 与 `finish_reason` 为 `error` 并结束

### Requirement: 上下文超限识别

状态 400 且响应文本（含错误体）不区分大小写命中超限特征时，系统 SHALL 只下发 `{"finish_reason": "context_overflow"}`（不带 `content`）且 SHALL NOT 重试；特征集合 SHALL 覆盖中英措辞：`context length`、`context window`、`context_length_exceeded`、`context is too long`、`prompt is too long`、`too long`、`exceeds the maximum`、`maximum context`、`input length`、`超出上下文`、`上下文长度`、`超出限制`、`超限`。未命中任何特征的 400 SHALL 按不可重试错误处理。

#### Scenario: 命中超限特征
- **WHEN** 400 响应文本含 `prompt is too long`
- **THEN** 只下发 `{"finish_reason": "context_overflow"}`，不重试

#### Scenario: 未命中特征
- **WHEN** 400 响应文本不含任何超限特征
- **THEN** 按不可重试错误下发错误事件（`finish_reason` 为 `error`）

### Requirement: 流中断上报与静默挂死检测

流读取异常且未收到完成标记时，系统 SHALL 下发 `{"stream_interrupted": {"kind": …, "detail": …}}`：`kind` 为 `timeout`（超时类异常，含挂死检测伪造的超时）、`network`（传输类异常）、`unknown`（其他）；`detail` 为异常文本前 200 字符。流已收到首个数据块后静默超过 180 秒 SHALL 触发挂死检测：记录超时类异常并主动关闭连接，随后按流中断上报；首字节之前的静默 SHALL NOT 触发该检测（由读超时兜底）。同一轮流只保留并上报第一个异常。OpenAI 兼容协议以是否收到完成标记为准，Anthropic 兼容协议以是否收到结束事件（`message_delta`）为准。

#### Scenario: 中途断开
- **WHEN** 流读取抛出传输类异常且此前未收到完成标记
- **THEN** 下发 `{"stream_interrupted": {"kind": "network", "detail": <异常文本>}}`，不产出完成事件

#### Scenario: 静默挂死
- **WHEN** 已收到数据块后流静默超过 180 秒
- **THEN** 记录超时类异常、主动关闭连接，并上报 `kind` 为 `timeout` 的流中断

#### Scenario: 首字节前静默
- **WHEN** 尚未收到任何数据时静默超过 180 秒
- **THEN** 不触发挂死检测

#### Scenario: 完成后的异常
- **WHEN** 已收到完成标记（或 Anthropic 结束事件）之后流才抛出异常
- **THEN** 不上报流中断

### Requirement: 取消与中断

系统 SHALL 在**请求发送期（建连/等响应头）与流式接收期**均以不大于 0.05 秒的间隔轮询取消标记：取消标记为真时 SHALL 静默结束本轮（不下发任何事件、不重试）并清理活跃请求句柄。请求发送 SHALL 在独立执行体中执行（主流程轮询取消标记）：取消命中时 SHALL 立即返回（不等发送完成）、尽力关闭在途连接，且发送执行体在阻塞解除后 SHALL 自毁其结果（关闭响应/流，关闭异常忽略）——保证任何网络阶段（含连接建立中，此时关闭对在途请求是空操作）取消都能及时收敛。系统 SHALL 维护单一活跃请求句柄：请求发出前指向本次请求的句柄、流就绪后指向响应流、本轮结束（含异常路径）清空；中断触发时 SHALL 关闭该句柄，关闭异常被忽略。OpenAI 兼容协议的请求句柄 SHALL 为请求级 scope：中断仅影响本次在途请求，并标记客户端待重建（后续请求使用新客户端）——SHALL NOT 因一次中断使共享客户端永久不可用。中断回调 SHALL 以单槽位注册，未注册或句柄为空时触发为空操作。

#### Scenario: 流内取消
- **WHEN** 流式接收中取消标记被置位
- **THEN** 静默结束（无完成事件、无错误事件、无中断事件）

#### Scenario: 发送期取消（连接建立中）
- **WHEN** 请求发送阻塞于连接建立阶段时取消标记被置位（此时关闭对在途请求无效）
- **THEN** 本轮在 ≤0.05 秒轮询粒度内静默结束，不等待连接建立完成或响应头到达

#### Scenario: 中断关闭连接
- **WHEN** 中断触发且存在活跃请求句柄
- **THEN** 该连接或流被关闭，本轮随即结束

#### Scenario: 空闲中断
- **WHEN** 中断触发但无活跃请求句柄
- **THEN** 空操作

#### Scenario: 中断后 OpenAI 客户端仍可用
- **WHEN** OpenAI 兼容协议下在响应头到达前中断本轮
- **THEN** 后续轮次请求正常发出并完成（共享客户端已被重建，不产生永久性失败）

### Requirement: 工具定义动态管理

系统 SHALL 在构造时复制调用方传入的工具定义列表；动态变更 SHALL NOT 改动调用方持有的原列表，并 SHALL 对下一轮请求立即生效（请求组装时现读该列表）。目标模式开启 SHALL 向列表注入 GoalComplete 定义并保持幂等（已存在不重复注入）；关闭 SHALL 移除该定义（幂等）。热追加（MCP 服务器连接成功后送入定义）SHALL 按工具名去重：同名定义保留先到者、不重复追加，名称为空的定义跳过，同一次送入列表内部也去重；热注销 SHALL 按名移除全部匹配定义，空名单为无操作。系统 SHALL NOT 改写工具名（重名消解由来源侧在送入前完成）。

#### Scenario: 注入幂等
- **WHEN** 目标模式连续两次开启
- **THEN** 工具列表中 GoalComplete 定义仍只存在一份

#### Scenario: 热追加去重
- **WHEN** 送入的定义与列表现有定义同名
- **THEN** 不重复追加；不同名定义被追加且在下一轮请求即可见

#### Scenario: 热注销
- **WHEN** 按名清单注销工具定义
- **THEN** 清单中所有同名定义被移除，其余定义不受影响

#### Scenario: 原列表不变
- **WHEN** 构造后执行任意动态变更
- **THEN** 调用方传入的原列表内容保持不变

### Requirement: 原始响应取证

Anthropic 兼容协议 SHALL 在每轮请求开始时清空记录，并按到达顺序记录本次流的所有 `data:` 数据段（其余行不记录），读取时返回副本。OpenAI 兼容协议无该记录，读取为无效（空）。

#### Scenario: 记录数据段
- **WHEN** Anthropic 兼容流中出现 `data:` 行
- **THEN** 按到达顺序可读取到对应数据段

#### Scenario: OpenAI 无记录
- **WHEN** 使用 OpenAI 兼容协议后读取取证数据
- **THEN** 得到无效（空）结果

### Requirement: 请求裁剪选项

请求 SHALL 支持两个裁剪选项：不带工具定义（内部摘要等调用使用）——请求 SHALL 不携带 `tools`；不带思考——thinking 参数按禁用分支构造（有禁用映射时传显式关闭参数，无禁用映射时不传任何思考参数）。裁剪选项 SHALL NOT 影响思考回传开关的判定与完成事件中的思考输出。

#### Scenario: 不带工具定义
- **WHEN** 以不带工具定义的方式请求
- **THEN** 两种协议的请求体内均不出现 `tools`

#### Scenario: 不带思考且无禁用映射
- **WHEN** 以不带思考的方式请求，且当前（协议×模型）无禁用映射
- **THEN** 请求体内不出现任何 thinking 相关参数

#### Scenario: 裁剪不影响回传
- **WHEN** 以不带思考的方式请求
- **THEN** 完成事件中的思考输出仍按思考回传开关与格式判定（与裁剪无关）

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（避免用户可感知差异）：OpenAI 兼容协议对名称与参数均为空的工具调用增量仍输出该调用（不过滤），而 Anthropic 兼容协议在结束事件缺失的兜底路径会过滤 id 或名称为空的工具调用；该兜底路径下若工具调用全部被过滤，则本轮不产出任何完成事件；思考内容作为正式文本兜底输出时，完成事件的 `thinking` 为空串（而非无效值）；多个思考块并存时 `thinking_signature` 不上报（仅恰一个思考块且签名非空时上报）；400 超限特征匹配宽松（`too long`、`超限` 等宽泛子串可能把非超限错误判为超限）；Anthropic 兼容协议的连接超时为 10 秒（与 OpenAI 兼容协议的 5 秒不一致）；非 200 且不属于固定分类的状态（如 3xx 重定向）走通用错误文案。若未来修正，须作为独立变更处理。

#### Scenario: 空名工具调用（兼容怪癖）
- **WHEN** OpenAI 兼容协议收到过工具调用增量且名称与参数均为空
- **THEN** 完成事件仍含一条名称为空的工具调用（现状保持）

#### Scenario: 兜底过滤（兼容怪癖）
- **WHEN** Anthropic 兼容流缺结束事件，且工具调用块 id 或名称为空
- **THEN** 该工具调用被过滤；若全部被过滤则不产出完成事件（现状保持）

#### Scenario: 多思考块签名（兼容怪癖）
- **WHEN** Anthropic 兼容流出现两个以上思考块且带签名
- **THEN** 完成事件中 `thinking_signature` 为空值（宁缺勿错，现状保持）

#### Scenario: 3xx 状态（兼容怪癖）
- **WHEN** Anthropic 兼容端点返回 302
- **THEN** 按通用错误文案 `[错误: API调用失败(302): <响应文本前 200 字符>]` 上报（现状保持）
