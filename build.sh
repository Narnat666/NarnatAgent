#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Narnat 一键构建
#
#   默认流程：同步界面 → 编译单文件 exe → 打包内嵌 exe 的 vsix → 汇总到 dist/
#   dist/ 里同时给出可执行文件与插件包：用户可以只装插件（vsix），
#   也可以只拿 exe 用。
#
#   Windows（Git Bash）：bash build.sh
#   Linux / WSL：        ./build.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$ROOT/vscode-extension"
DIST="$ROOT/dist"

MODE=""              # 空 | all | exe | vsix
SKIP_EXE_PATH=""     # --skip-exe 给的已有 exe
RUNTIME="beside"
JOBS=""
ASSUME_YES="0"
DRY_RUN="0"
STRICT="1"           # dry-run 时降级为告警
CK_FAILED=0

# ── 输出与命令执行 ───────────────────────────────────────────────────────────

step() {
  printf '\n[%s] %s\n' "$1" "$2"
}

quote_cmd() {
  local out="" a
  for a in "$@"; do
    case "$a" in
      *[[:space:]]*) out="$out '$a'" ;;
      "") out="$out ''" ;;
      *) out="$out $a" ;;
    esac
  done
  printf '%s' "${out# }"
}

fail_cmd() {
  local rc="$1"
  shift
  printf '\n[失败] 命令退出码 %s（脚本自身按退出码表返回 1）\n' "$rc" >&2
  printf '       目录: %s\n' "$PWD" >&2
  printf '       命令: %s\n' "$(quote_cmd "$@")" >&2
  printf '       修正后重跑即可（该步骤不会留下半成品到 dist/）。\n' >&2
}

# RUN 命令...：打印后执行；非 0 立即打印失败命令并以 1 退出。
# 子命令的原始退出码只作诊断打印，不向外透传——否则 build.py 返回 2（参数错）
# 会被当成 build.sh 的参数错，返回 9 则落到退出码表里根本没有的码上。
RUN() {
  local shown rc=0
  shown="$(quote_cmd "$@")"
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$shown"
    return 0
  fi
  printf '[run] %s\n' "$shown"
  "$@" || rc=$?
  if [ "$rc" -ne 0 ]; then
    fail_cmd "$rc" "$@"
    exit 1
  fi
}

ck() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "1" ]; then
    printf 'PASS %s%s\n' "$name" "${detail:+ → $detail}"
  else
    printf 'FAIL %s%s\n' "$name" "${detail:+ → $detail}"
    CK_FAILED=$((CK_FAILED + 1))
  fi
}

warn() {
  printf '[警告] %s\n' "$*" >&2
}

die_arg() {
  printf '[错误] %s\n' "$*" >&2
  printf '       用 bash build.sh --help 查看全部参数。\n' >&2
  exit 2
}

die_env() {
  local first="1" line
  for line in "$@"; do
    if [ "$first" = "1" ]; then
      printf '[错误] %s\n' "$line" >&2
      first="0"
    else
      printf '       %s\n' "$line" >&2
    fi
  done
  exit 3
}

usage() {
  cat <<'EOF'
Narnat 一键构建（单文件 exe + 内嵌 exe 的 VSCode 插件 vsix）

用法：bash build.sh [选项]

选项：
  --exe-only              只编译单文件 exe（output/narnat[.exe]）
  --vsix-only             只打包 vsix（要求 output/narnat.exe 已存在）
  --skip-exe <path>       跳过编译，用指定的已有 exe 打包 vsix
  --runtime <beside|tmp>  透传给 build.py：narnat_runtime 解压位置（默认 beside）
  --jobs <N>              透传给 build.py：并行编译任务数（默认 CPU 核数）
  --assume-yes            透传给 build.py：无人值守，自动确认 Nuitka 下载提示
  --dry-run               只打印将要执行的完整命令，不实际执行
  -h, --help              显示本帮助

产物（dist/）：
  dist/narnat.exe                     单文件可执行程序（Linux 上为 dist/narnat）
  dist/narnat-vscode-<版本>.vsix      内嵌上面这个 exe 的 VSCode 插件包
  装 vsix：code --install-extension dist/narnat-vscode-<版本>.vsix
  用 exe： 把 dist/narnat.exe 放到任意目录直接运行（首次启动在 exe 旁解压
           narnat_runtime/<版本>/，与启动目录无关）

退出码：
  0 成功   1 构建步骤失败   2 参数错误   3 缺少依赖（python/node/npm/vsce）
  4 前置产物缺失（应为 output/narnat.exe）
  某个步骤失败时会先打印失败的那条命令与它的子命令原始退出码（供排查），
  脚本自身的退出码始终按上表返回：构建步骤一律 1，不透传子命令的码。

典型流程：
  bash build.sh                              # 一条龙：同步界面 → 编译 → 打包 → 自检
  bash build.sh --exe-only --jobs 4          # 只编译 exe（约 10~30 分钟）
  bash build.sh --vsix-only                  # exe 编译好了，只重打插件包
  bash build.sh --skip-exe D:/x/narnat.exe   # 用已有 exe 打包
  bash build.sh --dry-run                    # 只看会跑哪些命令
EOF
}

# ── 参数解析 ─────────────────────────────────────────────────────────────────

abspath() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    [A-Za-z]:[\\/]*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "$PWD/$1" ;;
  esac
}

set_mode() {
  local mode="$1" opt="$2" cur=""
  if [ "$MODE" = "exe" ]; then
    cur="--exe-only"
  fi
  if [ "$MODE" = "vsix" ]; then
    cur="--vsix-only"
  fi
  if [ -n "$cur" ] && [ "$MODE" != "$mode" ]; then
    die_arg "$opt 与 $cur 不能同时使用。"
  fi
  MODE="$mode"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --exe-only)
      set_mode "exe" "--exe-only"
      shift
      ;;
    --vsix-only)
      set_mode "vsix" "--vsix-only"
      shift
      ;;
    --skip-exe)
      [ $# -ge 2 ] || die_arg "--skip-exe 需要一个 exe 路径参数。"
      SKIP_EXE_PATH="$(abspath "$2")"
      shift 2
      ;;
    --skip-exe=*)
      SKIP_EXE_PATH="$(abspath "${1#*=}")"
      shift
      ;;
    --runtime)
      [ $# -ge 2 ] || die_arg "--runtime 需要 beside 或 tmp。"
      RUNTIME="$2"
      shift 2
      ;;
    --runtime=*)
      RUNTIME="${1#*=}"
      shift
      ;;
    --jobs)
      [ $# -ge 2 ] || die_arg "--jobs 需要一个正整数。"
      JOBS="$2"
      shift 2
      ;;
    --jobs=*)
      JOBS="${1#*=}"
      shift
      ;;
    --assume-yes)
      ASSUME_YES="1"
      shift
      ;;
    --dry-run)
      DRY_RUN="1"
      STRICT="0"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_arg "未知参数：$1"
      ;;
  esac
done

case "$RUNTIME" in
  beside|tmp) ;;
  *) die_arg "--runtime 只能是 beside 或 tmp（收到 '$RUNTIME'）。" ;;
esac

if [ -n "$JOBS" ] && ! [[ "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
  die_arg "--jobs 需要正整数（收到 '$JOBS'）。"
fi

if [ -n "$SKIP_EXE_PATH" ]; then
  # --skip-exe 的语义就是「不编译、直接打包」，等价于 vsix-only + 指定 exe
  set_mode "vsix" "--skip-exe"
fi

[ -n "$MODE" ] || MODE="all"

# ── 环境与路径工具 ───────────────────────────────────────────────────────────

# 传给 node/python 的路径：Git Bash 下换成 Windows 形式，避免 msys 路径转换被禁用时出错
win_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w -- "$1"
  else
    printf '%s\n' "$1"
  fi
}

case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) EXE_NAME="narnat.exe" ;;
  *) EXE_NAME="narnat" ;;
esac

PY=""
NODE=""
detect_python() {
  local c
  for c in python python3 py; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      PY="$c"
      return 0
    fi
  done
  return 1
}

# package-vsix.js 用 `npx --yes @vscode/vsce package`，本地/全局/缓存任一命中即可
have_vsce() {
  if [ -e "$EXT_DIR/node_modules/.bin/vsce" ] \
    || [ -e "$EXT_DIR/node_modules/.bin/vsce.cmd" ] \
    || [ -d "$EXT_DIR/node_modules/@vscode/vsce" ]; then
    return 0
  fi
  if command -v npx >/dev/null 2>&1; then
    if (cd "$EXT_DIR" && npx --no-install @vscode/vsce --version >/dev/null 2>&1); then
      return 0
    fi
  fi
  if command -v vsce >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

missing_dep() {
  if [ "$STRICT" = "1" ]; then
    die_env "$@"
  else
    warn "$1"
  fi
}

check_env() {
  local need_pack="0"
  if [ "$MODE" != "exe" ]; then
    need_pack="1"
  fi

  if ! detect_python; then
    missing_dep \
      "找不到可用的 python（需要 3.8+）。" \
      "安装：https://www.python.org/downloads/ 或 winget install Python.Python.3.12"
    # 走到这里说明是 dry-run（真实运行上面已退出 3）：用占位名让命令序列可读
    PY="python"
  fi

  # node / npm / vsce 只在需要打包 vsix 时才用得上。
  # --exe-only 根本不打包插件，不该被 node 缺失挡住（原先无条件检查，缺 node 直接退出 3）。
  if [ "$need_pack" = "1" ]; then
    if ! command -v node >/dev/null 2>&1; then
      missing_dep "找不到 node（VSCode 扩展打包需要 Node.js 18+）。" \
        "安装：https://nodejs.org/ 或 winget install OpenJS.NodeJS.LTS"
      NODE="node"
    else
      NODE="node"
      if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' >/dev/null 2>&1; then
        missing_dep "node 版本过低（当前 $(node -v 2>/dev/null)），vsce 需要 18+。"
      fi
    fi

    if ! command -v npm >/dev/null 2>&1; then
      missing_dep "找不到 npm（打包 vsix 需要 npm/npx）。" \
        "通常随 Node.js 一起安装；确认 PATH 里有 npm。"
    fi
    if ! have_vsce; then
      missing_dep \
        "找不到 vsce（@vscode/vsce），无法打包 vsix。" \
        "任选其一安装后重试：" \
        "  cd vscode-extension && npm i -D @vscode/vsce      （本地安装，推荐）" \
        "  npm i -g @vscode/vsce                            （全局安装）" \
        "  npx --yes @vscode/vsce@latest --version          （联网拉取一次，之后可离线）"
    fi
  fi

  if [ ! -f "$ROOT/build.py" ]; then
    missing_dep "缺少 $ROOT/build.py，无法编译 exe。"
  fi
  if [ "$MODE" != "vsix" ] || [ -z "$SKIP_EXE_PATH" ]; then
    if [ ! -f "$ROOT/main.py" ]; then
      missing_dep "缺少 $ROOT/main.py，无法编译 exe。"
    fi
  fi
  if [ ! -f "$EXT_DIR/package.json" ]; then
    missing_dep "缺少 $EXT_DIR/package.json，无法打包 vsix。"
  fi
}

# 版本号一律用纯 bash 读，不依赖 python：
# dry-run 缺 python 时原来会走到 `"$PY" - ...`，PY 为空 → `build.sh: line 343: : command not found`。
read_app_version() {
  local line
  if [ ! -f "$ROOT/main.py" ]; then
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^__version__[[:space:]]*=[[:space:]]*[\"\']([^\"\']+) ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  done < "$ROOT/main.py"
  return 0
}

read_ext_version() {
  local line v=""
  if [ ! -f "$EXT_DIR/package.json" ]; then
    return 0
  fi
  # 有 python 就精确解析 JSON；没有（dry-run）则退回纯 bash 正则读第一条 "version"
  if [ -n "$PY" ]; then
    v="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
      "$(win_path "$EXT_DIR/package.json")" 2>/dev/null)" || v=""
  fi
  if [ -z "$v" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" =~ \"version\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
        v="${BASH_REMATCH[1]}"
        break
      fi
    done < "$EXT_DIR/package.json"
  fi
  printf '%s\n' "$v"
}

# ── 自检 ─────────────────────────────────────────────────────────────────────

selfcheck_exe() {
  local exe="$DIST/$EXE_NAME" out="" first="" rc=0
  if [ ! -f "$exe" ]; then
    ck "dist/$EXE_NAME 存在" "0" "文件不存在"
    return 0
  fi
  out="$("$exe" -v 2>&1)" || rc=$?
  first="${out%%$'\n'*}"
  if [ "$rc" -eq 0 ] && [[ "$first" =~ ^narnat\ [0-9]+\.[0-9]+\.[0-9]+ ]]; then
    ck "dist/$EXE_NAME -v 输出 narnat <版本>" "1" "'$first'"
  else
    ck "dist/$EXE_NAME -v 输出 narnat <版本>" "0" "退出码 $rc，输出 '$first'"
  fi
  if [ -n "${APP_VERSION:-}" ] && [ "$first" != "narnat $APP_VERSION" ]; then
    warn "exe 版本 '$first' 与 main.py 的版本 'narnat $APP_VERSION' 不一致（用 --skip-exe 时属正常）。"
  fi
}

selfcheck_vsix() {
  local vsix="$DIST/$VSIX_NAME" exe="$DIST/$EXE_NAME" rc=0
  "$PY" - "$(win_path "$vsix")" "$(win_path "$exe")" <<'PY' || rc=$?
import os
import sys
import zipfile

vsix, exe = sys.argv[1], sys.argv[2]
entry = "extension/bin/narnat.exe"
try:
    with zipfile.ZipFile(vsix) as z:
        names = z.namelist()
        if entry not in names:
            print("FAIL vsix 内含 %s → 未找到（共 %d 个条目）" % (entry, len(names)))
            sys.exit(1)
        inner = z.getinfo(entry).file_size
except Exception as exc:
    print("FAIL vsix 内含 %s → 读取 %s 失败: %s" % (entry, vsix, exc))
    sys.exit(1)

outer = os.path.getsize(exe)
if inner == outer:
    print("PASS vsix 内含 %s 且大小与 dist/%s 一致 → %d 字节"
          % (entry, os.path.basename(exe), inner))
    sys.exit(0)
print("FAIL vsix 内含 %s 大小不一致 → 包内 %d 字节 / dist %d 字节"
      % (entry, inner, outer))
sys.exit(1)
PY
  if [ "$rc" -ne 0 ]; then
    CK_FAILED=$((CK_FAILED + 1))
  fi
}

list_dist() {
  printf 'dist/ 清单：\n'
  "$PY" - "$(win_path "$DIST")" <<'PY'
import os
import sys

d = sys.argv[1]
for name in sorted(os.listdir(d)):
    p = os.path.join(d, name)
    if os.path.isfile(p):
        size = os.path.getsize(p)
        print("  %-32s %12d 字节  %7.1f MB" % (name, size, size / 1048576.0))
PY
}

# ── 主流程 ───────────────────────────────────────────────────────────────────

cd "$ROOT"

EXE_SRC="$ROOT/output/$EXE_NAME"
if [ -n "$SKIP_EXE_PATH" ]; then
  EXE_SRC="$SKIP_EXE_PATH"
fi
EXT_VERSION=""
APP_VERSION=""
VSIX_NAME=""
TOTAL="2"
if [ "$MODE" = "all" ]; then
  TOTAL="4"
fi

printf '\n== Narnat 构建 ==\n'
printf '  仓库  : %s\n' "$ROOT"
printf '  模式  : %s\n' "$MODE"
printf '  产物  : %s\n' "$DIST"
if [ "$DRY_RUN" = "1" ]; then
  printf '  执行  : 仅打印命令（dry-run）\n'
fi

check_env
APP_VERSION="$(read_app_version)" || APP_VERSION=""
EXT_VERSION="$(read_ext_version)" || EXT_VERSION=""
if [ -z "$EXT_VERSION" ]; then
  printf '[错误] 无法从 %s 读取 version 字段。\n' "$EXT_DIR/package.json" >&2
  exit 1
fi
VSIX_NAME="narnat-vscode-$EXT_VERSION.vsix"
printf '  版本  : 后端 %s / 扩展 %s\n' "${APP_VERSION:-未知}" "$EXT_VERSION"

# --vsix-only 必须已有 exe：缺了要给明确指引，而不是让 vsce 报天书
# （dry-run 不做前置检查，只提示，否则连「会跑哪些命令」都看不到）
if [ "$MODE" = "vsix" ] && [ -z "$SKIP_EXE_PATH" ] && [ ! -f "$EXE_SRC" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    warn "output/$EXE_NAME 不存在；真实运行会在此退出（退出码 4）。"
  else
    printf '\n[错误] 未找到 %s，无法打包内嵌 exe 的 vsix。\n' "$EXE_SRC" >&2
    printf '       请先编译后端，任选其一：\n' >&2
    printf '         bash build.sh                          # 一键：编译 exe + 打包 vsix\n' >&2
    printf '         python build.py                        # 只编译 exe（约 10~30 分钟）\n' >&2
    printf '         bash build.sh --skip-exe <已有exe路径>  # 用别的 exe 打包\n' >&2
    exit 4
  fi
fi
if [ -n "$SKIP_EXE_PATH" ] && [ ! -f "$SKIP_EXE_PATH" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    warn "--skip-exe 指定的 exe 不存在：$SKIP_EXE_PATH（dry-run 不校验）"
  else
    printf '\n[错误] --skip-exe 指定的 exe 不存在：%s\n' "$SKIP_EXE_PATH" >&2
    printf '       请给出绝对路径（例如 bash build.sh --skip-exe D:/build/narnat.exe）。\n' >&2
    exit 4
  fi
fi

if [ "$MODE" = "all" ]; then
  step "1/$TOTAL" "同步界面（design/ui-full.html → media/）"
  RUN "$NODE" "$EXT_DIR/scripts/sync-ui.js"
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "exe" ]; then
  if [ "$MODE" = "all" ]; then
    step "2/$TOTAL" "编译单文件 exe（python build.py）"
  else
    step "1/$TOTAL" "编译单文件 exe（python build.py）"
  fi
  build_args=("$PY" "$ROOT/build.py" "--runtime" "$RUNTIME")
  if [ -n "$JOBS" ]; then
    build_args+=("--jobs" "$JOBS")
  fi
  if [ "$ASSUME_YES" = "1" ]; then
    build_args+=("--assume-yes")
  fi
  RUN "${build_args[@]}"
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "vsix" ]; then
  if [ "$MODE" = "all" ]; then
    step "3/$TOTAL" "打包 VSCode 插件 vsix（内嵌 exe）"
  else
    step "1/$TOTAL" "打包 VSCode 插件 vsix（内嵌 exe）"
  fi
  RUN "$NODE" "$EXT_DIR/scripts/package-vsix.js" --exe "$(win_path "$EXE_SRC")"
fi

if [ "$MODE" = "all" ]; then
  step "4/$TOTAL" "汇总产物到 dist/ 并自检"
else
  step "2/$TOTAL" "汇总产物到 dist/ 并自检"
fi

RUN rm -rf "$DIST"
RUN mkdir -p "$DIST"
RUN cp -f "$EXE_SRC" "$DIST/$EXE_NAME"
RUN chmod +x "$DIST/$EXE_NAME"
if [ "$MODE" != "exe" ]; then
  RUN cp -f "$EXT_DIR/narnat-vscode.vsix" "$DIST/$VSIX_NAME"
fi

if [ "$DRY_RUN" = "1" ]; then
  printf '[dry-run] （将执行自检：dist/%s -v' "$EXE_NAME"
  if [ "$MODE" = "exe" ]; then
    printf '；本次 --exe-only 不打包 vsix，跳过 vsix 校验）\n'
  else
    printf '、比对 vsix 内 extension/bin/narnat.exe 与 dist 的大小）\n'
  fi
  printf '[dry-run] （将打印 %s 的文件清单）\n' "$DIST"
  printf '\n[dry-run] 未执行任何构建，以上为完整命令序列。\n'
  exit 0
fi

printf '\n── 自检 ──\n'
selfcheck_exe
if [ "$MODE" = "exe" ]; then
  printf 'SKIP vsix 校验（--exe-only 本次未打包 vsix）\n'
else
  selfcheck_vsix
fi

printf '\n'
list_dist

if [ "$CK_FAILED" -ne 0 ]; then
  printf '\nFAILED %d 项自检未通过\n' "$CK_FAILED" >&2
  exit 1
fi

printf '\nALL_PASS\n'
if [ "$MODE" = "all" ] || [ "$MODE" = "vsix" ]; then
  printf '装插件：code --install-extension "%s"\n' "$(win_path "$DIST/$VSIX_NAME")"
fi
if [ "$MODE" = "all" ] || [ "$MODE" = "exe" ]; then
  printf '用 exe：%s\n' "$(win_path "$DIST/$EXE_NAME")"
fi
