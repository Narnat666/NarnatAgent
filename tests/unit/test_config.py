"""T2.1 config 积木单元测试 —— spec 场景逐条覆盖 + 风险重点 + 旧实现基准对照。

- 行为金标准：`openspec/changes/recast-v2/specs/config/spec.md`（Scenario 逐条映射，
  命名见各测试 docstring）；界面键名的中英优先级同时对照 `specs/output/spec.md`。
- 基准对照：`tests/baseline/data/loader.json` 与 `defaults.json`（T0.1 由旧实现生成），
  逐用例比对；基准缺失时跳过（不阻塞）。
- 全部磁盘用例只用 tmp_path，不触碰真实 `.narnat/`；不启动进程、不连网。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import platform
import sys
from pathlib import Path

import pytest

from narnat_agent import config as cfg
from narnat_agent.config import defaults

BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline" / "data"


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════

def _no_packaged_env(monkeypatch):
    """清除 NARNAT_HOME 与打包态标记，使目录发现走开发态分支。"""
    monkeypatch.delenv("NARNAT_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def _write_config(root: Path, document) -> Path:
    """在 root/.narnat/config/narnat.json 写入给定文档。"""
    path = root / ".narnat" / "config" / "narnat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_with(base: Path, document, **kwargs):
    """构造独立项目并 load_config（base 下新建 proj/，互不干扰）。"""
    root = base / "proj"
    (root / ".narnat").mkdir(parents=True, exist_ok=True)
    _write_config(root, document)
    return cfg.load_config(project_root=str(root), **kwargs)


def _chain(root: Path, depth: int) -> Path:
    """创建 root/d0/.../d{depth-1} 并返回最深层目录。"""
    path = root
    for i in range(depth):
        path = path / f"d{i}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ═══════════════════════════════════════════════════════════════
# 1. `.narnat` 目录发现
# ═══════════════════════════════════════════════════════════════

def test_env_home_valid_wins(tmp_path, monkeypatch):
    """spec Scenario「NARNAT_HOME 指定且有效」：该目录优先，忽略其它分支。"""
    home = tmp_path / "home"
    (home / ".narnat").mkdir(parents=True)
    other = tmp_path / "other"
    (other / ".narnat").mkdir(parents=True)
    monkeypatch.setenv("NARNAT_HOME", str(home))
    monkeypatch.chdir(other)

    assert cfg.find_project_root() == str(home)


def test_env_home_invalid_falls_back(tmp_path, monkeypatch):
    """spec Scenario「NARNAT_HOME 无效时继续回退」：其下无 .narnat 时忽略该变量。"""
    bad = tmp_path / "bad"
    bad.mkdir()
    cwd_root = tmp_path / "cwd_root"
    (cwd_root / ".narnat").mkdir(parents=True)
    monkeypatch.setenv("NARNAT_HOME", str(bad))
    monkeypatch.chdir(cwd_root)

    assert cfg.find_project_root() == str(cwd_root)


def test_dev_mode_walks_up_to_ancestor(tmp_path, monkeypatch):
    """spec Scenario「开发态向上查找」：使用最近的含 .narnat 的祖先目录。"""
    _no_packaged_env(monkeypatch)
    root = tmp_path / "root"
    (root / ".narnat").mkdir(parents=True)
    deep = _chain(root, 4)
    monkeypatch.chdir(deep)

    assert cfg.find_project_root() == str(root)


def test_dev_mode_walk_up_limit_is_10(tmp_path, monkeypatch):
    """风险重点：向上查找上限 10 层——第 9 层祖先命中，第 10 层祖先超出边界。"""
    _no_packaged_env(monkeypatch)
    root = tmp_path / "root"
    (root / ".narnat").mkdir(parents=True)

    monkeypatch.chdir(_chain(root, 9))
    assert cfg.find_project_root() == str(root)

    monkeypatch.chdir(_chain(root, 10))
    assert cfg.find_project_root() == os.getcwd()


def test_dev_mode_no_narnat_returns_cwd(tmp_path, monkeypatch):
    """找不到 `.narnat` 时退回当前工作目录。

    用 >10 层的隔离链，保证外部环境（如 %TEMP% 下已存在的 .narnat）不会落入
    向上查找的 10 层窗口，测试与运行环境无关。
    """
    _no_packaged_env(monkeypatch)
    deep = _chain(tmp_path / "iso", 12)
    monkeypatch.chdir(deep)

    assert cfg.find_project_root() == os.getcwd()


def test_frozen_uses_executable_dir(tmp_path, monkeypatch):
    """打包态（PyInstaller/standalone）：无论有无 .narnat 都用 exe 所在目录。"""
    _no_packaged_env(monkeypatch)
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "nn.exe"))

    assert cfg.find_project_root() == str(exe_dir)

    (exe_dir / ".narnat").mkdir()
    assert cfg.find_project_root() == str(exe_dir)


def test_onefile_uses_exe_dir(tmp_path, monkeypatch):
    """Nuitka onefile：exe 目录有 .narnat 用 exe 目录，没有也返回 exe 目录。"""
    _no_packaged_env(monkeypatch)
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    monkeypatch.setattr(cfg.loader, "is_nuitka_onefile", lambda: True)
    monkeypatch.setattr(cfg.loader, "find_narnat_exe_dir", lambda: str(exe_dir))

    assert cfg.find_project_root() == str(exe_dir)


def test_is_nuitka_onefile_heuristics(monkeypatch):
    """onefile 判定：(a) 临时目录 onefile_ + python 可执行名；(b) __main__.__compiled__。"""
    monkeypatch.delattr(sys.modules["__main__"], "__compiled__", raising=False)

    monkeypatch.setattr(sys, "executable", os.path.join("C:" + os.sep, "tmp", "onefile_1_0", "python.exe"))
    assert cfg.is_nuitka_onefile() is True

    monkeypatch.setattr(sys, "executable", os.path.join("C:" + os.sep, "python312", "python.exe"))
    assert cfg.is_nuitka_onefile() is False

    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", True, raising=False)
    assert cfg.is_nuitka_onefile() is True


def test_find_narnat_exe_dir_from_argv(tmp_path, monkeypatch):
    """exe 目录定位：argv[0] 为存在的完整路径 → 其目录（非 win32 分支）。"""
    exe = tmp_path / "bin" / "nn.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", [str(exe)])

    assert cfg.find_narnat_exe_dir() == str(exe.parent)


# ═══════════════════════════════════════════════════════════════
# 2. 首次运行初始化
# ═══════════════════════════════════════════════════════════════

def test_first_run_creates_default_files(tmp_path, monkeypatch):
    """spec Scenario「全新环境首次启动」：建 config/ data/、生成默认 json 与空 md；
    AND：logs 目录不在此阶段创建。

    用 >10 层的隔离链充当"全新环境"，保证向上查找不会命中外部 .narnat。
    """
    _no_packaged_env(monkeypatch)
    root = _chain(tmp_path / "iso", 11)
    monkeypatch.chdir(root)

    conf = cfg.load_config()

    narnat = root / ".narnat"
    json_path = narnat / "config" / "narnat.json"
    md_path = narnat / "config" / "narnat.md"
    assert json_path.is_file()
    assert md_path.is_file()
    assert md_path.read_text(encoding="utf-8") == ""
    assert (narnat / "data").is_dir()
    assert not (narnat / "logs").exists()

    # 固定键结构与值（抽查：模型、界面、工具、压缩、忽略目录、余额查询）
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert list(data) == ["智能体", "余额查询", "接口密钥组", "定价", "费用日志",
                          "界面", "工具", "会话", "压缩", "计划", "忽略目录"]
    assert data["智能体"]["模型"] == {"当前": defaults.DEFAULT_MODEL,
                                  "列表": [defaults.DEFAULT_MODEL]}
    assert data["智能体"]["协议"] == defaults.DEFAULT_PROTOCOL
    assert data["智能体"]["LLM重试次数"] == defaults.DEFAULT_LLM_RETRY_COUNT
    assert data["界面"] == {"show_cost": False, "show_balance": False,
                            "max_output_tokens": 128000}
    assert data["工具"] == {"输出上限KB": 64, "超时上限秒": 1800}
    assert data["压缩"] == {"占比显示": False, "告警": 50, "压缩": 95, "保留尾部": 16000}
    assert data["忽略目录"] == list(defaults.DEFAULT_IGNORE_DIRS)
    assert data["余额查询"]["启用"] is True
    assert data["计划"] == {}
    assert data == defaults.build_first_run_config()

    # UTF-8、2 空格缩进、中文不转义、无 BOM
    raw = json_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert '\n  "智能体": {' in text
    assert "\\u" not in text

    assert conf.paths.project_root == os.path.abspath(str(root))
    assert conf.paths.narnat_dir == os.path.join(os.path.abspath(str(root)), ".narnat")
    assert conf.ai.model == defaults.DEFAULT_MODEL
    assert conf.system_prompt == defaults.BASE_PROMPT_TEMPLATE.format(
        model=defaults.DEFAULT_MODEL, cwd=os.getcwd(), platform=platform.system())


def test_existing_config_not_overwritten(tmp_path, monkeypatch):
    """spec Scenario「配置文件已存在」：不覆盖、不补写任何键。"""
    _no_packaged_env(monkeypatch)
    root = tmp_path / "proj"
    (root / ".narnat").mkdir(parents=True)
    json_path = _write_config(root, {"智能体": {"模型": {"当前": "m-x", "列表": ["m-x"]}}})
    md_path = root / ".narnat" / "config" / "narnat.md"
    md_path.write_text("# 规则\n", encoding="utf-8")
    before_json, before_md = json_path.read_bytes(), md_path.read_bytes()

    conf = cfg.load_config(project_root=str(root))

    assert json_path.read_bytes() == before_json  # 不补写缺失键
    assert md_path.read_bytes() == before_md
    assert conf.ai.model == "m-x"
    assert conf.ai.model_options == ["m-x"]


def test_broken_json_falls_back_to_defaults(tmp_path):
    """损坏 JSON → 静默回落全默认（且不重写文件）。"""
    root = tmp_path / "proj"
    path = root / ".narnat" / "config" / "narnat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 坏掉的 json", encoding="utf-8")
    before = path.read_bytes()

    conf = cfg.load_config(project_root=str(root))

    assert conf.ai.model == defaults.DEFAULT_MODEL
    assert path.read_bytes() == before


def test_top_level_non_dict_behaves_like_legacy(tmp_path):
    """顶层非 dict 无类型校验（与现状一致：按 dict 使用会抛 AttributeError）。"""
    root = tmp_path / "proj"
    path = root / ".narnat" / "config" / "narnat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(AttributeError):
        cfg.load_config(project_root=str(root))


# ═══════════════════════════════════════════════════════════════
# 3. 配置键名兼容（含 specs/output「中文键名与英文别名」）
# ═══════════════════════════════════════════════════════════════

def test_ui_english_alias_show_cost(tmp_path):
    """spec Scenario「英文别名」：`show_cost` 生效，等同 `显示费用`。"""
    conf = _load_with(tmp_path / "a", {"界面": {"show_cost": True}})
    assert conf.ui.show_cost is True

    conf_cn = _load_with(tmp_path / "b", {"界面": {"显示费用": True}})
    assert conf_cn.ui.show_cost is True


def test_ui_switch_en_key_wins_over_cn_alias(tmp_path):
    """顶层开关中英并存：英文键优先（先查英文名，命中即用）。"""
    conf = _load_with(tmp_path / "a", {"界面": {"显示费用": True, "show_cost": False}})
    assert conf.ui.show_cost is False

    conf2 = _load_with(tmp_path / "b", {"界面": {"显示费用": False, "show_cost": True}})
    assert conf2.ui.show_cost is True


def test_section_en_existing_keeps_cn_key_untouched(tmp_path):
    """分组名中英并存：英文键已存在时中文键原样保留（不迁移、不覆盖）。"""
    conf = _load_with(tmp_path / "a", {"界面": {
        "颜色": {"accent": "#111111"}, "colors": {"accent": "#222222"}}})
    assert conf.ui.raw["colors"]["accent"] == "#222222"
    assert conf.ui.raw["颜色"] == {"accent": "#111111"}


def test_section_cn_alias_first_wins(tmp_path):
    """同组中文别名并存（`标注`/`标记`）：先到者生效，另一个保留原键。"""
    conf = _load_with(tmp_path / "a", {"界面": {
        "标注": {"bold": "x"}, "标记": {"bold": "y"}}})
    assert conf.ui.raw["markdown"]["bold"] == "x"
    assert conf.ui.raw["标记"] == {"bold": "y"}


def test_old_flat_color_migrates_to_group(tmp_path):
    """spec Scenario「旧扁平配色迁移」：`标题色` 写入基础色分组的强调角色。"""
    conf = _load_with(tmp_path / "a", {"界面": {"标题色": "红"}})
    assert conf.ui.raw["base_colors"]["accent"] == "红"
    assert "标题色" not in conf.ui.raw


def test_old_flat_color_overrides_existing(tmp_path):
    """旧扁平键迁移为直赋迁入：目标位置已有值时覆盖（且旧键被移除）。"""
    conf = _load_with(tmp_path / "a", {"界面": {
        "标题色": "红", "base_colors": {"accent": "蓝"}}})
    assert conf.ui.raw["base_colors"]["accent"] == "红"
    assert "标题色" not in conf.ui.raw


def test_ui_section_inner_key_alias_and_switch(tmp_path):
    """分组内中文键 → 英文键；`最大输出token数` 覆盖默认值。"""
    conf = _load_with(tmp_path / "a", {"界面": {
        "标注": {"标题1": "bold accent"}, "最大输出token数": "2048"}})
    assert conf.ui.raw["markdown"]["heading_h1"] == "bold accent"
    assert conf.ui.max_output_tokens == 2048


def test_ui_recipe_zh_color_replacement_quirk(tmp_path):
    """配方值中文色名替换（含顺序怪癖：`强调` 先于 `强调色` 命中）。"""
    conf = _load_with(tmp_path / "a", {"界面": {
        "markdown": {"heading_h1": "bold 强调色"}}})
    assert conf.ui.raw["markdown"]["heading_h1"] == "bold emphasis色"


def test_ui_max_output_tokens_falls_back_to_model_config(tmp_path):
    """「界面」未配置最大输出 token → 取模型配置的「最大输出token数」。"""
    conf = _load_with(tmp_path / "a", {"智能体": {"最大输出token数": 4096}})
    assert conf.ui.max_output_tokens == 4096

    conf2 = _load_with(tmp_path / "b", {})
    assert conf2.ui.max_output_tokens == defaults.DEFAULT_UI_MAX_OUTPUT_TOKENS


# ═══════════════════════════════════════════════════════════════
# 4. 数值解析与容错
# ═══════════════════════════════════════════════════════════════

def test_token_amount_k_suffix(tmp_path):
    """spec Scenario「字符串 Token 量」："10k" → 10000；"1.5K" → 1500。"""
    assert _load_with(tmp_path / "a",
                      {"会话": {"自动保存Token量": "10k"}}).session.auto_save_tokens == 10000
    assert _load_with(tmp_path / "b",
                      {"会话": {"自动保存Token量": "1.5K"}}).session.auto_save_tokens == 1500


def test_token_amount_variants():
    """Token 量解析矩阵：bool/非法/负数/浮点。"""
    assert cfg.parse_token_amount(True) == 0
    assert cfg.parse_token_amount("abc", 777) == 777
    assert cfg.parse_token_amount(-5) == 0
    assert cfg.parse_token_amount(1234.9) == 1234
    assert cfg.parse_token_amount(None) == 0
    assert cfg.parse_token_amount("") == 0


def test_retain_tokens_fallback_and_zero(tmp_path):
    """spec Scenario「非法数值回落」：`保留尾部` 非法/缺失 → 16000；显式 0 合法；负数 → 0。"""
    assert _load_with(tmp_path / "a",
                      {"压缩": {"保留尾部": "abc"}}).session.retain_tokens == 16000
    assert _load_with(tmp_path / "b", {}).session.retain_tokens == 16000
    assert _load_with(tmp_path / "c",
                      {"压缩": {"保留尾部": 0}}).session.retain_tokens == 0
    assert _load_with(tmp_path / "d",
                      {"压缩": {"保留尾部": -5}}).session.retain_tokens == 0


def test_tool_output_limit_conversion(tmp_path):
    """spec Scenario「单位换算」：64KB → 65536 字符；≤0 → 0（不限制）。"""
    assert _load_with(tmp_path / "a",
                      {"工具": {"输出上限KB": 64}}).tools.max_output_chars == 65536
    assert _load_with(tmp_path / "b",
                      {"工具": {"输出上限KB": 0}}).tools.max_output_chars == 0
    assert _load_with(tmp_path / "c",
                      {"工具": {"输出上限KB": -3}}).tools.max_output_chars == 0


def test_cost_log_capacity(tmp_path):
    """spec Scenario「费用日志容量」：50MB → 50×1024×1024；负值 → 0；非法 → 50MB。"""
    conf = _load_with(tmp_path / "a", {"费用日志": {"最大容量MB": 50}})
    assert conf.cost_log.max_bytes == 50 * 1024 * 1024

    assert _load_with(tmp_path / "b",
                      {"费用日志": {"最大容量MB": -1}}).cost_log.max_bytes == 0
    assert _load_with(tmp_path / "c",
                      {"费用日志": {"最大容量MB": "abc"}}).cost_log.max_bytes == 50 * 1024 * 1024


def test_context_window_semantics(tmp_path):
    """上下文窗口：显式 ≤0 保留原值；非法/缺失 → 默认。"""
    assert _load_with(tmp_path / "a",
                      {"智能体": {"上下文窗口大小": 0}}).ai.context_window == 0
    assert _load_with(tmp_path / "b",
                      {"智能体": {"上下文窗口大小": "-5"}}).ai.context_window == -5
    assert _load_with(tmp_path / "c",
                      {"智能体": {"上下文窗口大小": "x"}}).ai.context_window == defaults.DEFAULT_CONTEXT_WINDOW
    assert _load_with(tmp_path / "d", {}).ai.context_window == defaults.DEFAULT_CONTEXT_WINDOW


def test_goal_max_rounds_zero_invalid_negative(tmp_path):
    """目标模式最大轮数：0/非法 → 100；负值保留（进入续跑逻辑）。"""
    assert _load_with(tmp_path / "a",
                      {"智能体": {"目标模式最大轮数": 0}}).ai.goal_max_rounds == 100
    assert _load_with(tmp_path / "b",
                      {"智能体": {"目标模式最大轮数": "abc"}}).ai.goal_max_rounds == 100
    assert _load_with(tmp_path / "c",
                      {"智能体": {"目标模式最大轮数": -5}}).ai.goal_max_rounds == -5


def test_ratio_fields_or_default(tmp_path):
    """告警/压缩占比：0/非法 → 默认（50/95）；负值保留（`or` 语义）。"""
    assert _load_with(tmp_path / "a",
                      {"压缩": {"告警": 0}}).session.warn_ratio == 50
    assert _load_with(tmp_path / "b",
                      {"压缩": {"压缩": 0}}).session.compress_ratio == 95
    assert _load_with(tmp_path / "c",
                      {"压缩": {"告警": -10}}).session.warn_ratio == -10


def test_temperature_and_max_tokens_fallback(tmp_path):
    """温度/最大输出 token 非法 → None（不传参）。"""
    conf = _load_with(tmp_path / "a", {"智能体": {"温度": "abc", "最大输出token数": ""}})
    assert conf.ai.temperature is None
    assert conf.ai.max_tokens is None

    conf2 = _load_with(tmp_path / "b", {"智能体": {"温度": "0.7", "最大输出token数": "4096"}})
    assert conf2.ai.temperature == 0.7
    assert conf2.ai.max_tokens == 4096


def test_bare_int_conversion_errors_kept(tmp_path):
    """现状保持：裸 `int()` 处非法值直接抛错（不复刻为容错回落）。"""
    with pytest.raises(ValueError):
        _load_with(tmp_path / "a", {"工具": {"输出上限KB": "很多"}})
    with pytest.raises(ValueError):
        _load_with(tmp_path / "b", {"界面": {"最大输出token数": "x"}})
    with pytest.raises(ValueError):
        _load_with(tmp_path / "c", {"智能体": {"LLM重试次数": "x"}})


def test_ignore_dirs_string_split_quirk(tmp_path):
    """兼容怪癖：`忽略目录` 为字符串时按字符逐字拆分（现状保持）。"""
    assert _load_with(tmp_path / "a",
                      {"忽略目录": "output"}).tools.ignore_dirs == ("o", "u", "t", "p", "u", "t")
    assert _load_with(tmp_path / "b",
                      {"忽略目录": ["a", "b"]}).tools.ignore_dirs == ("a", "b")
    assert _load_with(tmp_path / "c", {}).tools.ignore_dirs == ()


def test_string_bool_treated_as_true_quirk(tmp_path):
    """spec Scenario「字符串布尔按真值处理（兼容怪癖）」：`"false"` 等同 true。"""
    assert _load_with(tmp_path / "a",
                      {"界面": {"显示费用": "false"}}).ui.show_cost is True

    conf = _load_with(tmp_path / "b", {"智能体": {"思考": {"启用": "0", "回传": ""}}})
    assert conf.ai.thinking_enabled is True   # bool("0") 为真
    assert conf.ai.thinking_passback is False  # bool("") 为假


def test_coerce_value_matrix():
    """`coerce_value` 矩阵：None/空串/非法 → None；bool 不排除；不 trim。"""
    assert cfg.coerce_value(None, int) is None
    assert cfg.coerce_value("", int) is None
    assert cfg.coerce_value("abc", int) is None
    assert cfg.coerce_value("12.7", int) is None
    assert cfg.coerce_value(12.7, int) == 12
    assert cfg.coerce_value(True, int) == 1
    assert cfg.coerce_value([1], float) is None
    assert cfg.coerce_value("0.75", float) == 0.75
    assert cfg.coerce_value(42, str) == "42"


# ═══════════════════════════════════════════════════════════════
# 5. 模型配置归一
# ═══════════════════════════════════════════════════════════════

def test_model_current_not_in_list_inserted(tmp_path):
    """spec Scenario「当前模型不在列表」：插入列表首位。"""
    conf = _load_with(tmp_path / "a", {"智能体": {
        "模型": {"当前": "deepseek-flash", "列表": ["deepseek-v4-pro"]}}})
    assert conf.ai.model == "deepseek-flash"
    assert conf.ai.model_options == ["deepseek-flash", "deepseek-v4-pro"]


def test_model_config_normalization():
    """模型配置归一矩阵：非 dict、列表非 list、元素非 str、当前为空。"""
    assert cfg.parse_model_config(None) == (defaults.DEFAULT_MODEL, [defaults.DEFAULT_MODEL])
    assert cfg.parse_model_config("model") == (defaults.DEFAULT_MODEL, [defaults.DEFAULT_MODEL])
    assert cfg.parse_model_config({"列表": ["a", 1, None]}) == ("a", ["a"])
    assert cfg.parse_model_config({"当前": "", "列表": []}) == (defaults.DEFAULT_MODEL,
                                                              [defaults.DEFAULT_MODEL])
    assert cfg.parse_model_config({"当前": "m-c", "列表": ["m-a"]}) == ("m-c", ["m-c", "m-a"])


# ═══════════════════════════════════════════════════════════════
# 6. 系统提示词组装
# ═══════════════════════════════════════════════════════════════

def test_system_prompt_appends_user_md(tmp_path):
    """spec Scenario「用户指令追加」：系统提示词 = 基础模板 + 换行 + 用户指令。"""
    root = tmp_path / "proj"
    (root / ".narnat" / "config").mkdir(parents=True)
    (root / ".narnat" / "config" / "narnat.md").write_text(
        "项目规则：用中文回复\n", encoding="utf-8")

    conf = cfg.load_config(project_root=str(root))

    base = defaults.BASE_PROMPT_TEMPLATE.format(
        model=defaults.DEFAULT_MODEL, cwd=os.getcwd(), platform=platform.system())
    assert conf.system_prompt == base + "\n" + "项目规则：用中文回复"
    assert "项目规则" not in base


def test_headless_strips_paired_hidden_block(tmp_path):
    """spec Scenario「headless 隐藏区块」：配对区块整体删除；未配对标记原样保留。"""
    root = tmp_path / "proj"
    (root / ".narnat" / "config").mkdir(parents=True)
    (root / ".narnat" / "config" / "narnat.md").write_text(
        "前<!-- subagent:hide -->隐藏内容<!-- /subagent:hide -->后", encoding="utf-8")

    interactive = cfg.load_config(project_root=str(root), headless=False)
    assert "隐藏内容" in interactive.system_prompt

    head = cfg.load_config(project_root=str(root), headless=True)
    assert "隐藏内容" not in head.system_prompt
    assert head.system_prompt.endswith("前后")

    (root / ".narnat" / "config" / "narnat.md").write_text(
        "a<!-- subagent:hide --> 单侧标记", encoding="utf-8")
    head2 = cfg.load_config(project_root=str(root), headless=True)
    assert head2.system_prompt.endswith("a<!-- subagent:hide --> 单侧标记")


def test_strip_subagent_hidden_variants():
    """剥离函数：多区块逐个删除；单侧标记保留；正则容忍标记内空白。"""
    assert cfg.strip_subagent_hidden("无标记文本") == "无标记文本"
    assert cfg.strip_subagent_hidden(
        "A<!-- subagent:hide -->1<!-- /subagent:hide -->B<!-- subagent:hide -->2<!-- /subagent:hide -->C"
    ) == "ABC"
    assert cfg.strip_subagent_hidden("a<!-- /subagent:hide -->b") == "a<!-- /subagent:hide -->b"
    assert cfg.strip_subagent_hidden(
        "a<!--  subagent:hide  -->x<!--  /subagent:hide  -->b") == "ab"


def test_build_system_prompt_without_user_md():
    """用户指令为空 → 只有基础模板。"""
    prompt = cfg.build_system_prompt("m", "", cwd="D:/w", os_name="Windows")
    assert prompt == defaults.BASE_PROMPT_TEMPLATE.format(model="m", cwd="D:/w", platform="Windows")
    assert prompt.endswith("\n")


# ═══════════════════════════════════════════════════════════════
# 7. 项目技能目录三态与技能加载
# ═══════════════════════════════════════════════════════════════

def test_skill_project_roots_three_states(tmp_path):
    """技能目录三态：缺失/非列表 → None；[] → ();非空列表 → 元组（过滤非字符串项）。"""
    assert _load_with(tmp_path / "a", {}).skills.project_roots is None
    assert _load_with(tmp_path / "b", {"技能": {"项目技能目录": "not-list"}}).skills.project_roots is None
    assert _load_with(tmp_path / "c", {"技能": {"项目技能目录": []}}).skills.project_roots == ()
    assert _load_with(tmp_path / "d", {"技能": {
        "项目技能目录": ["skills", "", "  ", None, "x"]}}).skills.project_roots == ("skills", "x")


def test_project_skill_scan_disabled(tmp_path):
    """spec Scenario「关闭扫描」：`项目技能目录` 为 [] 时不进行项目技能发现。"""
    narnat_dir = tmp_path / "narnat"
    (narnat_dir / "config" / "skills").mkdir(parents=True)
    (narnat_dir / "config" / "skills" / "sys.md").write_text("系统技能", encoding="utf-8")
    work = tmp_path / "work"
    (work / "skills").mkdir(parents=True)
    (work / "skills" / "proj.md").write_text("项目技能", encoding="utf-8")

    auto_content, _, _ = cfg.load_skill(str(narnat_dir), "proj", cwd=str(work))
    assert auto_content == "项目技能"

    off_content, off_err, _ = cfg.load_skill(str(narnat_dir), "proj", project_roots=(), cwd=str(work))
    assert off_content == ""
    assert off_err == "技能不存在: proj"

    sys_content, _, _ = cfg.load_skill(str(narnat_dir), "sys", project_roots=(), cwd=str(work))
    assert sys_content == "系统技能"


def test_skill_explicit_roots_and_nested_path(tmp_path):
    """显式技能根：相对路径解析 + 层级路径加载。"""
    narnat_dir = tmp_path / "narnat"
    (narnat_dir / "config").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "myroots" / "sub").mkdir(parents=True)
    (work / "myroots" / "sub" / "a.md").write_text("层级技能", encoding="utf-8")

    content, err, path = cfg.load_skill(str(narnat_dir), "sub/a", project_roots=("myroots",), cwd=str(work))
    assert content == "层级技能"
    assert err == ""
    assert path.endswith("a.md")

    missing, err2, _ = cfg.load_skill(str(narnat_dir), "sub/a", project_roots=("nope",), cwd=str(work))
    assert missing == "" and "技能不存在" in err2


def test_skill_dir_resolution_rules(tmp_path):
    """目录形态技能：SKILL.md 优先；唯一 .md 直接加载；多个 .md 提示可选文件。"""
    narnat_dir = tmp_path / "narnat"
    (narnat_dir / "config").mkdir(parents=True)
    work = tmp_path / "work"
    skill_dir = work / "skills" / "one"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("主技能", encoding="utf-8")
    (skill_dir / "extra.md").write_text("其他", encoding="utf-8")
    multi_dir = work / "skills" / "multi"
    multi_dir.mkdir()
    (multi_dir / "a.md").write_text("a", encoding="utf-8")
    (multi_dir / "b.md").write_text("b", encoding="utf-8")

    content, err, _ = cfg.load_skill(str(narnat_dir), "one", cwd=str(work))
    assert content == "主技能" and err == ""

    multi, err2, _ = cfg.load_skill(str(narnat_dir), "multi", cwd=str(work))
    assert multi == ""
    assert "多个技能文件" in err2


def test_skill_unsafe_name_rejected(tmp_path):
    """路径安全：绝对路径/盘符/`..`/空组件被拒。"""
    narnat_dir = tmp_path / "narnat"
    (narnat_dir / "config").mkdir(parents=True)
    for name in ("../x", "a/../b", "/abs", "C:x", "a//b"):
        content, err, path = cfg.load_skill(str(narnat_dir), name, cwd=str(tmp_path))
        assert content == "" and path == ""
        assert "技能不存在" in err

    empty, err_empty, _ = cfg.load_skill(str(narnat_dir), "   ", cwd=str(tmp_path))
    assert empty == "" and err_empty == "技能不存在: "


def test_skill_encoding_fallback(tmp_path):
    """编码回退：UTF-8（含 BOM）优先，GBK 次之，二进制报错不崩溃。"""
    narnat_dir = tmp_path / "narnat"
    skills_dir = narnat_dir / "config" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "bom.md").write_bytes("\ufeffBOM技能".encode("utf-8"))
    (skills_dir / "gbk.md").write_bytes("中文GBK".encode("gbk"))
    (skills_dir / "bad.md").write_bytes(b"\xff\xfe\x00\x81\x40")

    assert cfg.load_skill(str(narnat_dir), "bom", cwd=str(tmp_path))[0] == "BOM技能"
    assert cfg.load_skill(str(narnat_dir), "gbk", cwd=str(tmp_path))[0] == "中文GBK"
    content, err, _ = cfg.load_skill(str(narnat_dir), "bad", cwd=str(tmp_path))
    assert content == ""
    assert "读取失败" in err


def test_list_skill_tree_system_and_project(tmp_path):
    """技能树：系统技能与项目技能合并；目录节点含 single 标记。"""
    narnat_dir = tmp_path / "narnat"
    skills_dir = narnat_dir / "config" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "sys.md").write_text("s", encoding="utf-8")
    work = tmp_path / "work"
    proj = work / "skills" / "p1"
    proj.mkdir(parents=True)
    (proj / "only.md").write_text("p", encoding="utf-8")

    tree = cfg.list_skill_tree(str(narnat_dir), cwd=str(work))

    assert {"name": "sys", "type": "file", "origin": "system"} in tree
    dir_node = next(n for n in tree if n["name"] == "p1")
    assert dir_node["type"] == "dir" and dir_node["single"] is True
    assert dir_node["children"] == [{"name": "only.md", "type": "file", "origin": "project"}]


def test_skill_discovery_respects_ignore_dirs_and_depth(tmp_path):
    """自动发现：忽略目录不进入；超过 scan_depth 的 skills 目录不发现；显式根不受限。"""
    narnat_dir = tmp_path / "narnat"
    (narnat_dir / "config").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "node_modules" / "skills").mkdir(parents=True)
    (work / "node_modules" / "skills" / "x.md").write_text("x", encoding="utf-8")
    (work / "a" / "skills").mkdir(parents=True)
    (work / "a" / "skills" / "y.md").write_text("y", encoding="utf-8")

    content, _, _ = cfg.load_skill(str(narnat_dir), "x",
                                   ignore_dirs=("node_modules",), cwd=str(work))
    assert content == ""

    # work/a/skills 属于第 2 层：scan_depth=1 时不发现，=2 时发现
    assert cfg.load_skill(str(narnat_dir), "y", cwd=str(work), scan_depth=1)[0] == ""
    assert cfg.load_skill(str(narnat_dir), "y", cwd=str(work), scan_depth=2)[0] == "y"


# ═══════════════════════════════════════════════════════════════
# 8. MCP 服务器参数解析
# ═══════════════════════════════════════════════════════════════

def test_mcp_command_array_normalization():
    """spec Scenario「命令数组归一」：数组拆为 command + args。"""
    srv = cfg.parse_mcp_server("srv", {"command": ["npx", "-y", "server"]})
    assert srv.command == "npx"
    assert srv.args == ("-y", "server")

    srv2 = cfg.parse_mcp_server("srv", {"command": ["python", "main.py"], "args": ["--x"]})
    assert srv2.command == "python"
    assert srv2.args == ("main.py", "--x")


def test_mcp_string_bool_false():
    """spec Scenario「字符串布尔」："false" 解析为未启用（白名单式）。"""
    assert cfg.parse_mcp_server("srv", {"enabled": "false"}).enabled is False
    assert cfg.parse_mcp_server("srv", {"enabled": "0"}).enabled is False
    assert cfg.parse_mcp_server("srv", {"enabled": "否"}).enabled is False
    assert cfg.parse_mcp_server("srv", {"enabled": " YES "}).enabled is True
    assert cfg.parse_mcp_server("srv", {"enabled": False}).enabled is False


def test_mcp_alias_keys_and_defaults():
    """中英键别名、取值归一、超时回落、env/名单字符串化。"""
    srv = cfg.parse_mcp_server("srv", {
        "命令": "python", "参数": ["-m", "srv"], "环境变量": {"K": 1, "J": None},
        "工作目录": "D:/x", "启用": "false", "启动超时秒": "60", "工具超时秒": 0,
        "工具白名单": ["a"], "工具黑名单": ["b"],
    })
    assert srv.command == "python"
    assert srv.args == ("-m", "srv")
    assert srv.env == {"K": "1", "J": "None"}
    assert srv.cwd == "D:/x"
    assert srv.enabled is False
    assert srv.startup_timeout == 60
    assert srv.tool_timeout == defaults.DEFAULT_MCP_TOOL_TIMEOUT
    assert srv.enabled_tools == ("a",)
    assert srv.disabled_tools == ("b",)

    # 中文键优先于英文键
    assert cfg.parse_mcp_server("srv", {"命令": "cn", "command": "en"}).command == "cn"


def test_mcp_invalid_inputs():
    """name 空白或条目非 dict → None；非列表 args/名单 → 空；超时非法 → 默认。"""
    assert cfg.parse_mcp_server("", {}) is None
    assert cfg.parse_mcp_server("   ", {}) is None
    assert cfg.parse_mcp_server("srv", "notdict") is None

    srv = cfg.parse_mcp_server("srv", {"args": "x", "enabled_tools": "t", "启动超时秒": -5})
    assert srv.args == ()
    assert srv.enabled_tools == ()
    assert srv.startup_timeout == defaults.DEFAULT_MCP_STARTUP_TIMEOUT

    minimal = cfg.parse_mcp_server("srv", {})
    assert minimal == cfg.parse_mcp_server("srv", {})
    assert minimal.command == "" and minimal.enabled is True


# ═══════════════════════════════════════════════════════════════
# 9. 磁盘路径布局
# ═══════════════════════════════════════════════════════════════

def test_disk_layout_constants_and_paths(tmp_path):
    """spec Scenario「会话与日志落点」：sessions/ 与 cost_log.csv 的位置契约。"""
    conf = _load_with(tmp_path / "a", {})

    assert defaults.NARNAT_DIR == ".narnat"
    assert defaults.CONFIG_SUBDIR == "config"
    assert defaults.DATA_SUBDIR == "data"
    assert defaults.LOGS_SUBDIR == "logs"
    assert defaults.SESSIONS_SUBDIR == "sessions"
    assert defaults.NARNAT_JSON == "narnat.json"
    assert defaults.NARNAT_MD == "narnat.md"

    narnat_dir = os.path.join(os.path.abspath(str(tmp_path / "a" / "proj")), ".narnat")
    assert conf.paths.narnat_dir == narnat_dir
    assert conf.paths.config_dir == os.path.join(narnat_dir, "config")
    assert conf.paths.data_dir == os.path.join(narnat_dir, "data")
    assert conf.paths.logs_dir == os.path.join(narnat_dir, "logs")

    sessions_dir = os.path.join(conf.paths.data_dir, defaults.SESSIONS_SUBDIR)
    assert sessions_dir.endswith(os.path.join(".narnat", "data", "sessions"))
    assert conf.cost_log.path == os.path.join(conf.paths.data_dir, "cost_log.csv")

    # 未配置自定义输出文件时 cost_log 落于 data/ 下；配置后按配置
    custom = _load_with(tmp_path / "b", {"费用日志": {"输出文件": "D:/out/x.csv"}})
    assert custom.cost_log.path == "D:/out/x.csv"


def test_misc_group_parsing(tmp_path):
    """其余分组：安全确认/计划/定价/余额/接口密钥组/工具会话数。"""
    conf = _load_with(tmp_path / "a", {
        "工具": {"SSH最大会话数": 8, "最大传输文件MB": 20, "git免确认": True, "rm免确认": "false"},
        "计划": {"计划优先": True, "计划最低工具数": 3},
        "定价": {"模型": {"m1": {"输入": 1, "缓存命中": 0.5, "输出": 2}}},
        "余额查询": {"启用": "false", "查询地址": "http://x", "认证方式": "x-api-key",
                     "响应路径": "a.b", "货币路径": "c"},
        "接口密钥组": {"websearch": "k"},
    })
    assert conf.tools.max_sessions == 8
    assert conf.tools.max_transfer_mb == 20
    assert conf.safety.git_skip_confirm is True
    assert conf.safety.rm_skip_confirm is True       # bool("false") 怪癖
    assert conf.plan.require_plan is True
    assert conf.plan.min_tools == 3
    assert conf.pricing.user_pricing == {"m1": {"input": 1, "cache_hit": 0.5, "output": 2}}
    assert conf.balance.enabled is True               # bool("false") 怪癖
    assert conf.balance.url == "http://x"
    assert conf.balance.auth_method == "x-api-key"
    assert conf.balance.value_path == "a.b"
    assert conf.balance.currency_path == "c"
    assert conf.api_keys == {"websearch": "k"}


# ═══════════════════════════════════════════════════════════════
# 10. 旧实现基准对照（T0.1 基准逐用例比对）
# ═══════════════════════════════════════════════════════════════

_TARGET_TYPES = {"int": int, "float": float, "str": str}


def _jsonable(obj):
    """与 extract_old.py 相同的序列化约定（dataclass → dict、tuple → list）。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return repr(obj)


def _baseline_loader_result(group: str, case: dict):
    """按基准组调用新实现（基准组名 = 旧实现私有函数名）。"""
    inp = case["input"]
    if group == "loader._coerce":
        return _jsonable(cfg.coerce_value(inp["v"], _TARGET_TYPES[inp["target"]]))
    if group == "loader._parse_token_amount":
        return _jsonable(cfg.parse_token_amount(inp["v"], inp["default"]))
    if group == "loader._parse_model_config":
        return _jsonable(cfg.parse_model_config(copy.deepcopy(inp["value"])))
    if group == "loader._parse_pricing":
        return _jsonable(cfg.parse_pricing(copy.deepcopy(inp["value"])))
    if group == "loader._parse_project_skill_roots":
        return _jsonable(cfg.parse_project_skill_roots(copy.deepcopy(inp["value"])))
    if group == "loader.parse_mcp_server":
        return _jsonable(cfg.parse_mcp_server(inp["name"], copy.deepcopy(inp["entry"])))
    if group == "loader._strip_subagent_hidden":
        return cfg.strip_subagent_hidden(inp["md"])
    if group == "loader._build_system_prompt":
        # 基准入参含 shell_name（模板未引用该占位，无观察差异；新实现去掉死参数）
        return cfg.build_system_prompt(inp["model"], inp["user_md"],
                                       cwd=inp["cwd"], os_name=inp["os_name"])
    if group == "loader._build_ai_config":
        # 注：新实现把 LLM重试次数 解析并入 build_ai_config（消除二次重建）；
        # 基准用例均未设置该键，故结果仍等价（缺失 → 3）。
        return _jsonable(cfg.build_ai_config(copy.deepcopy(inp["value"])))
    if group == "loader._build_ui_config":
        return _jsonable(cfg.build_ui_config(copy.deepcopy(inp["value"]), inp["max_output_tokens"]))
    raise AssertionError(f"未知基准组: {group}")


def _baseline_defaults_result(group: str, case: dict):
    inp = case["input"]
    if group == "defaults.resolve_thinking_params":
        body_top, extra_body = defaults.resolve_thinking_params(
            inp["protocol"], inp["model"], inp["thinking_enabled"], inp["effort"])
        return {"body_top": _jsonable(body_top), "extra_body": _jsonable(extra_body)}
    if group == "defaults.resolve_thinking_passback":
        return defaults.resolve_thinking_passback(inp["protocol"], inp["model"])
    if group == "defaults.prompt_templates":
        return getattr(defaults, case["id"])
    raise AssertionError(f"未知基准组: {group}")


def _compare_baseline(file_name: str, resolver):
    path = BASELINE_DIR / file_name
    if not path.exists():
        pytest.skip(f"config 相关基准未生成（{file_name}），跳过对照")
    payload = json.loads(path.read_text(encoding="utf-8"))
    diffs = []
    total = 0
    for group, cases in payload["groups"].items():
        for case in cases:
            total += 1
            got = resolver(group, case)
            if got != case["result"]:
                diffs.append(f"{group}::{case['id']}\n  old={case['result']!r}\n  new={got!r}")
    assert not diffs, f"与旧实现基准不一致 {len(diffs)}/{total} 处:\n" + "\n".join(diffs[:20])


def test_baseline_loader_matrix():
    """基准对照：loader 解析矩阵（parsers/loader 的公开等价面）逐用例一致。"""
    _compare_baseline("loader.json", _baseline_loader_result)


def test_baseline_defaults_matrix():
    """基准对照：thinking 映射与 prompt 模板逐用例一致。"""
    _compare_baseline("defaults.json", _baseline_defaults_result)
