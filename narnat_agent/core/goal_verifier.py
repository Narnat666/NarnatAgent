"""目标完成验证器 —— 独立上下文 AI 复核（只读工具 + 三态裁决）

背景：/goal 目标模式下 AI 自报完成不可信（人工核查多数没完成）。本模块实现
"独立 AI 验证器"：主 AI 声明完成后，用全新会话（不带主会话历史）持只读工具
（Read/Glob/Grep）实地复核 AI 提交的结构化完成清单，输出三态裁决：

- pass      ：全部要求都有直接证据支撑，可结案
- fail      ：存在可修复缺口 → 给出返工提示词（continue_prompt），由上层注入
              主会话继续工作（最多拦截若干次，超限强制收尾）
- uncertain ：证据根本性不足且工具也无法查证，或验证过程本身不可用

设计约束（与主流程的边界）：
- 复用主会话 LLMClient 实例，但请求完全独立：每次 verify 构造全新 messages，
  只读工具用独立的轻量 ToolContext，不触碰主会话消息列表/统计/上下文状态；
- 请求走请求级覆盖（llm.chat_stream 的 tool_defs/model 参数）：工具表只发三只读
  工具，模型用 config.ai.goal_verify_model（空=当前模型）；
- 消息与主会话完全同构（OpenAI 格式），由 llm.py 转换层负责双协议兼容；
- 任何异常不得抛出到调用方：网络/超时/工具错误一律收敛为 uncertain。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..tools import registry
from ..tools.tool_context import ToolContext
from ..tools.exec_signal import error_line, strip_tags
from ..tools.read import DEFINITION as _READ_DEF
from ..tools.glob import DEFINITION as _GLOB_DEF
from ..tools.grep import DEFINITION as _GREP_DEF

# ── 常量 ──
VERIFY_MAX_ROUNDS = 12       # 验证器 mini 循环最大轮数（防失控）
_VERDICTS = ("pass", "fail", "uncertain")
# 验证器可用裁决外的异常信息截断长度（错误文本进 summary，不泄漏任何凭证）
_ERR_TEXT_MAX = 200

VERIFY_SYSTEM_PROMPT = (
    "你是独立完成验证员。只做核实与裁决：拿任务要求逐条核对 AI 的完成清单与最终答复，"
    "判定任务是否真的完成，不参与完成任务。\n"
    "\n"
    "核心规则：\n"
    "1. 把完成当作未证实：AI 自报完成不可信，只依据可检查证据（文件内容、命令输出、实际现象）。\n"
    "2. 证据不确定或无法核实，不算完成；「已处理」「已验证」这类空泛表述不算证据。\n"
    "3. 应当用只读工具（Read/Glob/Grep）实地核实：证据提到文件就打开确认，提到内容就搜索确认。\n"
    "4. 不得添加任务要求之外的新要求。\n"
    "5. 不得因 AI 的措辞自信或篇幅长而放行。\n"
    "\n"
    "输出（严格 JSON，仅此格式）：\n"
    '{"verdict":"pass|fail|uncertain","gaps":["..."],"continue_prompt":"...","summary":"..."}\n'
    "- pass：全部要求都有直接证据支撑；gaps/continue_prompt 可为空。\n"
    "- fail：存在可修复缺口；gaps 逐条列明未通过项；continue_prompt 是给主 AI 的可直接执行的"
    "返工指令（缺什么、怎么补、如何自证；结尾提示「完成后重新提交完成清单并调用 GoalComplete」）。\n"
    "- uncertain：证据根本性不足且工具无法查证；summary 说明原因。\n"
    "\n"
    "只输出 JSON，不要输出任何其他文字（可用 ```json 代码块包裹）。\n"
    "工具使用约束：只读、少而精（建议 ≤5 次工具调用）；不修改任何文件。"
)

# JSON 解析失败时回给验证器的一次性纠正提示（再失败即判 uncertain）
_JSON_RETRY_HINT = (
    "你的回复不是合法的裁决 JSON。请只输出 JSON（可用 ```json 代码块包裹），"
    "不要输出任何其他文字：\n"
    '{"verdict":"pass|fail|uncertain","gaps":["..."],"continue_prompt":"...","summary":"..."}'
)


@dataclass
class VerifyResult:
    """验证裁决结果（契约字段，后续接线任务按此消费）"""
    verdict: str          # "pass" | "fail" | "uncertain" | "interrupted"
    gaps: list = field(default_factory=list)      # 未通过的缺口清单（fail 时非空）
    continue_prompt: str = ""                     # fail 时给主 AI 的返工提示词（fail 必非空）
    summary: str = ""                             # 终端一行中文摘要（如 "未通过：2 项证据不足"）


def parse_verdict_json(text: str) -> Optional[dict]:
    """从验证器输出文本中容错提取裁决 JSON 对象；提取失败返回 None。

    容错范围：整体即 JSON、```json 代码块包裹、JSON 前后带任意文字、
    JSON 后还续有文字（raw_decode 兜底）；只接受含 verdict 字段的 dict。
    """
    if not text or not isinstance(text, str):
        return None
    candidates = [text.strip()]
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text):
        candidates.append(m.group(1).strip())
    left, right = text.find("{"), text.rfind("}")
    if left != -1 and right > left:
        candidates.append(text[left:right + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    # raw_decode 兜底：从每个 { 位置尝试解析（容忍 JSON 后带非 JSON 文字）
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        idx = text.find("{", idx + 1)
    return None


def _build_result(parsed: dict) -> VerifyResult:
    """裁决 JSON → VerifyResult。

    fail 兜底（契约：gaps 非空、continue_prompt 必非空）：模型漏填时补齐
    缺口清单与结案指令，保证返工提示词一定包含缺口信息与"重新提交清单并调用
    GoalComplete"的收尾要求。
    """
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in _VERDICTS:
        verdict = "uncertain"

    raw_gaps = parsed.get("gaps")
    if isinstance(raw_gaps, list):
        gaps = [g if isinstance(g, str) else json.dumps(g, ensure_ascii=False)
                for g in raw_gaps if g]
    elif raw_gaps:
        gaps = [str(raw_gaps)]
    else:
        gaps = []

    continue_prompt = str(parsed.get("continue_prompt") or "").strip()
    summary = str(parsed.get("summary") or "").strip()

    if verdict == "fail":
        if not gaps:
            gaps = [summary or "验证器判定存在未满足的要求（未列出具体缺口）"]
        if not any(g in continue_prompt for g in gaps):
            head = "【独立验证未通过】以下要求缺少可核实证据：\n" + \
                   "\n".join(f"- {g}" for g in gaps)
            continue_prompt = f"{head}\n{continue_prompt}" if continue_prompt else head
        if "GoalComplete" not in continue_prompt:
            continue_prompt = (continue_prompt.rstrip() +
                               "\n完成后重新提交完成清单并调用 GoalComplete。")
    if not summary:
        if verdict == "pass":
            summary = "通过"
        elif verdict == "fail":
            summary = f"未通过：{len(gaps)} 项证据不足"
        else:
            summary = "无法核实：证据不足"
    return VerifyResult(verdict=verdict, gaps=gaps,
                        continue_prompt=continue_prompt, summary=summary)


class GoalVerifier:
    """独立完成验证器：全新会话 + 只读工具复核 + 三态裁决"""

    def __init__(self, llm, config, logger):
        self._llm = llm
        self._config = config
        self._logger = logger
        # 只读三工具定义（从各工具模块 DEFINITION 取，顺序固定）
        self._tool_defs = [_READ_DEF, _GLOB_DEF, _GREP_DEF]
        self._tool_names = {d["function"]["name"] for d in self._tool_defs}
        # 轻量独立 ToolContext：只填 ignore_dirs 与输出硬上限，不触碰主上下文状态
        self._tool_context = ToolContext(
            ignore_dirs=list(getattr(config.tools, "ignore_dirs", ()) or ()),
            max_tool_output_chars=getattr(config.tools, "max_output_chars", 0) or 0,
        )

    def verify(self, task: str, checklist: list, final_answer: str,
               cancel_check=None) -> VerifyResult:
        """独立验证：返回三态裁决（pass/fail/uncertain/interrupted）。

        Args:
            task: 任务原文
            checklist: 完成清单 [{"要求","证据","状态"}, ...]
            final_answer: AI 的最终答复文本
            cancel_check: 可选可调用，返回 True 表示用户中断

        任何异常不外抛：捕获后返回 uncertain（summary 注明"验证不可用: 原因"）。
        """
        rounds = 0
        try:
            if cancel_check and cancel_check():
                return VerifyResult(verdict="interrupted", summary="验证已中断")
            messages = [
                {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_input(task, checklist, final_answer)},
            ]
            model = self._verify_model()
            json_retried = False
            while rounds < VERIFY_MAX_ROUNDS:
                rounds += 1
                if cancel_check and cancel_check():
                    return VerifyResult(verdict="interrupted", summary="验证已中断")

                content, tool_calls, problem = self._run_round(messages, model, cancel_check)
                if problem == "cancelled":
                    return VerifyResult(verdict="interrupted", summary="验证已中断")
                if problem:
                    self._log("warning", f"验证不可用(第{rounds}轮): {problem}")
                    return VerifyResult(
                        verdict="uncertain", summary=f"验证不可用: {problem}")

                if tool_calls:
                    names, tool_results = self._execute_tools(tool_calls)
                    self._log("info", f"验证第{rounds}轮: 工具调用 {len(names)} 个"
                                      f"（{'、'.join(names)}）")
                    messages.append({"role": "assistant", "content": content or None,
                                     "tool_calls": tool_calls})
                    for tc_id, result in tool_results:
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": result})
                    continue

                parsed = parse_verdict_json(content)
                if parsed is None:
                    if not json_retried:
                        json_retried = True
                        self._log("info", f"验证第{rounds}轮: 裁决 JSON 解析失败，追问一次")
                        messages.append({"role": "assistant", "content": content or None})
                        messages.append({"role": "user", "content": _JSON_RETRY_HINT})
                        continue
                    self._log("warning", "验证不可用: 裁决 JSON 解析失败（已重试）")
                    return VerifyResult(
                        verdict="uncertain", summary="验证不可用: 验证器输出无法解析为裁决 JSON")
                result = _build_result(parsed)
                self._log("info", f"验证完成: 轮数={rounds} verdict={result.verdict} "
                                  f"summary={result.summary}")
                return result

            self._log("warning", f"验证轮数超限({VERIFY_MAX_ROUNDS})，转 uncertain")
            return VerifyResult(
                verdict="uncertain", summary=f"验证不可用: 轮数超限({VERIFY_MAX_ROUNDS}轮)")
        except Exception as e:
            self._log("error", f"验证异常: {e}")
            return VerifyResult(verdict="uncertain", summary=f"验证不可用: {e}")

    # ── 内部实现 ──

    def _verify_model(self) -> Optional[str]:
        """验证用模型（config.ai.goal_verify_model，空=当前模型 → None）"""
        model = getattr(self._config.ai, "goal_verify_model", "") or ""
        return str(model).strip() or None

    def _build_input(self, task, checklist, final_answer) -> str:
        """构造首条 user 消息：任务原文 + 完成清单 + 最终答复"""
        parts = ["【任务原文】", str(task or "(空)").strip(), "", "【AI 提交的完成清单】"]
        items = checklist if isinstance(checklist, list) else []
        if items:
            for i, item in enumerate(items, 1):
                if isinstance(item, dict):
                    req = item.get("要求", "")
                    ev = item.get("证据", "")
                    st = item.get("状态", "")
                else:
                    req, ev, st = str(item), "", ""
                parts.append(f"{i}. 要求：{req}")
                parts.append(f"   证据：{ev}")
                parts.append(f"   状态：{st}")
        else:
            parts.append("(清单为空)")
        parts += [
            "",
            "【AI 的最终答复】",
            str(final_answer or "(空)").strip(),
            "",
            "请逐条核对：用只读工具实地核实证据是否真实成立，然后按系统指令只输出裁决 JSON。",
        ]
        return "\n".join(parts)

    def _run_round(self, messages, model, cancel_check):
        """执行一轮验证请求；返回 (content, tool_calls, problem)。

        problem: None=正常完成；"cancelled"=用户中断；其它=不可用原因（转 uncertain）。
        """
        content_parts = []
        tool_calls = []
        finish = None
        interrupted_info = None
        for chunk in self._llm.chat_stream(
                messages,
                tool_defs=self._tool_defs,
                model=model,
                no_thinking=True,
                cancel_check=cancel_check):
            if cancel_check and cancel_check():
                return "", [], "cancelled"
            if "content" in chunk and "tool_calls" not in chunk:
                content_parts.append(chunk["content"])
            if "tool_calls" in chunk:
                tool_calls = chunk["tool_calls"]
            if "stream_interrupted" in chunk:
                interrupted_info = chunk["stream_interrupted"]
            if "finish_reason" in chunk:
                finish = chunk["finish_reason"]

        content = "".join(content_parts)
        if finish is None:
            detail = ""
            if interrupted_info:
                detail = (f"（{interrupted_info.get('kind', '')}: "
                          f"{str(interrupted_info.get('detail', ''))[:_ERR_TEXT_MAX]}）")
            return content, [], f"响应流中断{detail}"
        if finish == "error":
            return content, [], f"LLM调用出错: {content[:_ERR_TEXT_MAX]}"
        if finish == "context_overflow":
            return content, [], "请求超出模型上下文限制"
        return content, tool_calls, None

    def _execute_tools(self, tool_calls):
        """依次执行本轮工具调用（白名单只读三工具）；返回 (名称列表, [(id, 结果)])"""
        names = []
        results = []
        for tc in tool_calls or []:
            tc_id = tc.get("id", "") or ""
            func = tc.get("function") or {}
            name = func.get("name", "") or ""
            names.append(name)
            try:
                args = json.loads(func.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            if name not in self._tool_names:
                results.append((tc_id, strip_tags(error_line(
                    f"验证器只允许只读工具(Read/Glob/Grep)，拒绝执行: {name}"))))
                continue
            llm_result, _color = registry.execute(name, args, self._tool_context)
            if isinstance(llm_result, str):
                llm_result = strip_tags(llm_result)
            results.append((tc_id, llm_result))
        return names, results

    def _log(self, level: str, msg: str) -> None:
        """日志出口（logger 可缺省；日志失败不影响验证流程）"""
        if not self._logger:
            return
        try:
            getattr(self._logger, level)("goal_verifier", msg)
        except Exception:
            pass
