"""SSH 单通道执行器 —— 每命令一条独立通道，完成判定 = SSH 协议 exit_status

设计要点（详见 tool_exp/TERMINAL_V2_DESIGN.md §3）:
- 复用一条 TCP 连接（transport），每命令 open_session 一条新通道
- 非 PTY: 无回显、无 ANSI 光标定位、无提示符 → 无需任何输出清洗
- 完成判定唯一依据: chan.recv_exit_status()（SSH 协议保证，永不猜）
- 超时: 关闭该命令通道 + 明确报错（不谎称"仍在后台运行"）
- stdout/stderr 分离（非 PTY 通道天然分离），两流都必须持续消费防阻塞
"""

import os
import threading
import time
from typing import Optional

import paramiko

from ..exec_signal import error_line, rc_line


class _StreamBuffer:
    """单流输出累积器: 保留首 head_limit 字节 + 末尾 tail_limit 字节，中段丢弃。

    大输出（递归列目录、日志刷屏）若无上限会撑爆内存；保留首尾已足以判断命令
    行为，缺口由渲染时的中段截断提示说明（上限语义见设计文档 §3.2）。
    """

    def __init__(self, head_limit: int, tail_limit: int):
        self._head_limit = head_limit
        self._tail_limit = tail_limit
        self._head = bytearray()
        self._tail = b""
        self.total = 0  # 累计读到的字节数（含被丢弃的中段）

    def add(self, data: bytes) -> None:
        self.total += len(data)
        if len(self._head) < self._head_limit:
            room = self._head_limit - len(self._head)
            self._head += data[:room]
            data = data[room:]
        if data and self._tail_limit > 0:
            self._tail = (self._tail + data)[-self._tail_limit:]

    @property
    def truncated(self) -> bool:
        """有中段被丢弃（保留的首尾不足以覆盖全部读到的数据）"""
        return self.total > len(self._head) + len(self._tail)

    def head(self) -> bytes:
        return bytes(self._head)

    def tail(self) -> bytes:
        return self._tail


class SSHSession:
    """一个 SSH 会话（连接复用 + 每命令独立通道）"""

    # 单次读取块大小
    RECV_CHUNK = 32768
    # 输出上限超出后的"截断保留"策略: 保留首/尾（见 _truncate 文案）
    # 超时检查间隔（读循环 poll 间隔，秒）
    POLL_INTERVAL = 0.05
    # exit_status 就绪后、EOF 未到时的静默等待轮次（× POLL_INTERVAL ≈ 500ms）
    EOF_WAIT_ROUNDS = 10

    def __init__(self, host: str, username: str, port: int = 22,
                 key_path: Optional[str] = None, password: Optional[str] = None,
                 timeout: int = 15):
        self.host = host
        self.username = username
        self.port = port
        self.is_windows = False   # connect 后一次性探测（见 _probe_platform）
        self.banner = ""          # SSH 服务端标识串（transport.get_banner），connect 后填充

        # 重连凭据
        self._reconnect_params = dict(
            host=host, username=username, port=port,
            key_path=key_path, password=password, timeout=timeout)

        self._client: Optional[paramiko.SSHClient] = None
        self._cmd_lock = threading.Lock()      # 命令串行化（同会话一次一条）
        self._reconnect_lock = threading.Lock()  # 并发重连互斥（exec 与文件工具可能同时触发）
        self._interrupt = threading.Event()    # ESC 中断标志
        self._flying_chan = None               # 飞行中通道（interrupt 时关闭）
        self._flying_lock = threading.Lock()

        self._open_client(key_path, password, timeout)

        # 平台探测（一次性，仅决定输出解码优先；不影响完成判定）
        self._probe_platform()

    # ── 连接管理 ──

    def _open_client(self, key_path: Optional[str], password: Optional[str],
                     timeout: int):
        """建立 paramiko SSHClient（密钥/密码/默认密钥三合一）"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
        }
        # password 三合一: 私钥路径 / 登录密码 / 空=自动尝试默认密钥+agent
        passwd = (password or "").strip()
        if passwd and not key_path and os.path.isfile(os.path.expanduser(passwd)):
            kwargs["key_filename"] = os.path.expanduser(passwd)
        else:
            kwargs["password"] = passwd
        if key_path:
            kwargs["key_filename"] = os.path.expanduser(key_path.strip())
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True

        try:
            client.connect(**kwargs)
        except Exception:
            # 认证/握手失败：显式关闭，回收 paramiko Transport 线程与 TCP 连接。
            # 不 close 时半成品 Transport 线程持有引用，GC 无法回收，长跑重试会累积泄漏。
            try:
                client.close()
            except Exception:
                pass
            raise
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
            try:
                self.banner = transport.get_banner() or ""
            except Exception:
                self.banner = ""
        self._client = client

    def _probe_platform(self):
        """connect 后一次性探测远端平台: 执行 `echo %OS%`，输出含 Windows_NT → cmd。

        失败/超时 → 保持 POSIX 默认。用途：输出解码时 UTF-8 失败后回退 GBK。
        探测不影响完成判定（判定永远是 exit_status）。
        """
        try:
            result = self.execute("echo %OS%", timeout=5)
            self.is_windows = "Windows_NT" in result
        except Exception:
            self.is_windows = False

    @property
    def alive(self) -> bool:
        """transport 存活（供槽位回收/status/重连判定）"""
        try:
            t = self._client.get_transport() if self._client else None
            return bool(t and t.is_active())
        except Exception:
            return False

    @property
    def busy(self) -> bool:
        """有命令在飞行（供 status 显示）"""
        return self._cmd_lock.locked()

    def reconnect(self) -> None:
        """断线重连：复用凭据重开 client。失败抛异常由调用方处理。"""
        with self._reconnect_lock:
            if self.alive:
                return  # 已被其他线程重连成功
            self._interrupt.clear()
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            p = self._reconnect_params
            self._open_client(p.get("key_path"), p.get("password"), p.get("timeout", 15))
            self._probe_platform()

    def close(self) -> None:
        """关闭会话（channel + client；不阻塞调用方）"""
        self._interrupt.set()
        self._close_flying()
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass

    def interrupt(self) -> None:
        """ESC 打断: 置中断标志 + 关闭飞行中通道（幂等）"""
        self._interrupt.set()
        self._close_flying()

    def _close_flying(self):
        with self._flying_lock:
            chan = self._flying_chan
            self._flying_chan = None
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass

    def open_sftp(self):
        """返回 paramiko SFTPClient（供 remote.py / transfer 使用）；断线时先重连。"""
        if not self.alive:
            raise RuntimeError(f"SSh会话已断开: {self.host}")
        return self._client.open_sftp()

    # ── 命令执行（核心） ──

    def _decode_strict(self, data: bytes) -> str:
        """严格解码: utf-8 优先，Windows 平台回退 gbk；不匹配时抛 UnicodeDecodeError"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            if self.is_windows:
                return data.decode("gbk")
            raise

    def _decode(self, data: bytes) -> str:
        """整段输出解码（§3.4）: 严格 utf-8 → 平台回退（Windows→GBK）→ 替换解码"""
        if not data:
            return ""
        try:
            return self._decode_strict(data)
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def _decode_edge(self, data: bytes, is_head: bool) -> str:
        """边界片段解码: 切点可能落在多字节字符中间，先剥掉边界残字节再按平台解码

        必须先用 utf-8 严格试（不能直接走平台 fallback：cmd 的 GBK 解码对
        任意字节序列几乎都"成功"，会把未对齐的片段解成整段乱码）。
        """
        probe = data
        for _ in range(3):
            try:
                return probe.decode("utf-8")
            except UnicodeDecodeError:
                probe = probe[:-1] if is_head else probe[1:]
        return self._decode(data)

    def _render_stream(self, buf: _StreamBuffer) -> str:
        """把单流累积缓冲渲染成文本（丢了中段时插截断提示）

        截断边界吸附到换行符：换行不会出现在多字节字符内部，按行切可保证两段
        片段从字符边界开始，避免半个字符解码成乱码。
        """
        if not buf.truncated:
            return self._decode(buf.head() + buf.tail())
        head_raw = buf.head()
        tail_raw = buf.tail()
        nl = head_raw.rfind(b"\n")
        if nl >= 0:
            head_raw = head_raw[:nl + 1]
        nl = tail_raw.find(b"\n")
        if nl >= 0:
            tail_raw = tail_raw[nl + 1:]
        head_text = self._decode_edge(head_raw, is_head=True)
        tail_text = self._decode_edge(tail_raw, is_head=False)
        return (
            head_text
            + f"\n...[中间截断: 输出共{buf.total}字节, 已保留首{len(head_text)}字符"
              f"+尾{len(tail_text)}字符。增大max_output_chars可获取完整输出]\n"
            + tail_text
        )

    def _render_all(self, out_buf: _StreamBuffer, err_buf: _StreamBuffer) -> str:
        """渲染两流为最终文本（stderr 非空时附 [stderr] 段）"""
        stdout_text = self._render_stream(out_buf)
        stderr_text = self._render_stream(err_buf)
        if not stderr_text:
            return stdout_text
        if stdout_text:
            return f"{stdout_text}\n[stderr]\n{stderr_text}"
        return f"[stderr]\n{stderr_text}"

    def execute(self, command: str, timeout: int = 120,
                max_output_chars: int = 8000) -> str:
        """执行一条命令，返回 `{rc行}\n{输出}` 或错误文案（见设计文档 §3.2）

        返回形态（成功）:
            [exit code: {rc}] [{TAG}]
            {stdout}
            [stderr]\\n{stderr}             ← stderr 非空时附加
        超时（返回，不抛）:
            {已收集输出}
            [错误: 命令超时（{timeout}秒），已终止该命令通道。命令可能仍在远端运行，长时间任务请用 nohup/setsid 后台化后轮询日志]
        中断（ESC，返回）:
            {已收集输出}\\n[用户中断]
        """
        # 入口复位: ESC 落在两次调用之间时，避免下条命令一启动就被判为中断
        self._interrupt.clear()

        # 断线自愈: 复用凭据重连（失败返回错误文案，由调用方给出设备级提示）
        if not self.alive:
            try:
                self.reconnect()
            except Exception as e:
                return error_line(f"连接中断，自动重连失败（设备可能未开机/网络不通）: {e}")

        if max_output_chars <= 0:
            return error_line("max_output_chars需为正整数")

        head_limit = max_output_chars * 2 // 3
        tail_limit = max_output_chars - head_limit

        with self._cmd_lock:
            cmd = command
            out_buf = _StreamBuffer(head_limit, tail_limit)
            err_buf = _StreamBuffer(head_limit, tail_limit)
            deadline = time.time() + timeout
            interrupted = False
            timed_out = False
            chan = None
            try:
                transport = self._client.get_transport() if self._client else None
                if transport is None or not transport.is_active():
                    raise IOError("SSH传输层不可用")
                chan = transport.open_session()
                with self._flying_lock:
                    self._flying_chan = chan
                chan.exec_command(cmd)

                silent_rounds = 0
                while True:
                    if self._interrupt.is_set():
                        interrupted = True
                        break
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    if chan.recv_ready():
                        out_buf.add(chan.recv(self.RECV_CHUNK))
                        silent_rounds = 0
                        continue
                    if chan.recv_stderr_ready():
                        err_buf.add(chan.recv_stderr(self.RECV_CHUNK))
                        silent_rounds = 0
                        continue
                    # 完成判定：exit_status 就绪 ≠ 数据已到齐。
                    # paramiko transport 线程异步填充读缓冲：exit-status 消息先被
                    # 处理、而剩余 stdout 数据尚未 feed 的窗口真实存在（实测 108KB
                    # 输出丢 43KB 且 rc 仍为 0，静默丢数据）。
                    # 判据：EOF 已收到（OpenSSH 在数据+exit-status 之后发送）→ 确定完成；
                    # 未收到 EOF 时按静默轮次兜底（兼容不发 EOF 的实现）。
                    if chan.exit_status_ready():
                        if getattr(chan, "eof_received", False):
                            break
                        silent_rounds += 1
                        if silent_rounds >= self.EOF_WAIT_ROUNDS:
                            break
                    time.sleep(self.POLL_INTERVAL)

                # 收尾抽干：break 前 transport 可能刚把尾块 feed 进来，再读一次
                while chan.recv_ready():
                    out_buf.add(chan.recv(self.RECV_CHUNK))
                while chan.recv_stderr_ready():
                    err_buf.add(chan.recv_stderr(self.RECV_CHUNK))

                if timed_out:
                    text = self._render_all(out_buf, err_buf)
                    msg = (f"命令超时（{timeout}秒），已终止该命令通道。"
                           f"命令可能仍在远端运行，长时间任务请用 nohup/setsid 后台化后轮询日志")
                    return f"{text}\n{error_line(msg)}" if text else error_line(msg)

                if interrupted:
                    text = self._render_all(out_buf, err_buf)
                    tag = "[用户中断: 已关闭命令通道；远端进程可能仍在运行]"
                    return f"{text}\n{tag}" if text else tag

                rc = chan.recv_exit_status()
                # rc == -1：paramiko 在"服务端未提供退出码"时返回（传输中断/通道异常关闭）。
                # 真实命令退出码范围为 0-255，-1 只可能是"无退出码"——不能当成功返回，
                # 否则 AI 与 UI 都看不到失败（命令结果未知）
                if rc == -1:
                    raise IOError("未收到命令退出码（连接可能中断），命令结果未知")
            except Exception:
                if self._interrupt.is_set():
                    # ESC 从其他线程关闭了飞行中通道: 读循环异常退出属正常中断路径
                    text = self._render_all(out_buf, err_buf)
                    tag = "[用户中断: 已关闭命令通道；远端进程可能仍在运行]"
                    return f"{text}\n{tag}" if text else tag
                raise
            finally:
                with self._flying_lock:
                    if self._flying_chan is chan:
                        self._flying_chan = None
                if chan is not None:
                    try:
                        chan.close()
                    except Exception:
                        pass

            text = self._render_all(out_buf, err_buf)
            if text:
                return f"{rc_line(rc)}\n{text}"
            return rc_line(rc)
