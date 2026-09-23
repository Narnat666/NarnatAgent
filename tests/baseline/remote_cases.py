"""远程工具族的行为用例集 —— 新旧实现共用（与 `cases.py` 同一约定）。

- 本模块只含**纯数据与调用编排**，不 import 任何实现；实现经 `api` 命名空间注入
  （旧包 / 新包各构造一份，见 `extract_old_remote.py` 与
  `tests/unit/test_tools_remote.py` 的 `_api_*`）。
- 标签类文本用 `parts` 协议拼装（`lit`/`rc`/`err_line`/`tag_error`），由调用方用
  自己的实现生成，保证索引/长度类结果与标签取值无关（进程级随机标签，故提取与
  比对都对输出做 `mask_tags` 模式化）。
- 用例输入可 JSON 序列化（字节类输入用 base64）。
"""
from __future__ import annotations

import base64
import re
from typing import Any

MASK_TAG_RE = re.compile(r"\[[0-9a-f]{8}\]")
TAG_PLACEHOLDER = "[<TAG>]"


def mask_tags(text: str) -> str:
    """把进程级随机标签模式化为 `[<TAG>]`（与 extract_old.py 同一约定）。"""
    return MASK_TAG_RE.sub(TAG_PLACEHOLDER, text)


def build_text(parts: list[list[str]], api: Any) -> str:
    """按 parts 协议拼装文本（含框架随机标签）。"""
    chunks: list[str] = []
    for kind, value in parts:
        if kind == "lit":
            chunks.append(value)
        elif kind == "rc":
            chunks.append(api.rc_line(value))
        elif kind == "err_line":
            chunks.append(api.error_line(value))
        elif kind == "tag_error":
            chunks.append(api.tag_error(value))
        else:
            raise ValueError(f"未知 parts 类型: {kind}")
    return "".join(chunks)


def b64(raw: str | bytes) -> str:
    """文本 → base64（字节类用例的输入编码，保证 JSON 可序列化）。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value)


# ═══════════════════════════════════════════════════════════════
# 用例输入（纯数据）
# ═══════════════════════════════════════════════════════════════

CASES: dict[str, list[dict]] = {
    # ── 工具定义（LLM 侧契约：逐字对照）──
    "terminal.definition": [
        {"id": "terminal", "input": {}},
    ],
    "serial.definition": [
        {"id": "serial", "input": {}},
    ],

    # ── Terminal 输出截断 ──
    "terminal.truncate_output": [
        {"id": "short", "input": {"parts": [["lit", "abc"]], "max_chars": 8000}},
        {"id": "exact-limit", "input": {"parts": [["lit", "x" * 40]], "max_chars": 40}},
        {"id": "zero-limit", "input": {"parts": [["lit", "abc"]], "max_chars": 0}},
        {"id": "negative-limit", "input": {"parts": [["lit", "abc"]], "max_chars": -5}},
        {"id": "ascii-oversize", "input": {"parts": [["lit", "x" * 100]], "max_chars": 30}},
        {"id": "cjk-oversize", "input": {"parts": [["lit", "中" * 60]], "max_chars": 30}},
        {"id": "multiline-oversize", "input": {
            "parts": [["lit", "\n".join(f"line-{i}" for i in range(20))]],
            "max_chars": 40}},
        {"id": "rc-tag-at-cut", "input": {
            "parts": [["lit", "A" * 18], ["rc", 3], ["lit", "B" * 20]],
            "max_chars": 30}},
        {"id": "err-tag-in-middle", "input": {
            "parts": [["lit", "A" * 18], ["err_line", "x"], ["lit", "B" * 20]],
            "max_chars": 30}},
        {"id": "rc-line-prefixed", "input": {
            "parts": [["rc", 1], ["lit", "\nhello world\n"], ["lit", "tail-marker$ "]],
            "max_chars": 25}},
    ],

    # ── Terminal 输出清洗 ──
    "terminal.clean_output": [
        {"id": "ansi-strip", "input": {"text": "\x1b[31mred\x1b[0m plain"}},
        {"id": "osc-title", "input": {"text": "\x1b]0;title\x07user@host:~$ "}},
        {"id": "cr-overwrite", "input": {"text": "progress 10%\rprogress 90%"}},
        {"id": "cr-crlf", "input": {"text": "a\r\nb\rc"}},
        {"id": "markers-removed", "input": {
            "text": "out\n__NARNAT_MARKER_123__0\n__NARNAT_PWD_456__\n__NARNAT_CWD_789__\npwd"}},
        {"id": "blank-lines", "input": {"text": "a\n\n\n\n\nb"}},
        {"id": "leading-space-kept", "input": {"text": "  indented\nplain"}},
        {"id": "trailing-space-trimmed", "input": {"text": "line   \n  \n"}},
        {"id": "cr-only", "input": {"text": "aaa\rbbb\nccc"}},
    ],

    # ── PS1 提示符形态判定 ──
    "terminal.ps1_candidate": [
        {"id": "strict-bash", "input": {"text": "root@dev:/tmp# "}},
        {"id": "strict-user-home", "input": {"text": "user@srv:~/work$ "}},
        {"id": "loose-busybox", "input": {"text": "# "}},
        {"id": "loose-short", "input": {"text": "~ $ "}},
        {"id": "loose-with-space", "input": {"text": "root@dev:~ # "}},
        {"id": "osc-glued", "input": {"text": "output-no-newline\x1b]0;title\x07user@host:~$ "}},
        {"id": "ps2-continuation", "input": {"text": "> "}},
        {"id": "empty", "input": {"text": ""}},
        {"id": "multiline-last-line", "input": {"text": "line1\nline2\nroot@a:b# "}},
        {"id": "not-prompt-dollar-text", "input": {"text": "price is 5 dollars"}},
        {"id": "too-long-loose", "input": {"text": "a" * 40 + " $ "}},
        {"id": "ansi-colored-prompt", "input": {"text": "\x1b[32mroot@dev:~#\x1b[0m "}},
        {"id": "cr-terminated", "input": {"text": "line\ruser@dev:~$ "}},
    ],

    # ── 哨兵行判定 ──
    "terminal.sentinel_line_present": [
        {"id": "plain-line", "input": {"text": "out\n__PWD_MARK__\nprompt", "marker": "__PWD_MARK__"}},
        {"id": "echo-line-only", "input": {"text": "rc=$?; printf '\\n'; echo __PWD_MARK__\n", "marker": "__PWD_MARK__"}},
        {"id": "mid-line", "input": {"text": "prefix __PWD_MARK__ suffix", "marker": "__PWD_MARK__"}},
        {"id": "ansi-prefixed", "input": {"text": "\x1b[?2004l\r__PWD_MARK__\n", "marker": "__PWD_MARK__"}},
        {"id": "absent", "input": {"text": "nothing here", "marker": "__PWD_MARK__"}},
    ],

    # ── 回显剥离 / 尾提示符剥离 / ^C 回显剥离 ──
    "terminal.strip_echo": [
        {"id": "drop-first", "input": {"text": "cmd\nreal-output", "drop": True}},
        {"id": "keep-first", "input": {"text": "real-output\nmore", "drop": False}},
        {"id": "single-line", "input": {"text": "echo hi", "drop": True}},
    ],
    "terminal.strip_trailing_prompt": [
        {"id": "prompt-at-end", "input": {"text": "done\nroot@dev:~$ "}},
        {"id": "ansi-prompt", "input": {"text": "done\n\x1b[32mroot@dev:~$\x1b[0m"}},
        {"id": "no-prompt", "input": {"text": "done\nmore output"}},
        {"id": "only-prompt", "input": {"text": "root@dev:~$ "}},
        {"id": "pwd-like-line", "input": {"text": "cd /tmp\n/tmp\nroot@dev:/tmp$ "}},
    ],
    "terminal.strip_caret_echo": [
        {"id": "caret-then-out", "input": {"text": "^C\nreal"}},
        {"id": "caret-blanks", "input": {"text": "^C\n\n\nout"}},
        {"id": "no-caret", "input": {"text": "out"}},
        {"id": "only-caret", "input": {"text": "^C\n"}},
    ],

    # ── 正常完成解析（裸实例：仅 _last_command 状态）──
    "terminal.parse_output": [
        {"id": "normal", "input": {
            "raw": "ls -l\n\x1b[?2004l\rfile1\nfile2\n\x1b[?2004h\x1b]0;t\x07root@dev:~$ "
                   "rc=$?; printf '\\n'; echo __NARNAT_MARKER_1__0; pwd -P; echo __NARNAT_PWD_1__\n"
                   "__NARNAT_MARKER_1__0\n/home/user\n__NARNAT_PWD_1__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_1__", "pwd_marker": "__NARNAT_PWD_1__",
            "last_command": "ls -l"}},
        {"id": "no-output-cmd", "input": {
            "raw": "true\n\x1b[?2004l\r__NARNAT_MARKER_2__0\n/tmp\n__NARNAT_PWD_2__\nroot@dev:/tmp$ ",
            "marker": "__NARNAT_MARKER_2__", "pwd_marker": "__NARNAT_PWD_2__",
            "last_command": "true"}},
        {"id": "exit-code-parsed", "input": {
            "raw": "false\n__NARNAT_MARKER_3__1\n/home/u\n__NARNAT_PWD_3__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_3__", "pwd_marker": "__NARNAT_PWD_3__",
            "last_command": "false"}},
        {"id": "exit-code-broken", "input": {
            "raw": "cmd\n__NARNAT_MARKER_4__x\n/u\n__NARNAT_PWD_4__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_4__", "pwd_marker": "__NARNAT_PWD_4__",
            "last_command": "cmd"}},
        {"id": "first-line-echo-exact", "input": {
            "raw": "cmd\nreal line\n__NARNAT_MARKER_5__0\n/u\n__NARNAT_PWD_5__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_5__", "pwd_marker": "__NARNAT_PWD_5__",
            "last_command": "cmd"}},
        {"id": "first-line-not-echo", "input": {
            "raw": "GOT:G\n__NARNAT_MARKER_6__0\n/u\n__NARNAT_PWD_6__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_6__", "pwd_marker": "__NARNAT_PWD_6__",
            "last_command": "G"}},
        {"id": "continuation-echo", "input": {
            "raw": "echo a\n> \x1b[?2004h> b\na\nb\n__NARNAT_MARKER_7__0\n/u\n__NARNAT_PWD_7__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_7__", "pwd_marker": "__NARNAT_PWD_7__",
            "last_command": "echo a\\\nb"}},
        {"id": "glued-output", "input": {
            "raw": "printf hi\n\x1b[?2004l\rhi\x1b[?2004h\x1b]0;t\x07root@dev:~$ "
                   "rc=$?; printf '\\n'; echo __NARNAT_MARKER_8__0; pwd -P; echo __NARNAT_PWD_8__\n"
                   "__NARNAT_MARKER_8__0\n/home/u\n__NARNAT_PWD_8__\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_8__", "pwd_marker": "__NARNAT_PWD_8__",
            "last_command": "printf hi"}},
        {"id": "no-marker", "input": {
            "raw": "cmd\npartial output\nroot@dev:~$ ",
            "marker": "__NARNAT_MARKER_9__", "pwd_marker": "__NARNAT_PWD_9__",
            "last_command": "cmd"}},
        {"id": "ansi-in-marker-line", "input": {
            "raw": "cmd\r\x1b[?2004l\r\x1b[?2004h__NARNAT_MARKER_10__7\n/srv\n__NARNAT_PWD_10__\nroot@dev:/srv$ ",
            "marker": "__NARNAT_MARKER_10__", "pwd_marker": "__NARNAT_PWD_10__",
            "last_command": "cmd"}},
    ],
    "terminal.parse_partial_output": [
        {"id": "no-marker", "input": {
            "raw": "cmd\n输出中\nroot@dev:~$ ", "marker": "__NARNAT_MARKER_11__"}},
        {"id": "with-marker", "input": {
            "raw": "cmd\nout\n__NARNAT_MARKER_12__0\n", "marker": "__NARNAT_MARKER_12__"}},
        {"id": "echo-only", "input": {
            "raw": "echo hi\n", "marker": "__NARNAT_MARKER_13__"}},
    ],
    "terminal.extract_cwd": [
        {"id": "plain", "input": {
            "raw": "__NARNAT_MARKER_14__0\n/home/user\n__NARNAT_PWD_14__\n",
            "marker": "__NARNAT_MARKER_14__", "pwd_marker": "__NARNAT_PWD_14__"}},
        {"id": "root-dir", "input": {
            "raw": "__NARNAT_MARKER_15__0\n/\n__NARNAT_PWD_15__\n",
            "marker": "__NARNAT_MARKER_15__", "pwd_marker": "__NARNAT_PWD_15__"}},
        {"id": "echo-line-first", "input": {
            "raw": "echo __NARNAT_PWD_16__\n__NARNAT_MARKER_16__0\n/opt/app\n__NARNAT_PWD_16__\n",
            "marker": "__NARNAT_MARKER_16__", "pwd_marker": "__NARNAT_PWD_16__"}},
        {"id": "missing-marker", "input": {
            "raw": "/home/u\n__NARNAT_PWD_17__\n",
            "marker": "__NARNAT_MARKER_17__", "pwd_marker": "__NARNAT_PWD_17__"}},
        {"id": "non-path-between", "input": {
            "raw": "__NARNAT_MARKER_18__0\nnot a path\n__NARNAT_PWD_18__\n",
            "marker": "__NARNAT_MARKER_18__", "pwd_marker": "__NARNAT_PWD_18__"}},
    ],

    # ── Serial 文本层 ──
    "serial.truncate_output": [
        {"id": "short", "input": {"text": "abc", "max_chars": 8000}},
        {"id": "zero-limit", "input": {"text": "abc", "max_chars": 0}},
        {"id": "negative-limit", "input": {"text": "abc", "max_chars": -1}},
        {"id": "oversize", "input": {"text": "x" * 100, "max_chars": 30}},
        {"id": "multiline", "input": {"text": "\n".join(f"row{i}" for i in range(30)), "max_chars": 45}},
        {"id": "exact", "input": {"text": "y" * 30, "max_chars": 30}},
    ],
    "serial.clean_output": [
        {"id": "ansi-strip", "input": {"text": "\x1b[33mwarn\x1b[0m ok"}},
        {"id": "crlf-normalized", "input": {"text": "a\r\nb\r\nc"}},
        {"id": "cr-overwrite", "input": {"text": "booting 10%\rbooting 99%"}},
        {"id": "mixed-cr", "input": {"text": "a\r\nb\rc\nd"}},
        {"id": "blank-lines", "input": {"text": "a\n\n\n\nb"}},
        {"id": "strip-outer", "input": {"text": "\n\n  login:  \n\n"}},
    ],
    "serial.merge_cr_line": [
        {"id": "no-cr", "input": {"line": "plain"}},
        {"id": "overwrite-longer", "input": {"line": "12345\r99"}},
        {"id": "overwrite-shorter", "input": {"line": "12\r99999"}},
        {"id": "trailing-cr", "input": {"line": "abc\r"}},
        {"id": "leading-cr", "input": {"line": "\rabc"}},
        {"id": "empty-segments", "input": {"line": "a\r\rb"}},
    ],
    "serial.strip_command_echo": [
        {"id": "exact-echo", "input": {"output": "help\nusage: ...", "command": "help"}},
        {"id": "similar-echo", "input": {"output": "heIp\nusage: ...", "command": "help"}},
        {"id": "dissimilar-keep", "input": {"output": "usage: ...", "command": "help"}},
        {"id": "long-command-prefix", "input": {
            "output": "show version brief\nVersion 1.2", "command": "show version brief"}},
        {"id": "long-command-truncated", "input": {
            "output": "show version brie\nVersion 1.2",
            "command": "show version brief-extra-long-suffix"}},
        {"id": "empty-command", "input": {"output": "out", "command": ""}},
        {"id": "echo-only", "input": {"output": "help\n", "command": "help"}},
    ],
    "serial.dedup_prompt_lines": [
        {"id": "dup-prompt", "input": {"output": "# \n# \ncmd"}},
        {"id": "dup-prompt-space-diff", "input": {"output": "#\n# \ncmd"}},
        {"id": "dup-non-prompt-kept", "input": {"output": "same\nsame\ncmd"}},
        {"id": "single-line", "input": {"output": "# "}},
        {"id": "echo-and-real-prompt", "input": {"output": "COM3 > echo hi\nCOM3 > hi\nCOM3 > "}},
    ],
    "serial.polish_output": [
        {"id": "echo-and-prompt", "input": {"raw": "help\r\nusage:\r\n# ", "sent": "help"}},
        {"id": "no-echo", "input": {"raw": "Version 1.2\r\n# ", "sent": "show version"}},
        {"id": "prompt-dup", "input": {"raw": "help\r\n# \r\n# ", "sent": "help"}},
        {"id": "short-input", "input": {"raw": "G\r\nGOT:G\r\n# ", "sent": "G"}},
    ],
    "serial.is_at_prompt": [
        {"id": "hash-prompt", "input": {"text": "# ", "pattern": ""}},
        {"id": "gt-prompt", "input": {"text": "> ", "pattern": ""}},
        {"id": "mid-line-dollar", "input": {"text": "price$ ", "pattern": ""}},
        {"id": "cr-merged", "input": {"text": "doing\rlogin: ", "pattern": ""}},
        {"id": "custom-pattern-hit", "input": {"text": "== READY == ", "pattern": r"READY\s*==\s*$"}},
        {"id": "custom-pattern-miss", "input": {"text": "# ", "pattern": r"READY\s*$"}},
        {"id": "empty", "input": {"text": "", "pattern": ""}},
    ],

    # ── 传输大小 ──
    "transfer.format_size": [
        {"id": "bytes", "input": {"size": 0}},
        {"id": "bytes-1023", "input": {"size": 1023}},
        {"id": "kb", "input": {"size": 1024}},
        {"id": "kb-frac", "input": {"size": 1536}},
        {"id": "mb", "input": {"size": 5 * 1024 * 1024 + 512 * 1024}},
        {"id": "gb", "input": {"size": 2 * 1024 * 1024 * 1024 + 100}},
    ],
    "transfer.check_transfer_size": [
        {"id": "under-limit", "input": {"size": 1024, "max_mb": 100}},
        {"id": "exact-limit", "input": {"size": 100 * 1024 * 1024, "max_mb": 100}},
        {"id": "over-limit", "input": {"size": 100 * 1024 * 1024 + 1, "max_mb": 100}},
        {"id": "unlimited-zero", "input": {"size": 10 * 1024 * 1024 * 1024, "max_mb": 0}},
        {"id": "unlimited-negative", "input": {"size": 10, "max_mb": -3}},
    ],

    # ── 远程文件纯函数 ──
    "remote.make_diff": [
        {"id": "changed", "input": {"old": "a\nb\nc\n", "new": "a\nB\nc\n", "path": "/tmp/x.txt"}},
        {"id": "same", "input": {"old": "a\n", "new": "a\n", "path": "/tmp/x.txt"}},
        {"id": "no-slash", "input": {"old": "1\n", "new": "2\n", "path": "x.txt"}},
        {"id": "crlf-vs-lf", "input": {"old": "a\r\nb\r\n", "new": "a\nb\n", "path": "/d/f.txt"}},
        {"id": "added-lines", "input": {"old": "a\n", "new": "a\nb\nc\n", "path": "/d/f.txt"}},
    ],
    "remote.describe_bytes_only_change": [
        {"id": "crlf-to-lf", "input": {"old_b64": b64("a\r\nb\r\n"), "new_b64": b64("a\nb\n")}},
        {"id": "trailing-newline-added", "input": {"old_b64": b64("a\nb"), "new_b64": b64("a\nb\n")}},
        {"id": "trailing-newline-removed", "input": {"old_b64": b64("a\n"), "new_b64": b64("a")}},
        {"id": "cr-only", "input": {"old_b64": b64("a\rb"), "new_b64": b64("a\nb")}},
        {"id": "encoding-change", "input": {
            "old_b64": b64("中文".encode("gbk")), "new_b64": b64("中文")}},
        {"id": "bom-added", "input": {
            "old_b64": b64("abc"), "new_b64": b64("\ufeffabc")}},
        {"id": "no-newline-both", "input": {"old_b64": b64("ab"), "new_b64": b64("aB")}},
    ],
    "remote.detect_text_encoding": [
        {"id": "pure-ascii", "input": {"head_b64": b64("hello world")}},
        {"id": "utf8-cjk", "input": {"head_b64": b64("中文内容")}},
        {"id": "utf8-bom", "input": {"head_b64": b64("\ufeffabc")}},
        {"id": "gbk-cjk", "input": {"head_b64": b64("中文内容".encode("gbk"))}},
        {"id": "truncated-utf8-tail", "input": {"head_b64": b64("中".encode("utf-8")[:2])}},
        {"id": "empty", "input": {"head_b64": b64("")}},
    ],
}


# ═══════════════════════════════════════════════════════════════
# 运行器（按组把 api 命名空间与输入映射到结果）
# ═══════════════════════════════════════════════════════════════

RUNNERS = {
    "terminal.definition": lambda api, i: api.terminal_definition,
    "serial.definition": lambda api, i: api.serial_definition,
    "terminal.truncate_output": lambda api, i: api.ssh_truncate(
        build_text(i["parts"], api), i["max_chars"]),
    "terminal.clean_output": lambda api, i: api.ssh_clean(i["text"]),
    "terminal.ps1_candidate": lambda api, i: api.ssh_ps1(i["text"]),
    "terminal.sentinel_line_present": lambda api, i: api.ssh_sentinel(i["text"], i["marker"]),
    "terminal.strip_echo": lambda api, i: api.ssh_strip_echo(i["text"], i["drop"]),
    "terminal.strip_trailing_prompt": lambda api, i: api.ssh_strip_trailing_prompt(i["text"]),
    "terminal.strip_caret_echo": lambda api, i: api.ssh_strip_caret_echo(i["text"]),
    "terminal.parse_output": lambda api, i: api.ssh_parse_output(
        i["raw"], i["marker"], i["pwd_marker"], i["last_command"]),
    "terminal.parse_partial_output": lambda api, i: api.ssh_parse_partial(i["raw"], i["marker"]),
    "terminal.extract_cwd": lambda api, i: api.ssh_extract_cwd(
        i["raw"], i["marker"], i["pwd_marker"]),
    "serial.truncate_output": lambda api, i: api.serial_truncate(i["text"], i["max_chars"]),
    "serial.clean_output": lambda api, i: api.serial_clean(i["text"]),
    "serial.merge_cr_line": lambda api, i: api.serial_merge_cr(i["line"]),
    "serial.strip_command_echo": lambda api, i: api.serial_strip_command_echo(
        i["output"], i["command"]),
    "serial.dedup_prompt_lines": lambda api, i: api.serial_dedup_prompt_lines(i["output"]),
    "serial.polish_output": lambda api, i: api.serial_polish_output(i["raw"], i["sent"]),
    "serial.is_at_prompt": lambda api, i: api.serial_is_at_prompt(i["text"], i["pattern"]),
    "transfer.format_size": lambda api, i: api.format_size(i["size"]),
    "transfer.check_transfer_size": lambda api, i: api.check_transfer_size(
        i["size"], i["max_mb"]),
    "remote.make_diff": lambda api, i: api.make_diff(i["old"], i["new"], i["path"]),
    "remote.describe_bytes_only_change": lambda api, i: api.describe_bytes_change(
        unb64(i["old_b64"]), unb64(i["new_b64"])),
    "remote.detect_text_encoding": lambda api, i: api.detect_encoding(unb64(i["head_b64"])),
}


def run_group(api: Any, group: str) -> list[dict]:
    """对某组全部用例跑一遍实现，返回 `[{id, result}, ...]`（结果已标签模式化）。"""
    runner = RUNNERS[group]
    out = []
    for case in CASES[group]:
        result = runner(api, case["input"])
        out.append({"id": case["id"], "result": _normalize(result)})
    return out


def _normalize(value: Any) -> Any:
    """结果归一化：tuple → list，文本做标签模式化。"""
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return mask_tags(value)
    if isinstance(value, bytes):
        return b64(value)
    return value
