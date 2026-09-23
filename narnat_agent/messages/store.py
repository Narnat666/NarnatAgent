"""消息存储 —— 对话历史（messages）的唯一所有者、只读视图与请求前序列修复。

行为契约：`openspec/changes/recast-v2/specs/messages/spec.md`。消息字典的字段名与角色
取值是对外数据契约（磁盘会话文件、模型请求构造、压缩重建均以其为准，必须跨版本兼容）。

结构（design D1/D6/D7）：
- 唯一所有者 + 唯一修改入口：全部修改都经 `MessageStore` 的受控接口；外部读取只拿得到
  只读视图 `MessageView`，无法增删改内部列表结构；
- 无委托层：现状 `MessageList`（持有列表）+ `MessageManager`（逐方法纯转发）合并为一个
  所有者类；无消费方且与整体替换重复的 `count_role` / `clear_and_rebuild` /
  `compress_and_rebuild` 已删除（证据见 R2 报告「补丁痕迹·死代码」）；
- 职责边界：压缩编排归 compression 积木、日志实现归 app（本模块只经 `LogSink` 端口调用）。

依赖规则：本积木位于 L1，只依赖标准库（跨积木共享面若需新增，须经 contracts）。
"""
from __future__ import annotations

from typing import Any, Iterator, Protocol

from ..contracts.messages import INTERRUPTED_TOOL_RESULT, SYNTHETIC_THINKING

__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "SYNTHETIC_THINKING",
    "LogSink",
    "MessageStore",
    "MessageView",
]

# INTERRUPTED_TOOL_RESULT / SYNTHETIC_THINKING 为跨积木共享字面量，
# 定义处已上提至 `contracts.messages`（llm 转换层与 messages 共用同一定义），
# 此处 re-export 保持本积木对外导出面不变。


class LogSink(Protocol):
    """日志端口（构造注入）：`info(模块名, 消息)`。

    实现方（app 侧日志器）仅在调试模式写文件、未启动时日志调用静默丢弃；本积木不感知其实现。
    """

    def info(self, module: str, message: str) -> None:
        """记录一条 info 日志。"""
        ...


class MessageView:
    """messages 的只读视图：零拷贝、实时、结构只读。

    - 只暴露读取能力：`len()` / 下标（含负下标，`-1` 为末条）/ 迭代 / `to_list()`；
    - 实时视图：内部消息变化立即反映（整体替换亦不替换内部列表对象）；
    - `to_list()` 返回浅拷贝列表，外部增删该列表不影响内部结构；
    - 兼容怪癖（specs/messages「兼容性怪癖保持」）：视图取出的消息字典与内部共享，
      调用方可改写其字段内容（除列表结构外无隔离）。
    """

    __slots__ = ("_messages",)

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    def __len__(self) -> int:
        """消息条数。"""
        return len(self._messages)

    def __getitem__(self, index: int) -> dict[str, Any]:
        """按下标取消息字典（支持负下标，`-1` 为末条）。"""
        return self._messages[index]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """按发生顺序迭代消息字典。"""
        return iter(self._messages)

    def to_list(self) -> list[dict[str, Any]]:
        """导出浅拷贝列表（供序列化落盘或构造模型请求）。"""
        return list(self._messages)


class MessageStore:
    """对话历史（messages）的唯一所有者。

    创建即以一条 `system` 消息（系统提示词）开头，此后按发生顺序追加 `user`（用户输入或
    系统提醒）、`assistant`（模型回复）与 `tool`（工具结果）消息。整体替换（压缩重建、
    会话切换）是唯一的历史整体更新入口，同样经本类生效。
    """

    def __init__(self, system_prompt: str, log: LogSink | None = None) -> None:
        """以系统提示词构建消息列表。

        log：日志端口（构造注入）；缺省则不记录任何日志。
        """
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._log = log

    # ── 只读接口 ──

    def view(self) -> MessageView:
        """返回只读实时视图（零拷贝，调用方无法修改列表结构）。"""
        return MessageView(self._messages)

    def __len__(self) -> int:
        """消息条数。"""
        return len(self._messages)

    # ── 受控修改接口（唯一修改入口）──

    def append_system(self, content: str) -> None:
        """追加 `system` 消息（技能注入、子会话结论等）；首条系统提示词不被覆盖。"""
        self._messages.append({"role": "system", "content": content})

    def append_user(self, content: str) -> None:
        """追加 `user` 消息（用户输入或系统提醒）。"""
        self._messages.append({"role": "user", "content": content})

    def append_assistant(
        self,
        content: str | None,
        tool_calls: list | None = None,
        thinking: str | None = None,
        thinking_signature: str | None = None,
    ) -> None:
        """追加 `assistant` 消息（specs/messages「assistant 可选字段写入规则」）。

        - `content` 为空串或缺省时记 `null`，非空原样保留；
        - `thinking` / `thinking_signature` 仅在显式给出时写入该字段（空串是合法值），
          未给出则不出现该字段；`thinking` 为 `None` 表示未捕获（如历史会话）；
        - `tool_calls` 仅非空时写入该字段。
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or None}
        if thinking is not None:
            msg["thinking"] = thinking
        if thinking_signature is not None:
            msg["thinking_signature"] = thinking_signature
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def append_tool_result(self, tool_call_id: str, result: str) -> None:
        """回填某次工具调用的结果：`{"role": "tool", "tool_call_id": …, "content": …}`。"""
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })

    def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:
        """为未完成的工具调用按原顺序补齐 `[用户中断]` 结果（工具批执行被用户中断时调用）。

        已有真实结果（id 在 `completed_ids` 中）的调用不动。缺 `id` 的调用项直接读取 `id`
        → 异常（specs/messages「兼容性怪癖保持」：中断补齐缺 id 即异常，与此处的跳过规则不同）。
        """
        for tc in tool_calls:
            if tc["id"] not in completed_ids:
                self.append_tool_result(tc["id"], INTERRUPTED_TOOL_RESULT)

    def replace_all(self, new_messages: list[dict[str, Any]]) -> None:
        """以完整新列表一次性原子替换全部消息（压缩重建、会话切换）。

        接受任意长度（含清空，不做最小长度保护）；原地替换列表内容，既有视图立即可见新内容。
        """
        self._messages[:] = new_messages

    # ── 请求前序列修复 ──

    def repair(self) -> bool:
        """修复被中断留下的不完整序列（每次发出请求前调用）。

        1. 未回复的工具调用：以全部已有 `tool` 消息的非空 `tool_call_id` 组成已回复集合，
           遍历 `assistant` 消息的 `tool_calls`，为不在集合中的调用按序在序列末尾追加
           `[用户中断]` 工具消息（同一 id 只补一次；缺 id 的调用项跳过）。
        2. 尾部合成 assistant：仅当第 1 步确实追加过、且修复后序列末条为 `tool` 时，追加
           一条 `content` 与 `thinking` 同为 `SYNTHETIC_THINKING` 的 assistant（思考模式下
           请求尾部的 assistant 必须携带非空思考块），且不写工具调用字段。

        未发生第 1 步修复时不追加合成消息；只处理「缺失工具结果」与「修复后尾部为工具」两种
        形态，顺序错乱、工具消息无对应 assistant 等其它异常不处理（specs/messages「兼容性
        怪癖保持」）。

        Returns:
            本次是否发生修复（无缺陷序列返回 `False` 且序列不变）。修复发生时经日志端口
            记录一条 info 日志。
        """
        replied_ids: set[str] = set()
        for msg in self.view():
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    replied_ids.add(tc_id)

        repaired = False
        # 快照遍历：本次修复会向同一列表追加，故遍历原始条目的稳定副本
        for msg in tuple(self.view()):
            if msg.get("role") != "assistant" or "tool_calls" not in msg:
                continue
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id and tc_id not in replied_ids:
                    self.append_tool_result(tc_id, INTERRUPTED_TOOL_RESULT)
                    replied_ids.add(tc_id)
                    repaired = True

        if repaired and self.view()[-1].get("role") == "tool":
            self.append_assistant(SYNTHETIC_THINKING, thinking=SYNTHETIC_THINKING)

        if repaired and self._log is not None:
            self._log.info("messages", "repair: 修复了打断后的消息序列")
        return repaired
