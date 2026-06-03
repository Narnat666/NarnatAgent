"""Terminal工具 —— 可持续SSH终端

基于paramiko实现SSH交互式会话，AI可像人一样持续操控远程Linux设备。

核心设计:
- 每个SSH连接是一个会话(session)，通过host标识
- AI发命令 → 写入channel → 读取输出+prompt → 返回给AI
- 会话持久化，多次调用复用同一连接
- 命令结束检测：发送唯一marker，读到marker即表示命令执行完毕
- 每次exec返回: 命令输出 + 当前prompt，AI能看到自己在哪个目录
"""

import os
import re
import time
import threading
from typing import Optional

import paramiko
import socket

from ..config.defaults import MAX_BASH_OUTPUT


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

        # 创建交互式channel，分配PTY
        self._channel = self._client.invoke_shell(term="xterm", width=200, height=50)
        self._channel.settimeout(0.1)

        # 等待shell初始化，记录初始prompt
        self._initial_output, self._last_prompt = self._read_until_prompt(timeout=5)

    def execute(self, command: str, timeout: int = 30) -> str:
        """在远程shell中执行命令，返回输出+prompt"""
        # 生成唯一marker用于检测命令结束
        marker = f"__NARNAT_MARKER_{time.time_ns()}__"

        # 发送: 命令 + echo marker + pwd（获取当前目录）
        # 这样marker之后一定会有prompt，我们可以读到prompt
        full_cmd = f"{command}; echo {marker}\n"
        self._channel.send(full_cmd)

        # 读取输出直到marker出现，然后继续读一点拿到prompt
        return self._read_until_marker(marker, timeout=timeout)

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

    def _read_until_marker(self, marker: str, timeout: int = 30) -> str:
        """读取channel输出，直到读到marker，然后继续读prompt"""
        output = ""
        deadline = time.time() + timeout
        marker_found = False

        while time.time() < deadline:
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk

                if not marker_found and marker in output:
                    marker_found = True
                    # marker找到了，再读一小段时间等prompt出现
                    # prompt通常在marker后立即出现
                    prompt_deadline = time.time() + 0.3
                    while time.time() < prompt_deadline:
                        try:
                            extra = self._channel.recv(4096).decode("utf-8", errors="replace")
                            output += extra
                        except socket.timeout:
                            break
                    break

            except socket.timeout:
                if marker_found:
                    break
                continue
            except Exception:
                break

        # 解析输出：分离命令输出和prompt
        cmd_output, prompt = self._parse_output(output, marker)
        self._last_prompt = prompt

        # 截断
        if len(cmd_output) > MAX_BASH_OUTPUT:
            cmd_output = cmd_output[:MAX_BASH_OUTPUT] + f"\n... (输出超过{MAX_BASH_OUTPUT}字符，已截断)"

        # 返回: 命令输出 + prompt行
        if cmd_output and prompt:
            return f"{cmd_output}\n{prompt}"
        elif prompt:
            return prompt
        elif cmd_output:
            return cmd_output
        else:
            return f"[超时: 命令执行超过{timeout}秒]"

    def _parse_output(self, raw: str, marker: str) -> tuple[str, str]:
        """解析原始输出，分离命令输出和prompt

        原始输出结构:
          命令回显\n  命令输出\n  marker\n  prompt

        返回: (命令输出, prompt)
        """
        # 先按marker分割
        parts = raw.split(marker)
        before_marker = parts[0] if len(parts) > 0 else ""
        after_marker = parts[1] if len(parts) > 1 else ""

        # before_marker: 命令回显 + 命令输出
        # 去掉第一行（命令回显）
        lines = before_marker.split("\n")
        # 找到命令回显行（通常第一行包含我们发送的命令）
        cmd_output_lines = []
        for i, line in enumerate(lines):
            # 跳过第一行（命令回显）
            if i == 0:
                continue
            cmd_output_lines.append(line)

        # after_marker: prompt
        prompt = self._clean_output(after_marker)

        cmd_output = self._clean_output("\n".join(cmd_output_lines))

        return cmd_output, prompt

    def _read_until_prompt(self, timeout: int = 5) -> tuple[str, str]:
        """等待shell初始化完成，返回(初始输出, prompt)"""
        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk
                # 检测常见prompt特征
                if re.search(r'[#$>]\s*$', output.strip()):
                    break
            except socket.timeout:
                continue
            except Exception:
                break

        cleaned = self._clean_output(output)
        # 分离初始输出和prompt
        # prompt通常是最后一行
        lines = cleaned.split("\n")
        if len(lines) > 1 and re.search(r'[#$>]', lines[-1]):
            prompt = lines[-1].strip()
            init_output = "\n".join(lines[:-1]).strip()
            return init_output, prompt
        return cleaned, ""

    @staticmethod
    def _clean_output(raw: str) -> str:
        """清洗ANSI转义码和回车符"""
        # CSI序列: ESC [ (可选?) (数字;)* 字母  — 覆盖颜色、光标、bracketed paste等
        # OSC序列: ESC ] ... BEL/ST
        # 其他: ESC (字母) 等单字符序列
        ansi_re = re.compile(
            r'\x1b\[\??[0-9;]*[a-zA-Z]'   # CSI: ESC[?2004l, ESC[0m, ESC[1;34m 等
            r'|\x1b\].*?(?:\x07|\x1b\\)'  # OSC: ESC]...BEL 或 ESC]...ST
            r'|\x1b[()][A-Za-z0-9]'       # 字符集选择: ESC(B, ESC)0 等
        )
        cleaned = ansi_re.sub('', raw)
        cleaned = cleaned.replace('\r', '')
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
                # 返回当前prompt，让AI知道自己在哪
                prompt = session._last_prompt
                return f"会话已存在: {session_key}\n{prompt}" if prompt else f"会话已存在: {session_key}，直接exec即可"
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

        # 返回初始输出 + prompt
        parts = [f"已连接: {session_key}"]
        if session._initial_output:
            parts.append(session._initial_output)
        if session._last_prompt:
            parts.append(session._last_prompt)
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
            prompt = f" | {session._last_prompt}" if session._last_prompt else ""
            lines.append(f"  {key} [{alive}]{prompt}")
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
