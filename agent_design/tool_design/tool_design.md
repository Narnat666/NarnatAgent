# Agent 工具设计规范

## 设计理念

- **最小完备集**：7个工具覆盖所有开发场景，不按功能域分，按操作类型分
- **原子操作**：每个工具只做一件事，无内部状态，工具间不直接调用
- **LLM调度**：LLM是唯一调度者，决定调哪个工具+传什么参数
- **安全优先**：Read/Glob/Grep只读无风险，Edit/Write需确认覆写，Bash需确认删除命令

## 工具清单

### 1. Read

**用途**：读取文件内容，理解代码

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file_path | string | 是 | 文件绝对路径 |
| offset | int | 否 | 起始行号（0-based，0=第一行），省略则从头读 |
| limit | int | 否 | 最大行数，省略则读全文 |

**输出**：带行号的文件内容字符串，格式 `行号→内容`

**设计要点**：
- 默认读全文，offset/limit仅用于大文件分段
- 超大文件(>2000行)自动截断并提示用offset/limit分段读
- 单行超过2000字符自动截断
- 文件不存在返回错误文本，不抛异常

**实现算法**：
```
1. 校验path存在且为文件
2. 若文件>2000行且无offset/limit，读前2000行+提示分段
3. 按offset/limit读取指定行
4. 每行前缀行号，格式 "  行号→内容"
5. 返回拼接后的字符串
```

---

### 2. Glob

**用途**：按文件名模式搜索文件

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| pattern | string | 是 | glob模式，如 `**/*.py`、`src/**/*.ts` |
| path | string | 否 | 搜索根目录，省略为当前工作目录 |

**输出**：匹配的文件路径列表，每行一个，按修改时间排序

**设计要点**：
- 支持标准glob语法：`*` `**` `?` `[...]`
- `**` 递归匹配子目录
- 结果按修改时间倒序（最近改的排前面）
- 无匹配返回空列表文本

**实现算法**：
```
1. 以path为根，递归遍历目录
2. 对每个文件路径匹配pattern（fnmatch或pathlib.PurePath.match）
3. 跳过.git/__pycache__/node_modules等忽略目录
4. 按mtime倒序排序
5. 每行输出一个路径
```

---

### 3. Grep

**用途**：按内容搜索代码，定位关键行

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| pattern | string | 是 | 正则表达式 |
| path | string | 否 | 搜索目录，省略为当前工作目录 |
| glob | string | 否 | 限定文件类型，如 `*.py` |
| output_mode | string | 否 | `files_with_matches`(默认)/`content`(含行号)/`count` |
| i | bool | 否 | 忽略大小写 |
| n | bool | 否 | 显示行号（output_mode=content时有效） |
| multiline | bool | 否 | 多行匹配模式 |
| A | int | 否 | 匹配行后显示N行上下文 |
| B | int | 否 | 匹配行前显示N行上下文 |
| C | int | 否 | 匹配行前后各显示N行上下文 |
| head_limit | int | 否 | 限制输出前N条匹配结果 |

**输出**：
- `files_with_matches`：匹配的文件路径列表
- `content`：`文件路径:行号:匹配行内容`
- `count`：`文件路径:匹配数`

**设计要点**：
- pattern必须是合法正则，非法正则返回错误提示
- 默认只返回文件列表（快速定位），需要看内容时用content模式
- 跳过.git/__pycache__/node_modules等忽略目录
- content模式单文件最多显示匹配的前N行，避免输出爆炸

**实现算法**：
```
1. 基于ripgrep(rg)引擎执行搜索，非Python逐行匹配
2. 编译pattern为正则对象（失败则返回错误）
3. 遍历path下所有文件（受glob过滤，自动跳过.git等忽略目录）
4. 按output_mode格式化输出
5. content模式单文件截断前N个匹配，避免输出爆炸
```

---

### 4. Edit

**用途**：精确修改文件内容，最核心的写操作工具

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file_path | string | 是 | 文件路径 |
| old_string | string | 是 | 要替换的原文（必须精确匹配，含缩进） |
| new_string | string | 是 | 替换后的新文 |
| replace_all | bool | 否 | 替换所有匹配（默认只替换第一个） |

**输出**：确认信息 + unified diff

**设计要点**：
- **old_string必须精确匹配**：包括缩进、空格、换行，模糊匹配是万恶之源
- old_string在文件中不唯一且未设replace_all → 报错，要求扩大上下文使其唯一
- old_string未找到 → 返回错误+相似行提示，引导LLM先Read确认
- 文件不存在 → 报错，应用Write创建
- 空old_string不允许

**实现算法**：
```
1. 读取path全文
2. 在全文中查找old_string
   - 未找到：计算编辑距离，返回最相似的行作为提示
   - 找到多个且无replace_all：报错"不唯一，请扩大上下文"
   - 找到1个或replace_all=True：执行替换
3. 替换后写回文件
4. 生成unified diff（替换前→替换后）
5. 返回确认信息+diff
```

**关键**：Edit的可靠性完全取决于old_string的精确性。LLM必须先Read确认内容再Edit，这是调度逻辑的铁律。

---

### 5. Write

**用途**：创建新文件或完整覆写文件

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file_path | string | 是 | 文件路径 |
| content | string | 是 | 完整文件内容 |

**输出**：确认信息 + 写入字节数

**设计要点**：
- 文件已存在时**完整覆写**，旧内容丢失
- 覆写已有文件前必须先Read确认当前内容（CodeArts硬性要求，未Read则Write报错）
- 修改已有文件应优先用Edit，Write只用于新建或需要全量重写的场景
- 自动创建父目录（os.makedirs）
- content为空字符串是合法的（创建空文件）

**实现算法**：
```
1. os.makedirs创建父目录（exist_ok=True）
2. 以utf-8写入content
3. 返回确认+字节数
```

---

### 6. Bash

**用途**：执行shell命令，万能执行器

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| command | string | 是 | shell命令 |
| description | string | 否 | 命令描述（5-10字，便于日志） |
| timeout | int | 否 | 超时毫秒数，默认120000，最大600000 |
| run_in_background | bool | 否 | 后台运行，返回后可用BashOutput读取输出 |
| dangerouslyDisableSandbox | bool | 否 | 禁用沙箱（仅特殊场景） |

**输出**：stdout + stderr + 退出码

**设计要点**：
- **禁止交互式命令**：vim/top/less等会阻塞
- **删除命令需确认**：rm/del/Remove-Item等，通过权限回调拦截
- 超时后杀进程，返回已输出的部分+超时提示
- 工作目录为当前项目根目录
- 长输出(>30KB)截断，提示用文件重定向

**实现算法**：
```
1. 检测删除命令（正则匹配rm/del等），触发权限确认
2. 根据平台选择Shell：Windows→PowerShell，Linux/macOS→bash
3. 启动命令，stdout/stderr用PIPE捕获，设置timeout
4. 等待进程结束或超时
5. 超时则process.kill()，返回已输出部分
6. 格式化输出：stdout + [stderr] + [exit_code]
7. 输出>30KB时截断+提示
```

---

### 7. WebSearch

**用途**：搜索互联网，查API文档/解决方案/技术文章

**说明**：CodeArts当前无此工具，需自行实现。建议加入，因为Bash curl搜网页质量远不如专用搜索API。

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| query | string | 是 | 搜索关键词 |
| max_results | int | 否 | 最大结果数，默认10 |

**输出**：搜索结果列表，每条格式：`序号. 标题\n   摘要\n   链接`

**设计要点**：
- 不用于搜索本地代码（那是Grep的活）
- 搜索引擎降级链：百度 → Bing → DuckDuckGo
- 结果只返回标题+摘要+链接，不自动抓取网页内容
- LLM根据摘要判断是否需要进一步阅读

**实现算法**：
```
1. 尝试百度搜索（requests.get）
2. 百度失败则尝试Bing
3. Bing失败则尝试DuckDuckGo（duckduckgo-search库）
4. 解析搜索结果，提取title+snippet+url
5. 格式化输出，最多max_results条
```

---

## 工具调用铁律

1. **Edit前必须Read** — 确认old_string精确匹配，禁止凭记忆猜测
2. **改一处验一处** — 不批量改多处再验证，改完立即验证
3. **优先Edit而非Write** — 修改已有文件用Edit，新建文件用Write
4. **Bash仅用于执行** — 文件操作用Read/Edit/Write/Grep，不用Bash
5. **Grep定位→Read确认→Edit修改** — 标准三步流程

## 风险分级

| 级别 | 工具 | 行为 |
|---|---|---|
| READONLY | Read, Glob, Grep, WebSearch | 无风险，直接执行 |
| WRITE | Edit, Write | 覆写风险，记录diff |
| DESTRUCTIVE | Bash | 删除命令需用户确认 |

---

## 工具搭配范式：7个工具完成所有项目开发

### 范式1：修Bug

```
Grep(pattern="报错关键词")          → 定位出错文件
Read(path=定位到的文件)             → 理解上下文
Edit(old_string=错误代码, new_string=修复代码) → 修改
Bash(command="pytest tests/")      → 验证修复
```

### 范式2：加新功能

```
Glob(pattern="**/*.py")            → 了解项目结构
Read(path=入口文件)                → 理解架构
Grep(pattern="相关接口/类名")       → 找到插入点
Read(path=插入点文件)              → 确认插入位置
Edit(old_string=插入点, new_string=新功能代码) → 添加功能
Write(path=新模块文件, content=...) → 创建新模块
Bash(command="运行测试")           → 验证
```

### 范式3：重构代码

```
Grep(pattern="要重构的函数名", output_mode="content") → 找所有调用点
Read(path=每个调用文件)            → 理解每个调用上下文
Edit(逐个修改调用点)               → 改调用方
Read(path=定义文件)                → 确认原定义
Edit(修改定义)                     → 改定义方
Bash(command="运行测试")           → 验证重构正确
```

### 范式4：从零建项目

```
Bash(command="mkdir -p src/tests") → 创建目录结构
Write(path=src/main.py, ...)       → 写入口
Write(path=src/config.py, ...)     → 写配置
Write(path=requirements.txt, ...)  → 写依赖
Bash(command="pip install -r requirements.txt") → 安装依赖
Bash(command="python -m pytest")   → 跑测试
```

### 范式5：接入第三方API

```
WebSearch(query="xxx API 文档")    → 查API用法
Read(path=现有代码)                → 理解当前架构
Edit(添加API调用代码)              → 接入API
Bash(command="测试API调用")        → 验证连通性
```

### 范式6：性能优化

```
Bash(command="python -m cProfile script.py") → 性能分析
Grep(pattern="热点函数名", output_mode="content") → 找到瓶颈代码
Read(path=瓶颈文件)                → 理解算法
Edit(优化算法)                     → 改实现
Bash(command="性能对比测试")       → 验证提升
```

### 范式7：代码审查/安全审计

```
Grep(pattern="eval\(|exec\(|os\.system\(", glob="*.py") → 找危险调用
Grep(pattern="SELECT.*FROM", glob="*.py", i=True)       → 找SQL注入风险
Grep(pattern="password|secret|token", i=True)           → 找硬编码密钥
Read(path=每个风险文件)            → 确认是否真有问题
Edit(修复安全问题)                 → 修复
```

### 范式8：跨项目迁移/适配

```
Glob(pattern="**/*.py")            → 源项目结构
Grep(pattern="核心接口", output_mode="content") → 找核心逻辑
Read(path=核心文件)                → 理解逻辑
Write(path=目标项目文件, ...)       → 写入目标项目
Edit(适配目标项目依赖)             → 改import/配置
Bash(command="目标项目测试")       → 验证兼容
```

### 核心规律

所有范式都是以下3种基本流程的组合：

| 基本流程 | 工具链 | 适用场景 |
|---|---|---|
| **定位→理解→修改→验证** | Grep→Read→Edit→Bash | 修bug/重构/优化 |
| **搜索→学习→创建→验证** | WebSearch→Read→Write→Bash | 新功能/接入API/建项目 |
| **扫描→确认→修复** | Grep→Read→Edit | 安全审计/批量修复 |

**本质**：7个工具的排列组合能覆盖所有开发任务，因为开发只有4件事——**读、写、搜、执行**。
