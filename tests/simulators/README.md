# NarnatAgent 仿真平台开发手册

## 一、核心理念

### 1.1 为什么需要仿真平台

在真实环境（Ubuntu/Windows）上测试成本高、速度慢、不可重复。
仿真平台让AI在本地进程内自由测试，快速穷举边界条件，
通过大量模拟测试确保模块质量，再到真实环境做最终验证。

**类比：飞机的风洞测试**
- 机翼(terminal) → mock_ssh_server → 风洞测试
- 发动机(llm) → mock_llm_server → 台架测试
- 航电(web_search) → mock_http_server → 仿真舱测试
- 机身(agent) → mock_agent → 全机地面测试

### 1.2 暴力测试 vs 仿真测试

| | 暴力测试 | 仿真测试 |
|---|---|---|
| **本质** | 穷举边界值 | 在仿真环境中自由穷举 |
| **环境** | 真实环境或临时文件 | 仿真模块(MockXXX) |
| **覆盖** | 已知边界 | 未知边界（AI自由探索） |
| **速度** | 快（纯逻辑） | 中（需启动仿真服务） |
| **深度** | 单模块 | 模块+交互+闭环 |
| **关系** | 仿真测试包含暴力测试 | 暴力测试是仿真测试的子集 |

**仿真测试 = 仿真环境 + 暴力穷举 + 交互闭环**

### 1.3 目录结构

```
tests/
├── simulators/                    # 仿真模块（纯基础设施，不含测试逻辑）
│   ├── __init__.py
│   ├── mock_ssh_server.py         # SSH服务器仿真 → Terminal/Remote
│   ├── mock_llm_server.py         # LLM API仿真 → Agent闭环
│   ├── mock_http_server.py        # HTTP服务器仿真 → WebSearch
│   ├── mock_filesystem.py         # 文件系统仿真 → Read/Glob/Grep/Edit/Write
│
├── test_simulations/              # 仿真测试（基于simulators的暴力测试）
│   ├── __init__.py
│   ├── test_terminal_sim.py       # Terminal仿真测试
│   ├── test_file_tools_sim.py     # 文件工具仿真测试
│   ├── test_llm_sim.py            # LLM仿真测试
│   └── test_http_sim.py           # HTTP仿真测试
│
├── test_brutal.py                 # 暴力测试（原有）
├── test_tools/                    # 工具测试（原有）
└── ...
```

---

## 二、现有仿真模块

### 2.1 MockSSHServer — SSH服务器仿真

**对应模块**: `narnat_agent/tools/terminal.py`, `narnat_agent/tools/remote.py`

**能力**:
- invoke_shell PTY交互式终端
- SFTP子系统（remote.py测试）
- 虚拟文件系统（mkdir/touch/cat/ls等操作真实可见）
- sudo密码验证
- 后台进程模拟(nohup/disown)
- 40+个Linux命令模拟

**用法**:
```python
from tests.simulators.mock_ssh_server import MockSSHServer
from narnat_agent.tools import terminal

with MockSSHServer() as server:
    # 连接
    terminal.execute(action="connect", host=server.host,
                     username=server.username, password=server.password,
                     port=server.port)
    # 执行命令
    result = terminal.execute(action="exec", host=server.host, command="ls -la")

    # 直接操作VFS（shell中可见）
    server.vfs.write_file("/tmp/test.txt", b"hello")
    result = terminal.execute(action="exec", host=server.host, command="cat /tmp/test.txt")
```

**VFS直接测试（不需要SSH连接）**:
```python
from tests.simulators.mock_ssh_server import SimulatedShell, VirtualFileSystem

shell = SimulatedShell()
assert shell.execute("echo hello") == "hello"
shell.execute("mkdir -p /tmp/test")
assert "test" in shell.execute("ls /tmp")

vfs = VirtualFileSystem()
vfs.write_file("/tmp/file.txt", b"content")
assert vfs.read_file("/tmp/file.txt") == b"content"
```

### 2.2 MockLLMServer — LLM API仿真

**对应模块**: `narnat_agent/core/llm.py`, `narnat_agent/core/agent.py`

**能力**:
- 模拟OpenAI兼容API（/v1/chat/completions）
- 预设tool_call序列，Agent按序收到
- 支持流式(SSE)和非流式响应
- 队列耗尽自动返回结束消息

**用法**:
```python
from tests.simulators.mock_llm_server import MockLLMServer

with MockLLMServer() as server:
    # 预设AI的决策序列
    server.enqueue_tool_calls([
        {"name": "Read", "arguments": {"file_path": "/tmp/test.txt"}},
    ])
    server.enqueue_text("文件内容已读取。")
    server.enqueue_tool_calls([
        {"name": "Edit", "arguments": {"file_path": "/tmp/test.txt",
                                       "old_string": "old", "new_string": "new"}},
    ])
    server.enqueue_text("修改完成。")

    # Agent使用server.base_url作为API端点
    # agent = Agent(ai_config=AIConfig(
    #     api_key="test", base_url=server.base_url, model="mock-model"))
```

### 2.3 MockHTTPServer — HTTP服务器仿真

**对应模块**: `narnat_agent/tools/web_search.py`

**能力**:
- 模拟 Open WebSearch daemon 的 API 接口
- 健康检查端点（GET /health）
- 搜索API端点（POST /search，JSON格式）
- 预设搜索结果

**用法**:
```python
from tests.simulators.mock_http_server import MockHTTPServer

with MockHTTPServer() as server:
    server.add_search_result("python教程", [
        {"title": "Python官方教程", "url": "https://docs.python.org/3/tutorial/",
         "snippet": "Welcome to Python"},
    ])
    # server.base_url 可作为 daemon 地址
```

### 2.4 MockFileSystem — 文件系统仿真

**对应模块**: `narnat_agent/tools/read.py`, `glob.py`, `grep.py`, `edit.py`, `write.py`

**能力**:
- 创建临时目录，填充典型项目结构
- 自动清理
- 便捷的文件/目录创建和读取

**用法**:
```python
from tests.simulators.mock_filesystem import MockFileSystem

with MockFileSystem() as fs:
    # fs.root 是临时目录路径
    # 已创建: src/main.py, tests/test_main.py, pyproject.toml 等
    path = fs.abs_path("src/main.py")
    result = read.execute(path)

    # 创建自定义文件
    fs.create_file("src/new.py", "def hello(): pass")

    # 检查文件存在
    assert fs.file_exists("src/new.py")
```

---

## 三、如何开发新仿真模块

### 3.1 开发流程

```
1. 识别新模块的外部依赖
2. 设计仿真接口（与真实接口一致）
3. 实现仿真模块 → tests/simulators/mock_xxx.py
4. 编写仿真测试 → tests/test_simulations/test_xxx_sim.py
5. 运行测试验证 → pytest tests/test_simulations/test_xxx_sim.py
6. 全部通过 → 可用于AI自由测试
```

### 3.2 仿真模块设计原则

1. **接口一致**: 仿真模块的接口与真实模块完全一致，AI代码不需要改
2. **上下文管理器**: 用`with MockXXX() as mock:`模式，自动启动/停止
3. **纯仿真不含测试**: `simulators/`只做仿真，`test_simulations/`才含测试逻辑
4. **可独立启动**: 每个仿真模块可单独使用，不依赖其他仿真模块
5. **轻量级**: 仿真模块代码量应是业务模块的1/5~1/10

### 3.3 仿真模块模板

```python
"""仿真XXX服务器 —— 本地闭环测试YYY工具

[描述仿真模块的能力和用法]

用法:
    with MockXXX() as server:
        # server.xxx 可用
        # YYY工具可直接使用server
"""

import socket
import threading
import time


class MockXXX:
    """仿真XXX服务器

    用法:
        with MockXXX() as server:
            # 使用server
    """

    def __init__(self, ...):
        self.host = "127.0.0.1"
        self.port = 0
        self._sock = None
        self._thread = None
        self._running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        """启动仿真服务器"""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def stop(self):
        """停止仿真服务器"""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _serve(self):
        """服务器主循环"""
        while self._running:
            try:
                self._sock.settimeout(0.5)
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # 处理客户端...
```

### 3.4 仿真测试模板

```python
"""XXX仿真测试 —— 基于MockXXX的闭环暴力测试

AI在仿真XXX环境中自由测试YYY工具。
"""

import pytest
from narnat_agent.tools import yyy
from tests.simulators.mock_xxx import MockXXX


# ── 辅助 ──

def _r(result):
    """解包工具返回值: tuple→取第一个元素, str→原样返回"""
    return result[0] if isinstance(result, tuple) else result


# ── 正常用例 ──

class TestXXXBasic:
    """基础功能测试"""

    def setup_method(self):
        self.mock = MockXXX()
        self.mock.start()

    def teardown_method(self):
        self.mock.stop()

    def test_normal_operation(self):
        result = yyy.execute(...)
        assert "expected" in _r(result)


# ── 边界用例 ──

class TestXXXBoundary:
    """边界场景测试"""

    def setup_method(self):
        self.mock = MockXXX()
        self.mock.start()

    def teardown_method(self):
        self.mock.stop()

    def test_empty_input(self):
        ...

    def test_very_long_input(self):
        ...

    def test_unicode_input(self):
        ...


# ── 极端用例 ──

class TestXXXBrutal:
    """极端场景暴力测试"""

    def test_rapid_operations(self):
        """快速连续操作不崩溃"""
        for i in range(100):
            result = yyy.execute(...)
            assert isinstance(_r(result), str)

    def test_mixed_operations(self):
        """混合操作不崩溃"""
        ...
```

---

## 四、如何用仿真平台做测试

### 4.1 AI自由测试流程

```
1. 启动仿真环境
2. 在仿真环境中自由穷举测试
3. 发现bug → 修复 → 重新测试
4. 全部通过 → 总结测试边界和测试项目
5. 到真实环境做最终验证
```

### 4.2 测试三层递进

| 层级 | 内容 | 命令 |
|------|------|------|
| **单元测试** | SimulatedShell/VFS纯逻辑 | `pytest tests/test_simulations/ -k "Unit"` |
| **仿真测试** | MockXXX闭环测试 | `pytest tests/test_simulations/ -k "Basic or Boundary"` |
| **暴力测试** | 极端场景穷举 | `pytest tests/test_simulations/ -k "Brutal"` |

### 4.3 运行命令

```bash
# 运行全部仿真测试
pytest tests/test_simulations/ -v

# 运行特定模块仿真测试
pytest tests/test_simulations/test_terminal_sim.py -v
pytest tests/test_simulations/test_file_tools_sim.py -v
pytest tests/test_simulations/test_llm_sim.py -v
pytest tests/test_simulations/test_http_sim.py -v

# 运行快速测试（不含SSH连接，约10秒）
pytest tests/test_simulations/ -v -k "Unit or file_tools or llm_sim or http_sim"

# 运行SSH仿真测试（较慢，约5分钟）
pytest tests/test_simulations/test_terminal_sim.py -v -k "not Unit"
```

### 4.4 测试边界总结模板

每个模块测试完毕后，AI应总结：

```markdown
## [模块名] 测试边界总结

### 已验证的边界
- [x] 正常输入
- [x] 空输入
- [x] 超长输入
- [x] Unicode输入
- [x] 特殊字符
- [x] 连续操作不丢输出
- [x] 错误恢复

### 已发现的bug
- [bug描述] → [修复方式]

### 未覆盖的边界（需真实环境验证）
- [ ] 真实网络延迟
- [ ] 真实文件权限
- [ ] 真实进程信号
```

---

## 五、模块与仿真对应关系

| 业务模块 | 仿真模块 | 仿真测试 | 状态 |
|----------|----------|----------|------|
| `tools/terminal.py` | `mock_ssh_server.py` | `test_terminal_sim.py` | 已完成 |
| `tools/remote.py` | `mock_ssh_server.py`(SFTP) | `test_terminal_sim.py` | 已完成 |
| `tools/bash.py` | 无需独立仿真(terminal仿真覆盖) | `test_terminal_sim.py` | 已完成 |
| `tools/read.py` | `mock_filesystem.py` | `test_file_tools_sim.py` | 已完成 |
| `tools/glob.py` | `mock_filesystem.py` | `test_file_tools_sim.py` | 已完成 |
| `tools/grep.py` | `mock_filesystem.py` | `test_file_tools_sim.py` | 已完成 |
| `tools/edit.py` | `mock_filesystem.py` | `test_file_tools_sim.py` | 已完成 |
| `tools/write.py` | `mock_filesystem.py` | `test_file_tools_sim.py` | 已完成 |
| `tools/web_search.py` | `mock_http_server.py` | `test_http_sim.py` | 已完成 |
| `core/llm.py` | `mock_llm_server.py` | `test_llm_sim.py` | 已完成 |
| `core/agent.py` | `mock_llm_server.py` | 待开发 | 待开发 |
| `tools/todo_write.py` | 无需仿真(纯逻辑) | 待开发 | 待开发 |
| `core/compressor.py` | 无需仿真(纯逻辑) | 待开发 | 待开发 |
| `ui/ui_design.py` | `mock_terminal_ui.py` | 待开发 | 待开发 |

---

## 六、新增模块时的操作清单

当开发一个新模块 `narnat_agent/tools/new_tool.py` 时：

1. **识别外部依赖**: 新模块依赖什么外部服务？（SSH/HTTP/LLM/文件系统/...）
2. **检查现有仿真**: `simulators/`下是否已有对应仿真模块？
3. **如果没有，创建仿真模块**:
   - `tests/simulators/mock_new_tool.py`
   - 遵循3.2设计原则和3.3模板
4. **创建仿真测试**:
   - `tests/test_simulations/test_new_tool_sim.py`
   - 遵循3.4模板
   - 三层递进: 正常用例 → 边界用例 → 极端用例
5. **运行测试**:
   - `pytest tests/test_simulations/test_new_tool_sim.py -v`
6. **修复发现的bug**
7. **总结测试边界**（4.4模板）
8. **更新本文档的对应关系表**

---

## 七、关键提醒

1. **仿真模块一旦稳定就不再改动** — 它是基础设施，不像业务模块频繁迭代
2. **仿真不需要100%还原真实环境** — 发现90%的bug即可，剩下10%由真实环境验证
3. **Python写仿真极快** — 一个仿真模块约100-500行，开发量是业务模块的1/5~1/10
4. **仿真测试可以替代大部分手动测试** — 在仿真环境跑100次暴力测试，比在真实Ubuntu上手动测1次效率高100倍
5. **工具返回值是tuple** — `(llm_result, color_diff)`，测试时用`_r()`解包
