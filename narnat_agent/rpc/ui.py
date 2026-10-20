"""RPC 模式 UI —— 流式输出与命令结果转成 JSON-RPC 事件

RpcUI 是 UIInterface / HeadlessUI 之后的第三个实现：
- read_input() 阻塞从线程安全队列取输入（chat/send 投递）
- create_stream() 返回 RpcStream，feed() 把增量文本变成 stream/delta 通知
- dispatch_command 里 /exit 一律折算成 HANDLED：RPC 模式下进程必须常驻，
  退出走 shutdown（agent.run() 收到 EXIT 会 os._exit(0)）

一轮的事件边界（协议 4.10/4.11）：
- 每次 finish() 都下发 stats（RPC 形态恒发；with_stats 只是终端"是否打印统计栏"的语义）
- turn/end 只在这一轮真正结束时下发：finish(with_stats=True) 当场发；目标模式的中间轮
  （show_stats=False）与工具确认流程（core 里 with_stats=False 后另建新流）这类
  "流提前结束但轮次未结"的情况，由 RpcUI 在轮次边界（read_input / create_stream）收口

事件字段全部按 docs/vscode-rpc-protocol.md 第 3、4 节，不增删改名。
"""

import json
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional

from ..output import DisplayState
from ..ui.interrupt import _interrupt_ctrl
from .protocol import get_transport


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")
_BG_SUBMIT_RE = re.compile(r"(bg\d+)\s*已提交")
_BG_PATH_RE = re.compile(r"结果:\s*(.+?\.log)")


def emit(method: str, params: Optional[Dict[str, Any]] = None) -> None:
    """下发一条通知（一行一条 JSON，UTF-8 原样输出）"""
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    try:
        line = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    get_transport().write_line(line)


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


class RpcStream:
    """流式会话句柄：把 feed/工具钩子/思考钩子变成协议通知

    与 UIStreamSession 公开方法集保持一致（begin/feed/pause_spinner/
    resume_spinner/flush_renderer/reset_renderer/finish/abort/cancelled/aborted），
    另外提供 core 侧可选钩子 on_tool_call / on_tool_result / on_thinking_delta。
    """

    def __init__(self, ui: "RpcUI", round_no: int = 0) -> None:
        self._ui = ui
        self._round = round_no
        self._lock = threading.RLock()
        self._begun = False
        self._aborted = False
        self._turn_closed = False    # turn/end 已下发（本轮真正结束）
        self._cancelled = False
        self._thinking_started = False
        self._thinking_t0 = 0.0
        self._thinking_chars = 0

    # ── 状态位 ──

    @property
    def cancelled(self) -> bool:
        return self._cancelled or _interrupt_ctrl.is_set

    @cancelled.setter
    def cancelled(self, value: bool) -> None:
        self._cancelled = bool(value)

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def turn_closed(self) -> bool:
        return self._turn_closed

    def cancel(self) -> bool:
        """chat/cancel 入口：置 cancelled（工具调度与 LLM 循环都会轮询到）"""
        with self._lock:
            if self._turn_closed or self._aborted:
                return False
            self._cancelled = True
            return True

    # ── 流生命周期 ──

    def begin(self) -> None:
        with self._lock:
            if self._begun or self._turn_closed or self._aborted:
                return
            self._begun = True
        emit("turn/begin", {"round": self._round, "goal": self._ui.goal_state()})

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            if self._turn_closed or self._aborted:
                return
        self._end_thinking()
        emit("stream/delta", {"text": chunk})

    def notice(self, text: str) -> None:
        """框架的运行提示（压缩中、重试中、超限…），**不是** AI 说的话。

        原先进 feed() → stream/delta，客户端当成 AI 正文渲染：
        用户看到「⚠ 上下文占比超阈值」混在回复里，而且这些文本不进消息历史，
        切走再切回就消失——同一轮内容前后不一致。
        单独发 notice 事件，客户端用系统行展示。
        """
        if not text:
            return
        with self._lock:
            if self._turn_closed or self._aborted:
                return
        emit("notice", {"text": _clean(text).strip()})

    def pause_spinner(self) -> None:
        pass

    def resume_spinner(self) -> None:
        pass

    def flush_renderer(self) -> None:
        pass

    def reset_renderer(self) -> None:
        pass

    def progress_stats(self, input_tokens: int = 0, output_tokens: int = 0,
                       cache_ratio: float = 0.0, cost: float = 0.0,
                       balance: float = 0.0) -> None:
        """长任务中途补推一次 stats（不结束轮次）。

        原来 stats 只在 finish() 发，而一轮长任务（实测 26 次工具调用、
        2.5 分钟）中途不 finish，界面的「上下文 x%」整段时间都是 —，
        用户看不到上下文在长，也就无法预判什么时候会被压缩。
        """
        with self._lock:
            if self._turn_closed or self._aborted:
                return
        try:
            values = self._ui.resolve_stats(
                input_tokens, output_tokens, cache_ratio, cost, balance, "")
            emit("stats", values)
        except Exception:
            pass

    def finish(self, input_tokens: int = 0, output_tokens: int = 0,
               cache_ratio: float = 0.0, cost: float = 0.0,
               balance: float = 0.0, thinking_effort: str = "",
               with_stats: bool = True) -> None:
        """RPC 形态：stats 每次都发（协议 4.10「每轮结束时下发」）。

        with_stats 是终端"是否打印统计栏"的语义，RPC 侧照发不误；turn/end 只在
        with_stats=True 时当场下发，否则留给 RpcUI 在轮次边界收口——core 里
        with_stats=False 有两类调用：目标模式中间轮（本轮确实结束）与工具确认流程
        （本轮未结束，稍后另建新流），只有轮次边界能区分两者。
        """
        with self._lock:
            if self._turn_closed or self._aborted:
                return
        self._end_thinking()
        values = self._ui.resolve_stats(input_tokens, output_tokens, cache_ratio,
                                        cost, balance, thinking_effort)
        emit("stats", values)
        if with_stats:
            self.close_turn(self._final_reason("done"))

    def _final_reason(self, default: str) -> str:
        """这一轮真正结束时该报什么 reason。

        目标模式里 AI 调了 GoalComplete 且**真正完成**时，应报 goal_complete：
        协议 §4.13 列了这个 reason，界面也实现了「✓ 目标已完成」，
        但 core 的 run() 只 break、不传 reason，于是永远发 done——
        用户开了目标模式、任务已确认完成，界面上却只看到普通的一轮结束。

        但这两种「没真完成」的放行路径**不能**报 goal_complete，否则界面
        会给用户一个肯定性的完成确认：验证器判失败后预算耗尽强制放行、
        或验证结论存疑。这时退回 done，由 AI 的总结正文说明情况。
        （清单含未完成/受阻项不在此列：那类申报同样要过独立验证，
        阻碍被核实成立才算通过，见 agent_loop 的验证触发条件。）
        """
        if default != "done":
            return default
        tc = getattr(self._ui, "_tool_context", None)
        if tc is None or not getattr(tc, "goal_complete", False):
            return default
        for flag in ("goal_forced", "goal_suspect"):
            if getattr(tc, flag, False):
                return default
        return "goal_complete"

    def abort(self, message: Optional[str] = None) -> None:
        with self._lock:
            if self._turn_closed:
                return
            self._aborted = True
        self._end_thinking()
        if self._cancelled or _interrupt_ctrl.is_set:
            reason = "cancelled"
        else:
            reason = "error"
            if message:
                emit("error", {"message": _clean(message).strip(), "detail": None})
        self.close_turn(reason)

    def close_turn(self, reason: str, intermediate: bool = False) -> None:
        """下发 turn/end（幂等）；由 finish(with_stats=True)、abort 或轮次边界调用

        intermediate=True 表示这是「同一个用户请求内部的中间轮收口」
        （目标模式自动续跑时每轮都会新建流，上一流要被收口）。
        客户端据此不退出运行态、服务端也不解除 busy——否则目标模式跑到一半，
        界面上停止按钮就消失了，用户还能插一条新消息进来，两个轮次在同一进程
        里交错推进（实测）。协议 §4.13 的多组 turn/begin↔turn/end 由此而来。
        """
        with self._lock:
            if self._turn_closed:
                return
            self._turn_closed = True
        self._ui.emit_pending_verify_result()
        payload = {"reason": reason}
        if intermediate:
            payload["intermediate"] = True
        emit("turn/end", payload)
        self._ui.notify_turn_end(reason, self, intermediate)

    # ── 可选钩子（core 侧 getattr 探测）──

    def on_tool_call(self, name: str = None, label: str = None, dispatch: str = None,
                     summary: str = None, arguments: Optional[dict] = None,
                     call_id: str = None, **kwargs) -> None:
        call_id = call_id or kwargs.get("tool_call_id") or kwargs.get("id")
        name = name or kwargs.get("name")
        label = label or kwargs.get("label")
        dispatch = dispatch or kwargs.get("dispatch")
        summary = summary if summary is not None else kwargs.get("summary")
        if arguments is None:
            arguments = kwargs.get("arguments")
        emit("tool/call", {
            "id": call_id,
            "name": name,
            "label": label,
            "dispatch": dispatch,
            "summary": summary,
            "arguments": arguments if isinstance(arguments, dict) else {},
        })

    def on_tool_result(self, call_id: str = None, ok: bool = None,
                       duration_ms: int = None, output: str = None,
                       failed: bool = None, diff=None, **kwargs) -> None:
        call_id = call_id or kwargs.get("tool_call_id") or kwargs.get("id")
        if duration_ms is None:
            duration_ms = kwargs.get("duration_ms")
        if output is None:
            output = kwargs.get("output")
        if failed is None:
            failed = kwargs.get("failed")
        if ok is None:
            ok = kwargs.get("ok")
        text = output if isinstance(output, str) else ""
        emit("tool/result", {
            "id": call_id,
            "ok": bool(ok) if ok is not None else not bool(failed),
            "durationMs": int(duration_ms or 0),
            "output": text,
            "failed": bool(failed),
            "diff": diff,
        })
        self._maybe_bg_submit(text)

    def on_thinking_delta(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._turn_closed or self._aborted:
                return
            first = not self._thinking_started
            if first:
                self._thinking_started = True
                self._thinking_t0 = time.time()
                self._thinking_chars = 0
            self._thinking_chars += len(text)
        if first:
            emit("thinking/begin", {})
        emit("thinking/delta", {"text": text})

    # ── 内部 ──

    def _end_thinking(self) -> None:
        with self._lock:
            if not self._thinking_started:
                return
            elapsed = int(max(0.0, time.time() - self._thinking_t0) * 1000)
            chars = self._thinking_chars
            self._thinking_started = False
        emit("thinking/end", {"chars": chars, "durationMs": elapsed})

    def _maybe_bg_submit(self, output: str) -> None:
        """Shell 的 background 提交没有专用钩子，从返回文本识别后补 bg/submit"""
        hit = _BG_SUBMIT_RE.search(output or "")
        if not hit:
            return
        path = _BG_PATH_RE.search(output or "")
        emit("bg/submit", {
            "slot": hit.group(1),
            "logPath": path.group(1).strip() if path else None,
        })


def _now() -> float:
    return time.time()


class RpcUI:
    """RPC 模式 UI 总接口（与 HeadlessUI 公开方法集一致）"""

    def __init__(self, model_name: str = "", session_mgr=None, config=None,
                 tool_context=None, stats=None) -> None:
        self._model = model_name
        self._mgr = session_mgr
        self._config = config
        self._tool_context = tool_context
        self._stats = stats
        self._input_queue: "queue.Queue[str]" = queue.Queue()
        self._stream_lock = threading.RLock()
        self._active_stream: Optional[RpcStream] = None
        self._turn_end_callback = None
        self._round = 0
        self._command_lock = threading.RLock()
        # 正在等用户点「确认执行」的闸门：点停止时要能把它推开。
        # 不处理会怎样（实测）：后端阻塞在确认读上，界面已显示「已打断」，
        # 用户随后点一下确认，那条命令照样执行——停止等于没停。
        self._confirm_lock = threading.RLock()
        self._awaiting_confirm = False
        # 是否正在压缩上下文。压缩不走 _active_stream，所以「点停止」时
        # 不能靠它判断；也不该在压缩期间留中断标（会污染下一轮）。
        self._compressing = False
        self._compress_t0 = 0.0
        self._verify_open = False
        self._verify_t0 = 0.0
        self.last_command_output: List[str] = []

    def attach_stats(self, stats) -> None:
        """注入 StatsTracker（它在 assembly 第 14 步才构造，故后置回注）

        注入后 RpcStream.finish() 的 inputTokens/outputTokens/cacheRatio/cost/
        balance/thinkingEffort 缺省值回落到 stats 的当前值。
        """
        self._stats = stats

    # ── 输入通道 ──

    def push_input(self, text: str) -> None:
        """chat/send 投递（不阻塞）"""
        self._input_queue.put(text)

    def start(self) -> None:
        """无 banner、无 prompt_toolkit"""

    def read_input(self) -> Optional[str]:
        """阻塞等输入。此处即"轮次已结束"的确定性边界：进来先把悬空的 turn/end 收口。"""
        self._close_dangling("done")
        return self._input_queue.get()

    def read_input_with_prompt(self, prompt_text: str = "") -> Optional[str]:
        """确认提示（删除/git 确认）：推 confirm/request 通知，等客户端应答。

        客户端用 confirm/reply 投递 y/n（不能用 chat/send：此时轮次仍在进行中，
        chat/send 会被 busy 拒绝）。没有这条通知，用户在界面上只看到「运行中」
        却不知道后端在等他确认，等同卡死。

        prompt 里只有「确认执行此命令?」，命令内容不在里面——那是终端形态的
        写法（命令就在上面几行打印着）。面板里用户看不到命令，只能盲批准。
        所以这里额外把「这次要确认哪几条命令」也发出去（`commands`），
        由界面渲染成可读的列表。取不到就只发 prompt，不影响终端行为。
        """
        payload = {"prompt": prompt_text or "确认执行？"}
        cmds = self._pending_confirm_commands()
        if cmds:
            payload["commands"] = cmds
        emit("confirm/request", payload)
        with self._confirm_lock:
            self._awaiting_confirm = True
        try:
            return self._input_queue.get()
        finally:
            with self._confirm_lock:
                self._awaiting_confirm = False

    def _pending_confirm_commands(self) -> list:
        """从工具上下文里取「正在等确认的命令」。取不到返回空列表。

        agent_loop 在调 read_input_with_prompt 之前把待确认命令记在
        ToolContext._pending_commands（与 confirm_ids 一一对应）；这里只读，
        不改它——终端形态不用这个字段，行为完全不变。
        """
        try:
            ctx = getattr(self, "_tool_context", None)
            cmds = getattr(ctx, "_pending_commands", None) if ctx is not None else None
            if not cmds:
                return []
            out = []
            for item in list(cmds)[:8]:
                if isinstance(item, dict):
                    name = str(item.get("name") or "")
                    arg = item.get("arguments") or {}
                    if isinstance(arg, dict):
                        detail = arg.get("command") or arg.get("file_path") or ""
                    else:
                        detail = str(arg)
                    out.append({"name": name, "detail": str(detail)[:400]})
                else:
                    out.append({"name": "", "detail": str(item)[:400]})
            return out
        except Exception:
            return []

    # ── 命令 ──

    def dispatch_command(self, cmd: str, args: str):
        result, _lines = self.exec_command(cmd, args)
        return result

    def exec_command(self, cmd: str, args: str):
        """同步执行命令并收集文本输出；/exit 折算为 HANDLED（进程常驻）"""
        from ..ui.session_commands import _dispatch_command, CommandResult

        with self._command_lock:
            cap = get_transport().capture()
            with cap:
                result = _dispatch_command(cmd, args, self._mgr)
            text = _clean(cap.text)
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        self.last_command_output = lines
        if int(result) == int(CommandResult.EXIT):
            result = CommandResult.HANDLED
        return result, lines

    # ── 流 ──

    def create_stream(self) -> RpcStream:
        # 上一轮流已渲染完毕但还没收口（工具确认流程 / 目标模式中间轮）→ 先发 turn/end。
        # 但这是**中间收口**：不能解除 busy、不能让界面退出运行态，
        # 否则目标模式自动续跑的每一轮都会提前放开 busy（实测）。
        self._close_dangling("done", intermediate=True)
        self._round += 1
        stream = RpcStream(self, round_no=self._round)
        with self._stream_lock:
            self._active_stream = stream
        stream.begin()
        return stream

    def _close_dangling(self, reason: str, intermediate: bool = False) -> None:
        with self._stream_lock:
            stream = self._active_stream
        if stream is not None and not stream.turn_closed:
            stream.close_turn(reason, intermediate)

    def on_interrupted(self) -> None:
        _interrupt_ctrl.clear()

    def close_cancelled_turn(self) -> None:
        """压缩被中断时收口这一轮：发 turn/end + 解除 busy + 清中断标。

        为什么需要：压缩发生在 create_stream() **之前**，此时没有任何
        RpcStream 可以 close_turn。用户在长会话里点「停止」把压缩打断后，
        agent 主循环拿到 compress_ok=False 直接 continue 回去等下一次输入，
        这一轮永远不发 turn/end —— 服务端的 _turn_active 一直是 true，
        之后所有 chat/send 都被 busy 拒，界面也一直显示「运行中」，
        用户只能重载窗口（与已修的「命令被当对话跑」同一类致命故障）。
        """
        try:
            emit("turn/end", {"reason": "cancelled"})
        except Exception:
            pass
        callback = self._turn_end_callback
        if callback is not None:
            try:
                callback("cancelled", False)
            except TypeError:
                try:
                    callback("cancelled")
                except Exception:
                    pass
            except Exception:
                pass
        # 清掉中断标：留着会污染下一次输入（下一句话会以 cancelled 空回答被吞）
        try:
            _interrupt_ctrl.clear()
        except Exception:
            pass

    @property
    def awaiting_confirm(self) -> bool:
        """当前是否在等用户点确认（供 confirm/reply 校验，避免多余 y/n 变成对话）。"""
        with self._confirm_lock:
            return self._awaiting_confirm

    def cancel_active(self) -> bool:
        # 正在等确认时点「停止」：把确认闸门按「不确认」推开。
        # 不推会怎样（实测）：后端一直阻塞在确认读上，界面显示「已打断」，
        # 之后用户点一下确认，那条被「停止」的命令照样执行了。
        with self._confirm_lock:
            awaiting = self._awaiting_confirm
        if awaiting:
            try:
                self._input_queue.put("n")
            except Exception:
                pass
        stream = None
        with self._stream_lock:
            stream = self._active_stream
        if stream is None or not stream.cancel():
            # 没有活跃流：可能是「chat/send 已受理、create_stream 之前」的窗口，
            # 也可能是「压缩过程中」（压缩不走 _active_stream）。
            # 压缩能被中断：handle_compress 的 cancel_check 每块都查中断标，
            # 中断后 on_interrupt 收尾、agent.py 的压缩失败分支再调
            # close_cancelled_turn 收口并清标，不会污染下一轮。
            # 以前这里直接 return（不置标）→ 用户点「停止」毫无反应，
            # 压缩照常跑完 7~11 秒，这期间 chat/send 一直被 busy 拒，
            # 界面上就是「点了停止没停、再发消息也没反应」（实测）。
            if self._compressing:
                try:
                    from ..ui.interrupt import _on_esc_detected

                    _on_esc_detected(_interrupt_ctrl)
                except Exception:
                    try:
                        _interrupt_ctrl._interrupt.set()
                    except Exception:
                        pass
                return True
            try:
                from ..ui.interrupt import _on_esc_detected

                _on_esc_detected(_interrupt_ctrl)
            except Exception:
                try:
                    _interrupt_ctrl._interrupt.set()
                except Exception:
                    pass
            return awaiting
        try:
            from ..ui.interrupt import _on_esc_detected

            _on_esc_detected(_interrupt_ctrl)
        except Exception:
            _interrupt_ctrl._interrupt.set()
        return True

    def notify_turn_end(self, reason: str, stream: Optional[RpcStream] = None,
                        intermediate: bool = False) -> None:
        with self._stream_lock:
            if stream is None or self._active_stream is stream:
                self._active_stream = None
        callback = self._turn_end_callback
        if callback is not None:
            try:
                callback(reason, intermediate)
            except TypeError:
                # 兼容只收一个参数的旧回调
                try:
                    callback(reason)
                except Exception:
                    pass
            except Exception:
                pass

    def set_turn_end_callback(self, callback) -> None:
        self._turn_end_callback = callback

    # ── 统计 ──

    def resolve_stats(self, input_tokens: int = 0, output_tokens: int = 0,
                      cache_ratio: float = 0.0, cost: float = 0.0,
                      balance: float = 0.0, thinking_effort: str = "") -> Dict[str, Any]:
        """组装 stats 字段（协议 4.10）；调用方没给的取注入的 StatsTracker 当前值"""
        stats = self._stats
        if stats is not None:
            if not input_tokens:
                input_tokens = getattr(stats, "input_tokens", 0) or 0
            if not output_tokens:
                output_tokens = getattr(stats, "output_tokens", 0) or 0
            if not cache_ratio:
                cache_ratio = getattr(stats, "cache_hit_ratio", 0.0) or 0.0
            if not cost:
                cost = getattr(stats, "cost", 0.0) or 0.0
            if not balance:
                balance = getattr(stats, "balance", 0.0) or 0.0
        if not thinking_effort:
            thinking_effort = self._thinking_effort_label()
        context_window = self._context_window()
        ratio = None
        if context_window > 0 and input_tokens > 0:
            ratio = round(input_tokens / context_window, 4)
        return {
            "inputTokens": int(input_tokens or 0),
            "outputTokens": int(output_tokens or 0),
            "cacheRatio": round(float(cache_ratio or 0.0), 4),
            "thinkingEffort": thinking_effort,
            "contextRatio": ratio,
            "maxOutputTokens": int(getattr(DisplayState, "max_tokens", 0) or 0),
            "cost": round(float(cost or 0.0), 6),
            "balance": round(float(balance or 0.0), 4),
        }

    def _context_window(self) -> int:
        try:
            return int(getattr(self._config.ai, "context_window", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    def _thinking_effort_label(self) -> str:
        try:
            options = self._config.ai.thinking_options or {}
            effort = self._config.ai.thinking_effort
            return options.get(effort, effort)
        except AttributeError:
            return ""

    # ── 压缩 / 合并 / 复核 ──

    def begin_compressing(self) -> None:
        self._compress_t0 = _now()
        self._compressing = True
        emit("compress/begin", {})

    def end_compressing(self) -> None:
        elapsed = int(max(0.0, _now() - self._compress_t0) * 1000) if self._compress_t0 else 0
        self._compress_t0 = 0.0
        self._compressing = False
        emit("compress/end", {"ok": None, "before": None, "after": None,
                              "durationMs": elapsed})

    def begin_summarizing(self) -> None:
        """协议没有合并事件，终端动画在 RPC 下无意义"""

    def end_summarizing(self) -> None:
        pass

    def begin_verifying(self) -> None:
        self._verify_open = True
        self._verify_t0 = _now()
        emit("verify/start", {})

    def end_verifying(self) -> None:
        """复核调用结束。裁决行由 core 在本方法之后才打印（agent_loop 顺序：
        begin_verifying → verify() → end_verifying → show_verify_result），
        故这里不下发 verify/result——真实裁决经 install_verify_result_hook()
        接管 show_verify_result/show_verify_skip 后结构化下发。"""
        self._verify_t0 = 0.0

    def emit_verify_result(self, verdict: Optional[str], blocks=None, limit=None,
                           duration_ms=None, gaps=None) -> None:
        """下发 verify/result。没走过 verify/start 的路径（如"无需复核"）先补 start，
        保证 start/result 成对；拿不到的字段按 null 如实上报。"""
        if not self._verify_open:
            self.begin_verifying()
        self._verify_open = False
        self._verify_t0 = 0.0
        emit("verify/result", {
            "verdict": verdict,
            "blocks": blocks,
            "limit": limit,
            "durationMs": duration_ms,
            "gaps": list(gaps or []),
        })

    def emit_pending_verify_result(self) -> None:
        """轮次收口时补发缺失的 verify/result（verdict 等按 null 如实上报）"""
        if self._verify_open:
            self.emit_verify_result(None)

    # ── 其它事件 ──

    def emit_todo(self, todos) -> None:
        items = []
        for todo in todos or []:
            if not isinstance(todo, dict):
                continue
            items.append({
                "content": str(todo.get("content", "")),
                "status": todo.get("status"),
            })
        emit("todo/update", {"todos": items})

    def goal_state(self) -> Dict[str, Any]:
        mgr = self._mgr
        enabled = bool(getattr(mgr, "_goal_enabled", False))
        max_rounds = 0
        if enabled:
            max_rounds = getattr(mgr, "_goal_max_rounds", 0) or 0
            if not max_rounds and self._config is not None:
                max_rounds = getattr(self._config.ai, "goal_max_rounds", 0) or 0
        current = getattr(self._tool_context, "goal_rounds_used", 0) if self._tool_context else 0
        return {"enabled": enabled, "current": int(current or 0), "max": int(max_rounds or 0)}

    @property
    def stats(self):
        return self._stats


class RpcTodoBridge:
    """TodoWrite 清单 → todo/update 事件（assembly 注入 ToolContext.ui_callback）

    终端打印（ToolCallbacks.on_todo_update）行为保持不变，只多接一路协议事件。
    """

    def __init__(self) -> None:
        self._ui: Optional[RpcUI] = None

    def bind(self, ui: RpcUI) -> None:
        self._ui = ui

    def __call__(self, todos) -> None:
        try:
            from ..core.tool_callbacks import TodoCallbacks

            TodoCallbacks.on_todo_update(todos)
        except Exception:
            pass
        if self._ui is not None:
            try:
                self._ui.emit_todo(todos)
            except Exception:
                pass


def install_verify_result_hook(ui: RpcUI) -> None:
    """把 core/agent_loop 的复核结果下发接到 RPC

    agent_loop 用模块级名字 `_ui_verify_result` / `_ui_verify_skip` 调用
    ui_design 的打印函数（import 时就绑定，改 ui_design 属性无效），故此处替换
    agent_loop 模块上的名字。原函数照旧执行（终端文本 → stderr），只是额外把
    结构化裁决转成 verify/result 通知。不改 core/ 下任何文件。
    """
    from ..core import agent_loop as _loop

    original_result = getattr(_loop, "_ui_verify_result", None)
    original_skip = getattr(_loop, "_ui_verify_skip", None)

    def _verify_result(verdict, summary="", gaps=None, secs=0.0, blocks=0, limit=0):
        if original_result is not None:
            original_result(verdict, summary, gaps, secs, blocks, limit)
        ui.emit_verify_result(
            verdict=verdict,
            blocks=int(blocks or 0),
            limit=int(limit or 0),
            duration_ms=int(max(0.0, float(secs or 0.0)) * 1000),
            gaps=list(gaps or []),
        )

    def _verify_skip():
        if original_skip is not None:
            original_skip()
        ui.emit_verify_result(verdict="skip", blocks=0, limit=0, duration_ms=0, gaps=[])

    _loop._ui_verify_result = _verify_result
    _loop._ui_verify_skip = _verify_skip
