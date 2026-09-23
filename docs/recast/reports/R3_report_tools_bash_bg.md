# R3 现状调研报告：终端执行类工具（bash / background / exec_signal）

> 读者：未读过本组源码的架构师。本报告用于①编写行为规格（specs）、②设计新结构（职责/接口/状态/依赖）。
>
> 调研方式：三个源文件**全文逐行通读**（Read），被依赖关系用 Grep 反查，行数用 `find /c /v ""` 实测。
> 本次调研**未执行**任何源文件中的命令执行函数、未发起网络/进程调用（符合任务边界条款）；
> 凡未实际运行验证的结论均标注「未验证」。所有行号以本次调研时的代码文本为准。
>
> 实测行数：`narnat_agent/tools/bash/__init__.py` = 1088 行；`narnat_agent/tools/background/__init__.py` = 702 行；
> `narnat_agent/tools/exec_signal.py` = 83 行（任务清单标注"约100"，实测 83）。

---

### narnat_agent/tools/bash/__init__.py（1088行）

**职责**：Shell（本地命令执行）工具的完整实现——负责命令送达本地 shell（Windows=cmd.exe 子进程 / Linux·macOS=bash -c）、输出解码截断、退出码行封装、cd 持久化、`&&`/`||` 分段执行、`python -c` 载荷直执行、超时/ESC 中断杀进程树、后台任务提交与管理的入口分发。

**对外接口**：

- `class BashRuntime`（20行）：全部模块级状态与参数的命名空间类（原模块级全局收敛于此，见 21-26 行 docstring）。
  - 类属性常量（导入时构建，之后只读）：`RE_DELETE`（30）、`RE_GIT`（37）、`RE_PY_C_DIRECT`（42）、`utf8_env`（50）、`DRAIN_GRACE = 0.3`（62）、`PLATFORM_LABEL`（64）。
  - 可变类属性：`active_proc: Optional[subprocess.Popen] = None`（55）、`active_proc_lock = threading.Lock()`（56）、`interrupted = False`（59）。
- `def kill_active():`（233）：ESC 打断时由 agent 层调用——置位 `BashRuntime.interrupted` 并后台线程杀当前前台进程树（用途：立即打断前台命令）。
- `def execute(command: str = None, timeout: int = 120, max_output_chars: int = 4000, max_output_tokens: int = None, background: bool = False, bg: str = None, id: int = None, _tool_context=None) -> str:`（423）：Shell 工具唯一入口（前台执行 + 后台任务分发 + 安全确认），返回给 LLM 的文本。
- `DEFINITION = {...}`（172-230）：LLM 工具定义（`"name": "Shell"`），参数 command/timeout/max_output_chars/background/bg/id，`"required": []`（227）。
- `def _kill_proc_tree(proc: subprocess.Popen):`（351）：杀进程树（Windows=taskkill /F /T；Unix=killpg SIGKILL）。**事实上的跨模块私有契约**：被 `tools/background` 直接 import（600、636 行）。
- 其余私有函数（模块内部实现，外部不应依赖，但测试脚本已依赖部分）：
  - `def _scan_code_suffix(tail: str):`（67）— `python -c "代码"` 尾部扫描/后缀剥离
  - `def _parse_suffix(suffix: str):`（99）— 解析 `| cmd`、`> file`、`>> file`、`>nul`
  - `def _try_extract_py_code(seg: str):`（132）— 识别 `python -c "code"` 并判定是否可直执行
  - `def _find_executable(*names: str) -> Optional[str]:`（247）— 按优先级 which 查找
  - `def _decode_output(raw: bytes) -> str:`（256）— UTF-8 优先、Windows 回退 GBK、兜底 replace
  - `def _split_commands(command: str) -> list:`（272）— 引号外/括号外按 `&&`、`||` 分段
  - `def _is_cd_command(cmd: str) -> bool:`（313）— 判定纯 cd/chdir
  - `def _extract_cd_path(cmd: str) -> Optional[str]:`（328）— 提取 cd 目标路径（含 `cd..`/`cd...`/`cd\`/`/d` 处理）
  - `def _drain_readers(*threads) -> None:`（373）— 读线程共享一个宽限收尾
  - `def _truncate_output(text: str, max_chars: int) -> str:`（388）— 首尾保留式截断 + 标签吸附
  - `def _format_prompt() -> str:`（408）— 仿终端提示符
  - `def _collect_proc_output(proc: subprocess.Popen, timeout: int, max_output_chars: int):`（688）— 等进程+收输出，返回 `(rc, out, err, status)`，status∈{ok,timeout,interrupt}
  - `def _format_result(rc: int, out: str, err: str, status: str, timeout: int, max_output_chars: int) -> str:`（754）— 统一结果组装
  - `def _execute_win32(command: str, timeout: int, max_output_chars: int) -> str:`（780）— Windows shell=True 单段执行
  - `def _execute_py_direct(exe: str, flags: str, tail: str, timeout: int, max_output_chars: int):`（805）— 绕过 cmd 直执行 python -c
  - `def _execute_py_suffixed(exe: str, flags: str, tail: str, spec, timeout: int, max_output_chars: int):`（830）— 直执行 + `|`/`>`/`>nul` 后缀
  - `def _execute_py_pipe(exe: str, flags: str, tail: str, pipe_cmd: str, timeout: int, max_output_chars: int):`（855）— 直执行后 stdout 喂管道命令
  - `def _execute_segments(segments: list, timeout: int, max_output_chars: int, _tool_context) -> str:`（902）— 逐段执行 `&&`/`||`

**依赖**：

- `..exec_signal`（16行）：`rc_line`、`error_line`、`tag_error`、`safe_cut_points`
- `..token_estimate`（17行）：`estimate_text_tokens`（用于截断提示的 ≈token 标注，400行）
- `..background`（**函数内延迟导入**，495行）：`bg_execute`（避免 bash↔background 循环）
- 标准库：os/re/subprocess/sys/threading/time/typing（8-14）；函数内另有 `import shutil`（158、249）、`import signal`（366）

**被依赖**（Grep 复验）：

- `narnat_agent/tools/registry.py:22`：`from .bash import execute as bash_execute, DEFINITION as BASH_DEF`（注册为工具名 `"Shell"`，42行；定义收集 59行）
- `narnat_agent/core/tool_dispatcher.py:12`：`from ..tools.bash import kill_active as _kill_bash`（调用点 138、154、177、203、285、309——流取消时杀前台进程）
- `narnat_agent/ui/interrupt.py:38`：`from ..tools.bash import kill_active`（调用点 42，ESC 检测线程内）
- `narnat_agent/tools/background/__init__.py:226`：`from ..bash import BashRuntime`（submit）；`:565`（wait_tasks）；`:600`、`:636`：`from ..bash import _kill_proc_tree`（cancel_task / cleanup_all）
- 非生产代码（供交叉参考，不影响契约）：`subagent_test/regress_fix.py:6`、`subagent_test/regress_fix3.py:6`、`tool_exp/esc_steal_fix_verify.py:13`、`tool_exp/test_2e1.py:7`、`tool_exp/test_py_direct.py:12`、`tool_exp/test_stress.py:18`、`tool_exp/test_suffix.py:7`、`tool_exp/verify_cd_fix.py:12/57`、`tool_exp/verify_shell_three_fixes.py:32-33`、`tool_exp/verify_ui_fail_gitdiff.py:13`、`tool_exp/verify_ui_judge_e2e.py:7`、`tool_exp/_obsolete_20260910/regression_test.py:142`、`tool_exp/_obsolete_20260910/regression_test_v2.py:101/112`、`tool_exp/_obsolete_20260910/verify_ux_round3.py:62`

**状态**：

- 模块级全局：无（全部收敛进 `BashRuntime` 类属性，170 行注释证据）。
- 类变量（BashRuntime）：
  - `RE_DELETE`（30）、`RE_GIT`（37）、`RE_PY_C_DIRECT`（42）、`DRAIN_GRACE`（62）、`PLATFORM_LABEL`（64）：导入时构建，之后只读。
  - `utf8_env`（50-52）：导入时由 `os.environ.copy()` 构建一次，之后只读（使用点 592、793、822、879、995；`tools/background` 287、300 也读）。
  - `active_proc`（55）：可变。写：601、695、1004（置为当前 proc）；清：685、751、1079（finally 置 None）。读：242（kill_active）。
  - `active_proc_lock`（56）：锁对象，保护 active_proc（241、600、684、694、750、1003、1078）。
  - `interrupted`（59）：可变、跨线程。写：240（kill_active 置 True）、515（Windows 入口清零）、626（Unix 入口清零）、637（消费后清零）、728-730（_collect_proc_output）、911（_execute_segments 入口清零）、1032-1034（段内消费）。读：635、728、1032。**模块外读写**：`tools/background/__init__.py` 566（wait_tasks 清零）、577（读）。
- 实例状态：本模块无类实例；`subprocess.Popen` 对象经 `active_proc` 跨线程共享。另有隐式进程级状态：`os.getcwd()`（被 execute 的 cd 路径修改：524、568、933；被 Popen cwd= 读取：590、792、821、878、993）。

**行为要点**（可观察行为；编号-行号）：

1. 工具定义与平台标签：DEFINITION 描述文本在导入时用 `PLATFORM_LABEL` 渲染为"本地Shell — 在Windows(cmd)执行命令。前台同步执行并返回输出；支持后台任务（提交、查询、等待、取消）。"（64、177-179）；参数无必填项（227）。
2. 参数归一化：`timeout`/`max_output_chars`/`max_output_tokens` 接受字符串数字（int() 强转）；`max_output_tokens` 是 `max_output_chars` 的**字符数语义别名**，两者同传时以 `max_output_tokens` 为准并覆盖前者（458-464）。转换失败返回 `error_line("timeout/max_output_chars需为整数")`（463-464）。
3. 安全确认在参数归一化之后、其它校验之前执行（465-491）：命中 `RE_DELETE`（\b(rm|del|rd|rmdir|erase|format)\b[\s/] 或 \bRemove-Item\b，30-34）或 `RE_GIT`（\bgit\b，37）时置 need_confirm，受 `tc.rm_skip_confirm`/`tc.git_skip_confirm` 开关控制（469-472）。
   - Windows：调用 `tc.confirm_callback(command)`，返回 False 则结果 `"[操作已取消: 此命令需用户确认]"`（475-477）；`confirm_callback` 为 None 时不拦截（条件短路）。
   - 非 Windows：若 `tc._delete_confirmed` 为 True 则消费该标志（置 False）继续执行；否则写 `tc.pending_delete = ("Shell", {command, timeout, max_output_chars, background, bg, id})` 并返回字面量 `"__AWAIT_CONFIRM__"`（478-491）。
4. 后台分发：`background=True` 或 `bg` 非空时，转发 `bg_execute(command, timeout, background, bg, id, _tool_context)`（494-496），前台逻辑不变；后台提交同样先过安全确认（466 注释）。
5. 前台参数校验顺序：command 空 → `error_line("command为空：前台执行需提供 command；后台任务用 background=true 提交，管理用 bg=status/wait/cancel")`（498-502）；`timeout <= 0` → `error_line("timeout需为正整数（秒）")`（504-505）；`tc.max_timeout_seconds > 0` 时 clamp timeout（507-508）。
6. cd 持久化（Windows，518-528）：纯 cd 命令由 Python 直接 `os.chdir`，使后续所有工具共享新 CWD。无参数 cd 返回 `[exit code: 0] + 提示符`（仅显示当前目录，522）；成功返回 `rc_line(0)+提示符`（528）；失败返回 `cd: {e}\n[exit code: 1] + 提示符`（527）。
7. cd 持久化（Unix，562-571）：与 Windows 一致，但**无参数 cd 等价于回 $HOME**（566）——平台语义差异（cmd 显示当前目录 vs bash 切 home）。
8. 多段命令：`_split_commands` 在引号外、括号组外、跳过 cmd `^` 转义字符，按 `&&`/`||` 切分（272-310）；段数 >1 时进入 `_execute_segments`（Windows：531-533；Unix：574-576）。
9. `python -c` 直执行（Windows 单段路径，535-548）：`_try_extract_py_code` 命中时绕过 cmd 用 `shell=False` 直接 CreateProcess（805-827），多行/%/&/| 等原样交给解释器；命中条件见边界行为 12。
10. 落点顺序（Windows）：确认 → 后台分发 → 空参校验 → cd → 多段 → py 直执行 → `_execute_win32`（469-550）。Unix：确认 → 后台分发 → 空参校验 → shell 查找 → cd → 多段 → `bash -c`（469-578）。
11. 执行监视：读线程按 4096 字节读 stdout/stderr（607-622、701-718、1010-1023）；主线程 50ms 轮询 `proc.poll()`，超时或 `interrupted` 置位即退出循环（631-639、724-732、1028-1036）；**先杀进程树再收尾输出**（644-646、735-737、1042-1044），随后 `_drain_readers` 共享 0.3s 宽限（373-385、648、739、1046）。
12. 结果格式（前台正常，675-682 与 754-777）：`[exit code: N] [随机标签]` 行 + stdout（非空时）+ `[stderr]\n{err}`（非空时）+ 提示符。
13. 结果格式（中断，653-662）：`stdout + [stderr]… + [用户中断] + 提示符`——**无退出码行**；结果不含错误标签（UI 不显示失败）。
14. 结果格式（超时，664-673）：`stdout + [stderr]… + [超时: 命令执行超过{timeout:.0f}秒，已终止] [错误标签] + 提示符`——带错误标签，UI 会显示失败。
15. 提示符（408-420）：Windows 为 `{cwd}>`；Unix 为 `~$ `（home）、`~/path$ `（home 下）、`{cwd}$ `（其它）。
16. 输出截断（388-405）：超长时保留首 `max_chars*2//3` 与尾 `max_chars - head`，中段插入 `...[中间截断: 输出共N字符, 已保留首X字符+尾Y字符(≈Ttoken)。增大max_output_chars可获取完整输出]`；切点先经 `safe_cut_points` 吸附，保证框架标签不被切开（399）。
17. 多段执行语义（902-1088）：`&&` 在 `prev_rc != 0` 时跳过本段并输出 `[跳过: 前一命令失败(退出码N)] {seg}`（919-921）；`||` 在 `prev_rc == 0` 时跳过并输出 `[跳过: 前一命令成功] {seg}`（922-924）；每段独立预算 = `remaining_timeout`，段后 `remaining_timeout = max(0, remaining_timeout - seg_elapsed)`（956、1039）；成功段不输出退出码行，失败段输出各自的 `rc_line`（970-979、1065-1075）；末尾追加总退出码（`prev_rc >= 0 且未中断`，1085-1086）；中断则追加 `[用户中断]` 且不输出总码（1081-1082）。
18. 多段中的 cd 段：由 Python `os.chdir` 执行，成功/失败分别置 `prev_rc = 0/1`，成功时**不产生任何输出**（927-938）；失败输出 `cd: {e}`（936）。
19. python 段直执行（多段内，940-981）：仍走 `_execute_py_direct`/`_execute_py_suffixed`；中断 → 置 was_interrupted 并 break；超时 → 输出 `[超时: 命令执行超过{max(seg_elapsed,1.0):.1f}秒，已终止]` + 已有输出，`prev_rc=-1` 并 break。
20. `>nul`/`>`/`>>`/`|` 后缀（830-852、855-899）：`>nul` 丢弃 stdout（仅留 stderr，841-843）；`>file`/`>>file` 把 stdout 以 UTF-8 字节写入文件，返回的 stdout 置空（845-852）；`| cmd` 把 python 的 stdout 字节喂给 `cmd`（shell=True）并在剩余预算 `max(0.1, timeout-elapsed)` 内收集，python stderr 与管道 stderr 合并展示（855-899）。
21. 中断转发链：`kill_active()` 置位 interrupted 并起后台线程杀树（233-244）——ESC 后主线程不被杀树阻塞；`execute` 各入口清零 interrupted（515、626、911），消费后清零（637、730、1034）。
22. `bg=wait` 中断语义（在 background 模块，见下节）：仅打断等待本身，后台任务继续运行。

**边界/异常行为**（编号-行号）：

1. `timeout`/`max_output_chars` 非数字 → 早退错误文本（463-464）；但 `max_output_chars=0`/负数不早退，命令**照常执行完**后在 `_truncate_output` 返回 `error_line("max_output_chars需为正整数")`（394-395）——即命令副作用已发生、只丢输出。
2. `_split_commands` 对空命令返回 `[("", "")]`（303-304）；`execute` 已在之前拦截空 command，故不会以空命令进入执行（498-502）。
3. `_is_cd_command` 拒绝任何含 `&`、`|`、`;` 的命令（320）——`cd x && ls` 不会被当作纯 cd，而是进入多段逻辑后由段级 cd 处理；`cd x; ls` 在 Unix 下作为整段交给 bash -c（CWD 不持久）。
4. `_extract_cd_path` 支持 `cd..`→`".."`、`cd...`→`../..`、`cd\`→`"\"`（333-338）；`/d ` 前缀剥离（344-345）；`strip('"')` 去引号；`expandvars`+`expanduser` 展开（348）。**Windows 下 `cd \`（带空格）不匹配 `cd\` 简写**，会走通用路径 → `os.chdir("\\")`（切当前盘根）。
5. 无参数 cd：Windows 返回仅显示当前目录（522）；Unix 切 $HOME（566）；多段内的无参数 cd 两平台都仅置 prev_rc=0 不切换（929-930）。
6. `os.chdir` 失败在单段路径返回 `cd: {e}\n[exit code: 1] + 提示符`（527、570）；在多段路径输出 `cd: {e}` 且 prev_rc=1（935-937）。
7. 进程启动异常：FileNotFoundError→`error_line("cmd.exe未找到: {e}")` / `error_line("Shell未找到: {e}")`（595、795）；OSError/ValueError（含 NUL 字符）→`error_line("启动失败: {e}")`（596-598、797-799、824-826 返回 `(1,"","启动失败…","ok")`、881-882 返回管道启动失败合并 stderr、997-1001 段启动失败 `error_line(f"段{i}启动失败: {e}")` 且 prev_rc=-1 并 break）。
8. 超时/中断后 `proc.wait(timeout=5)`（646、737、1044）**未捕获 `subprocess.TimeoutExpired`**——杀树失败（如权限不足）时异常向上传播到 registry 的 `except Exception` → `error_line("工具执行失败(Shell): …")`。未验证真实触发概率。
9. `_kill_proc_tree` 自带 `proc.poll() is not None` 短路（353-354）；taskkill 异常被 `except Exception: pass` 吞（363-364）；Unix `killpg(os.getpgid(pid), SIGKILL)` 忽略 ProcessLookupError/OSError（369-370）。
10. `_drain_readers` 只在 0.3s 共享宽限内 join（381-385）；`start /b` 分离孙进程继承管道写端时（注释 376-379），读线程可能仍存活但输出只保留已读到的部分——读线程是 daemon，不阻塞退出。
11. `_decode_output`：空 bytes → `""`（258-259）；UTF-8 成功即返回；失败且 Windows → 尝试 GBK；再失败 → `raw.decode("utf-8", errors="replace")`（260-269）。Unix 无 GBK 回退。
12. `_try_extract_py_code` 的命中条件（132-168）：整体匹配 `RE_PY_C_DIRECT`（42-46，exe 可为解释器名/带盘符空格的全路径、flags 排除 -c/-m 与引号开头项、`-c` 后整段载荷 re.S）；载荷必须以 `"` 开头且 **代码尾以 `"` 结尾**（146-147、156-157）；`_scan_code_suffix` 允许引号后跟空白/`2>&1`（剥离，语义等价）/`|`/`>`，其它 token 回退（67-96、148-153、94）；`shutil.which(exe)` 必须解析成功（159-161）；解析结果 basename 必须匹配 `py(?:thon)?\d*(?:w)?`（163-164，防 `spy.exe` 误命中）；扩展名必须是空或 `.exe`（165-167，`.bat` 垫片回退 cmd）。
13. `_parse_suffix` 不支持的形态一律返回 None → 回退 cmd 原路径（99-129、150-153）：`|` 命令内含 `<>&|` 回退（107-109）；空的管道/重定向目标回退（105-106、116-117）；`> "带空格路径"` 允许，但引号后有尾随 token 则回退（120-123）；裸路径含空格（多于一个 token）回退（126-128）；`>nul`（大小写不敏感）→ discard（118-119）。
14. 管道/重定向路径的预算处理：`_execute_py_pipe` 用剩余时间 `max(0.1, timeout - elapsed)`（870）；python 阶段非 ok（中断/超时）时跳过管道阶段并原样返回（867-868）；喂数据用 daemon 线程，BrokenPipeError/OSError 忽略（884-894）。
15. `_execute_py_suffixed` 在 `status != "ok"` 或 discard 时**不写文件**（841-843）；写文件失败返回 `(1, "", "写入文件失败: {e}", "ok")`（850-851）。
16. `_execute_segments` 中 `remaining_timeout` 可被减到 0（956、1039）：后续段的 `deadline = now + 0` → 立即判定超时并杀树（1025-1031）。即总预算耗尽后，剩余段全部立即"超时"并结束循环。
17. 段超时/中断时 `seg_elapsed` 提示用 `max(seg_elapsed, 1.0)` 保底为 1.0 秒（962、1054）；单段/collect 路径的提示用 `timeout:.0f`（672、770）。
18. 多段落有界：段启动异常（NUL 等）→ 记录 `段{i}启动失败` + `prev_rc=-1` + break（997-1001）。
19. Windows 与 Unix 的 `_try_extract_py_code` 调用点不对称：Windows 单段（537）与多段（942，两平台共用）都会直执行；**Unix 单段命令不会直执行**（574-578 之后直接 `bash -c`）——同样 `python -c "..."` 在 Unix 单段走 bash，在多段走直执行。`_try_extract_py_code` 自身平台无关，Unix 下 `which("python3")` 解析为 `/usr/bin/python3`（basename 匹配、ext 为空）可通过（163-167）。未在 Linux 运行验证（未验证）。
20. 多段的非 python 段在 Windows 用 `shell=True`（cmd.exe）、在 Unix 同样 `shell=True` → 由 **/bin/sh** 执行（987-996），与单段路径"优先 bash、无则 sh"（555）不同——平台分支不一致。
21. ESC 中断标志跨调用残留：若 kill_active 在没有活动进程时被调用，`interrupted` 仍被置 True（240），只能靠下一次 execute 入口清零（515、626、911）。
22. `interrupted` 消费点与置位点之间存在竞态窗口（轮询 50ms）：窗口内进程自然结束时，中断标记可能保留到下次调用才被清零（635-638 路径：进入循环时置 was_interrupted 才清零）。
23. Windows 单段执行**未设置** `start_new_session`（786-794），Unix 单段设置（591）；Windows 杀树依赖 taskkill /T（356-364），Unix 依赖 killpg（365-370）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【高】`execute()` 单函数承载 5 类职责：参数归一化、安全确认、后台分发、平台分支执行、结果格式化（423-685，共 263 行）；Windows/Unix 两套并行的 cd/多段/py 判定重复实现（513-550 vs 552-578）。
2. 【高】执行循环三重重复：`_reader` + 轮询 + 杀树 + 组装在 603-682（Unix 单段）、688-751（`_collect_proc_output`）、1006-1079（段执行）三处近乎复制；结果组装在 653-673、744-748+754-777、1048-1061 四处重复。
3. 【高】`BashRuntime` 可变全局跨层共享：`interrupted` 被 bash 自身（240、635、728、1032）、background（566、577）读写；`active_proc` 被 kill_active/dispatch 跨线程读写（55、242、600-601、684-685）。跨层引用证据：`ui/interrupt.py:38`、`core/tool_dispatcher.py:12`。
4. 【高】background 跨模块 import 本模块**私有**函数 `_kill_proc_tree`（background 600、636）——通用工具函数被放在 bash 内并被外部私有引用。
5. 【中】魔法值/常量重复：字面量 `"__AWAIT_CONFIRM__"`（491）与 `tools/tool_context.py:12` 的 `AWAIT_CONFIRM` 重复定义；且 483-490 直接构造 `tc.pending_delete` 的字典形状（knowledge 重复于 agent_loop 的消费端）。
6. 【中】绕过封装：476 行直接调用 `tc.confirm_callback(command)`，而非 `ToolContext.confirm_delete()` 方法（tool_context.py:71-75）。
7. 【中】过时文档与实现不符：`execute` docstring 声称 "Windows: 持久化cmd会话，命令直写stdin"（436-437），实现早已是 `subprocess.Popen(shell=True)` 子进程模型（780-802）；模块顶层 docstring（1-6）已更新为子进程描述——两处注释口径不一。
8. 【中】脆弱正则+魔法解析：`RE_PY_C_DIRECT` 巨型正则（42-46）+ `_scan_code_suffix` 手工扫描（67-96）对 `2>&1` 字面硬编码（86-88），任何新后缀形态都需改扫描器。
9. 【中】历史包袱注释残留："（以上模块级状态已收敛为 BashRuntime 类成员）"（170）、"（原有逻辑）"（553）、"此前 cd 走 bash -c 子进程执行…"（559-561）——记录历次补丁的注记散布在实现中。
10. 【中】吞异常：`_reader` `except Exception: pass`（614-615、708-709、1017-1018）；`_kill_proc_tree` 的 `except Exception: pass`（363-364）；`_feed` 的 finally 内 `except Exception: pass`（891-894）。
11. 【低】死代码/无生产调用者：`_find_executable` 仅 Unix 分支用（247、555）；`_scan_code_suffix`/`_parse_suffix` 仅被 `_try_extract_py_code` 用（67、99）。
12. 【低】外部历史包袱证据：`tool_exp/verify_cd_fix.py:12` 与 `tool_exp/_obsolete_20260910/regression_test_v2.py:101` 引用已不存在的 `_has_nonpersistent_cd`（本仓库 narnat_agent 内已无该符号，`findstr` 无匹配）——说明曾存在后经重构移除，旧验证脚本已失效。
13. 【低】重复的 `import shutil`（158、249 函数内导入）与模块已导入标准库风格不一致。

**可测性**：

- 可独立单测（纯函数、无子进程）：
  - `_split_commands`（272）、`_is_cd_command`（313）、`_extract_cd_path`（328）：纯字符串逻辑，边界（引号/括号/`^`/`cd..`）可全覆盖。
  - `_decode_output`（256）：喂字节即测（含 GBK 与坏字节）。
  - `_truncate_output`（388）+ `safe_cut_points`：构造含标签的长文本断言切点与提示文本。
  - `_scan_code_suffix`（67）、`_parse_suffix`（99）、`_try_extract_py_code`（132）：纯字符串+`shutil.which`（可 monkeypatch which 结果）——注意 `_try_extract_py_code` 最后一步依赖真实 `shutil.which(exe)`（159），需 patch 才能脱离环境。
  - `_format_prompt`（408）：依赖 `os.getcwd()`，可临时 chdir。
  - `_format_result`（754）/`_collect_proc_output` 的组装部分：前者可对构造的 (rc,out,err,status) 直接断言输出格式。
- 需要集成测试（真起子进程）：
  - `execute` 的 Windows/Unix 分支（513-578）、`_execute_win32`（780）、cd 持久化对 os.getcwd 的影响（518-528）、多段短路（902）、`_execute_py_direct/_py_suffixed/_py_pipe`（805-899）、超时杀树（644-646）、`kill_active` 中断路径（233：需配合真跑长命令并置 flag）。
  - 平台差异天然需要双平台 CI；本机（Windows）只能覆盖一半。
- 无法自动化（时序/终端交互）：
  - 真实 ESC 按键与"子进程偷吃控制台输入"的回归（stdin DEVNULL 修复 511-512、781-784）——需要真实终端；
  - `start /b` 分离孙进程导致管道不 EOF 的收尾时序（376-379）——依赖 Windows 进程/管道时序；
  - `proc.wait(timeout=5)` 抛 TimeoutExpired 的真实触发（杀树失败场景）。

---

### narnat_agent/tools/background/__init__.py（702行）

**职责**：Shell 工具 `background=true` / `bg=` 参数的后端——后台任务提交（固定 8 槽位 bg1~bg8）、输出转码落盘（含 50MB 截断与 1MB 尾部缓冲）、状态快照/等待/取消、槽位复用归档与会话清理。

**对外接口**（模块无 `__all__`，以下为模块内全部符号）：

- 常量：`MAX_SLOTS = 8`（48）、`MAX_FILE_BYTES = 50 * 1024 * 1024`（49）、`TAIL_BYTES = 1 * 1024 * 1024`（50）、`DRAIN_GRACE = 0.3`（51）、`TAIL_SUFFIX = ".tail"`（52）、`BG_DIR_PREFIX = "narnat_bg_"`（54）、`BG_STALE_SECONDS = 7 * 24 * 3600`（55）、`STATUS_RUNNING/DONE/FAILED/CANCELLED = "running"/"done"/"failed"/"cancelled"`（57-60）。
- `def bg_execute(command, timeout, background, bg, task_id, tool_context) -> str:`（665）：Shell 工具 bg 参数分发入口（被 `bash.execute` 转发，bash:495-496）。
- `def submit(command: str) -> str:`（218）：提交后台任务，立即返回 `bgN 已提交（后台运行）\n结果: {log_path}\n查状态…` 文本。
- `def snapshot() -> str:`（555）：状态快照文本（转调 `_snapshot`）。
- `def wait_tasks(timeout: int) -> str:`（559）：等待任意任务完成/超时/ESC 打断，返回文案+完整快照。
- `def cancel_task(bg_id: int) -> str:`（598）：取消指定任务（杀进程树，保留已产出内容）。
- `def running_count() -> int:`（620）：运行中任务数。
- `def running_summary() -> str:`（625）：运行中任务的短摘要（`bgN(cmd40)` 用顿号连接）；无则空串。
- `def cleanup_all() -> None:`（634）：会话结束硬兜底（杀全部 running + 清目录 + 重建槽位表）。
- `def prepare() -> None:`（656）：会话开始预清（清空上次残留）。
- 类/内部符号：`@dataclass class _Slot`（131-150）、`class _StreamDecoder`（332-401，含 `feed(data)`/`flush()`）、`_root_dir()`（66）、`_bg_dir()`（74）、`_sweep_stale()`（78）、`_has_fresh_file(p, now)`（116）、`_ensure_dir()`（160）、`_wipe_dir()`（167）、`_archive_old(slot, min_bytes)`（174）、`_fmt_bytes(n)`（201）、`_fmt_dur(sec)`（209）、`_pump(slot, stream, gen)`（404）、`_monitor(slot, proc, gen)`（436）、`_finalize(slot, proc, gen, rc)`（456）、`_append_output(slot, data)`（490）、`_file_write(slot, data)`（507）、`_append_tail(slot, data)`（515）、`_snapshot()`（528）。

**依赖**：

- `..exec_signal`（45行）：`error_line`
- `..bash`（**函数内延迟导入**）：`BashRuntime`（226 submit、565 wait_tasks）、`_kill_proc_tree`（600 cancel_task、636 cleanup_all）
- 标准库：codecs/os/shutil/subprocess/sys/tempfile/threading/time/dataclasses/typing（34-43）

**被依赖**（Grep 复验）：

- `narnat_agent/tools/bash/__init__.py:495`：`from ..background import bg_execute`
- `narnat_agent/tools/registry.py:16`：`from . import background`（显式导入，注释：确保 Nuitka 打包）
- `narnat_agent/core/agent.py:41`、`:198`：`from ..tools.background import prepare as _bg_prepare`（调用 42、199——会话启动预清）；`:180`、`:271`：`from ..tools.background import cleanup_all as _bg_cleanup`（调用 183、274——finally 硬清理）
- `narnat_agent/core/agent_loop.py:15`：`from ..tools.background import running_count as _bg_running_count`（用 393）；`:16`：`from ..tools.background import running_summary as _bg_running_summary`（用 372）
- `narnat_agent/tools/goal_complete/__init__.py:59`：`from ..background import cleanup_all`（调用 60；包裹在 try/except 中 58-62）
- 非生产代码：`tool_exp/verify_bg_isolation.py:30/39`（直接 `submit`/`wait_tasks`/`prepare`）、`tool_exp/verify_shell_three_fixes.py:108`（`_StreamDecoder`）、`:135`（`snapshot`）

**状态**：

- 模块级全局（全部可变）：
  - `_base_dir: Optional[str] = None`（63）：首次 `_root_dir()` 时 mkdtemp 并调用 `_sweep_stale()`（66-71）；此后只读（74-75、160-163、167-171）。
  - `_slots: List[_Slot]`（154，初始化 bg1~bg8）：`cleanup_all` 整体重建（637、650）；`submit` 在锁内改字段（228-257）；`_pump/_monitor/_finalize` 在锁内改字段（419-433、444-453、457-472）。
  - `_lock = threading.Lock()`（155）：保护槽位表与文件写入（写入点 228、274、303、419、428、431、444、457、479、531、567、582、601、621、627、639）。
  - `_terminal_event = threading.Event()`（156）：`_finalize` 置位（470）；`wait_tasks` clear（570）与 wait（580）。
  - `_archive_seq = 0`（157）：`_archive_old` 递增（189-191）。
- 类变量：`_Slot` 为 dataclass，无类级可变状态；`_StreamDecoder` 无类级状态（`_candidates`/`_is_incomplete` 为 staticmethod，346-354）。
- 实例状态：`_Slot` 字段（131-150）：`id`、`command`、`proc`、`status`（空串=空闲）、`exit_code`、`started_at`、`finished_at`、`file_bytes`、`total_bytes`、`truncated`、`log_path`、`fh`、`tail_chunks`、`tail_bytes`、`readers_left`、`cancel_flag`、`generation`。

**行为要点**（编号-行号）：

1. 结果目录：首次使用创建系统临时目录下 `narnat_bg_<随机>`（`tempfile.mkdtemp(prefix=BG_DIR_PREFIX)`，69），并在创建时执行 `_sweep_stale()`（70）；`_ensure_dir` 用 `makedirs(exist_ok=True)`（160-164）。目录与会话/进程绑定，AI 的 cd 不影响结果路径（62 注释、96-101 的证据：路径绝对化）。
2. 过期清扫（78-113）：遍历 `tempfile.gettempdir()` 下 `narnat_bg_*`，三道防线——目录 mtime ≤7天跳过（98-99）；目录内有任一文件 mtime ≤7天跳过（100-101、`_has_fresh_file` 116-128）；判定过期后先 `os.replace(p, p+".stale")`（102-108，Windows 下目录内有打开句柄则 rename 失败 → 保留），成功才 `rmtree(ignore_errors=True)`（109）。所有 OSError 一律放弃（110-113）。注释明示 Linux 无句柄锁，"本防线不生效——尽力而为"（85-86）。
3. 槽位分配（228-257）：自由槽 = `status != "running"`（229，即"终态即释放"、"空槽也可用"）；全忙时返回错误并列出 running 列表（230-237）；`free.sort(key=lambda s: s.finished_at if s.status else -1.0)`（239）→ 空闲槽（`finished_at=0` 但先按 `-1.0` 排最前）优先、终态槽按完成时间升序（最旧优先）。
4. 占位与代次（241-257）：锁内 `slot.generation += 1`、`command=command.strip()`、`status="running"`、清空 exit_code/file_bytes/total_bytes/truncated/tail/readers_left/cancel_flag、记录 `started_at`；`gen` 快照保存（244-257）。占位先于起进程，杜绝并发重复分配，并让旧任务的 monitor/泵线程立即失效（241-242 注释）。
5. 复用归档（259-262、`_archive_old` 174-198）：`prev_total = slot.total_bytes`（占位前快照，243）为 0 或槽位无 status/log_path 时不动（181-182）；旧主文件存在且非空才归档（186-188）；`_archive_seq += 1`（189-190）→ `os.replace(old_main, f"{old_main}.{seq}.prev")`，同名 `.tail` 一并改名（191-194）；归档后 `slot.log_path` 指向归档文件，随后被调用方重置为新路径（195、262）。归档 OSError → 返回 ""（197-198）。
6. 任务日志头（263-272）：打开 `bgN.log`（wb）后立即写 `# bgN 提交于 HH:MM:SS | 命令: {command}\n`（UTF-8）并 flush——AI 读日志即可识别归属（265-267 注释）。打开失败 → 清 status 并返回 `error_line("无法写入结果文件: {e}")`（273-276）。
7. 进程启动（278-301）：Windows `Popen(slot.command, shell=True, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, cwd=os.getcwd(), env=BashRuntime.utf8_env)`；Unix `[sh, "-c", command]`（sh = bash 优先）、`start_new_session=True`；找不到 shell 时 `raise OSError("未找到shell，请安装bash或sh后重试")`（290-292）。启动失败 → 清 status、关句柄、返回 `error_line("后台任务启动失败: {e}")`（302-309）。
8. 启动成功（311-317）：`slot.proc = proc`、`slot.readers_left = 2`，为 stdout/stderr 各起一个 `_pump(slot, stream, gen)`，再起一个 `_monitor(slot, proc, gen)`，全部 daemon。
9. 提交返回文本（318-325）：`bgN 已提交（后台运行）\n结果: {log_path}\n查状态: Shell(bg="status") · 等待完成: Shell(bg="wait")`；有归档时追加 `\n提示: 已归档该槽位旧任务结果到 {archived}，仍可 Read 读取`。
10. 泵线程（404-433）：循环 `stream.read(4096)`；每次写盘前在锁内校验 `slot.generation != gen` → 立即 return（不写文件、不递减 readers_left，420-421 注释）；EOF 后 `decoder.flush()` 收尾（426-430）；finally 中代次相符才 `readers_left = max(0, readers_left-1)`（431-433）。
11. 增量解码（332-401）：锁定编码前把累计缓冲整体尝试 `utf-8`（Windows 再 `gbk`）（365-373）；纯 ASCII 直接输出（361-364）；全部候选失败时区分"结尾不完整"（`_is_incomplete`，350-354）→ 等待下一块（374-380），否则 `utf-8 replace` 兜底并锁定（381-384）；锁定后交给增量解码器（358-359）。`flush` 吐出残留（386-401）。**平台差异**：非 Windows 仅 utf-8 候选（348）。
12. 监控线程（436-453）：`proc.wait()`（异常时 rc=-1，438-441）→ 在 0.3s 宽限内轮询 `readers_left == 0`（442-452，20ms 步进）→ `_finalize`。宽限理由：孙进程继承管道不 EOF（446-451 注释）。
13. 终态判定（456-489）：锁内先校验代次（458-459）；`exit_code = rc`；`cancel_flag` → cancelled，否则 rc==0 → done，否则 failed（461-466）；`finished_at`；`truncated` 时拼接 tail_chunks（468）；摘除 fh 引用（469）；`_terminal_event.set()`（470）。出锁关 fh（473-477）；tail 在代次仍相符时写 `log_path + ".tail"`（478-487，OSError 忽略）。
14. 落盘策略（490-521）：`total_bytes` 累计全部输出；主文件写入到 `MAX_FILE_BYTES`（50MB），溢出的那一块先写 `data[:room]` 再置 truncated 并把剩余转入尾部缓冲（493-504）；尾部缓冲滚动保留**最新** `TAIL_BYTES`（1MB），`while tail_bytes > TAIL_BYTES and len(chunks) > 1` 丢最老块（515-521）；`_file_write` 每块 `flush()`（510）——运行中即可 Read 增量。
15. 状态快照（528-552）：仅列出 `status` 非空的槽；命令截 40 字符加 `…`（534）；运行中 `bgN 运行中 已运行MM:SS`（536）；完成 `bgN 完成 exit=N (HH:MM:SS)`；失败 `bgN 失败 exit=N (HH:MM:SS)`；取消 `bgN 已取消`（537-542）；行尾 `输出{size}`（543），truncated 时加 `（超限截断，尾部见 {log_path}.tail）`（544-545）；下一行 `    结果: {log_path}`（546）。无任务 → `[后台任务] 当前无后台任务`（549-550）；否则尾部附加 `空闲槽位: bg1, bg2…`（551-552）。
16. 等待语义（559-595）：入口清零 `BashRuntime.interrupted`（566）；锁内 `_terminal_event.clear()` 并与运行中列表采样原子化（567-571）；无运行中任务 → 立即返回 `[后台任务] 当前无运行中的后台任务\n` + 快照（572-573）；循环内先查 `BashRuntime.interrupted`（打断 → 返回 `[后台任务] 等待已被用户打断（后台任务不受影响，继续运行）\n` + 快照，577-579），再 `_terminal_event.wait(0.1)`（580-581）；事件触发或超时后收集 `before_running` 中已终态的任务（582-587）；有 done/failed → `[后台任务] bgN 完成/失败 exit=N；…\n` + 快照（588-594）；否则 `[后台任务] 等待{timeout}秒超时，当前无新完成\n` + 快照（595）。
17. 取消语义（598-617）：编号无效 → `error_line("bgN 编号无效（有效范围 bg1~bg8）")`（603-604）；空闲 → `error_line("bgN 空闲（无任务）")`（605-606）；已终态 → `error_line("bgN 已处于终态（{status}），无需取消")`（607-608）；合法则置 `cancel_flag = True` 并取 proc/log_path 快照（609-611），进程仍活时后台线程杀树（612-613），返回 `bgN 已取消（进程树已终止，已产出内容保留在 {log_path} 仍可 Read 读取；管道中尚未收口的剩余输出随后补落盘）`（614-617）。
18. 计数与摘要（620-631）：均为锁内实时统计；摘要格式 `bgN({command[:40]})` 用 `、` 连接，空则 `""`（625-631）。
19. 会话结束硬兜底（634-653）：锁内收集 running 且存活的 proc、关闭全部 fh（644-648），重建 `_slots`（650，注释：旧 monitor 写旧对象，不污染新表），出锁对每个 proc 起后台线程杀树（651-652），`_wipe_dir()`（653）。幂等（635 注释）。
20. 会话开始预清（656-658）：仅 `_wipe_dir()`（不杀进程）。
21. bg 参数路由（665-702）：`op = (bg or "").strip().lower()`；`background=True` 时 command 空 → `error_line("background=true 提交后台任务需提供 command")`，否则 `submit(command)`（677-680）；`status`→`snapshot()`（681-682）；`wait`→`t = timeout if timeout else 120`、int 校验、`t<=0` 报错、`tool_context.max_timeout_seconds` clamp（683-693）；`cancel`→id 缺失/非整数报错，否则 `cancel_task(tid)`（694-701）；其它 → `error_line(f"未知 bg 操作: {bg or ''}（可用: status / wait / cancel）")`（702）。

**边界/异常行为**（编号-行号）：

1. 并发上限 8：第 9 个提交返回 `后台并发已达上限8(bg1~bg8)，当前运行: bg1, …。请用 Shell(bg="wait") 等待完成，或 Shell(bg="cancel", id=N) 取消不再需要的任务`（230-237）。
2. `submit` 的 `command.strip()`：首尾空白被剥离后落盘与执行（245）；空 command 由 `bg_execute` 拦截（678-679），但直接调 `submit("")` 不被拦截（218；`bg_execute` 是唯一生产调用方）。
3. 代次防串扰覆盖"占位与 proc 赋值之间"的竞态窗口（241-242、311 注释；实现见 419-421、431-433、444-446、457-459、479-481）。
4. 旧任务 tail 归属：`_finalize` 写 tail 前重查代次，不符则放弃旧尾部（479-481，注释：主文件已归档）。
5. `readers_left` 计数与 monitor 宽限的交互：monitor 等"主进程退出 + 0.3s 宽限"；分离孙进程持有管道时，宽限后强制收口，泵线程可能仍在阻塞（446-451 注释）；此后落盘仍会继续（`_pump` 每块都过代次校验），但状态已终态。
6. 50MB 截断：`file_bytes ≤ MAX_FILE_BYTES` 由 `room` 计算保证（494-501）；`total_bytes` 仍统计全部（492）；截断后状态快照含 `.tail` 提示（544-545）。未验证 50MB 边界在真实大输出下的行为。
7. 尾部缓冲上界：单块大于 1MB 时 `while … len(chunks) > 1` 保证至少保留最后一块，最终 tail 可能超过 1MB（519-521）。
8. `_file_write` 失败（句柄已关/磁盘问题）被 `except (OSError, ValueError): pass` 吞（511-512）——数据静默丢失，仅 file_bytes 已累加。
9. `_archive_old` 的 `min_bytes <= 0` 短路（181）：仅当旧任务 `total_bytes==0`（如上次 Popen 失败）时不归档，即使日志内有信息头也不留档（186-188 再兜一层"主文件 >0 字节"）。
10. 归档命名：`bgN.log.<seq>.prev`、`bgN.log.<seq>.prev.tail`（191-194）；`_archive_seq` 进程内递增（会话内），跨进程可能重名——但目录按进程唯一（69），故无冲突。
11. `wait_tasks` 中"取消"导致的唤醒不进 events 文案（588-592 只收 done/failed）——cancelled 任务会让 wait 提前醒来却报 `等待{timeout}秒超时，当前无新完成`（595）。行为不一致（见补丁痕迹）。
12. `wait_tasks(timeout)` 的 countdown：无论是否被事件提前唤醒，未发现新终态时都报 `等待{timeout}秒超时`（595）——即使实际远未等满。
13. `wait_tasks` 的无运行任务短路：`[后台任务] 当前无运行中的后台任务`（573）。注意已终态任务不会导致等待。
14. `_terminal_event` 是全局共享事件：任何任务终态置位（470）都会让所有等待者醒来；醒来后若无可报告项则走超时文案（580-595）。
15. 取消后 `status` 依赖 `_finalize` 落 `cancelled`（461-462）——取消返回文本时状态可能仍是 running（快照时序）。
16. 编号越界：`cancel_task` 对 `bg_id` 不在 1..8 返回 error_line（602-604）；`bg_execute` 对非整数 id 返回 `error_line(f"id 需为整数: {task_id}")`（697-700）。
17. 平台分支：Windows 无 `start_new_session`（279-288），Unix `start_new_session=True`（293-301），杀树依赖 `_kill_proc_tree` 的平台实现（bash:351-370）。
18. 清理的 `_wipe_dir` 用 `rmtree(ignore_errors=True)`（167-171）——Windows 上若仍有打开句柄，删除可能失败/残留（未验证），`prepare()` 也有同样限制（656-658）。
19. `_root_dir` 的 mkdtemp+清扫只发生一次；若清扫抛 OSError 被外层吞（90-113），不影响使用。
20. `bg_execute` 的 `tool_context` clamp：`tool_context.max_timeout_seconds > 0` 时 `t = min(t, …)`（691-692），与前台 timeout 的 clamp 行为一致（bash:507-508）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【高】跨模块私有依赖：`from ..bash import BashRuntime`（226、565）与 `from ..bash import _kill_proc_tree`（600、636）——后台模块直接使用 bash 的私有实现与状态容器；两个模块实为强耦合整体。
2. 【高】模块级单例 + 整体重建式隔离：`_slots`（154）在 `cleanup_all` 中重建（650）；`generation` 代次（150、244）是为"槽位复用串扰"打的竞态补丁（241-242、446 注释）；`_terminal_event` 全局事件（156）配合 `clear/set` 手工同步。
3. 【高】全局可变状态清单长：`_base_dir`/`_slots`/`_terminal_event`/`_archive_seq`（63、154-157）均模块级可变，无实例封装（对比 bash 已收敛成 BashRuntime）。
4. 【中】wait 语义与取消语义不一致：cancelled 唤醒被报告为"超时无新完成"（588-595）——历史补丁叠加（先后加入 done/failed 事件文案与 cancelled 状态）留下缺口。
5. 【中】吞异常密集：`_pump` 的 `except Exception: pass`（423-424）、`_file_write`（511-512）、`_finalize` 关句柄/tail 写（474-477、486-487）、`_archive_old`（197-198）、`_sweep_stale` 双层 OSError 吞（110-113）、`_monitor` 的 `except Exception: rc = -1`（440-441）。
6. 【中】魔法值散落：40（命令截断，534、631）、0.1（轮询，580）、0.02（20ms，452）、4096（读块，413）——第 534/631 行的 40 重复出现。
7. 【中】`_Slot` 的 `proc`/`fh` 字段无类型注解（136、145），dataclass 失去静态检查；`status: str = ""` 承担"空闲/占用"双重语义（133、229、532、605）。
8. 【中】注释即补丁日志："（P2修复）"（510）、"沿用 bash 模块 _drain_readers 的教训"（18）、"（旧缺陷：GBK 字节直通与 UTF-8 头部混编…）"（27-28）——修复背景散落在文档字符串与行内注释。
9. 【低】`_has_fresh_file` 只查目录内文件 mtime，不递归子目录（116-128）——与 `_sweep_stale` 的语义匹配现有目录结构。
10. 【低】`submit` 成功后返回值拼装分散（318-325），提示文本与 DEFINITION 描述（bash:201-212）重复描述同一语义。
11. 【低】`_snapshot` 对 `cancelled` 的行头固定为 `bgN 已取消`（542），不带时间/exit——与 done/failed 的信息密度不一致。

**可测性**：

- 可独立单测（无进程、无网络）：
  - `_fmt_bytes`（201）、`_fmt_dur`（209）：纯格式化。
  - `_StreamDecoder`（332-401）：喂任意分块字节（UTF-8 跨块、GBK、乱码、ASCII）断言输出与 flush——**高价值，且无需子进程**。
  - `_append_output`/`_append_tail`/`_file_write`（490-521）+ 假 `_Slot` 与临时文件：可断言 50MB 截断、1MB 尾部滚动、flush 行为（用减小后的常量注入或 monkeypatch MAX_FILE_BYTES/TAIL_BYTES）。
  - `_archive_old`（174）：构造真实文件 + 假槽位，断言命名与 `.tail` 一同归档。
  - `_snapshot`（528）：构造槽位状态矩阵（空/running/done/failed/cancelled/truncated/空闲）断言文本。
  - `bg_execute`（665）的路由层：monkeypatch `submit`/`snapshot`/`wait_tasks`/`cancel_task` 断言参数与错误分支；wait 的 timeout 语义与 clamp 可纯测。
  - `_sweep_stale`（78）：构造临时目录 + 伪造 mtime（`os.utime`）断言三道防线；改名防线在 Windows 需真实句柄（见下）。
- 需要集成测试（真进程/真文件系统）：
  - `submit`（218）全链路：进程启动、日志头落盘、返回值、快照可见性。
  - `_pump`/`_monitor`/`_finalize` 生命周期与代次防串扰（复用槽位时旧线程行为）。
  - 50MB 截断+`.tail` 落盘（可用 monkeypatch 缩小常量以减少 IO）。
  - `cancel_task`（598）真杀进程树；`cleanup_all`（634）幂等与目录清理。
  - `wait_tasks`（559）事件/超时/ESC 三路径（BashRuntime.interrupted 需真实赋值，或直接设置类属性）。
- 无法自动化（时序/终端交互/平台语义）：
  - `start /b` 分离孙进程→管道不 EOF 的宽限收口（446-452）——Windows 管道/进程时序。
  - `_sweep_stale` 防线3 的"Windows 句柄占用致 rename 失败"（102-108）——需真实打开句柄的跨进程场景。
  - 多 agent 会话并发抢目录的真实隔离（注释 7）——需多进程并发环境。

---

### narnat_agent/tools/exec_signal.py（83行）

**职责**：框架与判定侧共用的退出码/错误"带随机标签"文本协议——生成带不可伪造标签的退出码行与错误行，并提供判定（has_error/parse_rc）、剥离（strip_tags）与截断切点吸附（safe_cut_points）。

**对外接口**：

- `def rc_line(rc: int) -> str:`（29）：生成 `[exit code: {rc}] [{_TAG}]`（31）
- `def error_line(msg: str) -> str:`（34）：生成 `[错误: {msg}] [{_ERR_TAG}]`（38）
- `def tag_error(text: str) -> str:`（41）：给任意文本追加 ` [{_ERR_TAG}]`（44）
- `def has_error(result: str) -> bool:`（47）：结果是否含错误标签（唯一判据：`f"[{_ERR_TAG}]" in result`，49）
- `def parse_rc(result: str) -> Optional[int]:`（52）：取最后一个带标签退出码行（正则 `_RC_LINE_RE`，54-55）；无则 None
- `def strip_tags(result: str) -> str:`（58）：剥离两类标签（先剥 `_TAG`，再剥 `_ERR_TAG`，60）
- `def safe_cut_points(text: str, head_pos: int, tail_pos: int) -> Tuple[int, int]:`（63）：截断切点吸附，保证标签不被切开（72-83）

**依赖**：无内部依赖（仅标准库 `re`、`uuid`、`typing.Optional/Tuple`，13-15）。

**被依赖**（Grep 复验；`exec_signal` 全仓库 import 列表）：

- `narnat_agent/tools/bash/__init__.py:16`：`from ..exec_signal import rc_line, error_line, tag_error, safe_cut_points`
- `narnat_agent/tools/background/__init__.py:45`：`from ..exec_signal import error_line`
- `narnat_agent/tools/registry.py:11`：`from .exec_signal import error_line`（用点 124、154、156）
- `narnat_agent/core/tool_dispatcher.py:17`：`from ..tools.exec_signal import has_error, strip_tags`（判定 246、248-249）
- `narnat_agent/core/agent_loop.py:21`：`from ..tools.exec_signal import strip_tags`（用点 455）
- `narnat_agent/mcp/__init__.py:17`：`from ..tools.exec_signal import error_line`（用点 342）
- `narnat_agent/mcp/client.py:25`：`from ..tools.exec_signal import error_line`（用点 160）
- `narnat_agent/tools/mcp_tool/__init__.py:8`：`from ..exec_signal import error_line`（用点 60-108 多处）
- `narnat_agent/tools/terminal/__init__.py:22`：`from ..exec_signal import error_line`
- `narnat_agent/tools/terminal/ssh_session.py:38`：`from ..exec_signal import rc_line, error_line, safe_cut_points`
- 非生产代码：`tool_exp/test_error_tag.py:12`、`tool_exp/repro_ui_fail_judge.py:12`、`tool_exp/verify_review_fixes.py:26`、`tool_exp/verify_shell_three_fixes.py:32`、`tool_exp/verify_ui_fail_gitdiff.py:85`、`tool_exp/zz_check_truncate_regression.py:12`、`tool_exp/zz_rollback_final.py:14`、`tool_exp/mcp_test/test_mcp.py:107/130`、`tool_exp/mcp_test/test_mcp_runtime.py:189`
- **注意**：`parse_rc` 在 `narnat_agent/` 内**无任何调用点**（全仓库 `parse_rc(` 仅命中原文件定义与 tool_exp 测试）——见补丁痕迹。

**状态**：

- 模块级全局（导入时一次性生成/编译，之后只读）：
  - `_TAG = uuid.uuid4().hex[:8]`（18）：进程级随机退出码标签。
  - `_ERR_TAG = uuid.uuid4().hex[:8]`（22）：进程级随机错误标签（独立取值）。
  - `_RC_LINE_RE`（24）、`_TAG_RE = re.compile(r" ?\[" + _TAG + r"\]")`（25）、`_ERR_TAG_RE = re.compile(r" ?\[" + _ERR_TAG + r"\]")`（26）。
- 类变量：无。实例状态：无（纯函数模块）。

**行为要点**（可观察行为；编号-行号）：

1. 两个标签都是**进程级随机**（uuid4 前 8 个 hex 字符，18、22），在进程启动时生成，之后不再变化——同一进程内所有退出码行/错误行共享同值（docstring 9-10）。
2. `rc_line` 输出格式固定为 `[exit code: {rc}] [{_TAG}]`（31），其中 `{rc}` 可为负数（`-?\d+` 在正则中支持，24；测试 `tool_exp/verify_review_fixes.py:47` 印证负码用途）。
3. `error_line` 输出 `[错误: {msg}] [{_ERR_TAG}]`（38）；`tag_error` 输出 `{text} [{_ERR_TAG}]`（44）——两者仅供框架使用，命令输出无法预知标签值（docstring 4-7、20-21）。
4. `has_error` 判据是**子串包含** `[{_ERR_TAG}]`（49）——不带正则、不检查位置。
5. `parse_rc` 用正则匹配 `\[exit code: (-?\d+)\] \[{_TAG}\]`（24、54），取**最后一个**匹配（推导"整体退出码"，docstring 9-10）；无匹配返回 None（55）。
6. `strip_tags` 先剥退出码标签再剥错误标签（60），把 `" [TAG]"`（含前导可选空格，25-26）一并移除——AI 看到的文本与"无标签"版本一致（docstring 6-7）。剥离后 AI 仍能看到 `[exit code: N]` 与 `[错误: msg]`/`[超时: …]` 的**文本本身**（仅随机标签消失）。
7. `safe_cut_points`（63-83）：收集两类标签的 span 并排序（72-74）；头部切点若落在任一标签内部 → 吸附到标签结尾（75-77，标签留在头部侧）；尾部切点若落在标签内部 → 吸附到标签开头（78-80，标签留在尾部侧）；吸附后若 `head_pos > tail_pos` → `head_pos = tail_pos`（81-82，退化场景保两侧都不切开标签）。
8. 使用链路（跨模块、可观察）：工具层生成带标签文本（如 bash 的 rc_line/tag_error、background/terminal/registry 的 error_line）→ `core/tool_dispatcher.py` 在交给 LLM 前用 `has_error` 判定 UI 失败显示（246）、再 `strip_tags`（249）→ `core/agent_loop.py:455` 在删除确认重执行路径同样 strip。
9. 标签是"防伪造"机制：命令自身的输出里出现 `[exit code: 0]` 或 `[错误: …]` 文本不会被识别（缺随机标签）。

**边界/异常行为**（编号-行号）：

1. 标签仅在**进程生命周期内**稳定：与旧会话日志比对时标签不同（跨进程不可复用）；单进程多会话共享同一标签（18、22 在导入时生成）。
2. `has_error` 与 `parse_rc` 实现风格不一致：子串 vs 正则（49 vs 24/54）。若标签被截断，`has_error` 与 `parse_rc` 同时失效（子串/正则都匹配不上）。
3. 截断场景的防护依赖"调用方主动调用 `safe_cut_points`"：bash 的 `_truncate_output` 已接（bash:399）；但 `tools/registry.py:139-150` 的**全局输出硬截断**直接按字符切（`llm_result[:head]` + `[-tail:]`），**未做标签吸附**——若标签恰落在切点，会泄漏残缺标签并让判定失效。未验证实际触发频率（标签仅 12 字符宽，概率低但存在）。
4. `strip_tags` 的前导空格处理（`" ?"`，25-26）：标签前恰有一个空格会被一起剥掉；标签前有多于一个空格时会残留多余空格（`"  [TAG]"` 只剥 `" [TAG]"`）——属可观察的文本细节。
5. 标签长度 8 hex 字符（32 bit 熵）：碰撞/猜测概率极低，但非密码学强度（18、22 注释仅声明"命令无法预知/伪造"）。
6. 无标签输入：`strip_tags` 原样返回；`has_error` 返回 False；`parse_rc` 返回 None（49、55、60）。
7. `parse_rc` 对多个退出码行取最后一行（多段命令输出"各失败段+末尾总码"，docstring 9-10；bash 多段实现 970-979、1085-1086）。
8. `safe_cut_points` 的 `head_pos`/`tail_pos` 参数不校验取值范围（63）；越界输入按不等式吸附逻辑处理（如 tail_pos 超出文本长度时无 span 覆盖 → 原值返回）。
9. 同一进程内 `_TAG` 与 `_ERR_TAG` 独立生成（18、22），理论上可能出现相同值（概率 2^-32，未验证/未防护）。

**补丁痕迹**（编号；附行号+证据；严重度）：

1. 【中】双标签机制本身是叠补丁：`_TAG`（18）与 `_ERR_TAG`（22）注释显示错误标签是为"UI 判定失败"后加的（19-21），两种标签两套正则（24-26）并行维护。
2. 【中】判定接口实现风格不统一：`has_error` 子串（49）vs `parse_rc` 正则（52-55）；且截断安全性依赖外部调用 `safe_cut_points`（bash:399 接了，tools/registry.py:139-150 未接）——协议完整性靠各调用点自律。
3. 【中】`parse_rc` 无生产调用者（本次 grep 全仓库确认）：疑为旧 UI 判定路径的遗留 API，现仅测试脚本引用——死代码嫌疑（保留成本：协议面扩大）。
4. 【低】标签熵与格式为硬编码魔法值：`[:8]`（18、22）、`" ?"`（25、26）——协议参数不可配置。
5. 【低】模块 docstring 以"历次事故"叙事（3-7 行"仅靠文本识别会把成功命令误报为失败…"），实现与说明混排。
6. 【低】`strip_tags` 的两次正则替换串行（60），顺序若颠倒（先剥 ERR 再剥 TAG）在正常输入下等价，但两类标签值相同的极端情况下行为不同——无语义保护。

**可测性**：

- 可独立单测（纯函数，无外部依赖）——**本文件是全项目最易测的部分**：
  - `rc_line`/`error_line`/`tag_error`：断言格式与标签存在。
  - `has_error`：含/不含标签、被截断标签（应 False）的用例。
  - `parse_rc`：多行取最后、无标签行返回 None、负码、被截断标签返回 None。
  - `strip_tags`：剥离后与无标签文本一致、前导空格细节。
  - `safe_cut_points`：标签中点切、退化场景（head>tail）、多标签排序。
  - 注意：测试断言不能硬编码标签值（进程随机），须用 `rc_line(0)` 动态构造或直接引用 `_TAG`/`_ERR_TAG`（tool_exp/verify_review_fixes.py 即如此）。
- 需要集成测试的部分：
  - "截断 + 标签"跨模块不变量：`bash._truncate_output`（bash:388-405）与 `tools/registry.execute` 全局截断（registry:139-150）后 `has_error`/`parse_rc` 仍有效——**当前 registry 路径未吸附，建议立为规格项**。
  - `tool_dispatcher` 判定顺序（先 `has_error` 后 `strip_tags`，tool_dispatcher:246-249）——顺序敏感，需集成断言。
- 无法自动化的部分：无（本模块全部逻辑可自动化）。

---

## 总表

### 依赖关系矩阵（模块级 import 边，格式：A → B (行号)）

生产代码（`narnat_agent/`）：

| 边 | 行号 | 说明 |
|---|---|---|
| tools/bash → tools/exec_signal | bash:16 | rc_line/error_line/tag_error/safe_cut_points |
| tools/bash → tools/token_estimate | bash:17 | estimate_text_tokens |
| tools/bash → tools/background | bash:495（函数内延迟） | bg_execute |
| tools/background → tools/exec_signal | background:45 | error_line |
| tools/background → tools/bash | background:226、565、600、636（函数内延迟） | BashRuntime、_kill_proc_tree |
| tools/exec_signal → （无内部依赖） | — | 仅标准库 |
| tools/registry → tools/bash | registry:22 | execute、DEFINITION |
| tools/registry → tools/background | registry:16 | 显式导入（Nuitka 打包） |
| tools/registry → tools/exec_signal | registry:11 | error_line |
| core/tool_dispatcher → tools/bash | tool_dispatcher:12 | kill_active |
| core/tool_dispatcher → tools/exec_signal | tool_dispatcher:17 | has_error、strip_tags |
| core/agent_loop → tools/background | agent_loop:15、16 | running_count、running_summary |
| core/agent_loop → tools/exec_signal | agent_loop:21 | strip_tags |
| core/agent → tools/background | agent:41、180、198、271（函数内延迟） | prepare、cleanup_all |
| ui/interrupt → tools/bash | interrupt:38（函数内延迟） | kill_active |
| tools/goal_complete → tools/background | goal_complete:59（函数内延迟） | cleanup_all |
| mcp/__init__ → tools/exec_signal | mcp/__init__:17 | error_line |
| mcp/client → tools/exec_signal | client:25 | error_line |
| tools/mcp_tool → tools/exec_signal | mcp_tool:8 | error_line |
| tools/terminal → tools/exec_signal | terminal:22 | error_line |
| tools/terminal/ssh_session → tools/exec_signal | ssh_session:38 | rc_line、error_line、safe_cut_points |

非生产脚本（供参考，不构成契约）：`subagent_test/regress_fix.py:6`、`subagent_test/regress_fix3.py:6`、`tool_exp/verify_bg_isolation.py:30/39`、`tool_exp/verify_cd_fix.py:12/57`、`tool_exp/test_stress.py:18`、`tool_exp/test_py_direct.py:12`、`tool_exp/test_suffix.py:7`、`tool_exp/esc_steal_fix_verify.py:13`、`tool_exp/verify_shell_three_fixes.py:32-33/108/135`、`tool_exp/verify_ui_fail_gitdiff.py:13/85`、`tool_exp/verify_ui_judge_e2e.py:7`、`tool_exp/verify_review_fixes.py:26`、`tool_exp/repro_ui_fail_judge.py:12`、`tool_exp/test_error_tag.py:12`、`tool_exp/zz_check_truncate_regression.py:12`、`tool_exp/zz_rollback_final.py:14`、`tool_exp/mcp_test/test_mcp.py:107/130`、`tool_exp/mcp_test/test_mcp_runtime.py:189`、`tool_exp/_obsolete_20260910/*`（regression_test.py:142、regression_test_v2.py:101/112、verify_ux_round3.py:62）。

### 模块级可变状态全清单（含读写方）

| 状态 | 定义 | 写方（行号） | 读方（行号） | 跨模块可见性 |
|---|---|---|---|---|
| `BashRuntime.active_proc` | bash:55 | bash:601、695、1004（置）；685、751、1079（清） | bash:242 | 否（仅 bash 内部；kill_active 在其内） |
| `BashRuntime.interrupted` | bash:59 | bash:240、515、626、637、728、730、911、1032、1034；**background:566、577** | bash:635、728、1032；**background:577** | 是（被 tools/background 读写） |
| `BashRuntime.utf8_env` | bash:50 | 导入时构建一次（50-52） | bash:592、793、822、879、995；**background:287、300** | 是（被 background 读） |
| `BashRuntime.active_proc_lock` | bash:56 | —（锁对象） | bash:241、600、684、694、750、1003、1078 | 否 |
| 进程 CWD（`os.getcwd`） | 进程级 | bash:524、568、933（os.chdir） | bash:410、590、792、821、878、993；全项目工具 | 是（全局副作用） |
| `background._base_dir` | background:63 | 68-69（首次） | 74-75、160-163、167-171 | 否（模块私有） |
| `background._slots` | background:154 | 637、650（重建）；228-257（submit）；444-453（monitor）；457-472（finalize）；419-433（pump） | 229-234、532-548、571、582-587、602-611、621-628、640-648 | 否 |
| `background._lock` | background:155 | —（锁对象） | 228、274、303、419、428、431、444、457、479、531、567、582、601、621、627、639 | 否 |
| `background._terminal_event` | background:156 | 470（set）、570（clear） | 580（wait） | 否 |
| `background._archive_seq` | background:157 | 189-191 | 191 | 否 |
| `_Slot` 实例字段（16 项） | background:131-150 | submit/pump/monitor/finalize/cleanup_all | 各处 | 否 |
| `exec_signal._TAG` / `_ERR_TAG` | exec_signal:18、22 | 导入时生成一次 | 24-26、31、38、44、49 | 是（值只被本模块正则/函数使用，但格式被 bash/terminal/registry 等依赖） |

### 补丁痕迹 TOP10（按严重度排序，含文件:行号）

1. 【高】bash 与 background 互为私有实现依赖（`_kill_proc_tree`、`BashRuntime`）：bash/__init__.py:351 被 background:600、636 引用；BashRuntime 被 background:226、565 引用——两模块必须成对重构。
2. 【高】bash 执行循环三处复制 + 结果组装四处重复：bash/__init__.py:603-682、688-751、1006-1079；653-673、744-777、1048-1061。
3. 【高】可变全局跨层共享（`BashRuntime.interrupted`/`active_proc`）：bash:59、240、635 与 background:566、577；并发/时序正确性依赖"入口清零"惯例。
4. 【高】`execute()` 单函数 263 行承载 5 类职责，Windows/Unix 双实现并行：bash/__init__.py:423-685。
5. 【高】background 模块级单例 + 代次/重建补丁（对竞态的历史修复）：background:150、244、457-459、637、650。
6. 【中】协议完整性靠调用点自律：`safe_cut_points` 被 bash:399 使用，但 tools/registry.py:139-150 的全局硬截断未吸附标签；`has_error` 子串判定（exec_signal:49）与 `parse_rc` 正则（exec_signal:52-55）风格不一。
7. 【中】wait 语义缺口：cancelled 唤醒被报告为"超时无新完成"（background:588-595）。
8. 【中】魔法值与常量重复：`"__AWAIT_CONFIRM__"` 字面量（bash:491）vs `tool_context.AWAIT_CONFIRM`（tool_context:12）；background:534/631 的 40、452/580 的轮询间隔。
9. 【中】过时文档与实现不符：bash:436-437 docstring（"持久化cmd会话，命令直写stdin"）vs 实现 780-802；`parse_rc` 无生产调用者（exec_signal:52）。
10. 【中】吞异常密集：bash:614-615、708-709、1017-1018、363-364、891-894；background:423-424、486-487、511-512、197-198、110-113。
11. 【低】历史包袱外部证据：tool_exp 旧脚本引用已删除的 `_has_nonpersistent_cd`（tool_exp/verify_cd_fix.py:12、tool_exp/_obsolete_20260910/regression_test_v2.py:101）——本次核查 `narnat_agent/` 内无该符号。

### 本组对外契约清单（被本组之外模块依赖的 public API，即新架构必须保持的行为面）

A. Shell 工具面（LLM 可见契约）
1. 工具名 `"Shell"` 与 DEFINITION 结构（tools/registry.py:22、42、57-61）：参数 command/timeout/max_output_chars/background/bg/id，`required: []`（bash:180-227）。
2. `Shell.execute(...)` 的关键字参数名与默认值（bash:423-432；registry 以 `impl(**arguments, _tool_context=...)` 调用，registry:127-130）：含 `max_output_tokens` 别名、`_tool_context` 注入。
3. 返回文本格式（AI/UI 双方依赖）：退出码行 `[exit code: N]`、`[stderr]\n` 前缀、`[用户中断]`、`[超时: 命令执行超过N秒，已终止]`、`[跳过: 前一命令失败(退出码N)] {seg}`、`[跳过: 前一命令成功] {seg}`、`cd: {e}`、结尾提示符（bash:522-527、653-682、919-924、936、1081-1086）。
4. 中断/超时语义：命令被终止 + 输出保留 + 提示符尾随（bash:641-673）。
5. cd 持久化语义（单独 cd 改变后续调用的当前目录；无参数 cd：cmd 显示当前目录 / bash 回 home）（bash:518-528、562-571）。
6. 安全确认协议：`"__AWAIT_CONFIRM__"` 返回值 + `tc.pending_delete` 字典形状（bash:483-491，被 core/agent_loop.py:14、242-246 消费）+ Windows 回调路径（bash:476）。

B. 打断面
7. `bash.kill_active()`（tool_dispatcher:12→138/154/177/203/285/309；ui/interrupt:38→42）：ESC 后必须置位中断标志并无阻塞地杀前台进程树。

C. 后台任务面
8. `background.bg_execute(command, timeout, background, bg, task_id, tool_context)`（bash:495-496 唯一转发入口）。
9. `background.submit(command) -> str` / `snapshot()` / `wait_tasks(timeout)` / `cancel_task(bg_id)`：返回文案被 AI 直接消费，且 tool_exp 脚本直接调用（verify_bg_isolation.py:30/39）。
10. `background.running_count() -> int` 与 `running_summary() -> str`（agent_loop:15/16→372、393）：文本格式 `bgN(cmd40)`、`、` 连接。
11. `background.prepare()` / `cleanup_all()`（agent:41/42、180/183、198/199、271/274；goal_complete:59/60）：会话启动预清与会话结束硬兜底，`cleanup_all` 幂等且须真正杀 running 进程树。
12. 结果文件路径契约：会话专属临时目录 `narnat_bg_*` + `bgN.log`、`bgN.log.tail`、`bgN.log.<seq>.prev`（含 `.prev.tail`）；提交返回绝对路径；目录必须跨 agent 会话隔离（background:3-8、69、191-194、262、484）。
13. 槽位数与编号：固定 8 槽 bg1~bg8，终态释放复用，上限文案（background:48、230-237）。
14. `_kill_proc_tree` 作为被 background 复用的杀树原语（bash:351；background:600、636）——重构时须提供等价能力（可在新架构中新设公共位置）。

D. 标签协议面
15. `exec_signal.rc_line/error_line/tag_error/has_error/parse_rc/strip_tags/safe_cut_points` 的签名与语义（exec_signal:29-83），被 bash、background、terminal、ssh_session、mcp、registry、agent_loop、tool_dispatcher 共 10 处 import（见依赖矩阵）。
16. 标签格式 `[?TAG]`（前导可选空格）与"AI 可见文本不含随机标签、但保留 `[exit code: N]`/`[错误: …]` 文本"的等价性（exec_signal:25-31、58-60；tool_dispatcher:246-249 的判定顺序：先判后剥）。
17. 截断不切开标签（safe_cut_points 语义，exec_signal:63-83；bash 已接，registry 全局截断未接——新架构需明确该不变量覆盖所有截断点）。

### 未验证事项汇总

1. 未运行任何命令执行函数（含 `execute`/`submit`/`kill_active`）——所有进程/平台行为结论均来自源码阅读。
2. Windows `taskkill /F /T`、Unix `killpg` 的实际杀树效果与 `proc.wait(timeout=5)` 超时概率：未验证。
3. `start /b` 孙进程持管道导致读线程不 EOF 的真实时序：未验证（仅注释证据 bash:376-379、background:446-451）。
4. `_sweep_stale` 防线3（Windows 句柄占用致 rename 失败）与 Linux"尽力而为"的差异：未验证。
5. Linux 平台下 `_try_extract_py_code` 直执行的命中（`python3` basename/ext 判定，bash:162-167）与多段路径行为：未在 Linux 运行验证（推理结论，标注未验证）。
6. 50MB 主文件截断 + 1MB 尾部缓冲在真实大输出下的边界（单块>1MB 时 tail 上界）：未验证（源码逻辑见 background:515-521）。
7. registry 全局截断切入标签的实际概率：未验证（标签宽 12 字符）。
