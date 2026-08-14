"""Narnat Agent Web 层 —— DSH Web 前端的中转/适配模块

本包将 DeepSeek Harness（DSH）的 Web 前端静态工件与"宿主协议"
（HTTP RPC + WebSocket 下行）接入 Narnat Agent：
- dsh/collect_dsh.py  从 DSH 仓库/构建产物收集前端工件并生成插件清单
- dsh/server.py       HTTP + WebSocket 服务器（标准库实现）
- dsh/handlers.py     DSH 前端协议端点实现（把 Narnat 数据翻译成 DSH 协议）
"""
