"""
默认配置常量 —— 压缩prompt模板、阈值等
"""

# ── 安全确认默认值 ──
DEFAULT_GIT_SKIP = False   # git 命令默认不免确认（即需要二次确认）
DEFAULT_RM_SKIP = False     # rm 命令默认不免确认（即需要二次确认）

# ── 上下文压缩阈值 ──
WARN_TURN_1 = 50    # 提示对话已50轮
WARN_TURN_2 = 80   # 提示已80轮，建议开新对话
COMPRESS_TURN = 100  # 强制压缩

# ── 基础Prompt模板 ──
BASE_PROMPT_TEMPLATE = """\
| 身份 | {model} |
| 环境 | narnat agent |
| 任务 | 解难 |
| 工作目录 | {cwd} |
| 平台 | {platform} |
"""

# ── 压缩Prompt模板 ──
COMPRESS_PROMPT = """直接输出本轮对话核心经验总结，作为下一新会话的基础。必须包含：

1. 用户原始请求与当前目标
2. 已完成工作及结果（涉及文件、执行命令、实际产出）
3. 未完成任务及下一步计划
4. 关键决策及原因
5. 核心技术细节（文件路径、关键代码段）
6. 遇到的错误及解决方案"""

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
