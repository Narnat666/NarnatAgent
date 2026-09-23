"""实验三：中断置位 → 收敛显示的端到端延迟（真 ConversationLoop + 真 UiSink +
真 OpenAIBackend + 假 OpenAI 客户端；不发起任何真实网络请求）。

动机：实验一验证"按键判定"，实验二验证"连接关闭语义"；本实验把两者接上真实
对话内循环与界面句柄，量化"中断置位 → 屏幕出现『已打断』"的延迟，并验证
"共享客户端被中断关闭后，后续请求是否全部失败"。

场景：
  A 请求响应头等待期间置位（close 中断进行中的请求——实验二 A 的实测语义）
  B 流式输出期间置位
  C 反事实对照：若 close 不能中断进行中的请求（假设性库行为）→ 延迟来源演示
  D A 之后紧接第二轮请求（客户端已被关闭）→ 会话级后果

复跑：
    cd /d D:\\desktop\\NarnatAgent && chcp 65001 >nul && python docs\\recast\\esc_probe\\exp3_latency_chain.py
产物：
    docs/recast/esc_probe/out/exp3_results.json
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from narnat_agent.config import defaults as config_defaults            # noqa: E402
from narnat_agent.conversation import ConversationLoop                 # noqa: E402
from narnat_agent.interrupt import InterruptBus                        # noqa: E402
from narnat_agent.llm.openai_backend import OpenAIBackend              # noqa: E402
from narnat_agent.llm.runtime import LLMRuntime                        # noqa: E402
from narnat_agent.output import Console, DisplayState, Theme           # noqa: E402
from narnat_agent.ui.stream import UiSink                              # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_DIR = Path(__file__).resolve().parent / "out"
_FAKE_REQUEST = httpx.Request("POST", "http://stub.local/v1/chat/completions")


# ═══════════════════════════════════════════════════════════════
# 假 OpenAI 客户端（行为对齐实验二实测语义）
# ═══════════════════════════════════════════════════════════════


class Clock:
    """相对时间线记录器（所有组件共享，便于对齐事件顺序）。"""

    def __init__(self):
        self.t0: float | None = None
        self.marks: list[tuple[float, str]] = []

    def start(self):
        self.t0 = time.perf_counter()

    def mark(self, name: str):
        if self.t0 is not None:
            self.marks.append((round(time.perf_counter() - self.t0, 3), name))


def _content_chunk(text: str):
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta,
                                                               finish_reason=None)])


class _FakeStream:
    def __init__(self, owner: "FakeOpenAIClient"):
        self._owner = owner
        self._index = 0

    def __iter__(self):
        return self

    def __next__(self):
        from openai import APIConnectionError

        owner = self._owner
        if owner.stream_closed:
            raise APIConnectionError(request=_FAKE_REQUEST)
        if self._index >= owner.chunk_count:
            raise StopIteration
        time.sleep(owner.chunk_interval)
        if owner.stream_closed:
            raise APIConnectionError(request=_FAKE_REQUEST)
        self._index += 1
        owner.served += 1
        return _content_chunk(f"t{self._index} ")

    def close(self):
        self._owner.stream_closed = True
        self._owner.stream_closed_at = time.perf_counter()
        self._owner.clock.mark("stream.close()")


class FakeOpenAIClient:
    """模拟 OpenAI SDK 客户端的最小子面：`chat.completions.create` / `close` / 迭代流。

    - `create` 阻塞至响应头到达（`header_delay`）；`interruptible_header=True` 时，
      `close()` 会像真实 httpx 一样中断该阻塞并抛 `APIConnectionError`（实验二 A 实测）；
      False 为反事实对照（close 不中断请求）；
    - 已关闭后再次 `create` 立即抛 `APIConnectionError`（真实 SDK 包装后的形态，
      实验二 B 实测）。
    """

    def __init__(self, *, header_delay: float = 0.0, chunk_interval: float = 0.2,
                 interruptible_header: bool = True, chunk_count: int = 100000,
                 clock: "Clock | None" = None):
        self.header_delay = header_delay
        self.chunk_interval = chunk_interval
        self.interruptible_header = interruptible_header
        self.chunk_count = chunk_count
        self.clock = clock or Clock()
        self.closed = False
        self.closed_at: float | None = None
        self.stream_closed = False
        self.stream_closed_at: float | None = None
        self.create_calls = 0
        self.served = 0
        self._header_event = threading.Event()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        from openai import APIConnectionError

        self.create_calls += 1
        self.clock.mark("create() 进入")
        if self.closed:
            raise APIConnectionError(request=_FAKE_REQUEST)
        if self.header_delay > 0:
            deadline = time.monotonic() + self.header_delay
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._header_event.wait(min(remaining, 0.05)):
                    self._header_event.clear()
                    if self.interruptible_header:
                        self.clock.mark("create() 被 close 中断")
                        raise APIConnectionError(request=_FAKE_REQUEST)
                    # 反事实：close 不能中断等待，继续等满 header_delay
        self.clock.mark("create() 返回响应流")
        return _FakeStream(self)

    def close(self):
        self.closed = True
        self.closed_at = time.perf_counter()
        self._header_event.set()
        self.clock.mark("client.close() 被调用")


# ═══════════════════════════════════════════════════════════════
# 最小替身（端口实现；保留真实 UiSink / ConversationLoop / Backend）
# ═══════════════════════════════════════════════════════════════


class RecordingConsole(Console):
    def __init__(self):
        self.records: list[tuple[float, str]] = []
        super().__init__(stdout=io.StringIO(), platform="linux")

    def write(self, text: str) -> None:
        self.records.append((time.perf_counter(), text))
        super().write(text)

    def try_write(self, text: str) -> bool:
        self.records.append((time.perf_counter(), text))
        return super().try_write(text)

    def text(self) -> str:
        return "".join(t for _, t in self.records)


class RecordingSpinner:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class FakeListener:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


class StoreStub:
    def __init__(self):
        self.appended: list[tuple[str, object]] = []

    def view(self):
        return SimpleNamespace(to_list=lambda: [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "你好"},
        ])

    def repair(self):
        return False

    def append_assistant(self, content, tool_calls=None, thinking=None,
                         thinking_signature=None):
        self.appended.append(("assistant", content))

    def append_user(self, content):
        self.appended.append(("user", content))

    def append_tool_result(self, *args, **kwargs):
        pass

    def append_interrupted_tools(self, *args, **kwargs):
        pass


class StatsStub:
    input_tokens = 0
    output_tokens = 0
    cache_hit_ratio = 0.0
    cost = 0.0
    balance = 0.0

    def update(self, usage):
        pass


class InteractionStub:
    """会话级交互替身：begin_turn 走真实的 enter_run_mode + 真 UiSink。"""

    def __init__(self, console, theme, display, interrupt):
        self._console = console
        self._theme = theme
        self._display = display
        self._interrupt = interrupt
        self.spinners: list[RecordingSpinner] = []

    def begin_turn(self):
        self._interrupt.enter_run_mode()
        spinner = RecordingSpinner()
        sink = UiSink(self._theme, self._console, self._interrupt, self._display,
                      spinner=spinner)
        sink.begin()
        self.spinners.append(spinner)
        return sink

    def notify_interrupted(self):
        self._interrupt.enter_input_mode()


class BackendAdapter:
    """把 OpenAIBackend 适配为 ConversationLoop 需要的 LLMStreamSource 面。"""

    def __init__(self, backend: OpenAIBackend, clock: "Clock | None" = None):
        self._backend = backend
        self._clock = clock or Clock()

    def chat_stream(self, messages, no_tools=False, no_thinking=False, cancel_check=None):
        self._clock.mark("chat_stream 生成器创建")
        inner = self._backend.chat_stream(messages, no_tools=no_tools,
                                          no_thinking=no_thinking,
                                          cancel_check=cancel_check)
        first = True

        def wrapper():
            nonlocal first
            for item in inner:
                if first:
                    first = False
                    self._clock.mark("首个事件产出")
                yield item

        return wrapper()

    def set_retry_count(self, n):
        pass

    @property
    def raw_sse(self):
        return None


class FakeConfig:
    protocol = "openai"
    model = "fake-model"
    api_key = "sk-fake"
    base_url = "http://stub.local"
    temperature = None
    max_tokens = None
    retry_count = 3
    thinking_enabled = False
    thinking_effort = ""
    thinking_passback = True


def build_loop(*, fake_client: FakeOpenAIClient, max_retries: int, clock: "Clock"):
    console = RecordingConsole()
    theme = Theme(console)
    display = DisplayState()
    listener = FakeListener()
    bus = InterruptBus(listener)
    runtime = LLMRuntime(config_defaults, tool_defs=[])
    runtime.max_retries = max_retries
    backend = OpenAIBackend(FakeConfig(), runtime, client=fake_client)
    bus.subscribe(runtime.abort)          # 等价 LLMClient 构造时的订阅
    interaction = InteractionStub(console, theme, display, bus)
    loop = ConversationLoop(
        llm=BackendAdapter(backend, clock),
        store=StoreStub(),
        dispatcher=SimpleNamespace(execute=lambda *a: [], execute_one=lambda *a: None),
        env=SimpleNamespace(
            plan=SimpleNamespace(current=lambda: []),
            reminders=SimpleNamespace(try_trigger_plan=lambda: False,
                                      try_trigger_bg=lambda: False),
            goal=SimpleNamespace(is_set=False),
            delete_gate=SimpleNamespace(take=lambda: None),
        ),
        stats=StatsStub(),
        interaction=interaction,
        console=console,
        ai_options=SimpleNamespace(retry_count=3, thinking_effort="", thinking_options={}),
        data_dir=str(OUT_DIR),
        background=None,
        compression=None,
        ratio=None,
        logger=None,
    )
    return loop, bus, console, interaction, runtime


def scenario(name: str, *, header_delay: float, interrupt_after: float,
             interruptible_header: bool = True, chunk_interval: float = 0.2,
             second_round: bool = False, max_retries: int = 1) -> dict:
    clock = Clock()
    fake = FakeOpenAIClient(header_delay=header_delay, chunk_interval=chunk_interval,
                            interruptible_header=interruptible_header, clock=clock)
    loop, bus, console, interaction, runtime = build_loop(
        fake_client=fake, max_retries=max_retries, clock=clock)

    clock.start()
    raise_at: list[float] = []
    raise_state: dict = {}

    def interrupt_thread():
        time.sleep(interrupt_after)
        raise_at.append(time.perf_counter() - clock.t0)
        raise_state["handle_before"] = type(runtime._active_handle).__name__
        raise_state["is_set_before"] = bus.is_set
        bus.raise_()
        raise_state["is_set_after"] = bus.is_set
        raise_state["handle_after"] = type(runtime._active_handle).__name__
        clock.mark(f"raise_() 完成 is_set={bus.is_set} "
                   f"handle={type(runtime._active_handle).__name__}")

    sink = interaction.begin_turn()
    clock.mark("begin_turn 完成（运行模式已启动）")
    thread = threading.Thread(target=interrupt_thread, daemon=True)
    thread.start()
    clock.mark("loop.run 开始")
    outcome = loop.run(sink)
    finished_at = time.perf_counter() - clock.t0
    clock.mark(f"loop.run 返回 outcome={outcome.status}")
    thread.join(1.0)

    # 「已打断」出现的时刻
    shown_at = None
    for ts, text in console.records:
        if "已打断" in text:
            shown_at = ts - clock.t0
            break
    delay = None
    if shown_at is not None and raise_at:
        delay = shown_at - raise_at[0]

    result = {
        "scenario": name,
        "outcome": outcome.status,
        "raised_at_s": round(raise_at[0], 3) if raise_at else None,
        "interrupted_shown_at_s": round(shown_at, 3) if shown_at else None,
        "delay_raise_to_show_s": round(delay, 3) if delay is not None else None,
        "loop_finished_at_s": round(finished_at, 3),
        "raise_state": raise_state,
        "timeline": clock.marks,
        "fake_client": {
            "create_calls": fake.create_calls,
            "client_closed": fake.closed,
            "stream_closed": fake.stream_closed,
            "chunks_served": fake.served,
        },
        "console_tail": console.text()[-200:],
    }

    if second_round:
        t1 = time.perf_counter()
        sink2 = interaction.begin_turn()
        outcome2 = loop.run(sink2)
        result["second_round"] = {
            "outcome": outcome2.status,
            "elapsed_s": round(time.perf_counter() - t1, 2),
            "error_text": "".join(
                t for _, t in console.records if "错误" in t or "API调用失败" in t
            )[-300:],
        }
    return result


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scenarios = [
        # A：响应头等待期间置位（中断应能立即收敛——实验二 A 的语义）
        lambda: scenario("A 响应头等待期间置位", header_delay=5.0, interrupt_after=1.0),
        # B：流式输出期间置位
        lambda: scenario("B 流式输出期间置位", header_delay=0.3, interrupt_after=1.2,
                         chunk_interval=0.2),
        # C：反事实（close 无法中断请求）→ 演示"若中断无效"的延迟来源
        lambda: scenario("C 反事实：close 无法中断进行中请求",
                         header_delay=5.0, interrupt_after=1.0,
                         interruptible_header=False),
        # E：响应头已到、首个数据块前长时间静默（最贴近用户报告"一直显示思考中"）
        lambda: scenario("E 响应头已到、服务端思考静默期间置位",
                         header_delay=0.3, interrupt_after=1.5,
                         chunk_interval=10.0),
        # D：A 场景后第二轮（共享客户端已被关闭）
        lambda: scenario("D 中断关闭共享客户端后的第二轮请求", header_delay=5.0,
                         interrupt_after=1.0, second_round=True),
    ]
    results = []
    for make in scenarios:
        res = make()
        results.append(res)
        print(f"\n{res['scenario']}")
        print(f"    结果={res['outcome']}  置位@{res['raised_at_s']}s  "
              f"『已打断』@{res['interrupted_shown_at_s']}s  "
              f"延迟={res['delay_raise_to_show_s']}s  "
              f"循环结束@{res['loop_finished_at_s']}s")
        print(f"    置位时 handle={res['raise_state'].get('handle_before')} "
              f"is_set={res['raise_state'].get('is_set_after')}  "
              f"chunks={res['fake_client']['chunks_served']}")
        for ts, mark in res["timeline"]:
            print(f"      {ts:8.3f}s  {mark}")
        if "second_round" in res:
            print(f"    第二轮：{res['second_round']['outcome']} "
                  f"耗时{res['second_round']['elapsed_s']}s  "
                  f"错误={res['second_round']['error_text'][:120]!r}")

    (OUT_DIR / "exp3_results.json").write_text(
        json.dumps({"scenarios": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n结果已写 {OUT_DIR / 'exp3_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
