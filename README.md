# Narnat Agent

Linux 风格的终端 AI 代码智能体。极简，只保留最核心的功能。

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

### 2. 配置

首次运行会在当前目录生成 `.narnat/config/narnat.json`，编辑填入 API 密钥：

```json
{
  "接口密钥": "sk-xxxxxxxx",
  "接口地址": "https://api.deepseek.com/anthropic",
  "模型": "deepseek-v4-flash"
}
```

> **接口地址** 自动识别协议——含 `anthropic` 走 Anthropic 端点，否则走 OpenAI 兼容端点。支持 DeepSeek、OpenAI、Claude 及任意兼容 API。

### 3. 开始对话

启动后进入 `#` 提示符，输入问题即可：

![启动](img/登录界面.png)

AI 流式输出回答，代码块含语法高亮，末尾显示 token 消耗：

![对话](img/思考界面.png) ![回答](img/回答界面.png)

AI 会按需自主调用工具——读文件、改代码、执行命令、搜索网络。多工具可并行执行，结果回传后继续推理：

![工具调度](img/ai工具调度.png)

## 交互命令

在 `#` 提示符下输入 `/` 开头的命令，支持 Tab 补全：

| 命令 | 说明 |
|------|------|
| `/exit` | 退出 |
| `/save` | 保存当前会话 |
| `/show` | 列出所有会话 |
| `/enter <名称>` | 恢复历史会话 |
| `/delete <名称>` | 删除会话 |
| `/clear` | 清屏 |
| `/skill <名称>` | 加载技能 |
| `/thinking high\|max` | 切换思考强度 |
| `Esc` | 中断当前输出 |

![交互命令](img/工具使用效果.png) ![命令补全](img/支持的工具.png)

## 命令行参数

```
narnat -h         查看帮助
narnat -v         显示版本号
narnat -d         调试模式（记录详细日志到 .narnat/logs/）
```

## 全部配置项

`.narnat/config/narnat.json`，首次运行自动生成：

```json
{
  // ── AI 连接 ──
  "接口密钥": "sk-xxxxxxxx",
  "接口地址": "https://api.deepseek.com/anthropic",
  "模型": "deepseek-v4-flash",
  "思考强度": "high",
  "思考模式": { "high": "高", "max": "全开" },
  "温度": null,
  "最大输出token数": null,

  // ── 独立 API 密钥（WebSearch 等）──
  "接口密钥组": {
    "websearch": "",
    "websearch_url": "https://api.anysearch.com/mcp"
  },

  // ── 费用与余额显示 ──
  "显示费用": false,
  "显示余额": false,
  "余额查询地址": "",
  "定价": {
    "deepseek-v4-flash": { "输入": 1.0, "缓存命中": 0.25, "输出": 4.0 }
  },

  // ── 安全 ──
  "git免确认": false,
  "rm免确认": false,
  "计划优先": false,
  "计划最低工具数": 2,

  // ── 上下文压缩 ──
  "压缩轮次": 100,
  "警告轮次1": 50,
  "警告轮次2": 80,

  // ── 高级 ──
  "SSH最大会话数": 5,
  "LLM重试次数": 3,
  "忽略目录": [".git", "__pycache__", "node_modules", ".svn", ".hg", "venv"],

  // ── 界面配色（11 色可覆盖）──
  "用户输入色": "#FFFFFF",
  "AI输出色": "#D8DEE9",
  "标题色": "#5EEAD4",
  "成功色": "#A3BE8C",
  "行内代码色": "#EBCB8B",
  "错误色": "#BF616A",
  "链接色": "#81A1C1",
  "装饰色": "#B48EAD",
  "加载动画色": "#D08770",
  "次要文字色": "#4C566A",
  "代码块背景色": "#161821"
}
```

> 在 `.narnat/config/narnat.md` 中写入 Markdown，会作为自定义系统指令追加到 prompt 末尾。

## 项目结构

```
NarnatAgent/
├── main.py                 # 入口
├── narnat_agent/
│   ├── core/               # Agent 主循环、LLM 双协议、上下文压缩
│   ├── tools/              # 9 个工具（read/glob/grep/edit/write/bash/terminal/web_search/todo_write）
│   ├── ui/                 # prompt-toolkit 终端界面、流式 Markdown 渲染
│   ├── config/             # 配置加载
│   ├── output.py           # 终端输出/颜色控制
│   └── logger.py           # 日志
└── output/                 # 编译产物
```
