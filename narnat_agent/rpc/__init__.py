"""RPC 模式（--rpc）：JSON-RPC 2.0 over stdio

- protocol.py  stdout 协议通道 + 输出捕获
- ui.py        RpcUI / RpcStream（UIInterface 的第三种实现）
- server.py    stdin 分发 + agent 运行线程
"""

from .ui import RpcStream, RpcTodoBridge, RpcUI

__all__ = ["RpcUI", "RpcStream", "RpcTodoBridge"]
