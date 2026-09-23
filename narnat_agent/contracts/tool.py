"""工具协议与运行时状态协议 —— 工具实现面、只读配置面与状态归主的协议定义。

契约来源：
- `openspec/changes/recast-v2/specs/tools-shell/spec.md`（参数契约、安全确认门禁、
  非 Windows 挂起确认、退出码与错误标签协议）；
- `openspec/changes/recast-v2/specs/tools-remote/spec.md`（删除命令安全确认、
  文件传输上限）；
- `openspec/changes/recast-v2/specs/tools-file/spec.md`（工具运行时上下文数据语义）；
- `openspec/changes/recast-v2/specs/tools-todo/spec.md`（计划状态、目标完成标记、
  软提醒标志）；
- `openspec/changes/recast-v2/specs/mcp/spec.md`（connect/disconnect 语义、工具热注册端口）；
- `openspec/changes/recast-v2/specs/conversation/spec.md`（删除确认挂起消费）。

结构决策（design D3/D6）：工具 = `Tool` Protocol（name / definition() / execute()），
返回 `ToolResult`；原 `ToolContext` 杂物袋拆为职责单一的协议——`ToolSettings`
只读配置面与各 tracker（PlanTracker / GoalState / ReminderState / DeleteGate），
由 `ToolEnv` 聚合，装配时注入。**具体实现归各积木**（config 提供 ToolSettings 实现、
conversation 提供 tracker 实现），本模块只定义接口形态。

注：design D6 表中的 `ReadFileTracker`（已读文件集合）不予定义——specs/tools-file
「Edit 参数契约与唯一性校验」与「工具运行时上下文数据语义」明确规定“MUST NOT 要求
「编辑前必须先 Read」的前置校验（不追踪已读文件）”，以 specs 为准。

本模块为零逻辑纯定义层：只依赖标准库（typing / dataclasses / collections.abc）；
不 import 新包其他积木。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, runtime_checkable

__all__ = [
    "AWAIT_CONFIRM",
    "ConnectedServer",
    "DeleteGate",
    "GoalState",
    "McpPort",
    "PlanTracker",
    "ReminderState",
    "TodoItem",
    "Tool",
    "ToolCatalog",
    "ToolDefinition",
    "ToolEnv",
    "ToolFunctionDef",
    "ToolResult",
    "ToolSettings",
]

AWAIT_CONFIRM = "__AWAIT_CONFIRM__"
"""删除/危险命令挂起确认标记（specs/tools-shell「非 Windows 挂起确认」已发布字面量）。

同一事实的唯一来源：工具侧结构化表达用 `ToolResult.await_confirm`；凡以文本形态
传递或识别该标记的路径（如内循环扫描工具结果）一律引用本常量，禁止再出现散落的
`"__AWAIT_CONFIRM__"` 字面量（消灭现状双轨）。
"""

# ═══════════════════════════════════════════════════════════════
# 工具实现面（Tool / ToolDefinition / ToolResult）
# ═══════════════════════════════════════════════════════════════


class ToolFunctionDef(TypedDict):
    """工具定义的函数体：名称、描述与 JSON Schema 参数表。"""

    name: str
    description: str
    parameters: dict[str, object]


class ToolDefinition(TypedDict):
    """LLM 工具定义（保持 dict 形态契约：这是 LLM API 契约，不做 dataclass 化）。

    形状：`{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}`。
    """

    type: Literal["function"]
    function: ToolFunctionDef


@dataclass
class ToolResult:
    """工具执行结果（`Tool.execute` 的返回契约）。

    - `llm_text`：交给模型的文本（框架随机标签已剥离）；
    - `ui_text`：UI 展示文本（如着色 diff、额外的终端提示）；None 表示无差异输出；
    - `await_confirm`：挂起确认请求（工具返回 True 时命令未执行，等待内循环在
      提示符下确认后重新执行，见 specs/tools-shell「非 Windows 挂起确认」）；
    - `is_error`：执行失败标记（供调度层判定 UI 失败显示）。按 specs/tools-shell
      「退出码与错误标签协议」语义：失败判定只依据框架错误标签/错误前缀（先判定、
      后剥离），命令输出中出现同款文字不得置位本字段。
    """

    llm_text: str
    ui_text: str | None = None
    await_confirm: bool = False
    is_error: bool = False


@runtime_checkable
class Tool(Protocol):
    """工具实现协议（工具积木各工具族的统一形状）。

    - `name`：工具名（LLM 可见名与注册表键；重名消解由来源侧在送入前完成）；
    - `definition()`：返回 LLM 工具定义（dict 形态契约）；
    - `execute(args, env)`：执行工具；`args` 为 LLM 提交的参数（非法 JSON 按空参数
      处理），`env` 为运行时门面（只读配置 + 各状态 tracker + 确认回调 + MCP 端口）。
    """

    @property
    def name(self) -> str: ...

    def definition(self) -> ToolDefinition: ...

    def execute(self, args: dict[str, object], env: ToolEnv) -> ToolResult: ...


# ═══════════════════════════════════════════════════════════════
# 只读配置面（ToolSettings）
# ═══════════════════════════════════════════════════════════════


@runtime_checkable
class ToolSettings(Protocol):
    """工具只读配置面（specs/tools-file「工具运行时上下文数据语义」）。

    实现方（config 积木）须提供全部默认值的零参构造（对齐该规格「零参构造」场景）：
    `max_tool_output_chars=65536`、`max_timeout_seconds=1800`、`ignore_dirs=[]`、
    `require_plan=False`、`min_tools=2`；可变默认不共享。
    """

    @property
    def ignore_dirs(self) -> list[str]:
        """忽略目录集合（Glob/Grep 按目录名匹配剪枝；默认空）。"""
        ...

    @property
    def max_tool_output_chars(self) -> int:
        """工具输出全局硬上限（字符数；默认 65536，0 表示不限制）。

        由配置「工具.输出上限KB」换算注入。
        """
        ...

    @property
    def max_timeout_seconds(self) -> int:
        """工具超时全局上限（秒；默认 1800，0 表示不限制）。

        对前台 timeout 与 bg=wait 的等待秒数取较小值钳制。
        """
        ...

    @property
    def max_transfer_mb(self) -> int:
        """文件传输大小上限（MB；默认 100，0 或负值表示不限制）。

        由配置「工具.最大传输文件MB」注入（specs/tools-remote「文件传输」）。
        """
        ...

    @property
    def git_skip_confirm(self) -> bool:
        """git 免确认开关（True=含 git 的命令跳过确认直接执行）。"""
        ...

    @property
    def rm_skip_confirm(self) -> bool:
        """删除类免确认开关（True=rm/del/rd/rmdir/erase/format 等跳过确认）。"""
        ...

    @property
    def require_plan(self) -> bool:
        """计划优先开关（True=强制先写 TodoWrite 再执行其他工具）。"""
        ...

    @property
    def min_tools(self) -> int:
        """计划优先生效阈值（非计划工具数量达到该值时才拦截；默认 2）。"""
        ...

    def get_api_key(self, name: str) -> str:
        """查询 API 密钥（如 websearch）；未配置的键返回空串，不抛键错误。"""
        ...


# ═══════════════════════════════════════════════════════════════
# 运行时状态协议（状态归主；具体实现归各积木）
# ═══════════════════════════════════════════════════════════════


class TodoItem(TypedDict):
    """计划项：任务描述与状态（`pending` 待处理 / `in_progress` 进行中 / `completed` 已完成）。"""

    content: str
    status: Literal["pending", "in_progress", "completed"]


@runtime_checkable
class PlanTracker(Protocol):
    """当前计划（TodoWrite 写；计划优先拦截与收尾软提醒读）。

    保持引用语义：`replace(todos)` 后 `current()` 返回的列表与提交列表共享元素
    对象（specs/tools-todo 兼容怪癖「就地修正」）。
    """

    def current(self) -> list[TodoItem]:
        """当前计划列表（无计划时为空列表）。"""
        ...

    def replace(self, todos: list[TodoItem]) -> None:
        """整体替换当前计划（TodoWrite 每次提交完整列表）。"""
        ...


@runtime_checkable
class GoalState(Protocol):
    """目标完成标记（GoalComplete 置位；主循环消费并复位）。"""

    @property
    def is_set(self) -> bool:
        """标记是否已置位（统计栏结案信号等只读场景）。"""
        ...

    def mark(self) -> None:
        """置位（GoalComplete 通过收尾校验后调用）。"""
        ...

    def consume(self) -> bool:
        """消费即复位：返回消费前是否已置位，并把标记复位。

        目标模式下每轮结束后由主循环调用：True=本轮已声明完成（停止续跑）。
        """
        ...

    def reset(self) -> None:
        """复位（用户提交新输入时调用，防止普通模式残留误判）。"""
        ...


@runtime_checkable
class ReminderState(Protocol):
    """收尾软提醒标志（计划 / 后台任务各一，触发一次语义）。

    标志由新任务输入复位（specs/tools-todo「目标完成标记的主循环消费」）；
    两种提醒各自独立计数、互不影响。
    """

    def try_trigger_plan(self) -> bool:
        """尝试触发计划提醒：未触发过则置位并返回 True；已触发过返回 False。"""
        ...

    def try_trigger_bg(self) -> bool:
        """尝试触发后台任务提醒：未触发过则置位并返回 True；已触发过返回 False。"""
        ...

    def reset(self) -> None:
        """复位两个标志（用户提交新输入时调用）。"""
        ...


@runtime_checkable
class DeleteGate(Protocol):
    """删除确认挂起门（非 Windows 平台；工具写挂起、内循环确认后消费）。

    现状语义来源：`ToolContext.pending_delete` + `_delete_confirmed`
    （specs/tools-shell「非 Windows 挂起确认」、specs/conversation「删除确认挂起」）。
    """

    def pend(self, tool_name: str, arguments: dict[str, object]) -> None:
        """暂存待确认的调用（工具名与完整参数），命令未执行。"""
        ...

    def take(self) -> tuple[str, dict[str, object]] | None:
        """取出暂存调用（取出后清空暂存）；无暂存时返回 None。"""
        ...

    def mark_confirmed(self) -> None:
        """置「已确认」一次性标记（用户在提示符下确认后调用）。"""
        ...

    def consume_confirmed(self) -> bool:
        """消费「已确认」标记：返回消费前是否已确认，并把标记复位。

        工具执行入口调用：True=本次调用已确认，跳过挂起直接执行。
        """
        ...


# ═══════════════════════════════════════════════════════════════
# MCP 端口与运行时门面（McpPort / ToolEnv）
# ═══════════════════════════════════════════════════════════════


class ConnectedServer(TypedDict):
    """已连接的 MCP 服务器条目：服务器名与登记的工具数。"""

    name: str
    tool_count: int


@runtime_checkable
class ToolCatalog(Protocol):
    """工具目录端口（动态工具热注册/注销的唯一通道，design D8）。

    由装配点组合实现（注册表 + LLM 工具表），构造注入给 `mcp`；`mcp` 积木不再
    经函数内延迟导入直取工具注册表（依赖方向保持单向：mcp 不 import tools）。

    行为语义（specs/mcp「MCP 工具热注册与命名规则」「MCP 断开、工具注销与进程
    终止」「兼容性怪癖保持」）：
    - 注册立即生效：工具可被调用与列举，其定义对下一轮 LLM 请求可见；
    - 与既有工具重名（内置或已注册动态）时跳过该工具、不影响其余；
    - 注销从「可调用实现 + LLM 工具表」两侧按名移除；空或缺失名单不产生变更。
    """

    def get_tool_names(self) -> list[str]:
        """全部已用工具名（内置在前、动态在后；供动态命名去重）。"""
        ...

    def register_dynamic(self, tools: Iterable[Tool]) -> list[str]:
        """注册动态工具并使其定义对下一轮 LLM 请求可见；返回实际注册的工具名清单。"""
        ...

    def unregister_dynamic(self, names: Iterable[str] | None) -> None:
        """按名注销动态工具（连同其 LLM 定义一并移除）；空或缺失名单为无操作。"""
        ...


@runtime_checkable
class McpPort(Protocol):
    """MCP 连接管理端口（行为按 specs/mcp 的 connect/disconnect 语义）。

    由 mcp 积木实现；MCP 工具经 `ToolEnv.mcp` 访问。
    """

    def connect(self, name: str, config: dict[str, object]) -> tuple[int, list[str]]:
        """连接一个 stdio 型服务器（幂等：同名已连接且存活直接返回既有清单）。

        返回 `(注册工具数, [LLM 工具名, ...])`；失败抛异常由工具层转错误行。
        """
        ...

    def disconnect(self, name: str) -> int:
        """断开服务器并注销其工具（`all`/`*`/`全部` 为断开全部）；返回注销的工具数。"""
        ...

    def connected(self) -> list[ConnectedServer]:
        """当前已连接的服务器清单（供断开动作与提示文案使用）。"""
        ...


@runtime_checkable
class ToolEnv(Protocol):
    """工具运行时门面（聚合注入：装配点唯一构造，各工具只读）。

    - `settings`：只读配置面；
    - `plan` / `goal` / `reminders` / `delete_gate`：状态归主后的各 tracker
      （属主写入路径见各协议 docstring）；
    - `confirm`：同步确认回调（仅 Windows 使用；参数为命令文本，返回是否确认）。
      None 表示未注入（如 headless 运行）——命中删除类/git 的命令不拦截、直接执行；
    - `mcp`：MCP 连接管理端口引用；None 表示未初始化（工具返回「MCP 管理器未初始化」
      形态错误行）。
    """

    @property
    def settings(self) -> ToolSettings: ...

    @property
    def plan(self) -> PlanTracker: ...

    @property
    def goal(self) -> GoalState: ...

    @property
    def reminders(self) -> ReminderState: ...

    @property
    def delete_gate(self) -> DeleteGate: ...

    @property
    def confirm(self) -> Callable[[str], bool] | None: ...

    @property
    def mcp(self) -> McpPort | None: ...
