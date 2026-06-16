# UI 接口文档

## 架构

```
┌─────────────────────────────────────┐
│              后端                    │
│  ┌─ SessionCallbacks (持久化)       │
│  └─ generate(prompt) → Iterator    │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│           UIInterface               │
│  ├─ read_input()   → str | None    │
│  ├─ create_stream() → UIStreamSession │
│  └─ dispatch_command(cmd, args)     │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│        UIStreamSession              │
│  ├─ feed(chunk)    ← 后端喂 token   │
│  ├─ cancelled      → 中断标志       │
│  └─ finish(in,out,cache,cost)       │
└─────────────────────────────────────┘
```

## 快速开始

```python
from ui.ui_design import UIInterface, SessionCallbacks

class MyBackend(SessionCallbacks):
    def on_save(self, name: str) -> str:
        return ""

    def on_show(self) -> str:
        return ""

    def on_enter(self, name: str) -> str:
        return ""

    def on_delete(self, name: str) -> str:
        return ""

ui = UIInterface("deepseek-chat", callbacks=MyBackend())
ui.start()

while True:
    line = ui.read_input()
    if line is None:
        continue
    stripped = line.strip()
    if not stripped:
        continue
    if stripped == "/exit":
        break
    if stripped.startswith("/"):
        parts = stripped.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        if ui.dispatch_command(cmd, args):
            continue

    stream = ui.create_stream()
    try:
        for token in your_backend.generate(stripped):
            if stream.cancelled:
                break
            stream.feed(token)
        stream.finish(
            input_tokens=in_count,
            output_tokens=out_count,
            cache=0,
            cost=0.0)
    except KeyboardInterrupt:
        ui.on_interrupted()
        stream.abort()
```

---

## API 参考

### UIInterface

| 方法 | 返回值 | 说明 |
|------|--------|------|
| `UIInterface(name, callbacks)` | — | 构造函数。`callbacks` 选填，不传则命令输出默认提示 |
| `.start()` | — | 显示头部信息，初始化输入会话 |
| `.read_input()` | `str \| None` | 阻塞等待用户输入。`None` = 用户 Ctrl+C |
| `.dispatch_command(cmd, args)` | `bool` | 分发命令到 SessionCallbacks。`True` = 已处理 |
| `.create_stream()` | `UIStreamSession` | 创建流式输出会话（启动 spinner + 中断监听） |
| `.on_interrupted()` | — | 中断后重置输入会话 |
| `.begin_compressing()` | — | 启动橙色"正在压缩..."旋转动画 |
| `.end_compressing()` | — | 停止压缩动画，清除该行 |

### UIStreamSession

| 属性/方法 | 类型 | 说明 |
|-----------|------|------|
| `.feed(chunk)` | 方法 | **逐 token 喂入**。支持单字符 `"你"` 也支持多字符 `"你好\n"`，内部状态机自动处理行分割和代码块边界 |
| `.cancelled` | 属性 `bool` | 用户是否按了 ESC。`True` 时后端应停止生成 |
| `.finish(in, out, cache, cost)` | 方法 | 正常结束会话。刷出残留缓冲，显示 token/费用统计 |
| `.abort()` | 方法 | 异常/中断收尾。停止 spinner，显示"已打断" |

### SessionCallbacks

后端继承此类实现持久化。返回值约定：**空串表示成功，非空串为给用户的错误提示**。

| 方法 | 参数 | 返回值 | 触发命令 |
|------|------|--------|----------|
| `on_save(name)` | 用户输入的名称 | `str` | `/save <名称>` |
| `on_show()` | 无 | `str` | `/show` |
| `on_enter(name)` | 用户输入的名称 | `str` | `/enter <名称>` |
| `on_delete(name)` | 名称或 `"--all"` | `str` | `/delete <名称>` 或 `/delete --all` |

示例：

```python
class MyBackend(SessionCallbacks):
    def __init__(self):
        self._sessions = {}

    def on_save(self, name: str) -> str:
        if not name.strip():
            return "名称不能为空"
        self._sessions[name] = "当前会话内容"
        return ""

    def on_show(self) -> str:
        if not self._sessions:
            return ""
        return "\n".join(f"  {k}" for k in self._sessions)

    def on_enter(self, name: str) -> str:
        if name not in self._sessions:
            return f"会话不存在: {name}"
        return self._sessions[name]

    def on_delete(self, name: str) -> str:
        if name == "--all":
            self._sessions.clear()
            return ""
        if name not in self._sessions:
            return f"会话不存在: {name}"
        del self._sessions[name]
        return ""
```

## 中断机制

用户在流式输出期间按 `ESC` 或 `Ctrl+C` 时 `stream.cancelled` 变为 `True`。后端应在每次 token 生成前检查此标志：

```python
for token in backend.generate(prompt):
    if stream.cancelled:        # 用户打断
        break
    stream.feed(token)
```

## 注意事项

- `stream.feed()` 内部状态机是字符级的，`\n` 触发行渲染，`` ``` `` 触发代码块边界。后端无需关心 Markdown 结构
- `StreamingRenderer` 每行实时输出到 stdout，无需等全文结束
- Tab 可补全命令：`/clear /save /show /enter /delete /exit`
- 测试：`python narnat_agent/ui/test_ui.py`
