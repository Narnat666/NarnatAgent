"""NarnatAgent 仿真平台

每个仿真模块对应一个真实模块，提供闭环测试环境。
AI在仿真环境中自由测试，无需真实SSH/LLM/HTTP连接。

仿真模块:
    mock_ssh_server    → Terminal/Remote工具仿真
    mock_llm_server    → LLM API仿真（Agent闭环测试）
    mock_http_server   → WebSearch工具仿真
    mock_filesystem    → 文件操作仿真（增强tempfile）
"""
