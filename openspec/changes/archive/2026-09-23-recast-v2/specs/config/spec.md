# Spec Delta

## Purpose

定义配置加载能力的行为契约：`.narnat` 目录的发现与初始化、narnat.json 的解析（含历史键名兼容与类型容错）、默认值中心、用户指令组装与系统提示词拼接。本能力是全部其他能力的数据来源，其磁盘格式与键名是已发布契约，必须跨版本兼容。

## ADDED Requirements

### Requirement: `.narnat` 目录发现

系统 SHALL 按固定优先级定位 `.narnat` 目录：① 环境变量 `NARNAT_HOME`（仅当其下存在 `.narnat` 子目录时生效）；② 打包运行态（Nuitka onefile / PyInstaller）使用可执行文件所在目录；③ 开发态从当前工作目录向上最多查找 10 层含 `.narnat` 的目录，找不到时退回当前工作目录。

#### Scenario: NARNAT_HOME 指定且有效
- **WHEN** 环境变量 `NARNAT_HOME` 指向的目录下存在 `.narnat/`
- **THEN** 使用该目录作为项目根，忽略其他分支

#### Scenario: NARNAT_HOME 无效时继续回退
- **WHEN** `NARNAT_HOME` 已设置但其下无 `.narnat/`
- **THEN** 忽略该变量，继续按 exe 目录 / cwd 向上查找的规则定位

#### Scenario: 开发态向上查找
- **WHEN** 当前工作目录无 `.narnat/`，其某级祖先目录（≤10 层）存在 `.narnat/`
- **THEN** 使用最近的含 `.narnat/` 的祖先目录作为项目根

### Requirement: 首次运行初始化

系统 SHALL 在配置目录或数据目录缺失时自动创建，并在 `narnat.json` / `narnat.md` 缺失时生成默认文件；`narnat.md` 生成为空文件；日志目录不在此阶段创建。

#### Scenario: 全新环境首次启动
- **WHEN** 项目根下不存在 `.narnat/`
- **THEN** 系统创建 `.narnat/config/`、`.narnat/data/`，生成默认 `narnat.json`（固定键结构，UTF-8、2 空格缩进、中文不转义）与空 `narnat.md`
- **AND** `.narnat/logs/` 仅在调试模式启动日志时创建

#### Scenario: 配置文件已存在
- **WHEN** `narnat.json` 已存在
- **THEN** 系统不覆盖、不补写任何键

### Requirement: 配置键名兼容

系统 SHALL 接受首次生成模板发布的中文键名及其英文别名（如 `显示费用`/`show_cost`、`显示余额`/`show_balance`、`最大输出token数`/`max_output_tokens` 等）。别名同时存在时的裁决规则：顶层开关以英文键优先（先查英文名，命中即用）；分组名中→英迁移仅在英文键不存在时执行（英文键已存在时中文键原样保留）；分组内键名与旧扁平键的迁移为直赋迁入（目标位置已有值时覆盖）。解析 SHALL 接受历史扁平色键（如 `用户输入色`）并迁移到新的分组结构。

#### Scenario: 英文别名
- **WHEN** narnat.json 的 `界面` 组使用 `"show_cost": true`
- **THEN** 显示费用开关生效，等同写入 `"显示费用": true`

#### Scenario: 顶层开关中英并存
- **WHEN** 「界面」同时含 `显示费用` 与 `show_cost`（值相反）
- **THEN** 英文键 `show_cost` 生效

#### Scenario: 分组名中英并存
- **WHEN** 「界面」同时含 `颜色` 与 `colors` 两个分组
- **THEN** 两者均保留在原始配置中，不做迁移（`colors` 保持自身值）

#### Scenario: 旧扁平配色迁移
- **WHEN** 配置含旧版扁平键 `"标题色": "红"`
- **THEN** 该值直赋写入对应分组的强调角色，并移除旧键；若该角色已有值则被覆盖

### Requirement: 数值解析与容错

系统 SHALL 对可空数值型配置项（温度、最大输出token数、上下文窗口大小、保留尾部、Token量等）执行宽松解析：值为 `null`/空串/非法内容时回落默认值；`上下文窗口大小` 显式 ≤0 时保留原值（下游视为无效）；`保留尾部` 显式 0 合法；`目标模式最大轮数` 为 0/非法时回落 100、负值保留；Token 量支持 `"10k"`/`"1.5K"` 后缀（k=×1000）。

#### Scenario: 字符串 Token 量
- **WHEN** `会话.自动保存Token量` 为 `"10k"`
- **THEN** 解析为 10000；`"1.5K"` 解析为 1500

#### Scenario: 非法数值回落
- **WHEN** `压缩.保留尾部` 为 `"abc"` 或缺失
- **THEN** 回落为默认 16000

#### Scenario: 单位换算
- **WHEN** `工具.输出上限KB` 为 64
- **THEN** 内部换算为 65536 字符；值为 ≤0 时换算结果 0 表示不限制

#### Scenario: 费用日志容量
- **WHEN** `费用日志.最大容量MB` 为 50
- **THEN** 换算为 50×1024×1024 字节；负值按 0（不轮转）处理

### Requirement: 模型配置归一

系统 SHALL 归一化模型配置：`模型.列表` 非列表时视为空；元素仅保留字符串；`模型.当前` 为空时取列表首项（列表为空时取内置默认模型）；`当前` 不在列表中时插入列表首位。

#### Scenario: 当前模型不在列表
- **WHEN** `模型.当前` 为 `"deepseek-flash"`，`列表` 为 `["deepseek-v4-pro"]`
- **THEN** 列表归一为 `["deepseek-flash", "deepseek-v4-pro"]`

### Requirement: 系统提示词组装

系统 SHALL 以模板（含模型名、当前目录、操作系统三个占位）组装基础提示词，并在 `narnat.md` 用户指令非空时以换行拼接于末尾。headless 模式 SHALL 剔除 `narnat.md` 中 `<!-- subagent:hide -->…<!-- /subagent:hide -->` 配对区块；只出现单侧标记时不剔除。

#### Scenario: 用户指令追加
- **WHEN** `narnat.md` 内容为「项目规则：用中文回复」
- **THEN** 系统提示词 = 基础模板 + 换行 + 用户指令

#### Scenario: headless 隐藏区块
- **WHEN** headless 运行且 narnat.md 含一对 subagent:hide 标记
- **THEN** 配对区块整体从系统提示词中删除；未配对的标记原样保留

### Requirement: 项目技能目录三态

系统 SHALL 支持 `技能.项目技能目录` 三种语义：键缺失/非列表 = 自动发现；空列表 `[]` = 关闭项目技能扫描；非空字符串列表 = 仅扫描指定目录。

#### Scenario: 关闭扫描
- **WHEN** `技能.项目技能目录` 为 `[]`
- **THEN** 不进行任何项目技能目录自动发现

### Requirement: MCP 服务器参数解析

系统 SHALL 解析 MCP 服务器配置条目（供运行时连接使用），支持中英键别名（参数/args、命令/command、环境变量/env、启动超时秒/startup_timeout_sec、工具超时秒/tool_timeout_sec、工具白名单/enabled_tools、工具黑名单/disabled_tools、启用/enabled、工作目录/cwd）；`command` 为数组时拆为首项与其余参数；字符串布尔（如 `"false"`）按白名单解析；超时 ≤0 或非法时回落默认值；`name` 为空或条目非对象时返回无效（None）。

#### Scenario: 命令数组归一
- **WHEN** 条目 `"command": ["npx", "-y", "server"]`
- **THEN** 解析为 command=`npx`、args=`["-y", "server"]`

#### Scenario: 字符串布尔
- **WHEN** 条目 `"enabled": "false"`
- **THEN** 解析为未启用（而非按真值字符串处理）

### Requirement: 磁盘路径布局

系统 SHALL 使用固定磁盘布局：项目根 `.narnat/`，其下 `config/`（narnat.json、narnat.md、skills/）、`data/`（sessions/、cost_log.csv）、`logs/`；路径常量（`.narnat`、`config`、`data`、`logs`、`sessions`、`narnat.json`、`narnat.md`）为已发布契约，不得更改。

#### Scenario: 会话与日志落点
- **WHEN** 保存会话或写费用日志
- **THEN** 分别落于 `.narnat/data/sessions/` 与 `.narnat/data/cost_log.csv`（未配置自定义输出文件时）

### Requirement: 兼容性怪癖保持

以下现存边缘行为 SHALL 在重构中保持等价（避免用户可感知差异）：字符串布尔值 `"false"` 在开关类配置（`思考.启用`/`回传`、`界面.show_cost` 等）按非空字符串为真处理；`忽略目录` 为字符串时按字符逐字拆分；`目标模式最大轮数` 负值进入续跑逻辑。若未来修正，须作为独立变更处理。

#### Scenario: 字符串布尔按真值处理（兼容怪癖）
- **WHEN** `"显示费用": "false"`
- **THEN** 行为等同 `true`（现状保持）
