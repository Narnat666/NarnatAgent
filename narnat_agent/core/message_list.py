"""消息列表的唯一所有者 + 只读视图

MessageList 私有持有 messages 列表，外部通过 view() 获取只读视图，
通过受控方法修改。消除多处共享同一列表引用的问题。
"""

from typing import List, Dict, Any, Iterator, Optional, Set, Tuple

# 合成 assistant 消息（repair 伪造"[用户中断]"）挂载的思考占位文本。
# DeepSeek 思考模式校验：请求尾部 assistant 必须有 thinking 块，若为空则其前
# 最近的工具调用轮也必须有 thinking 块；用非空占位可同时兜住两种情形。
# 转换层（llm.py）仅在"思考回传"开关开启时回传该占位（与真实思考同规则）；
# 开关关闭时 thinking 段彻底删除（用户选择：计划放行修复后正常流程不再出现尾部AI发言）。
SYNTHETIC_THINKING = "（用户中断了工具执行）"

# 内容审核拦截恢复：被折叠工具结果的替换文案。原文不留存——服务端按内容
# 判定，残留原文会让后续每次请求继续被拒（折叠必须真正从上下文里移除）。
FOLDED_TOOL_MARKER = "[已折叠: 原工具结果共{n}字符，疑似触发服务端内容审核，原文不再保留]"

# 可折叠的最小长度：过低无收益（替换文案本身更长，反而增大上下文），
# 且会让"参数类 400"白跑折叠重试——低于此值一律视为无可折叠项
MIN_FOLDABLE_TOOL_CHARS = 200


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

    def append_assistant(self, content: str, tool_calls: Optional[list] = None,
                         thinking: Optional[str] = None,
                         thinking_signature: Optional[str] = None) -> None:
        """追加 assistant 消息

        thinking: 模型思考内容（思考模式 API 要求后续请求原样回传）。
        为 None 表示未捕获（如历史会话），不写入该字段；空串是合法值（合成消息）。
        thinking_signature: 思考块签名（Claude 回传思考块必需）；为 None 不写入。
        """
        msg = {"role": "assistant", "content": content or None}
        if thinking is not None:
            msg["thinking"] = thinking
        if thinking_signature is not None:
            msg["thinking_signature"] = thinking_signature
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

    def fold_largest_tool_result(self, folded_ids: Set[str]) -> Optional[Tuple[str, int]]:
        """就地折叠最长的一条尚未折叠的 tool 结果（内容审核拦截恢复用）。

        folded_ids: 已折叠的 tool_call_id 集合（调用方持有，跨次调用累计）。
        就地改写消息内容——请求副本是同一批 dict 的浅拷贝，改写同步可见。
        返回 (tool_call_id, 原字符数)；无可折叠项（无工具结果 / 均已折叠 /
        均低于 MIN_FOLDABLE_TOOL_CHARS）时返回 None。
        """
        best_idx, best_len = -1, 0
        for i, msg in enumerate(self._messages):
            if msg.get("role") != "tool":
                continue
            if (msg.get("tool_call_id") or "") in folded_ids:
                continue
            n = len(msg.get("content") or "")
            if n > best_len:
                best_idx, best_len = i, n
        if best_idx < 0 or best_len < MIN_FOLDABLE_TOOL_CHARS:
            return None
        msg = self._messages[best_idx]
        tc_id = msg.get("tool_call_id") or ""
        msg["content"] = FOLDED_TOOL_MARKER.format(n=best_len)
        folded_ids.add(tc_id)
        return tc_id, best_len

    def replace_all(self, new_messages: List[Dict[str, Any]]) -> None:
        """原子替换全部消息（会话切换时使用）。

        单次引用赋值（GIL 下原子）：并发读者要么看到旧列表、要么看到新列表，
        不会撞见 clear()+extend() 两步之间的空/半列表（后台保存线程读到空
        列表会把会话清空落盘）。
        """
        self._messages = list(new_messages)

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
