"""E2 实验运行器：启动目标（新/旧实现）→ 驱动注入采样 → 收集日志 → 清理进程。

一次调用跑一条序列的一次重复，产出 runs/<run_id>/ 结果目录。

用法：
    python runner.py --version new --plan baseline --attempt 1
    python runner.py --matrix          # 全矩阵（2 版本 × 5 序列 × 3 次）

隔离说明：
- 目标进程用 NARNAT_HOME 指向 %TEMP%\\narnat_esc_home（.narnat 的隔离副本），
  主线工作树的 .narnat 不被写入；
- 目标在 conhost 原生控制台窗口中运行（cmd start conhost 包装），便于注入与读屏。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW_DIR = Path(r"D:\desktop\NarnatAgent")
OLD_DIR = Path(os.path.expandvars(r"%TEMP%")) / "narnat_old"
ESC_HOME_NEW = Path(os.path.expandvars(r"%TEMP%")) / "narnat_esc_home_new"
ESC_HOME_OLD = Path(os.path.expandvars(r"%TEMP%")) / "narnat_esc_home_old"
RUNS_DIR = HERE / "runs"
PY = sys.executable
CONHOST = r"C:\Windows\System32\conhost.exe"
PLANS = ["baseline", "wrong_then_esc", "alternate", "rage", "delayed"]
EXTRA_PLANS = ["alternate_then_rage", "none",
               "esc_then_backtick", "dense_alternate"]  # FIX-ESC 验证序列
ATTEMPTS = 3


def log(msg: str, path: Path | None = None) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if path is not None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def prepare_esc_home(home: Path) -> None:
    """准备隔离 home：复制工作树 .narnat（含密钥的配置）一次。"""
    target = home / ".narnat"
    if target.is_dir():
        return
    shutil.copytree(NEW_DIR / ".narnat", target,
                    ignore=shutil.ignore_patterns(".git", "logs"))


def ensure_old_tree() -> None:
    """确保旧实现已导出到 %TEMP%\\narnat_old（git archive 只读导出）。"""
    if (OLD_DIR / "main.py").is_file():
        return
    zip_path = Path(os.path.expandvars(r"%TEMP%")) / "narnat_old.zip"
    subprocess.run(["git", "archive", "--format=zip", "7d075a2", "-o", str(zip_path)],
                   cwd=str(NEW_DIR), check=True)
    shutil.unpack_archive(str(zip_path), str(OLD_DIR), "zip")


def list_pids(image: str) -> set:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True)
    pids = set()
    for line in out.stdout.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and parts[1].strip('"').isdigit():
            pids.add(int(parts[1].strip('"')))
    return pids


def python_cmdlines() -> dict:
    """一次 PowerShell 查询所有 python.exe 进程的 {pid: 命令行}。"""
    ps = ('Get-CimInstance Win32_Process -Filter "name=\'python.exe\'" | '
          'Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation')
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    result = {}
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].isdigit():
            result[int(parts[0])] = parts[1]
    return result


def wait_target_pid(before: set, timeout: float = 20.0) -> int | None:
    """轮询找出新出现的 python 进程（命令行含 main.py）作为目标 pid。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, cmd in python_cmdlines().items():
            if pid not in before and "main.py" in (cmd or ""):
                return pid
        time.sleep(1.0)
    return None


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid not in list_pids("python.exe"):
            return True
        time.sleep(0.3)
    return False


def run_case(version: str, plan: str, attempt: int, duration: float,
             wait_ready: float, console: str = "wt") -> Path:
    run_id = f"{version}_{console}_{plan}_r{attempt}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rlog = run_dir / "runner.log"

    target_dir = NEW_DIR if version == "new" else OLD_DIR
    home = ESC_HOME_NEW if version == "new" else ESC_HOME_OLD
    logs_dir = home / ".narnat" / "logs"
    before_logs = {p.name for p in logs_dir.glob("*.log")} if logs_dir.is_dir() else set()
    before_python = list_pids("python.exe")
    before_conhost = list_pids("conhost.exe")

    env = os.environ.copy()
    env["NARNAT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    log(f"run={run_id} target_dir={target_dir} console={console}", rlog)
    if console == "conhost":
        cmdline = ["cmd.exe", "/c", "start", "", "conhost", PY, "main.py", "-d"]
    else:
        cmdline = ["cmd.exe", "/c", "start", "", PY, "main.py", "-d"]
    proc = subprocess.Popen(cmdline, cwd=str(target_dir), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"launcher_pid={proc.pid}", rlog)

    # 目标 pid：启动前后 python 进程差集（命令行含 main.py）
    target_pid = wait_target_pid(before_python, timeout=20.0)
    if target_pid is None:
        log("FAIL: 目标进程未启动", rlog)
        kill_tree(proc.pid)
        return run_dir
    log(f"target_pid={target_pid}", rlog)

    # 调 driver
    driver_out = run_dir / "result.json"
    driver_frames = run_dir / "frames.jsonl"
    driver_err = run_dir / "driver.stderr.txt"
    with open(driver_err, "wb") as errf:
        rc = subprocess.run(
            [PY, str(HERE / "driver.py"), "--pid", str(target_pid),
             "--out", str(driver_out), "--frames", str(driver_frames),
             "--plan", plan, "--duration", str(duration),
             "--wait-ready", str(wait_ready)],
            stdout=subprocess.DEVNULL, stderr=errf, timeout=duration + wait_ready + 60,
        ).returncode
    log(f"driver rc={rc}", rlog)

    # 清理目标进程树
    kill_tree(target_pid)
    gone = wait_gone(target_pid)
    log(f"target killed, gone={gone}", rlog)
    kill_tree(proc.pid)
    time.sleep(0.8)
    after_conhost = list_pids("conhost.exe")
    for pid in after_conhost - before_conhost:
        kill_tree(pid)
    if after_conhost - before_conhost:
        log(f"extra conhost cleaned: {sorted(after_conhost - before_conhost)}", rlog)

    # 收集日志
    collected = []
    if logs_dir.is_dir():
        for p in sorted(logs_dir.glob("*.log")):
            if p.name not in before_logs:
                dst = run_dir / f"narnat_{p.name}"
                shutil.copy2(p, dst)
                collected.append(dst.name)
    log(f"narnat logs collected: {collected}", rlog)

    meta = {
        "run_id": run_id, "version": version, "plan": plan, "attempt": attempt,
        "console": console, "target_dir": str(target_dir), "target_pid": target_pid,
        "driver_rc": rc, "narnat_logs": collected,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    return run_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", choices=["new", "old"], default="new")
    ap.add_argument("--plan", choices=PLANS + EXTRA_PLANS, default="baseline")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--console", choices=["wt", "conhost"], default="wt",
                    help="wt=默认终端（Windows Terminal/ConPTY）；conhost=经典控制台")
    ap.add_argument("--duration", type=float, default=25.0)
    ap.add_argument("--wait-ready", type=float, default=40.0)
    ap.add_argument("--matrix", action="store_true",
                    help="跑完整矩阵（2 版本 × 5 序列 × 3 次，串行）")
    args = ap.parse_args()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    prepare_esc_home(ESC_HOME_NEW)
    prepare_esc_home(ESC_HOME_OLD)
    ensure_old_tree()

    if args.matrix:
        for version in ("new", "old"):
            for plan in PLANS:
                for attempt in range(1, ATTEMPTS + 1):
                    run_case(version, plan, attempt, args.duration,
                             args.wait_ready, args.console)
        return 0

    run_case(args.version, args.plan, args.attempt, args.duration,
             args.wait_ready, args.console)
    return 0


if __name__ == "__main__":
    sys.exit(main())
