"""compression 积木自测（T3.1）—— spec Scenario 全覆盖 + 基准对照 + 归因/占比矩阵 + fake LLM 编排。

覆盖清单（对齐 T3.1 任务书测试要求）：
1. `specs/compression` 全部 32 个 Scenario（映射表见下；S3/S24 中属会话层的「请求流程/
   记账」部分随测试注释标明归属）；
2. 重点矩阵：切点选择与 `v2/tests/baseline/data/compressor.json` 逐用例对照、token 估算与
   `token_estimate.json` 的 `compressor.estimate_tokens` 组对照、防连续 user 三分支、
   压缩结果归因（5 值）与失败不改动历史、占比计算边界（窗口 ≤0、输入 0/负）；
3. 摘要请求与重建经 fake LLM 注入测试（请求形态、正文收集、流中取消、异常传播）。

spec Scenario 映射表
════════════════════

| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 窗口占比与触发判定 | 达到压缩阈值先压缩 | `test_scenario01_threshold_triggers_compress` |
| 2 | 窗口占比与触发判定 | 未达阈值直接请求 | `test_scenario02_below_threshold_no_compress` |
| 3 | 窗口占比与触发判定 | 压缩失败仍保留用户输入 | `test_scenario03_failed_compress_keeps_input`（「不产生 AI 回复/回到等待」归会话层） |
| 4 | 窗口占比与触发判定 | 告警仅一次 | `test_scenario04_warn_once` |
| 5 | 窗口占比与触发判定 | 窗口无效 | `test_scenario05_invalid_window` |
| 6 | 运行中主动压缩 | 自我运行期间超阈值 | `test_scenario06_mid_run_over_threshold`（触发时机的请求流程归会话层） |
| 7 | 运行中主动压缩 | 未超阈值不压缩 | `test_scenario07_mid_run_below_threshold` |
| 8 | 摘要请求构造 | 请求内容 | `test_scenario08_summary_request_shape` |
| 9 | 摘要请求构造 | 压缩请求自身超限 | `test_scenario09_summary_request_overflow` |
| 10 | 保留尾部切点选择 | 预算为 0 全量压缩 | `test_scenario10_zero_budget_full_compress` |
| 11 | 保留尾部切点选择 | 切点回退不拆散工具对 | `test_scenario11_cut_backoff_keeps_tool_pair` |
| 12 | 保留尾部切点选择 | system 消息不进保留区 | `test_scenario12_no_system_in_tail` |
| 13 | 保留尾部切点选择 | 无安全切点时全量压缩 | `test_scenario13_no_safe_cut_full_compress` |
| 14 | 压缩后会话重建 | 重建结构 | `test_scenario14_rebuild_structure` |
| 15 | 压缩后会话重建 | 尾部末条为 user 时合并新输入 | `test_scenario15_merge_input_into_tail_user` |
| 16 | 压缩后会话重建 | 尾部非 user 时追加输入 | `test_scenario16_append_input_when_tail_not_user` |
| 17 | 压缩后会话重建 | 手动压缩剔除尾部连续 user | `test_scenario17_manual_drops_trailing_users` |
| 18 | 压缩后会话重建 | 尾部含 system 被丢弃 | `test_scenario18_tail_system_dropped` |
| 19 | 结果归因与历史不变性 | 无历史可压缩（手动路径） | `test_scenario19_manual_empty` |
| 20 | 结果归因与历史不变性 | 零输出且已取消 | `test_scenario20_zero_output_cancelled` |
| 21 | 结果归因与历史不变性 | 摘要请求出错 | `test_scenario21_llm_error` |
| 22 | 结果归因与历史不变性 | 失败不改动历史 | `test_scenario22_history_unchanged_on_failure` |
| 23 | 溢出恢复 | 首次超限自动压缩重发 | `test_scenario23_overflow_recover_success`（「重发请求」归会话层） |
| 24 | 溢出恢复 | 二次超限不再恢复 | `test_scenario24_recover_stateless`（「每次运行最多一次」记账归会话层，本积木无状态） |
| 25 | 溢出恢复 | 恢复失败 | `test_scenario25_recover_failure` |
| 26 | 手动压缩 | 成功 | `test_scenario26_manual_success`（「保存落盘」归会话层） |
| 27 | 手动压缩 | 取消 | `test_scenario27_manual_cancel` |
| 28 | 手动压缩 | 无可压缩历史 | `test_scenario28_manual_empty_text` |
| 29 | token 估算口径 | 中英文估价 | `test_scenario29_estimate_cjk_ascii` |
| 30 | token 估算口径 | 消息级汇总 | `test_scenario30_estimate_message_sum` |
| 31 | 兼容性怪癖保持 | 恰等于阈值即触发 | `test_scenario31_threshold_inclusive` |
| 32 | 兼容性怪癖保持 | 失败后的占比处理 | `test_scenario32_ratio_after_failure` |

基准对照（`v2/tests/baseline/data/`，旧实现原位生成）：
- `compressor.json`：`_cut_balanced`（4）/ `select_cut_index`（9）/ `Compressor.build_*`（3）——
  三组全用例逐字对照；
- `token_estimate.json`：`compressor.estimate_tokens` 与 `token_estimate.estimate_text_tokens`
  两组（估算的唯一实现归 `messages.tokens`，本积木经 `compressor` 转发引用；对照面不变）。

旧实现（R1 报告补丁痕迹）中「转发壳 estimate_tokens」按 T3.1 要求删除，基准中该组用例改由
`estimate_message_tokens` 对照（值必须逐用例相等）。T3.11 进一步把估算实现唯一化到
`messages.tokens`（原先 compression 与 tools 各持一份同逻辑副本）：本积木不再持有副本，
`compressor.estimate_*` 变为转发导出（与 `messages.tokens` 同一函数对象）。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from narnat_agent.compression import (
    MANUAL_FAIL_TEXT,
    OVERFLOW_CONTINUE_MESSAGE,
    CompressResult,
    CompressionContext,
    CompressionCoordinator,
)
from narnat_agent.compression.compressor import (
    _cut_balanced,
    Compressor,
    estimate_message_tokens,
    estimate_text_tokens,
    select_cut_index,
)
from narnat_agent.config.defaults import COMPRESS_PROMPT
from narnat_agent.messages import MessageStore
from tests.baseline import cases

TESTS_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "compression"

MAIN_SYSTEM = "系统提示"
SUMMARY_TEXT = "摘要内容"

OK_EVENTS = [{"content": SUMMARY_TEXT}, {"finish_reason": "stop"}]


# ═══════════════════════════════════════════════════════════════
# 桩件与构造辅助
# ═══════════════════════════════════════════════════════════════


class FakeLog:
    """日志端口桩：按 `(级别, 模块名, 消息)` 记录调用序列。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def info(self, module: str, message: str) -> None:
        self.calls.append(("info", module, message))

    def warning(self, module: str, message: str) -> None:
        self.calls.append(("warning", module, message))

    def error(self, module: str, message: str) -> None:
        self.calls.append(("error", module, message))

    def texts(self, level: str) -> list[str]:
        return [message for kind, _, message in self.calls if kind == level]


class FakeInterrupt:
    """中断信号桩：可外部置位；记录运行/输入模式切换次序。"""

    def __init__(self) -> None:
        self.flag = False
        self.mode_calls: list[str] = []

    @property
    def is_set(self) -> bool:
        return self.flag

    def raise_(self) -> None:
        self.flag = True

    def clear(self) -> None:
        self.flag = False

    def subscribe(self, handler) -> None:  # pragma: no cover - 桩件不订阅
        raise AssertionError("压缩流程不应订阅中断处理器")

    def enter_run_mode(self) -> None:
        self.mode_calls.append("run")
        self.flag = False

    def enter_input_mode(self) -> None:
        self.mode_calls.append("input")
        self.flag = False


class FakeAnimator:
    """压缩动画桩：记录开始/结束次序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_compressing(self) -> None:
        self.calls.append("begin")

    def end_compressing(self) -> None:
        self.calls.append("end")

    def begin_summarizing(self) -> None:
        self.calls.append("summarizing_begin")

    def end_summarizing(self) -> None:
        self.calls.append("summarizing_end")


class FakeLLM:
    """摘要请求桩：按脚本产出事件流，记录请求参数。

    - `events`：依次产出的事件 dict 列表；
    - `cancel_at`：产出第 N 个事件（0 基）前先置位中断（模拟流中被取消）；
      `"end"` 表示流耗尽（无任何输出）后置位（模拟「零输出且已取消」）；
    - `raise_error`：迭代时抛出给定异常（模拟摘要请求执行中出错）。
    """

    def __init__(self, events=None, *, cancel_at=None, interrupt: FakeInterrupt | None = None,
                 raise_error: Exception | None = None) -> None:
        self.events = list(events or [])
        self.cancel_at = cancel_at
        self.interrupt = interrupt
        self.raise_error = raise_error
        self.requests: list[dict] = []

    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None):
        self.requests.append({
            "messages": messages,
            "no_tools": no_tools,
            "no_thinking": no_thinking,
            "cancel_check": cancel_check,
        })
        if self.raise_error is not None:
            raise self.raise_error
        for index, event in enumerate(self.events):
            if self.cancel_at == index and self.interrupt is not None:
                self.interrupt.flag = True
            yield event
        if self.cancel_at == "end" and self.interrupt is not None:
            self.interrupt.flag = True


class FakeConfig:
    """配置面桩：压缩所需最小字段（`system_prompt` / `session.retain_tokens`）。"""

    def __init__(self, retain_tokens: int = 0, system_prompt: str = MAIN_SYSTEM) -> None:
        self.system_prompt = system_prompt
        self.session = SimpleNamespace(retain_tokens=retain_tokens)


def make_env(history=None, events=None, *, retain_tokens=0, system_prompt=MAIN_SYSTEM,
             context=None, interrupt=None, cancel_at=None, llm=None,
             raise_error=None, logger=None):
    """按给定历史与摘要流构造一套压缩环境（store/coordinator/各桩件）。"""
    store = MessageStore(system_prompt)
    if history is not None:
        store.replace_all([dict(m) for m in history])
    context = context if context is not None else CompressionContext(
        context_window=1000, warn_ratio=50, compress_ratio=95)
    interrupt = interrupt if interrupt is not None else FakeInterrupt()
    animator = FakeAnimator()
    if llm is None:
        llm = FakeLLM(events if events is not None else OK_EVENTS,
                      cancel_at=cancel_at, interrupt=interrupt, raise_error=raise_error)
    config = FakeConfig(retain_tokens=retain_tokens, system_prompt=system_prompt)
    coordinator = CompressionCoordinator(
        config, store, llm, context, animator, interrupt, logger)
    return SimpleNamespace(
        coordinator=coordinator, store=store, llm=llm, context=context,
        interrupt=interrupt, animator=animator, logger=logger, config=config,
    )


def roles(messages) -> list[str]:
    return [m.get("role") for m in messages]


def user_text(messages, index: int = -1) -> str:
    return messages[index].get("content")


def base_history() -> list[dict]:
    """最小对话历史：system + 一轮问答。"""
    return [
        {"role": "system", "content": MAIN_SYSTEM},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]


# ═══════════════════════════════════════════════════════════════
# 1. spec Scenario 覆盖（1-5：窗口占比与触发判定）
# ═══════════════════════════════════════════════════════════════


def test_scenario01_threshold_triggers_compress():
    """S1：占比 ≥ 压缩阈值 → 需要压缩；成功后本次输入随重建写入历史且不重复追加。"""
    env = make_env(base_history(), retain_tokens=0)
    env.context.update_ratio(950)  # 950/1000 → 95%（恰等于阈值）
    assert env.context.need_compress() is True

    result = env.coordinator.compress("新问题")
    assert result.ok is True
    msgs = env.store.view().to_list()
    assert roles(msgs) == ["system", "system", "user"]
    assert msgs[1]["content"] == f"# 上一轮对话成果\n\n{SUMMARY_TEXT}"
    assert user_text(msgs) == "新问题"
    assert roles(msgs).count("user") == 1  # 新输入只出现一次
    assert env.context.ratio is None  # 压缩成功后占比重置


def test_scenario02_below_threshold_no_compress():
    """S2：占比低于压缩阈值 → 不需要压缩（会话层据此直接请求）。"""
    env = make_env(base_history())
    env.context.update_ratio(940)  # 94% < 95%
    assert env.context.need_compress() is False


def test_scenario03_failed_compress_keeps_input():
    """S3：压缩失败时用户输入仍写入历史；不产生任何 AI 回复内容。"""
    env = make_env(base_history(), events=[{"finish_reason": "error"}], retain_tokens=0)
    env.context.update_ratio(990)
    result = env.coordinator.compress("被保留的输入")
    assert result.ok is False
    msgs = env.store.view().to_list()
    assert roles(msgs) == ["system", "user", "assistant", "user"]  # 原历史 + 输入，无新回复
    assert user_text(msgs) == "被保留的输入"      # 输入写入历史
    assert all(m.get("content") != SUMMARY_TEXT for m in msgs)


def test_scenario04_warn_once():
    """S4：告警仅一次；压缩成功重置后可再次告警。"""
    env = make_env(base_history())
    env.context.update_ratio(600)  # 60% ≥ 50%
    assert env.context.check_warn() == "窗口占比已达50%，建议开启新对话"
    assert env.context.check_warn() == ""  # 同会话不再输出

    env.coordinator.compress("触发压缩")  # 压缩成功 → 重置
    env.context.update_ratio(600)
    assert env.context.check_warn() == "窗口占比已达50%，建议开启新对话"


def test_scenario05_invalid_window():
    """S5：窗口 ≤ 0 或尚未收到 token 数据 → 占比无数据，不告警也不压缩。"""
    for window in (0, -1):
        ctx = CompressionContext(context_window=window, warn_ratio=50, compress_ratio=95)
        ctx.update_ratio(10 ** 9)
        assert ctx.ratio is None
        assert ctx.need_compress() is False
        assert ctx.check_warn() == ""

    fresh = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95)
    assert fresh.ratio is None  # 从未收到 token 数据
    assert fresh.need_compress() is False
    assert fresh.check_warn() == ""


# ═══════════════════════════════════════════════════════════════
# 2. spec Scenario 覆盖（6-7：运行中主动压缩）
# ═══════════════════════════════════════════════════════════════


def test_scenario06_mid_run_over_threshold():
    """S6：运行中占比超阈值 → 自查压缩成功（无新输入，以内部继续指令收尾）。"""
    env = make_env(base_history(), retain_tokens=0)
    env.context.update_ratio(960)  # 96%
    assert env.coordinator.mid_run_guard() is True
    msgs = env.store.view().to_list()
    assert user_text(msgs) == OVERFLOW_CONTINUE_MESSAGE
    assert env.context.ratio is None


def test_scenario07_mid_run_below_threshold():
    """S7：请求前占比低于阈值 → 不做任何压缩，直接发请求。"""
    env = make_env(base_history())
    env.context.update_ratio(900)  # 90% < 95%
    assert env.coordinator.mid_run_guard() is False
    assert env.llm.requests == []  # 未发起摘要请求
    assert env.animator.calls == []


# ═══════════════════════════════════════════════════════════════
# 3. spec Scenario 覆盖（8-9：摘要请求构造）
# ═══════════════════════════════════════════════════════════════


def test_scenario08_summary_request_shape():
    """S8：摘要请求 = 全部历史 + 末尾压缩指令（user 角色），且不携带任何工具定义。"""
    history = base_history()
    env = make_env(history, retain_tokens=0)
    env.coordinator.compress_no_input()

    assert len(env.llm.requests) == 1
    request = env.llm.requests[0]
    assert request["no_tools"] is True
    assert request["messages"][:-1] == history  # 全部历史原样
    assert request["messages"][-1] == {"role": "user", "content": COMPRESS_PROMPT}
    # 浅拷贝语义：请求列表是副本，原历史列表结构未被追加指令
    assert len(history) == 3


def test_scenario09_summary_request_overflow():
    """S9：摘要请求因上下文超限被拒 → 归因「压缩请求超限」，不与「总结为空」混淆。"""
    env = make_env(base_history(), events=[{"finish_reason": "context_overflow"}],
                   retain_tokens=0, logger=FakeLog())
    env.context.update_ratio(990)
    result = env.coordinator.compress("x")
    assert result.ok is False
    assert result.reason == "overflow"
    assert env.logger.texts("error") == ["压缩失败: 压缩请求自身超出模型上下文限制"]


# ═══════════════════════════════════════════════════════════════
# 4. spec Scenario 覆盖（10-13：保留尾部切点选择）
# ═══════════════════════════════════════════════════════════════


def test_scenario10_zero_budget_full_compress():
    """S10：保留尾部预算为 0 → 全量压缩，压缩后仅剩系统提示词与摘要（+输入）。"""
    env = make_env(base_history(), retain_tokens=0)
    assert select_cut_index(env.store.view().to_list(), 0) is None
    result = env.coordinator.compress("输入")
    assert result.ok and result.replaced == 2  # u1/a1 全部替换
    assert roles(env.store.view().to_list()) == ["system", "system", "user"]


def test_scenario11_cut_backoff_keeps_tool_pair():
    """S11：候选切点落在 tool / assistant 上 → 回退到最近的 user，工具对不拆散。"""
    dataset = cases.COMPRESSOR_DATASETS["normal"]  # 含 assistant(tool_calls)/tool 对
    # 预算 85：累计至 tool（idx3）达预算 → 回退越过 tool 与 assistant 到 u1（idx1）
    assert select_cut_index(dataset, 85) == 1
    # 预算 61：累计至 a2（idx5）达预算 → 回退到 u2（idx4）
    assert select_cut_index(dataset, 61) == 4

    env = make_env(dataset, retain_tokens=85)
    result = env.coordinator.compress_no_input()
    assert result.ok is True
    msgs = env.store.view().to_list()
    # 工具调用与其结果同在保留区（a1 的 tool_calls 与 tool 消息都未被摘要）；
    # 末条 u3 为 user，内部继续指令合并进该条（不追加新 user）
    tail_roles = roles(msgs)[2:]
    assert tail_roles == ["user", "assistant", "tool", "user", "assistant", "user"]
    tool_id = next(m for m in msgs if m.get("role") == "assistant" and "tool_calls" in m)
    replied_ids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    assert tool_id["tool_calls"][0]["id"] in replied_ids


def test_scenario12_no_system_in_tail():
    """S12：历史中段存在 system（技能注入）时保留区/重建结果都不含 system 消息。"""
    history = base_history() + [
        {"role": "system", "content": "技能注入"},
        {"role": "user", "content": "u2"},
    ]
    env = make_env(history, retain_tokens=100000)  # 预算覆盖全部对话
    result = env.coordinator.compress_no_input()
    assert result.ok is True
    msgs = env.store.view().to_list()
    # 尾部末条 u2 为 user：继续指令合并进该条，重建结果中 system 只剩前两条
    assert roles(msgs) == ["system", "system", "user", "assistant", "user"]
    assert all(m.get("content") != "技能注入" for m in msgs)


def test_scenario13_no_safe_cut_full_compress():
    """S13：对话区首条不是 user（异常序列）→ 放弃保留，执行全量压缩。"""
    history = [
        {"role": "system", "content": MAIN_SYSTEM},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u1"},
    ]
    assert select_cut_index(history, 100000) is None
    env = make_env(history, retain_tokens=100000)
    result = env.coordinator.compress_no_input()
    assert result.ok is True and result.replaced == 2  # a1/u1 全部进摘要
    assert roles(env.store.view().to_list()) == ["system", "system", "user"]


# ═══════════════════════════════════════════════════════════════
# 5. spec Scenario 覆盖（14-18：压缩后会话重建）
# ═══════════════════════════════════════════════════════════════


def test_scenario14_rebuild_structure():
    """S14：新会话 = system(系统提示词) + system(`# 上一轮对话成果`摘要) + 保留尾部非 system 消息。"""
    env = make_env(base_history(), retain_tokens=100000, system_prompt="新系统提示词")
    result = env.coordinator.compress_no_input()
    assert result.ok is True
    msgs = env.store.view().to_list()
    assert msgs[0] == {"role": "system", "content": "新系统提示词"}
    assert msgs[1] == {"role": "system", "content": f"# 上一轮对话成果\n\n{SUMMARY_TEXT}"}
    assert msgs[2] == {"role": "user", "content": "u1"}        # 尾部逐字保留
    assert msgs[3] == {"role": "assistant", "content": "a1"}
    assert msgs[4] == {"role": "user", "content": OVERFLOW_CONTINUE_MESSAGE}  # 收尾


def test_scenario15_merge_input_into_tail_user():
    """S15：自动压缩且尾部末条是 user → 新输入以空行拼接进该条，不出现连续两条 user。"""
    history = base_history() + [{"role": "user", "content": "u2"}]
    env = make_env(history, retain_tokens=100000)
    result = env.coordinator.compress("补充输入")
    assert result.ok is True
    assert result.replaced == 0  # 预算覆盖全部对话：无消息被摘要替换
    msgs = env.store.view().to_list()
    assert user_text(msgs) == "u2\n\n补充输入"
    assert all(not (a.get("role") == "user" and b.get("role") == "user")
               for a, b in zip(msgs, msgs[1:]))  # 无连续 user


def test_scenario16_append_input_when_tail_not_user():
    """S16：自动压缩且尾部末条不是 user → 追加一条本次输入的 user 消息。"""
    env = make_env(base_history(), retain_tokens=100000)  # 尾部 = [u1, a1]
    result = env.coordinator.compress("全新输入")
    assert result.ok is True
    msgs = env.store.view().to_list()
    assert user_text(msgs) == "全新输入"
    assert roles(msgs) == ["system", "system", "user", "assistant", "user"]

    # 未保留尾部（全量）时同样追加
    env2 = make_env(base_history(), retain_tokens=0)
    env2.coordinator.compress("全量后的输入")
    assert roles(env2.store.view().to_list()) == ["system", "system", "user"]


def test_scenario17_manual_drops_trailing_users():
    """S17：手动压缩且尾部末条是 user → 从尾部向前的连续 user 整段剔除并计入替换统计。"""
    history = [
        {"role": "system", "content": MAIN_SYSTEM},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "user", "content": "u4"},
    ]
    env = make_env(history, retain_tokens=30)  # 切点落在 u2（idx3）：尾部 [u2,a2,u3,u4]
    status, text = env.coordinator.compress_manual()
    assert status == "ok"
    msgs = env.store.view().to_list()
    assert roles(msgs) == ["system", "system", "user", "assistant"]  # u3/u4 已剔除
    assert user_text(msgs, 2) == "u2"
    assert msgs[3]["content"] == "a2"
    # 替换统计 = 被摘要的 [u1,a1] + 剔除的 [u3,u4]，条数与估算 token 一并计入
    replaced_tokens = sum(estimate_message_tokens(history[i]) for i in (1, 2, 5, 6))
    assert text == f"已压缩 4 条历史消息（约 {replaced_tokens} tokens）"


def test_scenario18_tail_system_dropped():
    """S18：保留尾部中（非首位）出现 system → 重建时静默丢弃。"""
    history = [
        {"role": "system", "content": MAIN_SYSTEM},
        {"role": "user", "content": "u1"},
        {"role": "system", "content": "中段注入"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    env = make_env(history, retain_tokens=100000)
    assert env.coordinator.compress_no_input().ok is True
    msgs = env.store.view().to_list()
    # 尾部 [u1, S2(中段注入), a1, u2]：S2 丢弃；末条 u2 合并继续指令
    assert roles(msgs) == ["system", "system", "user", "assistant", "user"]
    assert all(m.get("content") != "中段注入" for m in msgs)
    assert OVERFLOW_CONTINUE_MESSAGE in msgs[-1]["content"]


# ═══════════════════════════════════════════════════════════════
# 6. spec Scenario 覆盖（19-22：结果归因与历史不变性）
# ═══════════════════════════════════════════════════════════════


def test_scenario19_manual_empty():
    """S19/S28：手动压缩在「对话区为空或保留预算覆盖全部对话」时提前拒绝，不发起摘要请求。"""
    for history, retain_tokens in (
        ([{"role": "system", "content": MAIN_SYSTEM}], 0),          # 对话区为空
        (base_history(), 100000),                                    # 预算覆盖全部对话
    ):
        env = make_env(history, retain_tokens=retain_tokens)
        assert env.coordinator.compress_manual() == ("empty", "没有可压缩的历史对话")
        assert env.llm.requests == []  # 未发起摘要请求
        assert env.store.view().to_list() == history  # 历史未变


def test_scenario20_zero_output_cancelled():
    """S20：摘要流没有任何输出且用户已取消 → 归因「用户中断」，而非「总结为空」。"""
    env = make_env(base_history(), events=[], cancel_at="end", retain_tokens=0)
    result = env.coordinator.compress("输入")
    assert result.ok is False
    assert result.reason == "interrupted"
    assert env.store.view().to_list()[-1] == {"role": "user", "content": "输入"}
    assert env.context.ratio is None


def test_scenario21_llm_error():
    """S21：摘要流返回错误完成标记 → 归因「摘要请求出错」，提示「LLM调用出错」，历史不变。"""
    history = base_history()
    env = make_env(history, events=[{"content": "[错误: API调用失败]", "finish_reason": "error"}],
                   retain_tokens=0, logger=FakeLog())
    env.context.update_ratio(990)
    result = env.coordinator.compress("x")
    assert result.ok is False and result.reason == "llm_error"
    assert env.logger.texts("error") == ["压缩失败: LLM调用出错"]
    # 历史未变（除自动路径的输入写回）：无摘要消息、原历史逐字保留
    msgs = env.store.view().to_list()
    assert msgs[:3] == history
    assert all(m.get("content") != "[错误: API调用失败]" for m in msgs)


@pytest.mark.parametrize("reason,events,cancel_at", [
    ("interrupted", [], "end"),
    ("interrupted", [{"content": "半截"}], 0),
    ("llm_error", [{"finish_reason": "error"}], None),
    ("empty_summary", [{"content": "   \n"}, {"finish_reason": "stop"}], None),
    ("overflow", [{"finish_reason": "context_overflow"}], None),
], ids=["cancelled_before_request", "cancelled_mid_stream", "llm_error",
        "empty_summary", "overflow"])
def test_scenario22_history_unchanged_on_failure(reason, events, cancel_at):
    """S22：任一失败/中断归因结束时消息序列与压缩前逐字一致（无输入路径）。"""
    history = base_history()
    env = make_env(history, events=events, cancel_at=cancel_at, retain_tokens=0)
    before = copy.deepcopy(env.store.view().to_list())
    result = env.coordinator.compress_no_input()
    assert result.ok is False and result.reason == reason
    assert env.store.view().to_list() == before


def test_cancelled_before_request_no_summary_request():
    """补：请求前已取消（含上一轮残留标记）时不发起摘要请求。"""
    env = make_env(base_history(), retain_tokens=0)
    env.interrupt.flag = True
    result = env.coordinator.compress_no_input()
    assert result.reason == "interrupted"
    assert env.llm.requests == []


def test_cancelled_mid_stream_keeps_history():
    """补：摘要流中被取消 → 归因「用户中断」，摘要不写入历史。"""
    env = make_env(base_history(), events=[{"content": "第一段"}, {"content": "第二段"}],
                   cancel_at=1, retain_tokens=0)
    result = env.coordinator.compress("输入")
    assert result.reason == "interrupted"
    assert all(m.get("content") != "第一段" for m in env.store.view().to_list())


# ═══════════════════════════════════════════════════════════════
# 7. spec Scenario 覆盖（23-25：溢出恢复）
# ═══════════════════════════════════════════════════════════════


def test_scenario23_overflow_recover_success():
    """S23：首次超限 → 压缩历史（无新输入），重建以内部继续指令收尾（原地重发的前提）。"""
    env = make_env(base_history(), retain_tokens=0)
    result = env.coordinator.compress_no_input()
    assert result.ok is True
    msgs = env.store.view().to_list()
    assert user_text(msgs) == OVERFLOW_CONTINUE_MESSAGE
    assert OVERFLOW_CONTINUE_MESSAGE.startswith("[]")  # 兼容怪癖：字面 `[]` 前缀
    assert env.context.ratio is None
    assert env.animator.calls == ["begin", "end"]


def test_scenario24_recover_stateless():
    """S24：「每次运行最多恢复一次」的记账不在本积木——连续调用都会执行压缩（无运行态）。"""
    env = make_env(base_history(), retain_tokens=0)
    assert env.coordinator.compress_no_input().ok is True
    assert env.coordinator.compress_no_input().ok is True
    assert len(env.llm.requests) == 2  # 无「已恢复」记忆，状态归调用方


def test_scenario25_recover_failure():
    """S25：溢出恢复的压缩失败 → 归因明确、占比延后（阈值 − 10），历史不变。"""
    history = base_history()
    env = make_env(history, events=[{"finish_reason": "error"}], retain_tokens=0,
                   logger=FakeLog())
    env.context.update_ratio(990)
    before = copy.deepcopy(env.store.view().to_list())
    result = env.coordinator.compress_no_input()
    assert result.ok is False and result.reason == "llm_error"
    assert env.store.view().to_list() == before
    assert env.context.ratio == 85.0  # 95 − 10


# ═══════════════════════════════════════════════════════════════
# 8. spec Scenario 覆盖（26-28：手动压缩）
# ═══════════════════════════════════════════════════════════════


def test_scenario26_manual_success():
    """S26：成功 → `已压缩 {N} 条历史消息（约 {M} tokens）`，重置占比与告警、恢复输入模式。"""
    env = make_env(base_history(), retain_tokens=0, logger=FakeLog())
    env.context.update_ratio(990)
    env.context.check_warn()  # 先置告警位，验证成功后被清除
    replaced_tokens = sum(estimate_message_tokens(m) for m in base_history()[1:])
    status, text = env.coordinator.compress_manual()
    assert status == "ok"
    assert text == f"已压缩 2 条历史消息（约 {replaced_tokens} tokens）"
    assert env.context.ratio is None
    env.context.update_ratio(600)
    assert env.context.check_warn() == "窗口占比已达50%，建议开启新对话"  # 告警已重置
    assert env.interrupt.mode_calls == ["run", "input"]  # 临时运行模式后恢复输入模式
    assert env.animator.calls == ["begin", "end"]
    assert any("手动压缩完成" in t for t in env.logger.texts("info"))


def test_scenario27_manual_cancel():
    """S27：Esc 取消 → `压缩已取消，历史未变更`，恢复输入等待；占比与历史不动。"""
    env = make_env(base_history(), events=[], cancel_at="end", retain_tokens=0,
                   logger=FakeLog())
    env.context.update_ratio(990)
    before = copy.deepcopy(env.store.view().to_list())
    status, text = env.coordinator.compress_manual()
    assert (status, text) == ("error", "压缩已取消，历史未变更")
    assert env.context.ratio == 99.0  # 手动路径除成功外不动占比
    assert env.store.view().to_list() == before
    assert env.interrupt.mode_calls == ["run", "input"]
    assert "手动压缩被中断" in env.logger.texts("info")


def test_scenario28_manual_empty_text():
    """S28：无可压缩历史 → `没有可压缩的历史对话`（S19 已覆盖判定；此处锁定文案与模式恢复）。"""
    env = make_env(base_history(), retain_tokens=100000)
    assert env.coordinator.compress_manual() == ("empty", "没有可压缩的历史对话")
    assert env.interrupt.mode_calls == ["run", "input"]


def test_manual_fail_texts():
    """手动压缩失败归因 → 用户提示映射（含「压缩请求超限」不与「总结为空」混淆）。"""
    cases_ = [
        ([{"content": "[错误: x]", "finish_reason": "error"}], None,
         "压缩失败: LLM调用出错，历史未变更"),
        ([{"content": "  ", "finish_reason": "stop"}], None,
         "压缩失败: 总结为空，历史未变更"),
        ([{"finish_reason": "context_overflow"}], None,
         "压缩失败: 压缩请求自身超出模型上下文限制，历史未变更"),
    ]
    for events, cancel_at, expected in cases_:
        env = make_env(base_history(), events=events, cancel_at=cancel_at, retain_tokens=0)
        assert env.coordinator.compress_manual() == ("error", expected)
    assert MANUAL_FAIL_TEXT["overflow"] == "压缩失败: 压缩请求自身超出模型上下文限制，历史未变更"


def test_manual_exception_restores_input_mode():
    """手动压缩执行中抛异常 → 异常向上传播，但输入模式与动画收尾必须完成。"""
    env = make_env(base_history(), retain_tokens=0,
                   raise_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        env.coordinator.compress_manual()
    assert env.interrupt.mode_calls == ["run", "input"]
    assert env.animator.calls == ["begin", "end"]


# ═══════════════════════════════════════════════════════════════
# 9. spec Scenario 覆盖（29-30：token 估算口径）
# ═══════════════════════════════════════════════════════════════


def test_scenario29_estimate_cjk_ascii():
    """S29：中文按约 0.7/字、英文按约 0.25/字符，向上取整；空文本为 0。"""
    assert estimate_text_tokens("你好世界") == 3        # 4×0.7=2.8 → 3
    assert estimate_text_tokens("hello world") == 3    # 11×0.25=2.75 → 3
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("（全角）！？") == 5    # 全角符号计入 CJK 系数


def test_scenario30_estimate_message_sum():
    """S30：消息级估值 = 8 + 正文 + 思考 + 各工具调用（名称与参数）。"""
    msg = {
        "role": "assistant",
        "content": "正文",
        "thinking": "思考",
        "tool_calls": [{"function": {"name": "Read", "arguments": '{"x":1}'}}],
    }
    expected = (8
                + estimate_text_tokens("正文")
                + estimate_text_tokens("思考")
                + estimate_text_tokens("Read")
                + estimate_text_tokens('{"x":1}'))
    assert estimate_message_tokens(msg) == expected
    assert estimate_message_tokens({}) == 8                 # 空消息只计框架开销
    assert estimate_message_tokens({"role": "user", "content": None}) == 8
    # content 为块列表时按文本近似（防御路径）
    assert estimate_message_tokens({"content": [{"text": "块"}]}) >= 8


# ═══════════════════════════════════════════════════════════════
# 10. spec Scenario 覆盖（31-32：兼容性怪癖保持）
# ═══════════════════════════════════════════════════════════════


def test_scenario31_threshold_inclusive():
    """S31：占比恰等于压缩阈值 → 判定为需要压缩（闭区间）。"""
    ctx = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95)
    ctx.update_ratio(950)
    assert ctx.ratio == 95.0
    assert ctx.need_compress() is True

    ctx2 = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95)
    ctx2.update_ratio(949)
    assert ctx2.need_compress() is False


def test_scenario32_ratio_after_failure():
    """S32：失败后占比处理——中断重置占比与告警、摘要请求出错置为「阈值 − 10」。"""
    # 摘要请求出错 → set_retry_soon
    env = make_env(base_history(), events=[{"finish_reason": "error"}], retain_tokens=0)
    env.context.update_ratio(990)
    env.coordinator.compress("x")
    assert env.context.ratio == 85.0
    assert env.context.need_compress() is False  # 延后重试：阈值以下

    # 中断 → reset（占比无数据 + 告警标记清除）
    env2 = make_env(base_history(), events=[], cancel_at="end", retain_tokens=0)
    env2.context.update_ratio(990)
    env2.context.check_warn()  # 置告警位
    env2.coordinator.compress("x")
    assert env2.context.ratio is None
    env2.context.update_ratio(600)
    assert env2.context.check_warn() == "窗口占比已达50%，建议开启新对话"  # 可再次告警

    # 溢出路径同一口径
    env3 = make_env(base_history(), events=[{"finish_reason": "error"}], retain_tokens=0)
    env3.context.update_ratio(990)
    env3.coordinator.compress_no_input()
    assert env3.context.ratio == 85.0

    # 手动路径除成功外均不动占比
    env4 = make_env(base_history(), events=[{"finish_reason": "error"}], retain_tokens=0)
    env4.context.update_ratio(990)
    env4.coordinator.compress_manual()
    assert env4.context.ratio == 99.0


def test_retry_soon_floor_zero():
    """补：压缩阈值 < 10 时「阈值 − 10」下限为 0（兼容怪癖边界）。"""
    ctx = CompressionContext(context_window=1000, warn_ratio=5, compress_ratio=5)
    ctx.update_ratio(100)
    ctx.set_retry_soon()
    assert ctx.ratio == 0.0


# ═══════════════════════════════════════════════════════════════
# 11. 占比计算边界（任务书重点矩阵）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("window,input_tokens,expected", [
    (1000, 500, 50.0),
    (1000, 950, 95.0),
    (1000, 0, None),
    (1000, -5, None),
    (0, 500, None),
    (-1, 500, None),
    (1, 1, 100.0),
], ids=["half", "boundary", "zero_input", "negative_input", "zero_window",
        "negative_window", "tiny_window"])
def test_context_ratio_boundaries(window, input_tokens, expected):
    """占比计算边界：窗口 ≤0、输入 0/负值一律「无数据」。"""
    ctx = CompressionContext(context_window=window, warn_ratio=50, compress_ratio=95)
    ctx.update_ratio(input_tokens)
    assert ctx.ratio == expected
    if expected is None:
        assert ctx.need_compress() is False
        assert ctx.check_warn() == ""


def test_context_update_overwrites_with_no_data():
    """占比按快照覆盖：无数据的新快照会清掉旧值（不是保持旧值）。"""
    ctx = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95)
    ctx.update_ratio(950)
    assert ctx.ratio == 95.0
    ctx.update_ratio(0)
    assert ctx.ratio is None


def test_context_warn_logs_once():
    """告警文案固定，且经日志端口记录一次 warning（未注入日志端口时静默）。"""
    log = FakeLog()
    ctx = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95, logger=log)
    ctx.update_ratio(600)
    assert ctx.check_warn() == "窗口占比已达50%，建议开启新对话"
    assert log.calls == [("warning", "compression", "窗口占比已达50%，建议开启新对话")]
    assert ctx.check_warn() == ""
    assert len(log.calls) == 1

    silent = CompressionContext(context_window=1000, warn_ratio=50, compress_ratio=95)
    silent.update_ratio(600)
    assert silent.check_warn() == "窗口占比已达50%，建议开启新对话"  # 无端口不报错


# ═══════════════════════════════════════════════════════════════
# 12. 摘要请求/重建的 fake LLM 编排细节（任务书重点矩阵）
# ═══════════════════════════════════════════════════════════════


def test_summary_collects_text_delta_only():
    """摘要正文只收集正常文本增量：带工具调用字段的块与用量事件不计入。"""
    events = [
        {"content": "忽略我", "tool_calls": [{"id": "c1"}]},  # 带工具调用字段 → 不计入
        {"content": "第一段"},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "cached_tokens": 0}},
        {"content": "第二段"},
        {"finish_reason": "stop"},
    ]
    env = make_env(base_history(), events=events, retain_tokens=0)
    assert env.coordinator.compress_no_input().ok is True
    summary_msg = env.store.view().to_list()[1]
    assert summary_msg["content"] == "# 上一轮对话成果\n\n第一段第二段"


def test_summary_blank_only_is_empty_summary():
    """摘要去除空白后为空 → 归因「总结为空」（不写入历史）。"""
    env = make_env(base_history(), events=[{"content": " \n\t "}, {"finish_reason": "stop"}],
                   retain_tokens=0, logger=FakeLog())
    result = env.coordinator.compress_no_input()
    assert result.reason == "empty_summary"
    assert env.logger.texts("error") == ["压缩失败: 总结为空"]
    assert len(env.store.view().to_list()) == 3


def test_compress_result_replaced_stats():
    """（补充）成功结果统计口径：被替换的对话消息条数与被剔除尾部 user 的估算 token。"""
    dataset = cases.COMPRESSOR_DATASETS["normal"]
    env = make_env(dataset, retain_tokens=0)
    result = env.coordinator.compress_no_input()
    assert result.ok is True
    assert result.replaced == 6  # 对话区 6 条全部被摘要替换
    assert result.tokens == sum(estimate_message_tokens(m) for m in dataset[1:])

    env2 = make_env(dataset, retain_tokens=0)
    status, text = env2.coordinator.compress_manual()
    # 手动路径（retain=0 → 未保留尾部，无尾部 user 可剔除）：替换统计与全量一致
    assert status == "ok"
    assert f"已压缩 {result.replaced} 条历史消息（约 {result.tokens} tokens）" == text


def test_mid_run_guard_logs_and_double_checks():
    """（补充）mid_run_guard 自查日志与幂等双检（未超阈值直接 False 不记录）。"""
    env = make_env(base_history(), retain_tokens=0, logger=FakeLog())
    env.context.update_ratio(900)
    assert env.coordinator.mid_run_guard() is False
    assert env.logger.texts("warning") == []

    env.context.update_ratio(960)
    assert env.coordinator.mid_run_guard() is True
    assert env.logger.texts("warning") == ["运行中占比超阈值，主动压缩历史"]


def test_compress_logs_flow():
    """（补充）压缩流程日志：触发、成功与保留尾部件数。"""
    env = make_env(base_history(), retain_tokens=100000, logger=FakeLog())
    env.coordinator.compress_no_input()
    infos = env.logger.texts("info")
    assert infos[0] == "压缩触发, messages=3条"
    assert infos[-1] == "压缩成功，新会话已创建, 保留尾部=2条"


# ═══════════════════════════════════════════════════════════════
# 13. 基准对照（v2/tests/baseline/data/，旧实现原位生成）
# ═══════════════════════════════════════════════════════════════


def _baseline_group(filename: str, group: str) -> list[dict]:
    path = TESTS_DIR / "baseline" / "data" / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["groups"][group]


@pytest.mark.parametrize("case", cases.COMPRESSOR_CUT_BALANCED_CASES, ids=lambda c: c["id"])
def test_cut_balanced_matches_baseline(case):
    """`_cut_balanced` 与基准逐用例对照（基准组名即 `compressor._cut_balanced`）。"""
    messages = copy.deepcopy(cases.COMPRESSOR_DATASETS[case["dataset"]])
    expected = {c["id"]: c["result"]
                for c in _baseline_group("compressor.json", "compressor._cut_balanced")}
    assert _cut_balanced(messages, case["cut"]) is expected[case["id"]]


@pytest.mark.parametrize("case", cases.COMPRESSOR_SELECT_CUT_CASES, ids=lambda c: c["id"])
def test_select_cut_index_matches_baseline(case):
    """切点选择矩阵与基准逐用例对照（预算边界/回退/异常序列/空历史）。"""
    messages = copy.deepcopy(cases.COMPRESSOR_DATASETS[case["dataset"]])
    expected = {c["id"]: c["result"]
                for c in _baseline_group("compressor.json", "compressor.select_cut_index")}
    assert select_cut_index(messages, case["retain_tokens"]) == expected[case["id"]]


@pytest.mark.parametrize("case", cases.COMPRESSOR_BUILD_CASES, ids=lambda c: c["id"])
def test_compressor_build_matches_baseline(case):
    """摘要请求构建与重建结构对照基准（含旧 `COMPRESS_PROMPT` 文本逐字一致判定）。"""
    dataset = copy.deepcopy(cases.COMPRESSOR_DATASETS[case["dataset"]])
    expected = {c["id"]: c["result"]
                for c in _baseline_group("compressor.json", "compressor.Compressor.build_*")}[case["id"]]
    compressor = Compressor()
    if case["id"] == "compress_msgs":
        built = compressor.build_compress_messages(dataset)
        got = {
            "count": len(built),
            "roles": [m.get("role") for m in built],
            "last_role": built[-1].get("role") if built else None,
            "last_content_len": len(built[-1].get("content", "")) if built else 0,
            "last_content_is_compress_prompt": bool(built) and built[-1].get("content") == COMPRESS_PROMPT,
        }
    else:
        built = compressor.build_new_session_messages(
            case["system_prompt"], case["summary"], dataset[case["tail_from"]:])
        got = {"messages": built}
    assert got == expected


@pytest.mark.parametrize("case", cases.TOKEN_ESTIMATE_MESSAGE_CASES, ids=lambda c: c["id"])
def test_estimate_message_matches_baseline(case):
    """消息级估算与基准 `compressor.estimate_tokens` 组对照（旧转发壳 → `messages.tokens`）。"""
    expected = {c["id"]: c["result"]
                for c in _baseline_group("token_estimate.json", "compressor.estimate_tokens")}
    assert estimate_message_tokens(copy.deepcopy(case["msg"])) == expected[case["id"]]


@pytest.mark.parametrize("case", cases.TOKEN_ESTIMATE_TEXT_CASES, ids=lambda c: c["id"])
def test_estimate_text_matches_shared_baseline(case):
    """文本估算与 `token_estimate.estimate_text_tokens` 组对照（唯一实现经两消费方转发）。"""
    expected = {c["id"]: c["result"]
                for c in _baseline_group("token_estimate.json", "token_estimate.estimate_text_tokens")}
    assert estimate_text_tokens(case["text"]) == expected[case["id"]]


# ═══════════════════════════════════════════════════════════════
# 14. 结构与公开面
# ═══════════════════════════════════════════════════════════════


def _relative_top(path: Path, node: ast.ImportFrom) -> str:
    """把相对导入解析为包内顶层积木名（`from ..config.defaults import X` → config）。"""
    pkg_parts = list(path.relative_to(PACKAGE_DIR.parent).parts[:-1])  # 如 ['compression']
    up = node.level - 1
    base = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
    if node.module:
        base = base + node.module.split(".")
    return base[0]


def test_dependency_purity():
    """compression 只依赖 contracts/config/messages（相对导入）；无绝对导入新包；模块级无小写赋值。"""
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "compression 积木文件缺失"
    allowed = {"contracts", "config", "messages"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    # 绝对导入只允许标准库（__future__ / typing / re / collections.abc / ...）
                    module_top = (node.module or "").split(".")[0]
                    assert module_top != "narnat_agent", (
                        f"{path.name}:{node.lineno} 绝对导入新包：{node.module}")
                    assert module_top in sys.stdlib_module_names, (
                        f"{path.name}:{node.lineno} 非标准库依赖：{node.module}")
                else:
                    top = _relative_top(path, node)
                    assert top in allowed or top == "compression", (
                        f"{path.name}:{node.lineno} 越层依赖 {top}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("narnat_agent"), (
                        f"{path.name}:{node.lineno} 绝对导入新包：{alias.name}")
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("__"):
                        assert target.id.isupper(), (
                            f"{path.name}:{node.lineno} 模块级可变名 {target.id}")


def test_public_exports():
    """聚合导出齐备（`__all__` 全部可解析；溢出继续指令字面 `[]` 前缀）。"""
    import narnat_agent.compression as compression

    for name in compression.__all__:
        assert hasattr(compression, name), name
    assert compression.OVERFLOW_CONTINUE_MESSAGE.startswith("[]")
    assert compression.CompressionContext is CompressionContext
    assert compression.CompressionCoordinator is CompressionCoordinator
    assert compression.CompressResult is CompressResult


def test_estimate_single_implementation():
    """T3.11：估算唯一实现归 `messages.tokens`；本积木只转发（同一函数对象，无本地副本）。"""
    from narnat_agent.messages import tokens as messages_tokens

    assert estimate_text_tokens is messages_tokens.estimate_text_tokens
    assert estimate_message_tokens is messages_tokens.estimate_message_tokens


def test_result_object_contract():
    """结果对象：成功/失败字段形态固定（0 值缺省；成功路径返回统计）。"""
    failed = CompressResult(False, reason="interrupted")
    assert (failed.ok, failed.replaced, failed.tokens) == (False, 0, 0)
    env = make_env(base_history(), retain_tokens=0)
    ok = env.coordinator.compress_no_input()
    assert ok.ok is True and ok.reason == "" and ok.replaced == 2 and ok.tokens > 0
