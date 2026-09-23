"""sessions 积木自测（T4.2）—— spec Scenario 全覆盖 + 状态迁移矩阵 + 基准对照。

覆盖清单（对齐 T4.2 任务书测试要求）：
1. `specs/sessions` 全部 17 Requirement / 70 Scenario（映射表见下）；
2. 状态迁移矩阵（每态每命令的可用性与效果）、`/done` 合并序列、树形文本对照基准、
   删除标记清理；
3. summarizer（LLM 命名/总结）用 fake 注入；
4. 结构验收：无 `_goal_enabled` 等私有互摸、无后置补线、公开导出齐备。

spec Scenario 映射表
════════════════════

| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 会话三态与命令可用性 | 游离态命令集 | `test_scenario01_free_state_commands` |
| 2 | 会话三态与命令可用性 | 根会话态命令集 | `test_scenario02_root_state_commands` |
| 3 | 会话三态与命令可用性 | 子会话态命令集 | `test_scenario03_child_state_commands` |
| 4 | 会话三态与命令可用性 | 不可用命令按未知处理 | `test_scenario04_unavailable_as_unknown` |
| 5 | 会话三态与命令可用性 | 绕过状态校验 | `test_scenario05_clear_goal_bypass_state` |
| 6 | 保存会话（/save） | 游离态空名自动命名 | `test_scenario06_free_auto_named_save` |
| 7 | 保存会话（/save） | 无对话内容 | `test_scenario07_no_user_message` |
| 8 | 保存会话（/save） | 命名失败 | `test_scenario08_name_failure` |
| 9 | 保存会话（/save） | 保存失败不切换状态 | `test_scenario09_save_error_keeps_state` |
| 10 | 保存会话（/save） | 根会话刷新与另存为 | `test_scenario10_root_refresh_and_save_as` |
| 11 | 进入历史会话（/cd） | 进入根会话 | `test_scenario11_enter_root` |
| 12 | 进入历史会话（/cd） | 进入子会话 | `test_scenario12_enter_child` |
| 13 | 进入历史会话（/cd） | 切换前落盘 | `test_scenario13_persist_before_switch` |
| 14 | 进入历史会话（/cd） | 缺参与加载失败 | `test_scenario14_cd_missing_or_error` |
| 15 | 会话列表（/ls） | 默认精简列表 | `test_scenario15_summary_list` |
| 16 | 会话列表（/ls） | 当前会话追加显示 | `test_scenario16_current_appended` |
| 17 | 会话列表（/ls） | 全量树形 | `test_scenario17_full_tree` |
| 18 | 会话列表（/ls） | 游离子会话的占位父行 | `test_scenario18_orphan_child_placeholder` |
| 19 | 会话列表（/ls） | 空列表 | `test_scenario19_empty_list` |
| 20 | 删除标记（/rm） | 游离态标记与根连带子 | `test_scenario20_free_state_mark_cascade` |
| 21 | 删除标记（/rm） | 根会话态删除子会话 | `test_scenario21_root_marks_child_only` |
| 22 | 删除标记（/rm） | --all 语义随状态 | `test_scenario22_all_semantics_by_state` |
| 23 | 删除标记（/rm） | 重复标记幂等 | `test_scenario23_mark_idempotent` |
| 24 | 退出语义（/exit） | 游离态退出程序 | `test_scenario24_free_exit_program` |
| 25 | 退出语义（/exit） | 根会话退出 | `test_scenario25_root_exit_ruling` |
| 26 | 退出语义（/exit） | 子会话暂离 | `test_scenario26_child_detach` |
| 27 | 退出语义（/exit） | 暂离失败 | `test_scenario27_detach_failure` |
| 28 | 手动压缩的会话侧收尾（/compact） | 压缩成功先重建基准再落盘 | `test_scenario28_compact_rebuild_then_persist` |
| 29 | 手动压缩的会话侧收尾（/compact） | 无可压缩历史 | `test_scenario29_compact_empty_no_persist` |
| 30 | 会话文件布局与原子写 | 根会话与子会话落点 | `test_scenario30_file_layout` |
| 31 | 会话文件布局与原子写 | 文件名安全替换 | `test_scenario31_safe_filename` |
| 32 | 会话文件布局与原子写 | 原子写 | `test_scenario32_atomic_write` |
| 33 | 会话文件布局与原子写 | 孤立代理字符清洗 | `test_scenario33_surrogate_cleaning` |
| 34 | 会话文件布局与原子写 | 读取失败文案 | `test_scenario34_load_error_text` |
| 35 | 探索分支创建（/explore） | 创建分支 | `test_scenario35_create_branch` |
| 36 | 探索分支创建（/explore） | 空名与同名 | `test_scenario36_explore_missing_or_same` |
| 37 | 探索分支合并（/done） | 首次合并 | `test_scenario37_first_merge` |
| 38 | 探索分支合并（/done） | 无新讨论 | `test_scenario38_no_new_discussion` |
| 39 | 探索分支合并（/done） | 总结取消或失败 | `test_scenario39_summary_cancel_fail` |
| 40 | 探索分支合并（/done） | 重复 /done | `test_scenario40_repeat_done` |
| 41 | 探索分支合并（/done） | 多轮合并 | `test_scenario41_multi_round_merge` |
| 42 | 探索分支合并（/done） | 父会话加载失败 | `test_scenario42_parent_load_failure` |
| 43 | 技能加载（/skill） | 加载系统技能 | `test_scenario43_load_system_skill` |
| 44 | 技能加载（/skill） | 加载项目技能层级路径 | `test_scenario44_load_project_skill_path` |
| 45 | 技能加载（/skill） | 技能不存在 | `test_scenario45_skill_not_found` |
| 46 | 技能加载（/skill） | 技能树补全 | `test_scenario46_skill_tree` |
| 47 | 思考与模型配置命令 | 思考强度查询与切换 | `test_scenario47_thinking_query_switch` |
| 48 | 思考与模型配置命令 | 思考强度非法值 | `test_scenario48_thinking_invalid` |
| 49 | 思考与模型配置命令 | 思考回传切换 | `test_scenario49_thinkback_switch` |
| 50 | 思考与模型配置命令 | 模型切换 | `test_scenario50_mode_switch` |
| 51 | 思考与模型配置命令 | 配置写回失败静默 | `test_scenario51_config_write_silent` |
| 52 | 目标模式命令（/goal） | 开启带轮数 | `test_scenario52_goal_on_with_rounds` |
| 53 | 目标模式命令（/goal） | 开启与关闭 | `test_scenario53_goal_on_off` |
| 54 | 目标模式命令（/goal） | 非法轮数 | `test_scenario54_goal_invalid_rounds` |
| 55 | 目标模式命令（/goal） | 查看状态 | `test_scenario55_goal_status` |
| 56 | 会话名解析 | 完整名与全路径匹配 | `test_scenario56_resolve_full_match` |
| 57 | 会话名解析 | 含 "/" 直接拆分 | `test_scenario57_resolve_split` |
| 58 | 会话名解析 | 裸名唯一匹配 | `test_scenario58_resolve_unique_child` |
| 59 | 会话名解析 | 多匹配与不存在 | `test_scenario59_resolve_ambiguous_missing` |
| 60 | 延迟删除执行与展示标记 | 退出时执行删除 | `test_scenario60_cleanup_executes_deletes` |
| 61 | 延迟删除执行与展示标记 | 展示标记 | `test_scenario61_list_marks` |
| 62 | 自动保存与退出保存 | 门槛不满足不触发 | `test_scenario62_auto_save_gates` |
| 63 | 自动保存与退出保存 | Token 门槛 | `test_scenario63_auto_save_token_threshold` |
| 64 | 自动保存与退出保存 | 自动保存成功后切换 | `test_scenario64_auto_save_then_switch` |
| 65 | 自动保存与退出保存 | 退出保存 | `test_scenario65_exit_save` |
| 66 | 兼容性怪癖保持 | 拆分不校验（兼容怪癖） | `test_scenario66_split_without_validation` |
| 67 | 兼容性怪癖保持 | 已合并位置 0 回退（兼容怪癖） | `test_scenario67_summarized_at_zero_fallback` |
| 68 | 兼容性怪癖保持 | 列表比较与着色误判（兼容怪癖） | `test_scenario68_list_compare_and_color_quirk` |
| 69 | 兼容性怪癖保持 | 落盘失败被忽略（兼容怪癖） | `test_scenario69_persist_failure_ignored` |
| 70 | 兼容性怪癖保持 | 分支文件重读忽略错误（兼容怪癖） | `test_scenario70_child_reload_error_ignored` |

规格偏差裁决记录（本实现 vs spec 字面，附证据）
═══════════════════════════════════════════════

- Scenario 25「根会话退出」：spec 写作「回到游离态，输出「已退出会话」，进程不结束」。
  旧实现实测（`v2/tests` 探针）为：根会话 /exit → 状态回游离态 → `should_exit_agent()`
  为真 → 命令返回退出语义（进程结束）；「已退出会话」提示为不可达死代码。
  `specs/ui` 的同名 Scenario（「/exit 退出语义」）与旧实现一致（「在根会话执行 /exit
  → 命令返回退出语义，进程在清理后结束」），design D11 要求行为等价（用户无感知）。
  三份证据（旧实现实测 / ui spec / D11）指向同一行为，故本实现取「退出语义」，
  并在 `test_scenario25_root_exit_ruling` 中锁定；spec 该处单点表述未采用。

其它覆盖（非 Scenario 的验收项）
════════════════════════════════
- 状态迁移矩阵：`test_state_matrix_command_availability`（每态每命令的可用性与副作用）、
  `test_state_matrix_transitions`（三态迁移与 /cd 直达子会话）；
- `/done` 合并序列：`test_done_sequence_memory_target_boundary`（memory/target 分界、
  多轮累积）、`test_done_sequence_reset_after_compact`（压缩后基准重建）、
  `test_done_summary_template_sections`（固定小节与规则）、
  `test_done_summary_request_shape`（单条 user 消息）、
  `test_done_legacy_boundary_marker`（历史「━━━」标记兜底）；
- 命令层细节：`test_command_name_normalization`（命令名归一）、
  `test_command_reply_styling`（着色与缩进契约）、`test_persist_current_round_end`
  （轮末落盘）、`test_save_auto_name_without_name_func`、
  `test_completion_candidates`（Tab 补全数据源）；
- 基准对照：`test_baseline_safe_filename` / `test_baseline_format_session_tree` /
  `test_baseline_format_session_summary` / `test_baseline_format_session_list`
  （`v2/tests/baseline/data/session_store.json` 全部 4 组）＋
  `test_baseline_tree_end_to_end`（真实落盘目录 → 树 → 文本的端到端对照）；
- 结构验收：`test_structure_no_private_field_cross_access`（验收项 5 的机械证据）、
  `test_structure_public_api_present`、`test_structure_no_module_level_mutable_state`、
  `test_structure_chinese_docstrings`、`test_structure_public_exports`。
"""
from __future__ import annotations

import ast
import copy
import json
import os
import re
from pathlib import Path

import pytest

from narnat_agent.config.models import AIConfig
from narnat_agent.messages import MessageStore
from narnat_agent.sessions import (
    AUTO_SAVE_WAIT_SECONDS,
    BOUNDARY_MARKER_PREFIX,
    SUMMARY_TASK_TEMPLATE,
    ChildSession,
    CommandKind,
    GoalMode,
    ModelState,
    NoSession,
    PlainColors,
    RootSession,
    SessionManager,
    SessionStore,
    clean_surrogates,
    format_session_list,
    format_session_summary,
    format_session_tree,
    safe_filename,
)

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "sessions"
BASELINE_FILE = (
    Path(__file__).resolve().parents[1] / "baseline" / "data" / "session_store.json"
)

# 时间戳哨兵（对齐 cases.py）：未来值 → 永远「今天」；epoch 0 → 永远「更早」
TS_FUTURE = 4102444800
TS_EPOCH = 0

# 时间文本遮罩（对齐 v2/tests/baseline/extract_old.py 的规范化约定）
_TIME_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"), "<YYYY-MM-DD HH:MM>"),
    (re.compile(r"\d{2}-\d{2} \d{2}:\d{2}"), "<MM-DD HH:MM>"),
    (re.compile(r"\d{2}:\d{2}"), "<HH:MM>"),
    (re.compile(r"\d{2}-\d{2}"), "<MM-DD>"),
]


# ═══════════════════════════════════════════════════════════════
# 工具与替身
# ═══════════════════════════════════════════════════════════════


class FakeTheme:
    """着色替身：以 `[S]` `[E]` 等标记替换 ANSI，便于断言着色分段。"""

    cmd_success = "[S]"
    cmd_error = "[E]"
    cmd_hint = "[H]"
    cmd_highlight = "[C]"
    cmd_muted = "[M]"
    r = "[R]"


class FakeAnimator:
    """合并动画替身：记录 begin/end 调用序列。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_summarizing(self) -> None:
        self.calls.append("begin_summarizing")

    def end_summarizing(self) -> None:
        self.calls.append("end_summarizing")

    def begin_compressing(self) -> None:
        self.calls.append("begin_compressing")

    def end_compressing(self) -> None:
        self.calls.append("end_compressing")


class FakeToolGate:
    """目标完成标记工具注入替身：记录开关序列。"""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, enabled: bool) -> None:
        self.calls.append(enabled)


def mask_time(text: str) -> str:
    """把时间文本替换为占位符（与旧基准的规范化一致）。"""
    for pattern, placeholder in _TIME_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def build_manager(tmp_path: Path, *, messages: MessageStore | None = None,
                  name_func=None, summarize_func=None, compact_func=None,
                  animator=None, theme=None, model_state=None, goal=None,
                  auto_save_enabled=False, auto_save_tokens=0,
                  skill_roots=None, skill_ignore_dirs=()):
    """构造一套会话管理器（真实存储与消息历史 + 假注入端口）。"""
    msgs = messages if messages is not None else MessageStore("S")
    store = SessionStore(str(tmp_path))
    mgr = SessionManager(
        store, msgs,
        name_func=name_func,
        summarize_func=summarize_func,
        compact_func=compact_func,
        animator=animator,
        theme=theme,
        model_state=model_state,
        goal=goal,
        auto_save_enabled=auto_save_enabled,
        auto_save_tokens=auto_save_tokens,
        skill_roots=skill_roots,
        skill_ignore_dirs=skill_ignore_dirs,
    )
    return mgr, msgs, store


def make_session(store: SessionStore, name: str, parent=None, status="active",
                 messages=None, timestamp=None, summary=None,
                 parent_msg_count=None, last_summarized_at=None) -> list:
    """写一个会话文件（可覆盖 timestamp，用于构造「更早」分组）。"""
    msgs = messages if messages is not None else [{"role": "user", "content": "hi"}]
    store.save(name, msgs, parent=parent, status=status, summary=summary,
               parent_msg_count=parent_msg_count, last_summarized_at=last_summarized_at)
    if timestamp is not None:
        path = store.path(name, parent=parent)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["timestamp"] = timestamp
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return msgs


def read_session(store: SessionStore, name: str, parent=None) -> dict:
    """读会话文件原始内容。"""
    return json.loads(Path(store.path(name, parent=parent)).read_text(encoding="utf-8"))


def plain(reply) -> str:
    """命令输出的可见文本（去掉命令行的两空格缩进与首尾空白）。"""
    return reply.text.strip()


def list_text(reply) -> str:
    """列表类输出的原样文本（保留行首缩进，仅去首尾换行与空白）。"""
    return reply.text.strip("\n").rstrip()


def file_bytes(store: SessionStore, name: str, parent=None) -> bytes:
    return Path(store.path(name, parent=parent)).read_bytes()


def enter_root(mgr: SessionManager, name: str) -> None:
    """让管理器进入指定根会话（替代 /exit 的进程退出语义，供测试编排）。"""
    err = mgr.state.enter(name)
    assert err == ""


def enter_child(mgr: SessionManager, name: str, parent: str) -> None:
    err = mgr.state.enter(f"{parent}/{name}")
    assert err == ""


# ═══════════════════════════════════════════════════════════════
# 1. 会话三态与命令可用性（场景 1-5）
# ═══════════════════════════════════════════════════════════════


def test_scenario01_free_state_commands(tmp_path):
    """场景 1：游离态命令集（12 条；/rm 说明「删除会话」、/exit 说明「退出程序」）。"""
    mgr, _, _ = build_manager(tmp_path)
    cmds = mgr.available_commands()
    assert list(cmds) == [
        "/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
        "/thinking", "/thinkback", "/mode", "/goal", "/exit",
    ]
    assert cmds["/rm"] == "删除会话"
    assert cmds["/exit"] == "退出程序"
    assert cmds["/clear"] == "清理屏幕"


def test_scenario02_root_state_commands(tmp_path):
    """场景 2：根会话态增加 /explore，/rm 变为「删除子会话」、/exit 变为「退出会话」。"""
    mgr, msgs, _ = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    cmds = mgr.available_commands()
    assert "/explore" in cmds
    assert cmds["/explore"] == "创建探索分支"
    assert cmds["/rm"] == "删除子会话"
    assert cmds["/exit"] == "退出会话"
    assert list(cmds) == [
        "/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
        "/thinking", "/thinkback", "/mode", "/goal", "/explore", "/exit",
    ]


def test_scenario03_child_state_commands(tmp_path):
    """场景 3：子会话态无 /save /rm /explore，新增 /done 与 /exit（暂离探索分支）。"""
    mgr, msgs, _ = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    cmds = mgr.available_commands()
    assert "/save" not in cmds and "/rm" not in cmds and "/explore" not in cmds
    assert cmds["/done"] == "完成探索分支"
    assert cmds["/exit"] == "暂离探索分支"
    assert list(cmds) == [
        "/clear", "/compact", "/ls", "/cd", "/skill", "/thinking",
        "/thinkback", "/mode", "/goal", "/done", "/exit",
    ]


def test_scenario04_unavailable_as_unknown(tmp_path):
    """场景 4：不在可用集内的命令按未知处理、不执行、无副作用。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    before = mgr.message_list()
    reply = mgr.dispatch("/save")
    assert reply.kind == CommandKind.UNKNOWN and reply.text == ""
    assert mgr.message_list() == before and isinstance(mgr.state, ChildSession)
    # 游离态 /explore
    mgr2, _, store2 = build_manager(tmp_path / "t2", name_func=lambda m: "A")
    reply = mgr2.dispatch("/explore x")
    assert reply.kind == CommandKind.UNKNOWN and reply.text == ""
    assert store2.list_tree() == [] and isinstance(mgr2.state, NoSession)


def test_scenario05_clear_goal_bypass_state(tmp_path):
    """场景 5：/clear 与 /goal 不经过状态校验（任何状态可执行）。"""
    mgr, msgs, _ = build_manager(tmp_path, name_func=lambda m: "A")
    # 游离态
    assert mgr.dispatch("/clear").kind == CommandKind.HANDLED
    assert mgr.dispatch("/goal on").kind == CommandKind.HANDLED
    assert mgr.goal.enabled is True
    # 根会话态
    msgs.append_user("hi")
    mgr.dispatch("/save")
    assert mgr.dispatch("/goal").kind == CommandKind.HANDLED
    assert mgr.dispatch("/clear").kind == CommandKind.HANDLED
    # 子会话态
    mgr.dispatch("/explore b")
    assert mgr.dispatch("/goal off").kind == CommandKind.HANDLED
    assert mgr.goal.enabled is False
    assert mgr.dispatch("/clear").kind == CommandKind.HANDLED


# ═══════════════════════════════════════════════════════════════
# 2. 保存会话（场景 6-10）
# ═══════════════════════════════════════════════════════════════


def test_scenario06_free_auto_named_save(tmp_path):
    """场景 6：游离态空名 /save 自动命名成功 → 落盘、进入根会话态、输出「会话已保存: 名」。"""
    seen: list[list] = []
    mgr, msgs, store = build_manager(
        tmp_path, name_func=lambda m: (seen.append(m), "生成名")[1])
    msgs.append_user("你好")
    reply = mgr.dispatch("/save")
    assert reply.kind == CommandKind.HANDLED
    assert plain(reply) == "会话已保存: 生成名"
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "生成名"
    assert store.load("生成名")[0] == mgr.message_list()
    assert seen and seen[0] == mgr.message_list()  # 命名基于当前对话


def test_scenario07_no_user_message(tmp_path):
    """场景 7：无任何用户消息 → 「没有对话内容，无需保存」，状态不变。"""
    mgr, _, store = build_manager(tmp_path, name_func=lambda m: "生成名")
    reply = mgr.dispatch("/save")
    assert plain(reply) == "没有对话内容，无需保存"
    assert isinstance(mgr.state, NoSession) and store.list_tree() == []


def test_scenario08_name_failure(tmp_path):
    """场景 8：空名且自动命名未获得名称 → 命名失败提示，状态不变。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "")
    msgs.append_user("你好")
    reply = mgr.dispatch("/save")
    assert plain(reply) == "自动命名失败，请手动指定: /save <名称>"
    assert isinstance(mgr.state, NoSession) and store.list_tree() == []


def test_scenario09_save_error_keeps_state(tmp_path, monkeypatch):
    """场景 9：落盘返回错误 → 错误原样输出，仍停留原状态（游离态与根会话另存）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("你好")
    monkeypatch.setattr(store, "save", lambda *a, **k: "保存失败: 磁盘错误")
    reply = mgr.dispatch("/save")
    assert plain(reply) == "保存失败: 磁盘错误"
    assert isinstance(mgr.state, NoSession)
    # 根会话态另存为失败
    monkeypatch.undo()
    mgr.dispatch("/save")
    monkeypatch.setattr(store, "save", lambda *a, **k: "保存失败: 磁盘错误")
    reply = mgr.dispatch("/save 新名")
    assert plain(reply) == "保存失败: 磁盘错误"
    assert mgr.session_name() == "A"
    # 根会话刷新失败（空名）
    reply = mgr.dispatch("/save")
    assert plain(reply) == "保存失败: 磁盘错误" and mgr.session_name() == "A"


def test_scenario10_root_refresh_and_save_as(tmp_path):
    """场景 10：根会话 /save 空名/同名 → 刷新落盘、状态不变；新名 → 另存并切入。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("你好")
    mgr.dispatch("/save")
    msgs.append_user("追加一句")
    reply = mgr.dispatch("/save")
    assert plain(reply) == "会话已保存: A" and mgr.session_name() == "A"
    assert read_session(store, "A")["messages"] == msgs.view().to_list()
    reply = mgr.dispatch("/save A")  # 同名 → 刷新
    assert plain(reply) == "会话已保存: A"
    msgs.append_user("再追加")
    reply = mgr.dispatch("/save 副本")
    assert plain(reply) == "会话已保存: 副本" and mgr.session_name() == "副本"
    assert read_session(store, "副本")["messages"] == msgs.view().to_list()
    assert read_session(store, "A")["messages"] != msgs.view().to_list()


# ═══════════════════════════════════════════════════════════════
# 3. 进入历史会话（场景 11-14）
# ═══════════════════════════════════════════════════════════════


def test_scenario11_enter_root(tmp_path):
    """场景 11：/cd 进入根会话 → 内容替换当前对话、进入根会话态、输出「已进入会话」。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    saved = [{"role": "system", "content": "S"}, {"role": "user", "content": "旧对话"}]
    make_session(store, "A", messages=saved)
    reply = mgr.dispatch("/cd A")
    assert plain(reply) == "已进入会话: A"
    assert mgr.message_list() == saved and isinstance(mgr.state, RootSession)


def test_scenario12_enter_child(tmp_path):
    """场景 12：/cd 指向 父/子 → 子会话内容替换对话、进入子会话态。"""
    mgr, msgs, store = build_manager(tmp_path)
    child_msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "分支"}]
    make_session(store, "父", messages=[{"role": "system", "content": "S"}])
    make_session(store, "子", parent="父", messages=child_msgs, status="new",
                 parent_msg_count=1, last_summarized_at=1)
    reply = mgr.dispatch("/cd 父/子")
    assert plain(reply) == "已进入会话: 父/子"
    assert mgr.message_list() == child_msgs
    assert isinstance(mgr.state, ChildSession) and mgr.is_child_session() is True
    # 裸名唯一匹配亦可
    mgr2, _, _ = build_manager(tmp_path / "t2", messages=MessageStore("S"))
    make_session(SessionStore(str(tmp_path / "t2")), "父2", messages=[{"role": "system", "content": "S"}])
    make_session(SessionStore(str(tmp_path / "t2")), "子2", parent="父2", messages=child_msgs)
    reply = mgr2.dispatch("/cd 子2")
    assert plain(reply) == "已进入会话: 子2" and isinstance(mgr2.state, ChildSession)


def test_scenario13_persist_before_switch(tmp_path):
    """场景 13：根/子会话态 /cd 前先落盘当前会话（修改不丢失）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("你好")
    mgr.dispatch("/save")          # A
    make_session(store, "B", messages=[{"role": "system", "content": "S"}])
    msgs.append_user("切走前的修改")
    mgr.dispatch("/cd B")
    assert [m["content"] for m in read_session(store, "A")["messages"]] == \
        ["S", "你好", "切走前的修改"]
    # 子会话态同样先落盘
    child = MessageStore("S")
    mgr2, _, store2 = build_manager(tmp_path / "t2", messages=child, name_func=lambda m: "P")
    child.append_user("父")
    mgr2.dispatch("/save")
    mgr2.dispatch("/explore b")
    child.append_user("分支讨论")
    make_session(store2, "Q", messages=[{"role": "system", "content": "S"}])
    mgr2.dispatch("/cd Q")
    assert [m["content"] for m in read_session(store2, "b", parent="P")["messages"]] == \
        ["S", "父", "分支讨论"]


def test_scenario14_cd_missing_or_error(tmp_path):
    """场景 14：/cd 缺参 → 用法提示；加载失败 → 「会话不存在: 名」，状态不变。"""
    mgr, _, store = build_manager(tmp_path)
    reply = mgr.dispatch("/cd")
    assert reply.kind == CommandKind.HANDLED and plain(reply) == "用法: /cd <名称>"
    make_session(store, "A")
    reply = mgr.dispatch("/cd 不存在")
    assert plain(reply) == "会话不存在: 不存在"
    assert isinstance(mgr.state, NoSession)
    reply = mgr.dispatch("/cd A/子")   # 含 "/" 直接拆分 → 加载阶段报错（兼容怪癖）
    assert plain(reply) == "会话不存在: 子"
    assert isinstance(mgr.state, NoSession)


# ═══════════════════════════════════════════════════════════════
# 4. 会话列表（场景 15-19）
# ═══════════════════════════════════════════════════════════════


def test_scenario15_summary_list(tmp_path):
    """场景 15：默认精简列表——今天全部列出（HH:MM），更早最多 3 个（MM-DD）＋剩余提示。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "今天甲")
    make_session(store, "早1", timestamp=TS_EPOCH + 400)
    make_session(store, "早2", timestamp=TS_EPOCH + 300)
    text = list_text(mgr.dispatch("/ls"))
    assert "  今天 (1)" in text and "  更早 (2)" in text
    assert "今天甲" in text and "早1" in text and "早2" in text
    assert re.search(r"今天甲  \(\d{2}:\d{2}, 1条\)", text)
    assert re.search(r"早1  \(\d{2}-\d{2}, 1条\)", text)
    assert "还有" not in text
    # 更早超过 3 个 → 提示剩余
    for i in range(3):
        make_session(store, f"早额外{i}", timestamp=TS_EPOCH + 200 - i * 10)
    text = list_text(mgr.dispatch("/ls"))
    assert "  更早 (5)" in text
    assert "… 还有 2 条更早的会话 (/ls --all 查看)" in text
    assert len([l for l in text.splitlines() if l.startswith("  ├──") or l.startswith("  └──")]) == 4


def test_scenario16_current_appended(tmp_path):
    """场景 16：当前会话属于更早分组且不在前 3 个 → 追加显示（避免看不见自己）。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "早1", timestamp=TS_EPOCH + 500)
    make_session(store, "早2", timestamp=TS_EPOCH + 400)
    make_session(store, "早3", timestamp=TS_EPOCH + 300)
    make_session(store, "早4", timestamp=TS_EPOCH + 200)
    make_session(store, "早5当前", timestamp=TS_EPOCH + 100)
    enter_root(mgr, "早5当前")
    text = list_text(mgr.dispatch("/ls"))
    assert "早5当前" in text and "◀ 当前" in text
    assert "… 还有 1 条更早的会话 (/ls --all 查看)" in text


def test_scenario17_full_tree(tmp_path):
    """场景 17：/ls --all 全量树形（父按时间倒序、子按时间升序；三种子状态文案）。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "父A", timestamp=TS_FUTURE,
                 messages=[{"role": "user", "content": "a"}] * 12)
    make_session(store, "子完成", parent="父A", status="completed",
                 timestamp=TS_FUTURE - 300)
    make_session(store, "子新", parent="父A", status="new", timestamp=TS_FUTURE - 200,
                 messages=[{"role": "user", "content": "b"}] * 5)
    make_session(store, "子进行", parent="父A", status="active", timestamp=TS_FUTURE - 100,
                 messages=[{"role": "user", "content": "c"}] * 7)
    make_session(store, "父B", timestamp=TS_EPOCH, messages=[{"role": "user", "content": "d"}] * 2)
    text = list_text(mgr.dispatch("/ls --all"))
    assert "✓ 已完成" in text and "⚠ 待完成" in text and "(<" not in text
    lines = text.splitlines()
    assert lines[0].startswith("  ├── 父A  (") and "12条)" in lines[0]
    assert "子完成" in lines[1] and "子新" in lines[2] and "子进行" in lines[3]
    assert "├── 子完成" in lines[1] and "└── 子进行" in lines[3]
    assert lines[4].startswith("  └── 父B  (") and "2条)" in lines[4]
    # 游离态末尾追加占位当前行
    assert lines[-1] == "   ◉  ◀ 当前"


def test_scenario18_orphan_child_placeholder(tmp_path):
    """场景 18：父文件缺失的游离子会话 → 以父名合成占位根行（时间与条数为 0）。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "孤儿", parent="缺失父", status="new", timestamp=TS_EPOCH + 10)
    text = list_text(mgr.dispatch("/ls --all"))
    assert "缺失父  (" in text and "0条)" in text
    assert "孤儿" in text


def test_scenario19_empty_list(tmp_path):
    """场景 19：无已保存会话且无待删除标记 → 「(无已保存会话)」。"""
    mgr, _, _ = build_manager(tmp_path)
    reply = mgr.dispatch("/ls")
    assert reply.kind == CommandKind.HANDLED and plain(reply) == "(无已保存会话)"
    reply = mgr.dispatch("/ls --all")
    assert plain(reply) == "(无已保存会话)"


# ═══════════════════════════════════════════════════════════════
# 5. 删除标记（场景 20-23）
# ═══════════════════════════════════════════════════════════════


def test_scenario20_free_state_mark_cascade(tmp_path):
    """场景 20：游离态 /rm <根> → 该根与其全部子会话均被标记。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "A")
    make_session(store, "b1", parent="A")
    make_session(store, "b2", parent="A")
    make_session(store, "B")
    reply = mgr.dispatch("/rm A")
    assert plain(reply) == "已标记删除: A  (退出agent时生效)"
    assert mgr.pending_deletes == {("A", None), ("b1", "A"), ("b2", "A")}


def test_scenario21_root_marks_child_only(tmp_path):
    """场景 21：根会话态只能标记自己的子会话；父名不符 → 「不是当前会话的子会话」。"""
    mgr, _, store = build_manager(tmp_path, name_func=lambda m: "A")
    make_session(store, "A")
    make_session(store, "b1", parent="A")
    make_session(store, "B")
    make_session(store, "b2", parent="B")
    enter_root(mgr, "A")
    reply = mgr.dispatch("/rm b1")
    assert plain(reply) == "已标记删除: b1  (退出agent时生效)"
    assert mgr.pending_deletes == {("b1", "A")}
    reply = mgr.dispatch("/rm A/b1")   # 父名等于当前会话 → 允许
    assert plain(reply) == "已标记删除: A/b1  (退出agent时生效)"
    reply = mgr.dispatch("/rm B/b2")   # 父名不是当前会话
    assert plain(reply) == "'B/b2' 不是当前会话的子会话"
    reply = mgr.dispatch("/rm b2")     # 不是当前会话的子会话
    assert plain(reply) == "'b2' 不是当前会话的子会话"


def test_scenario22_all_semantics_by_state(tmp_path):
    """场景 22：/rm --all 语义随状态（游离态=全部；根会话态=仅当前会话的子会话）。"""
    mgr, _, store = build_manager(tmp_path, name_func=lambda m: "A")
    make_session(store, "A")
    make_session(store, "a1", parent="A")
    make_session(store, "B")
    make_session(store, "b1", parent="B")
    mgr.dispatch("/rm --all")
    assert mgr.pending_deletes == {("A", None), ("a1", "A"), ("B", None), ("b1", "B")}
    mgr.pending_deletes.clear()
    enter_root(mgr, "A")
    mgr.dispatch("/rm --all")
    assert mgr.pending_deletes == {("a1", "A")}
    # 当前会话不在会话树中（文件与子目录被删）→ 「会话不存在」
    mgr.pending_deletes.clear()
    store.delete("A")
    reply = mgr.dispatch("/rm --all")
    assert plain(reply) == "会话不存在"
    reply = mgr.dispatch("/rm 任意")
    assert plain(reply) == "会话不存在: 任意"


def test_scenario23_mark_idempotent(tmp_path):
    """场景 23：同一次运行内重复 /rm 同一会话 → 标记集合去重、无额外效果。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "A")
    make_session(store, "b1", parent="A")
    mgr.dispatch("/rm A")
    mgr.dispatch("/rm A")
    mgr.dispatch("/rm b1")
    assert mgr.pending_deletes == {("A", None), ("b1", "A")}


# ═══════════════════════════════════════════════════════════════
# 6. 退出语义（场景 24-27）
# ═══════════════════════════════════════════════════════════════


def test_scenario24_free_exit_program(tmp_path):
    """场景 24：游离态 /exit → 退出程序（分发返回退出语义 + 应退出判定为真）。"""
    mgr, _, _ = build_manager(tmp_path)
    reply = mgr.dispatch("/exit")
    assert reply.kind == CommandKind.EXIT and mgr.should_exit_agent() is True
    assert isinstance(mgr.state, NoSession)


def test_scenario25_root_exit_ruling(tmp_path):
    """场景 25（规格偏差裁决）：根会话 /exit → 退出语义（状态回游离态，进程结束）。

    裁决依据见文件头「规格偏差裁决记录」：旧实现实测、`specs/ui` 同名 Scenario 与
    design D11 行为等价要求三者一致；spec 本处「进程不结束」表述不采用。
    """
    mgr, _, store = build_manager(tmp_path, name_func=lambda m: "A")
    make_session(store, "A")
    enter_root(mgr, "A")
    reply = mgr.dispatch("/exit")
    assert reply.kind == CommandKind.EXIT and mgr.should_exit_agent() is True
    assert isinstance(mgr.state, NoSession)
    assert "已退出会话" not in reply.text  # 旧实现的「已退出会话」提示为不可达死代码


def test_scenario26_child_detach(tmp_path):
    """场景 26：子会话 /exit → 暂离（父内容替换对话、回父根会话态、暂离提示）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("父内容")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    msgs.append_user("分支讨论")
    reply = mgr.dispatch("/exit")
    assert reply.kind == CommandKind.HANDLED
    assert plain(reply) == "已暂离探索分支  (/cd 回来继续)"
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "A"
    assert [m["content"] for m in mgr.message_list()] == ["S", "父内容"]


def test_scenario27_detach_failure(tmp_path):
    """场景 27：暂离失败 → 返回加载错误且停留子会话态（现状提示并存保持）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("父内容")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    msgs.append_user("分支讨论")
    Path(store.path("A")).unlink()
    reply = mgr.dispatch("/exit")
    assert reply.kind == CommandKind.HANDLED
    lines = reply.text.splitlines()
    assert "会话不存在: A" in lines[0]
    assert "已暂离探索分支  (/cd 回来继续)" in lines[1]  # 旧实现同样打印（行为等价）
    assert isinstance(mgr.state, ChildSession)


# ═══════════════════════════════════════════════════════════════
# 7. 手动压缩的会话侧收尾（场景 28-29）
# ═══════════════════════════════════════════════════════════════


def test_scenario28_compact_rebuild_then_persist(tmp_path):
    """场景 28：压缩成功 → 先按压缩后的对话重建子会话基准，随后落盘当前会话。"""
    holder = {}

    def compact():
        # 模拟压缩把消息列表整体替换为 [系统提示词, 摘要(system), 保留尾部…]
        holder["msgs"].replace_all([
            {"role": "system", "content": "S"},
            {"role": "system", "content": "# 上一轮对话成果\n\n摘要"},
            {"role": "user", "content": "尾部"},
        ])
        return "ok", "已压缩 3 条历史消息（约 10 tokens）"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A", compact_func=compact)
    holder["msgs"] = msgs
    msgs.append_user("父内容")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    reply = mgr.dispatch("/compact")
    assert plain(reply) == "已压缩 3 条历史消息（约 10 tokens）"
    data = read_session(store, "b", parent="A")
    assert data["parent_msg_count"] == 2 and data["last_summarized_at"] == 2
    assert [m["content"] for m in data["messages"]] == ["S", "# 上一轮对话成果\n\n摘要", "尾部"]
    # 根会话态同样落盘（无基准可重建）
    mgr2, msgs2, store2 = build_manager(
        tmp_path / "t2",
        compact_func=lambda: (msgs2.replace_all([{"role": "system", "content": "S"}])
                              or ("ok", "已压缩 1 条历史消息")),
        name_func=lambda m: "R")
    msgs2.append_user("内容")
    mgr2.dispatch("/save")
    mgr2.dispatch("/compact")
    assert len(read_session(store2, "R")["messages"]) == 1


def test_scenario29_compact_empty_no_persist(tmp_path):
    """场景 29：无可压缩历史 → 不落盘、对话与状态不变，返回提示而非错误。"""
    calls = []

    def compact():
        calls.append(1)
        return "empty", "没有可压缩的历史对话"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     compact_func=compact, theme=FakeTheme())
    msgs.append_user("内容")
    mgr.dispatch("/save")
    before = file_bytes(store, "A")
    reply = mgr.dispatch("/compact")
    assert "没有可压缩的历史对话" in reply.text and reply.text.startswith("  [H]")
    assert file_bytes(store, "A") == before  # 未落盘（timestamp 未刷新）
    # 失败路径（error）同样不落盘
    mgr_fail, msgs_f, store_f = build_manager(
        tmp_path / "t3", name_func=lambda m: "F",
        compact_func=lambda: ("error", "压缩失败: LLM调用出错，历史未变更"),
        theme=FakeTheme())
    msgs_f.append_user("x")
    mgr_fail.dispatch("/save")
    before_f = file_bytes(store_f, "F")
    reply = mgr_fail.dispatch("/compact")
    assert reply.text.startswith("  [E]") and file_bytes(store_f, "F") == before_f
    # 用户取消 → 灰色（非错误色）
    mgr_cancel, msgs_c, _sfc = build_manager(
        tmp_path / "t4", name_func=lambda m: "C",
        compact_func=lambda: ("error", "压缩已取消，历史未变更"),
        theme=FakeTheme())
    msgs_c.append_user("x")
    mgr_cancel.dispatch("/save")
    reply = mgr_cancel.dispatch("/compact")
    assert reply.text.startswith("  [M]")
    # 带参数 → 用法提示；压缩不可用 → 「压缩不可用」
    reply = mgr_cancel.dispatch("/compact 额外参数")
    assert "用法: /compact（无参数）" in reply.text
    mgr_no, msgs_no, _ = build_manager(tmp_path / "t5", name_func=lambda m: "N")
    msgs_no.append_user("x")
    mgr_no.dispatch("/save")
    reply = mgr_no.dispatch("/compact")
    assert plain(reply) == "压缩不可用"


# ═══════════════════════════════════════════════════════════════
# 8. 会话文件布局与原子写（场景 30-34）
# ═══════════════════════════════════════════════════════════════


def test_scenario30_file_layout(tmp_path):
    """场景 30：根会话落 `sessions/<名>.json`；子会话落 `sessions/<父名>/<子名>.json`。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "abc")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    assert store.path("abc") == os.path.join(str(tmp_path), "data", "sessions", "abc.json")
    assert os.path.isfile(store.path("abc"))
    mgr.dispatch("/explore x")
    assert store.path("x", parent="abc") == os.path.join(
        str(tmp_path), "data", "sessions", "abc", "x.json")
    assert os.path.isfile(store.path("x", parent="abc"))
    data = read_session(store, "abc")
    child = read_session(store, "x", parent="abc")
    for key in ("name", "timestamp", "messages", "parent", "status", "summary",
                "parent_msg_count", "last_summarized_at"):
        assert key in data and key in child
    assert child["parent"] == "abc" and child["name"] == "x"


def test_scenario31_safe_filename(tmp_path):
    """场景 31：文件名安全替换（`a/b:c`→`a_b_c.json`；`***`→`___.json`；空→`unnamed.json`）。"""
    mgr, msgs, store = build_manager(tmp_path)
    msgs.append_user("hi")
    assert mgr.dispatch("/save a/b:c").kind == CommandKind.HANDLED
    assert os.path.isfile(os.path.join(str(tmp_path), "data", "sessions", "a_b_c.json"))
    assert mgr.dispatch("/save ***").kind == CommandKind.HANDLED
    assert os.path.isfile(os.path.join(str(tmp_path), "data", "sessions",
                                       "***".replace("*", "_") + ".json"))
    assert safe_filename("***") == "___"
    assert safe_filename("a/b:c") == "a_b_c"
    assert safe_filename("") == "unnamed"
    assert safe_filename("..") == "_"
    assert safe_filename(" / \\ : < > | ? * ") == " _ _ _ _ _ _ _ _ "
    # 空名落盘为 unnamed.json（绕过 /save 的自动命名，直接调存储层）
    store.save("", [{"role": "user", "content": "x"}])
    assert os.path.isfile(os.path.join(str(tmp_path), "data", "sessions", "unnamed.json"))


def test_scenario32_atomic_write(tmp_path, monkeypatch):
    """场景 32：原子写——先写 `<路径>.json.tmp` 再改名替换目标文件。"""
    store = SessionStore(str(tmp_path))
    target = store.path("A")
    store.save("A", [{"role": "user", "content": "旧"}])
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst, os.path.isfile(src), os.path.isfile(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    err = store.save("A", [{"role": "user", "content": "新"}])
    assert err == ""
    assert calls == [(target + ".tmp", target, True, True)]
    assert not os.path.isfile(target + ".tmp")   # 临时文件不残留
    assert read_session(store, "A")["messages"] == [{"role": "user", "content": "新"}]


def test_scenario33_surrogate_cleaning(tmp_path):
    """场景 33：孤立代理字符写入前替换为 U+FFFD，文件按 UTF-8 正常写出与读回。"""
    store = SessionStore(str(tmp_path))
    msgs = [{"role": "user", "content": "前缀\ud800后缀"}]
    err = store.save("A", msgs)
    assert err == ""
    loaded, load_err = store.load("A")
    assert load_err == ""
    assert loaded[0]["content"].startswith("前缀") and loaded[0]["content"].endswith("后缀")
    assert "\ud800" not in loaded[0]["content"]
    # 单个孤立代理经 surrogatepass 编码为 3 字节坏序列 → 3 个替换符（与旧实现逐字一致）
    assert loaded[0]["content"].count("\ufffd") == 3
    assert clean_surrogates("a\udfff") == "a" + "\ufffd" * 3
    assert clean_surrogates({"k": ["x\ud800"]}) == {"k": ["x" + "\ufffd" * 3]}
    assert clean_surrogates(["正常文本"]) == ["正常文本"]


def test_scenario34_load_error_text(tmp_path):
    """场景 34：文件缺失 → 「会话不存在: 名」；损坏 JSON → 「加载失败: {错误}」。"""
    store = SessionStore(str(tmp_path))
    msgs, err = store.load("不存在")
    assert msgs == [] and err == "会话不存在: 不存在"
    Path(store.path("坏")).write_text("{非法 JSON", encoding="utf-8")
    msgs, err = store.load("坏")
    assert msgs == [] and err.startswith("加载失败: ")


# ═══════════════════════════════════════════════════════════════
# 9. 探索分支创建（场景 35-36）
# ═══════════════════════════════════════════════════════════════


def test_scenario35_create_branch(tmp_path):
    """场景 35：创建分支——状态 new、记录父消息数基准、初值等于父的全部消息。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("第一条")
    msgs.append_assistant("回答")
    mgr.dispatch("/save")
    reply = mgr.dispatch("/explore 分支")
    assert reply.kind == CommandKind.HANDLED
    assert plain(reply) == "已进入探索分支: 分支  (/done 合并结论, /exit 暂离)"
    data = read_session(store, "分支", parent="A")
    assert data["status"] == "new" and data["parent"] == "A"
    assert data["parent_msg_count"] == 3 and data["last_summarized_at"] == 3
    assert data["messages"] == msgs.view().to_list()
    assert isinstance(mgr.state, ChildSession)
    assert mgr.message_list() == msgs.view().to_list()


def test_scenario36_explore_missing_or_same(tmp_path):
    """场景 36：/explore 无参数 → 用法提示；与父同名 → 同名错误。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    reply = mgr.dispatch("/explore")
    assert plain(reply) == "用法: /explore <名称>"
    reply = mgr.dispatch("/explore A")
    assert plain(reply) == "[错误: 分支名不可与父会话同名（'A'），请换一个名称]"
    assert isinstance(mgr.state, RootSession) and store.list_tree()[0]["children"] == []


# ═══════════════════════════════════════════════════════════════
# 10. 探索分支合并（场景 37-42）
# ═══════════════════════════════════════════════════════════════


def test_scenario37_first_merge(tmp_path):
    """场景 37：首次合并——父末尾追加结论 system 消息、分支 completed、切回根会话态。"""
    animator = FakeAnimator()
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "结构化结论",
                                     animator=animator)
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论")
    mgr.persist_current()          # 轮末落盘（真实流程：讨论轮结束时刷盘）
    reply = mgr.dispatch("/done")
    assert reply.kind == CommandKind.HANDLED
    assert plain(reply) == "探索分支已完成，结论已合并"
    parent = read_session(store, "A")
    assert parent["messages"][-1] == {
        "role": "system", "content": "# 子会话 [分支] 结论\n\n结构化结论"}
    child = read_session(store, "分支", parent="A")
    assert child["status"] == "completed" and child["summary"] == "结构化结论"
    assert child["last_summarized_at"] == len(child["messages"])
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "A"
    assert mgr.message_list() == parent["messages"]
    assert animator.calls == ["begin_summarizing", "end_summarizing"]


def test_scenario38_no_new_discussion(tmp_path):
    """场景 38：已合并位置之后没有新消息 → 「没有新的讨论内容需要总结」，状态不变。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "结论")
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    reply = mgr.dispatch("/done")
    assert plain(reply) == "没有新的讨论内容需要总结"
    assert isinstance(mgr.state, ChildSession)
    assert read_session(store, "分支", parent="A")["status"] == "new"


def test_scenario39_summary_cancel_fail(tmp_path):
    """场景 39：总结被取消或未获得内容 → 「总结取消或失败」，父/分支/当前对话不变。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "")
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论")
    before_parent = file_bytes(store, "A")
    before_child = file_bytes(store, "分支", parent="A")
    reply = mgr.dispatch("/done")
    assert plain(reply) == "总结取消或失败"
    assert file_bytes(store, "A") == before_parent
    assert file_bytes(store, "分支", parent="A") == before_child
    assert isinstance(mgr.state, ChildSession)
    assert [m["content"] for m in mgr.message_list()] == ["S", "父请求", "分支讨论"]


def test_scenario40_repeat_done(tmp_path):
    """场景 40：分支状态已是 completed 时再次 /done → 「不可重复 /done」。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "结论")
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论")
    mgr.dispatch("/done")
    enter_child(mgr, "分支", "A")
    reply = mgr.dispatch("/done")
    assert plain(reply) == "该探索分支已完成，不可重复 /done"
    assert isinstance(mgr.state, ChildSession)


def test_scenario41_multi_round_merge(tmp_path):
    """场景 41：多轮合并——续跑后拉回 active、轮次标签「(第2轮)」、父累积两条结论。"""
    summaries = ["第一轮结论", "第二轮结论"]
    mgr, msgs, store = build_manager(
        tmp_path, name_func=lambda m: "A",
        summarize_func=lambda ms_, c: summaries.pop(0))
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论一")
    mgr.persist_current()
    mgr.dispatch("/done")
    assert read_session(store, "分支", parent="A")["status"] == "completed"
    # /cd 回分支继续讨论 → 轮末落盘把状态翻回 active（显示「⚠ 待完成」）
    enter_child(mgr, "分支", "A")
    msgs.append_user("分支讨论二")
    mgr.persist_current()
    assert read_session(store, "分支", parent="A")["status"] == "active"
    assert "⚠ 待完成" in list_text(mgr.dispatch("/ls --all"))
    mgr.dispatch("/done")
    parent = read_session(store, "A")
    contents = [m["content"] for m in parent["messages"] if m["role"] == "system"]
    assert contents[-1] == "# 子会话 [分支] (第2轮) 结论\n\n第二轮结论"
    assert any("第一轮结论" in c for c in contents)
    assert read_session(store, "分支", parent="A")["status"] == "completed"


def test_scenario42_parent_load_failure(tmp_path):
    """场景 42：/done 时无法加载父会话 → 「无法加载父会话: {错误}」，状态不变。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "结论")
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论")
    Path(store.path("A")).unlink()
    reply = mgr.dispatch("/done")
    assert plain(reply) == "无法加载父会话: 会话不存在: A"
    assert isinstance(mgr.state, ChildSession)
    assert read_session(store, "分支", parent="A")["status"] == "new"


# ═══════════════════════════════════════════════════════════════
# 11. 技能加载（场景 43-46）
# ═══════════════════════════════════════════════════════════════


def test_scenario43_load_system_skill(tmp_path, monkeypatch):
    """场景 43：命中系统技能 → system 消息注入（附「本技能目录」行）、输出已加载提示。"""
    monkeypatch.chdir(tmp_path)   # 隔离项目技能自动发现
    skills = tmp_path / "config" / "skills"
    skills.mkdir(parents=True)
    (skills / "myskill.md").write_text("技能正文内容", encoding="utf-8")
    mgr, msgs, _ = build_manager(tmp_path)
    reply = mgr.dispatch("/skill myskill")
    assert reply.kind == CommandKind.HANDLED
    assert plain(reply) == "已加载技能: myskill"
    injected = msgs.view()[-1]
    assert injected["role"] == "system"
    assert injected["content"].startswith(
        f"本技能目录: {os.path.realpath(str(skills))}")
    assert "技能正文里提到的相对路径" in injected["content"]
    assert injected["content"].endswith("技能正文内容")


def test_scenario44_load_project_skill_path(tmp_path, monkeypatch):
    """场景 44：项目技能层级路径（目录/文件.md；"\\" 与 "/" 等价）。"""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".agents" / "skills"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "proj.md").write_text("项目技能正文", encoding="utf-8")
    mgr, msgs, _ = build_manager(tmp_path)
    reply = mgr.dispatch("/skill dir/proj.md")
    assert plain(reply) == "已加载技能: dir/proj.md"
    assert msgs.view()[-1]["content"].endswith("项目技能正文")
    reply = mgr.dispatch("/skill dir\\proj.md")
    assert plain(reply) == "已加载技能: dir\\proj.md"
    assert msgs.view()[-1]["content"].endswith("项目技能正文")


def test_scenario45_skill_not_found(tmp_path, monkeypatch):
    """场景 45：技能不存在 → 「技能不存在: {名称}」，对话不变。"""
    monkeypatch.chdir(tmp_path)
    mgr, msgs, _ = build_manager(tmp_path)
    before = msgs.view().to_list()
    reply = mgr.dispatch("/skill 不存在")
    assert plain(reply) == "技能不存在: 不存在"
    assert msgs.view().to_list() == before
    reply = mgr.dispatch("/skill")
    assert plain(reply) == "用法: /skill <名称 | 项目目录/文件.md>"


def test_scenario46_skill_tree(tmp_path, monkeypatch):
    """场景 46：技能树按层级列举、文件节点标注来源（系统技能 / 项目技能）。"""
    monkeypatch.chdir(tmp_path)
    skills = tmp_path / "config" / "skills"
    skills.mkdir(parents=True)
    (skills / "sys.md").write_text("系统", encoding="utf-8")
    root = tmp_path / "skills"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "proj.md").write_text("项目", encoding="utf-8")
    (root / "top.md").write_text("项目2", encoding="utf-8")
    mgr, _, _ = build_manager(tmp_path)
    tree = mgr.list_skill_tree()
    files = [n for n in tree if n["type"] == "file"]
    assert {"name": "sys", "type": "file", "origin": "system"} in files
    assert {"name": "top.md", "type": "file", "origin": "project"} in files
    dirs = [n for n in tree if n["type"] == "dir"]
    assert dirs and dirs[0]["name"] == "sub"
    assert dirs[0]["children"] == [
        {"name": "proj.md", "type": "file", "origin": "project"}]
    assert dirs[0]["single"] is True


# ═══════════════════════════════════════════════════════════════
# 12. 思考与模型配置命令（场景 47-51）
# ═══════════════════════════════════════════════════════════════


def make_model_state(tmp_path: Path, *, config_dir: Path | None = None,
                     on_model_change=None) -> ModelState:
    """构造思考/模型配置句柄（可选 narnat.json 写回目录与模型切换回调）。"""
    ai = AIConfig(
        thinking_options={"high": "高", "max": "全开"},
        thinking_effort="high",
        thinking_passback=True,
        model="模型A",
        model_options=["模型A", "模型B"],
    )
    return ModelState(ai, config_dir=str(config_dir or ""), on_model_change=on_model_change)


def _write_narnat(config_dir: Path, data: dict | str) -> Path:
    """写 narnat.json（dict 为合法内容，str 原样写入便于构造损坏文件）。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "narnat.json"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_scenario47_thinking_query_switch(tmp_path):
    """场景 47：/thinking 无参显示当前强度；合法值切换并写回配置。"""
    config_dir = tmp_path / "config"
    narnat = _write_narnat(config_dir, {"智能体": {}})
    mgr, _, _ = build_manager(tmp_path, model_state=make_model_state(tmp_path, config_dir=config_dir))
    assert plain(mgr.dispatch("/thinking")) == "当前思考强度: 高"
    reply = mgr.dispatch("/thinking high")
    assert plain(reply) == "思考强度已切换为: 高"
    data = json.loads(narnat.read_text(encoding="utf-8"))
    assert data["智能体"]["思考"]["强度"] == "high"
    assert plain(mgr.dispatch("/thinking")) == "当前思考强度: 高"
    # 选项标签切换
    assert plain(mgr.dispatch("/thinking max")) == "思考强度已切换为: 全开"


def test_scenario48_thinking_invalid(tmp_path):
    """场景 48：/thinking 非法值 → 「无效值: 低（可用: high / max）」，配置与运行值不变。"""
    config_dir = tmp_path / "config"
    narnat = _write_narnat(config_dir, {"智能体": {}})
    before = narnat.read_bytes()
    mgr, _, _ = build_manager(tmp_path, model_state=make_model_state(tmp_path, config_dir=config_dir))
    reply = mgr.dispatch("/thinking low")
    assert plain(reply) == "无效值: low（可用: high / max）"
    assert narnat.read_bytes() == before
    assert mgr.model_state.thinking_effort == "high"
    assert plain(mgr.dispatch("/thinking")) == "当前思考强度: 高"


def test_scenario49_thinkback_switch(tmp_path):
    """场景 49：/thinkback 查询与切换（同值提示「已是」，异值提示「已开启/关闭」）。"""
    config_dir = tmp_path / "config"
    narnat = _write_narnat(config_dir, {"智能体": {}})
    mgr, _, _ = build_manager(tmp_path, model_state=make_model_state(tmp_path, config_dir=config_dir))
    assert plain(mgr.dispatch("/thinkback")) == "思考回传: 开（/thinkback on|off 切换）"
    assert plain(mgr.dispatch("/thinkback on")) == "思考回传已是开启状态"
    assert plain(mgr.dispatch("/thinkback off")) == "思考回传已关闭"
    data = json.loads(narnat.read_text(encoding="utf-8"))
    assert data["智能体"]["思考"]["回传"] is False
    assert plain(mgr.dispatch("/thinkback off")) == "思考回传已是关闭状态"
    assert plain(mgr.dispatch("/thinkback")) == "思考回传: 关（/thinkback on|off 切换）"
    assert plain(mgr.dispatch("/thinkback maybe")) == "无效值: maybe（可用: on / off）"


def test_scenario50_mode_switch(tmp_path):
    """场景 50：/mode 大小写不敏感匹配切换并写回配置；未命中列出候选。"""
    config_dir = tmp_path / "config"
    narnat = _write_narnat(config_dir, {"智能体": {}})
    changes: list[str] = []
    state = make_model_state(tmp_path, config_dir=config_dir,
                             on_model_change=changes.append)
    mgr, _, _ = build_manager(tmp_path, model_state=state)
    assert plain(mgr.dispatch("/mode")) == "当前模型: 模型A"
    reply = mgr.dispatch("/mode 模型b")   # 大小写不敏感
    assert plain(reply) == "设置成功：模型B"
    data = json.loads(narnat.read_text(encoding="utf-8"))
    assert data["智能体"]["模型"]["当前"] == "模型B"
    assert changes == ["模型B"]           # 同步统计组件（stats.set_model）
    assert plain(mgr.dispatch("/mode")) == "当前模型: 模型B"
    assert plain(mgr.dispatch("/mode 不存在")) == "无效值: 不存在（可用: 模型A / 模型B）"


def test_scenario51_config_write_silent(tmp_path):
    """场景 51：写回 narnat.json 失败（损坏/缺失）→ 静默，运行中的设置已生效。"""
    config_dir = tmp_path / "config"
    _write_narnat(config_dir, "{非法 JSON")
    mgr, _, _ = build_manager(tmp_path, model_state=make_model_state(tmp_path, config_dir=config_dir))
    reply = mgr.dispatch("/thinking max")
    assert plain(reply) == "思考强度已切换为: 全开"
    assert mgr.model_state.thinking_effort == "max"
    assert plain(mgr.dispatch("/mode 模型B")) == "设置成功：模型B"
    assert mgr.model_state.model == "模型B"
    # 目录不存在（写回目标缺失）同样静默
    mgr2, _, _ = build_manager(tmp_path / "t2",
                               model_state=make_model_state(tmp_path / "t2",
                                                            config_dir=tmp_path / "missing"))
    assert plain(mgr2.dispatch("/thinkback off")) == "思考回传已关闭"
    assert mgr2.model_state.thinking_passback is False


# ═══════════════════════════════════════════════════════════════
# 13. 目标模式命令（场景 52-55）
# ═══════════════════════════════════════════════════════════════


def test_scenario52_goal_on_with_rounds(tmp_path):
    """场景 52：/goal on 5 → 开启、临时上限 5、注入完成标记工具、输出本轮上限。"""
    gate = FakeToolGate()
    mgr, _, _ = build_manager(tmp_path, goal=GoalMode(default_max_rounds=3, tool_gate=gate))
    reply = mgr.dispatch("/goal on 5")
    assert plain(reply) == "目标模式已开启  (本轮上限 5 轮)"
    assert mgr.goal.enabled is True and mgr.goal.max_rounds == 5
    assert gate.calls == [True]


def test_scenario53_goal_on_off(tmp_path):
    """场景 53：/goal on（无 N）用配置默认轮数；/goal off 关闭并移除工具。"""
    gate = FakeToolGate()
    mgr, _, _ = build_manager(tmp_path, goal=GoalMode(default_max_rounds=3, tool_gate=gate))
    assert plain(mgr.dispatch("/goal on")) == "目标模式已开启"
    assert mgr.goal.max_rounds == 3
    assert plain(mgr.dispatch("/goal off")) == "目标模式已关闭"
    assert mgr.goal.enabled is False and mgr.goal.override_max_rounds == 0
    assert gate.calls == [True, False]
    assert plain(mgr.dispatch("/goal off")) == "目标模式已是关闭状态"


def test_scenario54_goal_invalid_rounds(tmp_path):
    """场景 54：/goal on abc → 「无效轮数: abc」；/goal on 0 → 「轮数必须为正整数」。"""
    mgr, _, _ = build_manager(tmp_path, goal=GoalMode(default_max_rounds=3))
    assert plain(mgr.dispatch("/goal on abc")) == "无效轮数: abc"
    assert plain(mgr.dispatch("/goal on 0")) == "轮数必须为正整数"
    assert mgr.goal.enabled is False and mgr.goal.max_rounds == 3
    # 临时覆盖优先于配置默认值
    mgr.dispatch("/goal on 7")
    assert mgr.goal.max_rounds == 7
    mgr.dispatch("/goal off")
    assert mgr.goal.max_rounds == 3


def test_scenario55_goal_status(tmp_path):
    """场景 55：/goal 无参显示状态（生效值：临时覆盖 > 配置默认 > 「默认」）。"""
    mgr, _, _ = build_manager(tmp_path, goal=GoalMode(default_max_rounds=4))
    assert plain(mgr.dispatch("/goal")) == "目标模式: 已关闭"
    mgr.dispatch("/goal on")
    assert plain(mgr.dispatch("/goal")) == "目标模式: 已开启  (轮数上限: 4)"
    mgr.dispatch("/goal on 2")
    assert plain(mgr.dispatch("/goal")) == "目标模式: 已开启  (轮数上限: 2)"
    # 均未配置 → 「默认」
    mgr2, _, _ = build_manager(tmp_path / "t2", goal=GoalMode(default_max_rounds=0))
    mgr2.dispatch("/goal on")
    assert plain(mgr2.dispatch("/goal")) == "目标模式: 已开启  (轮数上限: 默认)"


# ═══════════════════════════════════════════════════════════════
# 14. 会话名解析（场景 56-59）
# ═══════════════════════════════════════════════════════════════


def test_scenario56_resolve_full_match(tmp_path):
    """场景 56：完整名匹配——根会话名（可含 "/"）与「父/子」全路径优先。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "会话A")
    make_session(store, "子", parent="会话A")
    make_session(store, "含/斜杠")
    assert mgr.resolve_name("会话A") == ("会话A", None, "")
    assert mgr.resolve_name("会话A/子") == ("子", "会话A", "")
    assert mgr.resolve_name("含/斜杠") == ("含/斜杠", None, "")


def test_scenario57_resolve_split(tmp_path):
    """场景 57：含 "/" 且不匹配既有全路径 → 直接拆分返回（不校验存在性）。"""
    mgr, _, _ = build_manager(tmp_path)
    assert mgr.resolve_name("a/b") == ("b", "a", "")
    assert mgr.resolve_name("a/") == ("", "a", "")


def test_scenario58_resolve_unique_child(tmp_path):
    """场景 58：裸名在全部子会话中唯一命中 → 返回其父名。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "父1")
    make_session(store, "独子", parent="父1")
    assert mgr.resolve_name("独子") == ("独子", "父1", "")


def test_scenario59_resolve_ambiguous_missing(tmp_path):
    """场景 59：多匹配 → 列出全部全路径；无匹配 → 「会话不存在: 名」。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "父1")
    make_session(store, "同名", parent="父1")
    make_session(store, "父2")
    make_session(store, "同名", parent="父2")
    name, parent, err = mgr.resolve_name("同名")
    assert name is None and parent is None
    lines = err.splitlines()
    assert lines[0] == "'同名' 有多个，请用完整路径指定："
    assert set(lines[1:]) == {"      父1/同名", "      父2/同名"}   # 顺序随会话树（时间倒序）
    assert mgr.resolve_name("从未存在") == (None, None, "会话不存在: 从未存在")


# ═══════════════════════════════════════════════════════════════
# 15. 延迟删除执行与展示标记（场景 60-61）
# ═══════════════════════════════════════════════════════════════


def test_scenario60_cleanup_executes_deletes(tmp_path, monkeypatch):
    """场景 60：退出清理先删全部子会话、再删根会话（连带子目录），随后清空标记。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "A")
    make_session(store, "b1", parent="A")
    make_session(store, "b2", parent="A")
    make_session(store, "B")
    make_session(store, "b3", parent="B")
    mgr.dispatch("/rm A")
    mgr.dispatch("/rm B")
    calls: list[tuple[str, str | None]] = []
    real_delete = store.delete

    def spy(name, parent=None):
        calls.append((name, parent))
        return real_delete(name, parent=parent)

    monkeypatch.setattr(store, "delete", spy)
    mgr.exit_cleanup()
    child_count = sum(1 for _n, p in calls if p is not None)
    assert child_count == 3
    assert all(p is not None for _n, p in calls[:child_count])   # 先子
    assert all(p is None for _n, p in calls[child_count:])       # 后根
    assert mgr.pending_deletes == set()
    children_dir = os.path.join(str(tmp_path), "data", "sessions")
    assert not os.path.isfile(store.path("A")) and not os.path.isdir(os.path.join(children_dir, "A"))
    assert not os.path.isfile(os.path.join(children_dir, "B.json"))
    # /rm --all → 退出清理后会话目录为空（保留目录本身）
    mgr2, _, store2 = build_manager(tmp_path / "t2")
    make_session(store2, "X")
    make_session(store2, "x1", parent="X")
    mgr2.dispatch("/rm --all")
    mgr2.exit_cleanup()
    sdir = os.path.join(str(tmp_path / "t2"), "data", "sessions")
    assert os.path.isdir(sdir) and os.listdir(sdir) == []


def test_scenario61_list_marks(tmp_path):
    """场景 61：列表展示——待删除行「  ✘ 退出后删除」、当前行「  ◀ 当前」（正确着色）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A", theme=FakeTheme())
    make_session(store, "A")
    make_session(store, "b1", parent="A")
    enter_root(mgr, "A")
    mgr.dispatch("/rm b1")
    text = mgr.dispatch("/ls --all").text
    assert "✘ 退出后删除" in text and "[E]✘ 退出后删除[R]" in text
    assert "[C]◀ 当前[R]" in text
    # 无待删除标记时不做删除标记着色（文本中不出现删除标记，也无 [E] 段）
    mgr2, _, store2 = build_manager(tmp_path / "t2", theme=FakeTheme())
    make_session(store2, "C")
    text2 = mgr2.dispatch("/ls").text
    assert "✘ 退出后删除" not in text2 and "[E]" not in text2
    # 子会话行同样带标记与当前标记（根会话态标记 → 子会话态查看）
    mgr3, _, store3 = build_manager(tmp_path / "t3", name_func=lambda m: "P")
    make_session(store3, "P")
    make_session(store3, "c1", parent="P")
    enter_root(mgr3, "P")
    mgr3.dispatch("/rm c1")
    enter_child(mgr3, "c1", "P")
    text3 = mgr3.dispatch("/ls --all").text
    assert "c1" in text3 and "✘ 退出后删除" in text3 and "◀ 当前" in text3


# ═══════════════════════════════════════════════════════════════
# 16. 自动保存与退出保存（场景 62-65）
# ═══════════════════════════════════════════════════════════════


def test_scenario62_auto_save_gates(tmp_path):
    """场景 62：配置关闭 / 已有会话 / 本进程已发起过 → 不触发自动保存。"""
    assert AUTO_SAVE_WAIT_SECONDS == 5.0
    calls: list[int] = []
    named = lambda m: (calls.append(1), "自动名")[1]
    # ① 配置关闭
    mgr, msgs, store = build_manager(tmp_path, name_func=named,
                                     auto_save_enabled=False, auto_save_tokens=0)
    msgs.append_user("hi")
    mgr.maybe_auto_save(999)
    mgr.wait_auto_save()
    assert calls == [] and isinstance(mgr.state, NoSession) and store.list_tree() == []
    # ② 已有会话（根会话态）
    mgr2, msgs2, _ = build_manager(tmp_path / "t2", name_func=named,
                                   auto_save_enabled=True, auto_save_tokens=0)
    msgs2.append_user("hi")
    mgr2.dispatch("/save")
    calls.clear()
    mgr2.maybe_auto_save(999)
    mgr2.wait_auto_save()
    assert calls == [] and mgr2.session_name() == "自动名"
    # ③ 已发起过（命名失败也置「已发起」，不再重试）
    calls.clear()
    mgr3, msgs3, _ = build_manager(tmp_path / "t3",
                                   name_func=lambda m: (calls.append(1), "")[1],
                                   auto_save_enabled=True, auto_save_tokens=0)
    msgs3.append_user("hi")
    mgr3.maybe_auto_save(1)
    mgr3.wait_auto_save()
    assert calls == [1] and mgr3.session_name() is None
    mgr3.maybe_auto_save(9999)
    mgr3.wait_auto_save()
    assert calls == [1] and mgr3.session_name() is None


def test_scenario63_auto_save_token_threshold(tmp_path):
    """场景 63：门槛 > 0 时输入 token 需超过门槛；门槛 ≤ 0 时不限。"""
    calls: list[int] = []
    named = lambda m: (calls.append(1), "自动名")[1]
    mgr, msgs, _ = build_manager(tmp_path, name_func=named,
                                 auto_save_enabled=True, auto_save_tokens=100)
    msgs.append_user("hi")
    mgr.maybe_auto_save(100)      # 不超过 → 不触发
    mgr.wait_auto_save()
    assert calls == [] and mgr.session_name() is None
    mgr.maybe_auto_save(101)      # 超过 → 触发
    mgr.wait_auto_save()
    assert calls == [1] and mgr.session_name() == "自动名"
    # 门槛 ≤ 0：用户输入即触发
    calls.clear()
    mgr2, msgs2, _ = build_manager(tmp_path / "t2", name_func=named,
                                   auto_save_enabled=True, auto_save_tokens=0)
    msgs2.append_user("hi")
    mgr2.maybe_auto_save(1)
    mgr2.wait_auto_save()
    assert calls == [1] and mgr2.session_name() == "自动名"


def test_scenario64_auto_save_then_switch(tmp_path):
    """场景 64：自动保存触发 → 后台命名成功并写盘 → 下一轮输入前切换为根会话态。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "自动名",
                                     auto_save_enabled=True, auto_save_tokens=0)
    msgs.append_user("hi")
    mgr.maybe_auto_save(1)
    assert isinstance(mgr.state, NoSession)      # 触发同步返回，切换发生在收尾时
    mgr.wait_auto_save()
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "自动名"
    assert read_session(store, "自动名")["messages"] == msgs.view().to_list()


def test_scenario65_exit_save(tmp_path):
    """场景 65：退出流程——已收尾的自动保存落盘并输出「会话已自动保存: 名」。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "自动名",
                                     auto_save_enabled=True, auto_save_tokens=0)
    msgs.append_user("hi")
    mgr.maybe_auto_save(1)
    text = mgr.exit_cleanup()
    assert text == "会话已自动保存: 自动名"
    assert read_session(store, "自动名")["messages"] == msgs.view().to_list()
    # 无当前会话（游离态启动）→ 无提示、无落盘
    mgr2, _, store2 = build_manager(tmp_path / "t2")
    assert mgr2.exit_cleanup() == "" and store2.list_tree() == []


# ═══════════════════════════════════════════════════════════════
# 17. 兼容性怪癖（场景 66-70）
# ═══════════════════════════════════════════════════════════════


def test_scenario66_split_without_validation(tmp_path):
    """场景 66：/cd a/b 而 a/b 非任何既有会话 → 解析不报错，加载阶段报「会话不存在: b」。"""
    mgr, _, _ = build_manager(tmp_path)
    reply = mgr.dispatch("/cd a/b")
    assert plain(reply) == "会话不存在: b"
    assert isinstance(mgr.state, NoSession)


def test_scenario67_summarized_at_zero_fallback(tmp_path):
    """场景 67：分支文件已合并位置为 0 → 运行时回退为父基准值（0 无法表达）。"""
    captured: list[str] = []

    def summarize(messages, cancel):
        captured.append(messages[0]["content"])
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    make_session(store, "A", messages=[{"role": "system", "content": "S"},
                                       {"role": "user", "content": "父"}])
    make_session(store, "b", parent="A", status="active",
                 messages=[{"role": "system", "content": "S"},
                           {"role": "user", "content": "父"},
                           {"role": "user", "content": "分支讨论"}],
                 parent_msg_count=2, last_summarized_at=0)
    enter_child(mgr, "b", "A")
    assert mgr.state._last_summarized_at == 2   # 0 → 回退父基准
    # 行为面：已合并位置回退后 target 从父基准之后取（不重复合并分支早期内容）
    mgr.dispatch("/done")
    assert captured
    target_part = captured[0].split("## 2. 子分支探索日志")[1]
    assert "用户：分支讨论" in target_part
    memory_part = captured[0].split("## 2. 子分支探索日志")[0]
    assert "用户：分支讨论" not in memory_part


def test_scenario68_list_compare_and_color_quirk(tmp_path):
    """场景 68：列表比较用整条记录内容；标记着色为字面文本替换（会话名含字面量被误着色）。"""
    # ① 内容比较：两条完全相同的记录（同名同时间同条数）在「是否已显示」判定中被
    #    视为同一记录——第 4 条不触发追加、也不额外占用「还有 N 条」计数。
    tree = [
        {"name": "双", "timestamp": TS_EPOCH + 500, "message_count": 1, "children": []},
        {"name": "早2", "timestamp": TS_EPOCH + 400, "message_count": 1, "children": []},
        {"name": "早3", "timestamp": TS_EPOCH + 300, "message_count": 1, "children": []},
        {"name": "双", "timestamp": TS_EPOCH + 500, "message_count": 1, "children": []},
    ]
    text = format_session_summary(copy.deepcopy(tree), "双", None)
    assert text.count("── 双  (") == 2          # 内容相同的两条记录均按「已显示」渲染
    assert "… 还有 1 条更早的会话 (/ls --all 查看)" in text
    # ② 着色误判：会话名本身含「◀ 当前」/「✘ 退出后删除」字面量 → 被字面替换着色
    mgr, _, store = build_manager(tmp_path, theme=FakeTheme())
    make_session(store, "标题 ◀ 当前 尾巴")
    text = mgr.dispatch("/ls").text
    assert "标题 [C]◀ 当前[R] 尾巴" in text
    make_session(store, "名字 ✘ 退出后删除 结尾")
    mgr.dispatch("/rm 名字 ✘ 退出后删除 结尾")
    text = mgr.dispatch("/ls").text
    assert "名字 [E]✘ 退出后删除[R] 结尾" in text


def test_scenario69_persist_failure_ignored(tmp_path, monkeypatch):
    """场景 69：/cd 与 /explore 的「先落盘」路径忽略落盘错误继续切换。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    make_session(store, "B")
    make_session(store, "分支", parent="B", status="new")   # 先创建分支文件
    msgs.append_user("切走前的修改")
    monkeypatch.setattr(store, "save", lambda *a, **k: "保存失败: 只读磁盘")
    reply = mgr.dispatch("/cd B")
    assert plain(reply) == "已进入会话: B" and mgr.session_name() == "B"
    # /explore：落盘失败但目标分支文件存在 → 仍读回并进入子会话态
    reply = mgr.dispatch("/explore 分支")
    assert plain(reply) == "已进入探索分支: 分支  (/done 合并结论, /exit 暂离)"
    assert isinstance(mgr.state, ChildSession)
    # 回到根会话态：落盘失败且目标文件不存在时，加载阶段报错、不切换状态
    mgr.dispatch("/cd B")
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "B"
    reply = mgr.dispatch("/explore 全新分支")
    assert plain(reply) == "会话不存在: 全新分支"
    assert isinstance(mgr.state, RootSession)


def test_scenario70_child_reload_error_ignored(tmp_path):
    """场景 70：/done 末尾重读分支文件失败 → 按空内容继续保存该分支（文件缺失路径）。"""
    holder: dict = {}

    def summarize(messages, cancel):
        Path(holder["store"].path("分支", parent="A")).unlink()
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    holder["store"] = store
    msgs.append_user("父请求")
    mgr.dispatch("/save")
    mgr.dispatch("/explore 分支")
    msgs.append_user("分支讨论")
    reply = mgr.dispatch("/done")
    assert plain(reply) == "探索分支已完成，结论已合并"
    child = read_session(store, "分支", parent="A")
    assert child["messages"] == [] and child["status"] == "completed"
    assert child["summary"] == "结论"


# ═══════════════════════════════════════════════════════════════
# 18. 状态迁移矩阵（每态每命令的可用性与效果）
# ═══════════════════════════════════════════════════════════════

ALL_COMMANDS = ["/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
                "/thinking", "/thinkback", "/mode", "/goal", "/explore", "/done",
                "/exit"]

STATE_COMMANDS = {
    "free": ["/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
             "/thinking", "/thinkback", "/mode", "/goal", "/exit"],
    "root": ["/clear", "/compact", "/save", "/ls", "/cd", "/rm", "/skill",
             "/thinking", "/thinkback", "/mode", "/goal", "/explore", "/exit"],
    "child": ["/clear", "/compact", "/ls", "/cd", "/skill", "/thinking",
              "/thinkback", "/mode", "/goal", "/done", "/exit"],
}


def _seed_state(tmp_path: Path, state_name: str, **kwargs):
    """构造指定状态的会话管理器（含可标记的子会话与另一根会话）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "当前", **kwargs)
    msgs.append_user("hi")
    make_session(store, "其它")
    make_session(store, "子1", parent="当前", status="new", parent_msg_count=0)
    if state_name != "free":
        mgr.dispatch("/save")
    if state_name == "child":
        enter_child(mgr, "子1", "当前")
    return mgr, msgs, store


@pytest.mark.parametrize("state_name", ["free", "root", "child"])
def test_state_matrix_command_availability(tmp_path, state_name):
    """状态迁移矩阵：每态的命令表与「可用命令可执行、不可用命令按未知处理」逐条断言。"""
    mgr, msgs, store = _seed_state(tmp_path, state_name)
    assert set(mgr.available_commands()) == set(STATE_COMMANDS[state_name])
    for idx, cmd in enumerate(ALL_COMMANDS):
        mgr2, msgs2, store2 = _seed_state(tmp_path / f"m{state_name}{idx}", state_name)
        before_tree = copy.deepcopy(store2.list_tree())
        reply = mgr2.dispatch(f"{cmd} 参数")
        if cmd in STATE_COMMANDS[state_name]:
            assert reply.kind != CommandKind.UNKNOWN, f"{state_name} 态 {cmd} 应可用"
        else:
            assert reply.kind == CommandKind.UNKNOWN and reply.text == "", \
                f"{state_name} 态 {cmd} 应按未知处理"
            # 不可用命令不执行、不产生副作用（会话树与待删除标记均不变）
            assert mgr2.pending_deletes == set()
            assert store2.list_tree() == before_tree


def test_state_matrix_transitions(tmp_path):
    """状态迁移：游离态 →(save)→ 根会话态 →(explore)→ 子会话态 →(exit)→ 根会话态。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=lambda ms, c: "结论")
    msgs.append_user("hi")
    assert isinstance(mgr.state, NoSession)
    mgr.dispatch("/save")
    assert isinstance(mgr.state, RootSession)
    mgr.dispatch("/explore b")
    assert isinstance(mgr.state, ChildSession)
    mgr.dispatch("/exit")
    assert isinstance(mgr.state, RootSession) and mgr.session_name() == "A"
    # 子会话凭 /cd 直接进入（不依赖 /explore 的当前状态）
    mgr2, msgs2, _ = build_manager(tmp_path / "t2")
    make_session(SessionStore(str(tmp_path / "t2")), "P")
    make_session(SessionStore(str(tmp_path / "t2")), "c", parent="P")
    assert mgr2.dispatch("/cd P/c").kind == CommandKind.HANDLED
    assert isinstance(mgr2.state, ChildSession)
    assert mgr2.dispatch("/cd P").kind == CommandKind.HANDLED
    assert isinstance(mgr2.state, RootSession)


# ═══════════════════════════════════════════════════════════════
# 19. /done 合并序列（memory/target 分界、基准重建）
# ═══════════════════════════════════════════════════════════════


def test_done_sequence_memory_target_boundary(tmp_path):
    """`/done` 增量区间：memory = 主分支基准（父消息数之前），target = 已合并位置之后。"""
    captured: list[str] = []

    def summarize(messages, cancel):
        captured.append(messages[0]["content"])
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    msgs.append_user("父一")
    msgs.append_assistant("父答")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    msgs.append_user("分支一")
    mgr.persist_current()
    mgr.dispatch("/done")
    memory_part, target_part = captured[0].split("## 2. 子分支探索日志")
    assert "用户：父一" in memory_part and "AI：父答" in memory_part
    assert "分支一" not in memory_part
    assert "用户：分支一" in target_part
    # 第二轮：target 只含上一轮已合并位置之后的新讨论
    enter_child(mgr, "b", "A")
    msgs.append_user("分支二")
    mgr.persist_current()
    mgr.dispatch("/done")
    memory_part2, target_part2 = captured[1].split("## 2. 子分支探索日志")
    assert "分支二" in target_part2 and "分支一" not in target_part2
    assert "父一" in memory_part2
    # 每轮总结都带提示词模板的固定小节与总长约定
    assert "## 原始请求与意图" in captured[0] and "总长不超过 1500 字" in captured[0]


def test_done_sequence_reset_after_compact(tmp_path):
    """压缩后重建基准：主分支基准与已合并位置重置为前导 system 条数。"""
    captured: list[str] = []

    def summarize(messages, cancel):
        captured.append(messages[0]["content"])
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    msgs.append_user("父")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    msgs.append_user("分支一")
    msgs.append_user("分支二")
    # 模拟压缩：消息列表整体替换为 [系统提示词, 摘要(system), 保留尾部…]
    msgs.replace_all([
        {"role": "system", "content": "S"},
        {"role": "system", "content": "# 上一轮对话成果\n\n压缩摘要"},
        {"role": "user", "content": "分支尾部"},
    ])
    mgr.state.reset_after_compact()
    assert mgr.state._parent_msg_count == 2
    assert mgr.state._last_summarized_at == 2
    mgr.dispatch("/done")
    memory_part, target_part = captured[0].split("## 2. 子分支探索日志")
    assert "系统：# 上一轮对话成果" in memory_part   # 摘要进主分支基准（背景素材）
    assert "分支尾部" not in memory_part
    assert "用户：分支尾部" in target_part           # 保留尾部逐字进增量区间


# ═══════════════════════════════════════════════════════════════
# 20. 命令层细节（归一化、着色、轮末落盘）
# ═══════════════════════════════════════════════════════════════


def test_command_name_normalization(tmp_path):
    """命令名归一：转小写、剥除全部前导斜杠（`/SAVE`、`///save` 等价）。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    assert plain(mgr.dispatch("///save 甲")) == "会话已保存: 甲"
    assert plain(mgr.dispatch("/SAVE")) == "会话已保存: 甲"
    assert mgr.dispatch("/Save").kind == CommandKind.HANDLED
    assert mgr.dispatch("save 甲").kind == CommandKind.UNKNOWN   # 非 "/" 开头不认


def test_command_reply_styling(tmp_path):
    """命令输出为已着色文本（两空格缩进、无结尾换行）；无色端口下拼出裸文本。"""
    mgr, msgs, _ = build_manager(tmp_path, name_func=lambda m: "A", theme=FakeTheme())
    msgs.append_user("hi")
    assert mgr.dispatch("/save").text == "  [S]会话已保存: [C]A[R]"
    assert mgr.dispatch("/cd").text == "  [H]用法: /cd <名称>[R]"
    assert mgr.dispatch("/rm").text == "  [H]用法: /rm <名称 | --all>[R]"
    assert mgr.dispatch("/explore").text == "  [H]用法: /explore <名称>[R]"
    assert mgr.dispatch("/thinking").text.startswith("  [C]")
    assert mgr.dispatch("/nope").text == ""
    assert mgr.dispatch("/clear").text == ""
    mgr2, msgs2, _ = build_manager(tmp_path / "t2", name_func=lambda m: "B")
    msgs2.append_user("hi")
    reply = mgr2.dispatch("/save")
    assert reply.text == "  会话已保存: B" and "\x1b" not in reply.text


def test_persist_current_round_end(tmp_path):
    """轮末落盘（persist_current）：有会话时刷新文件，游离态 no-op。"""
    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A")
    msgs.append_user("hi")
    mgr.dispatch("/save")
    msgs.append_assistant("回复")
    mgr.persist_current()
    assert read_session(store, "A")["messages"] == msgs.view().to_list()
    mgr2, _, store2 = build_manager(tmp_path / "t2")
    mgr2.persist_current()
    assert store2.list_tree() == []


def test_save_auto_name_without_name_func(tmp_path):
    """未注入命名函数时空名 /save → 命名失败提示（与命名函数返回空串同路径）。"""
    mgr, msgs, store = build_manager(tmp_path)   # name_func 缺省
    msgs.append_user("hi")
    assert plain(mgr.dispatch("/save")) == "自动命名失败，请手动指定: /save <名称>"
    assert isinstance(mgr.state, NoSession) and store.list_tree() == []


# ═══════════════════════════════════════════════════════════════
# 21. 基准对照（v2/tests/baseline/data/session_store.json）
# ═══════════════════════════════════════════════════════════════


def _load_baseline() -> dict:
    if not BASELINE_FILE.exists():  # pragma: no cover - 基准缺失时跳过（任务书允许）
        pytest.skip(f"无 session_store 基准：{BASELINE_FILE}")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def test_baseline_safe_filename():
    """基准对照：`session_store._safe_filename` 全用例。"""
    data = _load_baseline()
    cases = data["groups"]["session_store._safe_filename"]
    assert len(cases) == 13
    for case in cases:
        assert safe_filename(case["input"]["name"]) == case["result"], case["id"]


def test_baseline_format_session_tree():
    """基准对照：`session_store.format_session_tree` 全用例（时间遮罩后逐字节比对）。"""
    data = _load_baseline()
    cases = data["groups"]["session_store.format_session_tree"]
    assert len(cases) == 5
    for case in cases:
        tree = copy.deepcopy(case["input"]["tree"])
        got = mask_time(format_session_tree(tree, case["input"]["active_name"],
                                            case["input"]["active_parent"]))
        assert got == case["result"], case["id"]


def test_baseline_format_session_summary():
    """基准对照：`session_store.format_session_summary` 全用例。"""
    data = _load_baseline()
    cases = data["groups"]["session_store.format_session_summary"]
    assert len(cases) == 4
    for case in cases:
        tree = copy.deepcopy(case["input"]["tree"])
        got = mask_time(format_session_summary(tree, case["input"]["active_name"],
                                               case["input"]["active_parent"]))
        assert got == case["result"], case["id"]


def test_baseline_format_session_list():
    """基准对照：`session_store.format_session_list` 全用例。"""
    data = _load_baseline()
    cases = data["groups"]["session_store.format_session_list"]
    assert len(cases) == 2
    for case in cases:
        got = mask_time(format_session_list(copy.deepcopy(case["input"]["sessions"])))
        assert got == case["result"], case["id"]


def test_baseline_tree_end_to_end(tmp_path):
    """基准对照（端到端）：真实落盘目录经 list_tree 后与基准树结构一致。"""
    store = SessionStore(str(tmp_path))
    make_session(store, "会话A", timestamp=TS_FUTURE,
                 messages=[{"role": "user", "content": "x"}] * 12)
    for name, ts, status, count in (
        ("子任务-完成", TS_FUTURE - 300, "completed", 3),
        ("子任务-新", TS_FUTURE - 200, "new", 5),
        ("子任务-进行中", TS_FUTURE - 100, "active", 7),
    ):
        make_session(store, name, parent="会话A", status=status, timestamp=ts,
                     messages=[{"role": "user", "content": "y"}] * count)
    make_session(store, "会话B", timestamp=TS_EPOCH,
                 messages=[{"role": "user", "content": "z"}] * 2)
    tree = store.list_tree()
    assert [root["name"] for root in tree] == ["会话A", "会话B"]
    names = [child["name"] for child in tree[0]["children"]]
    assert names == ["子任务-完成", "子任务-新", "子任务-进行中"]  # 子按时间升序
    tree[1]["_delete_marked"] = True   # 待删除标记（基准 active_root 用例含此标记）
    got = mask_time(format_session_tree(copy.deepcopy(tree), "会话A", None))
    data = _load_baseline()
    case = next(c for c in data["groups"]["session_store.format_session_tree"]
                if c["id"] == "active_root")
    assert got == case["result"]


# ═══════════════════════════════════════════════════════════════
# 22. 结构验收（无私有互摸 / 无后置补线 / 导出齐备）
# ═══════════════════════════════════════════════════════════════

_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted(PACKAGE_DIR.glob("*.py"))}


def test_structure_no_private_field_cross_access():
    """验收项 5：无 `_goal_enabled` 等私有互摸（改公开目标模式 API 与配置句柄）。"""
    text = "\n".join(_sources().values())
    for token in ("_goal_enabled", "_goal_max_rounds", "_goal_default_rounds",
                  "_set_goal_tool", "_set_model", "stats._model",
                  "summary_anim_start", "summary_anim_stop", "_last_round_ok"):
        assert token not in text, f"存在私有互摸残留：{token}"
    # 后置补线形态（构造后裸赋值）不得出现
    for token in (".compact_func =", ".summary_anim_start =", ".summary_anim_stop =",
                  ".goal =", ".model_state =", ".theme ="):
        assert token not in text, f"存在后置补线：{token}"


def test_structure_public_api_present(tmp_path):
    """目标模式/配置句柄为公开 API（conversation / app 消费面）。"""
    gate = FakeToolGate()
    mgr, _, _ = build_manager(tmp_path, goal=GoalMode(default_max_rounds=2, tool_gate=gate))
    assert not hasattr(mgr, "_goal_enabled")
    assert mgr.goal.enabled is False and mgr.goal.max_rounds == 2
    mgr.goal.turn_on(5)
    assert mgr.goal.enabled is True and mgr.goal.max_rounds == 5
    assert gate.calls == [True]
    assert mgr.goal.turn_off() is True and gate.calls == [True, False]
    for attr in ("goal", "model_state", "theme", "store", "pending_deletes",
                 "commands", "state"):
        assert not attr.startswith("_") and hasattr(mgr, attr)


def test_structure_no_module_level_mutable_state():
    """无模块级可变状态（常量须全大写，与 check_layering 的机械护栏同规则）。"""
    for name, src in _sources().items():
        tree = ast.parse(src)
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for target in targets:
                if target.startswith("__"):
                    continue   # 包级 dunder（`__all__`）与检查器同规则
                assert _CONST_NAME_RE.match(target), f"{name}:{node.lineno} 模块级可变名 {target}"


def test_structure_chinese_docstrings():
    """公开模块 / 类 / 函数均有中文 docstring。"""
    for name, src in _sources().items():
        tree = ast.parse(src)
        assert ast.get_docstring(tree), f"{name} 缺模块 docstring"
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) \
                    and not node.name.startswith("_"):
                assert ast.get_docstring(node), f"{name}::{node.name} 缺 docstring"


def test_structure_public_exports():
    """聚合导出齐备（`__all__` 全部可解析）。"""
    import narnat_agent.sessions as sessions

    for name in sessions.__all__:
        assert hasattr(sessions, name), name
    assert sessions.PlainColors.cmd_success == ""
    assert sessions.BOUNDARY_MARKER_PREFIX == "━━━ 探索分支开始"
    assert "{memory}" in sessions.SUMMARY_TASK_TEMPLATE
    assert "{target}" in sessions.SUMMARY_TASK_TEMPLATE


# ═══════════════════════════════════════════════════════════════
# 23. 补充：总结提示词模板 / 历史基准兼容 / 补全数据源
# ═══════════════════════════════════════════════════════════════


def test_done_summary_template_sections():
    """`/done` 结构化结论模板：固定小节、空节「(无)」、总长 1500 字、无增量输出行。"""
    for section in ("## 原始请求与意图", "## 关键技术概念", "## 文件与代码",
                    "## 错误与修复", "## 待办任务", "## 当前工作", "## 下一步",
                    "## 关键上下文"):
        assert section in SUMMARY_TASK_TEMPLATE, section
    assert "空节写“(无)”" in SUMMARY_TASK_TEMPLATE
    assert "总长不超过 1500 字" in SUMMARY_TASK_TEMPLATE
    assert "`[无实质性更新]`" in SUMMARY_TASK_TEMPLATE
    assert "只记录**相对主分支已知信息的新增量**" in SUMMARY_TASK_TEMPLATE
    assert SUMMARY_TASK_TEMPLATE.count("{memory}") == 1
    assert SUMMARY_TASK_TEMPLATE.count("{target}") == 1


def test_done_summary_request_shape(tmp_path):
    """`/done` 的总结请求为单条 user 消息（提示词全文即其 content），且总结函数缺省即失败。"""
    captured: list[list] = []

    def summarize(messages, cancel):
        captured.append(messages)
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    msgs.append_user("父")
    mgr.dispatch("/save")
    mgr.dispatch("/explore b")
    msgs.append_user("讨论")
    mgr.persist_current()
    mgr.dispatch("/done")
    assert len(captured[0]) == 1
    assert captured[0][0]["role"] == "user"
    assert captured[0][0]["content"].startswith("# 任务：探索分支增量合并")
    # 未注入总结函数（缺省）→ 「总结取消或失败」
    mgr2, msgs2, _ = build_manager(tmp_path / "t2", name_func=lambda m: "A")
    msgs2.append_user("父")
    mgr2.dispatch("/save")
    mgr2.dispatch("/explore b")
    msgs2.append_user("讨论")
    reply = mgr2.dispatch("/done")
    assert plain(reply) == "总结取消或失败"
    assert isinstance(mgr2.state, ChildSession)


def test_done_legacy_boundary_marker(tmp_path):
    """历史会话兼容：基准缺失（0）时按「━━━ 探索分支开始」标记定位父基准。"""
    captured: list[str] = []

    def summarize(messages, cancel):
        captured.append(messages[0]["content"])
        return "结论"

    mgr, msgs, store = build_manager(tmp_path, name_func=lambda m: "A",
                                     summarize_func=summarize)
    make_session(store, "A", messages=[{"role": "system", "content": "S"},
                                       {"role": "user", "content": "父内容"}])
    make_session(store, "b", parent="A", status="active",
                 messages=[{"role": "user", "content": "父内容"},
                           {"role": "system", "content": f"{BOUNDARY_MARKER_PREFIX}，之后为分支"}],
                 parent_msg_count=0, last_summarized_at=None)
    enter_child(mgr, "b", "A")
    assert mgr.state._parent_msg_count == 0
    mgr.dispatch("/done")
    memory_part, target_part = captured[0].split("## 2. 子分支探索日志")
    assert "用户：父内容" in memory_part
    assert "探索分支开始" not in memory_part
    assert "探索分支开始" in target_part


def test_completion_candidates(tmp_path):
    """Tab 补全数据源：会话名 / 可删除名（按状态、跳过已标记）/ 思考与模型候选。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "A", timestamp=TS_EPOCH + 300)
    make_session(store, "b1", parent="A", timestamp=TS_EPOCH + 200)
    make_session(store, "B", timestamp=TS_EPOCH + 100)
    assert mgr.list_session_names() == ["A", "A/b1", "B"]
    assert mgr.list_rm_names() == ["A", "A/b1", "B"]
    mgr.dispatch("/rm A")                 # 根连带子一起标记 → 候选只剩未标记的
    assert mgr.list_rm_names() == ["B"]
    # 根会话态：仅当前会话的子会话（「父/子」形式）；已标记的不再出现
    enter_root(mgr, "B")
    make_session(store, "b2", parent="B", timestamp=TS_EPOCH + 50)
    assert mgr.list_rm_names() == ["B/b2"]
    mgr.dispatch("/rm b2")
    assert mgr.list_rm_names() == []
    # 子会话态：无 /rm，候选为空
    enter_child(mgr, "b1", "A")
    assert mgr.list_rm_names() == []
    # /thinking 与 /mode 候选
    state = make_model_state(tmp_path)
    mgr2, _, _ = build_manager(tmp_path / "t2", model_state=state)
    assert mgr2.list_thinking_options() == ["high", "max"]
    assert mgr2.list_model_options() == ["模型A", "模型B"]


def test_cleanup_delete_failure_silent(tmp_path, monkeypatch):
    """删除失败静默忽略：单条删除失败不中断退出清理，标记集合照常清空。"""
    mgr, _, store = build_manager(tmp_path)
    make_session(store, "A")
    make_session(store, "B")
    mgr.pending_deletes.add(("A", None))
    mgr.pending_deletes.add(("B", None))

    def boom(path):
        raise OSError("文件被占用")

    monkeypatch.setattr(os, "remove", boom)
    mgr.cleanup_deletes()                 # 不抛异常
    assert mgr.pending_deletes == set()
    assert os.path.isfile(store.path("A")) and os.path.isfile(store.path("B"))
