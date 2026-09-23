"""打印指定 run 的证据摘要（事件/标记时刻、关键帧屏幕片段、narnat 日志）。

用法：python evidence.py <run_dir 名称或前缀>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def pick_run(token: str) -> Path:
    cands = [p for p in RUNS.iterdir() if p.is_dir() and token in p.name]
    if not cands:
        raise SystemExit(f"没有匹配 {token} 的 run")
    return sorted(cands)[-1]


def main() -> None:
    run_dir = pick_run(sys.argv[1])
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    print(f"== {run_dir.name}")
    print("status:", result.get("status"))
    print("events:", result.get("events"))
    print("marks:", result.get("marks"))
    print("duration_sampled:", result.get("duration_sampled"))

    frames_path = run_dir / "frames.jsonl"
    if frames_path.is_file():
        frames = [json.loads(line) for line in frames_path.read_text(encoding="utf-8").splitlines()]
        print(f"frames: {len(frames)}")
        for f in frames[:3]:
            print(f"  [first] t={f['t']} :: " + " | ".join(f["text"].splitlines()[-3:]))
        for f in frames[-3:]:
            print(f"  [last ] t={f['t']} :: " + " | ".join(f["text"].splitlines()[-3:]))

    for log in sorted(run_dir.glob("narnat_*.log")):
        print(f"-- narnat log: {log.name}")
        for line in log.read_text(encoding="utf-8").splitlines():
            print("   " + line)


if __name__ == "__main__":
    main()
