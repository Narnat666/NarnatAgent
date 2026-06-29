"""
narnat agent 会话 JSON → 人类可读 Markdown 翻译器

用法: python translate_sessions.py [输入目录] [输出目录]
默认: 输入=当前目录, 输出=./translated/
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path



def pretty_args(args_str: str) -> str:
    """格式化 JSON 参数，失败则返回原字符串"""
    try:
        obj = json.loads(args_str)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return args_str


def format_timestamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return str(ts)


def translate_one(session_path: Path, output_dir: Path) -> str:
    """翻译单个会话 JSON → Markdown 文件，返回输出路径"""
    with open(session_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    name = data.get("name", session_path.stem)
    timestamp = data.get("timestamp", 0)
    messages = data.get("messages", [])

    lines = []
    lines.append(f"# {name}")
    lines.append("")
    lines.append(f"**时间**: {format_timestamp(timestamp)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 预处理：建立 tool_call_id → tool response 的索引
    tool_results = {}
    for msg in messages:
        if msg.get("role") == "tool":
            tool_results[msg.get("tool_call_id", "")] = msg.get("content", "")

    round_num = 1
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        # 跳过 system 消息
        if role == "system":
            i += 1
            continue

        # 用户消息 → 新的一轮
        if role == "user":
            lines.append(f"## 第 {round_num} 轮 — 用户提问")
            lines.append("")
            lines.append(f"> {msg.get('content', '').strip()}")
            lines.append("")
            round_num += 1

        # assistant 消息
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            content = msg.get("content")

            # 有 tool_calls → 展示工具调度
            if tool_calls:
                for j, tc in enumerate(tool_calls, 1):
                    fn = tc.get("function", {})
                    func_name = fn.get("name", "unknown")
                    func_args = fn.get("arguments", "{}")
                    call_id = tc.get("id", "")

                    lines.append(f"### 🔧 工具调用: `{func_name}`")
                    lines.append("")
                    lines.append("**参数**:")
                    lines.append("")
                    lines.append("```json")
                    lines.append(pretty_args(func_args))
                    lines.append("```")
                    lines.append("")

                    # 展示对应返回值
                    result = tool_results.get(call_id)
                    if result is not None:
                        lines.append("**返回**:")
                        lines.append("")
                        lines.append("```")
                        lines.append(result)
                        lines.append("```")
                        lines.append("")
                    else:
                        lines.append("*(无对应返回值)*")
                        lines.append("")

            # 有文本内容 → AI 分析/结论
            if content and content.strip():
                lines.append("### 💬 AI 分析/结论")
                lines.append("")
                lines.append(content.strip())
                lines.append("")

        i += 1

    # 写入文件
    output_path = output_dir / f"{name}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return str(output_path)


def main():
    # 目录
    input_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / "translated"

    output_dir.mkdir(parents=True, exist_ok=True)

    json_files = sorted(input_dir.glob("*.json"))
    # 排除自身（非会话文件，如 translate_sessions.py 生成的配置等）
    session_files = []
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "messages" in data and isinstance(data["messages"], list):
                session_files.append(jf)
        except (json.JSONDecodeError, KeyError):
            continue

    if not session_files:
        print("未找到 narnat 会话 JSON 文件。")
        return

    print(f"找到 {len(session_files)} 个会话文件:\n")

    for sf in session_files:
        out = translate_one(sf, output_dir)
        print(f"  {sf.name}  →  {Path(out).name}")

    print(f"\n全部输出到: {output_dir}")


if __name__ == "__main__":
    main()
