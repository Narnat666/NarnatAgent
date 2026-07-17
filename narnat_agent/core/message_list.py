"""消息列表的唯一所有者 + 只读视图

MessageList 私有持有 messages 列表，外部通过 view() 获取只读视图，
通过受控方法修改。消除多处共享同一列表引用的问题。
"""

from typing import List, Dict, Any, Iterator, Optional


class MessageView:
    """messages 的只读视图。零拷贝，但调用方无法修改列表结构。"""

    def __init__(self, messages: List[Dict[str, Any]]):
        self._messages = messages

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        return self._messages[index]

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self._messages)

    def to_list(self) -> List[Dict[str, Any]]:
        """返回浅拷贝列表，用于传给 httpx 等库或序列化到磁盘"""
        return list(self._messages)

    def count_role(self, role: str) -> int:
        """统计指定 role 的消息数"""
        return sum(1 for m in self._messages if m.get("role") == role)


class MessageList:
    """messages 列表的唯一所有者。

    所有修改都通过此对象的方法进行，外部无法获取内部列表引用。
    读取通过 view() 获取只读 MessageView。
    """

    def __init__(self, system_prompt: str):
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # ── 只读接口 ──

    def view(self) -> MessageView:
        """返回只读视图。零拷贝，但调用方无法修改列表结构。"""
        return MessageView(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    # ── 受控修改接口（唯一修改入口）──

    def append_system(self, content: str) -> None:
        """追加系统消息"""
        self._messages.append({"role": "system", "content": content})

    def append_user(self, content: str) -> None:
        """追加用户消息"""
        self._messages.append({"role": "user", "content": content})

    def append_assistant(self, content: str, tool_calls: Optional[list] = None) -> None:
        """追加 assistant 消息"""
        msg = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        """追加工具结果消息"""
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        """为未完成的 tool_call 追加中断结果"""
        for tc in tool_calls:
            if tc["id"] not in completed_ids:
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "[用户中断]",
                })

    def replace_all(self, new_messages: List[Dict[str, Any]]) -> None:
        """原子替换全部消息（会话切换时使用）"""
        self._messages.clear()
        self._messages.extend(new_messages)

    def clear_and_rebuild(self, system_prompt: str, summary: str,
                          compressor) -> None:
        """清空并重建消息列表（压缩后使用）"""
        new_messages = compressor.build_new_session_messages(system_prompt, summary)
        self._messages.clear()
        self._messages.extend(new_messages)

    def compress_and_rebuild(self, system_prompt: str, summary: str,
                             pending_input: str, compressor) -> None:
        """压缩后重建：system + summary + user_input"""
        new_messages = compressor.build_new_session_messages(system_prompt, summary)
        new_messages.append({"role": "user", "content": pending_input})
        self._messages.clear()
        self._messages.extend(new_messages)
