"""核心水印逻辑：EXIF 时间读取、水印渲染、批量处理。

完全本地运行，不依赖网络。
"""

import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

# 注册 HEIF/HEIC 支持（iPhone 照片）
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}

# EXIF 时间标签（按优先级排列）
# 36867 = DateTimeOriginal（真实拍摄时间）
# 36868 = DateTimeDigitized（数字化时间）
# 306   = DateTime（最后修改时间）
_EXIF_TIME_TAGS = (36867, 36868, 306)

EXIF_TIME_FORMAT = "%Y:%m:%d %H:%M:%S"

# 系统字体路径（macOS / Linux / Windows，CJK 优先，兼容"2026年"这类中文格式）
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",                      # macOS 中文字体
    "/System/Library/Fonts/Hiragino Sans GB.ttc",              # macOS 中文字体备选
    "/System/Library/Fonts/Helvetica.ttc",                     # macOS 西文
    "/System/Library/Fonts/Supplemental/Arial.ttf",            # macOS 西文备选
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux 中文
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",            # Linux 中文备选
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",         # Linux 西文
    "C:/Windows/Fonts/msyh.ttc",                               # Windows 微软雅黑
    "C:/Windows/Fonts/arial.ttf",                              # Windows 西文
]

_font_cache = {}


def is_supported(path: str) -> bool:
    """判断文件是否为支持的图片格式。"""
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def get_capture_time(path: str) -> tuple[datetime | None, str]:
    """获取照片拍摄时间。

    返回 (时间对象, 来源说明)。
    优先级：EXIF DateTimeOriginal > EXIF DateTimeDigitized > EXIF DateTime > 文件修改时间。
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            # getexif() 的顶层不包含 EXIF IFD 里的标签，需要单独取
            try:
                from PIL.Image import Exif

                ifd = exif.get_ifd(0x8769)  # Exif IFD
                for tag in _EXIF_TIME_TAGS:
                    if tag in ifd:
                        return datetime.strptime(ifd[tag], EXIF_TIME_FORMAT), "EXIF"
            except Exception:
                pass
            for tag in _EXIF_TIME_TAGS:
                if tag in exif:
                    return datetime.strptime(exif[tag], EXIF_TIME_FORMAT), "EXIF"
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)), "文件时间"
    except OSError:
        return None, "无法获取"


def get_gps(path: str) -> tuple[float, float] | None:
    """读取照片 GPS 坐标，返回 (纬度, 经度) 十进制；无 GPS 返回 None。

    EXIF GPS IFD (0x8825) 内:
      1 = 纬度参考 (N/S), 2 = 纬度, 3 = 经度参考 (E/W), 4 = 经度
    坐标为 (度, 分, 秒) 三元组（秒可为分数），兼容 float / 两元分数格式。
    """
    def _to_float(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, (tuple, list)):
            if len(v) == 3:  # (度, 分, 秒)
                d, m, s = float(v[0]), float(v[1]), float(v[2])
                return d + m / 60 + s / 3600
            if len(v) == 2:  # (分子, 分母)
                return float(v[0]) / float(v[1])
        return float(v)

    try:
        with Image.open(path) as img:
            gps = img.getexif().get_ifd(0x8825)
            lat_v, lat_ref = gps.get(2), gps.get(1)
            lon_v, lon_ref = gps.get(4), gps.get(3)
            if not (lat_v and lat_ref and lon_v and lon_ref):
                return None
            lat = _to_float(lat_v) * (-1 if str(lat_ref).upper() == "S" else 1)
            lon = _to_float(lon_v) * (-1 if str(lon_ref).upper() == "W" else 1)
            return round(lat, 6), round(lon, 6)
    except Exception:
        return None


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按字号查找可用系统字体，结果缓存。"""
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default(size=size) if size else ImageFont.load_default()
    _font_cache[size] = font
    return font


POSITIONS = ("右下角", "左下角", "右上角", "左上角", "底部居中", "顶部居中", "左侧居中", "右侧居中")

# 字号倍率：None = 自动（按图片短边缩放），其余为相对倍数
FONT_SIZES = {"自动": None, "小": 0.7, "标准": 1.0, "大": 1.4, "特大": 1.8}


def add_watermark(
    image_path: str,
    output_path: str,
    time_format: str = "%Y-%m-%d %H:%M",
    position: str = "右下角",
    quality: int = 95,
    location: str | None = None,
    font_scale: float | None = None,
    custom_text: str | None = None,
) -> None:
    """给单张照片加水印并保存到 output_path。

    custom_text 非空时，水印直接显示这段文字（适用于 EXIF 丢失、
    需手动补记拍摄信息的照片），不再读取时间与地点。
    location 为已匹配好的地点文本（如 "北京市 朝阳区"），
    非空时水印显示为 "2023-01-24 16:47 · 北京市 朝阳区"。
    font_scale 为 None 时自动按图片尺寸缩放；为数字时按倍率缩放（1.0=标准）。
    """
    if custom_text and custom_text.strip():
        time_str = custom_text.strip()
    else:
        capture_time, _ = get_capture_time(image_path)
        if capture_time is None:
            raise ValueError("无法获取照片时间")
        time_str = capture_time.strftime(time_format)
        if location:
            time_str = f"{time_str} · {location}"

    with Image.open(image_path) as img:
        # 应用 EXIF Orientation：手机竖拍照片的原始像素是横向的，
        # 靠 EXIF 方向标签转正显示。加水印前先物理转正像素，
        # 否则另存后标签丢失，照片会"转向"。
        img = ImageOps.exif_transpose(img)
        rgba = img.convert("RGBA")
        w, h = rgba.size

        # 字号随图片尺寸缩放：短边约 35~40 倍字号
        auto_size = max(14, int(min(w, h) / 34))
        if font_scale is None:
            font_size = auto_size
        else:
            font_size = max(10, int(auto_size * font_scale))
        font = _get_font(font_size)

        overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        bbox = draw.textbbox((0, 0), time_str, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        pad_x = font_size * 6 // 10
        pad_y = font_size * 4 // 10
        margin = font_size * 6 // 10
        rect_w = text_w + pad_x * 2
        rect_h = text_h + pad_y * 2

        if position == "右下角":
            x, y = w - rect_w - margin, h - rect_h - margin
        elif position == "左下角":
            x, y = margin, h - rect_h - margin
        elif position == "右上角":
            x, y = w - rect_w - margin, margin
        elif position == "左上角":
            x, y = margin, margin
        elif position == "底部居中":
            x, y = (w - rect_w) // 2, h - rect_h - margin
        elif position == "顶部居中":
            x, y = (w - rect_w) // 2, margin
        elif position == "左侧居中":
            x, y = margin, (h - rect_h) // 2
        elif position == "右侧居中":
            x, y = w - rect_w - margin, (h - rect_h) // 2
        else:
            x, y = w - rect_w - margin, h - rect_h - margin

        # 半透明白底 + 深色文字，保证任何背景下可读
        draw.rectangle([x, y, x + rect_w, y + rect_h], fill=(255, 255, 255, 175))
        draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), time_str, fill=(30, 30, 30, 255), font=font)

        result = Image.alpha_composite(rgba, overlay)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        ext = os.path.splitext(output_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            result.convert("RGB").save(output_path, format="JPEG", quality=quality)
        elif ext in (".heic", ".heif"):
            result.convert("RGB").save(output_path, format="HEIF", quality=quality)
        elif ext == ".png":
            result.save(output_path, format="PNG")
        else:
            result.save(output_path)


def scan_folder(folder: str, recursive: bool = True) -> list[str]:
    """扫描文件夹中所有受支持的图片。"""
    files = []
    if recursive:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in ("watermarked",)]
            for n in sorted(names):
                p = os.path.join(root, n)
                if is_supported(p):
                    files.append(p)
    else:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and is_supported(p):
                files.append(p)
    return files


def process_batch(
    paths: list[str],
    output_dir: str,
    time_format: str = "%Y-%m-%d %H:%M",
    position: str = "右下角",
    show_location: bool = True,
    location_provider=None,
    font_scale: float | None = None,
    custom_text: str | None = None,
    progress_callback=None,
) -> list[tuple[str, bool, str]]:
    """批量处理。输出文件名与原文件一致，保存到 output_dir。

    show_location 为 True 且 location_provider 非 None 时，
    每张照片调用 location_provider(path) 获取地点文本（None 表示无地点）。
    font_scale 为 None 时字号自动缩放；为数字时按倍率缩放。
    custom_text 非空时，全部照片统一使用这段文字作为水印，
    不再读取时间与地点（适用于 EXIF 丢失的场景）。

    返回 [(原路径, 是否成功, 信息), ...]。
    """
    results = []
    total = len(paths)
    for i, path in enumerate(paths):
        filename = os.path.basename(path)
        # HEIC 输出转 JPEG，兼容性更好
        name, ext = os.path.splitext(filename)
        if ext.lower() in (".heic", ".heif"):
            filename = name + ".jpg"
        output_path = os.path.join(output_dir, filename)
        try:
            location = None
            if custom_text and custom_text.strip():
                location = None
            elif show_location and location_provider is not None:
                location = location_provider(path)
            add_watermark(path, output_path, time_format, position,
                          location=location, font_scale=font_scale,
                          custom_text=custom_text)
            results.append((path, True, output_path))
        except Exception as e:  # noqa: BLE001
            results.append((path, False, str(e)))
        if progress_callback:
            progress_callback(i + 1, total)
    return results
