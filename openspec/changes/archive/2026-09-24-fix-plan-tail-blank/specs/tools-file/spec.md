# Spec Delta

## MODIFIED Requirements

### Requirement: 编辑类工具的返回形态与差分展示

`Edit` 与 `Write` SHALL 返回二元组 `(文本结果, 着色 diff)`，其中文本结果传给 AI、着色 diff 供终端展示；其它工具（`Read`/`Glob`/`Grep`）SHALL 只返回文本结果（由执行入口归一为「文本 + 空 diff」）。执行入口 SHALL 在返回后对文本结果施加全局输出上限截断与框架标签处理，并把着色 diff 交给 UI 层展示：非静默模式下逐行渲染（每行前置两个空格缩进）；**差异块为多行时 SHALL 以空行收尾**（与后续输出分隔），着色 diff 为单行 `[无差异]` 提示时其后不加空行；静默模式（quiet tools）下 SHALL 跳过 diff 展示。上述尾随规则按**声明的显示类别**（差异块 / 状态行列表，如 TodoWrite 计划见 `tools-todo`「TodoWrite 状态同步」）区分，不得以内容猜测决定尾随空行。失败判定 SHALL 以「非命令类工具的结果文本以 `[错误` 开头」为依据，`Edit`/`Write`/`Read`/`Glob`/`Grep` 均属此类；框架生成的错误行文本含进程级随机标签，标签在交给 AI 前剥离，AI 看到的内容与不带标签时一致。

#### Scenario: Edit 返回二元组
- **WHEN** `Edit` 成功
- **THEN** 执行入口得到 `(文本结果, 着色 diff)` 二元组；文本结果进 AI 上下文、着色 diff 进终端展示

#### Scenario: Read 返回文本
- **WHEN** `Read` 成功
- **THEN** 执行入口得到空 diff，终端不额外展示差分

#### Scenario: 静默模式跳过 diff
- **WHEN** 静默工具模式开启且 `Write` 覆盖文件产生 diff
- **THEN** 终端不展示该着色 diff

#### Scenario: 失败显示判定
- **WHEN** `Read` 的结果文本以 `[错误` 开头
- **THEN** UI 展示失败提示（非命令类工具的判定依据）

#### Scenario: 多行差异块以空行收尾
- **WHEN** 差异内容为多行（如 `--- a` / `+++ b` / 变更行）
- **THEN** 逐行缩进渲染后追加一个空行，与后续输出分隔

#### Scenario: 单行无差异不加空行
- **WHEN** 着色 diff 为单行 `[无差异]` 提示
- **THEN** 渲染该行后不加空行，直接接后续输出（现状保持）

#### Scenario: 差异块与状态行列表的尾随规则互不串用
- **WHEN** 状态行列表（非差异内容）与差异块分别渲染
- **THEN** 状态行列表不以空行收尾；差异块多行以空行收尾——两条规则按显示类别区分，不按内容猜测
