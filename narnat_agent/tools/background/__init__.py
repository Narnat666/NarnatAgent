"""后台任务管理 —— Shell 工具 bg 参数的后端（bash/__init__.py 分发调用）

8 固定槽位 bg1~bg8：一任务一文件 bgN.log（复用前旧结果先归档为
bgN.log.<seq>.prev，永不静默丢失），结果落在系统临时目录下本会话
专属子目录 narnat_bg_<pid>_<随机>（每进程唯一）：
- 不进用户工作目录：叉窗强杀残留也不会污染项目/桌面，仅占临时目录
- 每进程唯一：同项目多 agent、父子 agent 天然互不抢目录
- 陈旧清扫：进程启动时清理强杀残留（目录名里的 pid 已不存在 + 目录超过
  24 小时未动，两条同时满足才删——并行实例正在用的目录绝不动）
- 退出回收：正常退出删除本进程目录（SIGTERM/atexit + shutdown 收尾），
  被强杀收不到钩子时留给下次启动的陈旧清扫
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
- 进程退出收尾：shutdown_cleanup 同步终止本进程全部后台任务（先优雅
  terminate → 300ms 宽限 → 按 pid 强杀整棵树），再删本进程临时目录。
"""

import atexit
import codecs
import os
import shutil
import signal
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

BG_DIR_PREFIX = "narnat_bg_"       # 会话临时目录前缀（陈旧清扫按它识别自家目录）
BG_STALE_SECONDS = 24 * 3600       # 陈旧清扫保活期：目录超过此时间未动才可能被清
SHUTDOWN_GRACE = 0.3               # 退出时优雅终止的宽限（秒），超时即强杀进程树

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 结果根目录：首次使用时创建本会话专属临时目录（此后 AI 的 cd 不影响结果路径）
_base_dir: Optional[str] = None
_exit_hooks_done = False


def _root_dir() -> str:
    global _base_dir
    if _base_dir is None:
        # 目录名带 pid：崩溃残留可被后来者判定「创建者已不存在」后回收
        _base_dir = tempfile.mkdtemp(prefix=f"{BG_DIR_PREFIX}{os.getpid()}_")
        _register_exit_hooks()
        _sweep_stale()
    return _base_dir


def _bg_dir() -> str:
    return _root_dir()


def _register_exit_hooks() -> None:
    """注册退出清理钩子（幂等）：正常退出时回收后台任务 + 删本进程临时目录。

    覆盖解释器正常退出（含 SystemExit 后的收尾）与 SIGTERM；SIGTERM 钩子
    仅当仍是默认处理且当前在主线程时才装（signal 只允许主线程）。
    被 TerminateProcess 强杀时没有钩子可跑，残留交给下次启动的陈旧清扫。
    """
    global _exit_hooks_done
    if _exit_hooks_done:
        return
    _exit_hooks_done = True
    try:
        atexit.register(shutdown_cleanup)
    except Exception:
        pass
    try:
        if (sys.platform != "win32"
                and threading.current_thread() is threading.main_thread()
                and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL):
            signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass


def _on_sigterm(signum, _frame) -> None:
    """SIGTERM：清本进程临时目录后按默认语义终止（行为与未装钩子一致）。

    这里只删目录、不取槽位锁：信号处理可能打断持锁中的代码，同线程重入
    非重入锁会直接挂死进程——残留目录由下次启动的陈旧清扫兜底。
    """
    _wipe_dir()
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except Exception:
        pass


def _log_err(msg: str) -> None:
    """清理失败的 stderr 日志（用户排查用）。

    日志本身绝不允许再抛——清理失败的影响面必须止于「残留目录晚点回收」，
    连一行日志都打不出来时也不能打断启动流程。
    """
    try:
        print(f"[narnat] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _wipe_dir() -> None:
    """删除本进程的临时目录（幂等，失败记日志后忽略）。

    只删本进程创建的这一个目录：不创建（从未用过后台任务时无事可做）、
    不遍历临时目录、不碰其它实例的 narnat_bg_*。
    """
    d = _base_dir
    if not d:
        return
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        # 非 OSError（rmtree 内部逻辑异常等）也不会传出去：这里在 prepare()
        # 与退出钩子上都被调用，一抛就直接挡住后端启动/退出。
        _log_err(f"清理本进程临时目录失败（已忽略）: {type(e).__name__}: {e}")


def _pid_alive(pid: int) -> bool:
    """pid 对应进程是否仍存在。拿不到信息一律返回 True（宁可少删，不可误删）。"""
    if pid <= 0:
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _dir_pid(name: str) -> Optional[int]:
    """从目录名 narnat_bg_<pid>_<随机> 解析创建者 pid；解析不出返 None。"""
    token = name[len(BG_DIR_PREFIX):].split("_", 1)[0]
    try:
        return int(token)
    except ValueError:
        return None


def _sweep_stale() -> None:
    """清扫陈旧残留：临时目录下「创建者已不存在」且「超过保活期未动」的目录。

    删除判据两条必须同时满足（缺一不删）：
    1. 目录名里的 pid 已不存在（创建它的进程没了）；
    2. 目录 mtime 超过 BG_STALE_SECONDS（24 小时）。
    各层防线防误删（自上而下）：
    0. 名称不以 narnat_bg_ 开头 → 不是自家的，跳过（绝不删别的东西）；
    1. 目录 mtime 在保活期内（近期有提交/归档）→ 会话活跃，跳过；
    2. 目录内任一文件在保活期内（长跑任务只更新日志文件 mtime）→ 活跃，跳过；
    3. 判定过期后先原子改名再删：目录内若仍有活跃写入（打开的文件句柄），
       Windows 下改名必然失败 → 放弃删除；改名成功（真孤儿/纯旧日志）才删。
       Linux 无句柄锁（POSIX 语义），本防线不生效——尽力而为。
    幂等、异常一律吞掉并记日志：清扫失败绝不能挡住启动（本函数在
    prepare() 里被调用，异常一冒泡就到 agent.run() 的 try 之外，
    直接挡住后端启动），最坏后果只是旧日志延迟回收。
    """
    try:
        now = time.time()
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            if not name.startswith(BG_DIR_PREFIX):
                continue  # 防线0：不是自家的目录，永不触碰
            p = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(p) <= BG_STALE_SECONDS:
                    continue  # 防线1：目录近期有提交/归档
                pid = _dir_pid(name)
                if pid is not None and _pid_alive(pid):
                    continue  # 创建者还在跑（并行实例正在用），绝不动
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
            except Exception as e:
                # 单个目录出任何问题（含非 OSError）都只跳过它，不影响同轮其它目录
                _log_err(f"陈旧目录清扫失败（跳过 {name}）: "
                         f"{type(e).__name__}: {e}")
    except Exception as e:
        # 整轮失败（如临时目录不可读）：只记一行，绝不向上抛
        _log_err(f"陈旧目录清扫整轮失败（已忽略，不影响启动）: "
                 f"{type(e).__name__}: {e}")


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
_terminal_event = threading.Event()  # 任一任务进入终态 或 用户插话时置位（wait 依赖）
_insert_wake = threading.Event()     # 用户插话置位：只唤醒 wait，不杀任务
_archive_seq = 0                     # 归档文件序号（会话内递增，防同秒覆盖）


def _ensure_dir() -> str:
    """确保结果目录存在（临时目录下，无 git 提交风险，无需 .gitignore）"""
    d = _bg_dir()
    os.makedirs(d, exist_ok=True)
    return d


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
                raise OSError("未找到 bash/sh")
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
        f"结果: {slot.log_path}"
    )
    if archived:
        msg += f"\n提示: 已归档该槽位旧任务结果到 {archived}"
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


def wake_waiters() -> None:
    """用户插话：提前结束 wait_tasks 的等待（后台任务不受影响，仍在运行）

    为什么需要：AI 挂在一轮 wait 里时，用户插的话要等某个后台任务结束
    才被消费（长的要等满 timeout），界面上就是「已经发出去了，但没反应」。
    不能复用 BashRuntime.interrupted 走这条路——那条会把前台进程一起杀掉。
    """
    _insert_wake.set()


def wait_tasks(timeout: int) -> str:
    """等待任意任务完成：完成事件唤醒 / 超时 / 用户ESC 打断 / 用户插话唤醒。

    返回内容即完整状态快照（含新完成事件），AI 醒来无需再查。
    ESC 只打断"等待"本身，后台任务不受影响继续运行。
    用户插话（RPC 模式的 chat/insert）同样只提前结束等待：插话文本由
    agent_loop 的消费点追加进上下文，后台任务照旧运行。
    """
    from ..bash import BashRuntime  # 延迟导入：避免循环
    BashRuntime.interrupted = False  # 入口清零：上轮残留中断不污染本次等待
    with _lock:
        # clear 与采样原子化：任务完成要么发生在 clear 前（wait 开始前已终态，
        # 本就不在 running 列表），要么发生在采样后（事件置位可被 wait 捕获）
        _terminal_event.clear()
        # 插话标同样入口清零：否则上一次插话的置位会让每一次 wait 都立刻返回
        _insert_wake.clear()
        before_running = [s.id for s in _slots if s.status == STATUS_RUNNING]
    if not before_running:
        return "[后台任务] 当前无运行中的后台任务\n" + _snapshot()
    deadline = time.time() + timeout
    new_terminal = []
    woken_by_insert = False
    while time.time() < deadline:
        if BashRuntime.interrupted:
            BashRuntime.interrupted = False
            return "[后台任务] 等待已被用户打断（后台任务不受影响，继续运行）\n" + _snapshot()
        if _insert_wake.is_set():
            _insert_wake.clear()
            woken_by_insert = True
            break
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
    # 完成信息比「被插话唤醒」更有价值，故 events 优先；插话文本本身稍后会作为
    # user 消息进上下文，AI 不需要靠这句提示才知道用户说了话。
    if woken_by_insert:
        return "[后台任务] 等待因用户插入新消息而提前结束（后台任务未受影响，仍在运行）\n" + _snapshot()
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
    return f"bg{bg_id} 已取消（进程树已终止）。结果: {log_path}"


def cancel_all() -> int:
    """取消所有运行中的后台任务，返回真正发过终止的槽位数。

    用户点「停止」时用：那一格 UI 上写的是「已打断」，如果背后还有
    agent 自己派出去的后台进程在跑，它既继续烧钱、也继续往工作区写文件，
    而界面已经告诉用户"停了"——这是最不能接受的一种不一致。
    """
    from ..bash import _kill_proc_tree  # 延迟导入：避免循环
    victims = []
    with _lock:
        for slot in _slots:
            if slot.status != STATUS_RUNNING:
                continue
            slot.cancel_flag = True
            victims.append((slot.id, slot.proc, slot.log_path))
    killed = 0
    for _bg_id, proc, _log_path in victims:
        if proc is not None and proc.poll() is None:
            threading.Thread(target=_kill_proc_tree, args=(proc,), daemon=True).start()
            killed += 1
        else:
            killed += 1        # 已在收尾也算「已处理」
    return killed


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
    """会话开始预清：清空上次残留（异常退出场景），确保干净开局。

    清完立刻重建本进程目录：目录从会话一开始就存在（后台日志与退出回收
    的落点固定），不必等第一次提交。
    """
    _wipe_dir()
    _ensure_dir()


def _win_descendants(root_pid: int) -> List[int]:
    """Windows：快照里 root_pid 的全部后代 pid（不含自身，父可已退出）。

    优雅 terminate 只结束直接子进程（cmd.exe），孙进程会变成「父 pid 已
    不存在」的孤儿——此后 taskkill /T 再也枚举不到它们。故强杀前先按
    Toolhelp 快照的父子链把整棵树找出来。快照里的 ppid 是创建时记下的
    值，父进程已退出也能对上。
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        TH32CS_SNAPPROCESS = 0x00000002
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        parent_of = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parent_of[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        out: List[int] = []
        frontier = [int(root_pid)]
        while frontier:
            cur = frontier.pop()
            for pid, ppid in parent_of.items():
                if ppid == cur and pid not in out:
                    out.append(pid)
                    frontier.append(pid)
        return out
    except Exception:
        return []


def _taskkill_pids(pids: List[int]) -> None:
    """Windows：一次 taskkill /F 批量终止（按 pid，绝不按进程名）。"""
    args = ["taskkill", "/F"]
    for p in pids:
        args += ["/PID", str(int(p))]
    try:
        subprocess.run(args, capture_output=True, timeout=10)
    except Exception:
        pass


def _force_kill_tree(root_pids: List[int]) -> None:
    """强杀进程树（只按 pid）。Windows 后代先进名单（先子后父），POSIX 用进程组。"""
    roots = [int(p) for p in root_pids if p]
    if not roots:
        return
    if sys.platform == "win32":
        pids: List[int] = []
        for root in roots:
            for child in _win_descendants(root):
                if child not in pids:
                    pids.append(child)
        pids += [p for p in roots if p not in pids]
        _taskkill_pids(pids)
        return
    for root in roots:
        try:
            # 提交时 start_new_session：子进程自成进程组，killpg 即整棵树
            os.killpg(os.getpgid(root), signal.SIGKILL)
            continue
        except (ProcessLookupError, OSError):
            pass
        try:
            os.kill(root, signal.SIGKILL)
        except OSError:
            pass


def shutdown_cleanup(grace: float = SHUTDOWN_GRACE) -> int:
    """进程退出前收尾：终止本进程全部后台任务 + 删本进程临时目录。

    先优雅（terminate 直接子进程）→ 宽限 grace → 按 pid 强杀整棵进程树。
    只动 _slots 进程表里由本进程 Popen 出来的子进程（及其后代），绝不按
    名字杀——同机并行会话的后端与它们提交的任务一律不受影响。
    与 cancel_all 的区别：cancel_all 是「取消」语义、发完就走（后台线程）；
    这里是退出前的同步收尾，必须确保子进程真的没了——父进程一退，它们
    就成了 ppid 指向空号的孤儿（实测后端退出后仍有 cmd.exe 在跑）。

    返回强制终止的后台任务数（供退出 stderr 行使用）。
    """
    with _lock:
        tasks = [(s.proc, s.proc.pid) for s in _slots if s.proc is not None]
        for s in _slots:
            if s.status == STATUS_RUNNING:
                s.cancel_flag = True
    # 只处理仍在跑的：已终态的任务早就结束了，不该计入「强制终止」
    alive = [(proc, pid) for proc, pid in tasks if proc.poll() is None]
    for proc, _pid in alive:
        try:
            proc.terminate()  # 优雅：先给它一个正常结束的机会
        except Exception:
            pass
    if alive:
        deadline = time.time() + max(0.0, grace)
        while time.time() < deadline and any(proc.poll() is None for proc, _p in alive):
            time.sleep(0.02)
        _force_kill_tree([pid for _proc, pid in alive])
    # 句柄不关 → Windows 下 rmtree 删不掉本进程目录
    with _lock:
        for s in _slots:
            if s.fh is not None:
                try:
                    s.fh.close()
                except Exception:
                    pass
                s.fh = None
    _wipe_dir()
    return len(alive)


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
        # None（未传）走默认 120s；0/负数显式传参交给下方 <=0 校验报错——
        # 此前 "timeout if timeout else 120" 把 0 静默换成 120 挂起等待，
        # 与前台 Shell 对 timeout<=0 的报错语义不一致（AI 传 0 会意外卡住）
        t = 120 if timeout is None else timeout
        try:
            t = int(t)
        except (TypeError, ValueError, OverflowError):
            return error_line("timeout需为整数")
        if t <= 0:
            return error_line("timeout需为正整数（秒）")
        if tool_context is not None and tool_context.max_timeout_seconds > 0:
            t = min(t, tool_context.max_timeout_seconds)
        return wait_tasks(t)
    if op == "cancel":
        if task_id is None:
            return error_line("cancel 需指定任务编号 id")
        try:
            tid = int(task_id)
        except (TypeError, ValueError, OverflowError):
            return error_line(f"id 需为整数: {task_id}")
        return cancel_task(tid)
    return error_line(f"未知 bg 操作: {bg or ''}（可用: status / wait / cancel）")
