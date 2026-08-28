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


# 删除命令正则
# 边界后跟空白或/：覆盖无空格变体（rd/s、del/f、rmdir/q）及erase/format；
# \b边界防止误伤 delphi、3rd、formatting 等普通词
_RE_DELETE = re.compile(
    r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)

# 匹配 git 命令的简单正则（出现 git 即命中）
_RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

# 识别 `python -c "code"` 形态（py/python3/pythonw及全路径），用于绕过cmd直执行。
# exe: 解释器名或路径（可带盘符/空格，不可带引号）；flags: -c 前的真实旗标
# （排除 -c/-m 自身及引号开头项）；tail: -c 后的整段载荷（re.S 允许多行）。
_RE_PY_C_DIRECT = re.compile(
    r"^(?i:(?P<exe>(?:[A-Za-z]:)?[\w.\\/ -]*?py(?:thon)?\d*(?:w)?(?:\.exe)?))"
    r"(?P<flags>(?:\s+(?:-(?!c\b|m\b)\S+|[^\s\"-]\S+))*)\s+-c\s+(?P<tail>.+)$",
    re.S,
)


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
            return tail, None  # 引号后有其他token（追加参数等）→ 回退
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
    m = _RE_PY_C_DIRECT.match(seg)
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

# 子进程环境变量：强制 UTF-8 编码，解决 Windows 下 Python print emoji 等
# Unicode 字符在 GBK 代码页下报 UnicodeEncodeError 的问题
_utf8_env = os.environ.copy()
_utf8_env["PYTHONIOENCODING"] = "utf-8"
_utf8_env["PYTHONUTF8"] = "1"

# 当前运行的前台进程（agent层ESC打断后可调用kill_active杀掉）
_active_proc: Optional[subprocess.Popen] = None
_active_proc_lock = threading.Lock()

# ESC打断标记，kill_active()设置，execute()检查后清除
_interrupted = False

_PLATFORM_LABEL = "Windows(cmd)" if sys.platform == "win32" else "Linux/macOS(bash)"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Shell",
        "description": f"本地Shell — 在{_PLATFORM_LABEL}执行命令。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令"},
                "timeout": {"type": "integer", "description": "超时秒数（正整数，默认120，超时后命令会被终止）"},
                "max_output_chars": {"type": "integer", "description": "最大输出字符数（正整数，默认4000）"},
            },
            "required": ["command"],
        },
    },
}


def kill_active():
    """杀掉当前正在运行的前台子进程（ESC打断时由agent调用）

    杀进程树改在后台线程执行：ESC打断后主线程立即返回，
    输入界面马上还给用户；杀树期间用户输入新命令不受影响
    （新命令是新Popen，会覆盖_active_proc，后台线程持有旧proc引用）。
    """
    global _interrupted
    _interrupted = True
    with _active_proc_lock:
        proc = _active_proc
    if proc is not None and proc.poll() is None:
        threading.Thread(target=_kill_proc_tree, args=(proc,), daemon=True).start()


def _find_executable(*names: str) -> Optional[str]:
    """按优先级查找可执行文件，返回第一个找到的名称或路径。"""
    import shutil
    for name in names:
        if shutil.which(name):
            return name
    return None


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
    # cmd 无空格简写: cd..(父目录)、cd...(祖父目录)、cd\(根目录)
    if lower in ("cd..", "cd...", "chdir..", "chdir...", "cd\\", "chdir\\"):
        return True
    return lower.startswith("cd ") or lower == "cd" or lower.startswith("chdir ") or lower == "chdir"


def _has_nonpersistent_cd(command: str) -> bool:
    """检测复合命令中是否存在不会持久化的 cd 段。

    纯 cd 命令由 _is_cd_command 路径处理（os.chdir 持久化）；
    && / || 分段由 _execute_segments 处理（cd 段同样持久化）；
    但单个 & 或 ; 不分段，整条命令在子进程执行，其中的 cd 段只在子进程内生效。
    此时返回 True，调用方追加提示告知 AI，避免 AI 误以为目录已切换。
    """
    if "&" not in command and ";" not in command:
        return False
    # 与执行路径一致：先按 &&/|| 分段（分段器已把 cd 段拆出并持久化），
    # 仅检查各段内部含单个 & 或 ; 的 cd（如 `cd /tmp; ls`、`echo hi & cd x`）。
    # 不能直接在整条命令上搜 `(^|&|;)\s*cd`：`cd X && cmd 2>&1` 中 cd 已持久化，
    # 但 2>&1 的 & 会让整条正则命中开头的 cd，误报"cd 不持久化"误导 AI。
    for _op, seg in _split_commands(command):
        if "&" in seg or ";" in seg:
            if re.search(r"(^|&|;)\s*(cd|chdir)\s+", seg, re.IGNORECASE):
                return True
    return False


def _append_cd_hint(result: str, command: str) -> str:
    """复合命令含不持久化的 cd 段时，在结果尾部（prompt 行后）追加提示。"""
    if not _has_nonpersistent_cd(command):
        return result
    hint = "[提示: 复合命令中的 cd 仅在该命令内生效，不会改变后续工具调用的当前目录。如需切换目录，请单独执行 cd 命令]"
    lines = result.rstrip("\n").split("\n")
    # 找到最后一个 prompt 行（以 > 或 $ 结尾），提示插到其后
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].rstrip().endswith((">", "$")):
        idx -= 1
    if idx >= 0:
        lines.insert(idx + 1, hint)
        return "\n".join(lines)
    return result.rstrip("\n") + "\n" + hint


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


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部含提示符，对AI判断shell状态至关重要），中段提示"""
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


def _format_prompt() -> str:
    """返回当前路径提示符，仿终端显示。"""
    cwd = os.getcwd()
    if sys.platform == "win32":
        return f"{cwd}>"
    else:
        home = os.path.expanduser("~")
        if cwd == home:
            return "~$ "
        elif cwd.startswith(home + os.sep):
            return "~" + cwd[len(home):] + "$ "
        else:
            return f"{cwd}$ "


def execute(
    command: str,
    timeout: int = 120,
    max_output_chars: int = 4000,
    _tool_context=None,
) -> str:
    """
    执行shell命令。AI写什么就执行什么，不做翻译。

    Windows: 持久化cmd会话，命令直写stdin，行为与真实cmd窗口一致。
    Linux/macOS: bash -c 子进程。

    Args:
        command: shell命令
        timeout: 超时秒数
        max_output_chars: 返回内容最大字符数，正整数，默认4000
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        stdout + stderr + 退出码
    """
    global _interrupted  # 函数内多分支读写该标志，统一在函数级声明
    # AI可能传字符串类型的数值参数，统一转int（与Grep/Read容错风格一致）
    try:
        timeout = int(timeout) if timeout is not None else 120
        max_output_chars = int(max_output_chars) if max_output_chars is not None else 4000
    except (TypeError, ValueError):
        return "[错误: timeout/max_output_chars需为整数]"
    # ── 安全检查：删除命令和git命令根据配置决定是否需要确认 ──
    need_confirm = False
    tc = _tool_context
    if tc and not tc.rm_skip_confirm and _RE_DELETE.search(command):
        need_confirm = True
    elif tc and not tc.git_skip_confirm and _RE_GIT.search(command):
        need_confirm = True

    if need_confirm:
        if sys.platform == "win32":
            if tc and tc.confirm_callback and not tc.confirm_callback(command):
                return "[操作已取消: 此命令需用户确认]"
        else:
            if tc and tc._delete_confirmed:
                tc._delete_confirmed = False
            else:
                if tc is not None:
                    tc.pending_delete = ("Shell", {
                        "command": command,
                        "timeout": timeout,
                        "max_output_chars": max_output_chars,
                    })
                return "__AWAIT_CONFIRM__"

    if timeout <= 0:
        return "[错误: timeout需为正整数（秒）]"

    if _tool_context and _tool_context.max_timeout_seconds > 0:
        timeout = min(timeout, _tool_context.max_timeout_seconds)

    # ═════════════════════════════════════════════════════════════
    # Windows: cmd /c 子进程（stdin 继承 TTY，避免外部工具因管道 stdin 阻塞）
    # ═════════════════════════════════════════════════════════════
    if sys.platform == "win32":
        # 入口清零：上轮残留的ESC中断标志不污染本轮（覆盖单段/多段全部子路径）
        _interrupted = False

        # cd 命令：同步更新 Python 进程的 CWD（供 Read/Glob 等工具使用）
        if _is_cd_command(command):
            path = _extract_cd_path(command)
            if path is None:
                # 无参数cd：仅显示当前目录（与cmd.exe行为一致）
                return f"[exit code: 0]\n{_format_prompt()}"
            try:
                os.chdir(path)
            except OSError as e:
                # 带上退出码标记：与普通命令失败形态一致，AI一眼识别失败
                return f"cd: {e}\n[exit code: 1]\n{_format_prompt()}"
            return f"[exit code: 0]\n{_format_prompt()}"

        # 多段命令(&&/||)由Python端拆分后逐段执行
        segments = _split_commands(command)
        if len(segments) > 1:
            return _append_cd_hint(
                _execute_segments(segments, timeout, max_output_chars, _tool_context),
                command,
            )

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

        return _append_cd_hint(
            _execute_win32(command, timeout, max_output_chars), command
        )

    # ═════════════════════════════════════════════════════════════
    # Linux/macOS: bash -c 子进程（原有逻辑）
    # ═════════════════════════════════════════════════════════════
    shell = _find_executable("bash", "sh")
    if shell is None:
        return "[错误: 未找到shell，请安装bash或sh后重试]"

    # cd 命令：同步更新 Python 进程的 CWD（与 Windows 分支一致）。
    # 此前 cd 走 bash -c 子进程执行，目录切换不持久且无任何提示，
    # AI 会误以为已切换目录（DEFINITION 承诺"单独执行 cd 可改变后续调用的当前目录"）
    if _is_cd_command(command):
        path = _extract_cd_path(command)
        if path is None:
            # 无参数cd：bash 语义是回到 $HOME（cmd 是显示当前目录，已在 Windows 分支处理）
            path = os.path.expanduser("~")
        try:
            os.chdir(path)
        except OSError as e:
            return f"cd: {e}\n[exit code: 1]\n{_format_prompt()}"
        return f"[exit code: 0]\n{_format_prompt()}"

    # 多段命令(&&/||)由Python端拆分后逐段执行
    segments = _split_commands(command)
    if len(segments) > 1:
        return _append_cd_hint(
            _execute_segments(segments, timeout, max_output_chars, _tool_context),
            command,
        )

    shell_cmd = [shell, "-c", command]

    # 用新进程组，确保能 killpg 杀整棵树
    try:
        proc = subprocess.Popen(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            start_new_session=True,
            env=_utf8_env,
        )
    except FileNotFoundError as e:
        return f"[错误: Shell未找到: {e}]"
    except (OSError, ValueError) as e:
        # ValueError: 命令含NUL等非法字符时 Popen 拒绝启动
        return f"[错误: 启动失败: {e}]"

    with _active_proc_lock:
        global _active_proc
        _active_proc = proc

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

        _interrupted = False  # 入口清零：上轮残留的中断标志不污染本轮
        deadline = time.time() + timeout
        timed_out = False
        was_interrupted = False

        while proc.poll() is None:
            if time.time() >= deadline:
                timed_out = True
                break
            if _interrupted:
                was_interrupted = True
                _interrupted = False
                break
            time.sleep(0.05)

        # 先杀进程树再收尾输出："超时/中断"的语义是命令已被终止。
        # 若先 join 读线程(最多5秒)，进程在 join 窗口内自然跑完时会
        # 输出完整结果却仍标注"已终止"，语义矛盾且每次超时白等5秒。
        if was_interrupted or timed_out:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)

        for t in (t_out, t_err):
            t.join(timeout=5.0)

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
            return _truncate_output("\n".join(parts) + "\n" + _format_prompt(), max_output_chars)

        if timed_out:
            parts = []
            out = _decode_output(stdout)
            if out.strip():
                parts.append(out.strip())
            err = _decode_output(stderr)
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            parts.append(f"[超时: 命令执行超过{timeout:.0f}秒，已终止]")
            return _truncate_output("\n".join(parts) + "\n" + _format_prompt(), max_output_chars)

        parts = [f"[exit code: {proc.returncode}]"]
        out = _decode_output(stdout)
        if out.strip():
            parts.append(out.strip())
        err = _decode_output(stderr)
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        return _truncate_output("\n".join(parts) + "\n" + _format_prompt(), max_output_chars)
    finally:
        with _active_proc_lock:
            _active_proc = None


def _collect_proc_output(proc: subprocess.Popen, timeout: int, max_output_chars: int):
    """等待子进程并收集输出（读线程 + 超时/ESC中断 + 进程树杀）。

    返回 (returncode, stdout_text, stderr_text, status)，
    status: 'ok' | 'timeout' | 'interrupt'（超时/中断时已杀进程树）。
    """
    global _active_proc, _interrupted

    with _active_proc_lock:
        _active_proc = proc

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
            if _interrupted:
                was_interrupted = True
                _interrupted = False
                break
            time.sleep(0.05)

        # 先杀进程树再收尾输出："超时/中断"的语义是命令已被终止
        if was_interrupted or timed_out:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)

        for t in (t_out, t_err):
            t.join(timeout=5.0)

        out = _decode_output(b"".join(stdout_chunks))
        err = _decode_output(b"".join(stderr_chunks))

        if was_interrupted:
            return proc.returncode, out, err, "interrupt"
        if timed_out:
            return proc.returncode, out, err, "timeout"
        return proc.returncode, out, err, "ok"
    finally:
        with _active_proc_lock:
            _active_proc = None


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
        parts.append(f"[超时: 命令执行超过{timeout:.0f}秒，已终止]")
    else:
        parts = [f"[exit code: {rc}]"]
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
    return _truncate_output("\n".join(parts) + "\n" + _format_prompt(), max_output_chars)


def _execute_win32(command: str, timeout: int, max_output_chars: int) -> str:
    """Windows: shell=True 起子进程。cmd 交互式解析（引号按用户预期处理），
    stdin 继承控制台（避免 eza 等工具因管道 stdin 阻塞）。"""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            env=_utf8_env,
        )
    except FileNotFoundError as e:
        return f"[错误: cmd.exe未找到: {e}]"
    except (OSError, ValueError) as e:
        # ValueError: 命令行含NUL等非法字符时 Popen 拒绝启动
        return f"[错误: 启动失败: {e}]"

    rc, out, err, status = _collect_proc_output(proc, timeout, max_output_chars)
    return _format_result(rc, out, err, status, timeout, max_output_chars)


def _execute_py_direct(exe: str, flags: str, tail: str,
                       timeout: int, max_output_chars: int):
    """绕过cmd直接CreateProcess执行 python -c 载荷（shell=False）。

    载荷由 CommandLineToArgvW 规则解析：双引号内的换行/%/&/|/<等一律字面
    传给解释器，从根上规避 cmd 吞多行、改写特殊字符的问题。
    返回 (rc, out, err, status)，格式与 _collect_proc_output 一致。
    """
    try:
        proc = subprocess.Popen(
            f'"{exe}"{flags} -c {tail}',
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            env=_utf8_env,
        )
    except (OSError, ValueError) as e:
        # ValueError: 载荷含NUL等非法字符时 Popen 拒绝启动
        return (1, "", f"启动失败: {e}", "ok")
    return _collect_proc_output(proc, timeout, max_output_chars)


def _execute_py_suffixed(exe: str, flags: str, tail: str, spec,
                         timeout: int, max_output_chars: int):
    """直执行 python -c 并处理尾随后缀（|管道 / >重定向）。

    spec: ('pipe', cmd) | ('w'|'a', path) | ('discard',)
    返回 (rc, out, err, status)，格式与 _collect_proc_output 一致。
    """
    if spec[0] == "pipe":
        return _execute_py_pipe(exe, flags, tail, spec[1], timeout, max_output_chars)

    rc, out, err, status = _execute_py_direct(exe, flags, tail, timeout, max_output_chars)
    if status != "ok" or spec[0] == "discard":
        # 中断/超时不写文件；>nul 直接丢弃 stdout（stderr 仍展示）
        return rc, "", err, status

    mode, target = spec
    try:
        with open(target, mode + "b") as f:
            # PYTHONIOENCODING=utf-8 保证子进程输出UTF-8；\r\n 原样保留（与cmd一致）
            f.write(out.encode("utf-8"))
    except OSError as e:
        return (1, "", f"写入文件失败: {e}", "ok")
    return rc, "", err, status


def _execute_py_pipe(exe: str, flags: str, tail: str, pipe_cmd: str,
                     timeout: int, max_output_chars: int):
    """直执行 python -c，stdout 经 cmd 管道命令过滤后合并展示。

    先直执行 python（超时/ESC语义不变）；成功后把 stdout 字节喂给管道命令，
    剩余时间预算内收集。python 的 stderr 与管道 stderr 合并展示。
    """
    start = time.time()
    rc_py, out_py, err_py, status_py = _execute_py_direct(
        exe, flags, tail, timeout, max_output_chars
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
            cwd=os.getcwd(),
            env=_utf8_env,
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
    3. cd 命令作用到 os.chdir() 而非子进程
    """
    global _interrupted, _active_proc
    _interrupted = False  # 入口清零：上轮残留的中断标志不污染本轮
    all_parts = []
    prev_rc = 0
    remaining_timeout = timeout
    was_interrupted = False

    for i, (op, seg) in enumerate(segments):
        # 短路求值
        if op == "&&" and prev_rc != 0:
            all_parts.append(f"[跳过: 前一命令失败(退出码{prev_rc})] {seg}")
            continue
        if op == "||" and prev_rc == 0:
            all_parts.append(f"[跳过: 前一命令成功] {seg}")
            continue

        # cd 命令直接作用于 Python 进程
        if _is_cd_command(seg):
            path = _extract_cd_path(seg)
            if path is None:
                prev_rc = 0  # 无参数cd仅显示，不切换
            else:
                try:
                    os.chdir(path)
                    prev_rc = 0
                except OSError as e:
                    all_parts.append(f"cd: {e}")
                    prev_rc = 1
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
                    exe, flags, code_tail, seg_budget, max_output_chars
                )
            else:
                rc, out, err, status = _execute_py_suffixed(
                    exe, flags, code_tail, spec, seg_budget, max_output_chars
                )
            seg_elapsed = time.time() - seg_start
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            if status == "interrupt":
                was_interrupted = True
                break
            if status == "timeout":
                parts = [f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]"]
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                all_parts.append("\n".join(parts))
                prev_rc = -1
                break
            # 与shell单段一致：成功段不输出[exit code: 0]，失败段保留退出码
            parts = []
            if rc != 0:
                parts.append(f"[exit code: {rc}]")
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                start_new_session=True,
                env=_utf8_env,
            )
        except (OSError, ValueError) as e:
            # ValueError: 段含NUL等非法字符时 Popen 拒绝启动
            all_parts.append(f"[错误: 段{i}启动失败: {e}]")
            prev_rc = -1
            break

        with _active_proc_lock:
            _active_proc = proc

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
                if _interrupted:
                    _interrupted = False
                    was_interrupted = True
                    break
                time.sleep(0.05)

            seg_elapsed = time.time() - seg_start
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            # 先杀进程树再收尾输出（与 _execute_win32 一致）
            if was_interrupted or timed_out:
                _kill_proc_tree(proc)
                proc.wait(timeout=5)

            for t in (t_out, t_err):
                t.join(timeout=5.0)

            if was_interrupted:
                break

            if timed_out:
                out = _decode_output(b"".join(stdout_chunks))
                err = _decode_output(b"".join(stderr_chunks))
                parts = [f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]"]
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
                parts.append(f"[exit code: {proc.returncode}]")
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            if parts:
                all_parts.append("\n".join(parts))
            prev_rc = proc.returncode
        finally:
            with _active_proc_lock:
                _active_proc = None

    if was_interrupted:
        all_parts.append("[用户中断]")

    # 总退出码（最后执行段的退出码；超时/中断时不显示，避免误导AI）
    if prev_rc >= 0 and not was_interrupted:
        all_parts.append(f"[exit code: {prev_rc}]")

    return _truncate_output("\n".join(all_parts) + "\n" + _format_prompt(), max_output_chars)
