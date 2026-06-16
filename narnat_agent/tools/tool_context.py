"""工具上下文 —— 统一管理工具所需的回调和状态

替代各工具模块的模块级全局变量 + setter注入模式。
由Agent创建，通过registry传递给各工具。
"""

from dataclasses import dataclass, field
from typing import Optional, Callable, Any, Dict


@dataclass
class ToolContext:
    """工具运行时上下文"""

    # 删除确认回调（bash/terminal使用）
    confirm_callback: Optional[Callable[[str], bool]] = None

    # TodoWrite UI更新回调
    ui_callback: Optional[Callable[[Any], None]] = None

    # API密钥组（web_search使用）
    api_keys: Dict[str, str] = field(default_factory=dict)

    # 已Read过的本地文件集合（write使用）
    read_files: set = field(default_factory=set)

    # 已Read过的远程文件集合（remote使用）
    read_remote_files: set = field(default_factory=set)

    def confirm_delete(self, command: str) -> bool:
        """调用删除确认回调"""
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
