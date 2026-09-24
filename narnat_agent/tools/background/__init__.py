"""后台任务管理 —— Shell 工具 bg 参数的后端（bash/__init__.py 分发调用）

8 固定槽位 bg1~bg8：一任务一文件 bgN.log（复用前旧结果先归档为
bgN.log.<seq>.prev，永不静默丢失），结果落在系统临时目录下本会话
专属子目录 narnat_bg_<随机>（每进程唯一）：
- 不进用户工作目录：叉窗强杀残留也不会污染项目/桌面，仅占临时目录
- 每进程唯一：同项目多 agent、父子 agent 天然互不抢目录
- 过期清扫：会话启动时清理历史强杀残留（保活期外未动过的目录）
AI 用 Read/Grep 按 submit/status 返回的绝对路径读取结果文件，
从不接触文件管理。

职责边界：本模块只管"跑"——提交/状态/等待/取消/清理/落盘截断；
"看"由 AI 用现有工具完成。

关键实现点：
- 完成判定 = 主进程退出 + 管道收口宽限(DRAIN_GRACE)：命令内嵌 start /b
  时孙进程继承管道写端永不 EOF，宽限后仍未收口即强制收口，
  泵线程不会永久阻塞（沿用 bash 模块 _drain_readers 的教训）。
- 代次(generation)防串扰：槽位每次复用 generation+1，旧任务的 monitor/
  泵线程发现代次不符立即失效——不写状态表、不写文件、不动收口计数，
  覆盖了"占位与 proc 赋值之间"的竞态窗口。
- 复用归档：终态槽位被复用时旧日志改名 bgN.log.<seq>.prev（含 .tail），
  旧结果可追溯，杜绝"结果静默丢失"。
- 落盘粒度：泵线程按 4KB 读块落盘（写后即 flush）；输出量大的任务运行中
  可读到增量，输出小于一块的任务通常在结束（EOF）时才可见。
- 编码统一：泵线程把子进程原始字节（cmd 为 GBK）按前台同策略增量解码后
  以 UTF-8 落盘，日志全文件单编码（旧缺陷：GBK 字节直通与 UTF-8 头部混编，
  含中文输出的日志头/正文须牺牲其一）。
- 超大输出截断：主文件 50MB 封顶，其后输出滚动进内存尾部缓冲(1MB)，
  收口时落盘 bgN.log.tail —— 错误信息常在尾部，截断也不丢。
- 会话结束硬兜底：cleanup_all 杀全部 running 进程树 + 清目录 + 重建槽位表。
"""

import codecs
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..exec_signal import error_line

# ── 常量 ──
MAX_SLOTS = 8                      # 并发上限：固定槽位 bg1~bg8
MAX_FILE_BYTES = 50 * 1024 * 1024  # 主文件落盘上限（默认50MB）
TAIL_BYTES = 1 * 1024 * 1024       # 截断后滚动保留的尾部缓冲
DRAIN_GRACE = 0.3                  # 管道收口宽限（秒）
TAIL_SUFFIX = ".tail"

BG_DIR_PREFIX = "narnat_bg_"       # 会话临时目录前缀（过期清扫按它识别自家目录）
BG_STALE_SECONDS = 7 * 24 * 3600   # 强杀残留保活期：目录超过此时间未动即清扫

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 结果根目录：首次使用时创建本会话专属临时目录（此后 AI 的 cd 不影响结果路径）
_base_dir: Optional[str] = None


def _root_dir() -> str:
    global _base_dir
    if _base_dir is None:
        _base_dir = tempfile.mkdtemp(prefix=BG_DIR_PREFIX)
        _sweep_stale()
    return _base_dir


def _bg_dir() -> str:
    return _root_dir()


def _sweep_stale() -> None:
    """清扫历史强杀残留：系统临时目录下保活期外未动过的 narnat_bg_* 目录。

    三道防线防误删（自上而下）：
    1. 目录 mtime 在保活期内（近期有提交/归档）→ 会话活跃，跳过；
    2. 目录内任一文件在保活期内（长跑任务只更新日志文件 mtime）→ 活跃，跳过；
    3. 判定过期后先原子改名再删：目录内若仍有活跃写入（打开的文件句柄），
       Windows 下改名必然失败 → 放弃删除；改名成功（真孤儿/纯旧日志）才删。
       Linux 无句柄锁（POSIX 语义），本防线不生效——尽力而为。
    任何失败一律放弃，绝不强行删除；最坏后果仅是旧日志（会话级临时结果）
    延迟回收，不影响任何功能。
    """
    try:
        now = time.time()
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            if not name.startswith(BG_DIR_PREFIX):
                continue
            p = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(p) <= BG_STALE_SECONDS:
                    continue  # 防线1：目录近期有提交/归档
                if _has_fresh_file(p, now):
                    continue  # 防线2：长跑任务日志仍在持续落盘
                # 防线3：先原子改名再删。目录内有打开句柄（活跃写入）时
                # Windows 下 rename 失败 → 视为活跃会话，放弃删除
                trash = p + ".stale"
                try:
                    os.replace(p, trash)
                except OSError:
                    continue
                shutil.rmtree(trash, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def _has_fresh_file(p: str, now: float) -> bool:
    """目录内是否存在保活期内的文件（活跃会话的长跑日志在持续更新文件 mtime）"""
    try:
        entries = os.listdir(p)
    except OSError:
        return False
    for f in entries:
        try:
            if now - os.path.getmtime(os.path.join(p, f)) <= BG_STALE_SECONDS:
                return True
        except OSError:
            continue
    return False


@dataclass
class _Slot:
    """单个后台任务槽位。status 空串=空闲。"""
    id: int
    command: str = ""
    proc = None
    status: str = ""
    exit_code: Optional[int] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    file_bytes: int = 0      # 主文件已写字节数（≤MAX_FILE_BYTES）
    total_bytes: int = 0     # 总输出字节数（含截断后进入尾部缓冲的）
    truncated: bool = False
    log_path: str = ""
    fh = None                # 主文件句柄（wb，泵线程持续写）
    tail_chunks: List[bytes] = field(default_factory=list)
    tail_bytes: int = 0
    readers_left: int = 0    # 未收口的泵线程数（0=输出全部落盘）
    cancel_flag: bool = False
    generation: int = 0      # 任务代次：每次复用+1，monitor/泵线程据此识别是否仍是当前任务


# 槽位表（模块级，cleanup_all 时整体重建以杜绝旧 monitor 串扰）
_slots: List[_Slot] = [_Slot(id=i) for i in range(1, MAX_SLOTS + 1)]
_lock = threading.Lock()             # 槽位表锁（泵线程写文件也在其内）
_terminal_event = threading.Event()  # 任一任务进入终态时置位（wait 依赖）
_archive_seq = 0                     # 归档文件序号（会话内递增，防同秒覆盖）


def _ensure_dir() -> str:
    """确保结果目录存在（临时目录下，无 git 提交风险，无需 .gitignore）"""
    d = _bg_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _wipe_dir() -> None:
    try:
        shutil.rmtree(_bg_dir(), ignore_errors=True)
    except OSError:
        pass


def _archive_old(slot: _Slot, min_bytes: int) -> str:
    """槽位复用时把旧结果归档为 bgN.log.<seq>.prev（含 .tail），防静默丢失。

    min_bytes = 旧任务输出字节数（占位前快照）：为 0 说明日志仅有信息头
    （如上次 Popen 失败的空残留），无需归档。返回归档主文件路径。
    归档后 slot.log_path 指向归档文件，由调用方随即重置为新任务路径。
    """
    if min_bytes <= 0 or not slot.status or not slot.log_path:
        return ""
    old_main = slot.log_path
    old_tail = old_main + TAIL_SUFFIX
    try:
        has_content = os.path.exists(old_main) and os.path.getsize(old_main) > 0
        if not has_content:
            return ""
        global _archive_seq
        _archive_seq += 1
        archived = f"{old_main}.{_archive_seq}.prev"
        os.replace(old_main, archived)
        if os.path.exists(old_tail):
            os.replace(old_tail, archived + TAIL_SUFFIX)
        slot.log_path = archived
        return archived
    except OSError:
        return ""


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _fmt_dur(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


# ═══════════════════════════════════════════════════════════════
# 提交
# ═══════════════════════════════════════════════════════════════

def submit(command: str) -> str:
    """提交后台任务：分配可复用槽位，立即返回 bgN + 结果路径。

    槽位语义（对齐用户决策）：终态(done/failed/cancelled)槽位即释放；
    复用时旧结果先归档为 bgN.log.<seq>.prev（永不静默丢失），新任务
    日志写回 bgN.log——编号即身份，AI 只记"bgN 对应 bgN.log"。
    分配策略：最旧终态优先（刚完成的结果尽量多留读取窗口期）。
    """
    from ..bash import BashRuntime  # 延迟导入：避免 bash↔background 循环

    with _lock:
        free = [s for s in _slots if s.status != STATUS_RUNNING]
        if not free:
            running = [s.id for s in _slots if s.status == STATUS_RUNNING]
            return error_line(
                f"后台并发已达上限{MAX_SLOTS}(bg1~bg{MAX_SLOTS})，当前运行: "
                + (", ".join(f"bg{i}" for i in running) or "无")
                + "。请用 Shell(bg=\"wait\") 等待完成，"
                + "或 Shell(bg=\"cancel\", id=N) 取消不再需要的任务"
            )
        # 最旧终态优先：空闲槽(finished_at=0)最前，终态按完成时间升序
        free.sort(key=lambda s: s.finished_at if s.status else -1.0)
        slot = free[0]
        # 先占位：起进程前标记 running 并递增代次，防并发重复分配，
        # 同时让旧任务的 monitor/泵线程立即失效（防收尾串扰新任务）
        prev_total = slot.total_bytes  # 旧任务输出字节数（归档判定用，占位前快照）
        slot.generation += 1
        slot.command = command.strip()
        slot.status = STATUS_RUNNING
        slot.exit_code = None
        slot.started_at = time.time()
        slot.finished_at = 0.0
        slot.file_bytes = 0
        slot.total_bytes = 0
        slot.truncated = False
        slot.tail_chunks = []
        slot.tail_bytes = 0
        slot.readers_left = 0
        slot.cancel_flag = False
        gen = slot.generation

    d = _ensure_dir()
    # 复用归档：旧结果改名留存（含 .tail），新日志仍写 bgN.log
    archived = _archive_old(slot, prev_total)
    slot.log_path = os.path.join(d, f"bg{slot.id}.log")
    try:
        slot.fh = open(slot.log_path, "wb")
        # 立即写任务信息头：AI 读日志即可识别文件归属（防错读），
        # Windows 下 cmd 管道输出块缓冲，运行中至少能读到本头行
        head = (
            f"# bg{slot.id} 提交于 {time.strftime('%H:%M:%S')}"
            f" | 命令: {slot.command}\n"
        ).encode("utf-8")
        slot.fh.write(head)
        slot.fh.flush()
    except OSError as e:
        with _lock:
            slot.status = ""
        return error_line(f"无法写入结果文件: {e}")

    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                slot.command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                # 独立（无窗口）控制台：后台命令的 chcp/cls 等直写不再清用户终端屏幕
                creationflags=BashRuntime.WIN_NO_WINDOW,
                env=BashRuntime.utf8_env,
            )
        else:
            sh = shutil.which("bash") or shutil.which("sh")
            if sh is None:
                raise OSError("未找到shell，请安装bash或sh后重试")
            proc = subprocess.Popen(
                [sh, "-c", slot.command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                start_new_session=True,
                env=BashRuntime.utf8_env,
            )
    except (OSError, ValueError) as e:
        with _lock:
            slot.status = ""
        try:
            slot.fh.close()
        except Exception:
            pass
        return error_line(f"后台任务启动失败: {e}")

    slot.proc = proc
    slot.readers_left = 2
    # 泵线程：stdout/stderr 按读取顺序合并写入同一文件（代次不符即弃写退出）
    for stream in (proc.stdout, proc.stderr):
        threading.Thread(target=_pump, args=(slot, stream, gen), daemon=True).start()
    # 监控线程：主进程退出 → 管道收口 → 终态
    threading.Thread(target=_monitor, args=(slot, proc, gen), daemon=True).start()
    msg = (
        f"bg{slot.id} 已提交（后台运行）\n"
        f"结果: {slot.log_path}\n"
        f"查状态: Shell(bg=\"status\") · 等待完成: Shell(bg=\"wait\")"
    )
    if archived:
        msg += f"\n提示: 已归档该槽位旧任务结果到 {archived}，仍可 Read 读取"
    return msg


# ═══════════════════════════════════════════════════════════════
# 泵 / 监控
# ═══════════════════════════════════════════════════════════════

class _StreamDecoder:
    """后台输出流 → UTF-8 文本的增量解码器（跨读块保留不完整多字节尾巴）。

    策略与前台 _decode_output 一致：UTF-8 优先、Windows 回退 GBK、兜底
    替换。增量性：单次 read(4096) 可能在多字节字符中间截断，逐块直接
    decode 会在块边界产生乱码——锁定编码前对累计缓冲整体重试，锁定后
    交给增量解码器自行保留尾巴。日志头部为 UTF-8，统一转码后整个文件
    单编码，读端任意工具都能完整读取。
    """

    def __init__(self) -> None:
        self._buf = b""     # 未锁定编码前的探测缓冲
        self._dec = None    # 锁定后的增量解码器

    @staticmethod
    def _candidates():
        return ("utf-8", "gbk") if sys.platform == "win32" else ("utf-8",)

    @staticmethod
    def _is_incomplete(e: UnicodeDecodeError) -> bool:
        """编码器报错是否属于"结尾多字节序列未收全"（可等下一块）。"""
        reason = (e.reason or "").lower()
        return "end of data" in reason or "incomplete" in reason

    def feed(self, data: bytes) -> str:
        """喂入一块原始字节，返回可立即落盘的文本（空串=尾部不完整待续）。"""
        if self._dec is not None:
            return self._dec.decode(data)
        self._buf += data
        if self._buf.isascii():  # 纯ASCII在任何候选编码下等价，直接输出
            out = self._buf.decode("ascii")
            self._buf = b""
            return out
        for enc in self._candidates():
            try:
                out = self._buf.decode(enc)
            except UnicodeDecodeError:
                continue
            # 完整解码成功：锁定该编码，后续走增量解码
            self._dec = codecs.getincrementaldecoder(enc)(errors="replace")
            self._buf = b""
            return out
        # 全部候选失败：区分"结尾截断"（等下一块）与"确定乱码"（兜底替换）
        for enc in self._candidates():
            try:
                self._buf.decode(enc)
            except UnicodeDecodeError as e:
                if self._is_incomplete(e):
                    return ""
        out = self._buf.decode("utf-8", errors="replace")
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = b""
        return out

    def flush(self) -> str:
        """流结束收尾：吐出解码器/缓冲残留（不完整尾巴按替换字符落盘）。"""
        if self._dec is not None:
            return self._dec.decode(b"", final=True)
        if not self._buf:
            return ""
        for enc in self._candidates():
            try:
                out = self._buf.decode(enc)
                self._buf = b""
                return out
            except UnicodeDecodeError:
                continue
        out = self._buf.decode("utf-8", errors="replace")
        self._buf = b""
        return out


def _pump(slot: _Slot, stream, gen: int) -> None:
    """泵线程：读子进程管道 → 转码 UTF-8 → 合并写主文件（超限后滚动进尾部缓冲）。

    代次校验：槽位被复用后本线程立即失效——不再写文件（防旧输出串扰
    新任务日志）、不再动 readers_left（防误减新任务收口计数）。
    """
    decoder = _StreamDecoder()
    try:
        while True:
            data = stream.read(4096)
            if not data:
                break
            text = decoder.feed(data)
            if not text:
                continue
            with _lock:
                if slot.generation != gen:
                    return
                _append_output(slot, text.encode("utf-8"))
    except Exception:
        pass
    finally:
        text = decoder.flush()  # EOF 收尾：冲刷不完整尾巴
        if text:
            with _lock:
                if slot.generation == gen:
                    _append_output(slot, text.encode("utf-8"))
        with _lock:
            if slot.generation == gen:
                slot.readers_left = max(0, slot.readers_left - 1)


def _monitor(slot: _Slot, proc, gen: int) -> None:
    """监控线程：等主进程退出，宽限收口后落终态。"""
    try:
        rc = proc.wait()
    except Exception:
        rc = -1
    deadline = time.time() + DRAIN_GRACE
    while True:
        with _lock:
            if slot.generation != gen:
                return  # 槽位已被复用：本任务收尾交给新任务
            left = slot.readers_left
        if left == 0:
            break
        if time.time() >= deadline:
            break  # 孙进程继承管道：宽限后强制收口
        time.sleep(0.02)
    _finalize(slot, proc, gen, rc)


def _finalize(slot: _Slot, proc, gen: int, rc: int) -> None:
    with _lock:
        if slot.generation != gen:
            return  # 槽位已被复用/清空：旧任务收尾不得污染新任务
        slot.exit_code = rc
        if slot.cancel_flag:
            slot.status = STATUS_CANCELLED
        elif rc == 0:
            slot.status = STATUS_DONE
        else:
            slot.status = STATUS_FAILED
        slot.finished_at = time.time()
        tail = b"".join(slot.tail_chunks) if slot.truncated else None
        fh, slot.fh = slot.fh, None
        _terminal_event.set()
    # 出锁收尾：close 的是本任务句柄（锁内已摘除字段引用，
    # 与复用后新任务打开的新句柄互不干扰）
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    if tail is not None:
        with _lock:
            if slot.generation != gen:
                tail = None  # 已复用：tail 路径归属新任务，放弃旧尾部（主文件已归档）
        if tail is not None:
            try:
                with open(slot.log_path + TAIL_SUFFIX, "wb") as f:
                    f.write(tail)
            except OSError:
                pass


def _append_output(slot: _Slot, data: bytes) -> None:
    """持锁写入：主文件 ≤ MAX_FILE_BYTES，超出部分滚动进尾部缓冲。"""
    slot.total_bytes += len(data)
    if not slot.truncated:
        room = MAX_FILE_BYTES - slot.file_bytes
        if len(data) <= room:
            slot.file_bytes += len(data)
            _file_write(slot, data)
            return
        if room > 0:
            _file_write(slot, data[:room])
            slot.file_bytes += room
        slot.truncated = True
        data = data[room:]
    _append_tail(slot, data)


def _file_write(slot: _Slot, data: bytes) -> None:
    try:
        slot.fh.write(data)
        slot.fh.flush()  # 实时落盘：运行中即可 Read 增量内容（P2修复）
    except (OSError, ValueError):
        pass


def _append_tail(slot: _Slot, data: bytes) -> None:
    """尾部滚动缓冲：总长 ≤ TAIL_BYTES，丢最老块（保持最新 1MB）。"""
    slot.tail_chunks.append(data)
    slot.tail_bytes += len(data)
    while slot.tail_bytes > TAIL_BYTES and len(slot.tail_chunks) > 1:
        dropped = slot.tail_chunks.pop(0)
        slot.tail_bytes -= len(dropped)


# ═══════════════════════════════════════════════════════════════
# 状态 / 等待 / 取消 / 清理
# ═══════════════════════════════════════════════════════════════

def _snapshot() -> str:
    """全量状态快照文本（status 与 wait 返回共用）。"""
    lines = []
    with _lock:
        actives = [s for s in _slots if s.status]
        for s in actives:
            cmd = s.command[:40] + ("…" if len(s.command) > 40 else "")
            if s.status == STATUS_RUNNING:
                head = f"bg{s.id} 运行中 已运行{_fmt_dur(time.time() - s.started_at)}"
            elif s.status == STATUS_DONE:
                head = f"bg{s.id} 完成 exit={s.exit_code} ({time.strftime('%H:%M:%S', time.localtime(s.finished_at))})"
            elif s.status == STATUS_FAILED:
                head = f"bg{s.id} 失败 exit={s.exit_code} ({time.strftime('%H:%M:%S', time.localtime(s.finished_at))})"
            else:
                head = f"bg{s.id} 已取消"
            line = f"{head} \"{cmd}\" 输出{_fmt_bytes(s.total_bytes)}"
            if s.truncated:
                line += f"（超限截断，尾部见 {s.log_path}.tail）"
            line += f"\n    结果: {s.log_path}"
            lines.append(line)
        free_ids = [s.id for s in _slots if s.status != STATUS_RUNNING]
    if not lines:
        return "[后台任务] 当前无后台任务"
    free_note = f"空闲槽位: {', '.join('bg%d' % i for i in free_ids)}" if free_ids else ""
    return "[后台任务]\n" + "\n".join(lines) + (f"\n{free_note}" if free_note else "")


def snapshot() -> str:
    return _snapshot()


def wait_tasks(timeout: int) -> str:
    """等待任意任务完成：完成事件唤醒 / 超时 / 用户ESC 打断。

    返回内容即完整状态快照（含新完成事件），AI 醒来无需再查。
    ESC 只打断"等待"本身，后台任务不受影响继续运行。
    """
    from ..bash import BashRuntime  # 延迟导入：避免循环
    BashRuntime.interrupted = False  # 入口清零：上轮残留中断不污染本次等待
    with _lock:
        # clear 与采样原子化：任务完成要么发生在 clear 前（wait 开始前已终态，
        # 本就不在 running 列表），要么发生在采样后（事件置位可被 wait 捕获）
        _terminal_event.clear()
        before_running = [s.id for s in _slots if s.status == STATUS_RUNNING]
    if not before_running:
        return "[后台任务] 当前无运行中的后台任务\n" + _snapshot()
    deadline = time.time() + timeout
    new_terminal = []
    while time.time() < deadline:
        if BashRuntime.interrupted:
            BashRuntime.interrupted = False
            return "[后台任务] 等待已被用户打断（后台任务不受影响，继续运行）\n" + _snapshot()
        if _terminal_event.wait(timeout=0.1):
            break
    with _lock:
        new_terminal = [
            s for s in _slots
            if s.id in before_running
            and s.status in (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
        ]
    events = "；".join(
        f"bg{s.id} {'完成' if s.status == STATUS_DONE else '失败'} exit={s.exit_code}"
        for s in new_terminal
        if s.status in (STATUS_DONE, STATUS_FAILED)
    )
    if events:
        return f"[后台任务] {events}\n" + _snapshot()
    return f"[后台任务] 等待{timeout}秒超时，当前无新完成\n" + _snapshot()


def cancel_task(bg_id: int) -> str:
    """取消任务：杀进程树（后台线程），已产出内容保留在文件里仍可读。"""
    from ..bash import _kill_proc_tree  # 延迟导入：避免循环
    with _lock:
        slot = next((s for s in _slots if s.id == bg_id), None)
        if slot is None:
            return error_line(f"bg{bg_id} 编号无效（有效范围 bg1~bg{MAX_SLOTS}）")
        if not slot.status:
            return error_line(f"bg{bg_id} 空闲（无任务）")
        if slot.status != STATUS_RUNNING:
            return error_line(f"bg{bg_id} 已处于终态（{slot.status}），无需取消")
        slot.cancel_flag = True
        proc = slot.proc
        log_path = slot.log_path
    if proc is not None and proc.poll() is None:
        threading.Thread(target=_kill_proc_tree, args=(proc,), daemon=True).start()
    return (
        f"bg{bg_id} 已取消（进程树已终止，已产出内容保留在 {log_path} 仍可 Read 读取；"
        "管道中尚未收口的剩余输出随后补落盘）"
    )


def running_count() -> int:
    with _lock:
        return sum(1 for s in _slots if s.status == STATUS_RUNNING)


def running_summary() -> str:
    """运行中任务的短摘要（agent_loop 软提醒文案用）；无则返回空串。"""
    with _lock:
        rs = [s for s in _slots if s.status == STATUS_RUNNING]
        if not rs:
            return ""
        return "、".join(f"bg{s.id}({s.command[:40]})" for s in rs)


def cleanup_all() -> None:
    """会话结束硬兜底：杀全部 running 进程树 + 清目录 + 重建槽位表。幂等。"""
    from ..bash import _kill_proc_tree  # 延迟导入：避免循环
    global _slots
    procs = []
    with _lock:
        for s in _slots:
            if s.status == STATUS_RUNNING and s.proc is not None and s.proc.poll() is None:
                procs.append(s.proc)
            # 先关文件句柄：Windows 下句柄占用会导致 rmtree 删除失败
            if s.fh is not None:
                try:
                    s.fh.close()
                except Exception:
                    pass
        # 重建槽位表：旧 monitor 线程 finalize 写旧对象，不污染新表
        _slots = [_Slot(id=i) for i in range(1, MAX_SLOTS + 1)]
    for p in procs:
        threading.Thread(target=_kill_proc_tree, args=(p,), daemon=True).start()
    _wipe_dir()


def prepare() -> None:
    """会话开始预清：清空上次残留（异常退出场景），确保干净开局。"""
    _wipe_dir()


# ═══════════════════════════════════════════════════════════════
# Shell 工具的 bg 参数分发入口（bash.execute 转发调用）
# ═══════════════════════════════════════════════════════════════

def bg_execute(command, timeout, background, bg, task_id, tool_context) -> str:
    """bg 参数操作分发。

    Args:
        command: 命令（background=true 提交时使用）
        timeout: 已转 int 的秒数（bg=wait 时为最长等待秒数）
        background: true=提交后台任务
        bg: status / wait / cancel
        task_id: bg=cancel 时的任务编号
        tool_context: 工具上下文（wait 超时上限 clamp 用）
    """
    op = (bg or "").strip().lower()
    if background:
        if not command:
            return error_line("background=true 提交后台任务需提供 command")
        return submit(command)
    if op == "status":
        return snapshot()
    if op == "wait":
        t = timeout if timeout else 120
        try:
            t = int(t)
        except (TypeError, ValueError):
            return error_line("timeout需为整数")
        if t <= 0:
            return error_line("timeout需为正整数（秒）")
        if tool_context is not None and tool_context.max_timeout_seconds > 0:
            t = min(t, tool_context.max_timeout_seconds)
        return wait_tasks(t)
    if op == "cancel":
        if task_id is None:
            return error_line("cancel 需指定任务编号 id（如 id=3 取消 bg3）")
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return error_line(f"id 需为整数: {task_id}")
        return cancel_task(tid)
    return error_line(f"未知 bg 操作: {bg or ''}（可用: status / wait / cancel）")
