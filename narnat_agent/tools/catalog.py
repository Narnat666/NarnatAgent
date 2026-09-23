"""工具目录端口的装配实现 —— 组合注册表与 LLM 工具定义表。

契约来源：`contracts/tool.py` 的 `ToolCatalog` 协议 + specs/mcp「MCP 工具热注册
与命名规则」「MCP 断开、工具注销与进程终止」；结构决策 design D8（MCP 工具热注册
经端口构造注入，拒绝装配后裸接线）。

实现要点：
- 注册面（`get_tool_names` / `register_dynamic` / `unregister_dynamic`）转发给
  `ToolRegistry`；定义面（LLM 工具表热增删）转发给装配点注入的两个回调
  （如 `LLMClient.add_tool_definitions` / `remove_tool_definitions` 绑定方法）；
- 定义同步沿用现状口径：送入 LLM 表的是**全部候选工具**的定义（注册表侧因重名
  被跳过的工具也在其中，见 specs/mcp 兼容怪癖③的登记全量语义）；
- 本模块只依赖 contracts 与本积木 registry：定义面以回调注入，不 import `llm`。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from ..contracts.tool import Tool
from .registry import ToolRegistry

__all__ = ["ToolCatalogImpl"]


class ToolCatalogImpl:
    """`ToolCatalog` 端口的默认实现（装配点构造：注册表 + 两个定义表回调）。

    回调缺省为 None 表示未接 LLM 工具表（此时仅注册面生效，等价于旧实现的
    「不接工具表回调」文档语义）。
    """

    def __init__(self, registry: ToolRegistry,
                 add_definitions: Callable[[list], None] | None = None,
                 remove_definitions: Callable[[Iterable[str]], None] | None = None) -> None:
        self._registry = registry
        self._add_definitions = add_definitions
        self._remove_definitions = remove_definitions

    def get_tool_names(self) -> list[str]:
        """全部已用工具名（内置在前、动态在后）。"""
        return self._registry.get_tool_names()

    def register_dynamic(self, tools: Iterable[Tool]) -> list[str]:
        """注册动态工具到注册表，并把全部候选工具的定义热追加进 LLM 工具表。

        返回注册表实际注册的工具名清单（重名被跳过的不在其中）；LLM 表按名去重
        由定义表实现方保证（specs/llm「工具定义动态管理」）。
        """
        items = list(tools)
        registered = self._registry.register_dynamic(items)
        if self._add_definitions is not None and items:
            self._add_definitions([tool.definition() for tool in items])
        return registered

    def unregister_dynamic(self, names: Iterable[str] | None) -> None:
        """从注册表与 LLM 工具表两侧按名注销；空或缺失名单不产生变更。"""
        targets = list(names or ())
        if not targets:
            return
        self._registry.unregister_dynamic(targets)
        if self._remove_definitions is not None:
            self._remove_definitions(targets)
