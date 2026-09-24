"""工具注册表与统一执行入口 —— 名称解析、定义收集、动态注册与结果归一。

契约来源：
- specs/tools-file「工具执行入口协议」「工具输出全局上限截断」「编辑类工具的返回形态与
  差分展示」「动态工具注册与注销可见性」；
- specs/tools-shell「退出码与错误标签协议」（标签**先判定、后剥离**）；
- specs/mcp「MCP 工具热注册与命名规则」（动态工具注册面：重名跳过、幂等）；
- specs/tools-todo「GoalComplete 工具定义与目标模式可见性」（定义默认不进表、
  执行能力始终注册）；
- design D3（工具协议显式化）/ D6（运行时环境替代杂物袋）/ D8（装配线性化）。

与旧实现的结构差异（行为不变）：
- 注册表由模块级可变字典改为 `ToolRegistry` 实例（消灭模块级可变状态）；
- 工具为 `contracts.tool.Tool` 协议对象，执行入口以 `ToolEnv` 参数替代
  `_tool_context` 私有参数注入（原 `_CONTEXT_TOOLS` 名单随之取消：所有工具统一收
  `env`，各自按需读取）；
- 「有效参数」列表取自工具定义的 JSON Schema（新协议下参数面以定义为唯一来源，
  旧实现依赖 `inspect.signature`）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from ..contracts.tool import AWAIT_CONFIRM, Tool, ToolDefinition, ToolEnv, ToolResult
from .signal import error_line, has_error, strip_tags
from .token_estimate import estimate_text_tokens

__all__ = [
    "ToolRegistry",
    "friendly_type_error",
    "normalize_result",
    "valid_param_names",
]

UNEXPECTED_KEYWORD_RE = re.compile(r"unexpected keyword argument '([^']+)'")
QUOTED_RE = re.compile(r"'([^']+)'")


def valid_param_names(tool: Tool) -> list[str]:
    """取工具的有效参数名列表（来自工具定义的 JSON Schema properties，保持定义顺序）。

    定义缺失或形态异常时返回空列表——此时友好提示降级为原生异常文本
    （与旧实现「签名不可得 → 列表为空」的行为一致）。
    """
    try:
        parameters = tool.definition().get("function", {}).get("parameters", {})
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    except Exception:
        return []
    return list(properties) if isinstance(properties, dict) else []


def friendly_type_error(name: str, params: Sequence[str], err: Exception) -> str:
    """把 Python 原生 TypeError 转成对 LLM 友好的中文提示，并列出该工具的有效参数。

    LLM 传错参数名（如 filepath 而非 file_path）或漏传必填参数时，
    原生的英文报错信息虽可读但不够直接；列出有效参数名能让 AI 一次修正。
    两种形态都不匹配时原样返回异常文本（兼容：参数列表为空时亦降级为原文）。
    """
    msg = str(err)
    match = UNEXPECTED_KEYWORD_RE.search(msg)
    if match and params:
        return (f"收到未知参数 '{match.group(1)}'。"
                f"{name} 的有效参数: {', '.join(params)}")
    if "missing" in msg:
        # 支持单/多参数缺失: "missing 1 required positional argument: 'a'"
        # 或 "missing 2 required positional arguments: 'a' and 'b'"
        missing = QUOTED_RE.findall(msg)
        if missing and params:
            return (f"缺少必填参数: {', '.join(missing)}。"
                    f"{name} 的有效参数: {', '.join(params)}")
    return msg


def normalize_result(raw: object) -> ToolResult:
    """把工具返回值归一为 `ToolResult`。

    - `ToolResult` → 原样；
    - 二元组 `(文本结果, ui 文本)` → 按位取值（空 ui 文本归一为 None=无差异输出）；
    - 其它 → 视为纯文本结果（无 ui 文本）。
    """
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, tuple):
        text = raw[0] if raw else ""
        ui_raw = raw[1] if len(raw) > 1 else None
        return ToolResult(
            llm_text=text if isinstance(text, str) else str(text),
            ui_text=None if ui_raw is None else (str(ui_raw) or None),
        )
    return ToolResult(llm_text=raw if isinstance(raw, str) else str(raw))


class ToolRegistry:
    """工具注册表 —— 内置与动态工具的注册/注销、LLM 定义收集与统一执行入口。

    装配点构造后经构造注入传给消费方（conversation 调度、mcp 热注册、app 收集定义），
    不在模块级持有状态。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._hidden_definitions: set[str] = set()
        self._dynamic: dict[str, Tool] = {}

    # ═══════════════════════════════════════════════════════════
    # 注册与注销
    # ═══════════════════════════════════════════════════════════

    def register(self, tool: Tool, *, expose_definition: bool = True) -> bool:
        """注册内置工具（按注册顺序进入工具名清单与定义列表）。

        - 重名（内置或已注册动态工具占用该名）时跳过并返回 False；
        - `expose_definition=False` 时其定义不进默认定义列表（如 GoalComplete：
          仅在目标模式开启时经 `get_tool_definitions(include_goal=True)` 注入，
          执行能力始终注册）。
        """
        name = tool.name
        if name in self._tools or name in self._dynamic:
            return False
        self._tools[name] = tool
        if not expose_definition:
            self._hidden_definitions.add(name)
        return True

    def register_dynamic(self, tools: Iterable[Tool]) -> list[str]:
        """注册运行时动态工具（当前来源：MCP 服务器 tools/list）。

        与内置工具或已注册动态工具重名时跳过该工具（内置优先）、不影响其余；
        重复调用幂等。返回本次实际注册的工具名列表（调用方按既有语义另行记录
        登记清单，两者口径可以不同）。
        """
        registered: list[str] = []
        for tool in tools:
            name = tool.name
            if name in self._tools or name in self._dynamic:
                continue
            self._dynamic[name] = tool
            registered.append(name)
        return registered

    def unregister_dynamic(self, names: Iterable[str] | None) -> None:
        """注销运行时动态工具（MCP 断开时调用）；内置工具不受影响。

        空或缺失名单不产生任何变更；注销同时移除其工具定义。
        """
        targets = set(names or ())
        if not targets:
            return
        for name in targets:
            self._dynamic.pop(name, None)

    # ═══════════════════════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════════════════════

    def get_tool_names(self) -> list[str]:
        """全部工具名（内置在前、动态在后）。"""
        return list(self._tools) + list(self._dynamic)

    def get_tool_definitions(self, include_goal: bool = False) -> list[ToolDefinition]:
        """返回 LLM 工具定义列表（内置定义 + 动态定义）。

        过滤规则：默认隐藏的工具定义（GoalComplete）不进列表；`include_goal=True`
        时追加在末尾（目标模式开启时注入、关闭时不再传入即为移除，幂等由调用方
        按开关取用保证）。
        """
        definitions = [
            tool.definition()
            for name, tool in self._tools.items()
            if name not in self._hidden_definitions
        ]
        definitions += [tool.definition() for tool in self._dynamic.values()]
        if include_goal:
            definitions += [
                tool.definition()
                for name, tool in self._tools.items()
                if name in self._hidden_definitions
            ]
        return definitions

    def has_tool(self, name: str) -> bool:
        """该名是否已注册（内置或动态）。"""
        return name in self._tools or name in self._dynamic

    # ═══════════════════════════════════════════════════════════
    # 执行入口
    # ═══════════════════════════════════════════════════════════

    def execute(self, name: str, arguments: Mapping[str, object] | None,
                env: ToolEnv | None = None) -> ToolResult:
        """统一工具执行入口：名称解析 → 调用 → 异常包装 → 归一/判定/剥离/截断。

        - 内置工具名优先，其次运行时动态工具；未知工具名返回
          `[错误: 未知工具: {name}]`；
        - `arguments` 非映射（缺失或非法 JSON 形态）按空参数处理；
        - `TypeError` → `[错误: 工具参数错误({name}): {友好提示}]`；
          其它普通异常 → `[错误: 工具执行失败({name}): {异常文本}]`
          （系统退出/键盘中断等 BaseException 不捕获）；
        - 判定顺序（specs/tools-shell）：先以框架错误标签判定失败（命令输出中同款
          文字无标签、不触发），后剥离标签；截断在剥离之后（标签不参与截断）；
        - 全局输出上限截断（`env.settings.max_tool_output_chars`，0 表示不限制）：
          保留首部 2/3 与尾部 1/3，中段插入全局截断提示；`env` 为 None 时不截断。
        """
        tool = self._tools.get(name)
        if tool is None:
            tool = self._dynamic.get(name)
        if tool is None:
            return self._finalize(error_line(f"未知工具: {name}"), env)

        args = dict(arguments) if isinstance(arguments, Mapping) else {}
        try:
            raw = tool.execute(args, env)
        except TypeError as exc:
            detail = friendly_type_error(name, valid_param_names(tool), exc)
            return self._finalize(error_line(f"工具参数错误({name}): {detail}"), env)
        except Exception as exc:
            return self._finalize(error_line(f"工具执行失败({name}): {exc}"), env)
        return self._finalize(raw, env)

    def _finalize(self, raw: object, env: ToolEnv | None) -> ToolResult:
        """结果归一 + 标签先判定后剥离 + 全局输出上限截断。

        执行入口的全部出口（未知工具/参数错误/执行失败/正常结果）统一经此路径：
        错误行文本同样按标签判定失败后剥离，保证 `ToolResult.llm_text` 一律无标签。
        """
        result = normalize_result(raw)
        # 先判定（此时文本仍带框架标签；命令输出中的同款文字无标签、不误判）
        is_error = result.is_error or has_error(result.llm_text)
        # 后剥离（幂等：已剥离文本不受影响）
        text = strip_tags(result.llm_text)
        # 挂起确认：结构化字段优先；文本形态的已发布字面量一并识别（消灭双轨）
        await_confirm = result.await_confirm or text == AWAIT_CONFIRM
        return ToolResult(
            llm_text=self._truncate(text, env),
            ui_text=result.ui_text,
            ui_text_kind=result.ui_text_kind,
            await_confirm=await_confirm,
            is_error=is_error,
        )

    @staticmethod
    def _truncate(text: str, env: ToolEnv | None) -> str:
        """全局输出硬截断（保留首尾：尾部常含提示符等关键状态信息）。

        该路径按字符硬截断、不做标签吸附（specs/tools-shell 兼容怪癖：标签已在
        `_finalize` 的剥离步骤中移除，故不会切开标签）。
        """
        if env is None:
            return text
        limit = env.settings.max_tool_output_chars
        if limit <= 0 or len(text) <= limit:
            return text
        original_len = len(text)
        head = limit * 2 // 3
        tail = limit - head
        est = estimate_text_tokens(text)  # ≈token（AI预算单位，混合密度估算）
        return (
            text[:head]
            + f"\n...[全局截断: 输出共{original_len}字符(≈{est}token), 已达全局上限{limit // 1024}KB, 已保留首尾。如需更多内容，请缩小本次输出（过滤/分页/减小范围）]"
            + text[-tail:]
        )
