"""
SSH交互式会话 ── 单个SSH连接的封装

纯管道原则:
- AI输入什么就发送什么，不做翻译/注入/截断
- 设备输出什么就返回什么，不做裁剪
- 超时只告知AI，不替AI杀进程

核心设计:
- 每个SSH连接是一个会话(session)，通过session_id标识
- AI发命令 → 写入channel → 读取输出 → 返回给AI
- 哨兵机制: 追加 echo __MARKER__$?; pwd -P; echo __PWD_MARKER__ 检测命令结束
- timeout默认120秒，超时告知AI命令仍在运行：
  * 命令继续后台运行，终端标记为忙(busy)，后台watcher等命令完成后自动清除busy，
    并把完成输出存进backlog，下次exec时返回
  * AI可用 input 应答命令的交互提示（y/n、密码等），或用 input 发送 ^C 中断它
  * input 仅当有命令在等待输入时有效；空闲时拒绝发送，防止输入内容被当作命令执行

sudo密码自动注入:
- connect时可选设置sudo_password，后续exec遇到sudo密码提示自动注入
- 密码通过channel直接写入，不经过shell命令行，不出现在ps/历史记录中

输出解析(PTY基础设施，不是翻译):
- _strip_echo: 剥离PTY命令回显(不是AI命令的输出)
- _clean_output: 清洗ANSI码、内部标记、\\r覆盖(PTY噪声)
"""

import os
import re
import time
import threading
from typing import Optional

import paramiko
import socket


def _ansi_sub(text: str) -> str:
    """剥离ANSI转义序列"""
    return SSHSession.ANSI_RE.sub('', text)


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部含提示符，对AI判断shell状态至关重要），中段提示"""
    if max_chars <= 0:
        return "[错误: max_output_chars需为正整数]"
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    return (
        text[:head]
        + f"\n...[中间截断: 输出共{len(text)}字符, 已保留首{head}字符+尾{tail}字符。增大max_output_chars可获取完整输出]\n"
        + text[-tail:]
    )


class SSHSession:
    """一个SSH交互式会话"""

    # ── 会话参数常量（原模块级常量收敛于此）──
    # sudo/密码提示检测正则（用于自动注入sudo_password）
    RE_PASSWORD_PROMPT = re.compile(
        r"\[sudo\].*password"
        r"|Password\s*[:：]"
        r"|密码\s*[:：]"
        r"|passphrase\s*for\s+key",
        re.IGNORECASE,
    )

    # sudo 密码被拒绝的判别（覆盖主流sudo/busybox sudo文案）
    RE_SUDO_REJECT = re.compile(
        r"sorry.{0,30}try\s+again"
        r"|incorrect\s+password"
        r"|bad\s+password",
        re.IGNORECASE,
    )

    # 单次读循环内自动注入尝试上限：超过后视为无法自动完成，转交AI input
    # （覆盖：多连sudo、以及不输出拒绝文案的sudo变体反复要密码的场景）
    MAX_SUDO_INJECT_ATTEMPTS = 3

    # ANSI转义序列（_clean_output清洗 + _strip_echo判断续行复用）
    ANSI_RE = re.compile(
        r'\x1b\[\??[0-9;]*[a-zA-Z]'
        r'|\x1b\].*?(?:\x07|\x1b\\)'
        r'|\x1b[()][A-Za-z0-9]'
        r'|\x1b[0-9:;<=>?@[A-Z\[\]^_`]'  # DEC私有序列: ESC 7(保存光标), ESC 8(恢复光标)等
    )

    # 真实shell提示符行（user@host:path$ 形态），用于剥离恢复路径中重复的提示符
    PROMPT_LINE_RE = re.compile(r"^[^@]+@[^:]+:[^\n]*[#$>]\s*$")

    # 哨兵检测尾部窗口：哨兵永远出现在输出末尾（marker行+pwd输出+pwd_marker+prompt
    # 共数百字节），仅扫描尾部8KB即可判定，避免对全量输出做O(n²)切片搜索
    MARKER_TAIL_WINDOW = 8192

    def __init__(self, host: str, username: str, port: int = 22,
                 key_path: Optional[str] = None, password: Optional[str] = None,
                 sudo_password: Optional[str] = None, timeout: int = 15):
        self.host = host
        self.username = username
        self.port = port
        self._cwd = "~"
        self._sudo_password = sudo_password  # 用于自动注入sudo密码
        # 自动注入失败后置位：本会话后续不再自动注入（sudo密码与登录密码不同时，
        # 自动注入会反复失败并卡住命令；置位后密码提示一律转交AI用input注入）
        self._sudo_mismatch = False

        # 提前创建中断标志：connect 阻塞期间 ESC 打断（kill_active_exec）会访问
        # session._interrupt，若迟至 connect 之后才创建会抛 AttributeError
        self._interrupt = threading.Event()

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # timeout: TCP连接/SSH banner/认证的socket超时。黑洞IP无此参数会阻塞
        # 数十秒（OS默认TCP重试），AI连错IP时长时间无响应
        connect_kwargs = {
            "hostname": host, "port": port, "username": username,
            "timeout": timeout, "banner_timeout": timeout, "auth_timeout": timeout,
        }
        if key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(key_path)
        if password:
            connect_kwargs["password"] = password
        else:
            # 始终传password（空字符串），让paramiko在密钥认证失败后fallback到密码认证
            # 不传password时paramiko不会尝试密码认证，导致空密码设备无法连接
            connect_kwargs["password"] = ""
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        self._client.connect(**connect_kwargs)
        self._client.get_transport().set_keepalive(30)

        self._channel = self._client.invoke_shell(term="xterm", width=200, height=50)
        self._channel.settimeout(0.5)

        self._busy = False  # 通道是否被未完成的前台命令占用
        self._last_command = ""  # 最近执行的命令，供_parse_output剥离命令回显

        # 待完成命令的哨兵（超时后input接管时复用）
        self._pending_marker = ""
        self._pending_pwd_marker = ""

        # 后台watcher控制 + 线程引用
        self._watcher_stop = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None

        # 后台命令完成输出缓存（下次exec/input时返回给AI）
        self._backlog = ""
        self._backlog_lock = threading.Lock()

    def _initialize(self):
        """阻塞初始化：读初始输出、更新cwd。必须在 connect 中注册活跃执行会话之后调用，
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

    def execute(self, command: str, timeout: int = 0, max_output_chars: int = 8000) -> str:
        """在远程shell中执行命令，返回输出+prompt

        纯管道原则: AI输入什么就发送什么，不做翻译/注入。
        sudo密码自动注入: 检测到密码提示时，若session有sudo_password则自动注入。

        哨兵机制: 追加 echo __MARKER__$?; pwd -P; echo __PWD_MARKER__
        用于检测命令结束和捕获退出码，这是管道基础设施，不是翻译。

        timeout:
          >0  - 等待指定秒数，超时返回已收集输出+超时提示（命令继续后台运行，终端标记为忙）
          ≤0  - 等价于0（由上层校验保证不传，此处仅兜底）
        max_output_chars:
          返回内容最大字符数，正整数，默认8000
        """
        # 通道忙(上一个命令超时未完成)，直接告知AI
        if self._busy:
            return (f"[上一个命令尚未完成，此终端暂不可用。"
                    f"可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]\n{self.prompt}")

        # 上一条后台命令的完成输出，先返回给AI（纯管道: 设备输出不丢失）
        backlog = self._drain_backlog()

        # 发送新命令前，排空channel中可能残留的上次输出
        self._drain_stale_output()

        marker = f"__NARNAT_MARKER_{time.time_ns()}__"
        pwd_marker = f"__NARNAT_PWD_{time.time_ns()}__"

        # 先捕获退出码到变量：$? 必须紧跟用户命令取值，中间插入 printf 会导致 $? 恒为
        # printf 的退出码(0) → 之前所有命令都报告 exit code: 0
        # printf '\n' 保证哨兵行独立成行：命令输出无尾随换行时（cat 无换行文件、printf 等），
        # 若不加换行，`echo MARKER<rc>` 会粘在输出尾部 → marker行startswith检测失败，
        # 连锁导致退出码污染输出、pwd泄漏、cwd不更新（prompt显示旧目录）
        full_cmd = f"{command}; rc=$?; printf '\\n'; echo {marker}$rc; pwd -P; echo {pwd_marker}\n"
        self._last_command = command  # 供_parse_output剥离多行命令首行回显
        self._channel.send(full_cmd)

        result = self._read_until_marker(marker, pwd_marker, timeout=timeout)

        if backlog.strip():
            result = f"[后台命令已完成，输出如下]\n{backlog.strip()}\n{'-' * 30}\n{result}"
        return _truncate_output(result, max_output_chars)

    def send_input(self, text: str, timeout: int = 0, max_output_chars: int = 8000) -> str:
        """向当前终端发送交互输入（如sudo密码、y/n确认等）

        语义:
        - 仅当有命令在等待输入时有效（终端忙，通常是上个命令超时仍在后台运行）
        - text = "^C" 或 "\\x03" 时发送原始Ctrl+C，中断仍在运行的命令
        - 空闲时拒绝发送，防止输入内容被当作shell命令执行（安全）

        Args:
            text: 要输入的文本（如密码、y/n确认等）
            timeout: 等待响应的超时秒数，默认由上层传入120秒
            max_output_chars: 返回内容最大字符数，正整数，默认8000
        """
        if not self._busy:
            # 无等待输入的命令：拒绝发送，防止输入内容被当作命令执行
            parts = []
            backlog = self._drain_backlog()
            if backlog.strip():
                parts.append(f"[后台命令已完成，输出如下]\n{backlog.strip()}")
            parts.append("[当前无命令等待输入，输入内容未发送（避免被当作命令执行）。如需执行命令请用 exec]")
            return "\n".join(parts)

        # 停止后台watcher并等其退出，接管channel读取（避免两线程并发recv抢数据）
        self._watcher_stop.set()
        wt = self._watcher_thread
        if wt is not None and wt.is_alive():
            wt.join(timeout=2.0)

        # watcher已收集的输出先返回（输入前的输出）
        backlog = self._drain_backlog()

        self._interrupt.clear()

        if text == "^C" or text == "\x03":
            # ── 中断仍在运行的命令：发送原始Ctrl+C ──
            # 注意: bash收到SIGINT后放弃整行剩余命令，exec追加的哨兵不会执行，
            # 因此这里不能等哨兵，改为等待shell提示符重新出现。
            self._channel.send("\x03")
            raw = self._read_until_interrupt_prompt(timeout=timeout)
            self._interrupt.clear()

            at_prompt = False
            last_line = raw.rstrip().split("\n")[-1] if raw.strip() else ""
            if last_line and re.search(r'[#$>]\s*$', _ansi_sub(last_line)):
                at_prompt = True

            if at_prompt:
                # 命令已被终止，shell回到提示符
                self._busy = False
                self._pending_marker = ""
                self._pending_pwd_marker = ""
                cleaned = self._clean_output(raw)
                body = self._strip_caret_echo(self._strip_trailing_prompt(cleaned))
                if backlog.strip():
                    body = (
                        f"[输入前输出]\n{backlog.strip()}\n{'-' * 30}\n{body}"
                        if body else f"[输入前输出]\n{backlog.strip()}"
                    )
                if body:
                    return _truncate_output(
                        f"{body}\n[已中断: 正在运行的命令已被 ^C 终止]\n{self.prompt}",
                        max_output_chars,
                    )
                return _truncate_output(
                    f"[已中断: 正在运行的命令已被 ^C 终止]\n{self.prompt}",
                    max_output_chars,
                )

            # 未回到提示符（命令忽略SIGINT等）：保持忙状态，重启watcher
            self._busy = True
            self._start_busy_watcher(self._pending_marker, self._pending_pwd_marker)
            body = self._clean_output(raw).strip()
            tag = "[^C已发送但命令未终止，仍在后台运行。可稍后再试，或由用户按ESC中断]"
            if backlog.strip():
                body = f"[输入前输出]\n{backlog.strip()}\n{'-' * 30}\n{body}" if body else f"[输入前输出]\n{backlog.strip()}"
            if body:
                return _truncate_output(f"{body}\n{tag}\n{self.prompt}", max_output_chars)
            return _truncate_output(f"{tag}\n{self.prompt}", max_output_chars)

        # ── 普通交互输入（y/n、密码等）──
        payload = text + "\n"
        self._last_command = text  # 供_parse_output剥离输入回显
        self._channel.send(payload)

        # 等待原命令完成（复用exec时发送的哨兵）
        result = self._read_until_marker(
            self._pending_marker, self._pending_pwd_marker, timeout=timeout
        )
        if backlog.strip():
            result = f"[输入前输出]\n{backlog.strip()}\n{'-' * 30}\n{result}"
        return _truncate_output(result, max_output_chars)

    def _read_until_interrupt_prompt(self, timeout: float) -> str:
        """发送Ctrl+C后读取，直到shell提示符重新出现或超时。返回原始输出。"""
        output = ""
        deadline = time.time() + timeout if timeout > 0 else time.time() + 120
        prompt_pattern = re.compile(r'[#$>]\s*$')
        while time.time() < deadline:
            if self._interrupt.is_set():
                break
            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
            except socket.timeout:
                continue
            except Exception:
                break
            if not chunk:
                break
            output += chunk
            last_lines = output.rstrip().split("\n")
            if last_lines and prompt_pattern.search(_ansi_sub(last_lines[-1])):
                break
        return output

    def close(self):
        """关闭会话。channel立即关闭，transport在后台线程关闭，
        避免Windows closesocket不打断recv导致的5秒阻塞。"""
        self._watcher_stop.set()
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
        """超时后启动后台线程，持续读channel，等命令完成后自动清除busy标记。

        行为:
        - 命令完成（读到pwd_marker）→ 更新cwd，完成输出存进backlog
        - input接管（_watcher_stop置位）→ 停止读取，已收集输出存进backlog
        - ESC中断/通道断开 → 停止
        无论如何退出都清除busy（finally保证），使终端状态可恢复。
        """
        self._watcher_stop.clear()

        def _watch():
            output = ""
            finished = False
            try:
                while not self._watcher_stop.is_set():
                    if self._interrupt.is_set():
                        break
                    try:
                        chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                    if not chunk:
                        break  # channel关闭/EOF
                    output += chunk
                    if pwd_marker in output:
                        finished = True
                        break
            finally:
                if finished:
                    # 只保留哨兵前的命令输出（哨兵行/退出码/pwd输出是基础设施噪声）
                    body = self._clean_output(output.split(marker, 1)[0]).strip()
                    if body:
                        self._append_backlog(body)
                    cwd = self._extract_cwd(output, marker, pwd_marker)
                    if cwd:
                        self._cwd = cwd
                else:
                    body = self._clean_output(output).strip()
                    if body:
                        self._append_backlog(body)
                self._busy = False

        self._watcher_thread = threading.Thread(target=_watch, daemon=True)
        self._watcher_thread.start()

    def _drain_backlog(self) -> str:
        """取出并清空backlog（线程安全）"""
        with self._backlog_lock:
            out = self._backlog
            self._backlog = ""
            return out

    def _append_backlog(self, text: str) -> None:
        """追加后台命令输出到backlog（线程安全）"""
        if not text:
            return
        with self._backlog_lock:
            self._backlog = (self._backlog + "\n" + text) if self._backlog else text

    def _extract_cwd(self, output: str, marker: str, pwd_marker: str) -> Optional[str]:
        """从输出中提取pwd（marker行与pwd_marker行之间的路径行），失败返回None"""
        lines = output.split("\n")
        marker_idx = None
        pwd_idx = None
        for i, line in enumerate(lines):
            stripped = _ansi_sub(line).replace("\r", "").strip()
            if marker_idx is None and stripped.startswith(marker):
                marker_idx = i
            if pwd_marker in line:
                pwd_idx = i
                break
        if marker_idx is None or pwd_idx is None:
            return None
        for i in range(marker_idx + 1, pwd_idx):
            cleaned = self._clean_output(lines[i]).strip()
            if cleaned and (cleaned.startswith("/") or cleaned == "/"):
                return cleaned
        return None

    def _read_until_marker(self, marker: str, pwd_marker: str, timeout: int = 0) -> str:
        """读取channel输出，直到读到pwd_marker。

        timeout:
          >0  - 等待指定秒数，超时返回已收集输出+超时提示
                （命令继续后台运行，终端标记为忙，AI可用input应答或^C中断）
          ≤0  - 兜底：上层调用保证传入正数

        纯管道原则: 超时只告知AI，不替AI杀进程。
        ESC铁律: 用户按ESC立即中断，发Ctrl+C，宁可丢数据不卡住。
        sudo注入: 检测到密码提示时自动注入sudo_password(若有)。
        """
        # 记录本次命令的哨兵，供 input 在超时后接管读取
        self._pending_marker = marker
        self._pending_pwd_marker = pwd_marker

        output = ""
        # timeout≤0 兜底为无限等待（上层调用保证传正数）
        deadline = time.time() + timeout if timeout > 0 else float('inf')
        found = False
        # 找到marker后，连续recv超时次数达到此阈值才认为数据读完
        DRAIN_CONSECUTIVE_TIMEOUTS = 3
        # sudo密码注入状态: 是否已注入过(防止重复注入)
        sudo_injected = False
        # 本次读循环内累计注入次数（限次：防反复注入卡死，超限转交AI）
        inject_attempts = 0
        # 最近一次注入时的输出长度：注入后无新数据时不重复评估同一旧提示
        inject_mark_len = 0
        # 密码提示疑似时间戳: 0.0=无疑似。提示出现后需观察宽容期，避免命令自身输出含
        # "Password:"字样（如 echo "Password: x"）时被误判为真实密码提示
        prompt_suspect_ts = 0.0

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

                # 检测哨兵：跳过回显行（PTY会回显完整命令，含marker，不能误匹配）
                # 回显是output的第一行。哨兵永远出现在输出末尾，仅扫描尾部窗口
                # （此前对全量output做切片+搜索，大输出时O(n²)浪费CPU）
                if not found:
                    first_newline = output.find('\n')
                    if first_newline >= 0:
                        tail = output[max(first_newline + 1, len(output) - SSHSession.MARKER_TAIL_WINDOW):]
                        found = pwd_marker in tail
                        if found:
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
            except Exception:
                break

            # sudo密码提示检测与自动注入（三态状态机）
            # 在try/except外每次迭代都评估：真实提示出现后通道静默，宽容期计时
            # 必须靠超时轮空迭代推进（不能只在收到新数据时评估）。
            # 真实密码提示的判别条件（三重）:
            # 1. 清洗后输出含密码提示模式
            # 2. 原始输出尾部无换行 —— shell在提示符后阻塞等待输入；
            #    命令自身输出"Password:"字样（如 echo "Password: x"）以换行结尾
            # 3. 宽容期1.5秒内哨兵未到达 —— 误报时哨兵会紧随其后出现
            # 误判会误导AI输入密码、或把已完成的命令误标为busy。
            # 注入状态机:
            # - 未注入且未mismatch且有密码 → 自动注入
            # - 已注入后又出提示 → 有拒绝文案则判mismatch转交AI；
            #   无拒绝文案视为命令链中的下一个sudo，继续注入（限次）
            # - mismatch或未设密码 → 转交AI input
            if not found:
                # 仅清洗尾部窗口做提示检测：全量清洗在每chunk上重复执行是O(n²)，
                # 大输出命令（如cat大文件）会CPU飙升拖慢读取。密码提示总是出现在
                # 输出末尾（无尾随换行），尾部窗口足够判定
                if (not output.endswith(("\n", "\r"))
                        and SSHSession.RE_PASSWORD_PROMPT.search(self._clean_output(output[-2048:]))):
                    # 注入后通道仍静默（无新数据）：是同一份旧提示的重复评估，跳过。
                    # 否则sudo接受密码后命令静默运行期间，旧提示会被反复误判为新提示
                    # 导致重复注入（密码正确场景实测会连注两次）
                    if sudo_injected and len(output) <= inject_mark_len:
                        pass
                    elif prompt_suspect_ts == 0.0:
                        prompt_suspect_ts = time.time()
                    elif time.time() - prompt_suspect_ts >= 1.5:
                        if not sudo_injected and not self._sudo_mismatch and self._sudo_password:
                            # 自动注入: 通过channel直接写入，不经过shell命令行
                            self._channel.send(self._sudo_password + "\n")
                            sudo_injected = True
                            inject_attempts += 1
                            inject_mark_len = len(output)
                            prompt_suspect_ts = 0.0
                        elif sudo_injected:
                            rejected = SSHSession.RE_SUDO_REJECT.search(
                                self._clean_output(output[-4096:])
                            )
                            if rejected or inject_attempts >= SSHSession.MAX_SUDO_INJECT_ATTEMPTS:
                                # 判失败：本会话停用自动注入，密码提示一律转交AI input
                                self._sudo_mismatch = True
                                self._busy = True
                                self._start_busy_watcher(marker, pwd_marker)
                                reason = (
                                    "自动注入的登录密码被sudo拒绝"
                                    if rejected else "多次注入后仍在等待密码"
                                )
                                return (f"{self._clean_output(self._strip_echo(output))}\n"
                                        f"[{reason}：sudo密码与登录密码不同。"
                                        f"请向用户询问sudo密码，然后用input输入；"
                                        f"本会话后续不再自动注入]")
                            # 无拒绝文案: 命令链中的下一个sudo，继续注入
                            self._channel.send(self._sudo_password + "\n")
                            inject_attempts += 1
                            inject_mark_len = len(output)
                            prompt_suspect_ts = 0.0
                        else:
                            # mismatch 或未设置密码 → 告知AI，命令等待input
                            self._busy = True
                            self._start_busy_watcher(marker, pwd_marker)
                            return (f"{self._clean_output(self._strip_echo(output))}\n"
                                    f"[检测到密码提示，请用input action输入密码]")
                else:
                    prompt_suspect_ts = 0.0

        if not found:
            # 记录触发来源（必须在clear()之前，之后flag就丢了）
            interrupted = self._interrupt.is_set()

            if interrupted:
                # ── ESC打断: Ctrl+C已由kill_active_exec发送（远程进程正在终止）──
                # 只需排空channel收取 ^C 回显、提示符等残留输出
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
                    cmd_output, cwd, exit_code = self._parse_output(output, marker, pwd_marker)
                    if cwd:
                        self._cwd = cwd
                    cmd_output = self._strip_caret_echo(self._strip_trailing_prompt(cmd_output))
                    ec = f"[exit code: {exit_code}]\n" if exit_code is not None else ""
                    if cmd_output:
                        return f"{ec}{cmd_output}\n[用户中断]\n{self.prompt}"
                    else:
                        return f"{ec}[用户中断]\n{self.prompt}"

                # 哨兵仍未出现（极少见：进程忽略信号或shell异常）
                self._busy = False
                cmd_output = self._strip_caret_echo(self._strip_trailing_prompt(self._parse_partial_output(output, marker)))
                if cmd_output:
                    return f"{cmd_output}\n[用户中断]\n{self.prompt}"
                else:
                    return f"[用户中断]\n{self.prompt}"

            # ── 纯超时: 不杀进程，命令继续后台运行 ──
            # 终端标记为忙，后台watcher等命令完成后自动清除busy、缓存输出
            self._busy = True
            self._start_busy_watcher(marker, pwd_marker)
            cmd_output = self._parse_partial_output(output, marker)
            tag = (f"[超时: 命令执行超过{timeout}秒，仍在后台运行。"
                   f"可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]")
            if cmd_output:
                return f"{cmd_output}\n{tag}\n{self.prompt}"
            else:
                return f"{tag}\n{self.prompt}"

        # 正常解析
        self._busy = False
        cmd_output, cwd, exit_code = self._parse_output(output, marker, pwd_marker)
        if cwd:
            self._cwd = cwd

        ec = f"[exit code: {exit_code}]\n" if exit_code is not None else ""
        if cmd_output:
            return f"{ec}{cmd_output}\n{self.prompt}"
        else:
            return f"{ec}{self.prompt}"

    def _parse_output(self, raw: str, marker: str, pwd_marker: str) -> tuple[str, Optional[str], Optional[int]]:
        """解析正常完成的输出，返回 (命令输出, cwd, exit_code)"""
        exit_code = None
        cwd = None

        lines = raw.split("\n")

        # 按行查找: marker行以marker开头(后跟退出码数字)
        # 注意: 无输出命令时 \x1b[?2004l\r 会紧贴marker行前（如 true），需先剥离ANSI和\r
        marker_line_idx = None
        for i, line in enumerate(lines):
            if _ansi_sub(line).replace("\r", "").strip().startswith(marker):
                marker_line_idx = i
                # 提取退出码: marker行 = __NARNAT_MARKER_xxx__N
                exit_str = _ansi_sub(line).replace("\r", "").strip()[len(marker):]
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

        # 剥离命令回显：
        # 1. 含marker的行（命令回显/末行续行回显包含 "; echo <marker>" 或行首 "echo <marker>" 片段）
        # 2. "> "续行回显行（多行命令的中间续行，特征为含PTY续行序列 \x1b[?2004h 且剥离后以"> "开头）
        # 3. 命令首行回显（不含marker，特征为等于命令首行文本）
        # 注意: 真实输出行不含以上特征，不会被误删；这是比"首行=回显"更可靠的判断
        first_line = (self._last_command or "").split("\n")[0].strip()
        filtered = []
        first_content_seen = False
        for l in before_marker.split("\n"):
            ansi_clean = _ansi_sub(l).replace("\r", "").strip()
            if f"echo {marker}" in l:
                continue  # 哨兵命令行（exec回显或input路径的行首echo形态），含marker
            # 续行回显: PTY续行提示特征为 "\x1b[?2004h> "（开启序列后紧跟"> "提示符）。
            # 输出行是 "\x1b[?2004l\r内容"（关闭序列+内容，"> "是内容本身），不受影响
            if "\x1b[?2004h> " in l or "\x1b[?2004h>" in l:
                continue  # 续行回显
            # 首个非空行与命令首行/输入文本精确一致 → PTY回显，仅剥离一次。
            # 不能沿用startswith前缀匹配：input="G"时，真实输出行"GOT:G"以G开头
            # 会被误删（数据丢失）；同理exec命令首行也不能前缀匹配。
            if not first_content_seen and ansi_clean:
                first_content_seen = True
                if first_line and ansi_clean == first_line:
                    continue  # 命令/输入首行回显
            filtered.append(l)

        before_marker = "\n".join(filtered)

        # 命令回显已在上面剥离，首行是真实输出，不能再无条件跳首行（否则丢第一条输出）
        cmd_output = self._strip_echo(before_marker, drop_echo_first_line=False)

        return self._clean_output(cmd_output), cwd, exit_code

    def _parse_partial_output(self, raw: str, marker: str) -> str:
        """解析超时时的部分输出（marker可能还没出现）"""
        lines = raw.split("\n")

        # 按行查找marker行(以marker开头，先剥离ANSI和\r)
        marker_line_idx = None
        for i, line in enumerate(lines):
            if _ansi_sub(line).replace("\r", "").strip().startswith(marker):
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
    def _strip_caret_echo(text: str) -> str:
        """剥离开头的 ^C 回显行（PTY对Ctrl+C的回显）"""
        if not text:
            return text
        lines = text.split("\n")
        while lines and lines[0].strip() in ("^C", ""):
            lines.pop(0)
        return "\n".join(lines).strip()

    @staticmethod
    def _strip_trailing_prompt(text: str) -> str:
        """剥离末尾残留的真实shell提示符行（中断/恢复路径专用）。

        正常完成路径的cmd_output由哨兵截断，不含真实提示符；
        恢复路径的输出含Ctrl+C后shell打印的真实提示符（user@host:path$ 形态），
        与后续追加的合成提示符重复，需剥离避免双提示符。
        """
        if not text:
            return text
        lines = text.split("\n")
        while lines and SSHSession.PROMPT_LINE_RE.match(lines[-1]):
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def _strip_echo(raw: str, drop_echo_first_line: bool = True) -> str:
        """剥离PTY命令回显(第一行)。

        drop_echo_first_line:
          True  - 跳过第一行（默认，用于未过滤的原始输出如超时路径，首行是命令回显）
          False - 不跳第一行（用于已过滤命令回显的路径，首行是真实输出）

        注: 续行回显("> "提示)已在_parse_output的过滤步骤按"\x1b[?2004h>"特征剥离，
        此处不做续行判断，避免误删真实输出中"> "开头的行。
        """
        lines = raw.split("\n")
        if not lines:
            return raw

        start = 1 if drop_echo_first_line else 0
        return "\n".join(lines[start:])

    @staticmethod
    def _clean_output(raw: str) -> str:
        """清洗ANSI转义码、回车覆盖、内部标记(PTY噪声)"""
        cleaned = _ansi_sub(raw)

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

        # 注: 续行提示符("> ")的剥离已在_parse_output过滤步骤按"\x1b[?2004h>"特征处理，
        # 此处不再按行首"> "删——否则会误删真实输出中以"> "开头的行（如 echo '> quote'）

        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()
