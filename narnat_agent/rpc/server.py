"""RPC 服务器 —— JSON-RPC 2.0 over stdio

线程模型：
  主线程   读 stdin（一行一条 JSON-RPC）→ 解析 → 分发 → 写 stdout
           请求类（initialize/session/config/command）就地执行并回响应
           chat/send 投递到 RpcUI 的输入队列后立即回 {"accepted":true}
  工作线程 跑 agent.run()，主循环里的 read_input() 阻塞在队列上取输入

stdout 只出协议消息（见 protocol.py），debug 打印一律走 stderr。
错误码：-32700 解析失败 / -32601 方法不存在 / -32602 参数非法 /
       -32603 内部错误 / -32000 业务错误。
"""

import json
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from .protocol import get_transport
from .ui import RpcUI
from ..config.defaults import DEFAULT_THINKING_PASSBACK, INTERNAL_NOTICE_MARKERS
from ..core.input_history import append_history, history_path, load_history
from ..core.session_callbacks import _persist_config

PROTOCOL_VERSION = 1

_MISSING = object()

# 「换掉上下文」类操作等当前轮次收口的时长上限（秒）。
# 不能无限等：这些请求是同步处理 stdin 一行的，等太久界面所有操作一起卡住；
# 也不能太短：工具执行被 kill 后还要走完本轮收尾（append 中断结果、turn/end）。
_TURN_SETTLE_TIMEOUT = 3.0

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""")


class RpcError(Exception):
    """业务/协议错误：带 JSON-RPC 错误码"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _read_version() -> str:
    """从 main.py 里取版本号（与 build.py 同一来源）"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "main.py")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _VERSION_RE.match(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    try:
        import main as _main

        return str(getattr(_main, "__version__", ""))
    except Exception:
        return ""


def _parent_alive(ppid: int) -> bool:
    """父进程是否还活着。

    只用 stdlib：Windows 走 ctypes OpenProcess，POSIX 走 os.kill(pid, 0)。
    拿不到就返回 True（宁可多活，也不误杀自己）。
    """
    if ppid <= 0:
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(ppid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(ppid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


class _ParentWatchdog(threading.Thread):
    """父进程消失就收尾退出。

    为什么需要它：VSCode 扩展宿主被强杀时 deactivate 不执行，且 Windows 上
    stdin 管道可能被其他进程继承持有、不产生 EOF，仅靠 readline 返回空不够。
    实测留下过孤儿 python 进程。
    """

    def __init__(self, on_dead, interval: float = 5.0) -> None:
        super().__init__(name="narnat-parent-watchdog", daemon=True)
        self._on_dead = on_dead
        self._interval = interval
        self._stop = threading.Event()
        self._ppid = os.getppid()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            if not _parent_alive(self._ppid):
                self._on_dead()
                return


class RpcServer:
    """协议分发 + agent 运行线程"""

    def __init__(self, agent) -> None:
        self._agent = agent
        self._parts = agent._parts
        self._ui: RpcUI = agent._ui
        self._mgr = self._parts.session_mgr
        self._config = self._parts.config
        self._stats = self._parts.stats
        self._transport = get_transport()
        self._turn_lock = threading.RLock()
        self._turn_active = False
        self._shutdown = threading.Event()
        self._version = ""
        self._watchdog: "Optional[_ParentWatchdog]" = None

        # 让「会话保存失败」这类提示能在界面上看见。
        # 不注册会怎样（实测）：轮末自动保存失败时提示只落到 stderr，
        # 而扩展对 stderr 只做 console.log，用户在面板上一片安静——
        # 以为存下了，其实内容还在内存里，关掉窗口就没了。
        from ..core import session_callbacks as _scb

        def _notice(text: str) -> None:
            from .ui import emit

            emit("notice", {"text": text})

        _scb.set_notify_hook(_notice)

        # 会话因「另一个实例也在写」而另存成新名字（X (2)）时，界面要把当前
        # 会话指到内容实际所在的那个名字上——否则用户回到 X 只看到对方的内容，
        # 会以为自己刚聊的那轮被吞了。core 不 import rpc，由这里注入。
        def _on_state_resaved(name: str) -> None:
            from .ui import emit as _emit_resaved

            _emit_resaved("session/saved", {"name": name,
                                            "current": self._current_path() or name,
                                            "switched": True})

        _scb.set_resaved_hook(_on_state_resaved)

        # 自动保存完成 → 通知界面（协议 session/saved）。core 不 import rpc，
        # 所以由这里注入回调；回调在自动保存的后台线程里执行。
        # 不注册的话：磁盘上已有会话文件，进程内状态却仍是 NoSession，
        # 界面一直显示「未保存」，也没人告诉用户这段内容已经存好了。
        from .ui import emit as _emit

        def _on_session_saved(name: str) -> None:
            _emit("session/saved", {"name": name, "current": None, "switched": False})

        def _on_session_switched(name: str) -> None:
            _emit("session/saved", {"name": name,
                                    "current": self._current_path() or name,
                                    "switched": True})

        try:
            self._parts.auto_save_mgr.set_hooks(on_saved=_on_session_saved,
                                                on_switched=_on_session_switched)
        except Exception:
            pass   # 钩子注册失败不该挡住后端启动

        self._methods = {
            "initialize": self._m_initialize,
            "chat/send": self._m_chat_send,
            "chat/cancel": self._m_chat_cancel,
            "confirm/reply": self._m_confirm_reply,
            "command/exec": self._m_command_exec,
            "session/list": self._m_session_list,
            "session/new": self._m_session_new,
            "session/switch": self._m_session_switch,
            "session/delete": self._m_session_delete,
            "session/rename": self._m_session_rename,
            "session/explore": self._m_session_explore,
            "session/done": self._m_session_done,
            "config/set": self._m_config_set,
            "config/describe": self._m_config_describe,
            "narnatmd/get": self._m_narnatmd_get,
            "narnatmd/set": self._m_narnatmd_set,
            "shutdown": self._m_shutdown,
        }

    # ── 入口 ──

    def serve(self) -> int:
        self._version = _read_version()
        self._ui.set_turn_end_callback(self._on_turn_end)
        worker = threading.Thread(target=self._run_agent, name="narnat-agent", daemon=True)
        worker.start()
        # 父进程（扩展宿主）消失就自杀，避免成为孤儿进程
        self._watchdog = _ParentWatchdog(self._on_parent_dead)
        self._watchdog.start()
        stdin = getattr(sys.stdin, "buffer", sys.stdin)
        while not self._shutdown.is_set():
            try:
                raw = stdin.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = raw
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            self._handle_line(line)
        self._finish()
        return 0

    def _on_parent_dead(self) -> None:
        """看门狗回调：父进程没了，立刻收尾退出（在独立线程里跑）"""
        if self._shutdown.is_set():
            return
        print("[narnat] 父进程已退出，后端随之关闭", file=sys.stderr)
        self._finish()

    def _run_agent(self) -> None:
        try:
            self._agent.run()
        except SystemExit:
            pass
        except BaseException as e:  # 工作线程异常不能让主线程静默挂住
            traceback.print_exc(file=sys.stderr)
            try:
                from .ui import emit

                emit("error", {"message": "agent 主循环异常退出", "detail": str(e)})
            except Exception:
                pass
            self._finish()

    def _finish(self) -> None:
        """收尾：会话落盘 + 后台任务回收 + 延迟删除 + MCP 回收，最多等 1.2 秒后硬退出"""
        # 先在当前线程同步落盘一次，且**不依赖 LLM 命名**：on_exit 里的 wait()
        # 要等后台命名线程最多 5 秒，而这里只给 1.2 秒——命名没回来就 os._exit，
        # 最后一段整段丢失、退出码还是 0（P0-3，实测 2/2）。exit_flush 走的是
        # 「快速截断名 + unique_session_name + save_session」，纯本地写、毫秒级；
        # 游离态与已命名会话/分支两种状态都覆盖，已经在磁盘上的那段不重复保存。
        try:
            self._parts.session_mgr.exit_flush()
        except Exception as e:
            # 退出时用户可能已经看不到界面，但日志要留痕
            sys.stderr.write(f"[narnat] 退出前保存会话失败: {type(e).__name__}: {e}\n")
            sys.stderr.flush()

        # 队列里还有没被 agent 取走的输入（压缩窗口/长任务期间用户又发的话）：
        # exit_flush 只能落盘消息列表里的内容，这些输入谁也存不到。必须给一行
        # 可读提示——否则用户看到的是安静退出，那句话永久消失且毫无线索（P0）。
        queue = getattr(self._ui, "_input_queue", None)
        pending_n = queue.qsize() if hasattr(queue, "qsize") else 0
        if pending_n:
            sys.stderr.write(
                f"[narnat] 退出时还有 {pending_n} 条输入未被处理，未能写入会话文件\n")
            sys.stderr.flush()

        # 压缩仍在进行就退出（用户刚发的那句话还没走完摘要）：同样不能静默。
        # 这句话本身已在压缩开始前进了消息列表，exit_flush 能落盘；提示是为了让
        # 「这一轮没跑完」有迹可循——退出码 0 + 零输出会让用户以为一切正常（P0）。
        if getattr(self._ui, "_compressing", False):
            sys.stderr.write("[narnat] 压缩未完成即退出，当前对话内容已落盘\n")
            sys.stderr.flush()

        # 后台任务回收必须同步做完：这里末尾是 os._exit(0)，不走任何
        # finally/atexit，而 agent.run() 的 finally（cleanup_all）根本没机会跑。
        # 漏在这一步会怎样（实测）：后端退出后它提交的 cmd.exe 仍在跑，
        # ppid 指向一个已经不存在的进程——继续烧 CPU、继续改工作区。
        # 只终止本进程自己 Popen 出来的任务（后台模块进程表里的 pid）。
        try:
            from ..tools.background import shutdown_cleanup as _bg_shutdown

            killed = _bg_shutdown()
            print(f"[narnat] 已强制终止 {killed} 个后台任务", file=sys.stderr)
        except Exception:
            pass

        def _cleanup():
            try:
                self._parts.auto_save_mgr.on_exit()
            except Exception:
                pass
            try:
                self._parts.mcp_manager.cleanup()
            except Exception:
                pass
            try:
                self._parts.logger.close()
            except Exception:
                pass

        thread = threading.Thread(target=_cleanup, daemon=True)
        thread.start()
        thread.join(1.2)
        sys.stderr.flush()
        os._exit(0)

    # ── 消息收发 ──

    def _send(self, payload: Dict[str, Any]) -> None:
        try:
            line = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self._transport.write_line(line)

    def _send_result(self, msg_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _send_error(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"jsonrpc": "2.0", "id": msg_id, "error": error})

    def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            self._send_error(None, -32700, "Parse error")
            return
        except RecursionError:
            # 超深嵌套的 JSON（如一行里 1000 层 [）会让 json 解析器递归爆栈。
            # 不单独接住的话，这条异常会冲出读循环把整个后端点掉（实测退出码 1），
            # 用户看到的是「后端没了」而不是「这条请求有问题」。
            self._send_error(None, -32700, "Parse error: 请求嵌套过深")
            return
        if not isinstance(msg, dict):
            self._send_error(None, -32600, "Invalid Request")
            return
        msg_id = msg.get("id", _MISSING)
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            self._send_error(None if msg_id is _MISSING else msg_id,
                             -32600, "Invalid Request: method 缺失")
            return
        handler = self._methods.get(method)
        if handler is None:
            self._send_error(None if msg_id is _MISSING else msg_id,
                             -32601, "Method not found")
            return
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self._send_error(None if msg_id is _MISSING else msg_id,
                             -32602, "Invalid params: params 必须是对象")
            return
        try:
            result = handler(params)
        except RpcError as e:
            self._send_error(None if msg_id is _MISSING else msg_id,
                             e.code, e.message, e.data)
            return
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            # 不能把 {e} 原文抛给界面：那里面通常带绝对路径和系统错误码
            # （如 [WinError 32] 另一个程序正在使用此文件: 'C:\Users\...'），
            # 用户既看不懂也做不了什么。给一句人话，原始信息留在 stderr。
            self._send_error(None if msg_id is _MISSING else msg_id,
                             -32603, _humanize_internal_error(e))
            return
        if msg_id is _MISSING or msg_id is None:
            return
        self._send_result(msg_id, result)

    # ── 参数校验 ──

    @staticmethod
    def _require_str(params: Dict[str, Any], key: str, method: str,
                     allow_empty: bool = False) -> str:
        value = params.get(key, _MISSING)
        if value is _MISSING or not isinstance(value, str):
            raise RpcError(-32602, f"{method}: params.{key} 缺失或不是字符串")
        if not allow_empty and not value.strip():
            raise RpcError(-32602, f"{method}: params.{key} 不能为空")
        return value

    def _on_turn_end(self, _reason: str, intermediate: bool = False) -> None:
        # intermediate=True 是「同一用户请求内部的中间轮收口」（目标模式自动续跑）。
        # 这时**不能**解除 busy：解了之后用户能在目标跑到一半时再发一条消息，
        # 两个轮次在同一进程里交错推进（实测：一次目标模式请求产生 3 组
        # turn/begin↔turn/end，中间那条一到 busy 就放开）。
        if intermediate:
            return
        with self._turn_lock:
            self._turn_active = False

    # ── 方法：initialize ──

    def _m_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ai = self._config.ai
        mgr = self._mgr
        enabled = bool(getattr(mgr, "_goal_enabled", False))
        max_rounds = getattr(mgr, "_goal_max_rounds", 0) or 0
        if not max_rounds:
            max_rounds = getattr(ai, "goal_max_rounds", 0) or 0
        # 面板一打开就查一次余额：否则要等到第 1 轮对话开始才查，
        # 在那之前界面只能显示余额 0。用后台线程，不让网络请求卡住握手。
        self._kick_balance_probe()
        return {
            "version": self._version,
            "protocol": PROTOCOL_VERSION,
            "model": {"current": ai.model, "options": list(ai.model_options or [])},
            "thinking": {"effort": ai.thinking_effort,
                         "options": dict(ai.thinking_options or {}),
                         "passback": bool(getattr(ai, "thinking_passback",
                                                  DEFAULT_THINKING_PASSBACK))},
            "goal": {"enabled": enabled, "maxRounds": int(max_rounds)},
            "plugins": self._plugins(),
            "session": self._session_brief(),
            "workdir": os.getcwd(),
            # 配置文件的加载警告（暂时取一次即清空）。配置坏掉时后端会按默认
            # 配置继续跑，界面上一切正常，直到发消息收到 401 —— 用户会以为是
            # 密钥失效，实际是整份配置（含密钥）被丢弃了（实测）。
            "configWarnings": _take_config_warnings(),
            # 当前模型对应的接口密钥是否为空。首屏据此提前给一句「还没填 Key」，
            # 而不是等用户发完第一条消息、撞上整段英文 401 JSON 才知道
            #（新用户很容易把它判成"插件坏了"）。
            "apiKeyMissing": not bool(_model_api_key(self._config)),
            # 输入框按 ↑ 能翻到的历史（最旧在前）。与终端形态同一份文件，
            # 所以关掉 VSCode/重连后照样翻得到上次的提问。
            "inputHistory": load_history(history_path(self._config.paths.data_dir), 100),
        }

    def _kick_balance_probe(self) -> None:
        """后台查一次余额，成功后补发 stats 让界面立刻显示。"""
        ai = self._config.ai
        api_key = getattr(ai, "api_key", None)
        if not api_key:
            return
        if not getattr(self._config.balance, "enabled", False):
            return

        def _probe():
            try:
                # 用 round=1 是因为它必然触发查询（stats.fetch_balance 里
                # 「没查到过就立刻查」）。旧实现按 round%10==0 判，这里传 1
                # 永远不成立 —— 这个探针自始至终一次都没查过，界面只能显示 0。
                self._stats.fetch_balance(api_key, 1)
                # 判 balance_known 而不是 balance > 0：账户真的花光时也该
                # 如实推 ¥0.00，而不是留着「—」让人以为没查到。
                if not self._stats.balance_known:
                    return
                # 与 RpcStream.finish 走同一条下发通道（ui.emit_stats，内容相同则去重），
                # 界面收到即显示
                self._ui.emit_stats(self._ui.resolve_stats())
            except Exception:
                pass   # 余额查不到不影响使用

        threading.Thread(target=_probe, name="narnat-balance", daemon=True).start()

    def _plugins(self):
        from ..tools.registry import (PLUGIN_TOOL_NAMES, get_capability,
                                      get_plugin_states)

        states = get_plugin_states()
        items = []
        for name in PLUGIN_TOOL_NAMES:
            cap = get_capability(name)
            items.append({
                "name": name,
                "label": cap.get("plugin_label") or cap.get("label") or name,
                "enabled": bool(states.get(name, False)),
            })
        return items

    def _current_path(self) -> Optional[str]:
        state = self._mgr.state
        name = state.session_name()
        if not name:
            return None
        parent = state.session_parent()
        return f"{parent}/{name}" if parent else name

    def _session_brief(self) -> Dict[str, Any]:
        from ..config.session_store import list_sessions_tree

        state = self._mgr.state
        name = state.session_name()
        parent = state.session_parent()
        children = []
        root_name = parent or name
        if root_name:
            try:
                tree = list_sessions_tree(self._mgr.narnat_dir)
            except Exception:
                tree = []
            for root in tree:
                if root.get("name") == root_name:
                    children = [c.get("name") for c in root.get("children", [])]
                    break
        current = f"{parent}/{name}" if parent and name else name
        # isChild：是否是探索分支（子会话）。
        # 界面不能靠「current 里有 /」推断——会话名本身允许带 /（/save fix/a/b），
        # 那样会把普通会话误判成分支，进而禁掉「新建探索分支」。
        #
        # messages：把当前会话的历史一起带上。重载窗口后 webview 会重建，
        # 原来只回 current/children/isChild，界面于是清空且不再回填 ——
        # 用户以为刚才那几十分钟的对话丢了（实测重载后对话区空白，
        # 只剩一行「已连接后端」）。历史本来就在内存里，顺手带走即可。
        try:
            messages = _history_payload(self._mgr.get_full_messages())
        except Exception:
            messages = []
        return {"current": current, "children": children,
                "isChild": bool(parent), "messages": messages}

    # ── 方法：chat ──

    def _m_chat_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        text = self._require_str(params, "text", "chat/send", allow_empty=True)
        if not text.strip():
            raise RpcError(-32602, "chat/send: params.text 不能为空")
        # 以 / 开头的文本是命令，不是对话。客户端本该在发之前分流，但
        # 只要漏一处（实测：点「发送」按钮发 /mode）后果就是致命的——
        # 命令被当对话跑完，这一轮不产生 turn/end，_turn_active 永久为 true，
        # 之后所有消息都被 busy 拒绝、点停止也无效果，只能重载窗口。
        # 这里兜底：把命令执行掉并立刻回执，绝不进入轮次。
        stripped = text.strip()
        if stripped.startswith("/"):
            head, _, rest = stripped[1:].partition(" ")
            if head:
                result = self._m_command_exec(
                    {"command": head, "args": rest.strip()})
                if result.get("handled"):
                    return {"accepted": False, "handledAsCommand": True,
                            "command": head, "result": result}
                # 未注册的命令（如 /nosuchcmd）：不能当需求发给模型。
                # 前端已经拦了一道，但后端必须自己也能拦——不能只有前端守着
                # （老版本前端、第三方客户端都会漏过来，实测）。
                # 以斜杠开头的**路径**（/usr/local/bin）仍按对话发，不拦。
                low = head.lower()
                looks_like_path = any(ch in low for ch in "/\\.-") or len(low) > 14
                if not looks_like_path:
                    return {"accepted": False, "handledAsCommand": True,
                            "command": head,
                            "result": {"handled": True, "ok": False, "noop": False,
                                       "lines": ["没有 /" + head + " 这个命令。"
                                                 "输入 / 可查看全部命令，"
                                                 "或换个说法描述你的需求"],
                                       "stateChanged": []}}
        with self._turn_lock:
            if self._turn_active:
                raise RpcError(-32000, "busy")
            self._turn_active = True
        try:
            self._ui.push_input(text)
        except Exception as e:
            with self._turn_lock:
                self._turn_active = False
            raise RpcError(-32603, f"chat/send: 投递失败: {e}")
        # 记进输入历史（与终端形态同一个文件）。放在投递成功之后：被 busy 拒掉
        # 的那条从没发给模型，进了历史用户下次按 ↑ 会以为自己问过。
        # / 开头的命令走上面提前 return 的分支，天然不记（面板的 ↑ 也不在 /
        # 开头时翻历史，两边一致）。写失败不影响本条消息（append_history 不抛）。
        append_history(history_path(self._config.paths.data_dir), text)
        return {"accepted": True}

    def _m_chat_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cancelled = self._ui.cancel_active()
        # 「流还没建但轮次在跑」的窗口：cancel_active 拿不到流，会返回 False，
        # 但这一轮确实在跑（_turn_active 为 true）。中断标已经置上了，
        # 对用户来说这次「停止」是有效的——必须报 cancelled:true，
        # 否则界面把停止按钮收掉，而后端仍是 busy，用户既停不下也发不出去
        #（实测 1/8 概率复现）。
        with self._turn_lock:
            still_active = self._turn_active
        if still_active and not cancelled:
            cancelled = True
        # 一并停掉 agent 自己派出去的后台任务。
        # 不这么做会怎样（实测）：界面上写着「已打断 / 可继续输入」，
        # 但它后台提交的子进程还在跑——继续产出、继续计费、继续改工作区，
        # 而用户以为已经停了。停止按钮的心智就是"停下来"。
        bg_cancelled = 0
        try:
            from ..tools.background import cancel_all

            bg_cancelled = cancel_all()
        except Exception:
            bg_cancelled = 0
        if not cancelled:
            with self._turn_lock:
                active = self._turn_active
            if not active:
                # 协议约定：无论是否真打断，服务端都补一条 turn/end
                from .ui import emit

                emit("turn/end", {"reason": "cancelled"})
        return {"cancelled": cancelled, "bgCancelled": bg_cancelled}

    def _m_confirm_reply(self, params: Dict[str, Any]) -> Dict[str, Any]:
        text = self._require_str(params, "text", "confirm/reply")
        # 与 chat/send 走同一条输入队列，但不做 busy 检查：
        # 确认发生在轮次中途，此时 chat/send 必然被拒。
        # 但必须校验「确实在等确认」：不在等确认时把文本丢进队列，
        # 它会变成一条用户消息发给模型（实测：用户多点了一次「取消」，
        # 模型收到一个孤零零的 "n"，回一段莫名其妙的话）。
        awaiting = getattr(self._ui, "awaiting_confirm", None)
        if awaiting is False:
            # 幂等：重复应答直接忽略，不当成用户输入
            return {"accepted": False, "text": text, "ignored": True,
                    "reason": "no pending confirm"}
        self._ui.push_input(text)
        return {"accepted": True, "text": text}

    def _m_command_exec(self, params: Dict[str, Any]) -> Dict[str, Any]:
        command = self._require_str(params, "command", "command/exec")
        args = params.get("args", "")
        if args is None:
            args = ""
        if not isinstance(args, str):
            raise RpcError(-32602, "command/exec: params.args 必须是字符串")
        command = command.lstrip("/")

        # /rm 在面板形态下必须立即删除。
        # 终端形态的 /rm 只是「标记删除、退出时生效」（终端里打错字撤不回来），
        # 但面板里抽屉的删除按钮本来就是立即删的——同一个操作两套语义，
        # 用户打 /rm 会以为已经删了，实际要等到关窗口才生效，列表里也看不出标记。
        if command == "rm" and args.strip():
            target = args.strip()
            # 删光全部会话不可恢复，必须再确认一次。
            # 不加这层会怎样（实测）：在输入框随手打一句 "/rm --all"，
            # 全部会话立刻消失，没有任何确认，也没有撤销——
            # 用户可能只是在问这个命令是干什么的。面板里逐个删除、
            # 以及终端形态的标记删除都不走这条，不受影响。
            low = target.lower()
            if low == "--all":
                return {"handled": True, "ok": False, "noop": False,
                        "lines": ["删除全部会话不可恢复。确认删除请执行: /rm --all confirm"],
                        "stateChanged": []}
            if low in ("--all confirm", "--all yes", "--all -y"):
                return self._rm_all_sessions()
            # 危险保护：在子会话里 /rm 它的父会话，会把整个目录（含用户
            # 当前所在的分支）一起删掉——用户正在干的活连带消失，而且
            # 命令表里 /rm 在子会话状态本来就写着「不可用」，两处口径不一致。
            # 这里拦下来，让用户先 /cd 回父会话再删，意图更明确。
            cur_parent = self._mgr.state.session_parent()
            if cur_parent:
                try:
                    _t, _p, _err = self._mgr.resolve_session_name(target)
                except Exception:
                    _t, _p, _err = None, None, ""
                if not _err and _p is None and _t == cur_parent:
                    return {"handled": True, "ok": False, "noop": False,
                            "lines": [f"会话「{cur_parent}」正在被当前分支使用，"
                                      f"删除会连带丢掉当前分支的内容。"
                                      f"请先 /cd {cur_parent} 回到它，再执行 /rm"],
                            "stateChanged": []}
            try:
                res = self._m_session_delete({"name": target})
            except RpcError as e:
                return {"handled": True, "ok": False, "noop": False,
                        "lines": [str(e.message)], "stateChanged": ["session"]}
            return {"handled": True, "ok": True, "noop": False,
                    # broken：删的是列表里看不到的损坏文件，文案要如实说
                    # 「清理」而不是「删除了某会话」，否则用户不知道发生了什么
                    "lines": [("已清理损坏的会话文件: " if res.get("broken")
                               else "已删除会话: ") + res.get("name", target)],
                    "wasCurrent": bool(res.get("wasCurrent")),
                    "stateChanged": ["session"]}

        # /cd 会进另一个会话（enter → replace_messages）：不先打断就串台。
        # 实测：正在跑的那一轮正文会写进刚切过去的会话，原会话只剩一句提问。
        if command in ("cd", "explore", "done"):
            self._cancel_and_wait_turn()

        result, lines = self._ui.exec_command(command, args)
        handled = int(result) != 0
        # 成败以命令层返回的结构化结果为准（HANDLED=1 / FAILED=3）。
        # 不再靠猜输出文案：各个 handler 从 mgr 拿到的是「""=成功/非空=错误」，
        # 这层已经把它们折成 FAILED。靠文案猜必然两头出错——
        # 会话名叫「登录失败排查」时成功被打叉，「写入新会话失败」这类真错误漏判。
        ok = handled and int(result) != 3
        payload = {
            "handled": handled,
            "ok": ok,
            # noop：执行了但无变更（如 /compact 时没有历史可压缩）。
            # 界面据此显示中性行，不打「✓ 已执行」的勾。
            "noop": bool(handled and ok and _looks_noop(lines)),
            "lines": lines,
            "stateChanged": _state_changes(command),
        }
        # /cd、/explore、/done 都换了当前会话：必须把切换后的历史一起回给界面。
        # 不回会怎样（实测）：顶栏 chip、抽屉高亮、后端 current 都切到了 B，
        # 对话区还显示 A 的消息——用户会在「B 的标题 + A 的内容」下继续提问，
        # 或以为历史丢了。session/switch 那条路径一直是回 messages 的，
        # 命令这条原来只有 /cd 带。
        # /done 尤其不能漏：它把分支结论合并进父会话并 replace_messages，
        # 前端不重画的话用户只看到一行「结论已合并」，对话区里却没有结论
        #（P1，第 11 轮剧本实测）。
        #
        # 判据沿用本函数既有的两项（handled/ok 与 noop），不新造：
        #   · ok：只有成功路径才带。失败（FAILED）也带的话，前端会把一次失败
        #     当成「切换成功」去重画，与 R19 修好的 ok:false + 可读原因口径相悖。
        #   · noop：执行了但无变更（如 /done 时分支没有新增量）不带。
        # messages 用与 session/switch 完全相同的 _history_payload 口径，
        # 前端画出来的结果与切会话一致。
        if command in ("cd", "explore", "done") and handled and ok and not payload["noop"]:
            state = self._mgr.state
            history = self._mgr.get_full_messages()
            payload["current"] = self._current_path()
            payload["isChild"] = bool(state.session_parent())
            payload["messages"] = _history_payload(history)
            # 合并进父会话的「# 子会话 [X] 结论」是 system 消息，_history_payload
            # 对它保留了例外，所以结论就在上面这串 messages 里。
            #
            # 但前端的 command/result 渲染只对 command==='cd' 调 renderHistory
            #（media/narnat.js: cdSwitched），仅带 messages 还画不出来——
            # 实测：把带 messages 的 /done 回执喂给真实界面，#convo 里只有
            # 命令行、.msg 节点数为 0。前端不在本轮允许改动的范围内，
            # 所以把结论原文一并放进 lines：命令回执本来就是用户看得见的地方，
            # 与终端形态的「结论已合并」不冲突（终端回执只有一行）。
            if command == "done":
                for msg in reversed(history):
                    if (msg.get("role") == "system"
                            and str(msg.get("content", "")).startswith("# 子会话 [")):
                        payload["lines"] = list(lines) + str(msg["content"]).splitlines()
                        break
            # 游离态切走时，NoSession.enter 已把上一段兜底存成一条会话（P0：
            # 默认 自动保存=false 时状态长期停在 NoSession，原来 /cd 会把刚聊的
            # 整段丢掉、回执还说 ok:true）。存成了哪个名字必须让用户看得见——
            # 终端那一行由 enter 直接打印（RPC 下被 exec_command 捕获进 lines），
            # 这里再把同一个名字做成结构化字段，与 session/new 的 autoSaved 同口径。
            if command == "cd":
                auto = getattr(self._mgr, "_last_auto_saved", None)
                if auto:
                    payload["autoSaved"] = auto
        return payload

    # ── 方法：session ──

    def _m_session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..config.session_store import list_sessions_tree_checked, session_tree_payload

        show_all = params.get("all", False)
        if show_all is None:
            show_all = False
        if not isinstance(show_all, bool):
            raise RpcError(-32602, "session/list: params.all 必须是布尔值")
        tree, list_err = list_sessions_tree_checked(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        state = self._mgr.state
        payload = session_tree_payload(tree, state.session_name(),
                                       state.session_parent(), show_all=show_all)
        payload["isChild"] = bool(state.session_parent())
        # 读不到会话目录时必须说出来：只回空 groups 等于告诉用户「你的会话全没了」。
        # 契约：error 字段只在读失败时出现（非空字符串），此时 groups 可能为空或
        # 不完整；空目录（真的没有会话）不带该字段。前端据此显示一行错误，
        # 而不是把列表当空的。
        if list_err:
            payload["error"] = list_err
        return payload

    def _m_session_new(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """新建对话：回游离态 + 清空上下文（未保存的对话直接丢弃）。

        若有轮次在跑，先打断**并等它收口**再新建——用户点「新建对话」本意就是
        不要当前这段，让他先手动点停止是多余的摩擦；但只打断不等待会让这一轮
        后续的工具结果 append 进刚清空的新对话（P0，实测 9/13）。
        """
        self._settle_before_context_change("新建")
        # 先让轮末自动保存收尾：它可能在后台线程里正给这段内容命名落盘。
        # 不等它就 on_new，会各存一份（实测出现过两个同名会话）。
        auto = None
        try:
            auto = self._parts.auto_save_mgr.wait()
        except Exception:
            auto = None
        err = self._mgr.on_new()
        if err:
            raise RpcError(-32000, err)
        # autoSaved：新建前那段内容被自动存成的会话名（None=本来就是空对话）。
        # 界面据此告诉用户「已自动保存为 xxx」，而不是让人以为内容丢了。
        return {"current": None, "messageCount": len(self._mgr.get_messages()),
                "autoSaved": auto or self._mgr._last_auto_saved,
                "messages": _history_payload(self._mgr.get_full_messages())}

    def _preflight_switch_target(self, name: str):
        """只读校验目标会话是否进得去。返回 (target, parent, error)。

        必须在 cancel_active() **之前**做完：「切到不存在的会话」是个失败请求，
        不该顺带把正在跑的这一轮打断（P1，实测：文案只说会话不存在，
        用户那一轮却已经没了）。
        判据用「文件是否存在」+ 沿用加载路径的原始错误文案，
        与 on_enter() 的成功/失败口径一致（幽灵父、损坏文件都能如实报出）。
        """
        from ..config.session_store import load_session, session_exists

        mgr = self._mgr
        target, parent, err = mgr.resolve_session_name(name)
        if err:
            return None, None, err
        if not target:
            return None, None, f"会话不存在: {name}"
        try:
            exists = session_exists(mgr.narnat_dir, target, parent=parent)
        except Exception:
            exists = False
        if exists:
            return target, parent, ""
        # 名字解析得到但磁盘上没有（幽灵父 / 损坏文件）：把加载路径的原话带回去
        load_err = ""
        try:
            if parent is not None:
                _msgs, load_err = mgr.load_child_with_boundary(target, parent)
            else:
                _msgs, load_err = load_session(mgr.narnat_dir, target)
        except Exception as e:
            load_err = f"读取会话失败: {e}"
        return None, None, load_err or f"会话不存在: {name}"

    def _m_session_switch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = self._require_str(params, "name", "session/switch")
        # 先校验目标存在，再决定要不要打断当前轮次：
        # 目标不存在时这一轮不该被殃及（P1）。校验是纯读，不动任何状态。
        _target, _parent, pre_err = self._preflight_switch_target(name)
        if pre_err:
            raise RpcError(-32000, pre_err)
        # 有轮次在跑时必须先打断并等它收口再切。
        # 不打断会怎样：on_enter() 用 replace_all 就地换掉 MessageList，
        # 而正在跑的 agent_loop 持有同一个对象，继续 append_assistant /
        # append_tool_result —— 这一轮的正文和几十条工具记录全写进**新**会话，
        # 原会话只剩一句提问、永远得不到回答。不可恢复（实测 30 条工具结果串台）。
        self._settle_before_context_change("切换会话")
        # 切走前先把游离态那一段兜底存下来：这件事由状态机自己做
        # （NoSession.enter → carry_over，赶在 replace_messages 之前），
        # 这里不再单独调一次——两处都调会把同一段内容存出两个文件
        # （实测 "X.json" 与 "X (2).json" 各一份）。
        err = self._mgr.on_enter(name)
        if err:
            raise RpcError(-32000, err)
        return {"current": self._current_path(),
                "messageCount": len(self._mgr.get_messages()),
                "isChild": bool(self._mgr.state.session_parent()),
                "messages": _history_payload(self._mgr.get_full_messages()),
                # 由 NoSession.enter 填（与 /cd 回执同一份结果）
                "autoSaved": getattr(self._mgr, "_last_auto_saved", None)}

    def _rm_all_sessions(self) -> Dict[str, Any]:
        """面板形态的 /rm --all：把会话列表里所有根会话逐个删掉。

        不能用「把 --all 当会话名」去删（会报「会话不存在: --all」什么也不删），
        也不能沿用终端的「标记删除、退出时生效」——面板里删除就是立即删。
        删当前会话会让状态回游离态，最后统一报实际删了几个。
        """
        from ..config.session_store import list_sessions_tree

        try:
            tree = list_sessions_tree(self._mgr.narnat_dir)
        except Exception as e:
            return {"handled": True, "ok": False, "noop": False,
                    "lines": [f"读取会话列表失败: {e}"], "stateChanged": ["session"]}

        names = [r.get("name") for r in tree if r.get("name")]
        if not names:
            return {"handled": True, "ok": True, "noop": True,
                    "lines": ["没有可删除的会话"], "stateChanged": ["session"]}

        deleted = 0
        failed = []
        # 删之前记住「当前是否有会话」：删光之后状态一定是游离态，
        # 界面必须跟着清空对话区。不返回 wasCurrent 会怎样（实测）：
        # 面板里 /rm --all confirm 之后所有会话都没了，屏幕上还留着整段内容，
        # 与「后端上下文为空」矛盾——用户以为那些对话还在。
        was_current = self._current_path() is not None
        for name in names:
            try:
                self._m_session_delete({"name": name})
                deleted += 1
            except RpcError as e:
                failed.append(f"{name}: {e.message}")
        lines = [f"已删除 {deleted} 个会话"]
        if failed:
            lines.append("未删除：" + "; ".join(failed[:3]))
        return {"handled": True, "ok": not failed, "noop": deleted == 0,
                "lines": lines, "stateChanged": ["session"],
                "wasCurrent": was_current, "current": self._current_path()}

    def _delete_dangling_parent(self, name: str) -> Dict[str, Any]:
        """删掉「父 json 已丢、只剩 <父>/ 子会话文件」的幽灵父会话。

        父文件不在，delete_session 无从下手（回「会话不存在」），所以直接清整棵
        子树：子会话 json 全删、空目录一并删掉，清完这个名字即可复用。
        当前若停在这个幽灵父（或它的子会话）下，先复位状态再删：不复位的话
        current 还指着刚被删掉的名字，下一次保存会把它写回磁盘（实测）。
        成功给真实清理条数，失败抛带原因的 RpcError——没有回执用户只会重复点。
        """
        mgr = self._mgr
        cur_parent = mgr.state.session_parent()
        was_current = (self._current_path() == name) or (cur_parent == name)
        if was_current:
            self._settle_before_context_change("删除会话")
            mgr.on_new()
        # CLI 可能遗留同一目标待删标记，一并清掉
        mgr.pending_deletes.discard((name, None))
        try:
            removed = _rm_session_tree(mgr.narnat_dir, name)
        except OSError as e:
            raise RpcError(-32000,
                           f"删除会话失败：幽灵父「{name}」的文件被占用（{e}）。"
                           f"请关闭占用它的程序后重试")
        # 复查：rmtree 半途失败或文件被别的进程重建时不能报成功
        if _is_dangling_parent(mgr.narnat_dir, name):
            raise RpcError(-32000,
                           f"删除未完成：幽灵父「{name}」下仍有子会话文件没能删掉"
                           f"（可能被其它程序占用），请关闭后重试")
        return {"deleted": True, "name": name, "parent": None, "ghost": True,
                "childrenRemoved": removed, "wasCurrent": was_current,
                "current": self._current_path()}

    def _m_session_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """删除会话：允许删任意一个，并立刻落盘。

        与 CLI 的 /rm 有两处语义差异，都是面板场景决定的：
        1. CLI 的 RootSession 只允许删「自己的子会话」（防止在分支上下文里
           误删无关会话）；面板展示的是全部会话列表，用户点哪个就是要删哪个，
           按当前会话限制会直接报「不是当前会话的子会话」。
        2. CLI 把删除记成「退出时生效」的标记；面板没有退出这个时机，
           标记了就再也不会被执行（后端是长驻进程），所以这里直接删文件。
        """
        from ..config.session_store import delete_session

        name = self._require_str(params, "name", "session/delete")
        mgr = self._mgr
        target, parent, err = mgr.resolve_session_name(name)
        # 幽灵父会话（父 json 已丢、<父>/ 下还有子会话文件）必须先于常规删除处理。
        # resolve_session_name 对 ghost root 是「解析成功」（tree 里就有这一行），
        # 于是常规流程去 delete_session 时找不到父文件、只回「会话不存在」，
        # 子文件与目录永远清不掉、名字永远占着（实测 P1）。
        ghost = None
        if parent is None and target and _is_dangling_parent(mgr.narnat_dir, target):
            ghost = target
        elif _is_dangling_parent(mgr.narnat_dir, name):
            # 子文件全是坏 JSON 时 tree 里推不出 ghost 行，resolve 会报「不存在」：
            # 按入参再兜一次，否则这种残骸同样删不掉。
            ghost = name
        if ghost is not None:
            return self._delete_dangling_parent(ghost)
        if err:
            # 目标是「只剩空目录」的残骸（目录里一个 json 都没有）时
            # resolve_session_name 会认不出来。这里兜底删掉这个空目录：
            # 否则用户永远清不掉它，磁盘上一直留个空壳（实测），
            # 而且界面上一条对应记录都没有，没人知道它还在。
            if _sweep_empty_session_dir(mgr.narnat_dir, name):
                return {"deleted": True, "name": name, "parent": None,
                        "wasCurrent": False, "current": self._current_path()}
            # 目标是「损坏的会话文件」（JSON 截断/乱码）时同理：
            # list_sessions_tree 会跳过坏文件，所以它不在列表里、名字也解析不到，
            # 于是 /rm 回「会话不存在」——文件永久残留，用户无从清理。
            # 这里直接按文件名删（只删会话库底下的直属 .json，不越界）。
            if _rm_broken_session_file(mgr.narnat_dir, name):
                # broken：删掉的是列表里看不到的坏文件（JSON 截断，或能读成
                # JSON 但顶层不是对象）。界面要据此说「清理了损坏文件」而不是
                # 「删除了会话」——用户会疑惑自己删的是哪条看不见的会话。
                return {"deleted": True, "name": name, "parent": None,
                        "wasCurrent": False, "broken": True,
                        "current": self._current_path()}
            # 还有一类：文件能解析，但文件名和里面的 name 字段不一致。
            # 它在列表里显示的是 name 字段，所以用户看得见、能进去，
            # 却怎么都删不掉（实测）。按 name 字段反查真实文件再删。
            if _rm_mismatched_session_file(mgr.narnat_dir, name):
                return {"deleted": True, "name": name, "parent": None,
                        "wasCurrent": False, "current": self._current_path()}
            raise RpcError(-32000, err)

        full = f"{parent}/{target}" if parent else target
        # 删的是否会掏空当前所在位置：两种情况都算。
        #   1) 删的就是当前会话本身；
        #   2) 删的是当前会话的父（面板允许删任意会话）——当前停在 父/子，
        #      父被删后子会话文件跟着没了，状态却还指着 父/子。
        # 第 2 种漏掉的后果（实测）：状态悬空，用户再聊一轮，轮末自动保存
        # 把 父/子.json 写回磁盘 → 幽灵父会话复活，同一份讨论落成两份。
        cur_parent = mgr.state.session_parent()
        was_current = (self._current_path() == full) or (
            parent is None and cur_parent == target)
        if was_current:
            # 跑轮中删会话：必须先打断并等这一轮收口。
            # 不等会怎样（实测）：on_new() 之后这一轮的正文还在往同一个
            # MessageList 里 append，落进空对话成为「没有提问的孤立回答」，
            # 下一次保存时一并写盘，上下文被污染。
            # 与 session/new、session/switch 走同一段收口逻辑。
            self._settle_before_context_change("删除会话")
            mgr.on_new()

        err = delete_session(mgr.narnat_dir, target, parent=parent)
        # CLI 可能遗留同一目标待删标记，一并清掉，避免清理时重复删
        mgr.pending_deletes.discard((target, parent))
        if err:
            # 名字能解析出来（说明列表里就有它），但按名字找不到文件：
            # 典型是文件名与 json 里的 name 字段不一致（手工改过名、从别处拷来）。
            # 不兜底的话这条会话在界面上永远删不掉（实测）。
            if parent is None and _rm_mismatched_session_file(mgr.narnat_dir, target):
                return {"deleted": True, "name": target, "parent": None,
                        "wasCurrent": was_current,
                        "current": self._current_path()}
            raise RpcError(-32000, err)
        return {"deleted": True, "name": target, "parent": parent,
                "wasCurrent": was_current,
                "current": self._current_path()}

    def _m_session_rename(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """重命名会话。name=原名（可带 父/子），to=新名。"""
        from ..config.session_store import rename_session
        from ..core.session_callbacks import _check_reserved_session_name

        name = self._require_str(params, "name", "session/rename")
        to = self._require_str(params, "to", "session/rename")
        to = to.strip()
        if not to:
            raise RpcError(-32602, "session/rename: 新名称不能为空")
        err = _check_reserved_session_name(to)
        if err:
            raise RpcError(-32000, err)

        mgr = self._mgr
        target, parent, err = mgr.resolve_session_name(name)
        if err:
            # 原名可能是刚改过/已删除的：用不到就直说
            raise RpcError(-32000, err)

        cur_parent = mgr.state.session_parent()
        # 把父会话改成它自己子会话的名字 → 产生 `k/k` 这种父子同名路径，
        # /ls 显示混乱、save 直接不可用（实测）。
        # /explore 早有「分支名不可与父会话同名」的校验，rename 这条漏着。
        if parent is None and to == target:
            raise RpcError(-32000, f"新名称与原名相同: {to}")
        if parent is None:
            from ..config.session_store import list_sessions_tree

            for root in list_sessions_tree(mgr.narnat_dir):
                if root.get("name") != target:
                    continue
                if any(c.get("name") == to for c in root.get("children", [])):
                    raise RpcError(-32000,
                                   f"新名称不可与它的子会话同名（'{to}'），请换一个名称")
                break
        was_current = self._current_path() == (f"{parent}/{target}" if parent else target)
        # 改的是「当前所在子会话的父」：状态也得跟着搬到新父名下。
        # 不搬会怎样（实测）：改名成功但状态还指着 旧父/子，用户再聊一轮，
        # 轮末自动保存按旧路径写盘 → 同一份讨论在磁盘上出现两份，
        # 并且凭空长出一个幽灵父会话。
        renaming_my_parent = (parent is None and cur_parent == target)
        err = rename_session(mgr.narnat_dir, target, to, parent=parent)
        if err:
            raise RpcError(-32000, _rename_error_text(err, mgr.narnat_dir, to,
                                                      parent=parent))

        # 改的正是当前所在会话：把状态指到新名字，
        # 否则下一轮自动保存会按旧名再写一个文件出来。
        if was_current:
            if parent:
                mgr.switch_state(mgr.create_child_state(to, parent))
            else:
                mgr.switch_state(mgr.create_root_state(to))
        elif renaming_my_parent:
            mgr.switch_state(mgr.create_child_state(
                mgr.state.session_name(), to))
        return {"renamed": True, "from": target, "to": to, "parent": parent,
                "current": self._current_path()}

    def _cancel_and_wait_turn(self, timeout: float = _TURN_SETTLE_TIMEOUT):
        """有轮次在跑就先打断并等它收尾。返回 (是否确实打断过, 是否已收口)。

        凡是会**换掉 MessageList** 的操作都必须先调这个：
        正在跑的 agent_loop 持有同一个列表对象，换掉之后它的后续
        append_assistant/append_tool_result 会写进**新**会话，原会话
        永远只剩一句提问（实测不可恢复）。

        第二返回值（是否收口）必须被检查：只打断不等待就换列表，
        这一轮在换完之后才 append 的 tool 结果会落进新会话成为孤立的
        `role:"tool"`（P0，复现 9/13），真实端点对之后每次请求都回 400。
        """
        with self._turn_lock:
            active = self._turn_active
        if not active:
            return False, True
        self._ui.cancel_active()
        # 隐式取消也要停后台任务：用户点了切会话/删除，那一轮派出去的子进程
        # 继续跑就等于「界面上停了、实际还在烧钱改文件」（实测）。
        # 显式点「停止」早就这么做了，隐式路径漏着。
        try:
            from ..tools.background import cancel_all

            cancel_all()
        except Exception:
            pass
        # 等这一轮真正收尾（turn/end 回调把 _turn_active 置 False）。
        # 上限 timeout：cancel 通常百毫秒内生效，这段等待会挡住后续请求。
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._turn_lock:
                if not self._turn_active:
                    return True, True
            time.sleep(0.02)
        with self._turn_lock:
            return True, not self._turn_active

    def _settle_before_context_change(self, action: str) -> bool:
        """换掉上下文（新建/切换/删除会话）之前，先把当前轮次真正收口。

        顺序必须是「打断 → 等收口 → 才动 MessageList」。
        只打断不等待会怎样（P0，实测 9/13）：工具还在执行时点「新建对话」，
        on_new() 先把列表清空，这一轮的工具结果随后才 append，落进新对话
        成为孤立的 `role:"tool"`；之后每次请求都被真实端点回 400，
        用户只看到「一发消息就报错」。
        超时就按现状继续（不能把界面卡死），但必须告知——
        否则用户遇到的就是「点了新建、新会话却坏了」且没有任何线索。

        返回 True=已收口。
        """
        _interrupted, closed = self._cancel_and_wait_turn()
        if not closed:
            from .ui import emit

            emit("notice", {"text": (
                f"上一轮未收口（等了 {_TURN_SETTLE_TIMEOUT:g} 秒），已强制{action}")})
            return False
        # 收口后仍可能残留「assistant 带 tool_calls 但缺 tool 回复」的孤立对
        # （中断打断了工具结果回传）。走既有 repair 补齐，别另写一套：
        # 不补的话这段内容落盘时就带着残缺序列，下次进入该会话首轮即 400。
        # 无轮次在跑时同样调一次——repair 无事可做就是空操作，
        # 但能兜住「上一轮早已收口、残缺序列留在列表里」的入口。
        try:
            self._parts.msg_manager.repair()
        except Exception:
            pass
        return True

    def _m_session_explore(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # name 可以为空：不指定时由 core 用父会话内容自动命名（重名自动换序号）
        name = self._require_str(params, "name", "session/explore", allow_empty=True)
        # explore 会 replace_messages：不先打断就串台（同 session/switch）
        self._cancel_and_wait_turn()
        # 进入前先判「这个分支是不是已经存在」：已存在 = 继续之前的分支。
        # core 的提示只打到 stdout，RPC 形态下 stdout 已改道到 stderr、前端
        # 看不到那一行，所以结果里要明确带上 reentered/notice。
        from ..config.session_store import session_exists
        parent = self._mgr.state.session_name()
        reentered = bool(name and parent
                         and session_exists(self._mgr.narnat_dir, name, parent=parent))
        err = self._mgr.on_explore(name)
        if err:
            raise RpcError(-32000, err)
        payload = {"current": self._current_path()}
        if reentered:
            payload["reentered"] = True
            payload["notice"] = f"已重新进入分支 {name}"
        return payload

    def _m_session_done(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # done 会把结论合并回父会话并 replace_messages：不先打断就串台，
        # 实测正在跑的那一轮正文会写进父会话成为「没有提问的回答」，
        # 而子会话被误标 completed（数据错乱且不可恢复）。
        self._cancel_and_wait_turn()
        err = self._mgr.on_done()
        # 「没有新的讨论内容需要总结」是业务提示（分支确实没有增量），不是错误。
        # 若当成 -32000 抛给客户端，UI 会显示红色错误框，误导用户以为合并失败。
        if err and "没有新的讨论内容" in err:
            return {"merged": False, "reason": "no-new-content",
                    "message": err, "current": self._current_path(), "bytes": 0}
        if err:
            raise RpcError(-32000, err)
        merged = 0
        messages = self._mgr.get_full_messages()
        for msg in reversed(messages):
            if msg.get("role") == "system" and "# 子会话 [" in str(msg.get("content", "")):
                merged = len(str(msg.get("content", "")).encode("utf-8"))
                break
        # 必须把合并后的父会话历史一起回给界面。on_done() 里做了
        # replace_messages(parent_msgs) —— 后端上下文已经换成父会话了，
        # 界面却还留着刚刚那个分支的全部内容。不给 messages 会怎样（实测）：
        # 用户在「父会话的标题 + 分支的内容」下继续提问；/done 合并出来的
        # 结论在界面上根本看不到（要重载面板才会出现）。
        return {"merged": True, "current": self._current_path(), "bytes": merged,
                "isChild": bool(self._mgr.state.session_parent()),
                "messages": _history_payload(messages)}

    # ── 方法：config ──

    def _m_config_describe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """一次性返回设置面板需要的全部配置面。

        ## 三个分组（对应界面上三个分段）

        1. `api`      —— **模型接入配置**：接口地址 / 密钥 / 协议 / 模型名。
                        这是用户最需要改的（换厂商就是改这三项），
                        以前只能去手编 narnat.json。
        2. `websearch`—— **联网搜索接入配置**：provider / 服务器地址 / 密钥。
        3. `prompt`   —— narnat.md 用户指令（内容另走 narnatmd/get）。

        ## 为什么密钥要打码

        这份结果会进 webview。webview 内容可能被截图 / 录屏 / 贴进 issue，
        整串 key 出现在界面上就等于泄露。所以只回 `apiKeyMasked`
        （首尾各 4 位）与 `apiKeySet`（有没有填）。用户改的时候输入新值即可，
        不需要看见旧值。

        ## 为什么界面不该自己存一份配置

        主界面状态栏也有模型按钮。如果设置面板自己存一份、状态栏存另一份，
        两边必然漂移（这类双份状态在前面几轮已经踩过）。所以界面只做
        「展示后端给的值 + 提交」，改完重拉。
        """
        ai = self._config.ai
        api_snap = self._api_snapshot()
        ws_snap = self._websearch_snapshot()
        groups = [
            {"id": "api", "label": "模型 API"},
            {"id": "websearch", "label": "联网工具"},
            {"id": "prompt", "label": "指令"},
        ]
        items: List[Dict[str, Any]] = []

        # ────────── 1. 模型 API ──────────
        items.append({
            "id": "api.baseUrl", "group": "api", "label": "接口地址", "kind": "text",
            "value": api_snap["baseUrl"], "writable": True, "placeholder": "https://api.deepseek.com/anthropic",
        })
        items.append({
            "id": "api.apiKey", "group": "api", "label": "接口密钥", "kind": "secret",
            "value": "", "masked": api_snap["apiKeyMasked"], "isSet": api_snap["apiKeySet"],
            "writable": True,
            "placeholder": "（已设置，留空表示不改）" if api_snap["apiKeySet"] else "sk-...",
        })
        items.append({
            "id": "api.protocol", "group": "api", "label": "协议", "kind": "select",
            "value": api_snap["protocol"], "writable": True,
            "options": [
                {"value": "anthropic", "label": "anthropic（Claude / DeepSeek 官方）"},
                {"value": "openai", "label": "openai（OpenAI 兼容 / vLLM / Ollama）"},
            ],
        })
        items.append({
            "id": "api.model", "group": "api", "label": "模型名", "kind": "text",
            "value": ai.model, "writable": True, "placeholder": "deepseek-flash",
            "options": [{"value": m, "label": m} for m in (getattr(ai, "model_options", None) or [])],
        })
        items.append({
            "id": "api.thinking", "group": "api", "label": "思考强度", "kind": "select",
            "value": ai.thinking_effort, "writable": True,
            "options": [{"value": k, "label": v}
                        for k, v in (getattr(ai, "thinking_options", None) or {}).items()],
        })

        # ────────── 2. 联网工具 ──────────
        items.append({
            "id": "websearch.provider", "group": "websearch", "label": "搜索服务", "kind": "select",
            "value": ws_snap["provider"], "writable": True,
            "options": [
                {"value": "mcp", "label": "MCP 服务器（AnySearch 等）"},
                {"value": "deepseek", "label": "DeepSeek 官方搜索"},
            ],
        })
        items.append({
            "id": "websearch.url", "group": "websearch", "label": "服务器地址", "kind": "text",
            "value": ws_snap["url"], "writable": True, "showFor": ["mcp"],
            "placeholder": "https://api.anysearch.com/mcp",
        })
        items.append({
            "id": "websearch.key", "group": "websearch", "label": "搜索 API Key", "kind": "secret",
            "value": "", "masked": ws_snap["keyMasked"], "isSet": ws_snap["keySet"],
            "writable": True,
            "placeholder": "（已设置，留空表示不改）" if ws_snap["keySet"] else "as_sk_...",
        })
        # 联网搜索总开关（复用已有的 plugin 通道，不新造一套）
        for p in self._plugins():
            if str(p.get("name")) == "WebSearch":
                items.append({
                    "id": "plugin:WebSearch", "group": "websearch", "label": "启用联网搜索",
                    "kind": "bool", "value": bool(p.get("enabled")), "pluginName": "WebSearch",
                    "writable": True,
                })

        # ────────── 3. narnat.md ──────────
        items.append({
            "id": "narnatmd", "group": "prompt", "label": "narnat.md", "kind": "markdown",
            "value": "", "path": self._narnat_md_path(), "writable": True,
        })

        return {"items": items, "groups": groups,
                # 顶层也带一份：界面据此在整个面板顶部提示「改动尚未生效」，
                # 而不是逐字段去猜。含义见 _api_snapshot 的 restartPending。
                "restartPending": bool(api_snap.get("restartPending"))}

    def _narnat_md_path(self) -> str:
        """定位 narnat.md。

        不走「猜路径」：直接复用加载器正在用的那个 narnat_dir，
        否则设置面板写到的文件和后端实际读的文件可能是两个地方
       （扩展侧也有自己的一套 rpcSettings 路径推导，两边口径必须一致）。
        """
        narnat_dir = getattr(self._mgr, "narnat_dir", None)
        if not narnat_dir:
            from ..config.loader import _find_project_root
            from ..config.defaults import NARNAT_DIR, CONFIG_SUBDIR
            narnat_dir = os.path.join(_find_project_root(), NARNAT_DIR)
        from ..config.defaults import CONFIG_SUBDIR, NARNAT_MD
        return os.path.join(narnat_dir, CONFIG_SUBDIR, NARNAT_MD)

    def _m_narnatmd_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """读 narnat.md 原文（供设置面板回显）。"""
        path = self._narnat_md_path()
        text = ""
        exists = os.path.isfile(path)
        if exists:
            # 与 loader._load_user_md 同一套编码宽容策略：
            # 记事本存的 BOM 要吸收，ANSI/GBK 要能读，失败不崩。
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    text = f.read()
            except UnicodeDecodeError:
                try:
                    with open(path, "r", encoding="gbk") as f:
                        text = f.read()
                except OSError:
                    text = ""
            except OSError as e:
                raise RpcError(-32000, "读取 narnat.md 失败: %s" % e)
        return {"path": path, "text": text, "exists": exists}

    def _m_narnatmd_set(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """写 narnat.md 原文。

        写盘后**不热重载**系统提示词：那需要重建消息管理器的 system 段，
        而当前这一轮的上下文已经在跑了。如实告诉界面「下次启动生效」，
        比假装立即生效更可靠（否则用户改完发现 AI 行为没变，会以为保存失败）。
        """
        text = params.get("text")
        if not isinstance(text, str):
            raise RpcError(-32602, "narnatmd/set: params.text 必须是字符串")
        path = self._narnat_md_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as e:
            raise RpcError(-32000, "写入 narnat.md 失败: %s" % e)
        return {"ok": True, "path": path, "bytes": len(text.encode("utf-8")),
                "needRestart": True}

    def _m_config_set(self, params: Dict[str, Any]) -> Dict[str, Any]:
        key = self._require_str(params, "key", "config/set")
        value = params.get("value", _MISSING)
        if value is _MISSING:
            raise RpcError(-32602, "config/set: params.value 缺失")
        ai = self._config.ai
        if key == "model":
            if not isinstance(value, str):
                raise RpcError(-32602, "config/set: model 的 value 必须是字符串")
            text = self._mgr.on_mode(value)
            if text.startswith("无效值"):
                raise RpcError(-32000, text)
            # 「仅本次会话有效」= 内存改成功但配置写回磁盘失败。
            # 当成成功返回会怎样（实测）：面板下拉框显示已切换、界面一声不响，
            # 磁盘上的 narnat.json 还是旧值 —— 用户重启后发现设置自己变回去了，
            # 完全不知道发生过写入失败。这里如实报错。
            if "仅本次会话有效" in text:
                raise RpcError(-32000, text)
            return {"ok": True, "current": ai.model}
        if key == "thinking":
            if not isinstance(value, str):
                raise RpcError(-32602, "config/set: thinking 的 value 必须是字符串")
            text = self._mgr.on_thinking(value)
            if text.startswith("无效值"):
                raise RpcError(-32000, text)
            if "仅本次会话有效" in text:
                raise RpcError(-32000, text)
            return {"ok": True, "current": ai.thinking_effort}
        if key == "thinkback":
            act = "on" if _as_bool(value) else "off"
            text = self._mgr.on_thinkback(act)
            if text.startswith("无效值"):
                raise RpcError(-32000, text)
            if "仅本次会话有效" in text:
                raise RpcError(-32000, text)
            return {"ok": True, "current": bool(ai.thinking_passback)}
        if key == "goal":
            if not isinstance(value, dict):
                raise RpcError(-32602, "config/set: goal 的 value 必须是对象")
            enabled = bool(value.get("enabled", False))
            max_rounds = value.get("maxRounds", 0) or 0
            try:
                max_rounds = int(max_rounds)
            except (TypeError, ValueError):
                raise RpcError(-32602, "config/set: goal.maxRounds 必须是整数")
            if max_rounds < 0:
                raise RpcError(-32602, "config/set: goal.maxRounds 不能为负")
            self._mgr._goal_enabled = enabled
            self._mgr._goal_max_rounds = max_rounds if enabled else 0
            setter = getattr(self._mgr, "_set_goal_tool", None)
            if setter:
                setter(enabled)
            return {"ok": True, "current": {
                "enabled": enabled,
                "maxRounds": int(max_rounds or ai.goal_max_rounds),
            }}
        if key == "plugin":
            if not isinstance(value, dict):
                raise RpcError(-32602, "config/set: plugin 的 value 必须是对象")
            name = value.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RpcError(-32602, "config/set: plugin.name 缺失")
            enabled = _as_bool(value.get("enabled", False))
            status, text = self._mgr.on_plugin(f"{name} {'on' if enabled else 'off'}")
            if status == "error":
                raise RpcError(-32000, text.strip())
            # 同 model/thinking：写回失败时文本里带「仅本次会话有效」，
            # 不能当成成功（否则下拉框显示已切换、磁盘没变）
            if "仅本次会话有效" in text:
                raise RpcError(-32000, text.strip())
            return {"ok": True, "current": {"name": name, "enabled": enabled}}
        if key == "api":
            return self._set_api_config(value)
        if key == "websearch":
            return self._set_websearch_config(value)
        raise RpcError(-32602, f"config/set: 未知 key {key}")

    def _set_api_config(self, value: Any) -> Dict[str, Any]:
        """改模型的接入配置：接口地址 / 接口密钥 / 协议 / 模型名。

        ## 为什么必须重启后端才生效

        `AIConfig` 是**加载时**构造的不可变对象，LLMClient 持有它的一份引用。
        改磁盘上的 narnat.json 只影响**下一次启动**，当前进程里的 client
        还在用旧 key/旧 base_url。假装「立即生效」会让用户改完 key 直接提问，
        然后拿到 401 —— 却不知道是因为改动还没生效，会去怀疑新 key 是错的。

        所以这里如实返回 `needRestart: true`，界面显示「重启后端后生效」。
        （模型名和思考强度是例外：它们有 on_mode / on_thinking 热切换路径，
        那两条走的是 `config/set` 的 model/thinking 分支，不在这里。）
        """
        if not isinstance(value, dict):
            raise RpcError(-32602, "config/set: api 的 value 必须是对象")
        patch: Dict[str, Any] = {}
        if "baseUrl" in value:
            v = value.get("baseUrl")
            if not isinstance(v, str):
                raise RpcError(-32602, "config/set: api.baseUrl 必须是字符串")
            patch["接口地址"] = v.strip()
        if "apiKey" in value:
            v = value.get("apiKey")
            if not isinstance(v, str):
                raise RpcError(-32602, "config/set: api.apiKey 必须是字符串")
            patch["接口密钥"] = v.strip()
        if "protocol" in value:
            v = str(value.get("protocol") or "").strip().lower()
            if v not in ("openai", "anthropic"):
                raise RpcError(-32000, "config/set: 协议只支持 openai / anthropic")
            patch["协议"] = v
        if "model" in value:
            v = value.get("model")
            if not isinstance(v, str) or not v.strip():
                raise RpcError(-32602, "config/set: api.model 必须是非空字符串")
            patch["模型"] = {"当前": v.strip()}
        if not patch:
            raise RpcError(-32602, "config/set: api 没有任何可写字段")

        def _mutate(data):
            ai = data.setdefault("智能体", {})
            if "模型" in patch:
                # 模型是嵌套对象（当前/列表），只改"当前"，保住"列表"
                ai.setdefault("模型", {})["当前"] = patch["模型"]["当前"]
            for k, v in patch.items():
                if k != "模型":
                    ai[k] = v

        err = _persist_config(getattr(self._mgr, "_config_dir", ""), _mutate)
        if err:
            raise RpcError(-32000, "保存失败：" + err)
        # 热切模型（有 on_mode 路径，能立即生效）
        if "模型" in patch:
            try:
                self._mgr.on_mode(patch["模型"]["当前"])
            except Exception:
                pass
        return {"ok": True, "needRestart": True,
                "current": self._api_snapshot()}

    def _set_websearch_config(self, value: Any) -> Dict[str, Any]:
        """改联网搜索的接入配置。

        后端支持两种 provider（见 tools/web_search/_provider_name）：
          · "mcp"      —— 任意 MCP 搜索服务器（默认 AnySearch），要 url/auth/tool 等
          · "deepseek" —— DeepSeek 官方搜索 REST，只要 key
        两种需要的字段不同，所以这里按 provider 分别校验 ——
        统一按 mcp 校验会让选 deepseek 的人被要求填 url（那些字段对 deepseek 无意义）。
        """
        if not isinstance(value, dict):
            raise RpcError(-32602, "config/set: websearch 的 value 必须是对象")
        provider = str(value.get("provider") or "mcp").strip().lower()
        if provider not in ("mcp", "deepseek"):
            raise RpcError(-32000, "config/set: provider 只支持 mcp / deepseek")
        patch: Dict[str, Any] = {"provider": provider}
        for src, dst in (("url", "url"), ("key", "key"), ("auth", "auth"), ("tool", "tool")):
            if src in value:
                v = value.get(src)
                if not isinstance(v, str):
                    raise RpcError(-32602, f"config/set: websearch.{src} 必须是字符串")
                patch[dst] = v.strip()

        if provider == "deepseek":
            if "key" in patch and not patch["key"]:
                raise RpcError(-32000, "config/set: 选 deepseek 时必须填 API Key")
        else:
            # mcp 方式：url 是这个模式的核心（请求发到哪）
            if "url" in patch and not patch["url"]:
                raise RpcError(-32000, "config/set: 选 mcp 时必须填服务器地址")

        def _mutate(data):
            ws = data.setdefault("联网搜索", {})
            ws.update(patch)

        err = _persist_config(getattr(self._mgr, "_config_dir", ""), _mutate)
        if err:
            raise RpcError(-32000, "保存失败：" + err)
        return {"ok": True, "needRestart": True, "current": self._websearch_snapshot()}

    def _mask_key(self, key: Any) -> str:
        """密钥打码：只留首尾各 4 位。

        为什么不能原样返回：config/describe 的结果会进 webview，
        而 webview 的内容可能被用户截图、录屏、贴进 issue。
        整串 key 一旦出现在界面上就等于泄露。用户要看真值可以点「显示」，
        那需要额外一次请求（后续可做），默认一律打码。
        """
        if not isinstance(key, str) or not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 8) + key[-4:]

    def _api_snapshot(self) -> Dict[str, Any]:
        """当前 API 配置（密钥打码）。

        ## 为什么读磁盘而不是读内存里的 self._config.ai

        接口地址 / 密钥 / 协议改完是**写磁盘**，而 `self._config.ai` 是进程
        启动时加载的不可变快照 —— 它到进程结束都不会变。
        如果这里返回内存值，用户刚改完再看设置面板会显示**旧值**，
        看起来像"没保存成功"（实测：磁盘已是 test.example.com，
        面板仍显示 deepseek.com），用户会反复重试。

        所以除「模型名 / 思考强度 / 上下文」这类有热切换路径的项之外，
        一律以磁盘为准 —— 磁盘才是用户下次启动真正会用到的东西。
        当前进程实际在用什么，靠 `needRestart` 提示说明。
        """
        ai = self._config.ai
        disk = self._read_json_config().get("智能体") or {}
        if not isinstance(disk, dict):
            disk = {}
        base_url = disk.get("接口地址")
        protocol = disk.get("协议")
        key = disk.get("接口密钥")
        return {
            "baseUrl": base_url if isinstance(base_url, str) else ai.base_url,
            "apiKeyMasked": self._mask_key(key if isinstance(key, str) else ai.api_key),
            "apiKeySet": bool((key if isinstance(key, str) else ai.api_key)),
            "protocol": protocol if isinstance(protocol, str) else ai.protocol,
            "model": ai.model,
            # 内存值与磁盘值不一致 = 改动还没生效，界面据此提示重启
            "restartPending": (
                (isinstance(base_url, str) and base_url != ai.base_url)
                or (isinstance(protocol, str) and protocol != ai.protocol)
                or (isinstance(key, str) and key != ai.api_key)
            ),
        }

    def _read_json_config(self) -> Dict[str, Any]:
        """读 narnat.json（读盘失败返回空 dict，不抛）。

        编码用 utf-8-sig：用户很可能用记事本改过这个文件，
        BOM 不吸收会让 json.loads 在第一字节就失败。
        """
        cfg_dir = getattr(self._mgr, "_config_dir", "")
        if not cfg_dir:
            return {}
        p = os.path.join(cfg_dir, "narnat.json")
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _websearch_snapshot(self) -> Dict[str, Any]:
        """当前联网搜索配置（密钥打码）。

        读的是 narnat.json 的「联网搜索」段 —— 那几个字段没有进 AIConfig，
        而在 tool_context.api_keys 里按需读。所以这里直接读文件，
        但**复用** _read_json_config / _persist_config 的同一个 config_dir，
        避免读的和写的不是一份。
        """
        ws = self._read_json_config().get("联网搜索")
        if not isinstance(ws, dict):
            ws = {}
        return {
            "provider": str(ws.get("provider") or "mcp"),
            "url": str(ws.get("url") or ""),
            "keyMasked": self._mask_key(ws.get("key")),
            "keySet": bool(ws.get("key")),
            "auth": str(ws.get("auth") or ""),
            "tool": str(ws.get("tool") or ""),
        }

    # ── 方法：shutdown ──

    def _m_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._shutdown.set()
        return {"bye": True}


def _is_internal_notice(text: str) -> bool:
    """这条 user 消息是不是**后端自己注入的纠偏指令**（不是用户说的话）。

    为什么要判：agent_loop / compression_coordinator 会在对话里插一批
    `[系统提醒] …` 的 user 消息（TodoWrite 提醒、计划未勾、后台任务未收、
    上下文已压缩…）。它们对模型有用，但**不该出现在界面历史里** ——
    用户切回会话会看到一句自己从没打过的「我说的话」，以为账号串了或被改了记录。

    ## 判据为什么必须锚定整条，不能只看「以 [ 开头」

    实测用户的真实消息里就有大量以方括号开头的内容：
        `[Mapping Extensions and Extra Properties (MEEP)] Animal LemurKat…`
        `[ValleyTalk] VT-HTTP-START…`
        `[Wind Effects] Wind Effects are disabled for today.`
    这些是用户**粘贴的日志/模组输出**，必须原样显示。只看开头会把它们全吃掉。

    所以：
      · `[系统提醒]` 前缀 —— 程序独占（STYLE_NOTICE_TEMPLATE 也用它），
        只管前缀就够，因为用户配的提醒语是任意内容，必须能覆盖到。
      · 溢出压缩那条 `[]对话历史过长…` —— 用完整开头片段锚定。
    """
    if not isinstance(text, str):
        return False
    s = text.lstrip()
    if not s:
        return False
    for marker in INTERNAL_NOTICE_MARKERS:
        if s.startswith(marker):
            return True
    return False


def _history_payload(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把会话消息裁成界面可渲染的历史记录。

    保留 user / assistant 两类正文，以及**工具调用**（role="tool" 的条目）。

    ## 为什么必须带工具调用（第 27 轮修复）

    原来这里把 `tool_calls` 与 role="tool" 全丢了，理由是「体积大、重放无法
    还原当时的分组」。后果（实测）：切换会话回来，整段 AI 的工具调度信息
    全部消失，界面上只剩几行干巴巴的正文 —— 用户看不出 AI 当时搜了什么、
    读了哪个文件。而这些数据**磁盘上一直都在**（实测某会话 1644/1649 条
    消息带 tool_calls），纯粹是这一层筛掉了。

    现在按**真实发生顺序**输出：正文 → 该轮的每个工具 → 下一段正文。
    工具条目的结构刻意与实时事件 `tool/call` + `tool/result` 对齐
    （name/label/summary/arguments/output/failed），这样界面可以用同一套
    渲染逻辑画历史，不必再写一份。

    仍然过滤掉的：
      - system：提示词，给用户看毫无意义
      - 空 assistant 且没有 tool_calls：什么都没有，画不出东西

    例外：`/done` 合并进父会话的「# 子会话 [X] 结论」也是 system 角色，
    但它正是用户执行 /done 之后最想看到的东西。一并过滤掉的话，
    界面只显示「结论已合并」，对话区里却没有结论（实测），
    用户会以为合并失败或内容丢了。

    assistant 项额外带上 `thinking`：该条消息有思考内容时才有这个键
    （字符串，可能很长），没有则整键省略——界面靠键是否存在判断
    要不要渲染「思考过程」。`text` 始终是正文 content，思考不算正文。
    """
    # 先建「tool_call_id → 输出文本」的索引：工具结果在下一条 role="tool" 消息里，
    # 而我们要把它挂回**发起调用**的那个工具条目上，两条消息距离可能隔好几条
    #（一条 assistant 可以同时发起多个并行工具调用）。
    results: Dict[str, Any] = {}
    for m in messages or []:
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        if isinstance(cid, str) and cid:
            results[cid] = m.get("content")

    out: List[Dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.startswith("# 子会话 ["):
                out.append({"role": "system", "text": content})
            continue
        if role == "tool":
            # 已经并入对应的工具条目（见下面 _tool_history_entry），不单独成条
            continue
        if role not in ("user", "assistant"):
            continue
        # 后端注入的纠偏指令不是用户说的话，不进界面历史（见 _is_internal_notice）。
        if role == "user" and _is_internal_notice(content):
            continue
        calls = m.get("tool_calls")
        has_text = isinstance(content, str) and content.strip()
        if role == "assistant" and isinstance(calls, list) and calls:
            # 中间步（只有 tool_calls、正文为空）不能整条丢掉 —— 丢了就等于
            # 把这一轮的工具调度信息抹掉，这正是原来的 bug。
            pass
        elif not has_text:
            continue
        if has_text:
            item: Dict[str, Any] = {"role": role, "text": content}
            if role == "assistant":
                thinking = m.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    item["thinking"] = thinking
            out.append(item)
        if role == "assistant" and isinstance(calls, list):
            for tc in calls:
                entry = _tool_history_entry(tc, results)
                if entry is not None:
                    out.append(entry)
    return out


# 单条工具输出在历史里的长度上限。
# 不设上限会怎样（实测）：某会话 1644 条工具调用，其中 `Read` 一次就回几千行，
# 整份 session/switch 回执能到几十 MB —— 界面解析卡死、面板白屏。
# 实时流不受这个限制（那是用户正在看的那一条，必须完整），只有历史回放截断。
_HISTORY_TOOL_OUTPUT_MAX = 4000


def _tool_failed_from_output(output: str) -> bool:
    """从工具输出判断这次调用是否失败。

    存储里没有独立的 `failed` 字段，只能读 output 的固定标记。
    目前只认 bash 工具写的 `[exit code: N]`：
      · 只看这一种，不去猜其它文案 —— 猜错会把成功的行画成红色，
        比不标失败更糟（用户会去查一个根本不存在的错误）。
      · 编辑类工具失败时会回 `[编辑失败: …]` / `[写入失败: …]`，
        一并识别，否则失败行会显示成「完成」。
    """
    if not isinstance(output, str):
        return False
    m = re.search(r"\[exit code:\s*(-?\d+)\]", output)
    if m and int(m.group(1)) != 0:
        return True
    head = output.lstrip()[:20]
    return head.startswith("[编辑失败") or head.startswith("[写入失败")


def _rebuild_diff(name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从工具参数**重建**编辑的 diff。

    ## 为什么需要重建

    diff 从来不落盘 —— 它是 `on_tool_result(diff=...)` 的运行时参数，
    只走实时流。存储里 tool 消息的 content 只有 `[已写入: 路径 (N字节)]`。
    所以切换会话回来后，历史里的编辑行没有颜色差异，用户看不出当时改了什么
    （实测点名的问题）。

    但 `Edit` 的参数里有 old_string / new_string，`Write` 有完整 content ——
    足够把 diff 重建出来。重建走与实时**完全相同**的 `_struct_diff`，
    所以行号、+N −M、截断上限、颜色 class 全都一致，不会出现
    「实时的 diff 和历史里的 diff 长得不一样」。

    Write 没有「旧内容」可比（工具不去读原文件），只能按「整个文件都是新增」
    呈现。这不是精确 diff，但行头 +N −0 能让用户一眼看出写入了多少行，
    比完全没有强。
    """
    from ..core.tool_dispatcher import _struct_diff
    import difflib

    def _lines(v):
        if not isinstance(v, str):
            return []
        # 统一换行：old/new 可能一个用 \r\n 一个用 \n，
        # 不做统一会让每一行都被判成「一删一增」（整屏红绿），
        # 而实际上只改了一个词。
        return v.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if name == "Edit":
        old_s = args.get("old_string")
        new_s = args.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            return None
        old_l, new_l = _lines(old_s), _lines(new_s)
    elif name == "Write":
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return None
        old_l, new_l = [], _lines(content)
    else:
        return None

    path = args.get("file_path")
    fname = os.path.basename(str(path)) if isinstance(path, str) and path else "file"
    unified = list(difflib.unified_diff(
        old_l, new_l, fromfile="a/" + fname, tofile="b/" + fname, lineterm=""
    ))
    if not unified:
        return None
    return _struct_diff("\n".join(unified))


def _tool_history_entry(tc: Dict[str, Any], results: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条存储态 tool_call 转成界面可渲染的工具条目。

    结构与实时事件对齐（name/label/summary/arguments/output），
    界面因此能复用同一套渲染与折叠逻辑。
    """
    if not isinstance(tc, dict):
        return None
    fn = tc.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = fn.get("arguments")
    args: Any = {}
    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                args = parsed
        except (ValueError, TypeError):
            # 参数不是合法 JSON（旧会话 / 手工改过）：保留原文，界面按文本展开
            args = {"_raw": raw_args}
    if not isinstance(args, dict):
        args = {}

    # label / summary 复用派发器的同一套算法 —— 不另写一份，
    # 否则历史里的「执行命令 / dir」与实时显示的「执行命令 / dir」会对不上。
    from ..core.tool_dispatcher import ToolDispatcher
    try:
        label, summary = ToolDispatcher._tool_label_summary(name, args)
    except Exception:
        label, summary = name, ""

    cid = tc.get("id")
    cid = cid if isinstance(cid, str) else ""
    raw_out = results.get(cid)
    output = raw_out if isinstance(raw_out, str) else ""
    truncated = len(output) > _HISTORY_TOOL_OUTPUT_MAX
    if truncated:
        output = output[:_HISTORY_TOOL_OUTPUT_MAX] + "\n…（共 %d 字符，已截断）" % len(raw_out)

    entry: Dict[str, Any] = {
        "role": "tool",
        "id": cid,
        "name": name,
        "label": label or name,
        "summary": summary or "",
        "arguments": args,
        "output": output,
    }
    # 编辑类工具：把 diff 重建出来，界面才有红绿颜色差异。
    # 只有在**确实写成功了**才重建 —— 失败的编辑没改到磁盘，画出一堆
    # 红绿行会让用户以为改动已生效（比不显示更误导）。
    # 判据用工具给的输出文案（Write/Edit 成功时固定写「已写入 / 已编辑」）。
    if not _tool_failed_from_output(output):
        diff = _rebuild_diff(name, args)
        if diff is not None:
            entry["diff"] = diff
    if truncated:
        entry["truncated"] = True
    # 失败判定统一走 _tool_failed_from_output（退出码 / 编辑失败前缀），
    # 不在这里再写一遍正则 —— 两份判据会漂移。
    if _tool_failed_from_output(output):
        entry["failed"] = True
    return entry


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "1", "yes")
    return bool(value)


# 命令确实执行了，但没有产生任何变更（无事可做）。
# 单独标出来是因为界面拿 ok:true 会显示「✓ 已执行」——
# 对着「没有可压缩的历史对话」打勾，用户会以为真压缩了什么。
_NOOP_MARKERS = ("没有可压缩的历史对话", "没有新的讨论内容需要总结",
                 "没有对话内容，无需保存", "当前没有运行中的任务")


def _looks_noop(lines) -> bool:
    text = "\n".join(lines or [])
    return any(marker in text for marker in _NOOP_MARKERS)


_STATE_CHANGES = {
    "clear": ["context"],
    "compact": ["context"],
    "skill": ["context"],
    "save": ["session"],
    "ls": [],
    "cd": ["session"],
    "rm": ["session"],
    "explore": ["session"],
    "done": ["session"],
    "exit": ["session"],
    "thinking": ["thinking"],
    "thinkback": ["thinking"],
    "mode": ["model"],
    "goal": ["goal"],
    "plugin": ["plugin"],
}


def _state_changes(command: str) -> list:
    return list(_STATE_CHANGES.get(command.lower().lstrip("/"), []))


def _humanize_internal_error(e: BaseException) -> str:
    """把未预期异常翻译成用户能理解、且知道下一步做什么的一句话。

    原始异常类型与消息只留在 stderr（traceback 已经打过了）——里面常带
    绝对路径、WinError 编号，用户既看不懂也不该看到。
    """
    name = type(e).__name__
    if isinstance(e, (FileNotFoundError,)):
        return "后端处理失败：找不到需要的文件（可能刚被移动或删除）。请重试或重启后端"
    if isinstance(e, PermissionError):
        return "后端处理失败：文件被其他程序占用。请稍后重试（或用命令面板执行「重启后端」）"
    if isinstance(e, MemoryError):
        return "后端内存不足。请压缩上下文或开启新对话"
    if isinstance(e, RecursionError):
        return "后端处理失败：数据结构嵌套过深。请开启新对话"
    if isinstance(e, TimeoutError):
        return "后端处理超时。请重试"
    if isinstance(e, OSError):
        return "后端处理失败：读写文件出错。请重试，若持续失败请重启后端"
    if isinstance(e, (KeyError, IndexError, TypeError, ValueError, AttributeError)):
        return "后端处理失败：内部数据格式异常。请重试；若持续出现请反馈"
    return f"后端处理失败（{name}）。请重试，若持续失败请重启后端"


# rename_session 是「返回错误字符串」的存储层接口，它的文案里带 errno 与绝对路径
# （`写入新会话失败: [Errno 2] No such file or directory: 'E:\...\xxx.json'`）。
# 原样抛给界面就是一行磁盘路径：用户既看不懂，也做不了什么。
_ERRNO_RE = re.compile(r"\[?(?:Errno|WinError)\s*-?\d+\]?")
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\s\\])[^\s'\"，。）]*")
_RENAME_PARTIAL_RE = re.compile(r"^会话已改名为「(.+?)」，但(.+)$")


def _conflicting_session_name(narnat_dir: str, want: str,
                              parent: Optional[str] = None) -> str:
    """找出与 want「归一化后同名」的那条**已有**会话的真实名字；找不到返回 ""。

    归一化 = session_store._safe_filename（`/`、`\\`、`:` 等统一换成 `_`），
    所以 `c:/x` 与 `c:\\x` 会落到同一个文件上。rename 的失败文案原来直接回显
    请求里的新名，用户拿着「已有同名会话: c:\\x」去列表里找，看到的却是 `c:/x`
    ——既不知道撞的是谁，也没法照着改。这里回查会话库，报磁盘上那条的名字。
    """
    from ..config.session_store import _safe_filename, _sessions_dir, list_sessions_tree

    target = _safe_filename(want).lower()
    try:
        tree = list_sessions_tree(narnat_dir)
    except Exception:
        tree = []
    for root in tree or []:
        if parent:
            # 子会话只在自己的父会话命名空间里判重
            if root.get("name") != parent:
                continue
            for c in root.get("children") or []:
                if _safe_filename(str(c.get("name") or "")).lower() == target:
                    return str(c.get("name"))
            return ""
        if _safe_filename(str(root.get("name") or "")).lower() == target:
            return str(root.get("name"))
        # 根会话改名会连子会话目录一起搬：撞上别人子会话的名字同样算冲突
        for c in root.get("children") or []:
            if _safe_filename(str(c.get("name") or "")).lower() == target:
                return f"{root.get('name')}/{c.get('name')}"
    # 列表里查不到（目录里只剩损坏文件 / 空壳目录）：退回按实际文件名找，
    # 至少说得出撞的是哪个名字，而不是把请求里的新名复述一遍
    try:
        entries = os.listdir(_sessions_dir(narnat_dir))
    except OSError:
        return ""
    for entry in entries:
        stem = entry[:-5] if entry.endswith(".json") else entry
        if stem.lower() == target:
            return stem
    return ""


def _rename_error_text(err: str, narnat_dir: str, want: str,
                       parent: Optional[str] = None) -> str:
    """把 rename_session 的失败文案翻成能直接给用户看的一句话。

    原始 errno / 绝对路径只进 stderr（日志侧），RPC 错误串只留结论与下一步。
    措辞与 _humanize_internal_error 同一口径：说清楚「哪里失败 + 该做什么」。
    """
    if not err:
        return ""
    if err.startswith("已有同名会话"):
        other = _conflicting_session_name(narnat_dir, want, parent)
        if other and other != want:
            return f"该名称与已有会话「{other}」冲突（两者归一化后同名），请换一个名称"
        return err
    if not (_ERRNO_RE.search(err) or _ABS_PATH_RE.search(err)):
        return err
    print(f"[session/rename] 存储层原始失败信息（仅供排查）: {err}", file=sys.stderr)
    m = _RENAME_PARTIAL_RE.match(err)
    if m:
        # 主文件已经改名成功，只是收尾没做完：结论必须保留，
        # 只把尾巴里的绝对路径/errno 换掉（否则用户会以为根本没改成）
        name, tail = m.group(1), m.group(2)
        if "旧文件" in tail:
            return (f"会话已改名为「{name}」，但旧文件没能删除（可能正被其它程序占用）。"
                    "请关闭占用后手动删除旧文件，或重启后端再试")
        return (f"会话已改名为「{name}」，但它的子会话目录没能一起搬过去。"
                "请手动把旧目录改名，或删除该会话后重新创建")
    low = err.lower()
    if "no such file" in low or "errno 2" in low:
        return "写入新会话失败：找不到目标路径（会话库可能刚被移动或删除）。请重试或重启后端"
    if ("permission" in low or "winerror 5" in low or "errno 13" in low
            or "拒绝访问" in err or "占用" in err):
        return ("写入新会话失败：文件被其他程序占用。请稍后重试"
                "（或用命令面板执行「重启后端」）")
    if "too long" in low or "errno 36" in low or "过长" in err:
        return "写入新会话失败：会话名过长。请缩短后重试"
    return "写入新会话失败：读写会话文件出错。请重试，若持续失败请重启后端"


def _model_api_key(config) -> str:
    """当前模型用到的接口密钥（取不到就返回空串）。

    密钥挂在 Config.ai.api_key（AIConfig 的字段），不是 Config.api_key ——
    Config 上的 api_keys 是「联网搜索」各厂商的 key，与模型调用无关。
    读错会怎样（实测）：首屏「⚠ 还没填 API Key」对所有人恒亮，填了也还在，
    用户只能无视它（提示一旦恒假，之后就再也没人信）。
    """
    for obj, attr in ((getattr(config, "ai", None), "api_key"), (config, "api_key")):
        try:
            val = getattr(obj, attr, "") if obj is not None else ""
        except Exception:
            val = ""
        if val:
            return val
    return ""


def _take_config_warnings() -> list:
    """取走配置文件加载过程中产生的警告（取一次即清空）。"""
    try:
        from ..config.loader import take_load_warnings

        return take_load_warnings()
    except Exception:
        return []


def _is_dangling_parent(narnat_dir: str, name: str) -> bool:
    """name 是不是「父 json 已丢、但 <父>/ 下还有子会话文件」的幽灵父会话。

    比按 ghost 标记判断更宽：子文件是坏 JSON 时列表里根本推不出 ghost 行，
    那种残骸同样删不掉、名字同样被永久占住，必须一并认得出来。
    父 json 还在的会话不算（它有正常的删除路径）。
    """
    import os
    from ..config.session_store import _safe_filename, _sessions_dir, session_exists

    if not name or "/" in name or "\\" in name:
        return False
    safe = _safe_filename(name)
    if not safe or safe in (".", ".."):
        return False
    root = os.path.normpath(_sessions_dir(narnat_dir))
    sub = os.path.normpath(os.path.join(root, safe))
    # 只认会话库底下的直属目录（防 ../ 越界）
    if os.path.dirname(sub) != root or not os.path.isdir(sub):
        return False
    if session_exists(narnat_dir, name):
        return False
    for _dirpath, _dirnames, filenames in os.walk(sub):
        if any(f.lower().endswith(".json") for f in filenames):
            return True
    return False


def _rm_session_tree(narnat_dir: str, name: str) -> int:
    """删掉一个会话在磁盘上的整棵子树（幽灵父专用）。返回删掉的子会话 json 数。"""
    import os
    import shutil
    from ..config.session_store import _safe_filename, _sessions_dir

    root = os.path.normpath(_sessions_dir(narnat_dir))
    # 父 json（可能不存在）
    json_path = os.path.join(root, _safe_filename(name) + ".json")
    if os.path.isfile(json_path):
        os.remove(json_path)
    # 子会话目录
    sub = os.path.normpath(os.path.join(root, _safe_filename(name)))
    if os.path.dirname(sub) != root or not os.path.isdir(sub):
        return 0
    removed = sum(1 for _dp, _dn, fns in os.walk(sub)
                  for f in fns if f.lower().endswith(".json"))
    shutil.rmtree(sub)
    return removed


def _rm_mismatched_session_file(narnat_dir: str, name: str) -> bool:
    """删掉「文件名与内部 name 字段不一致」的会话文件。返回 True=真删了。

    什么时候会不一致：手工改过文件名、从别处拷进来、或旧版本写入中断。
    列表和 /cd 用的都是 json 里的 name，所以用户能看见它、能进去，
    但按名字删时找不到同名文件，于是「这条会话永远删不掉」（实测）。
    这里在按文件名找不到时，按 name 字段反查真实文件再删。
    """
    import json
    import os
    from ..config.session_store import _sessions_dir, _remove_file_with_retry

    root = os.path.normpath(_sessions_dir(narnat_dir))
    if not os.path.isdir(root):
        return False
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for fn in entries:
        if not fn.lower().endswith(".json"):
            continue
        path = os.path.normpath(os.path.join(root, fn))
        if os.path.dirname(path) != root or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or data.get("name") != name:
            continue
        if _remove_file_with_retry(path):
            return False
        sub = os.path.join(root, os.path.splitext(fn)[0])
        if (os.path.normpath(sub) != root and os.path.isdir(sub)
                and not os.listdir(sub)):
            try:
                os.rmdir(sub)
            except OSError:
                pass
        return True
    return False


def _rm_broken_session_file(narnat_dir: str, name: str) -> bool:
    """删掉一个「存在但不可用」的会话文件。返回 True=真删了。

    两类同等对待（对用户是同一件事：列表里看不到它、名字被永久占住、
    按名字删只会收到「会话不存在」）：
      1. 完全解析不出 JSON（被截断、编码乱掉）；
      2. 能解析成 JSON 但结构不合法——顶层是数组 / 字符串 / null。
         此前这条路只认第 1 类（`json.load` 成功就 return False），
         于是手工改成 `[]` 的文件永远删不掉、名字再也用不上（实测）。
    注意 messages 字段非数组的文件不算：它仍在会话列表里（count 记 0），
    能正常解析出名字并按名字删除，把这类误删等于删掉一条可用会话。
    只允许删会话库底下的直属 .json，不越界。
    """
    import json
    import os
    from ..config.session_store import _safe_filename, _sessions_dir

    safe = _safe_filename(name)
    if not safe or safe in (".", ".."):
        return False
    root = os.path.normpath(_sessions_dir(narnat_dir))
    target = os.path.normpath(os.path.join(root, safe + ".json"))
    if os.path.dirname(target) != root or not os.path.isfile(target):
        return False
    # 只对「真的不可用」的文件动手：结构合法的是正常会话，不该走这条路
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        pass
    except OSError:
        return False
    else:
        if isinstance(data, dict):
            return False
    try:
        os.remove(target)
        return True
    except OSError:
        return False


def _sweep_empty_session_dir(narnat_dir: str, name: str) -> bool:
    """删掉一个「只剩空目录」的会话残骸。返回 True=真删了。

    这种残骸界面上看不到（目录里一个 json 都没有，列表无从显示），
    但会永久占着名字：再想建同名会话会被判「已存在」，用户完全无从下手。

    「空」的判定要容忍两种实测形态：
      - 目录里还有一层空子目录（shutil.rmtree 能整体删掉）
      - 目录里只剩上次写盘的 .tmp 残骸（那也是垃圾）
    只要目录里**没有任何 .json**，就认为整棵子树都是残骸。
    """
    import os
    import shutil
    from ..config.session_store import _safe_filename, _sessions_dir

    # 用户可能输入 "父/子"：这种情况按父名清理（子已不存在）
    target_name = name.split("/")[0] if "/" in name else name
    safe = _safe_filename(target_name)
    if not safe or safe in (".", ".."):
        return False
    root = os.path.normpath(_sessions_dir(narnat_dir))
    target = os.path.normpath(os.path.join(root, safe))
    # 只允许动会话库底下的直属目录：防止 ../ 之类越界删到别处
    if os.path.dirname(target) != root or not os.path.isdir(target):
        return False
    try:
        for _dirpath, _dirnames, filenames in os.walk(target):
            if any(f.endswith(".json") for f in filenames):
                return False      # 还有真会话，不算残骸
        shutil.rmtree(target)
        return True
    except OSError:
        return False


def serve(agent) -> int:
    """main.py --rpc 入口：跑 RPC 服务器直到 shutdown/EOF"""
    return RpcServer(agent).serve()
