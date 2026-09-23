r"""tools/shell 工具族自测 —— T3.5 交付物（Shell 前台执行 + 后台任务）。

对齐 `openspec/changes/recast-v2/specs/tools-shell/spec.md`（16 Requirement / 88 Scenario）
的 Scenario 映射表（「归他处」表示该 Scenario 的用户可见部分由其他积木实现，
本文件只覆盖 Shell 侧的可观察面）：

| spec Scenario | 覆盖测试 |
|---|---|
| 平台标签渲染 | `test_definition_platform_label` |
| 无必填参数 | `test_bg_status_without_command` |
| 字符串数字容错 | `test_numeric_string_tolerance` |
| 别名覆盖 | `test_max_output_tokens_alias_overrides` |
| 非法数值报错文案 | `test_invalid_numeric_error_text` |
| 空命令文案 | `test_empty_command_text` |
| 非正超时文案 | `test_non_positive_timeout_text` |
| 超时上限钳制 | `test_timeout_clamped_by_settings` |
| rm 免确认开关 | `test_rm_skip_confirm_executes_delete` |
| git 命中确认 | `test_git_command_hits_confirm` |
| Windows 用户拒绝 | `test_windows_reject_cancels_without_execution` |
| 无确认回调时不拦截 | `test_missing_confirm_callback_not_intercepted` |
| 后台提交同样先过确认 | `test_background_submit_goes_through_confirm` |
| 首次调用挂起 | `test_non_windows_first_call_pends` |
| 确认后重执行 | `test_non_windows_confirmed_call_executes` |
| 拒绝回传文案 | `test_non_windows_pending_arguments_shape`（拒绝文案由主循环回传，见缺口说明） |
| 平台自适应 | `test_platform_adaptive_cmd_env_expansion` / `test_unix_missing_shell_text` |
| 超时终止 | `test_timeout_terminates_and_reports` |
| ESC 中断 | `test_escape_interrupt_result` |
| 输出解码回退 | `test_decode_output_gbk_fallback` / `test_decode_output_replace_fallback` |
| 子进程输出编码 | `test_child_utf8_output_for_emoji` |
| 启动失败文案 | `test_startup_failure_text` / `test_cmd_missing_text` |
| 正常结果 | `test_normal_result_shape` |
| 空输出段省略 | `test_empty_stdout_segment_omitted` |
| 中断结果 | `test_escape_interrupt_result` |
| 超时结果 | `test_timeout_result_shape` |
| 单条 cd 持久化 | `test_cd_persists_for_later_tools` |
| 无参数 cd 平台差异 | `test_noarg_cd_platform_difference` |
| 切换失败文案 | `test_cd_failure_text` |
| 简写与展开 | `test_cd_shortcut_and_expansion` |
| 复合运算符不按纯 cd 处理 | `test_compound_cd_goes_to_segments` |
| && 短路跳过 | `test_segment_and_short_circuit` |
| \|\| 短路跳过 | `test_segment_or_short_circuit` |
| 段输出与总退出码 | `test_segment_outputs_and_total_rc` |
| 预算递减与耗尽 | `test_segment_budget_exhaustion` |
| 段超时格式 | `test_segment_timeout_format`（python 段 + shell 段两路） |
| 多段中断 | `test_segments_interrupt` |
| 多段中的 cd 段 | `test_segment_cd_applies` |
| Windows 单段直执行 | `test_py_direct_windows_payload` |
| 多段中的 python 段 | `test_py_segment_in_multi_command` |
| 不支持形态回退 | `test_py_unsupported_forms_fall_back` |
| >nul 丢弃 stdout | `test_py_suffix_nul_discards_stdout` |
| 重定向写文件 | `test_py_suffix_redirect_writes_file` |
| 管道后缀 | `test_py_suffix_pipe` |
| 中断不写文件 | `test_py_suffix_interrupt_keeps_file_absent` |
| 首尾保留与提示文案 | `test_truncate_head_tail_and_notice` |
| 未超限原样返回 | `test_truncate_within_limit_untouched` |
| 标签不被切开 | `test_truncate_never_splits_tags` |
| 提交返回值与句柄 | `test_submit_returns_handle_and_header` |
| 空命令提交报错 | `test_submit_without_command` |
| 并发上限 | `test_submit_concurrency_limit` |
| 终态释放与编号复用 | `test_slot_reuse_after_terminal` |
| 复用归档 | `test_slot_reuse_archives_old_result` |
| 日志头与增量落盘 | `test_log_header_and_incremental_flush` |
| 编码统一 | `test_log_encoding_unified_utf8` |
| 启动失败释放槽位 | `test_background_startup_failure_releases_slot` / `test_unwritable_log_file` |
| 快照格式 | `test_snapshot_format` |
| 无任务快照 | `test_snapshot_without_tasks` |
| 完成事件唤醒等待 | `test_wait_wakes_on_completion` |
| 无运行任务立即返回 | `test_wait_without_running_tasks` |
| 等待超时 | `test_wait_timeout_text` |
| 打断等待不杀任务 | `test_wait_interrupt_keeps_tasks_running` |
| 等待参数校验 | `test_wait_parameter_validation` |
| 正常取消 | `test_cancel_running_task` |
| id 缺失与非整数 | `test_cancel_id_missing_or_non_integer` |
| 编号越界与空槽 | `test_cancel_out_of_range_and_empty_slot` |
| 终态不可取消 | `test_cancel_terminal_slot` |
| 未知操作 | `test_cancel_unknown_op` |
| 目录隔离 | `test_result_dirs_isolated_per_instance`（真多进程隔离未验证） |
| 预清不杀进程 | `test_prepare_wipes_without_killing` |
| 结束硬清理幂等 | `test_cleanup_all_idempotent` |
| 过期清扫 | `test_sweep_stale_guardrails` |
| 超大输出截断 | `test_main_file_cap_and_tail_file` |
| 运行摘要格式 | `test_running_summary_format` |
| 标签随机且进程内稳定 | `test_labels_random_but_stable_in_process` |
| LLM 可见文本等价 | `test_llm_visible_text_has_no_tags` |
| 伪造不生效 | `test_fake_output_does_not_trigger_error` |
| 判定顺序 | `test_registry_judges_error_before_stripping` |
| 整体退出码取最后 | `test_overall_rc_takes_last_line` |
| 输出上限 0 不早退（兼容怪癖） | `test_zero_max_output_chars_runs_command` |
| 预算耗尽即超时（兼容怪癖） | `test_segment_budget_exhaustion` |
| 中断丢弃段输出（兼容怪癖） | `test_segments_interrupt_drops_segment_output` |
| 取消唤醒报超时（兼容怪癖） | `test_cancel_wakes_wait_with_timeout_text` |
| 中断标志入口清零（兼容怪癖） | `test_residual_interrupt_cleared_on_entry` |
| Unix 单段不直执行（兼容怪癖） | `test_unix_single_segment_skips_py_direct` |
| 多段 sh 与单段 bash 的不一致（兼容怪癖） | `test_single_segment_shell_argv_vs_segments_shell_true`（结构断言；真双平台执行未验证） |
| 多段无参数 cd 不切换（兼容怪癖） | `test_segment_noarg_cd_does_not_switch` |
| 全局截断不吸附标签（兼容怪癖） | `test_registry_global_truncation_keeps_no_partial_tag` |

未验证项（本机 Windows 无法自动化，需 Linux/macOS 环境或真实终端）：
1. 真实 ESC 按键与「子进程偷吃控制台 stdin」的回归（需真实终端；本文件用 `ShellRuntime.interrupt()`
   直接驱动中断路径）；
2. Unix 平台真实执行的杀树（killpg）、`bash`/`sh` 优先级与 `~` 提示符渲染；
3. 会话结束硬清理在真实多进程/多 agent 并发下的目录隔离（本文件只覆盖同进程双实例）；
4. Windows `taskkill /F /T` 对 `start /b` 分离孙进程的实际收口时序；
5. 50MB 主文件上限的真实大输出边界（本文件用缩小后的常量注入覆盖同一逻辑）。
"""
from __future__ import annotations

import ast
import builtins
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from narnat_agent.tools import signal
from narnat_agent.tools.env import ToolEnvImpl
from narnat_agent.tools.registry import ToolRegistry
from narnat_agent.tools.shell import ShellTool
from narnat_agent.tools.shell import background as background_module
from narnat_agent.tools.shell import executor
from narnat_agent.tools.shell.background import BackgroundManager
from narnat_agent.tools.shell.process import ShellRuntime, decode_output

IS_WINDOWS = sys.platform == "win32"
SHELL_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "tools" / "shell"
SPEC_PATH = (Path(__file__).resolve().parents[3]
             / "openspec" / "changes" / "recast-v2" / "specs" / "tools-shell" / "spec.md")
SLEEPER = 'python -c "import time;time.sleep(8)"'


# ═══════════════════════════════════════════════════════════════
# 基建：测试替身与工具
# ═══════════════════════════════════════════════════════════════


def visible(text: str) -> str:
    """剥离框架随机标签后的 LLM 可见文本。"""
    return signal.strip_tags(text)


def rc_visible(rc: int) -> str:
    """退出码行的 LLM 可见文本（剥离进程级随机标签）。"""
    return signal.strip_tags(signal.rc_line(rc))


def prompt() -> str:
    """当前目录提示符（与执行器同口径）。"""
    return executor._format_prompt()


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立（用于后台线程落盘/终态的可见性等待）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def runtime() -> ShellRuntime:
    return ShellRuntime()


@pytest.fixture
def manager(runtime) -> BackgroundManager:
    mgr = BackgroundManager(runtime)
    yield mgr
    mgr.cleanup_all()


@pytest.fixture
def shell(runtime, manager) -> ShellTool:
    return ShellTool(runtime=runtime, background=manager)


@pytest.fixture
def env() -> ToolEnvImpl:
    return ToolEnvImpl()


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """把进程工作目录固定在临时目录（cd 类测试之外都使用它）。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ═══════════════════════════════════════════════════════════════
# 1. 工具定义与参数契约
# ═══════════════════════════════════════════════════════════════


def test_definition_platform_label(monkeypatch):
    """平台标签渲染：描述首句与参数表（无必填项）。"""
    import narnat_agent.tools.shell as shell_package

    tool = ShellTool()
    definition = tool.definition()
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "Shell"
    expected_label = "Windows(cmd)" if IS_WINDOWS else "Linux/macOS(bash)"
    assert executor.PLATFORM_LABEL == expected_label
    assert definition["function"]["description"] == (
        f"本地Shell — 在{expected_label}执行命令。"
        "前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。"
    )

    # 同一句在另一平台标签下渲染（monkeypatch 平台标签常量，验证文本随平台变化）
    monkeypatch.setattr(shell_package, "PLATFORM_LABEL", "Linux/macOS(bash)")
    other = tool.definition()["function"]["description"]
    assert other.startswith("本地Shell — 在Linux/macOS(bash)执行命令。")

    parameters = definition["function"]["parameters"]
    assert parameters["type"] == "object"
    assert list(parameters["properties"]) == [
        "command", "timeout", "max_output_chars", "background", "bg", "id",
    ]
    assert parameters["required"] == []


def test_bg_status_without_command(shell, env, workdir):
    """无必填参数：仅传 bg="status" 也进入后台分发而非参数校验失败。"""
    result = shell.execute({"bg": "status"}, env)
    assert result.llm_text == "[后台任务] 当前无后台任务"
    assert result.is_error is False


def test_numeric_string_tolerance(shell, env, workdir):
    """字符串数字容错：timeout="30"、max_output_chars="9999"。"""
    result = shell.execute(
        {"command": "echo ok", "timeout": "30", "max_output_chars": "9999"}, env
    )
    assert visible(result.llm_text) == "\n".join([rc_visible(0), "ok", prompt()])


def test_max_output_tokens_alias_overrides(shell, env, workdir):
    """别名覆盖：两者同传以 max_output_tokens 为准（字符数语义）。"""
    payload = 'python -c "print(\'A\'*200)"'
    truncated = shell.execute(
        {"command": payload, "max_output_chars": 4000, "max_output_tokens": 60}, env
    )
    assert "中间截断" in visible(truncated.llm_text)

    roomy = shell.execute(
        {"command": payload, "max_output_chars": 20, "max_output_tokens": 9999}, env
    )
    assert "中间截断" not in visible(roomy.llm_text)
    assert "A" * 200 in visible(roomy.llm_text)


def test_invalid_numeric_error_text(shell, env, workdir):
    """非法数值报错文案：归一化失败即返回错误行且不执行命令。"""
    marker = workdir / "marker.txt"
    result = shell.execute(
        {"command": f"echo x > {marker}", "timeout": "abc"}, env
    )
    assert visible(result.llm_text) == "[错误: timeout/max_output_chars需为整数]"
    assert not marker.exists()

    alias_bad = shell.execute(
        {"command": f"echo x > {marker}", "max_output_tokens": "abc"}, env
    )
    assert visible(alias_bad.llm_text) == "[错误: timeout/max_output_chars需为整数]"
    assert not marker.exists()

    max_bad = shell.execute(
        {"command": f"echo x > {marker}", "max_output_chars": "abc"}, env
    )
    assert visible(max_bad.llm_text) == "[错误: timeout/max_output_chars需为整数]"
    assert not marker.exists()


def test_empty_command_text(shell, env, workdir):
    """空命令文案（未传 command 且未传 background/bg）。"""
    result = shell.execute({}, env)
    assert visible(result.llm_text) == (
        "[错误: command为空：前台执行需提供 command；"
        "后台任务用 background=true 提交，管理用 bg=status/wait/cancel]"
    )


def test_non_positive_timeout_text(shell, env, workdir):
    """非正超时文案（0 与负值）。"""
    for value in (0, -5):
        result = shell.execute({"command": "echo x", "timeout": value}, env)
        assert visible(result.llm_text) == "[错误: timeout需为正整数（秒）]"


def test_timeout_clamped_by_settings(shell, env, workdir):
    """超时上限钳制：宿主上限 1 秒对 timeout=3600 取较小值。"""
    env.settings.max_timeout_seconds = 1
    result = shell.execute({"command": SLEEPER, "timeout": 3600}, env)
    text = visible(result.llm_text)
    assert text.startswith("[超时: 命令执行超过1秒，已终止]")
    assert signal.has_error(result.llm_text) is True


# ═══════════════════════════════════════════════════════════════
# 2. 安全确认门禁
# ═══════════════════════════════════════════════════════════════


def _recording_confirm(verdict: bool, calls: list):
    def _confirm(command: str) -> bool:
        calls.append(command)
        return verdict

    return _confirm


@pytest.mark.skipif(not IS_WINDOWS, reason="删除命令用例使用 cmd 内建 del")
def test_rm_skip_confirm_executes_delete(workdir):
    """rm 免确认开关：开关打开时删除类命令不进入确认流程，直接执行。"""
    marker = workdir / "marker.txt"
    marker.write_text("x", encoding="utf-8")
    calls: list = []
    env = ToolEnvImpl(confirm=_recording_confirm(False, calls))
    env.settings.rm_skip_confirm = True
    shell = ShellTool()

    result = shell.execute({"command": "del marker.txt"}, env)
    assert calls == []  # 未询问
    assert visible(result.llm_text).startswith("[exit code: 0]")
    assert not marker.exists()  # 命令确实执行了


def test_git_command_hits_confirm(workdir):
    """git 命中确认：未开「git免确认」时判定为需确认。"""
    calls: list = []
    env = ToolEnvImpl(confirm=_recording_confirm(False, calls))
    shell = ShellTool()

    result = shell.execute({"command": "git status"}, env)
    if IS_WINDOWS:
        assert calls == ["git status"]
        assert visible(result.llm_text) == "[操作已取消: 此命令需用户确认]"
    else:
        assert result.await_confirm is True
        assert result.llm_text == "__AWAIT_CONFIRM__"


def test_windows_reject_cancels_without_execution(workdir):
    """Windows 用户拒绝：结果为取消文案且命令未执行。"""
    if not IS_WINDOWS:
        pytest.skip("Windows 确认回调路径")
    marker = workdir / "marker.txt"
    env = ToolEnvImpl(confirm=lambda command: False)
    shell = ShellTool()

    result = shell.execute(
        {"command": f"del {marker} && echo ran"}, env
    )
    assert visible(result.llm_text) == "[操作已取消: 此命令需用户确认]"
    assert not marker.exists()


@pytest.mark.skipif(not IS_WINDOWS, reason="删除命令用例使用 cmd 内建 del")
def test_missing_confirm_callback_not_intercepted(workdir):
    """无确认回调时不拦截（headless 形态）：命令直接执行。"""
    marker = workdir / "marker.txt"
    marker.write_text("x", encoding="utf-8")
    env = ToolEnvImpl(confirm=None)
    shell = ShellTool()

    result = shell.execute({"command": "del marker.txt"}, env)
    assert visible(result.llm_text).startswith("[exit code: 0]")
    assert not marker.exists()


def test_background_submit_goes_through_confirm(manager, env, workdir):
    """后台提交同样先过确认：确认被拒时任务不提交。"""
    if not IS_WINDOWS:
        pytest.skip("Windows 确认回调路径")
    calls: list = []
    env = ToolEnvImpl(confirm=_recording_confirm(False, calls))
    shell = ShellTool(background=manager)

    result = shell.execute({"command": "del *", "background": True}, env)
    assert calls == ["del *"]
    assert visible(result.llm_text) == "[操作已取消: 此命令需用户确认]"
    assert manager.snapshot() == "[后台任务] 当前无后台任务"


def test_non_windows_first_call_pends(monkeypatch, workdir):
    """非 Windows 首次调用挂起：返回 __AWAIT_CONFIRM__ 且参数被暂存、命令未执行。"""
    monkeypatch.setattr(sys, "platform", "linux")
    env = ToolEnvImpl()
    shell = ShellTool()

    result = shell.execute({"command": "rm -rf build", "timeout": 30,
                            "max_output_chars": 999, "background": False,
                            "bg": None, "id": None}, env)
    assert result.llm_text == "__AWAIT_CONFIRM__"
    assert result.await_confirm is True
    assert env.delete_gate.take() == ("Shell", {
        "command": "rm -rf build",
        "timeout": 30,
        "max_output_chars": 999,
        "background": False,
        "bg": None,
        "id": None,
    })


def test_non_windows_confirmed_call_executes(monkeypatch, workdir):
    """确认后重执行：已确认标记消费一次、本次不再挂起，直接进入执行路径。"""
    monkeypatch.setattr(sys, "platform", "linux")
    env = ToolEnvImpl()
    shell = ShellTool()
    monkeypatch.setattr(executor, "find_executable", lambda *names: None)

    gate = env.delete_gate
    gate.mark_confirmed()
    result = shell.execute({"command": "rm -rf build"}, env)
    assert result.llm_text != "__AWAIT_CONFIRM__"
    assert result.await_confirm is False
    assert visible(result.llm_text) == "[错误: 未找到shell，请安装bash或sh后重试]"
    assert gate.consume_confirmed() is False  # 标记已被消费


def test_non_windows_pending_arguments_shape(monkeypatch, workdir):
    """挂起时暂存的是本次调用的完整参数（含归一化后的数值）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    env = ToolEnvImpl()
    shell = ShellTool()

    shell.execute({"command": "rm -rf build", "timeout": "30",
                   "max_output_tokens": 700, "background": True, "id": 3}, env)
    pending_name, arguments = env.delete_gate.take()
    assert pending_name == "Shell"
    assert arguments == {
        "command": "rm -rf build",
        "timeout": 30,
        "max_output_chars": 700,
        "background": True,
        "bg": None,
        "id": 3,
    }


def test_git_skip_confirm_executes(workdir):
    """git 免确认开关打开时不询问（执行真实 git 命令）。"""
    calls: list = []
    env = ToolEnvImpl(confirm=_recording_confirm(False, calls))
    env.settings.git_skip_confirm = True
    shell = ShellTool()

    result = shell.execute({"command": "git status"}, env)
    assert calls == []
    assert visible(result.llm_text) != "[操作已取消: 此命令需用户确认]"
    assert "[exit code:" in visible(result.llm_text)


# ═══════════════════════════════════════════════════════════════
# 3. 前台执行、超时与中断
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skipif(not IS_WINDOWS, reason="cmd 环境变量展开")
def test_platform_adaptive_cmd_env_expansion(shell, env, workdir):
    """平台自适应：Windows 下由 cmd 解析并展开 %TEMP%。"""
    result = shell.execute({"command": "echo %TEMP%"}, env)
    text = visible(result.llm_text)
    assert "%TEMP%" not in text
    assert "Temp" in text
    assert text.endswith(prompt())


def test_unix_missing_shell_text(monkeypatch, shell, env, workdir):
    """Unix 分支：bash 与 sh 皆无时返回固定错误行（同时证明未走 cmd 分支）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(executor, "find_executable", lambda *names: None)
    result = shell.execute({"command": "echo x"}, env)
    assert visible(result.llm_text) == "[错误: 未找到shell，请安装bash或sh后重试]"


def test_timeout_terminates_and_reports(shell, env, workdir):
    """超时终止：进程树被终止、结果含超时提示与错误标签。"""
    start = time.time()
    result = shell.execute({"command": SLEEPER, "timeout": 1}, env)
    elapsed = time.time() - start
    text = visible(result.llm_text)
    assert text.startswith("[超时: 命令执行超过1秒，已终止]\n")
    assert text.endswith(prompt())
    assert "[exit code:" not in text
    assert signal.has_error(result.llm_text) is True
    assert elapsed < 6  # 未等满 8 秒的 sleeper（进程树已被终止）


def test_escape_interrupt_result(shell, env, workdir, runtime):
    """ESC 中断：进程树被终止，结果保留已产出输出并以 [用户中断] 收尾、无退出码行。"""
    box: dict = {}

    def _run():
        box["result"] = shell.execute({"command": SLEEPER, "timeout": 30}, env)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.5)
    runtime.interrupt()
    thread.join(timeout=20)
    assert not thread.is_alive()

    result = box["result"]
    text = visible(result.llm_text)
    assert text == f"[用户中断]\n{prompt()}"
    assert "[exit code" not in text
    assert signal.has_error(result.llm_text) is False  # 中断不带错误标签


def test_decode_output_gbk_fallback(shell, env, workdir):
    """输出解码回退：GBK 字节输出解码为可读中文。"""
    result = shell.execute(
        {"command": 'python -c "import sys;sys.stdout.buffer.write(\'中文\'.encode(\'gbk\'))"'},
        env,
    )
    assert "中文" in visible(result.llm_text)


def test_decode_output_replace_fallback(monkeypatch):
    """两种情况都无法解码时按 UTF-8 替换字符兜底；Unix 无 GBK 回退。"""
    assert decode_output(b"") == ""
    assert decode_output("中文".encode("utf-8")) == "中文"
    if IS_WINDOWS:
        assert decode_output("中文".encode("gbk")) == "中文"
    else:
        assert decode_output("中文".encode("gbk")) != "中文"
    monkeypatch.setattr(sys, "platform", "linux")
    assert decode_output(b"\xd6\xd0") != "中文"  # Unix 仅 UTF-8 → 替换字符
    assert "\ufffd" in decode_output(b"\xff\xfe\x00")


def test_child_utf8_output_for_emoji(shell, env, workdir):
    """子进程输出编码：emoji 不因 GBK 代码页编码失败。"""
    result = shell.execute({"command": 'python -c "print(\'🎉\')"'}, env)
    text = visible(result.llm_text)
    assert "🎉" in text
    assert text.startswith("[exit code: 0]")
    assert "UnicodeEncodeError" not in text


def test_startup_failure_text(shell, env, workdir):
    """启动失败文案：命令含 NUL 等非法字符 → [错误: 启动失败: …]。"""
    result = shell.execute({"command": "echo a\x00b"}, env)
    assert visible(result.llm_text).startswith("[错误: 启动失败: ")


def test_cmd_missing_text(monkeypatch, shell, env, workdir):
    """cmd.exe 缺失文案（注入 Popen 抛 FileNotFoundError）。"""
    if not IS_WINDOWS:
        pytest.skip("Windows cmd.exe 路径")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("cmd.exe")

    monkeypatch.setattr(executor.subprocess, "Popen", _raise)
    result = shell.execute({"command": "echo x"}, env)
    assert visible(result.llm_text).startswith("[错误: cmd.exe未找到: ")


# ═══════════════════════════════════════════════════════════════
# 4. 前台结果格式与提示符
# ═══════════════════════════════════════════════════════════════


def test_normal_result_shape(shell, env, workdir):
    """正常结果：退出码行 + stdout 段 + 提示符（按序组成）。"""
    result = shell.execute({"command": "echo hello"}, env)
    assert result.llm_text == "\n".join([signal.rc_line(0), "hello", prompt()])


def test_empty_stdout_segment_omitted(shell, env, workdir):
    """空输出段省略：仅 stderr 有内容时不产生空行，[stderr] 行紧随退出码行。"""
    result = shell.execute(
        {"command": 'python -c "import sys;sys.stderr.write(\'boom\')"'}, env
    )
    assert visible(result.llm_text) == "\n".join(
        [rc_visible(0), "[stderr]\nboom", prompt()]
    )


def test_stderr_only_from_shell(shell, env, workdir):
    """命令失败时的 stderr 段位置：退出码行 → [stderr] 段 → 提示符。"""
    result = shell.execute({"command": "narnat_no_such_cmd_zzz"}, env)
    lines = visible(result.llm_text).split("\n")
    assert lines[0] == "[exit code: 1]"
    assert lines[1] == "[stderr]"
    assert lines[-1] == prompt()


def test_timeout_result_shape(shell, env, workdir):
    """超时结果：超时提示位于输出段之后、提示符之前，且带错误标签。"""
    result = shell.execute({"command": SLEEPER, "timeout": 1}, env)
    text = visible(result.llm_text)
    assert text.split("\n")[-2] == "[超时: 命令执行超过1秒，已终止]"
    assert text.split("\n")[-1] == prompt()
    assert signal.has_error(result.llm_text) is True


def test_format_prompt_platforms(monkeypatch, tmp_path):
    """提示符：Windows 为 `{cwd}>`；Unix 为 `~$ ` / `~/子路径$ ` / `{cwd}$ `。"""
    monkeypatch.chdir(tmp_path)
    if IS_WINDOWS:
        assert executor._format_prompt() == f"{tmp_path}>"
    else:
        assert executor._format_prompt() == f"{tmp_path}$ "

    monkeypatch.setattr(sys, "platform", "linux")
    home = Path.home()
    monkeypatch.chdir(home)
    assert executor._format_prompt() == "~$ "
    nested = home / "narnat-prompt-test"
    nested.mkdir(exist_ok=True)
    monkeypatch.chdir(nested)
    assert executor._format_prompt() == f"~{os.sep}narnat-prompt-test$ "


# ═══════════════════════════════════════════════════════════════
# 5. cd 持久化
# ═══════════════════════════════════════════════════════════════


def test_cd_persists_for_later_tools(shell, env, workdir):
    """单条 cd 持久化：宿主自身工作目录切换，后续调用共享新目录。"""
    target = workdir / "subdir"
    target.mkdir()
    result = shell.execute({"command": "cd subdir"}, env)
    assert visible(result.llm_text) == "\n".join([rc_visible(0), prompt()])
    assert Path.cwd() == target

    follow = shell.execute({"command": 'python -c "import os;print(os.getcwd())"'}, env)
    assert str(target) in visible(follow.llm_text)


def test_noarg_cd_platform_difference(monkeypatch, shell, env, workdir):
    """无参数 cd 平台差异：Windows 不切换目录；Unix 切换到 $HOME。"""
    if IS_WINDOWS:
        result = shell.execute({"command": "cd"}, env)
        assert visible(result.llm_text) == "\n".join([rc_visible(0), prompt()])
        assert Path.cwd() == workdir
    else:
        result = shell.execute({"command": "cd"}, env)
        assert Path.cwd() == Path.home()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(executor, "find_executable", lambda *names: "narnat_no_such_shell")
    result = shell.execute({"command": "cd"}, env)
    assert visible(result.llm_text) == "\n".join([rc_visible(0), prompt()])
    assert Path.cwd() == Path.home()


def test_cd_failure_text(shell, env, workdir):
    """切换失败文案：`cd: {错误}` 行 + [exit code: 1] + 提示符。"""
    result = shell.execute({"command": "cd no_such_dir_zzz"}, env)
    lines = visible(result.llm_text).split("\n")
    assert lines[0].startswith("cd: ")
    assert lines[1] == "[exit code: 1]"
    assert lines[2] == prompt()


def test_cd_shortcut_and_expansion(shell, env, workdir, monkeypatch):
    """简写与展开：cd.. → 父目录；cd %VAR% → 展开后的目录。"""
    parent = workdir.parent
    result = shell.execute({"command": "cd.."}, env)
    assert visible(result.llm_text).startswith("[exit code: 0]")
    assert Path.cwd() == parent

    target = workdir / "expanded"
    target.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("NARNAT_CD_TEST", str(target))
    shell.execute({"command": "cd %NARNAT_CD_TEST%"}, env)
    assert Path.cwd() == target


def test_cd_extract_helpers():
    """cd 解析纯函数：/d 前缀剥离、引号剥离、~ 展开、无参数返回 None。"""
    assert executor._extract_cd_path("cd") is None
    assert executor._extract_cd_path("cd..") == ".."
    assert executor._extract_cd_path("cd...") == str(Path("..") / "..")
    assert executor._extract_cd_path("cd\\") == "\\"
    assert executor._extract_cd_path('cd /d "C:\\Temp"') == "C:\\Temp"
    assert executor._is_cd_command("cd sub") is True
    assert executor._is_cd_command("cd sub && ls") is False
    assert executor._is_cd_command("cd /tmp; ls") is False


def test_compound_cd_goes_to_segments(shell, env, workdir):
    """复合运算符不按纯 cd 处理：cd sub && echo done 走多段执行流程。"""
    sub = workdir / "sub"
    sub.mkdir()
    result = shell.execute({"command": "cd sub && echo done"}, env)
    text = visible(result.llm_text)
    assert "done" in text
    assert text.split("\n")[-2] == "[exit code: 0]"  # 总退出码行 → 多段路径
    assert Path.cwd() == sub


# ═══════════════════════════════════════════════════════════════
# 6. 多段命令执行
# ═══════════════════════════════════════════════════════════════


def test_split_commands_rules():
    """分段逻辑：引号外、括号组外、跳过 ^ 转义。"""
    assert executor._split_commands("echo a && echo b") == [
        ("", "echo a"), ("&&", "echo b")]
    assert executor._split_commands('echo "a && b"') == [("", 'echo "a && b"')]
    assert executor._split_commands("(a && b) && c") == [
        ("", "(a && b)"), ("&&", "c")]
    assert executor._split_commands("echo ^&& x") == [("", "echo ^&& x")]
    assert executor._split_commands("a || b || c") == [
        ("", "a"), ("||", "b"), ("||", "c")]
    assert executor._split_commands("   ") == [("", "")]


def test_segment_and_short_circuit(shell, env, workdir):
    """&& 短路跳过：前段失败时后段跳过并输出跳过文案，末尾为总退出码。"""
    result = shell.execute({"command": "false && echo hi"}, env)
    assert visible(result.llm_text) == "\n".join([
        rc_visible(1),
        "[跳过: 前一命令失败(退出码1)] echo hi",
        rc_visible(1),
        prompt(),
    ])


def test_segment_or_short_circuit(shell, env, workdir):
    """|| 短路跳过：前段成功时后段跳过。"""
    result = shell.execute({"command": "echo hi || echo fallback"}, env)
    assert visible(result.llm_text) == "\n".join([
        "hi",
        "[跳过: 前一命令成功] echo fallback",
        rc_visible(0),
        prompt(),
    ])


def test_segment_outputs_and_total_rc(shell, env, workdir):
    """段输出与总退出码：各成功段无退出码行，末尾唯一总退出码行。"""
    result = shell.execute({"command": "echo a && echo b"}, env)
    text = visible(result.llm_text)
    assert text == "\n".join(["a", "b", rc_visible(0), prompt()])
    assert text.count("[exit code:") == 1


def test_segment_budget_exhaustion(shell, env, workdir, monkeypatch):
    """预算递减与耗尽：前段耗尽总预算后，后续段立即以「超过1.0秒」判定超时。"""
    runner = executor.ShellExecutor(shell.runtime)
    real_collect = executor.ShellExecutor._collect

    calls = {"n": 0}

    def _slow_first_collect(self, proc, timeout):
        result = real_collect(self, proc, timeout)
        calls["n"] += 1
        if calls["n"] == 1:
            # 首段耗时超过总预算 → 预算被耗尽（不真等 1.2 秒，直接注入耗时口径）
            return result._replace(wait_elapsed=result.wait_elapsed + 1.2)
        return result

    monkeypatch.setattr(executor.ShellExecutor, "_collect", _slow_first_collect)
    text = visible(runner._run_segments([("", "echo a"), ("&&", "echo SEG_MARK")], 1, 4000))
    assert "a" in text                                          # 首段正常完成（耗尽预算、无退出码行）
    assert text.count("[超时: 命令执行超过1.0秒，已终止]") == 1  # 仅预算为 0 的后续段
    assert text.index("a") < text.index("[超时:")                # 超时提示属于后续段
    assert "[exit code:" not in text                             # 段超时不追加总退出码行

    monkeypatch.undo()
    # 预算恰为 0 时同样立即判定超时
    text = visible(runner._run_segments([("", "echo a"), ("&&", "echo SEG_MARK")], 0, 4000))
    assert text.startswith("[超时: 命令执行超过1.0秒，已终止]")
    assert "SEG_MARK" not in text


def test_segment_timeout_format_python(shell, env, workdir):
    """段超时格式（python 段）：超时提示行开头，秒数为段耗时（下限 1.0，1 位小数）。"""
    result = shell.execute({"command": f"{SLEEPER} && echo SEG_MARK", "timeout": 1}, env)
    text = visible(result.llm_text)
    first_line = text.split("\n")[0]
    matched = re.fullmatch(r"\[超时: 命令执行超过(\d+\.\d)秒，已终止\]", first_line)
    assert matched is not None
    assert float(matched.group(1)) >= 1.0
    assert "SEG_MARK" not in text
    assert "[exit code:" not in text
    assert signal.has_error(result.llm_text) is True


def test_segment_timeout_format_shell(shell, env, workdir):
    """段超时格式（shell 段）：非 python 段超时同样终止后续段。"""
    if not IS_WINDOWS:
        pytest.skip("Windows ping 用例")
    result = shell.execute(
        {"command": "ping -n 6 127.0.0.1 >nul && echo SEG_MARK", "timeout": 1}, env
    )
    text = visible(result.llm_text)
    assert re.fullmatch(r"\[超时: 命令执行超过\d+\.\d秒，已终止\]", text.split("\n")[0])
    assert "SEG_MARK" not in text


def test_segments_interrupt(shell, env, workdir, runtime):
    """多段中断：终止后续段、末尾 [用户中断]、无总退出码行。"""
    box: dict = {}

    def _run():
        box["result"] = shell.execute({"command": f"{SLEEPER} && echo x"}, env)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.5)
    runtime.interrupt()
    thread.join(timeout=20)
    assert not thread.is_alive()
    text = visible(box["result"].llm_text)
    assert text == f"[用户中断]\n{prompt()}"
    assert "[exit code" not in text


def test_segments_interrupt_drops_segment_output(shell, env, workdir, runtime):
    """兼容怪癖③：多段中某段被中断时该段已产出的输出不保留。"""
    payload = ('python -c "import sys,time;print(\'partial\');'
               'sys.stdout.flush();time.sleep(8)"')
    box: dict = {}

    def _run():
        box["result"] = shell.execute({"command": f"{payload} && echo x"}, env)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(1.0)
    runtime.interrupt()
    thread.join(timeout=20)
    text = visible(box["result"].llm_text)
    assert text == f"[用户中断]\n{prompt()}"
    assert "partial" not in text


def test_segment_cd_applies(shell, env, workdir):
    """多段中的 cd 段：宿主直接切换工作目录（cd 段无输出），后续段在新目录执行。"""
    sub = workdir / "subdir"
    sub.mkdir()
    result = shell.execute(
        {"command": 'cd subdir && python -c "import os;print(os.getcwd())"'}, env
    )
    text = visible(result.llm_text)
    assert str(sub) in text
    assert "cd:" not in text
    assert Path.cwd() == sub


def test_segment_noarg_cd_does_not_switch(shell, env, workdir):
    """兼容怪癖⑧：多段中的无参数 cd 不切换目录（仅记录成功）。"""
    result = shell.execute(
        {"command": 'cd && python -c "import os;print(os.getcwd())"'}, env
    )
    assert str(workdir) in visible(result.llm_text)
    assert Path.cwd() == workdir


def test_segment_start_failure_text(shell, env, workdir):
    """段启动失败：`[错误: 段{i}启动失败: …]` 并终止后续段。"""
    result = shell.execute({"command": "echo a\x00b && echo SEG_MARK"}, env)
    text = visible(result.llm_text)
    assert text.startswith("[错误: 段0启动失败: ")
    assert "SEG_MARK" not in text


# ═══════════════════════════════════════════════════════════════
# 7. python 载荷直执行
# ═══════════════════════════════════════════════════════════════


def test_scan_and_parse_suffix_helpers(monkeypatch):
    """后缀扫描/解析：2>&1 剥离、>nul 丢弃、引号路径、不支持形态回退。"""
    assert executor._scan_code_suffix('"code" 2>&1') == ('"code"', None)
    assert executor._scan_code_suffix('"code" >nul') == ('"code"', ">nul")
    assert executor._scan_code_suffix('"code" | findstr x') == ('"code"', "| findstr x")
    assert executor._scan_code_suffix('"code" extra') == ('"code" extra', None)
    assert executor._parse_suffix(">nul") == ("discard",)
    assert executor._parse_suffix(">NUL") == ("discard",)
    assert executor._parse_suffix("> out.txt") == ("w", "out.txt")
    assert executor._parse_suffix(">> out.txt") == ("a", "out.txt")
    assert executor._parse_suffix('> "a b.txt"') == ("w", "a b.txt")
    assert executor._parse_suffix(">") is None
    assert executor._parse_suffix(">>") is None
    assert executor._parse_suffix("| a | b") is None
    assert executor._parse_suffix("| a > b") is None
    assert executor._parse_suffix('> "a b" extra') is None
    assert executor._parse_suffix("> a b") is None


def test_py_direct_hit_conditions(monkeypatch):
    """python 直执行命中条件：解释器可解析且实为 .exe，基名匹配 python 形态。"""
    monkeypatch.setattr(executor.shutil, "which", lambda name: r"C:\Python312\python.exe")
    hit = executor._try_extract_py_code('python -c "print(1)"')
    assert hit is not None and hit[0] == "python" and hit[3] is None
    assert executor._try_extract_py_code('python -c "print(1)" >nul')[3] == ("discard",)

    monkeypatch.setattr(executor.shutil, "which", lambda name: r"C:\tools\spy.exe")
    assert executor._try_extract_py_code('spy -c "print(1)"') is None

    monkeypatch.setattr(executor.shutil, "which", lambda name: r"C:\tools\python.bat")
    assert executor._try_extract_py_code('python -c "print(1)"') is None

    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    assert executor._try_extract_py_code('python -c "print(1)"') is None

    monkeypatch.setattr(executor.shutil, "which", lambda name: r"C:\Python312\python.exe")
    assert executor._try_extract_py_code("python -c print(1)") is None  # 无引号
    assert executor._try_extract_py_code('python -m mod "x"') is None
    assert executor._try_extract_py_code('python -c "print(1)" | a | b') is None


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 单段直执行路径")
def test_py_direct_windows_payload(shell, env, workdir):
    """Windows 单段直执行：多行与 % 原样送达解释器（不经 cmd 吞改字符）。"""
    payload = 'python -c "import sys\nprint(1<2)\nprint(\'%TEMP%\')"'
    result = shell.execute({"command": payload}, env)
    text = visible(result.llm_text)
    assert "True" in text
    assert "%TEMP%" in text  # 未被 cmd 展开 → 确由解释器原样接收


def test_py_segment_in_multi_command(shell, env, workdir):
    """多段中的 python 段：直执行且后续段独立执行、计入总退出码。"""
    result = shell.execute({"command": 'python -c "print(1)" && echo ok'}, env)
    assert visible(result.llm_text) == "\n".join(["1", "ok", rc_visible(0), prompt()])


def test_py_unsupported_forms_fall_back(shell, env, workdir):
    """不支持形态回退：`| a | b` 后缀交由 shell 原路径（未走直执行，故失败在 cmd 管道）。"""
    assert executor._try_extract_py_code('python -c "print(1)" | a | b') is None
    result = shell.execute({"command": 'python -c "print(1)" | a | b'}, env)
    text = visible(result.llm_text)
    assert "[exit code: 0]" not in text
    assert "[stderr]" in text


def test_py_suffix_nul_discards_stdout(shell, env, workdir):
    """>nul 丢弃 stdout：结果不含 stdout，stderr 仍展示。"""
    payload = 'python -c "import sys;print(\'x\');sys.stderr.write(\'ERR\')" >nul'
    result = shell.execute({"command": payload}, env)
    assert visible(result.llm_text) == "\n".join(
        [rc_visible(0), "[stderr]\nERR", prompt()]
    )


def test_py_suffix_redirect_writes_file(shell, env, workdir):
    """重定向写文件：stdout 以 UTF-8 写入（覆盖/追加），返回结果 stdout 为空。"""
    result = shell.execute({"command": 'python -c "print(\'x\')" > out.txt'}, env)
    assert visible(result.llm_text) == "\n".join([rc_visible(0), prompt()])
    assert (workdir / "out.txt").read_bytes() == b"x\r\n"

    shell.execute({"command": 'python -c "print(\'y\')" >> out.txt'}, env)
    assert (workdir / "out.txt").read_bytes() == b"x\r\ny\r\n"


def test_py_suffix_redirect_to_spaced_path(shell, env, workdir):
    """重定向到带空格路径（引号包裹）。"""
    result = shell.execute({"command": 'python -c "print(\'z\')" > "out file.txt"'}, env)
    assert visible(result.llm_text) == "\n".join([rc_visible(0), prompt()])
    assert (workdir / "out file.txt").read_bytes() == b"z\r\n"


def test_py_suffix_write_failure(shell, env, workdir):
    """写文件失败：退出码 1 + `[stderr]\\n写入文件失败: …`。"""
    (workdir / "adir").mkdir()
    result = shell.execute({"command": 'python -c "print(\'x\')" > adir'}, env)
    text = visible(result.llm_text)
    assert text.startswith("[exit code: 1]")
    assert "[stderr]\n写入文件失败: " in text


def test_py_suffix_pipe(shell, env, workdir):
    """管道后缀：解释器 stdout 喂给管道命令，展示管道命令输出。"""
    result = shell.execute(
        {"command": 'python -c "print(\'x\')" | findstr x'}, env
    )
    text = visible(result.llm_text)
    assert text == "\n".join([rc_visible(0), "x", prompt()])


def test_py_suffix_interrupt_keeps_file_absent(shell, env, workdir, runtime):
    """中断不写文件：带 > file 后缀的直执行命令被中断时不写目标文件。"""
    payload = ('python -c "import time,sys;print(\'x\');sys.stdout.flush();'
               'time.sleep(8)" > interrupted.txt')
    box: dict = {}

    def _run():
        box["result"] = shell.execute({"command": payload, "timeout": 30}, env)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.8)
    runtime.interrupt()
    thread.join(timeout=20)
    assert visible(box["result"].llm_text) == f"[用户中断]\n{prompt()}"
    assert not (workdir / "interrupted.txt").exists()


def test_py_suffix_timeout_keeps_file_absent(shell, env, workdir):
    """超时不写文件（同一判定的另一分支）。"""
    payload = ('python -c "import time;print(\'x\');time.sleep(8)" > timeout.txt')
    result = shell.execute({"command": payload, "timeout": 1}, env)
    assert visible(result.llm_text).startswith("[超时: 命令执行超过1秒，已终止]")
    assert not (workdir / "timeout.txt").exists()


# ═══════════════════════════════════════════════════════════════
# 8. 输出截断
# ═══════════════════════════════════════════════════════════════


def test_truncate_head_tail_and_notice():
    """首尾保留与提示文案：中段含四个关键片段。"""
    text = "A" * 100 + "B" * 100
    out = executor._truncate_output(text, 60)
    head_end = 40
    assert out.startswith("A" * head_end)
    assert out.endswith("B" * 20)
    assert f"输出共{len(text)}字符" in out
    assert f"已保留首{head_end}字符+尾{20}字符" in out
    assert re.search(r"≈\d+token", out)
    assert "增大max_output_chars可获取完整输出" in out


def test_truncate_within_limit_untouched():
    """未超限原样返回（无任何截断提示）。"""
    text = "A" * 60
    assert executor._truncate_output(text, 60) == text
    assert executor._truncate_output("短文", 4000) == "短文"


def _has_partial_tag(text: str) -> bool:
    """文本中是否残留残缺标签：剥离两个完整标签后仍出现「[+十六进制片段」。"""
    cleaned = text.replace(f"[{signal.TAG}]", "").replace(f"[{signal.ERR_TAG}]", "")
    return (
        re.search(r"\[[0-9a-f]{2,7}(?![0-9a-f])", cleaned) is not None
        or re.search(r"(?<![0-9a-f])[0-9a-f]{2,7}\]", cleaned) is not None
    )


def test_truncate_never_splits_tags():
    """标签不被切开：切点落在标签内部时吸附到标签边界（头部/尾部两侧）。

    断言口径：头部侧——切点落在退出码标签内部时吸附到标签结尾（parse_rc 仍可识别），
    否则标签整体落在中段被丢弃（parse_rc 为 None），绝不出现残缺片段；
    尾部侧同理。
    """
    for pad in range(0, 30):
        head_text = "A" * (40 - pad) + signal.rc_line(1) + "B" * 30 + "C" * 300
        tag_start = (40 - pad) + len("[exit code: 1] ")
        out = executor._truncate_output(head_text, 60)
        assert not _has_partial_tag(out)
        assert signal.parse_rc(out) == (1 if pad >= 15 else None)

        tail_text = "A" * 300 + "B" * 30 + signal.error_line("boom") + "C" * (40 - pad)
        err_start = 330 + len("[错误: boom] ")
        err_end = err_start + len(signal.ERR_TAG) + 2
        out = executor._truncate_output(tail_text, 60)
        assert not _has_partial_tag(out)
        assert signal.has_error(out) is (len(tail_text) - 20 < err_end)
        if err_start < len(tail_text) - 20 < err_end:
            assert signal.has_error(out) is True  # 切点落在标签内部 → 吸附后完整保留


def test_end_to_end_truncation(shell, env, workdir):
    """端到端截断：命令输出超过 max_output_chars 时结果带截断提示。"""
    result = shell.execute(
        {"command": 'python -c "print(\'A\'*5000)"', "max_output_chars": 4000}, env
    )
    text = visible(result.llm_text)
    assert "中间截断" in text
    assert "增大max_output_chars可获取完整输出" in text
    assert text.endswith(prompt())


def test_zero_max_output_chars_runs_command(shell, env, workdir):
    """兼容怪癖①：max_output_chars=0 不早退——命令执行完毕、副作用已发生、只丢输出。"""
    result = shell.execute(
        {"command": "echo side > zero.txt", "max_output_chars": 0}, env
    )
    assert visible(result.llm_text) == "[错误: max_output_chars需为正整数]"
    assert (workdir / "zero.txt").exists()


# ═══════════════════════════════════════════════════════════════
# 9. 后台任务：提交与槽位
# ═══════════════════════════════════════════════════════════════


def test_submit_returns_handle_and_header(manager, workdir):
    """提交返回值与句柄：bgN 编号、结果文件绝对路径、查状态/等待提示、日志头。"""
    text = manager.submit("echo bg-done")
    lines = text.split("\n")
    assert lines[0] == "bg1 已提交（后台运行）"
    log_path = Path(lines[1].removeprefix("结果: "))
    assert log_path.is_absolute()
    assert log_path.name == "bg1.log"
    assert log_path.parent.name.startswith("narnat_bg_")
    assert lines[2] == '查状态: Shell(bg="status") · 等待完成: Shell(bg="wait")'
    assert log_path.read_text(encoding="utf-8").startswith("# bg1 提交于 ")

    assert wait_until(lambda: "bg1 完成 exit=0" in manager.snapshot())
    assert "bg-done" in log_path.read_text(encoding="utf-8")


def test_submit_without_command(shell, env, workdir):
    """空命令提交报错。"""
    result = shell.execute({"background": True}, env)
    assert visible(result.llm_text) == "[错误: background=true 提交后台任务需提供 command]"


def test_submit_concurrency_limit(manager, workdir):
    """并发上限：8 个运行中时第 9 个提交返回固定错误文案。"""
    for _ in range(8):
        assert manager.submit(SLEEPER).startswith("bg")
    ninth = manager.submit("echo x")
    assert visible(ninth) == (
        "[错误: 后台并发已达上限8(bg1~bg8)，当前运行: "
        "bg1, bg2, bg3, bg4, bg5, bg6, bg7, bg8。"
        '请用 Shell(bg="wait") 等待完成，或 Shell(bg="cancel", id=N) 取消不再需要的任务]'
    )
    assert manager.running_count() == 8
    manager.cleanup_all()


def test_slot_reuse_after_terminal(manager, workdir):
    """终态释放与编号复用：空闲槽优先，其次复用完成时间最早的终态槽位。"""
    manager.submit("echo first")
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")

    second = manager.submit("echo second")
    assert second.startswith("bg2 已提交")  # 空闲槽优先
    assert manager.wait(10).startswith("[后台任务] bg2 完成 exit=0")

    for _ in range(6):
        manager.submit(SLEEPER)  # bg3~bg8 运行中

    reused = manager.submit("echo third")
    assert reused.startswith("bg1 已提交")  # 仅剩 bg1 空闲 → 复用终态槽位
    manager.cleanup_all()


def test_slot_reuse_archives_old_result(manager, workdir):
    """复用归档：旧结果改名 bgN.log.{序号}.prev（含 .tail），提交文本追加归档提示。"""
    submitted = manager.submit("echo archived-content")
    old_log = Path(submitted.split("\n")[1].removeprefix("结果: "))
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")
    tail = old_log.with_name(old_log.name + ".tail")
    tail.write_text("tail-marker", encoding="utf-8")

    for _ in range(7):
        manager.submit(SLEEPER)  # 占满其余槽位
    reused = manager.submit("echo new")
    assert reused.startswith("bg1 已提交")
    assert "提示: 已归档该槽位旧任务结果到 " in reused
    archived = Path(reused.split("提示: 已归档该槽位旧任务结果到 ")[1].split("，")[0])
    assert archived.name == "bg1.log.1.prev"
    assert "archived-content" in archived.read_text(encoding="utf-8")
    assert archived.with_name(archived.name + ".tail").read_text(encoding="utf-8") == "tail-marker"
    assert not tail.exists()
    manager.cleanup_all()


def test_log_header_and_incremental_flush(manager, workdir):
    """日志头与增量落盘：提交即可读到头行，运行中可读到分块增量。"""
    payload = ('python -c "import sys,time;'
               "[print(chr(88)*100) or sys.stdout.flush() or time.sleep(0.05) "
               'for _ in range(40)]"')
    text = manager.submit(payload)
    log_path = Path(text.split("\n")[1].removeprefix("结果: "))

    header = log_path.read_text(encoding="utf-8").split("\n")[0]
    assert re.fullmatch(r"# bg1 提交于 \d{2}:\d{2}:\d{2} \| 命令: .+", header)
    assert header.endswith(payload)

    grew = wait_until(lambda: log_path.stat().st_size > len(header) + 10, timeout=10)
    assert grew, "运行中应能读到增量输出"
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")
    assert log_path.read_text(encoding="utf-8").count("X" * 100) == 40


def test_log_encoding_unified_utf8(manager, workdir):
    """编码统一：GBK 输出经转码后日志为单编码 UTF-8、中文完整。"""
    payload = 'python -c "import sys;sys.stdout.buffer.write(\'中文\'.encode(\'gbk\'))"'
    text = manager.submit(payload)
    log_path = Path(text.split("\n")[1].removeprefix("结果: "))
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")
    raw = log_path.read_bytes()
    assert "中文" in raw.decode("utf-8")  # 严格 UTF-8 解码不报错 → 单编码


def test_background_startup_failure_releases_slot(manager, workdir):
    """启动失败释放槽位：进程启动失败返回固定文案且槽位不保持占用。"""
    failed = manager.submit("echo a\x00b")
    assert visible(failed).startswith("[错误: 后台任务启动失败: ")
    assert manager.snapshot() == "[后台任务] 当前无后台任务"
    assert manager.running_count() == 0

    again = manager.submit("echo ok")
    assert again.startswith("bg1 已提交")  # 槽位已释放可复用
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")


def test_unwritable_log_file(manager, workdir, monkeypatch):
    """结果文件不可写：`[错误: 无法写入结果文件: …]` 且槽位释放。"""
    real_open = builtins.open

    def _open(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".log"):
            raise OSError("磁盘满了")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)
    failed = manager.submit("echo x")
    assert visible(failed) == "[错误: 无法写入结果文件: 磁盘满了]"
    monkeypatch.undo()

    assert manager.snapshot() == "[后台任务] 当前无后台任务"


def test_background_unix_missing_shell(manager, workdir, monkeypatch):
    """Unix 缺 bash/sh 时后台提交错误文案为「未找到shell，请安装bash或sh后重试」。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(background_module, "find_executable", lambda *names: None)
    failed = manager.submit("echo x")
    assert visible(failed) == (
        "[错误: 后台任务启动失败: 未找到shell，请安装bash或sh后重试]"
    )
    assert manager.snapshot() == "[后台任务] 当前无后台任务"


def test_submit_via_shell_tool(manager, shell, env, workdir):
    """经 Shell 工具提交：background=true 返回句柄并按提交时刻工作目录启动。"""
    result = shell.execute({"command": 'python -c "import os;print(os.getcwd())"',
                            "background": True}, env)
    text = visible(result.llm_text)
    assert text.startswith("bg1 已提交（后台运行）")
    log_path = Path(text.split("\n")[1].removeprefix("结果: "))
    assert wait_until(lambda: f"{workdir}" in log_path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════
# 10. 后台任务：状态与等待
# ═══════════════════════════════════════════════════════════════


def test_snapshot_format(manager, workdir):
    """快照格式：运行中与失败条目的两行式结构 + 末尾空闲槽位列表。"""
    manager.submit(SLEEPER)
    manager.submit("narnat_no_such_cmd_zzz")
    manager.wait(10)

    snapshot = manager.snapshot()
    lines = snapshot.split("\n")
    assert lines[0] == "[后台任务]"
    assert re.fullmatch(r'bg1 运行中 已运行\d{2}:\d{2} "python -c "import time;time\.sleep\(8\)"" 输出\d+B', lines[1])
    assert lines[2].startswith("    结果: ")
    assert re.fullmatch(r'bg2 失败 exit=1 \(\d{2}:\d{2}:\d{2}\) "narnat_no_such_cmd_zzz" 输出\d+(B|KB)', lines[3])
    assert lines[4].startswith("    结果: ")
    assert lines[5] == "空闲槽位: bg2, bg3, bg4, bg5, bg6, bg7, bg8"
    manager.cleanup_all()


def test_snapshot_without_tasks(manager):
    """无任务快照。"""
    assert manager.snapshot() == "[后台任务] 当前无后台任务"


def test_wait_wakes_on_completion(manager, workdir):
    """完成事件唤醒等待：立即返回完成事件 + 完整快照。"""
    manager.submit("echo done")
    text = manager.wait(10)
    assert text.startswith("[后台任务] bg1 完成 exit=0\n[后台任务]\n")


def test_wait_multiple_events(manager, workdir, monkeypatch):
    """多条完成事件以 `；` 连接（等到全部任务收口再唤醒，模拟同一时刻多条收口）。"""
    real_event = manager._terminal_event

    class _AllDoneEvent:
        """替身终态事件：本任务全部收口后才放行唤醒（真实事件仍照常置位）。"""

        def clear(self):
            real_event.clear()

        def set(self):
            real_event.set()

        def wait(self, timeout=None):
            deadline = time.time() + (timeout if timeout else 0.1)
            while time.time() < deadline:
                if manager.running_count() == 0:
                    return True
                time.sleep(0.02)
            return real_event.wait(timeout=0.01)

    monkeypatch.setattr(manager, "_terminal_event", _AllDoneEvent())
    manager.submit('python -c "import time;time.sleep(0.5)"')
    manager.submit('python -c "import time;time.sleep(0.5)"')
    text = manager.wait(10)
    assert text.startswith("[后台任务] bg1 完成 exit=0；bg2 完成 exit=0\n")


def test_wait_without_running_tasks(manager):
    """无运行任务立即返回：提示 + 快照。"""
    text = manager.wait(5)
    assert text == "[后台任务] 当前无运行中的后台任务\n[后台任务] 当前无后台任务"


def test_wait_timeout_text(manager, workdir):
    """等待超时：等待达指定秒数仍无新完成时的文案 + 快照。"""
    manager.submit(SLEEPER)
    start = time.time()
    text = manager.wait(1)
    assert time.time() - start >= 1
    assert text.startswith("[后台任务] 等待1秒超时，当前无新完成\n")
    assert "运行中 已运行" in text
    manager.cleanup_all()


def test_wait_interrupt_keeps_tasks_running(manager, runtime, workdir):
    """打断等待不杀任务：等待被 ESC 打断的文案 + 快照，后台任务继续运行。"""
    manager.submit(SLEEPER)
    box: dict = {}

    def _wait():
        box["text"] = manager.wait(30)

    thread = threading.Thread(target=_wait)
    thread.start()
    time.sleep(0.4)
    runtime.interrupt()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert box["text"].startswith(
        "[后台任务] 等待已被用户打断（后台任务不受影响，继续运行）\n")
    assert manager.running_count() == 1  # 任务未受影响
    assert "运行中" in manager.snapshot()
    manager.cleanup_all()


def test_cancel_wakes_wait_with_timeout_text(manager, workdir):
    """兼容怪癖④：取消唤醒等待时报「等待{N}秒超时，当前无新完成」（不宣布取消事件）。"""
    manager.submit(SLEEPER)
    box: dict = {}

    def _wait():
        box["text"] = manager.wait(30)

    thread = threading.Thread(target=_wait)
    thread.start()
    time.sleep(0.4)
    manager.cancel(1)
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert box["text"].startswith("[后台任务] 等待30秒超时，当前无新完成\n")
    assert "已取消" in box["text"]


def test_wait_parameter_validation(manager, shell, env, workdir, monkeypatch):
    """等待参数校验：非数字在归一化拦截、负值报错、缺省/0 按 120 秒、受上限钳制。"""
    bad = shell.execute({"bg": "wait", "timeout": "abc"}, env)
    assert visible(bad.llm_text) == "[错误: timeout/max_output_chars需为整数]"

    negative = shell.execute({"bg": "wait", "timeout": -5}, env)
    assert visible(negative.llm_text) == "[错误: timeout需为正整数（秒）]"

    seen: list = []
    monkeypatch.setattr(manager, "wait", lambda t: seen.append(t) or "ok")
    shell.execute({"bg": "wait"}, env)
    shell.execute({"bg": "wait", "timeout": 0}, env)
    shell.execute({"bg": "wait", "timeout": 100}, env)
    env.settings.max_timeout_seconds = 30
    shell.execute({"bg": "wait", "timeout": 100}, env)
    assert seen == [120, 120, 100, 30]


# ═══════════════════════════════════════════════════════════════
# 11. 后台任务：取消
# ═══════════════════════════════════════════════════════════════


def test_cancel_running_task(manager, workdir):
    """正常取消：进程树被终止、已产出内容仍可读取。"""
    text = manager.submit(SLEEPER)
    log_path = Path(text.split("\n")[1].removeprefix("结果: "))
    result = manager.cancel(1)
    assert visible(result) == (
        f"bg1 已取消（进程树已终止，已产出内容保留在 {log_path} 仍可 Read 读取；"
        "管道中尚未收口的剩余输出随后补落盘）"
    )
    assert wait_until(lambda: "已取消" in manager.snapshot())
    assert log_path.exists()  # 已产出内容保留
    assert log_path.read_text(encoding="utf-8").startswith("# bg1 提交于 ")


def test_cancel_id_missing_or_non_integer(manager, workdir):
    """id 缺失与非整数。"""
    assert visible(manager.dispatch("", 120, False, "cancel", None)) == (
        "[错误: cancel 需指定任务编号 id（如 id=3 取消 bg3）]")
    assert visible(manager.dispatch("", 120, False, "cancel", "abc")) == (
        "[错误: id 需为整数: abc]")


def test_cancel_out_of_range_and_empty_slot(manager, workdir):
    """编号越界与空槽。"""
    assert visible(manager.cancel(9)) == "[错误: bg9 编号无效（有效范围 bg1~bg8）]"
    assert visible(manager.cancel(3)) == "[错误: bg3 空闲（无任务）]"


def test_cancel_terminal_slot(manager, workdir):
    """终态不可取消。"""
    manager.submit("echo done")
    manager.wait(10)
    assert visible(manager.cancel(1)) == "[错误: bg1 已处于终态（done），无需取消]"


def test_cancel_unknown_op(manager):
    """未知 bg 操作。"""
    assert visible(manager.dispatch("", 120, False, "foo", None)) == (
        "[错误: 未知 bg 操作: foo（可用: status / wait / cancel）]")


def test_bg_ops_via_shell_tool(shell, manager, env, workdir):
    """经 Shell 工具的后台管理入口：status/wait/cancel 参数透传与耗时语义。"""
    assert visible(shell.execute({"bg": "status"}, env).llm_text) == "[后台任务] 当前无后台任务"
    text = shell.execute({"bg": "cancel", "id": 9}, env)
    assert visible(text.llm_text) == "[错误: bg9 编号无效（有效范围 bg1~bg8）]"

    manager.submit("echo x")
    waited = shell.execute({"bg": "wait", "timeout": 10}, env)
    assert visible(waited.llm_text).startswith("[后台任务] bg1 完成 exit=0")


# ═══════════════════════════════════════════════════════════════
# 12. 结果文件生命周期与会话隔离
# ═══════════════════════════════════════════════════════════════


def test_result_dirs_isolated_per_instance(runtime, workdir):
    """目录隔离：不同实例的结果落在各自 narnat_bg_* 目录（真多进程隔离未验证）。"""
    first, second = BackgroundManager(runtime), BackgroundManager(runtime)
    try:
        first_log = first.submit("echo one").split("\n")[1].removeprefix("结果: ")
        second_log = second.submit("echo two").split("\n")[1].removeprefix("结果: ")
        assert Path(first_log).parent != Path(second_log).parent
        assert Path(first_log).parent.name.startswith("narnat_bg_")
        assert Path(first_log).is_absolute() and Path(second_log).is_absolute()
    finally:
        first.cleanup_all()
        second.cleanup_all()


def test_prepare_wipes_without_killing(manager, workdir):
    """预清不杀进程：清空残留结果目录，但不终止任何进程。"""
    done_log = Path(manager.submit("echo x").split("\n")[1].removeprefix("结果: "))
    assert manager.wait(10).startswith("[后台任务] bg1 完成 exit=0")
    manager.submit(SLEEPER)
    assert manager.running_count() == 1

    manager.prepare()
    assert not done_log.exists()          # 上次残留结果被清空
    assert manager.running_count() == 1   # 进程未被终止（运行中任务的日志句柄仍被占用）
    manager.cleanup_all()


def test_cleanup_all_idempotent(manager, workdir):
    """结束硬清理幂等：终止运行中任务、清空目录、重置槽位表，重复调用不报错。"""
    manager.submit(SLEEPER)
    manager.cleanup_all()
    assert manager.running_count() == 0
    assert manager.snapshot() == "[后台任务] 当前无后台任务"
    manager.cleanup_all()  # 幂等
    assert manager.snapshot() == "[后台任务] 当前无后台任务"


def test_sweep_stale_guardrails(tmp_path, monkeypatch):
    """过期清扫：保活期内（目录或目录内文件）保留；过期且无句柄才回收。"""
    import os
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = time.time() - background_module.BG_STALE_SECONDS - 3600

    fresh_dir = tmp_path / "narnat_bg_fresh"
    fresh_dir.mkdir()
    (fresh_dir / "bg1.log").write_text("x", encoding="utf-8")

    old_dir = tmp_path / "narnat_bg_old"
    old_dir.mkdir()
    old_file = old_dir / "bg1.log"
    old_file.write_text("x", encoding="utf-8")
    os.utime(old_file, (stale, stale))
    os.utime(old_dir, (stale, stale))

    mixed_dir = tmp_path / "narnat_bg_mixed"
    mixed_dir.mkdir()
    (mixed_dir / "bg1.log").write_text("x", encoding="utf-8")  # 新文件
    os.utime(mixed_dir, (stale, stale))                        # 旧目录 + 新文件 → 保留

    handled_dir = tmp_path / "narnat_bg_handled"
    handled_dir.mkdir()
    handle_file = handled_dir / "bg1.log"
    handle_file.write_text("x", encoding="utf-8")
    os.utime(handle_file, (stale, stale))
    os.utime(handled_dir, (stale, stale))

    other = tmp_path / "not_bg_dir"
    other.mkdir()
    os.utime(other, (stale, stale))

    with open(handle_file, "rb"):
        background_module._sweep_stale()

    assert fresh_dir.exists()
    assert mixed_dir.exists()
    assert not old_dir.exists()      # 过期且无活跃句柄 → 回收
    assert other.exists()            # 非本前缀目录不动
    if IS_WINDOWS:
        assert handled_dir.exists()  # 存在活跃文件句柄 → 改名失败 → 放弃删除


def test_main_file_cap_and_tail_file(manager, workdir, monkeypatch):
    """超大输出截断：主文件封顶、1MB 尾部缓冲收口落盘 .tail、快照标注。"""
    monkeypatch.setattr(background_module, "MAX_FILE_BYTES", 4096)
    monkeypatch.setattr(background_module, "TAIL_BYTES", 512)
    payload = ('python -c "import sys;'
               "sys.stdout.write('A'*3000+'B'*3000+'C'*3000)\"")
    text = manager.submit(payload)
    log_path = Path(text.split("\n")[1].removeprefix("结果: "))
    assert manager.wait(15).startswith("[后台任务] bg1 完成 exit=0")

    header_len = len(log_path.read_bytes().split(b"\n", 1)[0]) + 1
    size = log_path.stat().st_size
    assert 4096 <= size <= 4096 + header_len                     # 输出部分封顶 4096
    tail = Path(str(log_path) + ".tail")
    assert tail.exists()                                        # 尾部缓冲收口落盘
    assert b"C" in tail.read_bytes()                            # 保留的是最新尾部
    assert "（超限截断，尾部见" in manager.snapshot()
    assert f"输出{9000 / 1024:.1f}KB" in manager.snapshot()


def test_running_summary_format(manager, workdir):
    """运行摘要格式：bgN(命令前40字符) 以 、 连接；无运行中任务时为空串。"""
    assert manager.running_summary() == ""
    manager.submit(SLEEPER)
    manager.submit(SLEEPER)
    assert manager.running_summary() == (
        'bg1(python -c "import time;time.sleep(8)")、'
        'bg2(python -c "import time;time.sleep(8)")'
    )
    manager.cleanup_all()
    assert manager.running_summary() == ""


# ═══════════════════════════════════════════════════════════════
# 13. 退出码与错误标签协议（Shell 侧）
# ═══════════════════════════════════════════════════════════════


def test_labels_random_but_stable_in_process(shell, env, workdir):
    """标签随机且进程内稳定：多次生成的退出码行标签一致，命令无法预知。"""
    assert re.fullmatch(r"[0-9a-f]{8}", signal.TAG)
    result = shell.execute({"command": "echo x"}, env)
    assert f"[{signal.TAG}]" in result.llm_text
    assert result.llm_text.count(f"[{signal.TAG}]") == 1


def test_llm_visible_text_has_no_tags(shell, env, workdir):
    """LLM 可见文本等价：剥离标签后与不带标签时一致。"""
    registry = ToolRegistry()
    registry.register(shell)
    result = registry.execute("Shell", {"command": "echo hello"}, env)
    assert re.search(r"\[[0-9a-f]{8}\]", result.llm_text) is None
    assert result.llm_text == f"[exit code: 0]\nhello\n{prompt()}"


def test_fake_output_does_not_trigger_error(shell, env, workdir):
    """伪造不生效：命令自身打印同款文字不触发失败判定与退出码解析。"""
    registry = ToolRegistry()
    registry.register(shell)
    result = registry.execute(
        "Shell", {"command": 'echo [错误: 假消息] [exit code: 0]'}, env
    )
    assert result.is_error is False
    assert signal.has_error(result.llm_text) is False


def test_registry_judges_error_before_stripping(shell, env, workdir):
    """判定顺序：先以错误标签判定失败，再剥离标签后交 LLM。

    命令自身失败（无框架标签）不置失败标记；框架失败（超时/错误行）置位。
    """
    registry = ToolRegistry()
    registry.register(shell)
    result = registry.execute("Shell", {"command": "narnat_no_such_cmd_zzz"}, env)
    assert result.is_error is False  # 退出码非 0 属命令输出，不是框架失败
    assert visible(result.llm_text) == result.llm_text  # 已剥离

    timeout = registry.execute("Shell", {"command": SLEEPER, "timeout": 1}, env)
    assert timeout.is_error is True
    assert visible(timeout.llm_text).startswith("[超时: 命令执行超过1秒，已终止]")


def test_overall_rc_takes_last_line(shell, env, workdir):
    """整体退出码取最后：多段结果含多个退出码行时取最后一个。"""
    result = shell.execute(
        {"command": 'python -c "import sys;sys.exit(3)" || echo rescue'}, env
    )
    assert signal.parse_rc(result.llm_text) == 0
    assert visible(result.llm_text).count("[exit code:") == 2


def test_registry_global_truncation_keeps_no_partial_tag(shell, env, workdir):
    """兼容怪癖⑪：全局输出上限截断按字符硬截断，不残留残缺标签。"""
    registry = ToolRegistry()
    registry.register(shell)
    env.settings.max_tool_output_chars = 200
    result = registry.execute(
        "Shell", {"command": 'python -c "print(\'A\'*2000)"', "max_output_chars": 4000}, env
    )
    text = result.llm_text
    assert "[全局截断: 输出共" in text
    assert text.startswith("[exit code: 0]\n" + "A" * (133 - len("[exit code: 0]\n")))
    assert _has_partial_tag(text) is False


def test_residual_interrupt_cleared_on_entry(shell, env, workdir, runtime):
    """兼容怪癖⑤：中断标志跨调用残留时下一次执行入口清零，不误伤新命令。"""
    runtime.interrupt()  # 无活动进程时置位（模拟残留）
    result = shell.execute({"command": "echo ok"}, env)
    text = visible(result.llm_text)
    assert "ok" in text
    assert "[用户中断]" not in text


def test_unix_single_segment_skips_py_direct(monkeypatch, shell, env, workdir):
    """兼容怪癖⑥：Unix 下单段命令不做 python 直执行（仅多段中的 python 段直执行）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(executor, "find_executable", lambda *names: "narnat_no_such_shell")
    calls: list = []
    original = executor._try_extract_py_code

    def _spy(seg):
        calls.append(seg)
        return original(seg)

    monkeypatch.setattr(executor, "_try_extract_py_code", _spy)
    result = shell.execute({"command": 'python -c "print(1)"'}, env)
    assert calls == []
    assert visible(result.llm_text).startswith("[错误: Shell未找到: ")

    calls.clear()
    shell.execute({"command": 'python -c "print(1)" && echo ok'}, env)
    assert calls == ['python -c "print(1)"', "echo ok"]


def test_single_segment_shell_argv_vs_segments_shell_true():
    """兼容怪癖⑦（结构断言）：单段 Unix 走 `[shell, "-c", command]`，多段走 `shell=True`。

    真双平台执行差异未验证（本机 Windows）。
    """
    source = (SHELL_DIR / "executor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    executor_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShellExecutor"
    )
    run_method = next(
        node for node in executor_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    segments_method = next(
        node for node in executor_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_segments"
    )

    def _popen_calls(method):
        return [
            node for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ]

    single_calls = _popen_calls(run_method)
    assert any(
        isinstance(call.args[0], ast.List) and len(call.args[0].elts) == 3
        for call in single_calls
    ), "单段 Unix 路径应以 argv 列表启动 shell"

    segment_calls = _popen_calls(segments_method)
    assert segment_calls, "多段路径应起子进程"
    assert all(
        any(kw.arg == "shell" and kw.value.value is True for kw in call.keywords)
        for call in segment_calls
    ), "多段路径应经 shell=True（Unix 下即 /bin/sh，与单段 bash 不一致）"


# ═══════════════════════════════════════════════════════════════
# 14. 结构与 Scenario 映射表
# ═══════════════════════════════════════════════════════════════


def test_shell_tool_conforms_to_tool_protocol(shell):
    """`Tool` 协议实现：name / definition() / execute(args, env)。"""
    from narnat_agent.contracts.tool import Tool

    assert isinstance(shell, Tool)
    assert shell.name == "Shell"
    assert shell.definition()["function"]["name"] == "Shell"


class _SignalStub:
    """最小中断信号替身（`InterruptSignal` 形态）：记录订阅回调并支持手动广播。"""

    def __init__(self) -> None:
        self.handlers: list = []
        self.is_set = False

    def clear(self) -> None:
        self.is_set = False

    def raise_(self) -> None:
        self.is_set = True
        for handler in self.handlers:
            handler()

    def subscribe(self, handler) -> None:
        self.handlers.append(handler)

    def enter_input_mode(self) -> None:
        self.clear()

    def enter_run_mode(self) -> None:
        self.clear()


def test_interrupt_signal_subscription_kills_foreground(manager, env, workdir):
    """中断接线：构造注入中断信号后，ESC 广播直接终止前台进程树（结果为中断格式）。"""
    signal_stub = _SignalStub()
    tool = ShellTool(background=manager, signal=signal_stub)
    assert signal_stub.handlers == [tool.runtime.interrupt]

    box: dict = {}

    def _run():
        box["result"] = tool.execute({"command": SLEEPER, "timeout": 30}, env)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.5)
    signal_stub.raise_()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert visible(box["result"].llm_text) == f"[用户中断]\n{prompt()}"


def test_shell_modules_public_surface():
    """积木内结构：共享原语为公开接口（无跨模块私有 import），对外导出齐备。"""
    import narnat_agent.tools.shell as shell_package

    assert shell_package.__all__ == ["BackgroundManager", "ShellRuntime", "ShellTool"]
    for name in shell_package.__all__:
        assert hasattr(shell_package, name)

    for path in sorted(SHELL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                for alias in node.names:
                    assert not alias.name.startswith("_"), (
                        f"{path.name}: 跨模块引用了私有名 {alias.name}")


SCENARIO_MAP = {
    "平台标签渲染": "test_definition_platform_label",
    "无必填参数": "test_bg_status_without_command",
    "字符串数字容错": "test_numeric_string_tolerance",
    "别名覆盖": "test_max_output_tokens_alias_overrides",
    "非法数值报错文案": "test_invalid_numeric_error_text",
    "空命令文案": "test_empty_command_text",
    "非正超时文案": "test_non_positive_timeout_text",
    "超时上限钳制": "test_timeout_clamped_by_settings",
    "rm 免确认开关": "test_rm_skip_confirm_executes_delete",
    "git 命中确认": "test_git_command_hits_confirm",
    "Windows 用户拒绝": "test_windows_reject_cancels_without_execution",
    "无确认回调时不拦截": "test_missing_confirm_callback_not_intercepted",
    "后台提交同样先过确认": "test_background_submit_goes_through_confirm",
    "首次调用挂起": "test_non_windows_first_call_pends",
    "确认后重执行": "test_non_windows_confirmed_call_executes",
    "拒绝回传文案": "test_non_windows_pending_arguments_shape",
    "平台自适应": "test_platform_adaptive_cmd_env_expansion",
    "超时终止": "test_timeout_terminates_and_reports",
    "ESC 中断": "test_escape_interrupt_result",
    "输出解码回退": "test_decode_output_gbk_fallback",
    "子进程输出编码": "test_child_utf8_output_for_emoji",
    "启动失败文案": "test_startup_failure_text",
    "正常结果": "test_normal_result_shape",
    "空输出段省略": "test_empty_stdout_segment_omitted",
    "中断结果": "test_escape_interrupt_result",
    "超时结果": "test_timeout_result_shape",
    "单条 cd 持久化": "test_cd_persists_for_later_tools",
    "无参数 cd 平台差异": "test_noarg_cd_platform_difference",
    "切换失败文案": "test_cd_failure_text",
    "简写与展开": "test_cd_shortcut_and_expansion",
    "复合运算符不按纯 cd 处理": "test_compound_cd_goes_to_segments",
    "&& 短路跳过": "test_segment_and_short_circuit",
    "|| 短路跳过": "test_segment_or_short_circuit",
    "段输出与总退出码": "test_segment_outputs_and_total_rc",
    "预算递减与耗尽": "test_segment_budget_exhaustion",
    "段超时格式": "test_segment_timeout_format_python",
    "多段中断": "test_segments_interrupt",
    "多段中的 cd 段": "test_segment_cd_applies",
    "Windows 单段直执行": "test_py_direct_windows_payload",
    "多段中的 python 段": "test_py_segment_in_multi_command",
    "不支持形态回退": "test_py_unsupported_forms_fall_back",
    ">nul 丢弃 stdout": "test_py_suffix_nul_discards_stdout",
    "重定向写文件": "test_py_suffix_redirect_writes_file",
    "管道后缀": "test_py_suffix_pipe",
    "中断不写文件": "test_py_suffix_interrupt_keeps_file_absent",
    "首尾保留与提示文案": "test_truncate_head_tail_and_notice",
    "未超限原样返回": "test_truncate_within_limit_untouched",
    "标签不被切开": "test_truncate_never_splits_tags",
    "提交返回值与句柄": "test_submit_returns_handle_and_header",
    "空命令提交报错": "test_submit_without_command",
    "并发上限": "test_submit_concurrency_limit",
    "终态释放与编号复用": "test_slot_reuse_after_terminal",
    "复用归档": "test_slot_reuse_archives_old_result",
    "日志头与增量落盘": "test_log_header_and_incremental_flush",
    "编码统一": "test_log_encoding_unified_utf8",
    "启动失败释放槽位": "test_background_startup_failure_releases_slot",
    "快照格式": "test_snapshot_format",
    "无任务快照": "test_snapshot_without_tasks",
    "完成事件唤醒等待": "test_wait_wakes_on_completion",
    "无运行任务立即返回": "test_wait_without_running_tasks",
    "等待超时": "test_wait_timeout_text",
    "打断等待不杀任务": "test_wait_interrupt_keeps_tasks_running",
    "等待参数校验": "test_wait_parameter_validation",
    "正常取消": "test_cancel_running_task",
    "id 缺失与非整数": "test_cancel_id_missing_or_non_integer",
    "编号越界与空槽": "test_cancel_out_of_range_and_empty_slot",
    "终态不可取消": "test_cancel_terminal_slot",
    "未知操作": "test_cancel_unknown_op",
    "目录隔离": "test_result_dirs_isolated_per_instance",
    "预清不杀进程": "test_prepare_wipes_without_killing",
    "结束硬清理幂等": "test_cleanup_all_idempotent",
    "过期清扫": "test_sweep_stale_guardrails",
    "超大输出截断": "test_main_file_cap_and_tail_file",
    "运行摘要格式": "test_running_summary_format",
    "标签随机且进程内稳定": "test_labels_random_but_stable_in_process",
    "LLM 可见文本等价": "test_llm_visible_text_has_no_tags",
    "伪造不生效": "test_fake_output_does_not_trigger_error",
    "判定顺序": "test_registry_judges_error_before_stripping",
    "整体退出码取最后": "test_overall_rc_takes_last_line",
    "输出上限 0 不早退（兼容怪癖）": "test_zero_max_output_chars_runs_command",
    "预算耗尽即超时（兼容怪癖）": "test_segment_budget_exhaustion",
    "中断丢弃段输出（兼容怪癖）": "test_segments_interrupt_drops_segment_output",
    "取消唤醒报超时（兼容怪癖）": "test_cancel_wakes_wait_with_timeout_text",
    "中断标志入口清零（兼容怪癖）": "test_residual_interrupt_cleared_on_entry",
    "Unix 单段不直执行（兼容怪癖）": "test_unix_single_segment_skips_py_direct",
    "多段 sh 与单段 bash 的不一致（兼容怪癖）":
        "test_single_segment_shell_argv_vs_segments_shell_true",
    "多段无参数 cd 不切换（兼容怪癖）": "test_segment_noarg_cd_does_not_switch",
    "全局截断不吸附标签（兼容怪癖）": "test_registry_global_truncation_keeps_no_partial_tag",
}

EXTRA_TESTS = {
    "test_git_skip_confirm_executes",
    "test_stderr_only_from_shell",
    "test_format_prompt_platforms",
    "test_cd_extract_helpers",
    "test_split_commands_rules",
    "test_segment_timeout_format_shell",
    "test_segment_start_failure_text",
    "test_scan_and_parse_suffix_helpers",
    "test_py_direct_hit_conditions",
    "test_py_suffix_redirect_to_spaced_path",
    "test_py_suffix_write_failure",
    "test_py_suffix_timeout_keeps_file_absent",
    "test_end_to_end_truncation",
    "test_submit_via_shell_tool",
    "test_background_unix_missing_shell",
    "test_unwritable_log_file",
    "test_wait_multiple_events",
    "test_bg_ops_via_shell_tool",
    "test_cmd_missing_text",
    "test_unix_missing_shell_text",
    "test_decode_output_replace_fallback",
    "test_shell_tool_conforms_to_tool_protocol",
    "test_interrupt_signal_subscription_kills_foreground",
    "test_shell_modules_public_surface",
    "test_spec_scenario_mapping_is_complete",
}


def test_spec_scenario_mapping_is_complete():
    """映射表齐备：spec 的全部 Scenario 都在映射表中，且映射的测试函数真实存在。"""
    if not SPEC_PATH.exists():
        pytest.skip(f"spec 文件不可得: {SPEC_PATH}")
    scenarios = re.findall(
        r"^#### Scenario: (.+)$", SPEC_PATH.read_text(encoding="utf-8"), re.M
    )
    assert len(scenarios) == 88
    missing = [name for name in scenarios if name not in SCENARIO_MAP]
    assert missing == [], f"未映射的 Scenario: {missing}"

    module = sys.modules[__name__]
    for scenario, test_name in SCENARIO_MAP.items():
        assert hasattr(module, test_name), f"{scenario} 映射的测试不存在: {test_name}"
    for test_name in EXTRA_TESTS:
        assert hasattr(module, test_name), f"补充测试不存在: {test_name}"


def test_no_module_level_mutable_state():
    """模块级可变状态：三个实现模块顶层无小写赋值（常量须全大写）。"""
    for path in sorted(SHELL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name.startswith("__"), (
                    f"{path.name}: 模块级可变名 {name}")
