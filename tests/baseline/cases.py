"""T0.1 基准用例输入集 —— 纯数据，不导入任何实现。

每组常量是一批用例，每项为 dict：含 "id"（用例名）+ 输入字段。
extract_old.py 负责把输入喂给旧实现（narnat_agent/）的对应函数并序列化结果；
cases.py 本身不引用旧包，因此未来新实现比对时可原样复用本文件。

约定：
- 时间戳一律取 SESSION_TS_FUTURE / SESSION_TS_EPOCH：前者永远落在"今天"分组、
  后者永远落在"更早"分组，使 format_session_* 的分组结果与运行时刻无关
  （输出中的具体时间文本由提取器模式化替换）。
- exec_signal 用例用 parts 描述文本拼装，元素形态：
  ["lit", 原文] / ["rc", 退出码] / ["err_line", 错误消息] / ["tag_error", 文本]。
  拼装产生的随机标签由提取器模式化替换为 [<TAG>]，并记录 has_tag。
- 配置类用例一律用构造 dict，不经 load_config 的磁盘路径。
"""

# ═══════════════════════════════════════════════════════════════
# 1. config.loader
# ═══════════════════════════════════════════════════════════════

# _coerce(v, target_type)：target 为 "int" / "float" / "str"
LOADER_COERCE_CASES = [
    {"id": "none_to_int", "v": None, "target": "int"},
    {"id": "empty_str_to_int", "v": "", "target": "int"},
    {"id": "invalid_str_to_int", "v": "abc", "target": "int"},
    {"id": "digit_str_to_int", "v": "12", "target": "int"},
    {"id": "float_str_to_int", "v": "12.7", "target": "int"},
    {"id": "int_to_int", "v": 12, "target": "int"},
    {"id": "float_to_int", "v": 12.7, "target": "int"},
    {"id": "true_to_int", "v": True, "target": "int"},
    {"id": "false_to_int", "v": False, "target": "int"},
    {"id": "negative_to_int", "v": -3, "target": "int"},
    {"id": "zero_to_int", "v": 0, "target": "int"},
    {"id": "none_to_float", "v": None, "target": "float"},
    {"id": "str_to_float", "v": "0.75", "target": "float"},
    {"id": "list_to_float", "v": [1], "target": "float"},
    {"id": "int_to_str", "v": 42, "target": "str"},
    {"id": "bool_to_str", "v": True, "target": "str"},
]

# _parse_token_amount(v, default)
LOADER_TOKEN_AMOUNT_CASES = [
    {"id": "none_default", "v": None, "default": 0},
    {"id": "empty_str_default", "v": "", "default": 123},
    {"id": "true_uses_default", "v": True, "default": 5},
    {"id": "false_uses_default", "v": False, "default": 5},
    {"id": "int_value", "v": 10000, "default": 0},
    {"id": "negative_int_clamped", "v": -5, "default": 0},
    {"id": "float_truncated", "v": 1234.9, "default": 0},
    {"id": "digit_str", "v": "20000", "default": 0},
    {"id": "k_suffix", "v": "10k", "default": 0},
    {"id": "k_suffix_upper_float", "v": "1.5K", "default": 0},
    {"id": "k_suffix_invalid", "v": "abck", "default": 0},
    {"id": "dot_no_k", "v": "1.5", "default": 0},
    {"id": "padded_str", "v": " 8000 ", "default": 0},
    {"id": "invalid_uses_default", "v": "abc", "default": 777},
    {"id": "big_float", "v": 1e20, "default": 0},
]

# _parse_model_config(value) → (当前模型, 候选列表)
LOADER_MODEL_CONFIG_CASES = [
    {"id": "none", "value": None},
    {"id": "empty_dict", "value": {}},
    {"id": "current_only", "value": {"当前": "m-a"}},
    {"id": "list_only", "value": {"列表": ["m-a", "m-b"]}},
    {"id": "current_in_list", "value": {"当前": "m-b", "列表": ["m-a", "m-b"]}},
    {"id": "current_not_in_list", "value": {"当前": "m-c", "列表": ["m-a"]}},
    {"id": "list_with_non_str", "value": {"列表": ["m-a", 1, None]}},
    {"id": "empty_current_empty_list", "value": {"当前": "", "列表": []}},
    {"id": "empty_current_nonempty_list", "value": {"当前": "", "列表": ["m-a"]}},
    {"id": "not_dict_str", "value": "model"},
    {"id": "not_dict_list", "value": ["m-a"]},
]

# _parse_pricing(data)：中文 key → 英文 key
LOADER_PRICING_CASES = [
    {"id": "empty", "value": {}},
    {"id": "full_cn_keys", "value": {"m1": {"输入": 1, "缓存命中": 0.5, "输出": 2}}},
    {"id": "partial_keys", "value": {"m1": {"输入": 3}}},
    {"id": "non_dict_skipped", "value": {"m1": "x", "m2": {"输入": 1}}},
    {"id": "en_keys_ignored", "value": {"m1": {"input": 9}}},
    {"id": "values_passthrough", "value": {"m1": {"输入": "5", "输出": None}}},
]

# _parse_project_skill_roots(data)
LOADER_SKILL_ROOTS_CASES = [
    {"id": "empty_data", "value": {}},
    {"id": "missing_key", "value": {"技能": {}}},
    {"id": "empty_list", "value": {"技能": {"项目技能目录": []}}},
    {"id": "list_normal", "value": {"技能": {"项目技能目录": ["a", "b"]}}},
    {"id": "list_filter_non_str", "value": {"技能": {"项目技能目录": ["a", "", 3, "  ", None, "b"]}}},
    {"id": "not_list", "value": {"技能": {"项目技能目录": "a"}}},
    {"id": "list_keeps_spaces", "value": {"技能": {"项目技能目录": [" a "]}}},
]

# parse_mcp_server(name, entry)：中文/英文别名键、command 数组归一、布尔容错
LOADER_MCP_SERVER_CASES = [
    {"id": "entry_not_dict", "name": "srv", "entry": "notdict"},
    {"id": "name_empty", "name": "", "entry": {}},
    {"id": "name_blank", "name": "   ", "entry": {}},
    {"id": "minimal", "name": "srv", "entry": {}},
    {"id": "cn_keys", "name": "srv", "entry": {
        "命令": "python", "参数": ["-m", "srv"],
        "环境变量": {"K": 1, "J": None}, "工作目录": "D:/x",
        "启用": "false", "启动超时秒": "60", "工具超时秒": 0,
        "工具白名单": ["a"], "工具黑名单": ["b"],
    }},
    {"id": "en_keys", "name": "srv", "entry": {
        "command": "node", "args": ["a.js"], "env": {"X": "1"}, "cwd": "/tmp",
        "enabled": "yes", "startup_timeout_sec": 5, "tool_timeout_sec": "7",
        "enabled_tools": ["t"], "disabled_tools": ["u"],
    }},
    {"id": "cn_key_priority", "name": "srv", "entry": {"命令": "cn", "command": "en"}},
    {"id": "command_list", "name": "srv", "entry": {"command": ["python", "main.py"]}},
    {"id": "command_list_plus_args", "name": "srv", "entry": {"command": ["python", "main.py"], "args": ["--x"]}},
    {"id": "command_empty_list", "name": "srv", "entry": {"command": []}},
    {"id": "args_not_list", "name": "srv", "entry": {"args": "x"}},
    {"id": "env_not_dict", "name": "srv", "entry": {"env": ["x"]}},
    {"id": "enabled_str_off", "name": "srv", "entry": {"enabled": "off"}},
    {"id": "enabled_str_no_cn", "name": "srv", "entry": {"enabled": "否"}},
    {"id": "enabled_str_yes_padded", "name": "srv", "entry": {"enabled": " YES "}},
    {"id": "enabled_bool_false", "name": "srv", "entry": {"enabled": False}},
    {"id": "timeout_negative", "name": "srv", "entry": {"启动超时秒": -5}},
    {"id": "timeout_invalid_str", "name": "srv", "entry": {"启动超时秒": "abc"}},
]

# _strip_subagent_hidden(md)
LOADER_STRIP_SUBAGENT_CASES = [
    {"id": "plain", "md": "hello"},
    {"id": "single_block", "md": "a\n<!-- subagent:hide -->\nhidden\n<!-- /subagent:hide -->\nb"},
    {"id": "spaced_tags", "md": "a<!--  subagent:hide  -->x<!--  /subagent:hide  -->b"},
    {"id": "two_blocks", "md": "A<!-- subagent:hide -->1<!-- /subagent:hide -->B<!-- subagent:hide -->2<!-- /subagent:hide -->C"},
    {"id": "unclosed", "md": "a<!-- subagent:hide --> hidden"},
    {"id": "in_code_fence", "md": "`<!-- subagent:hide -->x<!-- /subagent:hide -->`"},
]

# _build_system_prompt(model, user_md, cwd, os_name, shell_name)
LOADER_SYSTEM_PROMPT_CASES = [
    {"id": "no_user_md", "model": "deepseek-v4-flash", "user_md": "",
     "cwd": "D:/work/proj", "os_name": "Windows", "shell_name": "cmd.exe"},
    {"id": "with_user_md", "model": "gpt-5", "user_md": "# 规则\n- 只说中文",
     "cwd": "/home/user/proj", "os_name": "Linux", "shell_name": "bash"},
]

# _build_ai_config(data) → AIConfig（asdict 后序列化）
LOADER_AI_CONFIG_CASES = [
    {"id": "empty", "value": {}},
    {"id": "full_cn_keys", "value": {"智能体": {
        "接口密钥": "sk-x", "接口地址": "https://example.com", "协议": "openai",
        "模型": {"当前": "m-b", "列表": ["m-a", "m-b"]},
        "温度": "0.7", "最大输出token数": "4096", "上下文窗口大小": "200000",
        "目标模式最大轮数": 5,
        "思考": {"启用": True, "强度": "max", "回传": True,
                 "强度选项": {"high": "高", "max": "全开"}},
    }}},
    {"id": "invalid_values", "value": {"智能体": {
        "温度": "abc", "最大输出token数": "", "上下文窗口大小": "x"}}},
    {"id": "context_window_zero", "value": {"智能体": {"上下文窗口大小": 0}}},
    {"id": "context_window_negative", "value": {"智能体": {"上下文窗口大小": "-5"}}},
    {"id": "goal_rounds_zero", "value": {"智能体": {"目标模式最大轮数": 0}}},
    {"id": "thinking_str_values", "value": {"智能体": {
        "思考": {"启用": "0", "回传": "", "强度": "max"}}}},
]

# _build_ui_config(data, max_output_tokens) → UIConfig（asdict 后序列化）
LOADER_UI_CONFIG_CASES = [
    {"id": "empty", "value": {}, "max_output_tokens": 128000},
    {"id": "default_max_tokens", "value": {}, "max_output_tokens": 4096},
    {"id": "cn_sections", "value": {"界面": {
        "颜色": {"强调色": "#112233"},
        "标注": {"标题1": "bold accent"},
        "代码块": {"行号": "secondary"},
        "差异": {"添加": "success"},
        "框架": {"标题": "accent"},
        "命令": {"错误": "error"},
        "提示符": {"符号": "bold #00ff00"},
    }}, "max_output_tokens": 128000},
    {"id": "top_switches", "value": {"界面": {
        "show_cost": True, "显示余额": True, "最大输出token数": "2048"}},
        "max_output_tokens": 128000},
    {"id": "old_flat_keys", "value": {"界面": {
        "用户输入色": "#FFFFFF", "代码块背景色": "code_bg", "标题色": "#111111"}},
        "max_output_tokens": 128000},
    {"id": "recipe_zh_color", "value": {"界面": {
        "markdown": {"heading_h1": "bold 强调色"}}},
        "max_output_tokens": 128000},
    {"id": "non_dict_section", "value": {"界面": {"markdown": "x"}},
        "max_output_tokens": 128000},
]

# ═══════════════════════════════════════════════════════════════
# 2. config.session_store
# ═══════════════════════════════════════════════════════════════

SESSION_SAFE_FILENAME_CASES = [
    "普通会话名",
    "a/b",
    "a\\b",
    "a:b",
    "a<b>",
    "a|b",
    "a?b",
    "a*b",
    "",
    "a..b",
    "..",
    "...",
    " / \\ : < > | ? * ",
]

# 时间戳哨兵：未来值 → 永远落在"今天"组；epoch 0 → 永远落在"更早"组
SESSION_TS_FUTURE = 4102444800
SESSION_TS_EPOCH = 0

SESSION_TREES = {
    "main": [
        {"name": "会话A", "timestamp": SESSION_TS_FUTURE, "message_count": 12,
         "children": [
             {"name": "子任务-完成", "timestamp": SESSION_TS_FUTURE - 300,
              "message_count": 3, "status": "completed", "summary": "已完成摘要"},
             {"name": "子任务-新", "timestamp": SESSION_TS_FUTURE - 200,
              "message_count": 5, "status": "new", "summary": None},
             {"name": "子任务-进行中", "timestamp": SESSION_TS_FUTURE - 100,
              "message_count": 7, "status": "active", "summary": "进行中的摘要"},
         ],
         "_delete_marked": False},
        {"name": "会话B", "timestamp": SESSION_TS_EPOCH, "message_count": 2,
         "children": [], "_delete_marked": True},
    ],
    "marked": [
        {"name": "会话C", "timestamp": SESSION_TS_FUTURE, "message_count": 1,
         "children": [
             {"name": "子任务-标记", "timestamp": SESSION_TS_FUTURE - 10,
              "message_count": 2, "status": "completed", "summary": None,
              "_delete_marked": True},
         ],
         "_delete_marked": True},
    ],
    "many_earlier": [
        {"name": "早1", "timestamp": SESSION_TS_EPOCH + 400, "message_count": 1, "children": []},
        {"name": "早2", "timestamp": SESSION_TS_EPOCH + 300, "message_count": 2, "children": []},
        {"name": "早3", "timestamp": SESSION_TS_EPOCH + 200, "message_count": 3,
         "children": [{"name": "早3-子", "timestamp": SESSION_TS_EPOCH + 150,
                       "message_count": 1, "status": "new", "summary": None}]},
        {"name": "早4", "timestamp": SESSION_TS_EPOCH + 100, "message_count": 4, "children": []},
        {"name": "早5-当前", "timestamp": SESSION_TS_EPOCH + 50, "message_count": 5, "children": []},
    ],
    "empty": [],
}

# format_session_tree(tree, active_name, active_parent)
SESSION_TREE_CASES = [
    {"id": "empty", "tree": "empty", "active_name": None, "active_parent": None},
    {"id": "no_active", "tree": "main", "active_name": None, "active_parent": None},
    {"id": "active_root", "tree": "main", "active_name": "会话A", "active_parent": None},
    {"id": "active_child", "tree": "main", "active_name": "子任务-新", "active_parent": "会话A"},
    {"id": "delete_marked", "tree": "marked", "active_name": "会话C", "active_parent": None},
]

# format_session_summary(tree, active_name, active_parent)
SESSION_SUMMARY_CASES = [
    {"id": "empty", "tree": "empty", "active_name": None, "active_parent": None},
    {"id": "today_and_earlier", "tree": "main", "active_name": None, "active_parent": None},
    {"id": "active_child", "tree": "main", "active_name": "子任务-进行中", "active_parent": "会话A"},
    {"id": "earlier_overflow", "tree": "many_earlier",
     "active_name": "早5-当前", "active_parent": None},
]

# format_session_list(sessions)
SESSION_LIST_CASES = [
    {"id": "empty", "sessions": []},
    {"id": "two_sessions", "sessions": [
        {"name": "会话A", "timestamp": SESSION_TS_FUTURE, "message_count": 12},
        {"name": "会话B", "timestamp": SESSION_TS_EPOCH, "message_count": 0},
    ]},
]

# ═══════════════════════════════════════════════════════════════
# 3. tools.exec_signal（输出含随机标签 → 提取器模式化）
# ═══════════════════════════════════════════════════════════════

EXEC_SIGNAL_RC_LINE_CASES = [
    {"id": "rc_zero", "rc": 0},
    {"id": "rc_one", "rc": 1},
    {"id": "rc_negative", "rc": -1},
    {"id": "rc_130", "rc": 130},
]

EXEC_SIGNAL_ERROR_LINE_CASES = [
    {"id": "err_cn", "msg": "连接失败"},
    {"id": "err_empty", "msg": ""},
    {"id": "err_with_brackets", "msg": "[超时: 命令已终止]"},
]

EXEC_SIGNAL_TAG_ERROR_CASES = [
    {"id": "tag_text", "text": "[超时: 命令已终止]"},
    {"id": "tag_plain", "text": "失败"},
]

EXEC_SIGNAL_HAS_ERROR_CASES = [
    {"id": "framework_error_line", "parts": [["err_line", "boom"]]},
    {"id": "fake_error_text", "parts": [["lit", "[错误: 命令找不到]"]]},
    {"id": "framework_rc_line", "parts": [["rc", 0]]},
    {"id": "tag_error_tail", "parts": [["lit", "开始 "], ["tag_error", "失败"]]},
    {"id": "plain_ok", "parts": [["lit", "全部成功"]]},
]

EXEC_SIGNAL_PARSE_RC_CASES = [
    {"id": "tagged_single", "parts": [["rc", 3]]},
    {"id": "tagged_multi_last_wins", "parts": [["rc", 1], ["lit", "中间输出"], ["rc", 0]]},
    {"id": "fake_exit_code_text", "parts": [["lit", "[exit code: 1]"]]},
    {"id": "tagged_negative", "parts": [["rc", -9]]},
    {"id": "no_rc", "parts": [["lit", "hello"]]},
    {"id": "tagged_then_plain", "parts": [["rc", 2], ["lit", "[exit code: 7]"]]},
]

EXEC_SIGNAL_STRIP_TAGS_CASES = [
    {"id": "both_tags", "parts": [["lit", "a"], ["rc", 2], ["lit", "b"], ["tag_error", "超时"]]},
    {"id": "tag_at_start", "parts": [["rc", 0], ["lit", "输出"]]},
    {"id": "fake_text_untouched", "parts": [["lit", "[exit code: 5] [错误: x]"]]},
]

# safe_cut_points(text, head, tail)：索引基于拼装后的固定结构
EXEC_SIGNAL_CUT_POINT_CASES = [
    {"id": "plain_no_tag", "parts": [["lit", "abcdefghij"]], "head": 2, "tail": 4},
    {"id": "head_inside_tag", "parts": [["lit", "HEAD"], ["rc", 1], ["lit", "TAIL"]],
     "head": 20, "tail": 29},
    {"id": "tail_inside_tag", "parts": [["lit", "HEAD"], ["rc", 1], ["lit", "TAIL"]],
     "head": 4, "tail": 22},
    {"id": "both_inside_cross", "parts": [["lit", "HEAD"], ["rc", 1], ["lit", "TAIL"]],
     "head": 20, "tail": 21},
    {"id": "head_at_tag_start", "parts": [["lit", "HEAD"], ["rc", 1], ["lit", "TAIL"]],
     "head": 18, "tail": 24},
    {"id": "err_tag_inside", "parts": [["lit", "AA"], ["err_line", "boom"], ["lit", "BB"]],
     "head": 15, "tail": 20},
]

# ═══════════════════════════════════════════════════════════════
# 4. config.defaults（thinking 映射矩阵）
# ═══════════════════════════════════════════════════════════════

THINKING_PROTOCOLS = ["anthropic", "openai"]

THINKING_PROTOTYPE_MODELS = [
    "deepseek-v4-pro", "glm-4.6", "kimi-k2", "mimo-v2.5",
    "qwen3-max", "gpt-5", "claude-sonnet-4", "unknown-model",
]

THINKING_EFFORTS = ["high", "max", "low", ""]

THINKING_PARAM_CASES = [
    {"id": "{}|{}|effort={}".format(proto, model, effort if effort else "empty"),
     "protocol": proto, "model": model, "enabled": True, "effort": effort}
    for proto in THINKING_PROTOCOLS
    for model in THINKING_PROTOTYPE_MODELS
    for effort in THINKING_EFFORTS
] + [
    {"id": "{}|{}|disabled".format(proto, model),
     "protocol": proto, "model": model, "enabled": False, "effort": "high"}
    for proto in THINKING_PROTOCOLS
    for model in THINKING_PROTOTYPE_MODELS
] + [
    {"id": "anthropic|DeepSeek-V4-upper|effort=high",
     "protocol": "anthropic", "model": "DeepSeek-V4", "enabled": True, "effort": "high"},
    {"id": "openai|QWEN3-MAX-upper|effort=max",
     "protocol": "openai", "model": "QWEN3-MAX", "enabled": True, "effort": "max"},
]

THINKING_PASSBACK_CASES = [
    {"id": "{}|{}".format(proto, model), "protocol": proto, "model": model}
    for proto in THINKING_PROTOCOLS
    for model in THINKING_PROTOTYPE_MODELS
] + [
    {"id": "other-protocol|deepseek", "protocol": "bedrock", "model": "deepseek-v4-pro"},
]

# ═══════════════════════════════════════════════════════════════
# 5. ui.renderer（先由提取器 apply_style 固定默认色 + 固定 TrueColor=False）
# ═══════════════════════════════════════════════════════════════

RENDERER_DISPLAY_WIDTH_CASES = [
    {"id": "empty", "text": ""},
    {"id": "ascii", "text": "abc"},
    {"id": "cjk", "text": "中文"},
    {"id": "mixed", "text": "中文abc"},
    {"id": "ambiguous_arrows", "text": "→≈±"},
    {"id": "emoji", "text": "😀🎉"},
    {"id": "ansi_colored", "text": "\x1b[31m红色\x1b[0m"},
    {"id": "ansi_interleaved", "text": "a\x1b[1mb\x1b[0mc"},
    {"id": "fullwidth", "text": "ＡＢＣ"},
    {"id": "tab", "text": "a\tb"},
]

RENDERER_CHAR_WIDTH_CASES = [
    {"id": "ascii_a", "ch": "a"},
    {"id": "cjk_zhong", "ch": "中"},
    {"id": "arrow", "ch": "→"},
    {"id": "emoji_grin", "ch": "😀"},
    {"id": "fullwidth_A", "ch": "Ａ"},
    {"id": "space", "ch": " "},
]

RENDERER_WRAP_CELL_CASES = [
    {"id": "plain_wrap", "text": "abcdefgh", "max_width": 3},
    {"id": "cjk_wrap", "text": "中文测试文本", "max_width": 4},
    {"id": "ansi_kept", "text": "\x1b[31mabcdef\x1b[0m", "max_width": 2},
    {"id": "zero_width", "text": "abc", "max_width": 0},
    {"id": "empty", "text": "", "max_width": 5},
    {"id": "exact_fit", "text": "ab", "max_width": 2},
    {"id": "cjk_exact_fit", "text": "中文", "max_width": 4},
]

RENDERER_RENDER_LINE_CASES = [
    {"id": "h1", "line": "# 标题"},
    {"id": "h2_bold", "line": "## 二级 **粗体**"},
    {"id": "h3", "line": "### 三级"},
    {"id": "h4", "line": "#### 四级"},
    {"id": "h5", "line": "##### 五级"},
    {"id": "hr_dash", "line": "---"},
    {"id": "hr_star", "line": "***"},
    {"id": "task_done", "line": "- [x] 完成任务"},
    {"id": "task_undone", "line": "- [ ] 未完成"},
    {"id": "ul_dash", "line": "- 无序项"},
    {"id": "ul_star", "line": "* 星号项"},
    {"id": "ol_dot", "line": "1. 有序项"},
    {"id": "ol_paren", "line": "10) 括号项"},
    {"id": "quote", "line": "> 引用"},
    {"id": "quote_nested", "line": ">> 嵌套引用"},
    {"id": "table_row", "line": "| A | B |"},
    {"id": "table_sep", "line": "| --- | --- |"},
    {"id": "paragraph_inline", "line": "段落 `代码` **粗** ~~删除~~ [链](http://x) ![图](y.png)"},
    {"id": "blank", "line": ""},
    {"id": "spaces_only", "line": "   "},
    {"id": "indented_table", "line": "  | 前导空格 | x |"},
]

RENDERER_SPLIT_CELLS_CASES = [
    {"id": "normal", "raw": "| a | b |"},
    {"id": "no_edges", "raw": "a | b"},
    {"id": "escaped_pipe", "raw": "| a \\| b | c |"},
    {"id": "double_pipe", "raw": "||"},
    {"id": "single_cell", "raw": "| a |"},
]

RENDERER_FIT_WIDTHS_CASES = [
    {"id": "plenty", "natural": [3, 5, 7], "avail": 40, "min_col": 2},
    {"id": "exact_min", "natural": [3, 5, 7], "avail": 6, "min_col": 2},
    {"id": "below_min", "natural": [3, 5, 7], "avail": 5, "min_col": 2},
    {"id": "single_col", "natural": [1], "avail": 10, "min_col": 2},
    {"id": "no_cols", "natural": [], "avail": 10, "min_col": 2},
    {"id": "proportional", "natural": [100, 200], "avail": 30, "min_col": 2},
]

RENDERER_INLINE_CASES = [
    {"id": "plain", "text": "纯文本"},
    {"id": "bold", "text": "**粗体**"},
    {"id": "italic", "text": "*斜体*"},
    {"id": "strike", "text": "~~删除~~"},
    {"id": "code", "text": "`代码`"},
    {"id": "link", "text": "[链接](http://a)"},
    {"id": "image", "text": "![图](a.png)"},
    {"id": "combined", "text": "**粗** 与 `码` 与 [链](u)"},
    {"id": "escaped_star", "text": "a\\*b"},
]

RENDERER_CODE_BLOCK_CASES = [
    {"id": "python", "lang": "python", "body": "def f():\n    return 1", "width": 80},
    {"id": "no_lang", "lang": "", "body": "plain", "width": 80},
    {"id": "unknown_lang", "lang": "UnKnown", "body": "x\ny", "width": 40},
    {"id": "trailing_spaces", "lang": "json", "body": "{\n  \"a\": 1   \n}", "width": 80},
]

RENDERER_COLORIZE_DIFF_CASES = [
    {"id": "empty", "diff": ""},
    {"id": "no_diff", "diff": "[无差异]"},
    {"id": "unified", "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n ctx"},
]

# ═══════════════════════════════════════════════════════════════
# 6. output（配方解析 / hex→ANSI / ptk 样式 / apply_style）
# ═══════════════════════════════════════════════════════════════

OUTPUT_RECIPE_CASES = [
    {"id": "empty", "value": ""},
    {"id": "spaces_only", "value": "   "},
    {"id": "bold", "value": "bold"},
    {"id": "bold_primary", "value": "bold primary"},
    {"id": "italic_dim_secondary", "value": "italic dim secondary"},
    {"id": "underline_error", "value": "underline error"},
    {"id": "hex_fg", "value": "#112233"},
    {"id": "hex_bg", "value": "bg:#112233"},
    {"id": "bg_named", "value": "bg:primary"},
    {"id": "invalid_cn_token", "value": "bold 蓝"},
    {"id": "invalid_en_token", "value": "bold unknown"},
    {"id": "two_colors", "value": "primary secondary"},
    {"id": "upper_style_ignored", "value": "BOLD"},
]

OUTPUT_HEX_ANSI_CASES = [
    {"id": "fg_1a2b3c", "hex": "#1A2B3C", "bg": False},
    {"id": "bg_1a2b3c", "hex": "#1A2B3C", "bg": True},
    {"id": "no_hash_fg", "hex": "FF0000", "bg": False},
    {"id": "black_fg", "hex": "#000000", "bg": False},
]

OUTPUT_PTK_STYLE_CASES = [
    {"id": "bold_primary", "value": "bold primary"},
    {"id": "unknown_name", "value": "纯白"},
    {"id": "empty", "value": ""},
    {"id": "hex_passthrough", "value": "bold #00ff00"},
    {"id": "accent", "value": "accent"},
]

# apply_style 后读取的 token 名（getattr(output, name)._value）
OUTPUT_STYLE_TOKEN_NAMES = [
    "C_PRIMARY", "C_SECONDARY", "C_USER", "C_ACCENT", "C_SUCCESS",
    "C_WARNING", "C_ERROR", "C_LINK", "C_DECORATION", "C_EMPHASIS", "C_CODE_BG",
    "MD_H1", "MD_CODE", "MD_TABLE_BORDER", "CB_LANG_CYAN", "DIFF_ADDED",
    "UI_SEPARATOR", "CMD_ERROR",
]

# ═══════════════════════════════════════════════════════════════
# 7. core.billing
# ═══════════════════════════════════════════════════════════════

BILLING_PRICING = {
    "m-basic": {"input": 1000000.0, "cache_hit": 100000.0, "output": 2000000.0},
    "m-zero": {"input": 0.0, "cache_hit": 0.0, "output": 0.0},
}

BILLING_GET_PRICING_CASES = [
    {"id": "hit", "model": "m-basic", "pricing": "default"},
    {"id": "zero_priced", "model": "m-zero", "pricing": "default"},
    {"id": "missing_model", "model": "m-missing", "pricing": "default"},
    {"id": "no_user_pricing", "model": "m-basic", "pricing": "none"},
]

# 模型 × 用量矩阵
BILLING_COST_MODELS = ["m-basic", "m-zero", "m-missing"]

BILLING_COST_USAGES = [
    {"id": "all_zero", "prompt": 0, "completion": 0, "cached": 0},
    {"id": "no_cache", "prompt": 1000000, "completion": 500000, "cached": 0},
    {"id": "partial_cache", "prompt": 1000000, "completion": 500000, "cached": 400000},
    {"id": "all_cached", "prompt": 1000, "completion": 1, "cached": 1000},
    {"id": "cached_over_prompt", "prompt": 100, "completion": 10, "cached": 200},
]

# _resolve_jsonpath(data, path)
BILLING_JSONPATH_CASES = [
    {"id": "nested_dict_index", "data": {"balance_infos": [{"total_balance": "12.5"}]},
     "path": "balance_infos.0.total_balance"},
    {"id": "missing_key", "data": {"a": {"b": 1}}, "path": "a.c"},
    {"id": "index_out_of_range", "data": {"a": [1]}, "path": "a.3"},
    {"id": "empty_path", "data": {"a": 1}, "path": ""},
    {"id": "scalar_mid_path", "data": {"a": 1}, "path": "a.b"},
    {"id": "non_int_index", "data": {"a": [1]}, "path": "a.x"},
    {"id": "top_level_scalar", "data": 5, "path": "x"},
]

# ═══════════════════════════════════════════════════════════════
# 8. tools.token_estimate
# ═══════════════════════════════════════════════════════════════

TOKEN_ESTIMATE_TEXT_CASES = [
    {"id": "empty", "text": ""},
    {"id": "ascii", "text": "hello world"},
    {"id": "cjk", "text": "你好世界"},
    {"id": "mixed", "text": "中英mixed混排 text"},
    {"id": "code", "text": "def f(x):\n    return x + 1"},
    {"id": "emoji", "text": "😀 emoji 测试"},
    {"id": "long_ascii", "text": "a" * 1000},
    {"id": "long_cjk", "text": "中" * 100},
    {"id": "fullwidth_punct", "text": "（全角）！？"},
]

TOKEN_ESTIMATE_MESSAGE_CASES = [
    {"id": "user_text", "msg": {"role": "user", "content": "你好"}},
    {"id": "assistant_thinking", "msg": {"role": "assistant", "content": "", "thinking": "思考中"}},
    {"id": "assistant_tool_calls", "msg": {
        "role": "assistant", "content": None,
        "tool_calls": [{"function": {"name": "Read", "arguments": "{\"file_path\": \"a.txt\"}"}}]}},
    {"id": "assistant_block_list", "msg": {"role": "assistant", "content": [{"type": "text", "text": "块列表"}]}},
    {"id": "tool_result", "msg": {"role": "tool", "tool_call_id": "1", "content": "结果"}},
    {"id": "empty_msg", "msg": {}},
]

# ═══════════════════════════════════════════════════════════════
# 9. core.message_list
# ═══════════════════════════════════════════════════════════════
# ops 元素形态：
#   {"op": "append_system", "content": str}
#   {"op": "append_user", "content": str}
#   {"op": "append_assistant", "content": str,
#    "thinking": str|None, "thinking_signature": str|None, "tool_calls": list|None}
#   {"op": "append_tool_result", "tool_call_id": str, "result": str}
#   {"op": "append_interrupted_tools", "tool_calls": list, "completed_ids": list}
#   {"op": "replace_all", "messages": list}
#   {"op": "clear_and_rebuild", "system_prompt": str, "summary": str}
#   {"op": "compress_and_rebuild", "system_prompt": str, "summary": str, "pending_input": str}
# 字段缺省 = 该参数不传（用于区分"显式传 None"与"省略"，提取器按 key 是否存在分发）

MESSAGE_SCRIPTS = [
    {"id": "basic_flow", "system_prompt": "你是助手", "ops": [
        {"op": "append_user", "content": "你好"},
        {"op": "append_assistant", "content": "回复内容", "thinking": "思考过程",
         "thinking_signature": "sig-1",
         "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{}"}}]},
        {"op": "append_tool_result", "tool_call_id": "c1", "result": "文件内容"},
        {"op": "append_system", "content": "技能注入"},
        {"op": "append_user", "content": "继续"},
    ]},
    {"id": "empty_content_and_optional", "system_prompt": "S", "ops": [
        {"op": "append_assistant", "content": "", "thinking": "", "thinking_signature": ""},
        {"op": "append_user", "content": ""},
    ]},
    {"id": "interrupted_repair", "system_prompt": "S", "ops": [
        {"op": "append_user", "content": "跑两个命令"},
        {"op": "append_assistant", "content": None, "thinking": "占位思考",
         "tool_calls": [
             {"id": "t1", "function": {"name": "Shell", "arguments": "{}"}},
             {"id": "t2", "function": {"name": "Shell", "arguments": "{}"}},
         ]},
        {"op": "append_tool_result", "tool_call_id": "t1", "result": "完成"},
        {"op": "append_interrupted_tools",
         "tool_calls": [{"id": "t1"}, {"id": "t2"}], "completed_ids": ["t1"]},
    ]},
    {"id": "replace_all", "system_prompt": "S", "ops": [
        {"op": "append_user", "content": "旧消息"},
        {"op": "replace_all", "messages": [
            {"role": "system", "content": "新系统"},
            {"role": "user", "content": "新消息"},
        ]},
    ]},
    {"id": "clear_and_rebuild", "system_prompt": "S", "ops": [
        {"op": "append_user", "content": "旧消息"},
        {"op": "clear_and_rebuild", "system_prompt": "新系统", "summary": "摘要文本"},
    ]},
    {"id": "compress_and_rebuild_with_summary", "system_prompt": "S", "ops": [
        {"op": "append_user", "content": "旧消息"},
        {"op": "compress_and_rebuild", "system_prompt": "新系统2", "summary": "摘要文本2",
         "pending_input": "待处理输入"},
    ]},
    {"id": "compress_and_rebuild_no_summary", "system_prompt": "S", "ops": [
        {"op": "append_user", "content": "旧消息"},
        {"op": "compress_and_rebuild", "system_prompt": "新系统3", "summary": "",
         "pending_input": "无摘要输入"},
    ]},
]

# ═══════════════════════════════════════════════════════════════
# 10. core.compressor
# ═══════════════════════════════════════════════════════════════

COMPRESSOR_DATASETS = {
    "normal": [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1",
         "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3" * 100},
    ],
    "head_assistant": [
        {"role": "system", "content": "系统提示"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u1"},
    ],
    "system_only": [
        {"role": "system", "content": "s1"},
        {"role": "system", "content": "s2"},
    ],
    "empty": [],
}

# _cut_balanced(messages, cut)
COMPRESSOR_CUT_BALANCED_CASES = [
    {"id": "normal_cut0_system", "dataset": "normal", "cut": 0},
    {"id": "normal_cut1_user", "dataset": "normal", "cut": 1},
    {"id": "normal_cut2_assistant", "dataset": "normal", "cut": 2},
    {"id": "normal_cut_end", "dataset": "normal", "cut": 7},
]

# select_cut_index(messages, retain_tokens)
COMPRESSOR_SELECT_CUT_CASES = [
    {"id": "zero_retain", "dataset": "normal", "retain_tokens": 0},
    {"id": "negative_retain", "dataset": "normal", "retain_tokens": -5},
    {"id": "tiny_retain", "dataset": "normal", "retain_tokens": 10},
    {"id": "medium_retain", "dataset": "normal", "retain_tokens": 100},
    {"id": "large_retain", "dataset": "normal", "retain_tokens": 100000},
    {"id": "head_assistant_small", "dataset": "head_assistant", "retain_tokens": 1},
    {"id": "head_assistant_large", "dataset": "head_assistant", "retain_tokens": 100000},
    {"id": "system_only", "dataset": "system_only", "retain_tokens": 100},
    {"id": "empty", "dataset": "empty", "retain_tokens": 100},
]

# Compressor().build_compress_messages / build_new_session_messages
COMPRESSOR_BUILD_CASES = [
    {"id": "compress_msgs", "dataset": "normal"},
    {"id": "new_session_with_tail", "dataset": "normal",
     "system_prompt": "新系统", "summary": "摘要内容", "tail_from": 4},
    {"id": "new_session_no_summary", "dataset": "normal",
     "system_prompt": "新系统", "summary": "", "tail_from": 0},
]

# ═══════════════════════════════════════════════════════════════
# 11. tools.param_utils 与 registry 参数错误路径
# ═══════════════════════════════════════════════════════════════

PARAM_UTILS_TO_BOOL_CASES = [
    {"id": "bool_true", "v": True},
    {"id": "bool_false", "v": False},
    {"id": "str_true", "v": "true"},
    {"id": "str_TRUE", "v": "TRUE"},
    {"id": "str_yes_padded", "v": " yes "},
    {"id": "str_on", "v": "on"},
    {"id": "str_1", "v": "1"},
    {"id": "str_t", "v": "t"},
    {"id": "str_y", "v": "y"},
    {"id": "str_false", "v": "false"},
    {"id": "str_0", "v": "0"},
    {"id": "str_no", "v": "no"},
    {"id": "str_off", "v": "off"},
    {"id": "str_f", "v": "f"},
    {"id": "str_n", "v": "n"},
    {"id": "str_empty", "v": ""},
    {"id": "str_spaces", "v": "  "},
    {"id": "str_other", "v": "maybe"},
    {"id": "int_1", "v": 1},
    {"id": "int_0", "v": 0},
    {"id": "int_neg", "v": -1},
    {"id": "float", "v": 2.5},
    {"id": "none", "v": None},
    {"id": "empty_list", "v": []},
    {"id": "list_with_zero", "v": [0]},
]

# _friendly_type_error(name, impl, err)：tool 用于从 registry 取真实 impl
PARAM_UTILS_FRIENDLY_ERROR_CASES = [
    {"id": "unknown_param", "tool": "Edit",
     "err": "Edit.execute() got an unexpected keyword argument 'filepath'"},
    {"id": "missing_one", "tool": "Read",
     "err": "Read.execute() missing 1 required positional argument: 'file_path'"},
    {"id": "missing_two", "tool": "Read",
     "err": "missing 2 required positional arguments: 'a' and 'b'"},
    {"id": "other_type_error", "tool": "Edit", "err": "boom"},
    {"id": "unknown_tool_impl", "tool": "NoSuchTool",
     "err": "unexpected keyword argument 'x'"},
]

# registry.execute 的错误路径（安全：不触发任何工具体执行）
PARAM_UTILS_EXECUTE_ERROR_CASES = [
    {"id": "unknown_tool", "name": "NoSuchTool", "arguments": {}},
    {"id": "edit_unknown_param", "name": "Edit", "arguments": {"filepath": "x"}},
    {"id": "read_missing_param", "name": "Read", "arguments": {}},
]
