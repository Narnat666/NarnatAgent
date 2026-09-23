# Tasks

## 1. 协议补声明

- [ ] 1.1 `contracts/output.py`：`OutputSink` 协议补 `flush()` / `pause()` / `resume()` 三个方法声明（docstring 注明语义与守卫；实现方已就绪）

## 2. 调度器接线

- [ ] 2.1 `conversation/dispatch.py`：新增 `DisplayProbe(CancelProbe)` 协议（`pause/flush/resume`）
- [ ] 2.2 `execute` 及 `_run_parallel / _run_write_groups / _run_serial` 签名升为 `DisplayProbe`；`_run_sequential_group` 增 `sink` 参数
- [ ] 2.3 `_run_single(name, arguments, sink)`：`sink.pause()` + `sink.flush()` → 显示摘要 → 执行 → 差异/失败显示 → `finally: sink.resume()`；全部线程池提交点下传 `sink`

## 3. 测试

- [ ] 3.1 `tests/unit/test_conversation.py`：`FakeSink` 补 `pause/flush/resume` 记录（与 `FakeConsole` 共享事件序列）
- [ ] 3.2 顺序断言：AI 文本轮 → 工具轮；断言「flush 先于首个工具行写入、resume 晚于最后一个显示」；覆盖只读并行 / 写入组 / 串行三类路径
- [ ] 3.3 静默模式用例：无工具行输出但停/落/恢复照常
- [ ] 3.4 全量单测 + 分层检查通过

## 4. 真机验证与归档

- [ ] 4.1 真机读屏验证（装置 `docs/recast/esc_probe_live`）：发"读一个文件并汇报"类任务，逐帧确认显示顺序为「AI 文本 → 工具行 → 结果」
- [ ] 4.2 `openspec archive fix-tool-line-flush`；更新 `docs/recast/PROGRESS.md`
