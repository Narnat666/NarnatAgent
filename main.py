"""
Narnat Agent 入口
"""

import argparse
import sys
import os


def main():
    """启动Narnat Agent"""
    parser = argparse.ArgumentParser(description="Narnat Agent - 代码智能体")
    parser.add_argument("-d", "--debug", action="store_true", help="调试模式，记录详细日志")
    args = parser.parse_args()

    # 确保项目根目录在sys.path中
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from narnat_agent.core.agent import Agent

    agent = Agent(debug=args.debug)
    agent.run()


if __name__ == "__main__":
    main()
