# tools-websearch Specification

## Purpose
定义网页搜索能力（`WebSearch` 工具）的行为契约：工具名与参数面、接口密钥与接口地址的来源、搜索接口的调用协议与超时、响应解析、相关性排序与结果格式化、错误与空结果文案。工具名、参数名、参数默认值、输出格式与错误文案是面向 AI 与用户的已发布契约，必须跨版本保持等价。

## Requirements

### Requirement: WebSearch 工具名与参数契约

系统 SHALL 以固定工具名 `WebSearch` 注册网页搜索工具，工具定义语义为「网页搜索（用于查找API文档、解决方案、技术文章等）」。参数 SHALL 为：`query`（string，必填，搜索查询词）、`num`（integer，可选，默认 5，返回结果数量，须为正整数，上限 20）。`num` 为空 `None` 时 SHALL 回落 5；以字符串等形态传入时 SHALL 容错转换为整数；转换失败或值为非正数 SHALL 返回 `[错误: num需为正整数]` 且不发起任何网络调用；大于 20 的值 SHALL 静默裁剪为 20（不报错）。

#### Scenario: 只传必填参数
- **WHEN** 调用 `WebSearch` 仅传 `query`
- **THEN** 按 `num=5` 处理

#### Scenario: num 非法
- **WHEN** `num` 为 0、负数或非数值字符串
- **THEN** 返回 `[错误: num需为正整数]`，不发起网络调用

#### Scenario: num 上限裁剪
- **WHEN** `num` 传 100
- **THEN** 按 20 处理（不报错）

### Requirement: WebSearch 接口密钥与接口地址来源

`WebSearch` 的接口密钥与接口地址 SHALL 在每次调用时从工具运行时上下文读取，不得跨调用缓存：密钥取自密钥表的 `websearch` 键；接口地址取自 `websearch_url` 键，该键缺失或未配置时使用默认地址 `https://api.anysearch.com/mcp`。密钥为空串（密钥未配置、密钥表缺失或上下文缺失）时 SHALL 返回 `[错误: 搜索失败]` 且不发起网络调用。

#### Scenario: 已配置密钥与自定义地址
- **WHEN** 密钥表为 `{"websearch": "sk-x", "websearch_url": "https://example.com/mcp"}`
- **THEN** 请求发往 `https://example.com/mcp` 且携带密钥（作为 `X-API-Key` 请求头）

#### Scenario: 未配置密钥
- **WHEN** 密钥表为空或缺少 `websearch` 键
- **THEN** 返回 `[错误: 搜索失败]`，不发起网络调用

#### Scenario: 未配置地址回落默认值
- **WHEN** 配置了 `websearch` 密钥但未配置 `websearch_url`
- **THEN** 请求发往默认地址 `https://api.anysearch.com/mcp`

### Requirement: WebSearch 接口调用协议与超时

`WebSearch` SHALL 以 JSON-RPC 2.0 的 `tools/call` 请求调用搜索接口：请求体的 `method` 为 `tools/call`、`params.name` 为 `search`、`params.arguments` 固定含 `query`（搜索词）、`max_results`（本次结果数）与 `zone`（固定 `"cn"`）；请求头 `Content-Type` 为 `application/json`，已配置密钥时附 `X-API-Key`。单次调用超时 SHALL 为 15 秒。响应 SHALL 取结果 `content` 列表中全部 `text` 块拼接后按 Markdown 解析为 `{title, url, description}` 结果列表：结果块以 `### N.` 序号标题起始，标题取标题行文本，链接取 `- **URL**: …` 形态行中的 http/https 地址，摘要取链接之后的文本并剥离 `- `、`* `、`**Description**: ` 前缀；标题或链接缺失的块 SHALL 被丢弃。

#### Scenario: 请求构造
- **WHEN** 以 `query="python asyncio"`、`num=3` 发起搜索
- **THEN** 请求参数为 `name=search`、`arguments={"query": "python asyncio", "max_results": 3, "zone": "cn"}`

#### Scenario: Markdown 响应解析
- **WHEN** 接口返回含 `### 1. 标题`、`- **URL**: https://…` 与摘要文本的内容块
- **THEN** 解析出一条结果，标题、链接、摘要分别取自对应位置（摘要不残留前缀标记）

#### Scenario: 超时保护
- **WHEN** 接口 15 秒内未响应
- **THEN** 调用以异常结束并按错误文案返回（见「错误与空结果文案」）

### Requirement: WebSearch 相关性排序与结果格式化

`WebSearch` SHALL 对解析出的全部结果按与查询词的相关性稳定降序排序，再截取前 `num` 条输出。相关性打分 SHALL 为「标题命中数 ×3 + 摘要命中数 ×1」除以「查询词数 ×4」；查询词集合由英文与数字词（长度 ≥2、小写）与中文连续段的所有相邻双字组合（单字段落取该字本身）构成；查询词集合为空时打分为 0.5（中性）。

输出 SHALL 为每条结果至多三行：标题行 `{序号}. {标题}`（序号从 1 起）；链接行三个空格 + `URL: {链接}`；摘要非空时输出一行三个空格 + 摘要。标题超过 120 字符 SHALL 截断为前 120 字符并追加 `…`；摘要 SHALL 将连续空白（含换行）压缩为单个空格并去除首尾空白，超过 300 字符截断为前 300 字符并追加 `…`；摘要为空 SHALL 不输出摘要行。

#### Scenario: 标题命中优先
- **WHEN** 两条结果的摘要相同，其中一条标题命中查询词
- **THEN** 标题命中者排在前面

#### Scenario: 无查询词的中性打分
- **WHEN** 查询词无法提取出任何英文数字词或中文字符
- **THEN** 全部结果打分为 0.5，保持接口返回的原始顺序（稳定排序）

#### Scenario: 截断与空白压缩
- **WHEN** 某结果摘要含换行与连续空格且长度超过 300 字符
- **THEN** 摘要压缩为单空格并截断为前 300 字符加 `…`；标题超过 120 字符时同样截断加 `…`

#### Scenario: 摘要为空
- **WHEN** 某结果的摘要为空串
- **THEN** 该结果只输出标题行与链接行

### Requirement: WebSearch 错误与空结果文案

`WebSearch` SHALL 在以下情形返回固定文案：接口调用异常（网络、HTTP、JSON 解析等）→ `[错误: 搜索失败: {异常文本}]`；接口成功但解析结果为空 → `[无搜索结果]`。以上失败 SHALL 作为普通工具结果返回，不得作为进程异常向上传播。

#### Scenario: 接口异常
- **WHEN** 网络不可达或接口返回非 JSON 内容
- **THEN** 返回 `[错误: 搜索失败: {异常文本}]`

#### Scenario: 无结果
- **WHEN** 接口响应中没有可解析的结果块
- **THEN** 返回 `[无搜索结果]`

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（避免用户/AI 可感知差异），若未来修正须作为独立变更处理：① 未配置密钥返回 `[错误: 搜索失败]`（不带原因），与接口故障的 `[错误: 搜索失败: …]` 共用同一文案前缀，AI 无法直接区分二者；② 请求的地域参数固定为 `cn`，无配置入口；③ `num` 超上限被静默裁剪、超长标题/摘要被静默截断，均无任何提示；④ 接口返回结果多于 `num` 条时先对全部结果排序、再截取前 `num` 条展示（排序使用全部返回结果而非仅前 `num` 条）。

#### Scenario: 无密钥与接口故障文案相近（兼容怪癖）
- **WHEN** 未配置密钥调用 `WebSearch`
- **THEN** 返回文案为 `[错误: 搜索失败]`（不额外附原因，现状保持）

#### Scenario: 地域参数固定（兼容怪癖）
- **WHEN** 发起搜索
- **THEN** 请求参数中的地域字段恒为 `cn`（现状保持）
