# Proposal

## Why

narnat agent 功能已稳定（v16.2.5），但它是长期迭代、打补丁的产物——耐用，料子已不是最初的设计。8 路只读调研（共 6,570 行现状报告，见 `docs/recast/reports/`）确认了结构性问题：

- **私有跨对象访问遍地**：`agent.py` 直摸 `session_mgr._goal_enabled`、`agent_loop._last_round_ok`、`dispatcher._executor`、`stats._model`；`terminal/__init__.py` 20+ 处直取 `SSHSession._client/_channel`。
- **模块级可变全局**：`ui/interrupt._interrupt_ctrl`、`core/interrupt._abort_callback`、`output._PLAIN/_QUIET_TOOLS`、`Background._slots`——时序正确性依赖"入口清零"惯例。
- **装配补线**：`Assembly` 构造后仍有 4 处后置赋值（`summary_anim_*`、`_set_model`、`compact_func`、`set_tool_sinks`）+ 12 个 lambda 闭包接线。
- **职责混杂**：`ToolDispatcher` 内含 UI 显示代码；`SessionManager` 902 行混装三态状态机+命令实现+配置读写；`McpManager.connect()` 单函数 70 行 6 职责。
- **结构性重复**：Terminal 与 Serial 六组函数结构重复；bash 执行循环三处复制。
- **断链与死代码**：`Serial.set_max_sessions` 从未被配置接线；`exec_signal.parse_rc` 无生产调用者；`interrupt._saved_sigint` 死逻辑。
- **零测试**：18,457 行代码无任何测试，行为无法回归验证。

既然已有"稳定可用"这个最好的证明，就不在旧衣服上继续缝补：**用最好的料子，按干净的设计重新做一件同样的衣服**。

## What Changes

- 以现有稳定版的**可观察行为**为唯一标尺，按"积木思想"重写 `narnat_agent/` 全部代码（约 18,457 行）：原子积木（配置、消息、渲染器、单个工具……）独立存活、独立测试；积木间只靠显式接口（Protocol/构造注入）连接，不掏对方内部；换一块积木不牵动全身。
- **用户无感知**：CLI 参数、交互命令集、配置格式（narnat.json 中文键+英文别名）、会话文件格式、输出样式与颜色体系、headless 协议（`[NN_DONE] reason=… rounds=…` 哨兵）全部不变。
- **BREAKING**（仅包内，不对外）：模块路径/类名允许重组，不提供旧导入路径兼容层；旧代码由 git 历史保留。
- 消除上述全部结构问题：私有访问改显式接口、可变全局改实例注入、后置补线并入构造、职责拆分、消除结构性重复。
- 修复断链与删除死代码：`Serial.set_max_sessions` 接上配置；`parse_rc`/`_saved_sigint` 等死代码按行为等价原则清理（若删除影响行为则保留并显式化）。
- 建立测试体系：为确定性逻辑（消息、压缩、配置、协议转换、工具参数处理、渲染纯函数）建立单元测试；为 CLI/headless 行为建立端到端冒烟基线。

## Capabilities

### New Capabilities

- `config`: 配置加载——narnat.json/narnat.md/skills 解析、默认值中心、`.narnat` 目录查找、字段兼容（中文键/别名）、单位换算、系统提示词拼接
- `llm`: LLM 双协议客户端——openai/anthropic 流式请求、统一事件流、重试与退避、thinking 参数厂商适配、工具定义动态管理、上下文超限检测
- `messages`: 消息存储——唯一所有者与只读视图、受控修改接口、中断修复（repair）、压缩编排（handle_compress）
- `compression`: 上下文压缩——触发判定（占比）、切点选择（保留尾部预算）、摘要请求、重建会话、溢出恢复、手动压缩
- `conversation`: 对话内循环——LLM 事件消费、工具调度协调、删除确认挂起、流中断重试、收尾软提醒（todo/后台）、目标模式自动续跑与收尾轮
- `sessions`: 会话状态机——三态（NoSession/RootSession/ChildSession）、持久化（保存/加载/删除/树形展示）、探索分支（/explore /done 增量合并）、交互命令实现（/save /ls /cd /rm /skill /thinking /mode /goal /compact /exit）
- `stats`: 统计与费用——token/缓存/费用累计、成本日志 CSV 写入与轮转、余额查询
- `interrupt`: 中断系统——ESC/SIGINT 信号采集、运行/输入双模式轮询、对 LLM/工具/后台的取消广播
- `output`: 终端输出——颜色体系（色板+角色引用+配方解析）、plain/quiet 模式、Windows VT 启用、DisplayState
- `ui`: 交互界面——流式 Markdown 渲染（表格稳定渲染/CJK 宽度）、输入会话（prompt_toolkit）、命令注册与 Tab 补全、流会话句柄、headless 纯文本输出
- `tools-file`: 文件类工具族——Read/Glob/Grep/Edit/Write（编码识别、忽略目录、截断协议、行号、diff 生成）
- `tools-shell`: Shell 与后台任务——本地命令执行（平台自适应、cd 持久化、多段执行、python 直执行）、后台槽位机制（提交/等待/状态/取消）、退出码标签协议
- `tools-remote`: 远程执行——Terminal（SSH 持久会话、交互输入、sudo 回填、文件传输）与 Serial（串口扫描/连接/交互）
- `tools-websearch`: 网页搜索工具——AnySearch API 调用与结果格式化
- `tools-todo`: 任务状态工具——TodoWrite（计划同步与 UI 通知）、GoalComplete（目标完成标记）
- `mcp`: MCP 集成——stdio 客户端（JSON-RPC 握手/工具列举/调用/重连）、连接管理（热注册/注销工具、进程生命周期）、MCP 工具（connect/disconnect）
- `app`: 应用装配与生命周期——Assembly 组装（唯一构造点）、Agent 主循环、headless 运行、退出清理

### Modified Capabilities

（无——本项目此前无 openspec 规格，本次为全新行为金标准基线。）

## Impact

- **代码**：`narnat_agent/` 全部（约 18,457 行）重写；旧实现由 git 分支历史保留，不作兼容。
- **入口**：`main.py` 参数与行为保持兼容（`-d/-v/-p/-g/-l`）。
- **数据兼容**：`.narnat/` 下 narnat.json（含中文键与旧英文别名）、会话 JSON、cost_log.csv、日志格式全部保持读写兼容。
- **平台**：Windows（cmd）与 Linux/macOS（bash）双平台行为保持；SSH/串口真实设备行为不变。
- **编译**：Nuitka 打包配置（README 命令）不变，产物仍为单文件。
- **开发方式**：新代码在隔离目录开发（不破坏当前运行中的稳定版），全部验证通过后一次性切换。
