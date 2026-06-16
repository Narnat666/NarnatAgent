# ESC 中断机制设计

人的作用是引导方向，ai可能会在一个设计上卡死，但是人的话要让他改变方向

## 核心原则

所有工具在后台线程执行（`ThreadPoolExecutor`, max_workers=16），主线程通过 `wait()` 轮询检测 ESC。ESC 触发后，主线程不再等待工具结果，但确保每个工具线程最终能自然退出回到 pool，**不泄漏线程资源**。

## 整体流程

```
ESC → stream.cancelled = True
    → LLM cancel_check 检测 → chat_stream 返回
    → _agent_loop 检测 stream.cancelled
    → 调用 _kill_bash() + _kill_terminal_exec()
    → 跳出 fut.done() 轮询
    → 为未完成的 tool_call 补空结果 "[用户中断]"
    → 返回

工具线程（在 pool 中）→ 被各自的通知机制唤醒 → 返回 pool
```

## 三类工具的处理方式

### 1. Read / Write / Edit / Glob / Grep / WebSearch —— 自然消亡

**不需要主动干预。**

这些操作要么是本地文件 I/O，要么是 HTTP 请求，执行时间很短（秒级）。ESC 后主线程跳出轮询，工具线程继续跑完自然返回 pool。

**为什么这样设计**：这些操作没有阻塞点需要打破，也不需要释放外部资源。让它们自然结束最简单、最安全。

### 2. Bash —— 杀子进程

**主动杀子进程树。**

bash 命令可能运行无限长时间的命令（如 `tail -f`、`ping` 等），`subprocess.Popen.stdout.read()` 会永远阻塞直到进程退出。必须从外部杀死进程才能让 stdout 关闭、read 返回、线程退出。

```
ESC → _kill_bash() → 杀子进程树 → stdout 关闭 → read() 返回 → 线程回 pool
```

`agent.py` 中 `_run_parallel`、`_run_single` 等所有工具执行路径在检测到 `stream.cancelled` 后都会调用 `_kill_bash()`。

### 3. Terminal —— 内部中断标志

**最特殊的一类。** 终端 SSH 连接是持久资源，不能杀连接。但 `paramiko.Channel.recv()` 在 `timeout=0` 时可能因 buffer 排空进入无限循环。解决方案：**内部 `threading.Event` 中断标志**。

```
ESC → kill_active_exec() → session._interrupt.set()  ← 只发信号，不杀连接

工具线程（在 _read_until_marker 等阻塞方法中）:
  recv() timeout (最多 0.5s) → 检测 _interrupt.is_set()
  → True → break/return → 线程回 pool
  SSH 会话保持存活，可复用
```

**检测点**（`terminal.py` 中所有 `socket.timeout` 处理处）：
- `_read_until_marker` 主循环
- `_read_until_marker` post-marker 排空循环
- `_read_until_prompt`
- `_update_cwd`
- `_start_busy_watcher`
- `_try_read_residual`

## 终端工具特殊处理的原因

### 问题：为什么不能直接 `channel.close()` 杀死终端线程？

`channel.close()` 关闭 SSH channel 后，当 buffer 排空，后续 `recv()` 只触发 `socket.timeout` 而不抛异常。如果 `timeout=0`（无限等待），`deadline = float('inf')` → 死循环 → **线程泄漏**。

### 为什么 transport close 曾经导致 5 秒阻塞？

Windows `closesocket()` 不打断跨线程 `recv()`（与 Linux 不同）。paramiko `Transport.close()` 内部调用 `thread.join(timeout=5.0)` 等待 recv 线程退出，在 Windows 上必然阻塞 5 秒。

**解决**：`SSHSession.close()` 将 transport close 放到后台 daemon 线程：
```python
def close(self):
    self._channel.close()          # 主线程，快 (~ms)
    t = threading.Thread(target=self._close_transport, daemon=True)
    t.start()                       # 后台阻塞 5s，不影响主线程
```

### 为什么不直接复用 `channel.close()` 来中断？

这是之前尝试过但失败的方案。`channel.close()` 后 buffer 排空，`recv()` 只 timeout 不抛异常 → 主循环 `timeout=0` → 永不退出。`_interrupt` 事件标志让线程在 timeout 后主动退出，不依赖 channel 状态。

## 资源回收总结

| 资源 | 回收方式 |
|------|----------|
| ThreadPoolExecutor 线程 | 工具完成后自动回 pool，`shutdown(wait=False)` 不等待 daemon 线程 |
| SSH channel | `SSHSession.close()` 或工具线程自然退出后 GC |
| SSH transport (TCP) | 后台 daemon 线程异步关闭 |
| bash 子进程 | `_kill_bash()` 杀进程树 |
| LLM streaming 线程 | Queue + stop_event → daemon 线程退出 |
| _busy_watcher 线程 | `_interrupt` 检测 → 清除 busy → 退出 |
