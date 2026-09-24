"""UI 真机验证运行器：启动目标 narnat → 调 ui_driver 跑场景 → 清理进程。

用法：
    python ui_run.py --scenario after_esc_input
    python ui_run.py --scenario no_key --console conhost
    python ui_run.py --scenario after_esc_input --target "%TEMP%\\narnat_old"  # 对照旧版

设计（方法论详见 README.md）：
- 目标默认取本仓库根（脚本上两级含 main.py 的目录）；--target 可指向任意
  narnat 副本，用于"修复前 / 修复后"对照实验；
- 隔离 home：默认 %TEMP%\\narnat_ui_probe_home，首次运行自动从目标目录 .narnat
  复制配置（排除 logs），不写工作树 .narnat；
- 结果：默认 %TEMP%\\narnat_ui_probe_runs\\<run_id>\\result.json，不污染工作区；
- 跑完杀目标进程树与测试期间新建的 conhost，不留残留。
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
PY = sys.executable
TEMP = Path(os.path.expandvars(r"%TEMP%"))
DEFAULT_HOME = TEMP / "narnat_ui_probe_home"
DEFAULT_RUNS = TEMP / "narnat_ui_probe_runs"


def find_project_root(start: Path) -> Path:
    """从 start 向上找含 main.py 的目录（脚本位于 <root>/docs/ui-probe/）。"""
    p = start
    for _ in range(5):
        if (p / "main.py").is_file():
            return p
        p = p.parent
    raise SystemExit(f"未找到项目根（含 main.py）：从 {start} 向上 5 级未命中，"
                     f"可用 --target 显式指定")


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
    ps = ('Get-CimInstance Win32_Process -Filter "name=\'python.exe\'" | '
          'Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress')
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    result = {}
    text = out.stdout.strip()
    if not text:
        return result
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return result
    if isinstance(data, dict):
        data = [data]
    for item in data:
        result[int(item["ProcessId"])] = item.get("CommandLine") or ""
    return result


def wait_target_pid(before: set, timeout: float = 20.0):
    """轮询找新出现的 python 进程（命令行含 main.py）作为目标 pid。

    注意：实验期间避免同时手动启动其它 narnat（main.py）实例，否则可能认错目标。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, cmd in python_cmdlines().items():
            if pid not in before and "main.py" in (cmd or ""):
                return pid
        time.sleep(1.0)
    return None


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def prepare_home(home: Path, target_dir: Path) -> None:
    """准备隔离 home：首次运行时从目标目录 .narnat 复制配置（排除运行数据）。"""
    marker = home / ".narnat" / "config" / "narnat.json"
    if marker.is_file():
        return
    src = target_dir / ".narnat"
    dst = home / ".narnat"
    dst.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "logs"))
    if not marker.is_file():
        print(f"[warn] 隔离 home 缺少配置（{marker}）——目标将用默认配置，可能无 API Key",
              flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=["after_esc_input", "no_key"])
    ap.add_argument("--console", default="wt", choices=["wt", "conhost"])
    ap.add_argument("--target", help="目标 narnat 目录（默认：本仓库根）")
    ap.add_argument("--home", help=f"隔离 home（默认：{DEFAULT_HOME}）")
    ap.add_argument("--runs-dir", help=f"结果输出目录（默认：{DEFAULT_RUNS}）")
    ap.add_argument("--esc-delay", type=float, default=0.5,
                    help="发消息后等多久注入 Esc（秒）")
    args = ap.parse_args()

    target_dir = Path(args.target).resolve() if args.target else find_project_root(HERE)
    home = Path(args.home).resolve() if args.home else DEFAULT_HOME
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else DEFAULT_RUNS
    runs_dir.mkdir(parents=True, exist_ok=True)

    run_id = f"{args.scenario}_{args.console}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    prepare_home(home, target_dir)

    env = os.environ.copy()
    env["NARNAT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    if args.console == "conhost":
        cmdline = ["cmd.exe", "/c", "start", "", "conhost", PY, "main.py", "-d"]
    else:
        cmdline = ["cmd.exe", "/c", "start", "", PY, "main.py", "-d"]

    before_python = list_pids("python.exe")
    before_conhost = list_pids("conhost.exe")
    print(f"target_dir={target_dir}", flush=True)
    print(f"home={home}", flush=True)
    proc = subprocess.Popen(cmdline, cwd=str(target_dir), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target_pid = wait_target_pid(before_python, timeout=20.0)
    print(f"target_pid={target_pid}", flush=True)
    rc = -1
    try:
        if target_pid is not None:
            out = run_dir / "result.json"
            rc = subprocess.run([PY, str(HERE / "ui_driver.py"), "--pid", str(target_pid),
                                 "--out", str(out), "--scenario", args.scenario,
                                 "--esc-delay", str(args.esc_delay)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=300).returncode
    finally:
        if target_pid is not None:
            kill_tree(target_pid)
        kill_tree(proc.pid)
        time.sleep(0.5)
        # 清理测试期间新建的 conhost（差集法：实验期间不要新开终端窗口）
        for pid in list_pids("conhost.exe") - before_conhost:
            kill_tree(pid)
    print(f"driver rc={rc}", flush=True)
    print(f"result={run_dir / 'result.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
