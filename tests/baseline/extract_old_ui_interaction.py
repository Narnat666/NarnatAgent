"""T3.10 补充基准提取器 —— 旧实现 ui 交互面（统计栏 / 横幅 / 打断提示 / 补全）固化。

背景：T0.1 的 `extract_old.py` 只覆盖纯函数面，`ui_design.show_stats` / `show_header` /
`show_interrupted` 与 `session_commands._CommandCompleter` 均为"写 stdout 或依赖鸭子接口
对象"的路径，未纳入基准。T3.10 把交互层（流句柄统计栏、启动横幅、打断提示、Tab 补全）
搬入积木后，"行为零变化"需要这些路径的判据，故本提取器用"重定向 stdout + 固定终端宽度 /
真彩 / 默认色板 / 非纯文本模式"把该路径纳入基准。

用法:
    cd /d D:\\desktop\\NarnatAgent && python v2/tests/baseline/extract_old_ui_interaction.py

产出: v2/tests/baseline/ui/ui_interaction.json（UTF-8、indent=2、ensure_ascii=False、LF）

只调用读取/渲染类接口：不启动进程、不连网、不写用户数据（补全用例的会话/技能数据
全部为构造数据）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

BASELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASELINE_DIR.parents[2]
OUT_DIR = BASELINE_DIR / "ui"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASELINE_DIR))

import ui_interaction_cases as cases  # noqa: E402

from narnat_agent import output  # noqa: E402
from narnat_agent.ui import session_commands as SC  # noqa: E402
from narnat_agent.ui import ui_design as U  # noqa: E402

OUT_FILE = OUT_DIR / "ui_interaction.json"


class _StubManager:
    """补全器的旧鸭子接口替身（数据全部来自用例，无磁盘 / 会话状态）。"""

    def __init__(self, state: dict):
        self._state = state

    def available_commands(self) -> dict:
        return dict(self._state["commands"])

    def on_list_names_tree(self) -> list:
        return list(self._state["names"])

    def on_list_rm_names(self) -> list:
        return list(self._state["rm_names"])

    def on_list_thinking_options(self) -> list:
        return list(self._state["thinking"])

    def on_list_model_names(self) -> list:
        return list(self._state["models"])

    def on_list_skill_tree(self) -> list:
        return json.loads(json.dumps(self._state["skill_tree"]))


def _fix_environment() -> None:
    """固定影响输出的全局开关（不修改旧包文件，仅固定本进程状态）。"""
    os.environ["NARNAT_TERM_WIDTH"] = str(cases.TERM_WIDTH)
    output.set_plain(False)
    output._TRUECOLOR = True
    default_colors = {name: hexv for name, hexv, _is_bg in output._BASE_DEFS}
    output.apply_style({"colors": dict(default_colors)})


def _capture(call, *args) -> str:
    """在重定向 stdout 下执行一次界面调用，返回捕获的全部输出。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue()


def _extract_stats() -> list:
    out = []
    for case in cases.STATS_CASES:
        switches = case["switches"]
        output.DisplayState.show_cost = switches["show_cost"]
        output.DisplayState.show_balance = switches["show_balance"]
        output.DisplayState.max_tokens = switches["max_tokens"]
        output.DisplayState.show_ratio = switches["show_ratio"]
        output.DisplayState.context_window = switches["context_window"]
        stats = case["stats"]
        text = _capture(
            U.show_stats,
            stats["input_tokens"],
            stats["output_tokens"],
            stats["cache_ratio"],
            stats["cost"],
            stats["balance"],
            stats["thinking_effort"],
        )
        out.append({"id": case["id"], "input": case, "result": text})
    return out


def _extract_banner() -> list:
    return [
        {"id": case["id"], "input": case, "result": _capture(U.show_header, case["model"])}
        for case in cases.BANNER_CASES
    ]


def _extract_interrupted() -> list:
    return [
        {"id": case["id"], "input": case, "result": _capture(U.show_interrupted)}
        for case in cases.INTERRUPT_CASES
    ]


def _extract_completions() -> list:
    from prompt_toolkit.document import Document

    out = []
    for case in cases.COMPLETION_CASES:
        text = case["text"]
        completer = SC._CommandCompleter(_StubManager(case["state"]))
        completions = list(completer.get_completions(Document(text, len(text)), None))
        out.append({
            "id": case["id"],
            "input": case,
            "result": [
                {
                    "text": c.text,
                    "start_position": c.start_position,
                    "meta": c.display_meta_text,
                }
                for c in completions
            ],
        })
    return out


def main() -> int:
    _fix_environment()
    data = {
        "generated_by": "v2/tests/baseline/extract_old_ui_interaction.py",
        "source": "narnat_agent/ui/ui_design.py + ui/session_commands.py（旧实现）",
        "note": (
            "T3.10 补充基准：ui 交互面输出固化。固定 NARNAT_TERM_WIDTH="
            f"{cases.TERM_WIDTH}、非纯文本模式、真彩、默认色板。"
        ),
        "module": "narnat_agent.ui",
        "groups": {
            "ui_design.show_stats": _extract_stats(),
            "ui_design.show_header": _extract_banner(),
            "ui_design.show_interrupted": _extract_interrupted(),
            "session_commands._CommandCompleter.get_completions": _extract_completions(),
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for group, items in data["groups"].items():
        print(f"{group}: {len(items)} 用例")
    print(f"写出 {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
