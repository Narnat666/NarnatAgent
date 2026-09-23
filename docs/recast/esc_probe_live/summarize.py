"""把 runs/ 下的实验结果汇总为复现矩阵与时间线（供 E2 报告引用）。

用法：
    python summarize.py            # 打印矩阵 + 汇总 + 关键时间线，并写 summary.md
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
OUT = HERE / "summary.md"

PLAN_ORDER = ["baseline", "wrong_then_esc", "alternate", "rage", "delayed"]
PLAN_CN = {
    "baseline": "1 基线（单 ESC）",
    "wrong_then_esc": "2 错键+单 ESC",
    "alternate": "3 交错连按",
    "rage": "4 纯狂按 ESC×10",
    "delayed": "5 延迟对照（2s 后 ESC）",
}
SLOW_THRESHOLD = 2.0  # 秒；超过视为"打断不丝滑（延迟显著）"


def load_runs() -> list:
    items = []
    for result_path in sorted(RUNS.glob("*/result.json")):
        run_dir = result_path.parent
        meta_path = run_dir / "meta.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        version = meta.get("version") or run_dir.name.split("_")[0]
        console = meta.get("console") or (run_dir.name.split("_")[1] if "_conhost_" in run_dir.name or "_wt_" in run_dir.name else "?")
        plan = meta.get("plan")
        if not plan:
            for p in PLAN_ORDER + ["none", "alternate_then_rage"]:
                if f"_{p}_" in run_dir.name:
                    plan = p
                    break
        items.append({
            "run_id": run_dir.name, "dir": run_dir, "version": version,
            "console": console, "plan": plan, "attempt": meta.get("attempt"),
            "result": result,
        })
    return items


def esc_events(result: dict) -> list:
    return [e for e in result.get("events", []) if e.get("key") == "esc"]


def classify(item: dict) -> dict:
    """一次实验的结论：延迟秒数 / 是否打断 / 状态。"""
    result = item["result"]
    marks = result.get("marks", {})
    escs = esc_events(result)
    first_esc = escs[0]["t"] if escs else None
    last_esc = escs[-1]["t"] if escs else None
    interrupted_at = marks.get("已打断")
    finished_at = marks.get("最大输出:")
    if interrupted_at is not None and first_esc is not None:
        delay = interrupted_at - first_esc
    else:
        delay = None
    if interrupted_at is not None:
        outcome = "打断"
    elif finished_at is not None:
        outcome = "未打断（回合正常完成）"
    else:
        outcome = "未打断（采样结束前无结果）"
    return {
        "status": result.get("status"), "first_esc": first_esc, "last_esc": last_esc,
        "interrupted_at": interrupted_at, "finished_at": finished_at,
        "delay": delay, "outcome": outcome, "events": result.get("events", []),
        "marks": marks, "dur": result.get("duration_sampled"),
    }


def main() -> None:
    items = load_runs()
    lines = []
    lines.append("# E2 实验汇总（自动生成）\n")

    # 明细表
    lines.append("## 明细\n")
    lines.append("| run_id | 版本 | 控制台 | 序列 | ESC时刻(s) | 已打断(s) | 延迟(s) | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for it in items:
        if it["plan"] not in PLAN_ORDER and it["plan"] not in ("none", "alternate_then_rage"):
            continue
        c = classify(it)
        delay = f"{c['delay']:.2f}" if c["delay"] is not None else "-"
        esc_t = ",".join(f"{e['t']:.2f}" for e in esc_events(it["result"])) or "-"
        ia = f"{c['interrupted_at']:.2f}" if c["interrupted_at"] is not None else "-"
        lines.append(f"| {it['run_id']} | {it['version']} | {it['console']} | {it['plan']} | "
                     f"{esc_t} | {ia} | {delay} | {c['outcome']}#{c['status']} |")

    # 矩阵汇总
    lines.append("\n## 矩阵（按 版本×序列 汇总）\n")
    lines.append("| 版本 | 序列 | 次数 | 打断及时(≤2s) | 打断延迟(>2s) | 未打断 | 延迟样本(s) |")
    lines.append("|---|---|---|---|---|---|---|")
    for console in ("wt", "conhost"):
        for version in ("new", "old"):
            for plan in PLAN_ORDER:
                group = [it for it in items
                         if it["version"] == version and it["plan"] == plan
                         and it["console"] == console]
                if not group:
                    continue
                fast = slow = fail = 0
                delays = []
                for it in group:
                    c = classify(it)
                    if c["delay"] is None:
                        fail += 1
                    elif c["delay"] <= SLOW_THRESHOLD:
                        fast += 1
                        delays.append(f"{c['delay']:.2f}")
                    else:
                        slow += 1
                        delays.append(f"**{c['delay']:.2f}**")
                lines.append(f"| {version} | {PLAN_CN.get(plan, plan)} | {len(group)} | {fast} | "
                             f"{slow} | {fail} | {', '.join(delays) or '-'} |")

    # 时间线（全部 run 的关键帧尾部）
    lines.append("\n## 时间线（frames 摘要：注入与标记时刻）\n")
    for it in items:
        c = classify(it)
        lines.append(f"- `{it['run_id']}`：ESC {[round(e['t'],2) for e in esc_events(it['result'])]} → "
                     f"打断@{c['interrupted_at']} / 完成@{c['finished_at']}；延迟={c['delay'] and round(c['delay'],2)}s")

    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
