"""Shell工具 —— 纯管道，AI写什么就执行什么

Windows用 cmd.exe 子进程执行（shell=True），Linux/macOS用bash -c。
Windows下 python -c 载荷绕过cmd直接执行（多行/%/&/|等无需转义，见 _try_extract_py_code）。
AI自己负责写正确语法，我们只管送达和返回。
"""

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from . import truncate_store
from .. import safety
from ..exec_signal import rc_line, error_line, tag_error, safe_cut_points, strip_tags
from ..token_estimate import estimate_text_tokens
from ..tool_context import AWAIT_CONFIRM
from ..param_utils import to_bool


class BashRuntime:
    """Shell 工具全部模块级状态与参数（原散落的模块级全局收敛于此）。

    - active_proc/interrupted: 跨调用共享的可变状态（ESC 打断路径）
    - utf8_env: 子进程环境（导入时构建一次）
    - 正则/平台常量: 仅本模块使用
    """
    # 删除命令正则 / git 命令正则：共享定义源 tools/safety.py（安全判定语义恒等）
    RE_DELETE = safety.RE_DELETE
    RE_GIT = safety.RE_GIT

    # 识别 `python -c "code"` 形态（py/python3/pythonw及全路径），用于绕过cmd直执行。
    # exe: 解释器名或路径（可带盘符/空格，不可带引号）；flags: -c 前的真实旗标
    # （排除 -c/-m 自身及引号开头项）；tail: -c 后的整段载荷（re.S 允许多行）。
    RE_PY_C_DIRECT = re.compile(
        r"^(?i:(?P<exe>(?:[A-Za-z]:)?[\w.\\/ -]*?py(?:thon)?\d*(?:w)?(?:\.exe)?))"
        r"(?P<flags>(?:\s+(?:-(?!c\b|m\b)\S+|[^\s\"-]\S+))*)\s+-c\s+(?P<tail>.+)$",
        re.S,
    )

    # 子进程环境变量：强制 UTF-8 编码，解决 Windows 下 Python print emoji 等
    # Unicode 字符在 GBK 代码页下报 UnicodeEncodeError 的问题
    utf8_env = os.environ.copy()
    utf8_env["PYTHONIOENCODING"] = "utf-8"
    utf8_env["PYTHONUTF8"] = "1"

    # 当前运行的前台进程（agent层ESC打断后可调用kill_active杀掉）
    active_proc: Optional[subprocess.Popen] = None
    active_proc_lock = threading.Lock()

    # ESC打断标记，kill_active()设置，execute()检查后清除
    interrupted = False

    # 读线程收尾宽限（秒），供 _drain_readers 共享一个截止（详见该函数）
    DRAIN_GRACE = 0.3

    PLATFORM_LABEL = "Windows(cmd)" if sys.platform == "win32" else "Linux/macOS(bash)"

    # Windows 子进程使用独立（无窗口）控制台：否则子进程内 chcp/cls 等直写
    # 控制台的命令会清掉用户终端屏幕（WT 清可见区、conhost 清全缓冲）；
    # 独立后这类直写只落在子进程自己的隐藏缓冲区。非 Windows 恒为 0
    # （CREATE_NO_WINDOW 属性 Unix 的 subprocess 模块不存在；creationflags
    # 传非 0 亦会 ValueError）
    WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _scan_code_suffix(tail: str):
    """扫描 tail（以引号开头的 -c 载荷），定位引号闭合处并剥离后缀。

    返回 (code_tail, suffix)。引号闭合后允许：空白、`2>&1`（剥离，语义等价）、
    `|`/`>`（作为 suffix 返回）。其余形态（追加参数等）返回原 tail 交由
    endswith 守卫回退。引号内按 POSIX 转义规则跳过（\\X 均在引号内）。
    """
    j = 1
    while j < len(tail):
        ch = tail[j]
        if ch == "\\" and j + 1 < len(tail):
            j += 2  # POSIX转义: \X 均在引号内
            continue
        if ch == '"':
            # 引号闭合：跳过空白与 2>&1，找 | / >
            k = j + 1
            while k < len(tail):
                while k < len(tail) and tail[k] in " \t":
                    k += 1
                if k + 4 <= len(tail) and tail[k : k + 4] == "2>&1":
                    k += 4
                    continue
                break
            if k >= len(tail):
                return tail[: j + 1], None  # 纯 code（仅空白/2>&1 尾随）
            if tail[k] in "|>":
                return tail[: j + 1], tail[k:]
            # 引号后有其他 token（追加参数/& 后续命令等）→ 回退。
            # 返回空串后缀（而非 None）与"纯 code"出口区分：调用方的
            # endswith('"') 兜底在追加 token 恰以引号结尾（如 `... & echo "x"`）
            # 时会误放行，把整段当 -c 载荷直执行、后续命令被静默吞掉。
            return tail, ""
        j += 1
    return tail, None


def _parse_suffix(suffix: str):
    """解析后缀（suffix[0] 为 '|' 或 '>'）。返回 None 表示形态不支持（回退cmd）。

    支持: `| cmd`(单层、不含<>|&)、`> file`、`>> file`、`> "带空格路径"`、`>nul`。
    """
    rest = suffix[1:].strip()
    if not rest:
        return None
    if suffix[0] == "|":
        if any(c in rest for c in "<>|&"):
            return None  # 嵌套管道/重定向组合 → 回退
        return ("pipe", rest)
    # 重定向
    mode = "w"
    if rest.startswith(">"):
        rest = rest[1:].strip()
        mode = "a"
        if not rest:
            return None
    if rest.lower() == "nul":
        return ("discard",)
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end == -1 or rest[end + 1 :].strip():
            return None
        return (mode, rest[1:end])
    # 裸路径：单 token（cmd 裸路径不能含空格）
    toks = rest.split(None, 1)
    if len(toks) > 1:
        return None
    return (mode, toks[0])


def _try_extract_py_code(seg: str):
    """识别 `python -c "code"` 形态的段。命中返回 (exe, flags, code_tail, spec)，否则 None。

    spec: None 无后缀 | ('pipe', cmd) | ('w'|'a', path) | ('discard',)
    仅当 -c 后为双引号包裹（允许尾随 2>&1、|管道、>重定向等AI高频后缀）、
    解释器可解析且实为 .exe 时走直执行路径；其余一律回退 cmd 原路径，零回归。

    尾随 ` 2>&1` 剥离：工具本就合并展示 stdout+stderr，语义等价。
    """
    m = BashRuntime.RE_PY_C_DIRECT.match(seg)
    if not m:
        return None
    exe, flags, tail = m.group("exe"), m.group("flags"), m.group("tail")
    tail = tail.strip()
    if len(tail) < 2 or not tail.startswith('"'):
        return None
    # 引号闭合处扫描：剥离 2>&1、识别 |管道 / >重定向后缀
    code_tail, suffix = _scan_code_suffix(tail)
    if suffix is not None:
        spec = _parse_suffix(suffix)
        if spec is None:
            return None  # 不支持的后缀形态 → 回退cmd
    else:
        spec = None
    if not code_tail.endswith('"'):
        return None
    import shutil
    resolved = shutil.which(exe)
    if resolved is None:
        return None
    base = os.path.splitext(os.path.basename(resolved))[0].lower()
    if not re.fullmatch(r"py(?:thon)?\d*(?:w)?", base):
        return None  # 非python解释器（如 spy.exe 等误命中）→ 交给cmd
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in ("", ".exe"):
        return None  # .bat垫片等 → 交给cmd处理
    return exe, flags, code_tail, spec

# （以上模块级状态已收敛为 BashRuntime 类成员）

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Shell",
        "description": (
            f"本地Shell — 在{BashRuntime.PLATFORM_LABEL}执行命令。"
            "前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "命令（前台执行或 background=true 提交时必填；"
                        "bg=status/wait/cancel 时可省略）"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "前台=超时秒数（正整数，默认120，超时后命令被终止）；"
                        "bg=wait时=最长等待秒数"
                    ),
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": (
                        "最大输出字符数（正整数，默认4000，仅前台命令生效；"
                        "超出时保留首尾，完整输出落盘并在提示中给出文件路径）"
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "true=命令在后台执行，立即返回 bgN 编号（不阻塞）；"
                        "结果写入会话专属临时目录的 bgN.log（提交时返回绝对路径，编号与文件一一对应），"
                        "用 Read/Grep 读取该文件；"
                        "输出随任务运行分块落盘（输出量小的任务可能到结束才可见）；"
                        "并发上限8个（bg1~bg8，终态槽位自动释放复用，"
                        "复用前旧结果归档为 bgN.log.N.prev，仍可 Read 读取、不丢失），"
                        "会话结束自动清理全部后台任务"
                    ),
                },
                "bg": {
                    "type": "string",
                    "description": (
                        "后台任务管理操作："
                        "status 查看所有后台任务状态（编号/状态/退出码/输出大小/结果路径）；"
                        "wait 挂起等待任意后台任务完成（有完成立即返回，超时返回最新状态快照）；"
                        "cancel 取消 bgN（杀进程树，已产出内容保留可读，配合 id）"
                    ),
                },
                "id": {
                    "type": "integer",
                    "description": "后台任务编号（bg=cancel 时必填，如 id=3 取消 bg3）",
                },
            },
            "required": [],
        },
    },
}


CAPABILITY = {
    "label": "执行命令",
    "dispatch": "serial",
    "summary": "command",
    "trusted_output": False,
}


def kill_active():
    """杀掉当前正在运行的前台子进程（ESC打断时由agent调用）

    杀进程树改在后台线程执行：ESC打断后主线程立即返回，
    输入界面马上还给用户；杀树期间用户输入新命令不受影响
    （新命令是新Popen，会覆盖BashRuntime.active_proc，后台线程持有旧proc引用）。
    """
    BashRuntime.interrupted = True
    with BashRuntime.active_proc_lock:
        proc = BashRuntime.active_proc
    if proc is not None and proc.poll() is None:
        threading.Thread(target=_kill_proc_tree, args=(proc,), daemon=True).start()


def _find_executable(*names: str) -> Optional[str]:
    """按优先级查找可执行文件，返回第一个找到的名称或路径。"""
    import shutil
    for name in names:
        if shutil.which(name):
            return name
    return None


def _decode_line(line: bytes) -> str:
    """单行解码：utf-8 严格 → gbk 严格 → utf-8 replace 兜底。"""
    if not line:
        return ""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return line.decode("gbk")
    except UnicodeDecodeError:
        pass
    return line.decode("utf-8", errors="replace")


def _decode_mixed_lines(raw: bytes) -> str:
    """逐行解码混合编码流（行分隔保留原样）。

    整段严格解码只适用于"单一编码"输出；同一 stdout 里 GBK 段（cmd 内建命令）
    与 UTF-8 段（python 等）混排时两级严格解码必然全失败，replace 兜底会把
    GBK 段整段变成乱码。此处按行各自判定编码，两段都能还原。
    性能：线性扫描（按 \\n 切分 + 每行两次严格解码尝试），仅在整段解码已失败
    时才走到这里，常规单编码输出不受影响。
    """
    out = []
    for i, line in enumerate(raw.split(b"\n")):
        if i:
            out.append("\n")
        out.append(_decode_line(line))
    return "".join(out)


def _decode_output(raw: bytes) -> str:
    """安全解码子进程输出。Windows下回退GBK，Unix下仅UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if sys.platform == "win32":
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
        # GBK 与 UTF-8 段混排（如 `echo 中文 & python -c "print('中文')"`）：
        # 整段两级严格解码全失败 → 逐行判定，避免 GBK 段被 replace 打烂
        return _decode_mixed_lines(raw)
    return raw.decode("utf-8", errors="replace")


def _split_commands(command: str) -> list:
    """在引号外、括号组外按 && 和 || 分割，返回 [(op, cmd), ...]。
    op: '' 表示首段，'&&' 或 '||' 表示后续段。

    括号内的 &&/|| 不分割：cmd/bash 中 () 是分组语法，
    (a && b) 是一个整体命令，拆开会导致语法错误。
    ^ 转义（cmd）：^ 后一字符是字面量，跳过不参与括号深度和分割。
    """
    splits = []  # [(pos, '&&'|'||')]
    in_quote = False
    paren_depth = 0
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "^" and i + 1 < n:
                i += 1  # cmd转义: 跳过后一字符（^&、^( 等为字面量）
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(paren_depth - 1, 0)
            elif paren_depth == 0 and i + 1 < n:
                two = command[i:i+2]
                if two in ("&&", "||"):
                    splits.append((i, two))
                    i += 1
        i += 1

    if not splits:
        return [("", command.strip())]

    result = [("", command[:splits[0][0]].strip())]
    for j, (pos, op) in enumerate(splits):
        next_pos = splits[j+1][0] if j + 1 < len(splits) else len(command)
        result.append((op, command[pos+2:next_pos].strip()))
    return result


def _is_cd_command(cmd: str) -> bool:
    """判断是否为 cd/chdir 命令（仅纯cd，不含 &/|/; 等复合操作符）"""
    lower = cmd.lower().strip()
    # 拒绝复合命令：含 & | && || ;
    # bash 的 ; 也是命令分隔符：`cd /tmp; ls` 若被误判为纯cd，
    # 会执行 os.chdir("/tmp; ls") 整体失败；cmd 虽不认 ; 作分隔符，
    # 但这类写法在 cmd 下本就不是合法cd，放行到子进程执行同样合理
    if "&" in cmd or "|" in cmd or ";" in cmd:
        return False
    # 重定向不是纯cd：`cd /tmp > f` 的目标是重定向文件，不是目录
    if ">" in cmd or "<" in cmd:
        return False
    # 多行命令不是纯cd：换行在 cmd/bash 下是命令分隔符（cmd 执行首行、
    # bash 逐行执行）。误判会让整条多行命令被拦截、一行未执行
    if "\n" in cmd or "\r" in cmd:
        return False
    # cmd 无空格简写: cd..(父目录)、cd...(祖父目录)、cd\(根目录)
    if lower in ("cd..", "cd...", "chdir..", "chdir...", "cd\\", "chdir\\"):
        return True
    return lower.startswith("cd ") or lower == "cd" or lower.startswith("chdir ") or lower == "chdir"


def _extract_cd_path(cmd: str) -> Optional[str]:
    """从 cd 命令中提取目标路径，处理 /d 等cmd标志。
    返回 None 表示无参数cd（仅显示当前目录，不切换）。"""
    # cmd 无空格简写: cd.. → 父目录、cd... → 祖父目录、cd\ → 当前盘根目录
    stripped = cmd.strip().lower()
    if stripped in ("cd..", "chdir.."):
        return ".."
    if stripped in ("cd...", "chdir..."):
        return os.path.join("..", "..")
    if stripped in ("cd\\", "chdir\\"):
        return "\\"
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return None  # 无参数cd：仅显示当前目录，不切换
    args = parts[1]
    # 去掉cmd的 /d 标志
    if args.lower().startswith("/d "):
        args = args[3:].strip()
    # 展开环境变量（%TEMP%、%USERPROFILE% 等，与cmd行为一致）和 ~（bash语义；
    # Windows下 expanduser 对非~开头路径原样返回，无副作用）
    return os.path.expanduser(os.path.expandvars(args.strip('"')))


def _kill_proc_tree(proc: subprocess.Popen):
    """杀掉进程树（Unix用killpg，Windows用taskkill）"""
    if proc.poll() is not None:
        return
    pid = proc.pid
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _drain_readers(*threads) -> None:
    """读线程收尾：共享一个宽限，而非逐个 join 各自的超时。

    `start /b` 分离出的孙进程会继承管道写端，cmd.exe 退出后管道仍不 EOF，
    读线程一直阻塞在 read()；逐个 join(timeout=5) 会串成 10 秒天花板，
    命令早已结束却仍要等满。管道背压使 cmd 退出时缓冲留存有界（毫秒级可
    读完），故共享宽限只对"永不 EOF"的分离场景生效，正常命令零影响。
    """
    deadline = time.time() + BashRuntime.DRAIN_GRACE
    for t in threads:
        remain = deadline - time.time()
        if remain > 0:
            t.join(timeout=remain)


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部常含命令最后输出，对AI判断执行结果至关重要），中段提示。

    切点先做标签吸附（safe_cut_points）：框架标签不允许被切开——残缺
    片段无法被 strip_tags 匹配，会泄漏给AI并让失败判定失效。
    截断发生时完整输出落盘（truncate_store，进程专属临时目录），提示中
    给出文件路径供AI用 Read/Grep 查询完整内容；落盘失败退回原提示文案。
    """
    if max_chars <= 0:
        return error_line("max_output_chars需为正整数")
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    head_end, tail_start = safe_cut_points(text, head, len(text) - (max_chars - head))
    est = estimate_text_tokens(text)  # ≈token（AI预算单位，混合密度估算）
    # 落盘前剥离进程级随机标签：标签是防伪协议的一部分，不得经落盘文件
    # 泄漏（AI 已知标签后可伪造 has_error 判定；AI 视图本来也不含标签）
    full_path = truncate_store.store_output(strip_tags(text))
    tip = (
        f"完整输出已落盘，用 Read/Grep 读取: {full_path}"
        if full_path
        else "增大max_output_chars可获取完整输出"
    )
    return (
        text[:head_end]
        + f"\n...[中间截断: 输出共{len(text)}字符, 已保留首{head_end}字符+尾{len(text) - tail_start}字符(≈{est}token)。{tip}]\n"
        + text[tail_start:]
    )


def execute(
    command: str = None,
    timeout: int = 120,
    max_output_chars: int = 4000,
    max_output_tokens: int = None,
    background: bool = False,
    bg: str = None,
    id: int = None,
    _tool_context=None,
) -> str:
    """
    执行shell命令。AI写什么就执行什么，不做翻译。

    Windows: 持久化cmd会话，命令直写stdin，行为与真实cmd窗口一致。
    Linux/macOS: bash -c 子进程。

    前台（默认）: command 同步执行，返回 stdout + stderr + 退出码。
    后台: background=true 提交后台任务（立即返回 bgN，结果落盘会话专属临时目录，
    绝对路径随提交返回）；bg=status/wait/cancel 管理后台任务（见 tools/background 模块）。

    Args:
        command: shell命令（前台必填；background=true 提交时必填；bg 操作可省略）
        timeout: 超时秒数（前台=命令超时；bg=wait 时=最长等待秒数）
        max_output_chars: 返回内容最大字符数，正整数，默认4000（仅前台生效）
        max_output_tokens: max_output_chars 的别名（字符数语义），两者同传以本参数为准
        background: true=命令后台执行
        bg: 后台任务管理操作: status / wait / cancel
        id: 后台任务编号（bg=cancel 时必填）
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        前台: stdout + stderr + 退出码；后台: 提交/状态/等待/取消结果
    """
    # 函数内多分支读写该标志（BashRuntime.interrupted）
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    # OverflowError：JSON 里的 1e999 → float('inf')，int(inf) 抛 OverflowError
    try:
        timeout = int(timeout) if timeout is not None else 120
        if max_output_tokens is not None:
            max_output_chars = max_output_tokens  # 别名：归一化后统一走字符语义
        max_output_chars = int(max_output_chars) if max_output_chars is not None else 4000
    except (TypeError, ValueError, OverflowError):
        return error_line("timeout/max_output_chars需为整数")
    # LLM 偶发把布尔值写成字符串（background="false"）：bool("false") 恒为 True，
    # 会把前台命令意外转入后台（前台拿不到输出），必须按字符串语义解析
    background = to_bool(background)
    # 参数校验前置：max_output_chars<=0 时若放到 _truncate_output 才报错，
    # 命令已执行、输出却被替换成参数错误（副作用已发生但结果不可见）——与
    # Terminal/Read 的"执行前拦截"保持一致
    if max_output_chars <= 0:
        return error_line("max_output_chars需为正整数")
    # 与全局输出上限（"工具"."输出上限KB"）提前融合：registry 层的全局截断
    # 对超限结果做首尾拼接（不落盘），若本层截断产物（正文+"完整输出已落盘"
    # 提示行）总长仍超全局上限，位于中段的落盘路径提示会被二次截断吞掉，
    # AI 拿不到路径、无法按 README 承诺用 Read/Grep 读回全文。此处按全局
    # 上限收口并预留提示行空间（截断提示+hint 约几百字符），产出整体不超上限。
    if _tool_context and _tool_context.max_tool_output_chars > 0:
        max_output_chars = min(max_output_chars,
                               max(300, _tool_context.max_tool_output_chars - 500))
    # ── 安全检查：删除命令和git命令根据配置决定是否需要确认 ──
    # 后台提交与前台同一套确认（bg 管理操作无 command 自然跳过）
    # 判定走 safety.match_*（含 cmd 等价写法归一化：d^el、del.\x、del\x、!VAR! 拼接）
    need_confirm = False
    tc = _tool_context
    if command and tc and not tc.rm_skip_confirm and safety.match_delete(command):
        need_confirm = True
    elif command and tc and not tc.git_skip_confirm and safety.match_git(command):
        need_confirm = True

    if need_confirm:
        # 暂存命令由 agent 主循环在 # 提示符下等用户确认后重放；
        # headless 下读不到输入 → 取消执行（fail-closed）
        if tc and tc._delete_confirmed:
            tc._delete_confirmed = False
        else:
            if tc is not None:
                tc.pending_delete.append(("Shell", {
                    "command": command,
                    "timeout": timeout,
                    "max_output_chars": max_output_chars,
                    "background": background,
                    "bg": bg,
                    "id": id,
                }))
            return AWAIT_CONFIRM

    # ── 后台任务分发（bg 参数）：独立模块实现，前台逻辑不变 ──
    if background or bg:
        from ..background import bg_execute
        return bg_execute(command, timeout, background, bg, id, _tool_context)

    if not command:
        return error_line(
            "command为空：前台执行需提供 command；"
            "后台任务用 background=true 提交，管理用 bg=status/wait/cancel"
        )

    if timeout <= 0:
        return error_line("timeout需为正整数（秒）")

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    # ═════════════════════════════════════════════════════════════
    # Windows: cmd /c 子进程（stdin 隔离为 DEVNULL，防止挂起子进程偷吃ESC）
    # ═════════════════════════════════════════════════════════════
    if sys.platform == "win32":
        # 入口清零：上轮残留的ESC中断标志不污染本轮（覆盖单段/多段全部子路径）
        BashRuntime.interrupted = False

        # 单段 cd：子进程内 cd 无副作用（执行完即退出），照常执行并附提示引导。
        # 不拦截：误判会让整条命令不执行，代价大于收益
        cd_hint = _is_cd_command(command)

        # 多段命令(&&/||)由Python端拆分后逐段执行
        segments = _split_commands(command)
        if len(segments) > 1:
            return _execute_segments(segments, timeout, max_output_chars, _tool_context)

        # python -c "code" 形态：绕过cmd直执行，多行/%/&/|等原样传给解释器
        # 尾随AI高频后缀（2>&1、|管道、>重定向）已剥离，一并直执行
        py = _try_extract_py_code(segments[0][1])
        if py is not None:
            exe, flags, code_tail, spec = py
            if spec is None:
                rc, out, err, status = _execute_py_direct(
                    exe, flags, code_tail, timeout, max_output_chars
                )
            else:
                rc, out, err, status = _execute_py_suffixed(
                    exe, flags, code_tail, spec, timeout, max_output_chars
                )
            return _format_result(rc, out, err, status, timeout, max_output_chars)

        result = _execute_win32(command, timeout, max_output_chars)
        return _with_multiline_hint(_with_cd_hint(result, cd_hint), command)

    # ═════════════════════════════════════════════════════════════
    # Linux/macOS: bash -c 子进程（原有逻辑）
    # ═════════════════════════════════════════════════════════════
    shell = _find_executable("bash", "sh")
    if shell is None:
        return error_line("未找到shell，请安装bash或sh后重试")

    # 单段 cd：子进程内 cd 无副作用，照常执行并附提示引导（不拦截）
    cd_hint = _is_cd_command(command)

    # 多段命令(&&/||)由Python端拆分后逐段执行
    segments = _split_commands(command)
    if len(segments) > 1:
        return _execute_segments(segments, timeout, max_output_chars, _tool_context)

    shell_cmd = [shell, "-c", command]

    # 用新进程组，确保能 killpg 杀整棵树
    # stdin 隔离为 DEVNULL：Shell 是纯管道语义（不支持交互，交互走
    # terminal/serial 工具），且挂起子进程继承控制台 stdin 会偷吃用户
    # 的 ESC 按键导致打断失效（同 Windows 分支的修复）
    try:
        proc = subprocess.Popen(
            shell_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            start_new_session=True,
            env=BashRuntime.utf8_env,
        )
    except FileNotFoundError as e:
        return error_line(f"Shell未找到: {e}")
    except (OSError, ValueError) as e:
        # ValueError: 命令含NUL等非法字符时 Popen 拒绝启动
        return error_line(f"启动失败: {e}")

    with BashRuntime.active_proc_lock:
        BashRuntime.active_proc = proc

    try:
        stdout_chunks = []
        stderr_chunks = []

        def _reader(stream, chunks):
            try:
                while True:
                    data = stream.read(4096)
                    if not data:
                        break
                    chunks.append(data)
            except Exception:
                pass

        t_out = threading.Thread(
            target=_reader, args=(proc.stdout, stdout_chunks), daemon=True
        )
        t_err = threading.Thread(
            target=_reader, args=(proc.stderr, stderr_chunks), daemon=True
        )
        for t in (t_out, t_err):
            t.start()

        BashRuntime.interrupted = False  # 入口清零：上轮残留的中断标志不污染本轮
        deadline = time.time() + timeout
        timed_out = False
        was_interrupted = False

        while proc.poll() is None:
            if time.time() >= deadline:
                timed_out = True
                break
            if BashRuntime.interrupted:
                was_interrupted = True
                BashRuntime.interrupted = False
                break
            time.sleep(0.05)

        # 先杀进程树再收尾输出："超时/中断"的语义是命令已被终止。
        # 若先 join 读线程，进程在 join 窗口内自然跑完时会输出完整结果
        # 却仍标注"已终止"，语义矛盾且每次超时白等收尾宽限。
        if was_interrupted or timed_out:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)

        _drain_readers(t_out, t_err)

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)

        if was_interrupted:
            parts = []
            out = _decode_output(stdout)
            if out.strip():
                parts.append(out.strip())
            err = _decode_output(stderr)
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            parts.append("[用户中断]")
            return _truncate_output("\n".join(parts), max_output_chars)

        if timed_out:
            parts = []
            out = _decode_output(stdout)
            if out.strip():
                parts.append(out.strip())
            err = _decode_output(stderr)
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            parts.append(tag_error(f"[超时: 命令执行超过{timeout:.0f}秒，已终止]"))
            return _truncate_output("\n".join(parts), max_output_chars)

        parts = [rc_line(proc.returncode)]
        out = _decode_output(stdout)
        if out.strip():
            parts.append(out.strip())
        err = _decode_output(stderr)
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        return _with_cd_hint(_truncate_output("\n".join(parts), max_output_chars), cd_hint)
    finally:
        with BashRuntime.active_proc_lock:
            BashRuntime.active_proc = None


def _with_cd_hint(result: str, cd_hint: bool) -> str:
    """单段 cd 命令的结果附提示：命令已照常执行，仅引导 AI 改用 cd X && 命令。

    不拦截：判断正则一旦误判，整条命令不执行（代价远大于收益）。
    """
    if not cd_hint:
        return result
    return f"{result}\n[提示: 单通道执行下 cd 不影响后续命令，当前目录: {os.getcwd()}。请用 'cd X && 命令' 形式]"


def _with_multiline_hint(result: str, command: str) -> str:
    """Windows 多行命令的结果附提示：cmd 仅执行首行，其余行被静默丢弃。

    Linux/macOS（bash -c 多行逐行执行）与 python -c 直执行路径不加。
    """
    if sys.platform != "win32" or ("\n" not in command and "\r" not in command):
        return result
    return (f"{result}\n[提示: 命令含换行符，Windows cmd 仅执行首行（其余行被静默丢弃）。"
            "多步命令请用 && 串联，或写入 .cmd 脚本后执行]")


def _collect_proc_output(proc: subprocess.Popen, timeout: int, max_output_chars: int):
    """等待子进程并收集输出（读线程 + 超时/ESC中断 + 进程树杀）。

    返回 (returncode, stdout_text, stderr_text, status)，
    status: 'ok' | 'timeout' | 'interrupt'（超时/中断时已杀进程树）。
    """
    with BashRuntime.active_proc_lock:
        BashRuntime.active_proc = proc

    try:
        stdout_chunks = []
        stderr_chunks = []

        def _reader(stream, chunks):
            try:
                while True:
                    data = stream.read(4096)
                    if not data:
                        break
                    chunks.append(data)
            except Exception:
                pass

        t_out = threading.Thread(
            target=_reader, args=(proc.stdout, stdout_chunks), daemon=True
        )
        t_err = threading.Thread(
            target=_reader, args=(proc.stderr, stderr_chunks), daemon=True
        )
        for t in (t_out, t_err):
            t.start()

        deadline = time.time() + timeout
        timed_out = False
        was_interrupted = False

        while proc.poll() is None:
            if time.time() >= deadline:
                timed_out = True
                break
            if BashRuntime.interrupted:
                was_interrupted = True
                BashRuntime.interrupted = False
                break
            time.sleep(0.05)

        # 先杀进程树再收尾输出："超时/中断"的语义是命令已被终止
        if was_interrupted or timed_out:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)

        _drain_readers(t_out, t_err)

        out = _decode_output(b"".join(stdout_chunks))
        err = _decode_output(b"".join(stderr_chunks))

        if was_interrupted:
            return proc.returncode, out, err, "interrupt"
        if timed_out:
            return proc.returncode, out, err, "timeout"
        return proc.returncode, out, err, "ok"
    finally:
        with BashRuntime.active_proc_lock:
            BashRuntime.active_proc = None


def _format_result(rc: int, out: str, err: str, status: str,
                   timeout: int, max_output_chars: int) -> str:
    """把 _collect_proc_output 的结果组装成统一输出（与历史格式一致）。"""
    if status == "interrupt":
        parts = []
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        parts.append("[用户中断]")
    elif status == "timeout":
        parts = []
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        parts.append(tag_error(f"[超时: 命令执行超过{timeout:.0f}秒，已终止]"))
    else:
        parts = [rc_line(rc)]
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
    return _truncate_output("\n".join(parts), max_output_chars)


def _execute_win32(command: str, timeout: int, max_output_chars: int) -> str:
    """Windows: shell=True 起子进程。cmd 交互式解析（引号按用户预期处理），
    stdin 隔离为 DEVNULL：防止挂起子进程（cooked 行读共享控制台输入队列）
    偷吃用户的 ESC 按键导致打断失效；DEVNULL 对读 stdin 的工具立即返回
    EOF 而不阻塞（不读 stdin 的工具不受影响）。"""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            creationflags=BashRuntime.WIN_NO_WINDOW,
            env=BashRuntime.utf8_env,
        )
    except FileNotFoundError as e:
        return error_line(f"cmd.exe未找到: {e}")
    except (OSError, ValueError) as e:
        # ValueError: 命令行含NUL等非法字符时 Popen 拒绝启动
        return error_line(f"启动失败: {e}")

    rc, out, err, status = _collect_proc_output(proc, timeout, max_output_chars)
    return _format_result(rc, out, err, status, timeout, max_output_chars)


def _execute_py_direct(exe: str, flags: str, tail: str,
                       timeout: int, max_output_chars: int, cwd: str = ""):
    """绕过cmd直接CreateProcess执行 python -c 载荷（shell=False）。

    载荷由 CommandLineToArgvW 规则解析：双引号内的换行/%/&/|/<等一律字面
    传给解释器，从根上规避 cmd 吞多行、改写特殊字符的问题。
    stdin 隔离为 DEVNULL（同 _execute_win32 的 ESC 偷吃防护）。
    返回 (rc, out, err, status)，格式与 _collect_proc_output 一致。
    """
    try:
        proc = subprocess.Popen(
            f'"{exe}"{flags} -c {tail}',
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or os.getcwd(),
            creationflags=BashRuntime.WIN_NO_WINDOW,
            env=BashRuntime.utf8_env,
        )
    except (OSError, ValueError) as e:
        # ValueError: 载荷含NUL等非法字符时 Popen 拒绝启动
        return (1, "", f"启动失败: {e}", "ok")
    return _collect_proc_output(proc, timeout, max_output_chars)


def _execute_py_suffixed(exe: str, flags: str, tail: str, spec,
                         timeout: int, max_output_chars: int, cwd: str = ""):
    """直执行 python -c 并处理尾随后缀（|管道 / >重定向）。

    spec: ('pipe', cmd) | ('w'|'a', path) | ('discard',)
    返回 (rc, out, err, status)，格式与 _collect_proc_output 一致。
    """
    if spec[0] == "pipe":
        return _execute_py_pipe(exe, flags, tail, spec[1], timeout, max_output_chars, cwd=cwd)

    rc, out, err, status = _execute_py_direct(exe, flags, tail, timeout, max_output_chars, cwd=cwd)
    if status != "ok" or spec[0] == "discard":
        # 中断/超时不写文件；>nul 直接丢弃 stdout（stderr 仍展示）
        return rc, "", err, status

    mode, target = spec
    # 相对重定向路径按段工作目录解析（`cd X && python -c "..." > out.txt`
    # 的 out.txt 应落在 X 下，与 cmd 语义一致；此前落在 agent 进程 cwd，
    # 返回 [exit code: 0] 静默成功、AI 反复重试）
    if not os.path.isabs(target):
        target = os.path.join(cwd or os.getcwd(), target)
    try:
        with open(target, mode + "b") as f:
            # PYTHONIOENCODING=utf-8 保证子进程输出UTF-8；\r\n 原样保留（与cmd一致）
            f.write(out.encode("utf-8"))
    except OSError as e:
        return (1, "", f"写入文件失败: {e}", "ok")
    return rc, "", err, status


def _execute_py_pipe(exe: str, flags: str, tail: str, pipe_cmd: str,
                     timeout: int, max_output_chars: int, cwd: str = ""):
    """直执行 python -c，stdout 经 cmd 管道命令过滤后合并展示。

    先直执行 python（超时/ESC语义不变）；成功后把 stdout 字节喂给管道命令，
    剩余时间预算内收集。python 的 stderr 与管道 stderr 合并展示。
    """
    start = time.time()
    rc_py, out_py, err_py, status_py = _execute_py_direct(
        exe, flags, tail, timeout, max_output_chars, cwd=cwd
    )
    elapsed = time.time() - start
    if status_py != "ok":
        return rc_py, out_py, err_py, status_py  # 中断/超时跳过管道阶段

    remaining = max(0.1, timeout - elapsed)
    try:
        proc = subprocess.Popen(
            pipe_cmd,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 管道命令按段工作目录执行（与 python 段一致；cd 后的相对路径有效）
            cwd=cwd or os.getcwd(),
            creationflags=BashRuntime.WIN_NO_WINDOW,
            env=BashRuntime.utf8_env,
        )
    except (OSError, ValueError) as e:
        return (1, out_py, f"{err_py}\n[管道启动失败: {e}]".strip(), "ok")

    def _feed():
        # 管道命令可能提前退出不读stdin → BrokenPipeError 忽略
        try:
            proc.stdin.write(out_py.encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    threading.Thread(target=_feed, daemon=True).start()
    rc, out, err, status = _collect_proc_output(proc, remaining, max_output_chars)
    errs = [e for e in (err_py.strip(), err.strip()) if e]
    return rc, out, "\n".join(errs), status


def _execute_segments(segments: list, timeout: int,
                      max_output_chars: int, _tool_context) -> str:
    """逐段执行 &&/|| 分割的命令，短路段跳过。

    虽然 cmd /c 本身支持 &&，但 Python 端拆分为逐段执行以获得：
    1. 每段独立的超时控制（超时时强杀整棵进程树）
    2. ESC 可在段内/段间打断
    3. cd 命令作用于本次调用内的局部工作目录（不修改 agent 进程 cwd）
    """
    BashRuntime.interrupted = False  # 入口清零：上轮残留的中断标志不污染本轮
    all_parts = []
    prev_rc = 0
    remaining_timeout = timeout
    was_interrupted = False
    local_cwd = os.getcwd()

    for i, (op, seg) in enumerate(segments):
        # 短路求值
        if op == "&&" and prev_rc != 0:
            all_parts.append(f"[跳过: 前一命令失败(退出码{prev_rc})] {seg}")
            continue
        if op == "||" and prev_rc == 0:
            all_parts.append(f"[跳过: 前一命令成功] {seg}")
            continue

        # cd 段：作用于本次调用内的局部工作目录（不修改 agent 进程 cwd）
        if _is_cd_command(seg):
            path = _extract_cd_path(seg)
            if path is None:
                all_parts.append(local_cwd)  # 无参数cd：显示当前（局部）目录
                prev_rc = 0
            else:
                target = path if os.path.isabs(path) else os.path.normpath(os.path.join(local_cwd, path))
                try:
                    os.stat(target)
                except OSError as e:
                    all_parts.append(f"cd: {e}")
                    prev_rc = 1
                else:
                    local_cwd = target
                    prev_rc = 0
            continue

        # python -c 载荷绕过cmd直执行：换行/%/&/|等不再被cmd吞掉
        # 尾随AI高频后缀（2>&1、|管道、>重定向）已剥离，一并直执行
        py = _try_extract_py_code(seg)
        if py is not None:
            exe, flags, code_tail, spec = py
            seg_budget = remaining_timeout
            seg_start = time.time()
            if spec is None:
                rc, out, err, status = _execute_py_direct(
                    exe, flags, code_tail, seg_budget, max_output_chars, cwd=local_cwd
                )
            else:
                rc, out, err, status = _execute_py_suffixed(
                    exe, flags, code_tail, spec, seg_budget, max_output_chars, cwd=local_cwd
                )
            seg_elapsed = time.time() - seg_start
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            if status == "interrupt":
                was_interrupted = True
                break
            if status == "timeout":
                parts = [tag_error(f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]")]
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                all_parts.append("\n".join(parts))
                prev_rc = -1
                break
            # 逐段标注：成功段不输出[exit code: 0]，失败段保留各自退出码；
            # 本次调用的总退出码在全部段结束后于末尾统一输出（单段路径的总码
            # 在首行，属历史格式，位置差异为已知记录项）
            parts = []
            if rc != 0:
                parts.append(rc_line(rc))
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            if parts:
                all_parts.append("\n".join(parts))
            prev_rc = rc
            continue

        # 执行单段（Popen + 进程树杀，与 _execute_win32 行为统一）
        seg_budget = remaining_timeout
        seg_start = time.time()
        try:
            proc = subprocess.Popen(
                seg,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=local_cwd,
                # 共用路径：Unix 用 start_new_session，Windows 忽略之，改用独立
                # 无窗口控制台（WIN_NO_WINDOW 在 Unix 恒为 0，不影响 Unix 路径）
                start_new_session=True,
                creationflags=BashRuntime.WIN_NO_WINDOW,
                env=BashRuntime.utf8_env,
            )
        except (OSError, ValueError) as e:
            # ValueError: 段含NUL等非法字符时 Popen 拒绝启动
            all_parts.append(error_line(f"段{i}启动失败: {e}"))
            prev_rc = -1
            break

        with BashRuntime.active_proc_lock:
            BashRuntime.active_proc = proc

        try:
            stdout_chunks = []
            stderr_chunks = []

            def _reader(stream, chunks):
                try:
                    while True:
                        data = stream.read(4096)
                        if not data:
                            break
                        chunks.append(data)
                except Exception:
                    pass

            t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
            for t in (t_out, t_err):
                t.start()

            deadline = time.time() + seg_budget
            timed_out = False

            while proc.poll() is None:
                if time.time() >= deadline:
                    timed_out = True
                    break
                if BashRuntime.interrupted:
                    BashRuntime.interrupted = False
                    was_interrupted = True
                    break
                time.sleep(0.05)

            seg_elapsed = time.time() - seg_start
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            # 先杀进程树再收尾输出（与 _execute_win32 一致）
            if was_interrupted or timed_out:
                _kill_proc_tree(proc)
                proc.wait(timeout=5)

            _drain_readers(t_out, t_err)

            if was_interrupted:
                break

            if timed_out:
                out = _decode_output(b"".join(stdout_chunks))
                err = _decode_output(b"".join(stderr_chunks))
                parts = [tag_error(f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]")]
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                all_parts.append("\n".join(parts))
                prev_rc = -1
                break

            out = _decode_output(b"".join(stdout_chunks))
            err = _decode_output(b"".join(stderr_chunks))
            # 成功的段不逐段输出[exit code: 0]（AI视角是纯噪音），
            # 失败段保留各自的退出码标注便于定位；总退出码统一在末尾输出
            parts = []
            if proc.returncode != 0:
                parts.append(rc_line(proc.returncode))
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            if parts:
                all_parts.append(_with_multiline_hint("\n".join(parts), seg))
            prev_rc = proc.returncode
        finally:
            with BashRuntime.active_proc_lock:
                BashRuntime.active_proc = None

    if was_interrupted:
        all_parts.append("[用户中断]")

    # set/export 段提示：多段命令逐段独立进程执行，前段的环境变量设置
    # 不作用于后续段（与真实 cmd/bash 同行 && 的语义不同），静默失效会
    # 误导 AI。不拦截、附提示引导用 `cmd /c "set X=Y && 命令"` 形式
    # （引号内 && 不拆分，整条交给 shell，语义与真实终端一致）。
    if any(re.match(r"(?i)\s*(?:set|export)\s", seg) for _op, seg in segments[:-1]):
        all_parts.append(
            "[提示: 各段独立进程执行，set/export 设置的环境变量不作用于后续段。"
            "如需跨命令生效，请用 cmd /c \"set X=Y && 命令\" 形式（或 bash 前置赋值 X=Y 命令）]")

    # 段内含 cd（非纯 cd 段）提示：`&`/`;` 混用段在独立子进程执行，段内 cd
    # 不作用于后续段（与真实终端同会话语义不同），静默失效会让后续命令在
    # 旧目录执行——附提示引导用独立 && cd 段形式（与 set/export 提示对齐）
    if any((not _is_cd_command(seg))
           and re.search(r"(?i)(?:^|[&|;(]\s*)cd(\s|$)", seg)
           for _op, seg in segments[:-1]):
        all_parts.append(
            "[提示: 段内含 cd 的命令在独立子进程内执行，cd 不作用于后续段。"
            "如需切换目录后继续执行，请用独立段形式：cd <目录> && <命令>]")

    # 总退出码（最后执行段的退出码；超时/中断时不显示，避免误导AI）
    if prev_rc >= 0 and not was_interrupted:
        all_parts.append(rc_line(prev_rc))

    return _truncate_output("\n".join(all_parts), max_output_chars)
