"""串口会话 —— 单个串口连接的封装

核心设计:
- 后台 reader 线程持续从串口读取数据到共享 buffer
- 提示符检测: 字符集匹配 + 稳定性采样(3次×100ms)
- 超时兜底: 默认 60s, 超时返回已收集数据
- 纯管道原则: AI 发什么就发什么, 不做翻译/注入
"""

import codecs
import re
import time
import threading
from typing import Optional

import serial


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
    PROMPT_RE = re.compile(r"[\])$#%>:❯=@~]\s*$")

    # 串口读取超时 (reader 线程阻塞上限)
    READ_TIMEOUT = 0.1

    # buffer 上限: 超出后丢弃最早一半, 防止设备失控导致 OOM
    BUFFER_MAX_CHARS = 1_000_000  # 1MB

    def __init__(self, port: str, baudrate: int = 115200,
                 databits: int = 8, parity: str = "N",
                 stopbits: float = 1, flow_control: str = "none",
                 line_ending: str = "\n", prompt_pattern: str = ""):
        self.port = port
        self.baudrate = baudrate
        self.line_ending = line_ending

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

        # 流式 UTF-8 解码器: 处理跨 read 块的多字节字符
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

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

    # ── 公开方法 ──

    def _ensure_ready(self) -> Optional[str]:
        """检查会话是否可用，返回错误信息或 None"""
        if self._dead or not self._ser.is_open:
            self._dead = True
            return f"[错误: 串口 {self.port} 已断开]"
        if self._busy:
            return "[上一个命令尚未完成，此串口暂不可用]"
        return None

    def execute(self, command: str, timeout: int = 120,
                max_output_chars: int = 8000) -> str:
        """发送命令，等待提示符或超时，返回输出"""
        err = self._ensure_ready()
        if err:
            return err

        self._busy = True
        try:
            return self._do_send(command, timeout, max_output_chars)
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
                    max_output_chars: int = 8000) -> str:
        """发送命令，纯超时返回，不做提示符检测。

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
            return self._do_raw_send(command, timeout, max_output_chars)
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
                        # 背压: buffer 超限时丢弃最早一半
                        if len(self._buffer) > SerialSession.BUFFER_MAX_CHARS:
                            keep = SerialSession.BUFFER_MAX_CHARS // 2
                            self._buffer = (
                                f"...[背压截断: 丢弃前{len(self._buffer) - keep}字符]\n"
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
                 max_output_chars: int) -> str:
        """核心: 发送文本, 等待提示符, 返回输出"""
        self._interrupt.clear()

        # 排空残留
        with self._lock:
            self._buffer = ""

        # 发送
        try:
            payload = text + self.line_ending
            self._ser.write(payload.encode("utf-8", errors="replace"))
            self._ser.flush()
        except serial.SerialException as e:
            self._dead = True
            return f"[错误: 串口写入失败: {e}]"

        # 等待提示符或超时
        output, found = self._wait_for_prompt(timeout)

        if found:
            return _truncate_output(
                self._clean_output(output), max_output_chars
            )

        # 超时/中断
        interrupted = self._interrupt.is_set()
        tag = "[用户中断]" if interrupted else f"[超时: 命令执行超过{timeout}秒]"
        cleaned = self._clean_output(output)
        if cleaned:
            return _truncate_output(f"{cleaned}\n{tag}", max_output_chars)
        return tag

    def _do_raw_send(self, text: str, timeout: int,
                     max_output_chars: int) -> str:
        """核心: 发送文本, 纯超时等待, 不做提示符检测"""
        self._interrupt.clear()

        with self._lock:
            self._buffer = ""

        try:
            payload = text + self.line_ending
            self._ser.write(payload.encode("utf-8", errors="replace"))
            self._ser.flush()
        except serial.SerialException as e:
            self._dead = True
            return f"[错误: 串口写入失败: {e}]"

        # 纯超时等待
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                break
            if self._interrupt.is_set():
                break
            remaining = timeout - elapsed
            with self._cond:
                self._cond.wait(timeout=min(0.1, remaining))

        with self._lock:
            output = self._buffer

        interrupted = self._interrupt.is_set()
        tag = "[用户中断]" if interrupted else f"[超时: 命令执行超过{timeout}秒]"
        cleaned = self._clean_output(output)
        result = f"{cleaned}\n{tag}" if cleaned else tag
        return _truncate_output(result, max_output_chars)

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
            return f"[错误: 串口写入失败: {e}]"

        output, found = self._wait_for_prompt(timeout)
        if found:
            return _truncate_output(self._clean_output(output), max_output_chars)

        cleaned = self._clean_output(output)
        if cleaned:
            return _truncate_output(f"{cleaned}\n[已发送Ctrl+C]", max_output_chars)
        return "[已发送Ctrl+C]"

    def _wait_for_prompt(self, timeout: int) -> tuple:
        """等待提示符出现, 返回 (output, found)

        稳定性检查仅在命令发送后至少 SerialSession.MIN_EXEC_TIME 秒才开始，
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

            # 提示符检测 + 稳定性校验（需满足最小执行时间）
            if elapsed >= SerialSession.MIN_EXEC_TIME and self._is_at_prompt(current):
                if self._check_stability():
                    with self._lock:
                        result = self._buffer
                    return result, True

            remaining = timeout - elapsed
            with self._cond:
                self._cond.wait(timeout=min(poll_interval, remaining))

            poll_interval = min(poll_interval * SerialSession.POLL_MULTIPLIER, SerialSession.POLL_MAX)

        with self._lock:
            result = self._buffer
        return result, False

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
