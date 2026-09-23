"""命令补全与分发骨架 —— 可用命令集与候选清单来自注入的命令源。

契约来源：`openspec/changes/recast-v2/specs/ui/spec.md`：
- 「命令集与分发语义」：分发把命令名转小写并剥除全部前导斜杠；`/clear` 在任何状态
  可用且不依赖会话管理器；`/goal` 不校验可用性；其余命令必须在当前可用集合中；
  分发结果为三态语义（未识别 = 该输入按普通用户消息继续处理、已处理 = 不调用模型
  回到输入、退出 = 清理后结束进程）；未识别命令不打印错误提示；
- 「Tab 补全」：命令名（单词语前缀补全 + 描述）、`/skill` 技能树逐层下钻、`/cd`
  `/rm` `/thinking` `/mode` 的名称候选（层级候选只补最后一段）、`/ls` 静态候选；
  其余命令在参数位置不产出候选；非斜杠输入不补全；
- 「兼容性怪癖保持」：命令名前导斜杠被全部剥除（`///save` 等同 `/save`）；
  `/clear` 以裸清屏转义序列实现、不经颜色体系。

分工（design D8）：命令处理器（文案、参数校验、会话操作）与可用命令表归 `sessions`
积木（`SessionManager` / `SessionCommands`），经 `CommandSource` 端口注入；本模块做
ui 侧的分发骨架（命令名归一、可用性门禁、`/clear` 清屏、三态结果、结果文本打印）
与补全器，不 import sessions。
"""
from __future__ import annotations

from enum import IntEnum
from typing import Dict, List, Mapping, Protocol, TypedDict, runtime_checkable

from prompt_toolkit.completion import Completer, Completion

from ..output import Console

__all__ = [
    "CLEAR_SCREEN",
    "CommandCompleter",
    "CommandReply",
    "CommandResult",
    "CommandRouter",
    "CommandSource",
    "SkillNode",
]

# 清屏转义序列（裸写、不经颜色体系：纯文本模式去色不影响它）
CLEAR_SCREEN = "\033[2J\033[H"


class CommandResult(IntEnum):
    """命令分发返回值（继承 int：主循环按数值比较，现状保持）。

    - `UNKNOWN`：未识别——该输入按普通用户消息继续处理；
    - `HANDLED`：已处理——不调用模型，回到输入；
    - `EXIT`：退出——清理后结束进程。
    """

    UNKNOWN = 0
    HANDLED = 1
    EXIT = 2


class SkillNode(TypedDict, total=False):
    """技能树节点（补全候选的数据形态）。

    - `name`：节点名（目录名或文件名）；
    - `type`：`dir` / `file`；
    - `children`：目录节点的子节点（逐层下钻）；
    - `origin`：叶子来源（`system` 系统技能 / `project` 项目技能）；
    - `single`：单技能目录（按叶子处理：裸名显示、不补尾 `/`）。
    """

    name: str
    type: str
    children: List["SkillNode"]
    origin: str
    single: bool


@runtime_checkable
class CommandReply(Protocol):
    """命令源的分发结果形状（`sessions.commands.CommandReply` 实现本形状）。

    - `kind`：三态语义字符串（`handled` 已处理 / `exit` 退出 / `unknown` 未识别）；
    - `text`：完整可打印文本（可多行、已着色、含行首缩进；空串不输出）。
    """

    kind: str
    text: str


# 命令源三态字符串 → ui 侧三态结果
REPLY_KIND_MAP: Dict[str, CommandResult] = {
    "handled": CommandResult.HANDLED,
    "exit": CommandResult.EXIT,
    "unknown": CommandResult.UNKNOWN,
}


@runtime_checkable
class CommandSource(Protocol):
    """命令源端口（由 `sessions` 积木实现；ui 只消费）。

    - `available_commands()`：当前会话状态决定的可用命令表 `{"/save": "保存会话"}`；
    - `dispatch(text)`：分发一条命令输入（含前导 `/` 的完整行，如 `/save 名称`），
      返回三态结果与待打印文本（未识别的命令返回 `unknown`）；
    - 名称候选清单（Tab 补全数据源）：`list_session_names` / `list_rm_names` /
      `list_thinking_options` / `list_model_options`；
    - `list_skill_tree()`：技能树（文件节点标注来源，目录节点可逐级下钻）。
    """

    def available_commands(self) -> Mapping[str, str]:
        """当前状态可用命令表（命令名含前导斜杠 → 描述）。"""
        ...

    def dispatch(self, text: str) -> CommandReply:
        """分发一条命令输入（含前导 `/` 的完整行；命令名归一由实现方负责）。"""
        ...

    def list_session_names(self) -> List[str]:
        """`/cd` 候选：全部会话名（层级路径用 `/` 分隔）。"""
        ...

    def list_rm_names(self) -> List[str]:
        """`/rm` 候选：当前状态可标记删除的会话名（已标记的不在其中）。"""
        ...

    def list_thinking_options(self) -> List[str]:
        """`/thinking` 候选：配置的思考强度选项。"""
        ...

    def list_model_options(self) -> List[str]:
        """`/mode` 候选：配置的模型列表。"""
        ...

    def list_skill_tree(self) -> List[SkillNode]:
        """技能树根节点列表（系统技能在前、项目技能在后）。"""
        ...


class CommandRouter:
    """命令分发骨架：命令名归一 → 可用性门禁 → 委托命令源 → 打印结果 → 三态。

    `/clear` 在本层直接完成（不依赖会话管理器，任何状态可用）；`/goal` 不经可用性
    校验；其余命令须在当前状态可用集合中，否则按未识别返回（不打印错误提示）。
    命令源返回的文本由本层打印（空串不输出；缺行尾换行时补齐一行）。
    """

    def __init__(self, source: CommandSource, console: Console):
        self._source = source
        self._console = console

    def dispatch(self, text: str) -> CommandResult:
        """分发一条命令输入（含前导 `/` 的完整行）。"""
        stripped = text.strip()
        name = stripped.split(None, 1)[0].lower().lstrip("/") if stripped else ""
        if name == "clear":
            self._console.write(CLEAR_SCREEN)
            return CommandResult.HANDLED
        if self._source is None:
            return CommandResult.UNKNOWN
        if name != "goal" and f"/{name}" not in self._source.available_commands():
            return CommandResult.UNKNOWN
        reply = self._source.dispatch(stripped)
        self._write_reply(reply)
        return REPLY_KIND_MAP.get(reply.kind, CommandResult.UNKNOWN)

    def _write_reply(self, reply: CommandReply) -> None:
        """打印命令结果文本（未识别不打印；空串不输出；缺行尾换行时补一行）。"""
        text = reply.text or ""
        if not text or reply.kind == "unknown":
            return
        self._console.write(text if text.endswith("\n") else text + "\n")


class CommandCompleter(Completer):
    """命令补全器（候选与描述全部来自注入的命令源，随会话状态变化）。

    - 命令名：输入为单个词且未以空格结尾 → 当前状态可用命令的前缀匹配，
      补全剩余字符并以命令描述作为展示说明；
    - `/skill`：技能树按 `/`（兼容 `\\`）逐层下钻；目录候选补尾 `/` 并标注"目录"，
      名字已完整输入时目录补 `/`（标注"进入目录"）、文件不产出候选；叶子标注
      "系统技能 / 项目技能"；
    - `/cd` `/rm` `/thinking` `/mode`：刚输入完命令加空格 → 列出全部候选；输入第二个
      词且未以空格结尾 → 前缀补全（候选含 `/` 时仅补全最后一段）；
    - `/ls`：静态候选 `--all`；其余命令在参数位置不产出候选。
    """

    _STATIC_OPTIONS: Dict[str, List[str]] = {
        "/ls": ["--all"],
    }

    def __init__(self, source: CommandSource):
        self._source = source
        self._name_lists = {
            "/cd": source.list_session_names,
            "/rm": source.list_rm_names,
            "/thinking": source.list_thinking_options,
            "/mode": source.list_model_options,
        }

    def get_completions(self, document, complete_event):
        """按光标前文本产出补全候选（非斜杠输入不产出任何候选）。"""
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        commands = self._source.available_commands()
        parts = text.split()
        num_parts = len(parts)

        if num_parts == 1 and not text.endswith(" "):
            word = parts[0].lower()
            for cmd, meta in commands.items():
                if cmd.startswith(word):
                    yield Completion(cmd[len(word):], start_position=0, display_meta=meta)
            return

        if num_parts >= 1:
            cmd = parts[0].lower()
            if cmd == "/skill" and cmd in commands:
                yield from self._skill_completions(text[len(parts[0]):].lstrip())
                return
            if cmd in self._name_lists:
                names = self._name_lists[cmd]()
                if num_parts == 1 and text.endswith(" "):
                    for name in names:
                        yield Completion(name, start_position=0)
                elif num_parts == 2 and not text.endswith(" "):
                    yield from self._prefix_completions(names, parts[1])
            elif num_parts == 1 and text.endswith(" "):
                for opt in self._STATIC_OPTIONS.get(cmd, []):
                    yield Completion(opt, start_position=0)

    @staticmethod
    def _prefix_completions(names: List[str], prefix: str):
        """名称候选的前缀补全；候选含 `/` 时仅替换（补全）最后一段。"""
        if "/" in prefix:
            slash_pos = prefix.rfind("/")
            parent_part = prefix[:slash_pos + 1]
            child_prefix = prefix[slash_pos + 1:]
            for name in names:
                if name.startswith(prefix) and "/" in name:
                    child_name = name[len(parent_part):]
                    if child_name.startswith(child_prefix):
                        yield Completion(child_name, start_position=-len(child_prefix))
        else:
            for name in names:
                if name.startswith(prefix):
                    yield Completion(name[len(prefix):], start_position=0)

    def _skill_completions(self, word: str):
        """技能层级补全：按当前层级逐步下钻（目录补尾 `/`，叶子裸名）。

        `word` 为 `/skill` 之后的参数部分（可空、可含空格）；层级分隔符 `/`，
        `\\` 兼容为 `/`。
        """
        tree = self._source.list_skill_tree()
        word = word.replace("\\", "/")
        segs = word.split("/")
        if segs and segs[-1] == "":
            prefix = ""
            segs = segs[:-1]
        else:
            prefix = segs[-1] if segs else ""
            segs = segs[:-1] if segs else []
        nodes = tree
        for seg in segs:
            nxt = None
            for node in nodes:
                if node.get("type") == "dir" and node["name"] == seg:
                    nxt = node["children"]
                    break
            if nxt is None:
                return
            nodes = nxt
        for node in nodes:
            name = node["name"]
            if not name.startswith(prefix):
                continue
            if name == prefix:
                # 名字已完整输入：目录 → 补 "/" 进入下一层（单技能目录也允许进入查看）；
                # 文件 → 无更多层级，不产出空补全（避免 Tab 后无任何可见反馈）
                if node.get("type") == "dir":
                    yield Completion("/", start_position=0, display_meta="进入目录")
                continue
            if node.get("type") == "dir" and not node.get("single"):
                yield Completion(name[len(prefix):] + "/", start_position=0,
                                 display_meta="目录")
            else:
                meta = "系统技能" if node.get("origin") == "system" else "项目技能"
                yield Completion(name[len(prefix):], start_position=0, display_meta=meta)
