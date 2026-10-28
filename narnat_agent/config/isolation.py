"""父子代理隔离声明表 —— 子代理模式下哪些配置/能力必须与父代理不同

目的：把散落的 `if headless:` 收敛成一张可查的声明表。新增一项隔离 =
在下面对应表里加一行，消费方（loader / assembly）无需改动。

生效面分三种，性质不同，必须分开登记——混在一起会让人误判保证强度：

1. CONFIG_OVERRIDES —— 配置面：覆盖 narnat.json 的取值。
   强制优先于用户配置：narnat.json 写了 true，子代理模式仍按 false 生效。
   由 apply_overrides() 覆盖进配置 dict，在构建各子配置对象之前完成。
2. TOOL_DENY —— 工具面：子代理拿不到的插件工具（名字同 narnat.json「工具.插件」的键）。
   只从发给 LLM 的工具表剔除，执行能力仍在注册表里（registry 既有设计，
   为兼容历史残留调用）。范围限插件工具：Read/Glob/Grep/Edit/Write/Shell/
   TodoWrite 不受此表控制，屏蔽它们需要另改 get_tool_definitions 的过滤集合。
3. STRUCTURAL —— 控制流面：靠"入口根本没调那条链路"保证，无需覆盖。
   登记在此**只为可查**（一眼看全子代理缺什么），不产生任何代码行为。
   风险提示：谁给 run_headless 补上对应调用，这项就不再成立——动代码前先看这里。

判据（决定一项该不该隔离）：该行为会以系统口吻注入对话、或只在父代理语境下
成立时，子代理侧必须关闭。典型是风格提醒——它以 [系统提醒] 前缀进入历史，
子代理无法分辨那是父代理的纠偏还是自己的系统指令。
"""

# ── 1. 配置面：((段, 键, ...), 强制值) ──
CONFIG_OVERRIDES = (
    # 风格提醒以 [系统提醒] 口吻注入，且提醒语可能指向父代理文档
    (("风格", "启用"), False),
)

# ── 2. 工具面：插件工具名 ──
TOOL_DENY = frozenset()

# ── 3. 控制流面：(配置键, 保证方式) —— 仅登记，不生效 ──
STRUCTURAL = (
    ("会话.自动保存", "run_headless 不调 try_save/on_auto_save/on_exit"),
    ("余额查询.启用", "run_headless 不查余额"),
    ("界面.显示费用", "HeadlessUI.finish 忽略统计参数"),
    ("界面.显示余额", "HeadlessUI.finish 忽略统计参数"),
)


def apply_overrides(data: dict, headless: bool) -> None:
    """就地把隔离声明覆盖进配置 data（须在构建各子配置对象之前调用）

    段不存在时创建；存在但类型畸形（非 dict）时整体替换为 dict——后续
    loader 的类型防御会把它当正常配置处理，不会因隔离反而崩掉。
    """
    if not headless:
        return
    for path, value in CONFIG_OVERRIDES:
        node = data
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[path[-1]] = value


def plugin_states(states: dict, headless: bool) -> dict:
    """合并用户插件开关与隔离屏蔽，返回给 registry.apply_plugin_config 的字典

    隔离项后写入 → 强制优先于用户配置。
    """
    merged = dict(states or {})
    if headless:
        for name in TOOL_DENY:
            merged[name] = False
    return merged
