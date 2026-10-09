"""GoalBaseline工具 —— 目标模式需求基线：把任务原文拆成编号需求条目并冻结

仅在目标模式（/goal on）下有意义：接到任务的第一步先把任务原文拆成编号需求
条目提交，一次提交即冻结——已有条目不得改写、不得删除，只能追加；确实做不到
的条目显式标「放弃」并写明原因。这张基线随后用于三处消费：每轮续跑提示回灌
（跑长了会丢需求）、GoalComplete 机械校验（清单编号 ⊇ 基线编号，缺号/多号/
放弃项标完成直接拒绝，纯机械、不占续跑预算）、独立验证器的评估基准（先核基线
是否覆盖任务原文，再核清单编号与内容的语义对应）。

为什么冻结：基线是 AI 对用户要求的承诺，完成清单是 AI 自报的"验收单"。若允许
事后改写，AI 可以把没做到的要求悄悄改掉再自报完成，验收基准就失去了意义。
任务确实新增的要求用不带 `编号` 的条目追加（只增不改）；放弃是显式降级动作，
留下"放弃+原因"的痕迹，而不是把条目删掉。
"""

from ..param_utils import to_positive_int

_STATUSES = ("保留", "放弃")
_ITEM_MAX_CHARS = 300
_BLOCK_MAX_CHARS = 4000

DEFINITION = {
    "type": "function",
    "function": {
        "name": "GoalBaseline",
        "description": (
            "记录/追加目标模式需求基线——接到任务的第一步先调用它，把任务原文拆成编号要求条目。\n"
            "提交即冻结：已有条目不得改写、不得删除；任务确有的新要求用不带 编号 的条目追加。\n"
            "确实做不到的条目，用带 编号 的条目把 状态 改为「放弃」并写明 原因"
            "（完成清单中仍须如实标注未完成/受阻）。\n"
            "与 TodoWrite 的区别：基线是对用户要求的承诺（不可改写），TodoWrite 是执行步骤（可变）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "编号": {
                                "type": "integer",
                                "description": "要更新的已有条目编号；新增条目不要填（框架自动分配）",
                            },
                            "要求": {
                                "type": "string",
                                "description": "条目内容；更新已有条目时必须与已存文本逐字相同",
                            },
                            "状态": {
                                "type": "string",
                                "enum": ["保留", "放弃"],
                                "description": "缺省 保留；确实做不到的条目标 放弃 并写明原因",
                            },
                            "原因": {
                                "type": "string",
                                "description": "状态=放弃 时必填：该要求做不到的原因",
                            },
                        },
                        "required": ["要求"],
                    },
                    "description": "需求条目：[{编号?, 要求, 状态?, 原因?}, ...]",
                },
            },
            "required": ["items"],
        },
    },
}


CAPABILITY = {
    "label": "需求基线",
    "dispatch": "serial",
    "summary": "none",
    "trusted_output": True,
}


def render_baseline(items) -> str:
    """渲染需求基线文本（唯一的格式定义处：续跑提示/压缩重注入/验证器三处消费）。

    空/非列表 → ""；条目要求超长按 _ITEM_MAX_CHARS 截断，整块超 _BLOCK_MAX_CHARS
    截断并提示（完整内容仍在 GoalBaseline 工具结果与 ctx.goal_baseline 里）。
    """
    if not isinstance(items, list) or not items:
        return ""
    lines = [f"【需求基线】共 {len(items)} 条（框架记录；已有条目不可改写/删除，"
             "新增要求用 GoalBaseline 追加）"]
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            it = {"编号": i, "要求": str(it)}
        text = str(it.get("要求", ""))
        if len(text) > _ITEM_MAX_CHARS:
            text = text[:_ITEM_MAX_CHARS] + "…"
        line = f"{it.get('编号', i)}. [{it.get('状态', '保留')}] {text}"
        lines.append(line + (f"（原因：{it.get('原因', '')}）"
                             if it.get("状态") == "放弃" else ""))
    block = "\n".join(lines)
    if len(block) > _BLOCK_MAX_CHARS:
        block = block[:_BLOCK_MAX_CHARS] + "\n…（基线过长已截断，完整内容见 GoalBaseline 工具结果）"
    return block


def execute(items=None, _tool_context=None) -> str:
    """
    记录需求基线：全量校验通过后一次性落盘（任何一条不合法即整体拒绝，不部分写入）。

    Args:
        items: 需求条目，[{'编号': int?, '要求': str, '状态': '保留'|'放弃', '原因': str?}, ...]，
            带 编号 = 更新已有条目（要求须逐字相同）；不带 编号 = 追加新条目
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        提示文本，回传给LLM
    """
    if _tool_context is None:
        # 理论不发生（registry 始终注入）；保持健壮：不校验、不落盘，仅提示
        return "[提示] 缺少工具上下文，需求基线未记录。请重新调用 GoalBaseline。"

    if not isinstance(items, list) or not items:
        return ("[拒绝] items 必须是非空数组：把任务原文拆成编号需求条目后重新调用，"
                "每条为 {要求: ...}（新增，不填 编号）或 {编号: N, 要求: 与已存文本逐字相同,"
                " 状态: 保留/放弃, 原因: ...}（更新已有条目）。")

    current = _tool_context.goal_baseline if isinstance(_tool_context.goal_baseline, list) else []
    by_no = {it.get("编号"): it for it in current if isinstance(it, dict)}

    # 先全量校验（收集更新与新增），全部通过才落盘——拒绝时 ctx.goal_baseline 保持原样
    updates, additions, seen = [], [], set()  # 更新(目标,状态,原因) / 新增要求文本 / 已见编号
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return f"[拒绝] 第{i}个条目不是对象，应为 {{编号?, 要求, 状态?, 原因?}} 形式的对象。"
        req = item.get("要求")
        if not isinstance(req, str) or not req.strip():
            return f"[拒绝] 第{i}个条目的 要求 必填，且不能为空白字符串。"

        status = item.get("状态")
        if status is None:
            status = "保留"  # 缺省 保留
        if not isinstance(status, str) or status.strip() not in _STATUSES:
            return f"[拒绝] 第{i}个条目的 状态 非法：{status!r}，只能是 保留/放弃。"
        status = status.strip()

        reason = item.get("原因")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            return f"[拒绝] 第{i}个条目的 原因 必须是字符串。"

        if item.get("编号") is None:
            if status == "放弃":
                return (
                    f"[拒绝] 第{i}个条目是新增条目，不能带 状态=放弃：新增条目必须为 保留。"
                    "先调用一次 GoalBaseline 追加该要求（不填 编号），再调用一次带 编号 把它标为 放弃。"
                )
            additions.append(req)
            continue

        no = to_positive_int(item.get("编号"))
        if no is None:
            return (f"[拒绝] 第{i}个条目的 编号 非法：{item.get('编号')!r}，必须是正整数"
                    "（或纯数字字符串）；新增条目不要填 编号。")
        if no in seen:
            return f"[拒绝] 第{i}个条目的 编号 {no} 在同一次调用中重复出现：每个编号只能出现一次。"
        seen.add(no)
        target = by_no.get(no)
        if target is None:
            return (f"[拒绝] 编号 {no} 不在需求基线中，不能更新；"
                    "要新增条目请不要填 编号（框架从当前最大编号+1 起自动分配）。")
        if req != target.get("要求", ""):
            return (f"[拒绝] 编号 {no} 的要求不得改写（逐字相同才能更新）；"
                    "确需新增要求请用不带 编号 的条目追加。")
        if status == "放弃" and not reason.strip():
            return f"[拒绝] 编号 {no} 标记为 放弃 时必须写明 原因（为什么该项做不到）。"
        # 保留 且未给原因时原因存 ""：允许把放弃项改回保留，不要求给原因
        updates.append((target, status, "" if status == "保留" else reason))

    # 全量校验通过：一次性落盘（更新原地生效；新增按传入顺序追加）
    for target, status, reason in updates:
        target["状态"] = status
        target["原因"] = reason
    nums = [it["编号"] for it in current if isinstance(it, dict) and isinstance(it.get("编号"), int)]
    next_no = max(nums) + 1 if nums else 1
    for req in additions:
        current.append({"编号": next_no, "要求": req, "状态": "保留", "原因": ""})
        next_no += 1
    _tool_context.goal_baseline = current

    tag = "[需求基线已更新]" if updates else "[需求基线已记录]"
    return (render_baseline(current) + "\n\n"
            + f"{tag} 后续工作与完成清单须逐条覆盖以上编号；"
            "完成清单每项必须带 编号 字段（见 GoalComplete）。")
