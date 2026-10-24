"""工具上下文 —— 统一管理工具所需的回调和状态

替代各工具模块的模块级全局变量 + setter注入模式。
由Agent创建，通过registry传递给各工具。
"""

import json
import os
import threading
from dataclasses import MISSING, dataclass, field, fields
from typing import Optional, Callable, Any, Dict, List


# 删除确认标记：bash/terminal检测到删除命令时返回此值，由agent主循环拦截处理
AWAIT_CONFIRM = "__AWAIT_CONFIRM__"

# narnat.json 里联网搜索配置所在的段（WebSearch 的连接/密钥契约全在这一段）
_SEARCH_SECTION = "联网搜索"


def _narnat_json_path() -> str:
    """定位 narnat.json（与 config.loader 同一套项目根解析）

    必须复用 loader 的解析而不是自己拼路径：NARNAT_HOME、打包后的 exe 目录、
    开发模式的向上查找都在那里，抄一份必然与真实加载点漂移。
    延迟导入：本模块是叶子工具模块，避免把 config 包拉进导入链。
    """
    try:
        from ..config.defaults import CONFIG_SUBDIR, NARNAT_DIR, NARNAT_JSON
        from ..config.loader import _find_project_root
        return os.path.join(_find_project_root(), NARNAT_DIR, CONFIG_SUBDIR, NARNAT_JSON)
    except Exception:
        return ""


def _file_sig(path: str):
    """文件指纹 (mtime_ns, size)；读不到返回 None

    mtime 判重是避免每次调用都读盘解析的关键；size 一起比是为了极短时间内的
    同长度改写（mtime 粒度受文件系统限制）。
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_search_section(path: str):
    """读 narnat.json 的「联网搜索」段，返回 dict；读不到/解析失败/段不是对象 → None

    编码处理与 config.loader._load_json 一致：先 utf-8-sig（记事本 BOM），
    解码失败（记事本"ANSI(GBK)"保存）再按 GBK 重读。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="gbk") as f:
                data = json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, OSError):
            return None
    except (json.JSONDecodeError, RecursionError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    section = data.get(_SEARCH_SECTION)
    return section if isinstance(section, dict) else None


class _HotSearchKeys(dict):
    """「联网搜索」配置的热读视图（dict 子类，读取前按 mtime 判断要不要重读磁盘）

    为什么需要它：这段配置决定 WebSearch 走哪个 provider、用哪个 key。用户按
    README 改完 narnat.json 却发现工具仍用旧配置，只会得到 401 或走错通道，
    然后以为自己配置写错了——而 load_config 只在启动时跑一次，一改就得重启。
    这里只让 api_keys 这一项按需重读，其它配置（模型/思考/会话…）的生效时机
    一个字节都不动：全局热更新会改变整个后端的配置语义。

    读取顺序：文件指纹(mtime+size)与上次一致 → 直接用缓存（零解析）；
    否则重新读盘解析「联网搜索」段。读不到 / JSON 坏 / 段不是对象 → 保留上次
    成功的值（首次即启动快照），**绝不抛异常**：配置热读失败不能牵连搜索工具。

    构造时把当前文件指纹记为「已同步」：调用方传进来的快照本来就读自这个文件
    （assembly 直接传 config.api_keys），标记同步后首次读取也不会白白重读一遍。
    """

    def __init__(self, snapshot=None):
        super().__init__(snapshot if isinstance(snapshot, dict) else {})
        self._lock = threading.RLock()
        self._path = _narnat_json_path()   # 定位只算一次（进程内 cwd 可能被工具改动）
        self._sig = _file_sig(self._path) if self._path else None

    def _refresh(self) -> None:
        """按文件指纹决定是否重读 narnat.json 的「联网搜索」段（就地更新自身）"""
        with self._lock:
            path = self._path or _narnat_json_path()
            if not path:
                return
            self._path = path
            sig = _file_sig(path)
            if sig is None:
                return                  # 文件没了/读不到：保留上次成功的值
            if sig == self._sig:
                return                  # mtime 未变：不重读、不解析
            data = _read_search_section(path)
            if data is None:
                # 坏文件也记下指纹：别每次调用都重复解析同一个坏文件。
                # 用户修好后 mtime 会再变，那时自然重读。
                self._sig = sig
                return
            dict.clear(self)
            dict.update(self, data)
            self._sig = sig

    # ── 读操作一律先刷新（写操作不刷新：本对象只给工具读） ──
    def __len__(self):
        self._refresh()
        return dict.__len__(self)

    def __getitem__(self, key):
        self._refresh()
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._refresh()
        return dict.__contains__(self, key)

    def __iter__(self):
        self._refresh()
        return dict.__iter__(self)

    def get(self, key, default=None):
        self._refresh()
        return dict.get(self, key, default)

    def keys(self):
        self._refresh()
        return dict.keys(self)

    def items(self):
        self._refresh()
        return dict.items(self)

    def values(self):
        self._refresh()
        return dict.values(self)

    def copy(self):
        self._refresh()
        return dict.copy(self)

    def __repr__(self):
        self._refresh()
        return dict.__repr__(self)


@dataclass
class ToolContext:
    """工具运行时上下文"""

    # TodoWrite UI更新回调
    ui_callback: Optional[Callable[[Any], None]] = None

    # API密钥组（web_search使用）。实际是 _HotSearchKeys（dict 子类）：
    # 每次读取都按 mtime 判断要不要重读 narnat.json 的「联网搜索」段，
    # 改完 provider/key 不必重启后端；其它配置仍按启动时的 load_config 结果生效
    api_keys: Dict[str, str] = field(default_factory=dict)

    # 忽略目录（glob/grep使用）
    ignore_dirs: List[str] = field(default_factory=list)

    # 安全确认开关（由配置驱动）
    git_skip_confirm: bool = False   # True=git命令免确认直接执行
    rm_skip_confirm: bool = False    # True=rm命令免确认直接执行

    max_transfer_mb: int = 100       # 文件传输大小上限(MB)，0=不限制

    # 工具输出全局硬上限（字符数），0=不限制。由配置"工具输出上限KB"驱动
    max_tool_output_chars: int = 65536

    # 数据目录（<项目>/.narnat/data），Read 的图片附件库建在其下的 attachments/。
    # 由 assembly 从 config.paths.data_dir 注入；为空时 Read 读图直接报错（不静默降级）
    data_dir: str = ""

    # 工具超时全局上限（秒），0=不限制。由配置"工具超时上限秒"驱动
    max_timeout_seconds: int = 1800

    # 取消检查回调（返回 True = 用户已中断）。由组装层/验证器按需注入：
    # 长耗时只读工具（Grep/Glob）据此在扫描中途提前返回；
    # 默认 None = 无取消语义，工具行为与注入前完全一致
    cancel_check: Optional[Callable[[], bool]] = None

    # MCP 服务器管理器（由 assembly 注入；MCP 工具用它做运行时连接/断开）
    mcp_manager: Any = None

    # 当前todo状态（由TodoWrite工具更新）
    current_todos: list = field(default_factory=list, metadata={"task_scoped": True})

    # 暂存的待确认命令（用户确认后由agent主循环按序逐条重放；每批处理完清空）
    # 格式: [(tool_name, arguments_dict), ...]
    pending_delete: list = field(default_factory=list)

    # 用户已确认删除，下次执行删除命令时跳过确认直接执行
    _delete_confirmed: bool = field(default=False, repr=False)

    # 目标模式完成标记：GoalComplete工具调用时置True，主循环据此停止自动续跑
    goal_complete: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 目标模式完成清单（GoalComplete 提交：[{"要求","证据","状态"}, ...]）
    goal_checklist: list = field(default_factory=list, metadata={"task_scoped": True})

    # 诚实收尾标记：清单含"未完成/受阻"项（允许收尾，用户会看到未完成项）
    goal_honest: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 目标模式预算内已消耗的回合数（agent.py 每轮结算时累加：
    # 本轮续跑消耗1 + 本轮验证打回次数k，合计 1+k）；机械拒绝不计入
    goal_rounds_used: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 连续机械拒绝计数（GoalComplete 机械校验拒绝路径累计；放行时清零）。
    # 与续跑预算无关，仅用于连续拒绝超限兜底放行防死循环
    goal_mech_rejects: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 强制放行标记：验证打回预算耗尽 / 连续机械拒绝超限
    goal_forced: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 验证存疑放行标记
    goal_suspect: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 收尾软提醒标志：任务收尾时计划未全部勾选会提醒一次，置True后放行不再提醒。
    # 每次用户新输入时由agent.py复位（与goal_complete一致）
    todo_reminded: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 迟到提醒标志：多轮工具后仍未建计划会温和提醒一次，置True后不再提醒。
    # 每次用户新输入时由agent.py复位（与todo_reminded一致）
    todo_nudge_sent: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 本任务累计工具轮数（迟到提醒的触发计数；每任务复位）
    tool_rounds_used: int = field(default=0, repr=False, metadata={"task_scoped": True})

    # 后台任务软提醒标志：结束回合时仍有running任务提醒一次，置True后放行不再提醒。
    # 每次用户新输入时由agent.py复位（与todo_reminded一致）
    bg_reminded: bool = field(default=False, repr=False, metadata={"task_scoped": True})

    # 风格提醒：连续纯讨论轮数（AI 未调用任何工具的用户轮计数）。
    # 达配置间隔后追加一条提醒并清零；AI 一调工具即清零（视作在干活）。
    # 不带 task_scoped：讨论是跨任务的会话级状态，不随用户新输入复位。
    style_discuss_rounds: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        # 启动快照包成热读视图：之后每次读 api_keys 都反映磁盘最新的「联网搜索」段
        self.api_keys = _HotSearchKeys(self.api_keys)

    def reset_for_task(self) -> None:
        """复位全部任务级字段（task_scoped 标记驱动）。

        新增任务级字段只需在字段定义处加 metadata={"task_scoped": True}，
        无需再维护别处的手工复位清单。
        """
        for f in fields(self):
            if not f.metadata.get("task_scoped"):
                continue
            if f.default is not MISSING:
                setattr(self, f.name, f.default)
            elif f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())

    def on_todo_update(self, todos):
        """调用TodoWrite UI回调"""
        if self.ui_callback:
            self.ui_callback(todos)

    def get_api_key(self, name: str) -> str:
        """获取指定服务的API密钥"""
        return self.api_keys.get(name, "")
