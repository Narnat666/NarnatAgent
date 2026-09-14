"""
headless 模式 UI —— 无交互、纯文本流输出

用于 nn -p 一次性任务执行：不启动 prompt_toolkit、不轮询键盘、
不显示 spinner/统计栏。流内容复用现有 StreamingRenderer 渲染
（纯文本模式下去色，表格/列表/代码块结构保留），直接输出到 stdout。
"""

from ..output import write as _stdout_write
from .renderer import StreamingRenderer


class HeadlessStream:
    """极简流会话：feed→渲染输出。无 spinner / 中断 / 统计栏。"""

    def __init__(self):
        self._renderer = StreamingRenderer()
        self._started = False
        self.aborted = False
        self.cancelled = False  # headless 不可打断（恒 False）

    def begin(self):
        pass

    def feed(self, chunk: str):
        self._started = True
        self._renderer.feed(chunk)

    def pause_spinner(self):
        pass

    def resume_spinner(self):
        pass

    def flush_renderer(self):
        if self._started:
            self._renderer.flush(final=False)

    def reset_renderer(self):
        self._renderer.reset()

    def finish(self, *args, **kwargs):
        """忽略统计参数（headless 不显示费用/token 统计栏）"""
        self._renderer.flush(final=True)

    def abort(self, message=None):
        self.aborted = True
        self._renderer.flush(final=True)
        if message:
            _stdout_write(message + "\n")


class HeadlessUI:
    """no-op UI 总接口：headless 下唯一职责是产流"""

    def __init__(self, model_name: str = ""):
        self._model = model_name

    def start(self):
        pass

    def read_input(self):
        return None

    def read_input_with_prompt(self, prompt_text: str = ""):
        return None

    def dispatch_command(self, cmd: str, args: str):
        return 0  # CommandResult.UNKNOWN

    def create_stream(self):
        return HeadlessStream()

    def on_interrupted(self):
        pass

    def begin_compressing(self):
        pass

    def end_compressing(self):
        pass

    def begin_summarizing(self):
        pass

    def end_summarizing(self):
        pass
