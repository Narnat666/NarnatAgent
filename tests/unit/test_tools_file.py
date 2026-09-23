"""tools/file 工具族自测 —— T3.4 交付物（Read / Glob / Grep / Edit / Write）。

行为金标准：`openspec/changes/recast-v2/specs/tools-file/spec.md`（25 Requirement / 97 场景）。

## spec Scenario 映射表（Requirement → 本文件测试）

| # | Requirement（场景数） | 测试 |
|---|---|---|
| 1 | 五个工具的工具名与定义要点（2） | `test_file_tool_order_matches_spec` / `test_tool_names_are_call_keys` |
| 2 | Read 参数契约（3） | `test_read_definition_param_contract` / `test_read_defaults_read_2000_lines` / `test_read_limit_non_positive` / `test_read_string_numeric_params` |
| 3 | Read 输出格式与行号（3） | `test_read_basic_output_format` / `test_read_absolute_line_numbers_with_offset` / `test_read_long_line_truncated` |
| 4 | Read 空结果与文件级错误文案（4） | `test_read_empty_file` / `test_read_offset_beyond_eof` / `test_read_directory_target` / `test_read_missing_file` |
| 5 | Read 编码识别与二进制拒绝（4） | `test_read_utf8_and_bom` / `test_read_gbk_file` / `test_detect_encoding_boundary_retry` / `test_read_binary_rejected` / `test_read_invalid_bytes_replaced` |
| 6 | Read 截断与续读协议（4） | `test_read_limit_truncation_hint` / `test_read_huge_file_hint_degraded` / `test_read_global_cap_by_lines` / `test_read_no_context_no_global_cap` |
| 7 | Read/Edit/Write 的设备参数契约（4） | `test_device_dev0_equals_local` / `test_device_invalid_value` / `test_remote_read_success_has_device_header` / `test_remote_read_error_has_no_header` |
| 8 | Glob 参数契约与模式语法（5） | `test_glob_definition_param_contract` / `test_glob_recursive_match` / `test_glob_brace_multi_patterns` / `test_glob_absolute_pattern` / `test_glob_empty_pattern` / `test_glob_max_results_hard_limit` |
| 9 | Glob 结果排序、输出与文案（4） | `test_glob_sorted_by_mtime_desc` / `test_glob_truncation_hint` / `test_glob_no_match_with_hidden_hint` / `test_glob_missing_root_dir` |
| 10 | Glob 忽略目录与隐藏项过滤（4） | `test_glob_ignore_dirs_pruned` / `test_glob_hidden_skipped_by_default` / `test_glob_dot_component_enables_hidden` / `test_glob_directory_participates` |
| 11 | Grep 参数契约与输入校验（5） | `test_grep_definition_param_contract` / `test_grep_defaults_case_sensitive` / `test_grep_cli_style_aliases` / `test_grep_unknown_param` / `test_grep_invalid_regex` / `test_grep_string_boolean` |
| 12 | Grep 输出格式与匹配计数（3） | `test_grep_group_header_and_match_lines` / `test_grep_context_lines_and_separator` / `test_grep_count_not_affected_by_budget` |
| 13 | Grep head_limit 预算与降级协议（2） | `test_grep_budget_exhausted_to_list` / `test_grep_unlimited_budget` |
| 14 | Grep 跳过规则、忽略目录与无匹配文案（4） | `test_grep_skips_binary_and_large_files` / `test_grep_missing_path_warning` / `test_grep_no_match_wording` / `test_grep_ignore_dirs_pruned` |
| 15 | Edit 参数契约与唯一性校验（6） | `test_edit_definition_param_contract` / `test_edit_unique_replacement` / `test_edit_multiple_matches_rejected` / `test_edit_replace_all` / `test_edit_similar_lines_hint` / `test_edit_string_boolean_replace_all` / `test_edit_without_prior_read` |
| 16 | Edit 编码与换行符保持（5） | `test_edit_keeps_crlf_and_bom` / `test_edit_lf_pattern_hits_crlf_file` / `test_edit_gbk_write_unencodable` / `test_edit_rejects_other_encoding` / `test_edit_no_change_hint` |
| 17 | Write 创建与覆盖行为（6） | `test_write_definition_param_contract` / `test_write_new_file` / `test_write_overwrite_returns_diff` / `test_write_identical_bytes` / `test_write_same_text_bytes_changed` / `test_write_always_utf8` / `test_write_directory_target` |
| 18 | 编辑类工具的返回形态与差分展示（4） | `test_edit_returns_text_and_colored_diff` / `test_read_returns_text_only` / `test_quiet_mode_diff_skip_is_consumer_side` / `test_error_prefix_for_failure_display` |
| 19 | diff 生成与着色（4） | `test_colorize_diff_roles` / `test_colorize_diff_no_difference` / `test_describe_eol_change` / `test_describe_encoding_change` |
| 20 | 工具执行入口协议（4） | `test_unknown_tool` / `test_unknown_param_hint` / `test_missing_required_hint` / `test_tool_exception_wrapped` |
| 21 | 工具输出全局上限截断（3） | `test_global_truncate_keeps_head_tail` / `test_global_truncate_disabled_when_zero` / `test_global_truncate_small_limit_kb` |
| 22 | 工具运行时上下文数据语义（3） | `test_ignore_dirs_from_settings` / `test_settings_zero_arg_defaults` / `test_api_key_default_empty` |
| 23 | 动态工具注册与注销可见性（3） | `test_dynamic_registration_visibility` / `test_dynamic_same_name_skipped` / `test_dynamic_unregister` |
| 24 | token 估算口径（4） | `test_token_estimate_mixed_density` |
| 25 | 兼容性怪癖保持（4） | `test_quirk_two_truncation_styles_coexist` / `test_quirk_remote_body_startswith_bracket` / `test_quirk_edit_write_failure_clears_file` / `test_quirk_empty_grep_pattern_matches_all` |

未在本文件覆盖的场景及去向（同一事实不重复测，见 T3.3 测试）：
- 20 的实例化路径（`ToolRegistry` 内部语义）、21 的整链判定、22 的 Trackers、23 的全量注册面、
  24 的逐用例基准 → `tests/unit/test_tools_framework.py`（T3.3）；本文件只复验与文件工具
  结合的实际路径。
- 18 的静默模式与 18/21 的终端展示 → `tests/unit/test_tools_ui_*.py`（T3.9/T3.10 渲染层）。

## 结构
1. 定义与参数契约（含与旧包 DEFINITION 的逐字节比对）；2. Read；3. Glob；4. Grep；
5. Edit；6. Write；7. 返回形态与 diff 着色；8. 执行入口/截断/上下文/注册/兼容怪癖；9. 结构约束。
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

from narnat_agent.contracts.tool import Tool, ToolEnv, ToolResult
from narnat_agent.tools.env import ToolEnvImpl, ToolSettingsImpl
from narnat_agent.tools.registry import ToolRegistry
from narnat_agent.tools import file as file_pkg
from narnat_agent.tools.file import (
    EDIT_DEFINITION,
    GLOB_DEFINITION,
    GREP_DEFINITION,
    READ_DEFINITION,
    WRITE_DEFINITION,
    EditTool,
    GlobTool,
    ReadTool,
    build_file_tools,
)
from narnat_agent.tools.file import diff_utils, encoding, glob as glob_module, grep as grep_module, read as read_module
from narnat_agent.tools.file.devices import device_hint, normalize_device
from narnat_agent.tools.file.param_utils import to_bool

PKG_DIR = Path(__file__).resolve().parents[2] / "narnat_agent"
FILE_DIR = PKG_DIR / "tools" / "file"
OLD_TOOLS_DIR = Path(__file__).resolve().parents[3] / "narnat_agent" / "tools"

BOM_UTF8 = b"\xef\xbb\xbf"
TAG_RE = re.compile(r"\[[0-9a-f]{8}\]")


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════


def make_env(**settings_kwargs) -> ToolEnv:
    return ToolEnvImpl(settings=ToolSettingsImpl(**settings_kwargs))


def make_registry(**tool_kwargs) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_file_tools(**tool_kwargs):
        assert registry.register(tool), tool.name
    return registry


def run(tool_name: str, args: dict, env: ToolEnv | None = None, **tool_kwargs) -> ToolResult:
    registry = make_registry(**tool_kwargs)
    return registry.execute(tool_name, args, env if env is not None else make_env())


def run_tool(tool, args: dict, env: ToolEnv | None = None) -> ToolResult:
    return tool.execute(args, env)


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_text(path: Path, text: str, encoding: str = "utf-8", newline: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)
    return path


def touch_all(root: Path, mtime: float = 1_000_000.0) -> None:
    """把目录下所有文件的修改时间统一（消除写入顺序噪声，让排序只按路径升序）。"""
    for p in sorted(root.iterdir()):
        if p.is_file():
            os.utime(p, (mtime, mtime))


class FakeRemote:
    """远程文件访问端口的测试替身（验证设备头/错误直通/端口注入语义）。"""

    def __init__(self, read_result: str = "  1→remote", devices: str = "dev1(user@host)"):
        self._read_result = read_result
        self._devices = devices
        self.calls: list[tuple] = []

    def device_list(self) -> str:
        return self._devices

    def read(self, file_path: str, offset: int, limit: int, device: str) -> str:
        self.calls.append(("read", file_path, offset, limit, device))
        return self._read_result

    def write(self, file_path: str, content: str, device: str) -> tuple[str, str]:
        self.calls.append(("write", file_path, content, device))
        return (f"[{device}] [已写入(远程): {file_path} ({len(content.encode('utf-8'))}字节)]", "colored-diff")

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool, device: str) -> tuple[str, str]:
        self.calls.append(("edit", file_path, old_string, new_string, replace_all, device))
        return (f"[{device}] [已替换1处]", "colored-diff")


def _old_definition(folder: str) -> dict:
    """从旧包源码提取 DEFINITION 字面量（ast 求值，不 import 旧包以免命名空间冲突）。"""
    path = OLD_TOOLS_DIR / folder / "__init__.py"
    if not path.exists():
        pytest.skip("旧包缺失（已切换）：以已固化的等价性报告为准")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFINITION" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} 未找到 DEFINITION")


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 1. 定义与参数契约
# ═══════════════════════════════════════════════════════════════

# Requirement 1：工具名与注册顺序（场景 2）
def test_file_tool_order_matches_spec():
    """前五项依次为 Read、Glob、Grep、Edit、Write，且 function.name 逐一一致。"""
    registry = make_registry()
    names = registry.get_tool_names()
    assert names == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert names[:5] == list(file_pkg.FILE_TOOL_ORDER)
    definitions = registry.get_tool_definitions()
    assert [d["function"]["name"] for d in definitions[:5]] == names[:5]
    assert file_pkg.build_file_tools()[0].name == "Read"


def test_tool_names_are_call_keys(tmp_path):
    """以工具名发起调用解析到对应文件类工具实现（五条路径各走一遍）。"""
    registry = make_registry()
    env = make_env()
    p = write_text(tmp_path / "x.txt", "hello\n")
    assert registry.execute("Read", {"file_path": str(p)}, env).llm_text == "  1→hello"
    assert registry.execute("Glob", {"pattern": "x.txt", "path": str(tmp_path)}, env).llm_text == "x.txt"
    assert registry.execute("Grep", {"pattern": "hello", "path": str(p)}, env).llm_text == f"{p} (1处):\n1:hello"
    assert registry.execute(
        "Edit", {"file_path": str(p), "old_string": "hello", "new_string": "hi"}, env
    ).llm_text.startswith("[已替换1处]")
    assert registry.execute(
        "Write", {"file_path": str(tmp_path / "y.txt"), "content": "z"}, env
    ).llm_text.startswith("[已写入: ")


@pytest.mark.parametrize("folder,definition,tool_name", [
    ("read", READ_DEFINITION, "Read"),
    ("glob", GLOB_DEFINITION, "Glob"),
    ("grep", GREP_DEFINITION, "Grep"),
    ("edit", EDIT_DEFINITION, "Edit"),
    ("write", WRITE_DEFINITION, "Write"),
], ids=["Read", "Glob", "Grep", "Edit", "Write"])
def test_definitions_match_old_package_bytewise(folder, definition, tool_name):
    """五个工具的 definition 与旧包 DEFINITION 逐字节（JSON 序列化）一致。"""
    old = _old_definition(folder)
    assert _json(definition) == _json(old)
    assert definition["function"]["name"] == tool_name


def test_file_tools_implement_protocol():
    for tool in build_file_tools():
        assert isinstance(tool, Tool)
        assert tool.definition()["type"] == "function"


# ── 参数表逐字（spec 参数表）──


def test_read_definition_param_contract():
    """Read：file_path/offset/limit/device；required 仅 file_path；描述逐字。"""
    props = READ_DEFINITION["function"]["parameters"]["properties"]
    assert list(props) == ["file_path", "offset", "limit", "device"]
    assert READ_DEFINITION["function"]["parameters"]["required"] == ["file_path"]
    assert props["file_path"] == {"type": "string", "description": "文件路径"}
    assert props["offset"] == {"type": "integer", "description": "起始行（默认1，含本行）"}
    assert props["limit"] == {"type": "integer", "description": "读取行数（正整数，默认2000）"}
    assert props["device"]["description"] == (
        "设备dev编号：默认dev0即读取本地指定文件，设置dev1..devn则读取指定远程设备文件")


def test_glob_definition_param_contract():
    """Glob：pattern/path/max_results；required 仅 pattern。"""
    props = GLOB_DEFINITION["function"]["parameters"]["properties"]
    assert list(props) == ["pattern", "path", "max_results"]
    assert GLOB_DEFINITION["function"]["parameters"]["required"] == ["pattern"]
    assert props["path"] == {"type": "string", "description": "搜索目录（默认当前目录）"}
    assert props["max_results"] == {"type": "integer", "description": "最大结果数（正整数，默认50）"}


def test_grep_definition_param_contract():
    """Grep：八个参数按序；required 仅 pattern；path 支持字符串/数组二形态。"""
    props = GREP_DEFINITION["function"]["parameters"]["properties"]
    assert list(props) == ["pattern", "path", "glob", "i", "A", "B", "C", "head_limit"]
    assert GREP_DEFINITION["function"]["parameters"]["required"] == ["pattern"]
    assert props["path"]["anyOf"] == [
        {"type": "string"}, {"type": "array", "items": {"type": "string"}}]
    assert props["i"]["type"] == "boolean"
    for key in ("A", "B", "C", "head_limit"):
        assert props[key]["type"] == "integer"


def test_edit_definition_param_contract():
    """Edit：五个参数；required 为 file_path 与 old_string。"""
    props = EDIT_DEFINITION["function"]["parameters"]["properties"]
    assert list(props) == ["file_path", "old_string", "new_string", "replace_all", "device"]
    assert EDIT_DEFINITION["function"]["parameters"]["required"] == ["file_path", "old_string"]
    assert props["new_string"]["description"] == "替换后的新文本"
    assert props["replace_all"]["type"] == "boolean"


def test_write_definition_param_contract():
    """Write：三个参数；required 为 file_path 与 content。"""
    props = WRITE_DEFINITION["function"]["parameters"]["properties"]
    assert list(props) == ["file_path", "content", "device"]
    assert WRITE_DEFINITION["function"]["parameters"]["required"] == ["file_path", "content"]
    assert props["content"] == {"type": "string", "description": "完整文件内容"}


# ═══════════════════════════════════════════════════════════════
# 2. Read
# ═══════════════════════════════════════════════════════════════


def test_read_basic_output_format(tmp_path):
    """基本输出：三行、行号自 1 起、格式「两个空格 + 行号 + → + 内容」。"""
    p = write_text(tmp_path / "a.txt", "第一行内容\n第二行\n第三行\n")
    out = run("Read", {"file_path": str(p)}).llm_text
    assert out == "  1→第一行内容\n  2→第二行\n  3→第三行"
    assert out.split("\n")[0].startswith("  1→")


def test_read_absolute_line_numbers_with_offset(tmp_path):
    """offset=10 → 输出首行行号为 10（基于 offset 的绝对行号）。"""
    p = write_text(tmp_path / "a.txt", "".join(f"L{i}\n" for i in range(1, 21)))
    out = run("Read", {"file_path": str(p), "offset": 10}).llm_text
    assert out.split("\n")[0] == "  10→L10"
    assert out.split("\n")[-1] == "  20→L20"


def test_read_defaults_read_2000_lines(tmp_path):
    """仅传 file_path：从第 1 行开始、最多读 2000 行（超出给出截断提示）。"""
    p = write_text(tmp_path / "big.txt", "".join(f"L{i}\n" for i in range(1, 2003)))
    out = run("Read", {"file_path": str(p)}).llm_text
    lines = out.split("\n")
    assert lines[0] == "  1→L1"
    assert lines[1999] == "  2000→L2000"
    assert "已显示 2000 行" in lines[-1] and "文件共2002行，剩余2行" in lines[-1]
    assert "使用 offset=2001 参数可读取其余部分" in lines[-1]


@pytest.mark.parametrize("bad_limit", [0, -1, "-3"], ids=["zero", "negative", "string-zero"])
def test_read_limit_non_positive(tmp_path, bad_limit):
    """limit 为 0 或负数 → [错误: limit需为正整数]，不读取文件。"""
    p = write_text(tmp_path / "a.txt", "x\n")
    out = run("Read", {"file_path": str(p), "limit": bad_limit}).llm_text
    assert out == "[错误: limit需为正整数]"


def test_read_string_numeric_params(tmp_path):
    """offset 传 "5"、limit 传 "10" → 按整数解析，行为与传整数一致。"""
    p = write_text(tmp_path / "a.txt", "".join(f"L{i}\n" for i in range(1, 30)))
    out = run("Read", {"file_path": str(p), "offset": "5", "limit": "10"}).llm_text
    assert out.split("\n")[0] == "  5→L5"
    assert out.split("\n")[9] == "  14→L14"
    assert "已显示 10 行" in out


def test_read_offset_none_falls_back(tmp_path):
    """offset/limit 显式传 None → 分别回落 0 与 2000。"""
    p = write_text(tmp_path / "a.txt", "one\ntwo\n")
    out = run("Read", {"file_path": str(p), "offset": None, "limit": None}).llm_text
    assert out == "  1→one\n  2→two"


def test_read_empty_file(tmp_path):
    """空文件 → [文件为空]。"""
    p = write_text(tmp_path / "empty.txt", "")
    assert run("Read", {"file_path": str(p)}).llm_text == "[文件为空]"


def test_read_offset_beyond_eof(tmp_path):
    """文件共 3 行、offset=10 → [无内容: offset=10 已超出文件末尾（文件共3行）]。"""
    p = write_text(tmp_path / "a.txt", "1\n2\n3\n")
    out = run("Read", {"file_path": str(p), "offset": 10}).llm_text
    assert out == "[无内容: offset=10 已超出文件末尾（文件共3行）]"


def test_read_directory_target(tmp_path):
    """路径是目录 → [错误: {路径} 是目录，请用 Glob 匹配或 Shell 查看目录内容]。"""
    d = tmp_path / "sub"
    d.mkdir()
    out = run("Read", {"file_path": str(d)}).llm_text
    assert out == f"[错误: {d} 是目录，请用 Glob 匹配或 Shell 查看目录内容]"


def test_read_missing_file(tmp_path, monkeypatch):
    """文件不存在 → 报错含路径与当前目录。"""
    monkeypatch.chdir(tmp_path)
    out = run("Read", {"file_path": "nope.txt"}).llm_text
    assert out == f"[错误: 文件不存在: nope.txt（当前目录: {os.getcwd()}）]"


def test_read_utf8_and_bom(tmp_path):
    """UTF-8（含/不含 BOM）文本原样输出，BOM 被剥离。"""
    plain = write_text(tmp_path / "plain.txt", "中文内容\n")
    assert run("Read", {"file_path": str(plain)}).llm_text == "  1→中文内容"
    bom = write_bytes(tmp_path / "bom.txt", BOM_UTF8 + "中文内容\n".encode("utf-8"))
    assert run("Read", {"file_path": str(bom)}).llm_text == "  1→中文内容"


def test_read_gbk_file(tmp_path):
    """GBK 编码文件按 GBK 解码输出（不出现大片替换字符）。"""
    p = write_bytes(tmp_path / "gbk.txt", "中文内容测试\n".encode("gbk"))
    out = run("Read", {"file_path": str(p)}).llm_text
    assert out == "  1→中文内容测试"
    assert "\ufffd" not in out


def test_detect_encoding_boundary_retry():
    """边界截断重试：首块恰好在多字节字符中间被切断 → 判定 UTF-8 而非 GBK。"""
    chunk = ("中" * 2730).encode("utf-8") + b"\xe4\xb8"
    assert len(chunk) == 8192
    assert encoding.detect_text_encoding(chunk) == "utf-8-sig"
    assert encoding.detect_text_encoding(b"") == "utf-8-sig"
    assert encoding.detect_text_encoding(b"plain ascii") == "utf-8-sig"
    assert encoding.detect_text_encoding("中文内容测试".encode("gbk")) == "gbk"
    # 怪癖（现状保持）：≤3 字节的 GBK 片段被"末尾窗口"规则当作边界截断，
    # 切除后空串解码成功 → 误判为 utf-8-sig（旧实现同款；读取侧 errors="replace" 兜底）
    assert encoding.detect_text_encoding("中".encode("gbk")) == "utf-8-sig"


def test_read_binary_rejected(tmp_path):
    """首块含 NUL 字节 → 二进制拒绝文案。"""
    p = write_bytes(tmp_path / "bin.dat", b"abc\x00def")
    assert run("Read", {"file_path": str(p)}).llm_text == (
        "[错误: 检测到二进制文件（含NUL字节），Read仅支持纯文本。请使用Shell工具处理]")


def test_read_invalid_bytes_replaced(tmp_path):
    """宽松解码：无法解码的字节以替换字符呈现，不中断读取。"""
    p = write_bytes(tmp_path / "bad.txt", b"abc\xffdef\n")
    out = run("Read", {"file_path": str(p)}).llm_text
    assert out == "  1→abc\ufffddef"


def test_read_long_line_truncated(tmp_path):
    """单行超 2000 字符 → 仅保留前 2000 字符 + 单行截断后缀（N 为原始字符数）。"""
    long_line = "x" * 2500
    p = write_text(tmp_path / "long.txt", long_line + "\n")
    out = run("Read", {"file_path": str(p)}).llm_text
    assert out.startswith("  1→" + "x" * 2000)
    assert out.endswith("...[单行截断: 本行共2500字符,仅显示前2000字符]")


def test_read_limit_truncation_hint(tmp_path):
    """limit 触发截断：文件共 100 行、limit=10 → 提示显示行数与续读 offset=11。"""
    p = write_text(tmp_path / "a.txt", "".join(f"L{i}\n" for i in range(1, 101)))
    out = run("Read", {"file_path": str(p), "limit": 10}).llm_text
    lines = out.split("\n")
    assert len(lines) == 11
    assert lines[-1] == "  ... [截断: 已显示 10 行。文件共100行，剩余90行。使用 offset=11 参数可读取其余部分]"


def test_read_huge_file_hint_degraded(tmp_path, monkeypatch):
    """文件大于 20MB（以阈值调小模拟）→ 提示不含总行数与剩余行数。"""
    monkeypatch.setattr(read_module, "READ_MAX_COUNT_BYTES", 50)
    p = write_text(tmp_path / "a.txt", "".join(f"L{i}\n" for i in range(1, 31)))
    out = run("Read", {"file_path": str(p), "limit": 10}).llm_text
    assert out.split("\n")[-1] == (
        "  ... [截断: 已显示 10 行。使用 offset=11 参数可读取其余部分]")
    assert "文件共" not in out


def test_read_global_cap_by_lines(tmp_path):
    """Read 的全局上限按行截断：保留完整行并给出 offset 续读提示（非首尾保留）。

    上限取 500：Read 的按行预算为 max(500-200, 300)=300 字符，截断后总长（内容+提示）
    仍在上限内，故执行入口不再二次截断——形态可干净对照（两套截断并存见怪癖测试）。
    """
    p = write_text(tmp_path / "a.txt", "".join(f"L{i:03d}-{'x' * 15}\n" for i in range(1, 41)))
    env = make_env(max_tool_output_chars=500)
    out = run("Read", {"file_path": str(p)}, env).llm_text
    assert "已达全局输出上限(500字符≈" in out
    assert "继续读取其余部分]" in out
    assert "已保留首尾" not in out
    m = re.search(r"仅显示前(\d+)行", out)
    assert m and int(m.group(1)) >= 1
    assert "offset=" in out


def test_read_no_context_no_global_cap(tmp_path):
    """未携带工具运行时上下文（env=None）→ 不施加全局截断。"""
    p = write_text(tmp_path / "a.txt", "".join(f"L{i:03d}-{'x' * 15}\n" for i in range(1, 41)))
    out = ReadTool().execute({"file_path": str(p)}, None).llm_text
    assert out.split("\n")[-1] == "  40→L040-" + "x" * 15
    assert "已达全局输出上限" not in out


# ── 设备参数契约（Read/Edit/Write 共通）──


@pytest.mark.parametrize("device", ["dev0", " DEV0 ", "dev0  ", ""], ids=["dev0", "upper-spaces", "trailing", "empty"])
def test_device_dev0_equals_local(tmp_path, device):
    """device=dev0/" DEV0 " 等同本机：按本机读取，结果不含设备头。"""
    p = write_text(tmp_path / "a.txt", "local\n")
    out = run("Read", {"file_path": str(p), "device": device}).llm_text
    assert out == "  1→local"
    assert not out.startswith("[dev")


def test_device_normalize_rules():
    assert normalize_device("dev1") == "dev1"
    assert normalize_device(" DEV2 ") == "DEV2"
    assert normalize_device("") == ""
    assert normalize_device(None) == ""
    assert normalize_device("host1") is None


def test_device_invalid_value():
    """device=host1 → 统一错误（指导语 + 无设备提示或已连接清单）。"""
    out = run("Read", {"file_path": "x.txt", "device": "host1"}).llm_text
    assert out.startswith("[错误: 设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)")
    assert "当前无已连接设备，请先Terminal connect" in out

    remote = FakeRemote()
    out2 = run("Read", {"file_path": "x.txt", "device": "host1"}, remote=remote).llm_text
    assert "当前已连接: dev1(user@host)" in out2
    assert device_hint(remote) == (
        "设备引用统一用devN编号(dev0=本机, dev1..devn=被控设备)。当前已连接: dev1(user@host)")


def test_remote_read_success_has_device_header():
    """device=dev1 且远程读取成功 → 结果首行为 [dev1] {file_path}。"""
    remote = FakeRemote(read_result="  1→remote-line")
    out = run("Read", {"file_path": "/tmp/a.txt", "device": "dev1"}, remote=remote).llm_text
    assert out == "[dev1] /tmp/a.txt\n  1→remote-line"
    assert remote.calls == [("read", "/tmp/a.txt", 0, 2000, "dev1")]


def test_remote_read_error_has_no_header():
    """远程读取返回以 `[` 开头的错误文本 → 原样返回，不附加设备头。"""
    remote = FakeRemote(read_result="[错误: 远程文件不存在: /tmp/a.txt]")
    out = run("Read", {"file_path": "/tmp/a.txt", "device": "dev1"}, remote=remote).llm_text
    assert out == "[错误: 远程文件不存在: /tmp/a.txt]"


def test_remote_default_port_returns_no_session_error():
    """未装配远程实现（缺省端口）→ 与「无任何 SSH 会话」一致的错误文案。"""
    out = run("Read", {"file_path": "/tmp/a.txt", "device": "dev1"}).llm_text
    assert out == "[错误: 无可用SSH会话，请先Terminal connect建立连接]"


# ═══════════════════════════════════════════════════════════════
# 3. Glob
# ═══════════════════════════════════════════════════════════════


def _tree(tmp_path: Path) -> Path:
    """构造标准测试树（含子目录、隐藏项、忽略目录、多扩展名）。"""
    write_text(tmp_path / "a.py", "a\n")
    write_text(tmp_path / "b.txt", "b\n")
    write_text(tmp_path / "src" / "c.py", "c\n")
    write_text(tmp_path / "src" / "deep" / "d.py", "d\n")
    write_text(tmp_path / "src" / "e.md", "e\n")
    write_text(tmp_path / ".hidden" / "h.py", "h\n")
    write_text(tmp_path / ".narnat" / "conf" / "settings.json", "{}\n")
    write_text(tmp_path / "node_modules" / "pkg" / "n.py", "n\n")
    return tmp_path


def test_glob_recursive_match(tmp_path):
    """pattern="*.py" → 所有层级的 .py 文件与目录参与匹配（不要求目录前缀）。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": "*.py", "path": str(root)}).llm_text
    found = out.split("\n")
    assert "a.py" in found
    assert any(f.endswith("c.py") and "src" in f for f in found)
    assert any(f.endswith("d.py") for f in found)
    assert ".hidden" not in out  # 隐藏目录默认不遍历


def test_glob_brace_multi_patterns(tmp_path):
    """pattern="*.{txt,md}" 等价于 *.txt 与 *.md 的模式合并去重。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": "*.{txt,md}", "path": str(root)}).llm_text
    names = out.split("\n")
    assert "b.txt" in names
    assert any(n.endswith("e.md") for n in names)
    assert len([n for n in names if n == "b.txt"]) == 1


def test_glob_absolute_pattern(tmp_path):
    """绝对路径模式 → 按绝对路径搜索，结果以绝对路径形态输出（分隔符按平台习惯）。"""
    root = _tree(tmp_path)
    pattern = str(root / "src" / "**" / "*.py")
    out = run("Glob", {"pattern": pattern}).llm_text
    assert str(root) in out.replace("/", "\\")  # 输出分隔符为平台习惯（Windows 为 \）
    assert any(line.endswith("c.py") for line in out.split("\n"))


@pytest.mark.parametrize("pattern", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_glob_empty_pattern(pattern):
    """pattern 为空或纯空白 → [错误: pattern不能为空]。"""
    assert run("Glob", {"pattern": pattern}).llm_text == "[错误: pattern不能为空]"


@pytest.mark.parametrize("bad", [0, -5, "abc"], ids=["zero", "negative", "non-int"])
def test_glob_bad_max_results(bad):
    """max_results 非整数或 ≤0 → [错误: max_results需为正整数]。"""
    assert run("Glob", {"pattern": "*", "max_results": bad}).llm_text == "[错误: max_results需为正整数]"


def test_glob_max_results_hard_limit(tmp_path):
    """max_results 传 80000 → 按 50000 处理（不报错）。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": "*.py", "path": str(root), "max_results": 80000}).llm_text
    assert "max_results需为正整数" not in out
    assert glob_module.MAX_HARD_LIMIT == 50_000


def test_glob_sorted_by_mtime_desc(tmp_path):
    """两文件均匹配 → 修改时间晚者在前。"""
    old = write_text(tmp_path / "old.txt", "old\n")
    new = write_text(tmp_path / "new.txt", "new\n")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    out = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}).llm_text.split("\n")
    assert out[0] == "new.txt" and out[1] == "old.txt"

    # 修改时间相同时按路径升序
    os.utime(old, (3_000_000, 3_000_000))
    os.utime(new, (3_000_000, 3_000_000))
    out2 = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}).llm_text.split("\n")
    assert out2[:2] == ["new.txt", "old.txt"]


def test_glob_truncation_hint(tmp_path):
    """匹配项总数超过 max_results → 末行给出已截断提示。"""
    for i in range(8):
        write_text(tmp_path / f"f{i}.txt", "x\n")
    out = run("Glob", {"pattern": "*.txt", "path": str(tmp_path), "max_results": 3}).llm_text
    lines = out.split("\n")
    assert len(lines) == 4
    assert lines[-1] == ("...[已截断: 共8个匹配项, 当前显示按修改时间最近的3个。"
                         "增大max_results可获取完整列表]")


def test_glob_no_match_with_hidden_hint(tmp_path):
    """无匹配 → [无匹配（搜索目录: root）] + 隐藏文件提示（默认跳过时）。"""
    write_text(tmp_path / ".hidden" / "x.py", "x\n")
    out = run("Glob", {"pattern": "*.py", "path": str(tmp_path)}).llm_text
    assert out.startswith(f"[无匹配（搜索目录: {tmp_path}）]")
    assert "隐藏文件默认跳过" in out


def test_glob_missing_root_dir(tmp_path):
    """path 指向不存在的目录 → [错误: 目录不存在: {root}（当前目录: {cwd}）]。"""
    missing = tmp_path / "nope"
    out = run("Glob", {"pattern": "*", "path": str(missing)}).llm_text
    assert out == f"[错误: 目录不存在: {missing}（当前目录: {os.getcwd()}）]"


def test_glob_missing_static_dir(tmp_path):
    """模式中的静态目录不存在 → [错误: 目录不存在: {静态目录}（当前目录: {cwd}）]。"""
    out = run("Glob", {"pattern": "nosuchdir/*.py", "path": str(tmp_path)}).llm_text
    assert out == "[错误: 目录不存在: nosuchdir（当前目录: " + os.getcwd() + "）]"


def test_glob_ignore_dirs_pruned(tmp_path):
    """上下文忽略目录 ["node_modules"] → 该目录不被遍历、不出现在结果中。"""
    root = _tree(tmp_path)
    env = make_env(ignore_dirs=["node_modules"])
    out = run("Glob", {"pattern": "*.py", "path": str(root)}, env).llm_text
    assert "n.py" not in out
    assert "node_modules" not in out
    env2 = make_env()
    out2 = run("Glob", {"pattern": "*.py", "path": str(root)}, env2).llm_text
    assert any("n.py" in line for line in out2.split("\n"))


def test_glob_hidden_skipped_by_default(tmp_path):
    """pattern="*.py" 且存在 .hidden/a.py → .hidden 不被遍历。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": "*.py", "path": str(root)}).llm_text
    assert ".hidden" not in out
    assert not any("h.py" in line for line in out.split("\n"))


def test_glob_dot_component_enables_hidden(tmp_path):
    """pattern=".narnat/**/*.json" → 隐藏目录被遍历，其下文件进入结果。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": ".narnat/**/*.json", "path": str(root)}).llm_text
    assert "settings.json" in out
    assert ".narnat" in out


def test_glob_directory_participates(tmp_path):
    """目录参与匹配且无尾斜杠（pattern="src" 命中目录 src）。"""
    root = _tree(tmp_path)
    out = run("Glob", {"pattern": "src", "path": str(root)}).llm_text
    assert "src" in out.split("\n")
    assert not any(line.endswith("/") for line in out.split("\n"))


@pytest.mark.parametrize("pattern,expect", [
    ("?.txt", ["b.txt"]),
    ("[ab].txt", ["b.txt"]),
    ("[!b].txt", []),
    ("*.{1..3}", []),
    ("b.{txt,md}", ["b.txt"]),
], ids=["question", "charclass", "negate", "range", "brace-alt"])
def test_glob_pattern_syntax(tmp_path, pattern, expect):
    """模式语法：?、[abc]、[!abc]、{1..3}、花括号多模式（含转义与字面降级）。"""
    write_text(tmp_path / "b.txt", "b\n")
    write_text(tmp_path / "z.md", "z\n")
    out = run("Glob", {"pattern": pattern, "path": str(tmp_path)}).llm_text
    for name in expect:
        assert name in out
    if not expect:
        assert out.startswith("[无匹配")


def test_glob_literal_braces_and_escape(tmp_path):
    """不含顶层分隔符的花括号保留字面；\\{ 转义为字面 { 搜索；展开结果上限 100。"""
    assert glob_module.expand_braces("a{b}c") == ["a{b}c"]
    assert glob_module.expand_braces("\\{a,b\\}") == ["\\{a,b\\}"]
    assert glob_module.unescape_braces("\\{a,b\\}") == "{a,b}"
    assert glob_module.expand_braces("{a,b}") == ["a", "b"]
    # `..` 区间触发展开但不生成序列（现状保持）：花括号被剥除、`1..2` 作字面量
    assert glob_module.expand_braces("{a,b}{1..2}") == ["a1..2", "b1..2"]
    assert len(glob_module.expand_braces("{a,b" + "".join(f",c{i}" for i in range(200)) + "}")) <= 100


def test_glob_hidden_skip_disabled_note(tmp_path):
    """「隐藏文件被跳过」提示仅在无匹配时输出；有结果时不提示。"""
    write_text(tmp_path / "a.py", "a\n")
    out = run("Glob", {"pattern": "*.py", "path": str(tmp_path)}).llm_text
    assert "隐藏文件默认跳过" not in out


# ═══════════════════════════════════════════════════════════════
# 4. Grep
# ═══════════════════════════════════════════════════════════════


def test_grep_defaults_case_sensitive(tmp_path, monkeypatch):
    """仅传 pattern：在当前目录递归搜索、不忽略大小写、无上下文行、head_limit=30。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.txt", "HELLO world\nhello again\n")
    out = run("Grep", {"pattern": "hello"}).llm_text
    assert out == "a.txt (1处):\n2:hello again"


def test_grep_cli_style_aliases(tmp_path, monkeypatch):
    """-i=true 与 -C=2 → 行为等同 i=true、C=2（A/B 随 C 生效）。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.txt", "one\nHELLO\ntwo\nthree\nfour\n")
    alias_out = run("Grep", {"pattern": "hello", "-i": "true", "-C": 2}).llm_text
    plain_out = run("Grep", {"pattern": "hello", "i": True, "C": 2}).llm_text
    assert alias_out == plain_out
    assert alias_out.split("\n")[:5] == ["a.txt (1处):", "1-one", "2:HELLO", "3-two", "4-three"]


def test_grep_head_limit_alias_overrides(tmp_path, monkeypatch):
    """-head_limit 别名无条件覆盖 head_limit 参数（怪癖）。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.txt", "hit\n" * 5)
    out = run("Grep", {"pattern": "hit", "head_limit": 5, "-head_limit": 2}).llm_text
    assert "该文件其余3处匹配未展开" in out


def test_grep_unknown_param():
    """-x=1 → 参数错误提示（列出有效参数）。"""
    out = run("Grep", {"pattern": "x", "-x": 1})
    assert out.llm_text == ("[错误: 工具参数错误(Grep): 收到未知参数 '-x'。"
                            "Grep 的有效参数: pattern, path, glob, i, A, B, C, head_limit]")
    assert out.is_error is True


def test_grep_invalid_regex():
    """pattern="[" → [错误: 非法正则: {异常文本}]。"""
    out = run("Grep", {"pattern": "["}).llm_text
    assert out.startswith("[错误: 非法正则: ")


def test_grep_pattern_too_long():
    """pattern 长度 >4096 → 拒绝执行以防 ReDoS。"""
    out = run("Grep", {"pattern": "a" * 4097}).llm_text
    assert out == "[错误: 正则表达式过长（>4096字符），拒绝执行以防ReDoS]"


@pytest.mark.parametrize("value,expected_hit", [("false", False), ("true", True), (False, False), (True, True)])
def test_grep_string_boolean(tmp_path, monkeypatch, value, expected_hit):
    """i 传字符串布尔："false" 按不忽略大小写处理。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.txt", "HELLO\n")
    out = run("Grep", {"pattern": "hello", "i": value}).llm_text
    assert ("HELLO" in out) is expected_hit


@pytest.mark.parametrize("bad", [0, -2], ids=["zero", "negative"])
def test_grep_bad_head_limit(bad):
    """head_limit 为非正数 → [错误: head_limit需为正整数]。"""
    assert run("Grep", {"pattern": "x", "head_limit": bad}).llm_text == "[错误: head_limit需为正整数]"


def test_grep_non_int_context():
    """A/B/C/head_limit 非整数 → [错误: A/B/C/head_limit需为整数]。"""
    assert run("Grep", {"pattern": "x", "A": "abc"}).llm_text == "[错误: A/B/C/head_limit需为整数]"
    assert run("Grep", {"pattern": "x", "head_limit": "x2"}).llm_text == "[错误: A/B/C/head_limit需为整数]"


def test_grep_group_header_and_match_lines(tmp_path, monkeypatch):
    """src/a.py 有 3 处命中 → 表头 `(3处):` + 逐行 `{行号}:{内容}`（不连续块加 `--`）。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "src" / "a.py", "hit1\nmiss\nhit2\nhit3\n")
    out = run("Grep", {"pattern": "hit", "path": "src"}).llm_text
    lines = out.split("\n")
    assert lines[0] == "a.py (3处):"
    assert lines[1:] == ["1:hit1", "--", "3:hit2", "4:hit3"]


def test_grep_context_lines_and_separator(tmp_path):
    """A=1 且两处命中间隔较大 → 上下文行 `{行号}-{内容}`，两块之间 `--`。"""
    write_text(tmp_path / "a.txt", "\n".join(["hit1", "after1", "x", "y", "z", "hit2"]) + "\n")
    out = run("Grep", {"pattern": "hit", "path": str(tmp_path), "A": 1}).llm_text
    lines = out.split("\n")
    assert lines == ["a.txt (2处):", "1:hit1", "2-after1", "--", "6:hit2"]


def test_grep_count_not_affected_by_budget(tmp_path):
    """单文件命中 50 处但 head_limit 只允许部分展开 → 表头 (50处) + 未展开提示。"""
    write_text(tmp_path / "a.txt", "hit\n" * 50)
    out = run("Grep", {"pattern": "hit", "path": str(tmp_path), "head_limit": 3}).llm_text
    assert out.split("\n")[0] == "a.txt (50处):"
    assert ("...[该文件其余47处匹配未展开: 已达head_limit(3)。增大head_limit或缩小搜索范围查看其余]"
            in out)


def test_grep_budget_exhausted_to_list(tmp_path):
    """预算耗尽 → 后续文件仅列清单行 `{标签} ({N}处)` + 末尾「已截断」提示。"""
    write_text(tmp_path / "a.txt", "hit\nhit\n")
    write_text(tmp_path / "b.txt", "hit\nhit\n")
    out = run("Grep", {"pattern": "hit", "path": str(tmp_path), "head_limit": 1}).llm_text
    lines = out.split("\n")
    assert lines[0] == "a.txt (2处):"
    assert "b.txt (2处)" in lines
    assert lines[-1] == ("...[已截断: 达到head_limit(1)，剩余文件仅列文件名（含计数）。"
                         "增大head_limit可展开更多匹配行]")


def test_grep_unlimited_budget(tmp_path):
    """head_limit 显式传 null → 全部展开，不出现清单行与「已截断」提示。"""
    write_text(tmp_path / "a.txt", "hit\nhit\n")
    write_text(tmp_path / "b.txt", "hit\n")
    out = run("Grep", {"pattern": "hit", "path": str(tmp_path), "head_limit": None}).llm_text
    assert "a.txt (2处):" in out and "b.txt (1处):" in out
    assert out.count("hit") == 3
    assert "已截断" not in out


def test_grep_skips_binary_and_large_files(tmp_path, monkeypatch):
    """含 NUL 的二进制文件与超大文件均静默跳过（阈值调小以构造场景）。"""
    write_text(tmp_path / "ok.txt", "needle\n")
    write_bytes(tmp_path / "bin.dat", b"needle\x00")
    write_text(tmp_path / "big.txt", "needle\n" * 3)
    monkeypatch.setattr(grep_module, "MAX_FILE_SIZE", 10)
    out = run("Grep", {"pattern": "needle", "path": str(tmp_path)}).llm_text
    assert "ok.txt (1处):" in out
    assert "bin.dat" not in out
    assert "big.txt" not in out
    assert "错误" not in out


def test_grep_missing_path_warning(tmp_path, monkeypatch):
    """path=["src", "missing"] 且 src 有命中 → 首行警告，其后为命中结果。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "src" / "a.txt", "needle\n")
    out = run("Grep", {"pattern": "needle", "path": ["src", "missing"]}).llm_text
    lines = out.split("\n")
    assert lines[0] == "路径不存在，已跳过: missing"
    assert "a.txt (1处):" in out


def test_grep_all_paths_invalid(tmp_path, monkeypatch):
    """全部路径无效 → [无匹配]（前置警告行）。"""
    monkeypatch.chdir(tmp_path)
    out = run("Grep", {"pattern": "x", "path": ["gone1", "gone2"]}).llm_text
    assert out == "路径不存在，已跳过: gone1、gone2\n[无匹配]"


def test_grep_no_match_wording(tmp_path, monkeypatch):
    """无命中文案随目标数量变化：单文件 / 单目录 / 多目标（>3 以「等」收尾）。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.txt", "content\n")
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    assert run("Grep", {"pattern": "zzz", "path": "a.txt"}).llm_text == "[无匹配]"
    assert run("Grep", {"pattern": "zzz", "path": "d1"}).llm_text == "[无匹配（搜索目录: d1）]"
    out = run("Grep", {"pattern": "zzz", "path": ["d1", "d2"]}).llm_text
    assert out == "[无匹配（搜索范围: d1、d2）]"
    (tmp_path / "d3").mkdir()
    (tmp_path / "d4").mkdir()
    out4 = run("Grep", {"pattern": "zzz", "path": ["d1", "d2", "d3", "d4"]}).llm_text
    assert out4 == "[无匹配（搜索范围: d1、d2、d3等）]"


def test_grep_ignore_dirs_pruned(tmp_path):
    """上下文忽略目录含 node_modules → 该目录下文件不被扫描。"""
    write_text(tmp_path / "node_modules" / "a.js", "needle\n")
    write_text(tmp_path / "src" / "a.js", "needle\n")
    env = make_env(ignore_dirs=["node_modules"])
    out = run("Grep", {"pattern": "needle", "path": str(tmp_path)}, env).llm_text
    assert "node_modules" not in out
    assert "a.js (1处):" in out
    out2 = run("Grep", {"pattern": "needle", "path": str(tmp_path)}, make_env()).llm_text
    assert "node_modules" in out2


def test_grep_multi_path_order_and_dedup(tmp_path, monkeypatch):
    """多路径按给定顺序输出；同一文件被多个路径项覆盖时只扫描一次。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "one.txt", "needle\n")
    write_text(tmp_path / "two.txt", "needle\n")
    out = run("Grep", {"pattern": "needle", "path": ["two.txt", "one.txt", "one.txt"]}).llm_text
    assert out.index("two.txt (1处):") < out.index("one.txt (1处):")
    assert out.count("one.txt (1处):") == 1


def test_grep_glob_filter(tmp_path, monkeypatch):
    """glob 过滤（含花括号多模式）：仅命中的文件被扫描。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "a.py", "needle\n")
    write_text(tmp_path / "b.txt", "needle\n")
    write_text(tmp_path / "c.c", "needle\n")
    out = run("Grep", {"pattern": "needle", "path": ".", "glob": "*.{py,c}"}).llm_text
    assert "a.py (1处):" in out and "c.c (1处):" in out
    assert "b.txt" not in out


def test_grep_gbk_file_matches(tmp_path):
    """GBK 中文文件按探测编码解码后中文 pattern 可命中。"""
    write_bytes(tmp_path / "gbk.txt", "中文内容\n".encode("gbk"))
    out = run("Grep", {"pattern": "中文", "path": str(tmp_path)}).llm_text
    assert out == "gbk.txt (1处):\n1:中文内容"


def test_grep_parallel_path_matches_serial(tmp_path):
    """目录内文件数 ≥10 走并行扫描：结果顺序仍按相对路径字典序。"""
    for i in range(12):
        write_text(tmp_path / f"f{i:02d}.txt", "needle\n")
    out = run("Grep", {"pattern": "needle", "path": str(tmp_path), "head_limit": None}).llm_text
    labels = [line for line in out.split("\n") if line.endswith("(1处):")]
    assert labels == [f"f{i:02d}.txt (1处):" for i in range(12)]


def test_grep_context_C_overrides_AB(tmp_path):
    """C>0 时覆盖 A 与 B 为同一值。"""
    write_text(tmp_path / "a.txt", "one\ntwo\nhit\nfour\nfive\n")
    out = run("Grep", {"pattern": "hit", "path": str(tmp_path), "A": 0, "B": 0, "C": 1}).llm_text
    assert out.split("\n") == ["a.txt (1处):", "2-two", "3:hit", "4-four"]


# ═══════════════════════════════════════════════════════════════
# 5. Edit
# ═══════════════════════════════════════════════════════════════


def test_edit_unique_replacement(tmp_path):
    """唯一匹配替换 → [已替换1处] 及其后的 unified diff。"""
    p = write_text(tmp_path / "x.txt", "a\nold\nb\n")
    out = run("Edit", {"file_path": str(p), "old_string": "old", "new_string": "new"})
    lines = out.llm_text.split("\n")
    assert lines[0] == "[已替换1处]"
    assert lines[1] == "--- a/x.txt" and lines[2] == "+++ b/x.txt"
    assert "@@" in lines[3]
    assert "-old" in lines and "+new" in lines
    assert p.read_text(encoding="utf-8") == "a\nnew\nb\n"


def test_edit_multiple_matches_rejected(tmp_path):
    """old_string 出现 3 次且 replace_all 未设 → 唯一性错误，文件不被写入。"""
    p = write_text(tmp_path / "x.txt", "dup\ndup\ndup\n")
    out = run("Edit", {"file_path": str(p), "old_string": "dup", "new_string": "x"})
    assert out.llm_text == ("[错误: 找到3处匹配，old_string不唯一。"
                            "请扩大上下文使其唯一，或设置replace_all=True]")
    assert p.read_text(encoding="utf-8") == "dup\ndup\ndup\n"


def test_edit_replace_all(tmp_path):
    """old_string 出现 3 次且 replace_all=true → 三处全部替换。"""
    p = write_text(tmp_path / "x.txt", "dup\ndup\ndup\n")
    out = run("Edit", {"file_path": str(p), "old_string": "dup", "new_string": "x",
                       "replace_all": True})
    assert out.llm_text.startswith("[已替换3处]\n")
    assert p.read_text(encoding="utf-8") == "x\nx\nx\n"


def test_edit_similar_lines_hint(tmp_path):
    """未匹配 → 相似行提示（最多 3 条、含行号与相似度百分比）。"""
    p = write_text(tmp_path / "x.txt", "\n".join(
        ["value = 123", "value = 124", "value = 125", "value = 126", "totally different"]) + "\n")
    out = run("Edit", {"file_path": str(p), "old_string": "value = 999", "new_string": "x"})
    lines = out.llm_text.split("\n")
    assert lines[0] == "[错误: 未找到匹配文本。请先Read确认文件内容。]"
    assert lines[1] == "相似行（供参考）:"
    hints = [ln for ln in lines[2:] if "相似度" in ln]
    assert 1 <= len(hints) <= 3
    assert re.match(r"^  行\d+: .* \(相似度\d+%\)$", hints[0])
    assert "value = " in hints[0]


def test_edit_no_similar_lines(tmp_path):
    """完全无相似行 → 仅错误首行（不输出空提示块）。"""
    p = write_text(tmp_path / "x.txt", "alpha\nbeta\n")
    out = run("Edit", {"file_path": str(p), "old_string": "zzzzzzzzzz", "new_string": "x"})
    assert out.llm_text == "[错误: 未找到匹配文本。请先Read确认文件内容。]\n"


@pytest.mark.parametrize("value", ["false", "0", "no", "off", False], ids=["str-false", "str-zero", "str-no", "str-off", "bool"])
def test_edit_string_boolean_replace_all(tmp_path, value):
    """replace_all 传 "false" 等 → 按「只替换唯一匹配」处理（多处匹配报唯一性错误）。"""
    p = write_text(tmp_path / "x.txt", "dup\ndup\n")
    out = run("Edit", {"file_path": str(p), "old_string": "dup", "new_string": "x",
                       "replace_all": value})
    assert out.llm_text.startswith("[错误: 找到2处匹配，old_string不唯一。")
    assert p.read_text(encoding="utf-8") == "dup\ndup\n"


@pytest.mark.parametrize("value", ["true", "1", "yes", True], ids=["str-true", "str-one", "str-yes", "bool"])
def test_edit_string_boolean_truthy(tmp_path, value):
    """replace_all 真假值（含字符串布尔）都按语义解析，不因 bool("false") 恒真而误判。"""
    p = write_text(tmp_path / "x.txt", "dup\ndup\n")
    out = run("Edit", {"file_path": str(p), "old_string": "dup", "new_string": "x",
                       "replace_all": value})
    assert out.llm_text.startswith("[已替换2处]")
    assert to_bool(value) is True


def test_edit_without_prior_read(tmp_path):
    """会话中未读取过目标文件便直接调用 Edit 且精确匹配 → 编辑正常执行。"""
    p = write_text(tmp_path / "fresh.txt", "content here\n")
    tool = EditTool()  # 全新实例，无任何已读状态
    out = tool.execute({"file_path": str(p), "old_string": "content here", "new_string": "changed"}, None)
    assert out.llm_text.startswith("[已替换1处]")
    assert p.read_text(encoding="utf-8") == "changed\n"


def test_edit_old_string_empty(tmp_path):
    """old_string 为空（含缺省）→ [错误: old_string不能为空]。"""
    p = write_text(tmp_path / "x.txt", "abc\n")
    assert run("Edit", {"file_path": str(p), "old_string": ""}).llm_text == "[错误: old_string不能为空]"
    assert run("Edit", {"file_path": str(p)}).llm_text == "[错误: old_string不能为空]"


def test_edit_missing_file(tmp_path):
    """file_path 不是文件 → [错误: 文件不存在: {路径}，如需创建请用Write工具]。"""
    missing = tmp_path / "nope.txt"
    out = run("Edit", {"file_path": str(missing), "old_string": "a", "new_string": "b"})
    assert out.llm_text == f"[错误: 文件不存在: {missing}，如需创建请用Write工具]"


def test_edit_new_string_default_deletes(tmp_path):
    """new_string 缺省为空串 → 删除匹配文本。"""
    p = write_text(tmp_path / "x.txt", "keep\ndrop\nkeep2\n")
    out = run("Edit", {"file_path": str(p), "old_string": "drop\n"})
    assert out.llm_text.startswith("[已替换1处]")
    assert p.read_text(encoding="utf-8") == "keep\nkeep2\n"


def test_edit_keeps_crlf_and_bom(tmp_path):
    """编辑 CRLF 换行且带 BOM 的 UTF-8 文件 → 写回仍 CRLF 且仍带 BOM。"""
    p = write_bytes(tmp_path / "x.txt", BOM_UTF8 + "line1\r\nline2\r\nline3\r\n".encode("utf-8"))
    out = run("Edit", {"file_path": str(p), "old_string": "line2", "new_string": "changed"})
    assert out.llm_text.startswith("[已替换1处]")
    data = p.read_bytes()
    assert data.startswith(BOM_UTF8)
    assert data == BOM_UTF8 + "line1\r\nchanged\r\nline3\r\n".encode("utf-8")
    assert b"\r\n" in data


def test_edit_lf_pattern_hits_crlf_file(tmp_path):
    """文件为 CRLF 风格、old_string 以 LF 书写 → 归一后匹配成功。"""
    p = write_bytes(tmp_path / "x.txt", "alpha\r\nbeta\r\ngamma\r\n".encode("utf-8"))
    out = run("Edit", {"file_path": str(p), "old_string": "alpha\nbeta", "new_string": "alpha\nBETA"})
    assert out.llm_text.startswith("[已替换1处]")
    assert p.read_bytes() == "alpha\r\nBETA\r\ngamma\r\n".encode("utf-8")


def test_edit_keeps_gbk_encoding(tmp_path):
    """探测为 GBK 时按 GBK 写回（保持原编码）。"""
    p = write_bytes(tmp_path / "x.txt", "中文内容\n".encode("gbk"))
    out = run("Edit", {"file_path": str(p), "old_string": "中文", "new_string": "汉字"})
    assert out.llm_text.startswith("[已替换1处]")
    assert p.read_bytes() == "汉字内容\n".encode("gbk")


def test_edit_gbk_write_unencodable(tmp_path):
    """GBK 文件写入 emoji → 编码无法表示的错误（文件已被清空的怪癖保持）。"""
    p = write_bytes(tmp_path / "x.txt", "中文内容\n".encode("gbk"))
    out = run("Edit", {"file_path": str(p), "old_string": "中文", "new_string": "🙂"})
    assert out.llm_text == (
        f"[错误: 新内容包含文件编码(gbk)无法表示的字符，写入失败: {p}]")
    assert p.read_bytes() == b""  # 兼容怪癖：先截断后写入


def test_edit_unstrict_decodable_reports_binary(tmp_path):
    """非 UTF-8/GBK 且无法严格解码 → 实际返回二进制拒绝文案（旧实现等价行为）。

    spec 场景「非 UTF-8/GBK 编码拒绝」描述的是文档性分支：旧实现中
    `UnicodeDecodeError` 是 `ValueError` 子类，被先匹配的二进制分支吸收（该分支不可达）。
    本实现按旧实现逐行保持（design D11：除明示修复项外零差异），偏差已在 T3.4 报告登记
    （旧实现实测：UTF-16/BOM/Latin-1/非法字节流均返回本文案）。
    """
    raw = b"\xff\xfe" + "中文内容".encode("utf-16-le")
    p = write_bytes(tmp_path / "x.txt", raw)
    out = run("Edit", {"file_path": str(p), "old_string": "x", "new_string": "y"})
    assert out.llm_text == "[错误: 检测到二进制文件（含NUL字节），Edit仅支持文本文件。请使用Shell工具处理]"
    assert p.read_bytes() == raw


def test_edit_binary_rejected(tmp_path):
    """含 NUL 字节的文件 → 二进制拒绝文案。"""
    p = write_bytes(tmp_path / "x.bin", b"abc\x00def")
    out = run("Edit", {"file_path": str(p), "old_string": "abc", "new_string": "x"})
    assert out.llm_text == "[错误: 检测到二进制文件（含NUL字节），Edit仅支持文本文件。请使用Shell工具处理]"


def test_edit_no_change_hint(tmp_path):
    """old_string 与 new_string 相同 → 空编辑提示（文件已照常写盘）。"""
    p = write_text(tmp_path / "x.txt", "same\n")
    out = run("Edit", {"file_path": str(p), "old_string": "same", "new_string": "same"})
    assert out.llm_text == "[提示: 新旧内容相同，文件无实质修改。请确认new_string是否漏写]"
    assert "[无差异]" in out.ui_text
    assert p.read_text(encoding="utf-8") == "same\n"


def test_edit_remote_branch():
    """device=devN → 远程编辑端口接管（返回其文本与着色 diff）。"""
    remote = FakeRemote()
    out = run("Edit", {"file_path": "/tmp/x.txt", "old_string": "a", "new_string": "b",
                       "replace_all": "true", "device": "dev1"}, remote=remote)
    assert out.llm_text == "[dev1] [已替换1处]"
    assert out.ui_text == "colored-diff"
    assert remote.calls == [("edit", "/tmp/x.txt", "a", "b", True, "dev1")]


def test_edit_device_invalid(tmp_path):
    """非法设备值 → 统一设备错误（先于本地文件检查）。"""
    out = run("Edit", {"file_path": "nope.txt", "old_string": "a", "device": "host9"})
    assert out.llm_text.startswith("[错误: 设备引用统一用devN编号")


# ═══════════════════════════════════════════════════════════════
# 6. Write
# ═══════════════════════════════════════════════════════════════


def test_write_new_file(tmp_path):
    """新建文件（含缺失父目录），返回 [已写入: {路径} ({N}字节)]，无 diff。"""
    p = tmp_path / "deep" / "nested" / "new.txt"
    out = run("Write", {"file_path": str(p), "content": "hello\n中文\n"})
    assert out.llm_text == f"[已写入: {p} ({len('hello\n中文\n'.encode('utf-8'))}字节)]"
    assert out.ui_text is None
    assert p.read_bytes() == "hello\n中文\n".encode("utf-8")


def test_write_overwrite_returns_diff(tmp_path):
    """覆盖既有文本文件且内容有变化 → 回执 + unified diff。"""
    p = write_text(tmp_path / "x.txt", "old\n")
    out = run("Write", {"file_path": str(p), "content": "new\n"})
    assert out.llm_text.startswith(f"[已写入: {p} (4字节)]\n")
    assert "--- a/x.txt" in out.llm_text and "+new" in out.llm_text
    assert out.ui_text and "\x1b[" in out.ui_text


def test_write_identical_bytes(tmp_path):
    """新内容与旧文件字节级一致 → 无实质修改提示。"""
    p = write_text(tmp_path / "x.txt", "same\n")
    out = run("Write", {"file_path": str(p), "content": "same\n"})
    assert out.llm_text == "[提示: 新旧内容完全相同（字节级一致），文件无实质修改。请确认content是否漏写]"


def test_write_same_text_bytes_changed(tmp_path):
    """覆盖 CRLF 文件且正文相同、行尾不同 → 字节层面变化提示（含行尾符描述）。"""
    p = write_bytes(tmp_path / "x.txt", b"a\r\nb\r\n")
    out = run("Write", {"file_path": str(p), "content": "a\nb\n"})
    assert out.llm_text == "[提示: 文件已写入，正文内容相同，但字节层面有变化（行尾符 CRLF→LF）]"
    assert p.read_bytes() == b"a\nb\n"


def test_write_trailing_newline_change(tmp_path):
    """末尾换行增减 → 「末尾换行已添加/已移除」描述。"""
    p = write_bytes(tmp_path / "x.txt", b"a\nb")
    out = run("Write", {"file_path": str(p), "content": "a\nb\n"})
    assert "末尾换行已添加" in out.llm_text


def test_write_always_utf8(tmp_path):
    """覆盖 GBK 既有文件 → 写盘结果为 UTF-8（不保持原编码）+ 字节变化提示。"""
    p = write_bytes(tmp_path / "x.txt", "中文内容\n".encode("gbk"))
    out = run("Write", {"file_path": str(p), "content": "中文内容\n"})
    assert out.llm_text == (
        "[提示: 文件已写入，正文内容相同，但字节层面有变化（"
        "换行符以外的字节表示变化（9→13字节，如编码或BOM差异））]")
    assert p.read_bytes() == "中文内容\n".encode("utf-8")


def test_write_directory_target(tmp_path):
    """file_path 指向目录 → [错误: {路径} 是目录，请使用正确的文件路径]。"""
    d = tmp_path / "adir"
    d.mkdir()
    out = run("Write", {"file_path": str(d), "content": "x"})
    assert out.llm_text == f"[错误: {d} 是目录，请使用正确的文件路径]"


def test_write_binary_old_file(tmp_path):
    """旧文件为二进制（首块含 NUL）→ 不做「无实质修改」判定，直接回执。"""
    p = write_bytes(tmp_path / "x.dat", b"\x00\x01\x02abc")
    out = run("Write", {"file_path": str(p), "content": "text"})
    assert out.llm_text == f"[已写入: {p} (4字节)]"
    assert "无实质修改" not in out.llm_text


def test_write_empty_content(tmp_path):
    """content 为空串 → 写空文件，0 字节。"""
    p = tmp_path / "empty.txt"
    out = run("Write", {"file_path": str(p), "content": ""})
    assert out.llm_text == f"[已写入: {p} (0字节)]"
    assert p.read_bytes() == b""


def test_write_remote_branch():
    """device=devN → 远程写入端口接管。"""
    remote = FakeRemote()
    out = run("Write", {"file_path": "/tmp/x.txt", "content": "data", "device": "dev2"}, remote=remote)
    assert out.llm_text == "[dev2] [已写入(远程): /tmp/x.txt (4字节)]"
    assert out.ui_text == "colored-diff"
    assert remote.calls == [("write", "/tmp/x.txt", "data", "dev2")]


def test_write_device_invalid():
    out = run("Write", {"file_path": "x.txt", "content": "y", "device": "host1"})
    assert out.llm_text.startswith("[错误: 设备引用统一用devN编号")


def test_write_missing_content_param(tmp_path):
    """缺少必填参数 content → 执行入口的中文提示。"""
    out = run("Write", {"file_path": str(tmp_path / "x.txt")})
    assert out.llm_text == ("[错误: 工具参数错误(Write): 缺少必填参数: content。"
                            "Write 的有效参数: file_path, content, device]")


# ═══════════════════════════════════════════════════════════════
# 7. 返回形态与 diff 着色
# ═══════════════════════════════════════════════════════════════


def test_edit_returns_text_and_colored_diff(tmp_path):
    """Edit 成功 → 执行入口得到「文本结果 + 着色 diff」（ui_text 含 ANSI 且保留行首符号）。"""
    p = write_text(tmp_path / "x.txt", "old\n")
    out = run("Edit", {"file_path": str(p), "old_string": "old", "new_string": "new"})
    assert out.ui_text and "\x1b[" in out.ui_text
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.ui_text)
    assert plain == out.llm_text.split("\n", 1)[1]
    assert "-old" in plain and "+new" in plain


def test_read_returns_text_only(tmp_path):
    """Read 成功 → 执行入口得到空 diff（ui_text 为 None），终端不额外展示差分。"""
    p = write_text(tmp_path / "x.txt", "x\n")
    assert run("Read", {"file_path": str(p)}).ui_text is None
    assert run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}).ui_text is None
    assert run("Grep", {"pattern": "x", "path": str(p)}).ui_text is None


def test_quiet_mode_diff_skip_is_consumer_side(tmp_path):
    """静默模式跳过 diff 属消费方（UI）职责：工具始终返回 ui_text，不感知展示开关。

    证据：文件工具不持有输出端口（构造签名只有 remote/colors），静默判定由
    渲染层读取 `Console.is_quiet_tools()` 完成（T3.10 覆盖展示路径）。
    """
    import inspect

    assert list(inspect.signature(EditTool.__init__).parameters) == ["self", "remote", "colors"]
    p = write_text(tmp_path / "x.txt", "old\n")
    out = run("Write", {"file_path": str(p), "content": "new\n"})
    assert out.ui_text  # 覆盖产生 diff → ui_text 已生成，展示与否由消费方决定


def test_error_prefix_for_failure_display(tmp_path):
    """失败判定依据：结果文本以 `[错误` 开头（非命令类工具）。"""
    out = run("Read", {"file_path": str(tmp_path / "nope.txt")})
    assert out.llm_text.startswith("[错误")
    ok = run("Read", {"file_path": str(write_text(tmp_path / "x.txt", "ok\n"))})
    assert not ok.llm_text.startswith("[错误")


def test_colorize_diff_roles():
    """着色角色：---/+++ 加粗青、@@ 暗淡青、- 红、+ 绿、其余灰；行首符号未改动。"""
    colors = diff_utils.DEFAULT_DIFF_COLORS
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n ctx\n-old\n+new"
    colored = diff_utils.colorize_diff(diff).split("\n")
    assert colored[0] == f"{colors.diff_header}--- a/x{colors.rst}"
    assert colored[1] == f"{colors.diff_header}+++ b/x{colors.rst}"
    assert colored[2] == f"{colors.diff_range}@@ -1 +1 @@{colors.rst}"
    assert colored[3] == f"{colors.diff_context} ctx{colors.rst}"
    assert colored[4] == f"{colors.diff_removed}-old{colors.rst}"
    assert colored[5] == f"{colors.diff_added}+new{colors.rst}"
    # 加粗青 = bold + accent；暗淡青 = dim + accent（旧实现的 B+C / D+C 组合）
    assert colors.diff_header.startswith("\x1b[1m")
    assert colors.diff_range.startswith("\x1b[2m")
    assert colors.diff_context.endswith("m") and colors.rst == "\x1b[0m"


@pytest.mark.parametrize("value", ["", None, "[无差异]"], ids=["empty", "none", "no-diff"])
def test_colorize_diff_no_difference(value):
    """空输入或 [无差异] → 灰色的 [无差异]。"""
    expected = f"{diff_utils.DEFAULT_DIFF_COLORS.diff_context}[无差异]{diff_utils.DEFAULT_DIFF_COLORS.rst}"
    assert diff_utils.colorize_diff(value) == expected


def test_colorize_diff_with_injected_theme():
    """取色可注入：注入 output 主题后纯文本模式消色、正常模式按角色着色。"""
    from io import StringIO

    from narnat_agent.output.console import Console
    from narnat_agent.output.style import Theme

    console = Console(stdout=StringIO(), truecolor=True)
    theme = Theme(console)
    diff = "--- a/x\n+new"
    colored = diff_utils.colorize_diff(diff, theme)
    assert colored.startswith(str(theme.diff_header))
    console.set_plain(True)
    assert diff_utils.colorize_diff(diff, theme) == diff  # 颜色求值为空，文本结构不变


@pytest.mark.parametrize("old,new,expected", [
    (b"a\r\nb\r\n", b"a\nb\n", "行尾符 CRLF→LF"),
    (b"a\r\nb\r\n", b"a\r\nb", "末尾换行已移除"),
    (b"a\nb", b"a\nb\n", "末尾换行已添加"),
    (b"\xd6\xd0", "中".encode("utf-8"), "换行符以外的字节表示变化（2→3字节，如编码或BOM差异）"),
    (b"abc", b"abcd", "换行符以外的字节表示变化（3→4字节，如编码或BOM差异）"),
    (b"", b"", "换行符以外的字节表示变化（0→0字节，如编码或BOM差异）"),
], ids=["eol", "trailing-removed", "trailing-added", "encoding", "fallback", "both-empty"])
def test_describe_bytes_only_change(old, new, expected):
    """字节层面差异描述：行尾符 / 末尾换行 / 兜底描述，多片段以「、」连接。"""
    assert diff_utils.describe_bytes_only_change(old, new) == expected
    assert diff_utils.describe_bytes_only_change(b"a\r\nb\n", b"a\nb\n") == "行尾符 CRLF+LF→LF"
    assert diff_utils.describe_bytes_only_change(b"x", b"y") == "换行符以外的字节表示变化（1→1字节，如编码或BOM差异）"


def test_make_diff_output():
    """unified diff 形态：a/{文件名} → b/{文件名}、行间 \\n 连接、无差异为 [无差异]。"""
    diff = diff_utils.make_diff("a\nb\n", "a\nc\n", "dir/x.txt")
    assert diff.split("\n")[0] == "--- a/x.txt"
    assert diff.split("\n")[1] == "+++ b/x.txt"
    assert "-b" in diff and "+c" in diff
    assert diff_utils.make_diff("same", "same", "x.txt") == "[无差异]"


# ═══════════════════════════════════════════════════════════════
# 8. 执行入口 / 全局截断 / 上下文 / 动态注册 / 兼容怪癖
# ═══════════════════════════════════════════════════════════════


def test_unknown_tool():
    """调用不存在的工具名 → [错误: 未知工具: Foo]。"""
    registry = make_registry()
    assert registry.execute("Foo", {}).llm_text == "[错误: 未知工具: Foo]"


def test_unknown_param_hint(tmp_path):
    """AI 传 filepath（正确参数名为 file_path）→ 未知参数提示并列出有效参数。"""
    out = run("Read", {"filepath": str(tmp_path / "x.txt")})
    assert out.llm_text == ("[错误: 工具参数错误(Read): 收到未知参数 'filepath'。"
                            "Read 的有效参数: file_path, offset, limit, device]")


def test_missing_required_hint():
    """未传 file_path 调用 Read → 缺少必填参数提示。"""
    out = run("Read", {})
    assert out.llm_text == ("[错误: 工具参数错误(Read): 缺少必填参数: file_path。"
                            "Read 的有效参数: file_path, offset, limit, device]")


def test_missing_required_multi_params():
    """Write 同时缺 file_path/content → 多参数缺失提示。"""
    out = run("Write", {})
    assert out.llm_text == ("[错误: 工具参数错误(Write): 缺少必填参数: file_path, content。"
                            "Write 的有效参数: file_path, content, device]")


def test_tool_exception_wrapped(tmp_path):
    """工具内部普通异常 → [错误: 工具执行失败({name}): {异常文本}]，进程不中断。"""
    out = run("Read", {"file_path": str(write_text(tmp_path / "x.txt", "x\n")), "offset": "abc"})
    assert out.llm_text.startswith("[错误: 工具执行失败(Read): ")
    assert out.is_error is True


def test_global_truncate_keeps_head_tail(tmp_path):
    """执行入口全局截断：保留前 2/3 与后 1/3（面向非 Read 工具的输出）。"""
    for i in range(30):
        write_text(tmp_path / f"f{i:02d}.txt", "x\n")
    touch_all(tmp_path)
    env = make_env(max_tool_output_chars=100)
    out = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}, env).llm_text
    assert "...[全局截断: 输出共" in out
    assert "已达全局上限0KB" in out
    assert "已保留首尾" in out
    assert out.endswith("f29.txt")


def test_global_truncate_disabled_when_zero(tmp_path):
    """上下文输出上限为 0 → 不限制、不做任何截断。"""
    for i in range(30):
        write_text(tmp_path / f"f{i:02d}.txt", "x\n")
    touch_all(tmp_path)
    env = make_env(max_tool_output_chars=0)
    out = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}, env).llm_text
    assert "全局截断" not in out
    assert out.split("\n")[-1] == "f29.txt"


def test_global_truncate_kb_display(tmp_path):
    """上限 <1024 时 K 显示为 0；上限 1500 时显示 1（整数除法）。"""
    for i in range(60):
        write_text(tmp_path / f"f{i:02d}-{'x' * 20}.txt", "x\n")
    touch_all(tmp_path)
    out_small = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)},
                    make_env(max_tool_output_chars=100)).llm_text
    assert "已达全局上限0KB" in out_small
    out_1500 = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)},
                   make_env(max_tool_output_chars=1500)).llm_text
    assert "已达全局上限1KB" in out_1500


def test_quirk_two_truncation_styles_coexist(tmp_path):
    """兼容怪癖①：Read 的按行截断（给行号续读）与执行入口的首尾截断并存。"""
    p = write_text(tmp_path / "a.txt", "".join(f"L{i:03d}-{'x' * 20}\n" for i in range(1, 41)))
    env = make_env(max_tool_output_chars=500)
    read_out = run("Read", {"file_path": str(p)}, env).llm_text
    assert "已达全局输出上限(500字符≈" in read_out
    assert "已保留首尾" not in read_out

    for i in range(30):
        write_text(tmp_path / f"f{i:02d}-{'x' * 15}.txt", "x\n")
    touch_all(tmp_path)
    glob_out = run("Glob", {"pattern": "*.txt", "path": str(tmp_path)}, env).llm_text
    assert "已保留首尾" in glob_out
    assert "已达全局输出上限" not in glob_out


def test_quirk_remote_body_startswith_bracket():
    """兼容怪癖②：远程正文首字符为 [ 时不加设备头（成败判定用 startswith('[')）。"""
    remote = FakeRemote(read_result="[正文以方括号开头]\n  1→x")
    out = run("Read", {"file_path": "/tmp/a.txt", "device": "dev1"}, remote=remote).llm_text
    assert out == "[正文以方括号开头]\n  1→x"
    assert not out.startswith("[dev1]")


def test_quirk_edit_write_failure_clears_file(tmp_path):
    """兼容怪癖④：Edit 写回先截断后写入，编码失败时磁盘原内容已被清空。"""
    p = write_bytes(tmp_path / "x.txt", "中文\n".encode("gbk"))
    out = run("Edit", {"file_path": str(p), "old_string": "中文", "new_string": "🙂"})
    assert out.llm_text.startswith("[错误: 新内容包含文件编码(gbk)无法表示的字符")
    assert p.read_bytes() == b""


def test_quirk_empty_grep_pattern_matches_all(tmp_path):
    """兼容怪癖⑨：Grep 的空 pattern 不被拦截（匹配所有行）。"""
    write_text(tmp_path / "a.txt", "one\ntwo\n")
    out = run("Grep", {"pattern": "", "path": str(tmp_path)}).llm_text
    assert out.split("\n")[0] == "a.txt (2处):"
    assert "1:one" in out and "2:two" in out


def test_quirk_glob_truncation_total_counts_per_pattern(tmp_path):
    """兼容怪癖⑥：截断提示的「匹配总数」按逐模式累计（重叠模式重复计数），展示按路径去重。"""
    write_text(tmp_path / "src" / "only.txt", "x\n")
    out = run("Glob", {"pattern": "{src/*.txt,src/*.txt}", "path": str(tmp_path),
                       "max_results": 1}).llm_text
    lines = out.split("\n")
    assert lines[0].endswith("only.txt")
    assert lines[1] == ("...[已截断: 共2个匹配项, 当前显示按修改时间最近的1个。"
                        "增大max_results可获取完整列表]")


def test_quirk_grep_relative_path_labels(tmp_path, monkeypatch):
    """同一文件被多个路径项覆盖时只扫描一次；目录内文件按相对路径字典序处理。"""
    monkeypatch.chdir(tmp_path)
    write_text(tmp_path / "b.txt", "hit\n")
    write_text(tmp_path / "a.txt", "hit\n")
    out = run("Grep", {"pattern": "hit", "path": "."}).llm_text
    assert out.index("a.txt (1处):") < out.index("b.txt (1处):")


# ── 上下文数据语义 ──


def test_ignore_dirs_from_settings(tmp_path):
    """忽略目录来源为运行时上下文（配置注入）；上下文缺失时为空。"""
    from narnat_agent.tools.file import param_utils

    env = make_env(ignore_dirs=["node_modules", ".git"])
    assert param_utils.settings_of(env).ignore_dirs == ["node_modules", ".git"]
    assert param_utils.settings_of(None) is None

    write_text(tmp_path / "node_modules" / "a.py", "x\n")
    write_text(tmp_path / "a.py", "x\n")
    with_env = GlobTool().execute({"pattern": "*.py", "path": str(tmp_path)}, env).llm_text
    without_env = GlobTool().execute({"pattern": "*.py", "path": str(tmp_path)}, None).llm_text
    assert "node_modules" not in with_env
    assert "node_modules" in without_env


def test_settings_zero_arg_defaults():
    """零参构造：输出上限 65536、超时上限 1800、忽略目录空、计划优先关闭、阈值 2。"""
    settings = ToolSettingsImpl()
    assert settings.max_tool_output_chars == 65536
    assert settings.max_timeout_seconds == 1800
    assert settings.ignore_dirs == []
    assert settings.require_plan is False
    assert settings.min_tools == 2
    assert ToolSettingsImpl().ignore_dirs is not settings.ignore_dirs


def test_api_key_default_empty():
    """密钥表中不存在的键 → 空串（不抛异常）。"""
    assert ToolSettingsImpl().get_api_key("websearch") == ""


# ── 动态工具注册与注销可见性 ──


class _FakeDynamicTool:
    def __init__(self, name: str):
        self.name = name

    def definition(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": "动态工具", "parameters":
            {"type": "object", "properties": {}, "required": []}}}

    def execute(self, args, env) -> ToolResult:
        return ToolResult("dynamic-ok")


def test_dynamic_registration_visibility():
    """注册动态工具 → 名称清单含该名（内置之后）、定义列表含其定义、可被调用。"""
    registry = make_registry()
    registered = registry.register_dynamic([_FakeDynamicTool("mcp__demo__search")])
    assert registered == ["mcp__demo__search"]
    assert registry.get_tool_names()[-1] == "mcp__demo__search"
    assert [d["function"]["name"] for d in registry.get_tool_definitions()][-1] == "mcp__demo__search"
    assert registry.execute("mcp__demo__search", {}).llm_text == "dynamic-ok"


def test_dynamic_same_name_skipped():
    """注册名为 Read 的动态工具 → 跳过，Read 仍解析到内置实现，定义列表无重复。"""
    registry = make_registry()
    assert registry.register_dynamic([_FakeDynamicTool("Read")]) == []
    names = [d["function"]["name"] for d in registry.get_tool_definitions()]
    assert names.count("Read") == 1
    assert registry.has_tool("Read")


def test_dynamic_unregister():
    """注销动态工具 → 从名称清单与定义列表消失，再次调用返回未知工具。"""
    registry = make_registry()
    registry.register_dynamic([_FakeDynamicTool("mcp__demo__search")])
    registry.unregister_dynamic(["mcp__demo__search"])
    assert "mcp__demo__search" not in registry.get_tool_names()
    assert registry.execute("mcp__demo__search", {}).llm_text == "[错误: 未知工具: mcp__demo__search]"
    registry.unregister_dynamic(None)  # 空名单不产生变更
    registry.unregister_dynamic([])
    assert registry.get_tool_names() == ["Read", "Glob", "Grep", "Edit", "Write"]


# ── token 估算（与文件工具截断提示共用口径）──


def test_token_estimate_mixed_density():
    """CJK 0.7/字、其余 0.25/字、向上取整；空文本 0。"""
    from narnat_agent.tools.token_estimate import estimate_text_tokens

    assert estimate_text_tokens("中" * 10) == 7
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("") == 0


# ═══════════════════════════════════════════════════════════════
# 9. 结构约束（分层/依赖/导出）
# ═══════════════════════════════════════════════════════════════


def test_file_package_exports():
    """包导出完整：__all__ 每项可解析，五工具的类与定义均在。"""
    for name in file_pkg.__all__:
        assert hasattr(file_pkg, name), f"__all__ 中的 {name!r} 无法解析"
    assert file_pkg.ReadTool.name == "Read"
    assert file_pkg.READ_DEFINITION is READ_DEFINITION


def test_file_tools_dependencies_are_stdlib_and_contracts():
    """依赖纯净：不绝对导入新包；跨积木相对导入只允许 contracts（同族用 level=1/2）。"""
    sources = sorted(FILE_DIR.rglob("*.py"))
    assert sources, "file 目录不存在或为空"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("narnat_agent"), (
                        f"{path.name}: 禁止绝对导入新包（{alias.name}）")
            elif isinstance(node, ast.ImportFrom) and node.level:
                if node.level >= 3:
                    assert (node.module or "").startswith("contracts"), (
                        f"{path.name}: 跨积木导入只允许 contracts（{'.' * node.level}{node.module}）")


def test_file_package_passes_layering_check():
    """分层/私有访问/模块级状态检查：file 目录零违规（复用仓库检查脚本）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import check_layering  # noqa: E402

    violations = []
    for path in sorted(FILE_DIR.rglob("*.py")):
        violations.extend(check_layering.check_file(path))
    assert violations == [], "\n".join(str(v) for v in violations)
