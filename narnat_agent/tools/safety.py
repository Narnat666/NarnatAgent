"""安全判定共享正则 —— 单一定义源

消费方（多处引用，语义必须恒等，勿再各自复制正则）：
- tools/bash   ：Shell 删除/git 命令的免确认拦截判定
- tools/terminal：远程命令同一套拦截判定
- tools/serial ：串口命令同一套拦截判定
- core/goal_verifier：验证器只读策略（删除类拒绝 + git 子命令白名单入口）

约束：本模块不 import 任何项目内模块（纯常量模块，防循环导入）。

设计说明（判定结构，文本启发式，非完备沙箱）：
1) 归一化：展开 cmd 的 ^ 转义（`d^el` → `del`）；
2) 引号语义：普通引号内的文本是**参数文本**（`echo "del x"`、`findstr "del"`），
   不参与命令判定；解释器容器引号（`cmd /c "del x"`、`bash -c 'rm -rf /'`）
   与行首引号（`"rm" x`）内的内容是**命令串**，单独参与判定；
3) 删除词须处于命令位置（行首 / 空白 / & / | / ( / ; / 反引号 / 斜杠之后），
   且后跟分隔符（空白 / TAB / , ; = / 斜杠 / 反斜杠 / 引号 / 反引号）；
4) 变量拼接：`set 变量=` 与 `!VAR!` 成对出现才拦截（cmd /v:on 藏词条件）；
5) git 判定同套归一化与引号语义。

防误删优先、同时严控误报（headless 下误报 = 命令被 fail-closed 取消执行）。

已知边界（文本层不可完备覆盖，有意不拦）：命令词经**执行期重建**的形态——
cmd `for %i in (del) do %i x`（变量间接）、bash 字符拼接 `r''m`/`r\\m`/`$x`/
`rm${IFS}`、`%var:~0,0%` 切片拼接、PowerShell 反引号转义/别名（`ri`）与
-EncodedCommand、外部解释器删除（python os.remove / .NET Delete() / robocopy
等词表外途径）。这些形态对正常使用不可达；本拦截面向"常规写法的确认提示"，
不做语法级沙箱（构造级等价判定需完整 shell 解析器，超出范围）。
"""

import re

# 命令位置：行首，或空白 / & / | / ( / ; / 反引号 / 斜杠 之后，允许 @ 前缀。
# 路径中段（`...\cfg\format\` 的 format 前为 `\`）不成立——避免目录名恰为
# 删除词的普通路径误判。引号场景由 _split_quoted 的结构化处理接手。
_CMD_POS = r"(?:^|[\s&|(;`/])@?"

# 后缀分隔符：cmd 的 token 分隔符全集（空白/TAB 归 \s、, ; =）+ 路径分隔 / \ +
# 引号/反引号（`del"x"` 等）。不含 `.`：避免把 `format.py`、`del.py` 这类
# 文件名误判（点形态由分支2单独处理）。
_SUFFIX = r"[\s/\\,;=\"'`]"

# 删除命令正则（在 _split_quoted 产出的扫描段上使用）：
# 分支1：命令位置 + 命令名 + 后缀分隔符（del x、del/x、del,x、del;x、del=x、
#        del\x、del"x"）；
# 分支2：命令位置 + del/erase/rd/rmdir 后紧贴 `.`（cmd 的 `del.\x`/`del. x`
#        等价 `del .`）——点后须为分隔符，避免误判文件名；
# 分支3：Remove-Item（PowerShell）；
# 词尾 \b 防止误伤 delphi、3rd、formatting 等普通词。
RE_DELETE = re.compile(
    _CMD_POS + r"(?:rm|del|rd|rmdir|erase|format)\b" + _SUFFIX
    + r"|" + _CMD_POS + r"(?:del|erase|rd|rmdir)\.(?=[\s/\\])"
    + r"|\bRemove-Item\b",
    re.IGNORECASE,
)

# 匹配 git 命令的简单正则（出现 git 即命中）
RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)

# cmd 延迟展开变量引用（!VAR!）与 set 赋值：单独出现均为正常文本（如
# `echo "wow!nice!"`、`cmd /v:on /c "set PATH=…;!PATH! && …"`）；
# 劫词条件 = set 赋值与 !VAR! 成对出现 **且展开后文本命中删除判定**
# （见 _expand_delayed_vars——只拦真正藏了删除词的拼接）
RE_DELAYED_EXPAND = re.compile(r"!([A-Za-z_][A-Za-z0-9_]*)!")

# set 赋值提取（值到 & / 引号 / 行尾截断；`set "NAME=VALUE"` 形式兼容）
_RE_SET_VALUE = re.compile(r'(?i)\bset\s+"?([A-Za-z_][A-Za-z0-9_]*)=([^&"\r\n]*)')

# 解释器容器：`<解释器> [其他开关] -c / /c / -Command <引号>` —— 引号内是命令串。
# 中间允许其它开关参数（如 `cmd /v:on /c`、`powershell -NoProfile -Command`）。
# 前缀允许命令分隔/分组/回显抑制符（`&cmd`、`|cmd`、`(cmd`、`@cmd`）与
# 路径前缀（`C:\...\cmd.exe`）；解释器含 `%ComSpec%`（cmd 路径变量）。
# 仅绑定已知 shell 解释器（python/grep 等的 -c 参数是代码/开关，不是命令串）。
_RE_SHELL_C = re.compile(
    r"(?i)(?:^|[\s\\/(|&;@])(?:cmd(?:\.exe)?\b|%comspec%|powershell(?:\.exe)?\b|"
    r"pwsh\b|bash\b|sh\b|zsh\b|dash\b|ksh\b)[^&|;\n]*?\s(?:-c|/c|-command)\s*$"
)

# 行首引号包裹的命令名（`"rm" x`）中，引号内内容按命令串扫描时的"裸命令词"形态
_RE_BARE_CMD = re.compile(
    r"^\s*(?:rm|del|rd|rmdir|erase|format)\s*$", re.IGNORECASE
)


def scan_text(command: str) -> str:
    """安全判定用的命令文本归一化：展开 cmd 的 ^ 转义（^X 解析为 X）。

    仅用于"是否需确认"的匹配，不改变实际执行的命令文本。^ 在 POSIX shell
    中不是转义符，去掉后同样不产生删除词误判（多一次确认的保守代价可接受）。
    """
    return command.replace("^", "")


def _expand_delayed_vars(segment: str) -> str:
    """近似展开 cmd 延迟展开变量（!VAR! → 同段内 set 赋的值）。

    仅用于判定匹配：把"set 赋值 + !VAR! 引用"的拼接还原为展开后文本，
    使"藏了删除词的拼接"（`set D=del&!D! x`）命中，而正常拼接
    （`set PATH=…;!PATH! && cmd`）不误报。单轮替换（不做递归）。
    变量名大小写不敏感（cmd 语义），未知变量保留原样。
    """
    assigns = {m.group(1).upper(): m.group(2)
               for m in _RE_SET_VALUE.finditer(segment)}
    if not assigns:
        return segment

    def _repl(m):
        return assigns.get(m.group(1).upper(), m.group(0))

    return RE_DELAYED_EXPAND.sub(_repl, segment)


def _split_quoted(scan: str, _depth: int = 0):
    """把归一化命令按引号语义拆成参与判定的扫描段。

    返回 (outside, containers)：
    - outside：引号外的文本（普通引号及其内容被剔除——参数文本不是命令）；
    - containers：命令串引号的内容——解释器容器引号（`cmd /c "…"`、
      `bash -c '…'`）与行首引号（`"rm" x`）。

    容器内容边界取**到段末**：cmd 的嵌套引号（`cmd /v:on /c "set "D=del"
    & !D! x"`）无法用"最近配对引号"精确切分，按到段末整体纳入扫描是
    安全侧（宁可多判定，不可漏拦）；嵌套容器（`cmd /c "cmd /c "del x""`）
    的内容再递归解析（内层引号内仍是命令串）。
    引号不配对时其余文本归入 outside（保守：仍参与判定）。
    """
    outside = []
    containers = []
    i, n = 0, len(scan)
    while i < n:
        ch = scan[i]
        if ch in "\"'":
            j = scan.find(ch, i + 1)
            if j == -1:
                outside.append(scan[i:])
                break
            before = scan[:i].rstrip()
            if not before or _RE_SHELL_C.search(before):
                content = scan[i + 1:]  # 命令串容器：内容到段末
                containers.append(content)
                if _depth < 4:
                    _sub_out, sub_cont = _split_quoted(content, _depth + 1)
                    containers.extend(sub_cont)
                break
            i = j + 1
            continue
        outside.append(ch)
        i += 1
    return "".join(outside), containers


def _segment_hits(segment: str) -> bool:
    """单段扫描：删除词命中，或 set+!VAR! 拼接**展开后**命中删除判定"""
    if RE_DELETE.search(segment):
        return True
    if _RE_BARE_CMD.match(segment):  # `"rm" x` 的引号内裸命令词
        return True
    if RE_DELAYED_EXPAND.search(segment) and _RE_SET_VALUE.search(segment):
        return bool(RE_DELETE.search(_expand_delayed_vars(segment)))
    return False


def match_delete(command: str) -> bool:
    """删除类命令判定（含 ^ 归一化、引号语义结构化、变量拼接条件拦截）"""
    if not command or not isinstance(command, str):
        return False
    outside, containers = _split_quoted(scan_text(command))
    if _segment_hits(outside):
        return True
    return any(_segment_hits(c) for c in containers)


def match_git(command: str) -> bool:
    """git 命令判定（含 ^ 归一化与引号语义：引号内文本不误判）"""
    if not command or not isinstance(command, str):
        return False
    outside, containers = _split_quoted(scan_text(command))
    if RE_GIT.search(outside):
        return True
    return any(RE_GIT.search(c) for c in containers)
