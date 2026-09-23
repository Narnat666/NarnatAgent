"""ui 交互层单元测试 —— spec Scenario 映射 + 旧实现基准对照。

行为金标准：`openspec/changes/recast-v2/specs/ui/spec.md` 的非渲染面（交互层：
输入会话 / 命令集与分发 / Tab 补全 / 流式输出会话句柄 / 动画 / 统计栏 / headless /
打断提示）；渲染面归 T3.9 `test_render.py`。

映射表（Requirement / Scenario → 测试）：

| Requirement | Scenario | 测试 |
|---|---|---|
| 输入会话 | 提示符与提交 | `TestInputSession::test_prompt_symbol_and_submit` |
| | 换行键 | `TestInputSession::test_newline_keybindings` |
| | 历史文件落点 | `TestInputSession::test_history_path` |
| | Ctrl+C 返回空输入 | `TestInputSession::test_ctrl_c_returns_empty_and_rebuilds` |
| | 自定义提示输入 | `TestInputSession::test_custom_prompt_uses_primary_color` |
| | 启动横幅 | `TestBanner::test_banner_text_and_separator` |
| 命令集与分发语义 | 状态决定命令集 | `TestCommandRouter::test_state_decides_available_set` |
| | 未识别命令落到模型 | `TestCommandRouter::test_unknown_returns_unknown_without_output` / `test_unknown_reply_text_not_printed` / `test_non_command_text_is_unknown` |
| | 命令名归一 | `TestCommandRouter::test_command_name_normalized` |
| | /clear 恒可用 | `TestCommandRouter::test_clear_always_available` / `test_clear_without_source` |
| | 缺参提示（命令处理器归 sessions，T4.2） | `TestCommandRouter::test_missing_args_handler_business`（结果文本打印路径） |
| | /exit 退出语义（命令处理器归 sessions，T4.2） | `TestCommandRouter::test_exit_semantics_passthrough` |
| | /goal 开启与轮数校验（处理器归 sessions，T4.2） | `TestCommandRouter::test_goal_bypasses_availability` |
| | /compact 取消着色（着色在命令处理器，T4.2） | 见边界说明 |
| Tab 补全 | 命令名前缀补全 | `TestCompleter::test_command_prefix` |
| | 补全随会话状态变化 | `TestCompleter::test_state_changes_candidates` / `TestCommandSourceIntegration::test_root_state_candidates_after_save` |
| | /cd 名字补全 | `TestCompleter::test_cd_names` |
| | 层级候选只补最后一段 | `TestCompleter::test_nested_prefix_only_last_segment` |
| | /skill 目录与叶子 | `TestCompleter::test_skill_dir_and_leaf` |
| | /skill 叶子完整输入 | `TestCompleter::test_skill_leaf_complete_no_candidate` |
| | 静态选项 | `TestCompleter::test_static_option` |
| | 非斜杠输入不补全 | `TestCompleter::test_non_slash_no_candidates` |
| 流式输出会话句柄协议 | 正常生命周期 | `TestUiSink::test_finish_order_and_stats` |
| | 首块内容停止动画 | `TestUiSink::test_feed_stops_animation` |
| | 落定守卫 | `TestUiSink::test_flush_guard` |
| | 并行工具暂停计数 | `TestUiSink::test_pause_resume_counting` |
| | 中止后不再启动动画 | `TestUiSink::test_resume_after_abort_no_restart` |
| | 收尾不显示统计 | `TestUiSink::test_finish_without_stats` |
| | 自定义中止消息 | `TestUiSink::test_abort_custom_message` |
| 动画 | 思考中延迟启动 | `TestAnimation::test_delay_never_starts_on_stop` |
| | 光标隐藏与恢复 | `TestAnimation::test_cursor_hidden_and_restored` |
| | 掉帧容错 | `TestAnimation::test_dropped_frame_when_locked` |
| | 压缩动画幂等结束 | `TestUiAnimator::test_end_without_begin_idempotent` |
| 统计栏 | 完整统计行 | `TestStatsBar::test_spec_template_verbatim` |
| | 缓存为零不显示 | `TestStatsBar::test_cache_zero_hidden` |
| | 窗口占比回退 | `TestStatsBar::test_ratio_fallback` |
| | 余额为零不显示 | `TestStatsBar::test_balance_non_positive_hidden` |
| | 小数值原样 | `TestStatsBar::test_small_values_raw` |
| headless 纯文本输出 | 纯文本结构保留 | `TestHeadlessSink::test_plain_structure_preserved` |
| | 默认静默调度日志 | `TestHeadlessConsole::test_prepare_console` |
| | 结束哨兵（归 app 运行循环，T4.3） | 见边界说明 |
| | headless 收尾无统计 | `TestHeadlessSink::test_finish_no_stats` |
| | headless 不可打断 | `TestHeadlessSink::test_cancelled_always_false` |
| 打断提示与输入态恢复 | 默认打断提示 | `TestUiSink::test_abort_default_prompt` |
| | 异常提示替代打断提示 | `TestUiSink::test_abort_custom_message` |
| | 输入会话重建 | `TestUiInteraction::test_notify_interrupted_rebuilds_session` |
| 兼容性怪癖保持 | 目标模式前缀解析（处理器归 sessions，T4.2） | 见边界说明 |
| | 斜杠剥除（兼容怪癖） | `TestCommandRouter::test_leading_slashes_stripped` |
| | 字符串布尔开关（兼容怪癖） | `TestStatsBar::test_string_false_switch_truthy` |
| | 文案着色（处理器归 sessions，T4.2） | 见边界说明 |

边界说明（本任务不含 / 无法自动化）：
- 命令处理器（文案、参数校验、会话操作）与可用命令表归 `sessions` 积木（T4.2）：
  「缺参提示」「/exit 退出语义」「/goal 轮数校验」「/compact 取消着色」「文案着色」
  等场景由 sessions 的命令测试面覆盖；本任务侧锁定 ui 侧分发骨架（命令名归一 /
  门禁 / `/clear` 清屏 / 结果文本打印 / 三态）与补全器，并以
  `TestCommandSourceIntegration` 做真实 `SessionManager` 的跨积木接线核验
  （ui 不 import sessions，只经端口消费）。
- headless 结束哨兵 `[NN_DONE] reason={原因} rounds={轮数}` 由 app 的 headless 运行
  循环输出（T4.3）；本任务覆盖 headless 纯文本输出面与输出环境置位。
- **未验证**（真终端交互，无法自动化）：prompt_toolkit 真实按键序列（Enter 提交、
  Alt+Enter / Ctrl+O / Alt+J 换行）、补全菜单渲染与 Tab 触发、真实终端下的 Ctrl+C /
  EOF、动画帧在真实终端的原地刷新观感、ESC 打断的端到端时序。

基准对照：`v2/tests/baseline/ui/ui_interaction.json`（旧实现 ui 交互面用例，由
`v2/tests/baseline/extract_old_ui_interaction.py` 生成；判据 = 输出字节等价）。
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document

from narnat_agent.contracts import Animator, InteractionPort, OutputSink, TurnStats
from narnat_agent.output import Console, DisplayState, Theme
from narnat_agent.ui import (
    CLEAR_SCREEN,
    PROMPT_SYMBOL,
    SPINNER_DELAY,
    Animation,
    CommandCompleter,
    CommandResult,
    CommandRouter,
    CommandSource,
    HeadlessAnimator,
    HeadlessInteraction,
    HeadlessSink,
    InputSession,
    UiAnimator,
    UiInteraction,
    UiSink,
    frame_texts,
    history_path,
    make_keybindings,
    prepare_console,
    stats_line,
    write_banner,
)

BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline"
BASELINE_FILE = BASELINE_DIR / "ui" / "ui_interaction.json"

STATS_GROUP = "ui_design.show_stats"
BANNER_GROUP = "ui_design.show_header"
INTERRUPT_GROUP = "ui_design.show_interrupted"
COMPLETION_GROUP = "session_commands._CommandCompleter.get_completions"

TERM_WIDTH = 100

FREE_COMMANDS = {
    "/clear": "清屏",
    "/compact": "压缩上下文",
    "/save": "保存会话",
    "/ls": "列出已保存会话",
    "/cd": "进入会话",
    "/rm": "删除会话",
    "/skill": "加载技能",
    "/thinking": "思考强度",
    "/thinkback": "思考回传",
    "/mode": "切换模型",
    "/goal": "目标模式",
    "/exit": "退出程序",
}
ROOT_COMMANDS = {**FREE_COMMANDS, "/explore": "探索分支",
                 "/rm": "删除子会话", "/exit": "退出会话"}


# ═══════════════════════════════════════════════════════════════
# 测试替身与夹具
# ═══════════════════════════════════════════════════════════════

class FakeInterrupt:
    """中断信号替身（记录模式切换次数与事件顺序；`is_set` 可外部置位）。"""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.is_set = False
        self.input_mode_calls = 0
        self.run_mode_calls = 0

    def clear(self) -> None:
        self.is_set = False

    def raise_(self) -> None:
        self.is_set = True

    def subscribe(self, handler) -> None:
        pass

    def enter_input_mode(self) -> None:
        self.input_mode_calls += 1
        self.is_set = False
        self.events.append("enter_input_mode")

    def enter_run_mode(self) -> None:
        self.run_mode_calls += 1
        self.is_set = False
        self.events.append("enter_run_mode")


class FakeAnimation:
    """思考中动画替身（记录 start / stop 次数与事件顺序）。"""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.starts = 0
        self.stops = 0
        self.running = False

    def start(self) -> None:
        self.starts += 1
        self.running = True
        self.events.append("start_animation")

    def stop(self) -> None:
        self.stops += 1
        self.running = False
        self.events.append("stop_animation")


class FakeRenderer:
    """渲染器替身（记录 feed / flush / reset 调用序列）。"""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.calls = []

    def feed(self, text: str) -> None:
        self.calls.append("feed")
        self.events.append("feed")

    def flush(self) -> None:
        self.calls.append("flush")
        self.events.append("flush")

    def reset(self) -> None:
        self.calls.append("reset")
        self.events.append("reset")


class StubReply:
    """命令源返回结果替身（与 `sessions.commands.CommandReply` 同形状）。"""

    def __init__(self, kind: str = "handled", text: str = ""):
        self.kind = kind
        self.text = text


class StubSource:
    """命令源替身（可用命令表 + 名称清单 + 技能树 + 分发记录）。"""

    def __init__(self, commands=None, *, names=(), rm_names=(), thinking=(),
                 models=(), skill_tree=None, kind: str = "handled", text: str = ""):
        self.commands = dict(commands if commands is not None else ROOT_COMMANDS)
        self.names = list(names)
        self.rm_names = list(rm_names)
        self.thinking = list(thinking)
        self.models = list(models)
        self.skill_tree = list(skill_tree or [])
        self.kind = kind
        self.text = text
        self.dispatched = []

    def available_commands(self):
        return dict(self.commands)

    def dispatch(self, text: str) -> StubReply:
        self.dispatched.append(text)
        return StubReply(self.kind, self.text)

    def list_session_names(self):
        return list(self.names)

    def list_rm_names(self):
        return list(self.rm_names)

    def list_thinking_options(self):
        return list(self.thinking)

    def list_model_options(self):
        return list(self.models)

    def list_skill_tree(self):
        return json.loads(json.dumps(self.skill_tree))


@pytest.fixture()
def fixed_width(monkeypatch):
    """固定终端宽度（分隔线宽度确定化）。"""
    monkeypatch.setenv("NARNAT_TERM_WIDTH", str(TERM_WIDTH))
    return TERM_WIDTH


def make_env(*, plain: bool = False):
    """构造隔离的（缓冲, 控制台, 主题, 显示开关）。"""
    buf = io.StringIO()
    console = Console(buf, truecolor=True, platform="linux")
    console.set_plain(plain)
    theme = Theme(console)
    display = DisplayState(
        show_cost=True,
        show_balance=True,
        max_tokens=128000,
        show_ratio=True,
        context_window=1000000,
    )
    return buf, console, theme, display


def make_sink(*, plain: bool = False, events=None, renderer=None, spinner=None,
              interrupt=None, display=None):
    """构造注入了替身的 UiSink（无线程、无真实渲染器）。"""
    events = events if events is not None else []
    buf, console, theme, default_display = make_env(plain=plain)
    interrupt = interrupt if interrupt is not None else FakeInterrupt(events)
    renderer = renderer if renderer is not None else FakeRenderer(events)
    spinner = spinner if spinner is not None else FakeAnimation(events)
    sink = UiSink(
        theme,
        console,
        interrupt,
        display if display is not None else default_display,
        renderer=renderer,
        spinner=spinner,
    )
    return buf, sink, theme, interrupt, renderer, spinner


def texts(completions) -> list:
    """补全结果的规范化序列（与基准同口径）。"""
    return [
        {"text": c.text, "start_position": c.start_position, "meta": c.display_meta_text}
        for c in completions
    ]


def complete(source: StubSource, text: str) -> list:
    """驱动补全器并返回规范化结果（光标在文末）。"""
    completer = CommandCompleter(source)
    return texts(completer.get_completions(Document(text, len(text)), None))


def wait_for(buf: io.StringIO, needle: str, timeout: float = 3.0) -> bool:
    """轮询等待缓冲出现指定文本（动画线程测试用，避免固定 sleep）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in buf.getvalue():
            return True
        time.sleep(0.01)
    return needle in buf.getvalue()


# ═══════════════════════════════════════════════════════════════
# 流式输出会话句柄协议（UiSink）
# ═══════════════════════════════════════════════════════════════

class TestUiSink:
    """`UiSink` 的操作语义（spec「流式输出会话句柄协议」）。"""

    def test_satisfies_output_sink_port(self):
        """句柄满足 contracts.output.OutputSink 结构协议。"""
        _, sink, *_ = make_sink()
        assert isinstance(sink, OutputSink)

    def test_finish_order_and_stats(self, fixed_width):
        """正常生命周期：切回输入模式 → 停动画 → 落定缓冲 → 显示统计栏。"""
        events = []
        buf, sink, _, interrupt, _, spinner = make_sink(events=events)
        sink.feed("第一行\n")
        sink.finish(TurnStats(input_tokens=12345, output_tokens=678), with_stats=True)
        assert interrupt.input_mode_calls == 1
        assert events == ["stop_animation", "feed", "enter_input_mode",
                          "stop_animation", "flush"]
        assert spinner.stops == 2
        out = buf.getvalue()
        assert "输入:12.3k 输出:678" in out
        assert "─" * (fixed_width - 2) in out

    def test_feed_stops_animation(self):
        """首块内容注入即停止思考动画。"""
        events = []
        _, sink, _, _, renderer, spinner = make_sink(events=events)
        sink.begin()
        assert events == ["start_animation"]
        sink.feed("块")
        assert events == ["start_animation", "stop_animation", "feed"]
        assert spinner.stops == 1

    def test_flush_guard(self):
        """落定守卫：从未注入内容时不转发落定；注入后转发。"""
        _, sink, _, _, renderer, _ = make_sink()
        sink.flush()
        assert renderer.calls == []
        sink.feed("内容")
        sink.flush()
        assert renderer.calls == ["feed", "flush"]

    def test_pause_resume_counting(self):
        """并行工具暂停计数：首个暂停即停止动画，归零才重启，计数不为负。"""
        _, sink, _, _, _, spinner = make_sink()
        sink.begin()
        assert spinner.starts == 1
        sink.pause()
        assert spinner.stops == 1
        sink.pause()                      # 第二个并行工具：不重复停止
        assert spinner.stops == 1
        sink.resume()                     # 计数 1：仍未归零，不重启
        assert spinner.starts == 1
        sink.resume()                     # 计数 0：重启
        assert spinner.starts == 2
        sink.resume()                     # 计数不为负：仍按归零处理并可重启
        assert spinner.starts == 3

    def test_resume_after_abort_no_restart(self):
        """句柄已中止后恢复不再启动动画。"""
        _, sink, _, _, _, spinner = make_sink()
        sink.begin()
        sink.pause()
        sink.abort()
        before = spinner.starts
        sink.resume()
        assert spinner.starts == before

    def test_finish_without_stats(self, fixed_width):
        """收尾不显示统计：只落定缓冲。"""
        buf, sink, _, _, renderer, _ = make_sink()
        sink.feed("内容")
        sink.finish(TurnStats(input_tokens=100), with_stats=False)
        assert renderer.calls == ["feed", "flush"]
        assert buf.getvalue() == ""

    def test_abort_default_prompt(self):
        """默认打断提示：空行 + `已打断` + `继续...`，并切回输入模式。"""
        buf, sink, _, interrupt, _, spinner = make_sink()
        sink.begin()
        sink.abort()
        assert sink.aborted is True
        assert interrupt.input_mode_calls == 1
        assert spinner.stops == 1
        out = buf.getvalue()
        assert "已打断" in out and "继续..." in out
        assert out.startswith("\r\n  ")

    def test_abort_custom_message(self):
        """自定义中止消息：原样输出，不显示默认打断提示。"""
        buf, sink, _, _, _, _ = make_sink()
        sink.abort("⚠ 程序异常，本轮回复已停止: 原因")
        assert buf.getvalue() == "\r⚠ 程序异常，本轮回复已停止: 原因\n"
        assert "已打断" not in buf.getvalue()

    def test_cancelled_follows_interrupt(self):
        """取消查询反映进程级中断状态。"""
        _, sink, _, interrupt, _, _ = make_sink()
        assert sink.cancelled is False
        interrupt.raise_()
        assert sink.cancelled is True

    def test_restart_attempt_clears_buffer_and_guard(self):
        """重试重播：渲染缓冲清理、落定守卫复位（此前未完成内容作废）。"""
        _, sink, _, _, renderer, _ = make_sink()
        sink.feed("半行")
        sink.restart_attempt()
        assert renderer.calls == ["feed", "reset"]
        sink.flush()
        assert renderer.calls == ["feed", "reset"]   # 守卫复位后不再转发

    def test_notify_flushes_then_writes_notice(self, fixed_width):
        """状态提示：先落定缓冲，再按段落形态写出（不并入内容缓冲）。"""
        events = []
        buf, sink, _, _, renderer, spinner = make_sink(events=events, plain=True)
        sink.begin()
        sink.feed("内容")
        sink.notify("\n⚠ 服务端响应流中断，约2s后自动重试（第1/3次）…\n")
        assert events == ["start_animation", "stop_animation", "feed",
                          "stop_animation", "flush"]
        assert buf.getvalue() == "  ⚠ 服务端响应流中断，约2s后自动重试（第1/3次）…\n"


# ═══════════════════════════════════════════════════════════════
# 动画（Animation / UiAnimator）
# ═══════════════════════════════════════════════════════════════

class TestAnimation:
    """帧动画语义（spec「动画（思考中 / 正在压缩 / 正在合并）」）。"""

    def test_frame_texts(self):
        """帧序列：`* 标签` + 0/1/2/3 个点，粗体与暗色交替，行尾补位空格。"""
        _, _, theme, _ = make_env()
        frames = frame_texts(theme, "思考中")
        assert len(frames) == 4

        def plain(text: str) -> str:
            for color in (theme.b, theme.d, theme.ui_spinner, theme.rst):
                text = text.replace(str(color), "")
            return text

        assert [plain(f) for f in frames] == ["* 思考中   ", "* 思考中.  ",
                                              "* 思考中.. ", "* 思考中..."]
        assert str(theme.b) in frames[0] and str(theme.d) in frames[1]
        assert str(theme.b) in frames[2] and str(theme.d) in frames[3]

    def test_delay_never_starts_on_stop(self):
        """思考中延迟启动：延迟期内停止则动画从未显示（无视觉残留）。"""
        buf, console, theme, _ = make_env(plain=True)
        animation = Animation(console, theme, "思考中", delay=SPINNER_DELAY)
        assert SPINNER_DELAY == 0.666
        animation.start()
        time.sleep(0.05)
        animation.stop()
        assert buf.getvalue() == ""
        assert animation.running is False

    def test_cursor_hidden_and_restored(self):
        """启动隐藏光标；结束清行并恢复光标。"""
        buf, console, theme, _ = make_env(plain=True)
        animation = Animation(console, theme, "正在压缩")
        animation.start()
        assert wait_for(buf, "\x1b[?25l")
        animation.stop()
        out = buf.getvalue()
        assert out.startswith("\x1b[?25l")
        assert out.endswith("\r\x1b[K\x1b[?25h")
        assert animation.running is False

    def test_start_stop_idempotent(self):
        """重复 start / stop 幂等（无重复线程、无异常）。"""
        buf, console, theme, _ = make_env(plain=True)
        animation = Animation(console, theme, "思考中")
        animation.stop()
        assert buf.getvalue() == ""
        animation.start()
        animation.start()
        assert wait_for(buf, "\x1b[?25l")
        animation.stop()
        animation.stop()
        assert animation.running is False

    def test_dropped_frame_when_locked(self):
        """掉帧容错：输出锁被占用时丢帧且不等待，帧序号仍推进。"""
        buf, console, theme, _ = make_env(plain=True)
        animation = Animation(console, theme, "思考中")
        with console.lock:
            started = time.time()
            assert animation.write_frame(0) is False
            assert animation.write_frame(1) is False
            assert time.time() - started < 0.5
        assert buf.getvalue() == ""
        assert animation.write_frame(2) is True
        assert buf.getvalue() == "\r  * 思考中.. \x1b[K"


class TestUiAnimator:
    """压缩 / 合并动画的成对操作（`contracts.output.Animator` 实现）。"""

    def test_satisfies_animator_port(self):
        """满足 Animator 结构协议。"""
        buf, console, theme, _ = make_env(plain=True)
        assert isinstance(UiAnimator(console, theme), Animator)

    def test_end_without_begin_idempotent(self):
        """压缩动画幂等结束：未启动时结束无异常、无输出。"""
        buf, console, theme, _ = make_env(plain=True)
        animator = UiAnimator(console, theme)
        animator.end_compressing()
        animator.end_summarizing()
        assert buf.getvalue() == ""

    def test_begin_end_pair(self):
        """成对开始 / 结束：立即启动（无延迟）、退出时清行并恢复光标。"""
        buf, console, theme, _ = make_env(plain=True)
        animator = UiAnimator(console, theme)
        animator.begin_compressing()
        assert wait_for(buf, "\x1b[?25l")
        animator.end_compressing()
        assert buf.getvalue().endswith("\r\x1b[K\x1b[?25h")
        animator.begin_summarizing()
        assert wait_for(buf, "\x1b[?25l")
        animator.end_summarizing()
        assert buf.getvalue().count("\x1b[?25h") == 2


# ═══════════════════════════════════════════════════════════════
# 统计栏（stats_line 纯函数 + 显示路径）
# ═══════════════════════════════════════════════════════════════

STATS_EXPECTED_FULL = (
    "  输入:12.3k 输出:678  缓存:50.0%  思考:高  窗口占比:1%"
    "  最大输出:128k  费用:¥12.3456  余额:¥88.50"
)


class TestStatsBar:
    """统计栏格式化（spec「统计栏」；纯函数 + 开关实时读取）。"""

    def test_spec_template_verbatim(self, fixed_width):
        """完整统计行：与 spec 模板逐字一致（纯文本模式下取值，便于逐字对照）。"""
        buf, console, theme, display = make_env(plain=True)
        stats = TurnStats(input_tokens=12345, output_tokens=678, cache_ratio=0.5,
                          cost=12.3456, balance=88.5, thinking_effort="高")
        sink = UiSink(theme, console, FakeInterrupt(), display,
                      renderer=FakeRenderer(), spinner=FakeAnimation())
        sink.show_stats(stats)
        lines = buf.getvalue().split("\n")
        assert lines[1] == STATS_EXPECTED_FULL
        assert lines[0] == "  " + "─" * (fixed_width - 2)

    def test_cache_zero_hidden(self):
        """缓存为零：不显示缓存段。"""
        _, _, theme, display = make_env(plain=True)
        line = stats_line(theme, display, TurnStats(input_tokens=100, output_tokens=200))
        assert "缓存" not in line
        assert "输入:100 输出:200" in line

    def test_cache_capped_at_100_percent(self):
        """缓存比例超 100% 时按 100% 显示。"""
        _, _, theme, display = make_env(plain=True)
        line = stats_line(theme, display, TurnStats(cache_ratio=1.5))
        assert "缓存:100.0%" in line

    def test_ratio_fallback(self):
        """窗口占比回退：窗口无效或输入为 0 时显示 `--`。"""
        _, _, theme, display = make_env(plain=True)
        display.context_window = 0
        assert "窗口占比:--" in stats_line(theme, display, TurnStats(input_tokens=10))
        display.context_window = 1000000
        assert "窗口占比:--" in stats_line(theme, display, TurnStats(input_tokens=0))

    def test_ratio_hidden_when_switch_off(self):
        """窗口占比开关关闭时不显示该段。"""
        _, _, theme, display = make_env(plain=True)
        display.show_ratio = False
        assert "窗口占比" not in stats_line(theme, display, TurnStats(input_tokens=10))

    def test_balance_non_positive_hidden(self):
        """余额为 0 或负数：不显示余额段。"""
        _, _, theme, display = make_env(plain=True)
        assert "余额" not in stats_line(theme, display, TurnStats(balance=0.0))
        assert "余额" not in stats_line(theme, display, TurnStats(balance=-1.0))
        assert "余额:¥1.00" in stats_line(theme, display, TurnStats(balance=1.0))

    def test_cost_hidden_when_switch_off(self):
        """显示费用开关关闭时不显示费用段。"""
        _, _, theme, display = make_env(plain=True)
        display.show_cost = False
        assert "费用" not in stats_line(theme, display, TurnStats(cost=1.2345))

    def test_small_values_raw(self):
        """输入 / 输出 token 小于 1000 显示原值；最大输出同口径（取整 k 仅 ≥1000）。"""
        _, _, theme, display = make_env(plain=True)
        display.max_tokens = 500
        line = stats_line(theme, display, TurnStats(input_tokens=999, output_tokens=1))
        assert "输入:999 输出:1" in line and "最大输出:500" in line
        display.max_tokens = 1500
        assert "最大输出:2k" in stats_line(theme, display, TurnStats())

    def test_switches_read_at_display_time(self):
        """开关每次显示时动态读取（配置变更即时生效）。"""
        _, _, theme, display = make_env(plain=True)
        stats = TurnStats(input_tokens=10, balance=5.0, cost=1.0)
        assert "费用:¥1.0000" in stats_line(theme, display, stats)
        display.show_cost = False
        display.show_balance = False
        assert "费用" not in stats_line(theme, display, stats)
        assert "余额" not in stats_line(theme, display, stats)
        display.show_cost = True
        assert "费用:¥1.0000" in stats_line(theme, display, stats)

    def test_string_false_switch_truthy(self):
        """兼容怪癖：开关值为字符串 `"false"` 时按真值处理（费用段照常显示）。"""
        _, _, theme, display = make_env(plain=True)
        display.show_cost = "false"
        display.show_balance = "false"
        line = stats_line(theme, display, TurnStats(cost=1.0, balance=2.0))
        assert "费用:¥1.0000" in line and "余额:¥2.00" in line


# ═══════════════════════════════════════════════════════════════
# 输入会话与启动横幅（InputSession / write_banner）
# ═══════════════════════════════════════════════════════════════

class FakePromptSession:
    """ptk 会话替身：按脚本返回结果或抛异常，并记录提示符参数。"""

    def __init__(self, outbox: list, log: list):
        self._outbox = outbox
        self._log = log

    def prompt(self, message):
        self._log.append(message)
        item = self._outbox.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_input_session(*, results, interrupt=None, data_dir=""):
    """构造注入替身会话工厂的 InputSession。

    返回元组（会话, 提示记录, 已建会话列表, 中断替身, 主题）。
    """
    _, _, theme, _ = make_env()
    interrupt = interrupt if interrupt is not None else FakeInterrupt()
    log: list = []
    created: list = []
    outbox = list(results)

    def factory():
        session = FakePromptSession(outbox, log)
        created.append(session)
        return session

    session = InputSession(theme, interrupt, data_dir=data_dir, session_factory=factory)
    return session, log, created, interrupt, theme


class TestInputSession:
    """多行输入会话（spec「输入会话」）。"""

    def test_history_path(self, tmp_path, monkeypatch):
        """历史文件落点：数据目录下 `.narnat_history`；数据目录为空时用户主目录。"""
        assert history_path(str(tmp_path)) == str(tmp_path / ".narnat_history")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert history_path("") == str(tmp_path / ".narnat_history")

    def test_prompt_symbol_and_submit(self):
        """提示符固定 `# `；Enter 提交返回文本（可含内部换行）。"""
        session, log, created, interrupt, _ = make_input_session(
            results=["第一行\n第二行"]
        )
        assert PROMPT_SYMBOL == "# "
        assert session.read() == "第一行\n第二行"
        assert log == [[("class:prompt", "# ")]]
        assert len(created) == 1
        assert interrupt.input_mode_calls == 1      # 每次读取前进入输入模式

    def test_newline_keybindings(self):
        """换行键：Alt+Enter / Ctrl+O / Alt+J 均在光标处插入换行（不提交）。

        键位以 ptk 归一后的键名表达（`enter` 归一为 `c-m`）。
        """
        kb = make_keybindings()
        bindings = {
            tuple(str(getattr(key, "value", key)) for key in binding.keys): binding
            for binding in kb.bindings
        }
        assert set(bindings) == {("c-m",), ("escape", "c-m"), ("c-o",), ("escape", "j")}
        for keys in (("escape", "c-m"), ("c-o",), ("escape", "j")):
            assert bindings[keys].eager() is True
        assert bindings[("c-m",)].eager() is False

    def test_ctrl_c_returns_empty_and_rebuilds(self):
        """Ctrl+C 返回空输入并重建会话（再次输入时编辑缓冲为空）。"""
        session, _, created, _, _ = make_input_session(
            results=[KeyboardInterrupt(), "重建后输入"]
        )
        assert session.read() is None
        assert len(created) == 2
        assert session.read() == "重建后输入"
        assert len(created) == 2

    def test_eof_returns_empty(self):
        """EOF 同 Ctrl+C：返回空输入并重建会话。"""
        session, _, created, _, _ = make_input_session(results=[EOFError()])
        assert session.read() is None
        assert len(created) == 2

    def test_custom_prompt_uses_primary_color(self):
        """自定义提示输入：提示文本以主色显示；读取仍返回文本。"""
        session, log, _, _, theme = make_input_session(results=["y"])
        assert session.read("  确认执行此命令? [y/N]: ") == "y"
        rendered = log[0].value
        assert rendered == (f"{theme.c_primary}  确认执行此命令? [y/N]: {theme.rst}")


class TestBanner:
    """启动横幅（spec「输入会话」的启动横幅场景）。"""

    def test_banner_text_and_separator(self, fixed_width):
        """横幅为两空格缩进 + 模型名（主色），后接分隔线。"""
        buf, console, theme, _ = make_env()
        write_banner(theme, console, "narnat")
        lines = buf.getvalue().split("\n")
        assert lines[0] == f"\r  {theme.ui_header}narnat{theme.rst}"
        assert lines[1] == f"\r  {theme.ui_separator}{'─' * (fixed_width - 2)}{theme.rst}"


# ═══════════════════════════════════════════════════════════════
# 命令分发骨架（CommandRouter）
# ═══════════════════════════════════════════════════════════════

class TestCommandRouter:
    """命令分发骨架：归一 / 可用性门禁 / 三态结果（spec「命令集与分发语义」）。"""

    def router(self, source=None, *, commands=None):
        buf, console, _, _ = make_env()
        source = source if source is not None else StubSource(commands)
        return buf, source, CommandRouter(source, console)

    def test_three_state_values(self):
        """三态取值固定（未识别 / 已处理 / 退出；int 兼容）。"""
        assert (CommandResult.UNKNOWN, CommandResult.HANDLED, CommandResult.EXIT) == (0, 1, 2)

    def test_state_decides_available_set(self):
        """状态决定命令集：游离态无 /explore，根会话有 /explore。"""
        source = StubSource(FREE_COMMANDS)
        buf, _, router = self.router(source)
        assert "/save" in source.available_commands()
        assert "/explore" not in source.available_commands()
        assert router.dispatch("/save") is CommandResult.HANDLED
        assert router.dispatch("/explore x") is CommandResult.UNKNOWN
        assert source.dispatched == ["/save"]

    def test_unknown_returns_unknown_without_output(self):
        """未识别命令：返回未识别且不打印错误（该输入按普通用户消息继续处理）。"""
        buf, source, router = self.router()
        assert router.dispatch("/nope arg") is CommandResult.UNKNOWN
        assert source.dispatched == []
        assert buf.getvalue() == ""

    def test_non_command_text_is_unknown(self):
        """非斜杠文本不落到命令源（按普通用户消息继续处理）。"""
        buf, source, router = self.router()
        assert router.dispatch("看看这个") is CommandResult.UNKNOWN
        assert source.dispatched == []
        assert buf.getvalue() == ""

    def test_command_name_normalized(self):
        """命令名归一：转小写并剥除全部前导斜杠（可用性校验据此判定）。"""
        _, source, router = self.router()
        assert router.dispatch("/SAVE") is CommandResult.HANDLED
        assert router.dispatch("///save 名称") is CommandResult.HANDLED
        assert source.dispatched == ["/SAVE", "///save 名称"]

    def test_leading_slashes_stripped(self):
        """兼容怪癖：///save 等同 /save（前导斜杠全部剥除后校验可用性）。"""
        source = StubSource({"/save": "保存会话"})
        _, _, router = self.router(source)
        assert router.dispatch("///save 名称") is CommandResult.HANDLED
        assert router.dispatch("///nope") is CommandResult.UNKNOWN
        assert source.dispatched == ["///save 名称"]

    def test_result_text_printed(self):
        """命令结果文本由路由打印（空串不输出；缺行尾换行时补齐一行）。"""
        source = StubSource(text="  用法: /cd <名称>")
        buf, _, router = self.router(source)
        assert router.dispatch("/cd") is CommandResult.HANDLED
        assert buf.getvalue() == "\r  用法: /cd <名称>\n"
        empty = StubSource(text="")
        buf2, _, router2 = self.router(empty)
        router2.dispatch("/save")
        assert buf2.getvalue() == ""

    def test_unknown_reply_text_not_printed(self):
        """未识别命令不打印错误提示（即使命令源带回文本）。"""
        source = StubSource(kind="unknown", text="  错误: 未知命令")
        buf, _, router = self.router(source)
        assert router.dispatch("/nope") is CommandResult.UNKNOWN
        assert buf.getvalue() == ""

    def test_missing_args_handler_business(self):
        """缺参提示由命令处理器产出：路由原样打印并返回已处理（不落到模型）。"""
        source = StubSource(text="  用法: /rm <名称 | --all>")
        buf, _, router = self.router(source)
        assert router.dispatch("/rm") is CommandResult.HANDLED
        assert "用法: /rm" in buf.getvalue()

    def test_clear_always_available(self):
        """兼容怪癖：/clear 任何状态可用、不依赖命令源（裸清屏转义、不经颜色体系）。"""
        buf, source, router = self.router()
        assert router.dispatch("/clear") is CommandResult.HANDLED
        assert buf.getvalue() == "\r" + CLEAR_SCREEN
        assert source.dispatched == []
        assert CLEAR_SCREEN == "\033[2J\033[H"

    def test_clear_without_source(self):
        """/clear 不依赖会话管理器：命令源缺失时仍可用。"""
        buf, console, _, _ = make_env()
        router = CommandRouter(None, console)
        assert router.dispatch("clear") is CommandResult.HANDLED
        assert router.dispatch("/save") is CommandResult.UNKNOWN
        assert buf.getvalue() == "\r" + CLEAR_SCREEN

    def test_goal_bypasses_availability(self):
        """/goal 不经可用性校验（任何状态均可执行）。"""
        source = StubSource({"/save": "保存会话"})   # 可用集合不含 /goal 时不拦截
        _, _, router = self.router(source)
        assert router.dispatch("/goal on 5") is CommandResult.HANDLED
        assert source.dispatched == ["/goal on 5"]

    def test_exit_semantics_passthrough(self):
        """退出语义（EXIT）由命令源返回，路由原样透传（int 值 2 兼容现状）。"""
        source = StubSource(kind="exit")
        _, _, router = self.router(source)
        result = router.dispatch("/exit")
        assert result is CommandResult.EXIT and result == 2


# ═══════════════════════════════════════════════════════════════
# Tab 补全（CommandCompleter）
# ═══════════════════════════════════════════════════════════════

SKILL_TREE = [
    {"name": "code-review", "type": "file", "origin": "system"},
    {"name": "docs", "type": "dir", "children": [
        {"name": "guide.md", "type": "file", "origin": "project"},
        {"name": "advanced", "type": "dir", "children": [
            {"name": "tips.md", "type": "file", "origin": "project"},
        ]},
    ]},
    {"name": "single-skill", "type": "dir", "single": True, "children": [
        {"name": "SKILL.md", "type": "file", "origin": "project"},
    ]},
]


class TestCompleter:
    """Tab 补全语义（spec「Tab 补全」）。"""

    def test_satisfies_command_source_port(self):
        """命令源替身满足 CommandSource 结构协议（补全器只依赖该端口）。"""
        assert isinstance(StubSource(), CommandSource)

    def test_command_prefix(self):
        """命令名前缀补全：补全剩余字符并显示命令描述。"""
        result = complete(StubSource(), "/sa")
        assert result == [{"text": "ve", "start_position": 0, "meta": "保存会话"}]

    def test_state_changes_candidates(self):
        """补全随会话状态变化：游离态候选不含 /explore，根会话含 /explore。"""
        free = complete(StubSource(FREE_COMMANDS), "/")
        assert not any(item["meta"] == "探索分支" for item in free)
        root = complete(StubSource(ROOT_COMMANDS), "/")
        assert any(item["text"] == "explore" for item in root)

    def test_non_slash_no_candidates(self):
        """非斜杠输入不补全。"""
        assert complete(StubSource(), "hello") == []
        assert complete(StubSource(), "") == []

    def test_cd_names(self):
        """/cd：命令后带空格列出全部会话名；输入第二个词按前缀补全。"""
        source = StubSource(names=["父子会话", "会话A", "会话B"])
        assert [c["text"] for c in complete(source, "/cd ")] == \
            ["父子会话", "会话A", "会话B"]
        assert complete(source, "/cd 会") == [
            {"text": "话A", "start_position": 0, "meta": ""},
            {"text": "话B", "start_position": 0, "meta": ""},
        ]

    def test_nested_prefix_only_last_segment(self):
        """层级候选只补最后一段：路径前缀保留。"""
        source = StubSource(names=["父会话/子会话"])
        assert complete(source, "/cd 父会话/子") == [
            {"text": "子会话", "start_position": -1, "meta": ""}
        ]

    def test_static_option(self):
        """/ls 静态候选 `--all`；其余命令参数位置不产出候选。"""
        assert complete(StubSource(), "/ls ") == [
            {"text": "--all", "start_position": 0, "meta": ""}
        ]
        assert complete(StubSource(), "/save ") == []
        assert complete(StubSource(names=["会话A"]), "/cd 会话A 额外") == []

    def test_skill_dir_and_leaf(self):
        """/skill：目录候选补尾 `/` 并标注"目录"；叶子标注来源；下钻逐层。"""
        source = StubSource(skill_tree=SKILL_TREE)
        assert complete(source, "/skill d") == [
            {"text": "ocs/", "start_position": 0, "meta": "目录"}
        ]
        assert complete(source, "/skill code") == [
            {"text": "-review", "start_position": 0, "meta": "系统技能"}
        ]
        assert complete(source, "/skill docs/") == [
            {"text": "guide.md", "start_position": 0, "meta": "项目技能"},
            {"text": "advanced/", "start_position": 0, "meta": "目录"},
        ]
        assert complete(source, "/skill docs\\a") == [
            {"text": "dvanced/", "start_position": 0, "meta": "目录"}
        ]

    def test_skill_dir_complete_enters(self):
        """名字已完整输入的目录按 Tab 补 `/`（标注"进入目录"）。"""
        source = StubSource(skill_tree=SKILL_TREE)
        assert complete(source, "/skill docs") == [
            {"text": "/", "start_position": 0, "meta": "进入目录"}
        ]
        assert complete(source, "/skill single-skill") == [
            {"text": "/", "start_position": 0, "meta": "进入目录"}
        ]

    def test_skill_leaf_complete_no_candidate(self):
        """叶子完整输入不产出候选（避免 Tab 无反馈）；未知目录同样为空。"""
        source = StubSource(skill_tree=SKILL_TREE)
        assert complete(source, "/skill code-review") == []
        assert complete(source, "/skill nope/") == []

    def test_skill_requires_command_availability(self):
        """命令不在当前状态可用集合时，/skill 不产出技能候选。"""
        source = StubSource(commands={"/save": "保存会话"}, skill_tree=SKILL_TREE)
        assert complete(source, "/skill d") == []


# ═══════════════════════════════════════════════════════════════
# 会话级交互（UiInteraction）
# ═══════════════════════════════════════════════════════════════

class TestUiInteraction:
    """`UiInteraction` 的交互端口语义（创建流句柄 / 确认读取 / 中断收敛）。"""

    def make(self, *, results=("y",)):
        """构造交互环境（全部替身注入，无线程 / 无真实终端）。"""
        buf, console, theme, display = make_env()
        interrupt = FakeInterrupt()
        created: list = []

        def factory():
            session = FakePromptSession(list(results), [])
            created.append(session)
            return session

        session = InputSession(theme, interrupt, session_factory=factory)
        source = StubSource(names=["会话A"])
        router = CommandRouter(source, console)
        spinner = FakeAnimation()
        sink = UiSink(theme, console, interrupt, display,
                      renderer=FakeRenderer(), spinner=spinner)
        interaction = UiInteraction(theme, console, interrupt, display, session,
                                   router, sink_factory=lambda: sink)
        return SimpleNamespace(buf=buf, interaction=interaction, interrupt=interrupt,
                               session=session, created=created, source=source,
                               sink=sink, spinner=spinner, console=console, theme=theme)

    def test_satisfies_interaction_port(self):
        """满足 contracts.output.InteractionPort 结构协议。"""
        assert isinstance(self.make().interaction, InteractionPort)

    def test_begin_turn_enters_run_mode_and_starts_animation(self):
        """开始回合：进入运行模式（清除中断标志并开始打断监听）并启动思考动画。"""
        env = self.make()
        assert env.interaction.begin_turn() is env.sink
        assert env.interrupt.run_mode_calls == 1
        assert env.spinner.starts == 1

    def test_read_input_and_confirmation(self):
        """输入读取与确认：y / yes 视为确认，其余与空输入为取消。"""
        env = self.make(results=[" yes ", "n", ""])
        assert env.interaction.read_confirmation("确认?") is True
        assert env.interaction.read_confirmation("确认?") is False
        assert env.interaction.read_confirmation("确认?") is False

    def test_read_confirmation_empty_input_is_cancel(self):
        """无输入（Ctrl+C / EOF 返回空输入）按取消处理。"""
        env = self.make(results=[KeyboardInterrupt()])
        assert env.interaction.read_confirmation("确认?") is False

    def test_input_session_lazy_creation(self):
        """输入会话懒创建：构造阶段不建立（装配不依赖真实控制台），首次读取时建立。"""
        env = self.make()
        assert len(env.created) == 0
        env.interaction.read_confirmation("确认?")
        assert len(env.created) == 1

    def test_notify_interrupted_rebuilds_session(self):
        """中断收敛：切回输入模式并重建输入会话（丢弃残留编辑缓冲）。"""
        env = self.make()
        env.interaction.start("narnat")  # 启动建立会话
        assert len(env.created) == 1
        env.interaction.notify_interrupted()
        assert env.interrupt.input_mode_calls == 2  # start 一次 + 中断收敛一次
        assert len(env.created) == 2

    def test_start_writes_banner(self, fixed_width):
        """启动界面：输入模式 + 横幅（模型名）+ 分隔线。"""
        env = self.make()
        env.interaction.start("narnat")
        assert env.interrupt.input_mode_calls == 1
        out = env.buf.getvalue()
        assert f"  {env.theme.ui_header}narnat{env.theme.rst}" in out
        assert "─" * (fixed_width - 2) in out

    def test_dispatch_command_routes_to_source(self):
        """命令分发入口经命令路由（命令源可见完整命令行）。"""
        env = self.make()
        assert env.interaction.dispatch_command("/SAVE", "名称") is CommandResult.HANDLED
        assert env.source.dispatched == ["/SAVE 名称"]


# ═══════════════════════════════════════════════════════════════
# 命令源接线（真实 sessions 积木经端口注入；跨任务边界核验）
# ═══════════════════════════════════════════════════════════════

class TestCommandSourceIntegration:
    """真实 `SessionManager` 作为命令源的接线核验（ui 侧零适配直接消费）。

    覆盖 spec「命令集与分发语义」的状态联动与三态，以及「Tab 补全」的
    「补全随会话状态变化」（候选表来自命令源，ui 不 import sessions）。
    """

    def make(self, tmp_path):
        """构造（缓冲, 路由, 补全器, 命令源, 消息历史）。"""
        from narnat_agent.messages import MessageStore
        from narnat_agent.sessions.manager import SessionManager
        from narnat_agent.sessions.store import SessionStore

        messages = MessageStore("sys")
        manager = SessionManager(SessionStore(str(tmp_path)), messages,
                                 name_func=lambda msgs: "自动命名")
        buf, console, _, _ = make_env()
        return buf, CommandRouter(manager, console), CommandCompleter(manager), manager, messages

    def test_free_state_candidates_from_source(self, tmp_path):
        """游离态：候选表来自命令源，不含 /explore。"""
        _, _, completer, manager, _ = self.make(tmp_path)
        texts_ = [c.text for c in completer.get_completions(Document("/", 1), None)]
        assert "explore" not in texts_
        assert len(texts_) == len(manager.available_commands())

    def test_root_state_candidates_after_save(self, tmp_path):
        """保存进入根会话后候选表出现 /explore（状态驱动的补全）。"""
        _, router, completer, _, messages = self.make(tmp_path)
        messages.append_user("输入")
        assert router.dispatch("/save 接线核验") is CommandResult.HANDLED
        texts_ = [c.text for c in completer.get_completions(Document("/", 1), None)]
        assert "explore" in texts_

    def test_clear_and_unknown_and_usage(self, tmp_path):
        """/clear 恒可用（ui 清屏）；未识别无输出；缺参走真实处理器文案。"""
        buf, router, _, _, _ = self.make(tmp_path)
        assert router.dispatch("/clear") is CommandResult.HANDLED
        assert buf.getvalue() == "\r" + CLEAR_SCREEN
        buf.seek(0)
        buf.truncate(0)
        assert router.dispatch("/nope") is CommandResult.UNKNOWN
        assert buf.getvalue() == ""
        assert router.dispatch("/cd") is CommandResult.HANDLED
        assert "用法: /cd" in buf.getvalue()

    def test_goal_bypasses_and_targets_manager(self, tmp_path):
        """/goal 不经可用性校验，真实处理器回写目标模式。"""
        _, router, _, manager, _ = self.make(tmp_path)
        assert router.dispatch("/goal on 5") is CommandResult.HANDLED
        assert manager.goal.enabled is True and manager.goal.max_rounds == 5


# ═══════════════════════════════════════════════════════════════
# headless 纯文本输出（HeadlessSink / HeadlessInteraction / HeadlessAnimator）
# ═══════════════════════════════════════════════════════════════

def make_headless(*, plain: bool = True):
    """构造 headless（缓冲, 控制台, 主题, 流句柄）。"""
    buf, console, theme, _ = make_env(plain=plain)
    return buf, console, theme, HeadlessSink(theme, console)


class TestHeadlessSink:
    """headless 流会话替身（spec「headless 纯文本输出」）。"""

    def test_satisfies_output_sink_port(self):
        """满足 OutputSink 结构协议。"""
        _, _, _, sink = make_headless()
        assert isinstance(sink, OutputSink)

    def test_cancelled_always_false(self):
        """不可打断：取消查询恒为假；中止置位中止标记。"""
        _, _, _, sink = make_headless()
        assert sink.cancelled is False
        assert sink.aborted is False
        sink.abort()
        assert sink.aborted is True
        assert sink.cancelled is False

    def test_plain_structure_preserved(self):
        """纯文本结构保留：无 ANSI 转义与行首回车，表格边框与代码块行号保留。"""
        buf, _, _, sink = make_headless()
        sink.feed("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
        sink.flush()
        table_part = buf.getvalue()
        sink.feed("```python\nprint(1)\n```\n")
        sink.feed("## 标题\n- 项\n")
        sink.flush()
        out = buf.getvalue()
        assert "\x1b" not in out
        assert "\r" not in out
        assert table_part == ("    +---+---+\n    | A | B |\n    +---+---+\n"
                              "    | 1 | 2 |\n    +---+---+\n")
        rest = out[len(table_part):]
        assert rest == "  -- python --\n    1 print(1)\n  标题\n   * 项\n"

    def test_finish_no_stats(self):
        """headless 收尾无统计：忽略统计参数、只落定内容。"""
        buf, _, _, sink = make_headless()
        sink.feed("内容\n")
        sink.finish(TurnStats(input_tokens=12345, output_tokens=678), with_stats=True)
        out = buf.getvalue()
        assert "输入:" not in out and "─" not in out

    def test_abort_only_with_message(self):
        """中止：消息非空才输出（无默认打断提示）；中止前先落定内容。"""
        buf, _, _, sink = make_headless()
        sink.abort()
        assert buf.getvalue() == ""
        sink.abort("⚠ 程序异常，本轮回复已停止: 原因")
        assert buf.getvalue() == "⚠ 程序异常，本轮回复已停止: 原因\n"
        assert "已打断" not in buf.getvalue()

    def test_animation_calls_no_effect(self):
        """开始 / 暂停 / 恢复动画无效果（无输出、无异常）。"""
        buf, _, _, sink = make_headless()
        sink.begin()
        sink.pause()
        sink.resume()
        assert buf.getvalue() == ""

    def test_flush_guard_and_restart_attempt(self):
        """落定守卫与重试重播（与交互模式同语义）。"""
        buf, _, _, sink = make_headless()
        sink.feed("| A | B |\n")
        sink.restart_attempt()
        sink.flush()
        assert buf.getvalue() == ""

    def test_notify_plain_text(self):
        """状态提示：纯文本段落形态（无颜色），空白行不产生输出。"""
        buf, _, _, sink = make_headless()
        sink.notify("\n⚠ 服务端响应流中断，约2s后自动重试（第1/3次）…\n")
        assert buf.getvalue() == "  ⚠ 服务端响应流中断，约2s后自动重试（第1/3次）…\n"


class TestHeadlessInteraction:
    """headless 会话级交互替身。"""

    def test_satisfies_interaction_port(self):
        """满足 InteractionPort 结构协议。"""
        buf, console, theme, _ = make_env(plain=True)
        assert isinstance(HeadlessInteraction(theme, console), InteractionPort)

    def test_turn_and_inputs(self):
        """开始回合产流句柄；输入读取恒返回空；确认恒为取消；分发恒未识别。"""
        buf, console, theme, _ = make_env(plain=True)
        interaction = HeadlessInteraction(theme, console)
        sink = interaction.begin_turn()
        assert isinstance(sink, HeadlessSink)
        assert interaction.read_input() is None
        assert interaction.read_confirmation("确认?") is False
        assert interaction.dispatch_command("/save", "") is CommandResult.UNKNOWN
        interaction.notify_interrupted()
        interaction.start("narnat")
        assert buf.getvalue() == ""

    def test_turn_sinks_are_independent(self):
        """每次开始回合产出新的流句柄。"""
        buf, console, theme, _ = make_env(plain=True)
        interaction = HeadlessInteraction(theme, console)
        assert interaction.begin_turn() is not interaction.begin_turn()


class TestHeadlessAnimator:
    """headless 动画空实现：均为无效果。"""

    def test_satisfies_animator_port(self):
        """满足 Animator 结构协议。"""
        assert isinstance(HeadlessAnimator(), Animator)

    def test_all_no_effect(self):
        """四个操作均无异常。"""
        animator = HeadlessAnimator()
        animator.begin_compressing()
        animator.end_compressing()
        animator.begin_summarizing()
        animator.end_summarizing()


class TestHeadlessConsole:
    """headless 输出环境置位（去色 + 调度日志默认静默 + UTF-8）。"""

    def test_prepare_console(self):
        """默认静默：纯文本模式置位 + 工具调度日志静默；`-l` 恢复全量。"""
        buf, console, _, _ = make_env()
        prepare_console(console, tool_log=False, stream=io.StringIO())
        assert console.is_plain() is True
        assert console.is_quiet_tools() is True
        prepare_console(console, tool_log=True, stream=io.StringIO())
        assert console.is_plain() is True
        assert console.is_quiet_tools() is False

    def test_enable_utf8_stdout_tolerates_streams(self):
        """UTF-8 强制：支持 reconfigure 的流被调用；不支持的流静默跳过。"""

        class Stream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stream = Stream()
        prepare_console(make_env()[1], stream=stream)
        assert stream.calls == [{"encoding": "utf-8"}]
        prepare_console(make_env()[1], stream=object())   # 不支持时不抛异常

    def test_sink_surface_parity(self):
        """交互与 headless 两种流句柄成员同形（装配侧可无差别注入）。"""
        surface = {"cancelled", "aborted", "begin", "feed", "notify", "flush",
                   "pause", "resume", "restart_attempt", "finish", "abort"}
        _, sink, *_ = make_sink()
        _, _, _, headless = make_headless()
        for member in surface:
            assert hasattr(sink, member), member
            assert hasattr(headless, member), member


# ═══════════════════════════════════════════════════════════════
# 旧实现基准对照（v2/tests/baseline/ui/ui_interaction.json）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def snapshot() -> dict:
    if not BASELINE_FILE.exists():
        pytest.skip("ui 交互面基准不存在（跳过快照对照）")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


class TestOldImplementationUiSnapshot:
    """对旧实现 ui 交互面逐用例比对（判据 = 输出字节等价）。

    基准由 `extract_old_ui_interaction.py` 生成：统计栏（参数矩阵 + 开关组合 +
    字符串布尔怪癖）、启动横幅、默认打断提示、Tab 补全（命令源状态 × 光标前文本）。
    """

    def test_stats_bar_outputs_match_old_implementation(self, snapshot, fixed_width):
        for case in snapshot["groups"][STATS_GROUP]:
            switches = case["input"]["switches"]
            buf, console, theme, display = make_env()
            display.show_cost = switches["show_cost"]
            display.show_balance = switches["show_balance"]
            display.max_tokens = switches["max_tokens"]
            display.show_ratio = switches["show_ratio"]
            display.context_window = switches["context_window"]
            stats = case["input"]["stats"]
            sink = UiSink(theme, console, FakeInterrupt(), display,
                          renderer=FakeRenderer(), spinner=FakeAnimation())
            sink.show_stats(TurnStats(**stats))
            assert buf.getvalue() == case["result"], case["id"]

    def test_banner_outputs_match_old_implementation(self, snapshot, fixed_width):
        for case in snapshot["groups"][BANNER_GROUP]:
            buf, console, theme, _ = make_env()
            write_banner(theme, console, case["input"]["model"])
            assert buf.getvalue() == case["result"], case["id"]

    def test_interrupt_prompt_matches_old_implementation(self, snapshot):
        for case in snapshot["groups"][INTERRUPT_GROUP]:
            buf, sink, _, _, _, _ = make_sink()
            sink.abort()
            assert buf.getvalue() == case["result"], case["id"]

    def test_completions_match_old_implementation(self, snapshot):
        for case in snapshot["groups"][COMPLETION_GROUP]:
            state = case["input"]["state"]
            source = StubSource(
                state["commands"],
                names=state["names"],
                rm_names=state["rm_names"],
                thinking=state["thinking"],
                models=state["models"],
                skill_tree=state["skill_tree"],
            )
            assert complete(source, case["input"]["text"]) == case["result"], case["id"]

