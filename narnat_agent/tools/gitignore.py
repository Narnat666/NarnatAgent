"""`.gitignore` 规则匹配 —— Grep/Glob 共用的目录遍历过滤器

基于 `pathspec`（PyPI，纯 Python）的薄封装，不自己实现 glob 语义：
gitignore 的边界情况（转义、`**` 语义、`!` 与父目录规则的覆盖顺序）
比看起来绕得多，实测自写版在「`!` 能否救回被忽略目录内的文件」上
反复错了三次才对齐 git，交给 pathspec 维护更稳。

本模块只做三件 pathspec 不做的事：

1. **按目录分层**：每个目录下的 `.gitignore` 只对该目录及子树生效，
   子目录规则覆盖父目录。pathspec 单 spec 不处理层级。
2. **目录判定带尾斜杠**：`match_file('build')` 为 False，
   `match_file('build/')` 才是 True（实测），调用方不必记这个细节。
3. **先判祖先再判自身**：任一级祖先被忽略 → 整棵子树都忽略。
   git 文档原话 "It is not possible to re-include a file if a parent
   directory of that file is excluded"，实测 `git check-ignore` 也是
   这个行为；而 pathspec 单 spec 会让 `!build/keep.txt` 放行
   `build/keep.txt`，与 git 不一致。遍历时目录已被剪枝所以走不到，
   但单路径判断必须自己补这一步。

不支持：`.git/info/exclude`、`core.excludesFile`、`.ignore` 文件
（ripgrep 会读 `.ignore`，git 不读；这里只认 `.gitignore`）。
"""

import os

import pathspec

_GITIGNORE = ".gitignore"

# 空的 spec 复用同一个实例（大多数目录没有 .gitignore）
_EMPTY = pathspec.GitIgnoreSpec.from_lines([])


class GitIgnore:
    """按需加载的 .gitignore 规则集（按目录缓存）。

    判断某个路径时，沿它到搜索根的每一级目录按需读取该级 .gitignore
    并缓存 —— 同一目录只读一次文件，不需要调用方维护遍历栈。
    """

    __slots__ = ("_cache", "_root")

    def __init__(self, root):
        self._root = os.path.abspath(root)
        # {目录绝对路径: GitIgnoreSpec}
        self._cache = {}

    def _spec_of(self, dirpath):
        """该目录的 .gitignore spec（缓存；无文件返回空 spec）。"""
        if dirpath in self._cache:
            return self._cache[dirpath]
        spec = _EMPTY
        try:
            with open(os.path.join(dirpath, _GITIGNORE), "r",
                      encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            if lines:
                spec = pathspec.GitIgnoreSpec.from_lines(lines)
        except (OSError, ValueError):
            spec = _EMPTY
        self._cache[dirpath] = spec
        return spec

    def is_ignored(self, abspath, is_dir):
        """绝对路径是否被忽略。搜索根本身不参与忽略（它是 AI 显式指定的入口）。"""
        path = os.path.abspath(abspath)
        if path == self._root or _rel_under(self._root, path) is None:
            return False

        # 1. 祖先目录：任一级被忽略 → 整棵子树都忽略
        cur = os.path.dirname(path)
        while cur != self._root and _rel_under(self._root, cur) is not None:
            if self._match_one(cur, True):
                return True
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if cur != self._root:
            return False

        # 2. 自身
        return self._match_one(path, is_dir)

    def _match_one(self, path, is_dir):
        """只判这一层路径本身（不看祖先）。

        从最深层目录往搜索根走（深 → 浅）：**第一层命中即定论**，
        这是 git 的优先级（子目录 .gitignore 覆盖父目录）。实测依据：
        根 `.gitignore` 写 `*.pyc`、`sub/.gitignore` 写 `!b.pyc` 时，
        `sub/b.pyc` 不被忽略（`git check-ignore` 也这么判）。

        用 `check_file` 而不是 `match_file`：前者区分「命中忽略规则」
        与「命中 `!` 放行规则」（include=True/False），后者把两者都折算成
        布尔，无法实现跨层覆盖。
        """
        cur = os.path.dirname(path)
        while True:
            rel = _rel_under(cur, path)
            if rel:
                # 目录必须带尾斜杠才能匹配 `build/` 这类规则（pathspec 实测行为）
                res = self._spec_of(cur).check_file(rel + "/" if is_dir else rel)
                if res.include is not None:
                    return res.include
            if cur == self._root:
                return False
            parent = os.path.dirname(cur)
            if parent == cur or _rel_under(self._root, parent) is None:
                return False
            cur = parent


def _rel_under(base, path):
    """path 相对 base 的 posix 形式；不在 base 下返回 ""。"""
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return ""
    if rel == os.curdir:
        return ""
    return rel.replace(os.sep, "/")
