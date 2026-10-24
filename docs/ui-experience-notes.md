# 界面与配色改造 · 经验总结（供上下文压缩后接续）

> 写于 2026-10-24。本文件是**压缩替代品**：上下文被压掉后，读这一份就能继续干活。
> 只记录「事实 + 踩过的坑 + 验证方式」，不复述对话过程。

---

## 一、项目地形（改之前先看这里）

仓库根：`E:\桌面\narnat\NarnatAgent`

```
main.py                      v16.8.0，-p/--rpc/-g 三个入口
narnat_agent/
  rpc/server.py              RPC 方法表 + 全部回执（~2750 行，改后端基本都在这里）
  core/agent_loop.py         LLM↔工具内循环
  core/llm.py                消息展开（图片 → image block 在这里）
  core/message_list.py       消息追加（append_assistant / append_user_images）
  core/attachment.py         后端附件库：内容寻址、**无扩展名**、归一化字节
  core/input_history.py      ↑ 历史；_ATTACH_MARK 在这里
  ui/theme.py                ★新增：narnat.json「界面」→ 语义色快照
  ui/renderer.py             终端渲染器；RenderConfig.LANG_COLORS 是语言→配色分组的唯一表
  output.py                  ★配色单一定义源：_BASE_DEFS / _DERIVED_DEFS
  config/loader.py           normalize_ui_raw（中文键→英文键，唯一实现）
vscode-extension/
  design/ui-full.html        ★★界面真源（~12800 行，CSS + JS 全内联）
  media/narnat.{js,css,body.js}   生成物，**只能**由 scripts/sync-ui.js 生成
  src/extension.js           宿主（~2700 行，webview↔RPC 转发、附件、配置监视）
  src/attach-store.js        ★新增：用户附图落盘（纯函数、可单测）
  src/rpc-client.js          NDJSON 分帧 + 请求/通知
  scripts/                   sync-ui / package-vsix / install-vsix / verify-* / shot-*
  shots/                     目视核对 PNG 落地处
.narnat/config/narnat.json   运行配置（配色在「界面」段）
.narnat/data/sessions/       会话 json
```

**D:\deepseek-harness 是 dsh 的源码，只读参照**。凡「dsh 怎么做的」都要去翻那边源码，
不要凭印象说。已确认过的关键位置：
- `packages/client/ui-primitives/src/` — DisclosureRow / CodeToolbar / ReadBlock /
  TerminalBlock / DiffBlock / SearchBlock / CodeCard / MessageItem 的 css
- `packages/client/ui-chat/src/client/conversation-nodes/turn-process.ts`、
  `chat/TurnProcessNodeView.tsx` — 折叠行口径
- `packages/client/ui-attachment/src/MessageImage.tsx` — 图片尺寸与失败态
- `packages/attachment/attachment-local/src/store.ts` — 附件库（DSH_HOME/attachments/v1）
- `packages/llm/llm-deepseek/src/serialize.ts` — 图片直转 image block
- `packages/api/session-controller/src/history.ts` — 历史分页（DEFAULT_MAX_MESSAGES=50）

---

## 二、本轮做完的事（每条都能被 verify 脚本复验）

| # | 问题 | 落点 |
|---|---|---|
| 1 | 折叠行「失败 · 950 次工具调用」被读成「950 次全失败」 | `tpCountLabel` 改「N 次工具调用（其中 M 次失败）」；整轮 error 才前置「失败 ·」 |
| 2 | 历史只有工具、没有思考 | `_history_payload` 保留 `content=None + thinking=…` 的中间步（text 给空串） |
| 3 | 历史里凭空多一条「（图片内容见附件）」用户气泡，还把同一轮劈成两组 | `_history_payload` 跳过带 `image_for_tool_call` 的 user 消息 |
| 4 | 历史一次全量加载 | `_history_page`（尾页 + `historyHasMore/Before/Total`）+ `session/history` + 顶部「加载更早」；每页 `limit=50` 正文、**硬上限 300 条目**（允许劈轮，否则单 user 超长会话一页=全量） |
| 5 | 每个工具行挂两个复制按钮 | `IO_COPY_TOOLS` 收窄到 Read/Grep/Glob/Shell/Terminal/Edit/Write（dsh 只有这四类卡片带复制控件） |
| 6 | 视觉「一片灰」 | `▶` 字形换 1px 描边 SVG chevron；`·` 字符换 2px 圆点；行 hover 淡底 6%；折叠行 32px；`--fg-dim` 给主标题 |
| 7 | 行首全是 `>` | 七类语义图标（read/search/run/edit/think/web/plan/other）+ hover 换 chevron（dsh DisclosureRow 的 iconIdle↔chevronHover） |
| 8 | 插件不跟 narnat.json 配色 | 后端 `ui/theme.py` 快照（43 角色 + 11 基础 + 7 语言组）随 `initialize` 下发；`theme/get` 现读磁盘；前端只改亮度保色相饱和度做对比度适配 |
| 9 | 路径块显示在气泡里 / 图太小 | `splitAttachBlock` 在渲染层剥块；图片 240px、圆角 18px、多图 96 方块、lightbox、失败态可点重试 |
| 10 | 用户附图随原文件消失 | `src/attach-store.js`：复制进 `<项目>/.narnat/data/attachments_user/<sha16>.<ext>`，内容寻址+保留扩展名 |
| 11 | 图片只露出约 1/5 | `.msg .attach` 继承输入区 `max-height:5.5em`(≈71px)；改 `max-height:none` |
| 12 | 「3 秒」跑到行中间 | `.tp-caret` 与 `.tp-meta` **都用 `margin-left:auto`**，两个 auto 平分空间；只留 `.tp-meta` |
| 13 | 表格比 dsh 重 | 下边线 0.5px、表头字重 500、无底色；颜色仍走配置 |
| 14 | AI 输出的 markdown 文档碎成三块（json 正文漏成裸文本） | `splitFences()` 重写围栏解析：闭合必须「行首 + 纯反引号 + 长度 ≥ 开围栏」，再加嵌套栈容错 3 包 3 |
| 15 | 历史思考块没时间（只有「思考 · 303 字」） | **决定不改**：与 dsh 一致，思考耗时不落盘。界面流式那份耗时由 `rpc/ui.py` 的 `_thinking_t0` 自己计时、只发 `thinking/end` 事件；历史里只显示字数（`addHistoryThink`）。曾加过 `thinking_ms` 字段（agent_loop 计时 → message_list 落盘 → history 透传 `thinkingMs`），已按用户要求全部回退 |
| 16 | 代码块底色「太老土」 | 旧值取 editorWidget（比面板**暗**一档）+ 1px 灰全框 + 标题栏同色 = 老式文本框。改成：无配置色时从 `--bg` 提亮 8% 派生、标题栏再提亮一档（`--bg-code-hd`）、外框透明。依据 dsh（底 #151517→块 #1B1B1C→头 #2C2C2E，全中性灰、无边框）/ Tokyo Night / Catppuccin 都是「块比页面底亮一档」 |
| 17 | 浅色主题下三处对比度缺陷（换白主题才暴露） | ①行内代码复用 `--bg-code` 当底 → 文字 3.28:1；改用 `--bg-code-inline`（只混 3% 前景色）→ 4.50:1。②`dim` 修饰往底色混 22%，浅底上越混越亮 → 「引用」2.31:1；弱化后再压一次对比度下限（`adaptColor(hex, bg, min)`）→ 3.09:1。③`adaptSurface` 亮侧下限 0.82 让卡片变中灰 `#d3d7dd`；提到 0.96 → `#f5f6f8` |
| 18 | 后台任务提交行裸在折叠组外（`⚙ 后台任务 bg1 已提交 …log`） | `bgSubmit` 建的普通 `.sys` 没有 `data-turn-tool/think/text` 任何一个标记，`foldTurnProcess` 永远不收它。给这行打 `data-turn-proc="1"`，并把该标记加进收组判据 —— 用**显式声明**而不是「所有 .sys 都收」：压缩中/重试那些是**状态**，折起来等于把状态藏了 |
| 19 | `/skill` 补不出技能、`/ls` 补不出 `--all`、`/rm` 没有 `--all` | 根因是插件里 `ARG_SOURCE` 这张「哪些命令的参数可补」的清单只登记了 5 个命令（没登记 = 敲空格后面板直接关）。补登 `/skill`、`/ls`，并让 `/rm` 也带 `--all`。技能候选靠**新增后端 RPC `skill/list`**（`list_skill_tree` 一直存在、注释都写着「供 /skill Tab 补全」，但从来没有下发通道）；不吃 initialize 是为了不拖慢首屏（它要扫工作目录下所有 skills 目录）。`/plugin` 本来就是好的（实测有 4 个候选），未改 |

---

## 三、踩过的坑（最值钱的部分，别再踩）

### 界面/构建
1. **手改 `media/*` 白改** —— 那是 `sync-ui.js` 生成物，下次同步就覆盖。永远改 `design/ui-full.html`。
2. **新增 `src/*.js` 要改三处**：`package.json` 的 `check`、`scripts/package-vsix.js` 的 `need`、
   `scripts/install-vsix.js` 的 `MUST_MATCH`。漏了 = 打进包但没校验，或装完缺文件。
3. **webview 的 CSP 会丢掉内联 `style` 属性**（`style-src` 不含 unsafe-inline）。
   `element.style.foo = x`（CSSOM）有效；字符串拼 `style="…"` 无效。要么 CSSOM，要么写 `data-*` 交给 CSS 规则。

### headless 截图/断言（Edge `--headless=new`）
4. `--dump-dom` 和 `--screenshot` **同时给，dump 拿不到**。要分两次跑。
5. 把真实 payload 直插进 `<script>` 模板时，任意一条工具输出含脚本闭合标签的字面量就会提前闭合
   script 元素 → 后半段脚本静默不执行、页面空白、诊断恒报「(无)」。**用 base64 内联再 atob**。
6. `--virtual-time-budget` 不推进合成器时钟 → CSS transition 的中间值永远停在起点。
   要测旋转/动画的**终值**，先 `el.style.transition='none'`。
7. 折叠组收起后成员在 `.tp-body[hidden]` 里，`getBoundingClientRect()` 恒为 0；
   量尺寸用 `getComputedStyle`。

### CSS / 布局
8. **flex 里两个 `margin-left:auto` 会平分剩余空间**（不是都靠右）。耗时被推到行中间就是这个原因。
9. **样式复用要限作用域**：输入区 `.attach{max-height:5.5em}` 被消息气泡继承 → 图裁成 1/5。
   跨区域复用一律加父级前缀（`.msg .attach{…}`）。
10. `text-overflow:ellipsis` 默认砍尾留头；路径类要保尾部得 `direction:rtl`，
    但**不能同时写** `unicode-bidi:plaintext`（会抵消）。

### 前后端契约
11. 前后端共享的**字符串标记**（如 `_ATTACH_MARK`）必须一字不差，且要有断言（verify-attach-ui 的 S1）。
    两处各写一份必然分叉，分叉后果是路径块漏回气泡。
12. `_history_payload` 的过滤口径是**三件事共用**（分页切点 / 思考保留 / 配图剔除），
    改一处要同时想另外两处。
13. 前端 `normCmp`（判断「这条历史画过没有」）必须和渲染口径一致：
    渲染剥了附件块，比对前也得剥，否则永远失配 → 每次握手整段重画 → 工具行/思考块被抹掉。
14. 分页后 `historyDrawn` 的语义必须保持「后端全量条数」，不能改成「已画几条」——
    那段判据带着 6 条实测踩坑注释（确认条要保住、流式正文要保住……），动它连锁崩。

### Markdown 围栏
15. **闭合围栏必须「整行只有反引号」**。原来那条正则的闭合分支写成 `\n```"`，
    不要求行尾 → 「带语言标签的行」（```json）也算闭合。AI 讲「Markdown 文档」时
    写 ```markdown 外层套 ```json 内层，外层就在 ```json 处提前收尾，json 正文漏成
    裸文本、后面再被切成另一个块（用户截图报障）。同理要校验两条：闭合长度 ≥ 开
    围栏长度（4 包 3 的合法嵌套）、开围栏缩进 ≤ 3 空格（4 空格是缩进代码块）。
16. **严格 CommonMark 修不了 AI 的 3 包 3**：外层会被 json 的闭合行结束，结果是
    「代码块 + 正文 + 末尾一个空的未闭合块」，仍然碎。所以额外加一层**嵌套栈**：
    块内遇到带语言标签的围栏行就当内层开围栏，它自己那层先闭合，栈空才结束外层。
    这一条是**有意偏离 dsh**（dsh 用 micromark+mdast 严格解析，会碎），记在这里
    避免后人当 bug 改回去。
17. 正则写不出「长度 ≥」（反向引用只能精确相等），这类判定必须逐行扫描。

### 配色层次
17b. **深色界面里，浮起来的块要比底「亮」一档，不是更暗。** 旧代码块取
    `--vscode-editorWidget-background`（One Dark #21252b）比编辑器底 #282c34 暗，
    看起来像挖下去的洞，再套 1px 灰框就成了老式文本框（用户原话「太老土」）。
    三套主流都是亮一档：dsh 底 #151517→块 #1B1B1C→头 #2C2C2E（纯中性灰、无边框）、
    Tokyo Night 底 #1a1b26→面板 #1f2335、Catppuccin Mocha 底 #1e1e2e→surface0 #313244。
    另两条同样重要：**标题栏要与块体分两档**（同色只剩一条线在分隔）、
    **不要全包围 1px 框**（靠底色差分层）。
    实现：`--bg-code` 从 `--bg` 混 8% 前景色派生（等价 #2c313a），
    `--bg-code-hd` 再混一档，`--code-border: transparent`。
    对比图脚本 `scripts/shot-code-bg.js` 一次出 4 个方案（深色）。
17c. **同一套派生式必须两种主题都成立**（这是浅色主题暴露问题的根因）。
    `color-mix(in srgb, var(--bg) 92%, var(--fg) 8%)` 这种「往前景色混」的写法
    天生对称：深底上变亮、浅底上变暗，方向自动对——所以代码块三档不用写两套。
    但**沿用手写分支的地方要逐条查**：
    · `adaptSurface` 的亮侧下限（0.82 → 0.96）——写死一个亮度值就得同时想两边
    · `dim` 修饰（往底色混 22%）在浅底上是「越混越亮」，弱化会变成失读
    · 行内代码的底必须自己一档，不能复用「面」的底（文字对比度会被吃掉）
    核对方式：`scripts/verify-light.js` 注入 VSCode Light Modern 的真实变量值，
    断言「块底与页面底的亮度差在 1%~12%」「正文级文字对它**实际所在底色**的
    对比度 ≥ 4.5」。注意 `getComputedStyle().backgroundColor` 对 color-mix
    返回的是 `color(srgb 0.77 0.79 0.82)` 而不是 `rgb()`，解析函数两种都要认
    —— 只认 rgb() 会把有底色的元素误判成透明，算出的对比度全是假的。

### 命令面板补全
20. **新增一条 RPC 要同时改四处**（漏一处 = 静默失败，没有任何报错）：
    ① 后端 `self._methods` 方法表；② 宿主 `src/extension.js` 的
    `MESSAGE_METHODS`（严格白名单，取不到就 return，请求直接消失）；
    ③ 同文件 `RECEIPT_TYPES`（不登记的话 `await` 立刻返回空，像后端没回）；
    ④ 成功/失败两个分支各要 `broadcast` 一次回执（失败不回 = 界面永远等待）。
    界面侧再 `case '<name>/result':` 接住。
21. **补全候选的清单是 `ARG_SOURCE`**：没登记 = 敲完命令名加空格后面板
    直接关（`refreshPalette` 里 `!src` 就 return），看起来像「这个命令没有参数」。
    加新命令的参数补全只需在 `ARG_SOURCE` 加一行 + `listArgCandidates` 加一个分支。
22. 参数分组名别在渲染处统一拼：`g: '可进入的' + d` 对会话候选是对的，
    对 `/ls --all` 就成了一句病句（「可进入的列出全部会话…」）。
    让候选自带 `g`，渲染处 `x.g || 默认`。

### 会话存储
18. **普通对话轮不触发自动保存**：`try_save()` 只在 `agent.py` 的目标模式分支里调，
    普通聊天靠「新建对话时的 carry_over」或手动 /done 落盘。想端到端验落盘别等
    auto_save，也别指望 `-p` headless（它不写 sessions）。
19. `session_store.load_session()` 返回 **(messages, err) 元组**，不是 dict。
20. **思考耗时不要落盘**（用户已决定，与 dsh 一致）。dsh 的会话记录里同样没有
    reasoning 时长，它只在流式期间用「已思考」占位。界面上流式那份耗时是
    `rpc/ui.py` 的 `_thinking_t0` 自己按事件算的、只发 `thinking/end` 事件；
    历史回放只显示字数。
    历史里要显示耗时就得在协议消息里加字段（`thinking_ms`），那会**污染会话
    json、且要在 `llm._prepare_messages` 里 pop 掉才不发给 API** —— 曾这么做过
    并已全部回退（后端 5 个文件 `git checkout`、前端去掉 `thinkLabel`/`thinkingMs`）。
    回归脚本 `verify-history-thinking` 的 K13/K14 现在反向锁住这个决定：
    历史标题必须只有字数、即便 payload 里塞了 `thinkingMs` 也不许显示。

---

## 四、验证方式（每次改完照跑）

```bat
cd /d E:\桌面\narnat\NarnatAgent\vscode-extension
node scripts/sync-ui.js
npm run check

:: 界面回归（17 个，串跑）
for %f in (verify-palette verify-bg-fold verify-light verify-fence verify-fail-count ^
  verify-fail-row verify-history-mode verify-history-thinking verify-history-page ^
  verify-interrupt verify-interrupt-order verify-tag-scope verify-tool-copy ^
  verify-visual verify-theme-ui verify-attach-ui verify-attach-store) do @node scripts\%f.js

:: 后端回归
cd /d E:\桌面\narnat\NarnatAgent
python nn-tasks\verify-history-thinking.py     :: _history_payload 口径
python nn-tasks\R32\verify-theme.py           :: 配色快照（含真实 narnat.json 交叉验证）
python nn-tasks\R29\verify-history-page.py    :: 分页（含真实 --rpc 到 E7）
node nn-tasks\verify-history-rpc.js           :: 真实 --rpc 端到端
node nn-tasks\verify-skill-list-rpc.js        :: skill/list 与磁盘技能一致
node nn-tasks\e2e-skill-palette.js            :: 真后端产出 → 界面候选（含宿主白名单）
node nn-tasks\R32\xcheck-theme-rpc.js         :: 真实 --rpc 配色

:: 装插件（后端走 main.py，不打包 exe）
node scripts\install-vsix.js --skip-exe
```

**判据风格**：全部可计算（数量等式、字段相等、hue 差、对比度、几何坐标）。
不断言「好看」「正确」。视觉类改动另跑 `scripts/shot-*.js` 出 PNG 人工过一眼。

**校验口径**：子代理/单测给的数据不采信，关键结论自己起真实 `--rpc` 复核一次。

---

## 五、两个「抄谁」的判定规则（用户已明确，别再问）

- **几何 / 底色 / 结构 → 抄 dsh**（它是前端界面，同类产品的最优解）：
  行高、圆角、边框粗细、字号层级、图标形态、hover 行为、折叠形态、表格骨架、图片尺寸。
- **文字配色 → 抄终端（narnat.json「界面」段）**：
  标题/粗体/行内代码/链接/引用/diff/状态色，一律取配置里的语义角色；
  面板底色、顶栏、输入框**仍跟 VSCode 主题**（不能变成 VSCode 里嵌的另一块应用）。

当两者冲突时（如表格：骨架抄 dsh、颜色取配置），按上面分工各取一半。

---

## 六、遗留 / 待裁决

| 项 | 状态 |
|---|---|
| 表格表头要不要加一档色 | 用户「后续再说」；dsh 本身是朴素无色的 |
| 用户发图改成 dsh 那样「发送时入后端附件库 + 消息带引用」 | 用户「后续讨论」；现状是路径进正文、AI 用 Read 读 |
| 图片库本身被删后的找回 | 与 dsh 一致：无自动找回，只有可点重试的失败态 |
| 字号基准（我们 13px / dsh 14px） | 未动，改动牵涉大量布局断言 |
| 整条消息的悬浮操作行（复制/赞/踩/重试/用量/用时） | 未做 |
| `ruff` | 仓库无配置文件、既有约 208 条告警；改动遵循原有 `%` 格式化风格即可 |
| AI 用 ```markdown 包整篇文档时要不要**剥掉外层围栏按正文渲染** | 未定。现在收成一个代码块（源码可复制、语义明确）；剥掉更接近「看渲染效果」，但会让「AI 教学 markdown 语法」的场景失真 |
| 老会话的思考耗时 | 不显示（决定：耗时不落盘，与 dsh 一致）。流式期间仍有「思考 3.2s」，历史回放只显示字数 |

---

## 七、当前插件形态速查

- 布局：`activitybar` 启动器 + `secondarySidebar` 对话面板（与 Copilot Edits 同形态）
- 折叠：一轮结束把工具/思考/中间正文收成一个 `.turn-proc`，默认收起，
  标题「N 次工具调用（其中 M 次失败）· K 条消息」，副标题「已读取文件并搜索代码」，
  右侧常显耗时 + chevron（收起朝右、展开朝下）
- 行首：语义图标常显，hover 换 chevron（16px 定宽槽，切换不跳）
- 图片：气泡内 240px 内容尺寸、点击 lightbox、失败态可点重试
- 配色：43 个语义角色 + 11 个基础角色 + 7 组语言色，全部来自 narnat.json，
  前端按当前底色重算亮度（正文 4.5:1、线条 2.2:1 兜底）
- 改 `narnat.json` 的「界面」段 → 800ms 内自动换色（`theme/get` 现读磁盘，不重启后端）
