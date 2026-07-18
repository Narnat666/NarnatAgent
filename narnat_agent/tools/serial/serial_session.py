"""串口会话 —— 单个串口连接的封装

核心设计:
- 后台 reader 线程持续从串口读取数据到共享 buffer
- 提示符检测: 字符集匹配 + 稳定性采样(3次×100ms)
- 超时兜底: 默认 60s, 超时返回已收集数据
- 纯管道原则: AI 发什么就发什么, 不做翻译/注入
"""

import codecs
import os
import re
import time
import threading
from typing import Optional

import serial


# ── 常量 ──

# 稳定性采样: 连续 N 次 buffer 不变 → 确认在提示符
# 最小执行时间: 命令执行至少等待此时间后才开始稳定性检查，避免
# 输出中偶然出现的提示符字符在设备输出短暂停顿时触发误判
_MIN_EXEC_TIME = 0.5  # 500ms
_STABILITY_SAMPLES = 3
_STABILITY_INTERVAL = 0.1  # 100ms

# 轮询参数: 自适应增长, 避免 CPU 空转
_POLL_INITIAL = 0.05
_POLL_MAX = 0.3
_POLL_MULTIPLIER = 1.2

# ANSI 清洗正则
_ANSI_RE = re.compile(
    r"\x1b\[\??[0-9;]*[a-zA-Z]"
    r"|\x1b\].*?(?:\x07|\x1b\\)"
    r"|\x1b[()][A-Za-z0-9]"
    r"|\x1b[0-9:;<=>?@[A-Z\[\]^_`]"
)

# 提示符检测: 以提示符字符结尾, 后跟可选空白
_PROMPT_RE = re.compile(r"[\])$#%>:❯=@~]\s*$")

# 串口读取超时 (reader 线程阻塞上限)
_READ_TIMEOUT = 0.1

# buffer 上限: 超出后丢弃最早一半, 防止设备失控导致 OOM
_BUFFER_MAX_CHARS = 1_000_000  # 1MB


# ── 工具函数 ──

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
    """截断输出到指定字符数"""
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n...[已截断: 输出共{len(text)}字符, 当前显示前{max_chars}字符]"
    )


# ═══════════════════════════════════════════════════════════════
# XMODEM-1K + CRC-16 协议实现
# 参考 tehmaze/xmodem (MIT), 精简为仅 1K 块 + CRC 模式
# ═══════════════════════════════════════════════════════════════

_SOH = b'\x01'
_STX = b'\x02'
_EOT = b'\x04'
_ACK = b'\x06'
_NAK = b'\x15'
_CAN = b'\x18'
_CRC = b'C'

# CRC-16 查表 (XMODEM 多项式 0x1021)
_CRCTABLE = [
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7,
    0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52b5, 0x4294, 0x72f7, 0x62d6,
    0x9339, 0x8318, 0xb37b, 0xa35a, 0xd3bd, 0xc39c, 0xf3ff, 0xe3de,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64e6, 0x74c7, 0x44a4, 0x5485,
    0xa56a, 0xb54b, 0x8528, 0x9509, 0xe5ee, 0xf5cf, 0xc5ac, 0xd58d,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76d7, 0x66f6, 0x5695, 0x46b4,
    0xb75b, 0xa77a, 0x9719, 0x8738, 0xf7df, 0xe7fe, 0xd79d, 0xc7bc,
    0x48c4, 0x58e5, 0x6886, 0x78a7, 0x0840, 0x1861, 0x2802, 0x3823,
    0xc9cc, 0xd9ed, 0xe98e, 0xf9af, 0x8948, 0x9969, 0xa90a, 0xb92b,
    0x5af5, 0x4ad4, 0x7ab7, 0x6a96, 0x1a71, 0x0a50, 0x3a33, 0x2a12,
    0xdbfd, 0xcbdc, 0xfbbf, 0xeb9e, 0x9b79, 0x8b58, 0xbb3b, 0xab1a,
    0x6ca6, 0x7c87, 0x4ce4, 0x5cc5, 0x2c22, 0x3c03, 0x0c60, 0x1c41,
    0xedae, 0xfd8f, 0xcdec, 0xddcd, 0xad2a, 0xbd0b, 0x8d68, 0x9d49,
    0x7e97, 0x6eb6, 0x5ed5, 0x4ef4, 0x3e13, 0x2e32, 0x1e51, 0x0e70,
    0xff9f, 0xefbe, 0xdfdd, 0xcffc, 0xbf1b, 0xaf3a, 0x9f59, 0x8f78,
    0x9188, 0x81a9, 0xb1ca, 0xa1eb, 0xd10c, 0xc12d, 0xf14e, 0xe16f,
    0x1080, 0x00a1, 0x30c2, 0x20e3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83b9, 0x9398, 0xa3fb, 0xb3da, 0xc33d, 0xd31c, 0xe37f, 0xf35e,
    0x02b1, 0x1290, 0x22f3, 0x32d2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xb5ea, 0xa5cb, 0x95a8, 0x8589, 0xf56e, 0xe54f, 0xd52c, 0xc50d,
    0x34e2, 0x24c3, 0x14a0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xa7db, 0xb7fa, 0x8799, 0x97b8, 0xe75f, 0xf77e, 0xc71d, 0xd73c,
    0x26d3, 0x36f2, 0x0691, 0x16b0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xd94c, 0xc96d, 0xf90e, 0xe92f, 0x99c8, 0x89e9, 0xb98a, 0xa9ab,
    0x5844, 0x4865, 0x7806, 0x6827, 0x18c0, 0x08e1, 0x3882, 0x28a3,
    0xcb7d, 0xdb5c, 0xeb3f, 0xfb1e, 0x8bf9, 0x9bd8, 0xabbb, 0xbb9a,
    0x4a75, 0x5a54, 0x6a37, 0x7a16, 0x0af1, 0x1ad0, 0x2ab3, 0x3a92,
    0xfd2e, 0xed0f, 0xdd6c, 0xcd4d, 0xbdaa, 0xad8b, 0x9de8, 0x8dc9,
    0x7c26, 0x6c07, 0x5c64, 0x4c45, 0x3ca2, 0x2c83, 0x1ce0, 0x0cc1,
    0xef1f, 0xff3e, 0xcf5d, 0xdf7c, 0xaf9b, 0xbfba, 0x8fd9, 0x9ff8,
    0x6e17, 0x7e36, 0x4e55, 0x5e74, 0x2e93, 0x3eb2, 0x0ed1, 0x1ef0,
]


def _calc_crc(data: bytes, crc: int = 0) -> int:
    """计算 CRC-16/XMODEM"""
    for byte in data:
        idx = ((crc >> 8) ^ byte) & 0xff
        crc = ((crc << 8) ^ _CRCTABLE[idx]) & 0xffff
    return crc


class _XMODEM:
    """XMODEM-1K + CRC-16 文件传输协议

    getc(size, timeout) → bytes | None
    putc(data, timeout) → int | None
    """

    def __init__(self, getc, putc):
        self._getc = getc
        self._putc = putc

    def abort(self, timeout=2):
        """发送 CAN CAN 中止传输"""
        self._putc(_CAN, timeout)
        self._putc(_CAN, timeout)

    def send(self, stream, retry=10, timeout=60):
        """发送 stream → 设备。返回 True 成功 / False 失败"""
        error_count = 0
        cancel = 0

        # 等待接收方发起握手: 'C' (CRC) 或 NAK
        while True:
            char = self._getc(1, 3)
            if char == _CRC:
                break  # CRC 模式
            elif char == _NAK:
                break  # checksum 回退 (不推荐但仍支持)
            elif char == _CAN:
                if cancel:
                    return False
                cancel = 1
            else:
                error_count += 1
                if error_count > (retry * 2):
                    self.abort(timeout)
                    return False

        # 逐块发送
        sequence = 1
        error_count = 0
        while True:
            data = stream.read(1024)
            if not data:
                break  # EOF

            # 填充到 1024 字节 (CPMEOF = 0x1a)
            data = data.ljust(1024, b'\x1a')

            # 构建块: STX + seq + ~seq + data + CRC16
            header = _STX + bytes([sequence, 0xff - sequence])
            crc = _calc_crc(data)
            checksum = bytes([crc >> 8, crc & 0xff])
            packet = header + data + checksum

            # 发送并等待 ACK
            while True:
                self._putc(packet, timeout)
                char = self._getc(1, timeout)
                if char == _ACK:
                    error_count = 0
                    sequence = (sequence + 1) % 0x100
                    break
                elif char == _NAK:
                    pass  # 接收方请求重传当前块，不计 error_count
                else:
                    error_count += 1
                    if error_count > retry:
                        self.abort(timeout)
                        return False

        # EOT 握手
        error_count = 0
        while True:
            self._putc(_EOT, timeout)
            char = self._getc(1, timeout)
            if char == _ACK:
                return True
            error_count += 1
            if error_count > retry:
                self.abort(timeout)
                return False

    def recv(self, stream, retry=10, timeout=60):
        """从设备接收 → stream。返回接收字节数，失败返回 None"""
        error_count = 0
        cancel = 0

        # 发起握手: 发送 'C' 请求 CRC 模式
        while True:
            self._putc(_CRC, 1)
            char = self._getc(1, 3)
            if char == _SOH or char == _STX:
                break
            elif char == _CAN:
                if cancel:
                    return None
                cancel = 1
            else:
                error_count += 1
                if error_count > (retry * 2):
                    self.abort(timeout)
                    return None

        # 逐块接收
        total_bytes = 0
        sequence = 1
        error_count = 0
        cancel = 0

        while True:
            # 超时/断连: _getc 返回 None
            if char is None:
                error_count += 1
                if error_count > retry:
                    self.abort(timeout)
                    return None
                self._putc(_NAK, timeout)
                char = self._getc(1, timeout)
                continue

            # 确定块大小
            if char == _SOH:
                packet_size = 128
            elif char == _STX:
                packet_size = 1024
            elif char == _EOT:
                self._putc(_ACK, timeout)
                return total_bytes
            elif char == _CAN:
                if cancel:
                    return None
                cancel = 1
                char = self._getc(1, timeout)
                continue
            else:
                # 垃圾字节: purge 后 NAK
                error_count += 1
                if error_count > retry:
                    self.abort(timeout)
                    return None
                self._purge(timeout)
                self._putc(_NAK, timeout)
                char = self._getc(1, timeout)
                continue

            cancel = 0

            # 读取: 序号(2) + 数据 + CRC(2) 一次读完
            expected = 2 + packet_size + 2
            raw = self._getc(expected, timeout)
            if raw is None or len(raw) < 2:
                error_count += 1
                if error_count > retry:
                    self.abort(timeout)
                    return None
                self._putc(_NAK, timeout)
                char = self._getc(1, timeout)
                continue

            seq1 = raw[0]
            seq2 = 0xff - raw[1]
            block_data = raw[2:2 + packet_size]
            crc_bytes = raw[2 + packet_size:]

            if seq1 != seq2 or seq1 != sequence:
                # 序号不匹配: NAK 请求重传
                self._putc(_NAK, timeout)
                char = self._getc(1, timeout)
                continue

            # CRC 校验
            if len(crc_bytes) >= 2:
                their_crc = (crc_bytes[0] << 8) | crc_bytes[1]
                our_crc = _calc_crc(block_data)
                if their_crc != our_crc:
                    error_count += 1
                    if error_count > retry:
                        self.abort(timeout)
                        return None
                    self._putc(_NAK, timeout)
                    char = self._getc(1, timeout)
                    continue

            stream.write(block_data)
            total_bytes += len(block_data)
            error_count = 0
            self._putc(_ACK, timeout)
            sequence = (sequence + 1) % 0x100
            char = self._getc(1, timeout)

    def _purge(self, timeout=1, max_bytes=4096):
        """排空线路残留字节（有上限防止设备持续吐数据导致死循环）"""
        for _ in range(max_bytes):
            if self._getc(1, timeout) is None:
                break


# ═══════════════════════════════════════════════════════════════
# SerialSession
# ═══════════════════════════════════════════════════════════════

class SerialSession:
    """一个串口交互式会话"""

    def __init__(self, port: str, baudrate: int = 115200,
                 databits: int = 8, parity: str = "N",
                 stopbits: float = 1, flow_control: str = "none",
                 line_ending: str = "\n", prompt_pattern: str = ""):
        self.port = port
        self.baudrate = baudrate
        self.line_ending = line_ending

        # 提示符正则: 自定义覆盖内置
        self._prompt_re = _PROMPT_RE
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
        self._ser.timeout = _READ_TIMEOUT

        self._ser.open()

        self._busy = False
        self._interrupt = threading.Event()
        self._dead = False

        # 传输模式暂停: 设为 True 时 reader 线程跳过读取，XMODEM 独占串口
        self._transfer_paused = threading.Event()
        self._reader_paused = threading.Event()  # reader 已暂停确认

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
            return f"错误: 串口 {self.port} 已断开"
        if self._busy:
            return "[上一个命令尚未完成，此串口暂不可用]"
        return None

    def execute(self, command: str, timeout: int = 60,
                max_output_chars: int = 2000) -> str:
        """发送命令，等待提示符或超时，返回输出"""
        err = self._ensure_ready()
        if err:
            return err

        self._busy = True
        try:
            return self._do_send(command, timeout, max_output_chars)
        finally:
            self._busy = False

    def send_input(self, text: str, timeout: int = 60,
                   max_output_chars: int = 2000) -> str:
        """发送交互输入（密码、y/n 等），等待提示符或超时——等同于 execute"""
        return self.execute(text, timeout, max_output_chars)

    def raw_execute(self, command: str, timeout: int = 60,
                    max_output_chars: int = 2000) -> str:
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

    def transfer_send(self, local_path: str, remote_path: str,
                      timeout: int = 120, remote_cmd: str = "rx") -> str:
        """发送文件到设备 (XMODEM-1K + CRC)

        向设备写 rx 命令启动接收方, 然后通过 XMODEM 协议发送文件。
        传输期间 reader 线程暂停, 协议独占串口。
        """
        err = self._ensure_ready()
        if err:
            return err

        if not os.path.isfile(local_path):
            return f"错误: 本地文件不存在: {local_path}"

        file_size = os.path.getsize(local_path)
        remote_name = os.path.basename(remote_path) if remote_path else os.path.basename(local_path)
        if not remote_path:
            remote_path = "/tmp/" + remote_name

        self._busy = True
        try:
            return self._do_transfer_send(local_path, remote_path, file_size,
                                          remote_name, timeout, remote_cmd)
        finally:
            self._busy = False

    def transfer_recv(self, local_path: str, remote_path: str,
                      timeout: int = 120, remote_cmd: str = "sx") -> str:
        """从设备接收文件 (XMODEM-1K + CRC)

        向设备写 sx 命令启动发送方, 然后通过 XMODEM 协议接收文件。
        传输期间 reader 线程暂停, 协议独占串口。
        """
        err = self._ensure_ready()
        if err:
            return err

        if not remote_path:
            return "错误: receive 需要提供 remote_path（设备端文件路径）"

        self._busy = True
        try:
            return self._do_transfer_recv(local_path, remote_path, timeout, remote_cmd)
        finally:
            self._busy = False

    def kill_active(self):
        """ESC 打断: 设置中断标志"""
        self._interrupt.set()
        with self._cond:
            self._cond.notify_all()

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
            self._reader.join(timeout=1.0)

    # ── 内部实现 ──

    def _reader_loop(self):
        """后台线程: 持续从串口读取数据到 buffer"""
        while self._reader_alive:
            if self._interrupt.is_set():
                break
            if self._transfer_paused.is_set():
                self._reader_paused.set()  # 确认: reader 已暂停, XMODEM 可独占串口
                time.sleep(0.05)
                continue
            try:
                data = self._ser.read(4096)
                if data:
                    text = self._decoder.decode(data, final=False)
                    with self._cond:
                        self._buffer += text
                        # 背压: buffer 超限时丢弃最早一半
                        if len(self._buffer) > _BUFFER_MAX_CHARS:
                            keep = _BUFFER_MAX_CHARS // 2
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
            return f"错误: 串口写入失败: {e}"

        # 等待提示符或超时
        output, found = self._wait_for_prompt(timeout)

        if found:
            return _truncate_output(
                self._clean_output(output), max_output_chars
            )

        # 超时/中断
        interrupted = self._interrupt.is_set()
        tag = "[ESC中断]" if interrupted else f"[超时: {timeout}秒未检测到提示符]"
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
            return f"错误: 串口写入失败: {e}"

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
        tag = "[ESC中断]" if interrupted else f"[超时: {timeout}秒]"
        cleaned = self._clean_output(output)
        result = f"{cleaned}\n{tag}" if cleaned else tag
        return _truncate_output(result, max_output_chars)

    def _do_transfer_send(self, local_path: str, remote_path: str,
                          file_size: int, remote_name: str,
                          timeout: int, remote_cmd: str) -> str:
        """核心: 暂停 reader → 启动设备端接收命令 → XMODEM 发送 → 恢复 reader"""
        self._interrupt.clear()

        # 保存原始 timeout，传输结束后恢复
        saved_timeout = self._ser.timeout

        # 暂停 reader 线程, 等待其确认
        self._reader_paused.clear()
        self._transfer_paused.set()
        self._reader_paused.wait(timeout=1.0)

        try:
            # 构建 getc/putc: 直连串口, 不经过 buffer, 支持 ESC 打断
            def _getc(size, t=1):
                if self._interrupt.is_set():
                    return None
                self._ser.timeout = t
                result = b""
                deadline = time.time() + max(t * 10, 5)  # 总超时: 10×单次超时, 不低于5s
                while len(result) < size:
                    if time.time() > deadline:
                        break
                    chunk = self._ser.read(size - len(result))
                    if not chunk:
                        break
                    result += chunk
                return result if result else None

            def _putc(data, t=1):
                if self._interrupt.is_set():
                    return None
                self._ser.timeout = t
                return self._ser.write(data)

            # 发送接收命令启动设备端
            cmd = f"{remote_cmd} {remote_path}\n"
            self._ser.write(cmd.encode("utf-8", errors="replace"))
            self._ser.flush()
            time.sleep(0.3)  # 等设备启动接收方

            # XMODEM 发送
            xm = _XMODEM(_getc, _putc)
            with open(local_path, "rb") as f:
                ok = xm.send(f, timeout=timeout)

            if not ok:
                return "错误: XMODEM 发送失败（握手超时或超过重试次数）"

            return (
                f"传输完成: {remote_name} ({file_size:,} 字节) "
                f"→ {remote_path}"
            )

        except Exception as e:
            return f"错误: 传输失败: {e}"
        finally:
            self._ser.timeout = saved_timeout
            self._transfer_paused.clear()
            with self._lock:
                self._buffer = ""  # 清空 reader 暂停前残留的数据

    def _do_transfer_recv(self, local_path: str, remote_path: str,
                          timeout: int, remote_cmd: str) -> str:
        """核心: 暂停 reader → 启动设备端发送命令 → XMODEM 接收 → 恢复 reader"""
        self._interrupt.clear()

        # 保存原始 timeout，传输结束后恢复
        saved_timeout = self._ser.timeout

        # 暂停 reader 线程, 等待其确认
        self._reader_paused.clear()
        self._transfer_paused.set()
        self._reader_paused.wait(timeout=1.0)

        try:
            # 构建 getc/putc: 直连串口, 不经过 buffer, 支持 ESC 打断
            def _getc(size, t=1):
                if self._interrupt.is_set():
                    return None
                self._ser.timeout = t
                result = b""
                deadline = time.time() + max(t * 10, 5)
                while len(result) < size:
                    if time.time() > deadline:
                        break
                    chunk = self._ser.read(size - len(result))
                    if not chunk:
                        break
                    result += chunk
                return result if result else None

            def _putc(data, t=1):
                if self._interrupt.is_set():
                    return None
                self._ser.timeout = t
                return self._ser.write(data)

            # 发送发送命令启动设备端
            cmd = f"{remote_cmd} {remote_path}\n"
            self._ser.write(cmd.encode("utf-8", errors="replace"))
            self._ser.flush()
            time.sleep(0.3)  # 等设备启动发送方

            # XMODEM 接收
            xm = _XMODEM(_getc, _putc)
            with open(local_path, "wb") as f:
                byte_count = xm.recv(f, timeout=timeout)

            if byte_count is None:
                return "错误: XMODEM 接收失败（握手超时或超过重试次数）"

            return (
                f"传输完成: {remote_path} → {local_path} "
                f"({byte_count:,} 字节)"
            )

        except Exception as e:
            return f"错误: 传输失败: {e}"
        finally:
            self._ser.timeout = saved_timeout
            self._transfer_paused.clear()
            with self._lock:
                self._buffer = ""  # 清空 reader 暂停前残留的数据

    def _wait_for_prompt(self, timeout: int) -> tuple:
        """等待提示符出现, 返回 (output, found)

        稳定性检查仅在命令发送后至少 _MIN_EXEC_TIME 秒才开始，
        避免输出中偶然出现的提示符字符在短暂停顿时触发误判。
        """
        start = time.time()
        poll_interval = _POLL_INITIAL

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                break
            if self._interrupt.is_set():
                break

            with self._lock:
                current = self._buffer

            # 提示符检测 + 稳定性校验（需满足最小执行时间）
            if elapsed >= _MIN_EXEC_TIME and self._is_at_prompt(current):
                if self._check_stability():
                    with self._lock:
                        result = self._buffer
                    return result, True

            remaining = timeout - elapsed
            with self._cond:
                self._cond.wait(timeout=min(poll_interval, remaining))

            poll_interval = min(poll_interval * _POLL_MULTIPLIER, _POLL_MAX)

        with self._lock:
            result = self._buffer
        return result, False

    def _is_at_prompt(self, text: str) -> bool:
        """检查最后一行是否匹配提示符"""
        if not text:
            return False
        cleaned = _ANSI_RE.sub("", text)
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
            for _ in range(_STABILITY_SAMPLES - 1):
                self._cond.wait(timeout=_STABILITY_INTERVAL)
                curr = self._buffer
                if curr != prev:
                    return False
                prev = curr
        return True

    @staticmethod
    def _clean_output(raw: str) -> str:
        """清洗 ANSI 转义码、\\r 覆盖、多余空行"""
        cleaned = _ANSI_RE.sub("", raw)
        # 先归一化 \\r\\n → \\n（CRLF 是行结束符，非覆盖符）
        cleaned = cleaned.replace("\r\n", "\n")
        merged = [_merge_cr_line(line) for line in cleaned.split("\n")]
        cleaned = "\n".join(merged)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()
