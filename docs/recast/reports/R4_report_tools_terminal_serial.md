# R4 现状报告：远程执行类工具（terminal SSH / serial 串口）

调研范围（逐文件全文通读，行数与任务清单一致）：

| 文件 | 行数 |
|------|------|
| narnat_agent/tools/terminal/ssh_session.py | 1317 |
| narnat_agent/tools/terminal/__init__.py | 972 |
| narnat_agent/tools/terminal/remote.py | 308 |
| narnat_agent/tools/serial/serial_session.py | 549 |
| narnat_agent/tools/serial/__init__.py | 599 |

说明：
- 所有行号以当前工作副本为准（本报告未修改任何源文件，只读调研）。
- 「对外接口」列出本文件定义的类/函数/方法（含带下划线但被本组之外模块引用的名字，如 `get_session`、`_normalize_device_for_tools`、`_truncate_output`），逐个给签名与用途。
- 「未验证」= 静态阅读无法确认，需运行/真机才能判定。
- `tool_exp/` 为仓库内的实验与手工验证脚本目录（非产品代码路径），本报告单列「被依赖」中的脚本群，不逐条展开。

---

### narnat_agent/tools/terminal/ssh_session.py（1317行）

**职责**：封装单个 SSH 交互式会话（paramiko PTY channel），提供命令执行 / 交互输入 / Ctrl+C 中断 / 断线重连，并实现哨兵完成检测、PS1 提示符检测、sudo 密码自动注入、ANSI 输出清洗与后台耗时命令的 backlog 收敛。

**对外接口**（public 类/函数/方法，逐个列签名）：

模块级函数：
- `def _ansi_sub(text: str) -> str`（L42）：剥离 ANSI 转义序列（调用类常量 ANSI_RE）。
- `def _truncate_output(text: str, max_chars: int) -> str`（L47）：保留头 2/3、尾 1/3 的截断，切点做框架标签吸附（safe_cut_points），中段插入截断提示；max_chars<=0 返回 error_line。

类 `class SSHSession:`（L67）

类常量：
- `RE_PASSWORD_PROMPT = re.compile(...)`（L72-78）：sudo/密码提示检测正则（`[sudo].*password` / `Password:` / `密码:` / `passphrase for key`，IGNORECASE）。
- `RE_SUDO_REJECT = re.compile(...)`（L81-86）：sudo 密码被拒文案（`sorry...try again` / `incorrect password` / `bad password`）。
- `MAX_SUDO_INJECT_ATTEMPTS = 3`（L90）：单次读循环内自动注入尝试上限。
- `ANSI_RE = re.compile(...)`（L93-98）：ANSI/DEC 私有序列。
- `PROMPT_LINE_RE = re.compile(r"^[^@]+@[^:]+:[^\n]*[#$>]\s*$")`（L101）：真实 shell 提示符行。
- `PS1_STRICT_RE = re.compile(r'^[^\s@]+@[^\s:]+:[^\s]*[$#]\s*$')`（L107）：严格 PS1 形态。
- `PS1_LOOSE_RE = re.compile(r'^.{0,32}?[$#]\s*$')`（L109）：宽 PS1 形态（busybox/裸 root）。
- `PS1_CONFIRM_TIMEOUTS = 2`（L113）：PS1 静默确认窗口（每次≈channel timeout 0.5s）。
- `MARKER_TAIL_WINDOW = 8192`（L117）：哨兵检测尾部窗口。

公开方法：
- `def __init__(self, host: str, username: str, port: int = 22, key_path: Optional[str] = None, password: Optional[str] = None, sudo_password: Optional[str] = None, timeout: int = 15)`（L119-121）：建会话并立即建连（内部调用 `_open_channel`，L144）。
- `@property def prompt(self) -> str`（L275-276）：合成 `user@host:path$`（home 缩写规则见 L284-289）。
- `def execute(self, command: str, timeout: int = 0, max_output_chars: int = 8000) -> str`（L292）：发送命令并读取至哨兵完成 / 超时 / 中断。
- `def send_input(self, text: str, timeout: int = 0, max_output_chars: int = 8000) -> str`（L347）：向忙终端发送交互输入；`^C`/`\x03` 发原始 Ctrl+C。
- `def reconnect(self)`（L200）：复用凭据重建通道并恢复 cwd。
- `def close(self)`（L477）：关闭 channel，transport 交后台线程回收。

内部方法（同文件内被调用，但被 `terminal/__init__.py` 等外部模块直接触碰的已在「补丁痕迹」标注）：
- `def _open_channel(self, key_path: Optional[str], password: Optional[str], timeout: int)`（L171）
- `def _abort_interrupted(self)`（L257）
- `def _initialize(self)`（L269）：读初始输出 + `_update_cwd`（被 terminal/__init__.py L475 调用）。
- `def _read_until_interrupt_prompt(self, timeout: float) -> str`（L455）
- `def _close_transport(self)`（L489）
- `def _try_read_residual(self, duration: float = 3.0) -> str`（L496）
- `def _drain_stale_output(self)`（L523）
- `def _update_cwd(self, timeout: int = 3)`（L545）
- `def _start_busy_watcher(self, marker: str, pwd_marker: str, initial_output: str = "", ps1_suspect: bool = False)`（L596-597）
- `def _drain_backlog(self) -> str`（L701）
- `def _append_backlog(self, text: str) -> None`（L708）
- `def _extract_cwd(self, output: str, marker: str, pwd_marker: str) -> Optional[str]`（L715）
- `@staticmethod def _ps1_candidate(text: str) -> bool`（L738-739）
- `def _send_sentinel(self, marker: str, pwd_marker: str)`（L769）
- `def _read_until_marker(self, marker: str, pwd_marker: str, timeout: int = 0, expect_echo: bool = True) -> str`（L779-780）
- `def _parse_output(self, raw: str, marker: str, pwd_marker: str) -> tuple[str, Optional[str], Optional[int]]`（L1076）
- `def _parse_partial_output(self, raw: str, marker: str) -> str`（L1181）
- `def _read_until_prompt(self, timeout: int = 5) -> str`（L1199）
- `@staticmethod def _sentinel_line_present(text: str, pwd_marker: str) -> bool`（L1221-1222）
- `@staticmethod def _strip_caret_echo(text: str) -> str`（L1236-1237）
- `@staticmethod def _strip_trailing_prompt(text: str) -> str`（L1246-1247）
- `@staticmethod def _strip_echo(raw: str, drop_echo_first_line: bool = True) -> str`（L1262-1263）
- `@staticmethod def _clean_output(raw: str) -> str`（L1280-1281）

**依赖**（本文件 import 的内部模块，附行号）：

- `from ..exec_signal import rc_line, error_line, safe_cut_points`（L38）
- `from ..token_estimate import estimate_text_tokens`（L39）
- 外部：`paramiko`（L35）、`socket`（L36）；标准库 `os`(L28) `re`(L29) `shlex`(L30) `time`(L31) `threading`(L32) `typing.Optional`(L33)

**被依赖**（grep 复验）：

- `narnat_agent/tools/terminal/__init__.py:21` `from .ssh_session import SSHSession`
- `narnat_agent/tools/terminal/__init__.py:24` `SSHSession` 出现在 `__all__`
- `narnat_agent/tools/terminal/remote.py:10` `from . import get_session, SSHSession`（仅类型注解用途，L30/L270-272）
- `tool_exp/` 脚本群（≥47 个文件引用本模块，示例行号）：`tool_exp/diag_edge_cases.py:5`、`tool_exp/test_ps1_offline.py:5`、`tool_exp/vt_stress_240.py:10`、`tool_exp/verify_ssh_perline.py:10`、`tool_exp/zz_rc_regression.py:19`、`tool_exp/compare_versions_probe.py:13`、`tool_exp/probe_mechanism_risks.py:20`、`tool_exp/_obsolete_20260910/regression_test.py:22`

**状态**：

- 模块级全局：**无**（本文件不含模块级可变变量；`_ansi_sub`、`_truncate_output` 为无状态函数）。
- 类变量：全部为不可变正则/整数常量（见上，L72-L117）。
- 实例状态（`__init__` 建立，行号含读写方）：
  - `host` / `username` / `port`（L122-124）：只读语义；外部读 `terminal/__init__.py` L310/L411/L436/L491/L669。
  - `_cwd`（L125）：写 L593（`_update_cwd`）、L691（watcher）、L1012、L1068（正常/中断解析）；读 L284-289（prompt 合成）、外部读 L669（status）。
  - `_sudo_password`（L126）：读 L936；`_sudo_mismatch`（L129）：写 L949，读 L936/L943。
  - `_reconnect_params`（L132-134）：读 L241-242（重连）。
  - `_reconnect_lock`（L138）：并发重连互斥（L207）。
  - `_interrupt`（threading.Event，L142）：写（set）`__init__.py:167`、`__init__.py:180`；写（clear）L213、L262、L390、L397、L985；读 L246、L460、L502、L514、L553、L565、L623、L825、L856、L873、L891、L980、L1204、L1214。
  - `_client`（paramiko SSHClient，L174）/ `_channel`（PTY channel，L197）：外部跨层访阅见「补丁痕迹」第 1 条。
  - `_busy`（L146）：写 L231、L403、L425、L696、L950、L969、L1021、L1043、L1052、L1065；读 L308、L360；外部读 L177、`__init__.py:667`。
  - `_last_command`（L147）：写 L232、L333、L437；读 L1124。
  - `_echo_enabled`（L150）：写 L576；读 L1028。
  - `_pending_marker` / `_pending_pwd_marker`（L153-154）：写 L233-234、L404-405、L798-799；读 L426、L443-444、L951、L970。
  - `_sentinel_sent`（L157）：写 L235、L332、L406、L777；读 L618、L641、L840、L998。
  - `_watcher_stop`（L160）：set L219、L370、L480；clear L613；同名对象重置 L239。
  - `_watcher_thread`（L161）：写 L237、L698；读 L224、L371。
  - `_watcher_finished`（L165）：写 L665；读 L379。
  - `_backlog`（L168）+ `_backlog_lock`（L169）：写 L236、L688、L694、L713；读 L704。
  - `_initial_output`（L272）：外部读 `__init__.py` L502/L504。

**行为要点**（编号；每条附行号）：

1. 建连参数：`set_missing_host_key_policy(AutoAddPolicy())`（L174-175）；`timeout`/`banner_timeout`/`auth_timeout` 同时设置（L179-182）；`key_path` 走 `key_filename=os.path.expanduser(...)`（L183-184）；给 `password` 时直传（L185-186）；不给 password 时传空串并启用 `look_for_keys`/`allow_agent`（L187-192，注释说明空密码设备不可连的原因）；`set_keepalive(30)`（L195）；`invoke_shell(term="xterm", width=200, height=50)`（L197）；`channel.settimeout(0.5)`（L198）。
2. `prompt` 显示规则：`/home/{username}`→`~`，`/home/{username}/x`→`~/x`，其余原样（L284-290）。
3. `execute` 入口门控：忙则直接返回 `[上一个命令尚未完成，此终端暂不可用。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]` + prompt（L308-310）；随后先取 backlog（L313）、再排空残留输出（L316）。
4. 哨兵命名：`marker = __NARNAT_MARKER_{time.time_ns()}__`、`pwd_marker = __NARNAT_PWD_{time.time_ns()}__`（L318-319）。
5. **哨兵延迟注入协议**：只发送命令本体（L332-334），哨兵在「检测到真实 PS1 + 静默确认窗口」后才作为独立命令发送（L769-777），哨兵内容为 `rc=$?; printf '\n'; echo {marker}$rc; pwd -P; echo {pwd_marker}`（L776）。协议动机（旧协议缺陷）写在 L321-331。
6. 结果拼装：backlog 若以 `[错误` 开头则置顶（L338-342），否则以 `[后台命令已完成，输出如下]\n{backlog}\n{30个'-'}\n{result}` 前缀（L343-344）。
7. 正常完成返回格式：`{rc行}{命令输出}\n{prompt}`（L1064-1074）；`rc行 = rc_line(exit_code)`，exit_code 解析失败时为 None 且不输出该行（L1070）；解析入口 `_parse_output`（L1066）。
8. 纯超时返回：置 `_busy=True`（L1052）、启动后台 watcher（L1053-1055，传尾部 4096 字符 + PS1 候选状态）、返回 `{已读部分输出}\n[超时: 命令执行超过{timeout}秒，仍在后台运行。可用 input 应答其交互提示（如y/n、密码），或用 input 发送 ^C 中断它]\n{prompt}`（L1056-1062）。
9. 连接中断（conn_lost 或 exec 路径零输出且有回显）判定：主动关闭 channel（L1039-1042），清 busy，返回 `error_line("连接已中断（设备可能关机/重启或网络不通），命令结果未知")` + prompt（L1044-1045）；触发条件见 L1028-1038（EOF/套接字异常，或 `expect_echo and not output and self._echo_enabled`）。
10. ESC 中断路径：读循环退出后 `interrupted = self._interrupt.is_set()`（L980），clear（L985），`_try_read_residual(3.0)` 收残留（L987-989）；哨兵已出现则正常解析并返回 `…\n[用户中断]\n{prompt}`（L996-1018，含 `rc_line`）；未出现则 `_parse_partial_output` + `[用户中断]`（L1021-1026）；哨兵未发送但已见 PS1 时补发哨兵再读 3s（L998-1005）。
11. sudo 密码自动注入三态状态机（L908-976）：判别三条件（尾部 2048 字符清洗后命中 `RE_PASSWORD_PROMPT` L926-927、原始输出尾部无换行 L926、宽容期 1.5s 内哨兵未到 L935）；未注入且未 mismatch 且有密码 → 直接 `channel.send(password + "\n")`（L936-942，不经 shell 命令行）；已注入后再出提示：有拒绝文案或 `inject_attempts >= MAX_SUDO_INJECT_ATTEMPTS` → `_sudo_mismatch=True` + `_busy=True` + 启动 watcher + 返回 `[自动注入的登录密码被sudo拒绝/多次注入后仍在等待密码：sudo密码与登录密码不同。请向用户询问sudo密码，然后用input输入；本会话后续不再自动注入]`（L943-961）；无拒绝文案视为链中下一个 sudo 继续注入（L962-966）；mismatch 或未设密码 → `_busy=True` + watcher + `[检测到密码提示，请用input action输入密码]`（L967-974）；同一旧提示重复评估被 `inject_mark_len` 抑制（L931-932）。
12. `send_input` 语义：空闲（非 busy）时**拒绝发送**并把未发送内容说明返回（L360-367，安全设计：防止输入被当命令执行）；busy 时先 stop watcher 并 join(2.0)（L370-373）；join 后若 `_watcher_finished` 为真则拒绝（L379-385，区分「watcher 因 stop 退出」与「命令已完成」）；`^C`/`\x03` 分支读至提示符（L392-433），回提示符则清 busy 与 pending（L401-406）并返回 `…[已中断: 正在运行的命令已被 ^C 终止]\n{prompt}`（L414-422）；未回提示符则保持 busy 并重启 watcher，返回 `[^C已发送但命令未终止，仍在后台运行。可稍后再试，或由用户按ESC中断]`（L424-433）；普通输入 `text + "\n"`（L436-438）并复用 `_pending_marker/_pending_pwd_marker` 等完成（L443-446，`expect_echo=False`）。
13. PS1 检测两级 + OSC 切分：`_ps1_candidate` 取尾部 4096（L752），先按 `\x1b]0;`…`\x07` 切出 PS1 本体（L753-759），再严格匹配（L763）或宽匹配（L765）。
14. 静默确认后发哨兵：读循环中连续 2 次 recv 超时即 `_send_sentinel`（L898-902）；watcher 阶段 1 同逻辑（L628-634）。
15. 哨兵存在性判定以「行首」为准（`_sentinel_line_present`，L1221-1234），避免匹配到回显行。
16. 后台 watcher（L596-699）：阶段 1 等 PS1→静默→发哨兵（L628-634、L641-648）；阶段 2 等 `pwd_marker`，仅扫尾部 `MARKER_TAIL_WINDOW`（L649-655）；阶段 2 若又见 PS1 则视为哨兵丢失并重发（L656-663）；`finally` 中按 `finished` 入 backlog（只入 `initial_output` 之后的新增部分，L666-695），更新 `_cwd`（L689-691），**无条件清 busy**（L696）。
17. 回显能力探测：`_update_cwd` 中若收到输出且不含 `pwd -P`，则 `_echo_enabled=False`（L571-576）。
18. `_clean_output` 清洗链：ANSI 剥离（L1283）→ 逐行 `\r` 覆盖合并（L1285-1306）→ 删除内部标记 `__NARNAT_(?:MARKER|CWD|PWD)_\d+__`（L1309）→ 3 个以上连续换行压成 2 个（L1314）→ 仅剥首尾空行与行尾空白（L1317，保留行首空格）。
19. 回显剥离（`_parse_output` L1119-1157）：含 `echo {marker}` 的行整行丢弃，但若与无换行输出粘连（含 `\x1b[?2004h` 或 OSC 起点）则保留其前缀真实输出（L1129-1143）；续行回显按 `\x1b[?2004h> ` 特征剥离（L1146-1147）；首行仅在与命令首行/输入文本**精确相等**时剥离一次（L1151-1154，注释说明为何不能用前缀匹配）。
20. 完成后的真实 PS1 行剥离：以 OSC `\x1b]0;` 为界截断（L1166-1173）+ `_strip_trailing_prompt`（L1174、L1247-1260）。
21. `_truncate_output`：`head = max_chars*2//3`（L57），切点由 `safe_cut_points` 吸附到标签边界（L58），中段提示含总字符数与 `estimate_text_tokens` 估算（L59-63）。
22. `close`：channel 立即关闭，transport 由 daemon 线程关闭以避免 Windows `closesocket` 不打断 recv 的 5 秒阻塞（L477-487）。
23. `reconnect`：锁内先判 `_channel.closed`（并发复用，L207-210）→ clear 中断标志（L213）→ 保存旧 cwd（L215）→ 停 watcher/join(1.0)（L219-225）→ 重置 busy/last_command/pending/sentinel/backlog/watcher 状态（L231-239）→ 重建通道（L242）→ `_initialize`（L243）→ 中断则 `_abort_interrupted()` 抛 `RuntimeError("重连被用户中断（ESC）")`（L246-247、L257-267）→ `cd {shlex.quote(old_cwd)} 2>/dev/null` 恢复目录（超时 10s，异常忽略，L251-255）。
24. `_initialize`：`_initial_output = self._read_until_prompt(timeout=5)` + `_update_cwd()`（L272-273）；`_read_until_prompt` 以 `re.search(r'[#$>]\s*$')` 判定就绪（L1211）。

**边界/异常行为**（编号；附行号）：

1. `_truncate_output` 的 `max_chars <= 0` → `error_line("max_output_chars需为正整数")`（L53-54，带不可伪造错误标签）。
2. `_read_until_marker` 的 `timeout <= 0` → `deadline = inf`（无限等待，兜底，L802-803）。
3. recv 返回空字节 → `conn_lost=True` 立即退出（L830-833）；socket 异常 → `conn_lost=True`（L903-906）；两者都不走超时分支（L1028-1045）。
4. exec 路径零输出且 `_echo_enabled=True` → 判「连接已中断」而非「超时仍在运行」（L1028-1038 注释与判据）。
5. `expect_echo=False`（input 路径）时零输出不判断连（L443-446、L787-791）。
6. 密码提示误报防护：输出以换行结尾 → 不判（L926-927）；1.5s 宽容期内哨兵到达 → 不计（L933-935）；注入后无新数据 → 同一旧提示跳过（L931-932）；`_sudo_mismatch` 后本会话永久停用自动注入（L129、L936、L949）。
7. 哨兵丢失自愈：阶段 2 再见 PS1 → 重发哨兵（L656-663）；ESC 路径补发哨兵（L998-1005）。
8. 哨兵出现后收尾读：最多再读 3.0 秒（L854），连续 3 次超时或见 `[#$>]\s*$` 结束（L809、L862-877）。
9. 解码一律 `decode("utf-8", errors="replace")`（L463、L505、L556、L626、L829、L859）：非 UTF-8 输出会变成替换字符（无编码探测）。
10. 中断优先级最高：读循环每次迭代先查 `_interrupt`（L824-826），数据路径与超时路径都查（L891、L856、L873）。
11. `_drain_stale_output` 临时把 channel timeout 降到 0.02 读最多 0.15s，2 次超时结束，finally 恢复原 timeout（L523-543）。
12. `_try_read_residual` 连续 5 次超时/空数据提前退出（L496-521）。
13. 首行回显匹配对 `input="G"` 这类短输入使用**全等**判定，避免真实输出 `GOT:G` 被误删（L1148-1154 注释）。
14. `_strip_echo(drop_echo_first_line=True)` 默认跳首行（超时路径），`_parse_output` 传 False（L1177）。
15. `cd` 恢复路径经 `shlex.quote`（L253）防注入/空格破坏；`old_cwd` 为 `~` 或 `/` 时跳过（L251）。
16. `time.time_ns()` 作为 marker 唯一性来源（L318-319），理论同纳秒冲突未做去重（未验证，实际不可观测）。
17. 提示符正则体系三套并存：`PS1_STRICT_RE`/`PS1_LOOSE_RE`（L107/L109）与 `_read_until_prompt` 内的 `[#$>]\s*$`（L1211）以及 `_read_until_marker` 内 drain 的 `[#$>]\s*$`（L851）。
18. 重连期间无法中断 TCP 连接建立，只能靠返回后的检查点放弃（L244-247 注释）。
19. `close` 不 join watcher（只 set stop 标志，L480），watcher 为 daemon 线程（L698）。

**补丁痕迹**（编号；附行号+证据；严重度分级）：

1. **（高）跨层直取私有成员**：`terminal/__init__.py` 直接访问 `session._channel`（L256、L348、L435、L576、L592、L642、L666、L728）与 `session._interrupt`/`session._busy`（L167、L177、L180）；文件传输直接访问 `session._client.open_sftp()`（`__init__.py` L780、L795、L851、L882、L918-919）；`remote.py:32` 同。证据：`_client`/`_channel` 在 L174/L197 赋值的私有属性被 5 个文件跨越访问 → 封装被打破，重构时无法单独替换 SSH 实现。
2. **（高）三层协议状态机耦合**：哨兵延迟注入（L321-332）+ PS1 静默确认（L596-663、L893-902）+ 哨兵丢失自愈（L656-663、L893-902）与 backlog/sudo 注入共享同一个读循环变量集合（`found`/`ps1_suspect`/`silent_timeouts`/`prompt_suspect_ts`/`sudo_injected`/`inject_attempts`/`inject_mark_len`，L804-821）。认知负荷高、路径组合多。
3. **（中）魔法值密集**：`PS1_CONFIRM_TIMEOUTS=2`（L113）、1.5s 宽容期（L935）、2048/4096/8192 三档窗口（L927、L945、L752、L846、L653）、`DRAIN_CONSECUTIVE_TIMEOUTS=3`（L809）、drain 3.0s（L854）、residual 3.0s（L987、L1001）、stale 0.02s/0.15s（L527-528）、尾部 4096 传入 watcher（L952、L971、L1054）。
4. **（中）吞异常**：`except Exception: pass` 于 L222-223、L228-229、L265-266、L483-484、L493-494、L540-541、L1041-1042（关闭/清理路径可接受，但 L265/L493 掩盖了通道异常，故障无从诊断）。
5. **（中）职责混杂**：单类同时承担传输 I/O、终端协议解析、输出清洗、sudo 注入、后台任务调度（watcher/backlog）、cwd 维护、重连、状态展示辅助。
6. **（中）重复代码**：PS1 候选 + 静默计时在 `_read_until_marker`（L881-902）与 `_start_busy_watcher._watch`（L618-663）两处近似复制；backlog 拼装文案 4 处重复（L338-344、L381-385、L409-413、L447-452）；`\r` 覆盖合并逻辑与 `serial_session._merge_cr_line` 重复实现。
7. **（中）脆弱边界**：RE_PASSWORD_PROMPT 只覆盖英文 sudo 文案与固定中文（L72-78）；`[sudo].*password` 对定制 PAM/本地化环境失效（未验证）。
8. **（中）历史包袱注释**：L322-331 描述"旧协议"缺陷、L70 "原模块级常量收敛于此"、L116 "仅扫描尾部8KB即可判定" 等修复历史直接留在代码内。
9. **（低）死参数/未用分支**：`_strip_echo` 的 `drop_echo_first_line` 只有一个调用点传 False（L1177），其余路径默认值即隐式行为；`_read_until_prompt` 返回的初始输出经 `_clean_output`（L1219）后又被 `__init__.py` L502-511 二次裁剪。
10. **（低）文案与实现耦合**：`[用户中断]`、`[超时: …]`、`[连接已中断…]` 等中文字面量散落 6 处（L1016、L1024、L1044、L1057-1058、L416、L428），是 UI/AI 契约但无集中定义。

**可测性**：

- 可独立单测（不建连、无需网络）：
  - `_ansi_sub`（L42）、`_truncate_output`（L47）、`_clean_output`（L1280）、`_strip_echo`（L1262）、`_strip_caret_echo`（L1236）、`_strip_trailing_prompt`（L1246）、`_sentinel_line_present`（L1221）、`_ps1_candidate`（L738）——纯字符串函数（静态方法，可用 `SSHSession.__new__` 或直接静态调用）。
  - `_parse_output`（L1076）、`_parse_partial_output`（L1181）、`_extract_cwd`（L715）——只依赖 `self._last_command` 与类常量，可用伪造实例（`object.__new__`+手工赋属性）驱动；`tool_exp/` 已有同类离线脚本（如 `test_ps1_offline.py:5`、`zz_check_truncate_regression.py:10`）。
- 需要集成测试（真实 SSH/PTY 或伪终端）：`_open_channel`、`_initialize`、`_update_cwd`、`execute` 全链路、`send_input`（含 ^C）、sudo 注入状态机、`_start_busy_watcher`、`reconnect`、`close`。
- 无法自动化（时序/终端交互）：PS1 与无换行输出粘连的精确时序、echo off 的 tty、设备休眠/唤醒、用户 ESC 与真实进程终止的竞态、真实 sudo 多级提示。

---

### narnat_agent/tools/terminal/__init__.py（972行）

**职责**：Terminal 工具的对外入口与全局会话注册表——工具定义（DEFINITION）、action 分发、devN 设备解析与展示、status/close/cleanup、断线重连，以及本机↔远程↔远程的文件传输（transfer）。

**对外接口**（public 类/函数/方法，逐个列签名）：

- `__all__ = ["execute", "DEFINITION", "get_session", "SSHSession", "kill_active_exec", "cleanup", "TerminalRuntime", "resolve_dev_display"]`（L24）

类 `class TerminalRuntime:`（L27）
- `RE_DELETE = re.compile(r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]|\bRemove-Item\b", re.IGNORECASE)`（L37-41）
- `RE_DEV = re.compile(r"^dev(\d+)$", re.IGNORECASE)`（L44）
- `RE_GIT = re.compile(r"\bgit\b", re.IGNORECASE)`（L47）
- `max_sessions = 5`（L49）
- `TRANSFER_BUFFER_SIZE = 65536`（L51）
- `sessions: dict = {}`（L54）、`sessions_lock = threading.Lock()`（L55）
- `active_exec_session = None`（L58）、`active_exec_lock = threading.Lock()`（L59）
- `@classmethod def set_max_sessions(cls, n: int) -> None`（L61-64）：`cls.max_sessions = max(1, min(n, 10))`

- `DEFINITION = {...}`（L66-155）：OpenAI function-calling 定义（name=Terminal，parameters 含 action/host/username/port/password/command/input/timeout/max_output_chars/source_host/source_path/target_host/target_path）。
- `def kill_active_exec()`（L158）：ESC 打断（活跃会话 + 所有 busy 会话发 Ctrl+C）。
- `def execute(action: str = "exec", host: str = "", username: str = "", port: int = 22, key_path: str = "", password: str = "", sudo_password: str = "", command: str = "", input: str = "", timeout: Optional[int] = None, session_id: int = -1, max_output_chars: int = 8000, source_host: str = "", source_path: str = "", target_host: str = "", target_path: str = "", _tool_context=None) -> str`（L189-207）
- `def _allocate_session_id() -> int`（L245）
- `def _normalize_device(host: str) -> str`（L263）
- `def _normalize_device_for_tools(device: str) -> Optional[str]`（L272）
- `def _file_tool_device_hint() -> str`（L283）
- `def _device_error(host: str) -> Optional[str]`（L293）
- `def _dev_label(sid: int) -> str`（L303）
- `def _list_devices_locked() -> str`（L308）
- `def _list_devices() -> str`（L314）
- `def _local_host() -> str`（L320）
- `def resolve_dev_display(dev: str) -> str`（L329）
- `def _dev_hint_locked() -> str`（L353）
- `def _resolve_session_id(session_id: int, host: str = "") -> tuple[int, "SSHSession"]`（L362）
- `def _connect(host: str, username: str, port: int = 22, key_path: str = "", password: str = "", sudo_password: str = "", session_id: int = -1, timeout: int = 15) -> str`（L415-419）
- `def _exec(session_id: int, host: str, command: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str`（L528）
- `def _input(session_id: int, host: str, input: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str`（L599）
- `def _status() -> str`（L659）
- `def _close(session_id: int, host: str) -> str`（L677）
- `def cleanup()`（L714）
- `def _try_reconnect(session) -> bool`（L722）
- `def get_session(session_id: int = -1, host: str = "") -> Optional["SSHSession"]`（L742）
- `def _format_size(size_bytes: int) -> str`（L755）
- `def _check_transfer_size(size_bytes: int, max_transfer_mb: int) -> Optional[str]`（L766）
- `def _get_remote_file_size(session: "SSHSession", path: str) -> Optional[tuple]`（L775）
- `def _ensure_remote_dir(session: "SSHSession", remote_path: str) -> bool`（L790）
- `def _ensure_local_dir(local_path: str) -> bool`（L820）
- `def _transfer_local_to_remote(source_path: str, target_host: str, target_path: str, max_transfer_mb: int) -> str`（L831）
- `def _transfer_remote_to_local(source_host: str, source_path: str, target_path: str, max_transfer_mb: int) -> str`（L862）
- `def _transfer_remote_to_remote(source_host: str, source_path: str, target_host: str, target_path: str, max_transfer_mb: int) -> str`（L893）
- `def _transfer(source_host: str, source_path: str, target_host: str, target_path: str, _tool_context=None) -> str`（L942）

**依赖**（本文件 import 的内部模块，附行号）：

- `from .ssh_session import SSHSession`（L21）
- `from ..exec_signal import error_line`（L22）
- 外部：`paramiko`（L19）；标准库 `os`(L12) `re`(L13) `stat`(L14) `sys`(L15) `threading`(L16) `typing.Optional`(L17)；函数内 `import socket`（L323）

**被依赖**（grep 复验）：

- `narnat_agent/assembly.py:26` `from .tools.terminal import TerminalRuntime`；调用 `TerminalRuntime.set_max_sessions(config.tools.max_sessions)`（L51）
- `narnat_agent/core/agent.py:178`（cleanup 导入）、`:181`（调用）、`:269`、`:272`
- `narnat_agent/core/tool_dispatcher.py:13`（`kill_active_exec as _kill_terminal_exec`）、`:14`（`resolve_dev_display as _dev_display`）；使用处 L139/140、L155/156、L178/179、L204/205、L286/287、L310/311、L428、L458、L465、L467、L469、L474
- `narnat_agent/ui/interrupt.py:39`（导入）、`:43`（ESC 时调用）
- `narnat_agent/tools/read/__init__.py:11`（`_normalize_device_for_tools, _file_tool_device_hint`）、`:136`、`:138`、`:141`
- `narnat_agent/tools/edit/__init__.py:16`、`:99`、`:101`、`:104`
- `narnat_agent/tools/write/__init__.py:10`、`:52`、`:54`、`:57`
- `narnat_agent/tools/registry.py:23`（`execute as terminal_execute, DEFINITION as TERMINAL_DEF`）、`:43`、`:59`、`:105`
- `narnat_agent/tools/terminal/remote.py:10`、`:20-21`、`:173-174`
- `tool_exp/` 脚本群（≥62 个文件，示例行号）：`verify_terminal_hints.py:6`、`verify_device_ref_spec.py:11`、`test_error_tag.py:92`、`probe_mechanism_risks.py:157`、`zz15_transfer_replay.py:12`、`zz_rollback_smoke.py:7`、`_obsolete_20260910/verify_ux_round4.py:30`

**状态**：

- 模块级全局：`DEFINITION`（L66，常量字典）；`__all__`（L24）。无其他模块级可变全局。
- 类变量（`TerminalRuntime`）：
  - `RE_DELETE`/`RE_DEV`/`RE_GIT`（L37/L44/L47）：常量。
  - `max_sessions`（L49）：**可变**；写 L64（`set_max_sessions`，由 `assembly.py:51` 调用）；读 L252、L400、L431、L663、L673、L705。
  - `TRANSFER_BUFFER_SIZE`（L51）：常量；读 L925。
  - `sessions`（L54）：**可变**；写 L258、L439、L481、L685（clear）、L693（del）、L708（del）、L719（clear）；读 L176、L253、L310、L316、L346-347、L373-375、L385-386、L389、L393、L400-402、L405-407、L411、L433-434、L445、L488、L664-665、L672、L682-684、L690-692、L705-707、L717。
  - `sessions_lock`（L55）/`active_exec_lock`（L59）：锁对象，全文件加锁访问 L174、L299、L316、L346、L370、L428、L472、L477、L480、L486、L581、L586、L647、L652、L661、L679、L716、L730、L738。
  - `active_exec_session`（L58）：**可变**；写 L473、L478、L582、L587、L731、L739；读 L165（`kill_active_exec`）。
- 实例状态：本模块不定义持久实例对象（只操作 `SSHSession` 实例，其状态见上一节）。

**行为要点**（编号；每条附行号）：

1. action 分发：`connect`/`exec`/`input`/`status`/`close`/`transfer`（L222-240）；未知 action → `error_line("未知action '…'，可选: connect/exec/input/status/close/transfer")`（L241-242）。
2. 数值参数强转（port/timeout/session_id/max_output_chars 转 int，失败 → `error_line("port/timeout/session_id/max_output_chars需为整数")`，L213-220）。
3. 默认超时：connect 默认 15 秒且被 `_tool_context.max_timeout_seconds` 上限约束（L225-228）；exec/input 默认 120 秒（L230、L233）；`max_timeout_seconds` 为 0 时表示不限制（由 `_exec`/`_input` 内的 `> 0` 判断保证，L535、L606）。
4. session_id 分配：遍历 `0..max_sessions-1`，遇 `None` 槽或 `_channel.closed` 死会话回收（`session.close()` + `del`），全满返回 -1（L245-260）。
5. devN 语义：`dev0`/空 → 本机（归一化为 `""`，L263-269）；`dev1..devn` → 终端 `N-1`（L303-305）；`_normalize_device_for_tools` 对非法标识返回 `None`（L272-280）。
6. `_resolve_session_id` 三条解析路径（L362-412）：显式 `session_id >= 0` 直接查（L372-375）；`host` 为 `devN`（L380-402，`dev0` 报错并引导用 Shell 工具，L397-398）或宽松匹配 IP / `user@IP`（唯一命中即用，多命中报候选清单，L382-395）；未指定时唯一会话自动选中、0 个报 "无活跃会话，请先connect"、多个报清单要求显式 dev（L404-412）。
7. `_connect` 流程：host+username 必填（L421-422）；timeout 非正回落 15（L424-426）；锁内分配/校验槽位（L428-446，显式 session_id 越界 → `session_id范围0-N`，已连接 → `[devN(终端N)已连接: user@host]`）；password 三合一判定（L449-458：形似路径且文件存在 → 当私钥用，否则当密码）；`sudo_password` 缺省取登录密码（L465-466）；建 `SSHSession`（L469）；初始化前把会话注册为 `active_exec_session` 以支持 ESC 打断 connect（L471-478）；锁内落库（L480-481）；同 IP+用户名重复连接提示（L486-501）；初始输出 >4 行时折叠为「首行 + 省略 N 行 + 末两行」（L502-512）；返回 `[已连接 devN(终端N): user@host]` 起头（L495）。
8. `_connect` 异常分类：`paramiko.AuthenticationException` → 附提示（未提供 password / 私钥路径不存在两种 hint，L515-521）；`paramiko.SSHException`（L522-523）；其他 `Exception`（L524-525）。
9. `_exec`：command 必填（L530-531）、timeout 正数（L532-533）、`min(timeout, max_timeout_seconds)`（L535-536）、删除/git 确认（L538-567）、解析会话（L569-572）、断线重连（L576-577）、注册 active_exec_session 后调用 `session.execute`（L581-587）、结果前缀 `[devN(终端N)] `（L589）；异常分支：通道已关且重连成功 → `[devN(终端N)]连接曾中断，已自动重连，请重试命令`（L592-595）；重连失败 → `…自动重连失败（设备可能未开机/网络不通）。设备恢复后重试将自动重连`（L577、L595）；其他 → `…命令执行失败: {e}`（L596）。
10. `_input`：与 `_exec` 同构的校验、删除/git 确认、重连与结果前缀（L599-656），调用 `session.send_input`（L650）。
11. 安全确认双平台机制：`sys.platform == "win32"` → `tc.confirm_callback(command)` 同步询问，取消返回 `[操作已取消: 此命令需用户确认]`（L547-550、L618-620）；其他平台 → 若 `tc._delete_confirmed` 置位则本次放行并复位（L554-555、L622-623），否则写 `tc.pending_delete = ("Terminal", {...})` 并返回字面量 `"__AWAIT_CONFIRM__"`（L557-567、L624-634）。
12. `_status` 输出：`"[SSH会话]"` + `dev0: 本机(当前设备)` + 每槽 `devN(终端N): user@host [活跃|已断开|忙|闲] 目录:cwd` 或 `devN(终端N): [未连接]`（L659-671）；无会话时追加 `(无已连接设备，最多支持N个并发终端)`（L672-673）。
13. `_close`：无 session_id 无 host → 全关并返回 `[已关闭N个会话]`（L680-686）；显式 session_id（L689-694）；`host=devN`（L696-709）；`dev0` → `[dev0是当前设备(本机)，无需关闭]`（L702-703）；未指定 → `error_line("close需要指定dev编号(如host=dev1)或session_id")`（L711）。
14. `cleanup`：锁内逐个 close 并清空字典（L714-719）。
15. `kill_active_exec`：活跃执行会话 set 中断标志并发 `\x03`（L164-171）；再遍历所有 busy 会话（排除已处理的）同样处理（L173-184）——使 ESC 能自愈超时后仍在跑的 busy 状态。
16. `_try_reconnect`：把会话注册为 active_exec_session 后调 `session.reconnect()`，异常返回 False，finally 复位（L722-739）。
17. `get_session`：解析失败或重连失败返回 None，否则返回会话（L742-750，SFTP/文件工具入口）。
18. transfer 路由与校验：source/target 归一化（L949-950）、`_device_error` 校验（L951-953）、源目标同为同一路径 → `源和目标相同，无需传输`（L955-956）、`max_transfer_mb` 取 `_tool_context.max_transfer_mb`（默认 100，L958-960）；四象限分派（L962-972）：双本机 → 报错引导用本地工具；本机→远程（L831）；远程→本机（L862）；远程→远程（L893）。
19. 传输实现细节：本机→远程 `sftp.put`（L851-855）；远程→本机先 `sftp.stat` 判目录再 `sftp.get`（L867-887）；远程→远程双 SFTP + 64KB 流式中转（**不落盘**，L916-935），中断返回已传输量（L936-937）。
20. 目录一律拒绝并给出手工打包指引（L832-833、L871-872、L906-907）。
21. 远程父目录自动创建（逐级 `mkdir`，L790-817：先 `stat`，`IOError` 时 `mkdir`，再 `stat` 校验）；本地父目录 `os.makedirs(exist_ok=True)`（L820-828）。
22. 传输结果文案：`[已传输: 本机:{src} → {target_host}:{path} ({size})]`（L859）、`[已传输: {source_host}:{src} → 本机:{path} ({size})]`（L890）、`[已传输: {source_host}:{src} → {target_host}:{path} ({size})]`（L939）；失败含已传输量（L937）。
23. 大小格式化四档 B/KB/MB/GB，均保留 1 位小数（L755-763）。
24. `status` 的展示字段是 `_cwd`（非完整 prompt），注释说明避免 user@host 重复噪音（L668）。
25. 文件传输**不做任何压缩**：全仓 grep `zstd|zstandard|compress` 在 `narnat_agent/` 内只命中「上下文压缩」相关模块（core/compressor.py 等），terminal 传输实现中零命中；三路传输均为原始字节流（`sftp.put`/`sftp.get`/64KB 手工转发，L853、L884、L924-929），大小上限由 `max_transfer_mb`（默认 100MB，0=不限制）通过 `_check_transfer_size` 在传输**前**校验（L766-772、L838-840、L874-876、L909-911）。

**边界/异常行为**（编号；附行号）：

1. `max_output_chars` 在本层只做 int 转换，不校验正数（L218）；传 0 会下传到 `ssh_session._truncate_output` → 整个结果变成 `error_line("max_output_chars需为正整数")`（ssh_session.py L53-54）。
2. `_check_transfer_size`：`max_transfer_mb <= 0` 表示不限制（L767-768）；超限文案 `文件大小 X 超过传输上限 Y`（L771）。
3. `_get_remote_file_size` 捕获所有异常返回 None（L786-787）→ 上层统一报「源文件不存在或无法访问」，**权限问题与文件不存在不可区分**（L869、L904）。
4. `_ensure_remote_dir` 相对路径不处理？——实现按 `strip("/").split("/")` 逐级拼接绝对路径（L798-800），整体异常返回 False（L816-817）；`parent` 为空或等于自身时直接返回 True（L791-793）。
5. `_get_remote_file_size` 对目录也返回 `(size, True)`（L775-783），让上层区分「不存在」与「是目录」（L776-777 注释）。
6. 平台分支：`sys.platform == "win32"` 仅用于删除确认（L547、L618）；transfer 的远程路径一律按 `/` 处理（L791、L847、L176），Windows 本机作为源/目标时用 `os.path` API（L821、L832-835、L837）。
7. `_local_host` 失败回退 `"localhost"`（L320-326）。
8. `RE_DELETE` 需要动词后跟空白或 `/`（L37-41），`RE_GIT` 出现 `git` 即命中（L47）——`git` 出现在注释/字符串中也会要求确认，而 `git_skip_confirm` 默认关闭时影响面较大（L543、L614）。
9. `_allocate_session_id` 回收死会话时调用 `session.close()`（L257），但 `_channel.closed` 为真的会话其后台 watcher 可能仍在运行（未验证）。
10. `_resolve_session_id` 中 `sid >= max_sessions` 与 `sid not in sessions` 合并判定（L400、L705）。
11. `resolve_dev_display`：`devN` 未连接时原样返回 `devN`（L350），非 devN 输入原样返回（L340-341）。
12. `_close` 的 host 路径只认 `devN`（L697-700），IP 引用会报设备标识指导（L700）。
13. `_connect` 的 `key_path` 参数存在但 DEFINITION 未暴露（L194 vs L100-108），AI 只能通过 `password` 传私钥路径（L449-458）。
14. `session_id` 参数同样未在 DEFINITION 中声明但被 `execute` 接受（L200、L210 注释「内部参数」）。
15. `_exec`/`_input` 的 `except Exception` 会吞掉会话内异常并转为错误字符串（L590-596、L655-656），`Raw error` 细节只保留 str(e)。

**补丁痕迹**（编号；附行号+证据；严重度分级）：

1. **（高）跨层访问 `SSHSession` 私有成员**：`_channel.closed`（L256、L348、L435、L576、L592、L642、L666、L728）、`_channel.send`（L169、L182）、`session._interrupt.set()`（L167、L180）、`_busy`（L177）、`session._client.open_sftp()`（L780、L795、L851、L882、L918-919）、`session._initialize()`（L475）、`session._initial_output`（L502、L504）。证据：`SSHSession` 的私有属性被 5 处以上直接读写 → 会话对象无法独立替换/测试。
2. **（高）职责混杂**：一个文件同时实现工具入口（`execute`）、全局注册表（`TerminalRuntime`）、设备引用解析（6 个函数）、安全确认、状态展示、文件传输三路实现与大小格式化。
3. **（中）魔法值与散落默认值**：`15`（L225、L424-426）、`120`（L230、L233、L528、L599）、`8000`（L201、L218、L528…）、`100`（L958）、横幅折叠阈值 `4`/`-3`/`[-2:]`（L505-508）。
4. **（中）重复代码/近似变体**：`_normalize_device`（L263-269）与 `_normalize_device_for_tools`（L272-280）几乎相同；`_list_devices`/`_list_devices_locked`（L308-317）；`_dev_hint_locked`（L353-359）与 `_file_tool_device_hint`（L283-291）文案近似；三个 `_transfer_*` 函数骨架重复；`_exec` 与 `_input` 的确认/异常处理重复（L528-596 vs 599-656）。
5. **（中）确认协议双轨**：本模块用字面量 `"__AWAIT_CONFIRM__"`（L567、L634），而 serial 侧导入常量 `AWAIT_CONFIRM`（tool_context.py:12；serial/__init__.py:16、337）——同一契约两处定义。
6. **（中）`active_exec_session` 单变量覆盖**：多会话并发 exec 时后者覆盖前者（L473、L582），ESC 只能打断最后一个；serial 侧已改为集合并注释说明该缺陷（serial/__init__.py:43-45）。
7. **（中）吞异常**：`_get_remote_file_size`（L786-787）、`_ensure_remote_dir`（L816-817）、`_local_host`（L325-326）、`_try_reconnect`（L735-736）。
8. **（低）历史包袱注释**：「原散落的模块级全局收敛于此」（L29）、「隐藏兼容参数」（L465）、「示例引用用纯devN…照抄会报错」（L485-486）。
9. **（低）`KEY_PATH` 参数半死**：`execute`/`_connect` 声明 `key_path`（L194、L416）但 DEFINITION 不暴露（L100-108），成为事实上的内部参数。
10. **（低）`_device_error` 仅用于 transfer**（L951），exec/input 走 `_resolve_session_id` 的另一套报错文案，同一类错误存在两套提示（L293-300 vs L395-402）。

**可测性**：

- 可独立单测（无网络、可注入假 sessions）：
  - `_normalize_device`（L263）、`_normalize_device_for_tools`（L272）、`_dev_label`（L303）、`_format_size`（L755）、`_check_transfer_size`（L766）、`_local_host`（L320）；
  - `_list_devices_locked`（L308）、`_dev_hint_locked`（L353）、`_file_tool_device_hint`（L283）、`resolve_dev_display`（L329）、`_resolve_session_id`（L362）、`_allocate_session_id`（L245）、`_status`（L659）、`_close`（L677）、`execute` 的参数校验与未知 action 分支——均可通过向 `TerminalRuntime.sessions` 注入伪造 session（实现 `_channel.closed`/`username`/`host`/`_cwd`/`_busy` 属性的 stub）驱动。
- 需要集成测试（真实 SSH 服务端 + SFTP）：`_connect`、`_exec`、`_input`、`_status` 端到端、`_try_reconnect`（需断线模拟）、transfer 三路、`_ensure_remote_dir`。
- 无法自动化：多会话并发下 `kill_active_exec` 的实际打断效果、真实网络黑洞下的 connect 超时表现、设备重启恢复时序。

---

### narnat_agent/tools/terminal/remote.py（308行）

**职责**：通过 SFTP 为 Read/Edit/Write 三个文件工具提供远程（devN）文件读取、写入、编辑与 diff 生成能力。

**对外接口**（public 类/函数/方法，逐个列签名）：

- `def _no_session_msg(host: str = "") -> str`（L14）：无会话时的错误文案（区分无会话/指定设备未连接）。
- `def _get_sftp(session: SSHSession)`（L30）：从会话取 SFTP 客户端。
- `def remote_read(file_path: str, offset: int = 0, limit: int = 2000, host: str = "") -> str`（L37-38）：远程读取（行号前缀 + 截断提示）。
- `def remote_write(file_path: str, content: str, host: str = "") -> tuple`（L135）：远程写入，返回 `(文本结果, 着色diff)`。
- `def remote_edit(file_path: str, old_string: str = "", new_string: str = "", replace_all: bool = False, host: str = "") -> tuple`（L206-208）：远程字符串替换。
- `def _remote_write_and_diff(old_content: str, new_content: str, file_path: str, session: SSHSession, count: int, host: str = "") -> tuple`（L270-272）：写回并生成 diff。
- `def _make_diff(old_content: str, new_content: str, file_path: str) -> str`（L296）：unified diff（无差异返回 `[无差异]`）。

**依赖**（本文件 import 的内部模块，附行号）：

- `from . import get_session, SSHSession`（L10）
- `from ..diff_utils import colorize_diff, describe_bytes_only_change`（L11）
- 函数内延迟导入：`from . import _list_devices`（L20）、`from ..read import _detect_text_encoding`（L77）、`from . import _ensure_remote_dir`（L173）、`import stat as _stat_mod`（L59、L142）
- 外部：`difflib`(L7) `errno`(L8)

**被依赖**（grep 复验）：

- `narnat_agent/tools/read/__init__.py:141` `from ..terminal.remote import remote_read`
- `narnat_agent/tools/write/__init__.py:57` `from ..terminal.remote import remote_write`
- `narnat_agent/tools/edit/__init__.py:104` `from ..terminal.remote import remote_edit`
- `tool_exp/verify_patch.py:7`、`tool_exp/verify_fixes.py:8`、`tool_exp/verify_tool_ux_fixes.py:11`、`tool_exp/verify_review_fixes.py:27`、`tool_exp/_obsolete_20260910/regression_test.py:50`、`tool_exp/_obsolete_20260910/verify_ux_round4.py`（`remote` 相关）

**状态**：

- 模块级全局：**无**（无模块级变量、无缓存）。
- 类变量：无（本文件不定义类）。
- 实例状态：无（全部函数无状态；SFTP 句柄为局部变量，在每个函数内 close）。

**行为要点**（编号；每条附行号）：

1. `remote_read` 参数门控：`limit <= 0` → `"[错误: limit需为正整数]"`（L44-45）；无会话 → `_no_session_msg`（L47-49、L14-27）。
2. SFTP 打开失败 → `[错误: 远程SFTP打开失败: {e}]`（L51-54）。
3. `stat` 判目录 → `[错误: 远程路径是目录: {path}，请用 Terminal exec 查看目录内容]`（L59-68）。
4. 以 `"rb"` 模式打开并按行读取（L58 注释解释：`"r"` 模式 readline 会按 UTF-8 强解码致非 UTF-8 文件抛错；L70）。
5. 二进制检测：仅读首块 8192 字节，含 `\x00` 即拒绝（L71-74）。
6. 编码探测：`_detect_text_encoding(head)`（L76-78）后按该编码解码（L97，`errors="replace"`）。
7. offset 语义：`start = max(offset - 1, 0) if offset > 0 else 0`（L81），流式跳行（L83-88，记录 `skipped`）。
8. 输出行格式：`f"  {line_num}→{content}"`（L98），与本地 Read 对齐。
9. 截断判定：`for...else` + 末尾再读一行（L92-102）；截断时追加 `"  ... [截断: 已显示 {limit} 行。使用 offset={start + limit + 1} 参数可读取其余部分]"`（L126-127）。
10. 空结果区分：offset 超出末尾 → `[无内容: offset={offset} 已超出文件末尾（文件共{skipped}行）]`；否则 `[文件为空]`（L119-123）。
11. 错误分类：`errno.ENOENT` → 不存在；`errno.EACCES` → 权限不足（含 **EACCES** 字样）；其他 → 通用失败（L103-110、L111-112）；`finally` 关闭 sftp（L113-117）。
12. `remote_write` 流程：无会话 → `(_no_session_msg(host), "")`（L137-139）；stat 判存在与目录（L144-154，目录直接返回错误并关 SFTP）；存在则读旧内容生成 diff（L160-168，异常静默）；新文件且路径为绝对路径时自动建父目录（L171-177）；`content.encode("utf-8")` 写入（L180-182）；关闭 SFTP（L183）。
13. `remote_write` 变更判定基于**字节比较**（L188-198）：`diff == "[无差异]"` 且字节全等 → 「新旧内容完全相同（字节级一致）」提示；字节有变化 → 「文件已写入，正文内容相同，但字节层面有变化（{detail}）」，detail 来自 `describe_bytes_only_change`（L196-198）。
14. `remote_write` 返回文案：有 diff 时 `{dev_tag}[已写入(远程): {file_path} ({byte_count}字节)]\n{diff}`（L199-200）；`dev_tag = f"[{host}] "`（L190，host 为空则无前缀）。
15. `remote_edit` 流程：无会话 → 错误（L210-212）；读原始字节（L214-218）；错误分类同 ENOENT/EACCES（L219-226）；**非 UTF-8 直接拒绝编辑**（L230-236，注释说明 `errors="replace"` 会造成永久数据损坏）。
16. `remote_edit` 换行归一化（L242-251）：文件含 CRLF 时把 old/new 的 `\n` 归一为 CRLF；否则把 `\r\n`/`\r` 归一为 `\n`。
17. `remote_edit` 匹配计数：0 处 → `[错误: 未找到匹配文本。请先Read确认远程文件内容。]`（L253-255）；多处且未 `replace_all` → `[错误: 找到{count}处匹配，old_string不唯一。…]`（L256-259）；替换 1 次或全部（L261-264）。
18. 写回与 diff：`_remote_write_and_diff`（L270-293）写回失败 → `[错误: 远程写入失败: {e}]`；`old_content == new_content` → 「新旧内容相同，文件无实质修改」提示 + `colorize_diff("[无差异]")`（L285-289）；否则 `{dev_tag}[已替换{count}处]\n{diff}`（L290）。
19. `_make_diff`：`difflib.unified_diff(..., fromfile=f"a/{basename}", tofile=f"b/{basename}", lineterm="")`（L301-306），basename 取路径最后一段（L300），空 diff → `[无差异]`（L308）。
20. `_no_session_msg` 必须以 `[错误` 开头（L17-18 注释：终端 UI 依据），区分「无任何会话」（提示先 connect）与「指定设备未连接 + 合法标识清单」（L22-25）。

**边界/异常行为**（编号；附行号）：

1. SFTP 的 `EACCES` 与 `ENOENT` 显式区分（L106-109、L222-225），注释说明「一律报不存在会把排查引向路径拼写」（L104-105）。
2. 非 UTF-8 文件编辑被硬拒（L232-236），防 U+FFFD 静默破坏。
3. 相对路径（不以 `/` 开头）不自动创建父目录（L171-172）。
4. 旧内容读取失败时 diff 为空字符串，但写入仍继续（L160-168、L199-201 分支）——写入结果提示无 diff。
5. 二进制检测只覆盖首 8KB：8KB 之后才出现 NUL 的文件会被当作文本读入（L71-74，**未验证**实际影响，属设计取舍）。
6. `remote_read` 的 `limit` 无上限保护：巨大 limit 会按行读到 EOF 并把所有行拼进返回串（L92-98）。
7. `offset` 为负数 → 从第 1 行开始（L81）。
8. `_get_sftp` 不处理异常：SFTP 打开失败由调用方 try/except 兜住（L30-32；remote_read L51-54；remote_write L185-186；remote_edit L227-228）。
9. `remote_edit` 的 `sftp.close()` 在正常路径显式调用（L218），异常路径由 `except` 分支返回（L219-228）—— 未在 finally 中关闭（L214-228 结构）。
10. `remote_write` 中目录判定后又 `sftp.close()`（L153），随后 `except` 分支不会重复关闭（L185-186）。
11. `_make_diff` 的 `basename` 对无 `/` 的路径直接用原字符串（L300）。
12. `colorize_diff` 仅用于 UI 展示字段（返回 tuple 第二个元素），AI 看到的是未着色 diff（L166/L198/L289/L292）。

**补丁痕迹**（编号；附行号+证据；严重度分级）：

1. **（高）跨层访问私有 `_client`**：`_get_sftp` 直接 `session._client.open_sftp()`（L30-32）——SFTP 能力未由 `SSHSession` 提供公开接口。
2. **（中）反向+延迟导入掩盖循环依赖**：`from . import get_session, SSHSession`（L10，顶层）与函数内 `from . import _list_devices`（L20）、`from . import _ensure_remote_dir`（L173）混用；`from ..read import _detect_text_encoding`（L77）在工具层内横向依赖另一个工具的私有函数。
3. **（中）吞异常**：`_no_session_msg` 的兜底 `except Exception`（L26-27）、diff 生成 `except Exception: pass`（L167-168）、`finally: sftp.close()` 的 `except Exception: pass`（L114-117）。
4. **（中）职责混杂**：读/写/编辑/diff/目录创建/错误分类同处一文件，且错误分类逻辑（ENOENT/EACCES）在 read 与 edit 各写一遍（L103-110 vs L219-226）。
5. **（中）魔法值**：`8192`（L72）、默认 `limit=2000`（L37）、`limit <= 0` 判定（L44）、diff 前缀 `a/` `b/`（L303-304）、`"[无差异]"` 文案在 L193/L289/L308 三处重复。
6. **（低）重复 import**：`import stat as _stat_mod` 在 `remote_read`（L59）与 `remote_write`（L142）内各一次。
7. **（低）`_remote_write_and_diff` 的 `colorize_diff("[无差异]")`**（L289）对纯文案着色，与 `remote_write` 的 `colorize_diff(f"[正文相同，字节变化] {detail}")`（L198）不成体系。
8. **（低）错误字符串不统一**：read 用「远程读取失败」（L110、L112），edit 用「远程读取失败」（L228）与「远程文件无法读取」（L226）两种措辞。

**可测性**：

- 可独立单测（无需真实服务器）：`_make_diff`（L296，纯函数）；`_no_session_msg` 可在注入假 `TerminalRuntime.sessions` 后单测（L14-27）。
- 需要集成测试（真实 SFTP 或 paramiko mock）：`remote_read`（offset/limit/二进制/编码/EACCES）、`remote_write`（目录、父目录创建、字节级变更判定）、`remote_edit`（换行归一化、唯一性、非 UTF-8 拒绝）、`_remote_write_and_diff`。
- 无法自动化：与本地 Read/Edit/Write 的行为「一致性」需要差分测试（同一输入分别跑本地与远程路径比对），当前无此类测试。

---

### narnat_agent/tools/serial/serial_session.py（549行）

**职责**：封装单个串口会话——后台 reader 线程持续读串口进共享 buffer，靠「提示符正则 + 稳定性采样」判定命令结束，提供 exec / raw_exec（纯超时，含纯监听）/ input（含 ^C）三种发送语义。

**对外接口**（public 类/函数/方法，逐个列签名）：

模块级函数：
- `def _merge_cr_line(line: str) -> str`（L20）：单行内 `\r` 覆盖合并。
- `def _truncate_output(text: str, max_chars: int) -> str`（L34）：头 2/3 + 尾 1/3 截断；max_chars<=0 返回错误串。

类 `class SerialSession:`（L53）

类常量：
- `MIN_EXEC_TIME = 0.5`（L60）、`STABILITY_SAMPLES = 3`（L61）、`STABILITY_INTERVAL = 0.1`（L62）
- `POLL_INITIAL = 0.05`（L63）、`POLL_MAX = 0.3`（L64）、`POLL_MULTIPLIER = 1.2`（L65）
- `ANSI_RE = re.compile(...)`（L70-75）
- `PROMPT_RE = re.compile(r"[\])$#%>:❯=@~]\s*$")`（L78）
- `READ_TIMEOUT = 0.1`（L81）
- `BUFFER_MAX_CHARS = 1_000_000`（L84）

公开方法/属性：
- `def __init__(self, port: str, baudrate: int = 115200, databits: int = 8, parity: str = "N", stopbits: float = 1, flow_control: str = "none", line_ending: str = "\n", prompt_pattern: str = "")`（L86-89）
- `@property def prompt_info(self) -> str`（L145-148）：`f"{self.port} @{self.baudrate}"`
- `@property def busy(self) -> bool`（L150-152）
- `@property def is_alive(self) -> bool`（L154-156）：`not self._dead and self._ser.is_open`
- `def execute(self, command: str, timeout: int = 120, max_output_chars: int = 8000) -> str`（L169-170）
- `def send_input(self, text: str, timeout: int = 120, max_output_chars: int = 8000) -> str`（L182-183）
- `def raw_execute(self, command: str, timeout: int = 120, max_output_chars: int = 8000) -> str`（L201-202）
- `def kill_active(self)`（L220）
- `def close(self)`（L232）
- 实例属性 `initial_output`（L141）：连接后初始输出（供上层展示）。

内部方法：
- `def _ensure_ready(self) -> Optional[str]`（L160）
- `def _reader_loop(self)`（L250）
- `def _drain_initial(self, max_wait: float = 10.0, stable_time: float = 0.5) -> str`（L271）
- `def _do_send(self, text: str, timeout: int, max_output_chars: int) -> str`（L309-310）
- `def _do_raw_send(self, text: str, timeout: int, max_output_chars: int) -> str`（L343-344）
- `def _send_ctrl_c(self, timeout: int, max_output_chars: int) -> str`（L393）
- `def _wait_for_prompt(self, timeout: int) -> tuple`（L421）
- `def _is_at_prompt(self, text: str) -> bool`（L457）
- `def _check_stability(self) -> bool`（L469）
- `@staticmethod def _clean_output(raw: str) -> str`（L485-486）
- `def _polish_output(self, raw: str, sent_text: str) -> str`（L496）
- `@staticmethod def _strip_command_echo(output: str, command: str) -> str`（L512-513）
- `def _dedup_prompt_lines(self, output: str) -> str`（L534）

**依赖**（本文件 import 的内部模块，附行号）：

- **无内部模块依赖**（不 import narnat_agent 内任何模块）。
- 外部：`serial`（pyserial，L17）；标准库 `codecs`(L10) `difflib`(L11) `re`(L12) `time`(L13) `threading`(L14) `typing.Optional`(L15)

**被依赖**（grep 复验）：

- `narnat_agent/tools/serial/__init__.py:15` `from .serial_session import SerialSession`（使用处 L290）
- 间接：`tools/registry.py:27`（Serial.execute）、`core/tool_dispatcher.py:15`、`ui/interrupt.py:40`、`core/agent.py:179/270`（上游模块只依赖 serial 包，不直接依赖本文件）
- `tool_exp/` 脚本群：`tool_exp/test_esc_interrupt_regression.py:311`（`import narnat_agent.tools.serial as serial_mod`）

**状态**：

- 模块级全局：**无**。
- 类变量：全部为不可变常量（L60-L84）。
- 实例状态：
  - `port`（L90）、`baudrate`（L91）、`line_ending`（L92）：只读；外部读 `port`（serial/__init__.py L262、L504、L579）与 `baudrate`（经 `prompt_info`）。
  - `_prompt_re`（L95-100）：构造后按 `prompt_pattern` 覆盖为自定义正则。
  - `_ser`（pyserial.Serial，L111-121）：外部读 `is_open`（L156、L162）；`open()`（L121）可能抛异常（由上层 catch）。
  - `_busy`（L123）：写 L176、L194、L214；读 L165、L151；外部读 serial/__init__.py L477。
  - `_interrupt`（threading.Event，L124）：写（set）L222、L235；写（clear）L312、L349、L399；读 L282、L336、L369、L378、L434。
  - `_dead`（L125）：写 L164（`_ensure_ready`）、L268（reader 异常）、L324、L360、L409（写失败）；读 L156、L162。
  - `_buffer`（L128）：写 L258、L288、L306、L316、L352、L376、L403；读 L260-264、L286、L305、L438、L444、L454、L476-479。
  - `_lock`（L129）/`_cond`（L130）：Condition 复用同一锁；`notify_all` 在 L266；`wait` 在 L291、L301、L373、L448、L478。
  - `_reader_alive`（L131）：写 L234；读 L252。
  - `_decoder`（L134）：增量 UTF-8 解码器（`codecs.getincrementaldecoder("utf-8")("replace")`）。
  - `_reader`（L137-138）：daemon 线程。
  - `initial_output`（L141）：只读对外。

**行为要点**（编号；每条附行号）：

1. 构造流程：`prompt_pattern` 非法 → `raise ValueError(f"无效的 prompt_pattern 正则: {e}")`（L96-100）；databits/parity/stopbits 经映射表 `.get(..., default)` 兜底（L103-116）；`flow_control == "software"` → `xonxoff=True`，`== "hardware"` → `rtscts=True`（L117-118）；`timeout=READ_TIMEOUT`（L119）；`self._ser.open()`（L121）；启动 reader 线程（L137-138）；`initial_output = self._drain_initial()`（L141）。
2. reader 线程：`read(4096)` → 增量解码 → 追加 `_buffer` → `notify_all`（L250-266）；`len(_buffer) > BUFFER_MAX_CHARS` 时保留末 `50%` 并前置 `"...[背压截断: 丢弃前N字符]\n"`（L260-265）；任何异常 → `_dead=True` 并退出（L267-269）。
3. `execute` 门控：`_ensure_ready` 返回 `[错误: 串口 {port} 已断开]`（并置 `_dead=True`，L162-164）或 `[上一个命令尚未完成，此串口暂不可用]`（L165-166）；执行期间 `_busy` 由 try/finally 保证复位（L176-180）。
4. `_do_send` 步骤：clear 中断标志（L312）→ 清空 buffer（L315-316）→ 写 `text + line_ending`（`errors="replace"`）并 flush（L319-322）→ 写失败 → `_dead=True` + `[错误: 串口写入失败: {e}]`（L323-325）→ 等提示符（L328）。
5. `_do_send` 结果：命中提示符 → `_truncate_output(self._polish_output(output, text), max_output_chars)`（L330-333）；未命中 → tag 为 `[用户中断]` 或 `[超时: 命令执行超过{timeout}秒]`（L336-337），有输出时 `{cleaned}\n{tag}`、无输出时仅 tag（L338-341）。
6. `_do_raw_send`：`text` 为空 → 跳过发送（纯监听，L354-361 条件）；等待循环为 `min(0.1, remaining)` 步进的纯超时（L364-373）；tag 三态（L378-389）：有 text 时 `[用户中断]`/`[超时: …]`；空 text 时 `[用户中断]`、`[监听结束: 已达{timeout}秒]`（有输出）、`[监听结束: {timeout}秒内无输出]`（无输出）。
7. `send_input`：`^C`/`\x03` → `_send_ctrl_c`（L190-198）；其他文本 → 直接 `execute(text, timeout, max_output_chars)`（L199，即空闲时等同执行命令——DEFINITION 文案与此一致，见 serial/__init__.py L120-124）。
8. `_send_ctrl_c`：清中断标志与 buffer（L399-403）→ 写 `b"\x03"`（L406）→ 等提示符；命中 → 清洗输出（L413-414）；未命中 → 有输出附 `[已发送Ctrl+C]`，无输出仅 `[已发送Ctrl+C]`（L416-419）。
9. `_wait_for_prompt`：轮询检测「`elapsed >= MIN_EXEC_TIME` 且 `_is_at_prompt`」→ 再 `_check_stability()`（L441-445）；poll 间隔自适应增长并以 `POLL_MAX` 封顶（L451）；超时或中断返回 `(buffer, False)`（L453-455）。
10. `_is_at_prompt`：剥 ANSI → 取最后一行 → `rstrip()` → `_merge_cr_line` 合并 `\r` 覆盖 → `PROMPT_RE.search`（L457-467）。
11. `_check_stability`：在 Cond 内连续比较 `STABILITY_SAMPLES-1` 次（间隔 0.1s）buffer 不变即稳定（L469-483，注释说明全程持锁避免 notify 丢失）。
12. 默认提示符字符集 `[\\])$#%>:❯=@~]`（L78），自定义 `prompt_pattern` 完全替换（L95-98）。
13. `_clean_output`：ANSI 剥离（L488）→ **先** `\r\n` → `\n` 归一化（L490，注释区分 CRLF 与覆盖）→ 逐行 `\r` 合并（L491-492）→ 3 连换行压缩（L493）→ `strip()`（L494）。
14. `_polish_output`：`_clean_output` → `_strip_command_echo` → `_dedup_prompt_lines`（L505-510）。
15. `_strip_command_echo`：用 `difflib.SequenceMatcher(None, first, cmd).ratio() >= 0.6` 或（`len(cmd) > 20` 且 `first.startswith(cmd[:20])`）判定为回显并剥掉首行（L525-531）；不相似则原样返回（L532）。
16. `_dedup_prompt_lines`：仅当「相邻两行 rstrip 后相等」且「后者匹配 `_prompt_re`」时去重（L542-548）。
17. `_drain_initial`：收集到数据则持续等待，若 `collected` 非空且静默达 `stable_time`（0.5s）即返回；上限 `max_wait=10s`；中断可提前退出（L271-307）；返回前 `_clean_output`（L307）。
18. `kill_active`：set `_interrupt` + `notify_all` + 写 `b"\x03"` 并 flush（L220-230）。
19. `close`：`_reader_alive=False`、set `_interrupt`、`notify_all`、`_ser.close()`、`_reader.join(timeout=1.0)`（L232-246）。
20. `_truncate_output`：`max_chars <= 0` → `"[错误: max_output_chars需为正整数]"`（L36-37）；保留头 `max_chars*2//3`、尾 `max_chars-head`（L40-46）。

**边界/异常行为**（编号；附行号）：

1. 非法串口参数一律静默回落默认值：databits 非 5/6/7/8 → `EIGHTBITS`；parity 非 N/E/O/M/S → `PARITY_NONE`；stopbits 非 1/1.5/2 → `STOPBITS_ONE`（L114-116）——**无任何提示**。
2. `flow_control` 仅识别 `"software"`/`"hardware"`，其他值（含 `"none"`/非法值）→ 两者均为 False（L117-118）。
3. `line_ending` 的转义字符串还原与非法值回落发生在**上层**（serial/__init__.py L247-251），会话层直接使用（L320）。
4. 超时后设备命令仍在运行，但会话层**不保留任何后台收敛机制**（无 watcher/backlog），`_busy` 在 finally 中直接复位（L179-180）——与 SSH 侧行为不一致（SSH 超时后置忙并后台收敛，ssh_session.py L1052-1055、L596-699）。
5. 预置残留数据被丢弃：`_do_send`/`_do_raw_send`/`_send_ctrl_c` 开头都清空 `_buffer`（L315-316、L351-352、L402-403），因此「上一条命令的迟到输出」不会呈现给 AI。
6. buffer 背压截断是**静默数据丢失**（仅有一条前缀标注，L260-265），超长刷屏设备会丢最早一半。
7. reader 线程异常 → `_dead=True`（L267-269），此后所有操作返回 `[错误: 串口 {port} 已断开]`（L162-164）；上层还会 pop 会话槽（serial/__init__.py L361-364、L407-410、L446-449）。
8. 编码固定 UTF-8（读 `errors="replace"` L134/L256；写 `errors="replace"` L321/L357），非 UTF-8 设备（GBK 等）输出会变替换字符。
9. 提示符误判风险与缓解：`MIN_EXEC_TIME=0.5s` + 3×100ms 稳定性采样（L60-62、L441-445、L469-483）；`PROMPT_RE` 含 `:`/`=`/`@`/`~` 等宽松字符，若命令输出尾部出现此类字符仍可能命中（**未验证**真机误判率）。
10. `send_input` 的 `^C` 分支在会话空闲时也**允许**发送（与 Terminal 的 input 空闲拒绝语义不同，比较 ssh_session.py L360-367）。
11. `_drain_initial` 的退出条件是「静默达 stable_time（0.5s）」或「总时长达到 max_wait（10s）」：持续有数据时总时长由 `while time.time() < deadline` 限界（L279-281），数据到来只刷新 `last_data_time`（L289）。
12. `close` 幂等性：重复调用只重复 `_ser.close()`（异常被吞，L238-241）与 join（L242-246，`RuntimeError` 也被吞）。
13. `prompt_info` 文案 `f"{self.port} @{self.baudrate}"`（L148）中 `port` 与 `@` 之间有空格（展示细节，无功能影响）。
14. `_ensure_ready` 的 `_busy` 提示不带「可用 input 中断」等指导（L166），与 Terminal 的 busy 文案（ssh_session.py L309-310）不一致。

**补丁痕迹**（编号；附行号+证据；严重度分级）：

1. **（高）与 SSH 侧语义不对称**：SSH 侧有「超时后 busy + watcher + backlog」闭环（ssh_session.py L596-699、L1052-1055），Serial 只有「超时即返回、状态复位」（L336-341、L179-180）；而 `send_input` 的文档字符串自称「与 Terminal 的 input 对齐」（L186-189），实现却完全不同（空闲也发送）。这会让 AI 在两套工具上形成不一致预期。
2. **（中）魔法值密集**：0.5/3/0.1/0.05/0.3/1.2（L60-65）、`READ_TIMEOUT=0.1`（L81）、`1_000_000`（L84）、等待步长 `0.1`（L291、L298、L373）、相似度 `0.6` 与前缀 `20`（L527-528）。
3. **（中）跨线程共享可变状态缺少统一封装**：`_dead` 由 reader 线程（L268）与调用线程（L164、L324、L360、L409）双向写，未加锁；`_buffer` 在部分位置持 `_lock`（L376、L403）部分位置持 `_cond`（同锁）——**混用两把引用同一 Lock 的名称**，可读性差且易误用（L128-130）。
4. **（中）吞异常**：`close` 的 `except Exception: pass`（L238-241）与 `join` 的 `except RuntimeError: pass`（L243-246）；reader 的宽泛 `except Exception → _dead`（L267-269）丢失断因（无法区分拔线/权限/驱动错误）。
5. **（中）职责混杂**：I/O 线程、协议检测、输出清洗、提示符去重、稳定性采样、三套发送语义同处一类。
6. **（中）重复代码**：`_do_send`/`_do_raw_send`/`_send_ctrl_c` 三处共享「清标志 → 清 buffer → 写 → 等」骨架（L309-341、L343-391、L393-419）；`_truncate_output` 与 `ssh_session._truncate_output` 功能重复实现（L34-46 vs ssh_session.py L47-64），仅错误串与标签吸附不同。
7. **（低）字符串字面量契约散落**：`[错误: 串口 X 已断开]`（L164）、`[上一个命令尚未完成…]`（L166）、`[已发送Ctrl+C]`（L418-419）、`[监听结束: …]`（L387-389）等中文文案直写函数体内，无集中定义。
8. **（低）注释与实现漂移**：文件头声称「超时兜底: 默认 120s, 超时返回已收集数据」（L6），但 120 的默认值实际由上层 `serial/__init__.py` L177 传入。
9. **（低）语义重复的公有面**：`busy` 属性（L150-152）与 `_ensure_ready` 内部的 `_busy` 判断（L165-166）表达同一事实，但提示文案不同（`[上一个命令尚未完成，此串口暂不可用]`，L166）。

**可测性**：

- 可独立单测（无硬件）：
  - `_merge_cr_line`（L20）、`_truncate_output`（L34）、`_clean_output`（L485）、`_strip_command_echo`（L512）、`_dedup_prompt_lines`（L534）、`_polish_output`（L496）、`_is_at_prompt`（L457）——纯函数或仅依赖 `_prompt_re`，可用 `object.__new__(SerialSession)` + 手工赋 `_prompt_re` 驱动。
  - `_check_stability`（L469）与 `_wait_for_prompt`（L421）可在**不打开串口**的前提下，用注入的 `_cond`/`_buffer` 伪造 reader 行为做单测（当前无此类测试）。
- 需要集成测试（真实串口或虚拟串口对，如 com0com/socat pty）：`__init__`/`_drain_initial`、`execute`、`raw_execute`、`send_input`、`_reader_loop`、背压截断、`kill_active`、`close`。
- 无法自动化：真机时序（boot 日志、AT 固件、刷屏日志）、^C 在设备侧的实际效果、稳定性采样在真实噪声下的判定质量。

---

### narnat_agent/tools/serial/__init__.py（599行）

**职责**：Serial 工具的对外入口与串口会话注册表——工具定义、7 种 action 分发（scan/connect/exec/raw_exec/input/status/close）、session_id 分配与端口匹配、ESC 打断、删除命令安全确认。

**对外接口**（public 类/函数/方法，逐个列签名）：

- `__all__ = ["execute", "DEFINITION", "kill_active_exec", "cleanup", "SerialRuntime"]`（L18）

类 `class SerialRuntime:`（L21）
- `max_sessions = 5`（L28）：**硬编码，无外部写入方**（见补丁痕迹第 1 条）。
- `RE_DELETE = re.compile(r"\b(?:rm|del|rd|rmdir|erase|format)\b[\s/]|\bRemove-Item\b", re.IGNORECASE)`（L33-37）
- `sessions: dict = {}`（L40）、`sessions_lock = threading.Lock()`（L41）
- `active_exec_sids: set = set()`（L45）、`active_exec_lock = threading.Lock()`（L46）
- `@classmethod def set_max_sessions(cls, n: int) -> None`（L48-51）：`cls.max_sessions = max(1, min(n, 10))`（**当前无调用方**）

- `DEFINITION = {...}`（L54-149）：OpenAI function-calling 定义（name=Serial，parameters 含 action/port/baudrate/databits/parity/stopbits/flow_control/line_ending/prompt_pattern/command/input/timeout/session_id/max_output_chars）。
- `def kill_active_exec()`（L152）：对所有活跃 sid 调 `session.kill_active()`。
- `def execute(action: str = "exec", port: str = "", baudrate: int = 115200, databits: int = 8, parity: str = "N", stopbits: float = 1, flow_control: str = "none", line_ending: str = "\n", prompt_pattern: str = "", command: str = "", input: str = "", timeout: int = 120, session_id: int = -1, max_output_chars: int = 8000, _tool_context=None) -> str`（L165-181）
- `def _scan() -> str`（L214）
- `def _connect(port: str, baudrate: int = 115200, databits: int = 8, parity: str = "N", stopbits: float = 1, flow_control: str = "none", line_ending: str = "\n", prompt_pattern: str = "", session_id: int = -1) -> str`（L239-242）
- `def _check_delete_safety(command: str, session_id: int, port: str, timeout: int, max_output_chars: int, action_name: str, _tool_context) -> Optional[str]`（L312-314）
- `def _exec(session_id: int, port: str, command: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str`（L340-341）
- `def _raw_exec(session_id: int, port: str, command: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str`（L379-380）
- `def _input(session_id: int, port: str, text: str, timeout: int = 120, max_output_chars: int = 8000, _tool_context=None) -> str`（L425-426）
- `def _status() -> str`（L464）
- `def _close(session_id: int, port: str = "") -> str`（L485）
- `def cleanup()`（L526）
- `def _allocate_session_id() -> int`（L537）
- `def _resolve_session_id(session_id: int, port: str = "") -> tuple[int, "SerialSession"]`（L556）

**依赖**（本文件 import 的内部模块，附行号）：

- `from .serial_session import SerialSession`（L15）
- `from ..tool_context import AWAIT_CONFIRM`（L16）
- 外部：标准库 `re`(L10) `sys`(L11) `threading`(L12) `typing.Optional`(L13)；函数内 `from serial.tools.list_ports import comports`（L217）

**被依赖**（grep 复验）：

- `narnat_agent/core/agent.py:179`（cleanup 导入）、`:182`（调用）、`:270`、`:273`
- `narnat_agent/core/tool_dispatcher.py:15`（`kill_active_exec as _kill_serial_exec`）；使用处 L140、L156、L179、L205、L287、L311
- `narnat_agent/ui/interrupt.py:40`（导入）、`:44`（ESC 时调用）
- `narnat_agent/tools/registry.py:27`（`execute as serial_execute, DEFINITION as SERIAL_DEF`）、`:47`、`:60`、`:105`
- `tool_exp/audit_def_cost.py:8`、`tool_exp/audit_params_surface.py:8`、`tool_exp/test_esc_interrupt_regression.py:311`、`tool_exp/_obsolete_20260910/regression_test.py:155`
- **注意**：`narnat_agent/assembly.py` **不** import 本模块（只 import terminal，assembly.py:26、51）→ 见补丁痕迹第 1 条。

**状态**：

- 模块级全局：`DEFINITION`（L54，常量）；`__all__`（L18）。
- 类变量（`SerialRuntime`）：
  - `max_sessions`（L28）：**可变但无写入方**（`set_max_sessions` L48-51 全仓无调用）；读 L268、L269、L283、L468、L479、L544。
  - `RE_DELETE`（L33）：常量；读 L316。
  - `sessions`（L40）：**可变**；写 L277（del）、L286（=None 占位）、L299（del）、L304（=session）、L363（pop）、L409（pop）、L448（pop）、L493（clear）、L516（del）、L520（del）、L534（clear）、L551（del）；读 L259、L270-271、L282、L298、L467、L471-472、L479、L490、L502、L511、L514、L531、L545-547、L567、L569、L577、L588。
  - `sessions_lock`（L41）/`active_exec_lock`（L46）：锁；加锁处 L154、L257、L297、303、L362、L367、L372、L408、L413、L418、L447、L452、L457、L466、L487、L494、L521、L528、L530、L565。
  - `active_exec_sids`（L45）：**可变**；写 L368（add）、L373（discard）、L414、L419、L453、L458、L495（clear）、L522（discard）、L529（clear）；读 L155。
- 实例状态：本模块不持有持久实例状态（操作 `SerialSession`，见上一节）；`sessions` 中允许出现 `None` 占位（连接中，L286；读方需判空 L260、L473、L503-505、L515、L548-549、L578、L588）。

**行为要点**（编号；每条附行号）：

1. 参数强转：baudrate/databits/stopbits/timeout/session_id/max_output_chars 转数值，失败 → `"[错误: baudrate/databits/stopbits/timeout/session_id/max_output_chars需为数值]"`（L183-192）。
2. action 分发 7 种（L194-208）；未知 action → `[错误: 未知action '…'，可选: scan/connect/exec/raw_exec/input/status/close]`（L209）。
3. `scan`：`comports()` 列举；`ImportError` → "无法导入 pyserial"（L219-220）；其他异常 → "扫描串口失败: {e}"（L221-222）；空 → `[未检测到串口设备]`（L223-224）；每条格式 `  {device}  — {desc}  [{hwid}]`，`description` 为 "n/a" 或等于设备名时省略，`hwid` 为 "n/a" 时省略（L226-236）。
4. `connect` 参数规范化：`line_ending` 先做转义还原映射 `{"\\n": "\n", "\\r\\n": "\r\n", "\\r": "\r"}`，非法值回落 `"\n"`（L247-251）；Windows 下端口名转大写做键（L254）。
5. 同端口防重复：锁内遍历现有会话，端口相同且 `is_alive` → `[错误: {port} 已被终端{sid}占用，请先 close 终端{sid}]`（L257-264）。
6. session_id 分配：显式 `>= 0` 时校验范围（`session_id >= max_sessions` → `[错误: session_id 范围 0-{N-1}]`，L267-269）、已连接且存活 → `[串口终端{sid}已连接: {prompt_info}]`（L270-273）、已死则 close 并删除（L274-277）；自动分配走 `_allocate_session_id`（L279-283，满 → `[错误: 已达最大会话数(N)，当前终端: [...]]`）。
7. 三阶段连接（避免持锁 open）：锁内预留 `None` 槽（L285-286）→ 锁外构造 `SerialSession`（L288-294）→ 失败回收占位槽并返回 `[错误: 无法打开串口 {port}: {e}]`（L295-300）→ 锁内落库（L302-304）→ 返回 `[已连接终端{alloc_id}: {session.prompt_info}]` + `initial_output`（L306-309）。
8. `exec`：command 必填（L343-344）、timeout 正数（L345-346）、`min(timeout, max_timeout_seconds)`（L348-349）、删除安全确认（L351-354）、解析会话（L356-359）、`is_alive` 失败则 pop 槽位并返回 `[错误: 终端{sid}串口已断开，请重新 connect]`（L361-364）、注册 `active_exec_sids` 后执行（L366-373）、结果前缀 `[终端{sid}] `（L374）。
9. `raw_exec`：timeout 校验（L389-390）、**command 为空时跳过安全检查**（纯监听，L395-400）、其余同 `exec`（L402-422）。
10. `input`：text 必填（L428-429）、timeout 校验（L430-431）、对 **input 文本本身**也做删除安全确认（L436-439，防止绕过）、其余同 `exec`（L441-461）。
11. `_check_delete_safety` 语义（L312-337）：仅当 `_tool_context and not rm_skip_confirm and RE_DELETE.search(command)` 才进入确认；`sys.platform == "win32"` → `confirm_callback` 判定，取消返回 `[操作已取消: 此命令需用户确认]`（L319-322）；非 win32 → `_delete_confirmed` 放行并复位（L325-327），否则写 `pending_delete = ("Serial", {action, session_id, port, command, timeout, max_output_chars})` 并返回 `AWAIT_CONFIRM` 常量（L329-337）；返回 None 表示放行。
12. **Serial 不做 git 命令确认**（与 Terminal 不同，Terminal 有 `RE_GIT` 判定：terminal/__init__.py L543、L614）。
13. `status`：无会话 → `[无活跃串口会话，最多支持{N}个并发终端]`（L467-468）；否则 `"[串口会话]"` + 每槽 `终端{sid}: {prompt_info} [活跃|已断开|忙|闲]`（L476-478），连接中占位（session is None）→ `终端{sid}: [连接中...]`（L473-475）；空闲槽数 >0 时追加 `[{free}个空闲]`（L479-481）。
14. `close`：无 session_id 且无 port → 关全部（跳过 None）+ 清 `active_exec_sids`，返回 `[已关闭{count}个串口会话]`（L488-496）；仅 port → 大小写不敏感匹配（L498-509）；显式 session_id → 未连接提示 `[终端{sid}未连接]`（L511-512）；占位 None → 删除并返回 `[终端{sid}连接中，已取消]`（L515-517）；正常关闭 → `[已关闭终端{sid}]`（L519-523）。
15. `cleanup`：先清 `active_exec_sids`（L528-529），再关闭所有非 None 会话并清空字典（L530-534）。
16. `_allocate_session_id`：遍历 `0..max_sessions-1`，跳过不存在的键，**不抢占 None 占位槽**，回收 `not is_alive` 的死会话（close + del），全满返回 -1（L537-553）。
17. `_resolve_session_id` 三条路径（L556-599）：显式 `session_id >= 0`（L566-572，未连接 → `终端{sid}未连接，请先 connect`，占位 → `终端{sid}正在连接中，请稍候`）；`port` 匹配（L574-585，Windows 大写不敏感；未匹配 → `端口 {port} 未连接，请先 connect 或 status 查看已连接的串口`）；自动选择（L587-599，唯一则用、0 个 → "无活跃会话，请先 connect"、多个 → 列出 `终端{k}: {prompt_info}` 并提示指定 session_id 或 port）。
18. `kill_active_exec`：快照 `active_exec_sids`（L154-155）后逐 sid 取会话并 `session.kill_active()`（L156-160）——集合语义支持多会话并发打断。

**边界/异常行为**（编号；附行号）：

1. `timeout <= 0` → `[错误: timeout 需为正整数（秒）]`（L345-346、L389-390、L430-431）。
2. `exec` 空 command 报错，但 `raw_exec` 空 command 是合法「纯监听」（L343-344 vs L386-388、L396）。
3. `session_id` 越界（>= max_sessions）→ 范围提示（L268-269）；负数 → 视为「自动分配」，不报错（L279）。
4. 已达上限 → `[错误: 已达最大会话数(N)，当前终端: [...]]`（L281-283）。
5. 端口占用检查只在旧会话 `is_alive` 时触发（L263）——僵尸会话可被新连接复用同端口。
6. `max_timeout_seconds` 为 0 表示不限制（`> 0` 才截断，L348、L392、L433）。
7. `max_output_chars <= 0` 在本层不校验，下传到会话层得到 `"[错误: max_output_chars需为正整数]"`（serial_session.py L36-37）；由于 Serial **不在** UI 的 `tagged_judge` 集合（tool_dispatcher.py L242），UI 会按 `[错误` 前缀判定失败显示（tool_dispatcher.py L265-270）。
8. 平台分支 `sys.platform == "win32"` 共 7 处：端口名归一（L254、L262）、确认交互（L319）、close 端口匹配（L500、L504）、resolve 端口匹配（L575、L579）。
9. 会话为 `None` 占位时，`status`/`close`/`_resolve_session_id`/`_allocate_session_id` 都需判空（L473、L515、L570、L548）——已处理，但每个读 `sessions` 的位置都必须记得判空，属脆弱约定（例如 L259-263 的循环对 None 有显式 continue，L502-505 的列表推导有 `s is not None`）。
10. `_check_delete_safety` 在 `_tool_context` 为 None 时直接放行（L316 的条件短路）——headless/无上下文场景不会拦截删除命令。
11. `AWAIT_CONFIRM` 返回值由 agent 主循环识别（core/agent_loop.py L239-254、L411-451），Terminal 侧用同值字符串字面量（terminal/__init__.py L567、L634）。
12. 背压/截断提示由会话层生成（serial_session.py L263、L44），本层不二次加工。
13. `_scan` 的 `p.description or "(无描述)"` 与 `p.hwid or ""` 处理字段缺失（L228-229）。

**补丁痕迹**（编号；附行号+证据；严重度分级）：

1. **（高）配置未接线**：`SerialRuntime.max_sessions` 硬编码 5（L28），`set_max_sessions`（L48-51）全仓无调用方；`assembly.py:51` 只调用 `TerminalRuntime.set_max_sessions(config.tools.max_sessions)`（assembly.py:26、51），而配置项在 `config/loader.py:71`（`max_sessions: int = 5`，「SSH最大会话数」）。证据：grep `set_max_sessions` 仅命中 assembly.py:51（Terminal）与两处定义 → **Serial 的会话上限不可配置**。
2. **（高）与 `terminal/__init__.py` 结构性重复**：会话注册表/分配（L537-553 vs terminal L245-260）、解析（L556-599 vs L362-412）、status（L464-482 vs L659-674）、close（L485-523 vs L677-711）、cleanup（L526-534 vs L714-719）、安全确认（L312-337 vs L538-567、L609-634）六组函数近乎同构，差异仅在「端口 vs devN」「scan」「raw_exec」「git 确认」。
3. **（中）错误文案风格与 Terminal 不一致**：本模块用字面量 `"[错误: …]"`（L192、L209、L245…），Terminal 用 `error_line()`（terminal/__init__.py L220、L242、L422…）→ UI 判定路径不同（tool_dispatcher.py L242 的 `tagged_judge` 只含 Shell/Terminal）。
4. **（中）默认值三处重复声明**：`execute`（L165-181）、`_connect`（L239-242）、`SerialSession.__init__`（serial_session.py L86-89）各写一遍 115200/8/"N"/1/"none"/"\n"/120/8000。
5. **（中）平台判断散布**：`sys.platform == "win32"` 出现 7 处（L254、L262、L319、L500、L504、L575、L579），未抽成「端口名归一/比较」的单一函数。
6. **（中）吞异常/静默降级**：`_allocate_session_id` 与 `_connect` 中 `old.close()` 无 try（L276、L492、L519、L550，底层 `close` 内部吞异常，serial_session.py L238-246）——错误不可见。
7. **（中）`_check_delete_safety` 借用调用方参数但语义特殊**：`action_name` 只用于回填 `pending_delete`（L329-336），`max_output_chars` 仅透传；且 Serial 无 git 检查（对比 terminal/__init__.py L543）。
8. **（低）注释耦合 LLM 参数名**：「参数名 "input" 与 DEFINITION 对齐，不可改名（LLM 通过 **arguments 传参）」（L176）——公共 API 与 LLM 传参机制绑定的历史约束。
9. **（低）`prompt_info` 文案一致性**：`prompt_info` 输出 `"COM3 @115200"`（serial_session.py L148）被复用于 connect/status 文案（L273、L306、L478）。
10. **（低）未使用导出**：`__all__` 导出 `SerialRuntime`（L18）但产品代码无外部使用者（只有 tool_exp 间接可见）。

**可测性**：

- 可独立单测（无硬件）：
  - `_scan`（L214）：可断言输出格式（依赖本机串口列表，建议注入替换 `comports`）。
  - `_check_delete_safety`（L312）：用假 `_tool_context`（`rm_skip_confirm`/`confirm_callback`/`_delete_confirmed`/`pending_delete`）覆盖三条分支。
  - `_resolve_session_id`（L556）、`_allocate_session_id`（L537）、`_close`（L485）、`_status`（L464）：向 `SerialRuntime.sessions` 注入 stub（实现 `is_alive`/`port`/`prompt_info`/`busy`/`close`）。
  - `execute`（L165）的参数转换、未知 action、各 action 缺参分支（不触发真实串口）。
- 需要集成测试（真实/虚拟串口）：`_connect`（含三阶段与占位回收、同端口防重）、`_exec`、`_raw_exec`（含纯监听）、`_input`、`kill_active_exec`、断线（`is_alive=False`）后的槽位回收。
- 无法自动化：ESC 打断与设备 ^C 的时序、`initial_output` 在不同设备的形态。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

出边（本组向外的依赖）：

- `tools/terminal/ssh_session.py` → `tools/exec_signal` (L38)、`tools/token_estimate` (L39)、`paramiko` (L35)、`socket` (L36)
- `tools/terminal/__init__.py` → `tools/terminal/ssh_session` (L21)、`tools/exec_signal` (L22)、`paramiko` (L19)
- `tools/terminal/remote.py` → `tools/terminal/__init__` (L10；延迟 L20、L173)、`tools/diff_utils` (L11)、`tools/read` (延迟 L77)
- `tools/serial/serial_session.py` → `pyserial` (L17)（无内部依赖）
- `tools/serial/__init__.py` → `tools/serial/serial_session` (L15)、`tools/tool_context` (L16)、`serial.tools.list_ports` (延迟 L217)

入边（本组被谁依赖）：

- `narnat_agent/assembly.py` → `tools/terminal` (L26；调用 L51)
- `narnat_agent/core/agent.py` → `tools/terminal` (L178、L269)、`tools/serial` (L179、L270)
- `narnat_agent/core/tool_dispatcher.py` → `tools/terminal` (L13、L14)、`tools/serial` (L15)
- `narnat_agent/ui/interrupt.py` → `tools/terminal` (L39)、`tools/serial` (L40)
- `narnat_agent/tools/read/__init__.py` → `tools/terminal` (L11)、`tools/terminal/remote` (L141)
- `narnat_agent/tools/edit/__init__.py` → `tools/terminal` (L16)、`tools/terminal/remote` (L104)
- `narnat_agent/tools/write/__init__.py` → `tools/terminal` (L10)、`tools/terminal/remote` (L57)
- `narnat_agent/tools/registry.py` → `tools/terminal` (L23)、`tools/serial` (L27)
- `tool_exp/**` → 本组全部模块（≥62 个脚本，示例行号见各文件节）

环依赖提示：`tools/terminal/__init__.py` ← `tools/terminal/remote.py`（L10）而 remote 又被 read/edit/write 依赖；同时 terminal 包被 read/edit/write 顶层导入 → 形成 `read → terminal → (remote → read)` 的环，靠函数内延迟导入（remote.py L77）与延迟调用规避（**未验证**是否存在运行时循环导入风险，实际 import 顺序由 registry.py 决定）。

### 模块级可变状态全清单（含读写方）

| 状态 | 定义位置 | 写方（行号） | 读方（行号） |
|---|---|---|---|
| `TerminalRuntime.sessions` | terminal/__init__.py L54 | L258、L439、L481、L685、L693、L708、L719 | L176、L253、L310、L316、L346-347、L373-375、L385-386、L389、L393、L400-402、L405-407、L411、L433-434、L445、L488、L664-665、L672、L682-684、L690-692、L705-707、L717 |
| `TerminalRuntime.max_sessions` | L49 | L64（← assembly.py:51） | L252、L400、L431、L663、L673、L705 |
| `TerminalRuntime.active_exec_session` | L58 | L473、L478、L582、L587、L731、L739 | L165 |
| `TerminalRuntime.sessions_lock` / `active_exec_lock` | L55 / L59 | （锁，无写入） | L164、L174、L299、L316、L346、L370、L428、L472、L477、L480、L486、L581、L586、L647、L652、L661、L679、L716、L730、L738 |
| `SerialRuntime.sessions` | serial/__init__.py L40 | L277、L286、L299、L304、L363、L409、L448、L493、L516、L520、L534、L551 | L259、L270-271、L282、L298、L467、L471-472、L479、L490、L502、L511、L514、L531、L545-547、L567、L569、L577、L588 |
| `SerialRuntime.max_sessions` | L28 | **无写入方**（set_max_sessions L49 无调用） | L268、L269、L283、L468、L479、L544 |
| `SerialRuntime.active_exec_sids` | L45 | L368、L373、L414、L419、L453、L458、L495、L522、L529 | L155 |
| `SerialRuntime.sessions_lock` / `active_exec_lock` | L41 / L46 | （锁，无写入） | L154、L257、L297、L303、L362、L367、L372、L408、L413、L418、L447、L452、L457、L466、L487、L494、L521、L528、L530、L565 |
| `SSHSession` 实例状态（`_cwd`/`_busy`/`_backlog`/`_pending_*`/`_sentinel_sent`/`_echo_enabled`/`_watcher_*`/`_interrupt`） | ssh_session.py L125-L169 | 见 ssh_session 节「状态」 | 见 ssh_session 节「状态」；外部读 L177（`_busy`）、terminal L667 |
| `SerialSession` 实例状态（`_buffer`/`_busy`/`_dead`/`_interrupt`/`_reader_alive`） | serial_session.py L123-L141 | 见 serial_session 节「状态」 | 见 serial_session 节「状态」；外部读 serial L477（`busy`） |
| `remote.py` | — | 无模块级状态 | — |

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

1. **（高）跨层直取私有成员**：`terminal/__init__.py:256/348/435/576/592/642/666/728`（`session._channel.closed`）、`:167/169/180/182`（`_interrupt.set()` / `_channel.send`）、`:780/795/851/882/918-919`（`session._client.open_sftp()`）、`:475/502/504`（`_initialize` / `_initial_output`）；`remote.py:32`。证据：`_channel`/`_client` 定义在 ssh_session.py L174/L197（私有）。
2. **（高）Serial 配置未接线**：`serial/__init__.py:28/48-51`（`max_sessions` 硬编码、`set_max_sessions` 无调用）vs `assembly.py:26/51`（只设置 Terminal）。
3. **（高）terminal 与 serial 的注册表/解析/status/close/cleanup/确认六组函数结构性重复**：`terminal/__init__.py:245-260、362-412、659-674、677-711、714-719、538-567+609-634` vs `serial/__init__.py:537-553、556-599、464-482、485-523、526-534、312-337`。
4. **（高）SSH 哨兵延迟协议 + PS1 静默确认 + 哨兵自愈三层状态机共享读循环**：`ssh_session.py:321-332、596-663、893-902`（状态变量 L804-821）。
5. **（中）`"__AWAIT_CONFIRM__"` 契约双轨**：字面量 `terminal/__init__.py:567、634` vs 常量 `tool_context.py:12` / `serial/__init__.py:16、337`。
6. **（中）Terminal 的 `active_exec_session` 单变量在多会话并发 exec 时互相覆盖**：`terminal/__init__.py:58、473、582`；对照 `serial/__init__.py:43-45`（已改集合，注释明确说明该缺陷）。
7. **（中）近似重复函数与文案不统一**：`terminal/__init__.py:263-269` vs `:272-280`（设备归一化两版）、`:283-291` vs `:353-359`（设备提示两版）、`:308-317`（列表两版）；`remote.py:103-110` vs `:219-226`（errno 分类两版）；`ssh_session.py:47-64` vs `serial_session.py:34-46`（`_truncate_output` 两版）。
8. **（中）吞异常普遍**：`ssh_session.py:222-223、228-229、265-266、483-484、493-494、540-541、1041-1042`；`terminal/__init__.py:325-326、735-736、786-787、816-817`；`remote.py:26-27、114-117、167-168`；`serial_session.py:238-241、243-246、267-269`。
9. **（中）魔法值密集无集中定义**：`ssh_session.py:113、809、854、935、927、945、752、846、653`；`serial_session.py:60-65、81、84、291、298、373、527-528`；`terminal/__init__.py:505-508、958`。
10. **（中）超时后行为不对称**：SSH 超时置忙 + watcher + backlog 收敛（`ssh_session.py:1052-1055、596-699`），Serial 超时即复位且丢弃迟到输出（`serial_session.py:179-180、336-341、315-316`）；而双方文档字符串均自称与对方「对齐」（`serial_session.py:186-189`、`serial/__init__.py:559`）。

### 本组对外契约清单（被本组之外模块依赖的 public API，即新架构必须保持的行为面）

1. `Terminal.execute(action, host, username, port, key_path, password, sudo_password, command, input, timeout, session_id, max_output_chars, source_host, source_path, target_host, target_path, _tool_context)`（terminal/__init__.py:189-207）→ `tools/registry.py:43`（工具名 `Terminal`）。
2. `Terminal.DEFINITION`（terminal/__init__.py:66）→ `tools/registry.py:23、59`（LLM 工具定义，参数名与描述文案是 AI 侧契约）。
3. `Terminal.kill_active_exec()`（terminal/__init__.py:158）→ `core/tool_dispatcher.py:13`、`ui/interrupt.py:39`。
4. `Terminal.resolve_dev_display(dev)`（terminal/__init__.py:329）→ `core/tool_dispatcher.py:14、428、458、465、467、469、474`（终端摘要显示）。
5. `Terminal.cleanup()`（terminal/__init__.py:714）→ `core/agent.py:178、181、269、272`（退出清理）。
6. `Terminal.get_session(session_id=-1, host="")`（terminal/__init__.py:742）→ `tools/terminal/remote.py:10`（文件工具远程路径入口）。
7. `SSHSession`（含 `_client`/`_channel`/`_initialize`/`_initial_output`/`_interrupt`/`_busy`/`_cwd`/`prompt`/`execute`/`send_input`/`reconnect`/`close`）（ssh_session.py:67-1317）→ `terminal/__init__.py:21`、`remote.py:10`（间接：read/edit/write）。
8. `Terminal._normalize_device_for_tools(device)` / `Terminal._file_tool_device_hint()`（terminal/__init__.py:272、283）→ `tools/read/__init__.py:11`、`tools/edit/__init__.py:16`、`tools/write/__init__.py:10`（文件工具设备校验与提示）。
9. `TerminalRuntime.set_max_sessions(n)`（terminal/__init__.py:61-64）→ `assembly.py:26、51`（配置注入点）。
10. `remote_read(file_path, offset=0, limit=2000, host="")` / `remote_write(file_path, content, host="")` / `remote_edit(file_path, old_string, new_string, replace_all, host)`（remote.py:37、135、206）→ `tools/read/__init__.py:141`、`tools/write/__init__.py:57`、`tools/edit/__init__.py:104`（返回 tuple 的 `(llm_result, color_diff)` 形态须保持）。
11. `Serial.execute(action, port, baudrate, databits, parity, stopbits, flow_control, line_ending, prompt_pattern, command, input, timeout, session_id, max_output_chars, _tool_context)`（serial/__init__.py:165-181）→ `tools/registry.py:47`（工具名 `Serial`）。
12. `Serial.DEFINITION`（serial/__init__.py:54）→ `tools/registry.py:27、60`。
13. `Serial.kill_active_exec()`（serial/__init__.py:152）→ `core/tool_dispatcher.py:15`、`ui/interrupt.py:40`。
14. `Serial.cleanup()`（serial/__init__.py:526）→ `core/agent.py:179、182、270、273`。
15. **结果文本契约**（AI 与 UI 共同依赖，须逐字保持）：
    - Terminal/Serial 结果前缀 `[dev{n}(终端{sid})] `（terminal:589、654）与 `[终端{sid}] `（serial:374、420、459）；
    - SSH 的设计内提示（不带错误标签、UI 不显示失败）：`[上一个命令尚未完成…]`（ssh_session:309-310）、`[超时: 命令执行超过N秒，仍在后台运行…]`（ssh_session:1057-1058）、`[检测到密码提示，请用input action输入密码]`（ssh_session:974）、`[后台命令已完成，输出如下]`（ssh_session:342-344）、`[已中断: …^C 终止]`（ssh_session:416）、`[用户中断]`（ssh_session:1016/1024）、`[^C已发送但命令未终止…]`（ssh_session:428）、`[连接已中断（设备可能关机/重启或网络不通），命令结果未知]`（ssh_session:1044，经 `error_line` 带标签）；
    - 框架错误标签机制：Terminal 侧错误一律经 `exec_signal.error_line`（带进程级随机标签，`core/tool_dispatcher.py:242-247` 据此判定 UI 失败显示）；Serial 侧不参与该机制（tool_dispatcher.py:242 的 `tagged_judge` 仅含 Shell/Terminal/mcp__），改用 `[错误` 前缀判定（tool_dispatcher.py:265-270）；
    - `AWAIT_CONFIRM = "__AWAIT_CONFIRM__"`（tool_context.py:12）配合 `pending_delete = (tool_name, arguments_dict)`（tool_context.py:55），由 `core/agent_loop.py:239-254、411-451` 消费。
16. **配置面**：`config.tools.max_sessions`（config/loader.py:71，默认 5）→ `assembly.py:51`；`config.tools.max_transfer_mb`（config/loader.py:72，默认 100）→ `ToolContext.max_transfer_mb`（tool_context.py:35）→ `terminal/__init__.py:959-960`；`config.tools.max_timeout_seconds`（loader.py:74）→ exec/input/connect 截断（terminal:226-227、535-536、606-607；serial:348-349、392-393、433-434）。
17. **测试脚本面（非产品契约，但重构会破坏）**：`tool_exp/` 中 ≥62 个脚本直接 import `SSHSession`、`terminal.execute`、`terminal.remote`、`serial.execute`、`_truncate_output`、`_normalize_device`、`_dev_hint_locked`、`_file_tool_device_hint` 等（示例：`verify_shell_three_fixes.py:99`、`zz_check_truncate_regression.py:10`、`verify_device_ref_spec.py:11`、`test_ps1_offline.py:5`、`zz_rollback_final.py:15-16`）。

**未验证项汇总**（禁止臆测，需真机/运行确认）：

1. `ssh_session.py` 的 `time.time_ns()` marker 冲突概率（L318-319）与 `_ps1_candidate` 宽正则在真实多语言 PS1 下的误判率（L109）。
2. `serial_session.py` 的 `PROMPT_RE` 在真实设备输出下的误判率（L78、L441）。
3. `remote.py` 的 SFTP `errno` 在 paramiko 各版本/平台上是否稳定带 errno（L106、L222）。
4. `terminal/__init__.py` `_allocate_session_id` 回收死会话时后台 watcher 是否仍在运行（L256-259）。
5. `ssh_session.py` `reconnect` 后 watcher 与 `_abandon` 状态是否完全清理（L231-239）在并发文件操作下的表现。
6. `serial` 背压截断（serial_session.py L260-265）在高速刷屏设备下的实际丢数据量与 AI 可感知程度。
7. terminal 传输的吞吐与中断恢复行为（terminal/__init__.py L916-937）未做实测；传输过程中无进度/断点续传（未验证大文件场景的实际体验）。

