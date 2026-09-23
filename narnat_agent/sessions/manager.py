"""会话服务层 —— 三态状态机的宿主：共享资源、状态切换、命令转发与生命周期。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`（「会话名解析」
「延迟删除执行与展示标记」「自动保存与退出保存」及各命令 Requirement）与
`specs/app`（「交互主循环」的命令分发三态、「退出清理」）。

结构（design D1/D7/D8）：
- 本类只持有共享资源（会话存储、消息历史、待删除标记、目标模式、配置句柄、
  回调端口）并提供公开 API；状态对象（`state.py`）与命令层（`commands.py`）
  经这些公开 API 协作，无跨对象私有访问；
- 全部依赖构造注入：会话存储、消息历史（messages 积木的唯一所有者）、目标模式
  （`GoalMode`）、思考/模型配置句柄（`ModelState`）、着色端口（`ThemePort`）、
  合并动画端口（`contracts.output.Animator`）、模型命名与总结函数、取消检查、
  手动压缩（`compression` 积木的 `compress_manual`，构造注入替代现状的后置赋值）。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Optional

from ..config.models import AIConfig
from ..config.skill_store import list_skill_tree, load_skill
from ..messages import MessageStore
from .commands import CommandReply, SessionCommands
from .goal import GoalMode
from .model_state import ModelState
from .state import ChildSession, NoSession, RootSession, SessionState
from .store import SessionStore
from .theme import PlainColors, ThemePort

__all__ = ["AUTO_SAVE_WAIT_SECONDS", "SessionManager"]

# 自动保存的后台命名等待上限（秒）：主线程在下一轮输入前、退出流程收尾时的等待
AUTO_SAVE_WAIT_SECONDS = 5.0


class SessionManager:
    """会话管理器 —— 状态机宿主与对外 API（app / ui / conversation 消费）。

    构造注入（design D8，全部为构造期依赖，无后置补线）：
    - `store`：会话持久化（目录布局、读写、枚举）；
    - `messages`：消息历史唯一所有者（messages 积木）；
    - `goal`：目标模式状态（缺省为空状态，无工具注入端口）；
    - `model_state`：思考/模型配置句柄（缺省为不回写配置的空句柄）；
    - `theme`：命令输出着色端口（缺省无色，拼出裸文本）；
    - `animator`：会话合并动画端口（缺省无动画）；
    - `name_func` / `summarize_func`：模型命名与模型总结（缺省返回空串）；
    - `cancel_check`：取消查询（总结/命名期间的用户中断判定）；
    - `compact_func`：手动压缩（`compression` 积木的 `compress_manual`）；
    - `auto_save_enabled` / `auto_save_tokens`：自动保存开关与输入 token 门槛；
    - `skill_roots` / `skill_ignore_dirs`：项目技能根目录与扫描忽略目录。
    """

    def __init__(
        self,
        store: SessionStore,
        messages: MessageStore,
        *,
        goal: Optional[GoalMode] = None,
        model_state: Optional[ModelState] = None,
        theme: Optional[ThemePort] = None,
        animator: Any = None,
        name_func: Optional[Callable[[list[dict[str, Any]]], str]] = None,
        summarize_func: Optional[Callable[[list[dict[str, Any]], Callable[[], bool]], str]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        compact_func: Optional[Callable[[], tuple[str, str]]] = None,
        auto_save_enabled: bool = False,
        auto_save_tokens: int = 0,
        skill_roots: Optional[tuple] = None,
        skill_ignore_dirs: tuple = (),
    ) -> None:
        self._store = store
        self._messages = messages
        self._goal = goal if goal is not None else GoalMode()
        self._model_state = (model_state if model_state is not None
                             else ModelState(AIConfig()))
        self._theme: ThemePort = theme if theme is not None else PlainColors()
        self._animator = animator
        self._name_func = name_func
        self._summarize_func = summarize_func
        self._cancel_check = cancel_check or (lambda: False)
        self._compact_func = compact_func
        self._auto_save_enabled = bool(auto_save_enabled)
        self._auto_save_tokens = int(auto_save_tokens or 0)
        self._skill_roots = skill_roots
        self._skill_ignore_dirs = tuple(skill_ignore_dirs or ())
        self._pending_deletes: set[tuple[str, Optional[str]]] = set()
        self._state: SessionState = NoSession(self)
        self._auto_save_started = False
        self._pending_auto_save_name: Optional[str] = None
        self._auto_save_thread: Optional[threading.Thread] = None
        self._commands = SessionCommands(self)

    # ═══════════════════════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════════════════════

    @property
    def state(self) -> SessionState:
        """当前会话状态（三态之一）。"""
        return self._state

    @property
    def store(self) -> SessionStore:
        """会话持久化门面。"""
        return self._store

    @property
    def goal(self) -> GoalMode:
        """目标模式状态（conversation / app 经此读取与设置）。"""
        return self._goal

    @property
    def model_state(self) -> ModelState:
        """思考/模型配置句柄。"""
        return self._model_state

    @property
    def theme(self) -> ThemePort:
        """命令输出着色端口。"""
        return self._theme

    @property
    def pending_deletes(self) -> set[tuple[str, Optional[str]]]:
        """待删除标记集合（`(名称, 父名)`；`/rm` 写入、退出清理执行）。"""
        return self._pending_deletes

    @property
    def commands(self) -> SessionCommands:
        """命令层（命令表、分发与候选项数据源）。"""
        return self._commands

    def message_list(self) -> list[dict[str, Any]]:
        """当前对话消息的浅拷贝列表（供状态层读取与保存）。"""
        return self._messages.view().to_list()

    def session_name(self) -> Optional[str]:
        """当前会话名（游离态为 None）。"""
        return self._state.session_name()

    def session_parent(self) -> Optional[str]:
        """当前会话的父会话名（根会话与游离态为 None）。"""
        return self._state.session_parent()

    def is_child_session(self) -> bool:
        """当前是否子会话态。"""
        return self._state.is_child()

    def should_exit_agent(self) -> bool:
        """退出语义判定：当前处于游离态（`/exit` 应结束进程）。"""
        return isinstance(self._state, NoSession)

    def available_commands(self) -> dict[str, str]:
        """当前状态的可用命令表（分发校验与 Tab 补全的数据源）。"""
        return self._state.available_commands()

    def dispatch(self, text: str) -> CommandReply:
        """命令分发（转发命令层）。"""
        return self._commands.dispatch(text)

    # ═══════════════════════════════════════════════════════════
    # 状态切换与消息
    # ═══════════════════════════════════════════════════════════

    def replace_messages(self, new_msgs: list[dict[str, Any]]) -> None:
        """以目标会话内容整体替换当前对话（会话切换与结论合并后）。"""
        self._messages.replace_all(new_msgs)

    def switch_state(self, new_state: SessionState) -> None:
        """切换当前会话状态。"""
        self._state = new_state

    def create_root(self, name: str) -> RootSession:
        """构造根会话状态。"""
        return RootSession(self, name)

    def create_child(self, name: str, parent: str) -> ChildSession:
        """构造子会话状态。"""
        return ChildSession(self, name, parent)

    def exit_session(self) -> tuple[str, bool]:
        """执行 `/exit`：切换状态并返回 `(消息, 是否退出进程)`。

        退出判定取切换后的状态（游离态 = 退出程序）；子会话暂离失败时状态保持
        子会话态、不退出进程。
        """
        msg, new_state = self._state.exit()
        if new_state is not None:
            self.switch_state(new_state)
        return msg, self.should_exit_agent()

    # ═══════════════════════════════════════════════════════════
    # 名称解析与延迟删除
    # ═══════════════════════════════════════════════════════════

    def resolve_name(self, name: str) -> tuple[Optional[str], Optional[str], str]:
        """解析会话名。返回 `(目标名, 父名, 错误文本)`。

        固定优先级（spec「会话名解析」）：① 完整名匹配（根会话名本身，或
        「父/子」全路径）；② 名称含 "/" 时直接按 父/子 拆分返回（不校验存在性，
        兼容怪癖保持）；③ 裸名在全部子会话中唯一匹配——多个同名时列出全部全路径。
        """
        tree = self._store.list_tree()
        for root in tree:
            if root["name"] == name:
                return root["name"], None, ""
            for child in root.get("children", []):
                if f"{root['name']}/{child['name']}" == name:
                    return child["name"], root["name"], ""
        if "/" in name:
            parent, child = name.split("/", 1)
            return child, parent, ""
        matches = []
        for root in tree:
            for child in root.get("children", []):
                if child["name"] == name:
                    matches.append((child["name"], root["name"]))
        if len(matches) == 1:
            return matches[0][0], matches[0][1], ""
        if len(matches) > 1:
            paths = "\n".join(
                f"      {c}/{p}" if c else f"      {p}" for p, c in matches
            )
            return None, None, f"'{name}' 有多个，请用完整路径指定：\n{paths}"
        return None, None, f"会话不存在: {name}"

    def apply_delete_marks(self, tree: list[dict[str, Any]]) -> None:
        """给会话树节点注入待删除标记（`_delete_marked`，供列表渲染消费）。"""
        for root in tree:
            if (root["name"], None) in self._pending_deletes:
                root["_delete_marked"] = True
            for child in root.get("children", []):
                if (child["name"], root["name"]) in self._pending_deletes:
                    child["_delete_marked"] = True

    def cleanup_deletes(self) -> None:
        """执行全部待删除标记：先删子会话、再删根会话（连带其子目录）；清空标记。"""
        children = [(n, p) for n, p in self._pending_deletes if p is not None]
        roots = [(n, p) for n, p in self._pending_deletes if p is None]
        for name, parent in children:
            self._store.delete(name, parent=parent)
        for name, parent in roots:
            self._store.delete(name, parent=parent)
        self._pending_deletes.clear()

    # ═══════════════════════════════════════════════════════════
    # 会话生命周期（轮末落盘 / 自动保存 / 退出清理）
    # ═══════════════════════════════════════════════════════════

    def persist_current(self) -> None:
        """轮末落盘当前会话（游离态 no-op；落盘错误忽略）。

        由 app 主循环在每轮正常结束时调用（现状 `on_auto_save`）。
        """
        self._state.persist()

    def maybe_auto_save(self, input_tokens: int) -> None:
        """自动保存门槛判定与后台命名落盘（app 主循环在轮末调用）。

        四道门槛（spec「自动保存与退出保存」）：配置开启、本进程尚未发起过、
        当前处于游离态、最近一轮输入 token 量大于门槛（门槛 ≤ 0 时不限）。
        通过后立即置「已发起」标志（无论后续命名成功与否不再重试），后台线程
        命名成功后写盘；名称由 `wait_auto_save` 收尾时切换为根会话态。
        """
        if not self._auto_save_enabled:
            return
        if self._auto_save_started:
            return
        if self._state.session_name() is not None:
            return
        if self._auto_save_tokens > 0 and input_tokens <= self._auto_save_tokens:
            return
        self._auto_save_started = True
        thread = threading.Thread(target=self._auto_save_worker, daemon=True)
        self._auto_save_thread = thread
        thread.start()

    def wait_auto_save(self) -> None:
        """自动保存收尾等待（至多 `AUTO_SAVE_WAIT_SECONDS` 秒）。

        app 在主循环下一轮输入前调用；取得名称时切换到该根会话态。
        """
        thread = self._auto_save_thread
        if thread is not None:
            thread.join(timeout=AUTO_SAVE_WAIT_SECONDS)
            self._auto_save_thread = None
        name = self._pending_auto_save_name
        if name:
            self._pending_auto_save_name = None
            self.switch_state(self.create_root(name))

    def exit_cleanup(self) -> str:
        """退出程序清理：等自动保存收尾、落盘当前会话、执行全部待删标记。

        返回用户提示文本（当前会话已有名称时为「会话已自动保存: {名称}」，
        否则空串）——输出归调用方（app 主循环）。
        """
        self.wait_auto_save()
        text = ""
        name = self._state.session_name()
        if name:
            self._state.persist()
            text = f"会话已自动保存: {name}"
        self.cleanup_deletes()
        return text

    # ═══════════════════════════════════════════════════════════
    # 全局命令支撑（/skill /thinking /thinkback /mode /compact）
    # ═══════════════════════════════════════════════════════════

    def load_skill(self, name: str) -> str:
        """加载技能并注入当前对话（system 消息）。返回错误文本（空串 = 成功）。

        命中时正文前附「本技能目录: {目录}」行与相对路径解析说明（技能正文中的
        相对路径以该目录为基准）。
        """
        content, err, path = load_skill(self._store.narnat_dir, name,
                                        project_roots=self._skill_roots,
                                        ignore_dirs=self._skill_ignore_dirs)
        if err:
            return err
        if path:
            content = (f"本技能目录: {os.path.dirname(path)}\n"
                       "技能正文里提到的相对路径，请按上面的目录解析；引用的文件按需读取。\n\n"
                       f"{content}")
        self._messages.append_system(content)
        return ""

    def thinking(self, value: str) -> str:
        """`/thinking`：无值查询当前思考强度，有值切换并写回配置（失败静默）。"""
        options = self._model_state.thinking_options
        if not value:
            current = self._model_state.thinking_effort
            return f"当前思考强度: {options.get(current, current)}"
        effort = value.strip().lower()
        if effort not in options:
            return f"无效值: {effort}（可用: {' / '.join(options.keys())}）"
        self._model_state.set_thinking_effort(effort)
        return f"思考强度已切换为: {options[effort]}"

    def thinkback(self, action: str) -> str:
        """`/thinkback`：无值查询回传开关，有值（on/off）切换并写回配置。"""
        current = self._model_state.thinking_passback
        act = action.strip().lower()
        if not act:
            state = "开" if current else "关"
            return f"思考回传: {state}（/thinkback on|off 切换）"
        if act in ("on", "off"):
            target = act == "on"
            self._model_state.set_thinking_passback(target)
            if current == target:
                return f"思考回传已是{'开启' if target else '关闭'}状态"
            return f"思考回传已{'开启' if target else '关闭'}"
        return f"无效值: {act}（可用: on / off）"

    def switch_model(self, name: str) -> str:
        """`/mode`：无值查询当前模型，有值与模型列表做大小写不敏感匹配并切换。"""
        options = self._model_state.model_options
        if not name:
            return f"当前模型: {self._model_state.model}"
        target = name.strip()
        matched = next((opt for opt in options if opt.lower() == target.lower()), None)
        if matched is None:
            return f"无效值: {target}（可用: {' / '.join(options)}）"
        self._model_state.set_model(matched)
        return f"设置成功：{matched}"

    def compact(self) -> tuple[str, str]:
        """`/compact`：转发手动压缩，成功时先重建基准、再落盘当前会话。

        返回 `(status, text)`：status = `ok` / `empty` / `error`（压缩积木口径）。
        手动压缩不经过轮末自动保存，不落盘则压缩结果会在退出时丢失。
        """
        if self._compact_func is None:
            return "error", "压缩不可用"
        status, text = self._compact_func()
        if status == "ok":
            self._state.reset_after_compact()
            self._state.persist()
        return status, text

    def summarize(self, messages: list[dict[str, Any]]) -> str:
        """运行会话总结（合并动画 + 注入的总结函数 + 取消检查）。"""
        if self._animator is not None:
            self._animator.begin_summarizing()
        summary = ""
        if self._summarize_func is not None:
            summary = self._summarize_func(messages, self._cancel_check)
        if self._animator is not None:
            self._animator.end_summarizing()
        return summary

    def name_session(self, messages: list[dict[str, Any]]) -> str:
        """模型命名会话（未注入命名函数时返回空串）。"""
        if self._name_func is None:
            return ""
        return self._name_func(messages)

    # ═══════════════════════════════════════════════════════════
    # Tab 补全数据源
    # ═══════════════════════════════════════════════════════════

    def list_session_names(self) -> list[str]:
        """全部会话名（根名 + 「父/子」全路径；`/cd` 补全候选）。"""
        names = []
        for root in self._store.list_tree():
            names.append(root["name"])
            for child in root.get("children", []):
                names.append(f"{root['name']}/{child['name']}")
        return names

    def list_rm_names(self) -> list[str]:
        """`/rm` 补全候选（当前状态下可删除且尚未标记的会话名）。"""
        return self._state.deletable_names()

    def list_thinking_options(self) -> list[str]:
        """思考强度候选（`/thinking` 补全）。"""
        return list(self._model_state.thinking_options.keys())

    def list_model_options(self) -> list[str]:
        """模型候选（`/mode` 补全）。"""
        return self._model_state.model_options

    def list_skill_tree(self) -> list:
        """技能树（`/skill` 层级补全的数据源）。"""
        return list_skill_tree(self._store.narnat_dir,
                               project_roots=self._skill_roots,
                               ignore_dirs=self._skill_ignore_dirs)

    # ═══════════════════════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════════════════════

    def _auto_save_worker(self) -> None:
        """后台自动保存线程：模型命名 → 写盘（不碰状态切换，主线程收尾时切换）。"""
        name = self.name_session(self.message_list())
        if not name:
            return
        self._store.save(name, self.message_list())
        self._pending_auto_save_name = name
