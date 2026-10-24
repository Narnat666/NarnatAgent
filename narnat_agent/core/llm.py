"""
LLM调用层 —— 双协议支持（OpenAI兼容 + Anthropic兼容），流式输出，token计数

通过 AIConfig.protocol 显式选择协议：
- "anthropic" → AnthropicBackend（/v1/messages 端点，x-api-key 认证）
- "openai"    → OpenAIBackend（/chat/completions 端点，Bearer 认证）

上层统一使用 OpenAI 格式的 messages/tool_calls，AnthropicBackend 内部做双向转换。
Thinking 参数通过 THINKING_PARAM_MAP 映射表动态构造，不再硬编码。
两个后端统一使用 httpx，无 requests 依赖。
"""

import json
import queue
import random
import re
import threading
import time
import httpx
from typing import List, Dict, Any, Iterator, Optional

from ..config.loader import AIConfig
from ..config.defaults import (
    DEFAULT_THINKING_PASSBACK, resolve_thinking_params, resolve_thinking_passback,
    resolve_image_support,
)
from .interrupt import register_abort
from .message_list import SYNTHETIC_THINKING


def _strip_surrogates(obj):
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_surrogates(v) for v in obj]
    return obj

# ── 共享状态与重试参数已收敛为 LLMClient 类成员（见 LLMClient 类体）──
# - 中断句柄: LLMClient._active_response
# - 流队列哨兵: LLMClient._STREAM_END
# - 重试次数: LLMClient._max_network_retries / _max_rate_retries（set_retry_count 同步）


def retry_sleep(attempt: int, cancel_check=None) -> bool:
    """指数退避休眠，带 jitter 和中断检查。返回 False 表示用户取消。"""
    base = LLMClient.RETRY_BACKOFF_BASE[min(attempt, len(LLMClient.RETRY_BACKOFF_BASE) - 1)]
    jitter = base * 0.25 * (random.random() * 2 - 1)
    sleep_time = max(0, base + jitter)
    deadline = time.time() + sleep_time
    while time.time() < deadline:
        if cancel_check and cancel_check():
            return False
        time.sleep(min(0.2, deadline - time.time()))
    return True


_CANCEL_POLL_SECONDS = 0.05
# 取消标记轮询间隔（秒），与流式接收的轮询粒度一致

_SEND_THREAD_NAME = "narnat-llm-send"
# 阻塞发送线程名（排障用：线程转储里可直接定位请求发送线程）

_NO_RESULT = object()
# 结果槽初值哨兵：区分"阻塞调用尚未产出结果"与"结果为 None"


def safe_close(target) -> None:
    """尽力关闭句柄（异常忽略）——中断路径不得因关闭失败影响收敛。"""
    try:
        target.close()
    except Exception:
        pass


# ── SSL 上下文惰性单例：每请求新建 Client，但复用同一 SSLContext ──
# httpx 默认 verify=True 时每个 Client 构造都重新加载证书（certifi CA bundle
# 实测 1.0~1.3s/次、无缓存）；预构建一次后 Client 构造开销 ≈ 0.001s。
# 信任集 = 系统默认证书 + certifi（与 httpx 默认信任行为一致）。
_SSL_CONTEXT = None


def _get_ssl_context():
    """惰性构建并复用全局 SSLContext（首次调用构建，之后直接返回）。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        import ssl
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT


def run_cancelable(do_block, cancel_check, on_cancel=None,
                   poll_seconds: float = _CANCEL_POLL_SECONDS):
    """在子线程执行阻塞调用；主流程以 poll_seconds 粒度轮询取消标记。

    请求发送（建连、等服务端响应头）阻塞期间生成器体卡在该调用上，流循环的取消
    轮询还不可达；而中断触发的 close() 对"连接建立中"的在途请求是空操作，请求只能
    自己跑到响应头到达才收敛（实测 5~11.5 秒）。本原语把阻塞调用移入子线程，主流程
    轮询取消标记：取消命中即尽力掐断并立即返回，不再等阻塞调用自行结束。

    返回 (cancelled, result, error)：
    - cancelled=True：主流程立即返回（不等子线程）；on_cancel() 已调用（尽力掐断，
      对已建立连接有效）；子线程在阻塞解除后自毁 result（close 幂等，双保险）。
    - cancelled=False：result 或 error 二选一（阻塞调用的结果）。
    正常完成即唤醒主流程返回，不引入固定延迟；cancel_check 为 None 时退化为纯等待，
    与直接调用等价。
    """
    done = threading.Event()
    cancelled = threading.Event()
    box = {"result": _NO_RESULT, "error": None}

    def _worker() -> None:
        try:
            box["result"] = do_block()
        except BaseException as exc:  # 非 Exception 基类异常同样交回主流程上抛
            box["error"] = exc
        finally:
            done.set()
            # 兜底自毁：取消已置位且阻塞调用随后才产出结果时由子线程补关。
            # close 幂等，任何时序下结果至多被关闭两次（主线程竞态窗口一次、此处一次）
            if cancelled.is_set() and box["result"] is not _NO_RESULT:
                safe_close(box["result"])

    threading.Thread(target=_worker, name=_SEND_THREAD_NAME, daemon=True).start()

    while True:
        # 完成即唤醒（正常路径零额外延迟）；超时说明阻塞未结束，此时才查取消标记
        if done.wait(poll_seconds):
            break
        if cancel_check is not None and cancel_check():
            cancelled.set()
            if on_cancel is not None:
                try:
                    on_cancel()
                except Exception:
                    pass
            # 关闭竞态窗口：取消置位与阻塞完成同时发生时结果可能已产生，主线程补关一次
            if box["result"] is not _NO_RESULT:
                safe_close(box["result"])
            return True, None, None

    if box["error"] is not None:
        return False, None, box["error"]
    return False, box["result"], None


def _retry_notice(attempt: int, max_retries: int, reason: str = "网络连接失败") -> Dict[str, str]:
    """重试的用户提示事件（与响应流中断重试提示风格一致）。

    由 agent_loop 渲染到输出流，不计入 AI 回复内容与消息历史。
    """
    base = LLMClient.RETRY_BACKOFF_BASE[min(attempt - 1, len(LLMClient.RETRY_BACKOFF_BASE) - 1)]
    return {
        "retry_notice": f"\n{reason}，{base}s 后重试（{attempt}/{max_retries}）\n"
    }


def _classify_stream_error(e: Exception) -> Dict[str, str]:
    """把流读取异常归类为上层可识别的中断信息。"""
    if isinstance(e, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(e, httpx.TransportError):
        kind = "network"
    else:
        # OpenAI SDK 用自身异常类型包装底层连接错误（APITimeoutError 是 APIConnectionError 子类）
        try:
            from openai import APIConnectionError, APITimeoutError
        except ImportError:
            APIConnectionError = APITimeoutError = ()
        if isinstance(e, APITimeoutError):
            kind = "timeout"
        elif isinstance(e, APIConnectionError):
            kind = "network"
        else:
            kind = "unknown"
    return {"kind": kind, "detail": str(e)[:200]}


def _is_retryable_http(status: int) -> bool:
    return status in (408, 409) or status >= 500


def _as_text(content) -> str:
    """把消息正文规整成字符串 —— 转换层唯一的出口，两套协议都走它。

    ## 为什么必须有这道

    会话文件是普通 JSON，用户可以手改、同步工具可以写坏、旧版本也可能留下脏数据。
    `content` 一旦不是字符串，原实现会把它**原样塞进请求体**：

        {"role": "user", "content": 12345}          → 真端点 422（DeepSeek 实测）
        {"role": "user", "content": true}           → 同上
        {"role": "user", "content": {"text": "…"}}  → 同上

    后果不是"这一条发不出"，而是**该会话从此永久发不出去**：坏消息总在历史里，
    每一轮新请求都会重发它，用户看到的是"一发消息就报错"，且没有任何线索
    （`session/switch` 回执还是正常的、条数也对）。

    更隐蔽的一种：`content: 0` 或 `null`。原实现 `content or ""` 把 0 也吞成空串，
    **静默丢掉一条提问** —— 不报错，但用户会发现"我明明问过这句"。

    ## 规整规则（优先保内容，其次可读）

      · 字符串 → 原样
      · None / "" → ""
      · 0 / False → "0" / "False"   ← 不吞掉（原实现的 bug 就在这）
      · 列表 → 若是标准内容块 `[{"text": …}]` 就拼出文本；否则 JSON 序列化
      · 字典 / 其它 → JSON 序列化（sort_keys 保证同一份数据每次得到同一串，
        避免"看起来没变但请求体变了"导致上游缓存失效）

    返回的一定是 `str`，所以调用方不需要再判类型。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, bool):
        # 不用 str(True)="True"/str(False)="False" 之外的花样：保持可预测
        return str(content)
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, (list, tuple)):
        # 标准 Anthropic 内容块列表：只取 text 块拼起来（其余块类型在
        # "content 本该是字符串" 的位置上没有意义）。拼不出东西就整体序列化。
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(list(content), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(content)
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


# ── Anthropic 转换层的"开放位置 assistant"占位思考块 ──
# DeepSeek 思考模式校验：请求中处于"开放位置"的 assistant——尾部 assistant、
# 或后面紧跟另一条 assistant（连续链前序）——必须携带 thinking 块，否则 400
# "content[].thinking ... must be passed back"。正常对话的 assistant 后均跟
# user（含 tool_result），不触发；触发时补非空占位（不补必 400；占位属协议
# 合规填充，与"思考回传"开关的语义无关）。
_PLACEHOLDER_THINKING = "（占位：该轮思考未回传）"

# ── 上下文超限特征（400 响应归类，供 agent_loop 溢出恢复路径使用）──
# 注意：不使用裸 "too long"——任何含该词的 400（如字段/参数过长）都会被
# 误判为上下文超限并吞掉服务端真实错误，只保留上下文语境的短语匹配
_CONTEXT_OVERFLOW_HINTS = (
    "context length",
    "context window",
    "context_length_exceeded",
    "context is too long",
    "prompt is too long",
    "exceeds the maximum",
    "maximum context",
    "input length",
    "超出上下文",
    "上下文长度",
    "超出限制",
    "超限",
)


def _is_context_overflow(text: str) -> bool:
    """识别 API 400 响应中的上下文超限特征。

    提供商措辞可能随版本变化，多特征匹配兜底；命中后由上层
    （agent_loop）归类为 context_overflow 并走压缩恢复，而非普通错误。
    """
    lowered = text.lower()
    return any(hint in lowered for hint in _CONTEXT_OVERFLOW_HINTS)


# ── 400 的另外两类：结构错误 vs 内容风控 ──
# 原来**所有** 400 都被当成「内容审核拦截」，于是走折叠恢复（改写工具结果
# 原文后重发）。实测代价极大：用户切分支后消息被劈到两个文件，上下文里
# 出现「tool_result 找不到对应 tool_use」的结构错误，服务端回的是
#   invalid_request_error: unexpected `messages.34.content.4: tool_use_id` ...
# 我们把它当风控，白折了 5 条工具结果原文（用户数据被永久改写），
# 最后还报「请求被内容审核拦截」——用户以为自己的内容违规了，实际是格式坏了。
#
# 分类原则：**只认明确证据**。命中结构错误特征 → 修结构；命中风控特征 →
# 才折叠；两样都不命中 → 当普通错误如实上报（不动数据）。
_STRUCTURE_ERROR_HINTS = (
    "invalid_request_error",
    "tool_use_id",
    "tool_result",
    "tool_use",
    "tool_calls",
    "must have a corresponding",
    "unexpected `messages",
    "messages.",          # OpenAI/Anthropic 都按 messages.N 定位出错的条目
    "roles must alternate",
    "invalid role",
    "must be passed back",
    "content[].thinking",
)

# 内容风控的措辞。宁可漏判（当普通错误报出来）也不要误判——误判会改写数据。
_CONTENT_REJECT_HINTS = (
    "content_filter",
    "content filter",
    "content_policy",
    "sensitive",
    "responsible ai",
    "risk control",
    "内容安全",
    "内容审核",
    "违规",
    "不当内容",
    "风控",
)


def _classify_400(text: str) -> str:
    """把 400 归类为 'context_overflow' | 'invalid_structure' | 'content_rejected' | ''

    返回空串 = 认不出来，交给调用方按普通错误处理（**不改数据**）。
    判定顺序要紧：结构错误优先。两边的特征可能同时出现（服务端会把整份
    请求摘要放进报错），而「格式坏了」这个结论比「内容有问题」更可操作，
    也更能保护用户数据不被折叠误伤。
    """
    lowered = (text or "").lower()
    if _is_context_overflow(lowered):
        return "context_overflow"
    if any(h in lowered for h in _STRUCTURE_ERROR_HINTS):
        return "invalid_structure"
    if any(h in lowered for h in _CONTENT_REJECT_HINTS):
        return "content_rejected"
    return ""


# ── 认证类失败特征（401 响应归类，给出可操作的短提示）──
# 只给结论和动作：服务端 401 报文（几百字符的英文 JSON）对用户没有任何用处，
# 铺在面板里反而把「去填 Key」这个唯一动作淹没。
_AUTH_FAIL_LINE = "[错误: AI调度失败，请检查 narnat.json 配置文件]"
_AUTH_ERROR_HINTS = ("401", "authentication", "unauthorized")


def _auth_fail(status, text: str = "") -> str:
    """认证类失败 → 简短整行提示；其余返回空串。

    调用方写法是 `_auth_fail(...) or 原文错误行`：命中就不带英文报文，未命中则
    保持原有文案（网络错误、限流等仍要给出原始信息便于排查）。

    只有 HTTP 401 才有资格**整行替换**原文——那才是"密钥不对"这个确切结论。
    非 401 只靠报文里的 401/authentication/unauthorized 子串判断时，命中并不
    等于认证失败（代理返回的 HTML 错误页、网关的 "unauthorized" 提示、甚至 URL
    里带 401 都会撞上），此时把原文吞掉会让用户查无可查。所以这类保留原文，
    只在前面点一句结论。
    """
    if status == 401:
        return _AUTH_FAIL_LINE
    lowered = str(text).lower()
    if any(h in lowered for h in _AUTH_ERROR_HINTS):
        return "[错误: AI调度失败，请检查 narnat.json 配置文件]" + str(text)[:200]
    return ""


def _user_has_tool_result(user_msg: Dict[str, Any]) -> bool:
    """转换后的 user 消息内容块里是否含有 tool_result 块。"""
    content = user_msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


# ── 图片块展开（发送端二次适配的唯一实现处）──
# 历史里图片只以引用形式存在（msg["images"]，见 core/attachment.py），
# 每次请求前按"图片状态 x 模型能力"决定这一刻怎么发：
#   副本在 + 模型支持   → 句柄文本 + 真实图片块
#   副本在 + 模型不支持 → 句柄文本 + 一句"未发送"说明
#   副本丢失            → 句柄文本 + 一句"已丢失"说明
# 三种情况都**不改历史**：切回支持识图的模型，同一份历史立刻恢复发图。
_IMAGE_UNSUPPORTED_TEXT = "[图片未发送：当前模型不支持图片输入]"


def _load_image_bytes(data_dir: str, ref: Dict[str, Any]):
    """取图片 base64 + 媒体类型；失败返回 (None, 原因文本)。

    走附件库的进程内缓存：多图多轮对话每轮都要重新拼请求，
    没有缓存就是每轮几十 MB 的重复读盘+编码。
    data_dir 是 <项目>/.narnat/data，附件库在其下的 attachments/。
    """
    if not data_dir:
        return None, "[图片不可用：附件库路径未配置]"
    from . import attachment as _attachment
    root = _attachment.attachment_root(data_dir)
    try:
        b64 = _attachment.load_base64_cached(root, ref.get("id") or "")
    except _attachment.ImageUnavailable as e:
        return None, "[图片已丢失：%s。如仍需要该图，请让用户重新提供 %s]" % (
            e, ref.get("name") or ref.get("id") or "原文件")
    return b64, ""


def _resolve_images(refs, data_dir: str, supports_image: bool):
    """把一组图片引用展开成 (可用图列表, 降级说明文本列表)。

    可用图列表元素：(media_type, base64)。
    refs 直接接受 msg["images"] 的值（调用方通常已把字段 pop 出来）。
    """
    if not isinstance(refs, list) or not refs:
        return [], []

    available, notes = [], []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        if not supports_image:
            notes.append(_IMAGE_UNSUPPORTED_TEXT)
            continue
        b64, reason = _load_image_bytes(data_dir, ref)
        if b64 is None:
            notes.append(reason)
            continue
        available.append((ref.get("media_type") or "image/png", b64))
    return available, notes


def abort_active_llm_request():
    """关闭当前LLM请求的HTTP连接。"""
    resp = LLMClient._active_response
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass


register_abort(abort_active_llm_request)


def _iter_to_queue(iterator, q, err_box=None, data_ts=None):
    """后台线程：将迭代器的元素逐个放入队列，最后放入LLMClient._STREAM_END。

    迭代异常（如服务端中途断开）记录到 err_box，供主线程判断流是否被中断；
    不再静默吞掉，避免把截断响应当作正常完成。
    data_ts: 最近收到数据的时间戳（单元素列表，看门狗用）。
    err_box 已有内容（看门狗已记录）时不再重复追加。
    """
    try:
        for item in iterator:
            if data_ts is not None:
                data_ts[0] = time.time()
            q.put(item)
    except Exception as e:
        if err_box is not None and not err_box:
            err_box.append(e)
    finally:
        q.put(LLMClient._STREAM_END)


# ═══════════════════════════════════════════════════════════════
# LLM 客户端
# ═══════════════════════════════════════════════════════════════

class LLMClient:
    """LLM客户端，通过 config.protocol 选择 OpenAI 或 Anthropic 协议。"""

    # ── 类级共享状态与参数（进程内唯一客户端，两个后端共用）──
    _active_response = None                    # ESC中断句柄：当前活跃的HTTP连接/流
    _STREAM_END = object()                     # 流队列哨兵
    RETRY_BACKOFF_BASE = [1, 2, 4, 8, 8]       # 指数退避基数（秒）
    _STREAM_STALL_SECONDS = 180.0              # 流式静默超此值视为挂死，主动断连
    _max_network_retries = 3                   # 网络/服务端错误重试上限
    _max_rate_retries = 5                      # 429 限流重试上限（与网络重试同源同值）

    @classmethod
    def set_retry_count(cls, n: int) -> None:
        """设置LLM重试次数（assembly 初始化 + agent_loop 每轮从配置同步），网络与429统一控制"""
        cls._max_network_retries = max(1, min(n, 10))  # 限制1-10
        cls._max_rate_retries = max(1, min(n, 10))     # 429 与网络重试同源同值

    def __init__(self, config: AIConfig, logger=None, max_output_tokens: int = 128000,
                 tool_definitions: list = None, data_dir: str = ""):
        self._config = config
        self._logger = logger
        # 附件库根目录（<项目>/.narnat/data）：发请求时把图片引用展开成真实图片块
        self._data_dir = data_dir
        # 复制一份：set_goal_tool 动态增删 GoalComplete 时不影响调用方持有的原列表
        self._tool_defs = list(tool_definitions or [])
        self._max_output_tokens = max_output_tokens
        self.set_retry_count(config.retry_count)

        # 协议由 config.protocol 显式指定
        protocol = config.protocol
        self._protocol = protocol

        if protocol not in ("anthropic", "openai"):
            raise ValueError(f"不支持的协议: {protocol}（可选: anthropic / openai）")
        if protocol == "anthropic":
            self._backend = _AnthropicBackend(config, self._tool_defs, logger, max_output_tokens,
                                              data_dir=data_dir)
        else:
            self._backend = _OpenAIBackend(config, self._tool_defs, logger, data_dir=data_dir)

    def chat_stream(self, messages: List[Dict[str, Any]], no_tools: bool = False,
                    no_thinking: bool = False, cancel_check=None,
                    tool_defs: Optional[list] = None,
                    model: Optional[str] = None,
                    max_tokens: Optional[int] = None) -> Iterator:
        """流式对话。tool_defs/model/max_tokens 为请求级覆盖（None=用实例默认）：
        验证器等独立请求可不触碰主会话工具表、模型配置与输出上限，只影响本次请求。
        """
        return self._backend.chat_stream(_strip_surrogates(messages), no_tools=no_tools,
                                         no_thinking=no_thinking, cancel_check=cancel_check,
                                         tool_defs=tool_defs, model=model,
                                         max_tokens=max_tokens)

    def set_goal_tool(self, enabled: bool) -> None:
        """动态注入/移除 GoalComplete 工具定义。

        目标模式（/goal on）开启时注入，关闭时移除；普通模式不向 LLM 暴露该工具。
        幂等：重复开启/关闭不会重复添加或报错。
        """
        # 局部导入避免模块顶层循环依赖
        from ..tools.goal_complete import DEFINITION as _GOAL_DEF
        name = _GOAL_DEF["function"]["name"]
        exists = any(
            d.get("function", {}).get("name") == name
            for d in self._tool_defs
        )
        if enabled and not exists:
            self._tool_defs.append(_GOAL_DEF)
        elif not enabled and exists:
            self._tool_defs[:] = [
                d for d in self._tool_defs
                if d.get("function", {}).get("name") != name
            ]

    def add_tool_definitions(self, definitions: list) -> None:
        """热追加工具定义（MCP 运行时连接后调用）。按名称去重，幂等。

        与 set_goal_tool 同机制：_tool_defs 与后端共享同一列表对象，
        每轮请求现读 → 追加后 AI 下一轮即可见。
        """
        existing = {d.get("function", {}).get("name") for d in self._tool_defs}
        for definition in definitions or []:
            name = definition.get("function", {}).get("name")
            if name and name not in existing:
                self._tool_defs.append(definition)
                existing.add(name)

    def remove_tool_definitions(self, names) -> None:
        """热移除工具定义（MCP 断开时调用）"""
        names = set(names or ())
        if not names:
            return
        self._tool_defs[:] = [
            d for d in self._tool_defs
            if d.get("function", {}).get("name") not in names
        ]

    def set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        """插件工具开关热生效（/plugin 命令）：同步注册表开关状态 + 本实例工具表。

        关闭 = 该工具定义不再发给 LLM；开启 = 从注册表取回定义（MCP 含 mcp__ 动态工具）。
        只增删该插件相关项，GoalComplete 等其它项原样保留（不得全量重建）。
        返回是否命中已知插件名。
        """
        # 局部导入避免模块顶层循环依赖
        from ..tools import registry as _registry
        if not _registry.set_plugin_enabled(name, enabled):
            return False
        targets = set(_registry.plugin_definition_names(name))
        self._tool_defs[:] = [
            d for d in self._tool_defs
            if d.get("function", {}).get("name") not in targets
        ]
        if enabled:
            self.add_tool_definitions(_registry.get_plugin_definitions(name))
        return True

    @property
    def raw_sse(self):
        if hasattr(self._backend, '_last_raw_sse'):
            return list(self._backend._last_raw_sse)
        return None


# ═══════════════════════════════════════════════════════════════
# OpenAI 兼容后端
# ═══════════════════════════════════════════════════════════════

class _RequestScope:
    """abort路径的请求级句柄：掐断在途请求（尽力）+ 标记客户端重建（不污染后续）。

    旧实现把共享客户端自身当作中断句柄，中断即关闭共享客户端，导致打断一次后
    整局对话全部失败（APIConnectionError）。本句柄把"关闭"变成请求级动作：中断
    只掐断本次在途请求并把客户端标记为待重建，由后端在下次请求前换新。
    """

    def __init__(self, backend: "_OpenAIBackend", client) -> None:
        self._backend = backend
        self._client = client

    def close(self) -> None:
        """abort()调用：标记客户端待重建并尽力掐断在途请求。"""
        self._backend._invalidate_client(self._client)


class _OpenAIBackend:
    """OpenAI SDK 后端"""

    def __init__(self, config, tool_defs, logger, data_dir=""):
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._data_dir = data_dir
        self._client_lock = threading.Lock()
        # 中断标记：请求级scope被关闭后置位，下次请求前重建客户端
        self._need_rebuild = False
        self._client = self._build_client()

    def _build_client(self):
        """构造SDK客户端（底层自动重试关闭，重试自管）。"""
        from openai import OpenAI
        return OpenAI(
            api_key=self._config.api_key,
            base_url=self._config.base_url,
            max_retries=0,
        )

    def _ensure_client(self):
        """取本轮请求使用的客户端；中断标记了重建时先换新（共享客户端不被污染）。

        上一轮请求被中断（abort/取消）时旧客户端已被关闭，此处重建使后续轮次继续
        可用——旧实现中断关闭共享客户端后整局对话全部失败。
        """
        with self._client_lock:
            if self._need_rebuild:
                self._need_rebuild = False
                safe_close(self._client)
                self._client = self._build_client()
            return self._client

    def _invalidate_client(self, client) -> None:
        """请求级中止：标记客户端待重建（后续请求换新）并尽力掐断在途请求。"""
        with self._client_lock:
            self._need_rebuild = True
        safe_close(client)

    def _prepare_messages(self, messages, model=None):
        """内部消息 → OpenAI 协议请求消息。

        内部 assistant 消息可携带 thinking 字段（思考回传的通用内部表示）。
        学官方 harness 规则（serialize.ts）：reasoning_content 仅工具调用轮回传，
        纯文本轮被忽略，直接剥离省 token：
        - reasoning_content（DeepSeek/Kimi/GLM 查表命中）：assistant 含 tool_calls
          时才 thinking → 顶层 reasoning_content 字段
        - 其余（none / 纯文本轮 / 开关关闭）：剥离 thinking 字段
        model: 本次请求实际生效模型（请求级覆盖），查表用

        图片：内部 msg["images"] 是引用列表（不是字节），此处按当前模型能力展开。
        OpenAI 协议的图片块形如
            {"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}
        content 因此从 str 变成块列表 —— 只在**本条消息确实有可发图**时改变形态，
        纯文本消息仍是 str，与旧请求逐字节一致（保住 KV cache 前缀）。
        """
        model = model or self._config.model
        use_rc = (
            self._config.thinking_enabled
            and getattr(self._config, "thinking_passback", DEFAULT_THINKING_PASSBACK)
            and resolve_thinking_passback("openai", model) == "reasoning_content"
        )
        supports_image = resolve_image_support("openai", model)
        out = []
        for m in messages:
            m = dict(m)
            thinking = m.pop("thinking", None)
            m.pop("thinking_signature", None)
            # 图片归属标记只服务于 Anthropic 转换层，不属于任何协议字段
            m.pop("image_for_tool_call", None)
            # 折叠前的工具结果原文（本地存档，供用户取回），不进请求
            m.pop("folded_original", None)
            images = m.pop("images", None)
            # 正文一律规整成字符串：脏数据（数字/布尔/字典）原样进请求体会被
            # 真端点拒收，且坏消息永久留在历史里 → 该会话从此发不出去。
            if m.get("role") in ("user", "assistant") and "content" in m:
                m["content"] = _as_text(m.get("content"))
            if (use_rc and m.get("role") == "assistant"
                    and m.get("tool_calls") and thinking is not None):
                m["reasoning_content"] = thinking
            if images:
                self._apply_images(m, images, supports_image)
            out.append(m)
        return out

    def _apply_images(self, msg, images, supports_image):
        """把图片引用就地展开进 msg["content"]（OpenAI 形块列表）。

        无法发送的图（模型不支持 / 副本丢失）只追加一句说明文本，
        句柄文本本就在 content 里，模型仍知道有这张图、多大、在哪。
        """
        available, notes = _resolve_images(images, self._data_dir, supports_image)
        text = msg.get("content") or ""
        if notes:
            text = (text + "\n" + "\n".join(notes)) if text else "\n".join(notes)
        if not available:
            msg["content"] = text
            return
        blocks = [{"type": "text", "text": text}] if text else []
        for media_type, b64 in available:
            blocks.append({
                "type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (media_type, b64)},
            })
        msg["content"] = blocks


    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None,
                    tool_defs=None, model=None, max_tokens=None):
        """流式请求并产出统一事件流（生成器；首次迭代才真正发请求）。

        请求发送（建连/等响应头）与流式接收期间均以 0.05 秒粒度轮询取消标记，
        取消命中即静默结束本轮：消除"中断后仍等服务端响应头到达才收敛"的慢路径。

        tool_defs/model/max_tokens：请求级覆盖（None=用实例默认）——本次请求单独
        指定工具表、模型（含 thinking 参数查表）与输出上限，不改动实例状态。
        max_tokens 用于压缩请求：压缩输出给一个独立的小预算，避免压缩请求
        自身把上下文窗口撑爆。
        """
        request_model = model or self._config.model
        if self._logger:
            self._logger.info("core.llm", f"发送请求(OpenAI), messages={len(messages)}条")

        messages = self._prepare_messages(messages, model=request_model)

        from openai import APIStatusError, APIConnectionError, APITimeoutError

        network_retries = 0
        rate_retries = 0
        stream = None

        while True:
            client = self._ensure_client()
            # 句柄用请求级scope：abort掐断在途请求但不杀死共享客户端
            LLMClient._active_response = _RequestScope(self, client)
            try:
                # 动态构造 thinking 参数（不再硬编码）
                think_body_top, think_extra = resolve_thinking_params(
                    "openai", request_model,
                    self._config.thinking_enabled and not no_thinking,
                    self._config.thinking_effort,
                )
                kwargs = dict(
                    model=request_model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    # read 放宽到 300s：思考期可能长时间无字节输出，大上下文 prefill
                    # 也会挤占读超时；已开始输出后的流中静默由看门狗负责检测
                    timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=30.0),
                )
                kwargs.update(think_body_top)
                if think_extra:
                    kwargs["extra_body"] = think_extra
                if not no_tools:
                    kwargs["tools"] = self._tool_defs if tool_defs is None else tool_defs
                # thinking 模式下 temperature 不生效（部分厂商契约下直接报错），不传
                if (self._config.temperature is not None
                        and not (think_body_top or think_extra)):
                    kwargs["temperature"] = self._config.temperature
                if self._config.max_tokens is not None:
                    kwargs["max_tokens"] = self._config.max_tokens
                if max_tokens is not None:
                    # 请求级覆盖优先：压缩请求用独立的小输出预算
                    kwargs["max_tokens"] = max_tokens
                # 发送移入子线程 + 主流程轮询取消标记：建连/等响应头期间取消也能立即
                # 收敛（直接调用时生成器体卡在create上，取消检查点不可达）；取消同样
                # 标记客户端重建，避免下一轮复用已被关闭的客户端
                cancelled, stream, err = run_cancelable(
                    lambda: client.chat.completions.create(**kwargs),
                    cancel_check,
                    on_cancel=lambda: self._invalidate_client(client),
                )
                if cancelled:
                    LLMClient._active_response = None
                    return
                if err is not None:
                    raise err       # 交给下方既有异常分支处理（重试矩阵不变）
                break

            except APIStatusError as e:
                LLMClient._active_response = None
                status = e.status_code
                if status in (400, 401, 403, 404, 422):
                    # 上下文超限单独归类：溢出恢复路径（压缩→重发）而非普通错误
                    if status == 400 and _is_context_overflow(
                            f"{e}{getattr(e, 'body', '')}"):
                        if self._logger:
                            self._logger.warning("core.llm", "API返回400: 上下文超限")
                        yield {"finish_reason": "context_overflow"}
                        return
                    # 其余 400 按**证据**分类（见 _classify_400）：
                    #   invalid_structure → 修消息结构；content_rejected → 折叠重发；
                    #   认不出来 → 当普通错误报出来，绝不动数据。
                    # 原来这里一刀切当「内容审核」，把结构错误也拖进折叠路径，
                    # 结果改写掉用户 5 条工具结果原文（实测数据损坏）。
                    if status == 400:
                        detail = f"{e}{getattr(e, 'body', '')}"
                        kind = _classify_400(detail)
                        if kind == "invalid_structure":
                            if self._logger:
                                self._logger.warning(
                                    "core.llm", f"API返回400: 消息结构错误 {detail[:200]}")
                            yield {"finish_reason": "invalid_structure",
                                   "detail": detail[:500]}
                            return
                        if kind == "content_rejected":
                            if self._logger:
                                self._logger.warning("core.llm", "API返回400: 内容审核拦截")
                            yield {"finish_reason": "content_rejected", "detail": detail[:500]}
                            return
                        if self._logger:
                            self._logger.error("core.llm", f"API返回400(未归类): {detail[:300]}")
                        yield {"content": f"[错误: API调用失败(400): {detail[:500]}]",
                               "finish_reason": "error"}
                        return
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {e}")
                    # 认证类失败（401）只给一句可操作提示，其余错误文案不变
                    label = "401" if status == 401 else type(e).__name__
                    yield {"content": _auth_fail(status, str(e)) or f"[错误: API调用失败({label}): {e}]", "finish_reason": "error"}
                    return
                if cancel_check and cancel_check():
                    return
                if status == 429 and rate_retries < LLMClient._max_rate_retries:
                    rate_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                    yield _retry_notice(rate_retries, LLMClient._max_rate_retries, "请求被限流(429)")
                    if not retry_sleep(rate_retries - 1, cancel_check):
                        return
                    continue
                if _is_retryable_http(status) and network_retries < LLMClient._max_network_retries:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                    yield _retry_notice(network_retries, LLMClient._max_network_retries, f"服务端错误({status})")
                    if not retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败(重试耗尽): {e}")
                _used = rate_retries if status == 429 else network_retries
                yield {"content": _auth_fail(status, str(e)) or f"[错误: API调用失败({type(e).__name__}，重试{_used}次): {e}]", "finish_reason": "error"}
                return

            except (APIConnectionError, APITimeoutError) as e:
                LLMClient._active_response = None
                if cancel_check and cancel_check():
                    return
                if network_retries < LLMClient._max_network_retries:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"网络错误(第{network_retries}次重试): {e}")
                    yield _retry_notice(network_retries, LLMClient._max_network_retries)
                    if not retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"网络错误(重试耗尽): {e}")
                yield {"content": _auth_fail(None, str(e)) or f"[错误: API调用失败({type(e).__name__}，重试{network_retries}次): {e}]", "finish_reason": "error"}
                return

            except Exception as e:
                LLMClient._active_response = None
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": _auth_fail(None, str(e)) or f"[错误: API调用失败({type(e).__name__}): {e}]", "finish_reason": "error"}
                return

        LLMClient._active_response = stream

        try:
            tool_calls_buffer = {}
            _index_to_id = {}
            content_buffer = []
            reasoning_buffer = []  # DeepSeek/Kimi OpenAI 协议：reasoning_content（思考回传用）
            _tc_idx = 0
            # 思考内容捕获条件：与回传开关无关（捕获/存储 与 回传 已解耦）。
            # /thinkback off 只影响请求侧是否携带 reasoning_content，不影响本地
            # 捕获与流式下发——否则会话记录里就完全没有思考内容。
            rc_capture_on = (
                self._config.thinking_enabled
                and resolve_thinking_passback("openai", request_model) == "reasoning_content"
            )

            chunk_queue = queue.Queue()
            stream_err = []   # 流迭代异常（服务端中途断开）
            received_finish = False
            last_data_ts = [None]  # 最近收到数据的时间戳（看门狗用；收到首个chunk后才有值）
            reader = threading.Thread(
                target=_iter_to_queue,
                args=(iter(stream), chunk_queue, stream_err, last_data_ts),
                daemon=True)
            reader.start()

            while True:
                try:
                    chunk = chunk_queue.get(timeout=0.05)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    # 看门狗：已开始输出后静默超过阈值视为挂死，主动断连触发上层重试；
                    # 首字节前（prefill/思考期）静默由 read 超时兜底，不在此误断
                    if (not stream_err and last_data_ts[0] is not None
                            and time.time() - last_data_ts[0] > LLMClient._STREAM_STALL_SECONDS):
                        stream_err.append(httpx.ReadTimeout(
                            f"流式输出静默超过{LLMClient._STREAM_STALL_SECONDS:.0f}s"))
                        try:
                            stream.close()
                        except Exception:
                            pass
                    continue
                if chunk is LLMClient._STREAM_END:
                    break

                usage = getattr(chunk, 'usage', None)
                if usage:
                    cached = 0
                    details = getattr(usage, 'prompt_tokens_details', None)
                    if details:
                        cached = getattr(details, 'cached_tokens', 0)
                    if not cached:
                        # DeepSeek: 缓存命中数在顶层 prompt_cache_hit_tokens（OpenAI 原生无此字段）
                        cached = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
                        if not cached:
                            cached = (getattr(usage, 'model_extra', None) or {}).get('prompt_cache_hit_tokens', 0) or 0
                    yield {
                        "usage": {
                            "prompt_tokens": getattr(usage, 'prompt_tokens', 0) or 0,
                            "completion_tokens": getattr(usage, 'completion_tokens', 0) or 0,
                            "cached_tokens": cached,
                        }
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if delta.content:
                    content_buffer.append(delta.content)
                    yield {"content": delta.content}

                # 捕获 reasoning_content（DeepSeek/Kimi 思考模式：多轮对话需回传）
                # OpenAI SDK 对非原生字段可能放进 model_extra，双路兼容
                rc = getattr(delta, 'reasoning_content', None)
                if rc is None:
                    rc = (getattr(delta, 'model_extra', None) or {}).get('reasoning_content')
                if rc:
                    reasoning_buffer.append(rc)
                    if rc_capture_on:
                        yield {"thinking_delta": rc}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        tc_index = getattr(tc, 'index', None)
                        if tc.id:
                            tc_id = tc.id
                            if tc_index is not None:
                                _index_to_id[tc_index] = tc_id
                        elif tc_index is not None and tc_index in _index_to_id:
                            tc_id = _index_to_id[tc_index]
                        else:
                            tc_id = f"_tc_{_tc_idx}"
                            _tc_idx += 1
                        buf = tool_calls_buffer.setdefault(
                            tc_id, {"id": tc_id, "name": "", "arguments": ""},
                        )
                        if tc.function.name:
                            buf["name"] += tc.function.name
                        if tc.function.arguments:
                            buf["arguments"] += tc.function.arguments

                if finish_reason:
                    received_finish = True
                    # 思考内容始终上报（与回传开关解耦）：上层据此写入会话记录，
                    # 回传与否由请求侧 _prepare_messages 的 use_rc 决定。
                    thinking_out = "".join(reasoning_buffer) if rc_capture_on else None
                    if tool_calls_buffer:
                        completed_calls = []
                        for tc_id, buf in tool_calls_buffer.items():
                            completed_calls.append({
                                "id": buf["id"],
                                "type": "function",
                                "function": {"name": buf["name"], "arguments": buf["arguments"]},
                            })
                        yield {"tool_calls": completed_calls, "finish_reason": finish_reason,
                               "thinking": thinking_out}
                    else:
                        # 兜底：服务端只回 reasoning_content 不回 content（DeepSeek
                        # 偶发；anthropic 后端同款兜底）——把思考内容作为正式输出，
                        # 避免上层收到空回复（round_failed）
                        if not content_buffer and reasoning_buffer:
                            fallback_text = "".join(reasoning_buffer)
                            content_buffer.append(fallback_text)
                            yield {"content": fallback_text}
                            yield {"finish_reason": finish_reason, "thinking": ""}
                        else:
                            yield {"finish_reason": finish_reason, "thinking": thinking_out}

                    if self._logger:
                        total_out = len("".join(content_buffer))
                        self._logger.info("core.llm", f"响应完成, content_len={total_out}")

            # ── 流中断检测：迭代器异常退出且未收到完成标记 ──
            # 不 yield 虚假完成标记；上报中断信息，由上层（agent_loop）决定整轮重试
            if stream_err and not received_finish:
                if self._logger:
                    self._logger.warning("core.llm", f"响应流中断: {stream_err[0]}")
                yield {"stream_interrupted": _classify_stream_error(stream_err[0])}
            # ── 兜底：流正常结束但未收到完成标记且无任何内容产出（如网关
            # 200+HTML、无效 SSE 帧）：明确报错，避免上层空重试（anthropic
            # 后端同款兜底；有内容/完成标记/中断的正常路径不受影响）
            elif (not received_finish and not content_buffer
                    and not tool_calls_buffer and not reasoning_buffer):
                if self._logger:
                    self._logger.error("core.llm", "响应流为空或格式异常（未收到有效内容）")
                yield {"content": "[错误: API响应为空或格式异常（未收到有效内容）]", "finish_reason": "error"}
        finally:
            LLMClient._active_response = None


# ═══════════════════════════════════════════════════════════════
# Anthropic 兼容后端（httpx 实现，无 requests 依赖）
# ═══════════════════════════════════════════════════════════════

class _AnthropicBackend:
    """
    Anthropic 兼容后端。

    端点: {base_url}/v1/messages
    认证: x-api-key + anthropic-version
    流式: content_block_start/delta 事件

    内部做 OpenAI ↔ Anthropic 消息格式双向转换，
    对外统一 yield OpenAI 格式的 chunk。
    使用 httpx 替代 requests，统一HTTP客户端依赖。
    """

    def __init__(self, config, tool_defs, logger, max_output_tokens=128000, data_dir=""):
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._data_dir = data_dir
        self._max_output_tokens = max_output_tokens
        self._url = f"{config.base_url.rstrip('/')}/v1/messages"
        self._last_raw_sse = []
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None,
                    tool_defs=None, model=None, max_tokens=None):
        """流式请求并产出统一事件流（生成器；首次迭代才真正发请求）。

        请求发送（建连/等响应头）与流式接收期间均以 0.05 秒粒度轮询取消标记，
        取消命中即静默结束本轮并清理活跃请求句柄：消除"中断后仍等服务端响应头
        到达才收敛"的慢路径。

        tool_defs/model/max_tokens：请求级覆盖（None=用实例默认）——本次请求单独
        指定工具表、模型（含 thinking 参数查表）与输出上限，不改动实例状态。
        max_tokens 用于压缩请求：压缩输出给一个独立的小预算，避免压缩请求
        自身把上下文窗口撑爆。
        """
        request_model = model or self._config.model
        self._last_raw_sse.clear()
        if self._logger:
            self._logger.info("core.llm", f"发送请求(Anthropic), messages={len(messages)}条")

        # 转换消息格式
        try:
            system, anthropic_msgs = self._convert_messages(messages, model=request_model)
            anthropic_tools = self._convert_tools(self._tool_defs if tool_defs is None else tool_defs)
        except Exception as e:
            yield {"content": f"[错误: 消息格式转换失败: {e}]", "finish_reason": "error"}
            return

        # 动态构造 thinking 参数
        think_body_top, think_extra = resolve_thinking_params(
            "anthropic", request_model,
            self._config.thinking_enabled and not no_thinking,
            self._config.thinking_effort,
        )
        body = {
            "model": request_model,
            "messages": anthropic_msgs,
            "max_tokens": self._max_output_tokens,
            "stream": True,
        }
        # Anthropic 协议下 body_top 和 extra_body 都合并到 body 顶层
        body.update(think_body_top)
        body.update(think_extra)
        if system:
            body["system"] = system
        if anthropic_tools and not no_tools:
            body["tools"] = anthropic_tools
        # thinking 模式下 temperature 不生效（部分厂商契约下直接报错），不传
        if (self._config.temperature is not None
                and not (think_body_top or think_extra)):
            body["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens
        if max_tokens is not None:
            # 请求级覆盖优先：压缩请求用独立的小输出预算
            body["max_tokens"] = max_tokens

        # 发送请求（带重试，使用 httpx 流式）
        network_retries = 0
        rate_retries = 0

        while True:
            # read 放宽到 300s：思考期可能长时间无字节输出，大上下文 prefill 也会挤占读超时；
            # 流中静默挂死由看门狗（LLMClient._STREAM_STALL_SECONDS）主动断连触发重试。
            # verify 复用模块级 SSLContext：消除每请求重建（certifi 加载 ~1.2s）的固定开销
            client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=30.0),
                                  verify=_get_ssl_context())
            LLMClient._active_response = client
            try:
                # 使用 stream 模式发送请求，先拿到 status_code 再决定是否读取流
                req = client.build_request("POST", self._url, headers=self._headers, json=body)
                # 发送移入子线程 + 主流程轮询取消标记：建连/等响应头期间取消也能立即
                # 收敛（直接调用时生成器体卡在send上，取消检查点不可达）；本路径每次
                # 请求新建 httpx.Client，关闭不影响后续请求，无需请求级scope
                cancelled, resp, err = run_cancelable(
                    lambda: client.send(req, stream=True),
                    cancel_check,
                    on_cancel=lambda: safe_close(client),
                )
                if cancelled:
                    safe_close(client)  # 兜底幂等：与 on_cancel 的关闭重复无副作用
                    LLMClient._active_response = None
                    return
                if err is not None:
                    raise err       # 交给下方既有异常分支处理（TransportError/Exception 全保留）
                status = resp.status_code

                # 不重试
                if status in (400, 401, 403, 404, 422):
                    # stream 模式需先 read() 才能访问 text（否则抛 ResponseNotRead 掩盖真实错误）
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    LLMClient._active_response = None
                    # 上下文超限单独归类：溢出恢复路径（压缩→重发）而非普通错误
                    if status == 400 and _is_context_overflow(err_text):
                        if self._logger:
                            self._logger.warning("core.llm", "API返回400: 上下文超限")
                        yield {"finish_reason": "context_overflow"}
                        return
                    # 其余 400 按证据分类（同上方另一后端，见 _classify_400）
                    if status == 400:
                        kind = _classify_400(err_text)
                        if kind == "invalid_structure":
                            if self._logger:
                                self._logger.warning(
                                    "core.llm", f"API返回400: 消息结构错误 {err_text[:200]}")
                            yield {"finish_reason": "invalid_structure",
                                   "detail": err_text[:500]}
                            return
                        if kind == "content_rejected":
                            if self._logger:
                                self._logger.warning("core.llm", "API返回400: 内容审核拦截")
                            yield {"finish_reason": "content_rejected", "detail": err_text[:500]}
                            return
                        if self._logger:
                            self._logger.error("core.llm", f"API返回400(未归类): {err_text[:300]}")
                        yield {"content": f"[错误: API调用失败(400): {err_text[:500]}]",
                               "finish_reason": "error"}
                        return
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败(不可重试): {status} {err_text[:500]}")
                    yield {"content": _auth_fail(status, err_text) or f"[错误: API调用失败({status}): {err_text[:500]}]", "finish_reason": "error"}
                    return

                # 429 速率限制
                if status == 429:
                    # 先读响应体片段供报错（stream 模式需 read() 后才能访问 text）
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    if rate_retries < LLMClient._max_rate_retries:
                        rate_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回429(第{rate_retries}次重试)...")
                        yield _retry_notice(rate_retries, LLMClient._max_rate_retries, "请求被限流(429)")
                        if not retry_sleep(rate_retries - 1, cancel_check):
                            LLMClient._active_response = None
                            return
                        continue
                    LLMClient._active_response = None
                    yield {"content": f"[错误: API返回429速率限制(已重试{LLMClient._max_rate_retries}次): {err_text[:500]}]", "finish_reason": "error"}
                    return

                # 可重试服务端错误
                if _is_retryable_http(status):
                    # 先读响应体片段供报错（stream 模式需 read() 后才能访问 text）
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    if network_retries < LLMClient._max_network_retries:
                        network_retries += 1
                        if self._logger:
                            self._logger.warning("core.llm", f"API返回{status}(第{network_retries}次重试)...")
                        yield _retry_notice(network_retries, LLMClient._max_network_retries, f"服务端错误({status})")
                        if not retry_sleep(network_retries - 1, cancel_check):
                            LLMClient._active_response = None
                            return
                        continue
                    LLMClient._active_response = None
                    yield {"content": f"[错误: API返回{status}错误(已重试{LLMClient._max_network_retries}次): {err_text[:500]}]", "finish_reason": "error"}
                    return

                # 成功
                if status != 200:
                    resp.read()
                    err_text = resp.text
                    resp.close()
                    client.close()
                    LLMClient._active_response = None
                    if self._logger:
                        self._logger.error("core.llm", f"API调用失败: {status} {err_text[:500]}")
                    yield {"content": _auth_fail(status, err_text) or f"[错误: API调用失败({status}): {err_text[:500]}]", "finish_reason": "error"}
                    return
                break

            # TransportError 覆盖 Connect/Read/Write/RemoteProtocol 全部连接类异常，
            # 包括 "Server disconnected without sending a response"（RemoteProtocolError）
            except (httpx.TransportError, httpx.TimeoutException) as e:
                client.close()
                LLMClient._active_response = None
                if cancel_check and cancel_check():
                    return
                if network_retries < LLMClient._max_network_retries:
                    network_retries += 1
                    if self._logger:
                        self._logger.warning("core.llm", f"网络错误(第{network_retries}次重试): {e}")
                    yield _retry_notice(network_retries, LLMClient._max_network_retries)
                    if not retry_sleep(network_retries - 1, cancel_check):
                        return
                    continue
                if self._logger:
                    self._logger.error("core.llm", f"网络错误(重试耗尽): {e}")
                yield {"content": _auth_fail(None, str(e)) or f"[错误: API调用失败({type(e).__name__}，重试{network_retries}次): {e}]", "finish_reason": "error"}
                return

            except Exception as e:
                client.close()
                LLMClient._active_response = None
                if cancel_check and cancel_check():
                    return
                if self._logger:
                    self._logger.error("core.llm", f"API调用失败: {e}")
                yield {"content": _auth_fail(None, str(e)) or f"[错误: API调用失败({type(e).__name__}): {e}]", "finish_reason": "error"}
                return

        # 解析 Anthropic SSE 流（resp 已是 stream=True 模式）
        LLMClient._active_response = resp

        try:
            content_buffer = []
            thinking_buffer = []  # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text
            thinking_blocks = {}  # index → 思考文本（思考模式下需回传API，见官方thinking_mode文档）
            thinking_sigs = {}    # index → 思考块签名（Claude 要求回传时携带，经 signature_delta 送达）
            tool_use_blocks = {}
            _msg_delta_seen = False  # 守卫：防止 message_delta 正常到达后兜底重复 yield
            _start_usage = None     # message_start 中的初始 usage，兜底时补用

            line_queue = queue.Queue()
            stream_err = []          # 流读取异常（服务端中途断开）
            last_data_ts = [None]    # 最近收到数据的时间戳（看门狗用；收到首个事件后才有值）

            def _read_lines():
                """后台线程：从 httpx 流式响应中逐行读取 SSE"""
                try:
                    for line in resp.iter_lines():
                        last_data_ts[0] = time.time()
                        line_queue.put(line)
                except Exception as e:
                    # 看门狗已记录错误时不再重复追加
                    if not stream_err:
                        stream_err.append(e)
                finally:
                    line_queue.put(LLMClient._STREAM_END)

            reader = threading.Thread(target=_read_lines, daemon=True)
            reader.start()

            while True:
                try:
                    line = line_queue.get(timeout=0.05)
                except queue.Empty:
                    if cancel_check and cancel_check():
                        return
                    # 看门狗：已开始输出后静默超过阈值视为挂死，主动断连触发上层重试；
                    # 首字节前（prefill/思考期）静默由 read 超时兜底，不在此误断
                    if (not stream_err and last_data_ts[0] is not None
                            and time.time() - last_data_ts[0] > LLMClient._STREAM_STALL_SECONDS):
                        stream_err.append(httpx.ReadTimeout(
                            f"流式输出静默超过{LLMClient._STREAM_STALL_SECONDS:.0f}s"))
                        try:
                            resp.close()
                        except Exception:
                            pass
                    continue
                if line is LLMClient._STREAM_END:
                    break

                line = (line or "").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue

                self._last_raw_sse.append(data_str)

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                dtype = data.get("type", "")

                if dtype == "message_start":
                    # 捕获初始 usage，供 message_delta 缺失时兜底
                    msg = data.get("message", {})
                    usage = msg.get("usage", {})
                    if usage:
                        # DeepSeek 兼容层可能返回 cache_read_input_tokens 或 prompt_cache_hit_tokens，双兼容
                        cached = usage.get("cache_read_input_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0
                        _start_usage = {
                            "prompt_tokens": (usage.get("input_tokens", 0) or 0) + cached,
                            "completion_tokens": 0,
                            "cached_tokens": cached,
                        }
                    continue

                if dtype == "content_block_start":
                    cb = data.get("content_block", {})
                    idx = data.get("index", 0)
                    ctype = cb.get("type", "")
                    if ctype == "tool_use":
                        tool_use_blocks[idx] = {
                            "id": cb.get("id", ""),
                            "name": cb.get("name", ""),
                            "input_json": "",
                        }
                    elif ctype == "thinking":
                        # 思考块开始：DeepSeek 流式下 signature 为空串且不回填，回传时无需携带
                        thinking_blocks[idx] = ""

                elif dtype == "content_block_delta":
                    delta = data.get("delta", {})
                    idx = data.get("index", 0)
                    d_type = delta.get("type", "")

                    if d_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            content_buffer.append(text)
                            yield {"content": text}

                    elif d_type == "thinking_delta":
                        # 收集 thinking 内容，用于 thinking-only 回复兜底 + 会话记录留存；
                        # 是否随请求回传给模型由 _convert_messages 的开关判定决定
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            thinking_buffer.append(thinking_text)
                            thinking_blocks[idx] = thinking_blocks.get(idx, "") + thinking_text
                            yield {"thinking_delta": thinking_text}

                    elif d_type == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if idx in tool_use_blocks:
                            tool_use_blocks[idx]["input_json"] += pj

                    elif d_type == "signature_delta":
                        # Claude：思考块签名经 signature_delta 事件送达（回传思考块必需）
                        sig = delta.get("signature", "")
                        if sig:
                            thinking_sigs[idx] = sig

                elif dtype == "message_delta":
                    _msg_delta_seen = True
                    stop_reason = data.get("delta", {}).get("stop_reason") or "end_turn"
                    thinking_combined = "".join(thinking_blocks[i] for i in sorted(thinking_blocks))
                    # 思考内容始终上报（会话记录留存），本轮回传与否由 _convert_messages 决定
                    thinking_out = thinking_combined
                    # Claude 签名：仅当恰好一个思考块且签名非空时上报（多块边界无法可靠对齐，宁缺勿错）
                    thinking_sig_out = None
                    if len(thinking_blocks) == 1:
                        sig = thinking_sigs.get(next(iter(thinking_blocks)), "")
                        if sig:
                            thinking_sig_out = sig
                    if stop_reason:
                        if stop_reason == "end_turn":
                            finish_reason = "stop"
                        elif stop_reason == "tool_use":
                            finish_reason = "tool_calls"
                        elif stop_reason in ("max_tokens", "length"):
                            finish_reason = "max_tokens"
                        elif stop_reason == "content_filter":
                            finish_reason = "content_filter"
                        elif stop_reason == "insufficient_system_resource":
                            finish_reason = "server_busy"
                        else:
                            finish_reason = stop_reason

                        if tool_use_blocks:
                            completed_calls = []
                            for idx in sorted(tool_use_blocks.keys()):
                                tu = tool_use_blocks[idx]
                                completed_calls.append({
                                    "id": tu["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tu["name"],
                                        "arguments": tu["input_json"],
                                    },
                                })
                            yield {"tool_calls": completed_calls, "finish_reason": finish_reason,
                                   "thinking": thinking_out, "thinking_signature": thinking_sig_out}
                        else:
                            # 兜底：DeepSeek V4 有时只返回 thinking 不返回 text
                            # 此时将 thinking 内容作为正式输出
                            if not content_buffer and thinking_buffer:
                                fallback_text = "".join(thinking_buffer)
                                content_buffer.append(fallback_text)
                                yield {"content": fallback_text}
                                if self._logger:
                                    self._logger.info("core.llm", f"thinking-only兜底: 将thinking内容({len(fallback_text)}字符)作为text输出")
                                # thinking 已作为 text 输出，避免下一轮回传时重复占用上下文
                                yield {"finish_reason": finish_reason, "thinking": "",
                                       "thinking_signature": None}
                            else:
                                yield {"finish_reason": finish_reason, "thinking": thinking_out,
                                       "thinking_signature": thinking_sig_out}

                        if self._logger:
                            total_out = len("".join(content_buffer))
                            self._logger.info("core.llm", f"响应完成, content_len={total_out}, stop_reason={stop_reason}")

                    # 捕获usage
                    usage = data.get("usage", {})
                    if usage:
                        if usage.get("input_tokens") is None and _start_usage:
                            # Claude 官方 message_delta 的 usage 只含 output_tokens：
                            # input 缺失时采用 message_start 的输入口径（已含缓存合并，不再叠加）
                            yield {"usage": {
                                "prompt_tokens": _start_usage.get("prompt_tokens", 0),
                                "completion_tokens": usage.get("output_tokens", 0) or 0,
                                "cached_tokens": _start_usage.get("cached_tokens", 0),
                            }}
                        else:
                            prompt = usage.get("input_tokens", 0) or 0
                            # DeepSeek 兼容层可能返回 cache_read_input_tokens 或 prompt_cache_hit_tokens，双兼容
                            cached = usage.get("cache_read_input_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0
                            yield {"usage": {"prompt_tokens": prompt + cached, "completion_tokens": usage.get("output_tokens", 0) or 0, "cached_tokens": cached}}

                elif dtype == "error":
                    err_msg = data.get("error", {}).get("message", "未知错误")
                    yield {"content": f"[错误: {err_msg}]", "finish_reason": "error"}
                    return

            # ── 流中断：服务端中途断开且未收到完成标记 ──
            # 不 yield 虚假完成标记；上报中断信息，由上层（agent_loop）决定整轮重试
            if stream_err and not _msg_delta_seen:
                if self._logger:
                    self._logger.warning("core.llm", f"响应流中断: {stream_err[0]}")
                yield {"stream_interrupted": _classify_stream_error(stream_err[0])}

            # ── 兜底：流正常结束但未收到 message_delta（DeepSeek 偶发漏发）──
            # 此时缓冲区可能已有完整内容，直接使用
            elif not _msg_delta_seen:
                if _start_usage:
                    yield {"usage": _start_usage}
                thinking_combined = "".join(thinking_blocks[i] for i in sorted(thinking_blocks))
                # 思考内容始终上报（会话记录留存），本轮回传与否由 _convert_messages 决定
                thinking_out = thinking_combined
                thinking_sig_out = None
                if len(thinking_blocks) == 1:
                    sig = thinking_sigs.get(next(iter(thinking_blocks)), "")
                    if sig:
                        thinking_sig_out = sig
                if tool_use_blocks:
                    completed_calls = []
                    for idx in sorted(tool_use_blocks.keys()):
                        tu = tool_use_blocks[idx]
                        if tu["id"] and tu["name"]:
                            completed_calls.append({
                                "id": tu["id"],
                                "type": "function",
                                "function": {
                                    "name": tu["name"],
                                    "arguments": tu["input_json"],
                                },
                            })
                    if completed_calls:
                        yield {"tool_calls": completed_calls, "finish_reason": "tool_calls",
                               "thinking": thinking_out, "thinking_signature": thinking_sig_out}
                        if self._logger:
                            self._logger.info(
                                "core.llm",
                                f"兜底: 未收到message_delta，从缓冲区提取{len(completed_calls)}个工具调用",
                            )
                elif not content_buffer and thinking_buffer:
                    fallback_text = "".join(thinking_buffer)
                    content_buffer.append(fallback_text)
                    yield {"content": fallback_text}
                    yield {"finish_reason": "stop", "thinking": "", "thinking_signature": None}
                    if self._logger:
                        self._logger.info(
                            "core.llm",
                            f"兜底: 未收到message_delta，将thinking({len(fallback_text)}字符)作为text输出",
                        )
                elif content_buffer:
                    yield {"finish_reason": "stop", "thinking": thinking_out,
                           "thinking_signature": thinking_sig_out}
                    if self._logger:
                        self._logger.info(
                            "core.llm",
                            f"兜底: 未收到message_delta，但已有文字内容({len(''.join(content_buffer))}字符)",
                        )
                else:
                    # 全空兜底：流正常结束但既无内容也无完成标记（如网关 200+HTML、
                    # SSE 帧全非法）：明确报错，避免上层把空响应当作正常空轮次
                    if self._logger:
                        self._logger.error("core.llm", "响应流为空或格式异常（未收到有效内容）")
                    yield {"content": "[错误: API响应为空或格式异常（未收到有效内容）]", "finish_reason": "error"}

        finally:
            LLMClient._active_response = None
            resp.close()
            client.close()

    # ── 消息格式转换：OpenAI → Anthropic ──

    @staticmethod
    def _is_tool_result_only(content):
        """该 user 消息的 content 是否**只**含 tool_result 块。

        只有这种情况才允许继续往里追 tool_result：Anthropic 要求
        tool_result 必须紧跟 tool_use 且是 user 消息首块，
        一旦这条 user 已经含 text/image，再追 tool_result 就是 400。
        """
        return (isinstance(content, list) and bool(content)
                and all(isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content))

    def _tool_result_blocks(self, tool_call_id, text, images, supports_image):
        """构造一条 tool 消息对应的 tool_result 块（图片直接内嵌）。

        Anthropic 的 tool_result.content 接受字符串或内容块数组，
        块数组里可以含 image（已用真实端点验证）。把 Read 读到的图直接放进
        tool_result，比"另起一条 user 挂图"更合规：天然满足
        tool_use/tool_result 紧邻，也不会多出一条 user 打断角色交替。
        """
        available, notes = _resolve_images(images, self._data_dir, supports_image)
        body = text or ""
        if notes:
            body = (body + "\n" + "\n".join(notes)) if body else "\n".join(notes)
        if not available:
            return [{"type": "tool_result", "tool_use_id": tool_call_id,
                     "content": body}]

        parts = []
        if body:
            parts.append({"type": "text", "text": body})
        for media_type, b64 in available:
            parts.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        return [{"type": "tool_result", "tool_use_id": tool_call_id,
                 "content": parts}]

    def _user_blocks(self, content, images, supports_image):
        """把一条 user 消息的文本 + 图片引用转成 Anthropic 内容块列表。

        Returns:
            (blocks, text)：blocks 为内容块列表（文本块在前、图片块在后，
            与 deepseek-harness 的 handle-then-image 顺序一致：模型先读到
            "这是什么图"再看到图）；text 为拼好降级说明后的正文，
            供"最终没有图可发"时退回字符串 content 使用。
        """
        available, notes = _resolve_images(images, self._data_dir, supports_image)
        # 用 _as_text 而不是 `content or ""`：后者会把 0/False 吞成空串，
        # 静默丢掉一条真实提问，也会让非字符串 content 原样进请求体（→ 真端点 422）。
        text = _as_text(content)
        if notes:
            text = (text + "\n" + "\n".join(notes)) if text else "\n".join(notes)

        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for media_type, b64 in available:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            })
        return blocks, text

    def _convert_messages(self, messages, model=None):
        model = model or self._config.model
        supports_image = resolve_image_support("anthropic", model)
        system_parts = []
        anthropic_msgs = []

        # 图片归属预归集：带 image_for_tool_call 的 user 消息，其图片要并进
        # 对应 tool_result（而不是另起一条 user 打断 tool_use 紧邻约束）。
        # 先扫一遍建 map，转换主循环里据此取出并跳过那条 user 消息。
        images_by_call = {}
        for m in messages:
            if m.get("role") == "user" and m.get("image_for_tool_call"):
                key = m["image_for_tool_call"]
                images_by_call.setdefault(key, []).extend(m.get("images") or [])

        # 全部已声明的 tool_use id（供下面过滤孤儿 tool_result）。
        # 会话被劈成两个文件时（切会话/建分支期间工具还在跑，实测），
        # 结果在这里而声明在另一个文件里 —— 那种结果发出去必然 400。
        declared_tool_use = set()
        for m in messages:
            for tc in (m.get("tool_calls") or []):
                tid = tc.get("id")
                if tid:
                    declared_tool_use.add(tid)

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            images = msg.get("images")

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            # 图片已被并进 tool_result 的那条 user 消息：整体跳过。
            # 它只是消息层的"图片转运站"，在 Anthropic 侧没有独立语义。
            if role == "user" and msg.get("image_for_tool_call"):
                continue

            if role == "user":
                # 同 _user_blocks 里的理由：不能用 `content or ""`（0/False 会被吞）
                content = _as_text(content)
                blocks, text = self._user_blocks(content, images, supports_image)
                prev = anthropic_msgs[-1] if anthropic_msgs else None
                if (content and prev is not None and prev["role"] == "user"
                        and not _user_has_tool_result(prev)):
                    # 合并纯文本连续 user（Anthropic 强制角色交替）。
                    # 含 tool_result 的 user 消息不合并：DeepSeek 校验
                    # tool_result 必须紧邻 tool_use（同消息插入 text 会破坏
                    # 校验），且 tool_result 后的独立 user 已被真实API验证可接受。
                    if isinstance(prev["content"], list):
                        prev["content"].extend(blocks)
                    else:
                        prev["content"] = (
                            [{"type": "text", "text": prev["content"]}] + blocks)
                else:
                    if blocks and any(b.get("type") == "image" for b in blocks):
                        # 有图：必须用块列表（图片块不能塞进字符串 content）
                        anthropic_msgs.append({"role": "user", "content": blocks})
                    else:
                        # 无图（含"图发不出去"的降级情况）：用 text 而非原始 content，
                        # text 里可能已拼上"[图片未发送 / 已丢失]"说明；无说明时二者相同
                        anthropic_msgs.append({"role": "user", "content": text})

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                # 思考块回传按"协议+模型"查表（THINKING_PARAM_MAP.passback）：
                # - thinking_block（DeepSeek anthropic）：回传 thinking 块，API 强制否则400
                # - thinking_block_signed（Claude）：回传 thinking 块且必须携带签名；
                #   未捕获签名（多思考块/无签名）→ 省略该块的思考回传（安全，Claude 不报错）
                # 思考回传开关（/thinkback）关闭：彻底删除 thinking 段，一刀切。
                thinking = None
                sig = None
                if (self._config.thinking_enabled
                        and getattr(self._config, "thinking_passback", DEFAULT_THINKING_PASSBACK)):
                    mode = resolve_thinking_passback("anthropic", model)
                    if mode in ("thinking_block", "thinking_block_signed"):
                        t = msg.get("thinking")
                        if t is not None:
                            # 学官方 harness：思考仅工具调用轮回传（纯文本轮被忽略）。
                            # 例外：repair 合成占位（SYNTHETIC_THINKING）无 tool_calls 但必须
                            # 回传——DeepSeek 校验请求尾部 assistant 携带非空 thinking 块。
                            if tool_calls or t == SYNTHETIC_THINKING:
                                if mode == "thinking_block_signed":
                                    # Claude 要求签名；无签名则省略思考块（安全，Claude 不报错）
                                    s = msg.get("thinking_signature")
                                    if s:
                                        thinking, sig = t, s
                                else:
                                    thinking = t  # DeepSeek：无需签名（流式下签名恒空）
                if tool_calls:
                    blocks = []
                    if thinking is not None:
                        blk = {"type": "thinking", "thinking": thinking}
                        if sig:
                            blk["signature"] = sig
                        blocks.append(blk)
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        try:
                            inp = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": inp,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                elif thinking is not None:
                    blocks = [{"type": "thinking", "thinking": thinking}]
                    if sig:
                        blocks[0]["signature"] = sig
                    if content:
                        blocks.append({"type": "text", "text": content})
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                else:
                    anthropic_msgs.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                tc_id = msg.get("tool_call_id", "")
                # 最后一道闸：没有对应 tool_use 的 tool_result 一律不发。
                # Anthropic 强校验「每个 tool_result 都要在前一条 assistant 里
                # 找到 tool_use」，漏一个整份请求 400。repair() 会把它从历史里
                # 删掉，这里再兜一道 —— 修历史是异步的，发请求是即时的。
                if tc_id and tc_id not in declared_tool_use:
                    if self._logger:
                        self._logger.warning(
                            "core.llm",
                            f"跳过无对应 tool_use 的工具结果 {tc_id}"
                            f"（会造成请求 400；repair 会把它从历史里删掉）")
                    continue
                # 该工具调用若携带图片（Read 读到图），从预归集里取出来内嵌
                tool_images = images_by_call.get(tc_id)
                blocks = self._tool_result_blocks(tc_id, msg.get("content") or "",
                                                  tool_images, supports_image)
                # tool_result 必须是 user 消息的首块（Anthropic 强校验），
                # 因此只有当上一条 user 是"纯 tool_result 容器"时才并进去；
                # 若上一条含 text/image（图片配图消息等），必须新起一条，
                # 否则 tool_result 不在首位 → 400，且会插到别的块中间。
                if (anthropic_msgs and anthropic_msgs[-1]["role"] == "user"
                        and self._is_tool_result_only(anthropic_msgs[-1]["content"])):
                    anthropic_msgs[-1]["content"].extend(blocks)
                else:
                    anthropic_msgs.append({"role": "user", "content": blocks})

        # 开放位置 assistant 补占位思考块（见 _PLACEHOLDER_THINKING 注释）：
        # 尾部 assistant 与连续 assistant 链前序缺 thinking 块会被 API 拒
        # （400 must be passed back）；正常形态不触发（assistant 后均跟 user）
        for i, m in enumerate(anthropic_msgs):
            if m.get("role") != "assistant":
                continue
            is_open = (i == len(anthropic_msgs) - 1
                       or anthropic_msgs[i + 1].get("role") == "assistant")
            if not is_open:
                continue
            blocks = m.get("content")
            if isinstance(blocks, list) and any(
                    isinstance(b, dict) and b.get("type") == "thinking"
                    for b in blocks):
                continue
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}] if blocks else []
            else:
                blocks = list(blocks or [])
            m["content"] = [{"type": "thinking", "thinking": _PLACEHOLDER_THINKING}] + blocks

        system_text = "\n\n".join(system_parts) if system_parts else ""
        return system_text, anthropic_msgs

    # ── 工具定义转换：OpenAI → Anthropic ──

    def _convert_tools(self, tool_defs):
        if not tool_defs:
            return []
        anthropic_tools = []
        for t in tool_defs:
            func = t.get("function", t)
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {
                    "type": "object", "properties": {},
                }),
            })
        return anthropic_tools
