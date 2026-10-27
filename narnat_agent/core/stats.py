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
        # 消耗变化监听（rpc 层注入）：外部调用（压缩/复核/总结/命名）发生后
        # 立即推一次 stats，界面不用等下一次轮末才看到费用/缓存变化。
        self._usage_listener = None
        self._balance_to_show = 0.0
        self._balance_known = False   # 是否曾经成功查到过余额（持久，见 fetch_balance）
        # 本轮是否**刚刚**查到新余额（瞬时，每轮开始时由 begin_round 清零）。
        # 与上面的持久标记分工不同：终端统计栏只在"这一轮真的查了"时显示余额，
        # 否则同一条旧余额会连续出现在 10 个统计栏里，看着像在重复报告。
        self._balance_fresh = False

    def set_usage_listener(self, fn) -> None:
        """注入「消耗发生变化」回调（rpc 层用它即时刷新状态栏）。

        只在一个地方抛出去、由消费方自己容错：统计出错绝不能影响主流程
        （压缩/复核失败和「状态栏慢一拍」不是一个量级的问题）。
        """
        self._usage_listener = fn

    def _notify_usage(self) -> None:
        fn = self._usage_listener
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass

    def begin_round(self) -> None:
        """新的一轮开始：清掉"本轮刚查到余额"的瞬时标记。

        必须在每轮 fetch_balance 之前调用（agent.py 的轮次计数处），
        否则上一轮的 True 会残留到本轮，让没查的轮次也显示余额。
        """
        self._balance_fresh = False

    def reset_session(self) -> None:
        """会话级统计清零（换会话时由 SessionManager 的 context reset 链调用）。

        这些累计量原本从后端进程启动起一直累加，而换会话只换消息列表：不清零，
        新会话第一条消息就会顶着上一段会话的费用/缓存命中率上报（实测「新建对话
        发个你好，费用显示 ¥3.7033」，就是上一段的钱）。

        只清会话级：余额是账户属性（_balance_to_show/_balance_known/
        _balance_fresh），以及被 WPS/Excel 挡住后待补写的费用日志行，都保留。

        有意差异：费用日志 CSV 最后一列「累计费用」随之变成「本会话累计」，
        不再贯穿进程生命周期。列名未改，按进程总量核对历史数据需跨会话自行求和。
        """
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_prompt_tokens = 0
        self._total_cache_tokens = 0
        self._total_cost = 0.0

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

    def log_external_usage(self, usage: dict) -> None:
        """旁路调用的记账：压缩摘要 / 完成复核 / 分支总结 / 自动命名。

        这些调用的 token 与费用**同样是本会话真实花掉的钱**，所以与主口径
        一起累计（输出 token、prompt/cache token、费用）。不这样做会怎样
        （实测）：界面状态栏写 ¥0.0123，而同一目录的 cost_log.csv 求和是
        ¥0.0194 —— 用户按 CSV 对账只会以为界面在少报，且没有任何地方能
        解释差额。

        唯一的例外是 `input_tokens`（上下文占比的来源）：那是**主会话上下文
        快照**，外部调用有自己的独立 prompt，混进来会把「当前上下文占用」
        算成别人的。

        记账后主动通知一次（`_notify_usage`）：压缩/复核常常发生在轮中或轮
        间，不推的话界面要等到下一次轮末才更新，用户看到的是旧费用。
        """
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        cached = _as_int(usage.get("cached_tokens"))
        self._total_output_tokens += completion
        self._total_prompt_tokens += prompt
        self._total_cache_tokens += cached
        self._total_cost += calculate_cost(
            self._model,
            prompt,
            completion,
            cached,
            self._user_pricing,
        )
        # 费用日志与主口径同一分项落盘；关掉 CSV 只是不落盘，不影响上面的累计
        if self._cost_log_enabled:
            self._append_cost_log(prompt, completion, cached)
        self._notify_usage()

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
                # （表头错位）；超时（创建者写入失败/遗留空文件）则本进程补写
                # 表头——否则该文件此后永远无表头（数据行可解析性受损）
                deadline = time.time() + 0.2
                while time.time() < deadline:
                    try:
                        if os.path.getsize(path) > 0:
                            break
                    except OSError:
                        break
                    time.sleep(0.005)
                need_header = False   # 他人已创建（表头由创建者负责）
                try:
                    if os.path.getsize(path) == 0:
                        need_header = True   # 遗留空文件：无创建者，本进程补写
                except OSError:
                    pass
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
        else:
            # 尾部半行修复：文件尾若非换行（外部编辑/历史中断留下残行），
            # 直接追加会让两行拼成结构错位行——追加前补一个换行
            try:
                with open(path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) not in (b"\n",):
                        data = b"\n" + data
            except (OSError, ValueError):
                pass
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
        """每N轮查询一次余额。

        两个「一直显示 0」的坑，都是这里造成的：

        1. 原来无条件按 `round_num % interval == 0` 判，而 round_num 从 1 开始，
           于是**前 9 轮一次都不查**。插件界面的余额是常驻状态项，用户开面板
           做快速验证时永远等不到第 10 条消息，看到的就是空/0（实测反馈
           「余额一直显示是 0」）。现在改成：从没查到过就立刻查，查到过才按
           轮次间隔刷新。

        2. 原来用一个「本轮是否刚查询」的瞬时标记控制 balance，每轮先置 False，
           于是没查的轮次 balance 一律读成 0.0。
           等于拿 0.0 同时表示「本轮没查」和「余额真的是 0」，前端无法区分——
           它一旦见过正数，就会把后续每一个「没查」都读成「真花光了」，
           于是 ¥10134.68 只闪一轮，之后永久钉在 ¥0.00（实测就是这个现象）。
           现在 `_balance_known` 持久标记「查到过」，balance 返回最后一次
           查到的真实值，0.0 只剩「账户真的没钱了」一个含义。
        """
        if not api_key:
            return
        # 没查到过（首次 / 之前全部失败）→ 立刻查，不等间隔
        if self._balance_known and round_num % interval != 0:
            return
        bal = fetch_balance(api_key, self._balance_cfg)
        if bal:
            self._balance_to_show = bal["total"]
            self._balance_known = True
            self._balance_fresh = True

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
    def balance_known(self) -> bool:
        """是否成功查到过余额。

        调用方靠它区分 balance==0 的两种含义：还没查到（该重试 / 该显示「—」），
        还是账户真的为 0（该如实显示 ¥0.00 并提示充值）。
        """
        return self._balance_known

    @property
    def balance_fresh(self) -> bool:
        """本轮是否刚刚查到新余额（瞬时标记）。

        终端统计栏用它决定"这一栏要不要显示余额"：余额只在每 10 轮查一次，
        没查的轮次还显示同一个旧数字，会让用户以为它在反复报告同一件事，
        也看不出这个数是多久以前的。插件是常驻状态栏，不读这个标记。
        """
        return self._balance_fresh

    @property
    def balance(self) -> float:
        """最后一次查询成功的余额（0.0 = 账户真的为 0 或从未查到过）。

        不再随「本轮是否刚查」归零：余额是账户属性，不会因为你这一轮没查
        就变成 0。归零还有个更坏的后果——前端把 0 读成「钱花光了」，
        于是一直显示 ¥0.00（见 fetch_balance 注释）。
        """
        return self._balance_to_show if self._balance_known else 0.0
