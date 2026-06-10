"""style.json 加载测试"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from narnat_agent.ui.ui_design import apply_style, BLU, GRN, G

narnat_dir = os.path.join(os.path.dirname(__file__), "..", ".narnat")
style_path = os.path.join(narnat_dir, "style.json")

# 备份原有文件
backup = None
if os.path.isfile(style_path):
    with open(style_path, "r", encoding="utf-8") as f:
        backup = f.read()

try:
    style = {"链接色": "#00FF00", "成功色": "#FF0000"}
    with open(style_path, "w") as f:
        json.dump(style, f)

    ok = apply_style(narnat_dir)
    assert ok
    assert "\x1b[38;2;0;255;0m" in str(BLU)
    assert "\x1b[38;2;255;0;0m" in str(GRN)
    assert "100;116;139" in str(G)
    print("PASS: all style tests passed")

    assert not apply_style("/nonexistent/path")

    with open(style_path, "w") as f:
        f.write("not json")
    assert not apply_style(narnat_dir)
    print("PASS: error handling tests passed")

finally:
    if backup:
        with open(style_path, "w", encoding="utf-8") as f:
            f.write(backup)
    elif os.path.isfile(style_path):
        os.remove(style_path)
