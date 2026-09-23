"""sessions 积木 —— 会话域：三态状态机、持久化、探索分支与交互命令。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`
（17 Requirement：会话三态与命令可用性 / 保存会话 / 进入历史会话 / 会话列表 /
删除标记 / 退出语义 / 手动压缩的会话侧收尾 / 会话文件布局与原子写 / 探索分支
创建与合并 / 技能加载 / 思考与模型配置命令 / 目标模式命令 / 会话名解析 /
延迟删除执行与展示标记 / 自动保存与退出保存 / 兼容性怪癖保持）。

积木结构（design D1：跨积木只经显式接口）：
- `store.SessionStore`：会话文件布局与持久化（原子写、代理字符清洗、树形/摘要
  文本格式化；文本输出对齐 `v2/tests/baseline/data/session_store.json` 基准）；
- `state.NoSession` / `state.RootSession` / `state.ChildSession`：三态状态机
  （可用命令集、状态迁移、增量合并基准）；
- `commands.SessionCommands`：命令层（命令表、全部命令实现、三态分发结果）；
- `manager.SessionManager`：服务层（共享资源、状态切换、名称解析、延迟删除、
  自动保存与退出清理、目标模式与配置句柄的宿主）；
- `goal.GoalMode`：目标模式状态（conversation / app 消费的公开 API）；
- `model_state.ModelState`：思考/模型配置句柄（写回 narnat.json、同步 stats）；
- `theme.ThemePort` / `theme.PlainColors`：命令输出着色端口与无色替身。

依赖规则（design D1）：本积木位于 L3，只依赖 contracts 与下层积木
（config / messages / output / compression 经注入的端口）；`compression` 的手动
压缩经构造注入（`compact_func`），不留后置补线。
"""
from __future__ import annotations

from .commands import CommandKind, CommandReply, SessionCommands
from .goal import GoalMode
from .manager import AUTO_SAVE_WAIT_SECONDS, SessionManager
from .model_state import ModelState
from .state import (
    BOUNDARY_MARKER_PREFIX,
    SUMMARY_TASK_TEMPLATE,
    ChildSession,
    NoSession,
    RootSession,
    SessionState,
)
from .store import (
    SessionStore,
    clean_surrogates,
    format_session_list,
    format_session_summary,
    format_session_tree,
    safe_filename,
)
from .theme import PlainColors, ThemePort

__all__ = [
    # 子模块级聚合对象
    "SessionManager",
    "SessionCommands",
    "SessionStore",
    # 状态机
    "SessionState",
    "NoSession",
    "RootSession",
    "ChildSession",
    "BOUNDARY_MARKER_PREFIX",
    "SUMMARY_TASK_TEMPLATE",
    # 命令层
    "CommandKind",
    "CommandReply",
    # 目标模式与配置句柄
    "GoalMode",
    "ModelState",
    # 着色端口
    "PlainColors",
    "ThemePort",
    # 持久化与格式化
    "AUTO_SAVE_WAIT_SECONDS",
    "clean_surrogates",
    "format_session_list",
    "format_session_summary",
    "format_session_tree",
    "safe_filename",
]
