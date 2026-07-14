"""
narnat agent 会话 JSON → 人类可读 Markdown 翻译器

支持树形会话结构（父-子会话），保留目录层级。

用法: python translate_sessions.py [输入目录] [输出目录]
默认: 输入=当前目录, 输出=./translated/
"""

import json
import sys
from datetime import datetime
from pathlib import Path

SYSTEM_PROMPT_MARKER = "| 你的身份 |"


def pretty_args(args_str: str) -> str:
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


def classify_system_message(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith(SYSTEM_PROMPT_MARKER):
        return "prompt"
    if stripped.startswith("# 子会话") or "探索分支总结" in stripped[:80]:
        return "conclusion"
    if "探索分支开始" in stripped[:80]:
        return "branch_start"
    return "other"


def find_child_sessions(session_dir: Path, session_name: str) -> list[str]:
    child_dir = session_dir / session_name
    if not child_dir.is_dir():
        return []
    names = []
    for cf in sorted(child_dir.glob("*.json")):
        try:
            with open(cf, "r", encoding="utf-8") as f:
                cd = json.load(f)
            if "messages" in cd and isinstance(cd["messages"], list):
                names.append(cd.get("name", cf.stem))
        except (json.JSONDecodeError, KeyError):
            continue
    return names


def translate_one(session_path: Path, output_dir: Path, root_dir: Path) -> str:
    with open(session_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    name = data.get("name", session_path.stem)
    timestamp = data.get("timestamp", 0)
    messages = data.get("messages", [])
    parent = data.get("parent")
    status = data.get("status", "unknown")
    summary = data.get("summary")

    lines = []
    lines.append(f"# {name}")
    lines.append("")
    lines.append(f"**时间**: {format_timestamp(timestamp)}")
    lines.append(f"**状态**: {status}")
    lines.append(f"**父会话**: {parent or '(根会话)'}")
    if summary:
        display = summary[:200] + ("..." if len(summary) > 200 else "")
        lines.append(f"**摘要**: {display}")
    else:
        lines.append("**摘要**: (无)")
    lines.append("")

    child_names = find_child_sessions(root_dir, name)
    if child_names:
        lines.append("## 📎 子会话")
        lines.append("")
        for cn in child_names:
            lines.append(f"- [{cn}](./{name}/{cn}.md)")
        lines.append("")

    lines.append("---")
    lines.append("")

    tool_results = {}
    for msg in messages:
        if msg.get("role") == "tool":
            tool_results[msg.get("tool_call_id", "")] = msg.get("content", "")

    round_num = 1
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            msg_type = classify_system_message(content)
            if msg_type == "prompt":
                continue
            if msg_type == "conclusion":
                lines.append("### 📎 子会话结论")
            elif msg_type == "branch_start":
                lines.append("### 🌿 探索分支")
            else:
                lines.append("### ⚙️ 系统消息")
            lines.append("")
            lines.append(content.strip())
            lines.append("")
            lines.append("---")
            lines.append("")
            continue

        if role == "tool":
            continue

        if role == "user":
            lines.append(f"## 第 {round_num} 轮 — 用户提问")
            lines.append("")
            lines.append(f"> {content.strip()}")
            lines.append("")
            round_num += 1

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            text_content = msg.get("content")

            if tool_calls:
                for tc in tool_calls:
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

            if text_content and text_content.strip():
                lines.append("### 💬 AI 分析/结论")
                lines.append("")
                lines.append(text_content.strip())
                lines.append("")

    rel = session_path.parent.relative_to(root_dir)
    out_subdir = output_dir / rel
    out_subdir.mkdir(parents=True, exist_ok=True)
    output_path = out_subdir / f"{name}.md"

    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))

    return str(output_path)


def collect_session_files(root_dir: Path) -> list[Path]:
    result = []
    for json_file in sorted(root_dir.rglob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "messages" in data and isinstance(data["messages"], list):
                result.append(json_file)
        except (json.JSONDecodeError, KeyError):
            continue
    return result


def main():
    input_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / "translated"

    session_files = collect_session_files(input_dir)

    if not session_files:
        print("未找到 narnat 会话 JSON 文件。")
        return

    print(f"找到 {len(session_files)} 个会话文件:\n")

    for sf in session_files:
        out = translate_one(sf, output_dir, input_dir)
        rel_in = sf.relative_to(input_dir)
        rel_out = Path(out).relative_to(output_dir)
        print(f"  {rel_in}  →  {rel_out}")

    print(f"\n全部输出到: {output_dir}")


if __name__ == "__main__":
    main()
