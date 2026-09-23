"""T0.1 基准提取器 —— 对旧实现（narnat_agent/，原位未动）执行 cases.py 的用例并固化输出。

用法:
    cd /d D:\\desktop\\NarnatAgent && python v2/tests/baseline/extract_old.py

产出: v2/tests/baseline/data/*.json（每类用例一个文件，UTF-8、indent=2、ensure_ascii=False）

确定性保证（详见 README）：
- 每个 JSON 不含生成时间戳，连续运行逐字节一致；
- exec_signal 的随机标签模式化替换为 [<TAG>]（同时记录 has_tag/tag_count）；
- 会话格式化输出中的时间文本模式化替换为 <YYYY-MM-DD HH:MM> / <MM-DD HH:MM> / <HH:MM> / <MM-DD>；
- 渲染/配色相关输出在调用前固定"纯文本模式=False + TrueColor=True + 默认色板"，
  使 ANSI 值（精确 RGB）不随终端能力检测（COLORTERM/WT_SESSION/控制台模式）变化；
  16 色降级映射由 output 组的专门用例临时切换覆盖，见下。

只调用读取/计算类接口：不启动进程、不连网、不写用户数据、不经 load_config 的磁盘路径。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import sys
from pathlib import Path

BASELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASELINE_DIR.parents[2]
DATA_DIR = BASELINE_DIR / "data"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASELINE_DIR))

import cases  # noqa: E402

from narnat_agent import output  # noqa: E402
from narnat_agent.config import defaults, loader, session_store  # noqa: E402
from narnat_agent.core import billing, compressor, message_list  # noqa: E402
from narnat_agent.tools import exec_signal, param_utils, token_estimate  # noqa: E402
from narnat_agent.tools import registry as tool_registry  # noqa: E402
from narnat_agent.ui import renderer  # noqa: E402

# ═══════════════════════════════════════════════════════════════
# 序列化 / 模式化辅助
# ═══════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"\[[0-9a-f]{8}\]")
_TAG_PLACEHOLDER = "[<TAG>]"

_TIME_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"), "<YYYY-MM-DD HH:MM>"),
    (re.compile(r"\d{2}-\d{2} \d{2}:\d{2}"), "<MM-DD HH:MM>"),
    (re.compile(r"\d{2}:\d{2}"), "<HH:MM>"),
    (re.compile(r"\d{2}-\d{2}"), "<MM-DD>"),
]


def _jsonable(obj):
    """把函数返回值转成 JSON 原生结构（dataclass/tuple/set 归一）。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return [_jsonable(v) for v in sorted(obj)]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return repr(obj)


def _mask_tags(text: str) -> str:
    return _TAG_RE.sub(_TAG_PLACEHOLDER, text)


def _mask_time(text: str) -> str:
    for pattern, placeholder in _TIME_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def _tagged(text: str) -> dict:
    """带随机标签文本的基准形态：模式化文本 + 标签统计。"""
    return {
        "text": _mask_tags(text),
        "has_tag": bool(_TAG_RE.search(text)),
        "tag_count": len(_TAG_RE.findall(text)),
    }


def _build_parts(parts) -> str:
    """按 cases.py 的 parts 协议拼装文本（含框架随机标签）。"""
    chunks = []
    for kind, value in parts:
        if kind == "lit":
            chunks.append(value)
        elif kind == "rc":
            chunks.append(exec_signal.rc_line(value))
        elif kind == "err_line":
            chunks.append(exec_signal.error_line(value))
        elif kind == "tag_error":
            chunks.append(exec_signal.tag_error(value))
        else:
            raise ValueError(f"未知 parts 类型: {kind}")
    return "".join(chunks)


def _write_json(name: str, payload: dict) -> int:
    """写出一个基准文件，返回文件内用例数。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return sum(len(group) for group in payload["groups"].values())


def _envelope(module: str, groups: dict) -> dict:
    return {
        "generated_by": "v2/tests/baseline/extract_old.py",
        "source": "narnat_agent（旧实现，原位未动）",
        "note": "随机标签 → [<TAG>]；时间文本 → <YYYY-MM-DD HH:MM>/<MM-DD HH:MM>/<HH:MM>/<MM-DD>",
        "module": module,
        "groups": groups,
    }


# ═══════════════════════════════════════════════════════════════
# 环境固定
# ═══════════════════════════════════════════════════════════════

_TARGET_TYPES = {"int": int, "float": float, "str": str}


def _fix_environment() -> None:
    """固定影响输出的全局开关（不修改旧包文件，仅固定本进程状态）。

    TrueColor 固定为 True：ANSI 值直接编码 RGB，基准对调色板差异敏感，
    且不随终端能力检测（COLORTERM/WT_SESSION/控制台模式）变化；
    16 色降级路径由 output 组的专门用例临时切换覆盖。
    """
    output.set_plain(False)
    output._TRUECOLOR = True
    # apply_style({}) 只重置派生 token 与显示开关，不会重设基础色板；
    # 故显式传入默认 hex 全表，把 C_* 也一并固定。
    default_colors = {name: hexv for name, hexv, _is_bg in output._BASE_DEFS}
    output.apply_style({"colors": dict(default_colors)})


# ═══════════════════════════════════════════════════════════════
# 1. config.loader
# ═══════════════════════════════════════════════════════════════

def _extract_loader() -> dict:
    groups = {}

    groups["loader._coerce"] = [
        {"id": c["id"], "input": {"v": c["v"], "target": c["target"]},
         "result": _jsonable(loader._coerce(c["v"], _TARGET_TYPES[c["target"]]))}
        for c in cases.LOADER_COERCE_CASES
    ]

    groups["loader._parse_token_amount"] = [
        {"id": c["id"], "input": {"v": c["v"], "default": c["default"]},
         "result": loader._parse_token_amount(c["v"], c["default"])}
        for c in cases.LOADER_TOKEN_AMOUNT_CASES
    ]

    groups["loader._parse_model_config"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": _jsonable(loader._parse_model_config(copy.deepcopy(c["value"])))}
        for c in cases.LOADER_MODEL_CONFIG_CASES
    ]

    groups["loader._parse_pricing"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": _jsonable(loader._parse_pricing(copy.deepcopy(c["value"])))}
        for c in cases.LOADER_PRICING_CASES
    ]

    groups["loader._parse_project_skill_roots"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": _jsonable(loader._parse_project_skill_roots(copy.deepcopy(c["value"])))}
        for c in cases.LOADER_SKILL_ROOTS_CASES
    ]

    groups["loader.parse_mcp_server"] = [
        {"id": c["id"], "input": {"name": c["name"], "entry": c["entry"]},
         "result": _jsonable(loader.parse_mcp_server(c["name"], copy.deepcopy(c["entry"])))}
        for c in cases.LOADER_MCP_SERVER_CASES
    ]

    groups["loader._strip_subagent_hidden"] = [
        {"id": c["id"], "input": {"md": c["md"]},
         "result": loader._strip_subagent_hidden(c["md"])}
        for c in cases.LOADER_STRIP_SUBAGENT_CASES
    ]

    groups["loader._build_system_prompt"] = [
        {"id": c["id"],
         "input": {"model": c["model"], "user_md": c["user_md"], "cwd": c["cwd"],
                   "os_name": c["os_name"], "shell_name": c["shell_name"]},
         "result": loader._build_system_prompt(
             c["model"], c["user_md"], c["cwd"], c["os_name"], c["shell_name"])}
        for c in cases.LOADER_SYSTEM_PROMPT_CASES
    ]

    groups["loader._build_ai_config"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": _jsonable(loader._build_ai_config(copy.deepcopy(c["value"])))}
        for c in cases.LOADER_AI_CONFIG_CASES
    ]

    groups["loader._build_ui_config"] = [
        {"id": c["id"],
         "input": {"value": c["value"], "max_output_tokens": c["max_output_tokens"]},
         "result": _jsonable(loader._build_ui_config(
             copy.deepcopy(c["value"]), c["max_output_tokens"]))}
        for c in cases.LOADER_UI_CONFIG_CASES
    ]

    return _envelope("narnat_agent.config.loader", groups)


# ═══════════════════════════════════════════════════════════════
# 2. config.session_store
# ═══════════════════════════════════════════════════════════════

def _extract_session_store() -> dict:
    groups = {}

    groups["session_store._safe_filename"] = [
        {"id": name, "input": {"name": name}, "result": session_store._safe_filename(name)}
        for name in cases.SESSION_SAFE_FILENAME_CASES
    ]

    groups["session_store.format_session_tree"] = [
        {"id": c["id"],
         "input": {"tree": copy.deepcopy(cases.SESSION_TREES[c["tree"]]),
                   "active_name": c["active_name"], "active_parent": c["active_parent"]},
         "result": _mask_time(session_store.format_session_tree(
             copy.deepcopy(cases.SESSION_TREES[c["tree"]]),
             c["active_name"], c["active_parent"]))}
        for c in cases.SESSION_TREE_CASES
    ]

    groups["session_store.format_session_summary"] = [
        {"id": c["id"],
         "input": {"tree": copy.deepcopy(cases.SESSION_TREES[c["tree"]]),
                   "active_name": c["active_name"], "active_parent": c["active_parent"]},
         "result": _mask_time(session_store.format_session_summary(
             copy.deepcopy(cases.SESSION_TREES[c["tree"]]),
             c["active_name"], c["active_parent"]))}
        for c in cases.SESSION_SUMMARY_CASES
    ]

    groups["session_store.format_session_list"] = [
        {"id": c["id"], "input": {"sessions": c["sessions"]},
         "result": _mask_time(session_store.format_session_list(copy.deepcopy(c["sessions"])))}
        for c in cases.SESSION_LIST_CASES
    ]

    return _envelope("narnat_agent.config.session_store", groups)


# ═══════════════════════════════════════════════════════════════
# 3. tools.exec_signal
# ═══════════════════════════════════════════════════════════════

def _extract_exec_signal() -> dict:
    groups = {}

    groups["exec_signal.rc_line"] = [
        {"id": c["id"], "input": {"rc": c["rc"]},
         "result": _tagged(exec_signal.rc_line(c["rc"]))}
        for c in cases.EXEC_SIGNAL_RC_LINE_CASES
    ]

    groups["exec_signal.error_line"] = [
        {"id": c["id"], "input": {"msg": c["msg"]},
         "result": _tagged(exec_signal.error_line(c["msg"]))}
        for c in cases.EXEC_SIGNAL_ERROR_LINE_CASES
    ]

    groups["exec_signal.tag_error"] = [
        {"id": c["id"], "input": {"text": c["text"]},
         "result": _tagged(exec_signal.tag_error(c["text"]))}
        for c in cases.EXEC_SIGNAL_TAG_ERROR_CASES
    ]

    groups["exec_signal.has_error"] = [
        {"id": c["id"], "input": {"parts": c["parts"]},
         "result": {"value": exec_signal.has_error(_build_parts(c["parts"])),
                    "text": _mask_tags(_build_parts(c["parts"]))}}
        for c in cases.EXEC_SIGNAL_HAS_ERROR_CASES
    ]

    groups["exec_signal.parse_rc"] = [
        {"id": c["id"], "input": {"parts": c["parts"]},
         "result": {"value": exec_signal.parse_rc(_build_parts(c["parts"])),
                    "text": _mask_tags(_build_parts(c["parts"]))}}
        for c in cases.EXEC_SIGNAL_PARSE_RC_CASES
    ]

    groups["exec_signal.strip_tags"] = [
        {"id": c["id"], "input": {"parts": c["parts"]},
         "result": {"value": exec_signal.strip_tags(_build_parts(c["parts"])),
                    "text": _mask_tags(_build_parts(c["parts"]))}}
        for c in cases.EXEC_SIGNAL_STRIP_TAGS_CASES
    ]

    groups["exec_signal.safe_cut_points"] = [
        {"id": c["id"],
         "input": {"parts": c["parts"], "head": c["head"], "tail": c["tail"]},
         "result": {"text": _mask_tags(_build_parts(c["parts"])),
                    "cut": _jsonable(exec_signal.safe_cut_points(
                        _build_parts(c["parts"]), c["head"], c["tail"]))}}
        for c in cases.EXEC_SIGNAL_CUT_POINT_CASES
    ]

    return _envelope("narnat_agent.tools.exec_signal", groups)


# ═══════════════════════════════════════════════════════════════
# 4. config.defaults（thinking 映射）
# ═══════════════════════════════════════════════════════════════

def _extract_defaults() -> dict:
    groups = {}

    groups["defaults.resolve_thinking_params"] = []
    for c in cases.THINKING_PARAM_CASES:
        body_top, extra_body = defaults.resolve_thinking_params(
            c["protocol"], c["model"], c["enabled"], c["effort"])
        groups["defaults.resolve_thinking_params"].append({
            "id": c["id"],
            "input": {"protocol": c["protocol"], "model": c["model"],
                      "thinking_enabled": c["enabled"], "effort": c["effort"]},
            "result": {"body_top": _jsonable(body_top), "extra_body": _jsonable(extra_body)},
        })

    groups["defaults.resolve_thinking_passback"] = [
        {"id": c["id"], "input": {"protocol": c["protocol"], "model": c["model"]},
         "result": defaults.resolve_thinking_passback(c["protocol"], c["model"])}
        for c in cases.THINKING_PASSBACK_CASES
    ]

    groups["defaults.prompt_templates"] = [
        {"id": "BASE_PROMPT_TEMPLATE", "input": {},
         "result": defaults.BASE_PROMPT_TEMPLATE},
        {"id": "COMPRESS_PROMPT", "input": {}, "result": defaults.COMPRESS_PROMPT},
    ]

    return _envelope("narnat_agent.config.defaults", groups)


# ═══════════════════════════════════════════════════════════════
# 5. ui.renderer
# ═══════════════════════════════════════════════════════════════

def _extract_renderer() -> dict:
    groups = {}

    groups["renderer._display_width"] = [
        {"id": c["id"], "input": {"text": c["text"]},
         "result": renderer._display_width(c["text"])}
        for c in cases.RENDERER_DISPLAY_WIDTH_CASES
    ]

    groups["renderer._char_width"] = [
        {"id": c["id"], "input": {"ch": c["ch"]},
         "result": renderer._char_width(c["ch"])}
        for c in cases.RENDERER_CHAR_WIDTH_CASES
    ]

    groups["renderer._wrap_cell"] = [
        {"id": c["id"], "input": {"text": c["text"], "max_width": c["max_width"]},
         "result": renderer._wrap_cell(c["text"], c["max_width"])}
        for c in cases.RENDERER_WRAP_CELL_CASES
    ]

    groups["renderer.render_line"] = [
        {"id": c["id"], "input": {"line": c["line"]},
         "result": renderer.render_line(c["line"])}
        for c in cases.RENDERER_RENDER_LINE_CASES
    ]

    groups["renderer._split_cells / _is_table_separator"] = []
    for c in cases.RENDERER_SPLIT_CELLS_CASES:
        cells = renderer._split_cells(c["raw"])
        groups["renderer._split_cells / _is_table_separator"].append({
            "id": c["id"], "input": {"raw": c["raw"]},
            "result": {"cells": cells,
                       "is_table_separator": renderer._is_table_separator(cells)},
        })

    groups["renderer._fit_widths"] = [
        {"id": c["id"],
         "input": {"natural": c["natural"], "avail": c["avail"], "min_col": c["min_col"]},
         "result": renderer._fit_widths(c["natural"], c["avail"], c["min_col"])}
        for c in cases.RENDERER_FIT_WIDTHS_CASES
    ]

    groups["renderer.InlineRules.render"] = [
        {"id": c["id"], "input": {"text": c["text"]},
         "result": renderer.InlineRules.render(c["text"])}
        for c in cases.RENDERER_INLINE_CASES
    ]

    groups["renderer.CodeBlockRenderer.render"] = [
        {"id": c["id"], "input": {"lang": c["lang"], "body": c["body"], "width": c["width"]},
         "result": renderer.CodeBlockRenderer.render(c["lang"], c["body"], c["width"])}
        for c in cases.RENDERER_CODE_BLOCK_CASES
    ]

    groups["renderer.colorize_diff"] = [
        {"id": c["id"], "input": {"diff": c["diff"]},
         "result": renderer.colorize_diff(c["diff"])}
        for c in cases.RENDERER_COLORIZE_DIFF_CASES
    ]

    return _envelope("narnat_agent.ui.renderer", groups)


# ═══════════════════════════════════════════════════════════════
# 6. output（配方解析 / 主题）
# ═══════════════════════════════════════════════════════════════

def _extract_output() -> dict:
    groups = {}

    groups["output._parse_recipe"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": output._parse_recipe(c["value"])}
        for c in cases.OUTPUT_RECIPE_CASES
    ]

    groups["output._hex_to_ansi"] = [
        {"id": c["id"], "input": {"hex": c["hex"], "bg": c["bg"]},
         "result": output._hex_to_ansi(c["hex"], bg=c["bg"])}
        for c in cases.OUTPUT_HEX_ANSI_CASES
    ]

    groups["output._resolve_ptk_style"] = [
        {"id": c["id"], "input": {"value": c["value"]},
         "result": output._resolve_ptk_style(c["value"])}
        for c in cases.OUTPUT_PTK_STYLE_CASES
    ]

    default_colors = {name: hexv for name, hexv, _is_bg in output._BASE_DEFS}
    output.apply_style({"colors": dict(default_colors)})
    groups["output.apply_style(默认色板)"] = [{
        "id": "default_style",
        "input": {"colors": default_colors},
        "result": {
            "tokens": {name: getattr(output, name)._value
                       for name in cases.OUTPUT_STYLE_TOKEN_NAMES},
            "ptk": {"symbol": output.PTK_PROMPT_SYMBOL,
                    "text": output.PTK_PROMPT_TEXT,
                    "custom": output.PTK_PROMPT_CUSTOM},
            "base_hex": dict(output._BASE_HEX),
            "display_state": {
                "show_cost": output.DisplayState.show_cost,
                "show_balance": output.DisplayState.show_balance,
                "max_tokens": output.DisplayState.max_tokens,
                "show_ratio": output.DisplayState.show_ratio,
                "context_window": output.DisplayState.context_window,
            },
        },
    }]

    # 16 色降级映射：临时切换 _TRUECOLOR（仅计算、不 apply_style），记录后恢复
    groups["output._hex_to_ansi(非TrueColor降级)"] = []
    output._TRUECOLOR = False
    try:
        for c in cases.OUTPUT_HEX_ANSI_CASES:
            groups["output._hex_to_ansi(非TrueColor降级)"].append({
                "id": c["id"], "input": {"hex": c["hex"], "bg": c["bg"]},
                "result": output._hex_to_ansi(c["hex"], bg=c["bg"]),
            })
    finally:
        output._TRUECOLOR = True

    return _envelope("narnat_agent.output", groups)


# ═══════════════════════════════════════════════════════════════
# 7. core.billing
# ═══════════════════════════════════════════════════════════════

def _extract_billing() -> dict:
    groups = {}
    pricing = cases.BILLING_PRICING

    groups["billing.get_pricing"] = [
        {"id": c["id"],
         "input": {"model": c["model"],
                   "user_pricing": None if c["pricing"] == "none" else pricing},
         "result": _jsonable(billing.get_pricing(
             c["model"], None if c["pricing"] == "none" else pricing))}
        for c in cases.BILLING_GET_PRICING_CASES
    ]

    groups["billing.cost_breakdown+calculate_cost"] = []
    for model in cases.BILLING_COST_MODELS:
        for usage in cases.BILLING_COST_USAGES:
            groups["billing.cost_breakdown+calculate_cost"].append({
                "id": f"{model}|{usage['id']}",
                "input": {"model": model, "prompt_tokens": usage["prompt"],
                          "completion_tokens": usage["completion"],
                          "cached_tokens": usage["cached"], "user_pricing": pricing},
                "result": {
                    "breakdown": _jsonable(billing.cost_breakdown(
                        model, usage["prompt"], usage["completion"], usage["cached"], pricing)),
                    "total": billing.calculate_cost(
                        model, usage["prompt"], usage["completion"], usage["cached"], pricing),
                },
            })

    groups["billing.cost_breakdown(no_pricing)"] = [{
        "id": "no_pricing",
        "input": {"model": "m-basic", "prompt_tokens": 1000000,
                  "completion_tokens": 500000, "cached_tokens": 0, "user_pricing": None},
        "result": {
            "breakdown": _jsonable(billing.cost_breakdown(
                "m-basic", 1000000, 500000, 0, None)),
            "total": billing.calculate_cost("m-basic", 1000000, 500000, 0, None),
        },
    }]

    groups["billing._resolve_jsonpath"] = [
        {"id": c["id"], "input": {"data": c["data"], "path": c["path"]},
         "result": _jsonable(billing._resolve_jsonpath(copy.deepcopy(c["data"]), c["path"]))}
        for c in cases.BILLING_JSONPATH_CASES
    ]

    return _envelope("narnat_agent.core.billing", groups)


# ═══════════════════════════════════════════════════════════════
# 8. tools.token_estimate
# ═══════════════════════════════════════════════════════════════

def _extract_token_estimate() -> dict:
    groups = {}

    groups["token_estimate.estimate_text_tokens"] = [
        {"id": c["id"], "input": {"text": c["text"]},
         "result": token_estimate.estimate_text_tokens(c["text"])}
        for c in cases.TOKEN_ESTIMATE_TEXT_CASES
    ]

    groups["token_estimate.estimate_message_tokens"] = [
        {"id": c["id"], "input": {"msg": c["msg"]},
         "result": token_estimate.estimate_message_tokens(copy.deepcopy(c["msg"]))}
        for c in cases.TOKEN_ESTIMATE_MESSAGE_CASES
    ]

    groups["compressor.estimate_tokens"] = [
        {"id": c["id"], "input": {"msg": c["msg"]},
         "result": compressor.estimate_tokens(copy.deepcopy(c["msg"]))}
        for c in cases.TOKEN_ESTIMATE_MESSAGE_CASES
    ]

    return _envelope("narnat_agent.tools.token_estimate", groups)


# ═══════════════════════════════════════════════════════════════
# 9. core.message_list
# ═══════════════════════════════════════════════════════════════

def _run_message_script(script: dict) -> dict:
    ml = message_list.MessageList(script["system_prompt"])
    for op in script["ops"]:
        kind = op["op"]
        if kind == "append_system":
            ml.append_system(op["content"])
        elif kind == "append_user":
            ml.append_user(op["content"])
        elif kind == "append_assistant":
            kwargs = {}
            if "thinking" in op:
                kwargs["thinking"] = op["thinking"]
            if "thinking_signature" in op:
                kwargs["thinking_signature"] = op["thinking_signature"]
            if "tool_calls" in op:
                kwargs["tool_calls"] = copy.deepcopy(op["tool_calls"])
            ml.append_assistant(op["content"], **kwargs)
        elif kind == "append_tool_result":
            ml.append_tool_result(op["tool_call_id"], op["result"])
        elif kind == "append_interrupted_tools":
            ml.append_interrupted_tools(
                copy.deepcopy(op["tool_calls"]), set(op["completed_ids"]))
        elif kind == "replace_all":
            ml.replace_all(copy.deepcopy(op["messages"]))
        elif kind == "clear_and_rebuild":
            ml.clear_and_rebuild(op["system_prompt"], op["summary"], compressor.Compressor())
        elif kind == "compress_and_rebuild":
            ml.compress_and_rebuild(op["system_prompt"], op["summary"],
                                    op["pending_input"], compressor.Compressor())
        else:
            raise ValueError(f"未知 op: {kind}")

    view = ml.view()
    return {
        "messages": _jsonable(view.to_list()),
        "len": len(ml),
        "counts": {role: view.count_role(role)
                   for role in ("system", "user", "assistant", "tool")},
    }


def _extract_message_list() -> dict:
    groups = {
        "message_list.scripts": [
            {"id": s["id"], "input": {"script": s},
             "result": _run_message_script(s)}
            for s in cases.MESSAGE_SCRIPTS
        ],
        "message_list.SYNTHETIC_THINKING": [
            {"id": "constant", "input": {},
             "result": message_list.SYNTHETIC_THINKING}
        ],
    }
    return _envelope("narnat_agent.core.message_list", groups)


# ═══════════════════════════════════════════════════════════════
# 10. core.compressor
# ═══════════════════════════════════════════════════════════════

def _extract_compressor() -> dict:
    groups = {}

    groups["compressor._cut_balanced"] = [
        {"id": c["id"],
         "input": {"dataset": c["dataset"], "messages": cases.COMPRESSOR_DATASETS[c["dataset"]],
                   "cut": c["cut"]},
         "result": compressor._cut_balanced(
             copy.deepcopy(cases.COMPRESSOR_DATASETS[c["dataset"]]), c["cut"])}
        for c in cases.COMPRESSOR_CUT_BALANCED_CASES
    ]

    groups["compressor.select_cut_index"] = [
        {"id": c["id"],
         "input": {"dataset": c["dataset"], "messages": cases.COMPRESSOR_DATASETS[c["dataset"]],
                   "retain_tokens": c["retain_tokens"]},
         "result": compressor.select_cut_index(
             copy.deepcopy(cases.COMPRESSOR_DATASETS[c["dataset"]]), c["retain_tokens"])}
        for c in cases.COMPRESSOR_SELECT_CUT_CASES
    ]

    groups["compressor.Compressor.build_*"] = []
    for c in cases.COMPRESSOR_BUILD_CASES:
        dataset = copy.deepcopy(cases.COMPRESSOR_DATASETS[c["dataset"]])
        comp = compressor.Compressor()
        if c["id"] == "compress_msgs":
            built = comp.build_compress_messages(dataset)
            result = {
                "count": len(built),
                "roles": [m.get("role") for m in built],
                "last_role": built[-1].get("role") if built else None,
                "last_content_len": len(built[-1].get("content", "")) if built else 0,
                "last_content_is_compress_prompt":
                    bool(built) and built[-1].get("content") == defaults.COMPRESS_PROMPT,
            }
        else:
            built = comp.build_new_session_messages(
                c["system_prompt"], c["summary"], dataset[c["tail_from"]:])
            result = {"messages": _jsonable(built)}
        groups["compressor.Compressor.build_*"].append({
            "id": c["id"], "input": c,
            "result": result,
        })

    return _envelope("narnat_agent.core.compressor", groups)


# ═══════════════════════════════════════════════════════════════
# 11. tools.param_utils 与 registry 参数错误路径
# ═══════════════════════════════════════════════════════════════

def _extract_param_utils() -> dict:
    groups = {}

    groups["param_utils.to_bool"] = [
        {"id": c["id"], "input": {"v": c["v"]}, "result": param_utils.to_bool(c["v"])}
        for c in cases.PARAM_UTILS_TO_BOOL_CASES
    ]

    groups["registry._friendly_type_error"] = [
        {"id": c["id"], "input": {"tool": c["tool"], "err": c["err"]},
         "result": tool_registry._friendly_type_error(
             c["tool"], tool_registry._TOOL_IMPLEMENTATIONS.get(c["tool"]),
             TypeError(c["err"]))}
        for c in cases.PARAM_UTILS_FRIENDLY_ERROR_CASES
    ]

    groups["registry.execute(错误路径)"] = []
    for c in cases.PARAM_UTILS_EXECUTE_ERROR_CASES:
        llm_result, color_diff = tool_registry.execute(c["name"], copy.deepcopy(c["arguments"]))
        groups["registry.execute(错误路径)"].append({
            "id": c["id"], "input": {"name": c["name"], "arguments": c["arguments"]},
            "result": {"llm_result": _tagged(llm_result), "color_diff": color_diff},
        })

    return _envelope("narnat_agent.tools.param_utils / registry", groups)


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

_EXTRACTORS = [
    ("loader", _extract_loader),
    ("session_store", _extract_session_store),
    ("exec_signal", _extract_exec_signal),
    ("defaults", _extract_defaults),
    ("renderer", _extract_renderer),
    ("output_style", _extract_output),
    ("billing", _extract_billing),
    ("token_estimate", _extract_token_estimate),
    ("message_list", _extract_message_list),
    ("compressor", _extract_compressor),
    ("param_utils", _extract_param_utils),
]


def main() -> int:
    _fix_environment()

    results = []
    for name, extractor in _EXTRACTORS:
        payload = extractor()
        results.append((name, payload))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for stale in DATA_DIR.glob("*.json"):
        stale.unlink()

    total = 0
    for name, payload in results:
        count = _write_json(name, payload)
        total += count
        print(f"  {name}.json  ({count} 用例)  {payload['module']}")
    print(f"共写入 {len(results)} 个文件、{total} 用例 → {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
