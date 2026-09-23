"""narnat_agent —— 积木化重构后的 Narnat Agent 包。

本模块只提供包级元信息与入口门面（版本号与 main）：**轻量、不得 eager import
全链**——顶层 import 只拉入标准库与版本常量，`main` / `App` 在调用时按需导入
（`python -c "import narnat_agent"` 与 `-v` 版本查询均不触发任何积木加载与网络）。

积木清单（design D1 分层，低层在前）：
`contracts` → `config` / `output` / `interrupt` / `messages` / `llm` →
`compression` / `stats` / `tools` / `mcp` / `ui` → `conversation` / `sessions` → `app`。
"""
from __future__ import annotations

import sys

__version__ = "16.2.5"

__all__ = ["__version__", "main", "run_cli"]


def main(argv: list[str] | None = None) -> int:
    """命令行入口 —— 参数解析与分支契约见 `openspec/.../specs/app/spec.md`。

    参数：`-d/--debug`、`-v/--version`、`-p/--prompt`、`-g/--goal-rounds`、
    `-l/--tool-log`；`-v` 优先于其他一切行为；headless 分支置 UTF-8 输出与去色。

    Returns:
        进程退出码（0 正常；参数错误 2 由解析器抛出；分支错误 1）。
    """
    if argv is None:
        argv = sys.argv[1:]
    return run_cli(argv)


def run_cli(argv: list[str]) -> int:
    """按参数执行对应分支（`main` 的实现体；测试可直接驱动参数列表）。"""
    import argparse

    parser = argparse.ArgumentParser(description="Narnat Agent - 代码智能体")
    parser.add_argument("-d", "--debug", action="store_true", help="调试模式，记录详细日志")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本号")
    parser.add_argument("-p", "--prompt", help="headless模式：执行一次性任务后退出（纯文本输出）")
    parser.add_argument("-g", "--goal-rounds", type=int, default=0,
                        help="headless模式：自动续跑轮数上限（默认用配置值，-p 时生效）")
    parser.add_argument("-l", "--tool-log", action="store_true",
                        help="headless模式：显示详细工具调度日志（默认仅输出AI最终答复文本，-p 时生效）")
    args = parser.parse_args(argv)

    if args.version:
        print(f"narnat {__version__}")
        return 0

    if args.prompt is not None:
        if not args.prompt.strip():
            print("错误: -p 任务内容不能为空")
            return 1
        if args.goal_rounds < 0:
            print("错误: -g 轮数上限必须为正整数")
            return 1
        from .app import App

        agent = App(debug=args.debug, headless=True, tool_log=args.tool_log)
        agent.run_headless(args.prompt, max_rounds=args.goal_rounds)
        return 0

    from .app import App

    agent = App(debug=args.debug)
    agent.run()
    return 0
