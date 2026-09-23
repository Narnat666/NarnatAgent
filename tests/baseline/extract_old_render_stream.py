"""T3.9 补充基准提取器 —— 旧实现的 `StreamingRenderer` 流式路径输出固化。

背景：T0.1 的 `extract_old.py` 明确未覆盖 `StreamingRenderer.feed/flush/reset`
（该路径直接写 stdout，非纯函数——见 `README.md` 第 4 节）。T3.9 重组渲染管道
（解析 → 布局 → 着色）后，"行为零变化"需要流式路径的判据，故本提取器用
"重定向 stdout + 固定终端宽度/可见窗格/纯文本开关"把该路径纳入基准。

用法:
    cd /d D:\\desktop\\NarnatAgent && python v2/tests/baseline/extract_old_render_stream.py

产出: v2/tests/baseline/stream/render_stream.json（UTF-8、indent=2、ensure_ascii=False、LF）

确定性措施（与 extract_old.py 同口径）：
- 纯文本模式固定为 False、TrueColor 固定为 True、默认色板全表 apply_style；
- 终端宽度经 `NARNAT_TERM_WIDTH` 固定、srWindow 实测值固定为场景声明的 visible；
- 用例输入来自 `render_stream_cases.py`（纯数据，不导入实现），不含时间戳。

只调用读取/渲染类接口：不启动进程、不连网、不写用户数据。
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
OUT_DIR = BASELINE_DIR / "stream"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASELINE_DIR))

import render_stream_cases as cases  # noqa: E402

from narnat_agent import output  # noqa: E402
from narnat_agent.ui import renderer as R  # noqa: E402

OUT_FILE = OUT_DIR / "render_stream.json"


def _fix_environment() -> None:
    """固定影响输出的全局开关（不修改旧包文件，仅固定本进程状态）。"""
    output.set_plain(False)
    output._TRUECOLOR = True
    default_colors = {name: hexv for name, hexv, _is_bg in output._BASE_DEFS}
    output.apply_style({"colors": dict(default_colors)})


def _run(case: dict) -> str:
    """按场景操作序列驱动旧渲染器，返回捕获的全部 stdout。"""
    os.environ["NARNAT_TERM_WIDTH"] = str(case["width"])
    R._srwindow_cols = lambda: case["visible"]  # 固定二次校验（0 = 不可用）
    output.set_plain(bool(case["plain"]))
    buf = io.StringIO()
    renderer = R.StreamingRenderer()
    with contextlib.redirect_stdout(buf):
        for item in case["ops"]:
            op = item[0]
            arg = item[1] if len(item) > 1 else None
            if op == "feed":
                renderer.feed(arg)
            elif op == "flush":
                renderer.flush()
            elif op == "reset":
                renderer.reset()
            else:
                raise ValueError(f"未知操作: {op}")
    output.set_plain(False)
    return buf.getvalue()


def main() -> int:
    _fix_environment()

    group = []
    for case in cases.SCENARIOS:
        group.append({
            "id": case["id"],
            "input": {"width": case["width"], "plain": case["plain"],
                      "visible": case["visible"], "ops": [list(op) for op in case["ops"]]},
            "result": _run(case),
        })

    payload = {
        "generated_by": "v2/tests/baseline/extract_old_render_stream.py",
        "source": "narnat_agent.ui.renderer（旧实现，原位未动）",
        "note": ("流式路径基准：stdout 重定向捕获；plain=False、TrueColor=True、默认色板、"
                 "NARNAT_TERM_WIDTH 与 srWindow 固定为用例声明值；用例输入见 render_stream_cases.py"),
        "module": "narnat_agent.ui.renderer.StreamingRenderer",
        "groups": {"renderer.StreamingRenderer(feed/flush/reset)": group},
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"  render_stream.json  ({len(group)} 场景)  {payload['module']}")
    print(f"→ {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
