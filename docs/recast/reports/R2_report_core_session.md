# R2 现状报告：core 会话与支撑（会话状态机 / 消息管理 / 工具调度 / 统计）

调研范围：`narnat_agent/core/` 下 8 个文件（session_callbacks、tool_dispatcher、message_manager、stats、billing、message_list、auto_save_manager、tool_callbacks）。

调研方法：逐文件完整通读（Read 全文），跨文件"被依赖"用 Grep 全仓库复验；只读命令（dir/eza/wc/git log/status）。基准版本：git `HEAD = 7d075a2`（2026-09-22，工作区无未提交修改，`git status --porcelain` 无输出）。

行数说明：以 `wc -l` 实测为准。任务表给的 auto_save_manager=80、tool_callbacks=35 与实测（83、49）不一致，本报告用实测值。

标注约定：凡代码中不能直接读到、需要外部行为（真实 LLM、真实终端、运行时线程交错）才能确认的判断，一律写「未验证」。

---

### narnat_agent/core/session_callbacks.py（902行）

**职责**：会话状态机——三态模型（NoSession / RootSession / ChildSession）承载 `/save /ls /cd /rm /explore /done /exit` 及各全局命令的行为，`SessionManager` 持有共享资源（消息列表、回调、延迟删除集合、目标模式开关）并转发命令。

**对外接口**（public 类/函数/方法，逐个列签名）：

模块级：
- `def _format_messages_text(messages: list) -> str:`（L25）：把 messages 渲染成"用户：/AI：/工具返回 [id]：/系统："的纯文本，供 /done 的合并提示词使用；assistant 无 content 时不输出行（L32-34）。

`class SessionState`（L44，状态基类）：
- `def available_commands(self) -> Dict[str, str]:`（L47）：抽象，子类给出"命令→说明"表（同时是命令可用性与 Tab 补全数据源）。
- `def save(self, name: str) -> str:`（L50）：默认返回 `"当前状态不可用 /save"`（L51）。
- `def show(self) -> str:`（L53）：抽象（注意：无 args 形参，子类签名改为 `show(self, args="")`，见下）。
- `def enter(self, name: str) -> str:`（L56）：抽象。
- `def delete(self, name: str) -> str:`（L59）：默认 `"当前状态不可用 /rm"`（L60）。
- `def explore(self, name: str) -> str:`（L62）：默认 `"当前状态不可用 /explore"`（L63）。
- `def done(self) -> str:`（L65）：默认 `"当前状态不可用 /done"`（L66）。
- `def exit(self) -> Tuple[str, Optional['SessionState']]:`（L68）：返回 `(消息, 新状态)`；新状态 None 表示 agent 应退出（L69）。
- `def auto_save(self):`（L72）：默认 pass（no-op，L73）。
- `def reset_after_compact(self):`（L75）：默认 no-op（L76-77）。
- `def session_name(self) -> Optional[str]:`（L79）：默认 None。
- `def session_parent(self) -> Optional[str]:`（L82）：默认 None。
- `def is_child(self) -> bool:`（L85）：默认 False。

`class NoSession(SessionState)`（L89）：
- `def __init__(self, mgr: 'SessionManager'):`（L92）
- `def available_commands(self) -> Dict[str, str]:`（L95）：12 条命令表（L96-109），含 /rm=删除会话、/exit=退出程序。
- `def save(self, name: str) -> str:`（L111）：无会话时创建根会话（诞生）。
- `def show(self, args: str = "") -> str:`（L127）
- `def enter(self, name: str) -> str:`（L142）
- `def delete(self, name: str) -> str:`（L164）
- `def exit(self) -> Tuple[str, Optional[SessionState]]:`（L187）：返回 `("", None)`（L189）。

`class RootSession(SessionState)`（L192）：
- `def __init__(self, mgr: 'SessionManager', name: str):`（L195）
- `def available_commands(self) -> Dict[str, str]:`（L202）：11 条（L203-217），/rm=删除子会话、/explore=创建探索分支、/exit=退出会话。
- `def _persist(self):`（L219，私有但为状态机核心）
- `def save(self, name: str) -> str:`（L228）
- `def show(self, args: str = "") -> str:`（L241）
- `def enter(self, name: str) -> str:`（L254）
- `def delete(self, name: str) -> str:`（L277）
- `def explore(self, name: str) -> str:`（L305）
- `def exit(self) -> Tuple[str, Optional[SessionState]]:`（L324）：返回 `("", NoSession(self._mgr))`（L326）。
- `def auto_save(self):`（L328）：`self._persist()`（L329）。
- `def session_name(self) -> Optional[str]:`（L331）
- `def session_parent(self) -> Optional[str]:`（L334）

`class ChildSession(SessionState)`（L338）：
- 类常量 `BOUNDARY_MARKER_PREFIX = "━━━ 探索分支开始"`（L342）
- 类常量 `SUMMARY_TASK_TEMPLATE = """..."""`（L345-392，多行模板，占位符仅 `{memory}` 与 `{target}`，L351/L355）
- `def __init__(self, mgr: 'SessionManager', name: str, parent: str):`（L394）
- `def available_commands(self) -> Dict[str, str]:`（L406）：11 条（L407-419），含 /done、/exit=暂离探索分支；无 /save、/rm、/explore。
- `def _persist(self):`（L421）
- `def show(self, args: str = "") -> str:`（L433）
- `def enter(self, name: str) -> str:`（L446）
- `def done(self) -> str:`（L469）：合并结论回父会话。
- `def exit(self) -> Tuple[str, Optional[SessionState]]:`（L531）：回父会话（暂离）。
- `def auto_save(self):`（L541）
- `def reset_after_compact(self):`（L544）
- `def session_name(self) -> Optional[str]:`（L560）
- `def session_parent(self) -> Optional[str]:`（L563）
- `def is_child(self) -> bool:`（L566）：True。

`class SessionManager`（L570）：
- `def __init__(self, narnat_dir: str, messages: 'MessageList', config_dir: str = "", thinking_effort_getter: Callable[[], str] = None, thinking_effort_setter: Callable[[str], None] = None, thinking_options: dict = None, thinking_passback_getter: Callable[[], bool] = None, thinking_passback_setter: Callable[[bool], None] = None, model_getter: Callable[[], str] = None, model_setter: Callable[[str], None] = None, model_options: list = None, summarize_func: Callable[[List[Dict[str, Any]], Callable[[], bool]], str] = None, summary_anim_start: Callable[[], None] = None, summary_anim_stop: Callable[[], None] = None, cancel_check: Callable[[], bool] = None, name_func: Callable[[List[Dict[str, Any]]], str] = None, goal_tool_setter: Callable[[bool], None] = None, goal_max_rounds: int = 0, project_skill_roots=None, skill_ignore_dirs: tuple = ()):`（L573-592）
- `def get_messages(self) -> List[Dict[str, Any]]:`（L626）：返回浅拷贝列表。
- `def get_message_list(self):`（L630）：返回 MessageList 引用（**全仓库无调用方**，死代码）。
- `def replace_messages(self, new_msgs: List[Dict[str, Any]]):`（L634）：`replace_all` 批量替换。
- `def switch_state(self, new_state: SessionState):`（L638）
- `def create_root_state(self, name: str) -> RootSession:`（L641）
- `def create_child_state(self, name: str, parent: str) -> ChildSession:`（L644）
- `def load_child_with_boundary(self, name: str, parent: str) -> Tuple[List[Dict[str, Any]], str]:`（L647）：仅一行转发 `load_session`（L648-649）。
- `def apply_delete_marks(self, tree: List[Dict[str, Any]]):`（L652）：给 tree 节点注入 `_delete_marked`。
- `def cleanup_deletes(self):`（L660）：真正执行 pending 删除（先子后根），清空集合。
- `def resolve_session_name(self, name: str) -> Tuple[Optional[str], Optional[str], str]:`（L669）：返回 (名称, 父名, 错误)。
- `@property def state(self) -> SessionState:`（L698-700）
- `def on_save(self, name: str) -> str:`（L704）
- `def on_show(self, args: str = "") -> str:`（L707）
- `def on_enter(self, name: str) -> str:`（L710）
- `def on_delete(self, name: str) -> str:`（L713）
- `def on_explore(self, name: str) -> str:`（L716）
- `def on_done(self) -> str:`（L719）
- `def on_compact(self) -> Tuple[str, str]:`（L722）：转发注入的 `compact_func`，成功则重置基准并落盘（L730-736）。
- `def on_exit(self) -> str:`（L738）
- `def on_auto_save(self):`（L744）
- `def on_skill(self, name: str) -> str:`（L747）
- `def on_thinking(self, effort: str) -> str:`（L761）
- `def on_list_thinking_options(self) -> list:`（L785）
- `def on_thinkback(self, action: str) -> str:`（L788）
- `def on_mode(self, name: str) -> str:`（L818）
- `def on_list_model_names(self) -> list:`（L846）
- `def on_list_names(self) -> list:`（L849）：根名 + "父/子" 全路径名。
- `def on_list_names_tree(self) -> list:`（L858）：同 on_list_names（L859）。
- `def on_list_rm_names(self) -> list:`（L861）：按当前状态返回可删除名（供 /rm Tab 补全）。
- `def on_list_skill_tree(self) -> list:`（L886）
- `def is_child_session(self) -> bool:`（L892）
- `def has_active_session(self) -> bool:`（L895）：`session_name() is not None`（**无调用方**，仅定义）。
- `def should_exit_agent(self) -> bool:`（L898）：`isinstance(self._state, NoSession)`。
- `def available_commands(self) -> Dict[str, str]:`（L901）：转发当前状态。

**依赖**（本文件 import 的内部模块，附行号）：
- `..config.session_store`：save_session, load_session, delete_session, list_sessions_tree, format_session_tree, format_session_summary, load_session_meta（L16-20）
- `..config.skill_store`：load_skill, list_skill_tree（L21）
- `.message_list`：MessageList（L22，仅类型标注/构造注入口使用）
- `..ui.colors`：C, R, X（L136、L248、L440 三处**函数内**延迟导入，用于 /ls 输出的"◀ 当前"/"✘ 退出后删除"着色）
- 标准库：json（L12）、os（L13）、typing（L14）

**被依赖**（grep 复验点）：
- `narnat_agent/assembly.py:19` `from .core.session_callbacks import SessionManager`；构造于 L116-137；后置注入 `session_mgr.summary_anim_start/summary_anim_stop`（L147-148）、`session_mgr._set_model`（L169）、`session_mgr.compact_func`（L182）。
- `narnat_agent/core/auto_save_manager.py:12` `from .session_callbacks import SessionManager`（L23 类型标注）；`:44` `from .session_callbacks import NoSession`（L45 `isinstance(self._mgr.state, NoSession)`）；`self._mgr.state.session_name()`（L79、L81）；`switch_state/create_root_state`（L74）；`cleanup_deletes`（L83）。
- `narnat_agent/ui/session_commands.py`（命令层，非 import 类名而是持有 mgr 引用）：`available_commands()`（L58、L397）、`on_compact`（L188）、`on_explore`（L206）、`on_done`（L217）、`on_save`（L278）、`on_show`（L293）、`on_enter`（L305）、`on_skill`（L317）、`on_delete`（L329）、`on_thinking`（L340）、`on_thinkback`（L348）、`on_mode`（L355）、`is_child_session`（L367）、`on_exit`（L368）、`should_exit_agent`（L371）、`state.session_name()`（L285）、`on_list_skill_tree`（L114）、`on_list_names_tree/on_list_rm_names/on_list_thinking_options/on_list_model_names`（L40-43 经 getattr）、以及 `_goal_enabled/_goal_max_rounds/_goal_default_rounds/_set_goal_tool` 直接读写（L242-243、245-246、254-259、266-268）。
- `narnat_agent/core/agent.py`：`self._mgr._goal_enabled`（L105）、`_goal_max_rounds`（L107）、`_goal_enabled/_goal_max_rounds/_set_goal_tool`（L207-210）、`on_auto_save()`（L133、L160）。
- `narnat_agent/ui/ui_design.py`：持有 `session_manager`（L283-285），传给 `_CommandCompleter`（L410）与 `_dispatch_command`（L322）。
- 包外实验脚本：`tool_exp/verify_goal_regression.py:21`、`tool_exp/verify_mode_cmd.py:12`、`tool_exp/verify_goal_mode.py:76`、`tool_exp/verify_import_cleanup.py:43`。
- `narnat_agent/ui/session_commands.py:5-7` 仅 docstring 提到三态（非 import）。

**状态**：
- 模块级全局：**无**（仅模块级私有函数 `_format_messages_text`）。
- 类变量：`ChildSession.BOUNDARY_MARKER_PREFIX`（L342，str 常量）、`ChildSession.SUMMARY_TASK_TEMPLATE`（L345-392，str 常量）；两者只读，无写入方。
- `SessionManager` 实例状态（L593-624）：
  - `narnat_dir`、`_messages`（MessageList 引用）、`_config_dir`
  - `_get_thinking_effort/_set_thinking_effort/_thinking_options`（L596-598）
  - `_get_thinking_passback/_set_thinking_passback`（L599-600）
  - `_get_model/_set_model/_model_options`（L601-603）
  - `summarize_func/summary_anim_start/summary_anim_stop/cancel_check/name_func`（L604-608，公开属性，assembly/agent 后置写入）
  - `compact_func`（L610，缺省 None，assembly L182 注入）
  - `_set_goal_tool`、`_project_skill_roots`、`_skill_ignore_dirs`（L611-615）
  - `_auto_save_done`（L616）、`_pending_auto_save_name`（L617）：**由 AutoSaveManager 直接读写**（auto_save_manager.py L42/L50/L64/L71-74）
  - `pending_deletes: Set[Tuple[str, Optional[str]]]`（L618）
  - `_state`（L619，缺省 NoSession）
  - `_goal_enabled`（L622）、`_goal_max_rounds`（L623）、`_goal_default_rounds`（L624）：**由 ui/session_commands.py 与 core/agent.py 直接读写**
- 状态对象实例状态：
  - `NoSession._mgr`（L93）
  - `RootSession._mgr/_name/_status/_summary/_msg_count`（L196-200；`_status` 初值 None，`_msg_count = len(mgr.get_messages())`）
  - `ChildSession._mgr/_name/_parent`（L395-397）+ 从磁盘 meta 恢复的 `_status`（L399，默认 "active"）、`_summary`（L400）、`_parent_msg_count`（L401）、`_last_summarized_at`（L402-403）、`_msg_count`（L404）

**行为要点**（编号；每条附行号）：
1. 三态命令表即行为契约：NoSession 支持 /clear /compact /save /ls /cd /rm /skill /thinking /thinkback /mode /goal /exit（L96-109）；RootSession 在其上把 /rm 变为"删子会话"、增 /explore、/exit 语义为"退出会话"（L203-217）；ChildSession 无 /save、/rm、/explore，有 /done、/exit=暂离（L407-419）。`/clear` 与 `/goal` 在 ui/session_commands.py 中绕过状态校验（L391-396），其余命令先查 `available_commands()`（L397-402）。
2. NoSession.save（诞生）：消息里无任何 user 消息 → 返回 `"没有对话内容，无需保存"`（L113-114）；无名字时调 `name_func(msgs)` 自动命名，仍为空 → `"自动命名失败，请手动指定: /save <名称>"`（L115-119）；`save_session` 返回非空错误原样返回且不切换状态（L120-122）；成功 → 创建 RootSession 并切换，返回 `""`（L123-125）。
3. NoSession.enter（/cd）：空名 → `"[错误: 请指定会话名称]"`（L143-144）；`resolve_session_name` 报错回传（L145-147）；目标为子会话 → `load_child_with_boundary`，否则 `load_session`（L148-155）；加载成功后 `replace_messages` 并用 `create_child_state` / `create_root_state` 切换（L156-161）；**NoSession→enter 不做 _persist**（无会话可存）。
4. NoSession.delete（/rm）：空名报错（L165-166）；`--all` 把 tree 中全部根与全部子加入 pending_deletes（L167-173）；普通名解析后加入 pending（L174-177），若目标是根会话则连带其所有子会话一起标记（L178-184）；**只标记不删除**，实际删除在 `cleanup_deletes()`（L660-667），由 AutoSaveManager.on_exit 触发（auto_save_manager.py L83）。
5. NoSession.show（/ls）：`apply_delete_marks` 先注入删除标记（L129）；无 `--all` 走 `format_session_summary`（今天全列、更早最多 3 根 + 提示），有 `--all` 走 `format_session_tree`（L130-133）；结果为空串则返回 ""（L134-135）；对文本做两处颜色替换（L136-139）；NoSession 传入 active_name=None → session_store 会在末尾追加 `"   ◉  ◀ 当前"` 行（session_store.py L246-247）。
6. NoSession.exit：返回 `("", None)`（L189）→ SessionManager.on_exit 不切状态（L740-741）→ `should_exit_agent()` 为 True（L898-899）→ ui/session_commands.py L371-372 返回 EXIT → agent.py L66-72 走 `auto_save.on_exit()` + `os._exit(0)`。
7. RootSession._persist（持久化核心）：读取消息浅拷贝（L220）；若 `len(msgs) > self._msg_count` 且 `_status in ("new","completed")` → 状态改回 `"active"`（L221-222，这是"探索分支续跑后重新变为待完成"的机制）；`save_session(..., status=self._status or "active", summary=self._summary)`（L223-225）；更新 `_msg_count`（L226）。
8. RootSession.save：空名或同名 → 仅 `_persist()`（L229-233）；不同名 → 以新名保存一份（不带 status/summary，默认 active）并新建 RootSession 切换（L234-238，等价"另存为/复制"）。
9. RootSession.enter/explore 前必先 `_persist()`（L257、L310）——切走前把当前会话落盘；进入目标会话后 `replace_messages` + 切状态（L269-274、L319-321）。
10. RootSession.explore（创建探索分支）：空名 → `"[错误: 请指定分支名称]"`（L306-307）；与父同名 → 相应错误（L308-309）；把父消息逐条 `dict(m)` 浅拷贝为分支初值（L311）；`parent_msg_count = len(msgs)`（L312）；`save_session(name, msgs, parent=self._name, status="new", parent_msg_count=parent_msg_count, last_summarized_at=parent_msg_count)`（L313-315，**分支起点 = 父会话当前消息数**，作为 /done 合并的 memory/target 分界）；随后把磁盘上刚写的分支读回并 `replace_messages`（L316-319）；`create_child_state(name, self._name)` 切换（L320-321）。
11. RootSession.delete：`--all` = 标记当前会话的全部子会话（L280-287，找不到当前会话 → `"会话不存在"`）；含 "/" 的输入按 `父/子` 拆分并要求父名==当前会话名，否则 `"'{name}' 不是当前会话的子会话"`（L288-294）；在 tree 中找同名子会话命中则标记，未命中 → `"'{name}' 不是当前会话的子会话"`（L295-302）；当前会话不在 tree → `"会话不存在: {name}"`（L303）。
12. RootSession.exit → NoSession（L326）；`should_exit_agent()` 变 False，命令层打印"已退出会话"（ui/session_commands.py L376-377）。
13. ChildSession.__init__ 从磁盘 meta 恢复运行状态（L398-404）：`status` 默认 "active"、`summary` 默认 None、`parent_msg_count` 默认 0（`or 0`）、`last_summarized_at` 缺省回退到 `parent_msg_count`（L402-403）。
14. ChildSession._persist 与 RootSession 同构，但额外持久化 `parent=self._parent, parent_msg_count=self._parent_msg_count, last_summarized_at=self._last_summarized_at`（L425-430）。
15. ChildSession.done（探索分支合并，"行为等价核心"，L469-529）：
    - 已 completed → `"该探索分支已完成，不可重复 /done"`（L470-471）。
    - `parent_msg_count` 为 0（未持久化基准，历史会话）→ 在消息里找 role=system 且 content 含 `"━━━ 探索分支开始"` 的消息，其下标作为父基准（L474-478；当前版本无写入该 marker 的代码，见补丁痕迹 5）。
    - `last_summarized_at` 小于 `parent_msg_count` 时提升为 `parent_msg_count`（L480-482，防止把分支早期内容重复合并）。
    - `memory = msgs[:parent_msg_count]`（主分支基准）、`target = msgs[last_summarized_at:]`（本次新增讨论）(L484-485)；`target` 为空 → `"没有新的讨论内容需要总结"`（L487-488）。
    - 用 `SUMMARY_TASK_TEMPLATE.format(memory=..., target=...)` 组装提示词，作为唯一 user 消息（L490-493）。
    - 依次调 `summary_anim_start()`（可空）、`summarize_func(summary_msgs, cancel_check)`（可空）、`summary_anim_stop()`（L495-501）；summary 为空 → `"总结取消或失败"`（L502-503，**不改动任何状态**）。
    - 加载父会话（失败 → `"无法加载父会话: {err}"`，L505-507）。
    - 轮次号 = 父会话中 system 消息含 `"子会话 [{name}]"` 的条数 + 1，`>1` 时标签为 `" (第N轮)"`（L508-511）。
    - 父会话追加 `{"role":"system","content":"# 子会话 [{name}]{round_label} 结论\n\n{summary}"}` 并保存（L512-514）——**结论以 system 消息追加在父会话末尾**。
    - 自身：`_last_summarized_at = len(msgs)`、`_status = "completed"`、`_summary = summary`（L516-518）；从磁盘重读子会话消息（L519-520，错误被忽略）；以 `status="completed"` 重写子会话文件（L521-524）。
    - `replace_messages(parent_msgs)` + 切换回 `RootSession(self._parent)`（L526-528）；返回 "" → 命令层打印"探索分支已完成，结论已合并"（ui/session_commands.py L221）。
16. ChildSession.exit（暂离）：加载父会话，失败 → 返回 `(err, None)`（状态保持 ChildSession，命令层显示错误且不退出）；成功 → `replace_messages(父)` + 切到 RootSession(parent) + 返回 `("", new_state)`（L531-539）；命令层据 `is_child_session()`（先取值，L367）打印"已暂离探索分支（/cd 回来继续）"（L373-375）。
17. 多轮合并：done 后再 `/cd` 回该分支继续讨论，`_persist` 会把 completed 翻回 active（L423-424），再次 /done 时轮次号 +1（L508-511），父会话累积多条"结论"system 消息。
18. SessionManager.on_compact（/compact）：`compact_func` 未注入 → `("error","压缩不可用")`（L730-731）；成功（status=="ok"）→ 先 `reset_after_compact()`（修正子会话增量基准）再 `auto_save()` 落盘（L732-735）——注释说明"命令不经过轮末自动保存，不落盘压缩结果会丢失"（L725-727）；返回 `(status, text)`，status ∈ {ok, empty, error}（L728）。
19. ChildSession.reset_after_compact：把 `_parent_msg_count` 与 `_last_summarized_at` 重置为"前导 system 消息条数"（边界扫描 L554-558），使镜像压缩后的 `[system_prompt, 摘要, 尾部...]` 结构（memory=摘要、target=尾部逐字保留）。
20. resolve_session_name 三级解析（L669-696）：① 完整名匹配（根名本身，或 `"父/子"` 全路径）优先（L673-678）；② 含 "/" 时直接按 `父/子` 拆分返回（不再校验存在性，L680-682）；③ 裸名在全部子会话中匹配，唯一 → 返回；多个 → `"'{name}' 有多个，请用完整路径指定：\n<paths>"`（L691-695）；无 → `"会话不存在: {name}"`（L696）。注意 L692-694 的路径拼装写作 `f"{c}/{p}"`/`f"{p}"`（c=child，p=parent），即"父/子"格式。
21. on_skill：`load_skill(narnat_dir, name, project_roots=..., ignore_dirs=...)`（L748-750）；错误原样返回（L751-752）；成功且有 path 时在正文前插入"本技能目录: {dirname}"说明（L753-757）；内容以 `append_system` 注入消息列表（L758）并返回 ""。
22. on_thinking（/thinking）：无参 → `"当前思考强度: {label}"`（L763-766）；非法值 → `"无效值: {effort}（可用: high / max）"`（L767-770）；合法 → 调 setter（L771-772）并**写 narnat.json**（`智能体.思考.强度`，L773-782，异常静默吞掉），返回 `"思考强度已切换为: {label}"`（L783）。
23. on_thinkback（/thinkback）：无参 → `"思考回传: 开/关（/thinkback on|off 切换）"`（L795-798）；`on|off` → setter + 写 `智能体.思考.回传`（L799-812）；与旧值相同 → `"思考回传已是开启/关闭状态"`，不同 → `"思考回传已开启/关闭"`（L813-815）；其它 → `"无效值: {act}（可用: on / off）"`（L816）。
24. on_mode（/mode）：无参 → `"当前模型: {current}"`（L820-822）；大小写不敏感匹配 `model_options` 中的项（L824-831，未命中 → `"无效值: ...（可用: ...）"`）；命中 → setter + 写 `智能体.模型.当前` + 返回 `"设置成功：{matched}"`（L832-844）。注意 ui/session_commands.py 用 `result.startswith("无效值"/"设置成功")` 判定着色（L356-361）。
25. 目标模式开关（/goal）落在 SessionManager 上而非状态对象：`_goal_enabled`、`_goal_max_rounds`（临时覆盖）、`_goal_default_rounds`（配置默认），由 ui/session_commands.py L242-273 读写，core/agent.py L105-107、L207-211 读取以决定续跑轮数与 GoalComplete 注入。
26. on_list_rm_names（Tab 补全数据源）：NoSession → 全部会话名，但跳过已在 `pending_deletes` 中的（L863-875）；RootSession → 仅当前会话的子会话（"父/子" 形式），同样跳过已标记（L876-884）；ChildSession → `[]`（L884）。
27. apply_delete_marks 只做展示标记：给 tree 的 root/child dict 注入 `_delete_marked=True`（L652-658）；session_store 的格式化函数消费该字段渲染 `"✘ 退出后删除"`（session_store.py L227、L242、L270、L278）。
28. on_exit 的状态回写：`msg, new_state = self._state.exit()`；new_state 非 None 时写回 `self._state`（L739-742）——NoSession.exit 返回 None → agent 退出（由调用方判定）。

**边界/异常行为**（编号；附行号）：
1. `SessionState.show` 基类签名无 args（L53），三个子类均为 `show(self, args="")`（L127/L241/L433）——基类与子类签名不一致（多态面不齐；基类方法实际不可用）。
2. 空名字/`--all` 等输入特判分散在各命令实现中：`"[错误: 请指定会话名称]"`（L144/L166/L256/L279/L448）、`"[错误: 请指定分支名称]"`（L307）。
3. `/rm` 是延迟删除：同一次运行内重复 `/rm` 同名会重复 add（set 语义去重，L177/L300）；删除文件名与配置名可能不同（session_store 会做 `_safe_filename` 转义，session_store.py L24-31）。
4. 名称大小写与全角字符不做规范化；`resolve_session_name` L680-682 的"含 / 即拆分"使 `"a/b"` 在不存在时仍返回 `(b, a, "")`，随后由 `load_session` 报 `"会话不存在: b"`（不是预期的目录级错误）。
5. `/cd`、`/explore` 在 RootSession/ChildSession 中会先落盘当前会话；若落盘失败（`save_session` 返回错误），返回值被忽略——`_persist` 不检查错误（L223、L425，与 NoSession.save L120-122 的错误处理不一致）。
6. ChildSession.done 中 `load_session(self._name, parent=self._parent)` 的错误被丢弃（L519-520 `child_msgs, _ =`），若磁盘文件缺失/损坏会以空列表 + completed 状态覆盖分支文件（数据丢失路径）。
7. ChildSession.done 的 memory 取内存 `msgs[:parent_msg_count]`，而父会话结论取磁盘父文件（L505）；若父会话有未落盘修改（正常流程不会），done 会以磁盘版本覆盖内存（replace_messages L526）。
8. done 要求 `target` 非空：仅当 `last_summarized_at < len(msgs)`；`last_summarized_at` 在首次 done 后被推到 len(msgs)，因此"进入分支后没讨论就 /done"→ target 非空与否取决于进入动作本身是否追加消息（`explore` 后未追加消息时 target 为空 → L488 提示）。
9. `summary_anim_start/stop`、`summarize_func`、`cancel_check`、`name_func` 均可为 None：done 对 anim/summarize 做了空判（L495-501），而 `name_func` 在 save 中同样判空（L116-119）；未注入时 /done 返回 `"总结取消或失败"`。
10. on_thinking/on_thinkback/on_mode 写配置的异常被 `except Exception: pass` 静默吞掉（L781-782、L811-812、L842-843）——配置写失败用户无感（内存值仍已改）。
11. 写配置使用 `data.setdefault("智能体", {}).setdefault("思考", {})["强度"] = ...`（L778），固定的中文键路径；JSON 非法或编码异常同上静默。
12. `_msg_count` 以 `len(msgs)` 初始化（L200、L404），故"消息数不变"的编辑（如替换某条内容）不会触发状态翻转（L221/L423）。
13. `/compact` 在 NoSession 下：`reset_after_compact` 为 no-op、`auto_save` 为 no-op（L734-735 → L72-73、L76-77），即无会话时压缩结果不落盘（退出即丢）。
14. `load_child_with_boundary` 与 `load_session` 行为完全一致（L647-649），"boundary" 语义未在此函数体现（未验证命名意图）。
15. resolve_session_name 的"多匹配"错误信息拼装（L692-694）与 /rm 的 Tab 补全格式（"父/子"）一致，但错误文案写作 `"{c}/{p}"` 使用 child/parent 顺序（与"父/子"同名不同序，见 L677 的 `f"{root['name']}/{child['name']}"`——两处顺序约定存在冲突风险）。
16. ui 层调用 `state.session_name()`（ui/session_commands.py L285）读取保存名，因此 `RootSession.save` 另存为后命令层打印的名字来自新状态（一致）。
17. `/ls` 的着色是**后置文本替换**（L137-139）：若会话名本身含 `"◀ 当前"`/`"✘ 退出后删除"` 字面量会误着色（低风险）。

**补丁痕迹**（编号；附行号+证据；严重度）：
1. **【高】跨对象私有访问**：`SessionManager._auto_save_done`（L616）/`_pending_auto_save_name`（L617）由 AutoSaveManager 直接读写（auto_save_manager.py L42、L50、L64、L71-74）——后台线程与状态机通过私有字段耦合，无接口、无锁。
2. **【高】跨层私有状态访问**：`_goal_enabled/_goal_max_rounds/_goal_default_rounds`（L622-624）由 UI 命令层直接读写（ui/session_commands.py L242-243、L254-256、L266-268）并由 core/agent.py 读（L105、L107、L207-211）；`_set_goal_tool` 亦被外部直调（ui/session_commands.py L245-246、L258-259）。
3. **【高】三份重复实现**：`enter()` 在 NoSession/RootSession/ChildSession 中三份几乎逐行相同（L142-162、L254-275、L446-467）；`show()` 三份同构（L127-140、L241-252、L433-444）；`_persist()` 两份同构（L219-226、L421-431）；`exit()` 的"回父会话"逻辑与 done 尾部重复（L531-539 vs L526-528）。
4. **【中】core → ui 反向依赖（延迟导入）**：`from ..ui.colors import C, R, X` 写在三个方法体内（L136、L248、L440），同类逻辑三处复制。
5. **【中】历史兼容分支**：done 中 `BOUNDARY_MARKER_PREFIX`（L342）的搜索分支（L474-478）在包内**无写入方**（全仓库仅定义与读取，见 translator/translate_sessions.py L39 只读则说明是历史会话文件格式），当前版本改用 `parent_msg_count` 持久化基准。
6. **【中】状态双写**：`on_exit` 里 `if new_state is not None: self._state = new_state`（L740-741），而 ChildSession.exit 内部已 `switch_state`（L538）——同一职责两处实现（幂等但语义含混）。
7. **【中】类型检查替代多态**：`on_list_rm_names` 用 `isinstance(self._state, NoSession)` / `RootSession` 分派（L863、L876），`should_exit_agent` 用 `isinstance(NoSession)`（L898-899）——状态类已有多态接口，此处绕开。
8. **【中】隐式树契约**：`apply_delete_marks` 直接往 session_store 返回的 dict 里塞 `_delete_marked`（L652-658），契约仅存在于注释与消费端（session_store.py L227 等），无类型保护。
9. **【中】死代码**：`get_message_list`（L630-632）全仓库无调用；`has_active_session`（L895-896）无调用；`load_child_with_boundary`（L647-649）是同签名转发（保留了"可测性/命名"包装）。
10. **【中】可变全局注入**：`compact_func`（L610）非构造参数，由 assembly L182 后置赋值；`summary_anim_start/stop` 同样后置（assembly L147-148）——构造签名与真实依赖不一致。
11. **【低】`or` 语义回退**：`meta.get("last_summarized_at") or self._parent_msg_count`（L402-403）使已持久化的 0 值回退为 parent_msg_count（若两值均为 0 则结果为 0）。
12. **【低】魔法文本**：写父会话的标题格式 `f"# 子会话 [{self._name}]{round_label} 结论\n\n{summary}"`（L513）与轮次识别字符串 `f"子会话 [{self._name}]"`（L510）是同一隐式格式的两处硬编码。
13. **【低】空行/残缺语句**：`exit()` 定义与 return 之间留空行（L188、L325、L532）；`NoSession.exit` 里无逻辑（L187-189）。
14. **【低】状态字段语义混用**：`_status` 同时承担"新/已完成/活跃"三种值（L221、L399、L423、L517），且 `_status` 初值 None 依赖 `or "active"` 兜底（L224、L427）。

**可测性**：
- 可独立单测（使用临时目录 + 假回调注入）：
  - 全部三态命令：`NoSession.save/enter/delete/show/exit`、`RootSession.*`、`ChildSession.*`——依赖仅 session_store（文件 IO，可用 tmp 目录）与可注入回调（summarize_func/cancel_check/name_func 可为假函数）。
  - `_format_messages_text`（L25，纯函数）。
  - `resolve_session_name`（L669，纯逻辑 + list_sessions_tree 文件 IO）。
  - `apply_delete_marks` / `on_list_rm_names` / `on_list_names`（树数据结构转换）。
  - `on_thinking/on_thinkback/on_mode` 的非落盘分支（给假 setter/getter + 临时 config_dir 可测落盘分支）。
- 需集成测试：`on_compact`（需注入 compact_func 并断言 reset_after_compact + auto_save 调用序）；`ChildSession.done` 全流程（需假 summarizer 返回固定文本、真实 session_store 落盘、断言父/子文件内容与状态迁移）；`on_skill`（依赖 skill_store 目录布局）。
- 无法自动化（时序/终端交互）：`/ls` 的颜色输出（依赖 ui.colors 与真实终端色码，只能断言文本替换结果）；done 时的 spinner 动画起停（summary_anim_start/stop 与 UI 线程耦合）。

---

### narnat_agent/core/tool_dispatcher.py（541行）

**职责**：工具调用调度器——按"只读/写入/串行"三类分组执行一批 tool_calls（只读并行、写入按文件串行、串行工具逐个），渲染工具调用摘要与失败提示，并在 `require_plan` 开启时拦截未先建计划的调用。

**对外接口**：
模块级：
- `def _local_hostname() -> str:`（L21）：socket.gethostname()，异常回退 "localhost"（L26-27）。
- `def _mcp_target_hint(config) -> str:`（L30）：从 MCP config 提取"目标程序"后两段路径用于摘要显示（L35-46）。

`class ToolDispatcher`（L49）：
- 类属性 `READONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch"}`（L53）
- 类属性 `WRITE_TOOLS = {"Edit", "Write"}`（L54）
- 类属性 `SERIAL_TOOLS = {"Shell", "Terminal", "TodoWrite", "Serial", "GoalComplete"}`（L55；**全文件无引用，见补丁痕迹 6**）
- 类属性 `TOOL_LABELS = {...}`（L58-71，12 项）
- 类属性 `FILE_PATH_TOOLS = {"Read", "Edit", "Write"}`（L74）
- `def __init__(self, tool_context: ToolContext, executor: ThreadPoolExecutor, logger=None):`（L76）
- `def execute_tool_calls(self, tool_calls: List[Dict[str, Any]], stream) -> List[Tuple[str, str]]:`（L81-85）
- `def _run_single(self, tc_id: str, name: str, arguments: dict, stream) -> str:`（L219）
- `def _run_parallel(self, group, results, stream) -> None:`（L275）
- `def _run_sequential_group(self, group, results, stream) -> None:`（L323）
- `def _check_plan_required(self, tool_calls: List[Dict[str, Any]]) -> Optional[List[Tuple[str, str]]]:`（L331）
- `@staticmethod def _fmt_cmd(raw: str, empty_label: str = "(空命令)") -> str:`（L386-387）
- `@staticmethod def _tool_label(name: str) -> str:`（L398-399）
- `def _show_tool_call(self, name: str, arguments: dict):`（L410）
- `def _show_diff(self, color_diff: str):`（L527）
- `def _show_tool_failed(self, name: str):`（L536）

**依赖**（本文件 import 的内部模块）：
- `..tools.registry`：execute as tool_execute（L11）
- `..tools.bash`：kill_active as _kill_bash（L12）
- `..tools.terminal`：kill_active_exec as _kill_terminal_exec（L13）、resolve_dev_display as _dev_display（L14）
- `..tools.serial`：kill_active_exec as _kill_serial_exec（L15）
- `..tools.tool_context`：ToolContext（L16，类型标注）
- `..tools.exec_signal`：has_error, strip_tags（L17）
- `..output`：write as _stdout_write, D, R, X, is_quiet_tools（L18）
- 函数内 `import socket`（L23）
- 标准库：json（L6）、time（L7）、concurrent.futures（L8）、typing（L9）

**被依赖**：
- `narnat_agent/assembly.py:15` `from .core.tool_dispatcher import ToolDispatcher`；构造 L151-155（`ThreadPoolExecutor(max_workers=16)`）。
- `narnat_agent/core/agent_loop.py:13` `from .tool_dispatcher import ToolDispatcher`（L29 构造参数类型）；运行 `self._dispatcher.execute_tool_calls(tool_calls_result, stream)`（L228）。
- `narnat_agent/core/agent.py`：`self._parts.dispatcher._executor.shutdown(wait=False)`（L177、L268）——外部直接访问 dispatcher 私有 `_executor`。
- 包外实验脚本：`tool_exp/repair_seq_test.py:14`、`tool_exp/test_esc_interrupt_regression.py:252`、`tool_exp/mcp_test/test_mcp_runtime.py:187`、`tool_exp/repro_mcp_fail_label.py:15`、`tool_exp/verify_mcp_summary_full.py:18`、`tool_exp/verify_ui_dispatch_integration.py:11`、`tool_exp/verify_ui_fail_gitdiff.py:14`、`tool_exp/verify_review_fixes.py:24`。
- 包外失效引用（模块中**已无该符号**）：`tool_exp/repro_ui_fail_judge.py:11`、`tool_exp/verify_ui_judge_e2e.py:8`、`tool_exp/verify_review_fixes.py:24`、`tool_exp/verify_ui_fail_gitdiff.py:14` 均 `import _command_exec_failed`（历史版本符号，现已不存在）。

**状态**：
- 模块级全局：无（`_local_hostname` 每次调用 `socket.gethostname()`，不缓存）。
- 类变量：上述 5 个集合/字典字面量（只读，无写入方）。
- 实例状态：`_tool_context`（L77，共享 ToolContext，读 `require_plan/min_tools/current_todos`）、`_executor`（L78，外部共享线程池，被 agent.py 直接 shutdown）、`_logger`（L79，可为 None）。

**行为要点**（编号；附行号）：
1. 计划优先拦截（最早执行，先于解析/分组）：仅当 `ctx.require_plan` 为真（L334）；批内含 TodoWrite → 直接放行（L340-341，注释说明否则 TodoWrite 会被连坐导致结果缺失 → repair 伪造"[用户中断]" → 400 隐患）；非 TodoWrite 数量 < `ctx.min_tools` → 放行（L354-355）；`ctx.current_todos` 中已有 `status=="in_progress"` → 放行（L357-362）；否则拦截：日志（L364-365）、hint 文案（L366-369）、把每个被拦工具以 `_show_tool_call` + `_show_tool_failed` 渲染（L371-380），返回结果列表（第 1 条 = hint，其余 `"[计划优先拦截，详见上方]"`，L381-384）。
2. arguments 解析：`json.loads(func["arguments"])`，`JSONDecodeError` → `{}`（L106-110）；`tc["id"]`/`func["name"]` 直接下标（无防御）。
3. 分组规则（L117-124）：`READONLY_TOOLS` → 只读并行组；`WRITE_TOOLS` → 以 `arguments.get("file_path","")` 为键分组；**其余一切工具**（Shell/Terminal/TodoWrite/Serial/MCP(mcp__*)/GoalComplete/未知工具）→ 串行组（L123-124 的 else 兜底）。
4. 串行组排序：`serial_group.sort(key=lambda item: item[2] != "TodoWrite")`（L128）——稳定排序把 TodoWrite 提前，"先定计划再动工具"（注释 L126-127）。
5. 三阶段串行执行顺序：只读（L134-135）→ 写入（L144-189）→ 串行（L195-215）；注释声明目的是"保证写入看到最新文件状态"（L94）。
6. 执行过程中任意点检测 `stream.cancelled` 都会 kill 三类进程（`_kill_bash`/`_kill_terminal_exec`/`_kill_serial_exec`，L138-140、L155-157、L177-179、L203-205、L285-287、L309-311）；取消后返回**已完成**的部分结果（L141、L192、L217）。
7. 写组两形态：单文件组 → 逐条提交 + `while not fut.done()` 轮询（0.05s）后取结果（L146-166）；多文件组 → 每组提交 `_run_sequential_group`，用 `wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)` 循环收割（L167-189）。
8. 组内某工具抛异常：为**尚未产生结果**的该组工具调用 `_show_tool_failed`（L186-189，仅 UI 提示，不补 results）。
9. 串行阶段逐条：提交 → 轮询（0.05s）→ `fut.result()`；异常 → `results[idx] = (tc_id, f"[错误: 工具执行失败: {e}]")` + 失败提示（L210-215）；只读并行阶段异常文案为 `f"[错误: 工具执行失败({name}): {e}]"`（L296、L320，**与串行阶段文案不同**）。
10. 返回值：`[results[i] for i in range(len(parsed)) if i in results]`（L217）——按原始调用顺序，缺失项直接不出现。
11. `_run_single` 执行单元（L219-273）：`stream.pause_spinner()` → `stream.flush_renderer()` → `_show_tool_call`（UI 三连，L222-224）→ `tool_execute(name, arguments, self._tool_context)` 返回 `(llm_result, color_diff)`（L227）。
12. 失败判定双轨制（L228-247，注释 L228-241 详述理由）：命令类（`name in ("Shell","Terminal")`）或 MCP 工具（`name.startswith("mcp__")`）→ `has_error(llm_result)`（框架不可伪造的随机标签）；其余工具 → `llm_result.startswith("[错误")`。任何情况下都对 str 结果执行 `strip_tags` 后再交给 AI（L248-249）。
13. 日志两条：调用参数与结果，各截断 200 字符（L250-257；`self._logger.info` 无 None 保护）。
14. 显示优先级：`color_diff` 非空 → 显示 diff（L260-261）；否则若 `exec_failed`（标签判定）或"非命令类且以 `[错误` 开头" → `_show_tool_failed`（L262-270），即 **diff 与失败提示互斥**，有 diff 时失败提示不显示。
15. `_show_tool_call` 摘要规则（L410-525）：静默模式整体跳过（L411-413）；label 由 `_tool_label` 生成；`mcp__` 工具摘要 = 工具名 + 紧凑 JSON 参数（L416-424）；`FILE_PATH_TOOLS` 摘要 = `dev:file_path`，本机（dev 显示名 == 主机名）时省略 dev 前缀（L425-432，用 `_local_hostname()` 比较）；Shell 按 `background`/`bg` 分派（后台提交/状态/等待(≤Ns)/取消 bgN/普通命令，L433-445）；Terminal 按 action 分派 connect/exec/status/close/transfer/input（L446-478）；Grep/Glob → pattern（L479-482）；WebSearch → query（L483-484）；TodoWrite → `"{N}项"`/`"(空)"`（L485-487）；MCP → connect（带 `_mcp_target_hint`）/disconnect（L488-498）；Serial 按 action（L499-520）；无摘要时只打印 `[标签]`（L522-525）。
16. `_fmt_cmd`（L386-396）：strip 后非空 → 原文；仅含换行 → `"(换行)"`；纯空格 → `"(空格)"`；全空 → `empty_label`。
17. `_tool_label`（L398-408）：`mcp__<server>__<tool>` 三段 → `"MCP:<server>"`；否则 `TOOL_LABELS.get(name, name)`（未知工具显示原名）。
18. `_show_diff`（L527-534）：每行前缀两个空格；若 diff 是单行且含 `"[无差异]"` 则以 `"\n"` 收尾（不加空行），否则 `"\n\n"`。
19. `_show_tool_failed`（L536-541）：红字 `"  [<label>失败]"`，静默模式跳过。
20. `_run_parallel`（L275-321）：空组直接返回（L277-278）；单元素组走"提交+轮询"（L279-298）；多元素提交全部后 `wait` 循环收割，异常同样写 `"[错误: 工具执行失败({name}): {e}]"` + 失败提示（L300-321）。
21. `_run_sequential_group`（L323-329）：本组内逐条 `_run_single` 并写 results；不处理异常（由 `_run_single` 内部/调用方兜底），组内取消直接 break。
22. 并发写 results 字典：多个 `_run_sequential_group` 线程写同一 dict，但键为各自 group 内的唯一 idx（L329），无同键竞争（CPython 下 dict 单键写安全）。

**边界/异常行为**：
1. 未知工具名 → 串行组（L123-124）；`tool_execute` 返回 `"[错误: 未知工具: X]"`（registry.py L124）→ 非命令类 → 显示失败（L265-270）。
2. `arguments` 非法 JSON → `{}` 后照常执行，工具侧报参数错误（L108-109）。
3. `tool_calls` 为空列表 → 拦截检查返回 None（L351-352），三组为空，返回 `[]`（不报错）。
4. `executor.submit` 抛异常（池已 shutdown）→ 未被捕获，直接冒泡给 AgentLoop（L151/L200/L282/L304 均在 try 外，**未验证实际触发场景**）。
5. 忙等轮询无超时上限（L152-158、L201-207、L283-289）：工具本身若永不返回，循环依赖 `stream.cancelled` 才能退出。
6. `_run_single` 中 `self._logger.info`（L250、L254）无 None 判空，而 `_check_plan_required` 有（L364）——logger=None 时 AttributeError 会被外层 except 捕获成 `"[错误: 工具执行失败(...)]"`（包外脚本 tool_exp/repro_mcp_fail_label.py:24 即以 None 构造，未验证其是否走到该路径）。
7. 取消时返回部分结果，缺失的 tool_call 由上层 `append_interrupted_tools` 补 `"[用户中断]"`（agent_loop.py L232-233）后再 abort。
8. 计划拦截不产出被拦工具的真实结果，替换文本两条固定串（L383）；同时给每个被拦工具打印"失败"行（L380）。
9. 中文/编码：摘要按原样打印（`json.dumps(..., ensure_ascii=False)` L422）；`_show_tool_call` 对超长命令不截断（仅日志截断 200）。
10. 平台分支：无（L6-18 导入区与全文无 `sys.platform` 判定；Windows 删除确认由 `SafetyCallbacks` 与工具侧处理，见 tool_callbacks.py）。

**补丁痕迹**：
1. **【中】三份"提交+忙等轮询+异常兜底"重复代码**：L146-166（写组单文件）、L194-215（串行阶段）、L279-298（只读单元素），结构近乎复制。
2. **【中】`SERIAL_TOOLS` 是死代码**：L55 定义，全文件仅此处出现（复验：`grep SERIAL_TOOLS` 只命中 53-55 行区域）；实际分组用 else 兜底（L123-124），该集合既不校验也不使用——命名承诺与实际策略脱节。
3. **【中】职责混杂**：`_run_single` 同时做 UI（pause/flush/摘要/diff/失败行）、执行、日志、失败判定（L219-273）。
4. **【中】双轨失败判定**：标签判定（命令类/MCP）与文本前缀判定（其余）并存（L242-270），同一语义两套规则；工具名清单（`("Shell","Terminal")` + `mcp__` 前缀）为硬编码魔法值。
5. **【中】吞异常**：L186-189 `except Exception:` 后仅补 UI 失败提示、不打日志、不补结果；L319-321 同（有 results 兜底但仍无日志）。
6. **【中】外部直接操作私有 `_executor`**：agent.py L177、L268 `dispatcher._executor.shutdown(wait=False)`——shutdown 时机不在调度器内部，池生命周期与对象职责错位。
7. **【低】返回结构不一致的异常文案**：`"[错误: 工具执行失败: {e}]"`（L165、L214）与 `"[错误: 工具执行失败({name}): {e}]"`（L296、L320）并存。
8. **【低】`_show_tool_call` 超长命令**：`_fmt_cmd`（L386-396）不做长度截断，含换行的大命令会原样多行输出（L410-525，与日志 200 字符截断不一致）。
9. **【低】MCP 前缀解析重复**：`_tool_label`（L404-407）与 `_show_tool_call`（L417-419）各自 `split("__", 2)` 解析同一格式。
10. **【低】历史注释负担**：L228-241 用 14 行注释解释判定规则（含"从根上杜绝误判"等结论性表述），信息密度高但属于对既有补丁的说明。

**可测性**：
- 可独立单测（纯函数/静态方法）：`_fmt_cmd`（L386）、`_tool_label`（L398）、`_mcp_target_hint`（L30，输入 dict）、`_local_hostname`（L21，异常分支需打桩 socket）。
- 需集成测试（假 stream + 真线程池 + mock `tool_execute`）：`execute_tool_calls` 的分组/顺序/取消/计划拦截路径；`_check_plan_required` 可用真实 ToolContext 构造 6 种输入组合单测。
- 无法自动化：`_show_tool_call/_show_diff/_show_tool_failed` 的终端输出与 spinner 交错时序（需捕获 stdout 断言文本，但真实渲染顺序依赖 UI 线程）；`stream.pause_spinner/flush_renderer/resume_spinner` 时序。
- 注：模块级 `from ..tools.registry import execute as tool_execute` 是导入名绑定，单测需 `monkeypatch` 模块属性（包外脚本已用 t d.tool_execute 之类手法，见 tool_exp/verify_ui_dispatch_integration.py）。

---

### narnat_agent/core/message_manager.py（219行）

**职责**：消息管理门面——把列表持有与修改委托给 MessageList，自身实现 `repair`（中断修复）与 `handle_compress`（压缩流程编排，含摘要请求、切点、原子重建）。

**对外接口**：
- `class CompressResult(NamedTuple):`（L14）：字段 `ok: bool`（L24）、`replaced: int = 0`（L25）、`tokens: int = 0`（L26）、`reason: str = ""`（L27）；reason 取值集合见 docstring（L19-22：empty/interrupted/llm_error/empty_summary/overflow）。
- `class MessageManager:`（L30）
  - `def __init__(self, messages: MessageList, compressor: Compressor, logger: Optional[AgentLogger] = None):`（L33-34）
  - `@property def view(self) -> MessageView:`（L39-40）
  - `def append_system(self, content: str) -> None:`（L46）
  - `def append_user(self, content: str) -> None:`（L49）
  - `def append_assistant(self, content: str, tool_calls: Optional[list] = None, thinking: Optional[str] = None, thinking_signature: Optional[str] = None) -> None:`（L52-54）
  - `def append_tool_result(self, tool_call_id: str, result: str) -> None:`（L57）
  - `def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:`（L60）
  - `def repair(self) -> None:`（L65）
  - `def handle_compress(self, pending_input: Optional[str], system_prompt: str, llm_client, cancel_check, on_interrupt, on_llm_error, retain_tokens: int = 0) -> CompressResult:`（L102-104）
  - `def clear_and_rebuild(self, system_prompt: str, summary_text: str) -> None:`（L216，**无调用方**）

**依赖**：
- `.compressor`：Compressor, select_cut_index, estimate_tokens（L9）
- `.message_list`：MessageList, MessageView, SYNTHETIC_THINKING（L10）
- `..logger`：AgentLogger（L11）
- 标准库：typing（L7）

**被依赖**：
- `narnat_agent/assembly.py:16` `from .core.message_manager import MessageManager`；构造 L89（`MessageManager(message_list, compressor, logger)`）。
- `narnat_agent/core/agent_loop.py:12`（L28 构造参数）；使用 `repair()`（L98）、`view.to_list()`（L110、L474）、`append_assistant`（L115、L221、L315）、`append_interrupted_tools`（L233）、`append_tool_result`（L258、L429、L456、L461）、`append_user`（L357、L375）。
- `narnat_agent/core/compression_coordinator.py:12`（L36 构造参数）；调用 `handle_compress` 三次入口（L61、L91、L125）、`append_user`（L52、L58）。
- `narnat_agent/core/agent.py`：`self._msg_manager.repair/append_user/append_assistant`（L100-101、L128、L152、L165、L215-216、L250、L260）。
- 包外：`tool_exp/repair_seq_test.py:17`、`tool_exp/test_esc_interrupt_regression.py:216`。

**状态**：
- 模块级全局：无。
- 类变量：无（CompressResult 为 NamedTuple 类）。
- 实例状态：`_messages`（MessageList 引用）、`_compressor`、`_logger`（可 None）。

**行为要点**：
1. `repair()` 第 1 步：收集已回复 tool_call id（遍历 `role=="tool"` 且含 `tool_call_id` 的消息，L74-79）；再遍历 assistant 消息的 `tool_calls`，为未回复的 id 追加 `append_tool_result(tc_id, "[用户中断]")` 并标记 `repaired=True`（L81-89）。
2. `repair()` 第 2 步（**仅当第 1 步确实修复过**）：若最后一条消息是 tool → 追加 `append_assistant(SYNTHETIC_THINKING, thinking=SYNTHETIC_THINKING)`（L92-95），注释说明"思考模式下请求末尾 assistant 必须携带非空 thinking 块，否则 DeepSeek V4 返回 400"（L93-94）。
3. 修复发生时有日志 `"repair: 修复了打断后的消息序列"`（L97-98，`self._logger.info` 无 None 保护）。
4. `handle_compress` 开头日志 `"压缩触发, messages={n}条"`（L117）。
5. 切点前先算 `start`（跳过前导 system 消息，L123-125）；`cut = select_cut_index(full_messages, retain_tokens)`（L130）。
6. "无历史可压缩"提前拒绝仅限手动路径：`pending_input is None` 且（对话区为空 或 `cut <= start`）→ `CompressResult(False, reason="empty")`（L131-134）；自动路径保持旧行为（注释 L127-129 说明重建承担把触发输入写入历史并收尾的职责）。
7. 统计口径：`replaced = len(full_messages[start:cut])`（cut=None 时取到末尾，L137-138）；`tokens = sum(estimate_tokens(m) for m in replaced_msgs)`（L139）。
8. 请求前取消判定：`cancel_check()` 为真 → `on_interrupt()` + `reason="interrupted"`（L141-144）。
9. 压缩请求 = 全部历史 + 压缩指令（`build_compress_messages(full_messages)`，L147），以 `chat_stream(compress_messages, no_tools=True, cancel_check=cancel_check)` 流式收集（L152）。
10. 流处理：每 chunk 先判取消 → interrupted（L153-155）；`finish_reason == "context_overflow"` → `on_llm_error("压缩失败: 压缩请求自身超出模型上下文限制")` + `reason="overflow"`（L156-161）；`finish_reason == "error"` → 置 `llm_error=True` 并 break（L162-164）；仅当 chunk 含 `content` 且不含 `tool_calls` 时收集（L165-166）。
11. `llm_error` → `on_llm_error("压缩失败: LLM调用出错")` + `reason="llm_error"`（L168-170）。
12. 零输出且已取消 → 补判 interrupted（注释解释否则会被误归因为"总结为空"，L172-176）。
13. 空摘要（strip 后为空）→ `on_llm_error("压缩失败: 总结为空")` + `reason="empty_summary"`（L180-183）。
14. 重建：`tail = full_messages[cut:]`（cut=None → `[]`，L186）；手动路径把尾部**连续 user** 消息整段剔除（`dropped`）并计入 replaced 统计（注释：否则下次输入形成连续 user，Anthropic 严格后端强制角色交替，L188-197）；`build_new_session_messages(system_prompt, summary, tail)`（L198）。
15. 自动路径：若 `tail` 末条是 user → 把 `pending_input` 用 `"\n\n"` 合并进该条（写回 `new_messages[-1]`，L200-205）；否则 append 新 user 消息（L206-207）。
16. 原子替换 `self._messages.replace_all(new_messages)`（L208），日志 `"压缩成功，新会话已创建, 保留尾部={n}条"`（L210-213），返回 `CompressResult(True, replaced, replaced_tokens)`（L214）。
17. 失败/中断路径**不改动 messages**（docstring L112 承诺；实现上确实只在成功末尾 replace_all）。
18. `clear_and_rebuild`（L216-219）：`build_new_session_messages(system_prompt, summary_text)` + `replace_all`（与 MessageList 同名方法重叠，见补丁痕迹）。

**边界/异常行为**：
1. `repair` 在遍历 `msgs.view()`（实时视图）的同时向同一列表追加 tool 消息（L82-89）——迭代器会继续看到新追加元素，但因新增元素 role 为 tool 而被 `if` 过滤（行为正确，属脆弱写法）。
2. repair 的空判用 `len(msgs) > 0`（L92，走 MessageList.__len__）；`msgs.view()[-1]`（L92）依赖 MessageView.__getitem__ 支持负索引（list 语义）。
3. repair 不会修补"assistant 有 tool_calls 但消息顺序错误"或"tool 消息无对应 assistant"的情形（只处理"缺失 tool 回复"与"尾部 tool 无 assistant"，L81-95）。
4. `handle_compress` 对 `llm_client` 无类型约束，只要求 `chat_stream(messages, no_tools=..., cancel_check=...)` 的 chunk 协议（L152-166）。
5. `start` 假设 system 消息只能出现在消息列表前部（L123-125）；若 system 出现在对话中段（如技能注入、父会话结论注入），它会落在 `replaced_msgs`/`tail` 中参与压缩或保留（tail 侧的 system 会在 `build_new_session_messages` 中被过滤掉，compressor.py L104-106）。
6. `retain_tokens <= 0` → `select_cut_index` 返回 None → 全量压缩（无 tail）（compressor.py L45-46）。
7. `replaced`/`tokens` 仅为提示展示口径（docstring L24-27），与实际残留体积无强对应（L136-139）。
8. `reason="empty"` 只在手动路径产生（L131-134）；自动路径即便"无可压缩内容"也会走完整流程（可能把摘要替换为同内容摘要并 append pending_input）。
9. `on_interrupt`/`on_llm_error` 由调用方提供（compression_coordinator.py L49-58、L81-88、L116-120），本模块不假设其行为；二者抛异常会向调用方冒泡。
10. `pending_input` 为空串 `""` 时与非 None 判定等价（会追加空 user 消息，L199-207 不做内容校验）——未验证真实触发路径。

**补丁痕迹**：
1. **【中】遍历中修改列表**：repair 在 `for msg in msgs.view()` 内追加（L82-89），依赖"新元素被条件过滤"这一巧合（正确但脆弱）。
2. **【中】兼容性分支成对出现**：手动路径剔尾部 user（L188-197）与自动路径合并入尾部 user（L199-207）是为 Anthropic 角色交替 + 溢出恢复场景打的补丁，逻辑分散在两处。
3. **【中】参数过多**：`handle_compress` 9 个形参（含 3 个回调 + 1 个 llm 客户端）（L102-104），调用方需自备 on_interrupt/on_llm_error，编排职责部分外泄到 compression_coordinator。
4. **【低】死代码**：`MessageManager.clear_and_rebuild`（L216-219）无调用方（全仓库复验）；`MessageList.clear_and_rebuild`/`compress_and_rebuild` 亦无调用方（见 message_list 节）。
5. **【低】魔法字符串**：`"[用户中断]"`（L87）、reason 字面量集合（L134-183）散落多处，无枚举。
6. **【低】`SYNTHETIC_THINKING` 双用**：作为 assistant 的 content 与 thinking 同值（L95），跨模块契约（llm.py L1035-1037 判定该常量）。
7. **【低】logger 可 None 但多处不判空**：L98、L117、L133、L210。

**可测性**：
- 可独立单测：`repair`（构造真实 MessageList，覆盖"缺 tool 回复""尾部 tool""无需修复"三类）；`append_*` 全部为一行转发；`handle_compress` 可用**假 llm_client**（按需吐出预定 chunk 序列）+ 假 cancel_check + 真实 Compressor/MessageList，覆盖 overflow/llm_error/empty_summary/interrupted/成功（含手动与自动两条重建路径）——无需网络。
- 需集成测试：与 `CompressionCoordinator` 的协作（回调顺序、UI begin/end_compressing、context.reset）；与 `Compressor.build_new_session_messages` 的消息结构约束。
- 无法自动化：无（本模块不含终端交互/时序）。

---

### narnat_agent/core/stats.py（167行）

**职责**：Token 统计与费用追踪——累计输入/输出/缓存 token、按轮累加费用、可选（配置开关）把每次 LLM 请求写成 CSV 费用日志（含补写与轮转）、按周期查询余额。

**对外接口**：
- `COST_LOG_HEADER = [11 列]`（L14-20，模块级常量）
- `class StatsTracker:`（L23）
  - `def __init__(self, model: str, user_pricing=None, balance_cfg=None, cost_log_cfg=None):`（L26-27）
  - `def update(self, usage: dict) -> None:`（L46）
  - `def _append_cost_log(self, prompt: int, completion: int, cached: int) -> None:`（L72）
  - `def _write_cost_log_row(self, row: list) -> None:`（L100）
  - `def _rotate_to_bak(self, path: str) -> bool:`（L118）
  - `def fetch_balance(self, api_key: str, round_num: int, interval: int = 10) -> None:`（L137）
  - `@property def input_tokens(self) -> int:`（L146-147）
  - `@property def output_tokens(self) -> int:`（L150-151）
  - `@property def cache_hit_ratio(self) -> float:`（L154-155）
  - `@property def cost(self) -> float:`（L161-162）
  - `@property def balance(self) -> float:`（L165-166）

**依赖**：
- `.billing`：calculate_cost, cost_breakdown, fetch_balance（L11）
- 标准库：csv（L7）、os（L8）、time（L9）

**被依赖**：
- `narnat_agent/assembly.py:18` `from .core.stats import StatsTracker`；构造 L158-163，并在 L166-169 定义 `_set_model_with_stats` 后**直接改 `stats._model`**（L168）。
- `narnat_agent/core/agent_loop.py:17`（L30 构造参数）；使用 `input_tokens/output_tokens/cache_hit_ratio/cost/balance`（L157-161、L177-181、L200-204、L302-306、L331-335、L401-405、L433-437）与 `update(call_usage)`（L262、L342）。
- `narnat_agent/core/auto_save_manager.py:13`（L24 构造参数）；读 `input_tokens`（L48）。
- `narnat_agent/core/agent.py`：`self._stats.fetch_balance(api_key, self._round)`（L79）、`self._context.update_ratio(self._stats.input_tokens)`（L171）。

**状态**：
- 模块级全局：`COST_LOG_HEADER`（L14-20，list 字面量；无写入方，技术上可变）。
- 类变量：无。
- 实例状态：`_model`（L28，**被 assembly L168 外部改写**）、`_user_pricing`（L29）、`_balance_cfg`（L30）、`_cost_log_enabled`（L32）、`_cost_log_path`（L33）、`_cost_log_max_bytes`（L35-37）、`_cost_log_pending`（L38，写失败行队列）、`_total_input_tokens`（L39）、`_total_output_tokens`（L40）、`_total_prompt_tokens`（L41）、`_total_cache_tokens`（L42）、`_total_cost`（L43）、`_balance_to_show`（L44）。

**行为要点**：
1. `update(usage)`：`prompt = usage["prompt_tokens"]`（L51）；输出累加（L52）；**输入赋值而非累加**（L53，注释：每轮 prompt 已含全部历史，是当前上下文快照）；累计口径 `_total_prompt_tokens += prompt`、`_total_cache_tokens += cached`（L54-56，用于全程 token 加权缓存命中率）；`_total_cost += calculate_cost(model, prompt, completion, cached, user_pricing)`（L57-63）。
2. 费用日志仅在 `_cost_log_enabled` 为真时写（L65-70），与界面费用同一分项口径（docstring L3-4）。
3. `_append_cost_log`：`uncached = prompt - cached`（L78）；`cost_breakdown` 取三个分项（L79-81）；行 = [时间, 模型, prompt, cached, uncached, completion, 输入费用, 缓存费用, 输出费用, 本次费用, 累计费用]，费用均 `round(x, 9)`（L82-89）。
4. 补写语义：先把 `_cost_log_pending` 中积压行逐条写出（成功即 `pop(0)`），再写当前行；任何一次 `OSError` → 把**当前行**入队并返回（L90-98，注释：不丢行也不重复写）。
5. `_write_cost_log_row`：路径缺省 `os.path.join(".narnat","data","cost_log.csv")`（L102，相对当前工作目录）；`os.makedirs(dirname)` 确保目录（L103）；`size == 0` → 本次写表头（L104-108）；`max_bytes > 0 且 size >= max_bytes` → `_rotate_to_bak`，其返回 True 时为新文件写表头（L109-111）；以 `a` 模式 + `utf-8-sig` + `newline=""` 追加（L112-116）。
6. `_rotate_to_bak`：备份名 = 主名 + `_bak` + 扩展名（L126-127）；旧备份存在先删（L128-129）；`os.replace(path, bak)`（L130）；`OSError` → 返回 False（既删旧备份也失败时保持原文件，本次行继续追加到超限文件，L132-135）。
7. `fetch_balance(api_key, round_num, interval=10)`：`api_key` 非空且 `round_num % interval == 0` → 调 `billing.fetch_balance`，结果非 falsy 时把 `bal["total"]` 存入显示值（L139-142）；**其它轮次把 `_balance_to_show` 清 0.0**（L143-144）。
8. 属性：`input_tokens` = 最近一轮 prompt（L147）、`output_tokens` = 全程累计输出（L151）、`cache_hit_ratio` = 累计缓存/累计 prompt，分母 0 → 0.0（L155-159）、`cost` = 全程累计（L162）、`balance` = 上次可显示余额（L166）。

**边界/异常行为**：
1. `usage` 键访问：`usage["prompt_tokens"]`、`usage["completion_tokens"]` 直接下标（缺失 → KeyError 冒泡）；`cached_tokens` 用 `get(...,0)`（L51-56）。
2. `_cost_log_max_bytes` 解析失败（TypeError/ValueError）→ 0（表示不轮转）（L34-37）。
3. 写日志失败（文件被 WPS/Excel 独占）：文件被删/目录不可建时 `OSError` 同样只入队不重试（L97-98）——`_cost_log_pending` 只在**下一次** `_append_cost_log` 时才补写；若此后不再有请求，积压行永久驻留内存。
4. 轮转失败时不写表头（L111、L133-135），继续追加到原文件（尽力而为）。
5. `_balance_to_show` 的 0.0 同时表示"未到查询轮"与"余额为 0"，UI 侧无法区分（L142-144）。
6. `prompt - cached` 可能为负（服务端数据异常时），费用日志出现负数 uncached（L78）。
7. 时间使用本地时区 `time.strftime("%Y-%m-%d %H:%M:%S")`（L83）。
8. 并发：`update()` 由主线程调用（agent_loop L262/L342），无锁；若未来多线程调用会有竞态（当前调用点单线程）。
9. 模型切换后费用单价随 `_model` 变化（assembly L168 直接改字段），已累计费用不重算（L43 累计值保留）。

**补丁痕迹**：
1. **【中】余额显示语义混用**：非查询轮把 `_balance_to_show` 置 0.0（L143-144），UI 无法区分"没查"与"余额为 0"。
2. **【中】外部直改私有字段 `_model`**：assembly.py L168（模型热切换）；统计口径与配置耦合。
3. **【低】硬编码默认路径**：`".narnat/data/cost_log.csv"`（L102）绕过 `config.paths`（相对 cwd 解析）。
4. **【低】静默吞异常**：轮转失败 `except OSError: return False`（L132-135，设计上可接受但无日志）。
5. **【低】双口径并存**：`_total_input_tokens`（快照）与 `_total_prompt_tokens`（累计）两个"输入 token"字段（L39-41、L53-55），靠注释与命名区分。
6. **【低】魔法值**：`interval: int = 10`（L137）硬编码查询周期；round(…, 9) 精度魔法数（L86-88）。

**可测性**：
- 可独立单测：`update`（伪 usage dict，断言累计字段与属性）；`_append_cost_log`/`_write_cost_log_row`/`_rotate_to_bak`（tmp 目录 + 构造 `cost_log_cfg` 简易对象/None，覆盖表头、轮转、OSError 补写路径——用只读文件或目录占位模拟 OSError）；`cache_hit_ratio` 分母 0 分支。
- 需集成测试：`fetch_balance`（打桩 `billing.fetch_balance`，断言轮次整除语义与清零行为）。
- 无法自动化：CSV 被办公软件独占导致的 OSError 真实场景（可用文件锁模拟，平台相关）。

---

### narnat_agent/core/billing.py（142行）

**职责**：纯配置驱动的余额查询与费用计算——定价仅来自用户配置（无内置默认），余额查询按 `BalanceConfig` 构造请求并用简易 JSONPath 解析任意厂商响应。

**对外接口**：
- `def get_pricing(model: str, user_pricing: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[Dict[str, float]]:`（L19）
- `def cost_breakdown(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int, user_pricing: Optional[Dict[str, Dict[str, float]]] = None,) -> tuple:`（L26-32）
- `def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int, user_pricing: Optional[Dict[str, Dict[str, float]]] = None,) -> float:`（L52-58）
- `def _resolve_jsonpath(data: Any, path: str) -> Any:`（L65）
- `def fetch_balance(api_key: str, balance_cfg=None) -> Optional[Dict[str, Any]]:`（L88）

**依赖**：
- `httpx`（L10，第三方）
- `typing`（L11、L14-16）
- TYPE_CHECKING 下的 `..config.loader.BalanceConfig`（L15-16，仅类型标注，运行时无导入）

**被依赖**：
- `narnat_agent/core/stats.py:11` `from .billing import calculate_cost, cost_breakdown, fetch_balance`（本文件在包内的**唯一**导入方，grep 复验）。
- 无其它包内/包外导入（`tool_exp` 亦无）。

**状态**：
- 模块级全局：**无**。
- 类变量：无。
- 实例状态：无（全部为纯函数）。

**行为要点**：
1. `get_pricing`：仅当 `user_pricing` 非空且含该模型名时返回其定价 dict，否则 None（L21-23，docstring L20"未配置则返回None（不计算费用）"）。
2. `cost_breakdown`：无定价 → `(0.0, 0.0, 0.0)`（L42-43）；`uncached = prompt_tokens - cached_tokens`（L44）；返回 `(uncached*input/1e6, cached*cache_hit/1e6, completion*output/1e6)`（L45-49）。
3. `calculate_cost` = 三项之和（L60-62）。
4. `_resolve_jsonpath`：空 path → None（L70-71）；按 "." 分段；list 用 `int(part)` 索引（ValueError/IndexError → None，L76-80）；dict 用 `.get(part)`（L81-82）；标量中途 → None（L83-84）。
5. `fetch_balance`：`balance_cfg is None` → None（L98-99）；兼容 dict 与对象两种传入（L102-113，字段 enabled/url/auth_method/value_path/currency_path，默认 auth_method="bearer"）；`not enabled or not url` → None（L115-116）。
6. 请求：`x-api-key` 或 `Authorization: Bearer <key>`（L119-123）；`httpx.Client(timeout=8.0)` GET（L125-126）；`status_code != 200` → None（L127-128）；解析 JSON（L129）。
7. `value_path` 解析出的 total 为 None → None（L131-133）；否则返回 `{"total": float(total)}`（L135）；`currency_path` 存在且解析成功时补 `"currency"` 字段（L136-139）。
8. 任何异常（网络/超时/JSON 解析/类型转换）→ None（L141-142）。

**边界/异常行为**：
1. 全函数 `except Exception: return None`（L141-142）——错误原因完全不可见（无日志、无区分）。
2. `cost_breakdown` 直接下标取 `p["input"]`、`p["cache_hit"]`、`p["output"]`（L46-48）：用户定价缺键 → KeyError 冒泡（未捕获；调用方 StatsTracker.update L57 亦不捕获 → 会终止该轮统计调用，未验证真实影响）。
3. `uncached` 可能为负（cached > prompt），无钳制（L44）。
4. 时间单位：定价按"每百万 token"（L46-48 的 `/ 1_000_000`）。
5. `float(total)` 转换非数值字符串 → 抛 ValueError → 被外层吞掉返回 None（L135、L141）。
6. 无重试、无退避、无缓存（每次调用都可能发 HTTP，由 StatsTracker 的轮次整除控制频率；L118-142）。
7. `balance_cfg` 为 dict 时字段缺失用 `.get(..., 默认)`（L103-107）；为对象时用 `getattr(..., 默认)`（L109-113）。

**补丁痕迹**：
1. **【中】吞掉全部异常**：L141-142（"余额查询失败"与"响应格式不符"不可区分）。
2. **【中】定价键缺失不设防**：L46-48 直取三键，配置错误会在统计路径抛 KeyError（跨模块冒泡，见边界 2）。
3. **【低】`_resolve_jsonpath` 只支持点号路径与数字索引**（L65-68 docstring 明示），不支持 `[*]`/负索引/带引号键。
4. **【低】dict/对象双形态兼容**：L102-113 两套取值分支，为兼容测试与被 loader 复用而保留（无类型约束）。
5. **【低】魔法默认值**：`auth_method` 缺省 "bearer"（L105、L111）、timeout 8.0 秒硬编码（L125）。

**可测性**：
- 可独立单测（纯函数，无需网络）：`get_pricing`、`cost_breakdown`、`calculate_cost`、`_resolve_jsonpath`（含 list 索引/越界/标量中途/空 path 各分支）。
- 可单测但需打桩：`fetch_balance` 的 httpx 调用（推荐用 `httpx.MockTransport` 或打桩 `httpx.Client`；覆盖非 200、JSON 解析失败、value_path 命中/未命中、dict 与对象两种 cfg）。
- 无法自动化：真实厂商余额接口行为（未验证）。

---

### narnat_agent/core/message_list.py（126行）

**职责**：messages 列表的唯一所有者（受控修改入口）与只读视图（MessageView），消除多处共享同一 list 引用的问题。

**对外接口**：
- 模块级常量 `SYNTHETIC_THINKING = "（用户中断了工具执行）"`（L14，含 6 行注释说明 DeepSeek 思考模式校验与回传开关的关系，L9-13）。
- `class MessageView:`（L17）
  - `def __init__(self, messages: List[Dict[str, Any]]):`（L20）
  - `def __len__(self) -> int:`（L23）
  - `def __getitem__(self, index: int) -> Dict[str, Any]:`（L26）
  - `def __iter__(self) -> Iterator[Dict[str, Any]]:`（L29）
  - `def to_list(self) -> List[Dict[str, Any]]:`（L32）：浅拷贝列表。
  - `def count_role(self, role: str) -> int:`（L36，**无调用方**）
- `class MessageList:`（L41）
  - `def __init__(self, system_prompt: str):`（L48）
  - `def view(self) -> MessageView:`（L55）
  - `def __len__(self) -> int:`（L59）
  - `def append_system(self, content: str) -> None:`（L64）
  - `def append_user(self, content: str) -> None:`（L68）
  - `def append_assistant(self, content: str, tool_calls: Optional[list] = None, thinking: Optional[str] = None, thinking_signature: Optional[str] = None) -> None:`（L72-74）
  - `def append_tool_result(self, tool_call_id: str, result: str) -> None:`（L90）
  - `def append_interrupted_tools(self, tool_calls: list, completed_ids: set) -> None:`（L98）
  - `def replace_all(self, new_messages: List[Dict[str, Any]]) -> None:`（L108）
  - `def clear_and_rebuild(self, system_prompt: str, summary: str, compressor) -> None:`（L113-114，**无调用方**）
  - `def compress_and_rebuild(self, system_prompt: str, summary: str, pending_input: str, compressor) -> None:`（L120-121，**无调用方**）

**依赖**：
- 仅标准库 `typing`（L7）。无内部模块依赖（全仓库依赖图的最底层之一）。

**被依赖**：
- `narnat_agent/assembly.py:17` `from .core.message_list import MessageList`；构造 L88（`MessageList(config.system_prompt)`）；经 AssemblyResult 暴露（L196、L213、L221）。
- `narnat_agent/core/message_manager.py:10`（`MessageList, MessageView, SYNTHETIC_THINKING`）。
- `narnat_agent/core/session_callbacks.py:22`（MessageList，类型标注）。
- `narnat_agent/core/auto_save_manager.py:11`（MessageList，构造参数类型）。
- `narnat_agent/core/llm.py:25` `from .message_list import SYNTHETIC_THINKING`（使用于 L1035-1037：思考回传时允许合成占位）。
- 包外：`tool_exp/repair_seq_test.py:55/75/117`、`tool_exp/verify_mode_cmd.py:53`、`tool_exp/test_esc_interrupt_regression.py:217`。

**状态**：
- 模块级全局：`SYNTHETIC_THINKING`（L14，str，只读）。
- 类变量：无。
- 实例状态：`MessageView._messages`（L21，**内部列表的引用**，非拷贝）；`MessageList._messages`（L49-51，唯一可变数据，初值含一条 system）。

**行为要点**：
1. 构造即写入 `{"role":"system","content":system_prompt}`（L49-51）——消息列表永远以 system 开头。
2. `view()` 每次返回**新的** MessageView 实例（零拷贝，L56-57），但视图共享同一内部列表：视图生命周期内列表变化立即可见。
3. `append_assistant`：`content or None`（空串/None → content 字段为 None，L81）；`thinking is not None` 才写 `thinking` 字段（空串是合法值，L82-83，docstring L78 明示）；`thinking_signature is not None` 才写（L84-85）；`tool_calls` 非空才写（L86-87）。
4. `append_tool_result`：写入 `{"role":"tool","tool_call_id":...,"content":...}`（L92-96）。
5. `append_interrupted_tools`：对不在 `completed_ids` 中的 tool_call 追加 `"[用户中断]"` 结果（L100-106）；**依赖每个 tc 含 `id` 键**（L101 直接下标）。
6. `replace_all`：`clear()` + `extend(new_messages)`（L110-111）——原地替换，保持对象同一性（外部持有的引用持续有效）。
7. `clear_and_rebuild` / `compress_and_rebuild`：调 `compressor.build_new_session_messages(...)` 后 clear+extend（L113-126）；两者均无调用方。
8. `MessageView.to_list()` 返回浅拷贝（L32-34）：调用方可改列表结构而不影响内部；但元素 dict 仍共享（未深拷贝）。

**边界/异常行为**：
1. "只读视图"并不真正只读：`__getitem__` 返回内部 dict 引用（L26-27），调用方可通过 `view()[0]["content"]="x"` 篡改内部消息（结构不可改，内容可改）。`to_list()` 同理（浅拷贝，dict 共享）。
2. `__len__`/`__getitem__` 对越界索引抛 IndexError（无包装）；`view()[-1]`（message_manager L92）依赖 list 负索引语义。
3. `append_assistant(content="")` 会写入 `content: None`（L81），与"空串是合法值"注释（L78 针对 thinking）区分开。
4. `append_interrupted_tools` 中 `tc["id"]` 缺失 → KeyError（L101，无 `.get`）。
5. `replace_all([])` 允许把消息列表清空（无最小长度保护，L108-111）；调用方需自行保证不违反 API 约束（如"首条 system 或 user"）。
6. 无并发保护：内部 list 的读写无锁；跨线程访问（AutoSaveManager._do_save 在后台线程 `view().to_list()`，auto_save_manager.py L58/L62-63）依赖主线程调用时序（见 auto_save_manager 节）。
7. `SYNTHETIC_THINKING` 的文本被 llm.py 用作"是否回传合成思考"的判定依据（llm.py L1037 `t == SYNTHETIC_THINKING`）——字符串严格相等，改文本会跨模块失效。

**补丁痕迹**：
1. **【中】"只读视图"名不副实**：`MessageView.__getitem__`/`to_list` 暴露可变 dict（L26-27、L32-34），封装承诺（文件头 docstring L2-5）与实现有落差。
2. **【中】跨模块字符串契约**：`SYNTHETIC_THINKING`（L14）被 llm.py 做相等判定（llm.py L1037），契约靠注释维系（L9-13）。
3. **【低】死代码**：`count_role`（L36-38）、`clear_and_rebuild`（L113-118）、`compress_and_rebuild`（L120-126）无调用方；后两者与 `MessageManager.clear_and_rebuild`（message_manager.py L216）职责重叠（三处同名/近名实现）。
4. **【低】转发方法膨胀**：`append_*` 在 MessageList / MessageManager / SessionManager（append_system 用于技能注入）三处各有一层，纯转发成本。

**可测性**：
- 全部可独立单测（无 IO、无外部依赖）：`view`/`to_list` 浅拷贝语义、`append_assistant` 的字段省略规则、`replace_all` 的对象同一性、`append_interrupted_tools` 的补全集合语义、`SYNTHETIC_THINKING` 常量值（作为跨模块契约的金标准断言）。
- 需集成测试：与 session_store 序列化/反序列化的往返一致性（存盘 JSON 后读回、`thinking`/`tool_calls` 字段保留）。
- 无法自动化：无。

---

### narnat_agent/core/auto_save_manager.py（83行）

**职责**：NoSession 阶段的自动保存——后台线程用 LLM 给会话命名并写盘，主循环在输入同步点完成状态切换；退出时保存当前会话并执行延迟删除。

**对外接口**：
- `class AutoSaveManager:`（L19）
  - `def __init__(self, config: Config, message_list: MessageList, session_mgr: SessionManager, summarizer: Summarizer, stats: StatsTracker, logger: AgentLogger):`（L22-24）
  - `def try_save(self):`（L33）
  - `def _do_save(self):`（L56）
  - `def wait(self):`（L66）
  - `def on_exit(self):`（L76）

**依赖**：
- `..config.loader`：Config（L10）
- `.message_list`：MessageList（L11）
- `.session_callbacks`：SessionManager（L12）
- `.stats`：StatsTracker（L13）
- `.summarizer`：Summarizer（L14）
- `..logger`：AgentLogger（L15）
- `..output`：write as _stdout_write, D, E, R（L16）
- 函数内 `from .session_callbacks import NoSession`（L44，模块顶层已 import 同类所在的模块）
- 函数内 `from ..config.session_store import save_session`（L61）
- 标准库：threading（L7）、typing（L8）

**被依赖**：
- `narnat_agent/assembly.py:23` `from .core.auto_save_manager import AutoSaveManager`；构造 L172-174（注入 config/message_list/session_mgr/summarizer/stats/logger）。
- `narnat_agent/core/agent.py`：`self._auto_save = self._parts.auto_save_mgr`（L34）；`self._auto_save.wait()`（L54，输入同步点）、`self._auto_save.try_save()`（L134、L161）、`self._auto_save.on_exit()`（L67，退出）。
- 无其它导入方（grep 复验：仅 assembly + agent 经 AssemblyResult 使用）。

**状态**：
- 模块级全局：无。
- 类变量：无。
- 实例状态：`_config`、`_message_list`、`_mgr`（SessionManager）、`_summary`（Summarizer）、`_stats`、`_logger`（L25-30）、`_auto_save_thread`（L31，可 None）。
- **跨对象私有字段**（AutoSaveManager 读写 SessionManager 的私有成员）：`_mgr._auto_save_done`（L42、L50）、`_mgr._pending_auto_save_name`（L64、L71-73）、`_mgr.state`（L45、L79、L81）、`_mgr.switch_state/create_root_state`（L74）、`_mgr.cleanup_deletes`（L83）。

**行为要点**：
1. `try_save` 门控顺序（L40-49）：① `config.session.auto_save` 关闭 → 直接返回；② `_mgr._auto_save_done` 已为 True → 返回（**整个进程生命周期只自动保存一次**）；③ 当前状态不是 NoSession → 返回（已有会话不重复自动保存）；④ `auto_save_tokens > 0` 且 `_stats.input_tokens <= threshold` → 返回（token 门槛，0=无门槛；注释 L36-38 说明用途是避免随口聊几句就生成命名会话）。
2. 通过门控后立即置 `_auto_save_done = True`（L50）并启动 daemon 线程 `_do_save`（L52-54）——主线程不阻塞。
3. `_do_save`（后台线程）：`summarizer.name_session(messages)`（L58，取 `view().to_list()` 快照）；名为空 → 直接返回（**不再重试**，`_auto_save_done` 已置位）；否则 `save_session(narnat_dir, name, messages)` 写盘（L61-63）并置 `_mgr._pending_auto_save_name = name`（L64）。
4. `wait`（主线程同步点）：`join(timeout=5)` 后置空线程引用（L68-70）；若 `_pending_auto_save_name` 非空 → 清空并把状态切为 `RootSession(name)`（L71-74）；幂等（无 pending 时为 no-op）。
5. `on_exit`（agent 退出路径）：先 `wait()`（确保后台命名完成并切换状态，L78）；若当前状态有会话名 → `on_auto_save()`（落盘当前会话）+ 打印 `"会话已自动保存: {name}"`（L79-82）；最后 `cleanup_deletes()` 执行 `/rm` 延迟删除（L83）。

**边界/异常行为**：
1. `try_save` 判定用 `_mgr.state`（公开属性）但读私有标记（L42、L50）；`_auto_save_done` 一旦置位即永久生效——**用户在 NoSession 阶段删掉自动保存的会话后**不会再有自动保存（未验证：`_auto_save_done` 无复位点，全仓库复验）。
2. 后台线程读 `self._message_list.view().to_list()`（L58、L62-63）与主线程可能的写入之间存在竞态窗口：正常时序是"轮末 try_save → 用户下次输入时 wait()"，但如果用户输入间隔 <5s 且主线程已开始新一轮 append（agent.py L100-101 在 wait 之后才执行，L54 先 wait），竞态窗口取决于 wait 超时后的后续写入顺序（时序相关，无法静态确认，标「未验证」）。
3. `join(timeout=5)` 超时后线程继续存活（daemon），`_pending_auto_save_name` 可能稍后被后台线程写入——下一次 `wait()` 才消费（L68-74 幂等，因此不会丢，但状态切换时点后移）。
4. `name_session` 返回空串（LLM 失败/取消）→ 不保存、不切换（会话停留在 NoSession，用户需手动 /save）。
5. `save_session` 的返回值（错误串）被忽略（L62-63）——写盘失败时仍会置 `_pending_auto_save_name` 并切换为 RootSession，状态指向一个不存在的文件（未验证真实触发路径）。
6. `on_exit` 中 `session_name()` 为 None（NoSession）时不打印、不落盘，但仍执行 `cleanup_deletes()`（L79-83）。
7. 平台/编码无分支；线程异常（summarizer 抛异常）不会被捕获（线程启动 L52-54、_do_save L56-64 无 try），仅在后台线程终止时打印回溯（未验证）。

**补丁痕迹**：
1. **【高】跨对象私有成员读写**：`_mgr._auto_save_done`（L42、L50）、`_mgr._pending_auto_save_name`（L64、L71-73）——两个"一次性"标志跨对象共享，无接口、无锁、无复位。
2. **【中】函数内重复/延迟 import**：L44（NoSession，模块 L12 已导入同模块）、L61（save_session）——import 位置与模块顶部风格不一致。
3. **【中】后台线程触碰主线程数据结构**：两次 `view().to_list()`（L58、L62-63）分别用于命名与落盘，快照可能不同（命名后消息若有新增，落盘内容包含但命名基于旧快照），且无并发保护。
4. **【中】`wait()` 超时静默**：join 5s 超时后无任何提示（L69）。
5. **【低】忽略 save_session 错误**（L62-63）。
6. **【低】输出文案硬编码**（L82），与命令层（ui/session_commands.py L283/L287）两套"已保存"提示并存。

**可测性**：
- 可独立单测（假 summarizer + tmp narnat_dir + 简易 Config 替身）：`try_save` 的四道门控（含 `_auto_save_done` 一次性语义）、`_do_save`（空名/正常）、`wait`（join + 状态切换 + 幂等）、`on_exit`（打印 + cleanup_deletes 调用）。
- 需集成测试：与真实 Summarizer（需 LLM）联调；线程竞态场景（需要压力/时序注入，建议在小规模下用 Event 控制线程节拍）。
- 无法自动化：LLM 命名质量；真实 5s join 超时下的行为（需构造慢 LLM）。

---

### narnat_agent/core/tool_callbacks.py（49行）

**职责**：工具相关回调实现——Windows 删除命令确认（`SafetyCallbacks`）与 TodoWrite 的终端 UI 更新（`TodoCallbacks`）。

**对外接口**：
- `class SafetyCallbacks:`（L12）
  - `@staticmethod def confirm_delete(command: str) -> bool:`（L15-16）
- `class TodoCallbacks:`（L26）
  - `@staticmethod def on_todo_update(todos):`（L29-30）

**依赖**：
- `..output`：write as _stdout_write, B, D, E, G, R, Y, is_quiet_tools（L9）
- 标准库：sys（L7）

**被依赖**：
- `narnat_agent/assembly.py:20` `from .core.tool_callbacks import SafetyCallbacks, TodoCallbacks`；使用：`SafetyCallbacks.confirm_delete`（L99，仅非 headless 且 win32 时注册为 `ToolContext.confirm_callback`）、`TodoCallbacks.on_todo_update`（L102，注册为 `ToolContext.ui_callback`）。
- 无其它包内导入方（grep 复验）。
- 包外：`tool_exp/verify_review_fixes.py:25`、`tool_exp/verify_todo_no_activeform.py:14/16`（引用 `tcb` 模块属性）。

**状态**：
- 模块级全局：无。
- 类变量：无。
- 实例状态：无（两个类仅静态方法，无 `__init__`）。

**行为要点**：
1. `SafetyCallbacks.confirm_delete`：非 win32 平台直接返回 False（L17-18，**不弹确认**；Linux/macOS 走 AWAIT_CONFIRM 挂起机制，见 tool_dispatcher/tool_context 注释）；win32 → `input("  确认执行此命令? [y/N]: ")`，`strip().lower() in ("y","yes")` 为 True（L20-21）；`EOFError`/`KeyboardInterrupt` → False（L22-23）。
2. `TodoCallbacks.on_todo_update`：`is_quiet_tools()` 为真时整体跳过（L31-32，headless 模式由 main.py L45-47 设置）。
3. 每种状态的行格式（L34-49）：`completed` → `"  {E}✓{R} {D}{content}{R}"`；`in_progress` → `"  {Y}●{R} {B}正在{content}{R}"`，若 content 已以 `"正在"` 开头则不再叠加前缀（L42-44，注释：避免"正在正在…"）；其它状态 → `"  {G}○{R} {D}{content}{R}"`。
4. 每条 todo 单独一行写出（`line + "\n"`，L49）；依赖 `t["status"]` 与 `t.get("content","")`（L34-35）。

**边界/异常行为**：
1. `todos` 中缺少 `status` 键 → KeyError（L34 直接下标；数据来源是 AI 工具参数，schema 外输入会崩到 UI 回调调用点，未验证是否被上游捕获）。
2. `content` 缺失 → 空串（L35 用 get）。
3. 未知 status（如 "pending" 之外的自定义值）走 else 分支按未开始渲染（L45-47）。
4. 平台分支使 Windows 与 Linux/macOS 的确认交互路径完全不同：Windows 由本类同步阻塞 `input()`（在工具执行线程内，未验证具体调用栈），Linux/macOS 由 AgentLoop 在提示符下等待（agent_loop.py L411-464）。
5. Windows `input()` 在无 stdin 环境（重定向/服务）抛 EOFError → False（拒绝执行，L20-23）——fail-safe。
6. 输出无颜色清理（依赖 output 的 _PLAIN 全局与 _Color 求值），headless 时依赖 is_quiet_tools 提前返回（否则会带 ANSI 或纯文本输出）。

**补丁痕迹**：
1. **【中】平台分裂的确认机制**：Windows=回调内 `input()`，Linux/macOS=AWAIT_CONFIRM+AgentLoop 挂起（L17-18 与 agent_loop.py L239-254、L411-464）——同一用户流程两套实现，回归面翻倍。
2. **【低】`t["status"]` 无防御**（L34）。
3. **【低】中文前缀特判**：`content.startswith("正在")`（L43）把展示逻辑建立在 AI 文案习惯上。
4. **【低】"正在"前缀拼接**无空格地直接拼 content（L44），且该特判只覆盖前缀"正在"，未覆盖"已經/正"等变体。

**可测性**：
- 可独立单测：`confirm_delete`（monkeypatch `builtins.input` 与 `sys.platform`，覆盖 y/yes/N/EOF/非 win32）；`on_todo_update`（捕获 stdout 断言三种图标与"正在"去重，覆盖静默模式）。
- 需集成测试：与 ToolContext 的注册链路（assembly 注入 → 工具调用 → 回调触发）。
- 无法自动化：真实 Windows 控制台 `input()` 的交互手感（回车/中断语义）。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

本组文件为起点（出边 = 本文件 import 谁；入边见各文件"被依赖"节，此处列出跨组关键边）：

```
session_callbacks.py → config.session_store (L16-20)
session_callbacks.py → config.skill_store   (L21)
session_callbacks.py → core.message_list    (L22)
session_callbacks.py → ui.colors [函数内]    (L136, L248, L440)

tool_dispatcher.py   → tools.registry       (L11)
tool_dispatcher.py   → tools.bash           (L12)
tool_dispatcher.py   → tools.terminal       (L13, L14)
tool_dispatcher.py   → tools.serial         (L15)
tool_dispatcher.py   → tools.tool_context   (L16)
tool_dispatcher.py   → tools.exec_signal    (L17)
tool_dispatcher.py   → output               (L18)

message_manager.py   → core.compressor      (L9)
message_manager.py   → core.message_list    (L10)
message_manager.py   → logger               (L11)

stats.py             → core.billing         (L11)

billing.py           → httpx（第三方）       (L10)
billing.py           → config.loader [TYPE_CHECKING] (L15-16)

message_list.py      → （无内部依赖）

auto_save_manager.py → config.loader        (L10)
auto_save_manager.py → core.message_list    (L11)
auto_save_manager.py → core.session_callbacks (L12, L44[函数内])
auto_save_manager.py → core.stats           (L13)
auto_save_manager.py → core.summarizer      (L14)
auto_save_manager.py → logger               (L15)
auto_save_manager.py → output               (L16)
auto_save_manager.py → config.session_store [函数内] (L61)

tool_callbacks.py    → output               (L9)
```

入边（谁 import 本组文件；同组内互导已含在出边）：

```
assembly.py → tool_dispatcher (L15), message_manager (L16), message_list (L17),
              stats (L18), session_callbacks (L19), tool_callbacks (L20),
              auto_save_manager (L23)
agent_loop.py → message_manager (L12), tool_dispatcher (L13), stats (L17)
compression_coordinator.py → message_manager (L12)
llm.py → message_list (L25, SYNTHETIC_THINKING)
auto_save_manager.py → session_callbacks (L12, L44)
stats.py → billing (L11)
core/agent.py → 经 AssemblyResult 使用 session_mgr/msg_manager/stats/auto_save_mgr/dispatcher（L30-35）
ui/session_commands.py → （持 mgr 引用，非 import）session_callbacks 的公开命令面（L58-397）
ui/ui_design.py → （持 session_manager 引用）L283-285, L322, L410
main.py → output.set_quiet_tools（L45-47，影响 tool_dispatcher/tool_callbacks 的静默分支）
```

### 模块级可变状态全清单（含读写方）

本组文件的"模块级可变全局"实际为**零**（Phase 6 收敛后的现状），下列为等价的类级/跨模块可变状态，重构时必须保留其读写关系：

| 状态 | 声明处 | 读 | 写 |
|------|--------|----|----|
| `ChildSession.BOUNDARY_MARKER_PREFIX` / `SUMMARY_TASK_TEMPLATE` | session_callbacks.py L342 / L345-392 | done() L476 / L492 | 无 |
| `ToolDispatcher.READONLY_TOOLS/WRITE_TOOLS/TOOL_LABELS/FILE_PATH_TOOLS` | tool_dispatcher.py L53-74 | L118/L120/L408/L425 | 无 |
| `ToolDispatcher.SERIAL_TOOLS`（未使用） | tool_dispatcher.py L55 | 无 | 无 |
| `SYNTHETIC_THINKING` | message_list.py L14 | message_manager.py L95、llm.py L1037 | 无 |
| `COST_LOG_HEADER` | stats.py L14-20 | _write_cost_log_row L115 | 无 |
| `SessionManager._goal_enabled` | session_callbacks.py L622 | ui/session_commands.py L266、agent.py L105 | ui/session_commands.py L242、L255；agent.py L207 |
| `SessionManager._goal_max_rounds` | L623 | ui/session_commands.py L268、agent.py L107 | ui/session_commands.py L243、L256；agent.py L208 |
| `SessionManager._goal_default_rounds` | L624 | ui/session_commands.py L268 | 构造期（assembly L134） |
| `SessionManager._set_goal_tool` | L611 | — | assembly L133 注入；调用于 ui/session_commands.py L246、L259、agent.py L210 |
| `SessionManager.compact_func` | L610 | on_compact L730 | assembly L182 |
| `SessionManager.summary_anim_start/stop` | L605-606 | done L495/L500 | assembly L147-148 |
| `SessionManager._set_model` | L602 | on_mode L832 | assembly L169 |
| `SessionManager._auto_save_done` | L616 | auto_save_manager L42 | auto_save_manager L50 |
| `SessionManager._pending_auto_save_name` | L617 | auto_save_manager L71 | auto_save_manager L64；清理 L73 |
| `SessionManager.pending_deletes` | L618 | session_callbacks L138/L250/L442/L654/L657/L661-662/L869/L872/L883 | L170/L172/L177/L183/L285/L300 |
| `SessionManager._state` | L619 | 全命令经 `state` 属性（L698-700） | switch_state（L638）+ on_exit（L741） |
| `StatsTracker._model` | stats.py L28 | update L58、_append_cost_log L84 | assembly.py L168（外部直改） |
| `output._QUIET_TOOLS`（外部，影响本组） | output.py L46 | tool_dispatcher L412/L529/L538、tool_callbacks L31 | main.py L47 |
| `ToolContext.require_plan / min_tools`（外部） | tool_context.py L44-45 | tool_dispatcher L334/L354 | 构造期（assembly L110-111） |
| `ToolContext.current_todos`（外部） | tool_context.py L51 | tool_dispatcher L358 | TodoWrite 工具、agent.py L91 |
| `ToolContext.pending_delete / _delete_confirmed / goal_complete / todo_reminded / bg_reminded`（外部） | tool_context.py L55-69 | agent_loop L239-247、L451 | 各工具与 agent.py L84-88、agent.py L147/L244、agent_loop L354/L374 |

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

1. 【高】SessionManager 私有自动保存标记被 AutoSaveManager 跨对象读写：`session_callbacks.py:616-617` ←→ `auto_save_manager.py:42,50,64,71-74`。无接口、无锁、`_auto_save_done` 无复位点。
2. 【高】状态机私有目标模式字段被 UI/Agent 跨层直读写：`session_callbacks.py:622-624` ←→ `ui/session_commands.py:242-268`、`core/agent.py:105-107,207-211`；`_set_goal_tool` 亦被直调（`ui/session_commands.py:245-246,258-259`）。
3. 【高】三态 `enter()`（`session_callbacks.py:142-162 / 254-275 / 446-467`）与三态 `show()`（`127-140 / 241-252 / 433-444`）近乎逐行重复的三份实现；`_persist()` 两份同构（`219-226 / 421-431`）。
4. 【中】core→ui 反向依赖以函数内延迟 import 实现，三处复制：`session_callbacks.py:136,248,440`。
5. 【中】tool_dispatcher 三处"提交+0.05s 忙等轮询+异常兜底"重复：`tool_dispatcher.py:146-166,194-215,279-298`；且忙等无超时上限。
6. 【中】死代码与失效分类：`tool_dispatcher.py:55`（`SERIAL_TOOLS` 全无引用）、`message_list.py:36-38,113-126`（count_role/clear_and_rebuild/compress_and_rebuild 无调用）、`message_manager.py:216-219`（clear_and_rebuild 无调用）、`session_callbacks.py:630-632,895-896`（get_message_list/has_active_session 无调用）；包外脚本引用已删符号 `_command_exec_failed`（tool_exp/repro_ui_fail_judge.py:11 等）。
7. 【中】双轨失败判定与工具名硬编码：`tool_dispatcher.py:242-270`（标签判定 vs `startswith("[错误")`），名单 `("Shell","Terminal")`/`mcp__` 前缀写死。
8. 【中】历史兼容分支无写入方：`session_callbacks.py:342,474-478`（BOUNDARY_MARKER_PREFIX 仅被搜索）；`ChildSession.done` 另从磁盘重读子会话并忽略错误（`519-520`），存在"内存新内容不入盘/被旧盘面覆盖"的脆弱边界。
9. 【中】吞异常与静默失败：`tool_dispatcher.py:186-189`（组崩溃仅补 UI 提示、无日志、不补结果）、`billing.py:141-142`（全部异常 → None）、`session_callbacks.py:781-782,811-812,842-843`（配置写失败静默）、`stats.py:132-135`（轮转失败静默）。
10. 【中】Platform 分裂的删除确认（`tool_callbacks.py:17-18` 与 `agent_loop.py:239-254,411-464`）+ 可变 dict 冒充只读视图（`message_list.py:26-27,32-34`）。

（其余"低"级项见各文件节：余额清零语义 `stats.py:143-144`、定价缺键 KeyError `billing.py:46-48`、`t["status"]` 无防御 `tool_callbacks.py:34`、repair 遍历中修改 `message_manager.py:82-89`、隐式树契约 `session_callbacks.py:652-658` 等。）

### 本组对外契约清单（被 core 之外模块依赖的 public API，即新架构必须保持的行为面）

按"外部调用方 → 必须保持的行为"，只列跨模块边界（core 之外 + assembly）：

**A. SessionManager（调用方：ui/session_commands.py、ui/ui_design.py、assembly.py、auto_save_manager.py、core/agent.py）**
1. `available_commands() -> Dict[str,str]`：键=命令名（含 "/" 前缀）、值=中文说明；命令可用性随状态变化（ui/session_commands.py:58,397；ui/ui_design.py 补全器）。
2. `on_save/on_show/on_enter/on_delete/on_explore/on_done/on_exit -> str`：空串=成功；非空=给用户看的错误/提示文案（ui/session_commands.py:188-378）。
3. `on_compact() -> (status, text)`：status ∈ {"ok","empty","error"}；"error" 且 text 含"取消"时命令层按"非错误"着色（ui/session_commands.py:188-198）。
4. `on_skill/on_thinking/on_thinkback/on_mode -> str`：返回文案由命令层直接输出（ui/session_commands.py:317-361）。
5. `on_list_skill_tree/on_list_names_tree/on_list_rm_names/on_list_thinking_options/on_list_model_names -> list`：Tab 补全数据源（键名固定，ui/session_commands.py:39-43,114）。
6. `state` 属性 + `state.session_name()`（ui/session_commands.py:285）；`is_child_session()`/`should_exit_agent()`（L367-371）：决定 `/exit` 后是否结束进程与提示文案。
7. `on_auto_save()`：轮末/退出落盘（core/agent.py:133,160；auto_save_manager.py:80）。
8. 私有契约（虽非 public 但被外部依赖）：`_goal_enabled/_goal_max_rounds/_goal_default_rounds/_set_goal_tool`（session_callbacks.py:611,622-624；ui/session_commands.py:242-268；core/agent.py:105-107,207-211）、`_auto_save_done/_pending_auto_save_name`（session_callbacks.py:616-617；auto_save_manager.py:42,50,64,71-73）、`compact_func/summary_anim_start/summary_anim_stop/_set_model`（session_callbacks.py:602,605-606,610；assembly.py:147-148,169,182）。
9. 构造签名（assembly.py:116-137）：20 个参数，含中文无关的回调注入点与默认值。

**B. ToolDispatcher（调用方：core/agent_loop.py、core/agent.py、assembly.py）**
1. `execute_tool_calls(tool_calls, stream) -> List[(tool_call_id, result)]`：按原始顺序、只含已完成/有条目者；取消时返回部分结果（agent_loop.py:228-233 依赖"缺失项=未完成"语义）。
2. 序列化协议：`tc["id"]`、`tc["function"]["name"]`、`tc["function"]["arguments"]`（JSON 字符串）；`result` 为已 `strip_tags` 的纯文本（agent_loop.py:258；llm 侧不再处理标签）。
3. `tool_calls` 批内 TodoWrite 先行、`require_plan` 拦截的文案与结果结构（tool_dispatcher.py:96-99,331-384；agent_loop.py:228 直接回传消息列表）。
4. 终端输出契约（用户可见行为）：`  [{label}] {summary}`、`  [{label}失败]`、diff 缩进两空格等（tool_dispatcher.py:522-541；回归验收点）。
5. 私有契约：`dispatcher._executor.shutdown(wait=False)`（core/agent.py:177,268）。

**C. MessageManager（调用方：core/agent_loop.py、core/compression_coordinator.py、core/agent.py、assembly.py）**
1. `repair()`：无返回值；副作用=补齐 `"[用户中断]"` tool 结果 + 必要时补合成 assistant（agent_loop.py:98；agent.py:100,215）。
2. `handle_compress(...) -> CompressResult(ok, replaced, tokens, reason)`：reason ∈ {empty, interrupted, llm_error, empty_summary, overflow}（compression_coordinator.py:142-154 逐项映射用户文案）。
3. `view`（属性）→ 具备 `to_list()` 的对象（agent_loop.py:110,474）。
4. `append_user/append_assistant/append_tool_result/append_interrupted_tools`：签名与字段省略规则（agent_loop.py:115,221,258,315,357）。
5. 失败/中断不改动 messages、成功才 replace_all 的语义（message_manager.py:208-214；compression_coordinator 的善后回调依赖它）。

**D. StatsTracker（调用方：core/agent_loop.py、core/agent.py、core/auto_save_manager.py、assembly.py）**
1. `update(usage: dict)`：usage 键 `prompt_tokens/completion_tokens/cached_tokens`（agent_loop.py:262,342）。
2. 属性 `input_tokens/output_tokens/cache_hit_ratio/cost/balance`：UI 统计栏与占比刷新的输入（agent_loop.py:157-161 等；agent.py:171）。
3. `fetch_balance(api_key, round_num, interval=10)`（agent.py:79）。
4. 私有契约：`stats._model = v`（assembly.py:168）。

**E. MessageList（调用方：assembly.py、core/llm.py（常量）、core/message_manager.py、session_callbacks.py、auto_save_manager.py）**
1. 构造 `MessageList(system_prompt)` 即写入首条 system（assembly.py:88）。
2. `view()/to_list()` 浅拷贝语义（message_list.py:32-34；llm 请求体、落盘序列化）。
3. `SYNTHETIC_THINKING` 常量文本（llm.py:1037 相等判定）。
4. `append_assistant(content or None, thinking=..., thinking_signature=...)` 的字段规则（message_list.py:72-88；历史会话序列化 / 回传契约）。

**F. AutoSaveManager（调用方：core/agent.py、assembly.py）**
1. `wait()`（输入同步点，agent.py:54）、`try_save()`（轮末，L134/L161）、`on_exit()`（L67，含延迟删除）。
2. 用户可见行为：自动保存完成后下一轮输入前切换为 RootSession；退出时打印 `"会话已自动保存: {name}"`（auto_save_manager.py:66-83）。

**G. ToolCallbacks（调用方：assembly.py 注册进 ToolContext）**
1. `SafetyCallbacks.confirm_delete(command) -> bool`（tool_callbacks.py:17-23；仅 win32 生效，fail-safe False）。
2. `TodoCallbacks.on_todo_update(todos)`：终端逐行渲染契约（tool_callbacks.py:31-49；三种图标、"正在"前缀去重、静默模式跳过）。

**H. billing（调用方：core/stats.py）**
1. `calculate_cost/cost_breakdown/model, user_pricing` 口径：定价按每百万 token；未配置 → 0（billing.py:26-62）。
2. `fetch_balance(api_key, balance_cfg)`：返回 `{"total": float[, "currency": str]}` 或 None；8s 超时；异常 → None（billing.py:88-142）。
