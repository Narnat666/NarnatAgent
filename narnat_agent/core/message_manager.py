"""消息管理 —— 消息修复、压缩触发

MessageManager 负责消息的修复逻辑和压缩流程编排，
实际的列表持有和修改委托给 MessageList（唯一所有者）。
"""

from typing import Optional, NamedTuple

from .compressor import Compressor, select_cut_index, estimate_tokens
from .message_list import MessageList, MessageView, SYNTHETIC_THINKING
from ..logger import AgentLogger


class CompressResult(NamedTuple):
    """压缩结果。

    ok=True：history 已被摘要替换，replaced/tokens 为被替换的对话消息条数
    与其估算 token 数（统计口径，仅供提示展示）。
    ok=False：见 reason ——
      "empty"       没有可压缩的历史对话（全部落在保留预算内）
      "interrupted" 用户中断
      "llm_error" / "empty_summary" / "overflow" 压缩请求失败
    """
    ok: bool
    replaced: int = 0
    tokens: int = 0
    reason: str = ""


class MessageManager:
    """消息管理器 —— 委托 MessageList 执行修改，自身负责 repair 逻辑和压缩编排"""

    def __init__(self, messages: MessageList, compressor: Compressor,
                 logger: Optional[AgentLogger] = None):
        self._messages = messages
        self._compressor = compressor
        self._logger = logger

    @property
    def view(self) -> MessageView:
        """只读视图（供LLM调用等场景）"""
        return self._messages.view()

    # ── 追加方法（委托 MessageList）──

    def append_system(self, content: str) -> None:
        self._messages.append_system(content)

    def append_user(self, content: str) -> None:
        self._messages.append_user(content)

    def append_assistant(self, content: str, tool_calls: Optional[list] = None,
                         thinking: Optional[str] = None,
                         thinking_signature: Optional[str] = None) -> None:
        self._messages.append_assistant(content, tool_calls, thinking, thinking_signature)

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        self._messages.append_tool_result(tool_call_id, result)

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        self._messages.append_interrupted_tools(tool_calls, completed_ids)

    # ── 修复方法（repair 逻辑原样保留，通过 view 读取）──

    def repair(self) -> None:
        """修复messages：打断后可能留下不完整的消息序列。

        1. assistant含tool_calls但没有对应的tool消息 → 补上tool("[用户中断]")
        2. 如果第1步修复了，且末尾是tool消息 → 补上assistant（API要求tool后不能直接跟user）
        """
        msgs = self._messages

        # 1. 为未回复的tool_call补上空结果
        replied_ids = set()
        for msg in msgs.view():
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        for msg in msgs.view():
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        msgs.append_tool_result(tc_id, "[用户中断]")
                        replied_ids.add(tc_id)
                        repaired = True

        # 2. 只有在第1步确实修复了未回复的tool_call时，才补assistant
        if repaired and len(msgs) > 0 and msgs.view()[-1].get("role") == "tool":
            # 思考模式下请求末尾的 assistant 消息必须携带非空 thinking 块，
            # 否则 DeepSeek V4 返回 400（The content[].thinking ... must be passed back）
            msgs.append_assistant(SYNTHETIC_THINKING, thinking=SYNTHETIC_THINKING)

        if repaired:
            self._logger.info("core.message_manager", "repair: 修复了打断后的消息序列")

    # ── 压缩方法（逻辑原样保留，通过 view/to_list 读取）──

    def handle_compress(self, pending_input: Optional[str], system_prompt: str,
                        llm_client, cancel_check, on_interrupt, on_llm_error,
                        retain_tokens: int = 0) -> CompressResult:
        """
        处理上下文压缩。

        pending_input: 压缩后要以 user 消息收尾的新输入；None=手动压缩
        （/compact），不携带新输入。
        retain_tokens: 逐字保留的近期尾部预算（启发式估价token），0=全量压缩。
        摘要输入始终为全部历史；切点选择只决定压缩后保留哪些近期消息。
        中断/失败不改动 messages，由调用方回调善后。

        Returns:
            CompressResult；ok=True 时 pending_input（若有）已在 messages 中
        """
        self._logger.info("message_manager", f"压缩触发, messages={len(self._messages)}条")

        full_messages = self._messages.view().to_list()

        # 对话区起点：system 消息（含技能注入）不进保留区，
        # 重建时由新的 system_prompt + 摘要承担
        start = 0
        while start < len(full_messages) and full_messages[start].get("role") == "system":
            start += 1

        # 保留预算覆盖了全部对话（或本就没有对话）：压缩无收益，在消耗一次
        # 总结请求前先拒绝。仅手动压缩（无新输入）启用；自动路径保持旧行为——
        # 其重建过程承担着把触发输入写入历史并收尾的职责，不能在此提前返回
        cut = select_cut_index(full_messages, retain_tokens)
        if pending_input is None and (start >= len(full_messages)
                                      or (cut is not None and cut <= start)):
            self._logger.info("message_manager", "无历史可压缩，跳过")
            return CompressResult(False, reason="empty")

        # 被摘要替换掉的对话消息（统计口径，仅供提示展示）
        replaced_msgs = full_messages[start:cut] if cut is not None else full_messages[start:]
        replaced = len(replaced_msgs)
        replaced_tokens = sum(estimate_tokens(m) for m in replaced_msgs)

        # 请求前先判取消：用户已按 Esc（含上一轮残留标记）时不再发起压缩请求
        if cancel_check and cancel_check():
            on_interrupt()
            return CompressResult(False, reason="interrupted")

        # 构建压缩请求（全部历史 + 指令，摘要质量不受尾部切分影响）
        compress_messages = self._compressor.build_compress_messages(full_messages)

        # 发送压缩请求，收集AI输出
        summary_content = []
        llm_error = False
        for chunk in llm_client.chat_stream(compress_messages, no_tools=True, cancel_check=cancel_check):
            if cancel_check and cancel_check():
                on_interrupt()
                return CompressResult(False, reason="interrupted")
            if "finish_reason" in chunk:
                if chunk["finish_reason"] == "context_overflow":
                    # 压缩请求自身也超限（全量历史+指令仍超窗口）：单独归因，
                    # 否则会被下游误报为"总结为空"，掩盖真实原因
                    on_llm_error("压缩失败: 压缩请求自身超出模型上下文限制")
                    return CompressResult(False, reason="overflow")
                if chunk["finish_reason"] == "error":
                    llm_error = True
                    break
            if "content" in chunk and "tool_calls" not in chunk:
                summary_content.append(chunk["content"])

        if llm_error:
            on_llm_error("压缩失败: LLM调用出错")
            return CompressResult(False, reason="llm_error")

        # llm 层在取消时静默结束流（不产出任何 chunk），此处补判一次：
        # 否则"零输出 + 已取消"会被误归因为"总结为空"，掩盖真实原因
        if not summary_content and cancel_check and cancel_check():
            on_interrupt()
            return CompressResult(False, reason="interrupted")

        summary = "".join(summary_content)

        # 校验总结结果（内存中直接校验）
        if not summary.strip():
            on_llm_error("压缩失败: 总结为空")
            return CompressResult(False, reason="empty_summary")

        # 先完整构建新会话（含用户问题），再原子替换旧会话
        tail = full_messages[cut:] if cut is not None else []
        dropped = []
        if pending_input is None:
            # 手动压缩：不追加输入。尾部末条是 user 的罕见情形（上一轮没留下
            # AI 输出/历史含连续 user）整段剔除——否则下次输入会形成连续 user
            # （Anthropic 严格后端强制角色交替）；其内容已进摘要
            while tail and tail[-1].get("role") == "user":
                dropped.append(tail[-1])
                tail = tail[:-1]
            # 被剔除的消息同样由摘要替换掉，计入替换统计（口径=从历史消失的条数）
            replaced += len(dropped)
            replaced_tokens += sum(estimate_tokens(m) for m in dropped)
        new_messages = self._compressor.build_new_session_messages(system_prompt, summary, tail)
        if pending_input is not None:
            if tail and tail[-1].get("role") == "user":
                # 防连续 user（Anthropic 严格后端强制角色交替）：
                # 尾部末条已是 user（如溢出场景的触发输入）时把新输入合并进该条
                merged = dict(tail[-1])
                merged["content"] = (tail[-1].get("content") or "") + "\n\n" + pending_input
                new_messages[-1] = merged
            else:
                new_messages.append({"role": "user", "content": pending_input})
        self._messages.replace_all(new_messages)

        self._logger.info(
            "message_manager",
            f"压缩成功，新会话已创建, 保留尾部={len(tail)}条",
        )
        return CompressResult(True, replaced, replaced_tokens)

    def clear_and_rebuild(self, system_prompt: str, summary_text: str) -> None:
        """清空并重建消息列表（压缩后使用）"""
        new_messages = self._compressor.build_new_session_messages(system_prompt, summary_text)
        self._messages.replace_all(new_messages)
