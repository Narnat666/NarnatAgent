# FIX-ESC-1 实施任务：LLM 请求发送"可取消等待"（根治慢路径）+ openai 客户端污染修复

## 背景（先读，全部证据可复跑）

用户报告"ESC 打断不丝滑"（界面卡"思考中" 5~11.5 秒后突然显示"已打断"）。经三轮探索已闭环根因，完整机制链见 `docs/recast/esc_review_notes.md`"。要点：

- **缺陷 A（掐断不完整）**：中断时 `abort` 会关闭活跃句柄；但若请求正处于 **connect 阶段**（DNS/TCP/TLS 进行中），`close()` 是空操作——请求继续到自身完成。证据：`docs/recast/esc_probe/exp4_close_during_connect.py`（可复跑，实测 close 不中断在途 connect，请求继续到 8s 超时）。
- **缺陷 B（检查不完整，主因）**：`client.send()` / `chat.completions.create()` 阻塞期**没有任何取消检查点**——生成器体卡在阻塞调用上（流循环的 50ms 轮询尚未开始）。中断一旦没掐断请求，就要等服务端响应头（实测 5~11.5s = 服务端 TTFT）才收敛。
- **缺陷 P3（openai 路径）**：`openai_backend.py:124` 把共享 `self._client` attach 为活动句柄 → abort 关闭整个客户端 → **打断一次后整局对话全部失败**（`APIConnectionError`）。证据：`docs/recast/esc_probe/exp2_close_semantics.py`、`exp3_latency_chain.py`（场景 D）。

**行为金标准**：`openspec/changes/recast-v2/specs/llm/spec.md`（「取消与中断」章节）+ `specs/interrupt/spec.md`。本次修复是**有意行为变更**（消除慢路径），完成后由父代理统一更新 spec。

## 代码现状（已核实，按行号定位）

`narnat_agent/llm/anthropic_backend.py`：
- 139-151：`while True` 重试循环 → `client = self._client_factory()` → `self._runtime.attach_handle(client)` → `client.send(req, stream=True)` ← **B 阻塞点**
- 154-227：状态检查与错误分支（400/429/5xx 重试，`client.close()` + `detach_handle()`）
- 231-254：异常分支（TransportError / Exception，重试/报错）
- 264：`attach_handle(resp)` ← 流阶段句柄
- 295-311：流循环（`line_queue.get(timeout=STREAM_POLL_SECONDS)` + `cancel_check`）← 已有 50ms 取消轮询，但仅流阶段可达

`narnat_agent/llm/openai_backend.py`（同构，另加 P3）：
- 64-72：`self._client = client or OpenAI(...)`（**共享客户端**）
- 118-124：循环 → `attach_handle(self._client)` ← **P3 点**
- 149：`stream = self._client.chat.completions.create(**kwargs)` ← **B 阻塞点**
- 224：`attach_handle(stream)`
- 245-260：流循环（50ms 轮询 + cancel_check）

`narnat_agent/llm/runtime.py`：85-100（`attach_handle/detach_handle/abort`——abort 调 `handle.close()`）。

## 交付物

1. `narnat_agent/llm/cancelable.py`（新文件）——可取消等待原语
2. `narnat_agent/llm/anthropic_backend.py`（修改：send 线程化）
3. `narnat_agent/llm/openai_backend.py`（修改：create 线程化 + P3 修复）
4. `narnat_agent/llm/__init__.py`（如导出面需要）
5. `tests/unit/test_llm.py`（新增用例；既有用例保持全绿）

## 实施要求

### R1：可取消等待原语（`cancelable.py`）

```python
def run_cancelable(do_block: Callable[[], T],
                   cancel_check: Callable[[], bool] | None,
                   on_cancel: Callable[[], None] | None = None,
                   poll_seconds: float = 0.05) -> tuple[bool, T | None, Exception | None]:
    """在子线程执行阻塞调用；主流程以 poll_seconds 粒度轮询取消标记。

    返回 (cancelled, result, error)：
    - cancelled=True：主流程立即返回（不等子线程）；on_cancel() 已调用（尽力掐断，
      对已建立连接有效）；子线程在阻塞解除后自毁 result（close 幂等，双保险）。
    - cancelled=False：result 或 error 二选一（阻塞调用的结果）。
    """
```

实现要点（务必全对）：
- 子线程 daemon、具名（如 `narnat-llm-send`）；
- 用 `threading.Event().wait(poll)` 实现"完成即唤醒、超时轮询取消"——**正常路径不得引入固定延迟**（完成即返回）；
- 取消路径：置取消标志 → `on_cancel()`（吞异常）→ **主线程额外检查 result 是否已产生并 close**（关闭竞态窗口）→ 立即返回；
- 子线程 finally：若取消标志已置且 result 存在 → close（吞异常）——覆盖"取消后阻塞才解除"的兜底自毁；
- 竞态安全：任何时序下 result 至多被 close 两次（幂等可接受）。加注释说明。

### R2：anthropic 后端 send 线程化

- 将 `client.send(req, stream=True)`（151）替换为：
  ```python
  cancelled, resp, err = run_cancelable(
      lambda: client.send(req, stream=True),
      cancel_check,
      on_cancel=lambda: _safe_close(client),
  )
  if cancelled:
      client.close()            # 兜底幂等
      self._runtime.detach_handle()
      return                    # 生成器结束；loop 侧以 cancel_check 判定为中断
  if err is not None:
      raise err                 # 交给既有 except 分支处理（TransportError/Exception 全保留）
  ```
- 既有 except 分支（231-254）、状态检查（154-227）、流循环（295-311）**逻辑不变**；
- `attach_handle(client)` 保留（对"已连接阶段取消"仍有效——close 掐断）；
- 取消 return 前必须 `detach_handle()`（句柄清理），并保持日志风格。

### R3：openai 后端 create 线程化 + P3 修复

- **P3 修复**（请求级 scope，共享客户端不被杀死）：
  - 后端增加 `self._need_rebuild = False` + `self._client_lock = threading.Lock()`；
  - `_ensure_client()`：重建标记为真时 `close` 旧 client 并新建（`_build_client()` 抽取现有构造逻辑）；返回当前 client；
  - 每轮请求顶部：`client = self._ensure_client()`；
  - 新增请求级 scope 类（可放同文件）：
    ```python
    class _RequestScope:
        """abort 路径的请求级句柄：掐断在途请求（尽力）+ 标记客户端重建（不污染后续）。"""
        def __init__(self, backend, client): ...
        def close(self):   # abort() 调用
            self._backend._invalidate_client(self._client)
    ```
  - `_invalidate_client(client)`：置 `_need_rebuild=True`（加锁）→ `client.close()`（吞异常）；
  - `attach_handle(_RequestScope(self, client))` 替换原 `attach_handle(self._client)`；
- **create 线程化**（同 R2 模式）：
  ```python
  cancelled, stream, err = run_cancelable(
      lambda: client.chat.completions.create(**kwargs),
      cancel_check,
      on_cancel=lambda: _safe_close(client),
  )
  if cancelled:
      self._runtime.detach_handle()
      return
  if err is not None:
      raise err
  ```
- 既有重试/异常/流循环逻辑不变。

### 通用约束

- 保持现有日志（`发送请求(Anthropic)`、`网络错误(第N次重试)` 等）的**位置与文案**（日志是排障资产）；
- 所有既有重试、看门狗、`STREAM_END`、usage 兜底、`raw_sse` 等语义零变化；
- 无模块级可变状态；无跨模块私有访问；中文 docstring（注明"设计依据：esc_review_notes 根因 A/B/P3"与"待 spec 同步"）；
- 不改其他积木、不改 spec、不改 `main.py`。

## 测试要求（新增，全部离线）

1. `run_cancelable` 四态：
   - 正常完成（立即返回，无额外延迟 ≤0.3s）；
   - 取消命中（do_block 阻塞 2s，cancel 在 0.1s 触发 → 函数 ≤0.45s 返回 cancelled=True；do_block 完成时 result 被 close）；
   - 阻塞抛异常（返回 error）；
   - 竞态（do_block 完成与取消同时——用事件控制时序，断言 result 被 close 且函数返回 cancelled=True）。
2. 后端级：
   - anthropic：fake client `send` 阻塞 3s 且 `close` **无效**（模拟 exp4 connect 场景）→ cancel 在 0.1s 触发 → **生成器 ≤0.5s 结束**（核心回归！）；
   - anthropic：fake client `send` 阻塞且 `close` 有效（抛异常）→ 快路径保持；
   - openai：abort（scope.close）→ 下一轮 `_ensure_client` 返回**新** client；用 fake 客户端模拟"关闭后不可用"，断言第二轮请求成功发出（P3 回归）；
   - openai：create 阻塞 + 取消 → ≤0.5s 结束。
3. 既有 `test_llm.py` 全量保持绿（如有个别用例锁定"直接调 send"的实现细节，可更新为等价断言并在 docstring 注明）。

## 边界条款

- 只改 `narnat_agent/llm/` 与 `tests/unit/test_llm.py`；不改其他任何文件（spec 由父代理统一更新）
- 不运行 narnat；不发起真实网络请求（测试全离线）；不执行 git 写操作
- 与 spec 冲突时以"本任务书 + 根因证据"为准（本次为有意行为变更），在报告中列出差异清单

## 验收标准（可计算）

1. `cd /d D:\desktop\NarnatAgent && python -m pytest tests/unit/test_llm.py -q` 全绿
2. `python tests/check_layering.py` 无违规
3. 新增用例逐条通过（报告列出用例名与断言摘要）
4. 三个关键数字（报告给出实测）：① connect 阶段取消 → 收敛 ≤0.5s；② 正常路径无额外延迟（对比基线 <50ms 增量）；③ P3 场景第二轮请求成功
5. 未改文件清单核对（`git status --porcelain` 仅含本任务 5 个文件）

## 失败报告格式

①已尝试方案；②实际输出或报错原文（引用）；③怀疑原因。「确认失败」是合法终点。
