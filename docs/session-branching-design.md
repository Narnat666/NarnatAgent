# 会话分支（Session Branching）功能设计

> 版本: v1.1 | 日期: 2025-01 | 状态: 设计阶段

---

## 一、问题陈述

### 1.1 背景

用户向 AI 下发总任务后，AI 会将其拆分为若干子任务（模块1/2/3）。每完成一个子任务，用户需要与 AI 进入"讨论验证阶段"——审查代码、提问、修改、测试、最终定论。这个阶段会产生大量对话，包含不少**探索性内容**（试错方案 A/B/C，最终只有 D 被采纳）。

### 1.2 核心矛盾

- 验证阶段需要**广度优先探索**（试多个方向）
- 主任务需要**深度优先结论**（只要最终方案）

把探索的全部中间过程塞进主线上下文，会产生噪音、分散 AI 注意力、浪费 token、加速触发压缩。

### 1.3 类比

类似 Git 分支：

```
main:  总任务 → 子任务1结论 → 子任务2结论 → ...
                      ↑
branch:               ├→ 讨论A（废弃）
                      ├→ 讨论B（废弃）
                      └→ 讨论D（采纳）── 只把这个结论合并回 main
```

---

## 二、核心概念

### 2.1 会话层级

```
根会话 (root session)
  ├── 子会话 ① (child session)
  ├── 子会话 ②
  └── 子会话 ③
```

- **根会话**：普通会话，核心任务线。可包含多轮对话，可被 `/save` 命名保存。
- **子会话**：从根会话 fork 出来的探索分支。**仅一层**，子会话不能再开孙会话。

### 2.2 事务性

子会话是一个**原子事务**：

- `/done` → 总结注入父会话 → **提交（commit）**
- `/exit` → 暂离当前子会话，切回父会话。子会话保持 `active`，随时可 `/enter` 回来继续或 `/done`
- 崩溃/强杀 → 文件保留，父会话无感

父会话只在子会话成功完成（`/done`）后才受影响。子会话不会因为 `/exit` 或崩溃而丢失——它一直存在，直到用户 `/done` 或 `/delete`。

---

## 三、数据模型

### 3.1 JSON Schema

```json
{
  "name": "会话名称",
  "timestamp": 1712345678.0,
  "messages": [...],
  "parent": null,
  "status": "active",
  "summary": null
}
```

新增字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `parent` | `string \| null` | 父会话名。根会话为 `null` |
| `status` | `string` | `"active"` \| `"completed"` |
| `summary` | `string \| null` | `/done` 后填充的总结文本 |

### 3.2 状态迁移

```
                   /explore
   (不存在) ──────────────────→ active
                                  │
                    /done ────────→ completed (summary 非空)
                    /exit ────────→ 保持 active，切回父会话
                    崩溃/强杀 ────→ 文件保留，status 保持 active
                    /delete ──────→ 文件删除

只有 /done 能改变子会话状态（active → completed），/exit 不改变状态。
```

---

## 四、存储结构

```
.narnat/data/sessions/
├── 总任务.json                  ← 根会话文件
├── 总任务/                      ← 该根会话的子会话目录
│   ├── 验证模块A.json
│   └── 支付模块讨论.json
├── 重构计划.json
└── 重构计划/
    └── 接口迁移.json
```

**规则**：
- 根会话：`.narnat/data/sessions/<safe_name>.json`
- 子会话：`.narnat/data/sessions/<safe_parent_name>/<safe_child_name>.json`
- 删除父会话 → `os.remove(父.json)` + `shutil.rmtree(父文件夹/)`（级联删除）
- 删除子会话 → 仅删除该子会话文件，父会话不受影响

---

## 五、命令矩阵

### 5.1 命令可用性

| 命令 | 根会话 | 子会话 | 行为 |
|------|:------:|:------:|------|
| `/explore <name>` | ✅ | ❌ | 从当前 messages fork 创建子会话，自动切换 |
| `/done` | ❌ | ✅ | AI 总结 → `role:system` 注入父会话 → `status=completed` → 切回父 |
| `/exit` | ✅ | ✅ | 根：退出程序。子：保持 `active` → 切回父（随时可 `/enter` 恢复） |
| `/enter <name>` | ✅ | ✅ | 切换会话。支持 `父名/子名` 路径或裸名；裸名歧义时提示用路径 |
| `/show` | ✅ | ✅ | 展示会话树 |
| `/save` | ✅ | ❌ | 子会话禁用 |
| `/delete <name>` | ✅ | ✅ | 支持 `父名/子名` 或裸名。删子：仅该文件；删父：级联全部子；`--all`：清空 |
| `/clear` | ✅ | ✅ | 清屏 |
| `/skill` | ✅ | ✅ | 加载技能 |
| `/thinking` | ✅ | ✅ | 切换思考强度 |

### 5.2 `/explore <name>` 详细

```
前置条件：当前位置 = 根会话
行为：
  1. 拷贝当前 messages（深拷贝内容）
  2. 在拷贝的 messages 尾部插入分界标记：
     {"role": "system", "content": "━━━ 探索分支开始 ━━━\n以下为本分支的独立讨论内容。上方为主线背景，请勿重复主线已有结论。"}
  3. 创建子会话文件，status="active", parent=当前根会话名
  4. _active_name = 子会话名（自动保存开启）
  5. UI 提示（醒目边框）：
     "━━━━━━━━━━━━━━━━━━━━━━━━━━"
     "已进入探索分支: <name>"
     "⚠ 探索模式：对话与主线隔离"
     "   完成后用 /done 合并结论，/exit 暂离"
     "━━━━━━━━━━━━━━━━━━━━━━━━━━"
```

### 5.3 `/done` 详细

```
前置条件：当前位置 = 子会话
行为：
  1. 显示总结动画："* 正在总结..."（四帧循环，参照 思考中 和 压缩中 动画设计）
  2. 拼接总结 prompt + 子会话全量 messages
  3. 调用 LLM 收集总结文本
  4. 停止动画
  5. 以 role:"system" 追加到父会话 messages 尾部：
     {"role": "system", "content": f"# 子会话 [{子会话名}] 结论\n\n{summary}"}
  6. 保存父会话到磁盘
  7. 标记子会话 status="completed", summary=总结文本, 保存
  8. 自动切回父会话
  9. UI 提示："探索分支已完成，结论已合并 → 已回到主会话"
```

### 5.4 `/exit`（子会话中）

```
行为：
  1. 保存子会话到磁盘（瞬间完成，显示"会话已自动保存: <子会话名>"）
  2. 切换到父会话
  3. UI 提示："已离开探索分支（可随时 /enter 回来继续）→ 已回到主会话"

注意：/exit 不改变子会话状态，也不丢弃任何内容。
```

### 5.5 `/delete <name>` 详细

```
行为（任意位置均可执行）：
  1. name 为子会话 → 只删除该子会话文件，父会话不受影响
  2. name 为父会话 → 级联删除：父会话文件 + 整个子会话文件夹
  3. 如果删除的是当前所在会话 → 切回父会话（若当前已是父会话则回到未命名状态）
  4. name 为 "--all" → 清空所有会话（兼容现有行为）
```

### 5.6 命名与引用规则

```
存储层：子会话名在父文件夹内唯一（文件系统保证）。

用户引用：
  /enter 父名/子名      → 精确定位，始终可用
  /enter 子名           → 全树查找：唯一则直接进入，多个则提示歧义
  /delete 父名/子名     → 同上
  /delete 子名          → 同上

歧义提示示例：
  > /enter 验证模块A
    ⚠ "验证模块A" 有多个，请用完整路径指定：
      总任务/验证模块A
      重构计划/验证模块A

Tab 补全：
  /enter 时补全所有节点，格式为 父名/子名（子节点）或 父名（根节点）。
  用户输入 /enter 总任务/ 后按 Tab → 补全该父下的所有子会话名。
```

### 5.7 会话切换协议

```
所有会话切换（/enter、/done 后切回、/exit 后切回、/delete 当前后切回）遵循同一协议：

  1. 自动保存当前会话 → save_session()
  2. 加载目标会话 → load_session()
  3. 替换 Agent 内存中的 messages：
     current = self._get_messages()
     current.clear()
     current.extend(加载的 messages)
  4. 更新 _active_name = 目标会话名
  5. 如果目标是子会话 → _active_parent = 父会话名
     如果目标是根会话 → _active_parent = None
  6. 同步 context 轮次计数 → context.sync_from_messages(current)

此协议完全复用现有 on_enter 中的 clear()+extend() 模式。
```

### 5.8 `/done` 动画与中断处理

```
/done 调用 LLM 做总结（3-8 秒），期间：

动画：参照 ui_design.py 的 _compress_thread 模式
  新增 _summary_thread(stop: threading.Event)：
  四帧循环："* 正在总结   " → "* 正在总结.  " → "* 正在总结.. " → "* 正在总结..."

启动/停止：由 NarnatSessionCallbacks.on_done 内部管理
  需要注入一个 start_spinner / stop_spinner 回调对，
  或直接在 summarize_func 内部通过 _stdout_write 输出动画帧。

中断：summarize_func 支持 cancel_check 参数
  用户 ESC 取消 → 不注入父会话，子会话保持 active，动画停止
  和压缩流程的 cancel_check 模式一致
```

---

## 六、`/done` 总结 Prompt

```
以下是你在一个"探索分支"中的完整对话。messages 中有一条 "━━━ 探索分支开始 ━━━" 的分界线——
分界线之前是主线的背景上下文（你已知道），分界线之后才是本轮探索的讨论内容。

请**只总结分界线之后的内容**，不要复述主线已有的结论。

总结要求：
1. 子会话目标
2. 最终采用的方案及原因
3. 关键代码变更（文件路径、核心改动）
4. 已排除的无效方案及排除原因（一句话即可）
5. 后续注意事项

要求：精炼，不超过300字，不含讨论过程的中间细节。
```

> **代码常量定义**：以上文本在 `session_callbacks.py` 中定义为 `DONE_PROMPT`；分界标记定义为 `BOUNDARY_MARKER = "━━━ 探索分支开始 ━━━\n以下为本分支的独立讨论内容。上方为主线背景，请勿重复主线已有结论。"`

---

## 七、`/show` 树形展示

### 7.1 预期效果

```
会话管理:
  ├── 总任务  (01-15 14:30, 45条)
  │   ├── ① 用户模块验证  ✓ 已完成 (01-15 15:20)
  │   ├── ② 支付模块讨论  ⚠ 待完成 (01-15 16:00, 12条)  ◀ 当前
  │   └── ③ 数据库设计     ⚠ 待完成 (01-15 14:50, 3条)
  ├── 重构计划  (01-14 10:00, 8条)
  │   └── ① 接口迁移      ⚠ 待完成 (01-14 11:00, 3条)
  └── 未分类                (无父子关系)
```

当前所在节点末尾追加 `◀ 当前` 标记，一目了然。

### 7.2 状态图标

| 状态 | 图标 | 含义 |
|------|:----:|------|
| active（进行中，未 `/done`） | `⚠` | 提醒用户：该子会话尚未总结合并 |
| completed（已完成 `/done`） | `✓` | 结论已注入父会话 |

---

## 八、技术实现要点

### 8.1 影响文件清单

| 文件 | 改动级别 | 核心改动 |
|------|:--------:|------|
| `config/session_store.py` | 🟡 中 | 所有函数 +`parent` 参数；新增 `list_sessions_tree` `is_child_session` `get_parent_name` |
| `ui/session_commands.py` | 🟡 中 | `_dispatch_command` 返回值 `int`（0/1/2）；+`/explore` `/done`；`is_child` 检查；补全路径化 |
| `core/session_callbacks.py` | 🟡 中 | +`_active_parent`；+`on_explore` `on_done`；注入 `summarize_func`；`on_exit` 区分根/子；+`_switch_to_session` |
| `core/agent.py` | 🟢 小 | 删除硬编码 `/exit`；3态 dispatch；注入 `summarize_func`；`/done` 后 context 同步 |
| `ui/ui_design.py` | 🟢 小 | 新增 `_summary_thread` 动画（复用 `_compress_thread` 模式） |
| `core/context.py` | 🟢 无 | `sync_from_messages` 已满足需求 |
| `core/message_manager.py` | 🟢 无 | 不涉及 |
| `config/loader.py` | 🟢 无 | 不涉及 |

### 8.2 关键函数签名与契约

**`session_store.py` 新增/修改：**

```python
# 所有现有函数增加 parent 参数
def save_session(narnat_dir, name, messages, parent=None,
                 status="active", summary=None) -> str:
    """parent 非空 → sessions/<parent>/<name>.json"""
    path = _session_path(narnat_dir, name, parent=parent)
    data = {"name": name, "timestamp": time.time(), "messages": messages,
            "parent": parent, "status": status, "summary": summary}
    ...

def load_session(narnat_dir, name, parent=None) -> tuple:
    """同上，返回 (messages, error)"""

def delete_session(narnat_dir, name, parent=None) -> str:
    """parent 非空 → 仅删文件。parent 为空 → 级联删文件夹"""

def list_sessions_tree(narnat_dir) -> list:
    """返回嵌套结构:
    [{"name": "父A", "timestamp": ..., "message_count": 45,
      "children": [
        {"name": "子1", "status": "active", ...},
        {"name": "子2", "status": "completed", ...}
      ]},
     {"name": "父B", "children": []},
     {"name": "孤儿父", "children": [{"name": "遗孤子", ...}]}]
    """

def is_child_session(narnat_dir, name, parent=None) -> bool:
    """读取 JSON 的 parent 字段，非 null 则为子会话"""

def get_parent_name(narnat_dir, name, parent=None) -> Optional[str]:
    """读取 JSON 的 parent 字段"""

# 路径构建函数修改
def _session_path(narnat_dir, name, parent=None) -> str:
    safe_name = _safe_filename(name)
    if parent:
        safe_parent = _safe_filename(parent)
        return os.path.join(_sessions_dir(narnat_dir), safe_parent, f"{safe_name}.json")
    return os.path.join(_sessions_dir(narnat_dir), f"{safe_name}.json")
```

**`SessionCallbacks` 接口新增 (`session_commands.py`)：**

```python
class SessionCallbacks:
    # ── 新增方法 ──
    def is_child_session(self) -> bool:
        """当前是否在子会话中（决定 /done /explore /save 可用性）"""
        return False

    def on_explore(self, name: str) -> str:
        """创建子会话，返回错误或空串"""
        return ""

    def on_done(self) -> str:
        """AI 总结合并，返回错误或空串"""
        return ""

    def on_list_names_tree(self) -> list:
        """返回 ["父A", "父A/子1", "父B", ...] 供补全"""
        return []

    # ── 修改方法 ──
    def on_exit(self) -> str:
        """子会话中切回父，根会话中退出程序"""
        return ""

    def on_enter(self, name: str) -> str:
        """支持 父名/子名 路径解析"""
        return ""
```

**`NarnatSessionCallbacks` 新增字段与注入 (`session_callbacks.py`)：**

```python
class NarnatSessionCallbacks(SessionCallbacks):
    def __init__(self, narnat_dir, get_messages_func,
                 context_manager=None, config_dir="",
                 thinking_effort_getter=None, thinking_effort_setter=None,
                 thinking_options=None,
                 summarize_func=None,          # ← 新增
                 summary_anim_start=None,       # ← 新增：动画启动回调
                 summary_anim_stop=None):       # ← 新增：动画停止回调
        self._active_name = None        # 当前会话名
        self._active_parent = None      # 父会话名（子会话时非空）
        self._summarize_func = summarize_func
        self._summary_anim_start = summary_anim_start
        self._summary_anim_stop = summary_anim_stop

    @property
    def is_child(self) -> bool:
        return self._active_parent is not None

    def is_child_session(self) -> bool:
        return self.is_child
```

**`summarize_func` 契约：**

```python
# 签名
summarize_func: Callable[[List[Dict[str, Any]], Callable[[], bool]], str]
# 参数1: messages（子会话全量 + DONE_PROMPT 拼在末尾）
# 参数2: cancel_check（无参，返回 True = 用户已取消）
# 返回: 总结文本; 空串或异常 → on_done 返回错误提示
```

**`_dispatch_command` 返回值改为 int（原 bool）：**

```python
def _dispatch_command(cmd, args, cb) -> int:
    # 0 = 不是命令（继续 AI 处理循环）
    # 1 = 命令已处理（continue）
    # 2 = 退出程序（os._exit）
```

### 8.3 复用压缩总结模式

`/done` 的 LLM 总结调用参照 `handle_compress`：

```
Compressor.handle_compress:            /done:
  build_compress_messages()            拼接 DONE_PROMPT + 子会话 messages
  llm_client.chat_stream()             llm_client.chat_stream(no_tools=True)
  收集 summary_content                 收集 summary_content
  write_summary() → 磁盘              → 返回文本
  verify_summary()                     → 非空检查
  messages.clear() + 重建              → 注入父会话 messages
```

`summarize_func` 实现伪代码（在 `agent.py` `__init__` 中构造）：

```python
def _do_summarize(messages, cancel_check):
    summary_parts = []
    for chunk in self._llm.chat_stream(messages, no_tools=True,
                                        cancel_check=cancel_check):
        if cancel_check():
            return ""
        if "content" in chunk and "tool_calls" not in chunk:
            summary_parts.append(chunk["content"])
    return "".join(summary_parts)
```

### 8.4 会话切换协议（`_switch_to_session`）

所有切换统一走此流程，参照现有 `on_enter` 的 `clear()+extend()` 模式：

```python
def _switch_to_session(self, target_name, target_parent=None) -> str:
    """切到目标会话。返回空串=成功。"""
    # 1. 自动保存当前
    msgs = self._get_messages()
    save_session(self._narnat_dir, self._active_name, msgs,
                 parent=self._active_parent)
    # 2. 加载目标
    new_msgs, err = load_session(self._narnat_dir, target_name,
                                  parent=target_parent)
    if err:
        return err
    # 3. 替换内存 messages
    current = self._get_messages()
    current.clear()
    current.extend(new_msgs)
    # 4. 更新状态
    self._active_name = target_name
    self._active_parent = target_parent
    # 5. 同步 context 轮次
    if self._context:
        self._context.reset()
        self._context.sync_from_messages(current)
    return ""
```

### 8.5 `/explore` / `/done` / `/exit` 核心伪代码

```python
def on_explore(self, name):
    if self._active_parent is not None:
        return "错误: 探索分支中不可再开分支"
    msgs = [dict(m) for m in self._get_messages()]  # 深拷贝
    # 插入分界标记
    msgs.append({"role": "system", "content": BOUNDARY_MARKER})
    save_session(self._narnat_dir, name, msgs, parent=self._active_name,
                 status="active")
    return self._switch_to_session(name, target_parent=self._active_name)

def on_done(self):
    if self._active_parent is None:
        return "错误: 主会话中不可用 /done"
    # 启动动画
    if self._summary_anim_start:
        self._summary_anim_start()
    # 拼接 prompt + 调用 LLM
    msgs = list(self._get_messages())
    msgs.append({"role": "user", "content": DONE_PROMPT})
    summary = self._summarize_func(msgs, cancel_check=lambda: _interrupt_ctrl.is_set)
    if self._summary_anim_stop:
        self._summary_anim_stop()
    if not summary:
        return "总结取消或失败"

    # 注入父会话
    parent_msgs, err = load_session(self._narnat_dir, self._active_parent)
    if err:
        return f"无法加载父会话: {err}"
    parent_msgs.append({"role": "system",
        "content": f"# 子会话 [{self._active_name}] 结论\n\n{summary}"})
    save_session(self._narnat_dir, self._active_parent, parent_msgs)

    # 标记子会话完成
    child_msgs, _ = load_session(self._narnat_dir, self._active_name,
                                  parent=self._active_parent)
    save_session(self._narnat_dir, self._active_name, child_msgs,
                 parent=self._active_parent, status="completed", summary=summary)

    # 切回父会话
    return self._switch_to_session(self._active_parent, target_parent=None)

def on_exit(self):
    if self._active_parent is not None:
        # 子会话: 保存 + 切回父
        msgs = self._get_messages()
        save_session(self._narnat_dir, self._active_name, msgs,
                     parent=self._active_parent)
        return self._switch_to_session(self._active_parent, target_parent=None)
    else:
        # 根会话: 保存 + 返回待退出标记
        if self._active_name:
            save_session(self._narnat_dir, self._active_name, self._get_messages())
        return self._active_name or ""  # 非空 = 成功保存

def on_enter(self, name):
    """路径解析逻辑，覆盖父类方法"""
    if "/" in name:
        parts = name.split("/", 1)
        parent_name, child_name = parts[0], parts[1]
        return self._switch_to_session(child_name, target_parent=parent_name)
    else:
        tree = list_sessions_tree(self._narnat_dir)
        matches = []
        for root in tree:
            if root["name"] == name:
                matches.append((root["name"], None))
            for child in root.get("children", []):
                if child["name"] == name:
                    matches.append((child["name"], root["name"]))
        if len(matches) == 0:
            return f"会话不存在: {name}"
        if len(matches) > 1:
            paths = "\n".join(f"      {p}/{c}" if c else f"      {p}" for p, c in matches)
            return f"'{name}' 有多个，请用完整路径指定：\n{paths}"
        target_name, target_parent = matches[0]
        return self._switch_to_session(target_name, target_parent=target_parent)
```

### 8.6 `agent.py` 必改处

```python
# 1. 删除硬编码 /exit 拦截（原152-156行），让 /exit 走 dispatch：
# if stripped == "/exit":
#     self._auto_save_on_exit()
#     os._exit(0)
# ← 全部删除

# 2. dispatch 改为处理 3 态返回值：
if stripped.startswith("/"):
    result = self._ui.dispatch_command(cmd, args)
    if result == 2:        # 退出程序
        self._logger.close()
        os._exit(0)
    if result == 1:        # 命令已处理
        continue
    # result == 0: 不是命令，继续 AI 处理

# 3. 构造 summarize_func 注入到 callbacks：
self._callbacks = NarnatSessionCallbacks(
    ...
    summarize_func=lambda msgs, cancel: self._do_summarize(msgs, cancel),
    summary_anim_start=lambda: self._ui.begin_summarizing(),
    summary_anim_stop=lambda: self._ui.end_summarizing(),
)
```

### 8.7 `session_commands.py` `_dispatch_command` 关键改动

```python
# 返回值从 bool → int
# 新增命令：
if cmd == "explore":
    if cb.is_child_session():
        _stdout_write("错误: 探索分支中不可再开分支\n")
        return 1
    ...
if cmd == "done":
    if not cb.is_child_session():
        _stdout_write("错误: /done 仅在探索分支中可用\n")
        return 1
    ...
# /save 在子会话中：
if cmd == "save":
    if cb.is_child_session():
        _stdout_write("错误: 探索分支中禁用 /save（自动保存已开启）\n")
        return 1
    ...
# /exit 区分：
if cmd == "exit":
    if cb.is_child_session():
        result = cb.on_exit()
        ...
        return 1
    else:
        result = cb.on_exit()
        if result:
            _stdout_write(f"会话已自动保存: {result}\n")
        return 2  # 退出程序
```

### 8.8 `ui_design.py` 新增动画

```python
# 完全复用 _compress_thread 的模式
def _summary_thread(stop: threading.Event) -> None:
    """总结动画，四帧循环"""
    _stdout_write("\x1b[?25l")
    frames = (
        f"{B}{O}* {R}{O}正在总结   {R}",
        f"{D}{O}* {R}{O}正在总结.  {R}",
        f"{B}{O}* {R}{O}正在总结.. {R}",
        f"{D}{O}* {R}{O}正在总结...{R}",
    )
    i = 0
    while not stop.is_set():
        _stdout_try_write(f"\r  {frames[i]}\x1b[K")
        i = (i + 1) % 4
        stop.wait(0.15)
    _stdout_write("\r\x1b[K")
    _stdout_write("\x1b[?25h")

class UIInterface:
    def begin_summarizing(self) -> None:
        self._summary_stop = threading.Event()
        self._summary_thread = threading.Thread(
            target=_summary_thread, args=(self._summary_stop,), daemon=True)
        self._summary_thread.start()

    def end_summarizing(self) -> None:
        if self._summary_stop:
            self._summary_stop.set()
        if self._summary_thread:
            self._summary_thread.join(timeout=0.5)
````n---

## 九、边界情况决策

| 场景 | 决策 |
|------|------|
| 父会话在子会话活跃期间被 `/enter` 切回并修改 | `/done` 时注入父会话**当前最新** messages 尾部 |
| 父会话被 `/delete` | 级联删除所有子会话 |
| 子会话被 `/delete` | 仅删除该子会话文件，父会话不受影响 |
| 子会话中崩溃 | 自动保存已生效，重启后 `/enter` 可恢复；未 `/done` 前父会话不受影响 |
| 子会话中 `/enter` 到另一个子会话 | 允许，自由切换 |
| `/explore` 时父会话无消息 | 允许（只有 system prompt 也接受） |
| 同名子会话（同一父内） | 覆盖（和现有 `/save` 行为一致） |
| 同名子会话（不同父） | 允许（存储隔离）；`/enter` 裸名时提示歧义，用 `父名/子名` 路径精确引用 |
| 删除当前所在的子会话 | 自动切回父会话 |
| `安全化后的名称含 "/"` | `/` 作为路径分隔符被替换为 `_`，和现有安全化规则一致 |

---

## 十、不做的

- ❌ 子会话嵌套（子→孙）
- ❌ 子会话中 `/save` 改名
- ❌ `/done` 的总结支持用户手动编辑（v1 仅 AI 自动总结）
- ❌ 多个子会话并行对比视图（v1 仅树形 `/show`）

---

## 十一、讨论记录

| 日期 | 要点 |
|------|------|
| 2025-01 | 初始设计：Q1 全量继承 / Q2 AI 总结 / Q3 不嵌套 / Q4 事务性 / Q5 多子会话 + 树形 `/show` |
| 2025-01 | `/done` 注入格式确定为 `role:"system"` |
| 2025-01 | 存储结构确定为：父文件 + 同名字文件夹 |
| 2025-01 | 子会话禁用 `/save`，自动保存照常 |
| 2025-01 | `/done` 复用压缩总结的 LLM 调用模式 |
| 2025-01 | `/exit` 修正：不丢弃子会话，保持 `active` 以便 `/enter` 恢复后 `/done`；`/show` 用 `⚠` 标记未完成子会话 |
| 2025-01 | `/delete` 细化：删子仅删文件，删父级联全部；新增 `is_child` 状态传递机制 |
| 2025-01 | 命名冲突：不同父可有同名子（存储隔离）；`/enter` 支持 `父名/子名` 路径语法，裸名歧义时提示 |
| 2025-01 | UI 体验打磨：`/show` 标记当前位置 `◀ 当前`；`/done` 总结动画 + 自动切回父；`/exit` 瞬间保存；`/explore` 醒目边框提示 |
| 2025-01 | 分界线机制：`/explore` 时插入 `system` 分界标记，AI 在子会话中能区分"背景"和"探索"；`/done` prompt 明确"只总结分界线之后" |
| 2025-01 | 发现两个工具层问题（不阻塞本次实现，完成后修复） |


---
