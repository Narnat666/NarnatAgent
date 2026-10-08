#!/usr/bin/env python3
"""一键编译 narnat 单文件二进制（Windows / Linux 通用）。

用法：
    python build.py                  # 默认：解压目录固定在产物旁（narnat_runtime/<版本>/）
    python build.py --runtime tmp    # 解压目录放系统临时目录（每次启动解压、退出即删）
    python build.py --jobs 8         # 并行编译任务数（默认 CPU 核数）
    python build.py --assume-yes     # 无人值守：自动确认 Nuitka 的下载提示
    python build.py --patch-nuitka   # 只给 Nuitka 打补丁，不编译
    python build.py --revert-nuitka  # 还原 Nuitka 为上游原始代码

两种运行时解压目录模式：
  beside（默认）:
      --onefile-tempdir-spec="{PROGRAM_DIR}/narnat_runtime/{VERSION}"
      解压目录固定在产物所在目录旁，与启动目录无关；同版本缓存复用，之后启动
      免解压。该模式依赖 Nuitka 补丁（上游 4.1.2 会把 {PROGRAM_DIR} 当作相对
      启动目录处理），本脚本在 beside 模式下自动打补丁（幂等）。
  tmp:
      --onefile-tempdir-spec="{TEMP}/onefile_{PID}_{TIME_US}_{RANDOM}"
      即 Nuitka 默认行为：解压到系统临时目录、退出即删。注意运行期间该目录被
      清理会导致程序崩溃，被强杀时会在临时目录残留解压目录。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(ROOT, "main.py")

BESIDE_SPEC = "{PROGRAM_DIR}/narnat_runtime/{VERSION}"
TEMP_SPEC = "{TEMP}/onefile_{PID}_{TIME_US}_{RANDOM}"

NOFOLLOW_IMPORTS = (
    "tkinter",
    "unittest",
    "unittest.mock",
    "invoke",
    "test",
    "tests",
    "setuptools",
    "pip",
    "distutils",
)

# ── Nuitka 补丁 ──────────────────────────────────────────────────────────────
# 上游 Nuitka 4.1.x 把以 {PROGRAM_DIR} 开头的 --onefile-tempdir-spec 当作
# 「相对启动目录」的相对路径处理（issue #3830），导致同一 exe 从不同工作目录
# 启动时在每个启动目录下各解压一份 narnat_runtime。以下补丁恢复「固定在 exe
# 旁」的语义，并修复 Linux 分支 {PROGRAM_DIR} 展开的死循环缺陷。

MARKER = "NARNAT LOCAL PATCH"

OPTIONS_STRIP_ORIGINAL = """\
    if options.onefile_tempdir_spec.startswith("{PROGRAM_DIR}"):
        options.onefile_tempdir_spec = options.onefile_tempdir_spec[
            len("{PROGRAM_DIR}") :
        ].lstrip("/\\\\")
"""

OPTIONS_STRIP_PATCHED = """\
    # NARNAT LOCAL PATCH: upstream strips a leading "{PROGRAM_DIR}/" from the
    # spec, which turns it into a path relative to the current working
    # directory. Keep it intact so that the onefile payload is unpacked next
    # to the binary, no matter which directory the binary is started from.
"""

OPTIONS_DYNAMIC_HEADER_ORIGINAL = """\
def isDynamicSpec(spec):
    \"\"\"Check if a spec contains dynamic values that change every run.\"\"\"
    for candidate in (
"""

OPTIONS_DYNAMIC_HEADER_PATCHED = """\
def isDynamicSpec(spec):
    \"\"\"Check if a spec contains dynamic values that change every run.\"\"\"
    # NARNAT LOCAL PATCH: "{PROGRAM_DIR}" points to a stable location (the
    # directory of the binary) and therefore must not be treated as a dynamic
    # value. Otherwise a spec anchored at it would force the "temporary"
    # onefile mode, unpacking/removing the payload on every run.
    for candidate in (
"""

OPTIONS_DYNAMIC_LIST_ORIGINAL = """\
        "{PROGRAM}",
        "{PROGRAM_BASE}",
        "{PROGRAM_DIR}",
    ):
        if candidate in spec:
            return True
"""

OPTIONS_DYNAMIC_LIST_PATCHED = """\
        "{PROGRAM}",
        "{PROGRAM_BASE}",
    ):
        if candidate in spec:
            return True
"""

OPTIONS_WARN_ORIGINAL = """\
    elif not options.onefile_tempdir_spec.startswith(
        ("{TEMP}", "{HOME}", "{CACHE_DIR}")
    ):
"""

OPTIONS_WARN_PATCHED = """\
    elif not options.onefile_tempdir_spec.startswith(
        # NARNAT LOCAL PATCH: "{PROGRAM_DIR}" resolves at run time to the
        # binary's own directory, so it is not a path relative to wherever the
        # user happened to start the program and needs no warning.
        ("{TEMP}", "{HOME}", "{CACHE_DIR}", "{PROGRAM_DIR}")
    ):
"""

HELPERS_ORIGINAL = """\
                size_t length = strlen(target);

                // TODO: We should have an inplace strip dirname function, like for
                // Win32 stripFilenameW, but that then knows the length and check
                // if that empties the string, but this works for now.
                while (true) {
                    if (length == 0) {
                        return false;
                    }

                    if (target[length] == '/') {
                        break;
                    }

                    target[length] = 0;
                }
"""

HELPERS_PATCHED = """\
                // NARNAT LOCAL PATCH: the original loop never decreased
                // "length" and never found a separator (it looked at the
                // terminating zero byte), which is an endless loop. Strip the
                // last path component the same way Win32 does with
                // stripFilenameW.
                size_t length = strlen(target);

                while (true) {
                    if (length == 0) {
                        return false;
                    }

                    length -= 1;

                    if (target[length] == '/') {
                        target[length] = 0;
                        break;
                    }
                }
"""

PATCH_TARGETS = (
    ("options/Options.py", (
        (OPTIONS_STRIP_ORIGINAL, OPTIONS_STRIP_PATCHED),
        (OPTIONS_WARN_ORIGINAL, OPTIONS_WARN_PATCHED),
        (OPTIONS_DYNAMIC_HEADER_ORIGINAL, OPTIONS_DYNAMIC_HEADER_PATCHED),
        (OPTIONS_DYNAMIC_LIST_ORIGINAL, OPTIONS_DYNAMIC_LIST_PATCHED),
    )),
    ("build/static_src/HelpersFilesystemPaths.c", ((HELPERS_ORIGINAL, HELPERS_PATCHED),)),
)


def _read_text(path):
    # newline="" 保留原文件换行符，避免回写时改动整个文件的换行风格
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_text(path, content):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _apply_patch(path, pairs, revert):
    content = _read_text(path)
    backup = path + ".narnat_bak"
    crlf = "\r\n" in content

    def to_file_style(text):
        return text.replace("\n", "\r\n") if crlf else text

    pairs = tuple(
        (to_file_style(original), to_file_style(patched)) for original, patched in pairs
    )

    if revert:
        if os.path.exists(backup):
            shutil.copyfile(backup, path)
            print("已用备份还原: %s" % path)
            return True

        for original, patched in pairs:
            if patched in content:
                content = content.replace(patched, original, 1)

        _write_text(path, content)
        print("已还原（无备份，按文本反替换）: %s" % path)
        return True

    if MARKER in content:
        print("已打补丁，跳过: %s" % path)
        return True

    for original, patched in pairs:
        if original not in content:
            print("错误：未找到预期代码，未修改: %s" % path)
            return False
        content = content.replace(original, patched, 1)

    if not os.path.exists(backup):
        shutil.copyfile(path, backup)
        print("已备份原始文件: %s" % backup)

    _write_text(path, content)
    print("已打补丁: %s" % path)
    return True


def run_nuitka_patch(revert):
    import nuitka

    root = os.path.dirname(os.path.abspath(nuitka.__file__))
    print("Nuitka 目录: %s" % root)

    ok = True
    for rel_path, pairs in PATCH_TARGETS:
        path = os.path.join(root, rel_path.replace("/", os.sep))
        if not os.path.isfile(path):
            print("错误：未找到文件: %s" % path)
            ok = False
            continue
        ok = _apply_patch(path, pairs, revert) and ok

    if not ok:
        print("补丁未完全应用，请检查上面的错误信息。")
        return False

    if not revert:
        print("Nuitka 补丁完成，需重新编译才生效。")
    return True


# ── 编译 ────────────────────────────────────────────────────────────────────

def parse_version():
    if not os.path.isfile(MAIN_PY):
        raise SystemExit("错误：未找到 %s" % MAIN_PY)

    with open(MAIN_PY, "r", encoding="utf-8") as f:
        for line in f:
            matched = re.match(r"""^__version__\s*=\s*["']([^"']+)["']""", line)
            if matched:
                return matched.group(1)

    raise SystemExit("错误：未能从 main.py 解析 __version__")


def main():
    parser = argparse.ArgumentParser(
        description="一键编译 narnat 单文件二进制（Windows / Linux 通用）"
    )
    parser.add_argument(
        "--runtime",
        choices=("beside", "tmp"),
        default="beside",
        help="narnat_runtime 位置：beside=产物旁（默认，同版本缓存复用）；"
        "tmp=系统临时目录（每次启动解压、退出即删）",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="并行编译任务数（默认 CPU 核数）",
    )
    parser.add_argument(
        "--assume-yes",
        action="store_true",
        help="自动确认 Nuitka 的下载提示（无人值守环境）",
    )
    parser.add_argument(
        "--patch-nuitka",
        action="store_true",
        help="只给 Nuitka 打补丁后退出，不编译",
    )
    parser.add_argument(
        "--revert-nuitka",
        action="store_true",
        help="将 Nuitka 还原为上游原始代码后退出",
    )
    args = parser.parse_args()

    if args.patch_nuitka or args.revert_nuitka:
        return 0 if run_nuitka_patch(args.revert_nuitka) else 1

    version = parse_version()
    if args.runtime == "beside":
        if not run_nuitka_patch(False):
            return 1

    output_name = "narnat.exe" if os.name == "nt" else "narnat"
    spec = BESIDE_SPEC if args.runtime == "beside" else TEMP_SPEC

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--output-dir=output",
        "--output-filename=%s" % output_name,
        "--onefile-tempdir-spec=%s" % spec,
        "--product-version=%s" % version,
        "--jobs=%d" % args.jobs,
        "--lto=yes",
        "--python-flag=no_docstrings",
        "--include-module=openai",
    ]
    for name in NOFOLLOW_IMPORTS:
        cmd.append("--nofollow-import-to=%s" % name)
    if args.assume_yes:
        cmd.append("--assume-yes-for-downloads")
    cmd.append("main.py")

    print("[build] 版本: %s" % version)
    print("[build] 运行时目录模式: %s（%s）" % (args.runtime, spec))
    print("[build] 产物: %s" % os.path.join("output", output_name))

    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode != 0:
        raise SystemExit("编译失败，退出码 %d" % result.returncode)

    print("[build] 编译完成: %s" % os.path.join(ROOT, "output", output_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
