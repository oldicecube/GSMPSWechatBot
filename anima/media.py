"""Bounded image/animation decoding for ComfyUI references and masks."""
import io
import math
import warnings
from PIL import Image, ImageDraw, ImageOps

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 25_000_000


def png(image):
    buf = io.BytesIO(); image.save(buf, format="PNG"); return buf.getvalue()


def open_first(binary):
    if not isinstance(binary, bytes) or not binary or len(binary) > MAX_BYTES:
        raise ValueError("图片为空或超过20MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(binary)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("图片像素过大")
                animated = getattr(image, "n_frames", 1) > 1
                image.seek(0)
                return ImageOps.exif_transpose(image).convert("RGBA"), animated
    except (OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("无法解码图片或图片像素过大") from exc


def prepare_reference(binary):
    image, animated = open_first(binary)
    original_size = image.size
    factor = min(1, 1024 / max(image.size), math.sqrt(786432 / (image.width * image.height)))
    size = (max(16, int(image.width * factor) // 16 * 16), max(16, int(image.height * factor) // 16 * 16))
    image = image.resize(size, Image.Resampling.LANCZOS)
    alpha = image.getchannel("A")
    transparent = alpha.getextrema()[0] < 255
    background = Image.new("RGBA", size, "white"); background.alpha_composite(image)
    return {"png": png(background.convert("RGB")), "width":size[0], "height":size[1],
            "original_size":original_size, "animated":animated,
            "alpha_mask":png(ImageOps.invert(alpha)) if transparent else None}


def make_mask(reference, binary=None, region=None):
    size = (reference["width"], reference["height"])
    if region is not None:
        x,y,w,h = region
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rectangle((round(x*size[0]/100), round(y*size[1]/100),
            min(size[0]-1, math.ceil((x+w)*size[0]/100)-1), min(size[1]-1, math.ceil((y+h)*size[1]/100)-1)), fill=255)
    elif binary:
        image, _ = open_first(binary)
        if image.size not in {tuple(reference["original_size"]), size}:
            raise ValueError("蒙版尺寸必须与原图相同")
        mask = image.convert("RGB").convert("L").resize(size, Image.Resampling.NEAREST)
        mask = mask.point(lambda p:255 if p >= 128 else 0)
    elif reference.get("alpha_mask"):
        mask = Image.open(io.BytesIO(reference["alpha_mask"])).convert("L")
    else:
        raise ValueError("局部修正需要黑白蒙版或区域参数")
    if mask.getextrema()[1] == 0:
        raise ValueError("蒙版没有白色修改区域")
    return png(mask)
