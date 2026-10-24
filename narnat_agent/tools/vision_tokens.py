"""图片 token 估价 —— DeepSeek 视觉网格口径

移植 deepseek-harness 的 image-tokens.ts（其注释声明逐字移植自 DeepSeek 官方
api-docs 的 Token & Token Usage 计算器，v41 配置）：

    图片先放大到至少 544x544 总像素 → 对齐到 14px patch 网格
    → 每轴 3:1 下采样成 token cell → tokens = h*(w+1)+2 → 单图上限 1024

用途两处：
1. Read 工具的句柄文本（告诉模型"这张图约占多少 token"）
2. 压缩时的尾部预算估价（select_cut_index）——图片 token 常是纯文本的数十倍，
   用统一文本估算会让压缩阈值完全失真

为什么必须单独算：token_estimate.estimate_message_tokens 对块列表走
`str(content)` 估价，一张图的 base64 会被估成几万 token，直接误触发压缩。
"""

import math

# 视觉 patch 边长（像素）
PATCH_SIZE = 14
# 每轴 patch → token 的下采样比
DOWNSAMPLE_RATIO = 3
# 单图 token 上限（provider 侧硬约束）
MAX_IMAGE_TOKENS = 1024
# 总像素地板：更小的图会被放大到这么多像素再投影
MIN_PIXELS = 544 * 544
# 一个 token cell 覆盖的像素边长
CELL_SIZE = PATCH_SIZE * DOWNSAMPLE_RATIO


def _int_div(value, divisor):
    return int(math.floor(value / divisor))


def _ceil_div(value, divisor):
    return int(math.floor((value + divisor - 1) / divisor))


def _grid_tokens(grid_h, grid_w):
    """一个网格的 token 数：每行带一个分隔符，外加两个框定 token"""
    return grid_h * (grid_w + 1) + 2


def _grid_cells(padded_length):
    """一个方向上的 token cell 数"""
    return _ceil_div(_int_div(padded_length, PATCH_SIZE), DOWNSAMPLE_RATIO)


def _solve_resize_ratio(height, width, budget):
    """在预算内求解最大保比网格"""
    if height <= 0 or width <= 0:
        return 1, 1, 1, 1, 2
    aspect = float(height) / float(width)
    ideal_grid_width = math.sqrt((budget - 2) / aspect + 0.25) - 0.5
    ideal_grid_height = ideal_grid_width * aspect

    if ideal_grid_width < 1:
        solved_grid_width = 1
        solved_grid_height = _int_div(budget - 2, solved_grid_width + 1)
        best_width = solved_grid_width * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    elif ideal_grid_height < 1:
        solved_grid_height = 1
        solved_grid_width = _int_div(budget - 2, solved_grid_height) - 1
        best_width = solved_grid_width * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    else:
        solved_grid_width = int(ideal_grid_width)
        solved_grid_height = int(ideal_grid_height)
        if solved_grid_width <= 0 or solved_grid_height <= 0:
            return 1, 1, CELL_SIZE, CELL_SIZE, _grid_tokens(1, 1)
        scale = min(solved_grid_width * CELL_SIZE / float(width),
                    solved_grid_height * CELL_SIZE / float(height))
        best_width = int(width * scale / PATCH_SIZE) * PATCH_SIZE
        best_height = int(height * scale / PATCH_SIZE) * PATCH_SIZE

    grid_h = _grid_cells(best_height)
    grid_w = _grid_cells(best_width)
    return grid_h, grid_w, best_height, best_width, _grid_tokens(grid_h, grid_w)


def _safe_resize(height, width, padded_h, padded_w):
    grid_h = _grid_cells(padded_h)
    grid_w = _grid_cells(padded_w)
    direct = grid_h * (grid_w + 1) + 2
    if direct <= MAX_IMAGE_TOKENS:
        return grid_h, grid_w, padded_h, padded_w, direct
    return _solve_resize_ratio(height, width, MAX_IMAGE_TOKENS)


def _resize_once(width, height):
    scaled_w, scaled_h = width, height
    pixels = scaled_w * scaled_h
    if 0 < pixels < MIN_PIXELS:
        scale = math.sqrt(MIN_PIXELS / float(pixels))
        scaled_w = int(scaled_w * scale)
        scaled_h = int(scaled_h * scale)
    padded_w = _ceil_div(scaled_w, PATCH_SIZE) * PATCH_SIZE
    padded_h = _ceil_div(scaled_h, PATCH_SIZE) * PATCH_SIZE
    return _safe_resize(scaled_h, scaled_w, padded_h, padded_w)


def estimate_image_tokens(width: int, height: int) -> int:
    """估算一张图在 DeepSeek 侧占用的视觉 token 数（不超过 1024）。

    width/height 为**发送给模型的尺寸**（即归一化后尺寸），不是原图尺寸。
    非法尺寸返回 0（调用方按"未知"处理）。
    """
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return 0
    if width <= 0 or height <= 0:
        return 0

    result = _resize_once(width, height)
    for _ in range(1, 10):
        nxt = _resize_once(result[3], result[2])
        if nxt[3] == result[3] and nxt[2] == result[2]:
            return result[4]
        result = nxt
    return result[4]


def estimate_refs_tokens(refs) -> int:
    """估算一组图片引用（消息的 images 字段）的总 token"""
    total = 0
    for ref in refs or ():
        if isinstance(ref, dict):
            total += estimate_image_tokens(ref.get("width") or 0, ref.get("height") or 0)
    return total


# 发送端用于反推"该缩到多大"的目标。
# 单图 1024 token 是 provider 上限，留一点余量避免踩边界取整问题。
TARGET_IMAGE_TOKENS = 1000


def request_dimensions(width: int, height: int, target_tokens: int = TARGET_IMAGE_TOKENS):
    """反推"发这个尺寸最省 token 且不丢信息"的目标尺寸。

    当前实现直接返回归一化尺寸——归一化已把长边压到 2048，对应 token 数
    ≈1302 正方形网格内的最大值，已接近 provider 上限。
    保留此函数是为将来按模型精确倒算（不同 provider 网格不同）留接口。
    """
    return int(width), int(height)
