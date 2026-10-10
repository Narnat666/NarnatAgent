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

from .llm import run_cancelable
from ..tools import registry
from ..tools import safety
from ..tools.param_utils import to_bool
from ..tools.tool_context import ToolContext
from ..tools.exec_signal import error_line, strip_tags
from ..tools.read import DEFINITION as _READ_DEF
from ..tools.glob import DEFINITION as _GLOB_DEF
from ..tools.grep import DEFINITION as _GREP_DEF
from ..tools.bash import DEFINITION as _BASH_DEF

# ── 常量 ──
VERIFY_MAX_ROUNDS = 100      # 验证器 mini 循环最大轮数（防失控）
_VERDICTS = ("pass", "fail", "uncertain")
# 验证器可用裁决外的异常信息截断长度（错误文本进 summary，不泄漏任何凭证）
_ERR_TEXT_MAX = 200

VERIFY_SYSTEM_PROMPT = (
    "你现在是完成验证员。父代理 AI 声称任务已完成，并提交了它改动的文件清单；"
    "你的职责是核对它是否真的做到了用户的要求。\n"
    "\n"
    "规则：\n"
    "- 先立要求：从用户任务原文列出全部要求（只以原文为准，不新增、不缩小）。\n"
    "- 再核改动：对清单里的每个文件用 Read 读取真实内容，核对改动是否满足对应要求；"
    "可结合项目内直接相关的文件（调用方、配置、测试）判断改动是否完整、有没有改一半。\n"
    "- 把完成当作未证实，只依据可核验的证据；不采信父代理的文字描述。\n"
    "- 文件不存在、内容与描述不符、要求未被覆盖 → 判不通过。\n"
    "- 核对范围限于清单文件及其直接相关文件；不做全盘搜索。\n"
    "- 不重做任务、不修改任何文件（Shell 只用于查看：不得修改任何文件或系统状态，"
    "不得执行删除类命令，不得提交后台任务）。\n"
    "- 不得添加任务要求之外的新要求。\n"
    "- 证据涉及远程设备、串口或硬件的，本地查不到不构成未完成，按证据本身判断："
    "命令、路径、结果齐全且自洽，视为有效证据。\n"
    "\n"
    "输出（严格 JSON，仅此格式）：\n"
    '{"verdict":"pass|fail|uncertain","gaps":["..."],"continue_prompt":"...","summary":"..."}\n'
    "- pass：全部要求都被真实改动满足；gaps 与 continue_prompt 可为空。\n"
    "- fail：存在可修复缺口。gaps 逐条列明缺什么；continue_prompt 是给主 AI 的可直接执行的"
    '返工指令（缺什么、怎么补、如何自证），结尾提示"完成后重新提交完成清单并调用 GoalComplete"。\n'
    "- uncertain：无法核实真伪（如证据在远程设备）；summary 说明原因。\n"
    '- summary 结尾附覆盖结论并给出计数："任务原文要求 K 项，清单覆盖 M 项"（K/M 为你的判断值）。\n'
    "\n"
    "只输出 JSON，不要输出任何其他文字（可用 ```json 代码块包裹）。"
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


# ── Shell 抽查策略（验证器 Shell 只用于查看：删除类、写文件、后台、危险 git 一律拒绝）──

# 只读 git 子命令白名单。收窄原则：位置参数即写语义的子命令（branch/tag/remote/reflog）、
# 可写对象的（fsck --lost-found）一律不收——它们的只读用法各有平替
# （列分支/标签用 show-ref；当前分支看 status），从白名单层面消灭整类绕过。
_GIT_READONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files",
                 "ls-tree", "cat-file", "describe", "blame", "shortlog",
                 "show-ref", "name-rev", "merge-base", "grep",
                 "diff-tree", "whatchanged", "count-objects"}

# git 词元正则共享定义源 tools/safety.py（与 bash/terminal 拦截判定语义恒等）
_RE_GIT = safety.RE_GIT
# 文件重定向识别：`>` 目标为文件即拒绝。`>&` 仅当后跟文件
# 描述符号（2>&1、>&2）属合流放行；POSIX 的 `>& file`（写文件的等价写法）
# 不得被误放行——原前瞻只排除 `>&`，把 `>& out.txt` 当合流漏过
_RE_REDIRECT = re.compile(r">(?!\s*&\s*\d)")


def _check_shell_policy(arguments: dict) -> str:
    """验证器 Shell 抽查策略：返回 "" 放行，否则返回拒绝原因文本。

    规则（按序）：后台提交/管理一律拒绝（bg 操作无 command 字段，须先于
    空命令早退判定）→ 无 command 放行（交给 bash 层报参数错）→ 删除类命令
    拒绝（含 cmd 等价写法归一化）→ 含 git 时逐段解析子命令，只放行只读
    白名单，且拒绝 --output 写文件参数 → 向文件重定向拒绝。
    """
    # 后台任务（提交与 status/wait/cancel 管理）一律拒绝：后台槽位表为
    # 进程级全局，验证器以只读身份不应触碰主会话的后台任务
    # （字符串布尔归一化："false" 不应被误判为后台提交而 fail-closed 误拒）
    if to_bool(arguments.get("background")) or arguments.get("bg"):
        return "验证器不提交/管理后台任务"
    command = arguments.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        return ""
    if safety.match_delete(command):
        return f"验证器不执行删除类命令: {command[:60]}"
    # cmd 等价写法归一化后再做 git 判定（g^it → git；拆分与匹配都在归一化文本上）
    scan = safety.scan_text(command)
    if _RE_GIT.search(scan):
        # 拆段符含单个 `&`（cmd 的顺序执行分隔符）：`git status & git push` 必须逐段检查
        for segment in re.split(r"&&|\|\||[;|\n&]", scan):
            if not _RE_GIT.search(segment):
                continue
            tokens = segment.split()
            idx = next((i for i, t in enumerate(tokens)
                        if _RE_GIT.search(t)), None)
            if idx is None:
                continue
            sub = ""
            i = idx + 1
            while i < len(tokens):
                tok = tokens[i]
                if tok in ("-C", "-c"):
                    i += 2  # 旗标各吃掉紧随的一个参数（如 -C <路径>）
                    continue
                if tok.startswith("-"):
                    i += 1  # 其余旗标不吞参数
                    continue
                sub = tok
                break
            if not sub:
                return "验证器无法确认 git 子命令是否只读，拒绝执行"
            if sub.lower() not in _GIT_READONLY:
                return (f"验证器只放行只读 git 子命令（如 status/diff/log/show），"
                        f"拒绝: git {sub}")
            # --output 写文件参数拒绝（从 git 词元起全量检查，含子命令之前的全局选项；
            # git 的缩写匹配在有歧义前缀时会报错，只能写全称）
            for tok in tokens[idx + 1:]:
                if tok.startswith("--output"):
                    return f"验证器只放行只读 git 查看，拒绝写输出参数: {tok}"
    if _RE_REDIRECT.search(command):
        return "验证器查看命令不得重定向输出到文件"
    return ""


class GoalVerifier:
    """独立完成验证器：全新会话 + 只读工具复核 + 三态裁决"""

    def __init__(self, llm, config, logger, stats=None):
        self._llm = llm
        self._config = config
        self._logger = logger
        # 可选旁路记账（费用日志）：验证器的 API 调用同样计入 cost_log，
        # 但不触碰主会话的 token/费用累计（stats.log_external_usage）
        self._stats = stats
        # 验证器工具定义（从各工具模块 DEFINITION 取，顺序固定；Shell 走策略检查）
        self._tool_defs = [_READ_DEF, _GLOB_DEF, _GREP_DEF, _BASH_DEF]
        self._tool_names = {d["function"]["name"] for d in self._tool_defs}
        # 轻量独立 ToolContext：只填 ignore_dirs/输出硬上限，不触碰主上下文状态；
        # git_skip_confirm 让白名单 git 命令不被 bash 层确认拦下，超时上限收紧到 300 秒
        self._tool_context = ToolContext(
            ignore_dirs=list(getattr(config.tools, "ignore_dirs", ()) or ()),
            max_tool_output_chars=getattr(config.tools, "max_output_chars", 0) or 0,
            git_skip_confirm=True,      # 白名单 git 命令不被 bash 层 git 确认拦下
            max_timeout_seconds=300,    # Shell 抽查命令超时上限
        )

    def verify(self, task: str, checklist: list, final_answer: str,
               cancel_check=None) -> VerifyResult:
        """独立验证：返回三态裁决（pass/fail/uncertain/interrupted）。

        Args:
            task: 任务原文
            checklist: 完成清单 [{"要求","证据","状态","改动文件"}, ...]
            final_answer: AI 的最终答复文本
            cancel_check: 可选可调用，返回 True 表示用户中断

        任何异常不外抛：捕获后返回 uncertain（summary 注明"验证不可用: 原因"）。
        """
        rounds = 0
        # 工具级取消：把 cancel_check 注入验证器自己的 ToolContext，长耗时只读工具
        # （Grep/Glob 扫大目录）在扫描中途即可中断；验证结束/异常后清除，避免残留
        self._tool_context.cancel_check = cancel_check
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
                    names, tool_results = self._execute_tools(tool_calls, cancel_check)
                    if cancel_check and cancel_check():
                        return VerifyResult(verdict="interrupted", summary="验证已中断")
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
        finally:
            self._tool_context.cancel_check = None

    # ── 内部实现 ──

    def _verify_model(self) -> Optional[str]:
        """验证用模型（config.ai.goal_verify_model，空=当前模型 → None）"""
        model = getattr(self._config.ai, "goal_verify_model", "") or ""
        return str(model).strip() or None

    def _build_input(self, task, checklist, final_answer) -> str:
        """构造首条 user 消息：任务原文 + 完成清单（含改动文件）+ 最终答复"""
        parts = ["【任务原文】", str(task or "(空)").strip(), ""]
        parts += ["", "【AI 提交的完成清单】"]
        items = checklist if isinstance(checklist, list) else []
        if items:
            for i, item in enumerate(items, 1):
                if isinstance(item, dict):
                    req = item.get("要求", "")
                    ev = item.get("证据", "")
                    st = item.get("状态", "")
                    files = item.get("改动文件", "")
                else:
                    req, ev, st, files = str(item), "", "", ""
                parts.append(f"{i}. 要求：{req}")
                parts.append(f"   改动文件：{files if files else '(未申报)'}")
                parts.append(f"   证据：{ev}")
                parts.append(f"   状态：{st}")
        else:
            parts.append("(清单为空)")
        parts += [
            "",
            "【AI 的最终答复】",
            str(final_answer or "(空)").strip(),
            "",
            "请按上述清单里的改动文件逐个用只读工具读取真实内容核对，"
            "然后按系统指令只输出裁决 JSON。",
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
            # 旁路记账：验证器调用同样计入费用日志（不触碰主会话统计）
            if "usage" in chunk and self._stats is not None:
                try:
                    self._stats.log_external_usage(chunk["usage"])
                except Exception:
                    pass

        content = "".join(content_parts)
        if finish is None:
            # 取消标记已置位而流未收尾（LLM 层取消时生成器静默 return，无 finish_reason）：
            # 这是用户中断，不是"响应流中断"，据实归入 cancelled 以便上层产出 interrupted
            if cancel_check and cancel_check():
                return content, [], "cancelled"
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

    def _execute_tools(self, tool_calls, cancel_check=None):
        """依次执行本轮工具调用（白名单工具；Shell 先过抽查策略）；返回 (名称列表, [(id, 结果)])

        用户中断（cancel_check 命中）时不再执行后续工具并立即返回。
        工具调用走 run_cancelable：调用体移入后台线程，主线程按 0.05s 轮询取消标记，
        工具自身不查取消（或取消检查点稀疏）时用户按 ESC 也能立即收敛，而不是
        卡到工具跑完（大目录 Grep 实测分钟级）。
        """
        names = []
        results = []
        for tc in tool_calls or []:
            if cancel_check and cancel_check():
                break
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
                    f"验证器只允许只读工具(Read/Glob/Grep/Shell)，拒绝执行: {name}"))))
                continue
            if name == "Shell":
                reason = _check_shell_policy(args)
                if reason:
                    results.append((tc_id, strip_tags(error_line(
                        "验证器策略拒绝: " + reason))))
                    continue
            cancelled, exec_result, error = run_cancelable(
                lambda: registry.execute(name, args, self._tool_context),
                cancel_check)
            if cancelled:
                break
            if error is not None:
                # worker 内异常按文件既有风格转成工具错误文本，不外抛到 verify 之外
                results.append((tc_id, strip_tags(error_line(
                    f"工具执行失败({name}): {error}"))))
                continue
            llm_result, _color = exec_result
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
