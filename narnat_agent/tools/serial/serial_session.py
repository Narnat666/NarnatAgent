"""串口会话 —— 单个串口连接的封装

核心设计:
- 后台 reader 线程持续从串口读取数据到共享 buffer
- 提示符检测: 字符集匹配 + 稳定性采样(3次×100ms)
- wait_for: 指定正则命中即返回(exec/raw_exec)，非法正则降级按字面量匹配
- 文本编码可配(默认utf-8，GBK设备可设gbk)，解码失败在返回中提示
- DTR/RTS 信号线手动控制(high/low/pulse，复位场景)
- 超时兜底: 默认 120s, 超时返回已收集数据
- 纯管道原则: AI 发什么就发什么, 不做翻译/注入
"""

import codecs
import difflib
import re
import time
import threading
from typing import Optional

import serial

from ..exec_signal import error_line, tag_error


def _merge_cr_line(line: str) -> str:
    """合并单行内的 \r 覆盖：后段覆盖前段（模拟终端行为）"""
    if "\r" not in line:
        return line
    segments = line.split("\r")
    result = ""
    for seg in segments:
        if len(seg) >= len(result):
            result = seg
        else:
            result = seg + result[len(seg):]
    return result


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部常含设备提示符等关键状态），中段提示"""
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


# ═══════════════════════════════════════════════════════════════
# SerialSession
# ═══════════════════════════════════════════════════════════════

class SerialSession:
    """一个串口交互式会话"""

    # ── 会话参数常量（原模块级常量收敛于此）──
    # 稳定性采样: 连续 N 次 buffer 不变 → 确认在提示符
    # 最小执行时间: 命令执行至少等待此时间后才开始稳定性检查，避免
    # 输出中偶然出现的提示符字符在设备输出短暂停顿时触发误判
    MIN_EXEC_TIME = 0.5  # 500ms
    STABILITY_SAMPLES = 3
    STABILITY_INTERVAL = 0.1  # 100ms

    # 轮询参数: 自适应增长, 避免 CPU 空转
    POLL_INITIAL = 0.05
    POLL_MAX = 0.3
    POLL_MULTIPLIER = 1.2

    # ANSI 清洗正则
    ANSI_RE = re.compile(
        r"\x1b\[\??[0-9;]*[a-zA-Z]"
        r"|\x1b\].*?(?:\x07|\x1b\\)"
        r"|\x1b[()][A-Za-z0-9]"
        r"|\x1b[0-9:;<=>?@[A-Z\[\]^_`]"
    )

    # 提示符检测: 以提示符字符结尾, 后跟可选空白
    # (=/@ 更常见于数据内容行尾(如 "x = 1"、"user@host"), 保留会导致提示符误判、命令提前返回)
    PROMPT_RE = re.compile(r"[\])$#%>:❯~]\s*$")

    # 串口读取超时 (reader 线程阻塞上限)
    READ_TIMEOUT = 0.1

    # buffer 上限: 超出后丢弃最早一半, 防止设备失控导致 OOM
    BUFFER_MAX_CHARS = 1_000_000  # 1MB

    def __init__(self, port: str, baudrate: int = 115200,
                 databits: int = 8, parity: str = "N",
                 stopbits: float = 1, flow_control: str = "none",
                 line_ending: str = "\n", prompt_pattern: str = "",
                 encoding: str = "utf-8"):
        self.port = port
        self.baudrate = baudrate
        self.line_ending = line_ending

        # 文本编码(GBK设备等): 非法编码名回退utf-8, 提示暂存供 connect 返回
        self.encoding = (encoding or "utf-8").strip() or "utf-8"
        self.encoding_fallback = ""
        try:
            codecs.lookup(self.encoding)
        except LookupError:
            self.encoding_fallback = f"[提示: 编码名 '{self.encoding}' 无效，已回退 utf-8]"
            self.encoding = "utf-8"

        # 提示符正则: 自定义覆盖内置
        self._prompt_re = SerialSession.PROMPT_RE
        if prompt_pattern:
            try:
                self._prompt_re = re.compile(prompt_pattern)
            except re.error as e:
                raise ValueError(f"无效的 prompt_pattern 正则: {e}")

        # 参数映射
        _bytesize_map = {5: serial.FIVEBITS, 6: serial.SIXBITS,
                         7: serial.SEVENBITS, 8: serial.EIGHTBITS}
        _parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
                       "O": serial.PARITY_ODD, "M": serial.PARITY_MARK,
                       "S": serial.PARITY_SPACE}
        _stopbits_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO,
                         1.5: serial.STOPBITS_ONE_POINT_FIVE}

        self._ser = serial.Serial()
        self._ser.port = port
        self._ser.baudrate = baudrate
        self._ser.bytesize = _bytesize_map.get(databits, serial.EIGHTBITS)
        self._ser.parity = _parity_map.get(parity.upper(), serial.PARITY_NONE)
        self._ser.stopbits = _stopbits_map.get(stopbits, serial.STOPBITS_ONE)
        self._ser.xonxoff = (flow_control == "software")
        self._ser.rtscts = (flow_control == "hardware")
        self._ser.timeout = SerialSession.READ_TIMEOUT

        self._ser.open()

        self._busy = False
        self._interrupt = threading.Event()
        self._dead = False

        # 共享 buffer + Condition
        self._buffer = ""
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._reader_alive = True

        # 背压累计丢弃字符数（_reader_loop 累加，exec/status 返回时提示）
        self._dropped_chars = 0

        # 本次调用内产生的正则降级提示（由 _wait_for_prompt 写入，_append_hints 读取）
        self._regex_degrade_hint = ""

        # 流式解码器: 处理跨 read 块的多字节字符
        self._decoder = codecs.getincrementaldecoder(self.encoding)("replace")

        # 后台 reader 线程
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

        # 消化初始输出（boot 信息、login 提示等）
        self.initial_output = self._drain_initial()

    # ── 公开属性 ──

    @property
    def prompt_info(self) -> str:
        """会话摘要"""
        return f"{self.port} @{self.baudrate}"

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def is_alive(self) -> bool:
        return not self._dead and self._ser.is_open

    @property
    def dropped_chars(self) -> int:
        """背压累计丢弃字符数（>0 表示设备输出曾超过 buffer 上限）"""
        return self._dropped_chars

    # ── 公开方法 ──

    def _ensure_ready(self) -> Optional[str]:
        """检查会话是否可用，返回错误信息或 None"""
        if self._dead or not self._ser.is_open:
            self._dead = True
            return error_line(f"串口 {self.port} 已断开")
        if self._busy:
            return "[上一个命令尚未完成，此串口暂不可用]"
        return None

    def execute(self, command: str, timeout: int = 120,
                max_output_chars: int = 8000, wait_for: str = "") -> str:
        """发送命令，等待 wait_for 命中/提示符或超时，返回输出"""
        err = self._ensure_ready()
        if err:
            return err

        self._busy = True
        try:
            return self._do_send(command, timeout, max_output_chars, wait_for)
        finally:
            self._busy = False

    def send_input(self, text: str, timeout: int = 120,
                   max_output_chars: int = 8000) -> str:
        """发送交互输入（密码、y/n 等），等待提示符或超时。

        语义（与 Terminal 的 input 对齐）:
        - text = "^C" 或 "\\x03" → 发送原始 Ctrl+C 字节，中断设备上仍在运行的命令
        - 其他文本 → 追加行结束符发送，等同于 execute
        """
        if text == "^C" or text == "\x03":
            err = self._ensure_ready()
            if err:
                return err
            self._busy = True
            try:
                return self._send_ctrl_c(timeout, max_output_chars)
            finally:
                self._busy = False
        return self.execute(text, timeout, max_output_chars)

    def raw_execute(self, command: str, timeout: int = 120,
                    max_output_chars: int = 8000, wait_for: str = "") -> str:
        """发送命令，纯超时返回，不做提示符检测；wait_for 命中提前返回。

        适用场景:
        - 设备无标准提示符（裸机串口、AT 固件、bootloader 启动日志）
        - 输出中含大量提示符字符导致误判
        - exec 提示符检测误判时可切换到此模式
        """
        err = self._ensure_ready()
        if err:
            return err

        self._busy = True
        try:
            return self._do_raw_send(command, timeout, max_output_chars, wait_for)
        finally:
            self._busy = False

    def kill_active(self):
        """ESC 打断: 设中断标志 + 向设备发 Ctrl+C"""
        self._interrupt.set()
        with self._cond:
            self._cond.notify_all()
        # 向设备发送 Ctrl+C，终止正在运行的进程
        try:
            self._ser.write(b"\x03")
            self._ser.flush()
        except Exception:
            pass

    def set_signals(self, dtr: str = "", rts: str = "", pulse_ms: int = 100) -> str:
        """设置 DTR/RTS 信号线（high/low/pulse；pulse=取反-等待-恢复原值）。

        典型用法: ESP32 进 bootloader = DTR/RTS 组合时序，由调用方组合。
        不设 busy 门槛: 复位时序可能需要在命令卡住时下发。
        """
        if self._dead or not self._ser.is_open:
            self._dead = True
            return error_line(f"串口 {self.port} 已断开")
        try:
            pulse_ms = max(1, min(int(pulse_ms), 5000))
        except (TypeError, ValueError):
            pulse_ms = 100

        parts = []
        for name, val in (("DTR", dtr), ("RTS", rts)):
            val = (val or "").strip().lower()
            if not val:
                continue
            try:
                orig = bool(getattr(self._ser, name.lower()))
                if val == "pulse":
                    setattr(self._ser, name.lower(), not orig)
                    time.sleep(pulse_ms / 1000.0)
                    setattr(self._ser, name.lower(), orig)
                    parts.append(f"{name}=pulse {pulse_ms}ms")
                else:
                    target = (val == "high")
                    setattr(self._ser, name.lower(), target)
                    parts.append(
                        f"{name}={'high' if target else 'low'}(原{'high' if orig else 'low'})"
                    )
            except Exception as e:
                return error_line(f"{name} 信号线操作失败: {e}")
        return "[信号线: " + " ".join(parts) + "]"

    def close(self):
        """关闭会话"""
        self._reader_alive = False
        self._interrupt.set()
        with self._cond:
            self._cond.notify_all()
        try:
            self._ser.close()
        except Exception:
            pass
        if self._reader.is_alive():
            try:
                self._reader.join(timeout=1.0)
            except RuntimeError:
                pass

    # ── 内部实现 ──

    def _reader_loop(self):
        """后台线程: 持续从串口读取数据到 buffer"""
        while self._reader_alive:
            try:
                data = self._ser.read(4096)
                if data:
                    text = self._decoder.decode(data, final=False)
                    with self._cond:
                        self._buffer += text
                        # 背压: buffer 超限时丢弃最早一半（累计丢弃数可见化）
                        if len(self._buffer) > SerialSession.BUFFER_MAX_CHARS:
                            keep = SerialSession.BUFFER_MAX_CHARS // 2
                            self._dropped_chars += len(self._buffer) - keep
                            self._buffer = (
                                f"...[背压截断: 累计丢弃{self._dropped_chars}字符]\n"
                                + self._buffer[-keep:]
                            )
                        self._cond.notify_all()
            except Exception:
                self._dead = True
                break

    def _drain_initial(self, max_wait: float = 10.0, stable_time: float = 0.5) -> str:
        """消化连接后的初始输出。

        等待串口连续 stable_time 秒无新数据后返回，上限 max_wait 秒。
        比固定超时更可靠：嵌入式 Linux boot 日志可能超过 3s，但最终会停下来。
        """
        collected = ""
        last_data_time = time.time()
        deadline = time.time() + max_wait

        while time.time() < deadline:
            if self._interrupt.is_set():
                break

            with self._cond:
                if self._buffer:
                    collected += self._buffer
                    self._buffer = ""
                    last_data_time = time.time()
                    # 有新数据，继续等待
                    self._cond.wait(timeout=0.1)
                    continue

                # buffer 为空，检查是否已稳定足够久
                if collected and (time.time() - last_data_time) >= stable_time:
                    break

                remaining = min(0.1, deadline - time.time())
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

        # 收取最后残留
        with self._lock:
            collected += self._buffer
            self._buffer = ""
        return self._clean_output(collected) if collected else ""

    def _do_send(self, text: str, timeout: int,
                 max_output_chars: int, wait_for: str = "") -> str:
        """核心: 发送文本, 等待 wait_for 命中/提示符, 返回输出

        停止原因: wait_for 命中 / 检测到提示符 / 超时 / 用户中断。
        """
        self._interrupt.clear()

        # 排空残留
        with self._lock:
            self._buffer = ""

        wf_re, wf_hint = self._compile_wait_for(wait_for)
        self._regex_degrade_hint = ""

        # 发送
        try:
            payload = text + self.line_ending
            self._ser.write(payload.encode(self.encoding, errors="replace"))
            self._ser.flush()
        except serial.SerialException as e:
            self._dead = True
            return error_line(f"串口写入失败: {e}")

        # 等待 wait_for/提示符或超时
        output, found, stop_reason = self._wait_for_prompt(timeout, wf_re, text)

        if found:
            result = _truncate_output(
                self._polish_output(output, text), max_output_chars
            )
            if stop_reason == "wait_for":
                result += "\n[停止原因: wait_for 命中]"
            else:
                result += "\n[停止原因: 检测到提示符]"
        else:
            # 超时/中断
            interrupted = self._interrupt.is_set()
            tag = "[用户中断]" if interrupted else tag_error(f"[超时: 命令执行超过{timeout}秒]")
            cleaned = self._polish_output(output, text)
            if cleaned:
                result = _truncate_output(f"{cleaned}\n{tag}", max_output_chars)
            else:
                result = tag

        return self._append_hints(result, output, wf_hint)

    def _do_raw_send(self, text: str, timeout: int,
                     max_output_chars: int, wait_for: str = "") -> str:
        """核心: 发送文本, 纯超时等待, 不做提示符检测；wait_for 命中提前返回。

        text 为空 → 纯监听模式: 跳过发送，仅在 timeout 内被动收集设备输出。
        """
        self._interrupt.clear()

        with self._lock:
            self._buffer = ""

        wf_re, wf_hint = self._compile_wait_for(wait_for)
        self._regex_degrade_hint = ""

        if text:
            try:
                payload = text + self.line_ending
                self._ser.write(payload.encode(self.encoding, errors="replace"))
                self._ser.flush()
            except serial.SerialException as e:
                self._dead = True
                return error_line(f"串口写入失败: {e}")

        # 纯超时等待（wait_for 命中提前返回）
        start = time.time()
        wf_found = False
        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                break
            if self._interrupt.is_set():
                break
            if wf_re is not None:
                with self._lock:
                    current = self._buffer
                region = self._strip_command_echo(current, text) if text else current
                if wf_re.search(region):
                    wf_found = True
                    break
            remaining = timeout - elapsed
            with self._cond:
                self._cond.wait(timeout=min(0.1, remaining))

        with self._lock:
            output = self._buffer

        interrupted = self._interrupt.is_set()
        cleaned = self._polish_output(output, text)
        if wf_found:
            tag = "[停止原因: wait_for 命中]"
        elif text:
            # raw_exec 的语义就是"纯超时返回"：超时是设计内的工作方式，不是失败，
            # 故不加错误标签（与 exec 超时区分：后者是命令未在预期内完成）
            tag = "[用户中断]" if interrupted else f"[超时: 命令执行超过{timeout}秒]"
        else:
            # 纯监听模式：区分有无数据，避免"有输出却说无输出"的矛盾文案
            if interrupted:
                tag = "[用户中断]"
            elif cleaned:
                tag = f"[监听结束: 已达{timeout}秒]"
            else:
                tag = f"[监听结束: {timeout}秒内无输出]"
        result = f"{cleaned}\n{tag}" if cleaned else tag
        result = _truncate_output(result, max_output_chars)
        return self._append_hints(result, output, wf_hint)

    def _send_ctrl_c(self, timeout: int, max_output_chars: int) -> str:
        """发送原始 Ctrl+C 字节（不追加行结束符），等待设备回到提示符或超时。

        与 Terminal 的 input=^C 语义对齐：AI 的习惯是在命令超时后用 ^C
        中断设备上仍在运行的命令。发送后等待提示符重新出现。
        """
        self._interrupt.clear()

        # 排空残留（Ctrl+C 前的旧输出不混入结果）
        with self._lock:
            self._buffer = ""

        try:
            self._ser.write(b"\x03")
            self._ser.flush()
        except serial.SerialException as e:
            self._dead = True
            return error_line(f"串口写入失败: {e}")

        output, found, _reason = self._wait_for_prompt(timeout)
        if found:
            return _truncate_output(self._clean_output(output), max_output_chars)

        cleaned = self._clean_output(output)
        if cleaned:
            return _truncate_output(f"{cleaned}\n[已发送Ctrl+C]", max_output_chars)
        return "[已发送Ctrl+C]"

    def _wait_for_prompt(self, timeout: int, wait_for_re=None, sent_text: str = "") -> tuple:
        """等待 wait_for 命中或提示符出现, 返回 (output, found, stop_reason)

        - wait_for_re 非空时优先检测: 剥离命令回显后匹配，命中即返回
          （不做稳定性采样——命中即返回是 wait_for 的语义）
        - wait_for 与提示符检测同时启用时，先命中者胜
        - 稳定性检查仅在命令发送后至少 SerialSession.MIN_EXEC_TIME 秒才开始，
          避免输出中偶然出现的提示符字符在短暂停顿时触发误判。
        """
        start = time.time()
        poll_interval = SerialSession.POLL_INITIAL

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                break
            if self._interrupt.is_set():
                break

            with self._lock:
                current = self._buffer

            # wait_for 命中判定（剥离命令回显行，避免回显误命中；超时保护见 _safe_search）
            if wait_for_re is not None:
                region = self._strip_command_echo(current, sent_text) if sent_text else current
                matched, deg_hint = self._safe_search(wait_for_re, region)
                if deg_hint:
                    self._regex_degrade_hint = deg_hint
                if matched:
                    with self._lock:
                        result = self._buffer
                    return result, True, "wait_for"

            # 提示符检测 + 稳定性校验（需满足最小执行时间）
            if elapsed >= SerialSession.MIN_EXEC_TIME and self._is_at_prompt(current):
                if self._check_stability():
                    with self._lock:
                        result = self._buffer
                    return result, True, "prompt"

            remaining = timeout - elapsed
            with self._cond:
                self._cond.wait(timeout=min(poll_interval, remaining))

            poll_interval = min(poll_interval * SerialSession.POLL_MULTIPLIER, SerialSession.POLL_MAX)

        with self._lock:
            result = self._buffer
        return result, False, ""

    # 嵌套量词结构（ReDoS 灾难性回溯形态一）：括号内含贪婪量词，括号后紧跟量词。
    # 例：(a+)+$、(.+)*、(\d+){1,10}
    RE_NESTED_QUANT = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")
    # 量化的交替组（形态二）：提取括号内以 | 分隔的分支，供前缀重叠检测。
    # 例：(a|a)+、(a|aa)+、(\w|\w)+ —— 分支重叠导致同一文本有多种切分路径
    RE_ALT_GROUP = re.compile(r"\(([^()|]+(?:\|[^()|]+)+)\)\s*[+*{]")

    @staticmethod
    def _has_ambiguous_alternation(regex_text: str) -> bool:
        """检测"分支重叠的量化交替组"（ReDoS 形态二）。

        任一分支是另一分支的前缀（含完全相同）即视为重叠：
        (a|aa)+ 中 "a" 是 "aa" 前缀 → 同一串 a 有多种切分 → 指数级回溯。
        (a|b)+、(GET|POST) 这类无重叠的交替是安全的，不误伤。
        """
        for m in SerialSession.RE_ALT_GROUP.finditer(regex_text):
            branches = [b.strip() for b in m.group(1).split("|")]
            for i, a in enumerate(branches):
                for j, b in enumerate(branches):
                    if i != j and a and b and (a.startswith(b) or b.startswith(a)):
                        return True
        return False

    @staticmethod
    def _compile_wait_for(wait_for: str) -> tuple:
        """编译 wait_for 正则。非法正则 / 疑似 ReDoS 降级按字面量匹配（不报错、不拦截）。

        返回 (pattern_or_None, hint)。pattern 为 None 表示未启用 wait_for。
        ReDoS 检出两种形态：嵌套量词、分支重叠的量化交替组。
        （完全防护需多进程隔离，成本高于收益；静态检测覆盖已知形态）
        """
        wf = (wait_for or "").strip()
        if not wf:
            return None, ""
        try:
            if SerialSession.RE_NESTED_QUANT.search(wf):
                return (re.compile(re.escape(wf)),
                        "[提示: wait_for 含嵌套量词（可能灾难性回溯），已按字面量匹配]")
            if SerialSession._has_ambiguous_alternation(wf):
                return (re.compile(re.escape(wf)),
                        "[提示: wait_for 含重叠交替分支（可能灾难性回溯），已按字面量匹配]")
            return re.compile(wf), ""
        except re.error:
            return re.compile(re.escape(wf)), "[提示: wait_for 正则非法，已按字面量匹配]"

    def _safe_search(self, pattern, text: str) -> tuple:
        """wait_for 匹配（返回 (matched, degraded_hint)）。

        ReDoS 防护采用**静态检测**（见 _compile_wait_for 的 RE_NESTED_QUANT 与
        RE_AMBIGUOUS_ALT）：检出即降级字面量。不用线程超时——Python re 是 C 实现，
        灾难性回溯期间持有 GIL，工作线程 join(timeout) 同样会被阻塞（实测 33s），
        且卡死线程会永久占用 GIL，属负优化。
        """
        return bool(pattern.search(text)), ""

    def _append_hints(self, result: str, raw_output: str, wait_for_hint: str = "") -> str:
        """结果末尾追加提示区（wait_for 降级/编码乱码/背压丢弃），无提示时原样返回。

        提示加在截断之后：保证不被 max_output_chars 截掉。
        """
        hints = []
        if wait_for_hint:
            hints.append(wait_for_hint)
        if self._regex_degrade_hint:
            hints.append(self._regex_degrade_hint)
        n_bad = raw_output.count("\ufffd")
        if n_bad > 0:
            hints.append(
                f"[提示: 输出含{n_bad}个乱码字符，设备编码可能不是{self.encoding}，"
                f"可尝试 encoding=gbk]"
            )
        if self._dropped_chars > 0:
            hints.append(f"[提示: 本次会话累计丢弃{self._dropped_chars}字符（背压）]")
        if hints:
            return result + "\n" + "\n".join(hints)
        return result

    def _is_at_prompt(self, text: str) -> bool:
        """检查最后一行是否匹配提示符"""
        if not text:
            return False
        cleaned = SerialSession.ANSI_RE.sub("", text)
        # 取最后一行，合并 \r 覆盖（与 _clean_output 一致）
        lines = cleaned.split("\n")
        if not lines:
            return False
        last_line = _merge_cr_line(lines[-1].rstrip())
        return bool(self._prompt_re.search(last_line))

    def _check_stability(self) -> bool:
        """稳定性校验: 连续采样 buffer 不变

        全程持锁，通过 cond.wait 原子释放/重获。reader 写入 buffer 时
        notify_all 唤醒 wait，不会出现 notify 丢失窗口。
        """
        with self._cond:
            prev = self._buffer
            for _ in range(SerialSession.STABILITY_SAMPLES - 1):
                self._cond.wait(timeout=SerialSession.STABILITY_INTERVAL)
                curr = self._buffer
                if curr != prev:
                    return False
                prev = curr
        return True

    @staticmethod
    def _clean_output(raw: str) -> str:
        """清洗 ANSI 转义码、\\r 覆盖、多余空行"""
        cleaned = SerialSession.ANSI_RE.sub("", raw)
        # 先归一化 \\r\\n → \\n（CRLF 是行结束符，非覆盖符）
        cleaned = cleaned.replace("\r\n", "\n")
        merged = [_merge_cr_line(line) for line in cleaned.split("\n")]
        cleaned = "\n".join(merged)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _polish_output(self, raw: str, sent_text: str) -> str:
        """返回给AI前的统一打磨: 清洗ANSI + 剥离命令回显 + 提示符去重。

        命令回显: 串口设备几乎总回显输入，exec返回首行常是AI刚发的命令
        （零信息量）。错位回显（被设备输出覆盖/与上条命令交错）与命令仍有
        大量重合片段，用相似度而非精确匹配识别。
        - 相似/前缀匹配 → 剥掉首行
        - 不相似 → 保留（设备不回显时首行是真实输出，误删会丢数据）
        """
        cleaned = self._clean_output(raw)
        if not cleaned:
            return cleaned
        cleaned = self._strip_command_echo(cleaned, sent_text)
        cleaned = self._dedup_prompt_lines(cleaned)
        return cleaned.strip()

    @staticmethod
    def _strip_command_echo(output: str, command: str) -> str:
        """剥离输出首行的命令回显。"""
        if not output or not command:
            return output
        lines = output.split("\n")
        first = lines[0].rstrip()
        if not first:
            return output
        cmd = command.strip()
        if not cmd:
            return output
        # 相似度: 错位回显（中间片段被覆盖）仍有高重合
        ratio = difflib.SequenceMatcher(None, first, cmd).ratio()
        # 长命令部分回显（行宽截断）用公共前缀判定
        prefix_match = (len(cmd) > 20 and first.startswith(cmd[:20]))
        if ratio >= 0.6 or prefix_match:
            # 剥离后若为空则返回空串（仅回显、无真实输出）
            rest = "\n".join(lines[1:]).strip()
            return rest
        return output

    def _dedup_prompt_lines(self, output: str) -> str:
        """相邻重复的提示符行去重一行。

        串口raw模式常见: 设备提示符被返回两次（回显+真实），对AI纯噪音。
        仅对提示符行去重（普通输出重复可能是有意义的）。
        """
        if not output or "\n" not in output:
            return output
        lines = output.split("\n")
        result = [lines[0]]
        for line in lines[1:]:
            # rstrip 比较: 串口提示符行尾常有空格差异（回显带空格、真实无）
            if line.rstrip() == result[-1].rstrip() and self._prompt_re.search(line.rstrip()):
                continue
            result.append(line)
        return "\n".join(result)
