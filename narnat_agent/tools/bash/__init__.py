"""Shell工具 —— 纯管道，AI写什么就执行什么

Windows用持久化cmd会话（命令直写stdin，零转义损耗），Linux/macOS用bash -c。
AI自己负责写正确语法，我们只管送达和返回。
"""

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional, Callable

from .cmd_session import CmdSession


# 删除命令正则
_RE_DELETE = re.compile(
    r"\b(rm\s|del\s|Remove-Item\s|rmdir\s|rd\s)",
    re.IGNORECASE,
)

# 匹配 git 命令的简单正则（出现 git 即命中）
_RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

# 子进程环境变量：强制 UTF-8 编码，解决 Windows 下 Python print emoji 等
# Unicode 字符在 GBK 代码页下报 UnicodeEncodeError 的问题
_utf8_env = os.environ.copy()
_utf8_env["PYTHONIOENCODING"] = "utf-8"
_utf8_env["PYTHONUTF8"] = "1"

# ── 持久化 cmd 会话（Windows only）──
_cmd_session: Optional[CmdSession] = None
_cmd_session_lock = threading.Lock()


def _get_cmd_session() -> CmdSession:
    """获取或创建持久化 cmd 会话。进程已死或超时死亡则重建。"""
    global _cmd_session
    with _cmd_session_lock:
        if (_cmd_session is None
                or _cmd_session._proc.poll() is not None
                or _cmd_session._dead):
            if _cmd_session is not None:
                try:
                    _cmd_session.close()
                except Exception:
                    pass
            _cmd_session = CmdSession()
        return _cmd_session


# 当前运行的前台进程（agent层ESC打断后可调用kill_active杀掉，Linux路径使用）
_active_proc: Optional[subprocess.Popen] = None
_active_proc_lock = threading.Lock()

# ESC打断标记，kill_active()设置，execute()检查后清除
_interrupted = False

DEFINITION = {
    "type": "function",
    "function": {
        "name": "Shell",
        "description": f"持久化本地Shell — 在{__import__('sys').platform}执行命令。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令"},
                "timeout": {"type": "integer", "description": "超时秒数（正整数，默认120）"},
                "max_output_chars": {"type": "integer", "description": "最大输出字符数（正整数，默认4000）"},
            },
            "required": ["command"],
        },
    },
}


def kill_active():
    """杀掉当前正在运行的前台子进程（ESC打断时由agent调用）"""
    global _interrupted
    _interrupted = True
    with _active_proc_lock:
        proc = _active_proc
    if proc is not None and proc.poll() is None:
        _kill_proc_tree(proc)


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
    """在引号外按 && 和 || 分割，返回 [(op, cmd), ...]。
    op: '' 表示首段，'&&' 或 '||' 表示后续段。"""
    splits = []  # [(pos, '&&'|'||')]
    in_quote = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote and i + 1 < len(command):
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
    """判断是否为 cd/chdir 命令（仅纯cd，不含 &/| 等复合操作符）"""
    lower = cmd.lower()
    # 拒绝复合命令：含 & | && ||
    if "&" in cmd or "|" in cmd:
        return False
    return lower.startswith("cd ") or lower == "cd" or lower.startswith("chdir ") or lower == "chdir"


def _extract_cd_path(cmd: str) -> Optional[str]:
    """从 cd 命令中提取目标路径，处理 /d 等cmd标志。
    返回 None 表示无参数cd（仅显示当前目录，不切换）。"""
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return None  # 无参数cd：仅显示当前目录，不切换
    args = parts[1]
    # 去掉cmd的 /d 标志
    if args.lower().startswith("/d "):
        args = args[3:].strip()
    return args.strip('"')


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
    """截断输出到指定字符数，超出部分附加提示"""
    if max_chars <= 0:
        return "[错误: max_output_chars需为正整数]"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[已截断: 输出共{len(text)}字符, 当前显示前{max_chars}字符。增大max_output_chars可获取完整输出]"


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
        # cd 命令：同步更新 Python 进程的 CWD（供 Read/Glob 等工具使用）
        if _is_cd_command(command):
            path = _extract_cd_path(command)
            if path is None:
                # 无参数cd：仅显示当前目录（与cmd.exe行为一致）
                return f"[exit code: 0]\n{_format_prompt()}"
            try:
                os.chdir(path)
            except OSError as e:
                return f"cd: {e}\n{_format_prompt()}"
            return f"[exit code: 0]\n{_format_prompt()}"

        # 多段命令(&&/||)由Python端拆分后逐段执行
        segments = _split_commands(command)
        if len(segments) > 1:
            return _execute_segments(
                segments, timeout, max_output_chars, _tool_context
            )

        return _execute_win32(command, timeout, max_output_chars)

    # ═════════════════════════════════════════════════════════════
    # Linux/macOS: bash -c 子进程（原有逻辑）
    # ═════════════════════════════════════════════════════════════
    shell = _find_executable("bash", "sh")
    if shell is None:
        return "[错误: 未找到shell，请安装bash或sh后重试]"

    # 多段命令(&&/||)由Python端拆分后逐段执行
    segments = _split_commands(command)
    if len(segments) > 1:
        return _execute_segments(
            segments, timeout, max_output_chars, _tool_context
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
    except OSError as e:
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

        global _interrupted
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

        for t in (t_out, t_err):
            t.join(timeout=5.0)

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)

        if was_interrupted:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)
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
            _kill_proc_tree(proc)
            proc.wait(timeout=5)
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


def _execute_win32(command: str, timeout: int, max_output_chars: int) -> str:
    """Windows: shell=True 起子进程。cmd 交互式解析（引号按用户预期处理），
    stdin 继承控制台（避免 eza 等工具因管道 stdin 阻塞）。"""
    global _interrupted
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
    except OSError as e:
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

        for t in (t_out, t_err):
            t.join(timeout=5.0)

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)

        if was_interrupted:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)
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
            _kill_proc_tree(proc)
            proc.wait(timeout=5)
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
        except OSError as e:
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

            for t in (t_out, t_err):
                t.join(timeout=5.0)

            seg_elapsed = time.time() - seg_start
            remaining_timeout = max(0, remaining_timeout - seg_elapsed)

            if was_interrupted:
                _kill_proc_tree(proc)
                proc.wait(timeout=5)
                break

            if timed_out:
                _kill_proc_tree(proc)
                proc.wait(timeout=5)
                out = _decode_output(b"".join(stdout_chunks))
                err = _decode_output(b"".join(stderr_chunks))
                parts = [f"[超时: 命令执行超过{int(seg_elapsed)}秒，已终止]"]
                if out.strip():
                    parts.append(out.strip())
                if err.strip():
                    parts.append(f"[stderr]\n{err.strip()}")
                all_parts.append("\n".join(parts))
                prev_rc = -1
                break

            out = _decode_output(b"".join(stdout_chunks))
            err = _decode_output(b"".join(stderr_chunks))
            parts = [f"[exit code: {proc.returncode}]"]
            if out.strip():
                parts.append(out.strip())
            if err.strip():
                parts.append(f"[stderr]\n{err.strip()}")
            all_parts.append("\n".join(parts))
            prev_rc = proc.returncode
        finally:
            with _active_proc_lock:
                _active_proc = None

    if was_interrupted:
        all_parts.append("[用户中断]")

    return _truncate_output("\n".join(all_parts) + "\n" + _format_prompt(), max_output_chars)


def cleanup():
    """程序退出时清理持久化 cmd 会话（由 agent 调用）"""
    global _cmd_session
    with _cmd_session_lock:
        if _cmd_session is not None:
            try:
                _cmd_session.close()
            except Exception:
                pass
            _cmd_session = None
