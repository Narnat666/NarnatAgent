"""
技能功能暴力测试
用法: python tests/test_skill.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from narnat_agent.config.skill_store import load_skill, list_skill_names


def test():
    exe_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skills_dir = os.path.join(exe_dir, "skills")
    passed = 0
    failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS: {msg}")
        else:
            failed += 1
            print(f"  FAIL: {msg}")

    # ── 1. 列出所有技能 ──
    names = list_skill_names(exe_dir)
    check(len(names) == 3, f"应有3个技能, 实际: {names}")
    check("c++" in names, "存在 c++")
    check("JAVA" in names, "存在 JAVA")
    check("文档写作技巧" in names, "存在 文档写作技巧")

    # ── 2. 扁平结构加载 ──
    content, err = load_skill(exe_dir, "文档写作技巧")
    check(err == "", f"加载文档写作技巧: err='{err}'")
    check("文档写作技巧" in content, "内容正确")

    # ── 3. 目录结构加载 ──
    content, err = load_skill(exe_dir, "c++")
    check(err == "", f"加载c++: err='{err}'")
    check("C++" in content, "内容正确")

    content, err = load_skill(exe_dir, "JAVA")
    check(err == "", f"加载JAVA: err='{err}'")
    check("JAVA" in content, "内容正确")

    # ── 4. 不存在的技能 ──
    content, err = load_skill(exe_dir, "nonexistent")
    check(err != "", f"不存在的技能: '{err}'")

    # ── 5. 不存在的 skills 目录 ──
    fake_dir = os.path.join(exe_dir, "nonexistent_test_dir")
    check(len(list_skill_names(fake_dir)) == 0, "不存在的目录返回空列表")
    content, err = load_skill(fake_dir, "anything")
    check(err != "", f"不存在的目录加载返回错误: '{err}'")

    # ── 6. 空目录不含技能 ──
    empty_dir = os.path.join(skills_dir, "_empty")
    os.makedirs(empty_dir, exist_ok=True)
    check("_empty" not in list_skill_names(exe_dir), "空目录不出现在列表中")
    os.rmdir(empty_dir)

    # ── 7. 路径安全性：skills 在 exe_dir 下 ──
    narnat_dir = os.path.join(exe_dir, ".narnat")
    check(not os.path.isdir(os.path.join(narnat_dir, "skills")),
          ".narnat/skills/ 不应存在")

    # ── 8. 排序验证 ──
    check(names == sorted(names), f"名称已排序: {names}")

    # ── 汇总 ──
    print(f"\n{passed + failed} 项测试, {passed} 通过, {failed} 失败")
    return failed == 0


if __name__ == "__main__":
    ok = test()
    sys.exit(0 if ok else 1)
