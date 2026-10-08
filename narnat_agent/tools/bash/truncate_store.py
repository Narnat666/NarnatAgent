"""Shell 截断输出落盘 —— 截断发生时把完整输出写入临时文件，返回路径供 AI 查询

背景：Shell 输出超过 max_output_chars 时只保留首尾、丢弃中段，AI 想查完整
输出只能重跑命令或换小输出命令（代价高）。本模块在截断发生时把完整输出
落盘，返回文件绝对路径，读写交给 AI 现有的 Read/Grep/Glob 工具。

目录隔离（对齐 background 模块踩过的坑，机制独立、前缀独立）：
- 每进程唯一：首次使用时 tempfile.mkdtemp 建本进程专属目录 narnat_trunc_<随机>。
  父代理与 nn 子代理是独立进程（cwd 相同、环境变量还可能被继承），目录天然不同；
  历史事故（sessions/子代理防御测试与日志丢失）：nn 子代理结束清理时把主会话
  共享 cwd/.background 里的后台日志全部清掉，先完成者的结果无法取回。
- fork 防护：缓存目录时一并记录 pid；pid 变化（Unix fork 后子进程继承父进程
  内存态，会错认父目录）时重建目录，绝不复用他人目录。
- 过期清扫：首次使用时清理保活期外未动过的自家残留目录（三道防线同
  background._sweep_stale）。本模块没有"会话结束清理"钩子（不改 core），
  故另设目录字节滚动上限，防长会话在临时目录堆积。

命名与内容：
- 文件 trunc_<HHMMSS>_<seq>.log：seq 进程内自增（itertools.count，线程安全），
  同秒多次截断也不互相覆盖；目录名已标明来源，无需文件内头部注释。
- 内容 = 完整输出逐字（UTF-8、不转换行、无头部）：Read 行数与命令输出行数一致，
  AI 可对照命令真实输出定位行，中间被截断的部分也能在文件里检索到。

失败降级：任何落盘异常（磁盘满/权限/路径过长）一律吞掉返回空串，调用方退回
原截断文案——绝不因写日志失败影响命令返回。
"""

import itertools
import os
import shutil
import tempfile
import threading
import time

# ── 常量 ──
DIR_PREFIX = "narnat_trunc_"           # 会话临时目录前缀（过期清扫按它识别自家目录）
STALE_SECONDS = 7 * 24 * 3600          # 残留保活期：目录超过此时间未动即清扫
MAX_STORE_BYTES = 50 * 1024 * 1024     # 单次落盘上限（输出超过则放弃落盘，退回原提示）
MAX_DIR_BYTES = 512 * 1024 * 1024      # 目录总字节上限，超出按 mtime 删最旧文件

# 本进程专属目录（首次使用时创建）与其创建者 pid（fork 防护）
_base_dir = None
_base_pid = -1
_seq = itertools.count(1)              # 文件序号（next() 原子，多线程安全）
_lock = threading.Lock()


def _root_dir() -> str:
    """本进程专属结果目录：保证同一进程稳定、不同进程（含 fork）不同。

    目录被外部清理（清 C 盘/清临时文件）时自动重建：缓存目录已不存在
    时重新 mkdtemp——否则本进程内落盘能力永久失效（提示误导为
    "增大 max_output_chars"而不自知目录已没）。
    """
    global _base_dir, _base_pid
    pid = os.getpid()
    if _base_dir is not None and _base_pid == pid and os.path.isdir(_base_dir):
        return _base_dir
    with _lock:
        if _base_dir is None or _base_pid != pid or not os.path.isdir(_base_dir):
            _base_dir = tempfile.mkdtemp(prefix=DIR_PREFIX)
            _base_pid = pid
            _sweep_stale()
    return _base_dir


def _sweep_stale() -> None:
    """清扫历史残留：临时目录下保活期外未动过的 narnat_trunc_* 目录。

    三道防线（沿用 background 模块设计）：目录 mtime 在保活期内→跳过；
    目录内任一文件在保活期内→跳过；判定过期后先原子改名再删，Windows 下
    目录内仍有打开句柄时改名失败→放弃删除。任何失败一律放弃，最坏后果
    仅是旧日志延迟回收。
    """
    try:
        now = time.time()
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            if not name.startswith(DIR_PREFIX):
                continue
            p = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(p) <= STALE_SECONDS:
                    continue
                if _has_fresh_file(p, now):
                    continue
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
    """目录内是否存在保活期内的文件（近期有截断落盘的目录视为活跃）"""
    try:
        entries = os.listdir(p)
    except OSError:
        return False
    for f in entries:
        try:
            if now - os.path.getmtime(os.path.join(p, f)) <= STALE_SECONDS:
                return True
        except OSError:
            continue
    return False


def _rotate_dir(d: str) -> None:
    """目录总字节超限时删最旧文件（至少保留最新一个）。"""
    try:
        files = []
        total = 0
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= MAX_DIR_BYTES:
            return
        files.sort()
        for _, size, p in files[:-1]:
            if total <= MAX_DIR_BYTES:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
    except OSError:
        pass


def store_output(text: str) -> str:
    """完整输出落盘，返回绝对路径；任何失败返回空串（调用方退回原提示）。"""
    try:
        data = text.encode("utf-8")
        if len(data) > MAX_STORE_BYTES:
            return ""
        d = _root_dir()
        path = os.path.join(
            d, f"trunc_{time.strftime('%H%M%S')}_{next(_seq)}.log"
        )
        with open(path, "wb") as fh:
            fh.write(data)
        _rotate_dir(d)
        return path
    except (OSError, ValueError):
        return ""
