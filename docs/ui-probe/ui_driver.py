"""UI 场景驱动器（真机）：附着目标进程控制台，注入按键 + 读屏，产出 result.json。

内置场景：
  after_esc_input : 发消息→按 Esc 打断→等回到输入态→输入文本→验证回显→回车→
                    验证被接收→验证下一轮完整跑完（"打断后 UI 可用性"全链路）
  no_key          : 发消息→不按键→验证完整完成后提示符回归（正常路径对照）

用法（一般经 ui_run.py 调用；也可手动 --pid 单跑）：
    python ui_driver.py --pid <pid> --out result.json --scenario after_esc_input

扩展新场景：在 main() 里加 scenario 分支，断言套路（at_prompt / 完成标志 / 关键词）
与更多按键序列见同目录 README.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import winctl  # noqa: E402

PROBE_TEXT = "AFTER_ESC_UI_OK"


def at_prompt(text: str) -> bool:
    tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
    return any(ln.lstrip().startswith("#") for ln in tail)


def wait_prompt(io, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if at_prompt(io.screen_text()):
            time.sleep(1.0)
            if at_prompt(io.screen_text()):
                return True
        time.sleep(0.3)
    return False


def input_line_of(screen: str):
    """取底部提示符行的内容（去掉 '#' 前缀），非提示符行返回 None。"""
    for ln in reversed([l for l in screen.splitlines() if l.strip()][-3:]):
        s = ln.lstrip()
        if s.startswith("#"):
            return s[1:].strip()
    return None


def press_text(io, text: str, delay: float = 0.06) -> None:
    for ch in text:
        io.press(ch)
        time.sleep(delay)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scenario", choices=["after_esc_input", "no_key"], required=True)
    ap.add_argument("--text", default="你好")
    ap.add_argument("--wait-ready", type=float, default=40.0)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--esc-delay", type=float, default=0.5)
    args = ap.parse_args()

    out_path = Path(args.out)
    result = {"scenario": args.scenario, "pid": args.pid, "status": "unknown",
              "checks": {}, "marks": {}, "screens": {}, "errors": []}
    io = None
    try:
        io = winctl.ConsoleIO(args.pid, attach_timeout=args.wait_ready)
        if not wait_prompt(io, args.wait_ready):
            result["status"] = "not_ready"
            result["screens"]["final"] = io.screen_text()
            json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return 2

        io.press("ctrl-c")
        time.sleep(1.0)
        if not wait_prompt(io, 10.0):
            result["status"] = "not_ready_after_clear"
            json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return 2

        # 发消息
        press_text(io, args.text)
        time.sleep(0.08)
        io.press("enter")
        t0 = time.perf_counter()

        # 等 "思考中"
        deadline = t0 + 20
        while time.perf_counter() < deadline:
            if "思考中" in io.screen_text():
                result["marks"]["思考中"] = round(time.perf_counter() - t0, 3)
                break
            time.sleep(0.1)
        result["checks"]["thinking_shown"] = "思考中" in result["marks"]

        if args.scenario == "after_esc_input":
            # 按指定延迟注入 Esc（默认 0.5s）；1.2s 内无响应则补发第二发（诊断用）
            time.sleep(args.esc_delay)
            pre = io.screen_text()
            # 统计行（含"费用:"）是本轮完成的标志；"思考中"完成后会残留，不能用作运行中判据
            result["checks"]["completed_at_esc"] = "费用:" in pre
            result["t_esc_epoch"] = time.time()
            io.press("esc")
            t_esc = time.perf_counter()
            first_ok = False
            deadline = t_esc + 1.2
            while time.perf_counter() < deadline:
                if "已打断" in io.screen_text():
                    first_ok = True
                    break
                time.sleep(0.04)
            if first_ok:
                result["marks"]["已打断"] = round(time.perf_counter() - t_esc, 3)
                result["marks"]["esc_used"] = 1
            else:
                time.sleep(0.3)
                io.press("esc")
                t2 = time.perf_counter()
                second_ok = False
                deadline = t2 + 5
                while time.perf_counter() < deadline:
                    if "已打断" in io.screen_text():
                        second_ok = True
                        break
                    time.sleep(0.04)
                result["marks"]["esc_used"] = 2
                result["checks"]["second_esc"] = second_ok
                if second_ok:
                    result["marks"]["已打断"] = round(time.perf_counter() - t2, 3)
            result["checks"]["interrupted"] = bool(
                result["marks"].get("已打断") is not None)
            result["screens"]["after_interrupt"] = io.screen_text()

            # 等回到输入态
            time.sleep(1.5)
            ok_prompt = wait_prompt(io, 10.0)
            result["checks"]["back_to_prompt"] = ok_prompt
            result["screens"]["back_prompt"] = io.screen_text()

            # 输入探测文本
            press_text(io, PROBE_TEXT)
            time.sleep(1.2)
            screen = io.screen_text()
            result["screens"]["after_typing"] = screen
            line = input_line_of(screen)
            result["input_line"] = line
            # 精确检查：输入行内容 == 探测文本（无幽灵字符混入、无吞字）
            result["checks"]["typing_exact"] = (line == PROBE_TEXT)

            # 回车提交
            io.press("enter")
            t1 = time.perf_counter()
            accepted = False
            deadline = t1 + 12
            while time.perf_counter() < deadline:
                s2 = io.screen_text()
                if "思考中" in s2 and PROBE_TEXT in s2:
                    accepted = True
                    break
                time.sleep(0.1)
            result["checks"]["submit_accepted"] = accepted
            # 等第二轮完整完成（回到输入态、尾部无 spinner）——验证打断后请求链路正常
            completed2 = False
            if accepted:
                deadline = time.perf_counter() + 60
                while time.perf_counter() < deadline:
                    s2 = io.screen_text()
                    tail = [l for l in s2.splitlines() if l.strip()][-5:]
                    if at_prompt(s2) and not any("思考中" in l for l in tail):
                        completed2 = True
                        break
                    time.sleep(0.4)
            s3 = io.screen_text()
            result["checks"]["round2_completed"] = completed2
            result["checks"]["no_error_text"] = not any(
                k in s3 for k in ("API调用失败", "Traceback", "APIConnectionError"))
            result["screens"]["after_submit"] = s3
        else:
            # no_key：等完整完成（提示符回归）
            done = False
            deadline = t0 + args.duration
            while time.perf_counter() < deadline:
                screen = io.screen_text()
                if "已打断" in screen:
                    result["checks"]["unexpected_interrupt"] = True
                    break
                if at_prompt(screen):
                    done = True
                    break
                time.sleep(0.3)
            result["checks"]["completed"] = done
            result["checks"].setdefault("unexpected_interrupt", False)
            result["screens"]["final"] = io.screen_text()

        result["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            json.dump(result, open(out_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass
        if io is not None:
            try:
                io.close()
            except Exception:
                pass
    sys.stdout.flush()
    os._exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
