"""Write工具 —— 创建新文件或完整覆写文件"""

import os


# 跟踪已Read过的文件，Write覆写前需确认
_read_files: set = set()


def mark_read(file_path: str):
    """标记文件已被Read（由Read工具调用）"""
    _read_files.add(os.path.abspath(file_path))


def clear_read_files():
    """清空已读文件记录（压缩后调用，防止旧标记残留）"""
    _read_files.clear()


def execute(file_path: str, content: str) -> str:
    """
    创建或覆写文件。

    Args:
        file_path: 文件路径
        content: 完整文件内容

    Returns:
        确认信息 + 写入字节数
    """
    abs_path = os.path.abspath(file_path)

    # 覆写已有文件前检查是否Read过
    if os.path.isfile(abs_path):
        if abs_path not in _read_files:
            return (f"错误: 覆写已有文件前必须先Read确认当前内容。"
                    f"请先Read {file_path}，再决定用Edit还是Write。")

    # 自动创建父目录
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"错误: 写入失败: {e}"

    byte_count = len(content.encode("utf-8"))
    # 写入后标记为已读
    _read_files.add(abs_path)
    return f"已写入: {file_path} ({byte_count}字节)"
