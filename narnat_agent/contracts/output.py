"""输出/交互端口 —— 一个 AI 回合的输出通道与会话级交互、动画的端口定义。

契约来源：
- `openspec/changes/recast-v2/specs/ui/spec.md`「流式输出会话句柄协议」（OutputSink
  的操作语义来源）、「动画（思考中 / 正在压缩 / 正在合并）」、「headless 纯文本输出」
  （headless 流会话替身语义）、「统计栏」（TurnStats 字段语义）；
- `openspec/changes/recast-v2/specs/stats/spec.md`（统计栏数据供给口径）。

设计决策（design D4/D8）：交互模式由 ui 实现（渲染 + 动画 + 统计栏），headless
由纯文本实现；conversation 只依赖端口。spinner / 落定 / 重置等渲染细节收进
`OutputSink` 实现内部；`restart_attempt()` 语义对齐现状 `reset_renderer` + 重播
行为；会话总结/压缩动画为独立的 `Animator` 端口（压缩与 summarize 执行方构造注入）。

本模块为零逻辑纯定义层：只依赖标准库（typing / dataclasses）；
不 import 新包其他积木。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "Animator",
    "InteractionPort",
    "OutputSink",
    "TurnStats",
]


@dataclass
class TurnStats:
    """一轮回复的收尾统计（参数对齐 specs/ui「统计栏」显示参数）。

    - `input_tokens`：输入 token 数（最近一轮）；
    - `output_tokens`：输出 token 数（全程累计）；
    - `cache_ratio`：缓存命中率（0~1 的比例值，0 时不显示缓存段）；
    - `cost`：费用（全程累计，人民币元；显示费用开关开启时显示 4 位小数）；
    - `balance`：余额（最近一次成功查询的值；>0 且开关开启时显示 2 位小数）；
    - `thinking_effort`：思考强度（统计栏恒显示该段）。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_ratio: float = 0.0
    cost: float = 0.0
    balance: float = 0.0
    thinking_effort: str = "高"


@runtime_checkable
class OutputSink(Protocol):
    """一个 AI 回合的流式输出会话句柄（specs/ui「流式输出会话句柄协议」）。

    操作语义（实现方职责，与规格逐条对齐）：
    - 创建：进入运行模式（清除中断标志并开始 ESC 打断监听）、启动思考动画；
    - `feed`：内容增量——收到文本块即停止思考动画并交给渲染器增量渲染；
    - `notify`：状态提示——系统提示行（如重试提示、压缩提示），不走 AI 内容
      渲染管线（具体渲染形态归实现；headless 下按纯文本输出）；
    - `finish`：切回输入模式 → 停止动画 → 落定渲染缓冲 → 按 `with_stats` 显示
      统计栏（目标模式中间轮传 False：只落定不显示）；
    - `abort`：标记已中止 → 切回输入模式 → 停止动画 → 无消息时显示默认打断提示，
      有消息时原样输出该消息（如程序异常文案）；
    - `restart_attempt`：重试重播——此前未完成内容作废、渲染缓冲与状态清理
      （服务端流中断自动重试之前调用，防止残留与重播内容拼接错乱）；
    - `flush`：落定渲染缓冲（守卫：从未注入内容时不动作）；
    - `pause` / `resume`：动画的并行暂停与恢复（计数：首个暂停者停止动画；
      恢复递减且不为负、归零才重启动画；已中止后恢复不再启动动画）——工具行
      显示前暂停并落定、显示完成后恢复（specs/conversation「工具调度分组与结果回传」）；
    - `cancelled` / `aborted`：状态查询（取消反映进程级中断状态；中止反映标记）。
    """

    @property
    def cancelled(self) -> bool:
        """取消查询：反映进程级中断状态（headless 恒为 False，不可打断）。"""
        ...

    @property
    def aborted(self) -> bool:
        """已中止查询：反映中止标记（abort 后为 True）。"""
        ...

    def feed(self, text: str) -> None:
        """内容增量（走 Markdown 渲染）。"""
        ...

    def notify(self, text: str) -> None:
        """状态提示（纯文本提示行）。"""
        ...

    def flush(self) -> None:
        """落定渲染缓冲（守卫：从未注入内容时不动作）。"""
        ...

    def pause(self) -> None:
        """暂停动画（并行计数 +1；首个暂停者真正停止动画）。"""
        ...

    def resume(self) -> None:
        """恢复动画（计数递减且不为负；归零时才重启；已中止后不再启动）。"""
        ...

    def finish(self, stats: TurnStats, with_stats: bool = True) -> None:
        """收尾：落定缓冲，并按 `with_stats` 显示统计栏。"""
        ...

    def abort(self, message: str | None = None) -> None:
        """中止：标记已中止并恢复输入状态；message 非空时原样输出该消息。"""
        ...

    def restart_attempt(self) -> None:
        """重试重播：此前未完成内容作废、渲染缓冲清理。"""
        ...


@runtime_checkable
class InteractionPort(Protocol):
    """会话级交互端口（ui 与 headless 各自实现；conversation 只依赖本端口）。"""

    def begin_turn(self) -> OutputSink:
        """开始一个 AI 回合：进入运行模式（清除中断标志并开始 ESC 打断监听）、
        创建并返回该回合的流式输出句柄（对应现状 `create_stream`）。"""
        ...

    def read_confirmation(self, prompt: str) -> bool:
        """在提示符下读取一次确认（如 `  确认执行此命令? [y/N]: `）：

        输入 `y`/`yes`（忽略大小写与首尾空白）为 True；其余输入与无输入
        （含无交互环境）为 False（取消）。headless 恒返回 False。
        """
        ...

    def notify_interrupted(self) -> None:
        """中断收敛回调：停止采集、切回输入模式并重建输入会话
        （specs/interrupt「中断后的收敛与界面返还」）。"""
        ...


@runtime_checkable
class Animator(Protocol):
    """动画端口（成对的开始/结束操作；headless 实现均为无效果）。

    - 压缩动画：手动/自动压缩执行期间显示「正在压缩」（立即启动）；
    - 合并动画：会话总结（保存/合并）期间显示「正在合并」（立即启动）。

    结束对未启动情形幂等；等待动画线程退出的上限为 1 秒。思考中动画由
    `OutputSink` 内部控制（创建时启动、feed/finish/abort 时停止），无独立入口。
    """

    def begin_compressing(self) -> None: ...

    def end_compressing(self) -> None: ...

    def begin_summarizing(self) -> None: ...

    def end_summarizing(self) -> None: ...
