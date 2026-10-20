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
from ..core.session_callbacks import NoSession

PROTOCOL_VERSION = 1

_MISSING = object()

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
        """收尾：会话落盘 + 延迟删除 + MCP 回收，最多等 1.2 秒后硬退出"""
        # 先在当前线程把会话落盘：on_exit 里的 wait() 会等后台命名线程最多 5 秒，
        # 而这里只给 1.2 秒——等命名先于落盘会让这次退出直接丢掉会话内容
        #（实测：关窗口后磁盘上仍是上一轮的内容）。落盘是纯本地写、毫秒级。
        try:
            mgr = self._parts.session_mgr
            if mgr.state.session_name():
                mgr.on_auto_save()
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
                         "options": dict(ai.thinking_options or {})},
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
                self._stats.fetch_balance(api_key, 1)
                if self._stats.balance <= 0:
                    return
                # 与 RpcStream.finish 走同一条下发通道（ui.emit），界面收到即显示
                from .ui import emit as _emit
                _emit("stats", self._ui.resolve_stats())
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
            messages = _history_payload(self._mgr.get_messages())
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
                    "lines": ["已删除会话: " + res.get("name", target)],
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
        # /cd 换了会话：必须把切换后的历史一起回给界面。
        # 不回会怎样（实测）：顶栏 chip、抽屉高亮、后端 current 都切到了 B，
        # 对话区还显示 A 的消息——用户会在「B 的标题 + A 的内容」下继续提问，
        # 或以为历史丢了。session/switch 那条路径一直是回 messages 的，
        # /cd 这条漏着。
        if command == "cd" and handled and ok:
            state = self._mgr.state
            payload["current"] = self._current_path()
            payload["isChild"] = bool(state.session_parent())
            payload["messages"] = _history_payload(self._mgr.get_messages())
        return payload

    # ── 方法：session ──

    def _m_session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..config.session_store import list_sessions_tree, session_tree_payload

        show_all = params.get("all", False)
        if show_all is None:
            show_all = False
        if not isinstance(show_all, bool):
            raise RpcError(-32602, "session/list: params.all 必须是布尔值")
        tree = list_sessions_tree(self._mgr.narnat_dir)
        self._mgr.apply_delete_marks(tree)
        state = self._mgr.state
        payload = session_tree_payload(tree, state.session_name(),
                                       state.session_parent(), show_all=show_all)
        payload["isChild"] = bool(state.session_parent())
        return payload

    def _m_session_new(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """新建对话：回游离态 + 清空上下文（未保存的对话直接丢弃）。

        若有轮次在跑，先打断再新建——用户点「新建对话」本意就是不要当前这段，
        让他先手动点停止是多余的摩擦。
        """
        with self._turn_lock:
            active = self._turn_active
        if active:
            self._ui.cancel_active()
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
                "messages": _history_payload(self._mgr.get_messages())}

    def _m_session_switch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = self._require_str(params, "name", "session/switch")
        # 有轮次在跑时必须先打断再切。
        # 不打断会怎样：on_enter() 用 replace_all 就地换掉 MessageList，
        # 而正在跑的 agent_loop 持有同一个对象，继续 append_assistant /
        # append_tool_result —— 这一轮的正文和几十条工具记录全写进**新**会话，
        # 原会话只剩一句提问、永远得不到回答。不可恢复（实测 30 条工具结果串台）。
        with self._turn_lock:
            active = self._turn_active
        if active:
            self._ui.cancel_active()
            # 等这一轮真正收尾（turn/end 回调会把 _turn_active 置 False），
            # 否则 cancel 刚发出就换列表，仍在串台窗口里。
            # 上限 2 秒：cancel 通常在百毫秒内生效，这段等待会挡住后续请求，
            # 所以不能等太久；真等不到就照常切（此时 agent_loop 已被标记中断，
            # 下一轮 append 前会检查 cancelled，污染窗口极小）。
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with self._turn_lock:
                    if not self._turn_active:
                        break
                time.sleep(0.02)
        # 切走之前先把游离态那一段兜底存下来。必须赶在 on_enter 之前：
        # on_enter 用 replace_all 就地换掉 MessageList，之后拿到的已经是
        # 目标会话的内容，原对话再也取不回来。
        auto_saved = None
        try:
            if isinstance(self._mgr.state, NoSession):
                auto_saved = self._mgr.state.carry_over()
        except Exception:
            auto_saved = None
        err = self._mgr.on_enter(name)
        if err:
            raise RpcError(-32000, err)
        return {"current": self._current_path(),
                "messageCount": len(self._mgr.get_messages()),
                "isChild": bool(self._mgr.state.session_parent()),
                "messages": _history_payload(self._mgr.get_messages()),
                "autoSaved": auto_saved}

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
        if err:
            # 幽灵父会话（只剩子会话、父 json 已丢）：列表里可点、只能删。
            # resolve_session_name 现在会给它一条带处置建议的提示（不再是
            # 「会话不存在」），但删除这条路必须走得通——直接把整棵目录清掉。
            if _is_ghost_parent(mgr.narnat_dir, name):
                # 当前正停在这个幽灵父下（或它的子会话下）时要先复位：
                # 不复位的话 current 还指着刚被删掉的名字，界面高亮一条
                # 不存在的会话，下一次保存还会把它写回磁盘（实测）。
                cur_parent = mgr.state.session_parent()
                was_current = (self._current_path() == name) or (cur_parent == name)
                if was_current:
                    self._cancel_and_wait_turn()
                    mgr.on_new()
                mgr.pending_deletes.discard((name, None))
                try:
                    _rm_session_tree(mgr.narnat_dir, name)
                except OSError as e:
                    raise RpcError(-32000, f"删除会话失败: {e}")
                return {"deleted": True, "name": name, "parent": None,
                        "wasCurrent": was_current,
                        "current": self._current_path()}
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
                return {"deleted": True, "name": name, "parent": None,
                        "wasCurrent": False, "current": self._current_path()}
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
            # 跑轮中删会话：必须先打断并等这一轮收尾。
            # 不等会怎样（实测）：on_new() 之后这一轮的正文还在往同一个
            # MessageList 里 append，落进空对话成为「没有提问的孤立回答」，
            # 下一次保存时一并写盘，上下文被污染。
            # session/switch 早有这段等待，删除路径漏了。
            with self._turn_lock:
                active = self._turn_active
            if active:
                self._ui.cancel_active()
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    with self._turn_lock:
                        if not self._turn_active:
                            break
                    time.sleep(0.02)
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
            raise RpcError(-32000, err)

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

    def _cancel_and_wait_turn(self) -> bool:
        """有轮次在跑就先打断并等它收尾。返回是否确实打断过。

        凡是会**换掉 MessageList** 的操作都必须先调这个：
        正在跑的 agent_loop 持有同一个列表对象，换掉之后它的后续
        append_assistant/append_tool_result 会写进**新**会话，原会话
        永远只剩一句提问（实测不可恢复）。

        原先只有 session/switch 与 session/delete 调了，explore / done / cd
        三条同样换列表的路径都漏着——那三条实测都会串台。
        """
        with self._turn_lock:
            active = self._turn_active
        if not active:
            return False
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
        # 上限 2 秒：cancel 通常百毫秒内生效，这段等待会挡住后续请求。
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with self._turn_lock:
                if not self._turn_active:
                    break
            time.sleep(0.02)
        return True

    def _m_session_explore(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = self._require_str(params, "name", "session/explore")
        # explore 会 replace_messages：不先打断就串台（同 session/switch）
        self._cancel_and_wait_turn()
        err = self._mgr.on_explore(name)
        if err:
            raise RpcError(-32000, err)
        return {"current": self._current_path()}

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
        messages = self._mgr.get_messages()
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
        raise RpcError(-32602, f"config/set: 未知 key {key}")

    # ── 方法：shutdown ──

    def _m_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._shutdown.set()
        return {"bye": True}


def _history_payload(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把会话消息裁成界面可渲染的历史记录。

    只保留 user / assistant 两类且有正文的：
      - system：提示词，给用户看毫无意义
      - tool：工具返回体，体积大且界面上本就以折叠行呈现（重放无法还原当时的分组）
      - 空 assistant：只有 tool_calls 没有正文的中间步
    界面拿到这串就能把对话区画回来，不必理解内部结构。

    例外：`/done` 合并进父会话的「# 子会话 [X] 结论」也是 system 角色，
    但它正是用户执行 /done 之后最想看到的东西。一并过滤掉的话，
    界面只显示「结论已合并」，对话区里却没有结论（实测），
    用户会以为合并失败或内容丢了。
    """
    out = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.startswith("# 子会话 ["):
                out.append({"role": "system", "text": content})
            continue
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "text": content})
    return out


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


def _is_ghost_parent(narnat_dir: str, name: str) -> bool:
    """name 是不是「幽灵父会话」：列表里有它，但父 json 已丢、只剩子会话。"""
    try:
        from ..config.session_store import list_sessions_tree

        for root in list_sessions_tree(narnat_dir):
            if root.get("name") == name:
                return bool(root.get("ghost"))
    except Exception:
        return False
    return False


def _rm_session_tree(narnat_dir: str, name: str) -> None:
    """删掉一个会话在磁盘上的整棵子树（幽灵父专用）。"""
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
    if os.path.dirname(sub) == root and os.path.isdir(sub):
        shutil.rmtree(sub)


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
    """删掉一个「存在但解析不了」的会话文件。返回 True=真删了。

    这类文件（JSON 被截断、编码乱掉）会被 list_sessions_tree 跳过，
    所以它不在界面上、名字也解析不到，用户想删却只会收到「会话不存在」——
    文件永久占着磁盘，而且再想用同名保存会被判重复。这里按文件名直接删。
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
    # 只对「真的解析不了」的文件动手：能解析的是正常会话，不该走这条路
    try:
        with open(target, "r", encoding="utf-8") as f:
            json.load(f)
        return False
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        pass
    except OSError:
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
