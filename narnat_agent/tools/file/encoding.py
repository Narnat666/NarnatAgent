"""文本编码探测 —— 文件类工具族共用的首块判定（UTF-8 / GBK）。

契约来源：specs/tools-file「Read 编码识别与二进制拒绝」「Edit 编码与换行符保持」
（Edit 探测同源、但要求严格解码）。

行为搬运自旧实现：`narnat_agent/tools/read/__init__.py::_detect_text_encoding`
与 `narnat_agent/tools/grep/__init__.py::_detect_text_encoding`（两处逐字重复的
副本在 v2 合并为单点），算法与重试窗口逐字一致。
"""
from __future__ import annotations

__all__ = ["detect_text_encoding"]


def detect_text_encoding(head: bytes) -> str:
    """utf-8 严格解码成功 → "utf-8-sig"；失败 → "gbk"。

    中文 Windows 环境 GBK 文件常见（旧日志/导出文件）。固定 utf-8+replace 解码会
    把 GBK 内容变成大片 U+FFFD 乱码，AI 读到的是坏数据。此处用首块字节判定编码。

    尾部窗口重试：首块 8KB 可能恰好多字节序列边界截断，utf-8 严格解码在截断处
    抛错会误判为 GBK。`UnicodeDecodeError.start` 位于末尾 3 字节内时切除重试，
    最多 3 次；仍失败则判定为 GBK。
    """
    trial = head
    for _ in range(3):
        try:
            trial.decode("utf-8")
            return "utf-8-sig"
        except UnicodeDecodeError as e:
            if e.start >= len(trial) - 3:
                trial = head[: e.start]  # 疑似边界截断 → 切掉错误起点后重试
                continue
            break
    return "gbk"
