"""输入历史（按 ↑ 翻历史提问）的唯一权威实现

终端形态（prompt_toolkit 的 FileHistory）与插件形态（VSCode 面板）
读写的是**同一份文件** `<data_dir>/.narnat_history`，格式由 prompt_toolkit
定义，故本模块的读写一律走 `FileHistory`——自己再写一套解析必然与它跑偏：
prompt_toolkit 的格式是「每条以 `# 时间戳` 起头，正文每行前缀 `+`，多行
输入就是连续的多个 `+` 行」，且 `load_history_strings()` 返回的是
**newest-first**。
"""

import os
from typing import List

from prompt_toolkit.history import FileHistory

# 附件块起始标记：用户发送时前端会把附件清单一并拼进正文。
# 历史里只该留用户自己打的字，一长串本地路径翻出来既没用也挤满输入框。
_ATTACH_MARK = "[用户附带了以下本地路径，请用相应工具自行读取处理]"

# 内部注入：目标模式自动续跑、系统提示，都不是用户输入。
# 写进历史后用户按 ↑ 会翻到一句自己从没说过的话。
_INTERNAL_PREFIXES = ("[自动续跑]", "[系统提示]")

# 单条超长（整段贴进来的日志/代码）截断，免得一条历史把输入框撑爆。
_MAX_TEXT = 4000
_TRUNC_MARK = " …[已截断]"


def history_path(data_dir: str) -> str:
    """输入历史文件路径。data_dir 为空时回退到用户主目录（旧行为）。"""
    if not data_dir:
        return os.path.join(os.path.expanduser("~"), ".narnat_history")
    return os.path.join(data_dir, ".narnat_history")


def _clean_entry(text: str) -> str:
    """单条历史的归一化：去首尾空白、超长截断。空串表示这条不要。"""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) > _MAX_TEXT:
        t = t[:_MAX_TEXT] + _TRUNC_MARK
    return t


def load_history(path: str, limit: int = 100) -> List[str]:
    """读出历史，返回「最旧在前」的列表。

    文件不存在、格式坏掉、编码异常一律返回 []：历史读不出来只是少个便利
    功能，绝不能让调用方（握手、发消息）跟着崩。
    """
    try:
        if not path or not os.path.isfile(path):
            return []
        # newest-first → 反成最旧在前
        raw = list(FileHistory(path).load_history_strings())[::-1]
    except Exception:
        return []
    items = []
    for one in raw:
        t = _clean_entry(one)
        if t:
            items.append(t)
    if limit and limit > 0 and len(items) > limit:
        items = items[-limit:]
    # 逐条去重：同一句话只留一份，保留靠后（较新）的那份
    seen = set()
    out = []
    for t in reversed(items):
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.reverse()
    return out


def _clean_user_text(text: str) -> str:
    """要写进历史的用户正文；返回空串表示「这条不写」。"""
    t = (text or "").strip()
    if not t:
        return ""
    cut = t.find(_ATTACH_MARK)
    if cut >= 0:
        t = t[:cut].strip()
        if not t:
            return ""
    if t.startswith(_INTERNAL_PREFIXES):
        return ""
    return t


def _trim(path: str, max_bytes: int, keep: int) -> None:
    """文件超过 max_bytes 时只留最近 keep 条（用同一份 FileHistory 格式重写）"""
    tmp = path + ".trim"
    try:
        if max_bytes <= 0 or os.path.getsize(path) <= max_bytes:
            return
        items = load_history(path, keep)
        if not items:
            return
        if os.path.exists(tmp):
            os.remove(tmp)
        history = FileHistory(tmp)
        for one in items:
            history.store_string(one)
        os.replace(tmp, path)
    except Exception:
        # 裁剪失败就放弃：历史文件大一点不影响主流程，写坏了才是事故
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def append_history(path: str, text: str, max_bytes: int = 512 * 1024,
                   keep: int = 200) -> bool:
    """追加一条用户输入到历史文件。返回是否真的写了。

    任何异常都吞掉并返回 False：发消息这条主链路不能因为历史写失败而失败
    （目录只读、磁盘满、文件被占用都真实存在）。
    """
    try:
        clean = _clean_user_text(text)
        if not clean:
            return False
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        FileHistory(path).store_string(clean)
        _trim(path, max_bytes, keep)
        return True
    except Exception:
        return False
