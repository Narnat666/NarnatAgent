# 开发文档

面向维护者。用户安装与使用见 `README.md`，RPC 协议见 `../docs/vscode-rpc-protocol.md`。

## 源码开发模式

前置条件：Node.js 18 及以上、Python 3.10 及以上，且已安装后端依赖。

```bat
cd <仓库根>
python main.py --version
code --extensionDevelopmentPath=<仓库根>\vscode-extension --new-window
```

## 打包 vsix

```bat
cd <仓库根>
python build.py                            :: 产出 output\narnat.exe，约 2.5 小时
node vscode-extension\scripts\package-vsix.js
```

`package-vsix.js` 将 `output/narnat.exe` 复制进 `bin/` 后打包，并校验三项：exe 可执行、支持 `--rpc`、RPC 方法集与源码一致。三项均为关键校验，任一失败即中止，不覆盖已存在的 `bin/narnat.exe` 与 vsix。

可用参数：

| 参数 | 作用 |
|---|---|
| `--exe <路径>` | 使用指定 exe，默认 `output/narnat.exe` |
| `--skip-exe` | 不打包二进制，仍生成 vsix |
| `--no-package` | 仅校验结构，不生成 vsix |

`--skip-exe` 与 `--no-package` 相互独立。两者曾合并为单一开关，导致实际未打包却报成功，用户安装到的仍是旧 vsix。

产物：`output/narnat.exe` 39MB（Nuitka onefile + LTO），`narnat-vscode.vsix` 39MB，16 个条目，平台声明为 `win32-x64`。

## 后端查找与配置

后端查找顺序：`narnat.rpc.executable` → 工作区 `main.py` → 仓库根 `main.py` → 扩展 `bin/narnat.exe` → PATH。

配置文件位置随后端来源变化：

| 后端来源 | 位置 |
|---|---|
| 打包 exe 装入扩展目录（默认） | `~/.narnat/config/narnat.json` |
| 工作区源码 | `<工作区>/.narnat/config/narnat.json` |
| 自定义 `narnat.rpc.executable` | 该可执行文件同目录 |

数据目录不放在扩展目录下：升级 vsix 会重建整个扩展目录。`NARNAT_HOME` 可强制指定数据目录，优先级最高。

## 修改界面

界面唯一真源为 `design/ui-full.html`。

```bat
node scripts/sync-ui.js      :: 生成 media/narnat.{css,js,body.js}
```

`media/` 下的文件是生成物，直接修改会在下次 sync 时被覆盖。

`sync-ui.js` 仅提取第一个 `<style>` 与第一个 `<script>`，源文件中出现第二个标签时，其后内容被静默丢弃。脚本内置结构自检：标签计数、`node --check` 语法校验、RPC 处理覆盖检查，任一不满足则非零退出。双 `<script>` 会生成语法非法的产物，安装后表现为空白面板，此类失败必须在构建期拦截。

浏览器预览（无后端，模拟数据）：打开扩展根目录的 `preview.html`。
真实事件回放：`design/replay.html`，事件数据在同目录 `replay-events.json`。

## 测试

```bat
npm run check                        :: sync-ui 加各源文件 node --check
```

验证脚本位于 `scripts/`，按前缀分三类：

| 前缀 | 用途 | 示例 |
|---|---|---|
| `verify-` | 断言式回归，失败时非零退出 | `verify-rpc-contract.js`、`verify-msg-files.js`、`verify-theme-ui.js`、`verify-code-tokens.js` |
| `shot-` | 输出截图供人工比对 | `shot-attach-align.js`、`shot-code.js`、`shot-math.js` |
| `diag-` | 定位具体现象根因，输出诊断数据 | `diag-code-theme.js`、`diag-color-mix.js` |

多数脚本以无头 Edge 加载由 `design/ui-full.html` 与 `media/narnat.js` 拼装的页面，注入模拟的 `acquireVsCodeApi` 与 theme 快照后读取 DOM，不需要启动后端。

依赖后端的脚本：

```bat
node scripts/verify-c.js             :: 扩展宿主侧行为
node scripts/verify-c-host.js        :: 宿主侧边栏布局
node scripts/install-vsix.js         :: 已安装扩展的启动验证
```

`tests/` 目录当前为空；`package.json` 的 `npm test` 指向已不存在的 `test/runTest.js`。历史套件（`e2e-*`、`runTest.js`、`isolate.js`）已移除，现行验证全部位于 `scripts/`。

## 目录结构

```
vscode-extension/
├── package.json          扩展清单（命令、配置、菜单、激活事件）
├── src/
│   ├── extension.js      面板位置、重载恢复、自动打开、RPC 分发
│   ├── rpc-client.js     JSON-RPC over stdio 客户端（NDJSON 分帧、超时、spawn 失败处理）
│   ├── webview.js        getHtml()：nonce、asWebviewUri、CSP
│   └── attach-store.js   附件落库与校验
├── media/                生成物：narnat.{css,js,body.js} 与 icon.{svg,png}
├── scripts/              sync-ui、package-vsix、verify-、shot-、diag-
├── design/               界面真源与预览页
│   ├── ui-full.html      唯一真源
│   ├── preview-code.html、ui-commands.html、ui-compare.html、demo-convo.html
│   └── replay.html、replay-events.json
├── preview.html          浏览器预览入口，不随扩展分发
├── bin/narnat.exe        打包时复制的后端可执行文件
└── tests/                当前为空
```

`.vscodeignore` 排除 `design/**`、`scripts/**`、`preview.html`、`**/.narnat/**`、`bin/narnat_runtime/**` 等路径，开发文件与用户数据均不随扩展分发。

## 协议已知限制

- `thinking/*` 需将 `智能体.思考.回传` 设为 true，且当前模型需产出思考块（`deepseek-flash` 不产出属正常现象）
- `verify/result` 仅在目标模式下 AI 调用 `GoalComplete` 后触发
- `compress/end` 的 `ok`、`before`、`after` 恒为 null，仅 `durationMs` 可信
- `bg/submit` 依赖正则解析 Shell 输出，上游文案变化可能导致失效
- 回放页不显示用户消息（协议无 `user/message` 事件），真实使用不受影响
- 未做 i18n，全中文

## 后端文案变更的联动点

修改后端（`../narnat_agent/tools/`）的 model-facing 文案时，本目录有两处消费者会失配：

1. `rpc/server.py::_tool_failed_from_output` 与 `media/narnat.js` 均依据 `[exit code: N]` 判定失败，两处都取**最后一个**退出码。多段命令每段各标注一次，整体结果由末段决定；改为取第一个会在「前段失败、后段以 `||` 兜回」时错标失败。
2. 退出码为 0 时不再输出该行，即缺少 `[exit code: 0]` 代表成功。前端「退出码 0 但输出含失败」的判定（用于 `pytest | more` 这类管道使退出码取自末端命令的情形）不能再要求该行存在，否则该警告永不触发。

`scripts/verify-rpc-contract.js` 将「前端所有 post 类型都能在宿主方法表中命中」固化为断言，修改协议时先执行该脚本。
