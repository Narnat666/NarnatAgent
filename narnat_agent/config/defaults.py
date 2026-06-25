"""
默认配置常量 —— 压缩prompt模板、阈值等
"""

# ── 上下文压缩阈值 ──
WARN_TURN_1 = 50    # 提示对话已50轮
WARN_TURN_2 = 80   # 提示已80轮，建议开新对话
COMPRESS_TURN = 100  # 强制压缩

# ── 基础Prompt模板 ──
BASE_PROMPT_TEMPLATE = """你是 {model}，一个协助软件工程任务的代码智能体。

# 环境

- 工作目录：{cwd} | 平台：{platform} | Shell：{shell}
- 路径均相对工作目录，工具返回相对路径直接使用，禁止编造绝对路径。"""

# ── 压缩Prompt模板 ──
COMPRESS_PROMPT = """Please create a comprehensive summary of this conversation that captures all essential experience and outcomes. The summary MUST include:

1. User's original request and ongoing goals
2. All completed work and their outcomes (files modified, commands executed, results obtained)
3. Unfinished tasks and next steps
4. Key decisions made and their reasoning
5. Important file paths, code snippets, and technical details referenced
6. Any errors encountered and their resolutions

This summary will serve as the foundation for the next session, enabling it to build upon all experience gained in this conversation."""

# ── .narnat 目录名 ──
NARNAT_DIR = ".narnat"

# ── .narnat 内部子目录 ──
CONFIG_SUBDIR = "config"       # 配置层：静态、用户可编辑
DATA_SUBDIR = "data"           # 数据层：运行时持久化
LOGS_SUBDIR = "logs"           # 日志层：可清理

# ── 配置文件名（相对于 config/ 子目录） ──
NARNAT_JSON = "narnat.json"
NARNAT_MD = "narnat.md"

# ── 数据文件名（相对于 data/ 子目录） ──
LAST_SESSION_SUMMARY = "last_session_summary.md"

# ── 默认AI配置 ──
DEFAULT_API_KEY = ""
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-v4-flash"
