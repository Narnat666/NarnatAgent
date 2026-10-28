"""风格提醒双通道逻辑验证（独立模拟，不依赖完整组装）"""
import sys
sys.path.insert(0, r"d:\desktop\NarnatAgent")

from dataclasses import dataclass, field
from narnat_agent.config.loader import StyleConfig
from narnat_agent.config.defaults import STYLE_NOTICE_TEMPLATE


@dataclass
class FakeCtx:
    style_discuss_rounds: int = 0
    style_token_baseline: int = -1


class FakeStats:
    def __init__(self):
        self.input_tokens = 0


class FakeMsgManager:
    def __init__(self):
        self.injected = []

    def append_user_merged(self, content):
        self.injected.append(content)


@dataclass
class Harness:
    """复刻 AgentLoop 的三个方法（从源码拷贝逻辑，验证行为）"""
    cfg: StyleConfig
    ctx: FakeCtx
    stats: FakeStats
    mgr: FakeMsgManager

    def _inject_style_notice(self):
        self.mgr.append_user_merged(
            STYLE_NOTICE_TEMPLATE.format(text=self.cfg.notice_text))
        self.ctx.style_discuss_rounds = 0
        cur = self.stats.input_tokens
        self.ctx.style_token_baseline = cur if cur > 0 else 0

    def _check_token_interval(self):
        if not self.cfg.enabled or self.cfg.token_interval <= 0:
            return
        cur = self.stats.input_tokens
        if self.ctx.style_token_baseline < 0:
            self.ctx.style_token_baseline = cur if cur > 0 else 0
            return
        if cur < self.ctx.style_token_baseline:
            self.ctx.style_token_baseline = cur if cur > 0 else 0
            return
        if cur - self.ctx.style_token_baseline < self.cfg.token_interval:
            return
        self._inject_style_notice()

    def _discuss_round_done(self):
        if not self.cfg.enabled or self.cfg.notice_interval <= 0:
            return
        self.ctx.style_discuss_rounds += 1
        if self.ctx.style_discuss_rounds < self.cfg.notice_interval:
            return
        self._inject_style_notice()


def make(cfg_kwargs):
    cfg = StyleConfig(**cfg_kwargs)
    return Harness(cfg, FakeCtx(), FakeStats(), FakeMsgManager())


failures = []


def check(name, cond):
    if not cond:
        failures.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"PASS: {name}")


# ── 1. token 通道基本触发 ──
h = make(dict(enabled=True, notice_text="T", notice_interval=0, token_interval=100))
h.stats.input_tokens = 1000
h._check_token_interval()   # 初始化基准=1000，不注入
check("1a 首查初始化不注入", len(h.mgr.injected) == 0 and h.ctx.style_token_baseline == 1000)
h.stats.input_tokens = 1099
h._check_token_interval()
check("1b 增量99不注入", len(h.mgr.injected) == 0)
h.stats.input_tokens = 1100
h._check_token_interval()
check("1c 增量100注入", len(h.mgr.injected) == 1 and h.mgr.injected[0] == "[系统提醒] T")
check("1d 注入后基准重置=1100", h.ctx.style_token_baseline == 1100)
h.stats.input_tokens = 1199
h._check_token_interval()
check("1e 重置后增量99不注入", len(h.mgr.injected) == 1)

# ── 2. 轮次通道触发后 token 基准同步重置（同轮不双触发）──
h = make(dict(enabled=True, notice_text="T", notice_interval=2, token_interval=100))
h.stats.input_tokens = 500
h._check_token_interval()   # 基准=500
h._discuss_round_done()     # 轮1，不注入
check("2a 轮1不注入", len(h.mgr.injected) == 0)
h.stats.input_tokens = 900  # token 增量 400（已超 100，但轮次先达）
h._discuss_round_done()     # 轮2 → 轮次通道注入
check("2b 轮2注入一次", len(h.mgr.injected) == 1)
check("2c 注入后 token 基准重置为当前900", h.ctx.style_token_baseline == 900)
h.stats.input_tokens = 950
h._check_token_interval()
check("2d 重置后增量50不注入", len(h.mgr.injected) == 1)

# ── 3. token 通道注入后轮次计数清零（同轮不双触发）──
h = make(dict(enabled=True, notice_text="T", notice_interval=3, token_interval=100))
h.stats.input_tokens = 100
h._check_token_interval()
h._discuss_round_done()  # 轮1
h._discuss_round_done()  # 轮2（再一轮轮次通道就触发）
h.stats.input_tokens = 250
h._check_token_interval()  # 增量150 → token 通道注入
check("3a token 通道注入", len(h.mgr.injected) == 1)
check("3b 注入后轮次计数清零", h.ctx.style_discuss_rounds == 0)
h._discuss_round_done()  # 轮次重新从1开始
check("3c 清零后轮次重新累计不立即触发", len(h.mgr.injected) == 1)

# ── 4. 压缩后上下文变小 → 惰性重置 ──
h = make(dict(enabled=True, notice_text="T", notice_interval=0, token_interval=100))
h.stats.input_tokens = 10000
h._check_token_interval()
h.stats.input_tokens = 9900  # 增量 -100（压缩）
h._check_token_interval()
check("4a 压缩后基准重置为9900", h.ctx.style_token_baseline == 9900)
h.stats.input_tokens = 9950  # 从新基准起增量50
h._check_token_interval()
check("4b 压缩后重新计时不注入", len(h.mgr.injected) == 0)
h.stats.input_tokens = 10000
h._check_token_interval()
check("4c 新基准起增量100注入", len(h.mgr.injected) == 1)

# ── 5. 关闭开关：全通道静默 ──
h = make(dict(enabled=False, notice_text="T", notice_interval=2, token_interval=100))
h.stats.input_tokens = 1000
h._check_token_interval()
h._discuss_round_done()
h._discuss_round_done()
check("5a enabled=False 不注入", len(h.mgr.injected) == 0)

h = make(dict(enabled=True, notice_text="T", notice_interval=0, token_interval=0))
h.stats.input_tokens = 1000
h._check_token_interval()
check("5b token_interval=0 不注入且不初始化", h.ctx.style_token_baseline == -1)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过")
