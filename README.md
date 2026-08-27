# Narnat Agent

终端 AI 代码智能体。自主调用工具（读文件、改代码、执行命令、SSH/串口、联网搜索），支持会话探索分支、目标模式自动续跑、多模型热切换。

## 快速上手

### 1. 编译

从源码编译为单文件二进制。

**Windows**

| 组件 | 版本 |
|------|------|
| Nuitka | 4.1.2 |
| Python | 3.12.9 |
| C 编译器 | MSVC cl 14.3 |

```bash
pip install nuitka==4.1.2 httpx openai paramiko prompt_toolkit zstandard
python -m nuitka --onefile --output-dir=output --output-filename=narnat.exe \
  --jobs=16 --lto=yes --python-flag=no_docstrings --follow-imports \
  --include-module=openai \
  --nofollow-import-to=tkinter --nofollow-import-to=unittest --nofollow-import-to=unittest.mock \
  --nofollow-import-to=invoke --nofollow-import-to=test --nofollow-import-to=tests \
  --nofollow-import-to=setuptools --nofollow-import-to=pip --nofollow-import-to=distutils \
  main.py
```

产物 `output/narnat.exe`，约 30MB。

**Ubuntu**

| 组件 | 版本 |
|------|------|
| Nuitka | 4.1.2 |
| Python | 3.12.9 |
| C 编译器 | gcc 11.4.0 |

```bash
# 系统依赖
sudo apt install -y gcc patchelf build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev libgdbm-dev \
  liblzma-dev tk-dev libffi-dev

# Python 3.12.9（源码编译，不覆盖系统版本）
cd /tmp
wget https://npmmirror.com/mirrors/python/3.12.9/Python-3.12.9.tgz
tar xzf Python-3.12.9.tgz && cd Python-3.12.9
./configure --prefix=/usr/local/python3.12
make -j$(nproc) && sudo make install

# Nuitka + 依赖
/usr/local/python3.12/bin/pip3.12 install nuitka==4.1.2 httpx openai paramiko prompt_toolkit zstandard
```

编译命令与 Windows 相同（将 `python` 替换为 `/usr/local/python3.12/bin/python3.12`），耗时约 28 分钟。产物约 35MB，仅依赖 glibc ≥ 2.35。

## 界面预览

![启动](img/登录界面.png)

![思考](img/思考界面.png) ![回答](img/回答界面.png)

AI 按需自主调用工具——读文件、改代码、执行命令、搜索网络，多工具可并行执行：

![工具调度](img/ai工具调度.png)

交互命令与 Tab 补全：

![交互命令](img/工具使用效果.png) ![命令补全](img/支持的工具.png)

## 配置

所有配置位于项目根目录 `.narnat/`，首次运行自动生成：

```
.narnat/
├── config/
│   ├── narnat.json   # 主配置（唯一需要编辑的文件）
│   ├── narnat.md     # 自定义系统指令（追加到系统 prompt 末尾）
│   └── skills/       # 技能文件（/skill 加载）
├── data/             # 会话持久化数据
└── logs/             # 调试日志（-d 模式）
```

> 环境变量 `NARNAT_HOME` 可指定 `.narnat` 所在目录，优先级最高；否则从当前目录向上查找，编译版取 exe 所在目录。

### narnat.json

完整参考。注释标注了默认值；「可选」表示首次生成不含该 key，写进去即生效：

```jsonc
{
  // ── 模型连接 ──
  "智能体": {
    "接口密钥": "sk-xxxxxxxx",
    "接口地址": "https://api.deepseek.com/anthropic",
    "模型": {
      "当前": "deepseek-v4-flash",              // 当前使用模型
      "列表": ["deepseek-v4-flash"]            // /mode 可切换的候选模型
    },
    "协议": "anthropic",                        // "anthropic" | "openai"
    "温度": null,
    "最大输出token数": 128000,
    "上下文窗口大小": 1000000,                   // 模型上下文窗口 token 数，≤0 视为无效（占比显示 --）
    "目标模式最大轮数": 100,                     // /goal 开启后单个任务自动续跑轮数上限
    "思考": {
      "启用": true,                             // 关闭则不传 thinking 参数
      "强度": "high",                           // 当前生效值
      "强度选项": { "high": "高", "max": "全开" }  // /thinking 可选值 → 显示名
    },
    "LLM重试次数": 3
  },

  // ── 余额查询（独立分组，支持 DeepSeek / Kimi / GLM 等）──
  "余额查询": {
    "启用": true,
    "查询地址": "https://api.deepseek.com/user/balance",
    "认证方式": "bearer",                        // "bearer" | "x-api-key"
    "响应路径": "balance_infos.0.total_balance",
    "货币路径": "balance_infos.0.currency"
  },

  // ── 独立 API 密钥（WebSearch 等）──
  "接口密钥组": {
    "websearch": "",
    "websearch_url": "https://api.anysearch.com/mcp"
  },

  // ── 定价（用于费用统计，键为模型名）──
  "定价": {
    "模型": {
      "deepseek-v4-pro":   { "输入": 3.0, "缓存命中": 0.025, "输出": 6.0 },
      "deepseek-v4-flash": { "输入": 1.0, "缓存命中": 0.02,  "输出": 2.0 }
    }
  },

  // ── 界面（详见下方「界面配色」）──
  "界面": {
    "显示费用": false,                          // 或英文 "show_cost"
    "显示余额": false,
    "最大输出token数": 128000,
    "颜色": { "蓝": "#6C9FFF", "月光白": "#E0E4EA", "...": "自定义色板" },
    "基础色": { "主色": "月光白", "强调色": "蓝", "...": "引用上方色名" },
    "标注": { "标题1": "bold 蓝", "行内代码": "黄", "...": "Markdown 元素样式" },
    "代码块": { "背景": "卡片背景", "行号": "次文字", "...": "代码块元素样式" },
    "差异": { "添加": "绿", "删除": "红", "...": "diff 元素样式" },
    "框架": { "标题": "蓝", "加载动画": "橙", "...": "界面框架元素样式" },
    "命令": { "成功": "绿", "错误": "红", "...": "命令反馈样式" },
    "提示符": { "符号": "bold 绿", "文字": "纯白" }
  },

  // ── 工具 ──
  "工具": {
    "输出上限KB": 64,                           // 工具输出全局硬截断（保留首尾），0=不限制
    "超时上限秒": 1800,                         // 工具执行超时上限，0=不限制
    "SSH最大会话数": 5,                         // 可选，默认 5
    "最大传输文件MB": 100,                      // 可选，默认 100
    "git免确认": false,                         // 可选，默认 false（git 命令需二次确认）
    "rm免确认": false                           // 可选，默认 false（rm 命令需二次确认）
  },

  // ── 会话 ──
  "会话": {
    "自动保存": false,                          // 可选，默认 false；开启后按下方门槛自动保存
    "自动保存Token量": 0                        // 服务器输入 token > 此值才自动保存；支持 "10k"；0=无门槛
  },

  // ── 上下文压缩（按上下文窗口占比触发）──
  "压缩": {
    "占比显示": false,                          // 统计栏是否显示 窗口占比:x%
    "告警": 50,                                 // 窗口占比 ≥ 此百分比时提示一次
    "压缩": 95                                  // 窗口占比 ≥ 此百分比时先压缩再请求
  },

  // ── 计划优先 ──
  "计划": {
    "计划优先": false,                          // 可选，强制 AI 先制定计划再执行工具
    "计划最低工具数": 2                          // 可选，单轮工具调用数 ≥ 此值才强制先写计划
  },

  // ── 文件操作忽略目录（Glob/Grep/Read 跳过）──
  "忽略目录": [
    ".git", "__pycache__", "node_modules", ".svn", ".hg",
    "venv", ".venv", ".pytest_cache"
  ]
}
```

#### 界面配色

`"界面"` 采用「色板 + 角色引用」两级结构：`"颜色"` 定义具名色值，其余分组引用色名或十六进制色值，配方支持空格分隔组合（如 `"bold 蓝"`、`"italic dim 次文字"`）。分组名支持中文别名（标注/标记、差异/对比、框架）。旧版扁平格式（`"用户输入色"`、`"AI输出色"` 等）自动兼容转换。

#### thinking 参数自动适配

thinking 参数按 `(协议, 模型前缀)` 由内置映射表自动翻译为对应厂商格式（DeepSeek / GLM / Kimi / Qwen / GPT / Claude），无需手动写 `extra_body`。换模型只需改 `"智能体"` 分组字段，并通过 `/mode` 或 `"模型.列表"` 切换。

### narnat.md

`config/narnat.md` 的 Markdown 内容会作为自定义系统指令，追加到系统 prompt 末尾。首次运行生成空文件，直接编辑即可。

### 技能

`config/skills/` 下每个 Markdown 文件（或含 `.md` 的同名子目录）即一个技能。输入 `/skill <名称>` 将技能内容作为系统指令注入当前对话，用于切换 AI 的工作模式或行为风格。

## narnat_agent

### 项目结构

```
narnat_agent/
├── assembly.py               # 组装层：唯一对象构造点，注入依赖
├── config/                   # 配置
│   ├── defaults.py           #   默认常量、prompt 模板、thinking 参数映射表
│   ├── loader.py             #   narnat.json / narnat.md 加载与校验
│   ├── session_store.py      #   会话持久化与树形展示
│   └── skill_store.py        #   技能加载
├── core/                     # 核心
│   ├── agent.py              #   Agent 主循环、目标模式续跑
│   ├── agent_loop.py         #   单轮循环（请求 → 工具调度 → 回传）
│   ├── llm.py                #   LLM 双协议客户端（openai / anthropic）
│   ├── context.py            #   上下文窗口管理
│   ├── compressor.py         #   上下文压缩
│   ├── compression_coordinator.py  # 压缩协调（占比阈值触发）
│   ├── message_manager.py    #   消息管理
│   ├── session_callbacks.py  #   会话状态机（三态）与命令回调
│   ├── auto_save_manager.py  #   自动保存 / 自动命名
│   ├── tool_dispatcher.py    #   工具调度
│   ├── tool_callbacks.py     #   工具回调（安全确认、Todo 同步）
│   ├── summarizer.py         #   探索分支总结
│   ├── billing.py            #   费用 / 余额
│   ├── stats.py              #   统计
│   └── interrupt.py          #   打断机制
├── tools/                    # 内置工具（每个工具一个子目录）
│   ├── read/                 #   Read — 读取文件
│   ├── glob/                 #   Glob — 模式匹配（花括号展开）
│   ├── grep/                 #   Grep — 正则搜索
│   ├── edit/                 #   Edit — 字符串 / 行号替换编辑
│   ├── write/                #   Write — 创建 / 覆盖文件
│   ├── bash/                 #   Shell — 本地命令执行
│   ├── terminal/             #   Terminal — 多终端持久 SSH + 文件传输
│   ├── serial/               #   Serial — 多终端持久串口
│   ├── web_search/           #   WebSearch — 网页搜索
│   ├── todo_write/           #   TodoWrite — 任务列表
│   ├── goal_complete/        #   GoalComplete — 目标完成标记（目标模式动态注入）
│   ├── registry.py           #   工具注册表
│   ├── diff_utils.py         #   diff 生成
│   ├── param_utils.py        #   参数处理
│   └── tool_context.py       #   工具运行时上下文
├── ui/                       # prompt-toolkit 界面
│   ├── ui_design.py          #   界面与配色
│   ├── renderer.py           #   流式 Markdown 渲染（表格稳定渲染）
│   ├── session_commands.py   #   交互命令注册表
│   └── interrupt.py          #   Esc 打断
├── output.py                 # 终端输出 / 颜色控制
└── logger.py                 # 日志
```

### 内置工具

| 工具 | 说明 |
|------|------|
| Read | 读取纯文本文件（本地/远程设备），带行号，自动识别编码 |
| Glob | 按模式匹配文件，支持 `**` 递归与 `{}` 花括号展开 |
| Grep | 正则搜索文件内容，返回带行号的匹配行与每文件计数；path 支持多路径数组，匹配超 head_limit 自动降级为文件清单 |
| Edit | 字符串替换或行号替换编辑，自动保持编码与换行符 |
| Write | 创建新文件或全量覆盖 |
| Shell | 本地命令行执行（Windows cmd，支持超时/输出上限） |
| Terminal | 多终端持久 SSH（最多 5 个），支持交互输入、sudo 密码回填、设备间文件传输 |
| Serial | 多终端持久串口（最多 5 个），扫描/连接/交互 |
| WebSearch | 网页搜索 |
| TodoWrite | 任务列表管理（计划同步） |
| GoalComplete | 声明任务完成（仅 `/goal` 目标模式开启时注入给 AI） |

- 工具输出受「输出上限KB」全局硬截断（保留首尾），超时受「超时上限秒」约束
- 参数错误返回对 AI 友好的中文提示（未知参数 / 缺失参数直接列出有效参数名）
- 编辑类工具返回着色 diff，终端同步展示改动

### 交互命令

`#` 提示符下输入 `/` 开头命令，支持 Tab 补全；命令可用性随会话状态变化：

| 命令 | 说明 | 可用状态 |
|------|------|----------|
| `/save <名称>` | 保存当前会话（无名称自动命名） | 全部 |
| `/ls [--all]` | 列出会话。默认精简（今天全部 + 更早最多 3 个父会话）；`--all` 树形展开全部 | 全部 |
| `/cd <名称>` | 进入历史会话 | 全部 |
| `/rm <名称 \| --all>` | 删除会话（退出时生效） | 全部 |
| `/explore <名称>` | 从当前会话创建探索分支 | RootSession |
| `/done` | 完成分支探索，AI 总结后合并回父会话 | ChildSession |
| `/skill <名称>` | 加载技能文件 | 全部 |
| `/thinking <强度>` | 切换思考强度（由 `思考.强度选项` 定义） | 全部 |
| `/mode <模型>` | 切换模型（由 `模型.列表` 定义，支持 Tab 补全） | 全部 |
| `/goal on [N]` | 开启目标模式（N=临时轮数上限） | 全部 |
| `/goal off` | 关闭目标模式 | 全部 |
| `/goal` | 查看目标模式状态 | 全部 |
| `/clear` | 清屏 | 全部 |
| `/exit` | 退出会话 / 退出程序 | 全部 |
| `Esc` | 中断当前 AI 输出 | 全部 |

### 会话模型

三态会话状态机，支持探索分支——从任意会话分叉出子分支，在不影响主线的条件下验证想法，完成后由 AI 自动总结合并：

```
NoSession ──/save──▶ RootSession ──/explore──▶ ChildSession
    ▲                    ▲                         │
    │                    │◀────── /done ───────────┘
    │◀─── /exit ─────────┘
```

- **RootSession**：常规工作会话，`/save` 持久化后可通过 `/cd` 随时恢复
- **ChildSession**：探索分支，继承父会话全部上下文，`/done` 时 AI 将分支讨论总结为结构化结论追加到父会话末尾；`/exit` 暂离可稍后 `/cd` 回来继续
- 子分支通过 `父名/子名` 路径引用，`/ls` 以树形展示

### 目标模式

`/goal on` 开启后，AI 完成任务时调用 GoalComplete 工具声明完成；若一轮对话结束仍未声明完成，则自动以「续跑提示」发起下一轮，直至 AI 声明完成或达到轮数上限（`/goal on N` 临时覆盖 > `智能体.目标模式最大轮数` 默认 100）。达到上限后注入收尾指令，由 AI 总结当前进度后结束。普通模式下 GoalComplete 不暴露给 AI。

### 上下文压缩

按上下文窗口占比（token 数 / `智能体.上下文窗口大小`）触发：占比 ≥ `压缩.告警` 时提示一次，≥ `压缩.压缩` 时先压缩历史再发起请求；`压缩.占比显示` 开启后统计栏实时显示窗口占比。压缩由 AI 将历史对话总结为结构化经验（请求、进展、未完成任务、关键决策、错误与解法），替代原文进入新会话。
