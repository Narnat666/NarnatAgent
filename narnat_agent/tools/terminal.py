"""Terminal工具 —— 多终端可持续SSH

纯管道原则:
- AI输入什么就发送什么，不做翻译/注入/截断
- 设备输出什么就返回什么，不做裁剪
- 超时只告知AI，不替AI杀进程

核心设计:
- 支持最多MAX_SESSIONS(5)个并发SSH会话，每个会话有唯一session_id(0-4)
- AI通过session_id指定在哪个终端操作，实现多终端并行
- 每个SSH连接是一个会话(session)，通过session_id标识
- AI发命令 → 写入channel → 读取输出 → 返回给AI
- 会话持久化，多次调用复用同一连接
- 哨兵机制: 追加 echo __MARKER__$?; pwd -P; echo __PWD_MARKER__ 检测命令结束
- timeout默认120秒，超时告知AI命令仍在运行（AI可去其他终端继续工作）

sudo密码自动注入:
- connect时可选设置sudo_password，后续exec遇到sudo密码提示自动注入
- 密码通过channel直接写入，不经过shell命令行，不出现在ps/历史记录中
- 未设置sudo_password时，检测到密码提示返回提示信息，AI可用input action手动输入

输出解析(PTY基础设施，不是翻译):
- _strip_echo: 剥离PTY命令回显(不是AI命令的输出)
- _clean_output: 清洗ANSI码、内部标记、\\r覆盖(PTY噪声)
"""

import os
import re
import time
import threading
from typing import Callable, Optional

import paramiko
import socket


# 删除命令正则（仅保留删除确认，其他全部放行）
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# sudo/密码提示检测正则（用于自动注入sudo_password）
_RE_PASSWORD_PROMPT = re.compile(
    r"\[sudo\].*password"
    r"|Password\s*[:：]"
    r"|密码\s*[:：]"
    r"|passphrase\s*for\s+key",
    re.IGNORECASE,
)

_confirm_callback: Optional[Callable[[str], bool]] = None


def set_confirm_callback(cb: Callable[[str], bool]):
    """设置删除确认回调。cb返回True表示允许执行。"""
    global _confirm_callback
    _confirm_callback = cb


MAX_SESSIONS = 5  # 最多5个并发SSH会话

# session_id(0-4) → SSHSession
_sessions: dict[int, "SSHSession"] = {}
_sessions_lock = threading.Lock()

# 当前正在执行命令的SSH会话（agent层ESC打断后调用kill_active_exec杀死远程进程）
_active_exec_session: Optional["SSHSession"] = None
_active_exec_lock = threading.Lock()


def kill_active_exec():
    """ESC打断：发Ctrl+C终止远程进程，设中断标志让本地读取线程退出。"""
    with _active_exec_lock:
        session = _active_exec_session
    if session is not None:
        session._interrupt.set()
        try:
            session._channel.send("\x03")
        except Exception:
            pass


class SSHSession:
    """一个SSH交互式会话"""

    def __init__(self, host: str, username: str, port: int = 22,
                 key_path: Optional[str] = None, password: Optional[str] = None,
                 sudo_password: Optional[str] = None):
        self.host = host
        self.username = username
        self.port = port
        self._cwd = "~"
        self._sudo_password = sudo_password  # 用于自动注入sudo密码

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

        self._busy = False  # 通道是否被未完成的前台命令占用
        self._interrupt = threading.Event()  # ESC中断标志，各读取方法检测此标志退出

    def _initialize(self):
        """阻塞初始化：读初始输出、更新cwd。必须在 connect 中注册 _active_exec_session 之后调用，
        这样 ESC 打断 connect 时能通过 kill_active_exec() 关闭此会话。"""
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

    def execute(self, command: str, timeout: int = 0, max_output_chars: int = 2000) -> str:
        """在远程shell中执行命令，返回输出+prompt

        纯管道原则: AI输入什么就发送什么，不做翻译/注入。
        sudo密码自动注入: 检测到密码提示时，若session有sudo_password则自动注入。

        哨兵机制: 追加 echo __MARKER__$?; pwd -P; echo __PWD_MARKER__
        用于检测命令结束和捕获退出码，这是管道基础设施，不是翻译。

        timeout:
          >0  - 等待指定秒数，超时返回已收集输出+超时提示
          ≤0  - 等价于0（由上层校验保证不传，此处仅兜底）
        max_output_chars:
          返回内容最大字符数，默认2000。设为0或负数表示不限制
        """
        # 通道忙(上一个命令超时未完成)，直接告知AI
        if self._busy:
            return f"[上一个命令尚未完成，此终端暂不可用]\n{self.prompt}"

        # 发送新命令前，排空channel中可能残留的上次输出
        self._drain_stale_output()

        marker = f"__NARNAT_MARKER_{time.time_ns()}__"
        pwd_marker = f"__NARNAT_PWD_{time.time_ns()}__"

        # 用 $? 捕获退出码附加在marker行，pwd -P 独立获取路径
        full_cmd = f"{command}; echo {marker}$?; pwd -P; echo {pwd_marker}\n"
        self._channel.send(full_cmd)

        result = self._read_until_marker(marker, pwd_marker, timeout=timeout)
        return _truncate_output(result, max_output_chars)

    def send_input(self, text: str, timeout: int = 0, max_output_chars: int = 2000) -> str:
        """向当前终端发送交互输入（如sudo密码、确认提示等）

        直接通过channel写入文本+换行，然后读取直到下一个prompt。
        不追加哨兵marker，因为这是对已有交互提示的响应。

        Args:
            text: 要输入的文本（如密码、y/n确认等）
            timeout: 等待响应的超时秒数，默认由上层传入120秒
            max_output_chars: 返回内容最大字符数，默认2000。设为0或负数表示不限制
        """
        if self._busy:
            return f"[上一个命令尚未完成，此终端暂不可用]\n{self.prompt}"

        # 直接写入channel，不经过shell命令行
        self._channel.send(text + "\n")

        # 读取后续输出，等待命令完成(用marker机制)
        marker = f"__NARNAT_MARKER_{time.time_ns()}__"
        pwd_marker = f"__NARNAT_PWD_{time.time_ns()}__"
        # 发送一个空命令来获取marker，检测输入后的命令是否完成
        self._channel.send(f"echo {marker}$?; pwd -P; echo {pwd_marker}\n")

        result = self._read_until_marker(marker, pwd_marker, timeout=timeout)
        return _truncate_output(result, max_output_chars)

    def close(self):
        """关闭会话。channel立即关闭，transport在后台线程关闭，
        避免Windows closesocket不打断recv导致的5秒阻塞。"""
        try:
            self._channel.close()
        except Exception:
            pass
        # 后台线程关闭transport，不阻塞调用者
        t = threading.Thread(target=self._close_transport, daemon=True)
        t.start()

    def _close_transport(self):
        """后台线程：关闭paramiko transport，回收TCP连接。"""
        try:
            self._client.close()
        except Exception:
            pass

    def _try_read_residual(self, duration: float = 3.0) -> str:
        """安静地读取channel中残余数据，不中断任何命令。"""
        result = ""
        deadline = time.time() + duration
        consecutive_timeouts = 0
        while time.time() < deadline:
            if self._interrupt.is_set():
                break
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                if chunk:
                    result += chunk
                    consecutive_timeouts = 0
                else:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 5:
                        break
            except socket.timeout:
                if self._interrupt.is_set():
                    break
                consecutive_timeouts += 1
                if consecutive_timeouts >= 5:
                    break
            except Exception:
                break
        return result

    def _drain_stale_output(self):
        """排空channel中残留的旧输出，防止污染当前命令。"""
        old_timeout = self._channel.gettimeout()
        try:
            self._channel.settimeout(0.02)
            deadline = time.time() + 0.15
            consecutive_timeouts = 0
            while time.time() < deadline:
                try:
                    chunk = self._channel.recv(4096)
                    if not chunk:
                        break
                    consecutive_timeouts = 0
                except socket.timeout:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 2:
                        break
        except Exception:
            pass
        finally:
            self._channel.settimeout(old_timeout)

    def _update_cwd(self, timeout: int = 3):
        """通过执行pwd命令更新当前工作目录"""
        marker = f"__NARNAT_CWD_{time.time_ns()}__"
        self._channel.send(f"pwd -P; echo {marker}\n")

        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._interrupt.is_set():
                break
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                output += chunk
                if marker in output:
                    break
            except socket.timeout:
                if self._interrupt.is_set():
                    break
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

    def _start_busy_watcher(self, marker: str, pwd_marker: str):
        """超时后启动后台线程，持续读channel，等命令完成后自动清除busy标记。"""
        def _watch():
            output = ""
            while True:
                if self._interrupt.is_set():
                    self._busy = False
                    return
                try:
                    chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                    if not chunk:
                        # channel关闭/EOF，清除busy
                        self._busy = False
                        return
                    output += chunk
                    if pwd_marker in output:
                        # 命令完成了，清除busy标记
                        self._busy = False
                        # 更新cwd
                        before_marker = output.split(marker)[0]
                        lines = before_marker.strip().split("\n")
                        for line in reversed(lines):
                            cleaned = self._clean_output(line).strip()
                            if cleaned and (not cleaned.startswith("echo ")) and (("/" in cleaned) or (cleaned == "/")):
                                self._cwd = cleaned
                                break
                        return
                except socket.timeout:
                    if self._interrupt.is_set():
                        self._busy = False
                        return
                    continue
                except Exception:
                    # channel断开等异常，清除busy
                    self._busy = False
                    return

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def _read_until_marker(self, marker: str, pwd_marker: str, timeout: int = 0) -> str:
        """读取channel输出，直到读到pwd_marker。

        timeout:
          >0  - 等待指定秒数，超时返回已收集输出+超时提示
          ≤0  - 兜底：上层调用保证传入正数

        纯管道原则: 超时只告知AI，不替AI杀进程。
        ESC铁律: 用户按ESC立即中断，发Ctrl+C，宁可丢数据不卡住。
        sudo注入: 检测到密码提示时自动注入sudo_password(若有)。
        """
        output = ""
        # timeout≤0 兜底为无限等待（上层调用保证传正数）
        deadline = time.time() + timeout if timeout > 0 else float('inf')
        found = False
        # 找到marker后，连续recv超时次数达到此阈值才认为数据读完
        DRAIN_CONSECUTIVE_TIMEOUTS = 3
        # sudo密码注入状态: 是否已注入过(防止重复注入)
        sudo_injected = False

        while time.time() < deadline:
            # 中断检查：ESC打断时立即退出（数据路径中也检查，不只依赖timeout分支）
            if self._interrupt.is_set():
                break

            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                # EOF检测：channel关闭/远端断开时recv返回空字节，必须立即退出
                if not chunk:
                    break
                output += chunk

                # sudo密码提示检测与自动注入
                if not sudo_injected and not found:
                    cleaned_chunk = self._clean_output(output)
                    if _RE_PASSWORD_PROMPT.search(cleaned_chunk):
                        if self._sudo_password:
                            # 自动注入: 通过channel直接写入，不经过shell命令行
                            self._channel.send(self._sudo_password + "\n")
                            sudo_injected = True
                        else:
                            # 未设置sudo_password，告知AI
                            return f"{self._clean_output(self._strip_echo(output))}\n[检测到密码提示，请用input action输入密码，或在connect时设置sudo_password]"

                # 检测哨兵：跳过回显行（PTY会回显完整命令，含marker，不能误匹配）
                # 回显是output的第一行，从第二行开始检测
                first_newline = output.find('\n')
                search_region = output[first_newline + 1:] if first_newline >= 0 else ""
                if not found and pwd_marker in search_region:
                    found = True
                    # 继续读取，等待prompt出现或连续超时
                    # prompt格式: user@host:path$ (可能含~缩写)
                    prompt_pattern = re.compile(r'[#$>]\s*$')
                    consecutive_timeouts = 0
                    # 最多再读3秒，确保prompt和尾部数据到达
                    post_marker_deadline = time.time() + 3.0
                    while time.time() < post_marker_deadline:
                        if self._interrupt.is_set():
                            break
                        try:
                            extra = self._channel.recv(4096).decode("utf-8", errors="replace")
                            # EOF检测：channel关闭时立即退出
                            if not extra:
                                consecutive_timeouts += 1
                                if consecutive_timeouts >= DRAIN_CONSECUTIVE_TIMEOUTS:
                                    break
                                continue
                            output += extra
                            consecutive_timeouts = 0
                            # 检查是否已读到prompt(shell就绪)
                            last_lines = output.rstrip().split('\n')
                            if last_lines and prompt_pattern.search(last_lines[-1]):
                                break
                        except socket.timeout:
                            if self._interrupt.is_set():
                                break
                            consecutive_timeouts += 1
                            if consecutive_timeouts >= DRAIN_CONSECUTIVE_TIMEOUTS:
                                break
                        except Exception:
                            break
                    break

            except socket.timeout:
                if self._interrupt.is_set() or found:
                    break
                continue
            except Exception:
                break

        # 超时/中断处理: 发送Ctrl+C终止远程进程，排空channel后恢复正常
        if not found:
            # 记录触发来源（必须在clear()之前，之后flag就丢了）
            interrupted = self._interrupt.is_set()
            # 发送Ctrl+C终止远程正在运行的进程
            try:
                self._channel.send("\x03")
            except Exception:
                pass
            # 清除中断标志，允许排空阶段正常读取（中断只针对循环，排空需要正常收数据）
            self._interrupt.clear()
            # 等待远程进程终止、shell恢复并输出哨兵
            residual = self._try_read_residual(duration=3.0)
            if residual:
                output += residual
                # 跳过回显行检测哨兵
                first_nl = output.find('\n')
                check_region = output[first_nl + 1:] if first_nl >= 0 else ""
                if pwd_marker in check_region:
                    found = True

            # Ctrl+C后哨兵出现了 → 走正常解析(远程进程已被终止)
            if found:
                self._busy = False
                cmd_output, cwd = self._parse_output(output, marker, pwd_marker)
                if cwd:
                    self._cwd = cwd
                tag = "[ESC中断]" if interrupted else f"[超时中断: {timeout}秒]"
                if cmd_output:
                    return f"{cmd_output}\n{tag}\n{self.prompt}"
                else:
                    return f"{tag}\n{self.prompt}"

            # 哨兵仍未出现（极少见：进程忽略信号或shell异常）
            # 不再启动busy_watcher，直接标记空闲
            self._busy = False
            cmd_output = self._parse_partial_output(output, marker)
            tag = "[ESC中断，未收到哨兵]" if interrupted else f"[超时中断: {timeout}秒，未收到哨兵]"
            if cmd_output:
                return f"{cmd_output}\n{tag}\n{self.prompt}"
            else:
                return f"{tag}\n{self.prompt}"

        # 正常解析
        self._busy = False
        cmd_output, cwd = self._parse_output(output, marker, pwd_marker)
        if cwd:
            self._cwd = cwd

        if cmd_output:
            return f"{cmd_output}\n{self.prompt}"
        else:
            return self.prompt

    def _parse_output(self, raw: str, marker: str, pwd_marker: str) -> tuple[str, Optional[str]]:
        """解析正常完成的输出，返回 (命令输出, cwd)"""
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
            if self._interrupt.is_set():
                break
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                output += chunk
                if re.search(r'[#$>]\s*$', output.strip()):
                    break
            except socket.timeout:
                if self._interrupt.is_set():
                    break
                continue
            except Exception:
                break
        return self._clean_output(output)

    @staticmethod
    def _strip_echo(raw: str) -> str:
        """剥离PTY命令回显(第一行+续行"> "前缀)"""
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
        """清洗ANSI转义码、回车覆盖、内部标记(PTY噪声)"""
        ansi_re = re.compile(
            r'\x1b\[\??[0-9;]*[a-zA-Z]'
            r'|\x1b\].*?(?:\x07|\x1b\\)'
            r'|\x1b[()][A-Za-z0-9]'
            r'|\x1b[0-9:;<=>?@[A-Z\[\]^_`]'  # DEC私有序列: ESC 7(保存光标), ESC 8(恢复光标)等
        )
        cleaned = ansi_re.sub('', raw)

        # 回车覆盖合并: \r后面的内容覆盖同行前面内容
        # 逐行处理，每行内按\r分段，后段覆盖前段
        lines = cleaned.split('\n')
        merged_lines = []
        for line in lines:
            if '\r' not in line:
                merged_lines.append(line)
                continue
            # 按\r分段，模拟终端覆盖行为
            segments = line.split('\r')
            # 每个segment覆盖前一个segment的对应位置
            result = ""
            for seg in segments:
                if not seg:
                    continue
                # seg覆盖result的前len(seg)个字符
                if len(seg) >= len(result):
                    result = seg
                else:
                    result = seg + result[len(seg):]
            merged_lines.append(result)
        cleaned = '\n'.join(merged_lines)

        # 清理内部标记: __NARNAT_MARKER_xxx__, __NARNAT_CWD_xxx__, __NARNAT_PWD_xxx__
        cleaned = re.sub(r'__NARNAT_(?:MARKER|CWD|PWD)_\d+__', '', cleaned)

        # 清理续行提示符: 行首的 "> " (PS2 prompt回显)
        cleaned = re.sub(r'(^|\n)> ', r'\1', cleaned)

        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()


# 公开接口

def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出到指定字符数，超出部分附加提示"""
    if max_chars <= 0:
        return "(max_output_chars必须为正整数)"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[已截断: 输出共{len(text)}字符, 当前显示前{max_chars}字符。增大max_output_chars可获取完整输出]"


def execute(
    action: str = "exec",
    host: str = "",
    username: str = "",
    port: int = 22,
    key_path: str = "",
    password: str = "",
    sudo_password: str = "",
    command: str = "",
    input: str = "",
    timeout: int = 120,
    session_id: int = -1,
    max_output_chars: int = 2000,
) -> str:
    """
    Terminal工具：多终端可持续SSH。

    action:
      connect  - 建立SSH会话（首次连接或重连），自动分配或使用指定session_id
      exec     - 在指定会话中执行命令
      input    - 向终端发送交互输入（如sudo密码、确认提示等）
      status   - 查看当前所有会话状态
      close    - 关闭指定会话

    session_id:
      0-4  - 指定终端编号
      -1   - 自动选择（connect时自动分配，exec时选唯一活跃会话）

    sudo_password:
      connect时设置，后续exec遇到sudo密码提示自动注入

    max_output_chars:
      返回内容最大字符数，默认2000。设为0或负数表示不限制
    """
    if action == "connect":
        return _connect(host, username, port, key_path, password, sudo_password, session_id)
    elif action == "exec":
        return _exec(session_id, host, command, timeout, max_output_chars)
    elif action == "input":
        return _input(session_id, host, input, timeout, max_output_chars)
    elif action == "status":
        return _status()
    elif action == "close":
        return _close(session_id, host)
    else:
        return f"错误: 未知action '{action}'，可选: connect/exec/input/status/close"


def _allocate_session_id() -> int:
    """分配一个空闲的session_id，返回-1表示已满"""
    for i in range(MAX_SESSIONS):
        if i not in _sessions:
            return i
    return -1


def _resolve_session_id(session_id: int, host: str = "") -> tuple[int, "SSHSession"]:
    """解析session_id，返回 (session_id, session) 或抛出ValueError

    逻辑:
    1. session_id >= 0: 直接查找
    2. session_id == -1 且指定host: 按host模糊匹配
    3. session_id == -1 且无host: 只有一个会话时自动选择
    """
    with _sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id not in _sessions:
                raise ValueError(f"终端{session_id}未连接，请先connect")
            return session_id, _sessions[session_id]

        # 未指定session_id，按host匹配
        if host:
            for sid, session in _sessions.items():
                if host in session.host or host in f"{session.username}@{session.host}":
                    return sid, session
            raise ValueError(f"未找到host={host}的会话，请先connect")

        # 未指定session_id和host，自动选择唯一会话
        if len(_sessions) == 1:
            sid = list(_sessions.keys())[0]
            return sid, _sessions[sid]
        elif len(_sessions) == 0:
            raise ValueError("无活跃会话，请先connect")
        else:
            keys = list(_sessions.keys())
            raise ValueError(f"有多个会话，请指定session_id，当前终端: {keys}")


def _connect(host: str, username: str, port: int = 22,
             key_path: str = "", password: str = "",
             sudo_password: str = "",
             session_id: int = -1) -> str:
    """建立SSH会话"""
    if not host or not username:
        return "错误: connect需要提供host和username"

    with _sessions_lock:
        # 指定了session_id
        if session_id >= 0:
            if session_id >= MAX_SESSIONS:
                return f"错误: session_id范围0-{MAX_SESSIONS - 1}"
            if session_id in _sessions:
                session = _sessions[session_id]
                if not session._channel.closed:
                    return f"终端{session_id}已连接: {session.username}@{session.host}\n{session.prompt}"
                else:
                    session.close()
                    del _sessions[session_id]
            alloc_id = session_id
        else:
            # 自动分配
            alloc_id = _allocate_session_id()
            if alloc_id < 0:
                active = list(_sessions.keys())
                return f"错误: 已达最大会话数({MAX_SESSIONS})，当前终端: {active}，请先close释放"

    try:
        kwargs = {"host": host, "username": username, "port": port}
        if key_path:
            kwargs["key_path"] = key_path
        if password:
            kwargs["password"] = password
        if sudo_password:
            kwargs["sudo_password"] = sudo_password

        session = SSHSession(**kwargs)

        # 注册活跃会话，让 ESC 能在 connect 的阻塞初始化阶段打断
        with _active_exec_lock:
            global _active_exec_session
            _active_exec_session = session
        try:
            session._initialize()
        finally:
            with _active_exec_lock:
                _active_exec_session = None

        with _sessions_lock:
            _sessions[alloc_id] = session

        parts = [f"已连接终端{alloc_id}: {username}@{host}"]
        if session._initial_output:
            parts.append(session._initial_output)
        parts.append(session.prompt)
        return "\n".join(parts)

    except paramiko.AuthenticationException:
        return f"错误: 认证失败({username}@{host})，请检查key_path或password"
    except paramiko.SSHException as e:
        return f"错误: SSH连接失败({username}@{host}): {e}"
    except Exception as e:
        return f"错误: 连接失败({username}@{host}): {e}"


def _exec(session_id: int, host: str, command: str, timeout: int = 120, max_output_chars: int = 2000) -> str:
    """在指定会话中执行命令"""
    if not command:
        return "错误: exec需要提供command"
    if timeout <= 0:
        return "错误: timeout必须为正整数（秒），默认120秒"

    # 安全检查：删除命令需用户确认
    if _RE_DELETE.search(command):
        if _confirm_callback and not _confirm_callback(command):
            return "操作已取消: 删除命令需用户确认"

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return f"错误: {e}"

    if session._channel.closed:
        with _sessions_lock:
            _sessions.pop(sid, None)
        session.close()
        return f"错误: 终端{sid}会话已断开，请重新connect"

    try:
        # 注册活跃会话，agent层ESC打断后可通过kill_active_exec发送Ctrl+C
        with _active_exec_lock:
            global _active_exec_session
            _active_exec_session = session
        try:
            result = session.execute(command, timeout=timeout, max_output_chars=max_output_chars)
        finally:
            with _active_exec_lock:
                _active_exec_session = None
        # 在结果前标注终端编号
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"错误: 终端{sid}命令执行失败: {e}"


def _input(session_id: int, host: str, input: str, timeout: int = 120, max_output_chars: int = 2000) -> str:
    """向终端发送交互输入"""
    if not input:
        return "错误: input需要提供input内容"
    if timeout <= 0:
        return "错误: timeout必须为正整数（秒），默认120秒"

    try:
        sid, session = _resolve_session_id(session_id, host)
    except ValueError as e:
        return f"错误: {e}"

    if session._channel.closed:
        with _sessions_lock:
            _sessions.pop(sid, None)
        session.close()
        return f"错误: 终端{sid}会话已断开，请重新connect"

    try:
        result = session.send_input(input, timeout=timeout, max_output_chars=max_output_chars)
        return f"[终端{sid}] {result}"
    except Exception as e:
        return f"错误: 终端{sid}输入发送失败: {e}"


def _status() -> str:
    """查看所有会话状态"""
    with _sessions_lock:
        if not _sessions:
            return f"(无活跃SSH会话，最多支持{MAX_SESSIONS}个并发终端)"

        lines = []
        for sid in range(MAX_SESSIONS):
            if sid in _sessions:
                session = _sessions[sid]
                alive = "活跃" if not session._channel.closed else "已断开"
                busy = "忙" if session._busy else "闲"
                lines.append(f"  终端{sid}: {session.username}@{session.host} [{alive}|{busy}] {session.prompt}")
            else:
                lines.append(f"  终端{sid}: (空闲)")
        return "SSH会话:\n" + "\n".join(lines)


def _close(session_id: int, host: str) -> str:
    """关闭会话"""
    with _sessions_lock:
        if session_id < 0 and not host:
            # 关闭所有
            for session in _sessions.values():
                session.close()
            count = len(_sessions)
            _sessions.clear()
            return f"已关闭{count}个会话"

        # 指定了session_id
        if session_id >= 0:
            if session_id not in _sessions:
                return f"终端{session_id}未连接"
            _sessions[session_id].close()
            del _sessions[session_id]
            return f"已关闭终端{session_id}"

        # 按host匹配
        matched = None
        for sid, session in _sessions.items():
            if host in session.host or host in f"{session.username}@{session.host}":
                matched = sid
                break

        if matched is None:
            return f"未找到host={host}的会话"

        _sessions[matched].close()
        del _sessions[matched]
        return f"已关闭终端{matched}"


def cleanup():
    """程序退出时清理所有会话。channel立即关闭，transport后台回收。"""
    with _sessions_lock:
        for session in _sessions.values():
            session.close()
        _sessions.clear()


def get_session(session_id: int = -1, host: str = "") -> Optional["SSHSession"]:
    """获取指定SSH会话（供SFTP等内部使用）"""
    try:
        _, session = _resolve_session_id(session_id, host)
        return session
    except ValueError:
        return None
