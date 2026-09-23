"""Shell 前台执行器 —— 平台自适应送达、cd 持久化、多段执行、python 载荷直执行与结果组装。

行为逐行搬运自旧实现 `narnat_agent/tools/bash/__init__.py`，只做结构重组：
Windows/Unix 双路径、中断清零惯例、结果文案与截断口径全部保持等价。

- 平台自适应：Windows 经 cmd 解析（`shell=True`）；Linux/macOS 经 `bash -c`
  （优先 bash、缺失回退 sh，两者皆无时返回固定错误行）。
- cd 持久化：纯 cd 命令由宿主直接 `os.chdir`，使后续所有工具共享新目录。
- 多段执行：引号外、括号组外、跳过 cmd 转义 `^`，按 `&&`/`||` 切分后逐段执行，
  每段独立预算、短路跳过、末尾追加总退出码。
- python 载荷直执行：`python -c "…"` 形态绕过 shell 直接创建解释器进程，
  载荷原样送达（多行/`%`/`&`/`|` 无需转义），尾随 `2>&1`（剥离）与 `|`/`>` 后缀单独解析。
- 输出组装：退出码行 + stdout 段 + stderr 段 + 提示符；中断/超时走各自形态；
  超过 `max_output_chars` 时首尾保留并插入截断提示（切点做标签吸附）。

结构说明：旧实现的 `_collect_proc_output` 与两处内联的"读线程 + 轮询 + 杀树 +
解码"重复代码收敛为 `ShellExecutor._collect` 单一实现；旧自由函数
`_execute_win32` / `_execute_py_*` / `_execute_segments` 改为需要运行态的
`ShellExecutor` 方法；只读的字符串/组装工具仍为模块级纯函数。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import NamedTuple, Optional

from ..signal import error_line, rc_line, safe_cut_points, tag_error
from ..token_estimate import estimate_text_tokens
from .process import UTF8_ENV, ShellRuntime, decode_output, drain_readers, find_executable, kill_proc_tree

__all__ = [
    "PLATFORM_LABEL",
    "RE_DELETE",
    "RE_GIT",
    "ShellExecutor",
]

# 删除命令正则
# 边界后跟空白或/：覆盖无空格变体（rd/s、del/f、rmdir/q）及erase/format；
# \b边界防止误伤 delphi、3rd、formatting 等普通词
RE_DELETE = re.compile(
    r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)

# 匹配 git 命令的简单正则（出现 git 即命中）
RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

# 识别 `python -c "code"` 形态（py/python3/pythonw及全路径），用于绕过cmd直执行。
# exe: 解释器名或路径（可带盘符/空格，不可带引号）；flags: -c 前的真实旗标
# （排除 -c/-m 自身及引号开头项）；tail: -c 后的整段载荷（re.S 允许多行）。
RE_PY_C_DIRECT = re.compile(
    r"^(?i:(?P<exe>(?:[A-Za-z]:)?[\w.\\/ -]*?py(?:thon)?\d*(?:w)?(?:\.exe)?))"
    r"(?P<flags>(?:\s+(?:-(?!c\b|m\b)\S+|[^\s\"-]\S+))*)\s+-c\s+(?P<tail>.+)$",
    re.S,
)

# 解释器基名形态（防误命中 spy.exe 等）：py / python / python3 / pythonw / py3w …
PY_BASENAME_RE = re.compile(r"py(?:thon)?\d*(?:w)?")

POLL_INTERVAL = 0.05
"""前台进程轮询间隔（秒）：超时与中断的检查粒度。"""
READ_CHUNK = 4096
"""读线程读块大小（字节）。"""
KILL_WAIT_SECONDS = 5
"""杀树后等待主进程退出的上限（秒）。"""

PLATFORM_LABEL = "Windows(cmd)" if sys.platform == "win32" else "Linux/macOS(bash)"
"""工具描述中的平台标签。"""


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
    """解析后缀（suffix[0] 为 '|' 或 '>'）。返回 None 表示形态不支持（回退原路径）。

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
    # 裸路径：单 token（裸路径不能含空格）
    toks = rest.split(None, 1)
    if len(toks) > 1:
        return None
    return (mode, toks[0])


def _try_extract_py_code(seg: str):
    """识别 `python -c "code"` 形态的段。命中返回 (exe, flags, code_tail, spec)，否则 None。

    spec: None 无后缀 | ('pipe', cmd) | ('w'|'a', path) | ('discard',)
    仅当 -c 后为双引号包裹（允许尾随 2>&1、|管道、>重定向等高频后缀）、
    解释器可解析且实为 .exe 时走直执行路径；其余一律回退 shell 原路径，零回归。

    尾随 ` 2>&1` 剥离：工具本就合并展示 stdout+stderr，语义等价。
    """
    m = RE_PY_C_DIRECT.match(seg)
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
            return None  # 不支持的后缀形态 → 回退原路径
    else:
        spec = None
    if not code_tail.endswith('"'):
        return None
    resolved = shutil.which(exe)
    if resolved is None:
        return None
    base = os.path.splitext(os.path.basename(resolved))[0].lower()
    if not PY_BASENAME_RE.fullmatch(base):
        return None  # 非python解释器（如 spy.exe 等误命中）→ 交给shell
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in ("", ".exe"):
        return None  # .bat垫片等 → 交给shell处理
    return exe, flags, code_tail, spec


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
                two = command[i:i + 2]
                if two in ("&&", "||"):
                    splits.append((i, two))
                    i += 1
        i += 1

    if not splits:
        return [("", command.strip())]

    result = [("", command[:splits[0][0]].strip())]
    for j, (pos, op) in enumerate(splits):
        next_pos = splits[j + 1][0] if j + 1 < len(splits) else len(command)
        result.append((op, command[pos + 2:next_pos].strip()))
    return result


def _is_cd_command(cmd: str) -> bool:
    """判断是否为 cd/chdir 命令（仅纯cd，不含 &/|/; 等复合操作符）。"""
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


def _extract_cd_path(cmd: str) -> Optional[str]:
    """从 cd 命令中提取目标路径，处理 /d 等 cmd 标志。

    返回 None 表示无参数 cd（单段路径下：Windows 仅显示当前目录、Unix 回 $HOME）。
    """
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


class _ProcResult(NamedTuple):
    """子进程执行结果。

    - `rc` / `out` / `err` / `status`：退出码、stdout 文本、stderr 文本、执行状态
      （`ok` / `timeout` / `interrupt`，后两者已杀进程树）；
    - `wait_elapsed`：等待结束时刻的耗时（不含杀树与读线程收尾）——多段执行以它为
      「段耗时」口径（与旧实现同点计时）。
    """

    rc: int
    out: str
    err: str
    status: str
    wait_elapsed: float


def _truncate_output(text: str, max_chars: int) -> str:
    """截断输出：保留头部和尾部（尾部含提示符，对AI判断shell状态至关重要），中段提示。

    切点先做标签吸附（safe_cut_points）：框架标签不允许被切开——残缺
    片段无法被 strip_tags 匹配，会泄漏给AI并让失败判定失效。

    `max_chars` 为 0 或负数时不做提前拦截（命令已执行完毕），结果仅剩错误行
    ——兼容怪癖保持：副作用已发生、只丢输出。
    """
    if max_chars <= 0:
        return error_line("max_output_chars需为正整数")
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    head_end, tail_start = safe_cut_points(text, head, len(text) - (max_chars - head))
    est = estimate_text_tokens(text)  # ≈token（AI预算单位，混合密度估算）
    return (
        text[:head_end]
        + f"\n...[中间截断: 输出共{len(text)}字符, 已保留首{head_end}字符+尾{len(text) - tail_start}字符(≈{est}token)。增大max_output_chars可获取完整输出]\n"
        + text[tail_start:]
    )


def _format_prompt() -> str:
    """返回当前路径提示符，仿终端显示（Windows 为 `{cwd}>`、Unix 为 `~$/~/子路径$/绝对路径$`）。"""
    cwd = os.getcwd()
    if sys.platform == "win32":
        return f"{cwd}>"
    home = os.path.expanduser("~")
    if cwd == home:
        return "~$ "
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):] + "$ "
    return f"{cwd}$ "


def _format_result(rc: int, out: str, err: str, status: str,
                   timeout: int, max_output_chars: int) -> str:
    """把执行结果组装成统一输出（正常/中断/超时三形态，末尾恒有提示符）。"""
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
    return _truncate_output("\n".join(parts) + "\n" + _format_prompt(), max_output_chars)


class ShellExecutor:
    """前台命令执行器 —— 把一条命令送达本地 shell 并按固定格式组装结果。

    执行面全部经 `ShellRuntime` 读写中断标志与当前进程：入口清零 → 轮询检查点
    消费标志 → 超时/中断时先杀整棵进程树再收尾输出。
    """

    def __init__(self, runtime: ShellRuntime) -> None:
        self._runtime = runtime

    # ═══════════════════════════════════════════════════════════
    # 入口：平台分支
    # ═══════════════════════════════════════════════════════════

    def run(self, command: str, timeout: int, max_output_chars: int) -> str:
        """执行前台命令（平台自适应），返回给 LLM 的完整结果文本。"""
        # ═════════════════════════════════════════════════════════
        # Windows: cmd /c 子进程（stdin 隔离为 DEVNULL，防止挂起子进程偷吃ESC）
        # ═════════════════════════════════════════════════════════
        if sys.platform == "win32":
            # 入口清零：上轮残留的ESC中断标志不污染本轮（覆盖单段/多段全部子路径）
            self._runtime.clear_interrupt()

            # cd 命令：同步更新 Python 进程的 CWD（供 Read/Glob 等工具使用）
            if _is_cd_command(command):
                path = _extract_cd_path(command)
                if path is None:
                    # 无参数cd：仅显示当前目录（与cmd.exe行为一致）
                    return f"{rc_line(0)}\n{_format_prompt()}"
                try:
                    os.chdir(path)
                except OSError as e:
                    # 带上退出码标记：与普通命令失败形态一致，AI一眼识别失败
                    return f"cd: {e}\n{rc_line(1)}\n{_format_prompt()}"
                return f"{rc_line(0)}\n{_format_prompt()}"

            # 多段命令(&&/||)由Python端拆分后逐段执行
            segments = _split_commands(command)
            if len(segments) > 1:
                return self._run_segments(segments, timeout, max_output_chars)

            # python -c "code" 形态：绕过cmd直执行，多行/%/&/|等原样传给解释器
            # 尾随高频后缀（2>&1、|管道、>重定向）已剥离，一并直执行
            py = _try_extract_py_code(segments[0][1])
            if py is not None:
                exe, flags, code_tail, spec = py
                if spec is None:
                    rc, out, err, status, _ = self._run_py_direct(
                        exe, flags, code_tail, timeout
                    )
                else:
                    rc, out, err, status, _ = self._run_py_suffixed(
                        exe, flags, code_tail, spec, timeout
                    )
                return _format_result(rc, out, err, status, timeout, max_output_chars)

            return self._run_win32(command, timeout, max_output_chars)

        # ═════════════════════════════════════════════════════════
        # Linux/macOS: bash -c 子进程
        # ═════════════════════════════════════════════════════════
        shell = find_executable("bash", "sh")
        if shell is None:
            return error_line("未找到shell，请安装bash或sh后重试")

        # cd 命令：同步更新 Python 进程的 CWD（与 Windows 分支一致）。
        # cd 若走 bash -c 子进程执行，目录切换不持久且无任何提示，
        # AI 会误以为已切换目录（工具描述承诺"单独执行 cd 可改变后续调用的当前目录"）
        if _is_cd_command(command):
            path = _extract_cd_path(command)
            if path is None:
                # 无参数cd：bash 语义是回到 $HOME（cmd 是显示当前目录，已在 Windows 分支处理）
                path = os.path.expanduser("~")
            try:
                os.chdir(path)
            except OSError as e:
                return f"cd: {e}\n{rc_line(1)}\n{_format_prompt()}"
            return f"{rc_line(0)}\n{_format_prompt()}"

        # 多段命令(&&/||)由Python端拆分后逐段执行
        segments = _split_commands(command)
        if len(segments) > 1:
            return self._run_segments(segments, timeout, max_output_chars)

        # 用新进程组，确保能 killpg 杀整棵树
        # stdin 隔离为 DEVNULL：Shell 是纯管道语义（不支持交互，交互走
        # Terminal/Serial 工具），且挂起子进程继承控制台 stdin 会偷吃用户
        # 的 ESC 按键导致打断失效（同 Windows 分支）
        try:
            proc = subprocess.Popen(
                [shell, "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                start_new_session=True,
                env=UTF8_ENV,
            )
        except FileNotFoundError as e:
            return error_line(f"Shell未找到: {e}")
        except (OSError, ValueError) as e:
            # ValueError: 命令含NUL等非法字符时 Popen 拒绝启动
            return error_line(f"启动失败: {e}")

        self._runtime.clear_interrupt()  # 入口清零：上轮残留的中断标志不污染本轮
        result = self._collect(proc, timeout)
        return _format_result(result.rc, result.out, result.err, result.status,
                              timeout, max_output_chars)

    # ═══════════════════════════════════════════════════════════
    # 单段执行（Windows / 直执行 / 管道）
    # ═══════════════════════════════════════════════════════════

    def _run_win32(self, command: str, timeout: int, max_output_chars: int) -> str:
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
                env=UTF8_ENV,
            )
        except FileNotFoundError as e:
            return error_line(f"cmd.exe未找到: {e}")
        except (OSError, ValueError) as e:
            # ValueError: 命令行含NUL等非法字符时 Popen 拒绝启动
            return error_line(f"启动失败: {e}")

        result = self._collect(proc, timeout)
        return _format_result(result.rc, result.out, result.err, result.status,
                              timeout, max_output_chars)

    def _run_py_direct(self, exe: str, flags: str, tail: str, timeout: int):
        """绕过cmd直接CreateProcess执行 python -c 载荷（shell=False）。

        载荷由 CommandLineToArgvW 规则解析：双引号内的换行/%/&/|/<等一律字面
        传给解释器，从根上规避 cmd 吞多行、改写特殊字符的问题。
        stdin 隔离为 DEVNULL（同 _run_win32 的 ESC 偷吃防护）。
        返回 `_ProcResult`（与 `_collect` 一致）。
        """
        try:
            proc = subprocess.Popen(
                f'"{exe}"{flags} -c {tail}',
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                env=UTF8_ENV,
            )
        except (OSError, ValueError) as e:
            # ValueError: 载荷含NUL等非法字符时 Popen 拒绝启动
            return _ProcResult(1, "", f"启动失败: {e}", "ok", 0.0)
        return self._collect(proc, timeout)

    def _run_py_suffixed(self, exe: str, flags: str, tail: str, spec, timeout: int):
        """直执行 python -c 并处理尾随后缀（|管道 / >重定向）。

        spec: ('pipe', cmd) | ('w'|'a', path) | ('discard',)
        返回 `_ProcResult`（与 `_collect` 一致）。
        """
        if spec[0] == "pipe":
            return self._run_py_pipe(exe, flags, tail, spec[1], timeout)

        result = self._run_py_direct(exe, flags, tail, timeout)
        if result.status != "ok" or spec[0] == "discard":
            # 中断/超时不写文件；>nul 直接丢弃 stdout（stderr 仍展示）
            return _ProcResult(result.rc, "", result.err, result.status, result.wait_elapsed)

        mode, target = spec
        try:
            with open(target, mode + "b") as f:
                # PYTHONIOENCODING=utf-8 保证子进程输出UTF-8；\r\n 原样保留（与cmd一致）
                f.write(result.out.encode("utf-8"))
        except OSError as e:
            return _ProcResult(1, "", f"写入文件失败: {e}", "ok", 0.0)
        return _ProcResult(result.rc, "", result.err, result.status, result.wait_elapsed)

    def _run_py_pipe(self, exe: str, flags: str, tail: str, pipe_cmd: str, timeout: int):
        """直执行 python -c，stdout 经管道命令过滤后合并展示。

        先直执行 python（超时/ESC语义不变）；成功后把 stdout 字节喂给管道命令，
        剩余时间预算内收集。python 的 stderr 与管道 stderr 合并展示。
        """
        start = time.time()
        py_result = self._run_py_direct(exe, flags, tail, timeout)
        elapsed = time.time() - start
        if py_result.status != "ok":
            return py_result  # 中断/超时跳过管道阶段

        remaining = max(0.1, timeout - elapsed)
        try:
            proc = subprocess.Popen(
                pipe_cmd,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                env=UTF8_ENV,
            )
        except (OSError, ValueError) as e:
            return _ProcResult(
                1, py_result.out,
                f"{py_result.err}\n[管道启动失败: {e}]".strip(), "ok", elapsed,
            )

        def _feed():
            # 管道命令可能提前退出不读stdin → BrokenPipeError 忽略
            try:
                proc.stdin.write(py_result.out.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=_feed, daemon=True).start()
        result = self._collect(proc, remaining)
        errs = [e for e in (py_result.err.strip(), result.err.strip()) if e]
        return _ProcResult(result.rc, result.out, "\n".join(errs),
                           result.status, result.wait_elapsed)

    # ═══════════════════════════════════════════════════════════
    # 多段执行（&& / ||）
    # ═══════════════════════════════════════════════════════════

    def _run_segments(self, segments: list, timeout: int, max_output_chars: int) -> str:
        """逐段执行 &&/|| 分割的命令，短路段跳过。

        虽然 cmd /c 本身支持 &&，但 Python 端拆分为逐段执行以获得：
        1. 每段独立的超时控制（超时时强杀整棵进程树）
        2. ESC 可在段内/段间打断
        3. cd 命令作用到 os.chdir() 而非子进程
        """
        self._runtime.clear_interrupt()  # 入口清零：上轮残留的中断标志不污染本轮
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
            # 尾随高频后缀（2>&1、|管道、>重定向）已剥离，一并直执行
            py = _try_extract_py_code(seg)
            if py is not None:
                exe, flags, code_tail, spec = py
                seg_budget = remaining_timeout
                seg_start = time.time()
                if spec is None:
                    result = self._run_py_direct(exe, flags, code_tail, seg_budget)
                else:
                    result = self._run_py_suffixed(exe, flags, code_tail, spec, seg_budget)
                seg_elapsed = time.time() - seg_start
                remaining_timeout = max(0, remaining_timeout - seg_elapsed)

                if result.status == "interrupt":
                    was_interrupted = True
                    break
                if result.status == "timeout":
                    parts = [tag_error(f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]")]
                    if result.out.strip():
                        parts.append(result.out.strip())
                    if result.err.strip():
                        parts.append(f"[stderr]\n{result.err.strip()}")
                    all_parts.append("\n".join(parts))
                    prev_rc = -1
                    break
                # 与shell单段一致：成功段不输出[exit code: 0]，失败段保留退出码
                parts = []
                if result.rc != 0:
                    parts.append(rc_line(result.rc))
                if result.out.strip():
                    parts.append(result.out.strip())
                if result.err.strip():
                    parts.append(f"[stderr]\n{result.err.strip()}")
                if parts:
                    all_parts.append("\n".join(parts))
                prev_rc = result.rc
                continue

            # 执行单段（Popen + 进程树杀，与 _run_win32 行为统一）
            seg_budget = remaining_timeout
            try:
                proc = subprocess.Popen(
                    seg,
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=os.getcwd(),
                    start_new_session=True,
                    env=UTF8_ENV,
                )
            except (OSError, ValueError) as e:
                # ValueError: 段含NUL等非法字符时 Popen 拒绝启动
                all_parts.append(error_line(f"段{i}启动失败: {e}"))
                prev_rc = -1
                break

            result = self._collect(proc, seg_budget)
            # 段耗时取等待结束时刻（杀树与读线程收尾不计入），与旧实现同点计时
            seg_elapsed = result.wait_elapsed
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            if result.status == "interrupt":
                was_interrupted = True
                break
            if result.status == "timeout":
                parts = [tag_error(f"[超时: 命令执行超过{max(seg_elapsed, 1.0):.1f}秒，已终止]")]
                if result.out.strip():
                    parts.append(result.out.strip())
                if result.err.strip():
                    parts.append(f"[stderr]\n{result.err.strip()}")
                all_parts.append("\n".join(parts))
                prev_rc = -1
                break

            # 成功的段不逐段输出[exit code: 0]（AI视角是纯噪音），
            # 失败段保留各自的退出码标注便于定位；总退出码统一在末尾输出
            parts = []
            if result.rc != 0:
                parts.append(rc_line(result.rc))
            if result.out.strip():
                parts.append(result.out.strip())
            if result.err.strip():
                parts.append(f"[stderr]\n{result.err.strip()}")
            if parts:
                all_parts.append("\n".join(parts))
            prev_rc = result.rc

        if was_interrupted:
            all_parts.append("[用户中断]")

        # 总退出码（最后执行段的退出码；超时/中断时不显示，避免误导AI）
        if prev_rc >= 0 and not was_interrupted:
            all_parts.append(rc_line(prev_rc))

        return _truncate_output("\n".join(all_parts) + "\n" + _format_prompt(), max_output_chars)

    # ═══════════════════════════════════════════════════════════
    # 收集与收尾
    # ═══════════════════════════════════════════════════════════

    def _collect(self, proc: subprocess.Popen, timeout: float) -> _ProcResult:
        """等待子进程并收集输出（读线程 + 超时/ESC中断 + 进程树杀）。

        返回 `_ProcResult`；status: 'ok' | 'timeout' | 'interrupt'
        （超时/中断时已杀进程树）。

        超时/中断时先杀进程树再收尾输出："超时/中断"的语义是命令已被终止；
        若先 join 读线程，进程在 join 窗口内自然跑完时会输出完整结果却仍标注
        "已终止"，语义矛盾且每次超时白等收尾宽限。
        """
        start = time.time()
        self._runtime.attach(proc)
        try:
            stdout_chunks = []
            stderr_chunks = []

            def _reader(stream, chunks):
                try:
                    while True:
                        data = stream.read(READ_CHUNK)
                        if not data:
                            break
                        chunks.append(data)
                except Exception:
                    pass

            t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
            for t in (t_out, t_err):
                t.start()

            deadline = time.time() + timeout
            timed_out = False
            was_interrupted = False

            while proc.poll() is None:
                if time.time() >= deadline:
                    timed_out = True
                    break
                if self._runtime.consume_interrupt():
                    was_interrupted = True
                    break
                time.sleep(POLL_INTERVAL)

            wait_elapsed = time.time() - start  # 等待结束时刻（收尾耗时不计入）
            if was_interrupted or timed_out:
                kill_proc_tree(proc)
                proc.wait(timeout=KILL_WAIT_SECONDS)

            drain_readers(t_out, t_err)

            out = decode_output(b"".join(stdout_chunks))
            err = decode_output(b"".join(stderr_chunks))

            if was_interrupted:
                return _ProcResult(proc.returncode, out, err, "interrupt", wait_elapsed)
            if timed_out:
                return _ProcResult(proc.returncode, out, err, "timeout", wait_elapsed)
            return _ProcResult(proc.returncode, out, err, "ok", wait_elapsed)
        finally:
            self._runtime.detach()
