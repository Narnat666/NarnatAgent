"""中断回调注册 —— 解耦 ui↔llm 循环依赖

ui层通过注册回调来中断LLM请求，不再直接导入core.llm。
llm层通过此模块获取中断回调，不再需要知道ui层的存在。
"""

from typing import Optional, Callable

# LLM请求中断回调（由ui层注册）
_abort_callback: Optional[Callable[[], None]] = None


def register_abort(callback: Callable[[], None]) -> None:
    """注册LLM请求中断回调（由ui层调用）"""
    global _abort_callback
    _abort_callback = callback


def abort_request() -> None:
    """触发LLM请求中断（由ui层ESC轮询调用）"""
    if _abort_callback:
        _abort_callback()
