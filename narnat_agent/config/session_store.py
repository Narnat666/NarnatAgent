"""
会话持久化 —— 序列化/反序列化messages，供commands/调用
"""

import itertools
import json
import os
import shutil
import threading
import time
from typing import List, Dict, Any, Optional


def _strip_surrogates(obj):
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_surrogates(v) for v in obj]
    return obj

from .defaults import DATA_SUBDIR, SESSIONS_SUBDIR


def _safe_timestamp(value) -> float:
    """会话 timestamp 类型校验：非数值（字符串/None/对象）一律视作 0。

    手工编辑/半写文件会把 timestamp 写成字符串或 null，此前会让排序比较
    抛 TypeError（str 与 float 不可比），一条坏文件瘫痪整个会话列表。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _format_ts(ts, fmt: str) -> str:
    """时间格式化（安全版）：越界时间戳（如 1e18）会让 time.localtime 抛
    OSError/OverflowError，降级为占位符，避免一条坏文件让整个列表渲染崩溃。
    """
    try:
        return time.strftime(fmt, time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "未知时间"


# 进程内单调计数器：给临时文件名提供「任意两次调用都不同」的保证。
_tmp_seq = itertools.count(1)


def _unique_tmp_path(path: str) -> str:
    """为 path 生成一个**进程内唯一 + 线程内唯一**的临时文件路径。

    只带 pid 是不够的（实测 P0）：同一进程里轮末后台自动保存线程的 _persist
    与紧随其后的 /save 各写一次同一会话，两个线程抢同一个 `.<pid>.tmp`，
    互相截断后把原会话文件写成了非法 JSON（2/20），会话从列表里消失。
    这里三重标识：pid（跨进程）+ 线程标识（同进程不同线程）+ 进程内计数器
    （同一线程的任意两次调用，且线程标识在线程退出后可能被复用）。
    """
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{next(_tmp_seq)}.tmp"


# Windows 保留文件名：带扩展名也一样建不出来（`CON.json` 仍是 CON），
# 只能统一拒绝，否则用户拿到的是带绝对路径的系统级错误，看不懂也改不掉。
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def validate_session_name(name: str) -> str:
    """会话名合法性校验。返回 ""=通过，非空=可读原因（终端风格）。

    为什么必须在入口拦、而不是靠 _safe_filename 兜底：文件名里带孤立代理
    字符时 open()/encode 抛 UnicodeEncodeError，带 NUL 时抛 ValueError
    （embedded null byte）——两者都不是 OSError，会穿透 save/rename 的
    异常处理变成未捕获异常，并在磁盘上留下删不掉的 *.tmp 残骸（实测）。
    控制字符还会让名字在终端/抽屉里吞掉后面的内容，用户看不出名字里有什么。
    """
    if not isinstance(name, str):
        return "[错误: 会话名必须是文本]"
    # 孤立代理字符：U+D800–U+DFFF 中不成对出现的（合法代理对由两个码元
    # 组成，UTF-8 编码/文件名都没问题，不能误拦）
    i, n = 0, len(name)
    while i < n:
        o = ord(name[i])
        if 0xD800 <= o <= 0xDBFF:
            if i + 1 < n and 0xDC00 <= ord(name[i + 1]) <= 0xDFFF:
                i += 2
                continue
            return ("[错误: 会话名包含孤立的代理字符（U+D800–U+DFFF），"
                    "文件名无法编码，请换一个名称]")
        if 0xDC00 <= o <= 0xDFFF:
            return ("[错误: 会话名包含孤立的代理字符（U+D800–U+DFFF），"
                    "文件名无法编码，请换一个名称]")
        if o < 0x20 or 0x7F <= o <= 0x9F:
            return (f"[错误: 会话名不能包含控制字符（U+{o:04X}），"
                    "请换一个名称]")
        i += 1
    # 按层级逐段查保留名：'a/CON' 与 'CON' 一样建不出文件
    for part in name.split("/"):
        stem = part.split(".")[0].strip().upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            return (f"[错误: 会话名 '{part}' 是 Windows 保留名称"
                    "（CON/PRN/AUX/NUL/COM1-9/LPT1-9），请换一个名称]")
    return ""


def _safe_filename(name: str) -> str:
    safe_name = name.replace("/", "_").replace("\\", "_")
    safe_name = safe_name.replace(":", "_").replace("<", "_").replace(">", "_")
    safe_name = safe_name.replace("|", "_").replace("?", "_").replace("*", "_")
    # 双引号也是 Windows 文件名非法字符，漏了它会得到
    # 「[Errno 22] Invalid argument: 'C:\\...\\"周会记录".json.tmp'」这种
    # 带完整磁盘路径的系统错误。`/save "周会记录"` 是常见的带引号命名写法，
    # 实测必失败且用户看不懂错误原因。与上面几个字符同等对待即可。
    safe_name = safe_name.replace('"', "_")
    safe_name = safe_name.replace("..", "_")
    if not safe_name:
        safe_name = "unnamed"
    return safe_name


def _sessions_dir(narnat_dir: str) -> str:
    d = os.path.join(narnat_dir, DATA_SUBDIR, SESSIONS_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # 目录不可用（被同名文件占位/权限拒绝/磁盘满）：此处不抛——调用方按
        # 各自错误路径自然降级（保存返回可读错误、列举为空），避免一次目录
        # 异常把整个进程带崩
        pass
    return d


def file_signature(path: str) -> str:
    """会话文件指纹：(size, mtime_ns)。返回 ""=文件不存在。

    全仓**唯一**的指纹实现（session_callbacks 把它作为模块级函数对外暴露）：
    写入前的冲突检测、写完后的签名更新必须同一口径，各写一套会把自己刚写的
    东西判成「被外部改动」，于是每轮都另存一个新文件。
    用 (size, mtime_ns) 而不是内容 sha256：只需一次 stat（sha256 要把整份会话
    读一遍，大会话每轮多读几百 KB），且 os.replace 后 mtime 必然变化；
    同 size 同 mtime_ns 而内容不同的情况在 100ns 分辨率下不会出现。
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def session_signature(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    """按会话名取指纹（会话状态对象用它记住「上次读/写该文件时的指纹」）"""
    return file_signature(_session_path(narnat_dir, name, parent=parent))


def _load_json_with_shared_retry(path: str, attempts: int = 3):
    """读取会话 JSON：对共享冲突（并发写方 os.replace 的毫秒窗口）短退避重试。

    读端 open 在替换瞬间可能瞬时 Errno 13 / 文件暂时不可见（多进程或后台
    自动保存线程与前台命令并发时实测存在的瞬态失败）；重试后基本消除。
    重试耗尽仍失败时抛出最后一次 OSError，由调用方按既有语义处理
    （列表跳过 / 加载报错）。解码类异常（JSON 损坏等）立即抛出，不重试。
    """
    last_err = None
    for i in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except OSError as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.01 * (i + 1))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise
    raise last_err


def _session_path(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    safe_name = _safe_filename(name)
    if parent:
        safe_parent = _safe_filename(parent)
        child_dir = os.path.join(_sessions_dir(narnat_dir), safe_parent)
        try:
            os.makedirs(child_dir, exist_ok=True)
        except OSError:
            pass
        return os.path.join(child_dir, f"{safe_name}.json")
    return os.path.join(_sessions_dir(narnat_dir), f"{safe_name}.json")


def session_exists(narnat_dir: str, name: str, parent: Optional[str] = None) -> bool:
    return os.path.isfile(_session_path(narnat_dir, name, parent=parent))


def rename_session(narnat_dir: str, old_name: str, new_name: str,
                   parent: Optional[str] = None) -> str:
    """改会话名。返回 ""=成功，非空=错误信息。

    做法是「读旧文件 → 写新文件 → 删旧文件」而不是 os.rename：
    文件名经过 _safe_filename 归一化（特殊字符替换），直接改名会让
    文件里的 "name" 字段与文件名不一致，之后按名查找会错位。

    子会话改名只动它自己那个文件；根会话改名要连它的子会话目录一起搬，
    否则子会话会留在旧父名下成为孤儿。
    """
    old_path = _session_path(narnat_dir, old_name, parent=parent)
    if not os.path.isfile(old_path):
        return f"会话不存在: {old_name}"
    if old_name == new_name:
        return ""
    if session_exists(narnat_dir, new_name, parent=parent):
        return f"已有同名会话: {new_name}"

    sessions_root = _sessions_dir(narnat_dir)
    old_dir = os.path.join(sessions_root, _safe_filename(old_name))
    new_dir = os.path.join(sessions_root, _safe_filename(new_name))
    # 子会话目录的重名必须在**写文件之前**查。
    # 放到后面查会怎样（实测）：新 json 已写、旧 json 已删，这时才报
    # 「已有同名子会话目录」——用户看到失败，实际名字已经改了，
    # 子会话还留在旧目录里成了孤儿。失败要么什么都没发生，要么别报失败。
    if not parent and os.path.normpath(new_dir) != os.path.normpath(sessions_root):
        if os.path.isdir(new_dir):
            if os.listdir(new_dir):
                return f"已有同名会话: {new_name}"
            # 只剩空目录的残骸：清掉即可，否则换个名字都换不成
            try:
                os.rmdir(new_dir)
            except OSError as e:
                return f"目标名下有残留目录，无法重命名: {e}"

    try:
        with open(old_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
        return f"会话文件损坏，无法重命名: {old_name}"
    if not isinstance(data, dict):
        return f"会话文件格式异常，无法重命名: {old_name}"

    data["name"] = new_name
    new_path = _session_path(narnat_dir, new_name, parent=parent)
    # 先落新文件再删旧文件：中途失败只会多一个副本，不会两边都没有。
    # 新文件的创建必须用 O_EXCL 独占：两个进程同时把不同会话改名成同一个
    # 目标名时，只靠上面的 session_exists 检查两边都会通过，然后互相覆盖，
    # 先写那一整个会话静默消失（实测 40 轮里 20 次双成功）。
    try:
        fd = os.open(new_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return f"已有同名会话: {new_name}"
    except (OSError, ValueError) as e:
        # ValueError：目标名含无法编码的字符（UnicodeEncodeError 是其子类）
        # 或 NUL，同样收敛成可读失败而不是冒泡成未捕获异常
        return f"写入新会话失败: {e}"
    tmp = _unique_tmp_path(new_path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, new_path)
    except (OSError, ValueError) as e:
        # 占位文件要清掉，否则留一个空会话在列表里；临时文件也要清掉，
        # 否则会话库根上多一个谁也删不掉的 `*.tmp` 残骸（孤立代理字符/
        # 控制字符进文件名时 open 抛 UnicodeEncodeError/ValueError，
        # 不是 OSError，此前直接穿透成未捕获异常，tmp 就留在磁盘上了）。
        # 清理本身也要容忍 ValueError：残骸的文件名里可能正带着那个坏字符。
        for stale in (new_path, tmp):
            try:
                os.remove(stale)
            except (OSError, ValueError):
                pass
        return f"写入新会话失败: {e}"
    # 删旧文件走同一套重试：两个窗口同时改名时，一方可能正好撞上对方
    # 刚做的 replace，Windows 报 WinError 5（拒绝访问），裸 os.remove
    # 会把「删除旧会话失败: [WinError 5] …绝对路径」糊到界面上（实测）。
    err_del = _remove_file_with_retry(old_path)
    if err_del:
        return f"会话已改名为「{new_name}」，但旧文件没能删除（{err_del}）。请手动删除后再试"

    # 根会话改名：子会话目录跟着搬（重名已在上面查过）
    if not parent:
        if (os.path.normpath(old_dir) != os.path.normpath(sessions_root)
                and os.path.isdir(old_dir)):
            try:
                os.rename(old_dir, new_dir)
            except OSError as e:
                # 到这一步主文件已经改名成功了（新 json 已落盘、旧 json 已删），
                # 报「失败」会让用户以为没改成——实际名字变了、子会话却留在旧目录，
                # 于是列表里长出一个幽灵父会话（实测）。如实说明「部分完成」。
                return (f"会话已改名为「{new_name}」，但它的子会话目录没能一起搬"
                        f"（{type(e).__name__}）。请手动把 {old_dir} 改成 {new_dir}"
                        f"，或删除该会话后重新创建")
            # 子会话文件里有 "parent" 字段，必须一起改写：
            # 不改会在列表里挂出一个「幽灵父会话」（旧名，0 条消息），
            # 点进去还会报「会话不存在」。
            _rewrite_child_parent(new_dir, new_name)
    return ""


def _rewrite_child_parent(child_dir: str, new_parent: str) -> None:
    """把目录下所有子会话文件的 parent 字段改成 new_parent（尽力而为，不抛错）。"""
    try:
        names = os.listdir(child_dir)
    except OSError:
        return
    for fn in names:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(child_dir, fn)
        # 在 try 之前算好，except 里才能引用（读文件阶段抛异常时 tmp 尚未赋值，
        # 放在 try 内会在清理分支里变成 UnboundLocalError 穿透出去）
        tmp = _unique_tmp_path(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("parent") == new_parent:
                continue
            data["parent"] = new_parent
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except (OSError, ValueError, RecursionError):
            # 单个子会话改不动不影响改名本身：列表里最多少一个子项。
            # 临时文件要一并清掉，否则会在会话目录里留下 .tmp 残骸
            try:
                os.remove(tmp)
            except (OSError, ValueError):
                pass
            continue


def _claim_session_name(path: str) -> bool:
    """用 O_CREAT|O_EXCL 原子声明「这个会话文件归我」。返回 False=已被别人占住。

    「先查后写」在两个进程之间不原子：两个实例给同一段对话起了同一个名字时，
    两边的存在性检查都会通过，后写的一方把先写的整段内容覆盖掉（P0-2，实测
    1/3 概率整段消失）。O_EXCL 把「占名字」变成内核级原子操作，占不到的一方
    去拿下一个候选名（X (2)…）。建出来的是空占位文件，随后被 os.replace 原子
    替换成真正的 JSON；失败路径会清掉，不会留下 0 字节残骸。
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except (OSError, ValueError):
        # 建不出来（权限/名称非法）：不在这里报，交给随后的写盘给可读错误
        return True
    try:
        os.close(fd)
    except OSError:
        pass
    return True


def _claim_free_name(narnat_dir: str, base: str,
                     parent: Optional[str] = None) -> tuple:
    """挑一个没被占住的会话名并原子占位。返回 (name, path)，无候选时 (None, None)。

    每轮都拿**原始 base** 重新算候选名：占位失败说明该名字刚刚被别人建出来了
    （候选名已被占），unique_session_name 于是给出下一个序号 (2)/(3)…；
    用它已返回的名字再当 base 会得到 "X (2) (2)" 这种叠罗汉。
    """
    # 撞名兜底只有一份实现（session_callbacks.unique_session_name），这里按需
    # 导入：session_callbacks 在模块级 import 本模块，顶层反向 import 会成环。
    from ..core.session_callbacks import unique_session_name

    for _ in range(8):
        name = unique_session_name(narnat_dir, base, parent=parent)
        path = _session_path(narnat_dir, name, parent=parent)
        if _claim_session_name(path):
            return name, path
    return None, None


def _write_session_json(path: str, data: Dict[str, Any],
                        expect_sig: Optional[str] = None) -> tuple:
    """把 data 原子写进 path。返回 (err, raced)。

    raced=True 表示「落盘的最后一刻发现文件刚被别人改过」：本次 tmp 已丢弃、
    目标文件一个字节都没动，调用方应改落一个新名字。
    expect_sig 非 None 时把校验放到 os.replace 之前一步：这样「检查 → 替换」
    之间只剩一次 stat 与一次 replace（微秒级），跨进程窗口从「整个 tmp 写盘
    时长」（大会话可达几十毫秒）压到几乎不可命中——否则两个实例同轮写同一
    会话时仍可能双双通过检查、后写者覆盖先写者。
    """
    # 临时文件名必须**进程内唯一 + 线程内唯一**：同名会话被多线程并发保存时
    # （轮末后台自动保存与紧随其后的 /save 撞在同一个会话上），固定名或只带
    # pid 的名字会让两个线程抢同一个临时文件，互相截断后把会话文件写成非法
    # JSON（P0，实测 2/20）。各自写自己的临时文件后 os.replace 原子替换，
    # 语义不变：最终文件必然是一方的完整内容。
    tmp_path = _unique_tmp_path(path)
    last_err = None
    # os.replace 仍可能与另一进程的替换瞬间撞车（共享冲突）→ 短暂退避重试
    for attempt in range(3):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_strip_surrogates(data), f, ensure_ascii=False, indent=2)
            if expect_sig is not None and file_signature(path) != expect_sig:
                # 写 tmp 期间被别人改过：不覆盖，交给调用方另存
                try:
                    os.remove(tmp_path)
                except (OSError, ValueError):
                    pass
                return "", True
            os.replace(tmp_path, path)
            return "", False
        except UnicodeEncodeError as e:
            # 编码失败与并发无关，重试无意义
            last_err = e
            break
        except ValueError as e:
            # 文件名字符无法编码/含 NUL（embedded null byte）等：同样与并发
            # 无关，重试无意义；必须落到下面的清理分支，别留 .tmp 残骸
            last_err = e
            break
        except RecursionError as e:
            # 深嵌套 messages（实测约 400 层即触发）在 _strip_surrogates/
            # json.dump 递归时抛异常：load 端已按"损坏文件"防御，save 端
            # 同样收敛为可读失败（不冒泡到主循环）
            last_err = e
            break
        except OSError as e:
            last_err = e
            time.sleep(0.02 * (attempt + 1))
    # 失败路径清掉本次的临时文件，不留残骸（成功路径已被 os.replace 消费）。
    # 容忍 ValueError：临时文件名里可能正带着无法编码的字符
    try:
        os.remove(tmp_path)
    except (OSError, ValueError):
        pass
    return f"保存失败: {last_err}", False


def save_session(narnat_dir: str, name: str,
                 messages: List[Dict[str, Any]],
                 parent: Optional[str] = None,
                 status: str = "active",
                 summary: Optional[str] = None,
                 parent_msg_count: Optional[int] = None,
                 last_summarized_at: Optional[int] = None,
                 expected_signature: Optional[str] = None,
                 result: Optional[Dict[str, Any]] = None) -> str:
    """保存会话。返回 ""=成功，非空=可读失败原因。

    expected_signature 是「写入前冲突检测」的开关（指纹见 file_signature）：
        None → 不检测，按名覆盖（旧语义；/done 合并回父会话等有意覆盖的路径）
        ""   → 期望文件还不存在（首次保存 / 自动命名后新建），用 O_EXCL 原子占位
        非空 → 期望磁盘上就是这份指纹
    与磁盘实际不一致，说明有另一个实例改过这个文件（两个窗口切到同一会话各聊
    各的）：这时按名覆盖会让对方那一整段内容静默消失，且双方都收到成功回执
    （P0-1，实测 3/3 必现）。所以这里不覆盖，改用 unique_session_name 另存成
    "X (2)" 之类的新名字，并把实际落点回报给调用方去告知用户。

    result 是可选出参，成功时填入 {"name": 实际写入的名字,
                                   "conflict": 是否发生了冲突另存,
                                   "signature": 写入后的文件指纹}。
    """
    path = _session_path(narnat_dir, name, parent=parent)
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
            existing = {}
        # 顶层非对象（手工编辑/半写文件）视作无既有元数据，不能直接 .get()
        if not isinstance(existing, dict):
            existing = {}
    # 归一化碰撞检测：特殊字符被替换后映射到同一文件，静默覆盖会清掉另一会话。
    # 判空用 is not None：空串 name（命名失败写入的文件）也是"已有名称"，
    # falsy 短路会让它被 "unnamed" 静默覆盖（_safe_filename 把两者映射到同一
    # 文件）。老格式（无 name 键）保持放行升级（get 返回 None）。
    if existing.get("name") is not None and existing["name"] != name:
        return (f"保存失败: 会话名 '{name}' 与已有会话 '{existing['name']}' 冲突"
                f"（名称中的 / \\ : < > | ? * 等字符在存储时会被替换，导致两者映射到同一文件），请换一个名称")
    data = {
        "name": name,
        "timestamp": time.time(),
        "messages": messages,
        "parent": parent,
        "status": status,
        "summary": summary,
        "parent_msg_count": parent_msg_count if parent_msg_count is not None else existing.get("parent_msg_count"),
        "last_summarized_at": last_summarized_at if last_summarized_at is not None else existing.get("last_summarized_at"),
    }
    # ── 写入前冲突检测 + 落点选择 ──
    write_name, write_path, conflict, placeholder = name, path, False, False
    expect_sig: Optional[str] = None
    if expected_signature is not None:
        # expect_sig = 「这次替换前，磁盘上应当就是这个指纹」
        if expected_signature and file_signature(path) == expected_signature:
            expect_sig = expected_signature
        else:
            # 期望「还不存在」或「就是这个指纹」，实际不符：这个文件已经被别的
            # 实例动过（另一个窗口也在这个会话里写），绝不能按名覆盖
            conflict = file_signature(path) != ""
            write_name, write_path = _claim_free_name(narnat_dir, name, parent)
            if write_name is None:
                return f"保存失败: 会话名 '{name}' 附近的名字都被占用，请换一个名称后重试"
            placeholder = True
            # 只在真的换了名字时才算冲突（首次保存占到原名不算）
            conflict = conflict or write_name != name
            data["name"] = write_name
            # 占位文件是自己刚建的，它的指纹就是这次替换的期望值
            expect_sig = file_signature(write_path)

    err = ""
    for _attempt in range(3):
        err, raced = _write_session_json(write_path, data, expect_sig)
        if err or not raced:
            break
        # 写 tmp 期间文件又被别人改过：本次一个字节都没落，改落一个新名字
        conflict = True
        if placeholder:
            # 放弃上一个占位文件，否则留下 0 字节残骸占着这个名字
            try:
                os.remove(write_path)
            except (OSError, ValueError):
                pass
        write_name, write_path = _claim_free_name(narnat_dir, name, parent)
        if write_name is None:
            return f"保存失败: 会话名 '{name}' 附近的名字都被占用，请换一个名称后重试"
        placeholder = True
        data["name"] = write_name
        expect_sig = file_signature(write_path)

    if err:
        if placeholder:
            # 占位文件必须清掉：留着就是一个列表里看不见、名字却被永久占住的残骸
            try:
                os.remove(write_path)
            except (OSError, ValueError):
                pass
        return err
    if result is not None:
        result.clear()
        result.update({"name": write_name, "conflict": conflict,
                       "signature": file_signature(write_path)})
    return ""


def load_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> tuple:
    path = _session_path(narnat_dir, name, parent=parent)
    if not os.path.isfile(path):
        return [], f"会话不存在: {name}"
    try:
        data = _load_json_with_shared_retry(path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError) as e:
        # RecursionError：json.load 对超深嵌套（约1000层起）抛递归异常，属"文件损坏"
        return [], f"加载失败: {e}"
    # 字段类型校验：手工编辑/半写文件可能造成顶层非对象或 messages 非列表
    if not isinstance(data, dict):
        return [], f"加载失败: 会话文件格式非法（顶层应为对象）: {name}"
    messages = data.get("messages")
    if not isinstance(messages, list):
        return [], f"加载失败: 会话文件 messages 字段非法（应为数组）: {name}"
    return messages, ""


def list_sessions(narnat_dir: str) -> List[Dict[str, Any]]:
    sdir = _sessions_dir(narnat_dir)
    result = []
    try:
        entries = os.listdir(sdir)
    except OSError:
        return result   # 会话库目录不可用：按空列表降级（与"无会话"同观感）
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(sdir, fname)
        try:
            data = _load_json_with_shared_retry(fpath)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
            # 损坏文件（含超深嵌套）一律跳过，不影响其余会话
            continue
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        result.append({
            "name": data.get("name", fname[:-5]),
            "timestamp": _safe_timestamp(data.get("timestamp", 0)),
            "message_count": len(messages) if isinstance(messages, list) else 0,
        })
    result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return result


def _remove_file_with_retry(path: str) -> str:
    """删一个文件，把并发与占用两种情况都处理好。返回 ""=成功。

    - FileNotFoundError：另一个进程刚删掉了同一个文件。结果相同，不算失败
      （不捕获会抛 WinError 2，界面弹一句「Internal error」，像崩了）
    - PermissionError：Windows 文件共享冲突（另一进程正在读/替换），
      短退避重试几次（不重试会留下「点了删除但文件还在」）
    """
    try:
        os.remove(path)
        return ""
    except FileNotFoundError:
        return ""
    except PermissionError:
        for attempt in range(6):
            time.sleep(0.02 * (attempt + 1))
            try:
                os.remove(path)
                return ""
            except FileNotFoundError:
                return ""
            except OSError:
                continue
        return "删除会话失败（文件被其他程序占用）。请关闭占用它的程序后重试"
    except OSError:
        return "删除会话失败（磁盘或权限异常）。请检查后重试"


def delete_session(narnat_dir: str, name: str, parent: Optional[str] = None) -> str:
    if name == "--all":
        sdir = _sessions_dir(narnat_dir)
        try:
            entries = os.listdir(sdir)
        except OSError as e:
            return f"无法访问会话目录: {e}"
        for entry in entries:
            entry_path = os.path.join(sdir, entry)
            if os.path.isdir(entry_path):
                try:
                    shutil.rmtree(entry_path)
                except OSError:
                    pass
            elif entry.endswith(".json"):
                try:
                    os.remove(entry_path)
                except OSError:
                    pass
        return ""
    if parent:
        path = _session_path(narnat_dir, name, parent=parent)
        if not os.path.isfile(path):
            return f"会话不存在: {name}"
        os.remove(path)
        safe_parent = _safe_filename(parent)
        sessions_root = _sessions_dir(narnat_dir)
        child_dir = os.path.join(sessions_root, safe_parent)
        # parent="." 时 child_dir 即会话库根自身的别名：rmdir 绝不能触及
        if (os.path.normpath(child_dir) != os.path.normpath(sessions_root)
                and os.path.isdir(child_dir) and not os.listdir(child_dir)):
            try:
                os.rmdir(child_dir)
            except OSError:
                pass
        return ""
    path = _session_path(narnat_dir, name)
    if not os.path.isfile(path):
        return f"会话不存在: {name}"
    os.remove(path)
    safe_name = _safe_filename(name)
    sessions_root = _sessions_dir(narnat_dir)
    child_dir = os.path.join(sessions_root, safe_name)
    # "." 经文件名映射后仍是 "."（会话库根自身的别名）：rmtree 会连同
    # 其它全部会话清空整个会话库，必须排除
    if (os.path.normpath(child_dir) != os.path.normpath(sessions_root)
            and os.path.isdir(child_dir)):
        try:
            shutil.rmtree(child_dir)
        except OSError:
            pass
    return ""


def _sweep_stale_tmp(path: str, min_age_sec: float = 600.0) -> bool:
    """删掉一个**陈旧**的 `*.tmp` 写盘残骸。返回 True=真删了。

    残骸怎么来的：`_write_session_json` 写 tmp → `os.replace` 换成正式文件。
    正常异常路径都会清 tmp，但**进程被强杀**（`taskkill /F`、蓝屏、断电）时
    来不及清，于是会话目录里留下 `X.json.<pid>.<tid>.<n>.tmp`。
    第五轮 R40 实测：连杀 3 次留下 9 个、共 42.5MB，而且**没有任何清理路径** ——
    用户看到的是磁盘被悄悄占掉，删又不敢删（不知道是什么）。

    为什么必须带年龄阈值：如果另一进程此刻正在写 tmp（写盘窗口是一次
    json.dump，大会话可达几十毫秒），无脑删会把它的保存搞坏。
    600 秒远大于任何一次 json.dump（50MB 也就几百毫秒），
    所以「早于 10 分钟」的 tmp 必然是死人的。
    取不到 mtime（刚被删/权限）时**不动**，宁可不删也不错删。
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if time.time() - st.st_mtime < min_age_sec:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def list_sessions_tree_checked(narnat_dir: str) -> tuple:
    """会话树 + 「读不到」的原因。返回 (tree, err)。

    err 非空 = 目录枚举失败（权限拒绝/路径异常/目录建不出来），调用方必须把它
    说出来；err 为空且 tree 为空 = 真的没有会话。两者不能混为一谈：
    此前 os.walk 的 onerror 是默认的 None，PermissionError 被静默吞掉，
    session/list 于是返回空列表——用户看到的是「我的会话全没了」（实测）。
    """
    sdir = _sessions_dir(narnat_dir)
    errors = []

    def _on_walk_error(e):
        errors.append(e)

    roots = {}
    orphans = []
    for dirpath, dirnames, filenames in os.walk(sdir, onerror=_on_walk_error):
        for fname in filenames:
            if fname.endswith(".json"):
                fpath = os.path.join(dirpath, fname)
                try:
                    data = _load_json_with_shared_retry(fpath)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
                    # 损坏文件（含超深嵌套）一律跳过，不影响其余会话
                    continue
                if not isinstance(data, dict):
                    continue
                messages = data.get("messages")
                info = {
                    "name": data.get("name", fname[:-5]),
                    "timestamp": _safe_timestamp(data.get("timestamp", 0)),
                    "message_count": len(messages) if isinstance(messages, list) else 0,
                    "parent": data.get("parent"),
                    "status": data.get("status", "active"),
                    "summary": data.get("summary"),
                }
                if info["parent"]:
                    orphans.append(info)
                else:
                    roots[info["name"]] = {
                        "name": info["name"],
                        "timestamp": info["timestamp"],
                        "message_count": info["message_count"],
                        "children": [],
                    }
            elif fname.endswith(".tmp"):
                _sweep_stale_tmp(os.path.join(dirpath, fname))
    if errors:
        # 只报枚举失败，不把「部分读到的」吞掉：tree 照常返回，调用方两边都要看
        return _sort_session_tree(roots, orphans), f"无法读取会话目录：{errors[0]}"
    return _sort_session_tree(roots, orphans), ""


def _sort_session_tree(roots: dict, orphans: list) -> List[Dict[str, Any]]:
    """把 roots/orphans 挂成树并按时间排序（list_sessions_tree 的收尾段）"""
    for child in orphans:
        parent_name = child["parent"]
        if parent_name in roots:
            roots[parent_name]["children"].append({
                "name": child["name"],
                "timestamp": child["timestamp"],
                "message_count": child["message_count"],
                "status": child["status"],
                "summary": child["summary"],
            })
        else:
            root_entry = {
                "name": parent_name,
                "timestamp": 0,
                "message_count": 0,
                # ghost：父会话文件缺失，只剩子会话。RPC 侧据此标注「无记录」，
                # 不要把它当成一条正常的空会话。
                "ghost": True,
                "children": [child],
            }
            roots[parent_name] = root_entry
    for root in roots.values():
        root["children"].sort(key=lambda c: c.get("timestamp", 0))
    result = sorted(roots.values(), key=lambda r: r.get("timestamp", 0), reverse=True)
    return result


def list_sessions_tree(narnat_dir: str) -> List[Dict[str, Any]]:
    """只要会话树（读不到时按空树返回）。

    需要区分「读不到」与「没有会话」的场景请用 list_sessions_tree_checked：
    session/list 就是靠它把「读不到」说出来，不能在这里吞掉。
    """
    return list_sessions_tree_checked(narnat_dir)[0]


def format_session_tree(tree: List[Dict[str, Any]],
                        active_name: Optional[str] = None,
                        active_parent: Optional[str] = None) -> str:
    if not tree:
        return ""
    lines = []
    for i, root in enumerate(tree):
        is_last_root = (i == len(tree) - 1)
        prefix = "└──" if is_last_root else "├──"
        ts = _format_ts(root["timestamp"], "%m-%d %H:%M")
        is_current_root = (root["name"] == active_name and active_parent is None)
        delete_mark = f"  ✘ 退出后删除" if root.get("_delete_marked") else ""
        current_mark = "  ◀ 当前" if is_current_root else ""
        lines.append(f"  {prefix} {root['name']}  ({ts}, {root['message_count']}条){delete_mark}{current_mark}")
        child_prefix_base = "      " if is_last_root else "│     "
        children = root.get("children", [])
        for j, child in enumerate(children):
            is_last_child = (j == len(children) - 1)
            connector = "└──" if is_last_child else "├──"
            child_ts = _format_ts(child["timestamp"], "%m-%d %H:%M")
            if child.get("status") == "completed":
                status_str = f"✓ 已完成 ({child_ts})"
            elif child.get("status") == "new":
                status_str = f"({child_ts}, {child['message_count']}条)"
            else:
                status_str = f"⚠ 待完成 ({child_ts}, {child['message_count']}条)"
            if child.get("_delete_marked"):
                status_str += "  ✘ 退出后删除"
            current_mark = "  ◀ 当前" if (child["name"] == active_name and root["name"] == active_parent) else ""
            lines.append(f"  {child_prefix_base}{connector} {child['name']}  {status_str}{current_mark}")
    if active_name is None and active_parent is None:
        lines.append(f"   ◉  ◀ 当前")
    return "\n".join(lines)


def format_session_summary(tree: List[Dict[str, Any]],
                           active_name: Optional[str] = None,
                           active_parent: Optional[str] = None) -> str:
    """精简列表：今天全部列出，更早最多3个父会话并提示剩余（/ls --all 查看全部）。

    分组只看父会话 timestamp；子会话挂在父会话下按树形显示，格式与 /ls --all 一致。
    """
    if not tree:
        return ""
    now = time.localtime()
    today_midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  0, 0, 0, 0, 0, -1))

    def _entry(root):
        return {
            "name": root["name"],
            "timestamp": root["timestamp"],
            "count": root["message_count"],
            "active": (root["name"] == active_name and active_parent is None),
            "deleted": bool(root.get("_delete_marked")),
            "children": [
                {
                    "name": child["name"],
                    "timestamp": child["timestamp"],
                    "count": child["message_count"],
                    "status": child.get("status", "active"),
                    "active": (child["name"] == active_name and root["name"] == active_parent),
                    "deleted": bool(child.get("_delete_marked")),
                }
                for child in root.get("children", [])
            ],
        }

    today, earlier = [], []
    for root in tree:
        entry = _entry(root)
        (today if entry["timestamp"] >= today_midnight else earlier).append(entry)
    today.sort(key=lambda e: e["timestamp"], reverse=True)
    earlier.sort(key=lambda e: e["timestamp"], reverse=True)

    def _render(entry, ts_fmt, is_last):
        lines = []
        ts = _format_ts(entry["timestamp"], ts_fmt)
        marks = ""
        if entry["deleted"]:
            marks += "  ✘ 退出后删除"
        if entry["active"]:
            marks += "  ◀ 当前"
        connector = "└──" if is_last else "├──"
        lines.append(f"  {connector} {entry['name']}  ({ts}, {entry['count']}条){marks}")
        child_prefix_base = "      " if is_last else "│     "
        for j, child in enumerate(entry["children"]):
            is_last_child = (j == len(entry["children"]) - 1)
            c_connector = "└──" if is_last_child else "├──"
            child_ts = _format_ts(child["timestamp"], "%m-%d %H:%M")
            if child["status"] == "completed":
                status_str = f"✓ 已完成 ({child_ts})"
            elif child["status"] == "new":
                status_str = f"({child_ts}, {child['count']}条)"
            else:
                status_str = f"⚠ 待完成 ({child_ts}, {child['count']}条)"
            if child["deleted"]:
                status_str += "  ✘ 退出后删除"
            if child["active"]:
                status_str += "  ◀ 当前"
            lines.append(f"  {child_prefix_base}{c_connector} {child['name']}  {status_str}")
        return lines

    lines = []
    if today:
        lines.append(f"  今天 ({len(today)})")
        for i, e in enumerate(today):
            lines.extend(_render(e, "%H:%M", i == len(today) - 1))
        if earlier:
            lines.append("")
    if earlier:
        lines.append(f"  更早 ({len(earlier)})")
        shown = earlier[:3]
        # 当前会话（父或子）若不在前3棵，追加显示，保证"我在哪"可见
        current = next((e for e in earlier
                        if e["active"] or any(c["active"] for c in e["children"])), None)
        if current is not None and current not in shown:
            shown = shown + [current]
        for i, e in enumerate(shown):
            lines.extend(_render(e, "%m-%d", i == len(shown) - 1))
        rest = len(earlier) - len(shown)
        if rest > 0:
            lines.append(f"  … 还有 {rest} 条更早的会话 (/ls --all 查看)")
    if active_name is None and active_parent is None:
        lines.append(f"   ◉  ◀ 当前")
    return "\n".join(lines)


def session_tree_payload(tree: List[Dict[str, Any]],
                         active_name: Optional[str] = None,
                         active_parent: Optional[str] = None,
                         show_all: bool = False) -> Dict[str, Any]:
    """会话树 → 协议 session/list 的结构化结果（docs/vscode-rpc-protocol.md 3.5）。

    分组/排序规则与 format_session_summary 一致：今天全部列出，更早最多3个父会话
    （当前会话不在前3棵时追加显示），其余计入该组 hidden；show_all=True 不截断。

    Returns:
        {"current": "父/子" | 名称 | None,
         "groups": [{"label": "今天"|"更早", "count": 组内父会话总数,
                     "sessions": [{"name","time","count","group","current",
                                   "deleted","children":[...]}],
                     "hidden": 未展示条数（仅截断时出现）}]}
        子会话项为 {"name","status","time","count","current","deleted"}，
        status 取值 new / active / completed。
    """
    if active_parent is not None and active_name is not None:
        current = f"{active_parent}/{active_name}"
    else:
        current = active_name
    payload: Dict[str, Any] = {"current": current, "groups": []}
    if not tree:
        return payload

    now = time.localtime()
    today_midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  0, 0, 0, 0, 0, -1))

    def _entry(root):
        return {
            "name": root["name"],
            "timestamp": root["timestamp"],
            "count": root["message_count"],
            # ghost：父会话文件缺失（只剩子会话反推出的行）。
            # 界面据此标注「无记录」，别让用户以为这是条 0 条消息的空会话。
            "ghost": bool(root.get("ghost")),
            "current": (root["name"] == active_name and active_parent is None),
            "deleted": bool(root.get("_delete_marked")),
            "children": [
                {
                    "name": child["name"],
                    "timestamp": child["timestamp"],
                    "count": child["message_count"],
                    "status": child.get("status", "active"),
                    "current": (child["name"] == active_name and root["name"] == active_parent),
                    "deleted": bool(child.get("_delete_marked")),
                }
                for child in root.get("children", [])
            ],
        }

    today, earlier = [], []
    for root in tree:
        entry = _entry(root)
        (today if entry["timestamp"] >= today_midnight else earlier).append(entry)
    today.sort(key=lambda e: e["timestamp"], reverse=True)
    earlier.sort(key=lambda e: e["timestamp"], reverse=True)

    def _session(entry, ts_fmt, group):
        return {
            "name": entry["name"],
            "time": _format_ts(entry["timestamp"], ts_fmt),
            "count": entry["count"],
            "group": group,
            "ghost": entry.get("ghost", False),
            "current": entry["current"],
            "deleted": entry["deleted"],
            "children": [
                {
                    "name": child["name"],
                    "status": child["status"],
                    "time": _format_ts(child["timestamp"], ts_fmt),
                    "count": child["count"],
                    "current": child["current"],
                    "deleted": child["deleted"],
                }
                for child in entry["children"]
            ],
        }

    if today:
        payload["groups"].append({
            "label": "今天",
            "count": len(today),
            "sessions": [_session(e, "%H:%M", "today") for e in today],
        })
    if earlier:
        shown = earlier if show_all else earlier[:3]
        if not show_all:
            # 当前会话（父或子）若不在前3棵，追加显示，保证"我在哪"可见
            cur = next((e for e in earlier
                        if e["current"] or any(c["current"] for c in e["children"])), None)
            if cur is not None and cur not in shown:
                shown = shown + [cur]
        group = {
            "label": "更早",
            "count": len(earlier),
            "sessions": [_session(e, "%m-%d", "earlier") for e in shown],
        }
        rest = len(earlier) - len(shown)
        if rest > 0:
            group["hidden"] = rest
        payload["groups"].append(group)
    return payload


def load_session_meta(narnat_dir: str, name: str, parent: Optional[str] = None) -> dict:
    path = _session_path(narnat_dir, name, parent=parent)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
        return {}
    # 顶层类型防御（与 load_session 同构）：手工编辑/半写文件可能是数组/字符串
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "messages"}


def format_session_list(sessions: List[Dict[str, Any]]) -> str:
    if not sessions:
        return ""
    lines = []
    for s in sessions:
        ts = _format_ts(s["timestamp"], "%Y-%m-%d %H:%M")
        lines.append(f"  {s['name']}  ({ts}, {s['message_count']}条消息)")
    return "\n".join(lines)
