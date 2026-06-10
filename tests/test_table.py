"""
表格对齐渲染测试
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from narnat_agent.ui.ui_design import StreamingRenderer

SAMPLE = """| 项目 | 参数 |
|------|------|
| 芯片型号 | BM3803 |
| CPU 架构 | SPARC V8，32 位 |
| 核心数 | 单核 |
| 主频 | 50 MHz |
| Flash | 48 Mb（6 MB） |
| 工具链 | sparc-zephyr-elf-gcc |
| 操作系统 | RT-Thread |
| 设备类型 | 卫星电子单机 |"""

print("=" * 60)
print("测试1: 用户给的表格")
print("=" * 60)
r = StreamingRenderer()
r.feed("\n")
r.feed(SAMPLE)
r.feed("\n\n")
r.flush()

print()
print("=" * 60)
print("测试2: 中文列名更窄的表格")
print("=" * 60)
r2 = StreamingRenderer()
r2.feed("\n")
r2.feed("| 姓名 | 年龄 | 城市 |\n|---|---|---|\n| 张三 | 25 | 北京 |\n| 李四四四 | 30 | 上海 |\n")
r2.feed("\n\n")
r2.flush()

print()
print("=" * 60)
print("测试3: 表格内含粗体和代码")
print("=" * 60)
r3 = StreamingRenderer()
r3.feed("\n")
r3.feed("| 命令 | 说明 |\n|---|---|\n| **`/enter`** | 进入会话 |\n| `/skill` | 加载技能 |\n")
r3.feed("\n\n")
r3.flush()

print()
print("=" * 60)
print("测试4: 表格前后有普通文本")
print("=" * 60)
r4 = StreamingRenderer()
r4.feed("前面一段文字。\n\n")
r4.feed("| A | B |\n|---|---|\n| 1 | 2 |\n")
r4.feed("\n后面一段文字。\n")
r4.flush()
