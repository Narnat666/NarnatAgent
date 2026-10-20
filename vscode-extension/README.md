# Narnat VSCode 扩展

> 左侧图标一键唤起，面板停靠在右侧辅助侧边栏，代码始终可见。
> 已接真实 Python 后端（JSON-RPC over stdio）；后端不在时自动降级为离线演示态。

## 安装

**方式一：装 vsix（最快）**

1. `code --install-extension narnat-vscode.vsix`
   （或在 VSCode 里 `扩展` → 右上 `…` → `从 VSIX 安装…` → 选文件）
2. 重载窗口
3. 跳到下面「首次使用」三步

扩展自带 `bin/narnat.exe`（约 93MB），装了就能用，不需要本机装 Python。

**方式二：源码开发模式**

前置条件（缺一不可，缺了会表现为「面板一直停在离线演示态」）：

- Node.js 18+
- Python 3.10+，且装好后端依赖（`httpx`、`pyyaml` 等，见仓库根的导入语句）
- 仓库根能跑通 `python main.py --version`

```bat
cd <仓库根>
python main.py --version                       :: 先确认后端能跑
code --extensionDevelopmentPath=<仓库根>\vscode-extension --new-window
```

把 `<仓库根>` 换成你自己的路径 —— 不要照抄别人机器上的绝对路径。

**方式三：自己打 vsix**

```bat
cd <仓库根>
python build.py                                :: 编译出 output\narnat.exe（约 2.5 小时）
copy output\narnat.exe vscode-extension\bin\    :: 打包脚本从这里取二进制
node vscode-extension\scripts\package-vsix.js
```

`output\narnat.exe` 不存在时打包会直接失败（提示先跑 build.py），这是有意的。

## 布局

默认自动打开，形态取决于 VSCode 版本：

```
VSCode ≥ 1.106                     VSCode 1.90 ~ 1.105
[ 活动栏 ][ 代码 ][ Narnat 侧边栏 ]   [ 活动栏 ][ 代码 ][ Narnat 面板 ]
```

- **左侧活动栏有 Narnat 图标**，点它即在右侧唤起面板，随后左栏自动切回资源管理器；
- 1.106+ 用「辅助侧边栏」（和代码并排、不占编辑器标签）；更低版本退化为编辑器面板（`ViewColumn.Beside`），会占一个标签页；
- 中间代码和右侧面板可并排看，分隔条拖动调宽，VSCode 会记住；
- 关掉面板后，点左侧图标、编辑器标题栏的 Narnat 按钮，或执行命令都能重新打开；
- 重载窗口后 VSCode 自动恢复上次的位置。

不想开机自动打开：设置里搜 `Narnat: Auto Open` 关掉，之后点左侧图标唤起。

> 默认侧边栏可用宽度约 **249px**（视口 289 减去 IDE 两侧内边距）。界面按这个宽度做了分级让位，
> 详见下面「界面构成 · 状态行」。

## 架构

```
VSCode 扩展 (Node)                    narnat.exe / python main.py --rpc
      │                                            │
      ├── spawn（activate 时一次，常驻）─────────────┤
      ├──── stdin: JSON-RPC 请求/通知 ─────────────→ │
      │←─── stdout: 响应 + 事件通知 ────────────────┤
      │←─── stderr: 日志（不解析）───────────────────┤
```

协议契约：`../docs/vscode-rpc-protocol.md`（**14 个请求方法 + 17 类通知**，以后端 `rpc/server.py` 的
`self._methods` 与 `isForwardedEvent` 为准）。
后端常驻，`/cd` 切会话、`/mode` 切模型都保持进程内状态；扩展关闭时发 `shutdown` 优雅退出。

## 命令

| 命令 | 作用 |
|---|---|
| `Narnat: 打开对话` | 手动打开（已在右侧时只聚焦，不移动） |
| `Narnat: 切换调度面板` | 开关面板内的会话抽屉 |
| `Narnat: 打开配置文件（填 API Key）` | 用编辑器打开 narnat.json |
| `Narnat: 重启后端（改完配置后用）` | 重启后端进程（配置只在启动时读一次，改完必须重启） |

设置项：`narnat.autoOpen`（默认开）、`narnat.rpc.executable`、`narnat.rpc.args`、`narnat.rpc.timeout`。

## 界面构成

- **顶栏**：会话名（点开会话抽屉）
- **消息流**：用户消息 / 思考块（可折叠，含耗时与字数）/ 计划清单 `✓●○` /
  工具卡片（读取、搜索文件、搜索内容、编辑 diff、写入、执行命令、终端、串口、
  联网搜索、更新计划、MCP、声明完成）/ 系统事件行（压缩、续跑、复核、打断、待确认）
- **会话抽屉**：一列平铺（不分组），当前会话高亮；超过 6 条折叠成「展开其余 N 条」；
  悬停显示「改名 / 删除」（点整行即进入），删除有行内二次确认；子会话不进列表（`/ls --all` 可查）
- **输入区**：输入 `/` 弹命令面板；发送 ↔ 停止；会话 / 目录 / 目标模式 chips（目标 chip 可点开目标面板）
- **状态行**（输入框下方）：模型、思考强度、目标模式、插件数、上下文、缓存、费用、余额
  - 宽面板：8 项全显示
  - ≤620px：让掉缓存、余额（纯参考值）
  - ≤400px：让掉目标模式、插件按钮（`/goal`、`/plugin` 命令仍可用）
  - ≤340px（**默认侧边栏就会命中**）：只再省掉「费用」二字，金额本身始终保留
  - **「费用」任何宽度下都不会被隐藏** —— 它是用户最关心的数字

颜色全部读 VSCode 主题变量（`--vscode-editor-background` 等），换主题自动跟随。

输入框里可直接打 `/ls` `/cd` `/rm` `/save` `/explore` `/done` `/mode` `/thinking`
`/thinkback` `/goal` `/plugin` `/compact` `/skill`，输出按「先 `❯ /命令`、下面缩进给结果」排版。

## 开发方式

**改界面**（唯一真源，浏览器里直接改直接看）：

```
1. 编辑 design/ui-full.html        ← 唯一允许手写界面代码的地方
2. node scripts/sync-ui.js         生成 media/narnat.{css,js,body.js}
3. 双击 preview.html               看模拟态（无后端）
4. 双击 design/replay.html         看真实事件回放（离线，用录好的数据）
5. 重载 VSCode 窗口                确认宿主内效果
```

> **不要直接编辑 `media/` 下的文件**——它们是生成物，下次 sync 会被覆盖。
> 界面代码（含 RPC 事件渲染）必须写在 `design/ui-full.html` 里。
> `sync-ui.js` 若发现 design 里缺少 RPC 处理会打警告并返回码 2。

**接真后端**：

```bat
:: 开发态：扩展会自动发现仓库根有 main.py，用 python main.py --rpc 起后端
code --extensionDevelopmentPath=<仓库根>\vscode-extension --new-window
```

查找顺序：用户配置的 `narnat.rpc.executable` → 工作区 `main.py` → 仓库根 `main.py`
→ 扩展 `bin/narnat.exe` → PATH 里的 `narnat.exe`。

**跑测试**：

```bat
npm run check                        :: 语法检查，毫秒级
node test/frame.test.js              :: NDJSON 分帧（7 项）
node test/rpc.test.js                :: RPC 客户端对假服务端（8 项）
node test/e2e-protocol.js            :: 协议 12 个请求方法全覆盖（26 项）
node test/e2e-real.js                :: 真实后端对话+工具+取消（12 项）
node test/e2e-real.js --chat         :: 同上 + 真实调模型（会花钱）
node test/e2e-cancel.js              :: 取消语义专项（7 项）
node test/e2e-events.js              :: todo/update / bg/submit / diff（真实触发）
node test/e2e-thinking.js            :: thinking/* 三工况矩阵（真实模型）
node test/e2e-verify.js              :: verify/* 目标模式实质复核（成本较高）
node test/e2e-errors.js              :: 协议错误路径 + busy 并发语义（17 项）
node test/runTest.js index           :: VSCode 宿主：侧边栏布局（13 项）
node test/runTest.js rpc             :: VSCode 宿主：假后端渲染（9 项）
node test/runTest.js real            :: VSCode 宿主：连真实后端（6 项）
node test/runTest.js chat            :: VSCode 宿主 + 真实模型 + 真实聊天（4 项）
node test/runTest.js reload          :: 侧边栏形态与视图重建（4 项）
node test/runTest.js busy            :: busy 并发时界面不卡死（4 项，用假后端）
node test/runTest.js setup           :: 首次使用引导：打开配置/401 指引（6 项）
node test/runTest.js ui              :: UI 缺陷 DOM 级回归（52 项）
node test/runTest.js ux              :: 会话/配置/注入/超长等真实体验（16 项）
node test/e2e-errors.js              :: 协议错误路径 + busy 并发语义（17 项）
node test/e2e-paths.js               :: 未知命令兜底 + 模型热切换（9 项）
node test/e2e-nokey.js               :: 空 API Key 时的行为（6 项）
node test/e2e-exe.js                 :: 编译产物 narnat.exe 端到端（10 项，需先 build）
node test/runBundled.js              :: 解压 vsix 后用自带 exe 启动（4 项，验打包产物）
node test/runInstalled.js            :: 用已安装到 ~/.vscode/extensions 的扩展启动（4 项）
node test/runInstalled.js setup      :: 同上但跑首次引导（6 项，验全新安装无 .narnat 的自动生成）
node test/record.js                  :: 录一段真实事件，供 replay.html 回放
```

> **跑法建议**：`index / rpc / reload / real / chat / busy` 会读写同一个测试工作区的
> `narnat.rpc.*` 配置，**请串行跑**。并行跑会互相覆盖配置（实测踩过：busy 套件的假后端
> 路径残留，导致 real 套件连到 `fake-model`）。各套件末尾已自行还原配置。

**所有 e2e 脚本自动隔离**：`test/isolate.js` 为每次运行创建临时 `NARNAT_HOME`
与独立工作目录，测试产生的会话、日志、文件都不落进你自己的工作区。

**打包成 vsix**（内嵌 exe，用户装插件即可用）：

```bat
cd ..
python build.py                            :: 先编译，产出 output/narnat.exe（含 --rpc）
cd vscode-extension
node scripts/package-vsix.js               :: 复制 exe 到 bin/ 并打 vsix
node scripts/package-vsix.js --no-package   :: 只校验扩展结构，不打 vsix（--skip-exe 仍然会打包）
```

`package-vsix.js` 会校验：exe 能 `-v` 且 `--help` 含 `--rpc`、扩展文件齐全、
`.vscodeignore` 已排除 test/design/scripts、vsix 内确实含 `extension/bin/narnat.exe`。

实测产物：`output/narnat.exe` 93.0MB（Nuitka onefile + LTO，编译约 2.5 小时）；
`narnat-vscode.vsix` 92.9MB / 14 个条目，解压后 `extension/bin/narnat.exe` 存在，
用它启动时后端**自动走 bundled 分支**（不依赖用户的 python/PATH）。

> 编译慢主要是 `build.py` 里的 `--lto=yes`（链接阶段单步 55 分钟）。
> 若只为本地联调，可以先不打包，直接用 `python main.py --rpc`（扩展会自动发现）。

## 已验证范围

| 类别 | 覆盖 | 证据 |
|---|---|---|
| 协议 | 14 个请求方法 + 17 类通知，契约脚本 `CONTRACT_ALL_COVERED` | `docs/vscode-rpc-protocol.md` §7 有逐事件对照表 |
| 界面形态 | 对话停靠辅助侧边栏（同 Copilot Edits），不占编辑器标签 | `runTest.js index` 13 项（含 `surface=secondary`） |
| 错误路径 | busy 并发、参数校验、业务错误、未知方法、错误风暴后健康 | `e2e-errors.js` 17 项 |
| 真实用户路径 | 未知命令兜底、模型热切换、空 API Key、新建对话 | `e2e-paths.js` 9 + `e2e-nokey.js` 6 + `e2e-protocol.js` |
| UI 缺陷回归 | 抽屉展开、表格渲染、命令名、思考折叠、输入框自适应、新建对话 | `ui.js` 52 项（真 DOM，非快照） |
| 交互体验 | 会话进入/删除、配置下拉、注入安全、超长正文、半截表格 | `ux.js` 16 项 |
| 后端 | 握手/会话/命令/取消/并发/优雅退出/看门狗/确认应答 | `e2e-protocol.js` 28 项 + `verify-watchdog2.py` + `verify-confirm.py` + `verify-session-new.py` |
| 前端 | 分帧、请求配对、超时、崩溃、离线降级、busy 不卡死 | `frame.test.js` 7 + `rpc.test.js` 8 + 宿主 `busy.js` 4 |
| 宿主 | 侧边栏形态、真实聊天渲染、真实后端、视图重建 | `runTest.js` index/rpc/real/chat/reload 共 38 项 |
| 首次使用 | 打开配置文件命令、401 时可操作指引 | `setup.js` 6 项（源码 & 已安装两种环境各跑一遍） |
| 产物 | 编译 exe、打包 vsix、自带 exe、**真实安装后** | `e2e-exe.js` 10 + `runBundled.js` 4 + `runInstalled.js` 4+6 |

**界面形态说明**：对话面板是**辅助侧边栏视图**（`viewsContainers.secondarySidebar`），
和 Copilot Edits 同一位置、同一「文件目录」形态——不占编辑器标签、不挤走代码。
该容器需要 **VSCode ≥ 1.106**；更低的宿主会自动降级为编辑器区面板（形态差但可用），
并在日志里注明。

**已实测的产物与安装链路**：
```
python build.py                          → output/narnat.exe 93.0MB（Nuitka onefile + LTO）
copy output\narnat.exe vscode-extension\bin\   ← 打包脚本从这里取二进制
node scripts/package-vsix.js             → narnat-vscode.vsix 93.0MB / 14 条目（含 bin/narnat.exe）
code --install-extension ...vsix --force → 装到 ~/.vscode/extensions/narnat.narnat-vscode-0.0.1
node test/runInstalled.js                → kind=bundled，用的是安装目录内的 exe，version=16.8.0
node test/runInstalled.js setup          → 全新安装（bin 下无 .narnat）→ 自动生成 → 打开配置成功
```

> `output/` 里不一定有 `narnat.exe`（只有跑过 `build.py` 才有，约 2.5 小时）。
> 没有它时 `package-vsix.js` 会直接失败并提示先编译，这是有意的——不会静默打出缺二进制的包。

> `~/.narnat/` 不在 vsix 里（也不会放在扩展目录：升级 vsix 时 VSCode 会删掉整个
> 扩展目录重建，数据会跟着没）。用户首次使用时后端启动会自动创建它，
> `Narnat: 打开配置文件（填 API Key）` 会等这一两秒再打开，所以不需要预先存在。

**需用户手动验证的两项**（自动化测试无法覆盖）：
1. 真实「Developer: Reload Window」后 Narnat 面板是否自动回到原位（serializer 的运行时行为）
2. 面板的视觉观感与交互手感

**视觉验收**（AI 可自助）：

```bat
mkdir shots                                        :: see.py 不会自己建目录
python ..\docs\ui-probe\see.py shots\xxx.png "评估对齐、密度、对比度、缺陷"
```


## 目录结构

```
vscode-extension/
├── package.json            扩展清单（命令 / 配置 / 菜单 / 激活事件）
├── src/
│   ├── extension.js        面板开在最右列、重载恢复、自动打开
│   └── webview.js          getHtml()：注入 nonce、asWebviewUri、CSP
├── media/
│   ├── narnat.css          全部样式（VSCode 主题变量 + 深色回退）
│   ├── narnat.js           全部交互与模拟数据
│   ├── narnat.body.js      界面骨架（由 design/ui-full.html 生成）
│   └── icon.svg            面板图标
├── scripts/sync-ui.js      从设计真源生成 media/ 三件套
├── design/ui-full.html     设计真源
├── preview.html            浏览器入口
├── test/
│   ├── runTest.js          宿主测试启动器（@vscode/test-electron）
│   ├── suite/index.js      13 项宿主内集成断言
│   └── fixtures/workspace  测试工作区（自动生成 sample.js）
└── README.md
```

## 首次使用

1. 装好扩展后，命令面板运行 **`Narnat: 打开配置文件（填 API Key）`**
2. 在打开的 `narnat.json` 里填写 `智能体 → 接口密钥`，保存
3. 命令面板运行 **`Narnat: 重启后端（改完配置后用）`**，然后即可对话

> 配置只在后端进程**启动时**读一次。改完文件不重启，填了密钥也不会生效。

配置文件位置随后端来源而变，扩展会自动定位并打开：

| 后端来源 | 配置文件位置 |
|---|---|
| 打包 exe 装进扩展目录（默认） | `~/.narnat/config/narnat.json`（数据不放在扩展目录：升级 vsix 会整个删掉重建） |
| 工作区源码 | `<工作区>/.narnat/config/narnat.json` |
| 自定义 `narnat.rpc.executable` | 脚本所在目录（`rpc.args` 里的 `.py`）或该可执行文件旁边 |

环境变量 `NARNAT_HOME` 可强制指定数据目录（优先级最高）。

没填密钥直接发消息也能看到原因：后端会把 401 当正文流出来，webview 在下方补一行
「需要配置 API Key」并指明用哪个命令。

## 已知限制

- 用户消息在**回放页**里不显示（协议没有 `user/message` 事件，真实使用时 webview 在发送时本地立即渲染；
  回放只有后端事件，缺这一侧）。真实 VSCode 环境不受影响。
- `thinking/*` 需要两个前提同时满足才会出现：`智能体.思考.回传` 为 true（默认已是 true），
  且当前模型确实产出思考块（实测 `deepseek-v4-pro` 有，`deepseek-flash` 没有）。
  没有 thinking 事件属正常，不是缺陷。
- `verify/result` 只在目标模式下、且 AI 调 `GoalComplete` 后才触发。
- `compress/end` 的 `ok/before/after` 恒为 null（上游 `begin/end_compressing()` 无参数），
  只有 `durationMs` 可信。
- `bg/submit` 靠正则解析 Shell 工具输出文本识别，上游文案变化时可能不再下发。
- 后端有父进程看门狗（每 5s 检查），扩展宿主被强杀时不会留下孤儿进程；
  但极端情况下（看门狗判断不出父进程状态）可能多存活一个周期。
- 需要 VSCode **1.90 及以上**（`engines`）。其中**辅助侧边栏形态要 1.106+**；
  1.90~1.105 会退化成编辑器面板（占一个标签页），功能不变。
- 源码模式还需要本机 Python 3.10+ 且装好后端依赖，否则面板会一直停在「离线演示态」。
- 默认侧边栏可用宽度约 249px，界面按这个宽度做了分级让位（见「界面构成 · 状态行」）。
- 宿主测试首次运行会下载 VSCode（约 335MB）到 `.vscode-test/`，已加入 .gitignore。
- 未做 i18n，全中文。

