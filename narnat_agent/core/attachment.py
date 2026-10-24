"""附件存储 —— 图片的内容寻址归一化对象库

设计要点（对齐 deepseek-harness 的 attachment-local，砍掉其中对单机 CLI 无用的部分）：

- 存的是**归一化后**的字节，不是用户原图：转 sRGB、单帧、去 EXIF 方向（烤进像素）、
  缩到目标尺寸、按质量阶梯压到字节预算。原图一个字节都不动。
- id = "sha256:" + sha256(归一化字节)，落盘 `<root>/<前两位>/<sha256>`。
  内容寻址天然去重 + 幂等写入：同一张图存十次只占一份。
- 读时校验 digest（发现损坏），但**不设只读属性**：只读在 Windows 上会让
  用户清理/备份 .narnat 时遇到"拒绝访问"，而防篡改不在威胁模型内。
- **不做**（相对 dsh 的削减）：请求版本缓存层、Files API 上传索引、硬链接发布、
  目录 fsync、GC、配额回收、并发共享。这些只服务于 Web 端粘贴与多进程并发，
  终端 + 路径入口的场景下全是负担。

降级原则：任何失败都不抛给调用方，返回 None 或空，由上层换成文本占位继续对话。
"""

import hashlib
import io
import os
import threading

# ── 准入上限（超过即拒绝，不尝试挽救）──
# 这不是"正常使用会遇到的上限"，而是防止解一张 200Mpx 的图直接把内存吃光的护栏。
# 正常用户截图/照片远小于此，一律走下面归一化自动缩放。
MAX_SOURCE_BYTES = 100 * 1024 * 1024     # 100 MiB
MAX_SOURCE_PIXELS = 200_000_000          # 200 Mpx

# ── 归一化目标 ──
# 长边上限：够覆盖 DeepSeek 单图 1024 token 网格解出的最大尺寸（约 1302px 正方形），
# 再大的原图缩到这个上限就够用，多余的像素对模型是纯浪费。
NORMALIZED_MAX_DIMENSION = 2048
# 编码字节目标：超过则按质量阶梯降质。
NORMALIZED_MAX_BYTES = 2 * 1024 * 1024

# 质量阶梯：从高到低逐级尝试，第一个满足字节目标的胜出；全超则取最小的那个。
# 与 dsh 的 [85, 75, 60] 一致，间隔够大，每一级都换来真实体积下降。
ENCODING_QUALITIES = (85, 75, 60)

SUPPORTED_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")

# 支持的媒体类型 → 编码时的扩展名与 mime
_MEDIA_BY_FORMAT = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}

# PIL 是可选依赖：缺失时整个识图能力静默关闭，而不是让程序启动失败。
try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:
    Image = None
    ImageOps = None
    _PIL_OK = False


def pil_available() -> bool:
    """PIL 是否可用（缺失时上层据此关闭识图能力，不报错）"""
    return _PIL_OK


class ImageUnavailable(Exception):
    """附件对象读不到或校验失败。上层据此把该图降级成文本。"""


def attachment_root(data_dir: str) -> str:
    """附件库根目录：<data_dir>/attachments"""
    return os.path.join(data_dir, "attachments")


def object_path(root: str, sha256: str) -> str:
    """内容寻址对象路径：<root>/<前两位>/<sha256>"""
    return os.path.join(root, sha256[:2], sha256)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_ref_prefix(attachment_id: str) -> str:
    """"sha256:xxxx" → "xxxx"；非法返回空串"""
    if not isinstance(attachment_id, str):
        return ""
    if attachment_id.startswith("sha256:"):
        attachment_id = attachment_id[len("sha256:"):]
    if len(attachment_id) != 64:
        return ""
    return attachment_id.lower()


def _encode_with_ladder(image, has_alpha: bool):
    """按质量阶梯编码，返回 (bytes, media_type)。全超预算时取最小的那个。

    有 alpha → WebP（JPEG 不支持透明度）；否则 → JPEG。
    原图是 PNG 且有 alpha 时优先保持无损感的 WebP，避免透明区域被填黑。
    """
    quality_targets = ENCODING_QUALITIES
    smallest = None
    for quality in quality_targets:
        buf = io.BytesIO()
        if has_alpha:
            image.save(buf, format="WEBP", quality=quality, method=0)
            media = "image/webp"
        else:
            image.save(buf, format="JPEG", quality=quality)
            media = "image/jpeg"
        data = buf.getvalue()
        if smallest is None or len(data) < len(smallest[0]):
            smallest = (data, media)
        if len(data) <= NORMALIZED_MAX_BYTES:
            return data, media
    return smallest


def normalize(source: bytes):
    """把用户原图归一化成可长期存放的字节。

    Returns:
        dict: {"data": bytes, "sha256": str, "media_type": str,
               "width": int, "height": int, "source_width": int, "source_height": int}
        None: 不是图片 / 损坏 / 超准入上限 / PIL 不可用

    顺序很重要：先全解码（这一步就拒绝了"改名成 .png 的 txt"），
    再把 EXIF 方向烤进像素（否则后续所有尺寸判断都是错的），
    然后缩到目标尺寸，最后按质量阶梯压字节。
    """
    if not _PIL_OK:
        return None
    if not source or len(source) > MAX_SOURCE_BYTES:
        return None

    try:
        with Image.open(io.BytesIO(source)) as probe:
            source_width, source_height = probe.size
            if source_width * source_height > MAX_SOURCE_PIXELS:
                return None
            fmt = (probe.format or "").upper()
            if fmt not in SUPPORTED_FORMATS:
                return None
            probe.verify()
    except Exception:
        return None

    try:
        img = Image.open(io.BytesIO(source))
        img.load()
    except Exception:
        return None

    # GIF 取首帧（DeepSeek 只吃单帧）；动图信息对模型无用，且会让请求语义含糊
    if getattr(img, "is_animated", False):
        try:
            img.seek(0)
        except Exception:
            pass

    # EXIF 方向烤进像素：不烤的话相机竖拍的图在模型眼里是躺着的，
    # 尺寸、坐标、文字方向全错（codex 保留 EXIF、dsh 烤进像素，我们选后者）
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img = img.convert("RGBA")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    width, height = img.size
    long_edge = max(width, height)
    if long_edge > NORMALIZED_MAX_DIMENSION:
        scale = NORMALIZED_MAX_DIMENSION / float(long_edge)
        new_size = (max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))))
        img = img.resize(new_size, Image.LANCZOS)
        width, height = new_size

    encoded = _encode_with_ladder(img, has_alpha)
    if not encoded:
        return None
    data, media_type = encoded
    return {
        "data": data,
        "sha256": digest(data),
        "media_type": media_type,
        "width": width,
        "height": height,
        "source_width": source_width,
        "source_height": source_height,
    }


_write_lock = threading.Lock()


def store(root: str, source: bytes):
    """归一化并落盘，返回引用 dict；失败返回 None。

    幂等：同一内容重复存入只会写一份（内容寻址 + 已存在即跳过）。
    写入用临时文件 + os.replace，读者永远看不到半写文件。
    """
    prepared = normalize(source)
    if prepared is None:
        return None

    sha = prepared["sha256"]
    path = object_path(root, sha)
    directory = os.path.dirname(path)

    with _write_lock:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            # 已存在：不重写。同一 sha256 的内容必然相同，无需再校验一次
            # （校验留给读取侧，那里的代价才真正值得付）
            pass
        else:
            try:
                os.makedirs(directory, exist_ok=True)
                tmp = path + ".tmp." + str(os.getpid())
                with open(tmp, "wb") as f:
                    f.write(prepared["data"])
                os.replace(tmp, path)
            except OSError:
                return None

    return make_ref(prepared["sha256"], prepared["media_type"],
                    prepared["width"], prepared["height"],
                    prepared.get("source_width"), prepared.get("source_height"))


def make_ref(sha256: str, media_type: str, width: int, height: int,
             source_width=None, source_height=None, name: str = "") -> dict:
    """构造一个图片引用（消息里存的就是它，不是字节）。"""
    ref = {
        "id": "sha256:" + sha256,
        "media_type": media_type,
        "width": int(width) if width else 0,
        "height": int(height) if height else 0,
    }
    if source_width and source_height and (
            int(source_width) != ref["width"] or int(source_height) != ref["height"]):
        # 只在真的缩过时才记录原尺寸，避免每条引用都塞冗余字段
        ref["source_width"] = int(source_width)
        ref["source_height"] = int(source_height)
    if name:
        ref["name"] = name
    return ref


def load(root: str, attachment_id: str) -> bytes:
    """读取对象字节并校验 digest。

    Raises:
        ImageUnavailable: 引用非法 / 文件不存在 / 内容与指纹不符

    调用方一律捕获 ImageUnavailable 并降级成文本，不让它冒泡打断对话
    （dsh 在这里是 fail-closed 直接抛，我们选降级：终端场景下整轮失败的代价太大）。
    """
    sha = _strip_ref_prefix(attachment_id)
    if not sha:
        raise ImageUnavailable("引用格式非法")
    path = object_path(root, sha)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise ImageUnavailable("附件对象不存在")
    except OSError as e:
        raise ImageUnavailable("附件对象读取失败: %s" % e)
    if digest(data) != sha:
        raise ImageUnavailable("附件对象内容与指纹不符")
    return data


def load_base64(root: str, attachment_id: str) -> str:
    """读取对象并返回 base64 字符串（供序列化层直接喂给模型）。"""
    import base64
    return base64.b64encode(load(root, attachment_id)).decode("ascii")


# ── 请求级 base64 缓存 ──
# 每轮请求都要把所有历史图重新编码一遍：10 张 1MB 的图就是每轮 13MB 的字符串构造。
# 按 sha256 缓存，同一张图在整个进程生命周期内只编码一次。
# 上限 64MB（与 codex 的 MAX_IMAGE_CACHE_BYTES 一致），超出按插入顺序淘汰最旧。
_B64_CACHE_LIMIT = 64 * 1024 * 1024
_b64_cache = {}
_b64_cache_bytes = 0
_b64_lock = threading.Lock()


def load_base64_cached(root: str, attachment_id: str) -> str:
    """带缓存的 base64 读取。缓存未命中或对象已变时重新读盘。"""
    global _b64_cache_bytes
    sha = _strip_ref_prefix(attachment_id)
    cache_key = (root, sha)
    with _b64_lock:
        hit = _b64_cache.get(cache_key)
    if hit is not None:
        return hit

    value = load_base64(root, attachment_id)
    size = len(value)
    with _b64_lock:
        global _b64_cache_bytes
        if cache_key not in _b64_cache:
            _b64_cache[cache_key] = value
            _b64_cache_bytes += size
            while _b64_cache_bytes > _B64_CACHE_LIMIT and _b64_cache:
                oldest_key = next(iter(_b64_cache))
                _b64_cache_bytes -= len(_b64_cache.pop(oldest_key))
    return value


def clear_cache() -> None:
    """清空 base64 缓存（测试与会话切换用）"""
    global _b64_cache_bytes
    with _b64_lock:
        _b64_cache.clear()
        _b64_cache_bytes = 0
