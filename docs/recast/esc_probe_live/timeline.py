"""打印指定 run 的帧时间线（t + 最后一行），用于报告引用。

用法：python timeline.py <run_id> [<run_id> ...]
输出同时写 timeline_raw.txt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def main() -> None:
    out_lines = []
    for token in sys.argv[1:]:
        cands = [p for p in RUNS.iterdir() if p.is_dir() and token in p.name]
        if not cands:
            out_lines.append(f"==== {token} 无匹配")
            continue
        run_dir = sorted(cands)[-1]
        out_lines.append(f"==== {run_dir.name}")
        frames = [json.loads(line) for line in
                  (run_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
        for f in frames:
            last = f["text"].splitlines()[-1] if f["text"] else ""
            out_lines.append(f"  {f['t']:>7.2f} | {last.strip()[:70]}")
    text = "\n".join(out_lines) + "\n"
    (HERE / "timeline_raw.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
