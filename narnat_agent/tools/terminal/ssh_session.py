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

钩子报告协议(命令完成自报, bash专有):
- connect后协商: 注入PROMPT_COMMAND，shell在每条命令结束、打印提示符前
  输出报告序列 ESC]NARNAT;<随机暗号>;<退出码>;<cwd>BEL
- 暗号每次连接随机生成，命令输出无法预知/伪造；报告自带退出码与cwd，
  完成判定从"启发式猜测"变为"确定性收报"
- 验证闭环: 注入后等报告，收不到（dash/busybox/cmd等无此钩子）自动降级
  旧哨兵协议，行为与无钩子时完全一致
- 仅本会话进程生效（环境变量），不写文件，断开即消失，不影响用户会话

输出解析(PTY基础设施，不是翻译):
- _strip_echo: 剥离PTY命令回显(不是AI命令的输出)
- _clean_output: 清洗ANSI码、内部标记、\\r覆盖(PTY噪声)
"""

import os
import re
import shlex
import time
import threading
import uuid
from typing import Optional

import paramiko
import socket

from ..exec_signal import rc_line, error_line


def _ansi_sub(text: str) -> str:
    """剥离ANSI转义序列"""
    return SSHSession.ANSI_RE.sub('', text)


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部含提示符，对AI判断shell状态至关重要），中段提示"""
    if max_chars <= 0:
        return error_line("max_output_chars需为正整数")
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

    # ── PS1(命令完成)检测 ──
    # 严格形态: user@host:path$/# (bash真实提示符)。PS1与"无尾随换行的命令
    # 输出"粘连时（printf 输出后bash直接把PS1拼在其后），以bash打印PS1前的
    # OSC窗口标题序列 \x1b]0;...\x07 为界切分提取（见_ps1_candidate）。
    PS1_STRICT_RE = re.compile(r'^[^\s@]+@[^\s:]+:[^\s]*[$#]\s*$')
    # 宽形态: 无user@host前缀的短PS1（busybox sh 的 "# "、root 裸 "# " 等）
    PS1_LOOSE_RE = re.compile(r'^.{0,32}?[$#]\s*$')
    # PS1候选后的静默确认窗口: 连续N次recv超时（每次≈channel timeout 0.5s）无新数据
    # 才确认"命令已完成"。输出流动中出现的伪提示符行（cat文件内容含 xxx@xxx:xx$ 等）
    # 会被后续chunk推翻；真实PS1后通道必静默。误确认的代价仅是多等一个排队哨兵。
    PS1_CONFIRM_TIMEOUTS = 2

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

        # 重连凭据
        self._reconnect_params = dict(
            host=host, username=username, port=port,
            key_path=key_path, password=password, timeout=timeout)

        # 重连互斥：并行文件操作（如两个Read同时打一台设备）可能同时发现断线，
        # 无锁会并发替换 self._client，先进入者在认证途中client被换掉 → AttributeError
        self._reconnect_lock = threading.Lock()

        # 提前创建中断标志：connect 阻塞期间 ESC 打断（kill_active_exec）会访问
        # session._interrupt，若迟至 connect 之后才创建会抛 AttributeError
        self._interrupt = threading.Event()

        self._open_channel(key_path, password, timeout)

        self._busy = False  # 通道是否被未完成的前台命令占用
        self._last_command = ""  # 最近执行的命令，供_parse_output剥离命令回显
        # tty 回显能力（由 _update_cwd 按实际输出判定）：
        # echo off 的 tty 下"零输出"不再是断连证据，见 _read_until_marker
        self._echo_enabled = True

        # 钩子报告协议状态（_try_enable_report_protocol协商后生效）
        self._hook_token = ""      # 本次协商的报告暗号（进程级随机，命令不可预知）
        self._hook_active = False  # True=报告协议生效；False=旧哨兵协议
        self._report_re: Optional[re.Pattern] = None  # 报告序列匹配（含暗号）

        # 待完成命令的哨兵（超时后input接管时复用）
        self._pending_marker = ""
        self._pending_pwd_marker = ""
        # 哨兵延迟注入标志: 哨兵命令是否已写入PTY（execute读循环/watcher/input共享，
        # 防重复发送）。False=等PS1出现后发送，True=已发送只等pwd_marker输出。
        self._sentinel_sent = False

        # 后台watcher控制 + 线程引用
        self._watcher_stop = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None
        # watcher退出原因: True=命令已完成(哨兵齐)，False=被停止接管(input)
        # input在join后据此区分"命令已完成"与"接管继续喂输入"，避免把
        # 等待中的交互输入误判为已完成而拒绝发送
        self._watcher_finished = False

        # 后台命令完成输出缓存（下次exec/input时返回给AI）
        self._backlog = ""
        self._backlog_lock = threading.Lock()

    def _open_channel(self, key_path: Optional[str], password: Optional[str],
                      timeout: int):
        """建立SSH client+交互channel"""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # timeout: TCP连接/SSH banner/认证的socket超时。黑洞IP无此参数会阻塞
        # 数十秒（OS默认TCP重试），AI连错IP时长时间无响应
        connect_kwargs = {
            "hostname": self.host, "port": self.port, "username": self.username,
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

    def reconnect(self):
        """断线重连：复用凭据，保留dev槽位与工作目录。失败抛异常由调用方处理。

        重连是阻塞操作（TCP 连接最长等满 connect timeout），调用方已把本会话
        注册为活跃会话，故 ESC(kill_active_exec) 能置位中断标志：标志在下面的
        检查点生效，重连尽早放弃而不是卡满超时。
        """
        with self._reconnect_lock:
            # 并发调用中已被其他线程重连成功，直接复用（避免重复建连互相破坏）
            if not self._channel.closed:
                return

            # 先清残留标志（旧命令的中断不应影响新连接）；此后按下的 ESC 在检查点生效
            self._interrupt.clear()

            # _initialize 会刷新 _cwd，先取原值
            old_cwd = self._cwd

            # 旧watcher须先退出，否则与新通道竞争recv
            self._watcher_stop.set()
            try:
                self._channel.close()
            except Exception:
                pass
            if self._watcher_thread is not None and self._watcher_thread.is_alive():
                self._watcher_thread.join(timeout=1.0)
            try:
                self._client.close()
            except Exception:
                pass

            self._busy = False
            self._last_command = ""
            self._pending_marker = ""
            self._pending_pwd_marker = ""
            self._sentinel_sent = False
            self._backlog = ""
            self._watcher_thread = None
            self._watcher_finished = False
            self._watcher_stop = threading.Event()

            p = self._reconnect_params
            self._open_channel(p.get("key_path"), p.get("password"), p.get("timeout", 15))
            self._initialize()
            # TCP连接阻塞期间无法中断，返回后 _initialize 的读循环会因中断标志立即退出，
            # 此处据此放弃本次重连（否则会继续恢复cwd，白等一段时间）
            if self._interrupt.is_set():
                self._abort_interrupted()

            # 恢复原工作目录（目录不存在则退回home）。
            # shlex.quote防路径含引号/空格/特殊字符时破坏shell语法
            if old_cwd and old_cwd not in ("~", "/"):
                try:
                    self.execute(f"cd {shlex.quote(old_cwd)} 2>/dev/null", timeout=10)
                except Exception:
                    pass

    def _abort_interrupted(self):
        """ESC 打断重连：关闭半开通道并抛异常（调用方按重连失败处理）。

        清掉中断标志：标志残留会让后续命令一发起就被判为"用户中断"。
        """
        self._interrupt.clear()
        try:
            self._channel.close()
        except Exception:
            pass
        raise RuntimeError("重连被用户中断（ESC）")

    def _initialize(self):
        """阻塞初始化：读初始输出、更新cwd。必须在 connect 中注册活跃执行会话之后调用，
        这样 ESC 打断 connect 时能通过 kill_active_exec() 关闭此会话。"""
        self._initial_output = self._read_until_prompt(timeout=5)
        self._update_cwd()
        # 协商钩子报告协议（命令完成自报）：验证闭环失败自动降级旧哨兵协议。
        # 注入命令自身完成时钩子即触发一次报告，同时顺带校准cwd。
        self._try_enable_report_protocol()

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

        # ── 哨兵延迟注入协议 ──
        # 只发送用户命令本体，哨兵不再作为后续行预置进PTY输入流。
        # 旧协议把哨兵行与命令一起发送，命令执行期间任何从stdin读取的程序
        # （read内置/sudo/cat）会抢先消费哨兵行，造成连锁缺陷：
        #   - read 拿到 "rc=$?;..." 文本 → 交互输入被污染（用户第一个read值恒为垃圾）
        #   - sudo 把哨兵行当第一次密码 → "Sorry, try again"（工具误判注入失败）
        #   - 哨兵被吃掉后永不作为命令执行 → 读循环永远等不到完成标记 → busy不收敛，
        #     后续input文本被空闲shell当作命令执行
        # 新协议: 命令完成后（检测到真实PS1提示符+静默确认窗口），再发送独立哨兵
        # 命令 rc=$?; ... 捕获退出码与cwd（提示符出现后$?仍保持用户命令的退出码）。
        # 哨兵此时才进入输入流，shell空闲，不可能被用户命令消费。
        self._sentinel_sent = False
        self._last_command = command  # 供_parse_output剥离多行命令首行回显
        self._channel.send(f"{command.rstrip(chr(10) + chr(13))}\n")

        result = self._read_until_marker(marker, pwd_marker, timeout=timeout)

        if backlog.strip():
            if result.startswith("[错误"):
                # 断线错误是当前命令的结果，置顶展示；后台完成输出附后。
                # 反之错误跟在"[后台命令已完成]"之后会被误读为后台输出的一部分
                result = f"{result}\n[后台命令已完成，输出如下]\n{backlog.strip()}"
            else:
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

        # join期间watcher可能已检测到命令完成（哨兵齐、busy已清）。此时再发送
        # 输入文本会被空闲shell当作命令执行（明文泄露或误执行），必须拒绝。
        # 注意区分"watcher因stop被停止"（接管场景，命令仍等待输入，必须继续发
        # 送）：用_watcher_finished标志精确判别，不能用busy（被停止时也被清）。
        if self._watcher_finished:
            parts = []
            backlog = self._drain_backlog()
            if backlog.strip():
                parts.append(f"[后台命令已完成，输出如下]\n{backlog.strip()}")
            parts.append("[命令已完成，输入内容未发送（避免被当作命令执行）。如需执行命令请用 exec]")
            return "\n".join(parts)

        # watcher已收集的输出先返回（输入前的输出）
        backlog = self._drain_backlog()

        self._interrupt.clear()

        if text == "^C" or text == "\x03":
            # ── 中断仍在运行的命令：发送原始Ctrl+C ──
            # 注意: bash收到SIGINT后放弃整行剩余命令，等shell提示符重新出现。
            self._channel.send("\x03")
            raw = self._read_until_interrupt_prompt(timeout=timeout)
            self._interrupt.clear()

            at_prompt = self._ps1_candidate(raw)

            if at_prompt:
                # 命令已被终止，shell回到提示符
                self._busy = False
                self._pending_marker = ""
                self._pending_pwd_marker = ""
                self._sentinel_sent = False
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

        # 等待原命令完成（复用exec时发送的哨兵）。
        # expect_echo=False: 密码输入场景 echo off，输入与其后命令都可能无输出，
        # 零输出不能作为断连判据（否则存活连接被误报"连接已中断"并清busy）
        result = self._read_until_marker(
            self._pending_marker, self._pending_pwd_marker, timeout=timeout,
            expect_echo=False,
        )
        if backlog.strip():
            if result.startswith("[错误"):
                # 与exec一致：断线错误置顶，输入前的输出附后
                result = f"{result}\n[输入前输出]\n{backlog.strip()}"
            else:
                result = f"[输入前输出]\n{backlog.strip()}\n{'-' * 30}\n{result}"
        return _truncate_output(result, max_output_chars)

    def _read_until_interrupt_prompt(self, timeout: float) -> str:
        """发送Ctrl+C后读取，直到shell提示符重新出现或超时。返回原始输出。"""
        output = ""
        deadline = time.time() + timeout if timeout > 0 else time.time() + 120
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
            # 严格PS1检测：命令输出中以$/#结尾的行（如cat文件内容）不再误判
            # 为"已回到提示符"，避免误报已中断。
            if self._ps1_candidate(output):
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
                # 仅匹配「以marker开头的独立行」：回显行 "echo __NARNAT_CWD_x__"
                # 先于真实输出行到达，子串匹配会误判（cwd解析拿到的是回显文本）
                if SSHSession._sentinel_line_present(output, marker):
                    break
            except socket.timeout:
                if self._interrupt.is_set():
                    break
                continue
            except Exception:
                break

        # tty 回显能力判定：存活连接的 PTY 会回显提交的命令行（_strip_echo 依赖此前提）。
        # 有输出却不见"pwd -P"回显 → 该 tty 处于 echo off（静默登录脚本/stty -echo）。
        # 仅在确实收到输出时更新，避免初始化无输出时误判
        echo_probe = _ansi_sub(output)
        if echo_probe.strip():
            self._echo_enabled = "pwd -P" in echo_probe

        # 解析: ... /actual/path\n __MARKER__\n prompt
        # marker所在行之前的一行就是pwd输出。
        # 按行索引定位marker行（split(marker)会停在回显行"echo __NARNAT_CWD_x__"
        # 的第一次出现处，漏掉真实的pwd输出行）
        lines_all = output.split("\n")
        marker_idx = None
        for i, line in enumerate(lines_all):
            if _ansi_sub(line).replace("\r", "").strip().startswith(marker):
                marker_idx = i
                break
        if marker_idx is not None:
            for line in reversed(lines_all[:marker_idx]):
                cleaned = self._clean_output(line).strip()
                # 修复运算符优先级: and 优先于 or，需要括号
                if cleaned and (not cleaned.startswith("echo ")) and (("/" in cleaned) or (cleaned == "/")):
                    self._cwd = cleaned
                    break

    # ── 钩子报告协议（命令完成自报）──
    # bash的PROMPT_COMMAND在每条命令结束、打印提示符前执行。注入后shell
    # 每次命令完成都输出报告序列 ESC]NARNAT;<暗号>;<退出码>;<cwd>BEL。
    # 暗号每连接随机：命令输出无法预知伪造；报告自带退出码与cwd，
    # 使完成判定从"启发式猜测"变为"确定性收报"。验证闭环失败（shell
    # 不支持/注入失败）自动保持旧哨兵协议，行为与无钩子时完全一致。

    def _try_enable_report_protocol(self) -> bool:
        """协商钩子报告协议：注入PROMPT_COMMAND并验证闭环。

        注入语句:
          PROMPT_COMMAND="__NARNAT_EC__=\\$?;${PROMPT_COMMAND:+$PROMPT_COMMAND;}"'printf "\\033]NARNAT;<token>;%s;%s\\007" "$__NARNAT_EC__" "$(pwd -P)";unset __NARNAT_EC__'
        - 退出码在旧PROMPT_COMMAND执行前抢先捕获到__NARNAT_EC__：报告中的rc
          恒为用户命令的退出码，不被设备既有PROMPT_COMMAND的退出码覆盖。
          \\$?转义使$?以字面存入PROMPT_COMMAND，在每条命令完成后展开捕获，
          而非注入时刻展开（注入时刻$?是注入命令自己的退出码，恒为0）
        - 已有PROMPT_COMMAND以";"拼接保留，不破坏设备既有配置
        - unset在报告打印后清理捕获变量，不泄漏进用户shell环境
        - token每次协商重新生成（重连后旧shell已死，新shell用新暗号）
        - 注入命令自身完成时钩子即触发一次报告 → 收到报告=验证通过
        - 5秒内未收到报告（dash/busybox/cmd等无此钩子或注入失败）→ 返回False降级

        仅本会话进程生效（环境变量），不写任何文件；会话断开即消失，
        不影响用户自己的SSH会话。ESC可打断（检查点退出）。
        """
        self._hook_active = False
        self._hook_token = uuid.uuid4().hex[:8]
        self._report_re = re.compile(
            r"\x1b\]NARNAT;" + self._hook_token + r";(-?\d+);(.*?)\x07"
        )
        inject = (
            'PROMPT_COMMAND="__NARNAT_EC__=\\$?;${PROMPT_COMMAND:+$PROMPT_COMMAND;}"'
            f"'printf \"\\033]NARNAT;{self._hook_token};%s;%s\\007\" \"$__NARNAT_EC__\" \"$(pwd -P)\";unset __NARNAT_EC__'"
        )
        self._channel.send(inject + "\n")

        raw = ""
        deadline = time.time() + 5
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
            raw += chunk
            rc, cwd, _ = self._parse_report(raw)
            if rc is not None or cwd is not None:
                self._hook_active = True
                if cwd:
                    self._cwd = cwd  # 注入命令完成即上报当前cwd，顺带校准
                return True
        return False

    def _parse_report(self, raw: str) -> tuple[Optional[int], Optional[str], Optional[int]]:
        """在raw尾部窗口搜索报告序列，返回(rc, cwd, 报告绝对起始索引)。

        报告序列仅在输出末尾出现（提示符打印前），扫描尾部窗口足够。
        未找到返回(None, None, None)。
        """
        if self._report_re is None:
            return None, None, None
        tail_start = max(0, len(raw) - SSHSession.MARKER_TAIL_WINDOW)
        m = self._report_re.search(raw[tail_start:])
        if not m:
            return None, None, None
        try:
            rc = int(m.group(1))
        except ValueError:
            rc = None
        cwd = m.group(2) or None
        return rc, cwd, tail_start + m.start()

    def _parse_report_output(self, raw: str, report_idx: int) -> str:
        """钩子协议下命令输出解析：报告序列之前的内容，剥离命令回显。

        输出结构: 命令回显 + 真实输出 + [报告序列] + [提示符(丢弃)]
        报告序列由PROMPT_COMMAND打印（非命令回显），报告之后的提示符字节
        由report_idx截断丢弃（残留通道字节由下次execute的_drain_stale_output排空）。
        """
        before = raw[:report_idx]
        first_line = (self._last_command or "").split("\n")[0].strip()
        filtered = []
        first_content_seen = False
        for l in before.split("\n"):
            # 续行回显: "\x1b[?2004h> "（多行命令/heredoc中间行，与_parse_output一致）
            if "\x1b[?2004h> " in l or "\x1b[?2004h>" in l:
                continue
            ansi_clean = _ansi_sub(l).replace("\r", "").strip()
            if not first_content_seen and ansi_clean:
                first_content_seen = True
                if first_line and ansi_clean == first_line:
                    continue  # 命令/输入首行回显
            filtered.append(l)
        return self._clean_output("\n".join(filtered))

    def _start_busy_watcher(self, marker: str, pwd_marker: str,
                            initial_output: str = "", ps1_suspect: bool = False):
        """超时后启动后台线程，持续读channel，等命令完成后自动清除busy标记。

        行为（哨兵延迟协议）:
        - 哨兵未发送: 等待PS1提示符（继承initial_output中的候选状态）→
          静默确认窗口 → 发送独立哨兵命令 → 等pwd_marker输出
        - 哨兵已发送: 直接等pwd_marker输出
        - 命令完成 → 更新cwd，完成输出存进backlog
        - input接管（_watcher_stop置位）→ 停止读取，已收集输出存进backlog
        - ESC中断/通道断开 → 停止
        无论如何退出都清除busy（finally保证），使终端状态可恢复。

        initial_output/ps1_suspect: 调用方（读循环）已读走的部分输出与PS1候选
        状态。PS1可能已被读循环消费（超时前一刻命令恰好完成），watcher重新读
        将永远等不到PS1；继承候选状态后，静默窗口计时即发哨兵。
        """
        self._watcher_stop.clear()

        def _watch():
            initial_len = len(initial_output or "")
            output = initial_output or ""
            suspect = ps1_suspect or (not self._sentinel_sent and self._ps1_candidate(output))
            silent = 0
            finished = False
            # 钩子协议完成状态（报告=确定完成；旧协议的哨兵/PS1判定不受影响）
            hook_finish = False
            hook_rc = None
            hook_cwd = None
            hook_idx = 0
            try:
                while not self._watcher_stop.is_set():
                    if self._interrupt.is_set():
                        break
                    try:
                        chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                    except socket.timeout:
                        # PS1候选静默确认: 连续超时无新数据 → 确认完成 → 补发哨兵
                        # （幂等：阶段2哨兵丢失时同样靠此重发自愈）
                        if suspect:
                            silent += 1
                            if silent >= SSHSession.PS1_CONFIRM_TIMEOUTS:
                                self._send_sentinel(marker, pwd_marker)
                                silent = 0
                        continue
                    except Exception:
                        break
                    if not chunk:
                        break  # channel关闭/EOF
                    output += chunk
                    if self._hook_active and not self._sentinel_sent:
                        # 钩子协议优先: 报告到达=命令确定完成（后台命令的退出码一并带回）
                        rc, cwd, idx = self._parse_report(output)
                        if rc is not None or cwd is not None:
                            finished = True
                            hook_finish = True
                            hook_rc, hook_cwd, hook_idx = rc, cwd, idx
                            break
                    if not self._sentinel_sent:
                        # 阶段1: 等PS1。新数据推翻或维持候选
                        if self._ps1_candidate(output):
                            suspect = True
                            silent = 0
                        else:
                            suspect = False
                            silent = 0
                    else:
                        # 阶段2: 等pwd_marker。哨兵行永远在输出末尾，仅扫描尾部
                        # 窗口：全量逐行正则清洗在大输出后台命令（如make几十MB）
                        # 下是O(n²) CPU开销
                        if SSHSession._sentinel_line_present(output[-SSHSession.MARKER_TAIL_WINDOW:], pwd_marker):
                            finished = True
                            break
                        # 哨兵丢失自愈: shell又回到PS1但哨兵输出未出现 → 哨兵被
                        # 交互程序（误确认场景下的read等）消费或丢失 → 静默确认后重发
                        if self._ps1_candidate(output):
                            suspect = True
                            silent = 0
                        else:
                            suspect = False
                            silent = 0
            finally:
                self._watcher_finished = finished
                # 只把initial_output之后的新输出入backlog：initial部分是调用方
                # （execute读循环）已返回给AI的输出，重复入库会造成输出重复
                def _incremental_body(raw: str, strip_sentinel_echo: bool) -> str:
                    if len(raw) <= initial_len:
                        return ""
                    seg = raw[initial_len:]
                    if strip_sentinel_echo:
                        # 剥离哨兵命令回显行与尾部PS1行（协议噪声，不是命令输出）。
                        # 哨兵回显行特征: 含 "rc=$?"（哨兵命令第一段）。不能只查
                        # marker文本: split(marker,1)[0]已把回显行截断在marker处，
                        # 截断后行内不再含marker文本。
                        lines = [l for l in seg.split("\n")
                                 if "rc=$?" not in l
                                 and marker not in l and pwd_marker not in l]
                        seg = self._strip_trailing_prompt("\n".join(lines))
                    return self._clean_output(seg).strip()

                if finished and hook_finish:
                    # 钩子协议完成: 报告前的输出入backlog，退出码随backlog带回
                    # （旧协议丢后台命令退出码，报告协议补齐此信息）
                    body = _incremental_body(output[:hook_idx], strip_sentinel_echo=False)
                    if hook_rc is not None:
                        body = f"{rc_line(hook_rc)}\n{body}" if body else f"{rc_line(hook_rc)}"
                    if body:
                        self._append_backlog(body)
                    if hook_cwd:
                        self._cwd = hook_cwd
                elif finished:
                    # 只保留哨兵前的命令输出（哨兵行/退出码/pwd输出是基础设施噪声）
                    body = _incremental_body(output.split(marker, 1)[0],
                                             strip_sentinel_echo=True)
                    if self._hook_active:
                        # 钩子激活时的哨兵路径(伪PS1误判提前发哨兵): 哨兵$?已被
                        # 钩子重置为0，报告序列携带真实退出码，与钩子分支一致补齐
                        rep_rc, _, _ = self._parse_report(output)
                        if rep_rc is not None:
                            body = f"{rc_line(rep_rc)}\n{body}" if body else f"{rc_line(rep_rc)}"
                    if body:
                        self._append_backlog(body)
                    cwd = self._extract_cwd(output, marker, pwd_marker)
                    if cwd:
                        self._cwd = cwd
                else:
                    body = _incremental_body(output, strip_sentinel_echo=False)
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
        """从输出中提取pwd（marker行与pwd_marker行之间的路径行），失败返回None

        两个哨兵行均按「行首匹配」定位：回显行含 "echo __NARNAT_PWD_x__" 文本
        （子串匹配会误停），且回显先于真实输出到达，必须从marker行之后找pwd_marker行。
        """
        lines = output.split("\n")
        marker_idx = None
        for i, line in enumerate(lines):
            if _ansi_sub(line).replace("\r", "").strip().startswith(marker):
                marker_idx = i
                break
        if marker_idx is None:
            return None
        for i in range(marker_idx + 1, len(lines)):
            if _ansi_sub(lines[i]).replace("\r", "").strip().startswith(pwd_marker):
                for j in range(marker_idx + 1, i):
                    cleaned = self._clean_output(lines[j]).strip()
                    if cleaned and (cleaned.startswith("/") or cleaned == "/"):
                        return cleaned
                break
        return None

    @staticmethod
    def _ps1_candidate(text: str) -> bool:
        """检测输出尾部是否疑似出现shell提示符（命令完成迹象，待静默确认）。

        PS1提取: bash打印PS1前会先输出OSC窗口标题序列（\x1b]0;...\x07）。
        - 独立PS1行: OSC+PS1在同一行，切分后得到纯PS1行
        - PS1与无尾随换行的命令输出粘连（printf后PS1拼在输出后）: OSC序列
          是PS1的起点标记，从其终止符\x07后截取得到纯PS1

        两级判定:
        1. 严格: 行首即 user@host:path$/# 形态（bash真实PS1）
        2. 宽:   最后一行很短且以 $/# 结尾（busybox sh 的裸 "# "/"~ $ " 等）
        PS2续行提示"> "不以$/#结尾，绝不会被误判为完成。
        """
        raw_tail = text[-4096:]
        last_line_raw = raw_tail.split("\n")[-1]
        osc_start = last_line_raw.rfind("\x1b]0;")
        if osc_start >= 0:
            osc_end = last_line_raw.rfind("\x07", osc_start)
            if osc_end > osc_start:
                # OSC标题之后是PS1本体（含可能的\a后残留）
                last_line_raw = last_line_raw[osc_end + 1:]
        last_line = _ansi_sub(last_line_raw).replace("\r", "").strip()
        if not last_line:
            return False
        if SSHSession.PS1_STRICT_RE.match(last_line):
            return True
        if SSHSession.PS1_LOOSE_RE.search(last_line):
            return True
        return False

    def _send_sentinel(self, marker: str, pwd_marker: str):
        """向shell发送独立哨兵命令（延迟注入）。

        调用前提: shell已回到PS1提示符（命令已完成/被中断）。
        此时 $? 仍保持用户命令的退出码（PS1打印不改$?，除非设备配置了
        PROMPT_COMMAND），哨兵行以独立命令身份执行，绝不被用户命令消费。
        """
        self._channel.send(f"rc=$?; printf '\\n'; echo {marker}$rc; pwd -P; echo {pwd_marker}\n")
        self._sentinel_sent = True

    def _read_until_marker(self, marker: str, pwd_marker: str, timeout: int = 0,
                           expect_echo: bool = True) -> str:
        """读取channel输出，直到读到pwd_marker。

        钩子报告协议叠加: _hook_active时优先检测报告序列（确定性完成），
        收到即完成；未收到则完全走旧哨兵协议（PS1启发式/静默窗口/哨兵补发），
        两条路径互不干扰，钩子失效自动降级。

        timeout:
          >0  - 等待指定秒数，超时返回已收集输出+超时提示
                （命令继续后台运行，终端标记为忙，AI可用input应答或^C中断）
          ≤0  - 兜底：上层调用保证传入正数
        expect_echo:
          True  - exec 路径：tty 有回显时提交的命令行必被回显，零输出可判定连接已断
                  （tty 处于 echo off 时零输出无区分度，由 _echo_enabled 排除）
          False - input 路径：echo off 时输入不回显、其后命令可长时间静默，
                  零输出无法与"连接已死"区分，不能据此判中断

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
        # 连接中断标志：EOF或套接字异常时置位（设备关机/重启）。
        # 与"命令超时"必须区分：超时是命令还在跑，中断是连接已死、结果未知
        conn_lost = False
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
        # PS1候选状态: 输出尾部疑似出现shell提示符(命令完成迹象)，待静默确认窗口
        ps1_suspect = False
        silent_timeouts = 0
        # 钩子报告协议状态: 报告到达即命令确定完成（暗号随机，命令输出不可伪造）
        hook_finish = False
        hook_rc: Optional[int] = None
        hook_cwd: Optional[str] = None
        hook_idx = 0

        while time.time() < deadline:
            # 中断检查：ESC打断时立即退出（数据路径中也检查，不只依赖timeout分支）
            if self._interrupt.is_set():
                break

            try:
                chunk = self._channel.recv(4096).decode("utf-8", errors="replace")
                # EOF检测：channel关闭/远端断开时recv返回空字节，必须立即退出
                if not chunk:
                    conn_lost = True
                    break
                output += chunk

                if not found and self._hook_active and not self._sentinel_sent:
                    # ── 钩子报告协议: 报告到达 = 命令确定完成 ──
                    # 哨兵在途时不采信报告：哨兵命令完成时钩子同样触发（rc恒为0，
                    # 非用户命令退出码），且其输出与报告可能同块到达导致劫持判定
                    # PROMPT_COMMAND在提示符打印前输出报告序列(暗号+退出码+cwd)。
                    # 收到即完成，无需PS1启发式/静默窗口/哨兵补发；报告永远在
                    # 输出末尾，仅扫描尾部窗口。假报告不可能（暗号随机不可预知）。
                    hook_rc, hook_cwd, hook_idx = self._parse_report(output)
                    if hook_rc is not None or hook_cwd is not None:
                        hook_finish = True
                        found = True
                        break

                if not found:
                    # 阶段1(哨兵未发送): 等待PS1提示符（命令完成迹象）
                    # 阶段2(哨兵已发送): 等待pwd_marker输出；若shell又出现PS1
                    # 而哨兵输出缺失 → 哨兵被交互程序消费/丢失 → 置候选待重发
                    if self._sentinel_sent:
                        # 哨兵行永远在输出末尾，仅扫描尾部窗口。仅匹配「以
                        # pwd_marker开头的独立行」：哨兵命令回显行
                        # （"rc=$?; ...echo __NARNAT_PWD_x__"）行首是rc=，不会误命中。
                        first_newline = output.find('\n')
                        if first_newline >= 0:
                            tail = output[max(first_newline + 1, len(output) - SSHSession.MARKER_TAIL_WINDOW):]
                            found = SSHSession._sentinel_line_present(tail, pwd_marker)
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
                    if not found:
                        # PS1候选评估（两阶段共用）：新数据可推翻伪候选
                        if self._ps1_candidate(output):
                            ps1_suspect = True
                            silent_timeouts = 0
                        else:
                            ps1_suspect = False
                            silent_timeouts = 0

            except socket.timeout:
                if self._interrupt.is_set() or found:
                    break
                # PS1候选静默确认: 连续N次recv超时无新数据 → 确认命令已完成，
                # 发送独立哨兵命令（幂等：阶段2哨兵丢失时同样靠此重发自愈）。
                # 真实PS1后通道必静默；伪提示符（输出内容中的"xxx@xxx:xx$"行）
                # 会被后续chunk推翻，误确认仅导致哨兵在shell队列中排队
                # （命令完成后才执行，解析结果仍正确）。
                if ps1_suspect:
                    silent_timeouts += 1
                    if silent_timeouts >= SSHSession.PS1_CONFIRM_TIMEOUTS:
                        self._send_sentinel(marker, pwd_marker)
                        silent_timeouts = 0
            except Exception:
                # 套接字异常（设备重启后RST/网络断）：连接已死
                conn_lost = True
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
                                self._start_busy_watcher(marker, pwd_marker,
                                                         initial_output=output[-4096:],
                                                         ps1_suspect=ps1_suspect)
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
                            self._start_busy_watcher(marker, pwd_marker,
                                                     initial_output=output[-4096:],
                                                     ps1_suspect=ps1_suspect)
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
                # 等待远程进程终止、shell恢复（可能输出哨兵，也可能只回到提示符）
                residual = self._try_read_residual(duration=3.0)
                if residual:
                    output += residual

                def _sentinel_in_output() -> bool:
                    first_nl = output.find('\n')
                    check_region = output[first_nl + 1:] if first_nl >= 0 else ""
                    return SSHSession._sentinel_line_present(check_region, pwd_marker)

                if self._hook_active and not self._sentinel_sent:
                    # 钩子协议优先: ^C后shell回到提示符，钩子即打印报告（rc=130等）
                    # 哨兵在途时报告rc不可信（见读循环处注释），走哨兵判定
                    hook_rc, hook_cwd, hook_idx = self._parse_report(output)
                    if hook_rc is not None or hook_cwd is not None:
                        hook_finish = True
                        found = True

                if not hook_finish:
                    if _sentinel_in_output():
                        found = True
                    elif not self._sentinel_sent and self._ps1_candidate(output):
                        # shell已回到提示符（哨兵尚未发送）：补发哨兵取退出码/cwd
                        self._send_sentinel(marker, pwd_marker)
                        extra = self._try_read_residual(duration=3.0)
                        if extra:
                            output += extra
                        if _sentinel_in_output():
                            found = True

                # Ctrl+C后报告/哨兵出现了 → 走正常解析(远程进程已被终止)
                if found:
                    self._busy = False
                    if hook_finish:
                        # 钩子协议解析：报告自带退出码与cwd
                        if hook_cwd:
                            self._cwd = hook_cwd
                        cmd_output = self._strip_caret_echo(self._strip_trailing_prompt(
                            self._parse_report_output(output, hook_idx)))
                        ec = f"{rc_line(hook_rc)}\n" if hook_rc is not None else ""
                        if cmd_output:
                            return f"{ec}{cmd_output}\n[用户中断]\n{self.prompt}"
                        return f"{ec}[用户中断]\n{self.prompt}"
                    cmd_output, cwd, exit_code = self._parse_output(output, marker, pwd_marker)
                    if self._hook_active:
                        # 同正常完成路径: ^C后钩子报告(rc=130等)先于哨兵回显出现，
                        # 优先采信报告rc/cwd，避免哨兵$?被钩子重置为0
                        rep_rc, rep_cwd, _ = self._parse_report(output)
                        if rep_rc is not None:
                            exit_code = rep_rc
                        if rep_cwd:
                            cwd = rep_cwd
                    if cwd:
                        self._cwd = cwd
                    cmd_output = self._strip_caret_echo(self._strip_trailing_prompt(cmd_output))
                    ec = f"{rc_line(exit_code)}\n" if exit_code is not None else ""
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

            if conn_lost or (expect_echo and not output and self._echo_enabled):
                # 连接中断，命令结果未知。两种情况：
                # - conn_lost: EOF/套接字异常，TCP已察觉
                # - 零输出: 连PTY命令回显都未收到。exec路径存活连接必有回显
                #   （_strip_echo依赖此前提），零输出说明连接实际已断，只是TCP
                #   尚未报错（设备刚关机时的形态）；echo off 的 tty 无回显，
                #   零输出无区分度（_echo_enabled=False），退回超时语义
                # 不能走超时分支——那会误报"仍在后台运行"并置busy
                # 关闭channel：EOF/套接字异常不一定置 closed，主动关闭让
                # _exec/_input 入口的断线检测立即生效，设备恢复后下次调用即重连；
                # 否则要等 keepalive 判死，期间每次重试都白等一个 timeout
                try:
                    self._channel.close()
                except Exception:
                    pass
                self._busy = False
                return (error_line("连接已中断（设备可能关机/重启或网络不通），命令结果未知")
                        + f"\n{self.prompt}")

            # ── 纯超时: 不杀进程，命令继续后台运行 ──
            # 终端标记为忙，后台watcher等命令完成后自动清除busy、缓存输出。
            # 把已读输出的尾部与PS1候选状态传给watcher：PS1可能已被本循环读走
            # （超时前一刻命令恰好完成），watcher重新读将永远等不到PS1，必须
            # 继承候选状态在静默窗口后补发哨兵。
            self._busy = True
            self._start_busy_watcher(marker, pwd_marker,
                                     initial_output=output[-4096:],
                                     ps1_suspect=ps1_suspect)
            cmd_output = self._parse_partial_output(output, marker)
            tag = (f"[超时: 命令执行超过{timeout}秒，仍在后台运行。"
                   f"可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]")
            if cmd_output:
                return f"{cmd_output}\n{tag}\n{self.prompt}"
            else:
                return f"{tag}\n{self.prompt}"

        # 正常解析
        self._busy = False
        if hook_finish:
            # 钩子协议解析：报告序列自带退出码与cwd，报告之前的内容即命令输出
            # （报告后的提示符字节留在通道，由下次execute的_drain_stale_output排空）
            if hook_cwd:
                self._cwd = hook_cwd
            cmd_output = self._parse_report_output(output, hook_idx)
            ec = f"{rc_line(hook_rc)}\n" if hook_rc is not None else ""
            if cmd_output:
                return f"{ec}{cmd_output}\n{self.prompt}"
            return f"{ec}{self.prompt}"
        cmd_output, cwd, exit_code = self._parse_output(output, marker, pwd_marker)
        if self._hook_active:
            # 哨兵路径+钩子激活(伪PS1误判提前发哨兵): 钩子PROMPT_COMMAND在哨兵
            # 执行前已运行，$?被重置为0，哨兵捕获的rc恒为0。真实报告序列位于
            # 哨兵回显之前（尾部窗口首匹配即真实报告），优先采信其rc/cwd；
            # 无报告=钩子已死，$?未被污染，保留哨兵解析值。
            rep_rc, rep_cwd, _ = self._parse_report(output)
            if rep_rc is not None:
                exit_code = rep_rc
            if rep_cwd:
                cwd = rep_cwd
        if cwd:
            self._cwd = cwd

        ec = f"{rc_line(exit_code)}\n" if exit_code is not None else ""
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
                # 哨兵命令行（exec回显或input路径的行首echo形态），含marker。
                # 特殊形态: 命令输出无尾随换行时（printf、cat无换行文件等），bash把
                # prompt直接粘在输出后，哨兵命令行回显又粘在prompt后，整行形如:
                #   \x1b[?2004l\r真实输出\x1b[?2004h\x1b]0;标题\x07...$ rc=$?; ...; echo MARKER
                # 此时真实输出在prompt起点(\x1b[?2004h / \x1b]0; OSC标题)之前，
                # 裁剪保留；纯回显行裁剪后为空，整行丢弃（原行为）。
                idx = l.rfind("\x1b[?2004h")
                if idx < 0:
                    idx = l.rfind("\x1b]0;")
                if idx >= 0:
                    real_part = l[:idx]
                    if _ansi_sub(real_part).replace("\r", "").strip():
                        filtered.append(real_part)
                continue
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

        # ── 哨兵延迟协议适配: 剥离命令完成后的真实提示符行 ──
        # 新协议输出结构: 真实输出 + PS1 + [哨兵命令回显] + marker行...
        # PS1有两种形态:
        # 1. 独立成行(user@host:path$) → 尾部行剥离
        # 2. 与无尾随换行的输出粘连(printf输出后PS1直接拼在其后) →
        #    行含OSC标题序列 \x1b]0;...\x07（bash打印PS1前的窗口标题），
        #    以该序列为界截断，保留之前的真实输出
        if before_marker:
            lines_out = before_marker.split("\n")
            if lines_out:
                last = lines_out[-1]
                idx = last.rfind("\x1b]0;")
                if idx >= 0:
                    lines_out[-1] = last[:idx]
                before_marker = "\n".join(lines_out)
            before_marker = self._strip_trailing_prompt(before_marker)

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
    def _sentinel_line_present(text: str, pwd_marker: str) -> bool:
        """检测哨兵真实输出行是否存在（排除回显行误命中）。

        哨兵真实输出行 = 以 __NARNAT_PWD_x__ 开头的独立行。
        回显行含 "echo __NARNAT_PWD_x__" 文本（或 heredoc 场景中哨兵行
        被当作heredoc内容回显），必须以「行首匹配」排除。
        heredoc等多行命令场景下，哨兵行回显先于真实输出行到达，
        子串匹配会误判命令已完成，导致真实哨兵输出未读齐。
        """
        for line in text.split("\n"):
            if _ansi_sub(line).replace("\r", "").strip().startswith(pwd_marker):
                return True
        return False

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
        匹配前先剥ANSI（带颜色码的PS1行尾有\x1b[0m，不剥无法匹配）。
        """
        if not text:
            return text
        lines = text.split("\n")
        while lines and SSHSession.PROMPT_LINE_RE.match(_ansi_sub(lines[-1]).replace("\r", "")):
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
        # 只剥首尾空行与行尾空白，保留行首空格：
        # echo '  leading-space' 等输出行首空格是真实数据，strip()会误删
        return cleaned.strip("\n").rstrip()
