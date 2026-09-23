"""T3.10 补充基准用例集 —— ui 交互面（统计栏 / 启动横幅 / 打断提示 / Tab 补全）。

**纯数据**：不导入任何实现（新旧实现共用同一份输入），供
`extract_old_ui_interaction.py`（旧实现）与 `v2/tests/unit/test_ui_interaction.py`
（新实现）两侧消费。

覆盖范围（旧实现侧对应模块）：
| 组 | 旧实现 | 新实现 |
|---|---|---|
| `ui_design.show_stats` | `narnat_agent.ui.ui_design.show_stats` | `ui.stream.stats_line` + 分隔线（`UiSink.show_stats`） |
| `ui_design.show_header` | `narnat_agent.ui.ui_design.show_header` | `ui.prompt.write_banner` |
| `ui_design.show_interrupted` | `narnat_agent.ui.ui_design.show_interrupted` | `ui.stream.UiSink.abort(None)` |
| `session_commands._CommandCompleter.get_completions` | 旧补全器（mgr 鸭子接口） | `ui.commands.CommandCompleter`（命令源端口） |

确定性：终端宽度固定为 `TERM_WIDTH`（两测同值）、真彩 + 默认色板、非纯文本模式；
用例只含数值与字符串，不含时间戳。
"""
from __future__ import annotations

import copy

# 终端宽度（分隔线宽度由此决定；两侧一致）
TERM_WIDTH = 100

# ── 统计栏参数矩阵（数值边界 + 五个开关组合 + 字符串布尔怪癖） ──

_FULL_SWITCHES = {
    "show_cost": True,
    "show_balance": True,
    "max_tokens": 128000,
    "show_ratio": True,
    "context_window": 1000000,
}

STATS_CASES = [
    {
        "id": "full|input_12345_output_678_cache_50pct",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 12345, "output_tokens": 678, "cache_ratio": 0.5,
                  "cost": 12.3456, "balance": 88.5, "thinking_effort": "高"},
    },
    {
        "id": "cache|zero_hidden",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 1200, "output_tokens": 300, "cache_ratio": 0.0,
                  "cost": 1.0, "balance": 10.0, "thinking_effort": "中"},
    },
    {
        "id": "cache|capped_at_100pct",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 1200, "output_tokens": 300, "cache_ratio": 1.5,
                  "cost": 1.0, "balance": 10.0, "thinking_effort": "低"},
    },
    {
        "id": "tokens|below_1000_raw",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 999, "output_tokens": 1, "cache_ratio": 0.0,
                  "cost": 0.0, "balance": 0.0, "thinking_effort": "高"},
    },
    {
        "id": "tokens|max_rounding_1500",
        "switches": {**_FULL_SWITCHES, "max_tokens": 1500},
        "stats": {"input_tokens": 1000, "output_tokens": 1000, "cache_ratio": 0.0,
                  "cost": 0.0, "balance": 0.0, "thinking_effort": "高"},
    },
    {
        "id": "tokens|max_below_1000_raw",
        "switches": {**_FULL_SWITCHES, "max_tokens": 500},
        "stats": {"input_tokens": 0, "output_tokens": 0, "cache_ratio": 0.0,
                  "cost": 0.0, "balance": 0.0, "thinking_effort": "max"},
    },
    {
        "id": "balance|zero_hidden",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 100, "output_tokens": 200, "cache_ratio": 0.0,
                  "cost": 2.5, "balance": 0.0, "thinking_effort": "高"},
    },
    {
        "id": "balance|negative_hidden",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 100, "output_tokens": 200, "cache_ratio": 0.0,
                  "cost": 2.5, "balance": -3.0, "thinking_effort": "高"},
    },
    {
        "id": "ratio|invalid_context_window",
        "switches": {**_FULL_SWITCHES, "context_window": 0},
        "stats": {"input_tokens": 100, "output_tokens": 200, "cache_ratio": 0.0,
                  "cost": 0.0, "balance": 0.0, "thinking_effort": "高"},
    },
    {
        "id": "ratio|zero_input",
        "switches": dict(_FULL_SWITCHES),
        "stats": {"input_tokens": 0, "output_tokens": 200, "cache_ratio": 0.0,
                  "cost": 0.0, "balance": 0.0, "thinking_effort": "高"},
    },
    {
        "id": "switches|all_off",
        "switches": {"show_cost": False, "show_balance": False, "max_tokens": 65536,
                     "show_ratio": False, "context_window": 1000000},
        "stats": {"input_tokens": 4321, "output_tokens": 8765, "cache_ratio": 0.25,
                  "cost": 9.9999, "balance": 77.0, "thinking_effort": "高"},
    },
    {
        "id": "quirks|string_false_switch_is_truthy",
        "switches": {**_FULL_SWITCHES, "show_cost": "false", "show_balance": "false"},
        "stats": {"input_tokens": 1500, "output_tokens": 2500, "cache_ratio": 0.0,
                  "cost": 3.1416, "balance": 5.0, "thinking_effort": "高"},
    },
]

# ── 启动横幅（模型名，主色，两空格缩进）+ 分隔线 ──

BANNER_CASES = [
    {"id": "model|narnat", "model": "narnat"},
    {"id": "model|long_name", "model": "narnat-agent-deepseek-v4-flash"},
]

# ── 默认打断提示（无自定义消息） ──

INTERRUPT_CASES = [
    {"id": "message|none_default_prompt"},
]

# ── Tab 补全（命令源状态 + 光标前文本） ──

_FREE_COMMANDS = {
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
_ROOT_COMMANDS = {**_FREE_COMMANDS, "/explore": "探索分支",
                  "/rm": "删除子会话", "/exit": "退出会话"}
_CHILD_COMMANDS = {k: v for k, v in _FREE_COMMANDS.items()
                   if k not in ("/save", "/rm")}
_CHILD_COMMANDS.update({"/done": "完成探索分支", "/exit": "暂离探索分支"})

_NAMES = ["父会话", "父会话/子会话", "会话A", "会话B/嵌套会话"]
_RM_NAMES = ["父会话/子会话", "会话A"]
_THINKING = ["high", "max"]
_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "qwen-max"]

_SKILL_TREE = [
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


def _state(commands, *, names=(), rm_names=(), thinking=(), models=(), skill_tree=None):
    """构造补全用例的命令源状态（两侧按各自接口名消费同一份数据）。"""
    return {
        "commands": dict(commands),
        "names": list(names),
        "rm_names": list(rm_names),
        "thinking": list(thinking),
        "models": list(models),
        "skill_tree": copy.deepcopy(skill_tree if skill_tree is not None else _SKILL_TREE),
    }


COMPLETION_CASES = [
    {"id": "command|root_all", "text": "/",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "command|free_all", "text": "/",
     "state": _state(_FREE_COMMANDS)},
    {"id": "command|child_all", "text": "/",
     "state": _state(_CHILD_COMMANDS)},
    {"id": "command|prefix_sa", "text": "/sa",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "command|prefix_upper_case", "text": "/SA",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "command|no_match", "text": "/zz",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "command|unknown_not_in_state", "text": "/explore ",
     "state": _state(_FREE_COMMANDS)},
    {"id": "names|cd_list_all", "text": "/cd ",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "names|cd_prefix", "text": "/cd 会",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "names|cd_nested_prefix", "text": "/cd 父会话/子",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "names|cd_nested_prefix_partial_path", "text": "/cd 父/子",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "names|cd_no_match", "text": "/cd 无",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "names|rm_list_all", "text": "/rm ",
     "state": _state(_ROOT_COMMANDS, rm_names=_RM_NAMES)},
    {"id": "names|rm_prefix", "text": "/rm 会",
     "state": _state(_ROOT_COMMANDS, rm_names=_RM_NAMES)},
    {"id": "names|thinking_list_all", "text": "/thinking ",
     "state": _state(_ROOT_COMMANDS, thinking=_THINKING)},
    {"id": "names|mode_prefix", "text": "/mode deepseek-v4-p",
     "state": _state(_ROOT_COMMANDS, models=_MODELS)},
    {"id": "static|ls_option", "text": "/ls ",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "static|save_arg_no_candidate", "text": "/save ",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "static|third_word_no_candidate", "text": "/cd 父会话 子",
     "state": _state(_ROOT_COMMANDS, names=_NAMES)},
    {"id": "static|non_slash_input", "text": "看看这个",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "static|leading_space_slash", "text": "   /sa",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|dir_prefix", "text": "/skill d",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|leaf_partial_system", "text": "/skill code",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|leaf_complete_no_candidate", "text": "/skill code-review",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|dir_complete_enters", "text": "/skill docs",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|nested_dir_list", "text": "/skill docs/",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|nested_prefix", "text": "/skill docs/a",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|backslash_compat", "text": "/skill docs\\a",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|single_dir_enters", "text": "/skill single-skill",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|unknown_dir_no_candidate", "text": "/skill nope/",
     "state": _state(_ROOT_COMMANDS)},
    {"id": "skill|leaf_meta_project", "text": "/skill docs/g",
     "state": _state(_ROOT_COMMANDS)},
]


def completion_case_by_id(case_id: str) -> dict:
    """按 id 取补全用例（两侧消费入口，避免调用方各写一份查找）。"""
    for case in COMPLETION_CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)
