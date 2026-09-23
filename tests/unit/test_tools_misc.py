"""tools 小工具族自测 —— T3.7 交付物（WebSearch / TodoWrite / GoalComplete）。

行为金标准：`specs/tools-websearch/spec.md`（6 Requirement / 17 场景）与
`specs/tools-todo/spec.md`（9 Requirement / 28 场景）——两 spec 合并映射表如下
（45 场景全覆盖；标注「委派」的场景其可观察行为落在其它积木的消费侧，本文件按
契约面/等价通道验证并给出同渠道的替代断言；`（怪癖补充）`/`（定义面）`两行是
spec 正文之外的补充断言）。另有非 Scenario 用例：结构守卫（无模块级可变状态、
依赖纯净、`__all__` 可解析）、清理入口的可调用形态、后台管理器端口形态。

### tools-websearch

| spec Requirement | Scenario | 用例 |
|---|---|---|
| R1 工具名与参数契约 | 只传必填参数 | `test_num_default_when_omitted` |
| | num 非法 | `test_num_invalid_returns_error_without_network` |
| | num 上限裁剪 | `test_num_over_limit_is_clamped_silently` |
| R2 密钥与接口地址来源 | 已配置密钥与自定义地址 | `test_custom_url_and_key_header` |
| | 未配置密钥 | `test_missing_key_returns_error_without_network` |
| | 未配置地址回落默认值 | `test_missing_url_falls_back_to_default` |
| R3 接口调用协议与超时 | 请求构造 | `test_request_construction` |
| | Markdown 响应解析 | `test_markdown_response_parsed` |
| | 超时保护 | `test_timeout_becomes_error_text` |
| R4 排序与结果格式化 | 标题命中优先 | `test_title_hit_ranks_first` |
| | 无查询词的中性打分 | `test_neutral_score_keeps_api_order` |
| | 截断与空白压缩 | `test_title_truncation_and_description_compression` |
| | 摘要为空 | `test_empty_description_prints_two_lines` |
| R5 错误与空结果文案 | 接口异常 | `test_api_exception_becomes_error_text` |
| | 无结果 | `test_no_parsable_result_returns_empty_text` |
| R6 兼容性怪癖保持 | 无密钥与接口故障文案相近 | `test_no_key_text_has_no_reason` |
| | 地域参数固定 | `test_zone_is_fixed_cn` |
| | （怪癖补充）先排序再截取 | `test_sorts_all_results_before_slicing` |
| | （怪癖补充）超上限/超长静默裁剪 | `test_num_over_limit_is_clamped_silently` / `test_title_truncation_and_description_compression` |
| R1（定义面） | 工具定义契约 | `test_websearch_definition_contract` |
| | 缺必填参数（经执行入口） | `test_entry_missing_query_gives_friendly_param_error` |

### tools-todo

| spec Requirement | Scenario | 用例 |
|---|---|---|
| R1 工具名与参数契约 | 提交完整计划 | `test_submitted_list_replaces_plan` |
| | 三种状态语义 | `test_three_status_semantics` |
| | （定义面）| `test_todo_write_definition_contract` |
| R2 校验与错误文案 | 空列表 | `test_empty_todos_returns_error_without_side_effect` |
| | 项不是对象 | `test_item_not_object_reports_index` |
| | 缺必填字段 | `test_missing_required_field_reports_field` |
| | 非法状态值 | `test_illegal_status_reports_value_without_side_effect` |
| R3 进行中多项自动修正 | 多个进行中降级 | `test_multiple_in_progress_demoted` |
| | 单个进行中不提示 | `test_single_in_progress_has_no_fix_prefix` |
| R4 返回形态（未完成清单） | 全部完成 | `test_all_completed_returns_done_text` |
| | 存在未完成项 | `test_unfinished_list_lists_only_unfinished` |
| R5 状态同步 | 上下文同步 | `test_plan_tracker_holds_submitted_list` |
| | 终端显示三态 | `test_display_lines_three_states` |
| | 静默模式跳过显示 | `test_display_payload_is_separate_from_llm_text`（委派：UI 层渲染 `ui_text` 时按静默开关跳过，工具侧只产出负载） |
| | 无上下文直接调用 | `test_direct_call_without_context_skips_sync_and_display` |
| R6 GoalComplete 可见性 | 普通模式不可见 | `test_goal_complete_hidden_from_default_definitions` |
| | 目标模式注入与移除 | `test_goal_complete_definition_injected_on_demand` |
| | 注入移除幂等 | `test_goal_complete_injection_is_idempotent` |
| | 执行能力始终注册 | `test_goal_complete_execution_always_registered` |
| R7 GoalComplete 行为 | 首次调用且计划未勾选 | `test_first_call_warns_and_does_not_mark` |
| | 提醒后再次调用 | `test_second_call_marks_without_warning` |
| | 计划全部完成 | `test_all_completed_plan_marks_directly` |
| | 清理后台任务 | `test_mark_cleans_background_tasks` |
| | 无上下文调用 | `test_call_without_context_returns_text_only` |
| | （提醒文案上限）| `test_warning_lists_at_most_five_items` |
| R8 完成标记的主循环消费 | 声明完成后停止续跑 | `test_goal_mark_consume_resets`（委派：续跑注入由 conversation 消费 `consume()`） |
| | 新任务复位 | `test_new_task_reset_allows_warning_again` |
| R9 兼容性怪癖保持 | 就地修正 | `test_demotion_mutates_submitted_items_in_place` |
| | 提醒仅一次 | `test_second_call_marks_without_warning` |
| | 正在前缀去重 | `test_in_progress_prefix_not_duplicated` |
| | （怪癖补充）提醒标志各自独立 | `test_plan_and_bg_warning_flags_are_independent` |
"""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from narnat_agent.contracts.tool import Tool, ToolEnv
from narnat_agent.tools import plan, websearch
from narnat_agent.tools.env import (
    GoalStateImpl,
    PlanTrackerImpl,
    ReminderStateImpl,
    ToolEnvImpl,
    ToolSettingsImpl,
)
from narnat_agent.tools.registry import ToolRegistry

TOOLS_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "tools"

CUSTOM_URL = "https://example.com/mcp"


# ═══════════════════════════════════════════════════════════════
# 测试替身
# ═══════════════════════════════════════════════════════════════


class FakeResponse:
    """最小 `urlopen` 响应替身（支持 with 与 read，不发起真实请求）。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class UrlopenRecorder:
    """记录 `urlopen` 收到的请求与超时参数；`body` 为异常时抛出该异常。"""

    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.requests: list[object] = []
        self.timeouts: list[object] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.body, Exception):
            raise self.body
        return FakeResponse(self.body)


class FakeTheme:
    """计划状态行颜色来源替身：用可辨识标记替代 ANSI 序列。"""

    def __init__(self) -> None:
        self.c_success = "<OK>"
        self.c_warning = "<WARN>"
        self.c_secondary = "<MUTED>"
        self.d = "<DIM>"
        self.b = "<BOLD>"
        self.r = "<RST>"


class FakeBackground:
    """受管后台任务端口替身：记录 `cleanup_all` 调用（可配置为抛错）。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def cleanup_all(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def api_payload(*texts: str) -> bytes:
    """构造搜索接口的成功响应体（result.content 为若干 text 块）。"""
    return json.dumps({
        "result": {"content": [{"type": "text", "text": text} for text in texts]}
    }).encode("utf-8")


def api_text(*items: tuple[str, str, str]) -> str:
    """构造接口返回的 Markdown 文本（`### N. 标题` / `- **URL**: …` / 摘要行）。"""
    blocks = []
    for index, (title, url, description) in enumerate(items, 1):
        lines = [f"### {index}. {title}", f"- **URL**: {url}"]
        if description:
            lines.append(description)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def search_env(api_keys: dict[str, str] | None = None) -> ToolEnvImpl:
    return ToolEnvImpl(settings=ToolSettingsImpl(api_keys=dict(api_keys or {})))


def run_search(args: dict, api_keys: dict[str, str] | None = None):
    return websearch.WebSearchTool().execute(args, search_env(api_keys))


def patch_urlopen(monkeypatch, recorder: UrlopenRecorder) -> UrlopenRecorder:
    """替换 urllib 的 urlopen（不联网）；monkeypatch 结束后自动还原。"""
    monkeypatch.setattr(websearch.urllib.request, "urlopen", recorder)
    return recorder


def forbid_network(monkeypatch) -> None:
    """任何网络调用立即失败（用于「不发起网络调用」的断言）。"""

    def _boom(*args, **kwargs):
        raise AssertionError("本用例不应发起网络调用")

    monkeypatch.setattr(websearch.urllib.request, "urlopen", _boom)


def plan_env() -> ToolEnvImpl:
    return ToolEnvImpl(plan=PlanTrackerImpl(), goal=GoalStateImpl(),
                       reminders=ReminderStateImpl())


def todo_args(*items: tuple[str, str]) -> dict:
    return {"todos": [{"content": content, "status": status} for content, status in items]}


# ═══════════════════════════════════════════════════════════════
# WebSearch —— R1 工具名与参数契约
# ═══════════════════════════════════════════════════════════════


def test_websearch_definition_contract():
    """工具名/描述/参数面的已发布契约（query 必填、num 可选默认 5 上限 20）。"""
    tool = websearch.WebSearchTool()
    assert isinstance(tool, Tool)
    definition = tool.definition()
    assert definition["type"] == "function"
    function = definition["function"]
    assert function["name"] == "WebSearch"
    assert function["description"] == "网页搜索（用于查找API文档、解决方案、技术文章等）。"
    parameters = function["parameters"]
    assert parameters["required"] == ["query"]
    assert parameters["properties"]["query"] == {"type": "string", "description": "搜索查询词"}
    assert parameters["properties"]["num"] == {
        "type": "integer", "description": "返回结果数量（正整数，默认5，上限20）"}


def test_num_default_when_omitted(monkeypatch):
    """Scenario 只传必填参数：按 num=5 处理。"""
    calls: list[tuple] = []

    def fake_call(query, max_results, api_key, url):
        calls.append((query, max_results, api_key, url))
        return [{"title": "T", "url": "https://a.example", "description": ""}]

    monkeypatch.setattr(websearch, "call_search_api", fake_call)
    result = run_search({"query": "python"}, {"websearch": "sk-x"})
    assert calls == [("python", 5, "sk-x", websearch.DEFAULT_URL)]
    assert result.llm_text == "1. T\n   URL: https://a.example"


@pytest.mark.parametrize("bad", [0, -3, "0", "abc", {"num": 1}])
def test_num_invalid_returns_error_without_network(bad, monkeypatch):
    """Scenario num 非法：返回固定文案且不发起网络调用。"""
    forbid_network(monkeypatch)
    result = run_search({"query": "python", "num": bad}, {"websearch": "sk-x"})
    assert result.llm_text == "[错误: num需为正整数]"
    assert result.is_error is False


def test_num_none_falls_back_to_default(monkeypatch):
    """num 显式传 null 时回落默认 5。"""
    calls: list[int] = []
    monkeypatch.setattr(websearch, "call_search_api",
                        lambda query, max_results, api_key, url: calls.append(max_results) or [])
    run_search({"query": "python", "num": None}, {"websearch": "sk-x"})
    assert calls == [5]


@pytest.mark.parametrize("raw,expected", [("3", 3), (3, 3), ("20", 20), (100, 20), ("100", 20)])
def test_num_over_limit_is_clamped_silently(raw, expected, monkeypatch):
    """Scenario num 上限裁剪：字符串形态容错转换，超上限静默裁剪为 20。"""
    calls: list[int] = []
    monkeypatch.setattr(websearch, "call_search_api",
                        lambda query, max_results, api_key, url: calls.append(max_results) or [])
    result = run_search({"query": "python", "num": raw}, {"websearch": "sk-x"})
    assert calls == [expected]
    assert result.llm_text == "[无搜索结果]"


# ═══════════════════════════════════════════════════════════════
# WebSearch —— R2 接口密钥与接口地址来源
# ═══════════════════════════════════════════════════════════════


def test_custom_url_and_key_header(monkeypatch):
    """Scenario 已配置密钥与自定义地址：请求发往自定义地址且带 X-API-Key。"""
    recorder = patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(api_text(
        ("标题", "https://doc.example/a", "- 摘要")))))
    run_search({"query": "python"}, {"websearch": "sk-x", "websearch_url": CUSTOM_URL})
    request = recorder.requests[0]
    assert request.full_url == CUSTOM_URL
    assert request.get_header("X-api-key") == "sk-x"


def test_missing_url_falls_back_to_default(monkeypatch):
    """Scenario 未配置地址回落默认值：请求发往默认地址。"""
    recorder = patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(api_text(
        ("标题", "https://doc.example/a", "")))))
    run_search({"query": "python"}, {"websearch": "sk-x"})
    assert recorder.requests[0].full_url == "https://api.anysearch.com/mcp"


@pytest.mark.parametrize("keys", [None, {}, {"websearch_url": CUSTOM_URL}, {"websearch": ""}])
def test_missing_key_returns_error_without_network(keys, monkeypatch):
    """Scenario 未配置密钥：返回 `[错误: 搜索失败]` 且不发起网络调用。"""
    forbid_network(monkeypatch)
    result = run_search({"query": "python"}, keys)
    assert result.llm_text == "[错误: 搜索失败]"


def test_no_context_treated_as_missing_key(monkeypatch):
    """上下文缺失（直接调用且无 env）按未配置密钥处理，不发起网络调用。"""
    forbid_network(monkeypatch)
    result = websearch.WebSearchTool().execute({"query": "python"}, None)
    assert result.llm_text == "[错误: 搜索失败]"


def test_keys_are_read_per_call(monkeypatch):
    """密钥与地址每次调用读取、不跨调用缓存。"""
    recorder = patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(api_text(
        ("标题", "https://doc.example/a", "")))))
    tool = websearch.WebSearchTool()
    env_one = search_env({"websearch": "sk-1", "websearch_url": CUSTOM_URL})
    env_two = search_env({"websearch": "sk-2"})
    tool.execute({"query": "a"}, env_one)
    tool.execute({"query": "a"}, env_two)
    first, second = recorder.requests
    assert (first.full_url, first.get_header("X-api-key")) == (CUSTOM_URL, "sk-1")
    assert (second.full_url, second.get_header("X-api-key")) == (
        websearch.DEFAULT_URL, "sk-2")


# ═══════════════════════════════════════════════════════════════
# WebSearch —— R3 接口调用协议与超时
# ═══════════════════════════════════════════════════════════════


def test_request_construction(monkeypatch):
    """Scenario 请求构造：JSON-RPC tools/call + name=search + 固定参数 + 15 秒超时。"""
    recorder = patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(api_text(
        ("标题", "https://doc.example/a", "")))))
    run_search({"query": "python asyncio", "num": 3}, {"websearch": "sk-x"})
    request = recorder.requests[0]
    assert json.loads(request.data.decode("utf-8")) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search",
                   "arguments": {"query": "python asyncio", "max_results": 3, "zone": "cn"}},
    }
    assert request.get_header("Content-type") == "application/json"
    assert recorder.timeouts == [websearch.TIMEOUT_SECONDS] == [15]


def test_zone_is_fixed_cn(monkeypatch):
    """Scenario 地域参数固定（兼容怪癖）：地域字段恒为 `cn`，无配置入口。"""
    recorder = patch_urlopen(monkeypatch, UrlopenRecorder(api_payload("无可解析块")))
    run_search({"query": "python", "num": 1},
               {"websearch": "sk-x", "websearch_url": "https://other.example/mcp",
                "zone": "us"})
    body = json.loads(recorder.requests[0].data.decode("utf-8"))
    assert body["params"]["arguments"]["zone"] == "cn"


def test_markdown_response_parsed(monkeypatch):
    """Scenario Markdown 响应解析：标题/链接/摘要各取对应位置，摘要无前缀残留。"""
    markdown = (
        "结果前言文本（不构成结果块）\n"
        "\n"
        "### 1. Python 入门\n"
        "- **URL**: https://doc.example/a\n"
        "- **Description**: 快速入门\n"
        "\n"
        "### 2. 缺链接的块\n"
        "- 没有 URL 行\n"
        "\n"
        "### 3. 另一篇\n"
        "- **URL**: https://doc.example/b\n"
        "* 摘要文本\n"
    )
    patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(markdown)))
    result = run_search({"query": "python"}, {"websearch": "sk-x"})
    assert result.llm_text == (
        "1. Python 入门\n"
        "   URL: https://doc.example/a\n"
        "   快速入门\n"
        "2. 另一篇\n"
        "   URL: https://doc.example/b\n"
        "   摘要文本"
    )


def test_multiple_text_blocks_are_concatenated(monkeypatch):
    """响应含多个 text 块时拼接后统一解析。"""
    patch_urlopen(monkeypatch, UrlopenRecorder(api_payload(
        "### 1. A\n- **URL**: https://a.example\n- 甲",
        "### 2. B\n- **URL**: https://b.example\n- 乙",
    )))
    result = run_search({"query": "x"}, {"websearch": "sk-x"})
    assert "1. A" in result.llm_text and "2. B" in result.llm_text


def test_timeout_becomes_error_text(monkeypatch):
    """Scenario 超时保护：超时异常按错误文案返回，不向上传播。"""
    patch_urlopen(monkeypatch, UrlopenRecorder(TimeoutError("timed out")))
    result = run_search({"query": "python"}, {"websearch": "sk-x"})
    assert result.llm_text == "[错误: 搜索失败: timed out]"


def test_api_exception_becomes_error_text(monkeypatch):
    """Scenario 接口异常：非 JSON 响应（JSON 解析失败）转固定错误文案。"""
    patch_urlopen(monkeypatch, UrlopenRecorder(b"<html>not json</html>"))
    result = run_search({"query": "python"}, {"websearch": "sk-x"})
    assert result.llm_text.startswith("[错误: 搜索失败: ")


def test_no_parsable_result_returns_empty_text(monkeypatch):
    """Scenario 无结果：无 text 块或无结果块时返回 `[无搜索结果]`。"""
    patch_urlopen(monkeypatch, UrlopenRecorder(
        json.dumps({"result": {"content": []}}).encode("utf-8")))
    assert run_search({"query": "python"}, {"websearch": "sk-x"}).llm_text == "[无搜索结果]"
    patch_urlopen(monkeypatch, UrlopenRecorder(api_payload("这里没有任何结果块")))
    assert run_search({"query": "python"}, {"websearch": "sk-x"}).llm_text == "[无搜索结果]"


# ═══════════════════════════════════════════════════════════════
# WebSearch —— R4 相关性排序与结果格式化
# ═══════════════════════════════════════════════════════════════


def _patch_results(monkeypatch, results: list[dict]) -> None:
    monkeypatch.setattr(websearch, "call_search_api",
                        lambda query, max_results, api_key, url: list(results))


def test_title_hit_ranks_first(monkeypatch):
    """Scenario 标题命中优先：摘要相同、标题命中查询词者排前。"""
    _patch_results(monkeypatch, [
        {"title": "无关标题", "url": "https://a.example", "description": "python asyncio 指南"},
        {"title": "python asyncio 教程", "url": "https://b.example",
         "description": "python asyncio 指南"},
    ])
    result = run_search({"query": "python asyncio"}, {"websearch": "sk-x"})
    assert result.llm_text.splitlines()[0] == "1. python asyncio 教程"
    assert "2. 无关标题" in result.llm_text


def test_neutral_score_keeps_api_order(monkeypatch):
    """Scenario 无查询词的中性打分：全部 0.5 分，保持接口原始顺序（稳定排序）。"""
    assert websearch.extract_query_words("！！！") == set()
    assert websearch.extract_query_words("a") == set()
    assert websearch.relevance_score({"title": "任意"}, "！！！") == 0.5
    _patch_results(monkeypatch, [
        {"title": "甲", "url": "https://a.example", "description": ""},
        {"title": "乙", "url": "https://b.example", "description": ""},
        {"title": "丙", "url": "https://c.example", "description": ""},
    ])
    result = run_search({"query": "！！！"}, {"websearch": "sk-x"})
    assert result.llm_text.splitlines()[0::2] == ["1. 甲", "2. 乙", "3. 丙"]


def test_sorts_all_results_before_slicing(monkeypatch):
    """兼容怪癖：先对全部返回结果排序、再截取前 num 条展示。"""
    _patch_results(monkeypatch, [
        {"title": "无命中一", "url": "https://a.example", "description": ""},
        {"title": "无命中二", "url": "https://b.example", "description": ""},
        {"title": "python 教程", "url": "https://c.example", "description": ""},
    ])
    result = run_search({"query": "python", "num": 2}, {"websearch": "sk-x"})
    lines = result.llm_text.splitlines()
    assert lines[0] == "1. python 教程"
    assert lines[2] == "2. 无命中一"
    assert len(lines) == 4


def test_title_truncation_and_description_compression(monkeypatch):
    """Scenario 截断与空白压缩：标题超 120 截断、摘要压缩空白并截断到 300；均静默。"""
    title = "T" * 200
    description = "word  " * 100 + "\n\n" + "尾" * 100
    _patch_results(monkeypatch, [{"title": title, "url": "https://a.example",
                                  "description": description}])
    lines = run_search({"query": "python"}, {"websearch": "sk-x"}).llm_text.splitlines()
    assert lines[0] == "1. " + "T" * 120 + "…"
    assert lines[1] == "   URL: https://a.example"
    compressed = ("word " * 100 + "尾" * 100)
    assert lines[2] == "   " + compressed[:300] + "…"
    assert len(lines) == 3


def test_empty_description_prints_two_lines(monkeypatch):
    """Scenario 摘要为空：只输出标题行与链接行。"""
    _patch_results(monkeypatch, [{"title": "只有标题", "url": "https://a.example",
                                  "description": ""}])
    result = run_search({"query": "python"}, {"websearch": "sk-x"})
    assert result.llm_text == "1. 只有标题\n   URL: https://a.example"


def test_no_key_text_has_no_reason():
    """Scenario 无密钥与接口故障文案相近（兼容怪癖）：未配置密钥不带原因。"""
    result = run_search({"query": "python"}, {})
    assert result.llm_text == "[错误: 搜索失败]"
    assert not result.llm_text.startswith("[错误: 搜索失败: ")


# ═══════════════════════════════════════════════════════════════
# WebSearch —— 执行入口
# ═══════════════════════════════════════════════════════════════


def test_entry_missing_query_gives_friendly_param_error(monkeypatch):
    """缺必填参数经执行入口转友好提示（与旧实现缺参路径等价）。"""
    forbid_network(monkeypatch)
    registry = ToolRegistry()
    registry.register(websearch.WebSearchTool())
    result = registry.execute("WebSearch", {"num": 3}, search_env({"websearch": "sk-x"}))
    assert result.llm_text == ("[错误: 工具参数错误(WebSearch): 缺少必填参数: query。"
                              "WebSearch 的有效参数: query, num]")
    assert result.is_error is True


def test_entry_executes_registered_tool(monkeypatch):
    """执行入口正常路径：结果无框架标签、无失败标记。"""
    _patch_results(monkeypatch, [{"title": "标题", "url": "https://a.example",
                                  "description": "摘要"}])
    registry = ToolRegistry()
    registry.register(websearch.WebSearchTool())
    result = registry.execute("WebSearch", {"query": "python"}, search_env({"websearch": "sk-x"}))
    assert result.llm_text == "1. 标题\n   URL: https://a.example\n   摘要"
    assert result.is_error is False
    assert result.ui_text is None


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R1 工具名与参数契约
# ═══════════════════════════════════════════════════════════════


def test_todo_write_definition_contract():
    """工具名/描述/参数面的已发布契约（todos 数组含 content/status，整批替换）。"""
    tool = plan.TodoWriteTool()
    assert isinstance(tool, Tool)
    function = tool.definition()["function"]
    assert function["name"] == "TodoWrite"
    assert function["description"] == "创建并管理任务列表。多步任务开始前先与用户同步计划。"
    parameters = function["parameters"]
    assert parameters["required"] == ["todos"]
    item = parameters["properties"]["todos"]["items"]
    assert parameters["properties"]["todos"]["description"] == "任务列表（每次提交完整列表，整体替换）"
    assert item["required"] == ["content", "status"]
    assert item["properties"]["content"]["description"] == "任务描述（祈使句，如'运行测试'）"
    assert item["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]
    assert item["properties"]["status"]["description"] == (
        "任务状态（同时刻最多1个in_progress，多余的自动调整为待处理）")


def test_submitted_list_replaces_plan():
    """Scenario 提交完整计划：该列表整体替换既有计划状态。"""
    env = plan_env()
    env.plan.replace([{"content": "旧任务", "status": "pending"}])
    todos = [{"content": "运行测试", "status": "pending"}]
    result = plan.TodoWriteTool().execute({"todos": todos}, env)
    assert env.plan.current() is todos
    assert result.llm_text == "[你有未完成的任务，请继续:]\n\n1. [待处理] 运行测试"


def test_three_status_semantics():
    """Scenario 三种状态语义：三态分别按待处理/进行中/已完成处理与展示。"""
    env = plan_env()
    result = plan.TodoWriteTool(FakeTheme()).execute(
        todo_args(("已完成项", "completed"), ("进行中项", "in_progress"), ("待处理项", "pending")),
        env)
    assert [todo["status"] for todo in env.plan.current()] == [
        "completed", "in_progress", "pending"]
    assert result.llm_text == ("[你有未完成的任务，请继续:]\n\n"
                              "1. [进行中] 进行中项\n2. [待处理] 待处理项")
    assert result.ui_text.splitlines() == [
        "<OK>✓<RST> <DIM>已完成项<RST>",
        "<WARN>●<RST> <BOLD>正在进行中项<RST>",
        "<MUTED>○<RST> <DIM>待处理项<RST>",
    ]


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R2 校验与错误文案
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("args", [{}, {"todos": None}, {"todos": []}])
def test_empty_todos_returns_error_without_side_effect(args):
    """Scenario 空列表：空/None/缺失均返回固定文案，计划不被改动。"""
    env = plan_env()
    env.plan.replace([{"content": "既有计划", "status": "in_progress"}])
    result = plan.TodoWriteTool(FakeTheme()).execute(args, env)
    assert result.llm_text == "[错误: todos不能为空]"
    assert result.ui_text is None
    assert env.plan.current() == [{"content": "既有计划", "status": "in_progress"}]


def test_item_not_object_reports_index():
    """Scenario 项不是对象：报首个非对象的 1 起序号。"""
    result = plan.TodoWriteTool().execute({"todos": ["运行测试"]}, plan_env())
    assert result.llm_text == "[错误: 第1项不是对象]"
    result = plan.TodoWriteTool().execute(
        {"todos": [{"content": "甲", "status": "pending"}, "乙"]}, plan_env())
    assert result.llm_text == "[错误: 第2项不是对象]"


def test_missing_required_field_reports_field():
    """Scenario 缺必填字段：报首个缺失字段（content 先于 status 检查）。"""
    args = {"todos": [{"content": "甲", "status": "pending"}, {"content": "乙"}]}
    assert plan.TodoWriteTool().execute(args, plan_env()).llm_text == (
        "[错误: 第2项缺少必填字段: status]")
    assert plan.TodoWriteTool().execute(
        {"todos": [{"status": "pending"}]}, plan_env()).llm_text == (
        "[错误: 第1项缺少必填字段: content]")


@pytest.mark.parametrize("bad", ["done", "", None, "进行中", "PENDING"])
def test_illegal_status_reports_value_without_side_effect(bad):
    """Scenario 非法状态值：返回固定文案且计划不被改动。"""
    env = plan_env()
    env.plan.replace([{"content": "既有计划", "status": "in_progress"}])
    result = plan.TodoWriteTool(FakeTheme()).execute({"todos": [{"content": "甲", "status": bad}]}, env)
    assert result.llm_text == f"[错误: 第1项status非法: {bad}]"
    assert result.ui_text is None
    assert env.plan.current() == [{"content": "既有计划", "status": "in_progress"}]


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R3 进行中多项自动修正
# ═══════════════════════════════════════════════════════════════


def test_multiple_in_progress_demoted():
    """Scenario 多个进行中降级：保留第一个、其余降级并附自动修正前缀。"""
    env = plan_env()
    todos = todo_args(("甲", "in_progress"), ("乙", "in_progress"), ("丙", "in_progress"))["todos"]
    result = plan.TodoWriteTool().execute({"todos": todos}, env)
    assert [todo["status"] for todo in todos] == ["in_progress", "pending", "pending"]
    assert result.llm_text.startswith(
        "[已自动修正: 检测到多个in_progress，保留第一个，其余2项调整为待处理]\n")
    assert result.llm_text.endswith(
        "[你有未完成的任务，请继续:]\n\n1. [进行中] 甲\n2. [待处理] 乙\n3. [待处理] 丙")


def test_single_in_progress_has_no_fix_prefix():
    """Scenario 单个进行中不提示：无自动修正前缀。"""
    result = plan.TodoWriteTool().execute(todo_args(("甲", "in_progress")), plan_env())
    assert "已自动修正" not in result.llm_text
    assert result.llm_text == "[你有未完成的任务，请继续:]\n\n1. [进行中] 甲"


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R4 返回形态（未完成清单）
# ═══════════════════════════════════════════════════════════════


def test_all_completed_returns_done_text():
    """Scenario 全部完成：返回 `[任务全部完成]`。"""
    result = plan.TodoWriteTool().execute(
        todo_args(("甲", "completed"), ("乙", "completed")), plan_env())
    assert result.llm_text == "[任务全部完成]"


def test_unfinished_list_lists_only_unfinished():
    """Scenario 存在未完成项：只列未完成项并重新编号（进行中/待处理标签）。"""
    result = plan.TodoWriteTool().execute(
        todo_args(("进行项", "in_progress"), ("待办项", "pending"), ("已办项", "completed")),
        plan_env())
    assert result.llm_text == (
        "[你有未完成的任务，请继续:]\n\n1. [进行中] 进行项\n2. [待处理] 待办项")


def test_all_completed_with_fix_prefix_keeps_prefix():
    """自动修正前缀置于清单最前（全部完成时前缀 + 完成文案）。"""
    result = plan.TodoWriteTool().execute(
        todo_args(("甲", "completed"), ("乙", "in_progress"), ("丙", "in_progress")), plan_env())
    assert result.llm_text == (
        "[已自动修正: 检测到多个in_progress，保留第一个，其余1项调整为待处理]\n"
        "[你有未完成的任务，请继续:]\n\n1. [进行中] 乙\n2. [待处理] 丙")


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R5 状态同步（工具上下文与终端显示）
# ═══════════════════════════════════════════════════════════════


def test_plan_tracker_holds_submitted_list():
    """Scenario 上下文同步：上下文中的当前计划即本次提交列表（供计划优先判断）。"""
    env = plan_env()
    todos = todo_args(("甲", "in_progress"), ("乙", "pending"))["todos"]
    plan.TodoWriteTool().execute({"todos": todos}, env)
    assert env.plan.current() is todos
    assert any(todo["status"] == "in_progress" for todo in env.plan.current())


def test_display_lines_three_states():
    """Scenario 终端显示三态：图标与颜色角色（成功色/警告色/次要色、暗淡/加粗）。"""
    result = plan.TodoWriteTool(FakeTheme()).execute(
        todo_args(("做完了", "completed"), ("做一半", "in_progress"), ("还没做", "pending")),
        plan_env())
    assert result.ui_text == (
        "<OK>✓<RST> <DIM>做完了<RST>\n"
        "<WARN>●<RST> <BOLD>正在做一半<RST>\n"
        "<MUTED>○<RST> <DIM>还没做<RST>")


def test_in_progress_prefix_not_duplicated():
    """Scenario 正在前缀去重（兼容怪癖）：内容已带「正在」时不叠加。"""
    result = plan.TodoWriteTool(FakeTheme()).execute(
        todo_args(("正在安装依赖", "in_progress")), plan_env())
    assert result.ui_text == "<WARN>●<RST> <BOLD>正在安装依赖<RST>"


def test_display_lines_accept_output_theme():
    """装配注入 `output.Theme` 实例即可用（结构兼容，取当前颜色角色）。"""
    from narnat_agent.output import Console, Theme

    theme = Theme(Console(io.StringIO(), truecolor=True, platform="linux"))
    result = plan.TodoWriteTool(theme).execute(
        todo_args(("做完了", "completed")), plan_env())
    assert result.ui_text == f"{theme.c_success}✓{theme.r} {theme.d}做完了{theme.r}"
    assert result.ui_text != "" and "\x1b[" in result.ui_text


def test_display_payload_is_separate_from_llm_text():
    """Scenario 静默模式跳过显示（渠道分离）：显示负载只在 `ui_text`。

    静默工具模式由 UI 层在渲染 `ui_text` 时跳过（与着色 diff 同一处判定，
    见 specs/tools-file「编辑类工具的返回形态与差分展示」）；工具侧只产出负载，
    `llm_text` 中不含任何计划状态行。
    """
    result = plan.TodoWriteTool(FakeTheme()).execute(
        todo_args(("做完了", "completed"), ("还没做", "pending")), plan_env())
    assert result.ui_text is not None
    assert "✓" not in result.llm_text and "○" not in result.llm_text


def test_direct_call_without_context_skips_sync_and_display():
    """Scenario 无上下文直接调用：不做上下文同步与终端显示，仅返回结果文本。"""
    env = plan_env()
    result = plan.TodoWriteTool(FakeTheme()).execute(todo_args(("甲", "pending")), None)
    assert result.ui_text is None
    assert result.llm_text == "[你有未完成的任务，请继续:]\n\n1. [待处理] 甲"
    assert env.plan.current() == []


# ═══════════════════════════════════════════════════════════════
# GoalComplete —— R6 工具定义与目标模式可见性
# ═══════════════════════════════════════════════════════════════


def test_goal_complete_definition_contract():
    """工具定义契约：无参数表 + 调用条件说明。"""
    tool = plan.GoalCompleteTool()
    assert isinstance(tool, Tool)
    assert tool.name == "GoalComplete"
    function = tool.definition()["function"]
    assert function["name"] == "GoalComplete"
    assert function["parameters"] == {"type": "object", "properties": {}, "required": []}
    assert function["description"] == (
        "声明当前目标任务已完成——任务执行的最后一步调用。\n"
        "调用前必须全部满足：\n"
        "1. 任务目标已实际达成，关键结果已经真实验证，而非仅凭推理判断；\n"
        "2. 最终答复已完整写出。\n"
        "调用方式：在输出最终答复后调用本工具。\n"
        "收到工具结果后只用一两句话简短收尾，不要重复完整答复内容。"
    )


def _registered_registry() -> ToolRegistry:
    registry = ToolRegistry()
    plan.register_plan_tools(registry)
    return registry


def _definition_names(registry: ToolRegistry, *, include_goal: bool = False) -> list[str]:
    return [d["function"]["name"] for d in registry.get_tool_definitions(include_goal=include_goal)]


def test_goal_complete_hidden_from_default_definitions():
    """Scenario 普通模式不可见：LLM 工具定义列表中不含 GoalComplete。"""
    registry = _registered_registry()
    assert _definition_names(registry) == ["TodoWrite"]
    assert "GoalComplete" not in _definition_names(registry)


def test_goal_complete_definition_injected_on_demand():
    """Scenario 目标模式注入与移除：include_goal 开关即注入/移除（关闭不再传入）。"""
    registry = _registered_registry()
    assert "GoalComplete" in _definition_names(registry, include_goal=True)
    assert "GoalComplete" not in _definition_names(registry, include_goal=False)


def test_goal_complete_injection_is_idempotent():
    """Scenario 注入移除幂等：重复取用不出现重复定义、也不报错。"""
    registry = _registered_registry()
    names = _definition_names(registry, include_goal=True)
    assert names.count("GoalComplete") == 1
    assert _definition_names(registry, include_goal=True) == names
    assert _definition_names(registry) == ["TodoWrite"]
    assert _definition_names(registry, include_goal=True) == names
    assert registry.register(plan.GoalCompleteTool(), expose_definition=False) is False


def test_goal_complete_execution_always_registered():
    """Scenario 执行能力始终注册：普通模式下调用不返回未知工具错误。"""
    registry = _registered_registry()
    env = plan_env()
    assert registry.has_tool("GoalComplete") is True
    result = registry.execute("GoalComplete", {}, env)
    assert result.llm_text == plan.GOAL_COMPLETE_TEXT
    assert result.is_error is False
    assert env.goal.is_set is True


# ═══════════════════════════════════════════════════════════════
# GoalComplete —— R7 行为（收尾软提醒、完成标记与后台清理）
# ═══════════════════════════════════════════════════════════════


def test_first_call_warns_and_does_not_mark():
    """Scenario 首次调用且计划未勾选：不置完成标记，返回提醒文本并置位提醒标志。"""
    env = plan_env()
    env.plan.replace(todo_args(("甲", "pending"), ("乙", "in_progress"))["todos"])
    result = plan.GoalCompleteTool().execute({}, env)
    assert result.llm_text == (
        "[提醒] 计划仍有未勾选完成的项: 甲、乙。"
        "若这些任务实际已完成，请先调用TodoWrite勾选它们（并把仍需继续的项标记为进行中）"
        "，然后再次调用GoalComplete；若确实未完成或刻意跳过，"
        "请再次调用GoalComplete并在最终总结中向用户说明原因。"
    )
    assert env.goal.is_set is False


def test_warning_lists_at_most_five_items():
    """提醒文案列出前 5 项，多于 5 项时追加 `等共{N}项`。"""
    env = plan_env()
    items = [(f"任务{i}", "pending") for i in range(1, 8)]
    env.plan.replace(todo_args(*items)["todos"])
    result = plan.GoalCompleteTool().execute({}, env)
    assert result.llm_text.startswith(
        "[提醒] 计划仍有未勾选完成的项: 任务1、任务2、任务3、任务4、任务5等共7项。")


def test_second_call_marks_without_warning():
    """Scenario 提醒后再次调用 / 提醒仅一次（兼容怪癖）：直接置完成标记。"""
    env = plan_env()
    env.plan.replace(todo_args(("甲", "pending"))["todos"])
    tool = plan.GoalCompleteTool()
    tool.execute({}, env)
    result = tool.execute({}, env)
    assert result.llm_text == plan.GOAL_COMPLETE_TEXT
    assert env.goal.is_set is True


def test_all_completed_plan_marks_directly():
    """Scenario 计划全部完成：直接置完成标记并返回固定文本，无提醒。"""
    env = plan_env()
    env.plan.replace(todo_args(("甲", "completed"))["todos"])
    result = plan.GoalCompleteTool().execute({}, env)
    assert result.llm_text == plan.GOAL_COMPLETE_TEXT
    assert env.goal.is_set is True


def test_mark_cleans_background_tasks():
    """Scenario 清理后台任务：置完成标记时清理受管后台任务（每次调用一次）。"""
    background = FakeBackground()
    env = plan_env()
    env.plan.replace(todo_args(("甲", "completed"))["todos"])
    plan.GoalCompleteTool(background).execute({}, env)
    assert background.calls == 1


def test_warning_path_does_not_clean_background_tasks():
    """提醒路径不置位、不清理（让 AI 补勾选后再调用）。"""
    background = FakeBackground()
    env = plan_env()
    env.plan.replace(todo_args(("甲", "pending"))["todos"])
    plan.GoalCompleteTool(background).execute({}, env)
    assert background.calls == 0


def test_cleanup_failure_is_ignored():
    """清理失败静默忽略：仍返回固定文本并置位完成标记。"""
    background = FakeBackground(RuntimeError("清理失败"))
    env = plan_env()
    result = plan.GoalCompleteTool(background).execute({}, env)
    assert result.llm_text == plan.GOAL_COMPLETE_TEXT
    assert env.goal.is_set is True


def test_cleanup_accepts_plain_callable():
    """清理入口也接受同签名的可调用对象（如绑定方法）。"""
    calls: list[str] = []
    env = plan_env()
    plan.GoalCompleteTool(lambda: calls.append("cleanup")).execute({}, env)
    assert calls == ["cleanup"]


def test_shell_background_manager_matches_port():
    """端口形态与 `tools.shell` 的后台管理器一致（装配直接注入管理器实例）。"""
    from narnat_agent.tools.shell.background import BackgroundManager

    assert callable(getattr(BackgroundManager, "cleanup_all", None))


def test_call_without_context_returns_text_only():
    """Scenario 无上下文调用：返回固定文本且不产生任何状态写入。"""
    env = plan_env()
    result = plan.GoalCompleteTool().execute({}, None)
    assert result.llm_text == plan.GOAL_COMPLETE_TEXT
    assert result.ui_text is None
    assert env.goal.is_set is False
    assert env.reminders.try_trigger_plan() is True  # 提醒标志未被消耗


# ═══════════════════════════════════════════════════════════════
# GoalComplete —— R8 目标完成标记的主循环消费
# ═══════════════════════════════════════════════════════════════


def test_goal_mark_consume_resets():
    """Scenario 声明完成后停止续跑：消费即复位（主循环据其结束自动续跑）。"""
    env = plan_env()
    plan.GoalCompleteTool().execute({}, env)
    assert env.goal.is_set is True
    assert env.goal.consume() is True   # 本轮已声明完成 → 停止续跑
    assert env.goal.is_set is False
    assert env.goal.consume() is False  # 复位后不再重复消费


def test_new_task_reset_allows_warning_again():
    """Scenario 新任务复位：完成标记与提醒标志复位后，新任务可再次触发提醒。"""
    env = plan_env()
    env.plan.replace(todo_args(("甲", "pending"))["todos"])
    tool = plan.GoalCompleteTool()
    tool.execute({}, env)                       # 首次提醒
    tool.execute({}, env)                       # 二次调用 → 置完成标记
    env.goal.reset()
    env.reminders.reset()
    assert env.goal.is_set is False
    env.plan.replace(todo_args(("新任务", "pending"))["todos"])
    assert tool.execute({}, env).llm_text.startswith("[提醒] 计划仍有未勾选完成的项: 新任务。")


def test_plan_and_bg_warning_flags_are_independent():
    """兼容怪癖：计划提醒与后台任务提醒各自独立计数、互不影响。"""
    reminders = ReminderStateImpl()
    assert reminders.try_trigger_plan() is True
    assert reminders.try_trigger_plan() is False
    assert reminders.try_trigger_bg() is True
    assert reminders.try_trigger_plan() is False
    assert reminders.try_trigger_bg() is False


# ═══════════════════════════════════════════════════════════════
# TodoWrite —— R9 兼容性怪癖保持
# ═══════════════════════════════════════════════════════════════


def test_demotion_mutates_submitted_items_in_place():
    """Scenario 就地修正（兼容怪癖）：提交列表元素被就地改写且反映到计划状态。"""
    env = plan_env()
    todos = todo_args(("甲", "in_progress"), ("乙", "in_progress"))["todos"]
    plan.TodoWriteTool().execute({"todos": todos}, env)
    assert todos[1]["status"] == "pending"
    assert env.plan.current()[1] is todos[1]
    assert env.plan.current()[1]["status"] == "pending"


def test_error_text_index_starts_at_one():
    """兼容怪癖：校验错误文案的项序号从「第1项」起计。"""
    result = plan.TodoWriteTool().execute({"todos": [{"content": "甲", "status": "完成"}]}, plan_env())
    assert result.llm_text == "[错误: 第1项status非法: 完成]"


# ═══════════════════════════════════════════════════════════════
# 结构：无模块级可变状态、只依赖 contracts（tools 积木分层规则）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("filename", ["plan.py", "websearch.py"])
def test_no_module_level_mutable_state(filename):
    """模块顶层赋值必须为全大写常量（无模块级可变状态）。"""
    tree = ast.parse((TOOLS_DIR / filename).read_text(encoding="utf-8"), filename=filename)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    assert names, "未发现模块级常量（预期存在固定文案/口径常量）"
    assert all(name.isupper() or name.startswith("__") for name in names), names


@pytest.mark.parametrize("filename", ["plan.py", "websearch.py"])
def test_dependencies_stay_within_tools_and_contracts(filename):
    """依赖纯净：禁止绝对导入新包；跨积木相对导入只允许 contracts。"""
    tree = ast.parse((TOOLS_DIR / filename).read_text(encoding="utf-8"), filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("narnat_agent"), alias.name
        elif isinstance(node, ast.ImportFrom) and node.level >= 2:
            assert (node.module or "").startswith("contracts"), node.module


def test_public_surface_is_documented():
    """`__all__` 中每个名字均可从模块解析（对外面显式）。"""
    for module in (plan, websearch):
        for name in module.__all__:
            assert hasattr(module, name), f"{module.__name__}.{name}"
