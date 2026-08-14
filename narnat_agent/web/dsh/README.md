# DSH Web 前端中转模块（narnat_agent/web/dsh/）

把 **DeepSeek Harness（DSH）的 Web 界面**集成到 Narnat Agent 上：

- 界面 = DSH 原生前端（React 插件式外壳，零修改跑原始 DSH 界面风格）
- 数据 = Narnat Agent（会话 / 消息 / 工具调用 / 模型 / 技能 / 设置）
- 中转 = 本模块实现的"宿主协议"翻译层

```
浏览器 (DSH 前端)
   │  GET /            ← 静态外壳 dist + 注入 window.__DSH_BOOT__ 插件清单
   │  GET /plugins/<id>/client.js   ← DSH 各客户端插件包
   │  POST /api/<method>            ← DSH RPC 信封（client-request/server-response）
   │  GET /api/events.mux|host      ← SSE/WS 下行事件流
   ▼
narnat_agent/web/dsh/  (本模块, 纯标准库)
   ├─ server.py       HTTP+SSE+WS 服务器（ThreadingHTTPServer）
   ├─ protocol.py     DSH RPC 四象限消息编解码
   ├─ events.py       mux/host 事件中枢 + 每会话 seq 基线 + 投影快照
   ├─ translate.py    Narnat 数据 → DSH 协议（会话/消息/事件/模型）
   ├─ handlers.py     全部 /api 端点 + typert remote（commands/list 等）
   ├─ bridge.py       与 Agent 核心的桥（输入队列合并、流包装、轮次同步）
   └─ collect_dsh.py  工件收集器（从 DSH 仓库构建产物复制前端并生成插件清单）
   ▼
Narnat Agent 核心（只改了 main.py 与 core/agent.py 各几行）
```

## 使用

```bash
# 1. 一次性准备前端工件（需要 DSH 仓库已 pnpm build）
python -m narnat_agent.web.dsh.collect_dsh --dsh D:\dsh\deepseek-harness --preset narnat

# 2. 启动 Agent（终端与 Web 双通道，可同时使用）
python main.py --web            # 浏览器打开 http://127.0.0.1:8765/
```

`--web` 可选 `--port <端口>`（默认 8765）。终端输入与 Web 输入汇入同一队列，
同一时刻只有一轮对话在执行，两个界面看到的是同一份会话。

## 模块裁剪（只保留 Narnat 有的功能）

`--preset narnat` 从 DSH 启动清单中移除以下 18 个模块（Narnat Agent 没有对应能力）：

| 移除 | 原因 |
|---|---|
| `dsh-cordis-client-runner` + `dsh-client-ui-cordis` | Cordis 配置运行面板 |
| `ui-directory-picker-browse/native` | 宿主目录选择器（且抢同一插槽） |
| `ui-goal` / `ui-subagent` / `ui-jobs` / `ui-plan` / `ui-workflow-run` | 无目标/子代理/后台任务/计划审批/工作流 |
| `ui-agent-preset` / `ui-permission-presets` | 无 agent 预设 / 权限体系 |
| `ui-deliverables` / `ui-user-questions` / `ui-message-feedback` | 无产出物跟踪/用户提问/消息反馈 |
| `ui-settings-plugins` / `plugin-inventory` / `settings-models` / `settings-general` | 设置面板（配置走 .narnat/config/narnat.json），"设置"按钮随之消失 |

保留 21 个模块：会话/对话/工具轨迹/技能/斜杠命令/模型与思考强度切换/主题/
虚拟工作区 + 传输与模块内核。

## 关键协议映射

- 会话：Narnat `(name, parent)` → DSH sessionId（uuid5 确定性，跨重启稳定）；
  `/save` 后自动出现在侧栏（host/session-added 帧）
- **工作区**：DSH 的会话必须归属工作区，否则输入框被锁（"选择一个工作区开始"）。
  适配器提供虚拟 "Narnat 会话" 工作区承载全部会话（含未保存的 live 会话）
- **模型/思考强度**：`session.models` 返回 `routable: true` 并在模型目录里带
  `reasoning.efforts`（Narnat 的思考强度选项 high/max），模型选择器可切换
  模型与"推理等级"；`session.selectModel` 同时写入 `config.ai.model` 与
  `config.ai.thinking_effort`（/thinking 同款语义）
- 消息：OpenAI 风格 → DSH SessionEvent 序列
  `turn/start → user/message → step/start → assistant/message(+tool-call块) →
  tool/result → step/end → turn/end`，表面事件带 `surfaceOp: "append"`。
  两条一致性约束（违反会导致客户端渲染错乱）：
  1. 同一轮的 turn/step 编号在 claim、流式、差量翻译三处必须一致
     （claim 在用户消息入列表前计算，流包装复用 claim 的编号）；
  2. `step/end` 必须在 `assistant/message` 之后发出（先关 step 会把
     流式片段冻结成"已停止"节点并与最终消息重复渲染）。
- 流式：终端 UIStreamSession 被 `WebStreamBridge` 包装，增量转为
  `assistant/chunk` 事件广播；轮次结束后差量同步权威消息事件
- 标题/todos：经 `session/projection` 帧推送（侧栏行读 title 投影键）
- 设置：`ui-onboarding` 等命名空间持久化到 `.narnat/data/dsh_settings.json`
  （欢迎弹窗预置已确认版本，主题/偏好写入可正常保存）

## 两个必要的宿主端适配（已内置于 collect_dsh）

1. **事件流改走 SSE**：DSH 浏览器客户端默认用 WebSocket 打开事件流；DSH 协议
   本身提供完全等价的 SSE 载体（AbstractApiClient 默认即 readSse），收集器对
   `dsh-client-connection` 包做两处文本补丁把调用点指回 `readSse`，避开标准库
   HTTP 服务器在浏览器 WS 升级上的竞态。WS 端点仍保留（原生 socket 客户端可用）。
2. `host.describe` 的 `attachedSessions` 按 DSH schema 是**已挂载会话数量**（int），
   不是 id 列表。

## 测试

```bash
python -m narnat_agent.web.dsh.smoke_test   # 传输层冒烟（静态/插件/RPC/SSE/WS）
```

浏览器级验证脚本（Playwright）位于会话草稿文件中（`_ui_trace.mjs`），
验证过：侧栏会话列表、会话打开、完整对话渲染、实时收发消息。

## 再生成工件（DSH 升级后）

```bash
# DSH 仓库重新 pnpm build 后重跑收集器即可（同第 1 步命令）
```
