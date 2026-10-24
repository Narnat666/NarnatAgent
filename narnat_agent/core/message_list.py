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
# 注意「不发给服务端」≠「本地丢弃」：原文另存进消息的 folded_original 字段
# （转换层 pop，不进请求），见 fold_largest_tool_result。
FOLDED_TOOL_MARKER = "[已折叠: 原工具结果共{n}字符，疑似触发服务端内容审核，原文已存档]"

# 可折叠的最小长度：过低无收益（替换文案本身更长，反而增大上下文），
# 且会让"参数类 400"白跑折叠重试——低于此值一律视为无可折叠项
MIN_FOLDABLE_TOOL_CHARS = 200

# 压缩摘要消息的前缀。Compressor.build_new_session_messages 按它注入摘要，
# 载入会话时也靠它认出「当前上下文的摘要边界」：边界之前的历史属于归档
# （展示/落盘要保留，但不是发给模型的上下文）。
COMPACTION_SUMMARY_PREFIX = "# 上一轮对话成果"


def split_loaded_history(msgs: List[Dict[str, Any]], system_prompt: str
                         ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把落盘的全量历史拆成 (当前上下文, 归档)。

    落盘口径是 full_history() = 首条系统提示词 + 归档 + 当前，载入时必须拆回来：

    - 只把整个文件塞回「当前」会让压缩白做：压缩过的会话一重进上下文就从
      摘要态膨胀回全量历史，摘要还落在历史中间，下一次压缩再把整段（含旧摘要）
      重摘要一遍，摘要层层叠加（P1，实测 9→15 条并叠加摘要）。
    - 拆开后归档仍留在 full_history() 里（展示/落盘一字不少），但不发给模型。

    首条系统提示词：优先取下标 0；老版本文件把提示词写在归档之后（压缩归档先于
    提示词落盘），退化为「内容等于 system_prompt 的那条」；两者都没有就补一条
    ——保证任何路径下「首条 system 提示词存在且只有一条」（P0）。
    """
    msgs = list(msgs or [])
    head_idx = -1
    if msgs and msgs[0].get("role") == "system":
        head_idx = 0
    else:
        for i, m in enumerate(msgs):
            if m.get("role") == "system" and m.get("content") == system_prompt:
                head_idx = i
                break
    if head_idx < 0:
        return [{"role": "system", "content": system_prompt}] + msgs, []
    head = msgs[head_idx]
    rest = msgs[:head_idx] + msgs[head_idx + 1:]
    # 摘要边界 = 最后一条摘要系统消息（更早的摘要已被它整合替换，属归档）
    cut = -1
    for i, m in enumerate(rest):
        if (m.get("role") == "system"
                and str(m.get("content") or "").startswith(COMPACTION_SUMMARY_PREFIX)):
            cut = i
    current = [head] + (rest[cut:] if cut > 0 else rest)
    archived = rest[:cut] if cut > 0 else []
    # 同一份系统提示词出现多次（老版本把提示词连同归档重复落盘）：只留首条
    prompt = head.get("content")
    if isinstance(prompt, str) and prompt:
        current = [m for i, m in enumerate(current)
                   if i == 0 or not (m.get("role") == "system"
                                     and m.get("content") == prompt)]
        archived = [m for m in archived
                    if not (m.get("role") == "system" and m.get("content") == prompt)]
    return current, archived


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
        self._system_prompt = system_prompt
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        # 压缩掉的历史原文。压缩改的是「发给模型的上下文」（_messages），
        # 用户看得见的历史（展示/落盘）必须是 归档 + 当前，见 full_history()。
        self._archived: List[Dict[str, Any]] = []
        # 已归档消息的对象 id 集合（判重口径，见 archive）
        self._archived_ids: Set[int] = set()
        # 会话代际：每换一次会话内容 +1（见 generation 属性）
        self._generation: int = 0

    @property
    def generation(self) -> int:
        """会话代际：`replace_all` 每调一次 +1。

        用途是**写入守卫**：agent_loop 在主循环开始时绑定当时的代际，之后每次
        append 前比对。不一致 = 这一轮已经被「切会话」打断，它算出来的结果属于
        上一段对话，绝不能写进现在这个列表。

        为什么必须有（P0，实测）：工具还在执行时用户切会话，`_cancel_and_wait_turn`
        只等 3 秒；串口/SSH 那种底层超时几十秒的工具等不到收口，于是：
          · 工具的**声明**（assistant.tool_calls）已经在上一段对话里落盘
          · 工具的**结果**在这之后才 append，而 MessageList 已被换成新会话
        两边分处不同会话文件 → 结果成了「没有声明的孤儿 tool_result」→
        Anthropic 协议直接 400（`each tool_result block must have a
        corresponding tool_use block`），且该会话从此每条消息都发不出去。

        只在 `replace_all` 里 +1，**压缩路径不算换会话**（clear_and_rebuild /
        compress_and_rebuild 不动代际）：压缩前后是同一段对话，正在跑的轮次
        应当继续正常写入。
        """
        return self._generation

    # ── 只读接口 ──

    @property
    def system_prompt(self) -> str:
        """构造本列表时用的系统提示词（首条 system 的正文，缺失时也由它兜底）"""
        return self._system_prompt

    def is_system_prompt(self, index: int, msg: Dict[str, Any]) -> bool:
        """这条是不是「系统提示词」——MessageList 用 system_prompt 构造的首条 system。

        判据是「是不是首条」，**不是**"role 是不是 system"：/done 合并进父会话的
        「# 子会话 [X] 结论」、/skill 注入的技能正文同样是 system 角色，它们是
        历史内容，压缩时必须进归档（一刀切会让它们在展示/落盘里整段消失）。
        用下标而非内容比较：会话载入后首条 system 是该会话自己那份提示词。
        """
        return index == 0 and msg.get("role") == "system"

    def view(self) -> MessageView:
        """返回只读视图。零拷贝，但调用方无法修改列表结构。"""
        return MessageView(self._messages)

    def full_history(self) -> List[Dict[str, Any]]:
        """完整历史 = 首条系统提示词 + 归档 + 当前其余，返回**新列表**（供展示/落盘用）。

        调用方可以对这个新列表做任何操作（append/切片/序列化），但不得把它
        当成内部状态回写——列表是新建的，元素仍与内部同源 dict。

        首条系统提示词必须排在最前：它是最早的消息，而压缩时提示词留在当前
        列表里、被它替换掉的历史进了归档。若按「归档 + 当前」拼接，落盘后的
        顺序会变成「旧历史 → 提示词 → 摘要 → 保留尾部」——首条不是 system，
        列表中间多出一条 system；载入这种文件会让请求丢掉提示词（P0-2）、
        摘要消息夹在历史中游被再次摘要（P1）。顺序即时间顺序：提示词（最早）
        → 归档 → 当前其余（摘要 + 保留尾部 + 后续对话）。

        按对象 id 去重：压缩是「先 archive() 再 replace_all()」两步，这中间的
        瞬间两个列表会同时持有同一批消息；后台自动保存线程恰好此刻取快照就会
        把这一段写两遍。去重把这个窗口消掉（正常状态下不会有重复对象）。
        """
        out: List[Dict[str, Any]] = []
        seen: Set[int] = set()
        head = self._messages[0] if self._messages and self._messages[0].get("role") == "system" else None
        for m in ([head] if head is not None else []) + self._archived + \
                [m for m in self._messages if m is not head]:
            key = id(m)
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
        return out

    def __len__(self) -> int:
        return len(self._messages)

    # ── 受控修改接口（唯一修改入口）──

    def append_system(self, content: str) -> None:
        """追加系统消息"""
        self._messages.append({"role": "system", "content": content})

    def append_user(self, content: str) -> None:
        """追加用户消息"""
        self._messages.append({"role": "user", "content": content})

    def append_user_images(self, images: list, content: str = "",
                           tool_call_id: str = "") -> None:
        """追加一条只携带图片引用的 user 消息（工具结果配图用）。

        content 保持字符串形态（"图片标记 + 发送端二次适配"的核心）：
        图片引用挂在 images 字段上，发送端按"图片状态 x 模型能力"决定展开成
        真实图片块、降级成文本、还是丢弃。会话里永远不存图片字节。

        tool_call_id: 该图片归属的工具调用 id。Anthropic 协议要求
        tool_result 与 tool_use 紧邻且 tool_result 必须是 user 消息的首块，
        因此转换时会把图片**并入对应的 tool_result**（源码见 llm.py
        _convert_messages）；OpenAI 协议的 tool 消息只能字符串，只能另起
        user 消息。两种协议各自的限制在转换层分流，消息层只记录归属。

        引用格式见 core/attachment.py: make_ref。
        """
        refs = [dict(r) for r in (images or []) if isinstance(r, dict)]
        if not refs:
            return
        msg = {
            "role": "user",
            "content": content or "（图片内容见附件）",
            "images": refs,
        }
        if tool_call_id:
            msg["image_for_tool_call"] = tool_call_id
        self._messages.append(msg)

    def append_user_with_images(self, content: str, images: list) -> None:
        """追加一条既有文本又有图片的用户消息（用户自带输入用）。"""
        msg = {"role": "user", "content": content}
        refs = [dict(r) for r in (images or []) if isinstance(r, dict)]
        if refs:
            msg["images"] = refs
        self._messages.append(msg)

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

        **原文不丢**：折叠前把原文存进 `folded_original` 字段（本地留存，
        转换层发请求时会 pop 掉，不占服务端上下文）。原来直接丢弃原文，
        而折叠是被**误判**触发的（见 llm._classify_400），一次误判就永久
        毁掉用户几条工具结果（实测：896/268/204/220 字符各一条）。
        用户要看原文时还能从这里取回来。
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
        original = msg.get("content") or ""
        # 已经折过一次就不再覆盖存档（保留最早那份原文）
        if "folded_original" not in msg:
            msg["folded_original"] = original
        msg["content"] = FOLDED_TOOL_MARKER.format(n=best_len)
        folded_ids.add(tc_id)
        return tc_id, best_len

    def drop_orphan_tool_results(self) -> List[str]:
        """删掉「没有对应 tool_calls 声明」的 tool 消息。返回被删的 tool_call_id。

        什么时候会有孤儿：工具正在执行时用户切会话/建分支，`assistant`（带
        tool_calls）与它的 `tool` 结果被写进了**不同文件**（实测：父会话
        b.json 有 4 条结果、子会话 b/b_g.json 有那条声明）。合并回来时
        就只有结果没有声明。

        为什么必须删而不是留着：Anthropic 协议里 tool_result 必须能在
        **紧邻的前一条 assistant 消息**里找到对应 tool_use，否则整份请求
        400（`unexpected tool_use_id found in tool_result blocks`）。
        留着它 = 这个会话从此每条消息都发不出去，用户只能弃用。

        删掉是安全的：声明都没了，这条结果已经没有任何接收方。
        """
        declared: Set[str] = set()
        for msg in self._messages:
            for tc in (msg.get("tool_calls") or []):
                tid = tc.get("id")
                if tid:
                    declared.add(tid)
        keep: List[Dict[str, Any]] = []
        dropped: List[str] = []
        for msg in self._messages:
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id") or ""
                if tid and tid not in declared:
                    dropped.append(tid)
                    continue
            keep.append(msg)
        if dropped:
            self._messages = keep
        return dropped

    def archive(self, msgs) -> int:
        """把一批消息追加进归档（归档的唯一入口）。返回实际归档条数。

        判重口径：消息对象 id（不是内容指纹）。归档列表持有这些 dict 的引用，
        对象不会在归档期内被回收，id 自然不会被复用——所以 id 命中必然是
        「同一批消息」，比内容指纹严格：内容相同的两条不同消息（例如两次
        「继续」）不会被误判成重复而丢弃。
        """
        added = 0
        for m in msgs or []:
            key = id(m)
            if key in self._archived_ids:
                continue
            self._archived_ids.add(key)
            self._archived.append(m)
            added += 1
        return added

    def clear_archive(self) -> None:
        """清空归档（会话切换/新建时用：归档属于上一段会话，不可跨会话拼接）"""
        self._archived = []
        self._archived_ids.clear()

    def replace_all(self, new_messages: List[Dict[str, Any]]) -> None:
        """原子替换全部消息（会话切换时使用）。

        单次引用赋值（GIL 下原子）：并发读者要么看到旧列表、要么看到新列表，
        不会撞见 clear()+extend() 两步之间的空/半列表（后台保存线程读到空
        列表会把会话清空落盘）。

        注意：本方法不动归档（压缩路径正是「先 archive 再 replace_all」）。
        会话切换必须走 SessionManager.replace_messages()，它先 clear_archive()。

        代际 +1 也在这里：这是「换了一段对话」的唯一汇聚点（切换/新建/删除/
        载入/压缩后重建都经过它）。agent_loop 靠代际判断自己的结果还该不该写。
        """
        self._messages = list(new_messages)
        self._generation += 1

    def replace_last(self, msg: Dict[str, Any]) -> None:
        """替换最后一条消息（**不是**换会话，代际不变）。

        专供「合并连续 user」这类就地改写：它和 replace_all 长得像，
        但语义完全不同 —— 代际守卫靠 +1 识别「换了一段对话」，
        就地改一条消息如果也算，守卫会把自己的合并操作当成换会话，
        之后整轮 append 全被判定过期而丢弃（实测会静默丢掉整轮回复）。
        """
        if not self._messages:
            self._messages = [msg]
            return
        self._messages[-1] = msg

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
