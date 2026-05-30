"""
Narnat Agent 入口
"""

import sys
import os

# 确保项目根目录在sys.path中
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from narnat_agent.core.agent import Agent


def main():
    """启动Narnat Agent"""
    agent = Agent()  # 从当前工作目录查找.narnat配置
    agent.run()


if __name__ == "__main__":
    main()
