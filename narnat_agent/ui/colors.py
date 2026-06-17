"""
ANSI 颜色常量与配色管理

颜色常量和写入函数统一在 output.py 中定义，此模块从 output 导入并保留 apply_style。
"""

import os

# ── 从 output.py 导入颜色常量和写入函数 ──
from ..output import (
    _Color, RST, BLD, DIM, GRY, CYN, GRN, YLW, RED, BLU, MAG, ORG, BG8, WHT, WHT7,
    R, B, D, G, C, E, Y, X, U, M, O, BG, W, W7,
    STYLE_KEY_MAP as _STYLE_KEY_MAP,
    _stdout_lock, write as _stdout_write, try_write as _stdout_try_write,
)


def apply_style(config) -> bool:
    """从 AppConfig 加载自定义颜色和UI配置。

    Args:
        config: AppConfig 对象（新接口）或 narnat_dir 字符串（兼容旧接口）
    """
    import json
    from .. import output as _output

    # 兼容旧接口：传入字符串路径时从 style.json 读取
    if isinstance(config, str):
        narnat_dir = config
        path = os.path.join(narnat_dir, "style.json")
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False
    else:
        # 新接口：从 AppConfig.ui 读取
        data = config.ui.colors if hasattr(config, 'ui') else {}
        # 设置显示开关
        if hasattr(config, 'ui'):
            _output.SHOW_COST = config.ui.show_cost
            _output.SHOW_BALANCE = config.ui.show_balance
            _output.MAX_TOKENS = config.ui.max_output_tokens

    def _hex_to_ansi(hex_str: str, bg: bool = False) -> str:
        h = hex_str.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        code = "48" if bg else "38"
        return f"\x1b[{code};2;{r};{g};{b}m"

    for key, (var_name, is_bg) in _STYLE_KEY_MAP.items():
        if key in data:
            getattr(_output, var_name)._value = _hex_to_ansi(data[key], bg=is_bg)
    if isinstance(config, str):
        # 旧接口兼容
        if "显示费用" in data:
            _output.SHOW_COST = bool(data["显示费用"])
        if "显示余额" in data:
            _output.SHOW_BALANCE = bool(data["显示余额"])
        if "最大输出token数" in data:
            _output.MAX_TOKENS = int(data["最大输出token数"])
    return True
