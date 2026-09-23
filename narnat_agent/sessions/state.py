"""会话三态状态机 —— 游离态 / 根会话态 / 子会话态及其状态相关行为。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`
（「会话三态与命令可用性」「保存会话（/save）」「进入历史会话（/cd）」
「会话列表（/ls）」「探索分支创建（/explore）」「探索分支合并（/done）」
「退出语义（/exit）」「手动压缩的会话侧收尾（/compact）」）。

三态与命令可用集（命令可用性由状态类型决定）：
- `NoSession`（游离态）：`/rm`=删除会话、`/exit`=退出程序；无 `/explore`；
- `RootSession`（根会话态）：增加 `/explore`（创建探索分支），`/rm`=删除子会话、
  `/exit`=退出会话；
- `ChildSession`（子会话态）：无 `/save` `/rm` `/explore`；增加 `/done`
  （完成探索分支）、`/exit`=暂离探索分支。

结构（design D1/D7）：状态对象只持有状态专属数据与行为（`_name` / `_parent` /
`_status` / `_summary` / 合并基准等），共享资源（消息列表、会话存储、待删除标记、
颜色、总结函数）全经 `SessionManager` 的公开接口访问。状态方法返回错误文本
（空串 = 成功）——成功路径的用户可见文案由命令层（`commands.SessionCommands`）
输出。
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .store import format_session_summary, format_session_tree

if TYPE_CHECKING:  # 仅类型标注（manager 反向引用本模块）
    from .manager import SessionManager

__all__ = [
    "BOUNDARY_MARKER_PREFIX",
    "SUMMARY_TASK_TEMPLATE",
    "ChildSession",
    "NoSession",
    "RootSession",
    "SessionState",
]

# 历史会话的分支起点标记（`/done` 在父消息数基准缺失时的兜底识别依据；
# 当前版本的会话文件改用 parent_msg_count 字段，本标记只读、不写入）
BOUNDARY_MARKER_PREFIX = "━━━ 探索分支开始"

# 探索分支增量合并的总结提示词模板（占位符 `{memory}` / `{target}`）。
# 结构化检查点模板（对齐 COMPRESS_PROMPT 的固定小节骨架，见 config/defaults.py）：
# 固定小节顺序防止合并结论漏掉续跑最关键的信息；每条小节只写相对主分支的增量。
SUMMARY_TASK_TEMPLATE = """# 任务：探索分支增量合并

你现在是压缩引擎。把子分支的探索过程浓缩成一个结构化检查点，只将**有价值的增量信息**合并回主分支，使另一个模型可以无损接续工作，同时保持主会话简洁。

## 1. 主分支当前状态
这是主分支已知的上下文，视为“基准真理”，增量判定以它为基准。
{memory}

## 2. 子分支探索日志
这是在子分支中进行的调试、验证、试错过程。其中若包含主分支已知的内容（尤其是与上方摘要重复的部分），视为已知，不计入增量。
{target}

## 3. 输出结构
严格按下面的 Markdown 结构输出：保持每一节的顺序，用简练的要点而非散文。空节写“(无)”，不得省略任何一节。
除“原始请求与意图”外，每节只记录**相对主分支已知信息的新增量**：与主分支重复的内容一律不写；主分支已记录的事实不要原文照抄，只写新增、变化或修正的部分。

## 原始请求与意图
- [子分支本次探索的目标（即使与主分支相同也写明，供接续定位）；措辞重要处原文引用]

## 关键技术概念
- [本次探索新涉及的技术、框架、模式与约定]

## 文件与代码
- [精确路径：其重要性、关键改动或代码片段]

## 错误与修复
- [错误：如何解决的，以及相关用户反馈；失败的尝试一句话带过（如“排除了方案A，因为…”）]

## 待办任务
- [本次探索暴露的、尚未完成的工作]

## 当前工作
- [子分支结束时正在进行的精确工作]

## 下一步
- [与主分支衔接的单一动作，或“(无)”]

## 关键上下文
- [决策及其理由、约束、用户偏好、未决问题、继续所需的数据]

规则：
- 用简洁的中文工程语言。保留精确的文件路径、命令、报错串、标识符、数值、函数签名与语法片段。
- 忠实记录用户反馈与明确指令，尤其是纠正。
- 不要提及本次合并请求，也不要提及这是子分支的探索过程。
- 只输出检查点文本：不要调用任何工具或采取其他行动。
- 总长不超过 1500 字：宁可少写，不可废话。
- 若所有小节均为“(无)”——即探索没有产生任何新结论、只是重复或确认了主分支已知信息——则整段输出仅一行：`[无实质性更新]`（不要输出任何小节）。
"""


class SessionState:
    """会话状态基类 —— 状态数据接口 + 三态共用的行为（/cd、/ls）。

    子类只实现自己状态可用的命令（`/save` `/rm` `/explore` `/done`）与状态数据
    访问；不在当前状态可用集内的命令由命令层按未知命令拦截，基类的默认返回
    （「当前状态不可用 …」）仅为兜底。
    """

    def __init__(self, mgr: "SessionManager") -> None:
        self._mgr = mgr

    # ── 状态数据（子类覆盖）──

    def available_commands(self) -> dict[str, str]:
        """当前状态的可用命令表（命令 → 说明），同时是 Tab 补全的数据源。"""
        raise NotImplementedError

    def session_name(self) -> Optional[str]:
        """当前会话名（游离态为 None）。"""
        return None

    def session_parent(self) -> Optional[str]:
        """当前会话的父会话名（根会话与游离态为 None）。"""
        return None

    def is_child(self) -> bool:
        """是否子会话态。"""
        return False

    # ── 行为：默认兜底（命令层已按可用集过滤）──

    def save(self, name: str) -> str:
        """保存会话；返回错误文本（空串 = 成功）。"""
        return "当前状态不可用 /save"

    def delete(self, name: str) -> str:
        """标记待删除；返回错误文本（空串 = 成功）。"""
        return "当前状态不可用 /rm"

    def explore(self, name: str) -> str:
        """创建探索分支；返回错误文本（空串 = 成功）。"""
        return "当前状态不可用 /explore"

    def done(self) -> str:
        """合并探索分支结论；返回错误文本（空串 = 成功）。"""
        return "当前状态不可用 /done"

    # ── 行为：会话切换（/cd）──

    def enter(self, name: str) -> str:
        """/cd <名称>：进入历史会话（根/子会话先刷新落盘当前会话）。

        解析名称 → 加载目标内容 → 替换当前对话 → 切换到对应状态。加载失败
        原样返回错误（如「会话不存在: {名称}」）。落盘错误忽略（兼容怪癖保持：
        先落盘路径不因落盘失败中断切换）。
        """
        if not name:
            return "[错误: 请指定会话名称]"
        if self.session_name() is not None:
            self.persist()
        target_name, target_parent, err = self._mgr.resolve_name(name)
        if err:
            return err
        if target_parent is not None:
            new_msgs, load_err = self._mgr.store.load(target_name, parent=target_parent)
        else:
            new_msgs, load_err = self._mgr.store.load(target_name)
        if load_err:
            return load_err
        self._mgr.replace_messages(new_msgs)
        if target_parent is not None:
            self._mgr.switch_state(self._mgr.create_child(target_name, target_parent))
        else:
            self._mgr.switch_state(self._mgr.create_root(target_name))
        return ""

    # ── 行为：会话列表（/ls）──

    def show(self, args: str = "") -> str:
        """会话列表文本（`--all` 全量树形，否则精简列表）。

        文本为已着色形态：当前会话标记「◀ 当前」用高亮色、待删除标记
        「✘ 退出后删除」用警示色（后者仅在存在待删除标记时着色——着色是字面
        文本替换，会话名本身含标记字面量时会被误着色，兼容怪癖保持）。
        列表为空（含游离态）时返回空串，由命令层输出「(无已保存会话)」。
        """
        tree = self._mgr.store.list_tree()
        self._mgr.apply_delete_marks(tree)
        if args == "--all":
            result = format_session_tree(tree, self.session_name(), self.session_parent())
        else:
            result = format_session_summary(tree, self.session_name(), self.session_parent())
        if not result:
            return ""
        colors = self._mgr.theme
        result = result.replace("◀ 当前", f"{colors.cmd_highlight}◀ 当前{colors.r}")
        if self._mgr.pending_deletes:
            result = result.replace("✘ 退出后删除", f"{colors.cmd_error}✘ 退出后删除{colors.r}")
        return result

    # ── 行为：状态退出（/exit）──

    def exit(self) -> tuple[str, Optional["SessionState"]]:
        """执行 /exit。返回 `(消息, 新状态)`；新状态为 None 表示进程退出。"""
        raise NotImplementedError

    # ── 行为：持久化与基准 ──

    def persist(self) -> str:
        """把当前会话落盘（游离态 no-op）；返回错误文本（空串 = 成功）。"""
        return ""

    def reset_after_compact(self) -> None:
        """上下文压缩后修正子会话的增量合并基准（默认无基准可修正）。"""
        return None

    # ── 行为：/rm 的 Tab 补全候选 ──

    def deletable_names(self) -> list[str]:
        """当前状态下可删除且尚未标记的会话名（/rm 补全候选）。"""
        return []


class NoSession(SessionState):
    """游离态 —— 尚无会话；/save 创建根会话、/rm 可标记任意会话、/exit 退出程序。"""

    def available_commands(self) -> dict[str, str]:
        """游离态命令集（无 /explore；/rm=删除会话、/exit=退出程序）。"""
        return {
            "/clear": "清理屏幕",
            "/compact": "压缩上下文",
            "/save": "保存当前会话",
            "/ls": "显示所有会话",
            "/cd": "进入历史会话",
            "/rm": "删除会话",
            "/skill": "加载技能",
            "/thinking": "切换思考强度",
            "/thinkback": "思考回传开关",
            "/mode": "切换模型",
            "/goal": "目标模式开关",
            "/exit": "退出程序",
        }

    def save(self, name: str) -> str:
        """/save：把当前对话保存为新根会话并进入根会话态。

        无用户消息 →「没有对话内容，无需保存」；空名由模型自动命名，未获得名称
        →「自动命名失败，请手动指定: /save <名称>」；保存出错原样返回且不切换状态。
        """
        msgs = self._mgr.message_list()
        if not any(m.get("role") == "user" for m in msgs):
            return "没有对话内容，无需保存"
        if not name:
            name = self._mgr.name_session(msgs)
            if not name:
                return "自动命名失败，请手动指定: /save <名称>"
        err = self._mgr.store.save(name, msgs)
        if err:
            return err
        self._mgr.switch_state(self._mgr.create_root(name))
        return ""

    def delete(self, name: str) -> str:
        """/rm <名称|--all>：记录待删除标记（退出时执行）。

        游离态可标记任意会话：标记根会话时连带其全部子会话；`--all` 标记全部。
        """
        if not name:
            return "[错误: 请指定会话名称]"
        if name == "--all":
            for root in self._mgr.store.list_tree():
                self._mgr.pending_deletes.add((root["name"], None))
                for child in root.get("children", []):
                    self._mgr.pending_deletes.add((child["name"], root["name"]))
            return ""
        target_name, target_parent, err = self._mgr.resolve_name(name)
        if err:
            return err
        self._mgr.pending_deletes.add((target_name, target_parent))
        if target_parent is None:
            for root in self._mgr.store.list_tree():
                if root["name"] == target_name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], target_name))
                    break
        return ""

    def deletable_names(self) -> list[str]:
        """游离态：全部会话名（跳过已标记的）。"""
        result = []
        for name in self._mgr.list_session_names():
            if "/" in name:
                parent, child = name.split("/", 1)
                if (child, parent) in self._mgr.pending_deletes:
                    continue
            elif (name, None) in self._mgr.pending_deletes:
                continue
            result.append(name)
        return result

    def exit(self) -> tuple[str, Optional[SessionState]]:
        """游离态 /exit = 退出程序（新状态 None 表示进程退出）。"""
        return "", None


class RootSession(SessionState):
    """根会话 —— /save 刷新或另存、/rm 只删子会话、/explore 创建分支、/exit 回游离态。"""

    def __init__(self, mgr: "SessionManager", name: str) -> None:
        super().__init__(mgr)
        self._name = name
        self._status: Optional[str] = None
        self._summary: Optional[str] = None
        self._msg_count = len(mgr.message_list())

    def available_commands(self) -> dict[str, str]:
        """根会话态命令集（游离态命令 + /explore；/rm=删除子会话、/exit=退出会话）。"""
        return {
            "/clear": "清理屏幕",
            "/compact": "压缩上下文",
            "/save": "保存当前会话",
            "/ls": "显示所有会话",
            "/cd": "进入历史会话",
            "/rm": "删除子会话",
            "/skill": "加载技能",
            "/thinking": "切换思考强度",
            "/thinkback": "思考回传开关",
            "/mode": "切换模型",
            "/goal": "目标模式开关",
            "/explore": "创建探索分支",
            "/exit": "退出会话",
        }

    def session_name(self) -> Optional[str]:
        """会话名（根会话持久化的名称）。"""
        return self._name

    def persist(self) -> str:
        """刷新当前会话落盘；消息条数增长时把探索分支状态翻回 active。"""
        msgs = self._mgr.message_list()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        err = self._mgr.store.save(self._name, msgs,
                                   status=self._status or "active",
                                   summary=self._summary)
        self._msg_count = len(msgs)
        return err

    def save(self, name: str) -> str:
        """/save：空名或同名 → 刷新落盘；新名称 → 另存为新会话并切入。

        另存不检查目标是否已存在（同名文件被覆盖，兼容怪癖保持）；保存出错原样
        返回且不切换状态。
        """
        if not name or name == self._name:
            return self.persist()
        msgs = self._mgr.message_list()
        err = self._mgr.store.save(name, msgs)
        if err:
            return err
        self._mgr.switch_state(self._mgr.create_root(name))
        return ""

    def delete(self, name: str) -> str:
        """/rm：只标记当前会话的子会话；`--all` 标记当前会话的全部子会话。

        输入含 "/" 时按 父/子 拆分且父名必须为当前会话；目标不是当前会话的子会话
        →「'{名称}' 不是当前会话的子会话」；当前会话不在会话树中 →
        「会话不存在: {名称}」（`--all` 分支返回「会话不存在」）。
        """
        if not name:
            return "[错误: 请指定会话名称]"
        if name == "--all":
            for root in self._mgr.store.list_tree():
                if root["name"] == self._name:
                    for child in root.get("children", []):
                        self._mgr.pending_deletes.add((child["name"], self._name))
                    return ""
            return "会话不存在"
        if "/" in name:
            parent_name, child_name = name.split("/", 1)
            if parent_name != self._name:
                return f"'{name}' 不是当前会话的子会话"
            name = child_name
        for root in self._mgr.store.list_tree():
            if root["name"] == self._name:
                for child in root.get("children", []):
                    if child["name"] == name:
                        self._mgr.pending_deletes.add((name, self._name))
                        return ""
                return f"'{name}' 不是当前会话的子会话"
        return f"会话不存在: {name}"

    def explore(self, name: str) -> str:
        """/explore <名称>：创建探索分支（复制父会话消息为分支初值）。

        先落盘父会话（错误忽略，兼容怪癖保持）；分支以状态 new 持久化并记录
        父消息数作为增量合并基准（同时作为已合并位置初值）；读回分支内容后进入
        子会话态。空名 →「[错误: 请指定分支名称]」；与父同名 →
        「[错误: 分支名不可与父会话同名（'{名称}'），请换一个名称]」。
        """
        if not name:
            return "[错误: 请指定分支名称]"
        if name == self._name:
            return f"[错误: 分支名不可与父会话同名（'{name}'），请换一个名称]"
        self.persist()
        msgs = [dict(m) for m in self._mgr.message_list()]
        parent_msg_count = len(msgs)
        self._mgr.store.save(name, msgs, parent=self._name, status="new",
                             parent_msg_count=parent_msg_count,
                             last_summarized_at=parent_msg_count)
        new_msgs, err = self._mgr.store.load(name, parent=self._name)
        if err:
            return err
        self._mgr.replace_messages(new_msgs)
        self._mgr.switch_state(self._mgr.create_child(name, self._name))
        return ""

    def deletable_names(self) -> list[str]:
        """根会话态：仅当前会话的子会话（「父/子」形式，跳过已标记的）。"""
        for root in self._mgr.store.list_tree():
            if root["name"] == self._name:
                return [f"{root['name']}/{child['name']}"
                        for child in root.get("children", [])
                        if (child["name"], self._name) not in self._mgr.pending_deletes]
        return []

    def exit(self) -> tuple[str, Optional[SessionState]]:
        """根会话 /exit = 回到游离态（进程不结束，由命令层据状态判定）。"""
        return "", NoSession(self._mgr)


class ChildSession(SessionState):
    """子会话 —— /done 合并结论、/exit 暂离、/cd 去其它会话；无 /save /rm /explore。"""

    def __init__(self, mgr: "SessionManager", name: str, parent: str) -> None:
        super().__init__(mgr)
        self._name = name
        self._parent = parent
        meta = mgr.store.load_meta(name, parent=parent)
        self._status: str = meta.get("status", "active")
        self._summary: Optional[str] = meta.get("summary")
        self._parent_msg_count: int = meta.get("parent_msg_count") or 0
        self._last_summarized_at: int = (meta.get("last_summarized_at")
                                         or self._parent_msg_count)
        self._msg_count = len(mgr.message_list())

    def available_commands(self) -> dict[str, str]:
        """子会话态命令集（无 /save /rm /explore；有 /done 与 /exit=暂离探索分支）。"""
        return {
            "/clear": "清理屏幕",
            "/compact": "压缩上下文",
            "/ls": "显示所有会话",
            "/cd": "进入历史会话",
            "/skill": "加载技能",
            "/thinking": "切换思考强度",
            "/thinkback": "思考回传开关",
            "/mode": "切换模型",
            "/goal": "目标模式开关",
            "/done": "完成探索分支",
            "/exit": "暂离探索分支",
        }

    def session_name(self) -> Optional[str]:
        """分支名。"""
        return self._name

    def session_parent(self) -> Optional[str]:
        """父会话名。"""
        return self._parent

    def is_child(self) -> bool:
        """子会话态标记（True）。"""
        return True

    def persist(self) -> str:
        """刷新当前分支落盘（含合并基准与已合并位置）；条数增长时翻回 active。"""
        msgs = self._mgr.message_list()
        if len(msgs) > self._msg_count and self._status in ("new", "completed"):
            self._status = "active"
        err = self._mgr.store.save(self._name, msgs, parent=self._parent,
                                   status=self._status or "active",
                                   summary=self._summary,
                                   parent_msg_count=self._parent_msg_count,
                                   last_summarized_at=self._last_summarized_at)
        self._msg_count = len(msgs)
        return err

    def done(self) -> str:
        """/done：把分支增量讨论交给模型总结为结构化结论并合并回父会话。

        增量区间为「已合并位置之后」的新讨论，主分支基准之前的内容作为背景素材；
        无新讨论 →「没有新的讨论内容需要总结」；总结取消或失败 →
        「总结取消或失败」（不改动任何状态）；父会话加载失败 →
        「无法加载父会话: {错误}」。合并时父会话末尾追加 system 消息
        「# 子会话 [{名称}]{轮次标签} 结论」（轮次标签为「(第N轮)」，首轮无标签），
        分支状态置 completed 并记录已合并位置与总结；完成后当前对话替换为父会话
        内容、切回父的根会话态。
        """
        if self._status == "completed":
            return "该探索分支已完成，不可重复 /done"
        msgs = list(self._mgr.message_list())
        parent_msg_count = self._parent_msg_count
        if parent_msg_count == 0:
            for i, m in enumerate(msgs):
                if (m.get("role") == "system"
                        and BOUNDARY_MARKER_PREFIX in m.get("content", "")):
                    parent_msg_count = i
                    break
        last_summarized_at = self._last_summarized_at
        if last_summarized_at < parent_msg_count:
            last_summarized_at = parent_msg_count
        memory = msgs[:parent_msg_count]
        target = msgs[last_summarized_at:]
        if not target:
            return "没有新的讨论内容需要总结"
        task_content = SUMMARY_TASK_TEMPLATE.format(
            memory=_format_messages_text(memory),
            target=_format_messages_text(target),
        )
        summary = self._mgr.summarize([{"role": "user", "content": task_content}])
        if not summary:
            return "总结取消或失败"
        parent_msgs, err = self._mgr.store.load(self._parent)
        if err:
            return f"无法加载父会话: {err}"
        round_num = sum(1 for m in parent_msgs
                        if m.get("role") == "system"
                        and f"子会话 [{self._name}]" in m.get("content", "")) + 1
        round_label = f" (第{round_num}轮)" if round_num > 1 else ""
        parent_msgs.append({"role": "system",
                            "content": f"# 子会话 [{self._name}]{round_label} 结论\n\n{summary}"})
        self._mgr.store.save(self._parent, parent_msgs)
        self._last_summarized_at = len(msgs)
        self._status = "completed"
        self._summary = summary
        # 从磁盘重读分支内容再落盘（读取错误忽略；文件缺失时以空内容覆盖分支
        # 文件——兼容怪癖保持）
        child_msgs, _err = self._mgr.store.load(self._name, parent=self._parent)
        self._mgr.store.save(self._name, child_msgs, parent=self._parent,
                             status="completed", summary=summary,
                             parent_msg_count=self._parent_msg_count,
                             last_summarized_at=self._last_summarized_at)
        self._mgr.replace_messages(parent_msgs)
        self._mgr.switch_state(self._mgr.create_root(self._parent))
        return ""

    def exit(self) -> tuple[str, Optional[SessionState]]:
        """子会话 /exit = 暂离：加载父会话内容并回到父的根会话态。

        父会话加载失败 → 返回错误且停留在子会话态（新状态 None，由命令层据
        状态类型判定不退出进程）。
        """
        parent_msgs, err = self._mgr.store.load(self._parent)
        if err:
            return err, None
        self._mgr.replace_messages(parent_msgs)
        return "", self._mgr.create_root(self._parent)

    def reset_after_compact(self) -> None:
        """压缩后重设增量合并基准。

        压缩把消息列表整体替换为 [前导 system…, 摘要, 保留尾部…]，
        而合并基准与已合并位置是压缩前的消息下标，失配会让 /done 静默取不到增量
        （或把分支讨论计入父基准）。重置到摘要边界：memory = 压缩摘要（含父基准
        与早期分支内容），target = 压缩后逐字保留的分支近期讨论。
        """
        msgs = self._mgr.message_list()
        boundary = 0
        while boundary < len(msgs) and msgs[boundary].get("role") == "system":
            boundary += 1
        self._parent_msg_count = boundary
        self._last_summarized_at = boundary


def _format_messages_text(messages: list) -> str:
    """把消息列表渲染为合并提示词用的纯文本（用户：/AI：/工具返回：/系统：）。

    assistant 无 content 时不输出行（工具调用轮不产生文本行）。
    """
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            if content:
                lines.append(f"AI：{content}")
        elif role == "tool":
            tc_id = m.get("tool_call_id", "")
            label = f"工具返回 [{tc_id}]" if tc_id else "工具返回"
            lines.append(f"{label}：{content}")
        elif role == "system":
            lines.append(f"系统：{content}")
    return "\n".join(lines)
