"""
Narnat Agent 入口
"""

import argparse
import sys
import os

__version__ = "17.0.2"


def main():
    """启动Narnat Agent"""
    parser = argparse.ArgumentParser(description="Narnat Agent - 代码智能体")
    parser.add_argument("-d", "--debug", action="store_true", help="调试模式，记录详细日志")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本号")
    parser.add_argument("-p", "--prompt", help="headless模式：执行一次性任务后退出（纯文本输出）")
    parser.add_argument("-g", "--goal-rounds", type=int, default=0,
                        help="headless模式：开启目标模式并设置续跑预算轮数（≥1，如 -g 10）；"
                             "不带则单轮执行（不注入 GoalComplete、无完成验证）")
    parser.add_argument("-l", "--tool-log", action="store_true",
                        help="headless模式：显示详细工具调度日志（默认仅输出AI最终答复文本，-p 时生效）")
    parser.add_argument("--rpc", action="store_true",
                        help="RPC模式：JSON-RPC 2.0 over stdio，供 VSCode 扩展调用（进程常驻）")
    args = parser.parse_args()

    if args.version:
        print(f"narnat {__version__}")
        return

    if args.rpc and args.prompt is not None:
        print("错误: --rpc 与 -p 互斥（--rpc 是常驻进程，-p 是一次性任务）")
        sys.exit(1)

    # 确保项目根目录在sys.path中
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from narnat_agent.core.agent import Agent

    if args.prompt is not None:
        if not args.prompt.strip():
            print("错误: -p 任务内容不能为空")
            sys.exit(1)
        # headless：stdout/stderr 显式 UTF-8（重定向/管道下中文与emoji不乱码；
        # stderr 同设避免 2>&1 合并日志时两种编码混杂）
        if sys.platform == "win32":
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
            try:
                sys.stderr.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
        from narnat_agent.output import set_plain, set_quiet_tools
        set_plain(True)  # headless：全局去色，输出纯文本
        set_quiet_tools(not args.tool_log)  # headless：默认静默工具调度日志，-l 开启全量
        if args.goal_rounds < 0:
            print("错误: -g 轮数上限必须为正整数")
            sys.exit(1)
        # 子代理后台日志隔离：background 模块按进程创建唯一临时目录
        # （narnat_bg_<随机>），父/子、同项目多 agent 天然互不干扰，
        # 无需额外环境变量。
        agent = Agent(debug=args.debug, headless=True)
        reason = agent.run_headless(args.prompt, max_rounds=args.goal_rounds)
        if reason in ("round_failed", "aborted", "compress_failed", "unknown"):
            sys.exit(2)
        return

    if args.rpc:
        # RPC：stdout 是协议通道（一行一条 JSON-RPC），去色 + UTF-8；
        # 安装传输层后终端形态的所有打印自动改道 stderr
        if sys.platform == "win32":
            for _stream in (sys.stdout, sys.stderr):
                try:
                    _stream.reconfigure(encoding="utf-8")
                except (AttributeError, OSError):
                    pass
        from narnat_agent.output import set_plain, set_quiet_tools
        set_plain(True)
        # 工具调度行的终端打印对 RPC 是纯噪音（结构化数据已由 stream 钩子下发），
        # 且这些行会落到 stderr 干扰排查日志
        set_quiet_tools(True)
        from narnat_agent.rpc.protocol import install_stdio_transport
        install_stdio_transport()
        from narnat_agent.assembly import set_rpc_mode
        set_rpc_mode(True)
        agent = Agent(debug=args.debug)
        from narnat_agent.rpc.server import serve
        sys.exit(serve(agent))

    agent = Agent(debug=args.debug)
    agent.run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n程序异常退出: {e}")
        try:
            if sys.stdin and sys.stdin.isatty():
                input("按回车键退出...")
        except (KeyboardInterrupt, EOFError):
            pass
        sys.exit(1)
