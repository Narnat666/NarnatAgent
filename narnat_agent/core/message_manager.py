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
        # 写入守卫的当前代际（None = 不守卫，正常单轮路径就是 None）。
        # agent_loop 在主循环开始时 bind_generation()，收尾 finally 里解绑。
        self._bound_generation: Optional[int] = None

    # ── 写入守卫（会话代际）──
    #
    # 为什么需要（P0，实测数据损坏）：工具还在执行时用户切会话，
    # `_cancel_and_wait_turn` 只等 3 秒；串口/SSH 那种底层超时几十秒的工具
    # 等不到收口，于是工具的「声明」留在上一段对话、而「结果」在这之后才
    # append —— 此时 MessageList 已被换成新会话。结果成了孤儿 tool_result，
    # Anthropic 协议直接 400，且该会话从此每条消息都发不出去。
    #
    # 守卫就是让「上一轮的结果写不进新会话」：append 前比对代际，不一致就
    # 丢弃并记日志。这是**不依赖等待**的根治手段 —— 等待只能尽量收口，
    # 守卫保证即使等不到也不会写错地方。

    def bind_generation(self) -> Optional[int]:
        """绑定当前代际（agent_loop 每轮开始时调）。返回值仅用于日志。"""
        self._bound_generation = self._messages.generation
        return self._bound_generation

    def unbind_generation(self) -> None:
        """解绑（每轮结束/异常时调）。不解绑会让下一轮沿用旧代际而拒写。"""
        self._bound_generation = None

    def _generation_stale(self, what: str) -> bool:
        """本轮的代际是否已过期（= 期间换过会话）。过期时记一条日志。"""
        bound = self._bound_generation
        if bound is None:
            return False
        now = self._messages.generation
        if now == bound:
            return False
        if self._logger:
            self._logger.warning(
                "core.message_manager",
                f"丢弃「{what}」：会话已切换（代际 {bound}→{now}），"
                f"这一轮的结果属于上一段对话，不能再写入")
        return True

    @property
    def view(self) -> MessageView:
        """只读视图（供LLM调用等场景）"""
        return self._messages.view()

    # ── 追加方法（委托 MessageList）──

    def append_user(self, content: str) -> None:
        self._messages.append_user(content)

    def append_user_merged(self, content: str) -> None:
        """追加 user 消息；末尾已是 user 时合并进该条（防连续 user，Anthropic 严格后端要求角色交替）

        带 images 的 user 消息（工具结果配图）不参与合并：拼接只动字符串，
        合并会让 images 字段静默丢失（图还在历史里但再也发不出去）。
        这类消息后面直接新起一条，Anthropic 侧的连续 user 由转换层合并内容块处理。
        """
        view = self._messages.view()
        if (len(view) > 0 and view[-1].get("role") == "user"
                and not view[-1].get("images")):
            merged = dict(view[-1])
            merged["content"] = (view[-1].get("content") or "") + "\n\n" + content
            # 用 replace_last 而不是 replace_all：这只改一条消息，不是换会话，
            # 不能让代际 +1（否则守卫会把这次合并当成换会话，整轮 append 全丢）
            self._messages.replace_last(merged)
        else:
            self._messages.append_user(content)

    def append_user_images(self, images: list, content: str = "",
                           tool_call_id: str = "") -> None:
        """追加只携带图片引用的 user 消息（工具结果配图用），委托 MessageList。"""
        if self._generation_stale("工具结果配图"):
            return
        self._messages.append_user_images(images, content, tool_call_id)

    def append_assistant(self, content: str, tool_calls: Optional[list] = None,
                         thinking: Optional[str] = None,
                         thinking_signature: Optional[str] = None) -> bool:
        """追加 assistant 消息。返回 False = 代际已过期，本次写入被丢弃。

        返回值必须被调用方理会：agent_loop 在「已换会话」之后要立刻停下这一轮，
        继续跑只会白烧 token（结果全部写不进去）。
        """
        if self._generation_stale("assistant 消息"):
            return False
        self._messages.append_assistant(content, tool_calls, thinking, thinking_signature)
        return True

    def append_tool_result(self, tool_call_id: str, result: str) -> bool:
        """追加工具结果。返回 False = 代际已过期，本次写入被丢弃。

        这是**最要紧的一条**：会话被劈开的直接原因就是「切会话之后工具结果
        才 append 进来」—— 声明在旧会话、结果写进新会话，成为孤儿 tool_result。
        """
        if self._generation_stale("工具结果"):
            return False
        self._messages.append_tool_result(tool_call_id, result)
        return True

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        if self._generation_stale("中断结果"):
            return
        self._messages.append_interrupted_tools(tool_calls, completed_ids)

    # ── 内容审核拦截恢复（委托 MessageList）──

    def fold_largest_tool_result(self, folded_ids: set):
        """折叠最长的一条尚未折叠的 tool 结果。

        返回 (tool_call_id, 原字符数)；无可折叠项时 None（调用方据此以原错误收尾）。
        """
        return self._messages.fold_largest_tool_result(folded_ids)

    def drop_orphan_tool_results(self) -> list:
        """删掉无对应声明的 tool 结果（结构性 400 的修复手段）。"""
        return self._messages.drop_orphan_tool_results()

    # ── 修复方法（repair 逻辑原样保留，通过 view 读取）──

    def repair(self) -> None:
        """修复messages：打断后可能留下不完整的消息序列。

        1. **孤儿 tool 结果**（没有对应 tool_calls 声明）→ 删掉。这类消息会让
           Anthropic 协议整个请求 400，且删除是安全的（声明没了，结果无接收方）。
        2. assistant含tool_calls但没有对应的tool消息 → 补上tool("[用户中断]")
        3. 如果第2步修复了，且末尾是tool消息 → 补上assistant（API要求tool后不能直接跟user）

        顺序要求：必须先删孤儿再补结果。反过来的话，刚补的 `[用户中断]` 结果
        会因为「声明在后面某处被删了」而被误判成孤儿再删掉 —— 一轮工具调用
        白丢，而且补 assistant 的判据也会跟着失效。
        """
        msgs = self._messages

        # 1. 孤儿 tool 结果（声明在别的会话文件里 / 已被替换掉）
        dropped = msgs.drop_orphan_tool_results()
        if dropped:
            self._logger.warning(
                "core.message_manager",
                f"repair: 删除 {len(dropped)} 条无对应声明的工具结果"
                f"（声明与结果被劈到了不同会话文件）")

        # 2. 为未回复的tool_call补上空结果
        replied_ids = set()
        for msg in msgs.view():
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = bool(dropped)
        for msg in msgs.view():
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in replied_ids:
                        msgs.append_tool_result(tc_id, "[用户中断]")
                        replied_ids.add(tc_id)
                        repaired = True

        # 3. 只有在确实修复过时，才补 assistant
        if repaired and len(msgs) > 0 and msgs.view()[-1].get("role") == "tool":
            # 思考模式下请求末尾的 assistant 消息必须携带非空 thinking 块，
            # 否则 DeepSeek V4 返回 400（The content[].thinking ... must be passed back）
            msgs.append_assistant(SYNTHETIC_THINKING, thinking=SYNTHETIC_THINKING)

        if repaired:
            self._logger.info("core.message_manager", "repair: 修复了打断后的消息序列")

    # ── 压缩方法（逻辑原样保留，通过 view/to_list 读取）──

    def has_compressible_history(self, retain_tokens: int) -> bool:
        """保留预算内是否存在可被摘要替换的历史对话（handle_compress 与运行中自查共用判定）"""
        full_messages = self._messages.view().to_list()
        start = 0
        while start < len(full_messages) and full_messages[start].get("role") == "system":
            start += 1
        cut = select_cut_index(full_messages, retain_tokens)
        return not (start >= len(full_messages) or (cut is not None and cut <= start))

    def handle_compress(self, pending_input: Optional[str], system_prompt: str,
                        llm_client, cancel_check, on_interrupt, on_llm_error,
                        retain_tokens: int = 0, on_usage=None,
                        compress_max_tokens: Optional[int] = None) -> CompressResult:
        """
        处理上下文压缩。

        pending_input: 压缩后要以 user 消息收尾的新输入；None=手动压缩
        （/compact），不携带新输入。
        retain_tokens: 逐字保留的近期尾部预算（启发式估价token），0=全量压缩。
        摘要输入始终为全部历史；切点选择只决定压缩后保留哪些近期消息。
        compress_max_tokens: 压缩请求的输出上限（None=沿用主会话配置）。

        为什么压缩要单独给输出上限：压缩请求的输入是全部历史，本身已经接近
        窗口上限；若输出上限仍与正常轮次相同（如 128k），"历史 + 压缩输出"
        很容易超出窗口，压缩请求自己先溢出——压缩永远失败。给一个小预算后
        这件事在数学上不可能发生。

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
        if pending_input is None and not self.has_compressible_history(retain_tokens):
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

        # 发送压缩请求，收集AI输出（流迭代抛异常按 llm_error 处理：压缩失败
        # 不应阻塞对话，更不能让异常冒泡终止整个会话）
        summary_content = []
        llm_error = False
        # 必须照常带 tools 段：服务端前缀缓存顺序是 system → tools → messages，
        # 缺 tools 的请求与正常请求前缀在 tools 处分叉，压缩请求（全量历史，最贵）
        # 几乎全价。压缩是省上下文的手段，不能反过来烧钱。
        try:
            for chunk in llm_client.chat_stream(compress_messages,
                                                cancel_check=cancel_check,
                                                max_tokens=compress_max_tokens):
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
                    if chunk["finish_reason"] == "content_rejected":
                        # 压缩请求复刻全部历史：历史含触发内容风控的文本时必然
                        # 被拒。此处不折叠（压缩路径不持有消息表，折叠由 agent_loop
                        # 的恢复分支负责），据实归因，避免误报为"总结为空"
                        on_llm_error("压缩失败: 压缩请求被服务端内容审核拦截")
                        return CompressResult(False, reason="llm_error")
                if "content" in chunk and "tool_calls" not in chunk:
                    summary_content.append(chunk["content"])
                # 旁路记账：压缩调用同样计入费用日志（不触碰主会话统计）
                if "usage" in chunk and on_usage is not None:
                    try:
                        on_usage(chunk["usage"])
                    except Exception:
                        pass
        except Exception as e:
            on_llm_error(f"压缩失败: LLM调用出错({type(e).__name__}: {e})")
            return CompressResult(False, reason="llm_error")

        if llm_error:
            on_llm_error("压缩失败: LLM调用出错")
            return CompressResult(False, reason="llm_error")

        # llm 层在取消时静默结束流（不产出任何 chunk；已产出部分内容时同样
        # 直接结束）：无论内容多少，取消标记在 → 一律按取消处理——否则
        # "半截摘要"会落入成功路径替换全部历史（不可逆的信息损失）
        if cancel_check and cancel_check():
            on_interrupt()
            return CompressResult(False, reason="interrupted")

        summary = "".join(summary_content)

        # 校验总结结果（内存中直接校验）
        if not summary.strip():
            on_llm_error("压缩失败: 总结为空")
            return CompressResult(False, reason="empty_summary")

        # 先完整构建新会话（含用户问题），再原子替换旧会话
        tail = full_messages[cut:] if cut is not None else []
        # 尾部结构清洗（必须在下方"末尾角色"保护之前做）：
        # 1) 孤儿 tool（tool_call_id 在尾部内无对应声明）：API 对无 tool_use
        #    的 tool_result 会整体拒绝，压缩后的会话仍不可用——摘要请求覆盖
        #    全历史（含孤儿内容），此处直接从保留尾部剔除，压缩是清除这类
        #    坏结构的兜底机会
        # 2) 尾部内 system（技能注入等）：新会话重建本就会过滤（system 不进
        #    保留区）；提前过滤，否则末尾角色判断看的是即将消失的 system，
        #    "防连续 user"的合并/剔除保护被绕过
        if tail:
            declared: set = set()
            cleaned = []
            for _m in tail:
                _role = _m.get("role")
                if _role == "assistant":
                    for _tc in (_m.get("tool_calls") or []):
                        if isinstance(_tc, dict) and _tc.get("id"):
                            declared.add(_tc["id"])
                    cleaned.append(_m)
                elif _role == "tool":
                    if _m.get("tool_call_id") in declared:
                        cleaned.append(_m)
                elif _role != "system":
                    cleaned.append(_m)
            tail = cleaned
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
        # 先归档、再替换（顺序不能反：replace_all 之后旧列表就不在 _messages 里了）。
        # 归档范围 = 旧列表里「重建后消失」的消息：切点前的对话区、手动压缩
        # 被剔除的尾部 user、尾部清洗掉的孤儿 tool。保留尾部（tail）原样留在当前
        # 列表里，不入归档。
        # 系统提示词那条（下标 0 的 system，MessageList 用 system_prompt 构造）
        # 由新的 system_prompt + 摘要重建，不入归档——否则完整历史里会出现两份
        # 系统提示词。**只排除这一条**：/done 合并进父会话的「# 子会话 [X] 结论」、
        # /skill 注入的技能正文也都是 system 角色，但它们是历史内容，一刀切排除
        # 会让它们在展示/落盘里整段消失（P0，实测压缩后全库 grep 不到）。
        # 重复归档由 archive() 的 id 判重挡住。落盘/展示走 full_history()，模型
        # 上下文仍只看当前列表——压缩只优化「发给模型的上下文」，不该动用户看得
        # 见的历史。
        retained_ids = {id(m) for m in tail}
        self._messages.archive(
            [m for i, m in enumerate(full_messages)
             if id(m) not in retained_ids
             and not self._messages.is_system_prompt(i, m)])
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
