"""GoalComplete工具 —— 声明目标任务已完成，并提交结构化完成清单

仅在目标模式（/goal on）下有意义：AI 判定任务真正完成时调用，
必须提交'任务要求 → 可检查证据 → 状态'清单；系统先做机械校验，
校验不通过则拒绝调用（AI 修正后重新调用），通过则停止自动续跑。
机械拒绝只做格式纠正、不占续跑预算；连续拒绝超限时兜底强制放行（防死循环）。
"""

from ..param_utils import to_positive_int

_STATUSES = ("完成", "未完成", "受阻")
_FIELDS = ("要求", "证据", "状态")

# 连续机械拒绝上限：超过此数仍强制放行兜底（防死循环）。
# 机械拒绝不占续跑预算——它只是格式纠正，AI 一次就能改对；本上限仅防极端不配合
_MAX_MECHANICAL_REJECTS = 10

DEFINITION = {
    "type": "function",
    "function": {
        "name": "GoalComplete",
        "description": (
            "声明当前任务已完成——任务执行的最后一步调用。\n"
            "调用前必须全部满足：\n"
            "1. 任务目标已实际达成，关键结果已经真实验证，而非仅凭推理判断；\n"
            "2. 最终答复已完整写出。\n"
            "必须提交完成清单：逐条列出 任务要求 → 可检查证据 → 状态，覆盖任务的全部要求；未完成或受阻的项也要列出并写明原因。\n"
            "清单每项须带 编号，逐条覆盖需求基线（GoalBaseline）的全部编号；未建基线时须先调用 GoalBaseline 建立基线。\n"
            "证据必须可检查：可复现命令+输出摘要 / 文件路径 / 实际现象；"
            "「已验证」「已完成」这类空泛表述不算证据。\n"
            "未完成或受阻的项，如实标注并写明原因。\n"
            "收到工具结果后简短收尾，不要重复完整答复内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "checklist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "编号": {
                                "type": "integer",
                                "description": "对应需求基线条目编号（见 GoalBaseline）",
                            },
                            "要求": {
                                "type": "string",
                                "description": "任务的一项要求",
                            },
                            "证据": {
                                "type": "string",
                                "description": "可检查证据：可复现命令+输出摘要 / 文件路径 / 实际现象；"
                                               "「已验证」这类空泛表述不算证据",
                            },
                            "状态": {
                                "type": "string",
                                "enum": ["完成", "未完成", "受阻"],
                                "description": "该项要求的实际状态；未完成/受阻须写明原因",
                            },
                        },
                        "required": ["编号", "要求", "证据", "状态"],
                    },
                    "description": "完成清单：[{编号, 要求, 证据, 状态}, ...]",
                },
            },
            "required": ["checklist"],
        },
    },
}


CAPABILITY = {
    "label": "声明完成",
    "dispatch": "serial",
    "summary": "none",
    "trusted_output": True,
}


def _check_checklist(checklist, ctx):
    """机械校验完成清单：通过返回 None，否则返回对AI的具体问题描述"""
    if not isinstance(checklist, list) or not checklist:
        return (
            "未提交完成清单。请提交 checklist：逐条列出 任务要求 → 可检查证据 → 状态"
            "（状态为 完成/未完成/受阻），覆盖任务的全部要求。"
        )

    for i, item in enumerate(checklist, 1):
        if not isinstance(item, dict):
            return f"第{i}项不是对象，应为含「要求」「证据」「状态」三个字段的对象。"
        for key in _FIELDS:
            v = item.get(key)
            if v is None:
                return f"第{i}项缺少字段 {key}（要求/证据/状态 三个字符串字段均为必填）。"
            if not isinstance(v, str):
                return f"第{i}项的 {key} 必须是字符串。"
        if not item["要求"].strip():
            return f"第{i}项的 要求 为空白，请写明该项任务要求。"
        if item["状态"].strip() not in _STATUSES:
            return f"第{i}项的 状态 非法：{item['状态']}，只能是 完成/未完成/受阻。"
        if not item["证据"].strip():
            return (
                f"第{i}项的 证据 为空白。证据必须可检查：可复现命令+输出摘要 / "
                "文件路径 / 实际现象；「已验证」「已完成」这类空泛表述不算证据；"
                "未完成/受阻项须写明原因。"
            )

    # ── 编号校验：每项必须带 编号（对应需求基线条目），同一清单内不得重复 ──
    nos = []
    for i, item in enumerate(checklist, 1):
        no = to_positive_int(item.get("编号"))
        if no is None:
            return (f"第{i}项缺少有效的 编号（正整数）：清单每项须带 编号，对应需求基线条目；"
                    "先调用 GoalBaseline 建立需求基线，再按基线编号填写。")
        nos.append(no)
    dup = next((n for n in nos if nos.count(n) > 1), None)
    if dup is not None:
        return f"完成清单中 编号 {dup} 重复出现：每个基线编号对应清单一项，请合并或改填正确编号。"

    # ── 基线门禁：没有基线就没有覆盖基准，先拆基线再声明完成（纯机械判定）──
    baseline = ctx.goal_baseline if isinstance(ctx.goal_baseline, list) else []
    if not baseline:
        return ("尚未建立需求基线：目标模式下请先调用 GoalBaseline 把任务原文拆成编号需求清单"
                "（可只有 1 条），再提交完成清单。")

    base_items = [it for it in baseline if isinstance(it, dict)]
    covered = set(nos)
    # ── 编号覆盖：清单编号集合 ⊇ 基线编号集合（缺号为漏项，多号为越界）──
    missing = [it for it in base_items if it.get("编号") not in covered]
    if missing:
        listed = "、".join(f"编号 {it.get('编号')}（{it.get('要求', '')}）" for it in missing[:5])
        tail = f"，等共 {len(missing)} 项" if len(missing) > 5 else ""
        return (f"完成清单未覆盖需求基线的全部编号，缺失：{listed}{tail}。"
                "请补齐这些编号的清单项（确实未完成/受阻的如实标注），再重新调用 GoalComplete。")
    extra = [n for n in nos if n not in {it.get("编号") for it in base_items}]
    if extra:
        return ("完成清单出现需求基线中没有的编号："
                + "、".join(f"编号 {n}" for n in extra[:5])
                + "。请先调用 GoalBaseline 把该要求追加进基线（新增条目不要填 编号），或改用基线中的编号。")

    # ── 放弃项要求：基线已标 放弃 的编号，清单不得标 完成（如实标注未完成/受阻）──
    for item, no in zip(checklist, nos):
        base = next((it for it in base_items if it.get("编号") == no), None)
        if base and base.get("状态") == "放弃" and item["状态"].strip() == "完成":
            return (f"编号 {no} 在需求基线中已标记放弃（原因：{base.get('原因', '')}）；"
                    "清单中必须如实标注「未完成」或「受阻」并写明原因。")

    # todo 交叉校验：计划仍有未勾选项时，清单必须如实含'未完成/受阻'项
    unfinished = [
        t for t in (ctx.current_todos or [])
        if isinstance(t, dict) and t.get("status") != "completed"
    ]
    if unfinished and not any(it["状态"].strip() != "完成" for it in checklist):
        names = "、".join(str(t.get("content", "")) for t in unfinished[:5])
        tail = f"等共{len(unfinished)}项" if len(unfinished) > 5 else ""
        return (
            f"计划仍有未勾选完成的项: {names}{tail}。"
            "若这些任务实际已完成，请先调用TodoWrite勾选它们，再重新调用GoalComplete；"
            "若确实未完成或放弃，请在完成清单中如实标注「未完成」或「受阻」并写明原因。"
        )
    return None


def _approve(ctx, checklist, forced: bool = False) -> str:
    """放行路径：记录清单、置完成标记（后台任务清理由 agent_loop 结案点执行）"""
    items = checklist if isinstance(checklist, list) else []
    ctx.goal_checklist = items
    ctx.goal_complete = True
    ctx.goal_mech_rejects = 0  # 放行后连续机械拒绝计数清零
    if any(str(it.get("状态", "")).strip() not in ("", "完成")
           for it in items if isinstance(it, dict)):
        ctx.goal_honest = True
    if forced:
        ctx.goal_forced = True
        return (
            f"[GOAL_COMPLETE] 完成清单连续被拒已达上限（{_MAX_MECHANICAL_REJECTS}次），已放行。"
            "请向用户总结完成情况，并如实说明清单未通过校验的情况。"
        )
    if ctx.goal_honest:
        return (
            "[GOAL_COMPLETE] 完成声明已提交（清单含未完成/受阻项）。"
            "请向用户总结完成情况与未完成原因。"
        )
    return "[GOAL_COMPLETE] 完成声明已提交，系统将独立复核。请向用户总结完成情况。"


def execute(checklist=None, _tool_context=None) -> str:
    """
    声明目标任务完成：校验并记录结构化完成清单。

    Args:
        checklist: 完成清单，[{'要求': ..., '证据': ..., '状态': ...}, ...]，
            状态 ∈ {'完成','未完成','受阻'}
        _tool_context: 工具运行时上下文（内部参数，由registry注入）

    Returns:
        提示文本，回传给LLM
    """
    if _tool_context is None:
        # 理论不发生（registry 始终注入）；保持健壮：不校验、不计数，仅提示
        return "[提示] 缺少工具上下文，完成清单未记录。请重新调用GoalComplete。"

    problem = _check_checklist(checklist, _tool_context)
    if problem is None:
        return _approve(_tool_context, checklist)

    # 拒绝路径：机械拒绝只计数、不占续跑预算；
    # 连续机械拒绝超过 _MAX_MECHANICAL_REJECTS 次仍强制放行兜底，防死循环
    _tool_context.goal_mech_rejects += 1
    if _tool_context.goal_mech_rejects > _MAX_MECHANICAL_REJECTS:
        return _approve(_tool_context, checklist, forced=True)
    return f"[拒绝] 完成清单未通过校验：{problem}修正后重新调用GoalComplete。"
