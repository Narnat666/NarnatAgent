# esc_probe —— E1 ESC 打断机制受控实验

本目录是 E1 任务（`docs/recast/tasks/E1_esc_probe_mechanism.md`）的实验代码与产物。
**只读导入主线实现（不修改 `narnat_agent/`），不发起任何 LLM 网络请求。**

## 复跑

```cmd
cd /d D:\desktop\NarnatAgent
chcp 65001 >nul
python docs\recast\esc_probe\exp1_key_judgement.py    :: 按键序列 × 时间间隔 → 判定表（15 组 + 稳定性 + 降级）
python docs\recast\esc_probe\exp2_close_semantics.py  :: 连接关闭语义（本地回环 stub server，4 用例）
python docs\recast\esc_probe\exp3_latency_chain.py    :: 中断置位 → 「已打断」的端到端延迟（5 场景）
```

产物 JSON 落在 `out/`（`exp1_results.json` / `exp2_results.json` / `exp3_results.json`）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `fake_source.py` | 假字符源：按「字节 + 到达时刻」时间表投喂，驱动**真实的** `poll_keys`/`scan_escape`/`KeyListener`/`InterruptBus` |
| `exp1_key_judgement.py` | 判定表实验 + 重复稳定性 + 采集器静默降级验证 |
| `exp2_close_semantics.py` | httpx / OpenAI SDK 的 close 语义（是否中断在途请求、关闭后可否复用、流关闭是否中断读取） |
| `exp3_latency_chain.py` | 真 `ConversationLoop` + 真 `UiSink` + 真 `OpenAIBackend` + 假 OpenAI 客户端：量化「中断置位 → 屏幕出现『已打断』」 |

## 结论速览（详见报告 `../reports/E1_esc_probe.md`）

1. **判定窗口吞键（已证）**：Esc 之后 20ms 内出现非 Esc 字节、且其后 5 字节内无 Esc → Esc 连同最多 5 个后续字节被**丢弃**，判定"非中断"（C04/C08/C09）。
2. **"先按错键"不是根因（已证）**：错键在前（C02/C03/C12/C15）都能中断；敏感条件在 Esc **之后**（C04 型）。
3. **共享客户端被永久关闭（已证，严重）**：响应头到达前中断 → `abort` 关闭的是**整个 OpenAI 客户端**；请求会被中断（收敛快），但该客户端**不可复用** → 中断一次后后续所有请求失败（exp3-D）。
4. **收敛路径本机延迟 ≤ 0.3s（已测）**：A=0.0s、B=58ms、E（思考静默）=8ms；只有"close 无法中断在途请求"的反事实条件下才会等到响应头（C=4.07s）。
5. **静默降级（已证存在，真机是否命中待 E2）**：源构建失败/返回 None → 采集线程静默退出，Esc 完全无效且无任何提示。
