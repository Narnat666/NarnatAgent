"""stats 积木自测 —— 逐条对齐 `specs/stats/spec.md`（8 Requirement / 23 Scenario）。

Scenario 映射表：
| # | Requirement | Scenario | 测试函数 |
|---|---|---|---|
| 1 | 用量累计口径 | 多轮累计 | test_scenario_multi_round_accumulation |
| 2 | 用量累计口径 | 缓存命中率加权 | test_scenario_cache_hit_ratio_weighted |
| 3 | 用量累计口径 | 无数据时命中率 | test_scenario_cache_hit_ratio_without_data |
| 4 | 费用计算（定价表） | 按定价计算 | test_scenario_cost_by_pricing |
| 5 | 费用计算（定价表） | 未配置定价 | test_scenario_cost_unpriced_model_zero |
| 6 | 成本日志写入 | 首次写入含表头 | test_scenario_cost_log_first_write_header |
| 7 | 成本日志写入 | 未命中列口径 | test_scenario_cost_log_uncached_column |
| 8 | 成本日志写入 | 写失败补写 | test_scenario_cost_log_requeue_after_failure |
| 9 | 成本日志写入 | 未启用不写 | test_scenario_cost_log_disabled_no_write |
| 10 | 成本日志容量与轮转 | 达到上限触发轮转 | test_scenario_rotate_at_limit |
| 11 | 成本日志容量与轮转 | 上限为 0 不轮转 | test_scenario_no_rotate_when_limit_zero |
| 12 | 成本日志容量与轮转 | 轮转失败降级 | test_scenario_rotate_failure_degrade |
| 13 | 余额查询 | bearer 认证取值 | test_scenario_balance_bearer |
| 14 | 余额查询 | x-api-key 认证 | test_scenario_balance_x_api_key |
| 15 | 余额查询 | 失败静默 | test_scenario_balance_silent_failures |
| 16 | 余额查询 | 未启用不查询 | test_scenario_balance_disabled_or_empty_url_no_request |
| 17 | 余额查询节奏 | 整除轮查询 | test_scenario_balance_round_multiple |
| 18 | 余额查询节奏 | 非整除轮清零 | test_scenario_balance_non_multiple_resets |
| 19 | 余额查询节奏 | 查询失败保持上次值 | test_scenario_balance_failure_keeps_last |
| 20 | 统计栏数据供给 | 统计栏读数 | test_scenario_snapshot_readings |
| 21 | 兼容性怪癖保持 | 余额 0.0 双关 | test_quirk_balance_zero_ambiguous |
| 22 | 兼容性怪癖保持 | 负数未命中 | test_quirk_negative_uncached |
| 23 | 兼容性怪癖保持 | 定价缺键抛错 | test_quirk_pricing_missing_key_propagates |

另有：billing 基准矩阵对照（`v2/tests/baseline/data/billing.json` 全部分组）、
日志格式细节（BOM/时间/9 位小数/表头只写一次/默认路径）、配置适配
（`CostLog.from_config`）、缺用量键抛错、模型切换语义。
"""
from __future__ import annotations

import copy
import csv
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from narnat_agent.stats import (
    COST_LOG_HEADER,
    CostLog,
    StatsReadings,
    StatsTracker,
    calculate_cost,
    cost_breakdown,
    fetch_balance,
    get_pricing,
    resolve_jsonpath,
)
from narnat_agent.stats import billing as billing_mod
from narnat_agent.stats import tracker as tracker_mod

BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline" / "data"

# 定价表（对齐基准用例）
PRICING = {
    "m-basic": {"input": 1000000.0, "cache_hit": 100000.0, "output": 2000000.0},
    "m-zero": {"input": 0.0, "cache_hit": 0.0, "output": 0.0},
}

# 表头逐字契约（11 列）
EXPECTED_HEADER = [
    "时间", "模型",
    "输入tokens", "缓存命中tokens", "缓存未命中tokens",
    "输出tokens(含思考)",
    "输入费用(元)", "缓存费用(元)", "输出费用(元)",
    "本次费用(元)", "累计费用(元)",
]


# ═══════════════════════════════════════════════════════════════
# 工具与替身
# ═══════════════════════════════════════════════════════════════


def _make_log(
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_bytes: int = 0,
    name: str = "cost_log.csv",
) -> tuple[CostLog, Path]:
    path = tmp_path / name
    return CostLog(enabled=enabled, path=str(path), max_bytes=max_bytes), path


def _append(
    log: CostLog,
    *,
    model: str = "m-basic",
    prompt: int = 1000,
    completion: int = 10,
    cached: int = 300,
    breakdown: tuple[float, float, float] = (1.0, 2.0, 3.0),
    total_cost: float = 6.0,
) -> None:
    log.append(model, prompt, completion, cached, breakdown, total_cost)


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _balance_cfg(**overrides):
    cfg = {
        "enabled": True,
        "url": "https://api.example.com/user/balance",
        "auth_method": "bearer",
        "value_path": "balance_infos.0.total_balance",
        "currency_path": "balance_infos.0.currency",
    }
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, json_error: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("JSON 解析失败")
        return self._payload


class _FakeClient:
    def __init__(self, owner: "_FakeHttpx"):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, headers=None):
        self._owner.calls.append({"url": url, "headers": headers})
        return self._owner.response


class _FakeHttpx:
    """httpx 替身：记录请求并以预定响应作答（不联网）。"""

    def __init__(self, response=None):
        self.response = response if response is not None else _FakeResponse(payload={})
        self.calls: list[dict] = []
        self.timeouts: list[float] = []

    def Client(self, timeout=None):
        self.timeouts.append(timeout)
        return _FakeClient(self)


@pytest.fixture
def fake_httpx(monkeypatch):
    """返回 install(response=None) → fake；装入 billing 模块（monkeypatch 自动恢复）。"""

    def install(response=None) -> _FakeHttpx:
        fake = _FakeHttpx(response)
        monkeypatch.setattr(billing_mod, "httpx", fake)
        return fake

    return install


@pytest.fixture
def balance_probe(monkeypatch):
    """打桩统计层的余额查询网络调用（记录调用、可控结果）。"""
    calls: list[tuple] = []
    result = {"value": {"total": 88.0}}

    def fake(api_key, balance_cfg):
        calls.append((api_key, balance_cfg))
        return result["value"]

    monkeypatch.setattr(tracker_mod, "fetch_balance", fake)
    return SimpleNamespace(calls=calls, result=result)


# ═══════════════════════════════════════════════════════════════
# 1. 用量累计口径
# ═══════════════════════════════════════════════════════════════


def test_scenario_multi_round_accumulation():
    """Scenario: 多轮累计 —— 输入读数为最近一轮（500），输出为累计（30）。"""
    stats = StatsTracker("m-basic", PRICING)
    stats.update({"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 0})
    stats.update({"prompt_tokens": 500, "completion_tokens": 20, "cached_tokens": 0})
    assert stats.input_tokens == 500
    assert stats.output_tokens == 30


def test_scenario_cache_hit_ratio_weighted():
    """Scenario: 缓存命中率加权 —— 150/600 = 0.25（按 token 加权，非简单平均）。"""
    stats = StatsTracker("m-basic", PRICING)
    stats.update({"prompt_tokens": 100, "completion_tokens": 0, "cached_tokens": 50})
    stats.update({"prompt_tokens": 500, "completion_tokens": 0, "cached_tokens": 100})
    assert stats.cache_hit_ratio == 0.25


def test_scenario_cache_hit_ratio_without_data():
    """Scenario: 无数据时命中率 —— 0.0。"""
    assert StatsTracker("m-basic", PRICING).cache_hit_ratio == 0.0


def test_cached_tokens_key_defaults_to_zero():
    """缓存命中键缺失按 0 计（输入/输出键缺失则抛错，见怪癖测试）。"""
    stats = StatsTracker("m-basic", PRICING)
    stats.update({"prompt_tokens": 100, "completion_tokens": 10})
    assert stats.cache_hit_ratio == 0.0
    assert stats.cost == 120.0  # 100×1 + 10×2（m-basic 定价）


# ═══════════════════════════════════════════════════════════════
# 2. 费用计算（定价表）
# ═══════════════════════════════════════════════════════════════


def test_scenario_cost_by_pricing():
    """Scenario: 按定价计算 —— (60000×2 + 40000×0.5 + 20000×8)/1e6 = 0.3。"""
    pricing = {"m": {"input": 2.0, "cache_hit": 0.5, "output": 8.0}}
    stats = StatsTracker("m", pricing)
    stats.update(
        {"prompt_tokens": 100000, "completion_tokens": 20000, "cached_tokens": 40000}
    )
    assert stats.cost == pytest.approx(0.3)


def test_scenario_cost_unpriced_model_zero():
    """Scenario: 未配置定价 —— 本轮与累计费用均不增加（按 0 计算）。"""
    stats = StatsTracker("m-missing", PRICING)
    stats.update(
        {"prompt_tokens": 1000000, "completion_tokens": 500000, "cached_tokens": 0}
    )
    assert stats.cost == 0.0


# ═══════════════════════════════════════════════════════════════
# 3. 成本日志写入
# ═══════════════════════════════════════════════════════════════


def test_cost_log_header_contract():
    """表头逐字契约（列序与文案与已发布版本一致）。"""
    assert EXPECTED_HEADER == COST_LOG_HEADER
    assert len(COST_LOG_HEADER) == 11


def test_scenario_cost_log_first_write_header(tmp_path):
    """Scenario: 首次写入含表头 —— 表头行 + 11 列数据行。"""
    log, path = _make_log(tmp_path)
    _append(log)
    rows = _read_rows(path)
    assert rows[0] == COST_LOG_HEADER
    assert len(rows) == 2
    assert len(rows[1]) == 11
    assert rows[1][1] == "m-basic"


def test_cost_log_encoding_time_and_decimals(tmp_path):
    """UTF-8-sig（含 BOM、追加不重复）、本地时间格式、费用 9 位小数。"""
    log, path = _make_log(tmp_path)
    log.append("m-basic", 1000, 10, 300, (0.1234567891, 0.0, 0.0), 0.1234567891)
    _append(log)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1
    rows = _read_rows(path)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rows[1][0])
    assert float(rows[1][6]) == round(0.1234567891, 9)
    assert float(rows[1][9]) == round(0.1234567891, 9)
    assert float(rows[1][10]) == round(0.1234567891, 9)
    assert len(rows) == 3  # 表头只写一次


def test_scenario_cost_log_uncached_column(tmp_path):
    """Scenario: 未命中列口径 —— 输入 1000、缓存命中 300 → 700。"""
    log, path = _make_log(tmp_path)
    log.append("m-basic", 1000, 10, 300, (0.0, 0.0, 0.0), 0.0)
    row = _read_rows(path)[1]
    assert row[2] == "1000"
    assert row[3] == "300"
    assert row[4] == "700"
    assert row[5] == "10"


def test_scenario_cost_log_requeue_after_failure(tmp_path):
    """Scenario: 写失败补写 —— 失败行入队列；下次写入先补写积压行再写当前行。"""
    path = tmp_path / "cost_log.csv"
    path.mkdir()  # 目录占位 → 打开写入抛 OSError（模拟文件被独占）
    log = CostLog(enabled=True, path=str(path), max_bytes=0)
    _append(log, prompt=1)
    _append(log, prompt=2)
    assert len(log._pending) == 2  # 不丢行；此后无请求则永久驻留内存
    path.rmdir()  # 障碍解除
    _append(log, prompt=3)
    assert len(log._pending) == 0
    rows = _read_rows(path)
    assert [int(r[2]) for r in rows[1:]] == [1, 2, 3]  # 顺序保持、不重复写
    assert rows[0] == COST_LOG_HEADER


def test_scenario_cost_log_disabled_no_write(tmp_path):
    """Scenario: 未启用不写 —— 不进行任何日志写入。"""
    log, path = _make_log(tmp_path, enabled=False)
    _append(log)
    assert not path.exists()
    assert log._pending == []


def test_cost_log_default_path(tmp_path, monkeypatch):
    """默认输出文件 `.narnat/data/cost_log.csv`（相对当前工作目录）。"""
    monkeypatch.chdir(tmp_path)
    log = CostLog(enabled=True)
    _append(log)
    assert (tmp_path / ".narnat" / "data" / "cost_log.csv").exists()


def test_cost_log_from_config(tmp_path):
    """配置适配：max_bytes 非法值按 0（不轮转）；None 配置 → 关闭。"""
    path = tmp_path / "c.csv"
    log = CostLog.from_config(SimpleNamespace(enabled=True, path=str(path), max_bytes="abc"))
    for i in range(6):
        log.append("m", i, 0, 0, (0.0, 0.0, 0.0), 0.0)
    assert not (tmp_path / "c_bak.csv").exists()
    assert len(_read_rows(path)) == 7

    off = CostLog.from_config(None)
    off.append("m", 0, 0, 0, (0.0, 0.0, 0.0), 0.0)
    assert list(tmp_path.glob("**/*.csv")) == [path]  # 未启用 → 未写任何文件


# ═══════════════════════════════════════════════════════════════
# 4. 成本日志容量与轮转
# ═══════════════════════════════════════════════════════════════


def test_scenario_rotate_at_limit(tmp_path):
    """Scenario: 达到上限触发轮转 —— 旧备份替换、当前改名 _bak、新文件先表头。"""
    log, path = _make_log(tmp_path, max_bytes=200)
    bak = path.with_name(path.stem + "_bak" + path.suffix)
    bak.write_text("旧备份\n", encoding="utf-8")  # 预置旧备份，验证被替换

    _append(log, prompt=0)
    written = 1
    i = 1
    while path.stat().st_size < 200:
        _append(log, prompt=i)
        written += 1
        i += 1
    _append(log, prompt=999)  # 触发轮转

    rows = _read_rows(path)
    assert rows[0] == COST_LOG_HEADER
    assert len(rows) == 2
    assert rows[1][2] == "999"
    backups = _read_rows(bak)
    assert backups[0] == COST_LOG_HEADER
    assert len(backups) == 1 + written  # 轮转前全部数据行都在备份里
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([path.name, bak.name])


def test_scenario_no_rotate_when_limit_zero(tmp_path):
    """Scenario: 上限为 0（或负值按 0 处理）—— 永不轮转，持续追加同一文件。"""
    for max_bytes in (0, -5):
        log, path = _make_log(
            tmp_path, max_bytes=max_bytes, name=f"cost_{max_bytes}.csv"
        )
        for i in range(5):
            _append(log, prompt=i)
        rows = _read_rows(path)
        assert len(rows) == 6
        bak = path.with_name(path.stem + "_bak" + path.suffix)
        assert not bak.exists()


def test_scenario_rotate_failure_degrade(tmp_path):
    """Scenario: 轮转失败降级 —— 不写表头、继续向原文件追加，该行不丢失。"""
    path = tmp_path / "cost_log.csv"
    path.write_text("legacy-row\n" + "x" * 300 + "\n", encoding="utf-8")
    bak = path.with_name(path.stem + "_bak" + path.suffix)
    bak.mkdir()  # 备份名被目录占用 → 轮转改名失败（OSError）
    log = CostLog(enabled=True, path=str(path), max_bytes=100)
    _append(log, prompt=42)

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "legacy-row"  # 原文件保持原位
    assert len(lines) == 3
    assert not lines[1].startswith("时间")  # 未写表头
    assert lines[2].split(",")[2] == "42"  # 该行未丢失
    assert bak.is_dir()  # 轮转未发生（备份名仍被目录占用）


# ═══════════════════════════════════════════════════════════════
# 5. 余额查询
# ═══════════════════════════════════════════════════════════════


PAYLOAD = {"balance_infos": [{"total_balance": "12.5", "currency": "CNY"}]}


def test_scenario_balance_bearer(fake_httpx):
    """Scenario: bearer 认证取值 —— 携带 Bearer 头，返回余额（货币随附）。"""
    fake = fake_httpx(_FakeResponse(payload=PAYLOAD))
    result = fetch_balance("sk-1", _balance_cfg())
    assert result == {"total": 12.5, "currency": "CNY"}
    assert fake.calls[0]["headers"] == {"Authorization": "Bearer sk-1"}
    assert fake.timeouts == [8.0]


def test_scenario_balance_x_api_key(fake_httpx):
    """Scenario: x-api-key 认证 —— 以 x-api-key 头携带密钥。"""
    fake = fake_httpx(_FakeResponse(payload=PAYLOAD))
    result = fetch_balance("sk-2", _balance_cfg(auth_method="x-api-key"))
    assert result == {"total": 12.5, "currency": "CNY"}
    assert fake.calls[0]["headers"] == {"x-api-key": "sk-2"}


def test_scenario_balance_silent_failures(fake_httpx, capsys):
    """Scenario: 失败静默 —— 非 200 / JSON 解析失败 / 路径取值失败，无错误输出。"""
    fake_httpx(_FakeResponse(status_code=500))
    assert fetch_balance("k", _balance_cfg()) is None

    fake_httpx(_FakeResponse(json_error=True))
    assert fetch_balance("k", _balance_cfg()) is None

    fake_httpx(_FakeResponse(payload={"other": 1}))
    assert fetch_balance("k", _balance_cfg()) is None

    fake_httpx(_FakeResponse(payload={"balance_infos": [{"total_balance": "abc"}]}))
    assert fetch_balance("k", _balance_cfg()) is None

    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_scenario_balance_disabled_or_empty_url_no_request(fake_httpx):
    """Scenario: 未启用不查询 —— 不发请求，结果为空。"""
    fake = fake_httpx()
    assert fetch_balance("k", _balance_cfg(enabled=False)) is None
    assert fetch_balance("k", _balance_cfg(url="")) is None
    assert fetch_balance("k", None) is None
    assert fake.calls == []
    assert fake.timeouts == []


def test_balance_currency_path_miss_and_dict_config(fake_httpx):
    """货币路径未命中时不附带 currency；配置以字典传入同样有效。"""
    fake = fake_httpx(_FakeResponse(payload={"balance_infos": [{"total_balance": "12.5"}]}))
    assert fetch_balance("k", _balance_cfg()) == {"total": 12.5}
    cfg = {
        "enabled": True,
        "url": "https://x/balance",
        "value_path": "balance_infos.0.total_balance",
    }
    assert fetch_balance("k", cfg) == {"total": 12.5}
    assert fake.calls[-1]["headers"] == {"Authorization": "Bearer k"}


def test_balance_network_error_silent(monkeypatch):
    """网络异常静默为「无结果」。"""

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url, headers=None):
            raise OSError("连接失败")

    class _BoomHttpx:
        def Client(self, timeout=None):
            return _BoomClient()

    monkeypatch.setattr(billing_mod, "httpx", _BoomHttpx())
    assert fetch_balance("k", _balance_cfg()) is None


# ═══════════════════════════════════════════════════════════════
# 6. 余额查询节奏
# ═══════════════════════════════════════════════════════════════


def test_scenario_balance_round_multiple(balance_probe):
    """Scenario: 整除轮查询 —— 轮次号为间隔整数倍且密钥非空时发起并刷新。"""
    stats = StatsTracker("m-basic", PRICING, balance_cfg=_balance_cfg())
    stats.fetch_balance("sk-1", 10)
    assert stats.balance == 88.0
    assert len(balance_probe.calls) == 1
    stats.fetch_balance("sk-1", 20)  # 下一个整除轮照常查询
    assert len(balance_probe.calls) == 2


def test_scenario_balance_non_multiple_resets(balance_probe):
    """Scenario: 非整除轮清零 —— 显示余额置 0.0（密钥为空同）。"""
    stats = StatsTracker("m-basic", PRICING, balance_cfg=_balance_cfg())
    stats.fetch_balance("sk-1", 10)
    assert stats.balance == 88.0
    stats.fetch_balance("sk-1", 7)  # 非整除轮
    assert stats.balance == 0.0
    stats.fetch_balance("sk-1", 10)
    stats.fetch_balance("", 20)  # 密钥为空
    assert stats.balance == 0.0
    assert len(balance_probe.calls) == 2


def test_scenario_balance_failure_keeps_last(balance_probe, capsys):
    """Scenario: 查询失败保持上次值 —— 网络或解析异常不改动显示余额。"""
    stats = StatsTracker("m-basic", PRICING, balance_cfg=_balance_cfg())
    stats.fetch_balance("sk-1", 10)
    balance_probe.result["value"] = None  # 查询失败
    stats.fetch_balance("sk-1", 20)
    assert stats.balance == 88.0
    out, err = capsys.readouterr()
    assert out == "" and err == ""


# ═══════════════════════════════════════════════════════════════
# 7. 统计栏数据供给
# ═══════════════════════════════════════════════════════════════


def test_scenario_snapshot_readings():
    """Scenario: 统计栏读数 —— 返回输入/输出/缓存命中率/费用/余额五项。"""
    stats = StatsTracker("m-basic", PRICING)
    stats.update({"prompt_tokens": 500, "completion_tokens": 20, "cached_tokens": 100})
    snap = stats.snapshot()
    assert isinstance(snap, StatsReadings)
    assert snap.input_tokens == 500
    assert snap.output_tokens == 20
    assert snap.cache_hit_ratio == 0.2
    assert snap.cost == 450.0
    assert snap.balance == 0.0


# ═══════════════════════════════════════════════════════════════
# 8. 兼容性怪癖保持
# ═══════════════════════════════════════════════════════════════


def test_quirk_balance_zero_ambiguous(balance_probe):
    """Scenario: 余额 0.0 双关 —— 「未查询」与「余额为 0」不可区分。"""
    stats = StatsTracker("m-basic", PRICING, balance_cfg=_balance_cfg())
    assert stats.balance == 0.0
    stats.fetch_balance("sk-1", 5)  # 未到查询轮
    assert stats.balance == 0.0
    balance_probe.result["value"] = {"total": 0.0}
    stats.fetch_balance("sk-1", 10)  # 查询成功但余额为 0
    assert stats.balance == 0.0


def test_quirk_negative_uncached(tmp_path):
    """Scenario: 负数未命中 —— 缓存命中大于输入时未命中列为负数（不钳制）。"""
    log, path = _make_log(tmp_path)
    log.append("m-basic", 100, 10, 200, (0.0, 0.0, 0.0), 0.0)
    row = _read_rows(path)[1]
    assert float(row[4]) == -100.0


def test_quirk_pricing_missing_key_propagates():
    """Scenario: 定价缺键抛错 —— 错误从统计更新路径抛出（不静默兜底）。"""
    stats = StatsTracker("m", {"m": {"input": 1.0}})
    with pytest.raises(KeyError):
        stats.update({"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 0})


def test_quirk_missing_usage_keys_raise():
    """用量数据缺输入/输出 token 键时抛错（不做防御性取值）。"""
    stats = StatsTracker("m-basic", PRICING)
    with pytest.raises(KeyError):
        stats.update({"completion_tokens": 5})
    with pytest.raises(KeyError):
        stats.update({"prompt_tokens": 5})


def test_quirk_set_model_keeps_accumulated_cost():
    """运行中切换模型后已累计费用不重算；后续按新模型定价查表。"""
    stats = StatsTracker("m-basic", PRICING)
    stats.update({"prompt_tokens": 1000000, "completion_tokens": 0, "cached_tokens": 0})
    assert stats.cost == 1000000.0
    stats.set_model("m-missing")  # 新模型无定价
    stats.update({"prompt_tokens": 1000000, "completion_tokens": 0, "cached_tokens": 0})
    assert stats.cost == 1000000.0
    stats.set_model("m-basic")
    stats.update({"prompt_tokens": 1, "completion_tokens": 0, "cached_tokens": 0})
    assert stats.cost == 1000000.0 + 1.0


def test_update_writes_cost_log_via_tracker(tmp_path):
    """统计更新接通成本日志：一行对应一次 update（端到端小链路）。"""
    path = tmp_path / "cost_log.csv"
    stats = StatsTracker(
        "m-basic",
        PRICING,
        cost_log_cfg=SimpleNamespace(enabled=True, path=str(path), max_bytes=0),
    )
    stats.update({"prompt_tokens": 1000, "completion_tokens": 10, "cached_tokens": 300})
    rows = _read_rows(path)
    assert rows[0] == COST_LOG_HEADER
    assert rows[1][2:6] == ["1000", "300", "700", "10"]
    assert float(rows[1][10]) == stats.cost


# ═══════════════════════════════════════════════════════════════
# 基准对照（v2/tests/baseline/data/billing.json）
# ═══════════════════════════════════════════════════════════════


def _baseline() -> dict:
    return json.loads((BASELINE_DIR / "billing.json").read_text(encoding="utf-8"))


def test_baseline_billing_json_fully_consumed():
    """基准文件的分组集合与对照测试消费范围一致（防漏测）。"""
    groups = set(_baseline()["groups"])
    assert groups == {
        "billing.get_pricing",
        "billing.cost_breakdown+calculate_cost",
        "billing.cost_breakdown(no_pricing)",
        "billing._resolve_jsonpath",
    }


def test_baseline_billing_matrix():
    """基准对照：定价查询、费用分项与总费用矩阵（模型 × 用量）逐项一致。"""
    groups = _baseline()["groups"]

    for case in groups["billing.get_pricing"]:
        args = case["input"]
        assert get_pricing(args["model"], args["user_pricing"]) == case["result"]

    for group in ("billing.cost_breakdown+calculate_cost", "billing.cost_breakdown(no_pricing)"):
        for case in groups[group]:
            args = case["input"]
            breakdown = cost_breakdown(
                args["model"],
                args["prompt_tokens"],
                args["completion_tokens"],
                args["cached_tokens"],
                args["user_pricing"],
            )
            total = calculate_cost(
                args["model"],
                args["prompt_tokens"],
                args["completion_tokens"],
                args["cached_tokens"],
                args["user_pricing"],
            )
            assert list(breakdown) == case["result"]["breakdown"]
            assert total == case["result"]["total"]


def test_baseline_jsonpath_cases():
    """基准对照：JSONPath 解析各分支（含越界、标量中途、空路径）。"""
    for case in _baseline()["groups"]["billing._resolve_jsonpath"]:
        args = case["input"]
        assert resolve_jsonpath(copy.deepcopy(args["data"]), args["path"]) == case["result"]
