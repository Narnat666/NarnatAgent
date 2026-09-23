"""mcp 积木自测 —— T3.8 交付物（client / manager / MCP 工具）。

覆盖（对齐 T3.8 任务书测试要求）：
1. spec Scenario 全覆盖：映射表 `SPEC_SCENARIO_MAP`（11 Requirement / 45 场景 →
   测试函数），并由 `test_spec_scenario_map_complete` 机械校验（映射目标存在、
   数量吻合、无未归类测试）；
2. 命名规则矩阵（非法字符/超长/重名/空名哈希兜底）——纯函数直测，与 R8 报告
   「行为要点 22/23」逐条对照（见 `test_naming_matrix_matches_r8_report`）；
3. 客户端协议用 fake 子进程（临时目录里的 JSON-RPC 回显服务器脚本）测试
   握手顺序/分页/调用/错误/超时/关闭/服务端请求回绝；
4. 连接管理器用真实子进程（主链路：连接→调用→断线重连→断开）与假客户端
   （并发上限/幂等/死连接/落败路径/atexit/清理）两条路径；
5. MCP 工具文案与参数契约用假端口直测（错误行带框架标签、失败不阻塞）；
6. 结构：mcp 只依赖 contracts 与 config（AST 检查）、包导出可解析。
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from narnat_agent.mcp import (
    MCP_DEFINITION,
    MAX_SERVERS,
    McpDynamicTool,
    McpError,
    McpManager,
    McpStdioClient,
    McpTool,
    TOOL_PREFIX,
    decode_line,
    format_content,
    make_unique_name,
    sanitize_name_part,
)
from narnat_agent.mcp import client as client_module
from narnat_agent.mcp import manager as manager_module
from narnat_agent.tools import signal
from narnat_agent.tools.catalog import ToolCatalogImpl
from narnat_agent.tools.env import ToolEnvImpl
from narnat_agent.tools.registry import ToolRegistry

MCP_DIR = Path(__file__).resolve().parents[2] / "narnat_agent" / "mcp"

# ═══════════════════════════════════════════════════════════════
# spec Scenario 映射表（specs/mcp：11 Requirement / 45 场景）
# ═══════════════════════════════════════════════════════════════

SPEC_SCENARIO_MAP: dict[str, list[tuple[str, str]]] = {
    "MCP 工具名与参数契约": [
        ("默认 action", "test_action_defaults_to_connect"),
        ("未知 action", "test_unknown_action_error"),
        ("管理器缺失", "test_missing_manager_error"),
    ],
    "MCP connect 前置校验与结果文案": [
        ("缺 name", "test_connect_requires_name"),
        ("缺 config", "test_connect_requires_config"),
        ("缺启动命令", "test_connect_requires_command"),
        ("成功展示与 20 项上限", "test_connect_success_show_and_limit"),
    ],
    "MCP disconnect 行为与结果文案": [
        ("断开全部", "test_disconnect_all_text"),
        ("未连接", "test_disconnect_unknown_server"),
        ("大小写不敏感匹配", "test_disconnect_case_insensitive"),
    ],
    "连接流程与 JSON-RPC 握手协议": [
        ("握手顺序", "test_handshake_order"),
        ("分页列举", "test_tools_list_pagination"),
        ("启动失败", "test_start_failure"),
        ("握手失败不残留进程", "test_handshake_failure_closes_process"),
        ("服务端主动请求回绝", "test_server_request_rejected"),
    ],
    "MCP 工具热注册与命名规则": [
        ("命名规则", "test_make_unique_name_plain"),
        ("非法字符替换", "test_sanitize_name_part_replaces_illegal_chars"),
        ("纯中文名哈希兜底", "test_sanitize_name_part_hash_fallback"),
        ("超长与重名截断", "test_make_unique_name_truncates_overlong"),
        ("白黑名单过滤", "test_enabled_tools_filter"),
        ("下一轮即可见", "test_next_round_definitions_visible"),
    ],
    "MCP 工具调用协议与结果格式化": [
        ("文本内容", "test_call_text_content"),
        ("图片与资源块", "test_call_image_and_resource"),
        ("错误标志", "test_call_is_error_flag"),
        ("服务端错误响应", "test_call_server_error"),
        ("调用超时", "test_call_timeout"),
    ],
    "MCP 断开、工具注销与进程终止": [
        ("注销工具", "test_disconnect_unregisters_tools"),
        ("优雅关闭", "test_close_graceful"),
        ("超时强杀", "test_close_kills_stuck_server"),
        ("重复断开", "test_disconnect_twice_reports_not_connected"),
    ],
    "MCP 通道保活与自动重连": [
        ("断线后重连并要求重试", "test_reconnect_after_process_death"),
        ("不自动重跑", "test_reconnect_does_not_replay_call"),
        ("服务端自身错误不重连", "test_server_error_does_not_reconnect"),
        ("重连期间已被断开", "test_reconnect_discarded_when_disconnected"),
    ],
    "进程生命周期与回收": [
        ("独立进程组", "test_child_process_isolated_group"),
        ("并发上限", "test_server_limit"),
        ("幂等复用", "test_idempotent_reuse"),
        ("死连接重建", "test_dead_client_rebuild"),
        ("退出兜底回收", "test_atexit_fallback_registered"),
    ],
    "失败不阻塞原则与错误文案": [
        ("连接失败不阻塞", "test_connect_failure_does_not_block"),
        ("意外异常包装", "test_unexpected_exception_wrapped"),
        ("工具调用失败可继续", "test_tool_call_failure_does_not_break_session"),
    ],
    "兼容性怪癖保持": [
        ("启用开关被忽略（兼容怪癖）", "test_forced_enabled_config"),
        ("登记数与注册数不一致（兼容怪癖）", "test_registration_count_quirk"),
        ("分页静默截断（兼容怪癖）", "test_page_limit_silent_truncation"),
    ],
}

EXTRA_TESTS = {
    # 命名规则与解码/格式化的补充矩阵
    "test_sanitize_name_part_strips_underscores",
    "test_make_unique_name_resolves_conflicts",
    "test_naming_matrix_matches_r8_report",
    "test_decode_line_strategies",
    "test_format_content_blocks",
    # 管理器内部路径（假客户端）
    "test_connect_registers_tools",
    "test_concurrent_same_name_loser_discards_new_client",
    "test_disconnect_all_batch_stops_on_error",
    "test_cleanup_idempotent_and_keeps_registry",
    "test_reconnect_without_spec",
    "test_reconnect_incomplete_config",
    "test_tools_call_uses_raw_name",
    "test_builtin_name_collision_gets_hash_suffix",
    # MCP 工具文案补充
    "test_disconnect_requires_name",
    "test_mcp_tool_definition_contract",
    # 动态工具对象
    "test_dynamic_tool_forwards_and_formats",
    # 结构
    "test_package_exports",
    "test_mcp_dependencies_are_contracts_and_config_only",
    # 本映射表自身的校验
    "test_spec_scenario_map_complete",
}

# ═══════════════════════════════════════════════════════════════
# fake 子进程 MCP 服务器（JSON-RPC 回显；模式：normal/paged/endless/
# sampling/die_on_initialize/ignore_eof；transcript 记录全部收到/回出的消息）
# ═══════════════════════════════════════════════════════════════

MOCK_SERVER_SOURCE = '''\
import json
import sys
import time


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    transcript = sys.argv[2] if len(sys.argv) > 2 else ""
    tools = [
        {"name": "echo", "description": "回显参数",
         "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}},
        {"name": "search", "description": "搜索"},
        {"name": "rich"},
        {"name": "err"},
        {"name": "boom"},
        {"name": "slow"},
    ]

    def trace(obj):
        if transcript:
            with open(transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\\n")

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        trace(msg)
        method = msg.get("method")
        if method == "initialize":
            if mode == "sampling":
                send({"jsonrpc": "2.0", "id": 900, "method": "sampling/createMessage",
                      "params": {}})
                reply = sys.stdin.readline()
                trace({"client_reply": json.loads(reply or "null")})
            if mode == "die_on_initialize":
                sys.exit(0)
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "result": {"protocolVersion": "2025-06-18",
                             "serverInfo": {"name": "mock", "version": "0.1"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            if mode == "endless":
                page = int(cursor or 0)
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "result": {"tools": [{"name": "tool%d" % page}],
                                 "nextCursor": str(page + 1)}})
            elif mode == "paged":
                page = int(cursor or 0)
                result = {"tools": [{"name": "tool%d" % page}]}
                if page < 2:
                    result["nextCursor"] = str(page + 1)
                send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if name == "slow":
                time.sleep(30)
                continue
            if name == "boom":
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32000, "message": "boom"}})
            elif name == "err":
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "result": {"content": [], "isError": True}})
            elif name == "rich":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [
                    {"type": "image", "mimeType": "image/png", "data": "AA=="},
                    {"type": "resource", "resource": {"uri": "file:///x"}},
                ]}})
            else:
                args = (msg.get("params") or {}).get("arguments") or {}
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(args, ensure_ascii=False)}]}})
        else:
            if "id" in msg:
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32601, "message": "Method not found"}})

    if mode == "ignore_eof":
        time.sleep(30)


if __name__ == "__main__":
    main()
'''


class FakeLogger:
    """鸭子类型日志器：记录 `info(module, msg)` 调用。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, module: str, msg: str) -> None:
        self.records.append((module, msg))


class FakeToolTable:
    """模拟 LLM 工具表的热增删（对齐 LLMClient：按名去重、空名单无操作）。"""

    def __init__(self) -> None:
        self.definitions: list[dict] = []

    def add(self, definitions: list) -> None:
        names = {d["function"]["name"] for d in self.definitions}
        for definition in definitions or []:
            name = definition["function"]["name"]
            if name and name not in names:
                self.definitions.append(definition)
                names.add(name)

    def remove(self, names) -> None:
        targets = set(names or ())
        if not targets:
            return
        self.definitions[:] = [
            d for d in self.definitions if d["function"]["name"] not in targets
        ]


class FakeClient:
    """假 stdio 客户端（连接管理路径的可控替身）。"""

    def __init__(self, name, command, args=(), env=None, cwd=None, logger=None) -> None:
        self.name = name
        self.command = command
        self.args = list(args)
        self.alive = True
        self.closed = False
        self.tools = [{"name": "echo"}]
        self.info = {"serverInfo": {"name": "fake", "version": "1"}}
        self.calls: list[tuple] = []
        self.result: tuple[str, bool] = ("ok", False)
        self.raise_on_call: Exception | None = None

    def initialize(self, timeout):
        return dict(self.info)

    def list_tools(self, timeout):
        return [dict(tool) for tool in self.tools]

    def call_tool(self, tool_name, arguments, timeout):
        self.calls.append((tool_name, arguments, timeout))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.result

    def close(self, grace=3.0):
        self.closed = True
        self.alive = False


class FakeCaller:
    """假调用通道（动态工具对象直测用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result: tuple[str, bool] = ("ok", False)
        self.error: Exception | None = None

    def call_tool(self, server_name, tool_name, arguments, timeout):
        self.calls.append((server_name, tool_name, arguments, timeout))
        if self.error is not None:
            raise self.error
        return self.result


class FakePort:
    """假 McpPort（MCP 工具文案直测用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.servers: list[dict] = []
        self.connect_result: tuple[int, list[str]] = (2, ["mcp__demo__a", "mcp__demo__b"])
        self.disconnect_result = 4
        self.error: Exception | None = None

    def connect(self, name, config):
        self.calls.append(("connect", name, config))
        if self.error is not None:
            raise self.error
        return self.connect_result

    def disconnect(self, name):
        self.calls.append(("disconnect", name))
        if self.error is not None:
            raise self.error
        return self.disconnect_result

    def connected(self):
        return [dict(server) for server in self.servers]


class StubTool:
    """占位内置工具（注册表已用名模拟）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def definition(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": "内置",
            "parameters": {"type": "object", "properties": {}}}}

    def execute(self, args, env=None):
        return "内置结果"


class RejectingCatalog:
    """模拟「登记全量 vs 实际注册跳过」两个真相源的假工具目录（兼容怪癖③）。"""

    def __init__(self, rejected: str = "") -> None:
        self.rejected = rejected
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.definitions = 0

    def get_tool_names(self) -> list[str]:
        return list(self.registered)

    def register_dynamic(self, tools) -> list[str]:
        registered: list[str] = []
        for tool in tools:
            self.definitions += 1
            if tool.name == self.rejected:
                continue
            self.registered.append(tool.name)
            registered.append(tool.name)
        return registered

    def unregister_dynamic(self, names) -> None:
        targets = set(names or ())
        self.unregistered = list(names or ())
        self.registered[:] = [name for name in self.registered if name not in targets]


# ═══════════════════════════════════════════════════════════════
# fixtures 与帮助函数
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def mock_server(tmp_path_factory) -> str:
    """把 fake MCP 服务器脚本落到临时目录，返回脚本路径。"""
    path = tmp_path_factory.mktemp("mcp_mock") / "mock_mcp_server.py"
    path.write_text(MOCK_SERVER_SOURCE, encoding="utf-8")
    return str(path)


@pytest.fixture
def make_client(mock_server):
    """创建（并统一回收）真实子进程客户端。"""
    clients: list[McpStdioClient] = []

    def factory(mode: str = "normal", transcript=None, name: str = "demo") -> McpStdioClient:
        args = [mock_server, mode]
        if transcript is not None:
            args.append(str(transcript))
        client = McpStdioClient(name, sys.executable, args, logger=FakeLogger())
        clients.append(client)
        return client

    yield factory
    for client in clients:
        try:
            client.close(grace=0.2)
        except Exception:
            pass


def _server_config(mock_server: str, mode: str = "normal", transcript=None, **extra) -> dict:
    """构造 manager.connect 用的服务器配置（脚本 + 模式 + 记录文件）。"""
    args = [mock_server, mode]
    if transcript is not None:
        args.append(str(transcript))
    config = {"command": sys.executable, "args": args,
              "startup_timeout_sec": 10, "tool_timeout_sec": 10}
    config.update(extra)
    return config


def _read_transcript(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _make_catalog(registry: ToolRegistry | None = None):
    """装配 ToolCatalogImpl（注册表 + 假 LLM 工具表）。"""
    registry = registry if registry is not None else ToolRegistry()
    table = FakeToolTable()
    return ToolCatalogImpl(registry, table.add, table.remove), registry, table


def _make_manager(catalog, factory=None) -> McpManager:
    return McpManager(catalog, signal.error_line, logger=FakeLogger(), client_factory=factory)


def _fake_factory(clients: list[FakeClient]):
    def factory(name, command, args=(), env=None, cwd=None, logger=None):
        client = FakeClient(name, command, args)
        clients.append(client)
        return client
    return factory


def _dynamic_tool(caller, llm_name: str = "mcp__demo__tool", raw_name: str = "tool",
                  timeout: float = 5) -> McpDynamicTool:
    definition = {"type": "function", "function": {
        "name": llm_name, "description": "d",
        "parameters": {"type": "object", "properties": {}}}}
    return McpDynamicTool(manager=caller, definition=definition, server_name="demo",
                          raw_name=raw_name, timeout=timeout, error_line=signal.error_line)


def _plain(text: str) -> str:
    """剥离框架标签后的文本（交付 AI 前形态）。"""
    return signal.strip_tags(text)


# ═══════════════════════════════════════════════════════════════
# 1. 命名规则矩阵（纯函数；对照 R8 报告行为要点 22/23）
# ═══════════════════════════════════════════════════════════════


def test_sanitize_name_part_replaces_illegal_chars():
    """非法字符替换为 `_`，并去首尾 `_`；组合名走 `mcp__<服务器>__<工具>`。"""
    assert sanitize_name_part("my server") == "my_server"
    assert sanitize_name_part("a.b") == "a_b"
    assert make_unique_name("my server", "a.b", set()) == "mcp__my_server__a_b"


def test_sanitize_name_part_strips_underscores():
    """首尾下划线被去除（内部保留）。"""
    assert sanitize_name_part("__a__") == "a"
    assert sanitize_name_part("a__b") == "a__b"


def test_sanitize_name_part_hash_fallback():
    """清洗后为空（纯中文/空串）时以 `srv_` + SHA1 前 8 位兜底；稳定且成对唯一。"""
    expected = "srv_" + hashlib.sha1("中文服务".encode("utf-8")).hexdigest()[:8]
    assert sanitize_name_part("中文服务") == expected
    assert sanitize_name_part("中文服务") == expected       # 稳定
    assert sanitize_name_part("") == "srv_" + hashlib.sha1(b"").hexdigest()[:8]
    assert sanitize_name_part("中文甲") != sanitize_name_part("中文乙")


def test_make_unique_name_plain():
    """合法且未占用：直接 `mcp__<服务器>__<工具>`，并登记进 used_names。"""
    used: set = set()
    name = make_unique_name("demo", "search", used)
    assert name == f"{TOOL_PREFIX}demo__search"
    assert used == {name}


def test_make_unique_name_truncates_overlong():
    """超长：截断为前 51 字符 + `_` + 12 位哈希后缀（总长 ≤64）。"""
    used: set = set()
    long_tool = "x" * 80
    name = make_unique_name("demo", long_tool, used)
    identity = f"demo\0{long_tool}"
    suffix = "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    assert len(name) == 64
    assert name == (TOOL_PREFIX + f"demo__{long_tool}")[:64 - len(suffix)] + suffix
    assert name in used


def test_make_unique_name_resolves_conflicts():
    """重名：追加哈希后缀；同标识再冲突时更换哈希直到唯一。"""
    used: set = set()
    first = make_unique_name("demo", "search", used)
    second = make_unique_name("demo", "search", used)
    identity = "demo\0search"
    suffix = "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    assert second == first[:64 - len(suffix)] + suffix
    assert second != first and len(second) <= 64
    third = make_unique_name("demo", "search", used)
    assert len({first, second, third}) == 3
    assert all(len(n) <= 64 for n in used)


def test_naming_matrix_matches_r8_report():
    """R8 报告「行为要点 22/23」逐条对照（报告证据）。"""
    # 22：非法字符全替换 + strip("_")；清空 → srv_ + sha1(utf-8)[:8]
    assert sanitize_name_part("a b/c.d") == "a_b_c_d"
    assert sanitize_name_part("工具") == "srv_" + hashlib.sha1("工具".encode("utf-8")).hexdigest()[:8]
    # 23：base = mcp__ + 清洗(服务器) + __ + 清洗(工具)；≤64 且未占用直接使用
    assert make_unique_name("srv", "tool", set()) == "mcp__srv__tool"
    # 23：超长/重名 → base[:64-13] + "_" + sha1(server\0tool)[:12]
    used = {"mcp__demo__search"}
    conflicted = make_unique_name("demo", "search", used)
    suffix = "_" + hashlib.sha1(b"demo\0search").hexdigest()[:12]
    assert conflicted == "mcp__demo__search" + suffix
    assert conflicted != "mcp__demo__search" and len(conflicted) <= 64


# ═══════════════════════════════════════════════════════════════
# 2. 客户端纯函数（解码 / 内容格式化）
# ═══════════════════════════════════════════════════════════════


def test_decode_line_strategies():
    """UTF-8 严格优先；Windows 回退 GBK；无效字节 replace 兜底。"""
    assert decode_line("中文".encode("utf-8")) == "中文"
    gbk = "中文".encode("gbk")
    if sys.platform == "win32":
        assert decode_line(gbk) == "中文"
    else:
        assert "\ufffd" in decode_line(gbk)
    assert "\ufffd" in decode_line(b"\xff\xfe\xfa")


def test_format_content_blocks():
    """content 块拼接：text/image/resource/未知类型/非 dict/structuredContent 兜底。"""
    assert format_content({"content": [{"type": "text", "text": "你好"}]}) == "你好"
    assert format_content({"content": [{"type": "image", "mimeType": "image/png"},
                                       {"type": "image"}]}) == "[图片: image/png]\n[图片: 未知类型]"
    assert format_content({"content": [
        {"type": "resource", "resource": {"uri": "file:///x", "text": "内容"}},
        {"type": "resource", "resource": {"uri": "file:///y"}},
    ]}) == "内容\n[资源: file:///y]"
    assert format_content({"content": [{"type": "other", "k": 1}]}) == '{"type": "other", "k": 1}'
    assert format_content({"content": ["raw"]}) == "raw"
    assert format_content({"content": [], "structuredContent": {"a": 1}}) == '{"a": 1}'
    assert format_content({}) == ""


# ═══════════════════════════════════════════════════════════════
# 3. 客户端协议（fake 子进程）
# ═══════════════════════════════════════════════════════════════


def test_handshake_order(make_client, tmp_path):
    """握手顺序：initialize（逐字参数）→ notifications/initialized → tools/list。"""
    transcript = tmp_path / "t.jsonl"
    client = make_client("normal", transcript)
    assert client.initialize(timeout=10)["serverInfo"]["name"] == "mock"
    client.list_tools(timeout=10)

    methods = [m.get("method") for m in _read_transcript(transcript)]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    init_request = _read_transcript(transcript)[0]
    assert init_request["params"] == {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "narnat-agent", "title": "Narnat Agent", "version": "1.0"},
    }


def test_tools_list_pagination(make_client, tmp_path):
    """nextCursor 自动翻页：首轮不带 cursor，后续携带，各页合并为完整列表。"""
    transcript = tmp_path / "t.jsonl"
    client = make_client("paged", transcript)
    tools = client.list_tools(timeout=10)
    assert [t["name"] for t in tools] == ["tool0", "tool1", "tool2"]

    requests = [m for m in _read_transcript(transcript) if m.get("method") == "tools/list"]
    assert [r["params"] for r in requests] == [{}, {"cursor": "1"}, {"cursor": "2"}]


def test_page_limit_silent_truncation(make_client):
    """兼容怪癖⑥：持续返回 nextCursor 时只收集前 50 页（静默截断、无报错）。"""
    client = make_client("endless")
    tools = client.list_tools(timeout=10)
    assert len(tools) == 50
    assert tools[0]["name"] == "tool0" and tools[-1]["name"] == "tool49"


def test_call_text_content(make_client):
    """文本内容：单个 text 块即结果原文（arguments 经服务端回显）。"""
    client = make_client()
    client.initialize(timeout=10)
    text, is_error = client.call_tool("echo", {"x": "1"}, 10)
    assert text == '{"x": "1"}'
    assert is_error is False


def test_call_image_and_resource(make_client):
    """图片与资源块：`[图片: image/png]` 与 `[资源: file:///x]` 以换行连接。"""
    client = make_client()
    client.initialize(timeout=10)
    text, is_error = client.call_tool("rich", {}, 10)
    assert text == "[图片: image/png]\n[资源: file:///x]"
    assert is_error is False


def test_call_is_error_flag(make_client):
    """错误标志：isError=True 由本层回传标志（错误行由工具层生成）。"""
    client = make_client()
    client.initialize(timeout=10)
    text, is_error = client.call_tool("err", {}, 10)
    assert (text, is_error) == ("", True)


def test_call_server_error(make_client):
    """服务端错误响应：`{method} 失败: {错误消息}` 形态的 McpError。"""
    client = make_client()
    client.initialize(timeout=10)
    with pytest.raises(McpError) as excinfo:
        client.call_tool("boom", {}, 10)
    assert str(excinfo.value) == "tools/call 失败: boom"


def test_call_timeout(make_client):
    """调用超时：`tools/call 超时({超时值}s)` 形态（超时值为整数时显示整数秒）。"""
    client = make_client()
    client.initialize(timeout=10)
    with pytest.raises(McpError) as excinfo:
        client.call_tool("slow", {}, 0.2)
    assert str(excinfo.value) == "tools/call 超时(0.2s)"


def test_server_request_rejected(make_client, tmp_path):
    """服务端主动请求：回 -32601 错误响应并记日志，连接继续可用。"""
    transcript = tmp_path / "t.jsonl"
    client = make_client("sampling", transcript)
    logger = client._logger
    client.initialize(timeout=10)

    replies = [m["client_reply"] for m in _read_transcript(transcript) if "client_reply" in m]
    assert replies and replies[0]["error"] == {"code": -32601, "message": "Method not found"}
    assert any("已回绝" in msg for _, msg in logger.records)
    assert client.list_tools(timeout=10)  # 连接保持可用


def test_start_failure():
    """启动失败：命令不存在 → `启动失败: …` 的 McpError。"""
    with pytest.raises(McpError) as excinfo:
        McpStdioClient("demo", "narnat-no-such-command-xyz")
    assert str(excinfo.value).startswith("启动失败: ")


def test_handshake_failure_closes_process(mock_server):
    """握手失败不残留进程：initialize 阶段失败 → 已启动的子进程被关闭、错误上抛。"""
    catalog, _registry, _table = _make_catalog()
    created: list[McpStdioClient] = []

    def factory(name, command, args=(), env=None, cwd=None, logger=None):
        client = McpStdioClient(name, command, args, logger=logger)
        created.append(client)
        return client

    manager = _make_manager(catalog, factory)
    try:
        with pytest.raises(McpError) as excinfo:
            manager.connect("demo", _server_config(mock_server, mode="die_on_initialize"))
        assert "进程已退出" in str(excinfo.value)
        assert created and created[0]._proc.poll() is not None   # 不残留进程
        assert created[0].alive is False
        assert manager.connected() == []
    finally:
        manager.cleanup()


def test_child_process_isolated_group(monkeypatch):
    """独立进程组：Windows 新进程组 + 独立无窗口控制台；类 Unix 新会话。"""
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        raise OSError("boom")

    monkeypatch.setattr(client_module.subprocess, "Popen", fake_popen)
    with pytest.raises(McpError) as excinfo:
        McpStdioClient("demo", "cmd", ["a"])
    assert "启动失败" in str(excinfo.value)
    if sys.platform == "win32":
        assert captured["creationflags"] == (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    else:
        assert captured["start_new_session"] is True


def test_close_graceful(make_client):
    """优雅关闭：关 stdin 后服务端自行退出（不发送强杀信号，退出码 0）。"""
    client = make_client()
    client.initialize(timeout=10)
    client.close(grace=10)
    assert client._proc.poll() == 0
    assert client.alive is False


def test_close_kills_stuck_server(make_client):
    """超时强杀：3 秒宽限内未退出则强杀（测试用短宽限验证同一路径）。"""
    client = make_client("ignore_eof")
    client.initialize(timeout=10)
    client.close(grace=0.3)
    assert client._proc.poll() is not None
    assert client._proc.returncode != 0


# ═══════════════════════════════════════════════════════════════
# 4. 连接管理器（真实子进程主链路 + 假客户端内部路径）
# ═══════════════════════════════════════════════════════════════


def test_connect_registers_tools(mock_server):
    """连接成功：登记清单、注册表可调用、定义进 LLM 工具表（下一轮可见）。"""
    catalog, registry, table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        count, names = manager.connect("demo", _server_config(mock_server))
        assert count == 6
        assert names == ["mcp__demo__echo", "mcp__demo__search", "mcp__demo__rich",
                         "mcp__demo__err", "mcp__demo__boom", "mcp__demo__slow"]
        assert manager.connected() == [{"name": "demo", "tool_count": 6}]
        assert registry.has_tool("mcp__demo__echo")
        assert [d["function"]["name"] for d in table.definitions] == names
    finally:
        manager.cleanup()


def test_next_round_definitions_visible(mock_server):
    """下一轮即可见：注册后注册表定义列表与 LLM 工具表都已包含新工具定义。"""
    catalog, registry, table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        registry_names = [d["function"]["name"] for d in registry.get_tool_definitions()]
        assert "mcp__demo__search" in registry_names
        assert {d["function"]["name"] for d in table.definitions} >= {"mcp__demo__search"}
        definition = next(d for d in registry.get_tool_definitions()
                          if d["function"]["name"] == "mcp__demo__search")
        assert definition["function"]["description"] == "搜索"
        assert definition["function"]["parameters"] == {"type": "object", "properties": {}}
    finally:
        manager.cleanup()


def test_tools_call_uses_raw_name():
    """调用转发使用服务端原始工具名（LLM 可见名只作注册名）。"""
    catalog, registry, _table = _make_catalog()
    clients: list[FakeClient] = []

    def factory(name, command, args=(), env=None, cwd=None, logger=None):
        client = FakeClient(name, command, args)
        client.tools = [{"name": "a.b", "description": "点号工具"}]
        clients.append(client)
        return client

    manager = _make_manager(catalog, factory)
    count, names = manager.connect("my server", {"command": "x", "tool_timeout_sec": 42})
    assert (count, names) == (1, ["mcp__my_server__a_b"])
    result = registry.execute("mcp__my_server__a_b", {"k": 1}, None)
    assert clients[0].calls == [("a.b", {"k": 1}, 42)]      # 超时取该连接配置
    assert result.llm_text == "ok"


def test_enabled_tools_filter(mock_server):
    """白黑名单过滤（按服务端原始工具名，注册前过滤）。"""
    catalog, registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        count, names = manager.connect("demo", _server_config(mock_server, enabled_tools=["echo"]))
        assert (count, names) == (1, ["mcp__demo__echo"])
    finally:
        manager.cleanup()
    catalog, registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        count, _names = manager.connect("demo", _server_config(mock_server, disabled_tools=["echo", "search"]))
        assert count == 4
        assert not registry.has_tool("mcp__demo__echo")
        assert not registry.has_tool("mcp__demo__search")
    finally:
        manager.cleanup()


def test_disconnect_unregisters_tools(mock_server):
    """注销工具：从可调用清单与 LLM 工具表移除；随后调用返回未知工具错误行。"""
    catalog, registry, table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        client = manager._clients["demo"]
        removed = manager.disconnect("demo")
        assert removed == 6
        assert manager.connected() == []
        assert client._proc.poll() is not None          # 进程已终止
        assert not registry.has_tool("mcp__demo__search")
        assert table.definitions == []
        result = registry.execute("mcp__demo__search", {}, None)
        assert result.llm_text == "[错误: 未知工具: mcp__demo__search]"
        assert result.is_error is True
    finally:
        manager.cleanup()


def test_disconnect_twice_reports_not_connected(mock_server):
    """重复断开：对已断开服务器再次断开 → 未连接报错（无进程或注册残留）。"""
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        manager.disconnect("demo")
        with pytest.raises(McpError) as excinfo:
            manager.disconnect("demo")
        assert str(excinfo.value) == "服务器 demo 未连接"
        assert manager.connected() == []
    finally:
        manager.cleanup()


def test_disconnect_all_batch_stops_on_error():
    """兼容怪癖②：断开全部为逐个断开、批内不捕获单点错误（中断后放弃后续）。"""
    catalog, _registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    manager.connect("s1", {"command": "x"})
    manager.connect("s2", {"command": "x"})
    manager.connect("s3", {"command": "x"})
    manager._clients["s2"] = None          # 模拟并发已被断开（客户端已空）
    with pytest.raises(McpError):
        manager.disconnect("all")
    assert "s1" not in manager._clients    # 已断开的生效
    assert "s3" in manager._clients        # 错误中断整批，后续跳过


def test_reconnect_after_process_death(mock_server):
    """断线后重连并要求重试：自动重连成功、不代 AI 重跑；再次调用成功。"""
    catalog, registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        client = manager._clients["demo"]
        client._proc.kill()
        client._proc.wait()
        with pytest.raises(McpError) as excinfo:
            manager.call_tool("demo", "echo", {"x": 1}, 10)
        assert str(excinfo.value) == "连接曾中断，已自动重连，请重试本次调用"
        assert manager._clients["demo"] is not client
        # 再次调用成功（工具注册不变）
        result = registry.execute("mcp__demo__echo", {"y": 2}, None)
        assert result.llm_text == '{"y": 2}'
    finally:
        manager.cleanup()


def test_reconnect_does_not_replay_call(mock_server, tmp_path):
    """不自动重跑：重连只重建通道（不重发 tools/call），防写操作/仿真被静默执行两次。"""
    transcript = tmp_path / "t.jsonl"
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server, transcript=transcript))
        manager.call_tool("demo", "echo", {"n": 1}, 10)
        calls_before = sum(1 for m in _read_transcript(transcript)
                           if m.get("method") == "tools/call")
        client = manager._clients["demo"]
        client._proc.kill()
        client._proc.wait()
        with pytest.raises(McpError):
            manager.call_tool("demo", "echo", {"n": 1}, 10)
        calls_after = sum(1 for m in _read_transcript(transcript)
                          if m.get("method") == "tools/call")
        assert calls_after == calls_before
    finally:
        manager.cleanup()


def test_server_error_does_not_reconnect(mock_server):
    """服务端自身错误（进程仍存活）→ 原样上报（`tools/call 失败: …`），不触发重连。"""
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        client = manager._clients["demo"]
        with pytest.raises(McpError) as excinfo:
            manager.call_tool("demo", "boom", {}, 10)
        assert str(excinfo.value) == "tools/call 失败: boom"
        assert manager._clients["demo"] is client
        assert client.alive is True
    finally:
        manager.cleanup()


def test_reconnect_discarded_when_disconnected(mock_server):
    """重连期间已被断开：丢弃新建通道，报「已断开，无需重连」。"""
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog)
    try:
        manager.connect("demo", _server_config(mock_server))
        spawned: list = []
        original_spawn = manager._spawn

        def spawn_then_disconnect(cfg):
            manager.disconnect("demo")          # 重连推进期间被断开
            client, tools = original_spawn(cfg)
            spawned.append(client)
            return client, tools

        manager._spawn = spawn_then_disconnect
        with pytest.raises(McpError) as excinfo:
            manager._reconnect("demo")
        assert str(excinfo.value) == "服务器 demo 已断开，无需重连"
        assert spawned and spawned[0]._proc.poll() is not None   # 新建通道被丢弃关闭
        assert "demo" not in manager._clients
    finally:
        manager.cleanup()


def test_reconnect_without_spec():
    """无记录配置 → 「无配置，无法自动重连」。"""
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog, _fake_factory([]))
    with pytest.raises(McpError) as excinfo:
        manager._reconnect("ghost")
    assert str(excinfo.value) == "服务器 ghost 无配置，无法自动重连"


def test_reconnect_incomplete_config():
    """配置不完整（无启动命令）→ 「配置不完整，无法自动重连」。"""
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog, _fake_factory([]))
    manager._specs["demo"] = {"args": []}
    with pytest.raises(McpError) as excinfo:
        manager._reconnect("demo")
    assert str(excinfo.value) == "服务器 demo 配置不完整，无法自动重连"


def test_forced_enabled_config():
    """兼容怪癖①：connect 强制按「启用」解析（传 `启用: False` 也连接）。"""
    catalog, _registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    count, names = manager.connect("demo", {"command": "x", "启用": False})
    assert count == 1 and names == ["mcp__demo__echo"]
    assert len(clients) == 1


def test_registration_count_quirk():
    """兼容怪癖③：注册被跳过（内置占名）时，登记清单/计数仍全量、断开按登记清单注销。"""
    catalog = RejectingCatalog(rejected="mcp__demo__search")
    clients: list[FakeClient] = []

    def factory(name, command, args=(), env=None, cwd=None, logger=None):
        client = FakeClient(name, command, args)
        client.tools = [{"name": "search"}, {"name": "echo"}]
        clients.append(client)
        return client

    manager = _make_manager(catalog, factory)
    count, names = manager.connect("demo", {"command": "x"})
    assert count == 2                                   # 登记全量（大于实际注册数）
    assert names == ["mcp__demo__search", "mcp__demo__echo"]
    assert manager.connected() == [{"name": "demo", "tool_count": 2}]
    assert catalog.registered == ["mcp__demo__echo"]    # 注册被跳过的不在其中
    assert catalog.definitions == 2                     # LLM 工具表收全量定义
    assert manager.disconnect("demo") == 2              # 按登记清单注销
    assert catalog.unregistered == ["mcp__demo__search", "mcp__demo__echo"]
    assert clients[0].closed is True


def test_builtin_name_collision_gets_hash_suffix(mock_server):
    """内置工具占了同名（清洗后）时，MCP 工具改名（哈希后缀）而非抢占（现状保持）。"""
    registry = ToolRegistry()
    registry.register(StubTool("mcp__demo__search"))    # 内置先占该名
    catalog, _registry, _table = _make_catalog(registry)
    manager = _make_manager(catalog)
    try:
        count, names = manager.connect("demo", _server_config(mock_server))
        assert count == 6
        assert "mcp__demo__search" not in names
        assert any(name.startswith("mcp__demo__search_") for name in names)
        # 该名仍由内置工具占位（内置不受影响）
        assert registry.execute("mcp__demo__search", {}, None).llm_text == "内置结果"
    finally:
        manager.cleanup()


def test_idempotent_reuse():
    """幂等复用：同名已连接且存活时直接返回既有清单，不新建进程、不重复注册。"""
    catalog, registry, table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    first = manager.connect("demo", {"command": "x"})
    table.definitions.clear()
    second = manager.connect("demo", {"command": "x"})
    assert first == second == (1, ["mcp__demo__echo"])
    assert len(clients) == 1
    assert table.definitions == []            # 未重复热追加定义


def test_dead_client_rebuild():
    """死连接重建：同名旧进程已死 → 先注销旧工具并关闭旧进程，再建新连接。"""
    catalog, registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    manager.connect("demo", {"command": "x"})
    clients[0].alive = False
    count, names = manager.connect("demo", {"command": "x"})
    assert (count, names) == (1, ["mcp__demo__echo"])
    assert clients[0].closed is True
    assert len(clients) == 2 and manager._clients["demo"] is clients[1]
    assert registry.has_tool("mcp__demo__echo")


def test_server_limit():
    """并发上限：已有 8 个连接时连接第 9 个 → 固定文案错误。"""
    catalog, _registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    for index in range(MAX_SERVERS):
        manager.connect(f"s{index}", {"command": "x"})
    with pytest.raises(McpError) as excinfo:
        manager.connect("s8", {"command": "x"})
    assert str(excinfo.value) == (
        "已达并发上限(8个)，当前已连接: s0、s1、s2、s3、s4、s5、s6、s7。"
        "请先 disconnect 不需要的服务器")
    assert len(clients) == MAX_SERVERS


def test_concurrent_same_name_loser_discards_new_client():
    """兼容怪癖④：并发同名 connect 的落败方丢弃本次新建的进程并返回既有清单。"""
    catalog, _registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    winner = FakeClient("demo", "x")
    original_spawn = manager._spawn

    def spawn_with_race(cfg):
        client, tools = original_spawn(cfg)
        manager._clients["demo"] = winner                     # 模拟并发赢家先注册
        manager._server_tools["demo"] = ["mcp__demo__winner"]
        return client, tools

    manager._spawn = spawn_with_race
    count, names = manager.connect("demo", {"command": "x"})
    assert (count, names) == (1, ["mcp__demo__winner"])
    assert clients[0].closed is True                          # 本次新建被丢弃


def test_atexit_fallback_registered(monkeypatch):
    """退出兜底回收：首次成功连接后注册解释器退出钩子；后续连接不重复注册。"""
    registered: list = []
    monkeypatch.setattr(manager_module.atexit, "register",
                        lambda func: registered.append(func))
    catalog, _registry, _table = _make_catalog()
    manager = _make_manager(catalog, _fake_factory([]))
    manager.connect("s1", {"command": "x"})
    manager.connect("s2", {"command": "x"})
    assert len(registered) == 1
    assert registered[0] == manager.cleanup
    assert manager._atexit_registered is True


def test_cleanup_idempotent_and_keeps_registry():
    """清理：关闭全部连接并清空登记；兼容怪癖⑤不注销工具；重复调用无副作用。"""
    catalog, registry, _table = _make_catalog()
    clients: list[FakeClient] = []
    manager = _make_manager(catalog, _fake_factory(clients))
    manager.connect("s1", {"command": "x"})
    manager.connect("s2", {"command": "x"})
    manager.cleanup()
    assert all(client.closed for client in clients)
    assert manager.connected() == []
    assert registry.has_tool("mcp__s1__echo")                 # 工具注册残留（怪癖⑤）
    manager.cleanup()                                          # 幂等
    with pytest.raises(McpError) as excinfo:
        manager.connect("s3", {"command": "x"})
    assert str(excinfo.value) == "程序正在退出，无法连接"


# ═══════════════════════════════════════════════════════════════
# 5. MCP 工具（假端口：参数契约 / 文案 / 失败不阻塞）
# ═══════════════════════════════════════════════════════════════


def test_action_defaults_to_connect():
    """默认 action：不传 / 空串 / 纯空白 / 大小写混排归一扫白后按 connect 处理。"""
    tool = McpTool(signal.error_line)
    port = FakePort()
    expected = "[错误: connect 需要 name（服务器名）]"
    for args in ({}, {"action": ""}, {"action": "  "}, {"action": " Connect "}):
        result = tool.execute(args, ToolEnvImpl(mcp=port))
        assert _plain(result.llm_text) == expected, args
    assert port.calls == []


def test_unknown_action_error():
    """未知 action：`未知 action: {action}（可用: connect/disconnect）` 形态错误行。"""
    port = FakePort()
    result = McpTool(signal.error_line).execute({"action": "foo"}, ToolEnvImpl(mcp=port))
    assert signal.has_error(result.llm_text)                  # 带框架标签（不可伪造）
    assert _plain(result.llm_text) == "[错误: 未知 action: foo（可用: connect/disconnect）]"
    assert port.calls == []


def test_missing_manager_error():
    """管理器缺失：上下文缺失或 mcp 未初始化 → `MCP 管理器未初始化` 形态错误行。"""
    tool = McpTool(signal.error_line)
    assert _plain(tool.execute({}, None).llm_text) == "[错误: MCP 管理器未初始化]"
    assert _plain(tool.execute({}, ToolEnvImpl()).llm_text) == "[错误: MCP 管理器未初始化]"


def test_connect_requires_name():
    """缺 name：空白串 → `connect 需要 name（服务器名）` 形态错误行。"""
    port = FakePort()
    result = McpTool(signal.error_line).execute(
        {"name": "   ", "config": {"command": "x"}}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == "[错误: connect 需要 name（服务器名）]"
    assert port.calls == []


def test_connect_requires_config():
    """缺 config：未传或空对象 → `connect 需要 config（启动配置：command/args/env/cwd）`。"""
    tool = McpTool(signal.error_line)
    port = FakePort()
    assert _plain(tool.execute({"name": "demo"}, ToolEnvImpl(mcp=port)).llm_text) == \
        "[错误: connect 需要 config（启动配置：command/args/env/cwd）]"
    assert _plain(tool.execute({"name": "demo", "config": {}},
                               ToolEnvImpl(mcp=port)).llm_text) == \
        "[错误: connect 需要 config（启动配置：command/args/env/cwd）]"
    assert port.calls == []


def test_connect_requires_command():
    """缺启动命令：→ `config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器`。"""
    port = FakePort()
    result = McpTool(signal.error_line).execute(
        {"name": "demo", "config": {"args": ["x"]}}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == \
        "[错误: config 缺少 command（启动命令）；仅支持 stdio 型 MCP 服务器]"
    assert port.calls == []


def test_connect_success_show_and_limit():
    """成功展示：首行报告注册数；超 20 个只展示前 20 个并追加 `  …共{N}个`。"""
    tool = McpTool(signal.error_line)
    port = FakePort()
    port.connect_result = (2, ["mcp__demo__a", "mcp__demo__b"])
    text = tool.execute({"name": "demo", "config": {"command": "x"}},
                        ToolEnvImpl(mcp=port)).llm_text
    assert text == ("[已连接 MCP服务器 demo：注册 2 个工具，后续轮次可直接调用]\n"
                    "  mcp__demo__a\n  mcp__demo__b")
    assert port.calls[-1] == ("connect", "demo", {"command": "x"})

    names = [f"mcp__demo__t{i}" for i in range(25)]
    port.connect_result = (25, names)
    text = tool.execute({"name": "demo", "config": {"command": "x"}},
                        ToolEnvImpl(mcp=port)).llm_text
    lines = text.splitlines()
    assert lines[0] == "[已连接 MCP服务器 demo：注册 25 个工具，后续轮次可直接调用]"
    assert lines[1:21] == [f"  {name}" for name in names[:20]]
    assert lines[-1] == "  …共25个"
    assert len(lines) == 22


def test_disconnect_all_text():
    """断开全部：`all` → 返回累计注销数。"""
    port = FakePort()
    result = McpTool(signal.error_line).execute(
        {"action": "disconnect", "name": "all"}, ToolEnvImpl(mcp=port))
    assert result.llm_text == "[已断开全部 MCP服务器，注销 4 个工具]"
    assert port.calls == [("disconnect", "all")]


def test_disconnect_unknown_server():
    """未连接：`服务器 {name} 未连接。当前已连接: {清单或(无)}` 形态错误行。"""
    tool = McpTool(signal.error_line)
    port = FakePort()
    port.servers = [{"name": "A", "tool_count": 1}, {"name": "B", "tool_count": 2}]
    result = tool.execute({"action": "disconnect", "name": "C"}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == "[错误: 服务器 C 未连接。当前已连接: A、B]"

    port.servers = []
    result = tool.execute({"action": "disconnect", "name": "C"}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == "[错误: 服务器 C 未连接。当前已连接: (无)]"


def test_disconnect_case_insensitive():
    """大小写不敏感匹配：服务器以 `Demo` 连接，disconnect 传 `demo` → 断开并报原名。"""
    port = FakePort()
    port.servers = [{"name": "Demo", "tool_count": 3}]
    result = McpTool(signal.error_line).execute(
        {"action": "disconnect", "name": "demo"}, ToolEnvImpl(mcp=port))
    assert result.llm_text == "[已断开 MCP服务器 Demo，注销 4 个工具]"
    assert port.calls == [("disconnect", "Demo")]


def test_disconnect_requires_name():
    """缺 name（spec 参数面补充）：空白串 → `disconnect 需要 name…` 形态错误行。"""
    port = FakePort()
    result = McpTool(signal.error_line).execute(
        {"action": "disconnect", "name": "  "}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == \
        "[错误: disconnect 需要 name（已连接的服务器名，或 all 断开全部）]"
    assert port.calls == []


def test_connect_failure_does_not_block():
    """连接失败不阻塞：协议类错误 → `MCP connect 失败: {错误文本}` 形态文本。"""
    port = FakePort()
    port.error = McpError("启动失败: xyz")
    result = McpTool(signal.error_line).execute(
        {"name": "demo", "config": {"command": "x"}}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == "[错误: MCP connect 失败: 启动失败: xyz]"


def test_unexpected_exception_wrapped():
    """意外异常包装：非协议类异常 → `MCP {action} 异常: {异常文本}` 形态文本。"""
    port = FakePort()
    port.error = ValueError("weird")
    result = McpTool(signal.error_line).execute(
        {"name": "demo", "config": {"command": "x"}}, ToolEnvImpl(mcp=port))
    assert _plain(result.llm_text) == "[错误: MCP connect 异常: weird]"


def test_tool_call_failure_does_not_break_session():
    """工具调用失败可继续：连接类错误转 `MCP工具 {服务器}/{工具} 调用失败: …` 错误行。"""
    caller = FakeCaller()
    caller.error = McpError("连接曾中断，已自动重连，请重试本次调用")
    failed = _dynamic_tool(caller).execute({}, None)
    assert signal.has_error(failed.llm_text)
    assert _plain(failed.llm_text) == (
        "[错误: MCP工具 demo/tool 调用失败: 连接曾中断，已自动重连，请重试本次调用]")

    caller.error = None                                        # 其余调用不受影响
    assert _dynamic_tool(caller).execute({}, None).llm_text == "ok"


def test_dynamic_tool_forwards_and_formats():
    """动态工具对象：转发原始名/参数/超时；文本透传、空输出兜底、isError 转错误行。"""
    caller = FakeCaller()
    tool = _dynamic_tool(caller, llm_name="mcp__demo__a_b", raw_name="a.b", timeout=7)
    assert tool.name == "mcp__demo__a_b"
    assert tool.definition()["function"]["name"] == "mcp__demo__a_b"

    caller.result = ("结果文本", False)
    assert tool.execute({"q": 1}, None).llm_text == "结果文本"
    assert caller.calls == [("demo", "a.b", {"q": 1}, 7)]

    caller.result = ("", False)
    assert tool.execute({}, None).llm_text == "(工具无输出)"

    caller.result = ("", True)
    assert _plain(tool.execute({}, None).llm_text) == "[错误: 工具返回错误]"
    caller.result = ("业务错误", True)
    assert _plain(tool.execute({}, None).llm_text) == "[错误: 业务错误]"


def test_mcp_tool_definition_contract():
    """参数契约（spec Requirement 1）：工具名、描述语义、参数面与必填集。"""
    assert McpTool(signal.error_line).name == "MCP"
    definition = McpTool(signal.error_line).definition()
    assert definition["type"] == "function"
    function = definition["function"]
    assert function["name"] == "MCP"
    assert function["description"] == "MCP 服务器连接通道（仅支持本地 stdio 型服务器）。"
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["properties"]) == {"action", "name", "config"}
    assert parameters["required"] == []
    assert "默认connect" in parameters["properties"]["action"]["description"]
    assert "mcp__<服务器名>__<工具名>" in parameters["properties"]["action"]["description"]
    assert "all（断开全部）" in parameters["properties"]["name"]["description"]
    config_desc = parameters["properties"]["config"]["description"]
    for keyword in ("command", "args", "env", "cwd", "startup_timeout_sec",
                    "tool_timeout_sec", "enabled_tools/disabled_tools"):
        assert keyword in config_desc
    assert MCP_DEFINITION is definition


# ═══════════════════════════════════════════════════════════════
# 6. 结构：包导出、依赖纯净、spec 映射表齐备
# ═══════════════════════════════════════════════════════════════


def test_package_exports():
    """包导出可解析：`__all__` 全部可访问，关键绑定与子模块路径正确。"""
    import narnat_agent.mcp as mcp_package
    from narnat_agent.mcp import client, manager, tool

    for name in mcp_package.__all__:
        assert hasattr(mcp_package, name), f"__all__ 中的 {name!r} 无法从包解析"
    assert mcp_package.TOOL_PREFIX == "mcp__"
    assert mcp_package.MAX_SERVERS == 8
    assert client.__name__ == "narnat_agent.mcp.client"
    assert manager.__name__ == "narnat_agent.mcp.manager"
    assert tool.__name__ == "narnat_agent.mcp.tool"
    assert mcp_package.McpManager is manager.McpManager
    assert mcp_package.McpTool is tool.McpTool


def test_mcp_dependencies_are_contracts_and_config_only():
    """依赖纯净（design D8）：mcp 不 import tools/llm 等同层积木，只经 contracts/config。"""
    sources = sorted(MCP_DIR.glob("*.py"))
    assert sources, "mcp 目录不存在或为空"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("narnat_agent"), (
                        f"{path.name}: 禁止绝对导入新包（{alias.name}）")
            elif isinstance(node, ast.ImportFrom) and node.level:
                if node.level >= 2:
                    assert (node.module or "").startswith(("contracts", "config")), (
                        f"{path.name}: 跨积木导入越层（{'.' * node.level}{node.module}）")


def test_spec_scenario_map_complete():
    """映射表齐备：11 Requirement / 45 场景全部指向存在的测试，且无未归类测试。"""
    module = sys.modules[__name__]
    total = sum(len(scenarios) for scenarios in SPEC_SCENARIO_MAP.values())
    assert len(SPEC_SCENARIO_MAP) == 11
    assert total == 45
    mapped = set()
    for requirement, scenarios in SPEC_SCENARIO_MAP.items():
        for scenario, test_name in scenarios:
            assert callable(getattr(module, test_name, None)), (
                f"{requirement} / {scenario} → {test_name} 未定义")
            mapped.add(test_name)
    defined = {name for name in dir(module) if name.startswith("test_")}
    assert defined == mapped | EXTRA_TESTS, (
        f"未归类测试: {sorted(defined - mapped - EXTRA_TESTS)}；"
        f"多余映射: {sorted((mapped | EXTRA_TESTS) - defined)}")
