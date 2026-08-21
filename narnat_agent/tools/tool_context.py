"""工具上下文 —— 统一管理工具所需的回调和状态

替代各工具模块的模块级全局变量 + setter注入模式。
由Agent创建，通过registry传递给各工具。
"""

from dataclasses import dataclass, field
from typing import Optional, Callable, Any, Dict, List, Tuple


# 删除确认标记：bash/terminal检测到删除命令时返回此值，由agent主循环拦截处理
AWAIT_CONFIRM = "__AWAIT_CONFIRM__"


@dataclass
class ToolContext:
    """工具运行时上下文"""

    # 删除确认回调（仅Windows使用，Linux/macOS通过AWAIT_CONFIRM机制处理）
    confirm_callback: Optional[Callable[[str], bool]] = None

    # TodoWrite UI更新回调
    ui_callback: Optional[Callable[[Any], None]] = None

    # API密钥组（web_search使用）
    api_keys: Dict[str, str] = field(default_factory=dict)

    # 忽略目录（glob/grep使用）
    ignore_dirs: List[str] = field(default_factory=list)

    # 安全确认开关（由配置驱动）
    git_skip_confirm: bool = False   # True=git命令免确认直接执行
    rm_skip_confirm: bool = False    # True=rm命令免确认直接执行

    max_transfer_mb: int = 100       # 文件传输大小上限(MB)，0=不限制

    # 工具输出全局硬上限（字符数），0=不限制。由配置"工具输出上限KB"驱动
    max_tool_output_chars: int = 65536

    # 工具超时全局上限（秒），0=不限制。由配置"工具超时上限秒"驱动
    max_timeout_seconds: int = 1800

    # 计划优先开关（由配置驱动）
    require_plan: bool = False       # True=强制AI先写TodoWrite再执行其他工具
    min_tools: int = 2               # 单轮工具调用数≥此值时才强制要求先写计划

    # 当前todo状态（由TodoWrite工具更新）
    current_todos: list = field(default_factory=list)

    # 已Read过的本地文件集合（write使用）
    read_files: set = field(default_factory=set)

    # 已Read过的远程文件集合（remote使用）
    read_remote_files: set = field(default_factory=set)

    # 暂存的删除命令（Linux/macOS下，用户确认后由agent主循环重新执行）
    # 格式: (tool_name, arguments_dict) 或 None
    pending_delete: Optional[Tuple[str, dict]] = None

    # 用户已确认删除，下次执行删除命令时跳过确认直接执行
    _delete_confirmed: bool = field(default=False, repr=False)

    # 目标模式完成标记：GoalComplete工具调用时置True，主循环据此停止自动续跑
    goal_complete: bool = field(default=False, repr=False)

    def confirm_delete(self, command: str) -> bool:
        """调用删除确认回调（仅Windows使用）"""
        if self.confirm_callback:
            return self.confirm_callback(command)
        return False

    def on_todo_update(self, todos):
        """调用TodoWrite UI回调"""
        if self.ui_callback:
            self.ui_callback(todos)

    def get_api_key(self, name: str) -> str:
        """获取指定服务的API密钥"""
        return self.api_keys.get(name, "")

    def mark_read(self, file_path: str):
        """标记本地文件已被Read"""
        import os
        self.read_files.add(os.path.abspath(file_path))

    def is_read(self, file_path: str) -> bool:
        """检查本地文件是否已被Read"""
        import os
        return os.path.abspath(file_path) in self.read_files

    def clear_read_files(self):
        """清空已读文件记录"""
        self.read_files.clear()

    def mark_remote_read(self, file_path: str, host: str = ""):
        """标记远程文件已被Read"""
        key = f"{host}:{file_path}" if host else file_path
        self.read_remote_files.add(key)

    def is_remote_read(self, file_path: str, host: str = "") -> bool:
        """检查远程文件是否已被Read"""
        key = f"{host}:{file_path}" if host else file_path
        return key in self.read_remote_files
