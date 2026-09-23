"""LLM 客户端 —— 统一入口：协议分发、工具定义动态管理、事件产出外壳。

搬运来源：旧包 `narnat_agent/core/llm.py` 163-255 行（`LLMClient`）——协议分派
（只有 `anthropic` 是特例，其余任意值含拼写错误静默走 OpenAI）、消息非法代理字符
清理、工具定义列表的复制与原地增删语义逐条保持，结构上把类级共享状态收敛为
`LLMRuntime` 实例状态（对齐 design D6/D7 与 specs/llm 全部 Requirement）。
"""
from __future__ import annotations

from typing import Any, Iterator

from ..contracts.interrupt import InterruptSignal
from .anthropic_backend import AnthropicBackend
from .openai_backend import OpenAIBackend
from .retry import DEFAULT_MAX_RETRIES, clamp_retry_count
from .runtime import LLMRuntime

__all__ = ["DEFAULT_MAX_OUTPUT_TOKENS", "LLMClient", "strip_surrogates"]

DEFAULT_MAX_OUTPUT_TOKENS = 128000
"""Anthropic 兼容请求 `max_tokens` 的默认值（配置 `最大输出token数` 非空时被覆盖）。"""


def strip_surrogates(obj):
    """递归清理非法 UTF-8 代理字符（字典与列表递归处理，其他类型原样返回）。

    返回新结构，不改动调用方持有的对象（specs/llm「协议选择与请求构建」）。
    """
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_surrogates(v) for v in obj]
    return obj


class LLMClient:
    """LLM 客户端：按配置协议选择 OpenAI / Anthropic 兼容后端。

    实例级状态（无类级全局）：工具定义列表（后端共享同一列表对象，原地变更对下一轮
    请求立即生效，且不改动调用方传入的原列表）、活跃请求句柄（中断关闭）、重试上限
    （构造时与每轮对话开始时可同步，钳制 1–10）。
    """

    def __init__(self, config, logger=None, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                 tool_definitions: list | None = None, interrupt: InterruptSignal | None = None,
                 goal_tool_definition: dict | None = None, *, thinking_rules: Any) -> None:
        """构造客户端。

        - `config`：配置面（协议、模型、地址、密钥、温度、最大输出、重试次数、思考
          开关/强度/回传开关）；
        - `tool_definitions`：初始工具定义列表（内部复制一份）；
        - `interrupt`：中断信号总线（提供 `subscribe(handler)`）——订阅本客户端的
          `abort_active_request`，中断触发时关闭活跃 HTTP 连接/流；None 表示不接线；
        - `goal_tool_definition`：目标完成（GoalComplete）工具定义，由装配方注入
          （`llm` 不得反向依赖 `tools`），供 `set_goal_tool` 注入/移除；
        - `thinking_rules`：thinking 解析提供方，**必需注入（无默认值，未传即
          TypeError）**——传 `config.defaults`（提供 `resolve_thinking_params` /
          `resolve_thinking_passback`）；`llm` 不内置映射表副本，装配方负责接线。
        """
        self._config = config
        self._logger = logger
        self._goal_tool_definition = goal_tool_definition
        self._runtime = LLMRuntime(tool_defs=tool_definitions, thinking_rules=thinking_rules)
        self.set_retry_count(getattr(config, "retry_count", DEFAULT_MAX_RETRIES))

        if interrupt is not None:
            interrupt.subscribe(self.abort_active_request)

        # 协议由 config.protocol 显式指定：只有 anthropic 是特例，其余任意值走 OpenAI
        if config.protocol == "anthropic":
            self._backend = AnthropicBackend(config, self._runtime, logger, max_output_tokens)
        else:
            self._backend = OpenAIBackend(config, self._runtime, logger)

    def chat_stream(self, messages, no_tools: bool = False, no_thinking: bool = False,
                    cancel_check=None) -> Iterator[dict]:
        """发起流式请求，返回事件流生成器（首次迭代才真正发请求）。

        `no_tools=True` 请求不带工具定义、`no_thinking=True` 走 thinking 禁用分支；
        用户中断（`cancel_check` 为真）静默结束本轮，不产出任何事件。
        """
        return self._backend.chat_stream(strip_surrogates(messages), no_tools=no_tools,
                                         no_thinking=no_thinking, cancel_check=cancel_check)

    def set_retry_count(self, n: int) -> None:
        """设置重试上限（构造与每轮对话开始时同步，网络类与限流类同源同值）。"""
        self._runtime.max_retries = clamp_retry_count(n)

    def set_goal_tool(self, enabled: bool, definition: dict | None = None) -> None:
        """动态注入/移除目标完成（GoalComplete）工具定义。

        目标模式开启时注入，关闭时移除；普通模式不向 LLM 暴露该工具。幂等：重复
        开启/关闭不会重复添加或报错。定义缺省取构造注入的目标工具定义；两者皆无时
        为无操作（装配未接线）。
        """
        definition = definition if definition is not None else self._goal_tool_definition
        if definition is None:
            return
        name = definition["function"]["name"]
        defs = self._runtime.tool_defs
        exists = any(d.get("function", {}).get("name") == name for d in defs)
        if enabled and not exists:
            defs.append(definition)
        elif not enabled and exists:
            defs[:] = [d for d in defs if d.get("function", {}).get("name") != name]

    def add_tool_definitions(self, definitions: list) -> None:
        """热追加工具定义（MCP 服务器连接成功后送入）。按名称去重、幂等。

        `tool_defs` 与后端共享同一列表对象，每轮请求现读 → 追加后 AI 下一轮即可见；
        名称为空的定义跳过，同一次送入列表内部也去重。
        """
        existing = {d.get("function", {}).get("name") for d in self._runtime.tool_defs}
        for definition in definitions or []:
            name = definition.get("function", {}).get("name")
            if name and name not in existing:
                self._runtime.tool_defs.append(definition)
                existing.add(name)

    def remove_tool_definitions(self, names) -> None:
        """热移除工具定义（MCP 断开时调用）：按名移除全部匹配定义；空名单为无操作。"""
        names = set(names or ())
        if not names:
            return
        self._runtime.tool_defs[:] = [
            d for d in self._runtime.tool_defs
            if d.get("function", {}).get("name") not in names
        ]

    def abort_active_request(self) -> None:
        """关闭当前活跃请求的 HTTP 连接或响应流（中断回调；无句柄为空操作）。"""
        self._runtime.abort()

    @property
    def raw_sse(self) -> list[str] | None:
        """最近一轮的原始 SSE 数据段（Anthropic 兼容协议）；其他协议为 None。"""
        backend_sse = getattr(self._backend, "raw_sse", None)
        return list(backend_sse) if backend_sse is not None else None
