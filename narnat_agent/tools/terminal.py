"""Terminal工具 —— 可持续SSH终端

基于paramiko实现SSH交互式会话，AI可像人一样持续操控远程Linux设备。

核心设计:
- 每个SSH连接是一个会话(session)，通过host标识
- AI发命令 → 写入channel → 读取输出 → 返回给AI
- 会话持久化，多次调用复用同一连接
- 命令结束检测：发送唯一marker，读到marker即表示命令执行完毕
- 每次exec后主动获取pwd，构造 user@host:path$ 给AI看
- 超时后发送Ctrl+C中断远程命令，并排空channel缓冲区，防止后续命令被污染

输出解析(修复stdout丢失问题):
- PTY invoke_shell模式下，shell回显命令文本与实际stdout混合
- _strip_echo: 精确剥离命令回显(首行+续行"> "前缀)，保留纯stdout
- _clean_output: 清洗ANSI码、内部标记(__NARNAT_*)、续行提示符
- marker附加$?退出码: echo __MARKER__$?，一行同时标记结束和捕获退出码
- prompt属性: 自动将/home/user缩写为~，与真实shell一致
"""

import os
import re
import time
import threading
from typing import Callable, Optional

import paramiko
import socket

from ..config.defaults import MAX_BASH_OUTPUT


# ── 危险命令检测 ──

_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

_confirm_callback: Optional[Callable[[str], bool]] = None


def set_confirm_callback(cb: Callable[[str], bool]):
    """设置删除确认回调。cb返回True表示允许执行。由agent层注入。"""
    global _confirm_callback
    _confirm_callback = cb


# ── 会话管理 ──

_sessions: dict[str, "SSHSession"] = {}
_sessions_lock = threading.Lock()


class SSHSession:
    """一个SSH交互式会话"""

    def __init__(self, host: str, username: str, port: int = 22,
                 key_path: Optional[str] = None, password: Optional[str] = None):
        self.host = host
        self.username = username
        self.port = port
        self._cwd = "~"

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {"hostname": host, "port": port, "username": username}
        if key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(key_path)
        elif password:
            connect_kwargs["password"] = password
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        self._client.connect(**connect_kwargs)

        self._channel = self._client.invoke_shell(term="xterm", width=200, height=50)
        self._channel.settimeout(0.5)

        self._initial_output = self._read_until_prompt(timeout=5)
        self._update_cwd()

    @property
    def prompt(self) -> str:
        """构造当前prompt: user@host:path$

        path显示规则:
        - /home/username → ~
        - /home/username/xxx → ~/xxx
        - 其他路径原样显示
        """
        display_path = self._cwd
        home_prefix = f"/home/{self.username}"
        if self._cwd == home_prefix:
            display_path = "~"
        elif self._cwd.startswith(home_prefix + "/"):
            display_path = "~" + self._cwd[len(home_prefix):]
        return f"{self.username}@{self.host}:{display_path}$"

    def execute(self, command: str, timeout: int = 30) -> str:
        """在远程shell中执行命令，返回输出+prompt

        发送格式: command; echo __MARKER__$?; echo __PWD_MARKER__
        - 用 $? 捕获退出码，附加在marker后，一行搞定
        - pwd -P 独立获取路径，避免 $(pwd) 展开时序问题
        """
        # 发送新命令前，排空channel中可能残留的上次输出
        self._drain_stale_output()

        marker = f"__NARNAT_MARKER_{time.time_ns()}__"
        pwd_marker = f"__NARNAT_PWD_{time.time_ns()}__"

        # 用 $? 捕获退出码附加在marker行，pwd -P 独立获取路径
        full_cmd = f"{command}; echo {marker}$?; pwd -P; echo {pwd_marker}\n"
        self._channel.send(full_cmd)

        return self._read_until_marker(marker, pwd_marker, timeout=timeout)

    def close(self):
        """关闭会话"""
        try:
            self._channel.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass

    def _drain_channel(self, duration: float = 1.5):
        """排空channel缓冲区中的残留数据

        超时后远程命令可能还在跑，需要：
        1. 发Ctrl+C中断
        2. 等一小段时间让输出排完
        3. 丢弃所有残留数据
        """
        self._channel.send("\x03")
        time.sleep(0.1)
        self._channel.send("\n")

        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                self._channel.recv(4096)
            except socket.timeout:
                break
            except Exception:
                break

    def _drain_stale_output(self):
        """排空channel中可能残留的旧输出

        每次exec前调用，防止前一个命令的残留输出污染当前命令的返回。
        只做非阻塞读取，不发送Ctrl+C（不中断任何正在运行的命令）。
        临时使用短超时(0.1s)快速排空，避免无残留数据时浪费等待时间。
        """
        old_timeout = self._channel.gettimeout()
        try:
            self._channel.settimeout(0.1)
            while True:
                chunk = self._channel.recv(4096)
                if not chunk:
                    break
        except socket.timeout:
            pass
        except Exception:
            pass
        finally:
            self._channel.settimeout(old_timeout)

    def _update_cwd(self, timeout: int = 3):
        """通过执行pwd命令更新当前工作目录"""
        marker = f"__NARNAT_CWD_{time.time_ns()}__"
        # 用 pwd -P + marker，和 execute 同样的机制
        self._channel.send(f"pwd -P; echo {marker}\n")

        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk
                if marker in output:
                    break
            except socket.timeout:
                continue
            except Exception:
                break

        # 解析: ... /actual/path\n __MARKER__\n prompt
        # marker所在行之前的一行就是pwd输出
        if marker in output:
            before_marker = output.split(marker)[0]
            lines = before_marker.strip().split("\n")
            # 取最后一个非空行作为pwd
            for line in reversed(lines):
                cleaned = self._clean_output(line).strip()
                # 修复运算符优先级: and 优先于 or，需要括号
                if cleaned and (not cleaned.startswith("echo ")) and (("/" in cleaned) or (cleaned == "/")):
                    self._cwd = cleaned
                    break

    def _read_until_marker(self, marker: str, pwd_marker: str, timeout: int = 30) -> str:
        """读取channel输出，直到读到pwd_marker。超时则中断命令并排空缓冲区。

        找到pwd_marker后，继续读取直到连续N次recv超时(表示数据已全部到达)，
        而非固定时间窗口，确保命令的完整输出不被截断。
        """
        output = ""
        deadline = time.time() + timeout
        found = False
        # 找到marker后，连续recv超时次数达到此阈值才认为数据读完
        DRAIN_CONSECUTIVE_TIMEOUTS = 3

        while time.time() < deadline:
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk

                if not found and pwd_marker in output:
                    found = True
                    # 继续读取，直到连续多次recv超时(表示没有更多数据)
                    consecutive_timeouts = 0
                    while consecutive_timeouts < DRAIN_CONSECUTIVE_TIMEOUTS:
                        try:
                            extra = self._channel.recv(4096).decode("utf-8", errors="replace")
                            if extra:
                                output += extra
                                consecutive_timeouts = 0  # 有数据，重置计数
                            else:
                                consecutive_timeouts += 1
                        except socket.timeout:
                            consecutive_timeouts += 1
                        except Exception:
                            break
                    break

            except socket.timeout:
                if found:
                    break
                continue
            except Exception:
                break

        # 超时处理
        if not found:
            self._drain_channel(duration=1.5)
            cmd_output = self._parse_partial_output(output, marker)
            if len(cmd_output) > MAX_BASH_OUTPUT:
                cmd_output = cmd_output[:MAX_BASH_OUTPUT] + f"\n... (输出超过{MAX_BASH_OUTPUT}字符，已截断)"
            self._update_cwd()
            if cmd_output:
                return f"{cmd_output}\n[超时: 命令执行超过{timeout}秒，已发送Ctrl+C中断]\n{self.prompt}"
            else:
                return f"[超时: 命令执行超过{timeout}秒，已发送Ctrl+C中断]\n{self.prompt}"

        # 正常解析
        cmd_output, cwd = self._parse_output(output, marker, pwd_marker)
        if cwd:
            self._cwd = cwd

        if len(cmd_output) > MAX_BASH_OUTPUT:
            cmd_output = cmd_output[:MAX_BASH_OUTPUT] + f"\n... (输出超过{MAX_BASH_OUTPUT}字符，已截断)"

        if cmd_output:
            return f"{cmd_output}\n{self.prompt}"
        else:
            return self.prompt

    def _parse_output(self, raw: str, marker: str, pwd_marker: str) -> tuple[str, Optional[str]]:
        """解析正常完成的输出，返回 (命令输出, cwd)

        原始输出结构(PTY回显+实际输出混合):
          命令回显(含$?字面量, 可能多行PTY折行)
          命令实际输出行1
          命令实际输出行2
          __MARKER__0    <-- marker + 退出码(shell展开$?)
          /actual/working/dir    <-- pwd -P 的输出
          __PWD_MARKER__
          prompt

        关键: PTY回显中marker是字面量(含$?), 而输出行中marker已被shell展开(含数字)
        因此必须按行查找marker行(以marker开头), 而非简单的子串split
        """
        exit_code = None
        cwd = None

        lines = raw.split("\n")

        # 按行查找: marker行以marker开头(后跟退出码数字)
        marker_line_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith(marker):
                marker_line_idx = i
                # 提取退出码: marker行 = __NARNAT_MARKER_xxx__N
                exit_str = line.strip()[len(marker):]
                try:
                    exit_code = int(exit_str)
                except ValueError:
                    exit_code = None
                break

        # 按行查找: pwd_marker行
        pwd_marker_line_idx = None
        if marker_line_idx is not None:
            for i in range(marker_line_idx + 1, len(lines)):
                if pwd_marker in lines[i]:
                    pwd_marker_line_idx = i
                    break

        # 提取cwd: marker行和pwd_marker行之间
        if marker_line_idx is not None and pwd_marker_line_idx is not None:
            for i in range(marker_line_idx + 1, pwd_marker_line_idx):
                cleaned = self._clean_output(lines[i]).strip()
                if cleaned and (cleaned.startswith("/") or cleaned == "/"):
                    cwd = cleaned
                    break

        # 提取命令输出: marker行之前的所有内容，精确剥离命令回显
        if marker_line_idx is not None:
            before_marker = "\n".join(lines[:marker_line_idx])
        else:
            before_marker = raw

        cmd_output = self._strip_echo(before_marker)

        return self._clean_output(cmd_output), cwd

    def _parse_partial_output(self, raw: str, marker: str) -> str:
        """解析超时时的部分输出（marker可能还没出现）"""
        lines = raw.split("\n")

        # 按行查找marker行(以marker开头)
        marker_line_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith(marker):
                marker_line_idx = i
                break

        if marker_line_idx is not None:
            before_marker = "\n".join(lines[:marker_line_idx])
            return self._clean_output(self._strip_echo(before_marker))

        # marker都没出现，剥离命令回显
        return self._clean_output(self._strip_echo(raw))

    def _read_until_prompt(self, timeout: int = 5) -> str:
        """等待shell初始化完成，返回初始输出"""
        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk
                if re.search(r'[#$>]\s*$', output.strip()):
                    break
            except socket.timeout:
                continue
            except Exception:
                break
        return self._clean_output(output)

    @staticmethod
    def _strip_echo(raw: str) -> str:
        """从PTY输出中精确剥离命令回显

        PTY shell会回显用户输入的命令文本。在invoke_shell模式下，
        发送 "command; echo MARKER..." 后，shell先回显这整行，
        再输出命令的实际stdout。

        策略: 找到第一个换行符，跳过该行（命令回显的首行），
        然后继续跳过以 PS2 续行提示符("> "或">")开头的行。
        """
        lines = raw.split("\n")
        if not lines:
            return raw

        # 跳过第一行（命令回显首行）
        start = 1

        # 跳过续行回显: PTY在多行输入时回显 "> " 前缀
        while start < len(lines):
            stripped = lines[start].strip()
            if stripped.startswith("> ") or stripped == ">":
                start += 1
            else:
                break

        return "\n".join(lines[start:])

    @staticmethod
    def _clean_output(raw: str) -> str:
        """清洗ANSI转义码、回车符、内部标记和续行提示符"""
        ansi_re = re.compile(
            r'\x1b\[\??[0-9;]*[a-zA-Z]'
            r'|\x1b\].*?(?:\x07|\x1b\\)'
            r'|\x1b[()][A-Za-z0-9]'
        )
        cleaned = ansi_re.sub('', raw)
        cleaned = cleaned.replace('\r', '')

        # 清理内部标记: __NARNAT_MARKER_xxx__, __NARNAT_CWD_xxx__, __NARNAT_PWD_xxx__
        cleaned = re.sub(r'__NARNAT_(?:MARKER|CWD|PWD)_\d+__', '', cleaned)

        # 清理续行提示符: 行首的 "> " (PS2 prompt回显)
        cleaned = re.sub(r'(^|\n)> ', r'\1', cleaned)

        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()


# ── 公开接口 ──

def execute(
    action: str = "exec",
    host: str = "",
    username: str = "",
    port: int = 22,
    key_path: str = "",
    password: str = "",
    command: str = "",
    timeout: int = 30,
) -> str:
    """
    Terminal工具：可持续SSH终端。

    action:
      connect  - 建立SSH会话（首次连接或重连）
      exec     - 在已连接的会话中执行命令
      status   - 查看当前所有会话状态
      close    - 关闭指定会话
    """
    if action == "connect":
        return _connect(host, username, port, key_path, password)
    elif action == "exec":
        return _exec(host, command, timeout)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(host)
    else:
        return f"错误: 未知action '{action}'，可选: connect/exec/status/close"


def _connect(host: str, username: str, port: int = 22,
             key_path: str = "", password: str = "") -> str:
    """建立SSH会话"""
    if not host or not username:
        return "错误: connect需要提供host和username"

    session_key = f"{username}@{host}"

    with _sessions_lock:
        if session_key in _sessions:
            session = _sessions[session_key]
            if not session._channel.closed:
                return f"会话已存在: {session_key}\n{session.prompt}"
            else:
                del _sessions[session_key]

    try:
        kwargs = {"host": host, "username": username, "port": port}
        if key_path:
            kwargs["key_path"] = key_path
        if password:
            kwargs["password"] = password

        session = SSHSession(**kwargs)

        with _sessions_lock:
            _sessions[session_key] = session

        parts = [f"已连接: {session_key}"]
        if session._initial_output:
            parts.append(session._initial_output)
        parts.append(session.prompt)
        return "\n".join(parts)

    except paramiko.AuthenticationException:
        return f"错误: 认证失败({session_key})，请检查key_path或password"
    except paramiko.SSHException as e:
        return f"错误: SSH连接失败({session_key}): {e}"
    except Exception as e:
        return f"错误: 连接失败({session_key}): {e}"


def _exec(host: str, command: str, timeout: int = 30) -> str:
    """在已连接的会话中执行命令"""
    if not command:
        return "错误: exec需要提供command"

    # 安全检查：删除命令需用户确认
    if _RE_DELETE.search(command):
        if _confirm_callback and not _confirm_callback(command):
            return "操作已取消: 删除命令需用户确认"

    if not host:
        with _sessions_lock:
            if len(_sessions) == 1:
                session_key = list(_sessions.keys())[0]
            else:
                keys = list(_sessions.keys())
                return f"错误: 需要指定host，当前会话: {keys}" if keys else "错误: 无活跃会话，请先connect"
    else:
        session_key = None
        with _sessions_lock:
            for k in _sessions:
                if host in k:
                    session_key = k
                    break
        if not session_key:
            return f"错误: 未找到host={host}的会话，请先connect"

    with _sessions_lock:
        session = _sessions.get(session_key)

    if session is None:
        return f"错误: 会话不存在({session_key})，请先connect"

    if session._channel.closed:
        with _sessions_lock:
            _sessions.pop(session_key, None)
        return f"错误: 会话已断开({session_key})，请重新connect"

    try:
        return session.execute(command, timeout=timeout)
    except Exception as e:
        return f"错误: 命令执行失败({session_key}): {e}"


def _status() -> str:
    """查看所有会话状态"""
    with _sessions_lock:
        if not _sessions:
            return "(无活跃SSH会话)"

        lines = []
        for key, session in _sessions.items():
            alive = "活跃" if not session._channel.closed else "已断开"
            lines.append(f"  {key} [{alive}] {session.prompt}")
        return "SSH会话:\n" + "\n".join(lines)


def _close(host: str) -> str:
    """关闭会话"""
    with _sessions_lock:
        if not host:
            for session in _sessions.values():
                session.close()
            count = len(_sessions)
            _sessions.clear()
            return f"已关闭{count}个会话"

        session_key = None
        for k in _sessions:
            if host in k:
                session_key = k
                break

        if not session_key:
            return f"未找到host={host}的会话"

        _sessions[session_key].close()
        del _sessions[session_key]
        return f"已关闭: {session_key}"


def cleanup():
    """程序退出时清理所有会话"""
    with _sessions_lock:
        for session in _sessions.values():
            session.close()
        _sessions.clear()


def get_session(host: str = "") -> Optional[SSHSession]:
    """获取指定host的SSH会话（供SFTP等内部使用）"""
    with _sessions_lock:
        if not host and len(_sessions) == 1:
            return list(_sessions.values())[0]
        for key, session in _sessions.items():
            if host in key:
                return session
    return None
