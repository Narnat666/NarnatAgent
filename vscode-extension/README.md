# Narnat VSCode 扩展

左侧图标一键唤起，面板停靠在右侧辅助侧边栏，代码始终可见。已接真实 Python 后端（JSON-RPC over stdio），后端不在时降级为离线演示态。

## 安装

**装 vsix（最快）**

```bat
code --install-extension narnat-vscode.vsix
```

或 VSCode 里 `扩展` → 右上 `…` → `从 VSIX 安装…`。装完重载窗口。
扩展自带 `bin/narnat.exe`，不需要本机装 Python。

**源码开发模式**

前置条件：Node.js 18+、Python 3.10+ 且装好后端依赖。

```bat
cd <仓库根>
python main.py --version
code --extensionDevelopmentPath=<仓库根>\vscode-extension --new-window
```

**自己打 vsix**

```bat
cd <仓库根>
python build.py                            :: 产出 output\narnat.exe（约 2.5 小时）
node vscode-extension\scripts\package-vsix.js
```

## 首次使用

1. 命令面板运行 `Narnat: 打开配置文件（填 API Key）`
2. 填 `智能体 → 接口密钥`，保存
3. 命令面板运行 `Narnat: 重启后端（改完配置后用）`

> 配置只在后端**启动时**读一次。不重启，填了密钥也不生效。

配置文件位置：

| 后端来源 | 位置 |
|---|---|
| 打包 exe 装进扩展目录（默认） | `~/.narnat/config/narnat.json` |
| 工作区源码 | `<工作区>/.narnat/config/narnat.json` |
| 自定义 `narnat.rpc.executable` | 该可执行文件旁 |

数据不放在扩展目录——升级 vsix 会整个删掉重建。`NARNAT_HOME` 可强制指定，优先级最高。

后端查找顺序：`narnat.rpc.executable` → 工作区 `main.py` → 仓库根 `main.py` → 扩展 `bin/narnat.exe` → PATH。

## 布局与界面

```
VSCode ≥ 1.106                    VSCode 1.90 ~ 1.105
[ 活动栏 ][ 代码 ][ Narnat 侧边栏 ]  [ 活动栏 ][ 代码 ][ Narnat 面板 ]
```

1.106+ 用辅助侧边栏（与 Copilot Edits 同位，不占编辑器标签）；更低版本退化为编辑器面板，功能不变。点左侧图标唤起，关掉后可再点图标或命令打开。

- **消息流**：用户消息 / 思考块 / 计划清单 / 工具卡片 / 系统事件行
- **会话抽屉**：一列平铺，超 6 条折叠，悬停可改名删除，删除有二次确认
- **输入区**：输入 `/` 弹命令面板，会话 / 目录 / 目标模式 chips
- **状态行**：模型、思考强度、目标模式、插件数、上下文、缓存、费用、余额；窄面板分级让位，**费用任何宽度都不隐藏**

颜色全部读 VSCode 主题变量，换主题自动跟随。可用命令：`/ls` `/cd` `/rm` `/save` `/explore` `/done` `/mode` `/thinking` `/thinkback` `/goal` `/plugin` `/compact` `/skill`。

不想开机自动打开：设置里搜 `Narnat: Auto Open` 关掉。

## 命令

| 命令 | 作用 |
|---|---|
| `Narnat: 打开对话` | 手动打开（已在右侧时只聚焦） |
| `Narnat: 切换调度面板` | 开关会话抽屉 |
| `Narnat: 打开配置文件（填 API Key）` | 打开 narnat.json |
| `Narnat: 重启后端（改完配置后用）` | 重启后端进程 |

设置项：`narnat.autoOpen`、`narnat.rpc.executable`、`narnat.rpc.args`、`narnat.rpc.timeout`。

## 开发

**改界面**——唯一真源是 `design/ui-full.html`：

```bat
node scripts/sync-ui.js      :: 生成 media/narnat.{css,js,body.js}
```

> 不要直接改 `media/` 下的文件，它们是生成物，下次 sync 会被覆盖。

`sync-ui.js` 发现 design 里缺 RPC 处理会告警并返回码 2。双击 `preview.html` 可在浏览器看模拟态（无后端），`design/replay.html` 看真实事件回放。

**跑测试**

```bat
npm run check                        :: 语法检查
node test/e2e-protocol.js            :: 协议全覆盖
node test/e2e-real.js                :: 真实后端对话+工具+取消（加 --chat 会调真模型、花钱）
node test/runTest.js ui              :: UI DOM 级回归（52 项）
node test/runTest.js index           :: 宿主内侧边栏布局
node test/runInstalled.js            :: 已安装扩展启动验证
```

还有 `frame/rpc/e2e-cancel/e2e-errors/e2e-paths/e2e-nokey/e2e-exe` 等，各套件末尾自行还原配置。`test/isolate.js` 为每次运行创建临时 `NARNAT_HOME`，测试数据不落进你的工作区。

> `index / rpc / reload / real / chat / busy` 共用同一测试工作区配置，**必须串行跑**。

**打包校验**：`package-vsix.js` 会验证 exe 可执行、支持 `--rpc`、RPC 方法集与源码一致。任一关键项失败即中止，不覆盖已有的 `bin/narnat.exe` 和 vsix。

实测产物：`output/narnat.exe` 39MB（Nuitka onefile + LTO），`narnat-vscode.vsix` 39MB / 16 个条目。

## 目录结构

```
vscode-extension/
├── package.json          扩展清单（命令 / 配置 / 菜单 / 激活事件）
├── src/
│   ├── extension.js      面板开在最右列、重载恢复、自动打开
│   └── webview.js        getHtml()：nonce、asWebviewUri、CSP
├── media/                生成物：narnat.{css,js,body.js} + icon.{svg,png}
├── scripts/sync-ui.js    从设计真源生成 media/
├── design/ui-full.html   界面唯一真源
└── test/                 测试套件
```

## 已知限制

- 需要 VSCode **1.90+**；辅助侧边栏形态要 **1.106+**，更低版本退化为编辑器面板
- 源码模式还需本机 Python 3.10+ 及后端依赖，否则面板停在「离线演示态」
- `thinking/*` 需配置 `智能体.思考.回传` 为 true 且当前离线模型产出思考块（`deepseek-flash` 不产出属正常）
- `verify/result` 仅在目标模式下 AI 调 `GoalComplete` 后触发
- `compress/end` 的 `ok/before/after` 恒为 null，只有 `durationMs` 可信
- `bg/submit` 靠正则解析 Shell 输出识别，上游文案变化可能失效
- 回放页不显示用户消息（协议无 `user/message` 事件），真实使用不受影响
- 宿主测试首次运行会下载 VSCode 到 `.vscode-test/`（约 335MB）
- 未做 i18n，全中文
