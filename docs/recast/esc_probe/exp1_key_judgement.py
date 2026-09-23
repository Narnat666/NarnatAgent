"""实验一：按键序列 × 到达时刻 → ESC 判定结果（≥8 组受控用例）。

实验方法：用假字符源（可控投喂字节与到达时刻）驱动**真实的** `KeyListener` /
`poll_keys` / `scan_escape` / `InterruptBus`（只读导入主线，不修改），记录：
判定结果、中断置位时刻、被读取/被吞掉的字节。

复跑：
    cd /d D:\\desktop\\NarnatAgent && chcp 65001 >nul && python docs\\recast\\esc_probe\\exp1_key_judgement.py
产物：
    docs/recast/esc_probe/out/exp1_results.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fake_source import BQ, ESC, ScriptedSource, describe_bytes  # noqa: E402
from narnat_agent.interrupt import InterruptBus, KeyListener    # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_DIR = Path(__file__).resolve().parent / "out"

# 用例：名称 → (schedule, escape_immediate)
# schedule 元素 = (相对 t0 的到达秒数, 字节)；负时刻 = 采集启动前已积压
CASES: list[tuple[str, list[tuple[float, bytes]], bool]] = [
    ("C01 纯 Esc（采集启动前已在缓冲，基线）",
     [(0.0, ESC)], False),
    ("C02 错键在前：` 先 100ms，Esc 后",
     [(0.0, BQ), (0.100, ESC)], False),
    ("C03 错键紧邻在前：` 先 15ms，Esc 后",
     [(0.0, BQ), (0.015, ESC)], False),
    ("C04 Esc 后 10ms 来错键（落入 20ms 判定窗口）",
     [(0.0, ESC), (0.010, BQ)], False),
    ("C05 Esc 后 5ms 错键、再 5ms 第二个 Esc（窗口内出现 Esc）",
     [(0.0, ESC), (0.005, BQ), (0.010, ESC)], False),
    ("C06 两次 Esc 间隔 100ms（> 20ms 窗口）",
     [(0.0, ESC), (0.100, ESC)], False),
    ("C07 两次 Esc 间隔 10ms（< 20ms 窗口）",
     [(0.0, ESC), (0.010, ESC)], False),
    ("C08 Esc + 5 个非 Esc 字符（间隔 2ms 批量）",
     [(0.0, ESC), (0.002, b"a"), (0.004, b"b"), (0.006, b"c"),
      (0.008, b"d"), (0.010, b"e")], False),
    ("C09 Esc + 6 个非 Esc 字符（5 字节消费上限边界）",
     [(0.0, ESC), (0.002, b"a"), (0.004, b"b"), (0.006, b"c"),
      (0.008, b"d"), (0.010, b"e"), (0.012, b"f")], False),
    ("C10 Esc + 6 个非 Esc 字符 + 第 8 字节才是 Esc（超出消费窗口）",
     [(0.0, ESC), (0.002, b"a"), (0.004, b"b"), (0.006, b"c"),
      (0.008, b"d"), (0.010, b"e"), (0.012, b"f"), (0.014, ESC)], False),
    ("C11 交替连按 Esc/` 间隔 15ms ×3 轮（Esc 在先）",
     [(0.0, ESC), (0.015, BQ), (0.030, ESC), (0.045, BQ),
      (0.060, ESC), (0.075, BQ)], False),
    ("C12 交替连按 `/Esc 间隔 15ms ×3 轮（错键在先）",
     [(0.0, BQ), (0.015, ESC), (0.030, BQ), (0.045, ESC),
      (0.060, BQ), (0.075, ESC)], False),
    ("C13 交替连按 Esc/` 间隔 30ms ×3 轮（慢于判定窗口）",
     [(0.0, ESC), (0.030, BQ), (0.060, ESC), (0.090, BQ),
      (0.120, ESC), (0.150, BQ)], False),
    ("C14 Windows Terminal 事件源对照（escape_immediate）：Esc 与 ` 交错",
     [(0.0, ESC), (0.015, BQ), (0.030, ESC)], True),
    ("C15 采集启动前已积压：` (-500ms)、Esc (-300ms)（msvcrt 源不清缓冲）",
     [(-0.5, BQ), (-0.3, ESC)], False),
]


def run_case(name: str, schedule, escape_immediate: bool, timeout: float = 0.6) -> dict:
    """跑一条用例：真实采集线程 + 真实总线，记录判定与时刻。"""
    source = ScriptedSource(schedule, escape_immediate=escape_immediate, name=name)
    box: dict = {}

    def on_escape() -> None:
        box["bus"].raise_()

    listener = KeyListener(on_escape, source_factory=lambda: source)
    bus = InterruptBus(listener)
    box["bus"] = bus

    raised_at: list[float] = []
    hook_calls: list[tuple[str, float]] = []

    def hook() -> None:
        raised_at.append(source.now())
        hook_calls.append(("abort_like_subscriber", source.now()))

    bus.subscribe(hook)

    source.start_clock()
    bus.enter_run_mode()
    deadline = time.perf_counter() + timeout
    while not bus.is_set and time.perf_counter() < deadline:
        time.sleep(0.002)
    interrupted = bus.is_set
    # 等采集线程结束（触发一次即退出；未触发时等一轮判定窗口收尾）
    time.sleep(0.05)
    bus.enter_input_mode()

    records = [f"{t * 1000:7.1f}ms  read(timeout={to}) -> "
               f"{('<none>' if v is None else describe_bytes(v))}"
               for t, to, v in source.reads]
    result = {
        "name": name,
        "escape_immediate": escape_immediate,
        "schedule": [[round(t, 4), describe_bytes(b)] for t, b in schedule],
        "interrupted": interrupted,
        "raised_at_ms": round(raised_at[0] * 1000, 1) if raised_at else None,
        "delivered": [[round(t * 1000, 1), describe_bytes(b)] for t, b in source.delivered],
        "delivered_bytes": describe_bytes(source.delivered_bytes),
        "swallowed_bytes": describe_bytes(
            source.delivered_bytes if not interrupted and source.delivered_bytes else b""
        ) if not interrupted else "",
        "undelivered": [[round(t, 4), describe_bytes(b)] for t, b in source.remaining],
        "read_trace": records,
        "subscriber_calls": len(hook_calls),
    }
    return result


def check_silent_degrade() -> list[dict]:
    """验证采集器两条静默降级路径（源构建抛异常 / 返回 None）：

    期望：线程退出、不报错、running=False——即"无 Esc 中断能力"但界面无任何提示。
    """
    results = []
    for label, factory in (
        ("源构建抛异常", lambda: (_ for _ in ()).throw(OSError("no console"))),
        ("源返回 None", lambda: None),
    ):
        listener = KeyListener(lambda: None, source_factory=factory)
        listener.start()
        time.sleep(0.10)
        running = listener.running
        listener.stop()
        results.append({"case": label, "running_after_100ms": running,
                        "raise_logged": False})
        print(f"[降级验证] {label}: running={running}（无异常冒出，无任何提示）")
    return results


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for name, schedule, immediate in CASES:
        res = run_case(name, schedule, immediate)
        results.append(res)
        verdict = "中断" if res["interrupted"] else "未中断(吞键)"
        at = f"{res['raised_at_ms']}ms" if res["raised_at_ms"] is not None else "-"
        print(f"[{verdict:>8}] {name}")
        print(f"           置位时刻={at}  交付字节={res['delivered_bytes']!r}"
              f"  剩余={res['undelivered']}")

    # 重复稳定性：同一序列（C12 交替连按）复跑 5 次，暴露时序抖动
    stability = []
    for i in range(5):
        res = run_case(f"C16 稳定性复跑 #{i + 1}", CASES[11][1], False)
        stability.append({"interrupted": res["interrupted"],
                          "raised_at_ms": res["raised_at_ms"]})
        print(f"[稳定性 #{i + 1}] interrupted={res['interrupted']} "
              f"raised_at={res['raised_at_ms']}ms")

    degrade = check_silent_degrade()

    payload = {"cases": results, "stability_x5_C12": stability,
               "silent_degrade": degrade}
    (OUT_DIR / "exp1_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for r in results if r["interrupted"])
    print(f"\n汇总：{len(results)} 组用例，{n_ok} 组判定中断，"
          f"{len(results) - n_ok} 组被吞；结果已写 {OUT_DIR / 'exp1_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
