"""DSH 前端工件收集器 —— 从 DSH 仓库构建产物复制静态前端到 Narnat Agent

用法:
    python -m narnat_agent.web.dsh.collect_dsh --dsh D:\\dsh\\deepseek-harness [--out <dir>] [--exclude <id>]...

说明:
- 复制 apps/web/dist（Vite 外壳）到输出目录根
- 扫描 packages/*/*/package.json 中声明 dsh.client(platform=web) 且已构建
  lib/client.js 的包，复制为 plugins/<id>/client.js
- 生成 plugins.json：与 DSH 宿主注入的 window.__DSH_BOOT__ 同构的插件图
  {rev, entries:[{id,url,rev,inject,immediately}]}，rev 为内容 sha1 前 12 位
- --exclude 用于裁剪 Narnat Agent 没有的模块（含其依赖链警告检查）
"""

import argparse
import hashlib
import json
import os
import shutil
import sys


def _sha12(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


# ── 客户端 bundle 补丁（收集时应用，rev 按补丁后内容计算）──
# DSH 浏览器客户端默认用 WebSocket 打开事件流；本适配器以标准库 HTTP 服务器
# 承载，浏览器 WS 在启动连接风暴下存在竞态（后开的流可能"未建立即关闭"）。
# DSH 协议官方提供 SSE 等价载体（AbstractApiClient 默认即 readSse），
# 这里把浏览器覆盖的两个调用点指回 SSE，行为与宿主 SSE 端点完全一致。
BUNDLE_PATCHES = {
    "@deepseek-ai/dsh-client-connection": [
        (b"this.readWebSocket(", b"this.readSse("),
    ],
}


def apply_bundle_patches(pkg_name: str, content: bytes) -> bytes:
    for old, new in BUNDLE_PATCHES.get(pkg_name, []):
        if old not in content:
            print(f"  [warn] {pkg_name}: 补丁目标字节未找到: {old!r}", file=sys.stderr)
        content = content.replace(old, new)
    return content


# ── 预设裁剪：Narnat Agent 没有的能力 → 界面删除 ──
# 保留：layout/sidebar/conversation/tool/trajectory/input-trigger/commands/
#       skill/workspace/model-selection/theme/settings(壳)/传输与模块内核
NARNAT_TRIM_PRESET = [
    # Cordis 配置运行面板
    "@deepseek-ai/dsh-cordis-client-runner",
    "@deepseek-ai/dsh-client-ui-cordis",
    # 目录选择器（宿主目录原语 + 插槽冲突）
    "@deepseek-ai/dsh-client-ui-directory-picker-browse",
    "@deepseek-ai/dsh-client-ui-directory-picker-native",
    # 无目标(goal)/子代理(subagent)/后台任务(jobs)/计划审批(plan)/工作流(workflow)
    "@deepseek-ai/dsh-client-ui-goal",
    "@deepseek-ai/dsh-client-ui-subagent",
    "@deepseek-ai/dsh-client-ui-jobs",
    "@deepseek-ai/dsh-client-ui-plan",
    "@deepseek-ai/dsh-client-ui-workflow-run",
    # 无 agent 预设 / 权限体系
    "@deepseek-ai/dsh-client-ui-agent-preset",
    "@deepseek-ai/dsh-client-ui-permission-presets",
    # 无产出物跟踪 / 用户提问 / 消息反馈
    "@deepseek-ai/dsh-client-ui-deliverables",
    "@deepseek-ai/dsh-client-ui-user-questions",
    "@deepseek-ai/dsh-client-ui-message-feedback",
    # 设置面板（Narnat 配置走 .narnat/config/narnat.json）
    "@deepseek-ai/dsh-client-ui-settings-plugins",
    "@deepseek-ai/dsh-client-ui-settings-plugin-inventory",
    "@deepseek-ai/dsh-client-ui-settings-models",
    "@deepseek-ai/dsh-client-ui-settings-general",
]


def find_client_packages(dsh_root: str):
    """扫描 packages/ 下两级目录，返回 [(package_name, pkg_dir, dsh_client_decl)]"""
    packages_root = os.path.join(dsh_root, "packages")
    found = []
    if not os.path.isdir(packages_root):
        return found
    for group in sorted(os.listdir(packages_root)):
        group_dir = os.path.join(packages_root, group)
        if not os.path.isdir(group_dir):
            continue
        for name in sorted(os.listdir(group_dir)):
            pkg_dir = os.path.join(group_dir, name)
            pj_path = os.path.join(pkg_dir, "package.json")
            if not os.path.isfile(pj_path):
                continue
            try:
                with open(pj_path, "r", encoding="utf-8") as f:
                    pj = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            dsh = pj.get("dsh") or {}
            decl = dsh.get("client")
            if not isinstance(decl, dict):
                continue
            if decl.get("platform") != "web":
                continue
            client_js = os.path.join(pkg_dir, "lib", "client.js")
            if not os.path.isfile(client_js):
                print(f"  [skip] {pj.get('name')}: 未找到 lib/client.js（未构建?）", file=sys.stderr)
                continue
            found.append((pj.get("name"), pkg_dir, decl))
    return found


def collect(dsh_root: str, out_dir: str, excludes):
    """收集工件，返回插件清单 dict {rev, entries}"""
    # 1. 外壳 dist
    dist_dir = os.path.join(dsh_root, "apps", "web", "dist")
    if not os.path.isfile(os.path.join(dist_dir, "index.html")):
        raise SystemExit(f"未找到 DSH 前端构建产物: {dist_dir}\\index.html "
                         f"（先在 DSH 仓库执行 pnpm build）")
    os.makedirs(out_dir, exist_ok=True)
    for item in os.listdir(dist_dir):
        src = os.path.join(dist_dir, item)
        dst = os.path.join(out_dir, item)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    print(f"[ok] 外壳 dist -> {out_dir}")

    # 2. 插件包（先清空旧产物，保证裁剪后无残留）
    plugins_dir = os.path.join(out_dir, "plugins")
    if os.path.isdir(plugins_dir):
        shutil.rmtree(plugins_dir)
    os.makedirs(plugins_dir, exist_ok=True)
    packages = find_client_packages(dsh_root)
    if not packages:
        raise SystemExit("未找到任何 dsh.client 客户端包（packages/ 扫描为空）")

    excluded = set(excludes)
    entries = []
    file_index = {}  # id -> 相对输出目录的 bundle 路径
    copied = 0
    for pkg_name, pkg_dir, decl in packages:
        if pkg_name in excluded:
            continue
        client_js = os.path.join(pkg_dir, "lib", "client.js")
        with open(client_js, "rb") as f:
            content = apply_bundle_patches(pkg_name, f.read())
        rev = _sha12(content)
        rel = pkg_name.lstrip("@/")
        dst = os.path.join(plugins_dir, rel, "client.js")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(content)
        file_index[pkg_name] = f"plugins/{rel}/client.js"
        copied += 1
        entry = {
            "id": pkg_name,
            "url": f"/plugins/{pkg_name}/client.js?rev={rev}",
            "rev": rev,
        }
        inject = decl.get("inject")
        if isinstance(inject, list) and inject:
            entry["inject"] = [i for i in inject if isinstance(i, str)]
        if decl.get("immediately"):
            entry["immediately"] = True
        entries.append(entry)

    entries.sort(key=lambda e: e["id"])
    manifest = {"rev": _sha12(json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")), "entries": entries}

    # 3. 依赖链警告
    kept_ids = {e["id"] for e in entries}
    for e in entries:
        for dep in e.get("inject", []):
            if dep not in kept_ids:
                print(f"  [warn] {e['id']} 依赖被裁剪的模块 {dep}", file=sys.stderr)

    manifest_path = os.path.join(out_dir, "plugins.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    index_path = os.path.join(out_dir, "plugins_index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(file_index, f, ensure_ascii=False, indent=2)
    print(f"[ok] {copied} 个客户端插件包 -> {plugins_dir}")
    print(f"[ok] 插件清单 {len(entries)} 项 -> {manifest_path} (rev={manifest['rev']})")
    if excluded:
        print(f"[ok] 已裁剪模块: {', '.join(sorted(excluded))}")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="收集 DSH 前端工件到 Narnat Agent")
    parser.add_argument("--dsh", required=True, help="DSH 仓库根目录（已 pnpm build）")
    parser.add_argument("--out", default=None, help="输出目录（默认: 本包同级 dsh_static/）")
    parser.add_argument("--preset", choices=["narnat"], default=None,
                        help="预设裁剪：narnat = 删除 Narnat Agent 没有的能力模块")
    parser.add_argument("--exclude", action="append", default=[], metavar="PKG",
                        help="裁剪的客户端包名，可多次指定（@deepseek-ai/dsh-client-...）")
    args = parser.parse_args(argv)

    excludes = list(args.exclude)
    if args.preset == "narnat":
        excludes = list(dict.fromkeys(excludes + NARNAT_TRIM_PRESET))

    out_dir = args.out
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsh_static")
    manifest = collect(args.dsh, out_dir, excludes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
