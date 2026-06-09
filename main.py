"""
Narnat Agent 入口
"""

import argparse
import sys
import os

__version__ = "3.0.0"


def main():
    """启动Narnat Agent"""
    parser = argparse.ArgumentParser(description="Narnat Agent - 代码智能体")
    parser.add_argument("-d", "--debug", action="store_true", help="调试模式，记录详细日志")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本号")
    args = parser.parse_args()

    if args.version:
        print(f"narnat {__version__}")
        return

    # 确保项目根目录在sys.path中
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from narnat_agent.core.agent import Agent

    agent = Agent(debug=args.debug)
    agent.run()


if __name__ == "__main__":
    main()
