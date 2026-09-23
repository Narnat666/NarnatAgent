"""远程工具族的旧实现行为基准提取器（Terminal SSH / Serial 串口）。

对新包 `tools/remote` 的"行为等价"提供可计算判据：对**旧实现**（`narnat_agent/`，
原位未动）运行 `remote_cases.CASES` 的纯函数/静态方法/裸实例方法用例，把输出固化成
基准；新实现跑同一份用例、逐个比对（见 `tests/unit/test_tools_remote.py`）。

用法（任意 cwd）：
    python v2/tests/baseline/extract_old_remote.py

输出：`v2/tests/baseline/data_remote/remote.json`（UTF-8、indent=2、ensure_ascii=False、LF）。

- 随机标签模式化为 `[<TAG>]`（输出侧统一处理；`parts` 协议保证输入侧与标签取值无关）；
- 不连网、不打开串口、不写用户数据、不运行 narnat；
- 幂等：连续两次运行逐字节一致。
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
V2_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = TESTS_DIR / "baseline" / "data_remote"

sys.path.insert(0, str(V2_DIR))
sys.path.insert(0, str(REPO_ROOT))

from tests.baseline import remote_cases  # noqa: E402  (路径由上一行注入)


def make_api() -> types.SimpleNamespace:
    """构造旧实现的 api 命名空间（旧包的函数/静态方法引用）。"""
    from narnat_agent.tools import exec_signal
    from narnat_agent.tools import serial as serial_pkg
    from narnat_agent.tools import terminal as terminal_pkg
    from narnat_agent.tools.diff_utils import describe_bytes_only_change
    from narnat_agent.tools.read import _detect_text_encoding
    from narnat_agent.tools.serial import serial_session as ssh_serial
    from narnat_agent.tools.terminal import remote as terminal_remote
    from narnat_agent.tools.terminal import ssh_session

    def ssh_parse_output(raw: str, marker: str, pwd_marker: str, last_command: str):
        probe = object.__new__(ssh_session.SSHSession)
        probe._last_command = last_command
        return probe._parse_output(raw, marker, pwd_marker)

    def ssh_parse_partial(raw: str, marker: str):
        probe = object.__new__(ssh_session.SSHSession)
        return probe._parse_partial_output(raw, marker)

    def ssh_extract_cwd(raw: str, marker: str, pwd_marker: str):
        probe = object.__new__(ssh_session.SSHSession)
        return probe._extract_cwd(raw, marker, pwd_marker)

    def serial_probe(pattern: str):
        probe = object.__new__(ssh_serial.SerialSession)
        probe._prompt_re = ssh_serial.SerialSession.PROMPT_RE
        if pattern:
            probe._prompt_re = re.compile(pattern)
        return probe

    return types.SimpleNamespace(
        rc_line=exec_signal.rc_line,
        error_line=exec_signal.error_line,
        tag_error=exec_signal.tag_error,
        terminal_definition=terminal_pkg.DEFINITION,
        serial_definition=serial_pkg.DEFINITION,
        ssh_truncate=ssh_session._truncate_output,
        ssh_clean=ssh_session.SSHSession._clean_output,
        ssh_ps1=ssh_session.SSHSession._ps1_candidate,
        ssh_sentinel=ssh_session.SSHSession._sentinel_line_present,
        ssh_strip_echo=ssh_session.SSHSession._strip_echo,
        ssh_strip_trailing_prompt=ssh_session.SSHSession._strip_trailing_prompt,
        ssh_strip_caret_echo=ssh_session.SSHSession._strip_caret_echo,
        ssh_parse_output=ssh_parse_output,
        ssh_parse_partial=ssh_parse_partial,
        ssh_extract_cwd=ssh_extract_cwd,
        serial_truncate=ssh_serial._truncate_output,
        serial_clean=ssh_serial.SerialSession._clean_output,
        serial_merge_cr=ssh_serial._merge_cr_line,
        serial_strip_command_echo=ssh_serial.SerialSession._strip_command_echo,
        serial_dedup_prompt_lines=lambda output: serial_probe("")._dedup_prompt_lines(output),
        serial_polish_output=lambda raw, sent: serial_probe("")._polish_output(raw, sent),
        serial_is_at_prompt=lambda text, pattern: serial_probe(pattern)._is_at_prompt(text),
        format_size=terminal_pkg._format_size,
        check_transfer_size=terminal_pkg._check_transfer_size,
        make_diff=terminal_remote._make_diff,
        describe_bytes_change=describe_bytes_only_change,
        detect_encoding=_detect_text_encoding,
    )


def main() -> int:
    api = make_api()
    groups: dict[str, list[dict]] = {}
    total = 0
    for group in remote_cases.CASES:
        groups[group] = remote_cases.run_group(api, group)
        total += len(groups[group])

    payload = {
        "generated_by": "v2/tests/baseline/extract_old_remote.py",
        "source": ("narnat_agent/tools/terminal/{ssh_session,__init__,remote}.py, "
                   "narnat_agent/tools/serial/{serial_session,__init__}.py, "
                   "narnat_agent/tools/{exec_signal,diff_utils,read}.py"),
        "note": ("纯函数/静态方法/裸实例方法的行为基准；随机标签模式化为 [<TAG>]；"
                 "输入为 remote_cases.CASES（两实现共用）"),
        "module": "narnat_agent.tools.terminal / narnat_agent.tools.serial",
        "groups": groups,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "remote.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8", newline="\n")
    print(f"已写出 {out_path.relative_to(REPO_ROOT)}：{len(groups)} 组、{total} 用例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
