# ESC 打断问题：根因链与修复方案（父代理综合结论）

> 依据：E1（机制级，`reports/E1_esc_probe.md`）+ E2（真机复现，`reports/E2_esc_repro.md`）+ 父代理补充实验（`esc_probe/exp4_close_during_connect.py`）。

## 一、慢路径完整机制链（已闭环，与全部观察一致）

以 E2 慢路径 run（`new_wt_alternate_r1`，11.26s）为标本：

```
t=0.00  用户回车提交「你好」
t≈0.06  begin_turn → 采集线程启动（msvcrt 源）；loop 开始 → chat_stream 生成器 →
        convert → attach_handle(client) → client.send() 阻塞【连接建立 + 等响应头】
t=0.28  用户按 ESC → 采集器判中断【成功】→ raise_（中断标志置位）→ abort → close(client)
        ⚠ 若此刻请求仍在 connect 阶段（DNS/TCP/TLS 进行中）→ close 是空操作
          （exp4 实证：close 不中断在途 connect，请求继续到自身超时/完成）
        → 请求失控，继续等待响应头；采集线程判中断后退出
t=0.55+ 用户继续按键（` / ESC ...）→ 无采集器在读 → 按键在控制台缓冲积压
        （"狂按也无效"字面属实；回合结束后残留按键被 ptk 读走 → 输入框残影）
t=0.28 ~ 11.5 屏幕只有"思考中"（无反馈：中断已置位但无任何显示；请求未被掐断）
t≈11.5  服务端响应头到达 → send() 返回 → 进入流循环 → 首次 50ms 轮询命中 cancel_check
        → 生成器收敛 → UI 显示"已打断"（用户感知：11.5s 后才打断）
```

**根因（三个缺陷叠加）**：

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| A | 取消"掐断"不完整：`close()` 漏掉 connect 阶段（httpx 的 close 只关连接池，不中断在途 connect） | `llm/openai_backend.py:149`、`llm/anthropic_backend.py:151` 所在流程 | 请求失控，继续等到响应头 |
| B | 取消"检查"不完整：`send()/create()` 阻塞期**无取消检查点**（生成器体卡在阻塞调用上，流循环的 50ms 轮询尚未开始） | 同上；流循环检查点在 `anthropic_backend.py:295-300` / `openai_backend.py:245-260` 但不可达 | 即使已知被取消，也只能等服务端 |
| C | 按键层：判中断后采集线程退出 + 按键积压（残影/幽灵输入）；且 msvcrt 字节流源存在 20ms 窗口吞键（E1 P1，边缘） | `interrupt/keys.py:224-243`、`203-221` | 扰动体验；吞键为边缘缺陷 |

**快/慢随机性来源**：第一次 ESC 到达时请求所处阶段——"连接已建立/等响应头/流中"时 close 有效（快路径 0.04-0.59s）；"connect 阶段"时 close 漏掉（慢路径，等响应头）。与按键序列类型无关（E2 复现矩阵证实），概率 ~1/3。

**影响的实现范围**：新旧实现同等复现（属历史缺陷）；anthropic 与 openai 两条路径同构（openai 另有 P3：abort 关闭共享客户端 → 打断后整局失败，`openai_backend.py:124` + `runtime.py:93-100`，已被 E1 实验二/三证实）。

## 二、修复方案（本次实施）

| # | 修复 | 对治 | 设计要点 |
|---|---|---|---|
| R1 | **请求发送"可取消等待"**（llm 层） | A+B | `send()/create()` 移入子线程执行；主流程以 50ms 粒度轮询 `cancel_check`；取消命中 → ①尽力 close（对已连接阶段有效）②标记放弃 ③**立即返回**（不等线程）④子线程在阻塞解除后自毁（close resp/stream）——任何阶段都能 ≤100ms 收敛 |
| R2 | **openai 共享客户端不再被 abort 杀死** | P3 | abort 关闭的是"请求级 scope"（scope.close 触发 client 重建标记）；下次请求前重建 client——打断后会话继续可用 |
| R3 | **Windows 主路径改事件源** | C（吞键） | 优先 `ReadConsoleInput` 事件源（只认 Esc 键按下事件；方向键/Alt 组合不误触发），msvcrt 降级保留；spec「平台按键采集差异」同步更新 |
| R4 | **中断后继续消费按键** | C（积压/残影） | 采集循环判中断后继续读取（丢弃按键、不重复触发）直到停止——消灭积压与幽灵输入；spec「键盘监听生命周期」同步更新 |

**不做**（有意取舍）：P6"正在中断…"即时提示——R1 后收敛 ≤100ms，"已打断"本身即是反馈，不再引入额外输出复杂度；若真机验证发现仍慢再补。

## 三、验收（父代理亲自执行）

1. 单元测试全绿 + 分层检查（`tests/check_layering.py`）。
2. **E2 装置复跑**（`docs/recast/esc_probe_live/runner.py`）：基线/交错/狂按序列各 ≥5 次 → **新实现慢路径（>2s）应全部消失**（全部 <1.5s；目标 <0.6s）。
3. **专项实验**：新增"connect 阶段取消"用例（fake 阻塞 send + 取消 → 断言 ≤0.3s 收敛且线程自毁）。
4. **P3 验证**：响应头前打断 → 第二轮请求成功（不再 `APIConnectionError`）。
5. 回归：正常对话 / 工具调用 / 流中断重试 / 溢出恢复 路径冒烟。

## 四、参考

- E1 判定表与实验：`docs/recast/esc_probe/`（exp1-exp4 可复跑）
- E2 复现矩阵与时间线：`docs/recast/esc_probe_live/`（runner.py 可复跑）
- 用户现场证据：`D:\AgentByNarnat\.narnat\data\.narnat_history`（16:50:39 提问 → 17s 后恢复输入）
