"""分层与结构检查 —— 积木架构的机械化护栏。

检查项（全部为硬失败，退出码非零）：
1. 分层依赖：顶层积木之间只许「高层→低层」；同层禁止互引（跨积木一律经 contracts）。
2. 跨模块私有访问：`模块名._x` 形式（import 的模块对象上的下划线属性）。
3. 模块级可变状态：模块顶层的小写/混合大小写赋值（常量须全大写；正则/编译对象、
   白名单除外）。

用法：
    cd v2 && python tests/check_layering.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "narnat_agent"

# 顶层积木 → 层级（数字越小越底层）。新积木加入时在此登记。
LAYERS: dict[str, int] = {
    "contracts": 0,
    "config": 1,
    "llm": 1,
    "messages": 1,
    "output": 1,
    "signal": 1,
    "interrupt": 1,
    "compression": 2,
    "stats": 2,
    "tools": 2,
    "mcp": 2,
    "ui": 2,
    "conversation": 3,
    "sessions": 3,
    "app": 4,
}

# 模块级允许的可变小写名（如无）。键为包内相对路径（POSIX），值为名称集合。
MODULE_STATE_ALLOWLIST: dict[str, set[str]] = {}

# 模块级赋值允许的形态：全大写常量、正则编译结果、dacite/typing 构造等
_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Violation:
    __slots__ = ("path", "lineno", "kind", "detail")

    def __init__(self, path: str, lineno: int, kind: str, detail: str):
        self.path = path
        self.lineno = lineno
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} [{self.kind}] {self.detail}"


def _iter_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _top_block(rel_parts: tuple[str, ...]) -> str | None:
    """取文件所属顶层积木名（包内第一级），__init__.py 也按其所在目录归属。"""
    if not rel_parts:
        return None
    return rel_parts[0]


def _resolve_import_module(node: ast.ImportFrom, rel_parts: tuple[str, ...]) -> str | None:
    """把相对导入解析为包内顶层积木名（如 `..tools.file` → tools）。"""
    if node.level == 0:
        return None  # 绝对导入（标准库/三方），不参与分层检查
    # 当前文件所在包深度（文件相对包的 parts，除去文件名）
    pkg_parts = rel_parts[:-1]
    up = node.level - 1
    base = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
    if node.module:
        base = base + tuple(node.module.split("."))
    return base[0] if base else None


def _imported_module_aliases(tree: ast.AST, rel_parts: tuple[str, ...]) -> dict[str, str]:
    """收集 `import ... as alias` / `from x import module_obj` 的别名 → 包内顶层积木名。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            top = _resolve_import_module(node, rel_parts)
            if top is None:
                continue
            for alias in node.names:
                # from ..tools import registry → alias 名 registry 归 tools 积木
                aliases[(alias.asname or alias.name).split(".")[0]] = top
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("narnat_agent."):
                    seg = alias.name.split(".")[1]
                    aliases[(alias.asname or alias.name.split(".")[-1])] = seg
    return aliases


def check_file(path: Path) -> list[Violation]:
    rel = path.relative_to(PACKAGE)
    rel_parts = rel.parts
    own_top = _top_block(rel_parts)
    if own_top is None or own_top not in LAYERS:
        return []  # 顶层散文件（如 __init__.py）不参与积木检查
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    out: list[Violation] = []

    # 1) 分层依赖
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            top = _resolve_import_module(node, rel_parts)
            if top is None or top == own_top:
                continue
            if top not in LAYERS:
                continue
            if LAYERS[top] >= LAYERS[own_top]:
                out.append(Violation(str(rel), node.lineno, "layer",
                                     f"{own_top}(L{LAYERS[own_top]}) 不得导入 {top}(L{LAYERS[top]})"))

    # 2) 跨模块私有访问：别名模块对象 ._x
    aliases = _imported_module_aliases(tree, rel_parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                tgt = aliases[node.value.id]
                if tgt != own_top:
                    out.append(Violation(str(rel), node.lineno, "private",
                                         f"跨模块访问 {node.value.id}.{node.attr}（{own_top} → {tgt}）"))
    # self 上的下划线属性、类内使用不在此列（合法）

    # 3) 模块级可变状态（顶层赋值的小写名）
    allow = MODULE_STATE_ALLOWLIST.get(rel.as_posix(), set())
    for node in tree.body:
        target_names: list[str] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target_names.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names.append(node.target.id)
        for name in target_names:
            if _CONST_NAME_RE.match(name) or name.startswith("__"):
                continue
            if name in allow:
                continue
            out.append(Violation(str(rel), node.lineno, "module-state",
                                 f"模块级可变名 `{name}`（如需保留请加入 allowlist 并说明理由）"))
    return out


def main() -> int:
    if not PACKAGE.exists():
        print(f"包目录不存在: {PACKAGE}")
        return 2
    violations: list[Violation] = []
    for path in _iter_modules():
        violations.extend(check_file(path))
    if not violations:
        print("OK: 分层/私有访问/模块状态 检查全部通过")
        return 0
    print(f"发现 {len(violations)} 处违规：")
    for v in violations:
        print(" -", v)
    return 1


if __name__ == "__main__":
    sys.exit(main())
