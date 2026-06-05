"""NarnatAgent 测试套件

目录结构:
    test_simulations/   仿真测试（基于simulators的闭环暴力测试，AI自由穷举）
    test_tools/         工具单元测试
    test_core/          核心模块测试
    test_config/        配置模块测试
    test_commands/      命令模块测试
    test_brutal.py      暴力测试（极端场景）
    test_regression.py  回归测试（bug修复守卫）
    test_logger.py      日志模块测试

仿真平台:
    simulators/         仿真模块（MockSSH/MockLLM/MockHTTP/MockFS）
    simulators/README.md  仿真平台开发手册（必读）

开发新模块时:
    1. 先读 tests/simulators/README.md
    2. 开发模块 → 同步开发仿真 → 写仿真测试
    3. pytest tests/test_simulations/ 验证
"""
