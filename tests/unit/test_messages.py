"""messages 积木自测（T2.4）—— spec Scenario 全覆盖 + repair 全分支 + 视图不可变性 + 快照对照。

覆盖清单（对齐 T2.4 任务书测试要求）：
1. `specs/messages` 全部 15 个 Scenario（映射表见下）；
2. repair 全分支（无缺陷不动 / 未回复工具 / 尾部 tool 合成 assistant / 思考占位 / 幂等 /
   缺 id 跳过 / 其它异常不处理）；
3. 视图不可变性（结构上无法经视图修改内部列表）；
4. 快照对照：`v2/tests/baseline/data/message_list.json`（旧实现基准，逐用例比对）。

spec Scenario 映射表
════════════════════

| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 消息序列与角色契约 | 构建即含 system 首条 | `test_scenario01_system_first` |
| 2 | 消息序列与角色契约 | assistant 可选字段写入规则 | `test_scenario02_assistant_optional_fields` |
| 3 | 消息序列与角色契约 | tool 消息结构 | `test_scenario03_tool_message_structure` |
| 4 | 只读视图 | 外部拿不到内部列表引用 | `test_scenario04_view_hides_internal_list` |
| 5 | 只读视图 | 实时可见 | `test_scenario05_view_is_live` |
| 6 | 受控修改接口 | 追加 system 消息（技能注入） | `test_scenario06_append_system_skill` |
| 7 | 受控修改接口 | 原子替换 | `test_scenario07_replace_all_atomic` |
| 8 | 中断补齐 | 部分完成时补齐 | `test_scenario08_interrupted_partial` |
| 9 | 中断补齐 | 全部完成时不补齐 | `test_scenario09_interrupted_all_completed` |
| 10 | 请求前序列修复 | 未回复的工具调用被补齐 | `test_scenario10_repair_fills_missing` |
| 11 | 请求前序列修复 | 修复后尾部为工具则合成 assistant | `test_scenario11_repair_appends_synthetic` |
| 12 | 请求前序列修复 | 未修复时不追加合成 assistant | `test_scenario12_repair_no_synthetic_without_fix` |
| 13 | 请求前序列修复 | 重复 id 只补一次 | `test_scenario13_repair_duplicate_id_once` |
| 14 | 兼容性怪癖保持 | 消息字典共享（兼容怪癖） | `test_scenario14_message_dict_shared` |
| 15 | 兼容性怪癖保持 | 整体替换允许清空（兼容怪癖） | `test_scenario15_replace_all_empty` |

repair 分支 ↔ R2 报告（`docs/recast/reports/R2_report_core_session.md`「message_manager」节）证据
═══════════════════════════════════════════════════════════════════════════════════

| R2 行为描述（行号） | 测试 |
|---|---|
| 行为要点 1（L380）：已回复 id 集合 = tool 消息的非空 `tool_call_id`；未回复的补 `[用户中断]` 并置 repaired | `test_scenario10_repair_fills_missing`、`test_repair_ignores_empty_tool_call_id` |
| 行为要点 2（L381）：仅当第 1 步修复过、且末条为 tool → 追加合成 assistant（content 与 thinking 同为 SYNTHETIC_THINKING） | `test_scenario11_repair_appends_synthetic`、`test_scenario12_repair_no_synthetic_without_fix` |
| 行为要点 3（L382）：修复发生时有日志「repair: 修复了打断后的消息序列」 | `test_repair_logs_once_and_only_when_repaired` |
| 边界行为 1（L400）/补丁痕迹 1（L412）：遍历中追加（脆弱写法，结果正确） | 实现改为稳定快照遍历；`test_repair_is_idempotent` 锁定结果等价 |
| 边界行为 2（L401）：空判与 `view()[-1]` 依赖负索引 | `test_view_negative_index`、`test_scenario11_repair_appends_synthetic` |
| 边界行为 3（L402）：只处理「缺失工具结果」与「尾部 tool 无 assistant」两形态 | `test_repair_ignores_other_anomalies` |
| 兼容怪癖（spec L84）：修复对缺 id 的调用项跳过、中断补齐缺 id 即异常 | `test_repair_skips_tool_call_without_id`、`test_append_interrupted_tools_without_id_raises` |
| 死代码（L619）：`count_role` / `clear_and_rebuild` / `compress_and_rebuild` 无调用方 | `test_snapshot_baseline_scripts`（删除接口的对照处理见该测试注释） |
"""
from __future__ import annotations

import ast
import copy
import json
import re
import sys
from pathlib import Path

import pytest

from narnat_agent.messages import (
    INTERRUPTED_TOOL_RESULT,
    SYNTHETIC_THINKING,
    MessageStore,
    MessageView,
)
from narnat_agent.messages.tokens import (
    CJK_RE,
    MSG_OVERHEAD,
    TOKEN_PER_CJK,
    TOKEN_PER_OTHER,
    estimate_message_tokens,
    estimate_text_tokens,
)

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "messages"
BASELINE_FILE = (
    Path(__file__).resolve().parents[1] / "baseline" / "data" / "message_list.json"
)

# 有意删除的接口（T2.4 要求 3；R2 报告 L619「死代码」+ 本次消费方复验）：
# 基线 `message_list.scripts` 中用到这些接口的用例不可再对照，逐项给出理由。
REMOVED_INTERFACE_CASES = {
    "clear_and_rebuild": "MessageList.clear_and_rebuild 与 replace_all 重复且无调用方",
    "compress_and_rebuild_with_summary": "MessageList.compress_and_rebuild 无调用方；重建归 compression 积木",
    "compress_and_rebuild_no_summary": "同上",
}


class FakeLog:
    """日志端口桩：记录 `(模块名, 消息)` 调用序列。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def info(self, module: str, message: str) -> None:
        self.calls.append((module, message))


# ═══════════════════════════════════════════════════════════════
# 1. spec Scenario 覆盖（15 项）
# ═══════════════════════════════════════════════════════════════


def test_scenario01_system_first():
    """Scenario 1：构建即含 system 首条，后续追加依次排在其后。"""
    store = MessageStore("你是助手")
    assert store.view().to_list() == [{"role": "system", "content": "你是助手"}]
    store.append_user("你好")
    store.append_assistant("回复")
    assert [m["role"] for m in store.view()] == ["system", "user", "assistant"]
    assert len(store) == 3


def test_scenario02_assistant_optional_fields():
    """Scenario 2：assistant 可选字段写入规则（空内容记 null、可选字段按是否显式给出决定）。"""
    store = MessageStore("S")
    store.append_assistant("")
    store.append_assistant(None)
    store.append_assistant("正文")
    store.append_assistant("", thinking="")
    store.append_assistant("", thinking_signature="")
    store.append_assistant("", tool_calls=[])
    store.append_assistant("", tool_calls=[{"id": "c1"}])
    msgs = store.view().to_list()
    assert msgs[1] == {"role": "assistant", "content": None}
    assert msgs[2] == {"role": "assistant", "content": None}
    assert msgs[3] == {"role": "assistant", "content": "正文"}
    assert msgs[4] == {"role": "assistant", "content": None, "thinking": ""}  # 空串合法
    assert msgs[5] == {"role": "assistant", "content": None, "thinking_signature": ""}
    assert msgs[6] == {"role": "assistant", "content": None}  # 空 tool_calls 不写字段
    assert msgs[7] == {
        "role": "assistant", "content": None, "tool_calls": [{"id": "c1"}],
    }
    # 未给出 thinking/thinking_signature 时字段不出现
    assert "thinking" not in msgs[3] and "thinking_signature" not in msgs[3]


def test_scenario03_tool_message_structure():
    """Scenario 3：tool 消息结构为 tool_call_id + content。"""
    store = MessageStore("S")
    store.append_tool_result("call_1", "文件内容")
    assert store.view()[-1] == {
        "role": "tool", "tool_call_id": "call_1", "content": "文件内容",
    }


def test_scenario04_view_hides_internal_list():
    """Scenario 4：视图/导出列表都拿不到内部列表结构，增删它们不影响内部序列。"""
    store = MessageStore("S")
    store.append_user("u")
    exported = store.view().to_list()
    exported.append({"role": "user", "content": "注入"})
    exported.clear()
    assert len(store) == 2
    assert [m["content"] for m in store.view()] == ["S", "u"]


def test_scenario05_view_is_live():
    """Scenario 5：视图为实时视图——追加与整体替换后长度/末条立即反映。"""
    store = MessageStore("S")
    view = store.view()
    store.append_user("u1")
    assert len(view) == 2
    assert view[-1]["content"] == "u1"
    store.replace_all([{"role": "system", "content": "新系统"}])
    assert len(view) == 1
    assert view[-1]["content"] == "新系统"


def test_scenario06_append_system_skill():
    """Scenario 6：技能注入以新的 system 消息追加在序列中，首条系统提示词不被覆盖。"""
    store = MessageStore("系统提示词")
    store.append_user("问题")
    store.append_system("技能内容")
    assert [m["role"] for m in store.view()] == ["system", "user", "system"]
    assert store.view()[0]["content"] == "系统提示词"
    assert store.view()[-1]["content"] == "技能内容"


def test_scenario07_replace_all_atomic():
    """Scenario 7：整体替换——旧消息整体消失、新列表整体生效。"""
    store = MessageStore("旧系统")
    store.append_user("旧消息")
    store.append_assistant("旧回复")
    new_messages = [
        {"role": "system", "content": "新系统"},
        {"role": "user", "content": "新消息"},
    ]
    store.replace_all(new_messages)
    assert store.view().to_list() == new_messages
    assert len(store) == 2
    # 替换不与调用方列表共享结构（浅拷贝元素，外部改列表不影响内部）
    new_messages.append({"role": "user", "content": "追加"})
    assert len(store) == 2


def test_scenario08_interrupted_partial():
    """Scenario 8：3 个调用仅第 1 个完成时中断 → 按序补第 2、3 个 `[用户中断]` 结果。"""
    store = MessageStore("S")
    calls = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
    store.append_user("跑三个命令")
    store.append_assistant("", tool_calls=calls)
    store.append_tool_result("c1", "完成")
    store.append_interrupted_tools(calls, {"c1"})
    assert store.view().to_list()[-2:] == [
        {"role": "tool", "tool_call_id": "c2", "content": INTERRUPTED_TOOL_RESULT},
        {"role": "tool", "tool_call_id": "c3", "content": INTERRUPTED_TOOL_RESULT},
    ]


def test_scenario09_interrupted_all_completed():
    """Scenario 9：本轮全部已完成 → 不追加任何补齐消息。"""
    store = MessageStore("S")
    calls = [{"id": "c1"}, {"id": "c2"}]
    store.append_assistant("", tool_calls=calls)
    store.append_tool_result("c1", "完成1")
    store.append_tool_result("c2", "完成2")
    before = store.view().to_list()
    store.append_interrupted_tools(calls, {"c1", "c2"})
    assert store.view().to_list() == before


def test_scenario10_repair_fills_missing():
    """Scenario 10：存在未回复工具调用 → 按调用顺序补齐 `[用户中断]`，序列不再悬空。"""
    store = MessageStore("S")
    store.append_user("跑两个命令")
    store.append_assistant("", tool_calls=[{"id": "t1"}, {"id": "t2"}], thinking="思考")
    store.append_tool_result("t1", "完成")
    assert store.repair() is True
    msgs = store.view().to_list()
    assert [m["role"] for m in msgs] == [
        "system", "user", "assistant", "tool", "tool", "assistant",
    ]
    assert msgs[4] == {
        "role": "tool", "tool_call_id": "t2", "content": INTERRUPTED_TOOL_RESULT,
    }
    # 序列不再悬空：每个 tool_call id 都有对应的 tool 消息
    replied = {m.get("tool_call_id") for m in msgs if m["role"] == "tool"}
    assert {tc["id"] for m in msgs if "tool_calls" in m for tc in m["tool_calls"]} <= replied


def test_scenario11_repair_appends_synthetic():
    """Scenario 11：修复追加了工具消息且末条为 tool → 追加合成 assistant（正文=思考=占位）。"""
    store = MessageStore("S")
    store.append_assistant("", tool_calls=[{"id": "t1"}], thinking="思考")
    assert store.repair() is True
    last = store.view()[-1]
    assert last == {
        "role": "assistant",
        "content": SYNTHETIC_THINKING,
        "thinking": SYNTHETIC_THINKING,
    }
    assert "tool_calls" not in last  # 无工具调用字段


def test_scenario12_repair_no_synthetic_without_fix():
    """Scenario 12：无缺失工具结果、仅末条恰为 tool → 不做任何追加。"""
    store = MessageStore("S")
    store.append_user("u")
    store.append_assistant("", tool_calls=[{"id": "c1"}], thinking="思考")
    store.append_tool_result("c1", "结果")
    before = store.view().to_list()
    assert store.repair() is False
    assert store.view().to_list() == before
    assert [m["role"] for m in store.view()] == ["system", "user", "assistant", "tool"]


def test_scenario13_repair_duplicate_id_once():
    """Scenario 13：同一工具调用 id 在多条 assistant 中出现且均未回复 → 只补一条。"""
    store = MessageStore("S")
    store.append_assistant("", tool_calls=[{"id": "dup"}])
    store.append_assistant("", tool_calls=[{"id": "dup"}])
    assert store.repair() is True
    tools = [m for m in store.view() if m["role"] == "tool"]
    assert tools == [
        {"role": "tool", "tool_call_id": "dup", "content": INTERRUPTED_TOOL_RESULT},
    ]


def test_scenario14_message_dict_shared():
    """Scenario 14（兼容怪癖）：视图/导出列表中的消息字典与内部共享，可改写字段内容。"""
    store = MessageStore("S")
    store.append_user("原文")
    store.view()[1]["content"] = "经视图改写"
    assert store.view()[1]["content"] == "经视图改写"
    store.view().to_list()[1]["content"] = "经导出列表改写"
    assert store.view()[1]["content"] == "经导出列表改写"


def test_scenario15_replace_all_empty():
    """Scenario 15（兼容怪癖）：以空列表整体替换即清空，系统不补回 system 首条、无保护。"""
    store = MessageStore("S")
    store.append_user("u")
    store.replace_all([])
    assert len(store) == 0
    assert store.view().to_list() == []
    # 任意长度（无最小长度保护）：单条非 system 消息同样生效
    store.replace_all([{"role": "user", "content": "只有一条 user"}])
    assert store.view().to_list() == [{"role": "user", "content": "只有一条 user"}]


# ═══════════════════════════════════════════════════════════════
# 2. repair 全分支
# ═══════════════════════════════════════════════════════════════


def test_repair_is_idempotent():
    """多次调用幂等：第二次起不再追加、序列保持不变。"""
    store = MessageStore("S")
    store.append_user("u")
    store.append_assistant("", tool_calls=[{"id": "t1"}, {"id": "t2"}])
    assert store.repair() is True
    first = copy.deepcopy(store.view().to_list())
    assert store.repair() is False
    assert store.repair() is False
    assert store.view().to_list() == first


def test_repair_skips_tool_call_without_id():
    """修复对缺 id 的调用项跳过，同一消息中的其它调用照常补齐。"""
    store = MessageStore("S")
    store.append_assistant(
        "", tool_calls=[{"function": {"name": "Read"}}, {"id": "keep"}],
    )
    assert store.repair() is True
    assert [m for m in store.view() if m["role"] == "tool"] == [
        {"role": "tool", "tool_call_id": "keep", "content": INTERRUPTED_TOOL_RESULT},
    ]


def test_append_interrupted_tools_without_id_raises():
    """中断补齐直接读取 id：缺 id 即异常（与修复的跳过规则不同）。"""
    store = MessageStore("S")
    with pytest.raises(KeyError):
        store.append_interrupted_tools([{"function": {"name": "Read"}}], set())


def test_repair_ignores_empty_tool_call_id():
    """已回复集合只取非空 tool_call_id：空 id 的 tool 消息不算已回复。"""
    store = MessageStore("S")
    store.append_tool_result("", "空 id 结果")
    store.append_assistant("", tool_calls=[{"id": "c1"}])
    assert store.repair() is True
    assert [m for m in store.view() if m["role"] == "tool"] == [
        {"role": "tool", "tool_call_id": "", "content": "空 id 结果"},
        {"role": "tool", "tool_call_id": "c1", "content": INTERRUPTED_TOOL_RESULT},
    ]


def test_repair_ignores_other_anomalies():
    """只处理两种形态：孤儿 tool 消息、顺序错乱（tool 在 assistant 之前）均不修。"""
    store = MessageStore("S")
    store.append_tool_result("orphan", "无对应 assistant 的结果")
    store.append_user("u")
    store.append_assistant("已回复轮", tool_calls=[{"id": "c1"}])
    store.append_tool_result("c1", "结果")
    before = copy.deepcopy(store.view().to_list())
    assert store.repair() is False
    assert store.view().to_list() == before

    # 顺序错乱：tool 消息出现在其 assistant 之前 → 视为已回复，只补尾部（此处末条为 assistant）
    store2 = MessageStore("S")
    store2.append_tool_result("c9", "先到的结果")
    store2.append_assistant("", tool_calls=[{"id": "c9"}])
    before2 = copy.deepcopy(store2.view().to_list())
    assert store2.repair() is False
    assert store2.view().to_list() == before2


def test_repair_without_log_sink():
    """未注入日志端口时修复照常工作（日志调用静默丢弃）。"""
    store = MessageStore("S")
    store.append_assistant("", tool_calls=[{"id": "c1"}])
    assert store.repair() is True
    assert store.view()[-1]["content"] == SYNTHETIC_THINKING


def test_repair_logs_once_and_only_when_repaired():
    """修复发生时有且仅有一条 info 日志；无缺陷序列不写日志。"""
    log = FakeLog()
    store = MessageStore("S", log=log)
    store.append_assistant("", tool_calls=[{"id": "c1"}])
    assert store.repair() is True
    assert log.calls == [("messages", "repair: 修复了打断后的消息序列")]
    assert store.repair() is False  # 幂等：不再修复
    assert len(log.calls) == 1

    clean = MessageStore("S", log=log)
    clean.append_assistant("无工具回复")
    assert clean.repair() is False
    assert len(log.calls) == 1


# ═══════════════════════════════════════════════════════════════
# 3. 视图不可变性（结构）
# ═══════════════════════════════════════════════════════════════

_MUTATING_NAMES = (
    "append", "append_system", "append_user", "append_assistant",
    "append_tool_result", "append_interrupted_tools", "replace_all", "repair",
    "clear", "extend", "insert", "pop", "remove", "sort", "reverse",
    "__setitem__", "__delitem__", "__iadd__", "__imul__",
)


def test_view_is_structurally_read_only():
    """视图结构上无任何修改内部列表的入口（不是 list，也无增删改方法）。"""
    view = MessageStore("S").view()
    assert isinstance(view, MessageView)
    assert not isinstance(view, list)
    for name in _MUTATING_NAMES:
        assert not hasattr(view, name), f"视图不应暴露 {name}"


def test_view_negative_index():
    """视图下标支持负索引（-1 为末条，修复路径依赖该语义）。"""
    store = MessageStore("S")
    store.append_user("u1")
    store.append_user("u2")
    view = store.view()
    assert view[-1]["content"] == "u2"
    assert view[-2]["content"] == "u1"
    with pytest.raises(IndexError):
        _ = view[3]


# ═══════════════════════════════════════════════════════════════
# 4. 结构硬约束（design D1/D13）：只依赖标准库、无模块级可变状态
# ═══════════════════════════════════════════════════════════════

_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PACKAGE_PARTS = ("narnat_agent", "messages")


def _resolve_relative(path: Path, level: int, module: str | None) -> str:
    """把相对导入解析为绝对模块名（`from .store import x` → narnat_agent.messages.store）。"""
    pkg_parts = list(_PACKAGE_PARTS)
    if path.name != "__init__.py":
        pkg_parts.append(path.stem)
    base = pkg_parts[: len(pkg_parts) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def test_dependency_purity():
    """messages 不 import 新包其它积木；模块级无小写可变状态（常量全大写）。"""
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "messages 积木文件缺失"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    assert node.module, f"{path.name}:{node.lineno} 空绝对导入"
                    assert node.module.split(".")[0] != "narnat_agent", (
                        f"{path.name}:{node.lineno} 绝对导入新包模块"
                    )
                else:
                    target = _resolve_relative(path, node.level, node.module)
                    assert target.split(".")[:2] == list(_PACKAGE_PARTS), (
                        f"{path.name}:{node.lineno} 相对导入越出积木：{target}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root != "narnat_agent", f"{path.name}:{node.lineno} 导入新包"
                    assert root in sys.stdlib_module_names, (
                        f"{path.name}:{node.lineno} 非标准库依赖：{alias.name}"
                    )
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("__"):
                    assert _CONST_NAME_RE.match(target.id), (
                        f"{path.name}:{node.lineno} 模块级可变名 {target.id}"
                    )


def test_public_exports():
    """聚合导出齐备（`__all__` 全部可解析）。"""
    import narnat_agent.messages as messages

    for name in messages.__all__:
        assert hasattr(messages, name), name
    assert messages.SYNTHETIC_THINKING == SYNTHETIC_THINKING
    assert messages.INTERRUPTED_TOOL_RESULT == INTERRUPTED_TOOL_RESULT


# ═══════════════════════════════════════════════════════════════
# 5. 快照对照（v2/tests/baseline/data/message_list.json，旧实现基准）
# ═══════════════════════════════════════════════════════════════


def _load_baseline() -> dict:
    if not BASELINE_FILE.exists():  # pragma: no cover - 基准缺失时跳过（任务书允许）
        pytest.skip(f"无 messages 基准：{BASELINE_FILE}")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def _run_script(script: dict) -> dict:
    """按基线的 op 脚本执行同一串操作，返回与基线同构的结果。"""
    store = MessageStore(script["system_prompt"])
    for op in script["ops"]:
        kind = op["op"]
        if kind == "append_system":
            store.append_system(op["content"])
        elif kind == "append_user":
            store.append_user(op["content"])
        elif kind == "append_assistant":
            kwargs: dict = {}
            if "thinking" in op:
                kwargs["thinking"] = op["thinking"]
            if "thinking_signature" in op:
                kwargs["thinking_signature"] = op["thinking_signature"]
            if "tool_calls" in op:
                kwargs["tool_calls"] = copy.deepcopy(op["tool_calls"])
            store.append_assistant(op["content"], **kwargs)
        elif kind == "append_tool_result":
            store.append_tool_result(op["tool_call_id"], op["result"])
        elif kind == "append_interrupted_tools":
            store.append_interrupted_tools(
                copy.deepcopy(op["tool_calls"]), set(op["completed_ids"]))
        elif kind == "replace_all":
            store.replace_all(copy.deepcopy(op["messages"]))
        else:
            raise AssertionError(f"对照用例出现已删除接口的 op：{kind}")
    msgs = store.view().to_list()
    return {
        "messages": msgs,
        "len": len(store),
        "counts": {
            role: sum(1 for m in msgs if m.get("role") == role)
            for role in ("system", "user", "assistant", "tool")
        },
    }


def _removed_interface_case_ids(cases: dict) -> set[str]:
    return {
        cid for cid, case in cases.items()
        if any(op["op"] in ("clear_and_rebuild", "compress_and_rebuild")
               for op in case["input"]["script"]["ops"])
    }


def test_snapshot_baseline_scripts():
    """快照对照：基线 `message_list.scripts` 逐用例比对（messages/len/counts 全等）。

    基线中含 `clear_and_rebuild` / `compress_and_rebuild` 的 3 个用例不可对照——这两个接口
    按 T2.4 要求 3 有意删除（R2 报告 L619 记录其无调用方，本次全仓库复验亦无消费方；压缩
    重建的编排归 compression 积木，经 `replace_all` 生效）。此处以用例 id 白名单硬锁：
    基线一旦变化（新增/改名删除接口用例）即失败，不做静默跳过。剩余 4 个用例逐字节比对。
    """
    data = _load_baseline()
    cases = {c["id"]: c for c in data["groups"]["message_list.scripts"]}
    removed = _removed_interface_case_ids(cases)
    assert removed == set(REMOVED_INTERFACE_CASES), removed

    compared = []
    for cid, case in cases.items():
        if cid in removed:
            continue
        got = _run_script(case["input"]["script"])
        assert got == case["result"], f"用例 {cid} 与基线不一致：\n旧={case['result']}\n新={got}"
        compared.append(cid)
    assert compared == [
        "basic_flow",
        "empty_content_and_optional",
        "interrupted_repair",
        "replace_all",
    ]


def test_snapshot_synthetic_thinking_constant():
    """快照对照：合成占位常量与基线逐字一致（跨模块判定依据，禁止改文本）。"""
    data = _load_baseline()
    case = data["groups"]["message_list.SYNTHETIC_THINKING"][0]
    assert SYNTHETIC_THINKING == case["result"]
    assert SYNTHETIC_THINKING == "（用户中断了工具执行）"
    assert SYNTHETIC_THINKING  # 非空占位（思考模式校验要求）


def test_removed_interface_capability_kept_by_replace_all():
    """接口删除不丢能力：被删接口用例的最终序列可由 `replace_all` 完整复现。"""
    data = _load_baseline()
    cases = {c["id"]: c for c in data["groups"]["message_list.scripts"]}
    for cid in REMOVED_INTERFACE_CASES:
        expected = cases[cid]["result"]
        store = MessageStore("S")
        store.append_user("旧消息")
        store.replace_all(copy.deepcopy(expected["messages"]))
        msgs = store.view().to_list()
        assert msgs == expected["messages"], cid
        assert len(store) == expected["len"], cid


# ═══════════════════════════════════════════════════════════════
# 6. token 估算（T3.11 唯一实现归本积木）—— 系数矩阵
# ═══════════════════════════════════════════════════════════════

# 基准对照面（本积木不重复承载，避免同一口径多处断言）：
# - `tests/unit/test_tools_framework.py`（tools 侧转发壳 × `token_estimate.json` 两组 = 15 例）；
# - `tests/unit/test_compression.py`（compression 侧转发壳 × 同基准 +
#   `compressor.json` 的 `select_cut_index` 组经尾部累计估价间接锁定）。
# 本组只锁定系数/取整/分类矩阵本身（实现变更即失败，不依赖基准文件）。


def test_tokens_coefficients_are_contract():
    """系数与框架开销即契约（specs/compression 与 specs/tools-file 同源口径）。"""
    assert (TOKEN_PER_CJK, TOKEN_PER_OTHER, MSG_OVERHEAD) == (0.7, 0.25, 8)


@pytest.mark.parametrize("text,cjk,other,expected", [
    ("", 0, 0, 0),                     # 空文本（ceil 不适用）
    ("abcd", 0, 4, 1),                 # 4×0.25 = 1.0
    ("abcde", 0, 5, 2),                # 1.25 → 2
    ("a" * 8, 0, 8, 2),                # 2.0 恰为整数不 +1
    (" \t\n", 0, 3, 1),                # 空白按其余系数：0.75 → 1
    ("中", 1, 0, 1),                   # 0.7 → 1
    ("中中", 2, 0, 2),                 # 1.4 → 2
    ("你好世界", 4, 0, 3),              # 2.8 → 3
    ("中" * 10, 10, 0, 7),             # 7.0 恰为整数
    ("中" * 11, 11, 0, 8),             # 7.7 → 8
    ("中a", 1, 1, 1),                  # 0.7+0.25 = 0.95
    ("中ab", 1, 2, 2),                 # 0.7+0.5 = 1.2 → 2
    ("\u3400", 1, 0, 1),               # CJK 扩展 A 计入
    ("\uf900", 1, 0, 1),               # 兼容表意区计入
    ("ひらがな", 4, 0, 3),              # 假名计入：2.8 → 3
    ("한글", 2, 0, 2),                 # 谚文计入：1.4 → 2
    ("（全角）！？", 6, 0, 5),          # 全角区计入：4.2 → 5
    ("😀", 0, 1, 1),                   # 非 CJK 区（emoji）按其余系数
], ids=lambda v: v if isinstance(v, str) and len(v) <= 6 else None)
def test_estimate_text_tokens_matrix(text, cjk, other, expected):
    """文本系数矩阵：分类计数（CJK 正则命中数）与向上取整结果双重锁定。"""
    matched = len(CJK_RE.findall(text))
    assert (matched, len(text) - matched) == (cjk, other)
    assert estimate_text_tokens(text) == expected


@pytest.mark.parametrize("msg,expected", [
    ({}, 8),                                                    # 空消息只计框架开销
    ({"role": "user", "content": None}, 8),
    ({"role": "user", "content": ""}, 8),
    ({"content": "abcd"}, 9),                                   # 8 + 1
    ({"role": "user", "content": "你好"}, 10),                   # 8 + 2
    ({"role": "assistant", "content": "", "thinking": "思考中"}, 11),  # 8 + 3
    ({"role": "assistant", "content": [{"type": "text", "text": "块列表"}]}, 18),  # 块列表按文本近似：8+10
    ({"tool_calls": [{"function": {"name": "Read", "arguments": "abcd"}}]}, 10),  # 8+1+1
    ({"tool_calls": [{"function": {"name": "Read"}}]}, 9),       # 参数缺失按空
    ({"tool_calls": [{"name": "Read"}]}, 8),                     # 无 function 键 → 不计
    ({"tool_calls": []}, 8),
], ids=["empty", "content_none", "content_empty", "content_ascii", "content_cjk",
        "thinking", "block_list", "tool_call_full", "tool_call_no_args",
        "tool_call_no_function", "tool_calls_empty"])
def test_estimate_message_tokens_matrix(msg, expected):
    """消息级汇总矩阵：8 框架开销 + content / thinking / tool_calls（名称与参数）。"""
    assert estimate_message_tokens(msg) == expected
