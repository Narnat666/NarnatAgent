"""NarnatAgent 仿真测试

基于simulators/的闭环暴力测试，AI在仿真环境中自由穷举。

测试文件:
    test_terminal_sim.py    Terminal仿真测试（MockSSHServer + VFS + Shell单元测试）
    test_file_tools_sim.py  文件工具仿真测试（MockFileSystem + Read/Glob/Grep/Edit/Write）
    test_llm_sim.py         LLM仿真测试（MockLLMServer + 流式/非流式/队列）
    test_http_sim.py        HTTP仿真测试（MockHTTPServer + 搜索API + 页面预设）

运行:
    pytest tests/test_simulations/ -v                          # 全部
    pytest tests/test_simulations/ -v -k "Unit or llm or http" # 快速（约10秒）
"""
