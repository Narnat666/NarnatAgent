"""llm 积木自测 —— specs/llm 65 场景逐条覆盖 + 基线对照（全离线）。

覆盖分组（与 `openspec/changes/recast-v2/specs/llm/spec.md` 的 Requirement 一一对应，
映射表见实施报告）：
 1. 协议选择与请求构建          11. 完成原因取值与协议映射
 2. Anthropic 兼容请求体组装    12. 重试、退避与重试通知
 3. Thinking 参数映射规则       13. 错误事件与文案
 4. Thinking 厂商参数表         14. 上下文超限识别
 5. 思考回传格式与开关          15. 流中断上报与静默挂死检测
 6. 消息格式转换                16. 取消与中断
 7. 工具定义传递与转换          17. 工具定义动态管理
 8. 文本与思考事件流            18. 原始响应取证
 9. 工具调用流式聚合            19. 请求裁剪选项
10. 用量事件归一                20. 兼容性怪癖保持

离线手法：OpenAI 后端注入 fake SDK 客户端（脚本化返回 chunk 序列或抛真实 SDK 异常
实例），Anthropic 后端注入 `httpx.MockTransport` 客户端工厂（脚本化返回 SSE 响应或
抛真实 httpx 异常）；退避等待用注入的假时钟替代真实 sleep。全测试不发起网络请求。

注入契约：thinking 解析能力的唯一权威是 `config.defaults`（`llm` 不内置映射表副本），
故本文件从 `config.defaults` 取解析函数/表，并在构造 `LLMClient` / `LLMRuntime` 时注入。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from narnat_agent.config import defaults
from narnat_agent.contracts.llm_events import (
    KEY_CONTENT,
    KEY_FINISH_REASON,
    KEY_RETRY_NOTICE,
    KEY_STREAM_INTERRUPTED,
    KEY_THINKING,
    KEY_THINKING_SIGNATURE,
    KEY_TOOL_CALLS,
    KEY_USAGE,
    get_stream_interrupted,
)
from narnat_agent.llm import (
    CONTEXT_OVERFLOW_HINTS,
    RETRY_BACKOFF_BASE,
    RETRY_POLL_SECONDS,
    STREAM_END,
    STREAM_POLL_SECONDS,
    STREAM_STALL_SECONDS,
    SYNTHETIC_THINKING,
    AnthropicBackend,
    LLMClient,
    LLMRuntime,
    OpenAIBackend,
    backoff_base,
    clamp_retry_count,
    classify_stream_error,
    is_context_overflow,
    is_retryable_http,
    iter_to_queue,
    retry_notice,
    retry_sleep,
    run_cancelable,
    strip_surrogates,
)
from narnat_agent.llm import client as client_module
from narnat_agent.llm import retry as retry_module

BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline" / "data"


# ═══════════════════════════════════════════════════════════════
# 测试替身（配置 / 运行状态 / 传输层注入点）
# ═══════════════════════════════════════════════════════════════


@dataclass
class FakeConfig:
    """配置面替身（字段对齐 `llm.LLMConfig`）。"""

    protocol: str = "openai"
    model: str = "deepseek-v4-flash"
    api_key: str = "test-key"
    base_url: str = "https://api.example.com"
    temperature: float | None = None
    max_tokens: int | None = None
    retry_count: int = 3
    thinking_enabled: bool = True
    thinking_effort: str = "high"
    thinking_passback: bool = True


def make_runtime(tool_defs=None, thinking_rules=defaults, max_retries=3) -> LLMRuntime:
    """构造运行时状态；thinking 解析能力默认注入 `config.defaults`（必需注入项）。"""
    return LLMRuntime(tool_defs=tool_defs, thinking_rules=thinking_rules,
                      max_retries=max_retries)


class FakeOpenAI:
    """OpenAI 后端的 SDK 注入点：记录 create 参数 + 按脚本返回流/抛异常。"""

    def __init__(self, script=None, runtime: LLMRuntime | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.tools_at_call: list[list[str]] = []
        self.runtime = runtime
        self.handle_during_call = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        self.tools_at_call.append(
            [d.get("function", {}).get("name") for d in kwargs.get("tools", [])])
        if self.runtime is not None:
            self.handle_during_call = self.runtime._active_handle
        item = self.script.pop(0) if self.script else []
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def openai_chunk(content=None, *, reasoning=None, reasoning_in_extra=False,
                 tool_calls=None, finish_reason=None, usage=None, with_choices=True):
    """构造 OpenAI 流式 chunk 替身（字段齐备，语义同 SDK 对象）。"""
    model_extra = {}
    if reasoning_in_extra:
        model_extra["reasoning_content"] = reasoning
        reasoning = None
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            model_extra=model_extra or None, tool_calls=tool_calls)
    choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)] if with_choices else []
    return SimpleNamespace(usage=usage, choices=choices)


def openai_usage(prompt=10, completion=5, cached=None, extra_cached=None):
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    extra = {"prompt_cache_hit_tokens": extra_cached} if extra_cached is not None else None
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           prompt_tokens_details=details, model_extra=extra)


def tool_delta(index=0, id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=arguments))


def sdk_error(cls, status: int, message: str = "boom", body=None):
    """构造真实 openai SDK 异常实例（离线，附 httpx 响应）。"""
    request = httpx.Request("POST", "http://api.example.com/chat/completions")
    response = httpx.Response(status, request=request, text=message)
    return cls(message, response=response, body=body)


def connection_error(cls=openai.APIConnectionError):
    return cls(request=httpx.Request("POST", "http://api.example.com/chat/completions"))


class FailingStream(httpx.SyncByteStream):
    """先产出给定字节块，然后抛出指定异常（模拟服务端中途断开）。"""

    def __init__(self, chunks, exc):
        self._chunks = list(chunks)
        self._exc = exc

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        raise self._exc


class BlockingStream(httpx.SyncByteStream):
    """先产出给定字节块，然后阻塞直到被关闭（模拟流挂死）。"""

    def __init__(self, chunks, wait_seconds: float = 5.0):
        self._chunks = list(chunks)
        self._closed = threading.Event()
        self._wait_seconds = wait_seconds

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        self._closed.wait(self._wait_seconds)

    def close(self):
        self._closed.set()


class SilentStream(httpx.SyncByteStream):
    """不产出任何数据，静默一段真实时间后正常结束（模拟首字节前静默）。"""

    def __init__(self, silent_seconds: float):
        self._silent_seconds = silent_seconds

    def __iter__(self):
        time.sleep(self._silent_seconds)
        return
        yield b""  # pragma: no cover


class FakeHTTP:
    """Anthropic 后端的 httpx 注入点：记录请求 + 按脚本返回响应/抛异常。"""

    def __init__(self, script=None, runtime: LLMRuntime | None = None):
        self.script = list(script or [])
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.Client] = []
        self.runtime = runtime
        self.handle_during_request = "未观测"

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.runtime is not None:
            self.handle_during_request = self.runtime._active_handle
        if not self.script:
            return httpx.Response(200, content=b"")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def factory(self) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(self._handler))
        self.clients.append(client)
        return client


def sse_response(events) -> httpx.Response:
    """构造 200 SSE 响应；events 元素为 dict（JSON 序列化）或原始行字符串。"""
    lines = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
        lines.append(f"data: {payload}\n\n")
    return httpx.Response(200, content="".join(lines).encode("utf-8"))


def text_response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status, content=text.encode("utf-8"))


def make_anthropic(config=None, runtime=None, script=None, **kwargs):
    """构造 Anthropic 后端 + 注入点（重试上限默认随配置同步）。"""
    config = config or FakeConfig(protocol="anthropic", model="deepseek-v4-flash")
    runtime = runtime if runtime is not None else make_runtime(max_retries=config.retry_count)
    http = FakeHTTP(script, runtime)
    backend = AnthropicBackend(config, runtime, None, client_factory=http.factory, **kwargs)
    return backend, http, runtime


def make_openai(config=None, runtime=None, script=None, **kwargs):
    """构造 OpenAI 后端 + 注入点（重试上限默认随配置同步）。"""
    config = config or FakeConfig(protocol="openai", model="deepseek-v4-flash")
    runtime = runtime if runtime is not None else make_runtime(max_retries=config.retry_count)
    fake = FakeOpenAI(script, runtime)
    backend = OpenAIBackend(config, runtime, None, client=fake, **kwargs)
    return backend, fake, runtime


def a_text(text, index=0):
    return {"type": "content_block_delta", "index": index,
            "delta": {"type": "text_delta", "text": text}}


def a_thinking(text, index=0):
    return {"type": "content_block_delta", "index": index,
            "delta": {"type": "thinking_delta", "thinking": text}}


def a_tool_use(id_, name, index=1):
    return {"type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "id": id_, "name": name}}


def a_thinking_block(index=0):
    return {"type": "content_block_start", "index": index,
            "content_block": {"type": "thinking"}}


def a_json(partial, index=1):
    return {"type": "content_block_delta", "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial}}


def a_signature(signature, index=0):
    return {"type": "content_block_delta", "index": index,
            "delta": {"type": "signature_delta", "signature": signature}}


def a_message_delta(stop_reason=None, usage=None):
    delta = {}
    if stop_reason is not None:
        delta["stop_reason"] = stop_reason
    return {"type": "message_delta", "delta": delta, "usage": usage or {}}


def a_message_start(usage=None):
    return {"type": "message_start", "message": {"usage": usage or {}}}


class FakeTime:
    """假时钟：把退避等待变成即时推进（替代真实 sleep）。"""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.0)


def fast_time(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(retry_module, "time", fake)
    return fake


def recording_backend_class(created, events=()):
    class RecordingBackend:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.seen_messages = None
            self.seen_flags = None
            created.append(self)

        def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None):
            self.seen_messages = messages
            self.seen_flags = (no_tools, no_thinking)
            return iter(events)

    return RecordingBackend


# ═══════════════════════════════════════════════════════════════
# 1. 协议选择与请求构建
# ═══════════════════════════════════════════════════════════════


def test_protocol_selects_anthropic(monkeypatch):
    """Scenario 协议选择：协议值为 anthropic → 走 Anthropic 兼容协议。"""
    created: list = []
    monkeypatch.setattr(client_module, "AnthropicBackend", recording_backend_class(created))
    monkeypatch.setattr(client_module, "OpenAIBackend", recording_backend_class([]))
    LLMClient(FakeConfig(protocol="anthropic"), tool_definitions=[], thinking_rules=defaults)
    assert len(created) == 1
    assert created[0].args[0].protocol == "anthropic"


@pytest.mark.parametrize("protocol", ["openai", "OpenAI", "Anthorpic", "azure", ""])
def test_protocol_unknown_falls_back_to_openai(monkeypatch, protocol):
    """Scenario 协议选择：其他任意值（含拼写错误）静默走 OpenAI 兼容协议，不报错。"""
    created: list = []
    monkeypatch.setattr(client_module, "AnthropicBackend", recording_backend_class([]))
    monkeypatch.setattr(client_module, "OpenAIBackend", recording_backend_class(created))
    LLMClient(FakeConfig(protocol=protocol), tool_definitions=[], thinking_rules=defaults)
    assert len(created) == 1


def test_strip_surrogates_cleans_messages_without_mutating_caller(monkeypatch):
    """Scenario 非法代理字符清理：发送前被清理，且调用方持有的消息内容不变。"""
    created: list = []
    monkeypatch.setattr(client_module, "OpenAIBackend", recording_backend_class(created))
    monkeypatch.setattr(client_module, "AnthropicBackend", recording_backend_class([]))
    messages = [
        {"role": "system", "content": "系统\ud83d"},
        {"role": "user", "content": ["文本\udce9", {"nested": "\ud800x"}]},
    ]
    client = LLMClient(FakeConfig(), tool_definitions=[], thinking_rules=defaults)
    list(client.chat_stream(messages))
    sent = created[0].seen_messages
    # 孤立代理字符经 surrogatepass 编码为 3 字节后逐个替换为 U+FFFD
    assert sent[0]["content"] == "系统" + "\ufffd" * 3
    assert sent[1]["content"] == ["文本" + "\ufffd" * 3, {"nested": "\ufffd" * 3 + "x"}]
    assert "\ud83d" not in sent[0]["content"]
    # 调用方持有的原列表与元素未被改动
    assert messages[0]["content"] == "系统\ud83d"
    assert messages[1]["content"] == ["文本\udce9", {"nested": "\ud800x"}]


def test_strip_surrogates_keeps_other_types():
    """其他类型原样返回（int/None/tuple 不递归处理）。"""
    value = (1, "a\ud800b")
    assert strip_surrogates(value) == value
    assert strip_surrogates(None) is None
    assert strip_surrogates(7) == 7


def test_openai_request_stream_options_and_timeout():
    """请求以流式发起，携带 stream_options 与固定超时（连接 5 秒/读 300/写 60/池 30）。"""
    backend, fake, _ = make_openai(script=[[openai_chunk(content="hi", finish_reason="stop")]])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    kwargs = fake.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    timeout = kwargs["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 300.0, 60.0, 30.0)
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["messages"] == [{"role": "user", "content": "q"}]


def test_openai_client_auto_retry_disabled(monkeypatch):
    """底层客户端自身的自动重试关闭（重试完全自管）。"""
    recorded: dict = {}

    class Recorder:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", Recorder)
    backend = OpenAIBackend(FakeConfig(api_key="k", base_url="https://x"), make_runtime(), None)
    assert recorded == {"api_key": "k", "base_url": "https://x", "max_retries": 0}
    assert isinstance(backend._client, Recorder)


def test_openai_optional_params_absent_when_unconfigured():
    """Scenario 可选参数缺省：温度与最大输出未配置 → 请求不出现 temperature/max_tokens。"""
    backend, fake, _ = make_openai(script=[[]])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    kwargs = fake.calls[0]
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs


def test_openai_optional_params_present_when_configured():
    """温度与最大输出非空时携带。"""
    config = FakeConfig(temperature=0.3, max_tokens=2048)
    backend, fake, _ = make_openai(config=config, script=[[]])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    assert fake.calls[0]["temperature"] == 0.3
    assert fake.calls[0]["max_tokens"] == 2048


# ═══════════════════════════════════════════════════════════════
# 2. Anthropic 兼容请求体组装
# ═══════════════════════════════════════════════════════════════


def test_anthropic_endpoint_and_auth_headers():
    """Scenario 端点与认证：接口地址末尾斜杠先去除，请求发往 /v1/messages 并带认证头。"""
    config = FakeConfig(protocol="anthropic", base_url="https://api.example.com/anthropic/")
    backend, http, _ = make_anthropic(config=config, script=[sse_response([a_message_delta("end_turn")])])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    request = http.requests[0]
    assert str(request.url) == "https://api.example.com/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"


def test_anthropic_body_optional_fields_absent():
    """Scenario 可选字段缺省：无 system 且工具为空 → 请求体不含 system 与 tools。"""
    backend, http, _ = make_anthropic(script=[sse_response([a_message_delta("end_turn")])])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    body = json.loads(http.requests[0].read())
    assert "system" not in body
    assert "tools" not in body
    assert body["stream"] is True
    assert body["max_tokens"] == 128000
    assert body["model"] == "deepseek-v4-flash"
    assert body["messages"] == [{"role": "user", "content": "q"}]


def test_anthropic_max_tokens_override():
    """Scenario 最大输出覆盖：配置非空时覆盖默认 128000。"""
    config = FakeConfig(protocol="anthropic", max_tokens=4096)
    backend, http, _ = make_anthropic(config=config, script=[sse_response([])])
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    assert json.loads(http.requests[0].read())["max_tokens"] == 4096


def test_anthropic_max_output_tokens_injection():
    """构造注入的最大输出 token 数作为 anthropic 请求体默认真实生效。"""
    config = FakeConfig(protocol="anthropic", max_tokens=None)
    runtime = make_runtime()
    http = FakeHTTP([sse_response([])])
    backend = AnthropicBackend(config, runtime, None, max_output_tokens=64000,
                               client_factory=http.factory)
    list(backend.chat_stream([{"role": "user", "content": "q"}]))
    assert json.loads(http.requests[0].read())["max_tokens"] == 64000


def test_anthropic_request_timeout_shape():
    """请求超时：连接 10 秒、读 300 秒、写 60 秒、连接池 30 秒。"""
    client = AnthropicBackend._new_client()
    try:
        assert (client.timeout.connect, client.timeout.read,
                client.timeout.write, client.timeout.pool) == (10.0, 300.0, 60.0, 30.0)
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════
# 3. Thinking 参数映射规则 / 4. Thinking 厂商参数表
# （唯一权威表在 config.defaults；llm 只消费注入的解析能力）
# ═══════════════════════════════════════════════════════════════


def test_thinking_unmatched_model_has_no_params():
    """Scenario 未匹配模型：请求中不出现任何 thinking 相关参数。"""
    assert defaults.resolve_thinking_params("openai", "unknown-model", True, "high") == ({}, {})
    assert defaults.resolve_thinking_params("anthropic", "unknown-model", True, "high") == ({}, {})
    assert defaults.resolve_thinking_passback("openai", "unknown-model") == "none"


def test_thinking_disable_without_mapping_sends_nothing():
    """Scenario 禁用映射缺失：该厂商条目没有禁用映射 → 不传任何思考参数。"""
    assert defaults.resolve_thinking_params(
        "anthropic", "deepseek-v4-pro", False, "high") == ({}, {})


def test_thinking_effort_injection_with_mapping():
    """Scenario 强度注入：强度经映射转换后写入该厂商指定的参数路径。"""
    body_top, extra = defaults.resolve_thinking_params("openai", "deepseek-v4-pro", True, "high")
    assert body_top == {"reasoning_effort": "high"}
    assert extra == {"thinking": {"type": "enabled"}}


def test_thinking_qwen_effort_map_max_32000():
    """Scenario Qwen 强度映射：enable_thinking=true 与 thinking_budget=32000。"""
    _, extra = defaults.resolve_thinking_params("openai", "qwen3-max", True, "max")
    assert extra == {"enable_thinking": True, "thinking_budget": 32000}


def test_thinking_qwen_effort_map_spec_verbatim():
    """Qwen 强度映射表与 spec 逐字一致（7 档）。"""
    assert defaults.THINKING_PARAM_MAP[("openai", "qwen")]["effort_map"] == {
        "max": 32000, "xhigh": 24000, "high": 16000, "medium": 8000,
        "low": 4000, "minimal": 1000, "none": 0,
    }


def test_thinking_gpt_disabled_uses_reasoning_effort_none():
    """Scenario GPT 关闭思考：请求携带 reasoning_effort="none"。"""
    body_top, extra = defaults.resolve_thinking_params("openai", "gpt-5", False, "high")
    assert body_top == {"reasoning_effort": "none"}
    assert extra == {}


def test_thinking_kimi_has_no_effort_parameter():
    """Scenario Kimi 无强度参数：只含思考开关参数，不注入任何强度参数。"""
    body_top, extra = defaults.resolve_thinking_params("openai", "kimi-k2", True, "high")
    assert body_top == {}
    assert extra == {"thinking": {"type": "enabled"}}


def test_thinking_claude_adaptive_and_effort():
    """Claude：请求体 thinking={"type": "adaptive"}、强度经 effort。"""
    body_top, extra = defaults.resolve_thinking_params("anthropic", "claude-sonnet-4", True, "max")
    assert body_top == {"effort": "max"}
    assert extra == {"thinking": {"type": "adaptive"}}


def test_thinking_glm_clear_thinking_false_and_disable():
    """GLM：启用时 clear_thinking=false；关闭时 thinking={"type": "disabled"}。"""
    _, extra_on = defaults.resolve_thinking_params("openai", "glm-4.6", True, "high")
    assert extra_on == {"thinking": {"type": "enabled", "clear_thinking": False}}
    _, extra_off = defaults.resolve_thinking_params("openai", "glm-4.6", False, "high")
    assert extra_off == {"thinking": {"type": "disabled"}}


def test_thinking_qwen_disable_branch():
    """Qwen 关闭思考：enable_thinking=false。"""
    _, extra = defaults.resolve_thinking_params("openai", "qwen3-max", False, "high")
    assert extra == {"enable_thinking": False}


def test_thinking_mimo_both_protocols():
    """MiMo：OpenAI 协议走扩展体、Anthropic 协议合入请求体，均无强度参数。"""
    body_top, extra = defaults.resolve_thinking_params("openai", "mimo-v2.5", True, "high")
    assert body_top == {} and extra == {"thinking": {"type": "enabled"}}
    body_top, extra = defaults.resolve_thinking_params("anthropic", "mimo-v2.5", True, "high")
    assert body_top == {} and extra == {"thinking": {"type": "enabled"}}


def test_thinking_model_prefix_case_insensitive():
    """模型名前缀匹配不区分大小写。"""
    assert defaults.resolve_thinking_passback("openai", "QWEN3-MAX") == "none"
    body_top, _ = defaults.resolve_thinking_params("anthropic", "DeepSeek-V4-Pro", True, "high")
    assert body_top == {"output_config": {"effort": "high"}}


def test_thinking_deepseek_dual_protocol_request_shapes():
    """Scenario DeepSeek 双协议差异：Anthropic 合入请求体顶层、OpenAI 放扩展体。"""
    runtime = make_runtime()
    fake = FakeOpenAI([[]], runtime)
    openai_backend = OpenAIBackend(FakeConfig(model="deepseek-v4-pro"), runtime, None,
                                   client=fake)
    list(openai_backend.chat_stream([]))
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert fake.calls[0]["reasoning_effort"] == "high"
    assert "thinking" not in fake.calls[0]

    http = FakeHTTP([sse_response([a_message_delta("end_turn")])], runtime)
    anthropic_backend = AnthropicBackend(FakeConfig(protocol="anthropic", model="deepseek-v4-pro"),
                                         runtime, None, client_factory=http.factory)
    list(anthropic_backend.chat_stream([]))
    body = json.loads(http.requests[0].read())
    assert body["thinking"] == {"type": "enabled"}
    assert body["output_config"] == {"effort": "high"}
    assert "extra_body" not in body
    assert "reasoning_effort" not in body


def test_thinking_table_key_order_preserved():
    """映射表条目顺序保持（首个命中者生效的前提）。"""
    assert list(defaults.THINKING_PARAM_MAP)[:4] == [
        ("anthropic", "deepseek"), ("openai", "deepseek"),
        ("openai", "glm"), ("openai", "kimi"),
    ]
    assert len(defaults.THINKING_PARAM_MAP) == 9


def test_baseline_thinking_params_matrix():
    """基线对照：config.defaults.resolve_thinking_params 全部 82 例逐项一致。"""
    data = json.loads((BASELINE_DIR / "defaults.json").read_text(encoding="utf-8"))
    cases = data["groups"]["defaults.resolve_thinking_params"]
    assert len(cases) == 82
    for case in cases:
        inp = case["input"]
        body_top, extra = defaults.resolve_thinking_params(
            inp["protocol"], inp["model"], inp["thinking_enabled"], inp["effort"])
        assert {"body_top": body_top, "extra_body": extra} == case["result"], case["id"]


def test_baseline_thinking_passback_matrix():
    """基线对照：config.defaults.resolve_thinking_passback 全部 17 例逐项一致。"""
    data = json.loads((BASELINE_DIR / "defaults.json").read_text(encoding="utf-8"))
    cases = data["groups"]["defaults.resolve_thinking_passback"]
    assert len(cases) == 17
    for case in cases:
        inp = case["input"]
        assert defaults.resolve_thinking_passback(
            inp["protocol"], inp["model"]) == case["result"], case["id"]


class RecordingRules:
    """thinking 映射表提供方替身：记录查询并返回可控结果（模拟 config.defaults）。"""

    def __init__(self):
        self.params_calls: list = []
        self.passback_calls: list = []

    def resolve_thinking_params(self, protocol, model, thinking_enabled, effort):
        self.params_calls.append((protocol, model, thinking_enabled, effort))
        return {"marker_param": True}, {"marker_extra": True}

    def resolve_thinking_passback(self, protocol, model):
        self.passback_calls.append((protocol, model))
        return "reasoning_content"


def test_injected_thinking_rules_consumed_by_openai_backend():
    """注入的映射表提供方被 OpenAI 后端消费（body_top → 请求参数、extra_body → 扩展体）。"""
    rules = RecordingRules()
    runtime = make_runtime(thinking_rules=rules)
    fake = FakeOpenAI([[]], runtime)
    backend = OpenAIBackend(FakeConfig(), runtime, None, client=fake)
    list(backend.chat_stream([]))
    assert fake.calls[0]["marker_param"] is True
    assert fake.calls[0]["extra_body"] == {"marker_extra": True}
    assert rules.params_calls == [("openai", "deepseek-v4-flash", True, "high")]


def test_injected_thinking_rules_consumed_by_anthropic_backend():
    """注入的映射表提供方被 Anthropic 后端消费（两类位置均合入请求体顶层）。"""
    rules = RecordingRules()
    runtime = make_runtime(thinking_rules=rules)
    http = FakeHTTP([sse_response([a_message_delta("end_turn")])], runtime)
    backend = AnthropicBackend(FakeConfig(protocol="anthropic"), runtime, None,
                               client_factory=http.factory)
    messages = [{"role": "assistant", "content": "答", "thinking": "想",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Shell", "arguments": "{}"}}]}]
    list(backend.chat_stream(messages))
    body = json.loads(http.requests[0].read())
    assert body["marker_param"] is True
    assert body["marker_extra"] is True
    assert rules.passback_calls == [("anthropic", "deepseek-v4-flash")]


def test_client_passes_thinking_rules_to_runtime():
    """`LLMClient(thinking_rules=…)` 把注入的提供方原样接到运行时。"""
    rules = RecordingRules()
    client = LLMClient(FakeConfig(), tool_definitions=[], thinking_rules=rules)
    assert client._runtime.thinking_rules is rules


def test_thinking_rules_injection_is_required():
    """thinking 提供方为必需注入项：未传即 TypeError（不再静默回退到副本）。"""
    with pytest.raises(TypeError):
        LLMClient(FakeConfig(), tool_definitions=[])
    with pytest.raises(TypeError):
        LLMRuntime()


# ═══════════════════════════════════════════════════════════════
# 5. 思考回传格式与开关
# ═══════════════════════════════════════════════════════════════


def test_openai_passback_in_tool_round():
    """Scenario 工具轮回传（OpenAI 协议）：assistant 以顶层 reasoning_content 回传思考。"""
    backend, _, _ = make_openai()
    messages = [{"role": "assistant", "content": "t", "thinking": "思路",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Shell", "arguments": "{}"}}]}]
    out = backend.prepare_messages(messages)
    assert out[0]["reasoning_content"] == "思路"
    assert KEY_THINKING not in out[0]
    assert messages[0]["thinking"] == "思路"


def test_openai_text_round_strips_thinking():
    """Scenario 纯文本轮不回传：思考被剥离，请求中不含 reasoning_content。"""
    backend, _, _ = make_openai()
    out = backend.prepare_messages([{"role": "assistant", "content": "t",
                                     "thinking": "思路", "thinking_signature": "s"}])
    assert "reasoning_content" not in out[0]
    assert KEY_THINKING not in out[0]
    assert KEY_THINKING_SIGNATURE not in out[0]


@pytest.mark.parametrize("overrides", [
    {"thinking_passback": False},
    {"thinking_enabled": False},
])
def test_openai_passback_switch_disables(overrides):
    """Scenario 回传开关关闭：不回传思考。"""
    config = FakeConfig(**overrides)
    backend, _, _ = make_openai(config=config)
    messages = [{"role": "assistant", "content": "t", "thinking": "思路",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Shell", "arguments": "{}"}}]}]
    assert "reasoning_content" not in backend.prepare_messages(messages)[0]


def test_openai_passback_none_format_strips():
    """格式为 none（Qwen/GPT）时即使工具轮也不回传思考。"""
    config = FakeConfig(model="qwen3-max")
    backend, _, _ = make_openai(config=config)
    messages = [{"role": "assistant", "content": "t", "thinking": "思路",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Shell", "arguments": "{}"}}]}]
    assert "reasoning_content" not in backend.prepare_messages(messages)[0]


def test_anthropic_thinking_block_passback():
    """DeepSeek（Anthropic 协议）工具轮回传思考块（无需签名）。"""
    config = FakeConfig(protocol="anthropic", model="deepseek-v4-pro")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "答", "thinking": "思路",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "Shell", "arguments": "{}"}}]}])
    assert msgs[0]["content"][0] == {"type": "thinking", "thinking": "思路"}
    assert msgs[0]["content"][1] == {"type": "text", "text": "答"}
    assert msgs[0]["content"][2]["type"] == "tool_use"


def test_anthropic_signed_missing_signature_omits_block():
    """Scenario 签名缺失省略：Claude 的思考块没有签名 → 该思考块整体省略。"""
    config = FakeConfig(protocol="anthropic", model="claude-sonnet-4")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "答", "thinking": "思路",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "Shell", "arguments": "{}"}}]}])
    types = [b["type"] for b in msgs[0]["content"]]
    assert types == ["text", "tool_use"]


def test_anthropic_signed_with_signature_keeps_block():
    """Claude 有签名时回传思考块并携带签名。"""
    config = FakeConfig(protocol="anthropic", model="claude-sonnet-4")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "答", "thinking": "思路",
        "thinking_signature": "sig==",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "Shell", "arguments": "{}"}}]}])
    assert msgs[0]["content"][0] == {"type": "thinking", "thinking": "思路",
                                     "signature": "sig=="}


def test_anthropic_synthetic_placeholder_passes_back_without_tool_calls():
    """repair 合成占位（无工具调用）仍回传思考块，且占位文本与基线一致。"""
    data = json.loads((BASELINE_DIR / "message_list.json").read_text(encoding="utf-8"))
    baseline_value = data["groups"]["message_list.SYNTHETIC_THINKING"][0]["result"]
    assert SYNTHETIC_THINKING == baseline_value == "（用户中断了工具执行）"

    config = FakeConfig(protocol="anthropic", model="deepseek-v4-pro")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": SYNTHETIC_THINKING, "thinking": SYNTHETIC_THINKING}])
    assert msgs[0]["content"][0] == {"type": "thinking", "thinking": SYNTHETIC_THINKING}


def test_anthropic_text_round_without_tool_calls_no_passback():
    """纯文本轮的真实思考不回传（仅合成占位例外）。"""
    config = FakeConfig(protocol="anthropic", model="deepseek-v4-pro")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{"role": "assistant", "content": "答", "thinking": "思路"}])
    assert msgs[0] == {"role": "assistant", "content": "答"}


def test_anthropic_passback_switch_off():
    """Scenario 回传开关关闭：所有协议均不回传思考。"""
    config = FakeConfig(protocol="anthropic", model="deepseek-v4-pro", thinking_passback=False)
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "", "thinking": "思路",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "Shell", "arguments": "{}"}}]}])
    assert [b["type"] for b in msgs[0]["content"]] == ["tool_use"]


def test_completion_thinking_output_rules_openai():
    """OpenAI 完成事件 thinking：仅"启用+回传开启+reasoning_content 格式"时给出拼接结果。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(reasoning="想"), openai_chunk(reasoning="法", finish_reason="stop")]])
    events = list(backend.chat_stream([{"role": "user", "content": "q"}]))
    assert events[-1][KEY_THINKING] == "想法"
    assert KEY_CONTENT not in events[-1]

    backend, _, _ = make_openai(config=FakeConfig(thinking_passback=False), script=[[
        openai_chunk(reasoning="想", finish_reason="stop")]])
    assert list(backend.chat_stream([]))[-1][KEY_THINKING] is None

    backend, _, _ = make_openai(config=FakeConfig(model="qwen3-max"), script=[[
        openai_chunk(reasoning="想", finish_reason="stop")]])
    assert list(backend.chat_stream([]))[-1][KEY_THINKING] is None


def test_completion_thinking_output_rules_anthropic():
    """Anthropic 完成事件 thinking：仅由回传开关决定；关闭时为空值。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_thinking_block(), a_thinking("想"), a_text("答"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert events[-1][KEY_THINKING] == "想"

    config = FakeConfig(protocol="anthropic", thinking_passback=False)
    backend, _, _ = make_anthropic(config=config, script=[sse_response([
        a_thinking_block(), a_thinking("想"), a_text("答"), a_message_delta("end_turn")])])
    assert list(backend.chat_stream([]))[-1][KEY_THINKING] is None


# ═══════════════════════════════════════════════════════════════
# 6. 消息格式转换（OpenAI → Anthropic）
# ═══════════════════════════════════════════════════════════════


def test_convert_system_messages_promoted_and_joined():
    """Scenario 系统消息提升：两条 system 以空行连接为顶层文本，消息数组不含 system。"""
    backend, _, _ = make_anthropic()
    system, msgs = backend.convert_messages([
        {"role": "system", "content": "A"},
        {"role": "system", "content": ""},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "问"},
    ])
    assert system == "A\n\nB"
    assert [m["role"] for m in msgs] == ["user"]


def test_convert_consecutive_user_messages_merged():
    """Scenario 连续用户消息合并：合并为一条、两个文本块。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([
        {"role": "user", "content": "一"},
        {"role": "user", "content": "二"},
    ])
    assert len(msgs) == 1
    assert msgs[0]["content"] == [{"type": "text", "text": "一"}, {"type": "text", "text": "二"}]


def test_convert_user_after_tool_result_not_merged():
    """Scenario 工具结果不合并：前一条 user 含工具结果块时，新 user 文本保持独立。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "user", "content": "新问题"},
    ])
    assert [m["role"] for m in msgs] == ["assistant", "user", "user"]
    assert msgs[1]["content"] == [{"type": "tool_result", "tool_use_id": "c1", "content": "结果"}]
    assert msgs[2]["content"] == "新问题"


def test_convert_tool_message_appends_to_previous_user():
    """Scenario 工具结果映射：tool 消息转为工具结果块并追加进其后的 user 消息内容列表。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "R1"},
        {"role": "tool", "tool_call_id": "c2", "content": "R2"},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["c1", "c2"]


def test_convert_tool_message_without_user_creates_one():
    """前一条不是 user（或 content 非列表）时新建 user 消息承载工具结果。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([
        {"role": "assistant", "content": "兜底"},
        {"role": "tool", "tool_call_id": "c9", "content": "R"},
    ])
    assert msgs[1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c9", "content": "R"}]}


def test_convert_assistant_blocks_order_and_json_parse():
    """assistant 块顺序 [思考块, 文本块, 工具调用块…]，arguments 解析为对象。"""
    config = FakeConfig(protocol="anthropic", model="deepseek-v4-pro")
    backend, _, _ = make_anthropic(config=config)
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "说明", "thinking": "思路",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "Shell", "arguments": '{"command": "dir"}'}}]}])
    content = msgs[0]["content"]
    assert [b["type"] for b in content] == ["thinking", "text", "tool_use"]
    assert content[2] == {"type": "tool_use", "id": "c1", "name": "Shell",
                          "input": {"command": "dir"}}


def test_convert_assistant_invalid_arguments_empty_object():
    """arguments 非法 JSON → input 取空对象。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([{
        "role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "Shell", "arguments": "{坏 JSON"}}]}])
    assert msgs[0]["content"][0]["input"] == {}


def test_convert_assistant_plain_text():
    """无工具调用且无思考的 assistant 以纯文本承载。"""
    backend, _, _ = make_anthropic()
    _, msgs = backend.convert_messages([{"role": "assistant", "content": "答"}])
    assert msgs[0] == {"role": "assistant", "content": "答"}


def test_convert_failure_yields_error_event():
    """Scenario 转换失败：下发 [错误: 消息格式转换失败: <详情>] 并结束本轮。"""
    backend, _, _ = make_anthropic()
    events = list(backend.chat_stream([{"role": "user", "content": "问"}, "非法消息"]))
    assert len(events) == 1
    assert events[0][KEY_FINISH_REASON] == "error"
    assert events[0][KEY_CONTENT].startswith("[错误: 消息格式转换失败: ")


# ═══════════════════════════════════════════════════════════════
# 7. 工具定义传递与转换
# ═══════════════════════════════════════════════════════════════


def test_convert_tools_default_input_schema():
    """Scenario 参数模式缺省：无 parameters → 空对象模式。"""
    backend, _, _ = make_anthropic()
    tools = backend.convert_tools([{"type": "function", "function": {"name": "Shell"}}])
    assert tools == [{"name": "Shell", "description": "",
                      "input_schema": {"type": "object", "properties": {}}}]


def test_convert_tools_bare_definition():
    """Scenario 裸定义：未包 function 层时按定义自身字段取值。"""
    backend, _, _ = make_anthropic()
    tools = backend.convert_tools([{"name": "Read", "description": "读文件",
                                    "parameters": {"type": "object", "properties": {"path": {}}}}])
    assert tools == [{"name": "Read", "description": "读文件",
                      "input_schema": {"type": "object", "properties": {"path": {}}}}]


def test_convert_tools_empty_list():
    """工具定义为空 → 转换结果为空列表。"""
    backend, _, _ = make_anthropic()
    assert backend.convert_tools([]) == []
    assert backend.convert_tools(None) == []


def test_openai_tools_passed_verbatim():
    """OpenAI 兼容协议原样传递工具定义列表。"""
    defs = [{"type": "function", "function": {"name": "Shell", "parameters": {"x": 1}}}]
    backend, fake, _ = make_openai(runtime=make_runtime(tool_defs=defs), script=[[]])
    list(backend.chat_stream([]))
    assert fake.calls[0]["tools"] == defs


# ═══════════════════════════════════════════════════════════════
# 8. 文本与思考事件流
# ═══════════════════════════════════════════════════════════════


def test_text_delta_emitted_immediately():
    """Scenario 文本增量即时下发：每个增量即时以 {"content": …} 下发。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_text("你"), a_text("好"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert events[:2] == [{KEY_CONTENT: "你"}, {KEY_CONTENT: "好"}]
    assert events[2][KEY_FINISH_REASON] == "stop"


def test_reasoning_deltas_not_emitted_openai():
    """Scenario 思考不回显（OpenAI）：思考增量只累积，不单独下发。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(reasoning="思考一", reasoning_in_extra=True),
        openai_chunk(content="答", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    assert [e for e in events if KEY_CONTENT in e] == [{KEY_CONTENT: "答"}]
    assert events[-1][KEY_THINKING] is None or isinstance(events[-1][KEY_THINKING], str)


def test_anthropic_reasoning_not_emitted():
    """Scenario 思考不回显（Anthropic）：thinking 增量不单独下发。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_thinking_block(), a_thinking("思考"), a_text("答"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert [e[KEY_CONTENT] for e in events if KEY_CONTENT in e] == ["答"]


def test_thinking_only_fallback_as_text():
    """Scenario 仅思考回复兜底：思考以 {"content": …} 作为正式文本下发，thinking 为空串。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_thinking_block(), a_thinking("只有思考"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert {KEY_CONTENT: "只有思考"} in events
    completion = events[-1]
    assert completion[KEY_FINISH_REASON] == "stop"
    assert completion[KEY_THINKING] == ""
    assert completion[KEY_THINKING_SIGNATURE] is None


def test_multiple_content_events_in_one_stream():
    """同一轮流中 content 事件可出现多次。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(content="A"), openai_chunk(content="B"),
        openai_chunk(content="C", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    assert [e[KEY_CONTENT] for e in events if KEY_CONTENT in e] == ["A", "B", "C"]


# ═══════════════════════════════════════════════════════════════
# 9. 工具调用流式聚合
# ═══════════════════════════════════════════════════════════════


def test_tool_call_aggregation_split_deltas():
    """Scenario 增量聚合：name 为完整拼接结果、arguments 为原始拼接字符串。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(tool_calls=[tool_delta(0, "call-1", "Sh", '{"comm')]),
        openai_chunk(tool_calls=[tool_delta(0, None, "ell", 'and": "dir"}')]),
        openai_chunk(finish_reason="tool_calls")]])
    events = list(backend.chat_stream([]))
    assert events[-1][KEY_TOOL_CALLS] == [{
        "id": "call-1", "type": "function",
        "function": {"name": "Shell", "arguments": '{"command": "dir"}'}}]
    assert events[-1][KEY_FINISH_REASON] == "tool_calls"


def test_tool_call_index_mapping_fills_id():
    """Scenario 索引补 id：只有位置索引且此前同索引出现过 id → 归属到已记录的 id。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(tool_calls=[tool_delta(0, "call-1", "Shell", "")]),
        openai_chunk(tool_calls=[tool_delta(0, None, "", "{}")]),
        openai_chunk(finish_reason="tool_calls")]])
    events = list(backend.chat_stream([]))
    assert len(events[-1][KEY_TOOL_CALLS]) == 1
    assert events[-1][KEY_TOOL_CALLS][0]["id"] == "call-1"


def test_tool_call_placeholder_id_increments():
    """Scenario 占位 id：无 id 也无可用索引映射 → 生成 _tc_<序号> 并自增。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(tool_calls=[tool_delta(None, None, "A", "1")]),
        openai_chunk(tool_calls=[tool_delta(None, None, "B", "2")]),
        openai_chunk(finish_reason="tool_calls")]])
    ids = [tc["id"] for tc in list(backend.chat_stream([]))[-1][KEY_TOOL_CALLS]]
    assert ids == ["_tc_0", "_tc_1"]


def test_completion_without_tool_calls_has_no_key():
    """Scenario 无工具轮完成事件：{"finish_reason": …, "thinking": …}（不带 tool_calls 键）。"""
    backend, _, _ = make_openai(script=[[openai_chunk(content="答", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    assert KEY_TOOL_CALLS not in events[-1]
    assert set(events[-1]) == {KEY_FINISH_REASON, KEY_THINKING}


def test_anthropic_tool_calls_sorted_by_index_and_signature():
    """Anthropic 工具调用按内容块索引升序输出，完成事件含 thinking_signature。"""
    config = FakeConfig(protocol="anthropic", model="claude-sonnet-4")
    backend, _, _ = make_anthropic(config=config, script=[sse_response([
        a_tool_use("c2", "Read", index=2), a_json('{"p": "b"}', index=2),
        a_tool_use("c1", "Shell", index=1), a_json('{"command": "dir"}', index=1),
        a_thinking_block(index=0), a_thinking("思路", index=0), a_signature("sig==", index=0),
        a_message_delta("tool_use", usage={"input_tokens": 3, "output_tokens": 4})])])
    events = list(backend.chat_stream([]))
    completion = [e for e in events if KEY_TOOL_CALLS in e][0]
    assert [tc["id"] for tc in completion[KEY_TOOL_CALLS]] == ["c1", "c2"]
    assert completion[KEY_TOOL_CALLS][0]["function"]["arguments"] == '{"command": "dir"}'
    assert completion[KEY_THINKING_SIGNATURE] == "sig=="
    assert completion[KEY_FINISH_REASON] == "tool_calls"


def test_anthropic_arguments_not_parsed():
    """Anthropic 工具参数按原始拼接字符串输出（不做解析）。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_tool_use("c1", "Shell"), a_json('{"command": '), a_json('"dir"}'),
        a_message_delta("tool_use")])])
    tool_call = list(backend.chat_stream([]))[-1][KEY_TOOL_CALLS][0]
    assert tool_call["function"]["arguments"] == '{"command": "dir"}'


# ═══════════════════════════════════════════════════════════════
# 10. 用量事件归一
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("usage,cached", [
    (openai_usage(cached=7), 7),
    (openai_usage(extra_cached=9), 9),
    (openai_usage(), 0),
])
def test_openai_usage_cached_fallback(usage, cached):
    """Scenario 缓存字段回退：details → 顶层 prompt_cache_hit_tokens → 缺失取 0。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(usage=usage, with_choices=False),
        openai_chunk(content="x", finish_reason="stop")]])
    usage_event = [e for e in backend.chat_stream([]) if KEY_USAGE in e][0]
    assert usage_event[KEY_USAGE] == {"prompt_tokens": 10, "completion_tokens": 5,
                                      "cached_tokens": cached}


def test_openai_usage_only_chunk_processed():
    """Scenario 仅用量块：不含选项的仅用量块正常下发（不被跳过）。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(usage=openai_usage(prompt=100, completion=0), with_choices=False)]])
    events = list(backend.chat_stream([]))
    assert events == [{KEY_USAGE: {"prompt_tokens": 100, "completion_tokens": 0,
                                   "cached_tokens": 0}}]


def test_anthropic_usage_normalized_with_cache():
    """Anthropic 输入用量 = input_tokens + 缓存命中数；输出取 output_tokens。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_message_delta("end_turn", usage={"input_tokens": 10, "output_tokens": 3,
                                           "cache_read_input_tokens": 4})])])
    usage_event = [e for e in backend.chat_stream([]) if KEY_USAGE in e][0]
    assert usage_event[KEY_USAGE] == {"prompt_tokens": 14, "completion_tokens": 3,
                                      "cached_tokens": 4}


def test_anthropic_usage_prompt_cache_hit_tokens_alias():
    """缓存命中数兼容 prompt_cache_hit_tokens 字段名。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_message_delta("end_turn", usage={"input_tokens": 5, "output_tokens": 1,
                                           "prompt_cache_hit_tokens": 2})])])
    usage_event = [e for e in backend.chat_stream([]) if KEY_USAGE in e][0]
    assert usage_event[KEY_USAGE]["cached_tokens"] == 2
    assert usage_event[KEY_USAGE]["prompt_tokens"] == 7


def test_anthropic_usage_fallback_from_message_start():
    """Scenario 结束事件缺失补用量：以流首事件保存的初始用量补发一次（输出为 0）。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_message_start(usage={"input_tokens": 8, "cache_read_input_tokens": 2}),
        a_text("答")])])
    events = list(backend.chat_stream([]))
    usage_event = [e for e in events if KEY_USAGE in e][0]
    assert usage_event == {KEY_USAGE: {"prompt_tokens": 10, "completion_tokens": 0,
                                       "cached_tokens": 2}}
    assert events[-1][KEY_FINISH_REASON] == "stop"


# ═══════════════════════════════════════════════════════════════
# 11. 完成原因取值与协议映射
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stop_reason,expected", [
    ("end_turn", "stop"),
    ("tool_use", "tool_calls"),
    ("max_tokens", "max_tokens"),
    ("length", "max_tokens"),
    ("content_filter", "content_filter"),
    ("insufficient_system_resource", "server_busy"),
])
def test_anthropic_stop_reason_mapping(stop_reason, expected):
    """Anthropic 结束原因映射表逐项。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_text("答"), a_message_delta(stop_reason)])])
    assert list(backend.chat_stream([]))[-1][KEY_FINISH_REASON] == expected


def test_anthropic_unknown_stop_reason_passthrough():
    """Scenario 未知结束原因透传：映射表外的值原样出现在 finish_reason。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_text("答"), a_message_delta("some_new_reason")])])
    assert list(backend.chat_stream([]))[-1][KEY_FINISH_REASON] == "some_new_reason"


def test_anthropic_missing_stop_reason_defaults_end_turn():
    """Scenario 结束原因缺失：按 end_turn 处理 → stop。"""
    backend, _, _ = make_anthropic(script=[sse_response([a_text("答"), a_message_delta()])])
    assert list(backend.chat_stream([]))[-1][KEY_FINISH_REASON] == "stop"


@pytest.mark.parametrize("reason", ["stop", "tool_calls", "max_tokens", "content_filter",
                                    "server_busy"])
def test_openai_finish_reason_passthrough(reason):
    """OpenAI 兼容协议原样透传服务端给出的完成原因。"""
    backend, _, _ = make_openai(script=[[openai_chunk(content="答", finish_reason=reason)]])
    assert list(backend.chat_stream([]))[-1][KEY_FINISH_REASON] == reason


# ═══════════════════════════════════════════════════════════════
# 12. 重试、退避与重试通知
# ═══════════════════════════════════════════════════════════════


class BlockingChunks:
    """产出给定 chunk 后阻塞直到被关闭（模拟 OpenAI 侧流挂死）。"""

    def __init__(self, chunks, wait_seconds: float = 3.0):
        self._chunks = list(chunks)
        self._closed = threading.Event()
        self._wait_seconds = wait_seconds

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        self._closed.wait(self._wait_seconds)

    def close(self):
        self._closed.set()


def failing_chunks(chunks, exc):
    """产出给定 chunk 后抛出异常（模拟 OpenAI 侧流中断）。"""
    def _gen():
        for chunk in chunks:
            yield chunk
        raise exc

    return _gen()


@pytest.mark.parametrize("attempt,base", [(1, 1), (2, 2), (3, 4), (4, 8), (5, 8), (9, 8)])
def test_retry_notice_text_and_seconds(attempt, base):
    """重试通知文案固定，秒数与该次真实退避基数一致。"""
    event = retry_notice(attempt, 3, retry_module.RETRY_REASON_RATE)
    assert event == {KEY_RETRY_NOTICE: (
        f"\n⚠ 请求被限流(429)，约{base}s后自动重试（第{attempt}/3次）…\n")}


def test_retry_notice_reasons():
    """三类原因文案：网络连接失败 / 请求被限流(429) / 服务端错误(<状态码>)。"""
    assert "网络连接失败" in retry_notice(1, 3)[KEY_RETRY_NOTICE]
    assert "请求被限流(429)" in retry_notice(1, 3, retry_module.RETRY_REASON_RATE)[KEY_RETRY_NOTICE]
    assert "服务端错误(503)" in retry_notice(1, 3, retry_module.server_error_reason(503))[KEY_RETRY_NOTICE]


def test_retry_notice_uses_contract_key():
    """重试通知事件的键名取自 contracts.llm_events 常量。"""
    assert set(retry_notice(1, 3)) == {KEY_RETRY_NOTICE}


@pytest.mark.parametrize("attempt,base", [(0, 1), (1, 2), (2, 4), (3, 8), (4, 8), (7, 8)])
def test_backoff_base_sequence(attempt, base):
    """退避序列 [1,2,4,8,8]，超出末项后固定 8 秒。"""
    assert RETRY_BACKOFF_BASE == (1, 2, 4, 8, 8)
    assert backoff_base(attempt) == base


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 4, 5])
def test_retry_sleep_waits_backoff_with_jitter(monkeypatch, attempt):
    """退避等待为基数 ±25% 抖动。"""
    fake = fast_time(monkeypatch)
    assert retry_sleep(attempt) is True
    base = backoff_base(attempt)
    assert base * 0.75 <= fake.now - 1000.0 <= base * 1.25


def test_retry_sleep_polls_in_small_slices(monkeypatch):
    """等待期间以不大于 0.2 秒的分片轮询（分片内检查取消标记）。"""
    fake = fast_time(monkeypatch)
    retry_sleep(3)
    assert fake.slept and max(fake.slept) <= 0.2


def test_retry_sleep_cancel_returns_false(monkeypatch):
    """Scenario 退避期间取消：取消标记被置位 → 返回 False（不再重发请求）。"""
    fast_time(monkeypatch)
    assert retry_sleep(2, cancel_check=lambda: True) is False


@pytest.mark.parametrize("configured,effective", [(0, 1), (-3, 1), (1, 1), (3, 3), (10, 10),
                                                  (11, 10), (99, 10)])
def test_clamp_retry_count(configured, effective):
    """Scenario 重试上限钳制：配置值被钳制在 1–10 区间内。"""
    assert clamp_retry_count(configured) == effective


@pytest.mark.parametrize("configured,effective", [(0, 1), (20, 10), (3, 3)])
def test_client_syncs_retry_limit_from_config(configured, effective):
    """构造时从配置同步重试上限（钳制 1–10）。"""
    client = LLMClient(FakeConfig(retry_count=configured), tool_definitions=[],
                       thinking_rules=defaults)
    assert client._runtime.max_retries == effective


def test_set_retry_count_updates_runtime():
    """`set_retry_count` 为实例方法，更新实例重试上限（每轮对话开始时可同步）。"""
    client = LLMClient(FakeConfig(), tool_definitions=[], thinking_rules=defaults)
    client.set_retry_count(7)
    assert client._runtime.max_retries == 7
    client.set_retry_count(0)
    assert client._runtime.max_retries == 1


def test_openai_429_retry_then_success(monkeypatch):
    """Scenario 429 重试：先下发重试通知（原因与秒数）再退避重发。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(script=[
        sdk_error(openai.RateLimitError, 429),
        [openai_chunk(content="ok", finish_reason="stop")]])
    events = list(backend.chat_stream([{"role": "user", "content": "q"}]))
    assert events[0] == retry_notice(1, 3, retry_module.RETRY_REASON_RATE)
    assert events[1] == {KEY_CONTENT: "ok"}
    assert len(fake.calls) == 2


def test_openai_server_error_retry(monkeypatch):
    """Scenario 服务端错误重试：503 下发通知（原因 服务端错误(503)）后退避重发。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(script=[
        sdk_error(openai.InternalServerError, 503),
        [openai_chunk(content="ok", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    assert events[0] == retry_notice(1, 3, retry_module.server_error_reason(503))
    assert len(fake.calls) == 2


def test_openai_network_error_retry(monkeypatch):
    """网络类异常按网络类重试（原因 网络连接失败）。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(script=[
        connection_error(),
        [openai_chunk(content="ok", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    assert events[0] == retry_notice(1, 3, retry_module.RETRY_REASON_NETWORK)
    assert len(fake.calls) == 2


def test_openai_network_error_schedule_sequence(monkeypatch):
    """连续网络错误 → 通知秒数按 [1,2,4] 递进（上限 3 次）。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(script=[
        connection_error(), connection_error(), connection_error(),
        [openai_chunk(content="ok", finish_reason="stop")]])
    events = list(backend.chat_stream([]))
    notices = [e[KEY_RETRY_NOTICE] for e in events if KEY_RETRY_NOTICE in e]
    assert [("约1s" in n, "第1/3次" in n) for n in notices] == [(True, True), (False, False), (False, False)]
    assert "约2s" in notices[1] and "第2/3次" in notices[1]
    assert "约4s" in notices[2] and "第3/3次" in notices[2]
    assert len(fake.calls) == 4


@pytest.mark.parametrize("cancel_at", [0, 1])
def test_retry_cancelled_during_backoff_ends_silently(monkeypatch, cancel_at):
    """Scenario 退避期间取消：不再重发请求、静默结束本轮（无完成/错误事件）。"""
    fast_time(monkeypatch)
    polls = {"n": 0}

    def cancel_check():
        polls["n"] += 1
        return polls["n"] > cancel_at + 1

    backend, fake, _ = make_openai(script=[
        sdk_error(openai.RateLimitError, 429),
        [openai_chunk(content="ok", finish_reason="stop")]])
    events = list(backend.chat_stream([], cancel_check=cancel_check))
    assert events == [retry_notice(1, 3, retry_module.RETRY_REASON_RATE)]
    assert len(fake.calls) == 1


def test_anthropic_429_retry_then_success(monkeypatch):
    """Anthropic 429：通知文案（原因与秒数）后重发，成功则继续解析。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(script=[
        text_response(429, "rate limited"),
        sse_response([a_text("ok"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert events[0] == retry_notice(1, 3, retry_module.RETRY_REASON_RATE)
    assert events[1] == {KEY_CONTENT: "ok"}
    assert len(http.requests) == 2
    assert len(http.clients) == 2


def test_anthropic_503_retry_notice(monkeypatch):
    """Anthropic 可重试服务端状态：503 通知 服务端错误(503)。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(script=[
        text_response(503, "unavailable"),
        sse_response([a_text("ok"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert events[0] == retry_notice(1, 3, retry_module.server_error_reason(503))
    assert len(http.requests) == 2


def test_anthropic_network_error_retry(monkeypatch):
    """Anthropic 连接类异常按网络类重试。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(script=[
        httpx.ConnectError("连接失败"),
        sse_response([a_text("ok"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([]))
    assert events[0] == retry_notice(1, 3, retry_module.RETRY_REASON_NETWORK)
    assert len(http.requests) == 2


def test_anthropic_retry_cancel_during_backoff(monkeypatch):
    """Anthropic 退避期间取消 → 静默结束（无错误事件）。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(script=[
        text_response(503, "unavailable"),
        sse_response([a_text("ok"), a_message_delta("end_turn")])])
    events = list(backend.chat_stream([], cancel_check=lambda: True))
    assert events == [retry_notice(1, 3, retry_module.server_error_reason(503))]
    assert len(http.requests) == 1


# ═══════════════════════════════════════════════════════════════
# 13. 错误事件与文案
# ═══════════════════════════════════════════════════════════════


def test_openai_401_not_retried():
    """Scenario 不可重试状态（OpenAI 协议）：401 → 错误事件 + finish_reason=error，不重试。"""
    backend, fake, _ = make_openai(script=[sdk_error(openai.AuthenticationError, 401, "bad key")])
    events = list(backend.chat_stream([]))
    assert events == [{KEY_CONTENT: "[错误: API调用失败(AuthenticationError): bad key]",
                       KEY_FINISH_REASON: "error"}]
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status,cls", [(400, openai.BadRequestError),
                                        (403, openai.PermissionDeniedError),
                                        (404, openai.NotFoundError),
                                        (422, openai.UnprocessableEntityError)])
def test_openai_unretryable_statuses(status, cls):
    """400/403/404/422 一律不重试、走不可重试错误文案。"""
    backend, fake, _ = make_openai(script=[sdk_error(cls, status, "nope")])
    events = list(backend.chat_stream([]))
    assert events[0][KEY_FINISH_REASON] == "error"
    assert events[0][KEY_CONTENT] == f"[错误: API调用失败({cls.__name__}): nope]"
    assert len(fake.calls) == 1


def test_openai_other_exception_not_retried():
    """其他异常不重试：直接走不可重试错误文案。"""
    backend, fake, _ = make_openai(script=[RuntimeError("内部错误")])
    events = list(backend.chat_stream([]))
    assert events == [{KEY_CONTENT: "[错误: API调用失败(RuntimeError): 内部错误]",
                       KEY_FINISH_REASON: "error"}]
    assert len(fake.calls) == 1


def test_openai_retry_exhausted_text(monkeypatch):
    """重试耗尽文案：`[错误: API调用失败(<异常类型>，重试<次数>次): <详情>]`。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(config=FakeConfig(retry_count=1),
                                   script=[sdk_error(openai.RateLimitError, 429, "slow"),
                                           sdk_error(openai.RateLimitError, 429, "slow")])
    events = list(backend.chat_stream([]))
    assert events[-1] == {KEY_CONTENT: "[错误: API调用失败(RateLimitError，重试0次): slow]",
                          KEY_FINISH_REASON: "error"}
    assert len(fake.calls) == 2


def test_openai_network_error_exhausted_text(monkeypatch):
    """网络类重试耗尽文案（重试次数=实际上限）。"""
    fast_time(monkeypatch)
    backend, fake, _ = make_openai(config=FakeConfig(retry_count=1),
                                   script=[connection_error(), connection_error()])
    events = list(backend.chat_stream([]))
    assert events[-1][KEY_CONTENT].startswith("[错误: API调用失败(APIConnectionError，重试1次):")
    assert events[-1][KEY_FINISH_REASON] == "error"


def test_anthropic_429_exhausted_text(monkeypatch):
    """Scenario 429 重试耗尽（Anthropic 协议）：[错误: API返回429速率限制(已重试N次)]。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(config=FakeConfig(protocol="anthropic", retry_count=1),
                                      script=[text_response(429, "slow"), text_response(429, "slow")])
    events = list(backend.chat_stream([]))
    assert events[-1] == {KEY_CONTENT: "[错误: API返回429速率限制(已重试1次)]",
                          KEY_FINISH_REASON: "error"}
    assert len(http.requests) == 2


def test_anthropic_retryable_status_exhausted_text(monkeypatch):
    """可重试状态耗尽文案：[错误: API返回<状态码>错误(已重试<次数>次)]。"""
    fast_time(monkeypatch)
    backend, http, _ = make_anthropic(config=FakeConfig(protocol="anthropic", retry_count=1),
                                      script=[text_response(500), text_response(500)])
    events = list(backend.chat_stream([]))
    assert events[-1] == {KEY_CONTENT: "[错误: API返回500错误(已重试1次)]",
                          KEY_FINISH_REASON: "error"}


def test_anthropic_stream_error_event():
    """Scenario 流内错误事件：立即下发 [错误: <服务端错误消息>] 与 error 并结束。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        {"type": "error", "error": {"message": "服务端炸了"}}])])
    assert list(backend.chat_stream([])) == [
        {KEY_CONTENT: "[错误: 服务端炸了]", KEY_FINISH_REASON: "error"}]


def test_anthropic_stream_error_event_missing_message():
    """流内错误事件缺消息 → 文案取「未知错误」。"""
    backend, _, _ = make_anthropic(script=[sse_response([{"type": "error", "error": {}}])])
    assert list(backend.chat_stream([])) == [
        {KEY_CONTENT: "[错误: 未知错误]", KEY_FINISH_REASON: "error"}]


def test_user_cancel_produces_no_error_event(monkeypatch):
    """用户取消 SHALL NOT 产生任何错误事件。"""
    fast_time(monkeypatch)
    backend, _, _ = make_openai(script=[sdk_error(openai.RateLimitError, 429)])
    events = list(backend.chat_stream([], cancel_check=lambda: True))
    assert all(KEY_FINISH_REASON not in e for e in events)


# ═══════════════════════════════════════════════════════════════
# 14. 上下文超限识别
# ═══════════════════════════════════════════════════════════════


def test_context_overflow_hints_cover_spec_set():
    """特征集合与 spec 逐条一致（13 条中英措辞）。"""
    assert set(CONTEXT_OVERFLOW_HINTS) == {
        "context length", "context window", "context_length_exceeded",
        "context is too long", "prompt is too long", "too long",
        "exceeds the maximum", "maximum context", "input length",
        "超出上下文", "上下文长度", "超出限制", "超限"}


@pytest.mark.parametrize("text", ["Context Length exceeded", "PROMPT IS TOO LONG",
                                  "上下文长度超了", "请求超限", "this is too long for me"])
def test_is_context_overflow_matches(text):
    """命中特征（不区分大小写、宽松子串）。"""
    assert is_context_overflow(text) is True


@pytest.mark.parametrize("text", ["invalid api key", "bad request", ""])
def test_is_context_overflow_miss(text):
    """未命中任何特征。"""
    assert is_context_overflow(text) is False


def test_openai_400_overflow_only_finish_event():
    """Scenario 命中超限特征：只下发 {"finish_reason": "context_overflow"}，不重试。"""
    backend, fake, _ = make_openai(script=[
        sdk_error(openai.BadRequestError, 400, "prompt is too long")])
    events = list(backend.chat_stream([]))
    assert events == [{KEY_FINISH_REASON: "context_overflow"}]
    assert len(fake.calls) == 1


def test_anthropic_400_overflow_only_finish_event():
    """Anthropic 侧同样只下发超限事件且不重试。"""
    backend, http, _ = make_anthropic(script=[text_response(400, "prompt is too long: 200000")])
    events = list(backend.chat_stream([]))
    assert events == [{KEY_FINISH_REASON: "context_overflow"}]
    assert len(http.requests) == 1


def test_openai_400_without_hint_is_error():
    """Scenario 未命中特征：按不可重试错误下发（finish_reason 为 error）。"""
    backend, _, _ = make_openai(script=[sdk_error(openai.BadRequestError, 400, "invalid param")])
    events = list(backend.chat_stream([]))
    assert events[0][KEY_FINISH_REASON] == "error"
    assert KEY_CONTENT in events[0]


def test_anthropic_400_without_hint_is_error():
    """Anthropic 400 未命中特征 → 通用错误文案（含状态码与响应文本）。"""
    backend, _, _ = make_anthropic(script=[text_response(400, "invalid param")])
    assert list(backend.chat_stream([])) == [
        {KEY_CONTENT: "[错误: API调用失败(400): invalid param]", KEY_FINISH_REASON: "error"}]


def test_openai_400_overflow_checks_body():
    """超限判定把异常体（body）一并纳入匹配。"""
    error = sdk_error(openai.BadRequestError, 400, "bad request",
                      body='{"error": "maximum context length"}')
    backend, _, _ = make_openai(script=[error])
    assert list(backend.chat_stream([])) == [{KEY_FINISH_REASON: "context_overflow"}]


# ═══════════════════════════════════════════════════════════════
# 15. 流中断上报与静默挂死检测
# ═══════════════════════════════════════════════════════════════


def test_openai_stream_interrupted_network():
    """Scenario 中途断开：传输类异常且未收到完成标记 → 上报 network 中断，不产出完成事件。"""
    backend, _, _ = make_openai(script=[
        failing_chunks([openai_chunk(content="半")], httpx.ReadError("连接被重置"))])
    events = list(backend.chat_stream([]))
    assert events[0] == {KEY_CONTENT: "半"}
    interrupted = get_stream_interrupted(events[-1])
    assert interrupted == {"kind": "network", "detail": "连接被重置"}
    assert all(KEY_FINISH_REASON not in e for e in events)


def test_openai_stream_interrupted_timeout_kind():
    """超时类异常 → kind 为 timeout。"""
    backend, _, _ = make_openai(script=[
        failing_chunks([], httpx.ReadTimeout("读超时"))])
    assert get_stream_interrupted(list(backend.chat_stream([]))[-1])["kind"] == "timeout"


def test_openai_stream_interrupted_unknown_kind():
    """其他异常 → kind 为 unknown。"""
    backend, _, _ = make_openai(script=[failing_chunks([], ValueError("怪错误"))])
    assert get_stream_interrupted(list(backend.chat_stream([]))[-1])["kind"] == "unknown"


def test_stream_interrupted_detail_truncated_to_200():
    """detail 为异常文本前 200 字符。"""
    backend, _, _ = make_openai(script=[
        failing_chunks([], httpx.ReadError("x" * 500))])
    detail = get_stream_interrupted(list(backend.chat_stream([]))[-1])["detail"]
    assert len(detail) == 200


def test_anthropic_stream_interrupted_network():
    """Anthropic 侧中途断开同样上报中断（未收到结束事件）。"""
    response = httpx.Response(200, stream=FailingStream(
        [b"data: {\"type\": \"content_block_delta\", \"index\": 0, "
         b"\"delta\": {\"type\": \"text_delta\", \"text\": \"\xe5\x8d\x8a\"}}\n\n"],
        httpx.ReadError("连接被重置")))
    backend, _, _ = make_anthropic(script=[response])
    events = list(backend.chat_stream([]))
    assert get_stream_interrupted(events[-1]) == {"kind": "network", "detail": "连接被重置"}
    assert all(KEY_FINISH_REASON not in e for e in events)


def test_openai_watchdog_stall_detected():
    """Scenario 静默挂死：首个数据块后静默超过阈值 → 记录超时异常、主动断连、上报 timeout。"""
    stream = BlockingChunks([openai_chunk(content="半")])
    backend, _, _ = make_openai(script=[stream], stall_seconds=0.05)
    events = list(backend.chat_stream([]))
    interrupted = get_stream_interrupted(events[-1])
    assert interrupted["kind"] == "timeout"
    assert "流式输出静默超过" in interrupted["detail"]
    assert all(KEY_FINISH_REASON not in e for e in events)


def test_anthropic_watchdog_stall_detected():
    """Anthropic 侧静默挂死同样上报 timeout 中断。"""
    stream = BlockingStream([b"data: {\"type\": \"message_start\", \"message\": {}}\n\n"])
    backend, _, _ = make_anthropic(stall_seconds=0.05)
    backend._client_factory = FakeHTTP(
        [httpx.Response(200, stream=stream)], backend._runtime).factory
    events = list(backend.chat_stream([]))
    interrupted = get_stream_interrupted(events[-1])
    assert interrupted["kind"] == "timeout"
    assert "流式输出静默超过" in interrupted["detail"]


def test_anthropic_first_byte_silence_not_stalled():
    """Scenario 首字节前静默：不触发挂死检测（由读超时兜底）。"""
    response = httpx.Response(200, stream=SilentStream(0.15))
    backend, _, _ = make_anthropic(script=[response], stall_seconds=0.05)
    assert list(backend.chat_stream([])) == []


def test_openai_after_finish_exception_not_reported():
    """Scenario 完成后的异常：已收到完成标记 → 不上报流中断。"""
    backend, _, _ = make_openai(script=[
        failing_chunks([openai_chunk(content="答", finish_reason="stop")],
                       httpx.ReadError("尾部断开"))])
    events = list(backend.chat_stream([]))
    assert events[-1][KEY_FINISH_REASON] == "stop"
    assert all(KEY_STREAM_INTERRUPTED not in e for e in events)


def test_anthropic_after_message_delta_exception_not_reported():
    """Anthropic 已收到结束事件后流抛异常 → 不上报流中断。"""
    line = json.dumps(a_message_delta("end_turn"), ensure_ascii=False).encode("utf-8")
    response = httpx.Response(200, stream=FailingStream(
        [b"data: " + line + b"\n\n"], httpx.ReadError("尾部断开")))
    backend, _, _ = make_anthropic(script=[response])
    events = list(backend.chat_stream([]))
    assert all(KEY_STREAM_INTERRUPTED not in e for e in events)
    assert events[0][KEY_FINISH_REASON] == "stop"


def test_iter_to_queue_pumps_items_and_sentinel():
    """队列泵：元素逐个入队，最后放入哨兵。"""
    q: queue.Queue = queue.Queue()
    err: list = []
    iter_to_queue(iter(["a", "b"]), q, err, [None])
    assert q.get_nowait() == "a"
    assert q.get_nowait() == "b"
    assert q.get_nowait() is STREAM_END
    assert err == []


def test_iter_to_queue_records_first_error_only():
    """只保留第一个异常：看门狗已记录的错误不被迭代异常覆盖。"""
    def _boom():
        raise ValueError("读失败")
        yield  # pragma: no cover

    q: queue.Queue = queue.Queue()
    err: list = []
    iter_to_queue(_boom(), q, err, [None])
    assert isinstance(err[0], ValueError)
    assert q.get_nowait() is STREAM_END

    err2: list = ["看门狗已记录"]
    iter_to_queue(_boom(), queue.Queue(), err2, [None])
    assert err2 == ["看门狗已记录"]


@pytest.mark.parametrize("exc,kind", [
    (httpx.ReadTimeout("t"), "timeout"),
    (httpx.ConnectTimeout("t"), "timeout"),
    (httpx.ReadError("n"), "network"),
    (httpx.ConnectError("n"), "network"),
    (httpx.RemoteProtocolError("n"), "network"),
    (openai.APITimeoutError(request=httpx.Request("POST", "http://x")), "timeout"),
    (openai.APIConnectionError(request=httpx.Request("POST", "http://x")), "network"),
    (ValueError("other"), "unknown"),
])
def test_classify_stream_error(exc, kind):
    """流异常分类矩阵：超时 / 传输 / 其他（含 SDK 包装类型）。"""
    assert classify_stream_error(exc)["kind"] == kind


def test_classify_stream_error_detail_truncated():
    """分类结果的 detail 截断 200 字符。"""
    assert len(classify_stream_error(ValueError("y" * 400))["detail"]) == 200


@pytest.mark.parametrize("status,retryable", [(408, True), (409, True), (500, True),
                                              (503, True), (599, True), (429, False),
                                              (400, False), (404, False)])
def test_is_retryable_http_matrix(status, retryable):
    """服务端类重试判定：408、409 与全部 ≥500。"""
    assert is_retryable_http(status) is retryable


# ═══════════════════════════════════════════════════════════════
# 16. 取消与中断
# ═══════════════════════════════════════════════════════════════


def test_openai_stream_cancel_ends_silently():
    """Scenario 流内取消：静默结束（无完成事件、无错误事件、无中断事件）。"""
    polls = {"n": 0}

    def cancel_check():
        polls["n"] += 1
        return polls["n"] > 1

    backend, _, runtime = make_openai(
        script=[BlockingChunks([openai_chunk(content="半")])])
    events = list(backend.chat_stream([], cancel_check=cancel_check))
    assert events == [{KEY_CONTENT: "半"}]
    assert runtime._active_handle is None


def test_anthropic_stream_cancel_ends_silently():
    """Anthropic 流内取消同样静默结束并清理活跃句柄。"""
    polls = {"n": 0}

    def cancel_check():
        polls["n"] += 1
        return polls["n"] > 1

    response = httpx.Response(200, stream=BlockingStream(
        [b"data: {\"type\": \"content_block_delta\", \"index\": 0, "
         b"\"delta\": {\"type\": \"text_delta\", \"text\": \"\xe5\x8d\x8a\"}}\n\n"]))
    backend, _, runtime = make_anthropic(script=[response])
    events = list(backend.chat_stream([], cancel_check=cancel_check))
    assert events == [{KEY_CONTENT: "半"}]
    assert runtime._active_handle is None


class FakeInterruptBus:
    """中断信号总线替身：记录订阅并支持触发。"""

    def __init__(self):
        self.handlers: list = []

    def subscribe(self, handler):
        self.handlers.append(handler)

    def raise_(self):
        for handler in self.handlers:
            handler()


class CloseRecorder:
    """活跃请求句柄替身：记录关闭调用。"""

    def __init__(self, error: Exception | None = None):
        self.closed = 0
        self.error = error

    def close(self):
        self.closed += 1
        if self.error is not None:
            raise self.error


def test_interrupt_subscription_closes_active_handle():
    """Scenario 中断关闭连接：中断触发且存在活跃句柄 → 该连接/流被关闭。"""
    bus = FakeInterruptBus()
    client = LLMClient(FakeConfig(), tool_definitions=[], interrupt=bus,
                       thinking_rules=defaults)
    handle = CloseRecorder()
    client._runtime.attach_handle(handle)
    assert len(bus.handlers) == 1
    bus.raise_()
    assert handle.closed == 1


def test_interrupt_without_handle_is_noop():
    """Scenario 空闲中断：无活跃句柄 → 空操作。"""
    bus = FakeInterruptBus()
    client = LLMClient(FakeConfig(), tool_definitions=[], interrupt=bus,
                       thinking_rules=defaults)
    bus.raise_()
    assert client._runtime._active_handle is None


def test_abort_ignores_close_errors():
    """关闭异常被忽略（中断路径不抛错）。"""
    bus = FakeInterruptBus()
    client = LLMClient(FakeConfig(), tool_definitions=[], interrupt=bus,
                       thinking_rules=defaults)
    client._runtime.attach_handle(CloseRecorder(error=RuntimeError("已关闭")))
    bus.raise_()


def test_client_without_interrupt_bus_is_fine():
    """未注册中断时构造与取消检查均正常（不接线即无订阅）。"""
    client = LLMClient(FakeConfig(), tool_definitions=[], thinking_rules=defaults)
    client.abort_active_request()


def test_stream_constants_pinned_by_spec():
    """常量对齐 spec：流内取消轮询 ≤0.05 秒、退避取消轮询 ≤0.2 秒、静默阈值 180 秒。"""
    assert STREAM_POLL_SECONDS <= 0.05
    assert RETRY_POLL_SECONDS <= 0.2
    assert STREAM_STALL_SECONDS == 180.0
    backend, _, _ = make_openai(script=[[]])
    assert backend._stall_seconds == 180.0


def test_anthropic_handle_points_to_http_client_then_response():
    """活跃句柄语义：请求发出前指向底层 HTTP 客户端、流就绪后指向响应流、结束清空。"""
    backend, http, runtime = make_anthropic(script=[sse_response([
        a_text("答"), a_message_delta("end_turn")])])
    generator = backend.chat_stream([])
    assert next(generator) == {KEY_CONTENT: "答"}
    assert runtime._active_handle is not None
    assert runtime._active_handle is not http.clients[0]
    assert list(generator) == [{KEY_FINISH_REASON: "stop", KEY_THINKING: "",
                                KEY_THINKING_SIGNATURE: None}]
    assert runtime._active_handle is None
    assert http.handle_during_request is http.clients[0]


def test_openai_handle_points_to_client_then_stream():
    """OpenAI 侧活跃句柄：发请求前为请求级 scope（持有共享客户端），流结束后清空。

    （句柄语义随 FIX-ESC-1 有意变更：由"共享客户端本身"改为"请求级 scope"——abort
    关闭 scope 只掐断在途请求并标记客户端重建，不再杀死共享客户端，见 P3 回归用例。）
    """
    stream = [openai_chunk(content="答", finish_reason="stop")]
    backend, fake, runtime = make_openai(script=[stream])
    list(backend.chat_stream([]))
    handle = fake.handle_during_call
    assert handle is not fake
    assert handle._client is fake
    assert runtime._active_handle is None


# ═══════════════════════════════════════════════════════════════
# FIX-ESC-1：请求发送阶段可取消（根因 A/B 回归 + P3 修复回归）
# ═══════════════════════════════════════════════════════════════


class BlockingSendClient:
    """httpx 客户端替身：send 阻塞到显式释放，close 对其无效（模拟建连阶段，exp4 实证）。"""

    def __init__(self, block_seconds: float = 3.0):
        self.closed = 0
        self.released = threading.Event()
        self.response = CloseRecorder()
        self._block_seconds = block_seconds

    def build_request(self, method, url, headers=None, json=None):
        return httpx.Request(method, url, headers=headers or {})

    def send(self, request, stream=False):
        self.released.wait(self._block_seconds)
        return self.response

    def close(self):
        self.closed += 1  # 只关连接池，不中断在途请求（慢路径根因 A）


class InterruptibleSendClient:
    """httpx 客户端替身：send 阻塞直到 close 被调用（close 有效，掐断在途请求）。"""

    def __init__(self):
        self.closed = 0
        self._closed = threading.Event()

    def build_request(self, method, url, headers=None, json=None):
        return httpx.Request(method, url, headers=headers or {})

    def send(self, request, stream=False):
        self._closed.wait(3.0)
        raise httpx.ReadError("连接已关闭")

    def close(self):
        self.closed += 1
        self._closed.set()


class ClosableFakeOpenAI:
    """openai SDK 客户端替身：create 可阻塞、close 后不可用（模拟 abort 关闭客户端）。"""

    def __init__(self, script=None, block_seconds: float = 0.0):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.closed = 0
        self.entered = threading.Event()   # create 已进入（请求已在途）
        self.release = threading.Event()   # 放行阻塞中的 create（测试收尾用）
        self._block_seconds = block_seconds
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(self._block_seconds)
        if self.closed:
            raise openai.APIConnectionError(
                request=httpx.Request("POST", "http://api.example.com/chat/completions"))
        item = self.script.pop(0) if self.script else []
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed += 1


def wait_until(predicate, timeout: float = 2.0) -> bool:
    """轮询等待条件成立（后台线程收尾断言用，避免固定 sleep 拖慢用例）。"""
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


def test_run_cancelable_normal_path_returns_immediately():
    """正常完成：子线程完成即返回（无固定轮询延迟），结果为阻塞调用返回值。"""

    def do_block():
        return "ok"

    started = time.perf_counter()
    assert do_block() == "ok"
    baseline = time.perf_counter() - started

    samples = []
    for _ in range(3):
        started = time.perf_counter()
        outcome = run_cancelable(do_block, None)
        samples.append((time.perf_counter() - started, outcome))

    assert all(outcome == (False, "ok", None) for _, outcome in samples)
    assert min(elapsed for elapsed, _ in samples) < 0.3
    # 对比基线增量 < 50ms：若实现退化为"固定轮询周期等待"，增量必 ≥ poll_seconds
    assert min(elapsed for elapsed, _ in samples) - baseline < 0.05


def test_run_cancelable_checks_cancel_while_blocking():
    """阻塞期间以轮询粒度检查取消标记；未命中则等阻塞完成、返回其结果。"""
    polls: list[float] = []
    release = threading.Event()

    def do_block():
        release.wait(2.0)
        return "done"

    timer = threading.Timer(0.15, release.set)
    timer.start()
    try:
        cancelled, result, error = run_cancelable(do_block, lambda: (polls.append(1), False)[1])
    finally:
        timer.cancel()

    assert (cancelled, result, error) == (False, "done", None)
    assert len(polls) >= 1


def test_run_cancelable_cancel_returns_early_and_worker_self_destructs():
    """取消命中：不等阻塞调用结束即返回（cancelled=True）、on_cancel 已调用；
    阻塞解除后结果由子线程自毁（双保险）。"""
    cancel = threading.Event()
    release = threading.Event()
    handle = CloseRecorder()
    on_cancel_calls: list[int] = []

    def do_block():
        release.wait(2.0)  # 模拟"连接建立中"：取消对其无效，直到阻塞自然解除
        return handle

    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        started = time.perf_counter()
        cancelled, result, error = run_cancelable(
            do_block, cancel.is_set, on_cancel=lambda: on_cancel_calls.append(1))
        elapsed = time.perf_counter() - started
    finally:
        timer.cancel()

    assert (cancelled, result, error) == (True, None, None)
    assert elapsed < 0.45          # 阻塞时长 2s，主流程未等其结束
    assert on_cancel_calls == [1]  # 尽力掐断已调用

    release.set()                  # 阻塞解除 → 子线程补关结果
    assert wait_until(lambda: handle.closed == 1)


def test_run_cancelable_block_error_returned():
    """阻塞调用抛异常：以 error 原样返回（交调用方既有异常分支处理），不产生结果。"""
    boom = RuntimeError("连接被拒绝")

    def do_block():
        raise boom

    cancelled, result, error = run_cancelable(do_block, None)
    assert cancelled is False
    assert result is None
    assert error is boom


def test_run_cancelable_race_cancel_with_completed_result_closes_it():
    """竞态：取消命中与阻塞完成同时发生 → 主线程补关结果并返回 cancelled=True。

    close 幂等：任何时序下结果至多被关闭两次（主线程竞态窗口一次、子线程兜底一次）。
    """
    allow_finish = threading.Event()
    handle = CloseRecorder()

    def do_block():
        allow_finish.wait(2.0)
        return handle

    def cancel_check():
        allow_finish.set()  # 让阻塞调用在本次轮询期间完成
        time.sleep(0.15)    # 等结果确实已产生（取消与完成同一时刻）
        return True

    cancelled, result, error = run_cancelable(do_block, cancel_check, poll_seconds=0.05)
    assert (cancelled, result, error) == (True, None, None)
    assert 1 <= handle.closed <= 2


def test_anthropic_cancel_during_connect_converges_fast():
    """核心回归（根因 A+B）：send 卡在建连阶段且 close 无效时取消 → 生成器 ≤0.5s 结束。

    旧实现此场景要等响应头到达（实测 5~11.5s，见 docs/recast/esc_probe/exp4）。
    """
    runtime = make_runtime(max_retries=1)
    client = BlockingSendClient(block_seconds=3.0)
    backend = AnthropicBackend(FakeConfig(protocol="anthropic"), runtime, None,
                               client_factory=lambda: client)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        started = time.perf_counter()
        events = list(backend.chat_stream([{"role": "user", "content": "q"}],
                                          cancel_check=cancel.is_set))
        elapsed = time.perf_counter() - started
    finally:
        timer.cancel()

    assert events == []                     # 静默结束：无完成/错误/中断事件
    assert elapsed < 0.5
    assert runtime._active_handle is None   # 活跃句柄已清理
    assert client.closed >= 1               # 尽力掐断（对本场景无效，靠轮询收敛）

    client.released.set()                   # 阻塞解除 → 在途结果由子线程自毁
    assert wait_until(lambda: client.response.closed == 1)


def test_anthropic_cancel_with_working_close_keeps_fast_path():
    """close 有效（已建立连接阶段）时取消：掐断在途请求，生成器 ≤0.5s 静默结束。"""
    runtime = make_runtime(max_retries=1)
    client = InterruptibleSendClient()
    backend = AnthropicBackend(FakeConfig(protocol="anthropic"), runtime, None,
                               client_factory=lambda: client)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        started = time.perf_counter()
        events = list(backend.chat_stream([], cancel_check=cancel.is_set))
        elapsed = time.perf_counter() - started
    finally:
        timer.cancel()

    assert events == []
    assert elapsed < 0.5
    assert client.closed >= 1
    assert runtime._active_handle is None


def test_openai_cancel_during_create_converges_fast():
    """OpenAI 侧同构回归：create 卡在建连阶段时取消 → 生成器 ≤0.5s 静默结束。"""
    runtime = make_runtime(max_retries=1)
    fake = ClosableFakeOpenAI(block_seconds=3.0)
    backend = OpenAIBackend(FakeConfig(), runtime, None, client=fake)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        started = time.perf_counter()
        events = list(backend.chat_stream([{"role": "user", "content": "q"}],
                                          cancel_check=cancel.is_set))
        elapsed = time.perf_counter() - started
    finally:
        timer.cancel()

    assert events == []
    assert elapsed < 0.5
    assert runtime._active_handle is None
    assert fake.closed >= 1                 # 尽力掐断 + 标记客户端重建
    assert backend._need_rebuild is True
    fake.release.set()


def test_openai_client_reused_across_turns_without_interrupt():
    """无中断时共享客户端跨轮复用：不每次请求重建、不被关闭。"""
    fake = ClosableFakeOpenAI(script=[[openai_chunk(content="一", finish_reason="stop")],
                                      [openai_chunk(content="二", finish_reason="stop")]])
    backend = OpenAIBackend(FakeConfig(), make_runtime(), None, client=fake)
    list(backend.chat_stream([]))
    list(backend.chat_stream([]))
    assert backend._client is fake
    assert fake.closed == 0
    assert len(fake.calls) == 2


def test_openai_abort_rebuilds_client_for_next_turn(monkeypatch):
    """P3 回归：响应头前打断（abort 关闭请求级 scope）→ 下一轮用重建的客户端成功发出。

    旧实现 abort 直接关闭共享客户端，打断一次后整局对话全部失败（APIConnectionError）。
    """
    runtime = make_runtime(max_retries=1)
    first = ClosableFakeOpenAI(block_seconds=3.0)
    second = ClosableFakeOpenAI(script=[[openai_chunk(content="二", finish_reason="stop")]])
    backend = OpenAIBackend(FakeConfig(), runtime, None, client=first)
    built: list[int] = []
    monkeypatch.setattr(backend, "_build_client", lambda: (built.append(1), second)[1])
    interrupted = threading.Event()

    def press_esc():
        first.entered.wait(2.0)  # 请求已在途（等响应头，句柄为请求级 scope）
        runtime.abort()          # 中断广播：关闭活跃句柄
        interrupted.set()        # 中断标志置位（取消检查点）

    threading.Thread(target=press_esc, daemon=True).start()
    events = list(backend.chat_stream([{"role": "user", "content": "一"}],
                                      cancel_check=interrupted.is_set))
    assert events == []                     # 静默打断：无完成/错误/中断事件
    assert runtime._active_handle is None
    assert first.closed >= 1
    first.release.set()

    # 打断后会话继续：下一轮用重建的客户端发出请求并正常完成
    events2 = list(backend.chat_stream([{"role": "user", "content": "二"}]))
    assert backend._client is second
    assert len(built) == 1                  # 重建恰好一次（不重复换客户端）
    assert len(second.calls) == 1
    assert [e[KEY_CONTENT] for e in events2 if KEY_CONTENT in e] == ["二"]
    assert events2[-1][KEY_FINISH_REASON] == "stop"


# ═══════════════════════════════════════════════════════════════
# 17. 工具定义动态管理
# ═══════════════════════════════════════════════════════════════

GOAL_DEF = {"type": "function",
            "function": {"name": "GoalComplete", "description": "声明完成",
                         "parameters": {"type": "object", "properties": {}}}}
SHELL_DEF = {"type": "function",
             "function": {"name": "Shell", "description": "命令",
                          "parameters": {"type": "object", "properties": {}}}}
MCP_DEF = {"type": "function",
           "function": {"name": "mcp__x__y", "description": "MCP 工具",
                        "parameters": {"type": "object", "properties": {}}}}


def make_client(tool_definitions=None, goal=True, **config_kwargs):
    """构造客户端并在装配位注入 `config.defaults` 的 thinking 解析能力。"""
    config = FakeConfig(**config_kwargs)
    return LLMClient(config, tool_definitions=tool_definitions,
                     goal_tool_definition=GOAL_DEF if goal else None,
                     thinking_rules=defaults)


def tool_names(client) -> list[str]:
    return [d.get("function", {}).get("name") for d in client._runtime.tool_defs]


def test_set_goal_tool_injection_is_idempotent():
    """Scenario 注入幂等：目标模式连续两次开启 → GoalComplete 定义仍只存在一份。"""
    client = make_client([SHELL_DEF])
    client.set_goal_tool(True)
    client.set_goal_tool(True)
    assert tool_names(client) == ["Shell", "GoalComplete"]


def test_set_goal_tool_removal_is_idempotent():
    """关闭 SHALL 移除该定义（幂等）。"""
    client = make_client([SHELL_DEF])
    client.set_goal_tool(True)
    client.set_goal_tool(False)
    client.set_goal_tool(False)
    assert tool_names(client) == ["Shell"]


def test_set_goal_tool_accepts_explicit_definition():
    """定义可随调用送入（缺省用构造注入的定义）。"""
    client = make_client([], goal=False)
    client.set_goal_tool(True, GOAL_DEF)
    assert tool_names(client) == ["GoalComplete"]
    client.set_goal_tool(False, GOAL_DEF)
    assert tool_names(client) == []


def test_set_goal_tool_without_definition_is_noop():
    """未注入目标工具定义（装配未接线）时为无操作，不抛错。"""
    client = make_client([SHELL_DEF], goal=False)
    client.set_goal_tool(True)
    assert tool_names(client) == ["Shell"]


def test_add_tool_definitions_dedup_by_name():
    """Scenario 热追加去重：同名定义不重复追加，不同名定义被追加。"""
    client = make_client([SHELL_DEF])
    client.add_tool_definitions([SHELL_DEF, MCP_DEF])
    assert tool_names(client) == ["Shell", "mcp__x__y"]


def test_add_tool_definitions_intra_batch_and_empty_name():
    """同一次送入列表内部也去重；名称为空的定义跳过。"""
    client = make_client([])
    client.add_tool_definitions([MCP_DEF, dict(MCP_DEF), {"type": "function",
                                                          "function": {"name": ""}}])
    assert tool_names(client) == ["mcp__x__y"]


def test_remove_tool_definitions_removes_all_matches():
    """Scenario 热注销：清单中所有同名定义被移除，其余定义不受影响。"""
    client = make_client([SHELL_DEF, MCP_DEF])
    client.remove_tool_definitions(["mcp__x__y", "不存在"])
    assert tool_names(client) == ["Shell"]


def test_remove_tool_definitions_empty_noop():
    """空名单为无操作。"""
    client = make_client([SHELL_DEF])
    client.remove_tool_definitions([])
    client.remove_tool_definitions(None)
    assert tool_names(client) == ["Shell"]


def test_caller_tool_list_never_mutated():
    """Scenario 原列表不变：构造后任意动态变更不影响调用方传入的原列表。"""
    original = [SHELL_DEF]
    snapshot = json.loads(json.dumps(original, ensure_ascii=False))
    client = make_client(original)
    assert client._runtime.tool_defs is not original  # 构造时复制一份
    client.set_goal_tool(True)
    client.add_tool_definitions([MCP_DEF])
    client.remove_tool_definitions(["Shell"])
    assert original == snapshot
    assert tool_names(client) == ["GoalComplete", "mcp__x__y"]


def test_dynamic_tool_change_visible_in_next_request():
    """动态变更对下一轮请求立即生效（请求组装时现读该列表）。"""
    runtime = make_runtime(tool_defs=[SHELL_DEF])
    fake = FakeOpenAI([[], []], runtime)
    backend = OpenAIBackend(FakeConfig(), runtime, None, client=fake)
    list(backend.chat_stream([]))
    runtime.tool_defs.append(MCP_DEF)
    list(backend.chat_stream([]))
    assert fake.tools_at_call == [["Shell"], ["Shell", "mcp__x__y"]]


def test_tool_names_not_rewritten():
    """系统 SHALL NOT 改写工具名（重名消解由来源侧完成）。"""
    client = make_client([dict(SHELL_DEF, function=dict(SHELL_DEF["function"],
                                                       name="Read"))])
    assert tool_names(client) == ["Read"]


# ═══════════════════════════════════════════════════════════════
# 18. 原始响应取证
# ═══════════════════════════════════════════════════════════════


def test_anthropic_raw_sse_records_data_segments_in_order():
    """Scenario 记录数据段：按到达顺序可读取到对应数据段（其余行不记录）。"""
    raw = ('event: ping\n'
           'data: {"type": "message_start", "message": {}}\n'
           '\n'
           ': comment\n'
           '\n'
           'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n'
           '\n')
    backend, _, _ = make_anthropic(script=[httpx.Response(200, content=raw.encode("utf-8"))])
    list(backend.chat_stream([]))
    assert backend.raw_sse == [
        '{"type": "message_start", "message": {}}',
        '{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}',
    ]


def test_anthropic_raw_sse_reset_each_request():
    """每轮请求开始时清空记录。"""
    backend, _, _ = make_anthropic(script=[
        sse_response([a_text("1"), a_message_delta("end_turn")]),
        sse_response([a_text("2"), a_message_delta("end_turn")])])
    list(backend.chat_stream([]))
    first = backend.raw_sse
    assert len(first) == 2
    list(backend.chat_stream([]))
    assert len(backend.raw_sse) == 2
    assert backend.raw_sse[0] != first[0]


def test_raw_sse_read_returns_copy():
    """读取时返回副本（改写结果不影响内部记录）。"""
    backend, _, _ = make_anthropic(script=[sse_response([a_message_delta("end_turn")])])
    list(backend.chat_stream([]))
    copy = backend.raw_sse
    copy.append("伪造")
    raw = json.dumps(a_message_delta("end_turn"), ensure_ascii=False)
    assert backend.raw_sse == [raw]


def test_client_raw_sse_anthropic_and_openai():
    """Scenario OpenAI 无记录：OpenAI 协议读取取证数据得到无效（None）结果。"""
    anthropic_client = LLMClient(FakeConfig(protocol="anthropic"), tool_definitions=[],
                                 thinking_rules=defaults)
    assert anthropic_client.raw_sse == []
    openai_client = LLMClient(FakeConfig(protocol="openai"), tool_definitions=[],
                              thinking_rules=defaults)
    assert openai_client.raw_sse is None


# ═══════════════════════════════════════════════════════════════
# 19. 请求裁剪选项
# ═══════════════════════════════════════════════════════════════


def test_no_tools_omits_tools_both_protocols():
    """Scenario 不带工具定义：两种协议的请求体内均不出现 tools。"""
    runtime = make_runtime(tool_defs=[SHELL_DEF])
    fake = FakeOpenAI([[]], runtime)
    openai_backend = OpenAIBackend(FakeConfig(), runtime, None, client=fake)
    list(openai_backend.chat_stream([], no_tools=True))
    assert "tools" not in fake.calls[0]

    http = FakeHTTP([sse_response([a_message_delta("end_turn")])], runtime)
    anthropic_backend = AnthropicBackend(FakeConfig(protocol="anthropic"), runtime, None,
                                         client_factory=http.factory)
    list(anthropic_backend.chat_stream([], no_tools=True))
    assert "tools" not in json.loads(http.requests[0].read())


def test_tools_present_by_default_in_anthropic_body():
    """未裁剪且工具列表非空 → 请求体含 tools。"""
    runtime = make_runtime(tool_defs=[SHELL_DEF])
    http = FakeHTTP([sse_response([a_message_delta("end_turn")])], runtime)
    backend = AnthropicBackend(FakeConfig(protocol="anthropic"), runtime, None,
                               client_factory=http.factory)
    list(backend.chat_stream([]))
    assert json.loads(http.requests[0].read())["tools"][0]["name"] == "Shell"


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "mimo-v2.5", "claude-sonnet-4"])
def test_no_thinking_without_disable_mapping_sends_nothing(model):
    """Scenario 不带思考且无禁用映射：请求体内不出现任何 thinking 相关参数。"""
    runtime = make_runtime()
    http = FakeHTTP([sse_response([])], runtime)
    backend = AnthropicBackend(FakeConfig(protocol="anthropic", model=model), runtime, None,
                               client_factory=http.factory)
    list(backend.chat_stream([], no_thinking=True))
    body = json.loads(http.requests[0].read())
    assert "thinking" not in body
    assert "output_config" not in body
    assert "effort" not in body


def test_no_thinking_gpt_uses_disable_branch():
    """GPT 的禁用映射存在 → 显式传 reasoning_effort="none"。"""
    runtime = make_runtime()
    fake = FakeOpenAI([[]], runtime)
    backend = OpenAIBackend(FakeConfig(model="gpt-5"), runtime, None, client=fake)
    list(backend.chat_stream([], no_thinking=True))
    assert fake.calls[0]["reasoning_effort"] == "none"


def test_no_thinking_kimi_explicit_disable():
    """Kimi 的禁用映射存在 → 扩展体传 thinking={"type": "disabled"}。"""
    runtime = make_runtime()
    fake = FakeOpenAI([[]], runtime)
    backend = OpenAIBackend(FakeConfig(model="kimi-k2"), runtime, None, client=fake)
    list(backend.chat_stream([], no_thinking=True))
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_no_thinking_does_not_affect_passback_output():
    """Scenario 裁剪不影响回传：完成事件中的思考输出仍按回传开关与格式判定。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(reasoning="想"), openai_chunk(content="答", finish_reason="stop")]])
    events = list(backend.chat_stream([], no_thinking=True))
    assert events[-1][KEY_THINKING] == "想"


# ═══════════════════════════════════════════════════════════════
# 20. 兼容性怪癖保持
# ═══════════════════════════════════════════════════════════════


def test_quirk_openai_empty_name_tool_call_still_reported():
    """Scenario 空名工具调用（兼容怪癖）：名称与参数均为空仍输出该调用（不过滤）。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(tool_calls=[tool_delta(0, "call-1", "", "")]),
        openai_chunk(finish_reason="tool_calls")]])
    calls = list(backend.chat_stream([]))[-1][KEY_TOOL_CALLS]
    assert calls == [{"id": "call-1", "type": "function",
                      "function": {"name": "", "arguments": ""}}]


def test_quirk_anthropic_fallback_filters_empty_id_or_name():
    """Scenario 兜底过滤（兼容怪癖）：缺结束事件时 id 或名称为空的工具调用被过滤。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_tool_use("", "Shell"), a_json("{}"),
        a_tool_use("c2", "Read", index=2), a_json('{"p": "x"}', index=2)])])
    events = list(backend.chat_stream([]))
    completion = [e for e in events if KEY_TOOL_CALLS in e][0]
    assert [tc["id"] for tc in completion[KEY_TOOL_CALLS]] == ["c2"]
    assert completion[KEY_FINISH_REASON] == "tool_calls"


def test_quirk_anthropic_fallback_all_filtered_no_completion():
    """若工具调用全部被过滤 → 本轮不产出任何完成事件。"""
    backend, _, _ = make_anthropic(script=[sse_response([
        a_tool_use("", "Shell"), a_json("{}"),
        a_tool_use("c3", "", index=2), a_json("{}", index=2)])])
    assert list(backend.chat_stream([])) == []


def test_quirk_multi_thinking_blocks_no_signature():
    """Scenario 多思考块签名（兼容怪癖）：两个以上思考块 → thinking_signature 为空值。"""
    config = FakeConfig(protocol="anthropic", model="claude-sonnet-4")
    backend, _, _ = make_anthropic(config=config, script=[sse_response([
        a_thinking_block(0), a_thinking("思考一", 0), a_signature("sig1", 0),
        a_thinking_block(1), a_thinking("思考二", 1), a_signature("sig2", 1),
        a_text("答"), a_message_delta("end_turn")])])
    completion = list(backend.chat_stream([]))[-1]
    assert completion[KEY_THINKING] == "思考一思考二"
    assert completion[KEY_THINKING_SIGNATURE] is None


def test_quirk_anthropic_3xx_generic_error_text():
    """Scenario 3xx 状态（兼容怪癖）：按通用错误文案上报。"""
    backend, _, _ = make_anthropic(script=[text_response(302, "found")])
    assert list(backend.chat_stream([])) == [
        {KEY_CONTENT: "[错误: API调用失败(302): found]", KEY_FINISH_REASON: "error"}]


def test_quirk_anthropic_generic_error_text_truncated():
    """通用错误文案截取响应文本前 200 字符。"""
    backend, _, _ = make_anthropic(script=[text_response(302, "z" * 300)])
    content = list(backend.chat_stream([]))[0][KEY_CONTENT]
    assert content == "[错误: API调用失败(302): " + "z" * 200 + "]"


def test_quirk_anthropic_connect_timeout_differs_from_openai():
    """连接超时不一致（兼容怪癖）：Anthropic 10 秒 vs OpenAI 5 秒。"""
    anthropic_client = AnthropicBackend._new_client()
    try:
        assert anthropic_client.timeout.connect == 10.0
    finally:
        anthropic_client.close()
    backend, fake, _ = make_openai(script=[[]])
    list(backend.chat_stream([]))
    assert fake.calls[0]["timeout"].connect == 5.0


def test_quirk_openai_reasoning_read_from_model_extra():
    """思考增量两路读取：delta 属性或扩展字段（model_extra）均可捕获。"""
    backend, _, _ = make_openai(script=[[
        openai_chunk(reasoning="扩展", reasoning_in_extra=True),
        openai_chunk(finish_reason="stop")]])
    assert list(backend.chat_stream([]))[-1][KEY_THINKING] == "扩展"
