"""E2 实验驱动器（独立进程）：按键注入 + 屏幕采样，产出时间线证据。

由 runner.py 以子进程方式调用；自身不依赖 stdout（AttachConsole 后标准句柄失效），
结果一律写文件。

用法：
    python driver.py --pid <目标pid> --out <result.json> --frames <frames.jsonl>
                     --plan baseline [--duration 25] [--wait-ready 40] [--text 你好]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import winctl  # noqa: E402

# 按键计划（时间单位：秒，相对"回车注入完成"时刻 t0；间隔模拟人手节奏）
PLANS = {
    # 1 基线：回车后 120ms 单按 ESC
    "baseline": [(0.12, "esc")],
    # 2 错键+单 ESC：回车后 120ms 先按 `，260ms 再按 ESC
    "wrong_then_esc": [(0.12, "backtick"), (0.26, "esc")],
    # 3 交错连按：回车后 120ms 起 ` / ESC 交替各 5 轮，间隔 140ms
    "alternate": [(0.12 + 0.14 * i, "backtick" if i % 2 == 0 else "esc")
                  for i in range(10)],
    # 4 纯狂按：回车后等 1s，狂按 ESC ×10，间隔 100ms
    "rage": [(1.0 + 0.10 * i, "esc") for i in range(10)],
    # 5 延迟对照：回车后等 2s，单按 ESC
    "delayed": [(2.0, "esc")],
    # 探索用：不注入任何键（判定"迟到的打断"是否与按键相关）
    "none": [],
    # 探索用：交错连按 5 轮后，再狂按 ESC ×10（对应用户"后续狂按 esc 也没用"）
    "alternate_then_rage": ([(0.12 + 0.14 * i, "backtick" if i % 2 == 0 else "esc")
                             for i in range(10)]
                            + [(2.0 + 0.10 * i, "esc") for i in range(10)]),
    # 验证用（FIX-ESC）：Esc 后 10ms 紧跟错键——修复前 msvcrt 字节流源的
    # 20ms 判定窗口会把 Esc 判为转义序列前缀而吞掉（exp1 C04 条件）；
    # 事件源主路径下应即时中断。
    "esc_then_backtick": [(0.15, "esc"), (0.16, "backtick")],
    # 验证用（FIX-ESC）：密集交错（15ms 间隔）——修复前部分 Esc 被吞
    # （靠第二个 Esc 生效，exp1 C11/C12）；事件源下每个按键独立判定。
    "dense_alternate": [(0.12 + 0.015 * i, "backtick" if i % 2 == 0 else "esc")
                        for i in range(10)],
}

MARKERS = ("思考中", "已打断", "继续...", "最大输出:")


def line_counts(text: str) -> Counter:
    """屏幕文本按行计数（strip 后非空行），用于"新增行"差集检测。"""
    return Counter(line.strip() for line in text.splitlines() if line.strip())


def at_prompt(text: str) -> bool:
    """屏幕底部（最后 3 个非空行内）出现以 # 开头的提示符行。"""
    tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
    return any(ln.lstrip().startswith("#") for ln in tail)


def wait_prompt(io, timeout: float) -> bool:
    """等待提示符出现并稳定 1 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if at_prompt(io.screen_text()):
            time.sleep(1.0)
            if at_prompt(io.screen_text()):
                return True
        time.sleep(0.3)
    return False


def write_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--plan", default="baseline")
    ap.add_argument("--duration", type=float, default=25.0)
    ap.add_argument("--wait-ready", type=float, default=40.0)
    ap.add_argument("--text", default="你好")
    ap.add_argument("--keep-buffer", action="store_true",
                    help="不先注入 Ctrl+C 清空输入缓冲（默认清理残留编辑内容）")
    args = ap.parse_args()

    out_path, frames_path = Path(args.out), Path(args.frames)
    result = {"plan": args.plan, "pid": args.pid, "status": "unknown",
              "marks": {}, "events": [], "screen_before": "", "screen_final": "",
              "errors": []}
    frames = []

    io = None
    try:
        io = winctl.ConsoleIO(args.pid, attach_timeout=args.wait_ready)

        # 等待 UI 就绪：底部出现提示符 "#"
        if not wait_prompt(io, args.wait_ready):
            result["status"] = "not_ready"
            result["screen_final"] = io.screen_text()
            write_json(out_path, result)
            return 2

        # 清空输入缓冲：Ctrl+C 丢弃残留编辑内容（InputSession 会重建会话）
        if not args.keep_buffer:
            io.press("ctrl-c")
            time.sleep(1.0)
            if not wait_prompt(io, 10.0):
                result["status"] = "not_ready_after_clear"
                result["screen_final"] = io.screen_text()
                write_json(out_path, result)
                return 2

        screen_before = io.screen_text()
        result["screen_before"] = screen_before
        before_counts = line_counts(screen_before)

        # 注入输入文本，再注入回车；t0 = 回车注入完成时刻
        for ch in args.text:
            io.press(ch)
            time.sleep(0.06)
        time.sleep(0.08)
        io.press("enter")
        t0 = time.perf_counter()
        t0_epoch = time.time()

        schedule = list(PLANS[args.plan])
        events = []
        marks = {}
        last_text = None
        idx = 0
        end_after = None
        while True:
            elapsed = time.perf_counter() - t0
            if elapsed > args.duration:
                break
            # 到点注入
            while idx < len(schedule) and schedule[idx][0] <= elapsed:
                rel, key = schedule[idx]
                # 精确等待到目标时刻（最后 2ms 忙等）
                while True:
                    remain = rel - (time.perf_counter() - t0)
                    if remain <= 0.002:
                        break
                    time.sleep(min(remain / 2, 0.002))
                io.press(key)
                events.append({"key": key, "t": round(time.perf_counter() - t0, 4)})
                idx += 1
            # 读屏
            text = io.screen_text()
            if text != last_text:
                last_text = text
                frames.append({"t": round(time.perf_counter() - t0, 4), "text": text})
            # 标记检测（只认"新增行"：该行出现次数超过 baseline，排除历史残留）
            cur_counts = line_counts(text)
            new_lines = [ln for ln, cnt in cur_counts.items()
                         if cnt > before_counts.get(ln, 0)]
            for name in MARKERS:
                if name not in marks and any(name in ln for ln in new_lines):
                    marks[name] = round(time.perf_counter() - t0, 4)
            if end_after is None and ("已打断" in marks or "最大输出:" in marks):
                end_after = time.perf_counter()
            if end_after is not None and time.perf_counter() - end_after > 4.0:
                break
            time.sleep(0.02)

        result["status"] = "ok"
        result["t0_epoch"] = t0_epoch
        result["marks"] = marks
        result["events"] = events
        result["duration_sampled"] = round(time.perf_counter() - t0, 3)
        result["screen_final"] = io.screen_text()
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if io is not None:
            try:
                io.close()
            except Exception:
                pass

    write_json(out_path, result)
    with open(frames_path, "w", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps(fr, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
