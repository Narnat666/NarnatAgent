"""Token统计和费用追踪

从agent.py中提取，负责token计数、费用计算、余额查询。
配置"费用日志"开启时，每次LLM请求追加一行到CSV（与界面费用同口径）。
"""

import csv
import io
import os
import time

from .billing import calculate_cost, cost_breakdown, fetch_balance

# 费用日志CSV表头
COST_LOG_HEADER = [
    "时间", "模型",
    "输入tokens", "缓存命中tokens", "缓存未命中tokens",
    "输出tokens(含思考)",           # 服务端usage不分拆思考token，思考与正文合并计输出
    "输入费用(元)", "缓存费用(元)", "输出费用(元)",
    "本次费用(元)", "累计费用(元)",
]

# ── 追加写：以单次系统调用把整行原子追加到文件末尾（多进程并发不丢行）──
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _FILE_APPEND_DATA = 0x0004
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4      # READ | WRITE | DELETE
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _append_bytes(path: str, data: bytes) -> None:
    """把 data 原子追加到 path 末尾（单次系统调用）。

    Windows: FILE_APPEND_DATA 内核级原子追加——O_APPEND 经 CRT 的 seek+write
    非原子，8 进程并发实测丢行；其他平台: os.write + O_APPEND（POSIX 原子）。
    """
    if _IS_WINDOWS:
        h = _kernel32.CreateFileW(path, _FILE_APPEND_DATA, _FILE_SHARE_ALL, None,
                                  _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None)
        if h == _INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), f"无法打开费用日志: {path}")
        try:
            written = wintypes.DWORD(0)
            if not _kernel32.WriteFile(h, data, len(data), ctypes.byref(written), None) \
                    or written.value != len(data):
                raise OSError(ctypes.get_last_error(), f"费用日志写入失败: {path}")
        finally:
            _kernel32.CloseHandle(h)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _as_int(v) -> int:
    """usage 字段容错：None/字符串/非法值 → 0（防第三方 API 异常 usage 崩溃统计）"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class StatsTracker:
    """Token统计和费用追踪器"""

    def __init__(self, model: str, user_pricing=None,
                 balance_cfg=None, cost_log_cfg=None):
        self._model = model
        self._user_pricing = user_pricing
        self._balance_cfg = balance_cfg
        # cost_log_cfg 缺省(None)时各字段取默认值 → 功能关闭，与原行为一致
        self._cost_log_enabled = bool(getattr(cost_log_cfg, "enabled", False))
        self._cost_log_path = getattr(cost_log_cfg, "path", "")
        try:
            self._cost_log_max_bytes = int(getattr(cost_log_cfg, "max_bytes", 0) or 0)
        except (TypeError, ValueError):
            self._cost_log_max_bytes = 0
        self._cost_log_pending = []  # 写失败时缓存的行，下次写入前自动补写
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_prompt_tokens = 0  # 累计输入token（所有轮次求和）
        self._total_cache_tokens = 0   # 累计缓存命中token
        self._total_cost = 0.0
        self._balance_to_show = 0.0

    def update(self, usage: dict) -> None:
        """更新token统计（每轮LLM返回后调用）

        usage: {"prompt_tokens": int, "completion_tokens": int, "cached_tokens": int}
        字段缺失/类型异常（None/字符串）按 0 处理，不中断统计。
        """
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        cached = _as_int(usage.get("cached_tokens"))
        self._total_output_tokens += completion
        self._total_input_tokens = prompt  # 赋值：每轮已含全部历史（当前上下文快照）
        # 累计：用于计算全程 token 加权缓存命中率（小轮次不会被等权放大）
        self._total_prompt_tokens += prompt
        self._total_cache_tokens += cached
        self._total_cost += calculate_cost(
            self._model,
            prompt,
            completion,
            cached,
            self._user_pricing,
        )
        # 费用日志（配置开启时）：与上面同一分项口径落盘
        if self._cost_log_enabled:
            self._append_cost_log(prompt, completion, cached)

    def _append_cost_log(self, prompt: int, completion: int, cached: int) -> None:
        """追加一行费用记录到CSV（每次LLM请求调用一次）。

        写入失败（如文件被 WPS/Excel 独占）不丢行：缓存到内存队列，
        下次写入前自动补写；失败期间累计费用仍准确（按内存累计）。
        """
        uncached = prompt - cached
        inp_cost, cache_cost, out_cost = cost_breakdown(
            self._model, prompt, completion, cached, self._user_pricing
        )
        row = [
            time.strftime("%Y-%m-%d %H:%M:%S"),
            self._model,
            prompt, cached, uncached, completion,
            round(inp_cost, 9), round(cache_cost, 9), round(out_cost, 9),
            round(inp_cost + cache_cost + out_cost, 9),
            round(self._total_cost, 9),
        ]
        # 先补写积压行，再写当前行。写成功一行立即出队，失败即停：
        # 未写出的积压行与当前行留到下次再试，保证不丢行也不重复写。
        try:
            while self._cost_log_pending:
                self._write_cost_log_row(self._cost_log_pending[0])
                self._cost_log_pending.pop(0)
            self._write_cost_log_row(row)
        except OSError:
            self._cost_log_pending.append(row)

    def _write_cost_log_row(self, row: list) -> None:
        """写一行到CSV；表头由文件唯一创建者写；写满自动轮转 _bak。失败抛OSError。"""
        path = self._cost_log_path or os.path.join(".narnat", "data", "cost_log.csv")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        need_header = False
        if size > 0 and self._cost_log_max_bytes > 0 and size >= self._cost_log_max_bytes:
            # 写满 → 当前文件转 _bak（旧 _bak 被覆盖），新建活动文件继续写；
            # 新文件的表头归属由下方 O_EXCL 探测裁定（轮转者与并发进程一视同仁）
            if self._rotate_to_bak(path):
                size = 0
        if size == 0:
            # 表头归属：唯一创建者写表头（O_EXCL 原子探测；同刻多进程只有一人成功）。
            # 既有"size==0 → 写表头"判定与追加写入之间存在 TOCTOU：多进程同刻
            # 首建时各自看到 size==0，都会补写表头（数据行不丢、结构被污染）。
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, "O_BINARY", 0))
                os.close(fd)
                need_header = True
            except FileExistsError:
                # 文件已存在但为空：可能是创建者刚建立、表头块尚未落盘。
                # 短暂等待（有界）表头块落地，避免本进程的数据行抢在表头之前
                # （表头错位）；超时（创建者写入失败/遗留空文件）则直接写数据行。
                deadline = time.time() + 0.2
                while time.time() < deadline:
                    try:
                        if os.path.getsize(path) > 0:
                            break
                    except OSError:
                        break
                    time.sleep(0.005)
                need_header = False   # 他人已创建（表头由创建者负责）
        # 单次系统调用追加写（见 _append_bytes）：open("a") 文本缓冲写在多进程
        # 高频并发下互相覆盖丢行，故先构建完整单次写内容再原子追加。
        buf = io.StringIO()
        writer = csv.writer(buf)
        if need_header:
            writer.writerow(COST_LOG_HEADER)
        writer.writerow(row)
        data = buf.getvalue().encode("utf-8")
        if need_header:
            data = b"\xef\xbb\xbf" + data     # 新文件保留 BOM（与现状一致）
        _append_bytes(path, data)

    def _rotate_to_bak(self, path: str) -> bool:
        """双文件轮转：写满的当前文件改名为「主名_bak.扩展名」，旧 _bak 被覆盖。

        磁盘上始终只有 1 个活动文件 + 1 个备份文件：
        活动文件写满 → 转备份（旧的备份被顶掉），再新建活动文件从表头开始写。
        返回是否轮转成功（成功才需为新文件写表头）。
        """
        try:
            root, ext = os.path.splitext(path)
            bak = f"{root}_bak{ext}"
            os.replace(path, bak)   # 当前写满的文件 → _bak（可覆盖已存在的旧备份）
            return True
        except OSError:
            # 轮转失败（如文件被 WPS/Excel 独占）不中断写入：
            # 本次行继续追加到已超限的活动文件（尽力而为，不再写表头）
            return False

    def fetch_balance(self, api_key: str, round_num: int, interval: int = 10) -> None:
        """每N轮查询一次余额，其余轮次保留上次查询结果"""
        if api_key and round_num % interval == 0:
            bal = fetch_balance(api_key, self._balance_cfg)
            if bal:
                self._balance_to_show = bal["total"]

    @property
    def input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def output_tokens(self) -> int:
        return self._total_output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        """全程累计缓存命中率（0~1）：累计命中token / 累计输入token，token加权"""
        if self._total_prompt_tokens == 0:
            return 0.0
        return self._total_cache_tokens / self._total_prompt_tokens

    @property
    def cost(self) -> float:
        return self._total_cost

    @property
    def balance(self) -> float:
        return self._balance_to_show
