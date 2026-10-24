"""界面配色快照 —— 把 narnat.json "界面" 分组解析成 webview 能直接用的语义色。

## 为什么需要这一层

终端形态的配色早就由 narnat.json 控制（`output.apply_style`），但 VSCode 面板
完全没用它：AI 输出的文字只有 `--fg` 派生的 92% / 80% 两档灰，只有明度差、
没有色相，整屏看起来「一片黑」。用户对照终端形态后的原话：
「终端模式的输出文字都是由 narnat.json 里的主题控制的，非常的好；而插件里
都是一片黑色，视觉会疲劳」。

所以这里把同一份配置解析成**语义角色 → 颜色**的扁平表，交给 webview 落地成
CSS 变量。解析规则与终端侧**完全一致**（默认配方、色板引用、非法值容错），
单一定义源就是 `output.py` 的 `_BASE_DEFS` / `_DERIVED_DEFS` —— 这里不复制
任何一份默认值，否则两套形态的配色迟早分叉。

## 只给「内容层」，不给「框架层」

面板底色、顶栏、输入框仍跟 VSCode 主题走（用户明确要求）。这里的颜色只用于
内容层：markdown 正文、代码块、diff、状态、工具行语义色。理由是面板本身是
VSCode 的一部分，底色若用配置里的 `卡片背景`，会在侧边栏里嵌出「另一块应用」。

## 亮度重算不在后端做

配置里的颜色是按**终端底色**选的。用户切到浅色 VSCode 主题后，同一批颜色
可能糊在背景上。但后端不知道当前面板底色（那是宿主的事），所以亮度适配
放在 webview 侧做：只取色相与饱和度，亮度按当前底色重算到满足对比度。
后端只负责如实交出「配置说应该是什么颜色」。
"""

from typing import Any, Dict, List

from ..output import _BASE_DEFS, _DERIVED_DEFS, _is_valid_hex

# 配方里的样式 token（与 output._STYLE_MAP 的键一致）
_STYLE_TOKENS = ("bold", "dim", "italic", "underline")


def _default_base_hex() -> Dict[str, str]:
    """基础色默认值（直接取 output 的单一定义源）。"""
    return {name: hexval for name, hexval, _is_bg in _BASE_DEFS}


def _resolve_ref(ref: str, palette: Dict[str, str], base: Dict[str, str]) -> str:
    """把一个颜色引用名（调色板名 / 基础色角色 / #hex）解析成 hex。

    解析顺序与 output.apply_style 一致：调色板 → 基础色角色 → 字面 hex。
    解析不出来返回空串（调用方保留默认）。
    """
    token = str(ref or "").strip()
    if not token:
        return ""
    if token.startswith("#"):
        return token if _is_valid_hex(token) else ""
    if token in palette:
        return palette[token]
    if token in base:
        return base[token]
    return ""


def _parse_recipe(recipe: str, palette: Dict[str, str], base: Dict[str, str]) -> Dict[str, Any]:
    """解析一条配方（如 "bold 红"、"次文字 bg:卡片背景"、"#5EEAD4"）。

    @returns {"c": 前景 hex|"", "bg": 背景 hex|"", "s": ["bold","dim"]}
    """
    out: Dict[str, Any] = {"c": "", "bg": "", "s": []}
    for tok in str(recipe or "").strip().split():
        if tok in _STYLE_TOKENS:
            out["s"].append(tok)
        elif tok.startswith("bg:"):
            out["bg"] = _resolve_ref(tok[3:], palette, base)
        elif tok.startswith("#") or tok in palette or tok in base:
            hexval = _resolve_ref(tok, palette, base)
            if hexval:
                out["c"] = hexval
    return out


def _section_dict(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    sec = raw.get(name)
    return sec if isinstance(sec, dict) else {}


def snapshot(raw_ui: Any) -> Dict[str, Any]:
    """把 "界面" 分组（UIConfig.raw 的形态）解析成语义色表。

    @param raw_ui: narnat.json "界面" 分组（已做中文键 → 英文键映射）。
                   任意非 dict 输入都按「全默认」处理，绝不抛异常 ——
                   配置手误不该让面板白屏。
    @returns {
        "colors": {角色键: "#rrggbb"},   # 前景色
        "bgs":    {角色键: "#rrggbb"},   # 背景色（只有配方带 bg: 的才有）
        "decor":  {角色键: ["bold", ...]},
        "palette": {颜色名: "#rrggbb"},  # 用户自定义调色板，界面可据此做调色
    }
    """
    if not isinstance(raw_ui, dict):
        raw_ui = {}

    # 1. 用户调色板（"颜色" 段）。跳过 _ 开头的说明键与非法色值 ——
    #    与 output.apply_style 的容错口径一致（非法值保留默认，不崩）。
    palette: Dict[str, str] = {}
    colors_sec = _section_dict(raw_ui, "colors")
    for name, val in colors_sec.items():
        if not isinstance(name, str) or name.startswith("_"):
            continue
        text = str(val)
        if _is_valid_hex(text):
            palette[name] = text

    # 2. 基础色角色：默认值 + "基础色" 段的覆盖
    base = _default_base_hex()
    base_sec = _section_dict(raw_ui, "base_colors")
    for role, ref in base_sec.items():
        if not isinstance(role, str) or role.startswith("_"):
            continue
        if not isinstance(ref, str):
            continue
        hexval = _resolve_ref(ref, palette, base)
        if hexval:
            base[role] = hexval

    # 3. 代码块背景同样可以指向调色板
    codeblock = _section_dict(raw_ui, "codeblock")
    code_bg_ref = codeblock.get("background")
    if isinstance(code_bg_ref, str):
        hexval = _resolve_ref(code_bg_ref, palette, base)
        if hexval:
            base["code_bg"] = hexval

    # 4. 派生角色：用户覆盖优先，未覆盖用默认配方 —— 默认配方直接读
    #    output._DERIVED_DEFS，绝不在这里另抄一份。
    out_colors: Dict[str, str] = {}
    out_bgs: Dict[str, str] = {}
    out_decor: Dict[str, List[str]] = {}
    for _varname, full_key, default_recipe in _DERIVED_DEFS:
        section, short_key = full_key.split(".", 1)
        user = _section_dict(raw_ui, section).get(short_key)
        recipe = user if isinstance(user, str) else default_recipe
        parsed = _parse_recipe(recipe, palette, base)
        if parsed["c"]:
            out_colors[full_key] = parsed["c"]
        if parsed["bg"]:
            out_bgs[full_key] = parsed["bg"]
        if parsed["s"]:
            out_decor[full_key] = parsed["s"]

    # 5. 基础角色也交出去：工具行/折叠行要按「成功绿 / 错误红 / 次文字」
    #    上色，那些不是 markdown 角色，但语义完全一致。
    base_out = {role: hexval for role, hexval in base.items() if _is_valid_hex(hexval)}
    base_bgs = {role: hexval for role, _h, is_bg in _BASE_DEFS
                if is_bg and (hexval := base.get(role)) and _is_valid_hex(hexval)}

    return {
        "colors": out_colors,
        "bgs": out_bgs,
        "decor": out_decor,
        "base": base_out,
        "baseBg": base_bgs,
        "palette": palette,
        "langGroups": _lang_groups(),
    }


def _lang_groups() -> Dict[str, str]:
    """语言 → 代码块配色分组（cyan / yellow / green / magenta / red / blue / gray）。

    直接取终端渲染器的 `RenderConfig.LANG_COLORS`：代码块的语言标签在终端里
    是「python 青、shell 绿、json 紫…」，面板必须用**同一张表** —— 各写一份
    的话，同一段 python 代码在终端和面板里会是两种颜色。
    """
    try:
        from .renderer import RenderConfig

        return dict(RenderConfig.LANG_COLORS)
    except Exception:
        return {}
