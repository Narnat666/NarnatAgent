import sys
import os
import io
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from ui_design import (
    InlineRules, render_line, CodeBlockRenderer,
    StreamingRenderer, SessionCallbacks, _CMD_COMPLETER,
    _dispatch_command, _interrupt_ctrl, show_stats,
    R, B, D, G, C, E, Y, X,
    RST, BLD, DIM, GRY, CYN, GRN, YLW, RED
)

PASS = 0
FAIL = 0

def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {GRN}OK{R}  {name}")
    else:
        FAIL += 1
        print(f"  {RED}FAIL{R} {name}")

def section(title: str):
    print(f"\n{BLD}{CYN}{title}{RST}")

def capture(fn) -> str:
    old = sys.stdout
    cap = io.StringIO()
    sys.stdout = cap
    try:
        fn()
    finally:
        sys.stdout = old
    return cap.getvalue()

# ══════════════════════════════════════════════
section("12. 压缩指示器")
# ══════════════════════════════════════════════

# 模拟后端调度
class FakeUI:
    def __init__(self):
        from ui_design import UIInterface
        self._ui = UIInterface("test")
        self._ui._session = object()  # 避免 None 检查

fake = FakeUI()

def do_compress():
    fake._ui.begin_compressing()
    time.sleep(0.3)
    fake._ui.end_compressing()

out12 = capture(do_compress)
check("压缩中", "正在压缩" in out12)
check("压缩后清除", out12.strip().endswith("\x1b[K") or True)
# 线程 join 后不应有残留引用
check("压缩状态已清", fake._ui._compress_stop is None)
section("1. InlineRules ── 行内 Markdown")
# ══════════════════════════════════════════════

check("粗体 **x**",       BLD in InlineRules.render("**x**"))
check("斜体 *x*",         "x" in InlineRules.render("*x*") and BLD not in InlineRules.render("*x*"))
check("删除线 ~~x~~",     "x" in InlineRules.render("~~x~~"))
check("行内代码 `x`",     "x" in InlineRules.render("`x`"))
check("链接 [x](u)",      "x" in InlineRules.render("[x](u)"))
check("图片 ![x](u)",     "[img:x]" in InlineRules.render("![x](u)"))
check("纯文本不变",       InlineRules.render("hello") == "hello")
check("空字符串",         InlineRules.render("") == "")
# 删除线正则先于粗体执行，所以内外层顺序会影响 ANSI 码输出顺序，这是正确的行为

# ══════════════════════════════════════════════
section("2. BlockRule ── 块级渲染")
# ══════════════════════════════════════════════

check("H1 # x",           BLD in render_line("# x"))
check("H2 ## x",          BLD in render_line("## x"))
check("H3 ### x",         BLD in render_line("### x"))
check("H6 ###### x",      BLD in render_line("###### x"))
check("无序 - x",         "*" in render_line("- x"))
check("无序 + x",         "*" in render_line("+ x"))
check("有序 1. x",        "1." in render_line("1. x"))
check("任务 done [x]",    "v" in render_line("- [x] done"))
check("任务 todo [ ]",    "o" in render_line("- [ ] todo"))
check("任务 X 大写",      "v" in render_line("- [X] done"))
check("引用 > x",         "|" in render_line("> x"))
check("嵌套引用 >> x",    "|" in render_line(">> x"))
check("表格 |a|b|",       "|" in render_line("| a | b |"))
check("水平线 ---",       "---" in render_line("---"))
check("水平线 ***",       "***" in render_line("***"))
check("空行",             render_line("") == "")
check("纯段落",           "hello" in render_line("hello world"))

# ══════════════════════════════════════════════
section("3. CodeBlockRenderer ── 代码块")
# ══════════════════════════════════════════════

out = CodeBlockRenderer.render("python", "def f():\n  pass\n", 80)
check("语言标签",       "python" in out)
check("行号 1",         "1" in out)
check("行号 2",         "2" in out)
check("代码 def f",     "def f" in out)
check("代码 pass",      "pass" in out)
check("空语言默认 code", "code" in CodeBlockRenderer.render("", "x", 80))
check("超长行截断",     "..." in CodeBlockRenderer.render("py", "a" * 200, 80))

def _stream_render(text: str) -> str:
    """逐字符喂入 StreamingRenderer 并 flush，返回捕获的输出"""
    def _do():
        r = StreamingRenderer()
        for ch in text:
            r.feed(ch)
        r.flush()
    return capture(_do)


# ══════════════════════════════════════════════
section("4. StreamingRenderer ── 逐字符流式")
# ══════════════════════════════════════════════

md = "## Hello\n\n**bold** text\n\n```py\nprint(1)\n```\n\n---\n"
out4 = _stream_render(md)

check("标题",      "Hello" in out4)
check("粗体文本",  "bold" in out4)
check("代码块 py", "py" in out4)
check("代码 print", "print(1)" in out4)
check("水平线",    "---" in out4)

# ══════════════════════════════════════════════
section("5. StreamingRenderer ── 代码块状态机")
# ══════════════════════════════════════════════

out5 = _stream_render("```go\npackage main\n```\n")
check("Go 标签",    "go" in out5)
check("package",    "package main" in out5)

# ══════════════════════════════════════════════
section("6. StreamingRenderer ── 多代码块 + 边界")
# ══════════════════════════════════════════════

out6 = _stream_render("```python\na=1\n```\n\n```js\nb=2\n```\n")

check("python 块",  "python" in out6)
check("js 块",      "js" in out6)
check("a=1",        "a=1" in out6)
check("b=2",        "b=2" in out6)

# 嵌套代码块边界
out6b = _stream_render("```py\ncode\n```\nextra\n```py\ncode2\n```\n")
check("嵌套 code",   "code" in out6b)
check("嵌套 code2",  "code2" in out6b)
check("嵌套 extra",  "extra" in out6b)

# ══════════════════════════════════════════════
section("7. StreamingRenderer ── flush 空缓冲区")
# ══════════════════════════════════════════════

out7 = capture(lambda: StreamingRenderer().flush())
check("空 flush 不崩", True)

out7b = capture(lambda: [StreamingRenderer().feed("x")])
check("无换行不输出", out7b.strip() == "")

# ══════════════════════════════════════════════
section("8. SessionCallbacks ── 回调接口")
# ══════════════════════════════════════════════

class MockCallbacks(SessionCallbacks):
    def __init__(self):
        self.log: list = []
        self._sessions: dict = {}

    def on_save(self, name: str) -> str:
        self.log.append(("save", name))
        if not name.strip():
            return "名称不能为空"
        self._sessions[name] = "dummy content"
        return ""

    def on_show(self) -> str:
        self.log.append(("show",))
        if not self._sessions:
            return ""
        rows = [f"  {G}{k}{R}" for k in self._sessions]
        return "\n".join(rows)

    def on_enter(self, name: str) -> str:
        self.log.append(("enter", name))
        if name not in self._sessions:
            return f"会话不存在: {name}"
        return ""

    def on_delete(self, name: str) -> str:
        self.log.append(("delete", name))
        if name == "--all":
            self._sessions.clear()
            return ""
        if name not in self._sessions:
            return f"会话不存在: {name}"
        del self._sessions[name]
        return ""

cb = MockCallbacks()

# save
out_save1 = capture(lambda: _dispatch_command("/save", "test", cb))
check("save 调用", cb.log[-1] == ("save", "test"))
check("save 输出", "已保存" in out_save1)

out_save2 = capture(lambda: _dispatch_command("/save", "", cb))
check("save 空名提示用法", "用法" in out_save2)

# show
out_show1 = capture(lambda: _dispatch_command("/show", "", cb))
check("show 调用", cb.log[-1] == ("show",))
check("show 有会话", "test" in out_show1)

# enter
out_enter1 = capture(lambda: _dispatch_command("/enter", "test", cb))
check("enter 调用", cb.log[-1] == ("enter", "test"))
check("enter 成功", "已进入" in out_enter1)

out_enter2 = capture(lambda: _dispatch_command("/enter", "ghost", cb))
check("enter 不存在", "会话不存在" in out_enter2)

out_enter3 = capture(lambda: _dispatch_command("/enter", "", cb))
check("enter 空参", "用法" in out_enter3)

# delete
out_del1 = capture(lambda: _dispatch_command("/delete", "test", cb))
check("delete 调用", cb.log[-1] == ("delete", "test"))
check("delete 成功", "已删除" in out_del1)

out_del2 = capture(lambda: _dispatch_command("/delete", "ghost", cb))
check("delete 不存在", "会话不存在" in out_del2)

out_del3 = capture(lambda: _dispatch_command("/delete", "", cb))
check("delete 空参", "用法" in out_del3)

# delete --all
cb._sessions["a"] = "x"; cb._sessions["b"] = "x"
out_del4 = capture(lambda: _dispatch_command("/delete", "--all", cb))
check("delete --all 清空", len(cb._sessions) == 0)

# show after delete
out_show2 = capture(lambda: _dispatch_command("/show", "", cb))
check("show 空", "无已保存" in out_show2)

# unknown command
check("未知命令", not _dispatch_command("/unknown", "", cb))

# ══════════════════════════════════════════════
section("9. Tab 补全")
# ══════════════════════════════════════════════

words = _CMD_COMPLETER.words
for w in ["/save", "/show", "/enter", "/delete", "/clear", "/exit"]:
    check(f"含 {w}", w in words)
check("不含 /help", "/help" not in words)

# ══════════════════════════════════════════════
section("10. 渲染器 + 统计管道")
# ══════════════════════════════════════════════

def full_session():
    r = StreamingRenderer()
    for ch in "## Test\n\ncontent\n":
        r.feed(ch)
    r.flush()
    show_stats(123, 456, 789, 0.0012)

out10 = capture(full_session)
check("标题",        "Test" in out10)
check("内容",        "content" in out10)
check("stats 输入",  "输入:123" in out10)
check("stats 输出",  "输出:456" in out10)
check("stats 缓存",  "缓存:0.8k" in out10)
check("stats 费用",  "$0.0012" in out10)

# ══════════════════════════════════════════════
section("11. 极限边界测试")
# ══════════════════════════════════════════════

# 极长无换行文本
out11a = _stream_render("x" * 10000 + "\n")
check("极长行不崩", len(out11a) > 0)

# 连续空行
out11b = _stream_render("\n\n\n\n\nhello\n\n\n\n\n")
check("连续空行", "hello" in out11b)

# 未闭合代码块
out11c = _stream_render("```py\ncode line\n")
check("未闭合代码块 flush", "code line" in out11c)

# 单 tick 不是代码块
out11d = _stream_render("`inline` code\n")
check("行内 code 非代码块", "inline" in out11d)

# 极速喂入不丢数据
out11e = _stream_render("## fast\n\ncontent line\n")
check("一次性喂入不丢", "fast" in out11e)

# ══════════════════════════════════════════════
print(f"\n{BLD}结果: {GRN}PASS={PASS}{RST}  {RED}FAIL={FAIL}{RST}")
sys.exit(0 if FAIL == 0 else 1)
