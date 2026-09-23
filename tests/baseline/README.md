# 基准数据（对旧实现的行为基准）—— T0.1

> **切换后状态说明（recast-v2 已完成，工作树内为积木化新实现）**：
> - `data/`、`data_remote/`、`stream/`、`ui/` 下的基准数据**保持不变**——它们是旧实现（v16.2.5）固化行为，
>   单测持续以它们作为"行为等价"的判据（新实现 vs 旧基准）。
> - `extract_old*.py` 系列脚本面向**旧实现**；旧实现已由重构替换（保留于 git 提交 `7d075a2`），
>   脚本现仅在"回滚到旧实现"场景下有意义。**不要重跑这些脚本覆盖 `data/`**（会以新实现输出覆盖旧基准）。
> - 文件路径：脚本内为 `v2/tests/baseline/...`（重构期间路径）；当前目录为 `tests/baseline/...`，运行前需自行改路径或回滚后使用。

为 change `recast-v2` 的"行为等价"提供**可计算判据**：对旧实现（`narnat_agent/`，原位未动）
运行一组确定性用例，把输出固化成基准；未来新实现跑同样用例、逐个比对。

## 1. 文件

| 文件 | 作用 |
|---|---|
| `cases.py` | 用例输入集（**纯数据**，不导入任何实现；新旧实现共用同一份输入） |
| `extract_old.py` | 对**旧实现**执行用例并写出基准 |
| `data/*.json` | 基准数据（11 个文件、452 用例；UTF-8、indent=2、ensure_ascii=False、LF） |
| `README.md` | 本文件 |

`data/` 每个文件形如：

```json
{ "generated_by": "...", "source": "...", "note": "...", "module": "narnat_agent.xxx",
  "groups": { "<模块.函数>": [ {"id": "...", "input": ..., "result": ...}, ... ] } }
```

## 2. 如何生成基准

```cmd
cd /d D:\desktop\NarnatAgent
python v2/tests/baseline/extract_old.py
```

- 退出码 0，并打印每个文件的用例数；重复运行会先清空 `data/*.json` 再重写。
- **幂等**：连续两次运行逐字节一致。验证命令（两次输出应完全相同）：

```cmd
python -c "import hashlib,pathlib;[print(p.name,hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(pathlib.Path('v2/tests/baseline/data').glob('*.json'))]"
```

**确定性措施**（不满足则基准不可比）：

1. **随机标签模式化**：`tools/exec_signal` 的退出码/错误标签是进程级随机 uuid，基准把它替换为
   `[<TAG>]`，另记录 `has_tag` / `tag_count`；用例输入用 `parts` 协议（`["lit",文本]` /
   `["rc",N]` / `["err_line",msg]` / `["tag_error",文本]`）拼装，使索引类结果（如
   `safe_cut_points` 的切点）与标签取值无关。
2. **时间文本模式化**：会话格式化输出中的时间被替换为 `<YYYY-MM-DD HH:MM>` /
   `<MM-DD HH:MM>` / `<HH:MM>` / `<MM-DD>`。用例时间戳只用两个哨兵值
   （`SESSION_TS_FUTURE`=2100-01-01 → 永远"今天"组；`SESSION_TS_EPOCH`=0 → 永远"更早"组），
   故"今天/更早"分组、溢出提示等行为与运行时刻、时区均无关。
3. **终端能力固定**：提取器先 `output.set_plain(False)`、`output._TRUECOLOR = True`、
   再 `apply_style({默认 hex 全表})`，使 ANSI 值（精确 RGB）不随 `COLORTERM` /
   `WT_SESSION` / 控制台模式变化；16 色降级路径由 `output._hex_to_ansi(非TrueColor降级)`
   组临时切换覆盖。已验证：注入上述环境变量后重跑，11 个文件哈希不变。
4. **不含生成时间戳**，字典顺序、浮点 repr、行尾 LF 均固定。

## 3. 覆盖清单（逐项）

| # | 模块（旧包路径） | 基准文件 | 组 | 用例数 |
|---|---|---|---|---|
| 1 | config.loader | `loader.json` | `_coerce` / `_parse_token_amount` / `_parse_model_config` / `_parse_pricing` / `_parse_project_skill_roots` / `parse_mcp_server` / `_strip_subagent_hidden` / `_build_system_prompt` / `_build_ai_config` / `_build_ui_config` | 95 |
| 2 | config.session_store | `session_store.json` | `_safe_filename` / `format_session_tree` / `format_session_summary` / `format_session_list` | 24 |
| 3 | tools.exec_signal | `exec_signal.json` | `rc_line` / `error_line` / `tag_error` / `has_error` / `parse_rc` / `strip_tags` / `safe_cut_points`（**标签模式化**，附 `has_tag`） | 29 |
| 4 | config.defaults | `defaults.json` | `resolve_thinking_params`（协议×模型前缀×强度×启用开关）/ `resolve_thinking_passback` / 两个 prompt 模板全文 | 101 |
| 5 | ui.renderer | `renderer.json` | `_display_width` / `_char_width` / `_wrap_cell` / `render_line` / `_split_cells`+`_is_table_separator` / `_fit_widths` / `InlineRules.render` / `CodeBlockRenderer.render` / `colorize_diff` | 71 |
| 6 | output | `output_style.json` | `_parse_recipe` / `_hex_to_ansi`（含 16 色降级）/ `_resolve_ptk_style` / `apply_style` 后的 token 值与显示开关 | 27 |
| 7 | core.billing | `billing.json` | `get_pricing` / `cost_breakdown`+`calculate_cost`（模型×用量矩阵）/ `_resolve_jsonpath` | 27 |
| 8 | tools.token_estimate | `token_estimate.json` | `estimate_text_tokens` / `estimate_message_tokens` / `compressor.estimate_tokens` | 21 |
| 9 | core.message_list | `message_list.json` | 消息操作脚本序列（append 系列 / 中断修复 / `replace_all` / `clear_and_rebuild` / `compress_and_rebuild`）/ `SYNTHETIC_THINKING` | 8 |
| 10 | core.compressor | `compressor.json` | `_cut_balanced` / `select_cut_index`（消息序列×retain_tokens 矩阵）/ `build_compress_messages` / `build_new_session_messages` | 16 |
| 11 | tools.param_utils + registry | `param_utils.json` | `to_bool` / `_friendly_type_error` / `registry.execute` 的参数错误与未知工具路径 | 33 |

要求与边界（均已遵守）：只调用读取/计算类接口；不启动进程、不连网、不写用户数据、不经
`load_config` 的磁盘路径（配置用例全部用构造 dict）；不运行 narnat / main.py；不改
`narnat_agent/` 旧包。

## 4. 未覆盖的用例（原因）

| 未覆盖项 | 原因 |
|---|---|
| `renderer._terminal_width` / `_windows_console_window_cols` / `_srwindow_cols` | 依赖真实终端/控制台尺寸（Win32 API、`NARNAT_TERM_WIDTH`），无确定值 |
| `StreamingRenderer.feed/flush/reset`、`renderer._sep` | 会直接写 stdout，非纯函数（其内部依赖的渲染纯函数已覆盖）——**T3.9 补充基准已覆盖流式路径，见第 4.1 节** |
| `session_store.save_session/load_session/list_sessions/delete_session/list_sessions_tree/load_session_meta` | 读写用户数据目录（磁盘 IO、真实时间戳），禁止调用 |
| `config.loader.load_config/_find_project_root/_load_json/_load_user_md` | 读写 `.narnat/` 磁盘路径、依赖 cwd/打包环境（各子构建函数已用构造 dict 覆盖） |
| `core.billing.fetch_balance` | 网络请求 |
| `exec_signal` 的模块级 `_TAG`/`_ERR_TAG` 取值本身 | 进程级随机值，不做基准（以模式化 + `has_tag` 表达） |
| `renderer._visual_chars` | 纯函数但非清单要求项（已由 `_wrap_cell` 用例间接覆盖） |

### 4.1 T3.9 补充基准：旧实现流式渲染路径（`stream/render_stream.json`）

T3.9（ui/render 重组）需要"流式路径行为零变化"的判据，故用"重定向 stdout +
固定 `NARNAT_TERM_WIDTH`/srWindow/纯文本开关"把 `StreamingRenderer` 纳入基准：

| 文件 | 作用 |
|---|---|
| `render_stream_cases.py` | 流式场景输入集（**纯数据**；70 场景：块级/行内形态、表格四形态、缓冲上限与落定语义、代码块状态机、reset 重播、纯文本形态、切片粒度差异） |
| `extract_old_render_stream.py` | 对**旧实现**执行场景并写出基准 |
| `stream/render_stream.json` | 流式路径基准（1 个 group、70 场景；与 `data/` 同格式） |

```cmd
cd /d D:\desktop\NarnatAgent
python v2/tests/baseline/extract_old_render_stream.py
```

- 独立于 `data/`（`extract_old.py` 运行时清空的是 `data/*.json`，不影响本目录）。
- 幂等：连续两次运行逐字节一致（SHA-256 `0f957cf4…513d`，2026-09 生成）。
- 新实现侧对照：`v2/tests/unit/test_render.py::TestOldImplementationStreamSnapshot`。

### 4.2 T3.10 补充基准：旧实现 ui 交互面（`ui/ui_interaction.json`）

T3.10（ui 交互层搬入积木）需要"统计栏 / 启动横幅 / 打断提示 / Tab 补全"的行为判据，
这四处均为"写 stdout 或依赖鸭子接口对象"的路径，T0.1 未覆盖，故以"重定向 stdout +
固定 `NARNAT_TERM_WIDTH` + 真彩 + 默认色板 + 非纯文本模式"纳入基准：

| 文件 | 作用 |
|---|---|
| `ui_interaction_cases.py` | 用例输入集（**纯数据**；统计栏参数矩阵 12、横幅 2、打断提示 1、补全 31） |
| `extract_old_ui_interaction.py` | 对**旧实现**执行用例并写出基准 |
| `ui/ui_interaction.json` | 交互面基准（4 组、46 用例；与 `data/` 同格式） |

```cmd
cd /d D:\desktop\NarnatAgent
python v2/tests/baseline/extract_old_ui_interaction.py
```

- 组名与旧实现模块路径对应：`ui_design.show_stats` / `ui_design.show_header` /
  `ui_design.show_interrupted` / `session_commands._CommandCompleter.get_completions`。
- 补全组的命令源状态（可用命令表 / 会话名 / 删除候选 / 思考强度 / 模型 / 技能树）
  全部为构造数据，不读用户目录、不连网。
- 幂等：连续两次运行逐字节一致（SHA-256 `635265c3…a590f`，2026-09 生成）。
- 新实现侧对照：`v2/tests/unit/test_ui_interaction.py::TestOldImplementationUiSnapshot`
  （统计栏与横幅/打断提示判据 = 输出字节等价；补全判据 = 候选三元组逐字段等价）。

## 5. 如何比对（未来）新实现

前置：`cases.py` 是用例契约，比对期间不得改动（如需新增用例，须重新跑旧实现提取基准）。

1. **生成旧基准**（本仓库已含 `data/`；如需重建）：
   `python v2/tests/baseline/extract_old.py`
2. **为新实现写同构提取器** `v2/tests/baseline/extract_new.py`：从新包导入对应积木，
   对**同一份** `cases.py` 逐用例调用，按同样约定序列化（dataclass→dict、tuple→list、
   随机标签→`[<TAG>]`、时间→占位符、固定纯文本/TrueColor/默认色板），输出到
   `v2/tests/baseline/data_new/*.json`（同名文件、同名 groups、同名 case id）。
3. **逐字节比对**（因两侧都做了规范化，字节一致即等价）：

```cmd
python -c "import hashlib,pathlib;O=pathlib.Path('v2/tests/baseline/data');N=pathlib.Path('v2/tests/baseline/data_new');[print(p.name,'一致' if (N/p.name).exists() and hashlib.sha256(p.read_bytes()).digest()==hashlib.sha256((N/p.name).read_bytes()).digest() else '不一致') for p in sorted(O.glob('*.json'))]"
```

4. **定位差异**（字节不一致时按 case 粒度比对）：

```python
import json, pathlib
old_dir, new_dir = pathlib.Path("v2/tests/baseline/data"), pathlib.Path("v2/tests/baseline/data_new")
diffs = []
for f in sorted(old_dir.glob("*.json")):
    old = json.loads(f.read_text(encoding="utf-8"))
    new = json.loads((new_dir / f.name).read_text(encoding="utf-8"))
    for group, cases in old["groups"].items():
        by_id = {c["id"]: c for c in new["groups"].get(group, [])}
        for case in cases:
            got = by_id.get(case["id"])
            if got is None:
                diffs.append(f"{f.name}::{group}::{case['id']} 缺少用例")
            elif got.get("result") != case["result"]:
                diffs.append(f"{group}::{case['id']}\n  old={case['result']!r}\n  new={got.get('result')!r}")
print(f"差异 {len(diffs)} 处")
print("\n".join(diffs[:50]))
```

5. **判定**：差异要么是新实现的真实行为偏离（修新实现），要么是规格有意变更
   （须同步更新 `cases.py` 并重跑第 1 步），要么是序列化约定不一致（修 `extract_new.py`）。
   不得通过放宽比对（如忽略大小写、跳过组）来"消差异"。

## 6. 已知"怪癖"行为（基准如实记录，非缺陷）

未来实现者容易误判为基准错误，务必先看这里：

- `output._parse_recipe("bg:primary")` 返回的是**前景色**序列（`bg:` 分支查 `_BASE_COLORS`
  时取的是 `.value`），只有 `bg:#RRGGBB` 才走背景色转换。
- `loader._build_ui_config` 的配方中文化名替换是**按键插入顺序**逐个 `replace`：
  `"bold 强调色"` → `"bold emphasis色"`（`colors` 表的 `强调→emphasis` 先于 `base_colors`
  表的 `强调色→accent` 命中）。
- `loader._build_ai_config` 的 `思考.启用`/`思考.回传` 用 `bool()`：字符串 `"0"` 视为
  **True**（`"false"` 亦然）；`回传: ""` 视为 False。
- `_parse_token_amount(True)` 返回 `default`（布尔被显式排除）；`1e20` → 100000000000000000000。
- 非 TrueColor 终端的 16 色近似会让不同 hex 落到同一 ANSI（如 `#5EEAD4` 与 `#34D399`
  都是 `\u001b[36m`），基准的 `output._hex_to_ansi(非TrueColor降级)` 组如实记录。
- `session_store.format_session_tree` 在 `active_name`/`active_parent` 均为 None 时，末尾会
  多出一行 `   ◉  ◀ 当前`。
- `compressor.select_cut_index` 在"对话区首条不是 user"（异常序列）时返回 `None`（全量压缩兜底），
  而不是 0：见 `head_assistant_large` 用例。
- `compressor.Compressor.build_compress_messages` 基准只记结构摘要（条数/角色序列/末尾
  是否等于 `COMPRESS_PROMPT`/长度），prompt 全文另由 `defaults.prompt_templates` 组覆盖。

## 7. 验收记录（本次生成）

- 退出码：`python v2/tests/baseline/extract_old.py` → 0，输出 11 个文件、452 用例。
- 幂等：连续两次运行 + 注入 `WT_SESSION=1`/`COLORTERM=truecolor`/`NARNAT_TERM_WIDTH=200`
  运行，11 个文件 SHA-256 完全一致（命令见第 2 节）。
- 人工核对（任取 4 个基准值，JSON 基准 vs 直接调用旧实现）：

| 基准项 | JSON 基准 | 直接调用 | 一致 |
|---|---|---|---|
| `billing.cost_breakdown+calculate_cost` / `m-basic\|partial_cache` | breakdown `[600000.0, 40000.0, 1000000.0]`，total `1640000.0` | 同 | ✓ |
| `token_estimate.estimate_text_tokens` / `mixed`（"中英mixed混排 text"） | `6` | `6` | ✓ |
| `defaults.resolve_thinking_params` / `anthropic\|deepseek-v4-pro\|effort=high` | `body_top={"output_config":{"effort":"high"}}`，`extra_body={"thinking":{"type":"enabled"}}` | 同 | ✓ |
| `renderer._display_width` / `mixed`（"中文abc"） | `7` | `7` | ✓ |

核对命令示例（其余同理，按 `id` 取基准用例后直接调用旧实现比对）：

```cmd
cd /d D:\desktop\NarnatAgent
python -c "import json,sys;sys.path.insert(0,'.');from narnat_agent.core import billing;d=json.load(open('v2/tests/baseline/data/billing.json',encoding='utf-8'));c=[x for x in d['groups']['billing.cost_breakdown+calculate_cost'] if x['id']=='m-basic|partial_cache'][0];u=c['input'];bd=list(billing.cost_breakdown(u['model'],u['prompt_tokens'],u['completion_tokens'],u['cached_tokens'],u['user_pricing']));t=billing.calculate_cost(u['model'],u['prompt_tokens'],u['completion_tokens'],u['cached_tokens'],u['user_pricing']);print(c['result']);print({'breakdown':bd,'total':t});print('一致:',bd==c['result']['breakdown'] and t==c['result']['total'])"
```
