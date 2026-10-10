"""工具上下文 —— 统一管理工具所需的回调和状态

替代各工具模块的模块级全局变量 + setter注入模式。
由Agent创建，通过registry传递给各工具。
"""

from dataclasses import MISSING, dataclass, field, fields
from typing import Optional, Callable, Any, Dict, List


# 删除确认标记：bash/terminal检测到删除命令时返回此值，由agent主循环拦截处理
AWAIT_CONFIRM = "__AWAIT_CONFIRM__"


@dataclass
class ToolContext:
    """工具运行时上下文"""

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

    # 取消检查回调（返回 True = 用户已中断）。由组装层/验证器按需注入：
    # 长耗时只读工具（Grep/Glob）据此在扫描中途提前返回；
    # 默认 None = 无取消语义，工具行为与注入前完全一致
    cancel_check: Optional[Callable[[], bool]] = None

    # MCP 服务器管理器（由 assembly 注入；MCP 工具用它做运行时连接/断开）
    mcp_manager: Any = None

    # 当前todo状态（由TodoWrite工具更新）
    current_todos: list = field(default_factory=list, metadata={"task_scoped": True})

    # 暂存的待确认命令（用户确认后由agent主循环按序逐条重放；每批处理完清空）
    # 格式: [(tool_name, arguments_dict), ...]
    pending_delete: list = field(default_factory=list)

    # 用户已确认删除，下次执行删除命令时跳过确认直接执行
    _delete_confirmed: bool = field(default=False, repr=False)

    # 目标模式完成标记：GoalComplete工具调用时置True，主循环据此停止自动续跑
    goal_complete: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 目标模式完成清单（GoalComplete 提交：[{"要求","证据","状态"}, ...]）
    goal_checklist: list = field(default_factory=list, metadata={"task_scoped": True})

    # 诚实收尾标记：清单含"未完成/受阻"项（允许收尾，用户会看到未完成项）
    goal_honest: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 目标模式预算内已消耗的回合数（agent.py 每轮结算时累加：
    # 本轮续跑消耗1 + 本轮验证打回次数k，合计 1+k）；机械拒绝不计入
    goal_rounds_used: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 连续机械拒绝计数（GoalComplete 机械校验拒绝路径累计；放行时清零）。
    # 与续跑预算无关，仅用于连续拒绝超限兜底放行防死循环
    goal_mech_rejects: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 强制放行标记：验证打回预算耗尽 / 连续机械拒绝超限
    goal_forced: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 验证存疑放行标记
    goal_suspect: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 收尾软提醒标志：任务收尾时计划未全部勾选会提醒一次，置True后放行不再提醒。
    # 每次用户新输入时由agent.py复位（与goal_complete一致）
    todo_reminded: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 迟到提醒标志：多轮工具后仍未建计划会温和提醒一次，置True后不再提醒。
    # 每次用户新输入时由agent.py复位（与todo_reminded一致）
    todo_nudge_sent: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 本任务累计工具轮数（迟到提醒的触发计数；每任务复位）
    tool_rounds_used: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 后台任务软提醒标志：结束回合时仍有running任务提醒一次，置True后放行不再提醒。
    # 每次用户新输入时由agent.py复位（与todo_reminded一致）
    bg_reminded: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    def reset_for_task(self) -> None:
        """复位全部任务级字段（task_scoped 标记驱动）。

        新增任务级字段只需在字段定义处加 metadata={"task_scoped": True}，
        无需再维护别处的手工复位清单。
        """
        for f in fields(self):
            if not f.metadata.get("task_scoped"):
                continue
            if f.default is not MISSING:
                setattr(self, f.name, f.default)
            elif f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())

    def on_todo_update(self, todos):
        """调用TodoWrite UI回调"""
        if self.ui_callback:
            self.ui_callback(todos)

    def get_api_key(self, name: str) -> str:
        """获取指定服务的API密钥"""
        return self.api_keys.get(name, "")
