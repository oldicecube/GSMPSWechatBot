"""Bounded image symmetry with composited GIF frames and a reserved transparent index."""
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
import math
import random
import uuid
import warnings

from PIL import Image

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_FRAMES = 300
MAX_FRAME_PIXELS = 16_000_000
MAX_TOTAL_PIXELS = 40_000_000
MAX_RANDOM_PASSES = 50
MAX_TRANSFORM_PIXELS = 160_000_000


@dataclass(frozen=True)
class SymmetryAxis:
    angle: float
    x: float
    y: float
    keep_positive: bool


def random_axes(size, iterations, rng=None):
    if type(iterations) is not int or not 1 <= iterations <= MAX_RANDOM_PASSES:
        raise ValueError(f'随机对称次数必须是 1–{MAX_RANDOM_PASSES} 的整数')
    rng = rng or random
    w, h = size
    axes = []
    for _ in range(iterations):
        angle = rng.uniform(0.0, math.pi)
        x, y = rng.uniform(0.0, w), rng.uniform(0.0, h)
        nx, ny = -math.sin(angle), math.cos(angle)
        # Keep the side containing the canvas centre, reducing empty output
        # when the random axis passes near an edge of the image.
        keep_positive = nx * (w / 2 - x) + ny * (h / 2 - y) >= 0
        axes.append(SymmetryAxis(angle, x, y, keep_positive))
    return tuple(axes)


def reflect_axis(image, axis):
    """Keep one half-plane and mirror it to the other, using pixel centres."""
    image = image.convert('RGBA')
    w, h = image.size
    nx, ny = -math.sin(axis.angle), math.cos(axis.angle)
    offset = nx * axis.x + ny * axis.y
    a, b, e = 1 - 2 * nx * nx, -2 * nx * ny, 1 - 2 * ny * ny
    mirrored = image.transform(image.size, Image.Transform.AFFINE,
        (a, b, 2 * offset * nx, b, e, 2 * offset * ny),
        resample=Image.Resampling.NEAREST, fillcolor=(0, 0, 0, 0))
    # Project pixel centres onto a two-pixel [0, 255] mask. All samples stay
    # inside it; the boundary at y=1 exactly selects a half-plane. This avoids
    # polygon rasterization retaining pixels from the wrong side of the axis.
    sign = 1 if axis.keep_positive else -1
    scale = 1.0 / (abs(nx) * w + abs(ny) * h + abs(offset) + 1)
    selector = Image.new('L', (1, 2), 0)
    selector.putpixel((0, 1), 255)
    mask = selector.transform(image.size, Image.Transform.AFFINE,
        (0, 0, 0.5, sign * nx * scale, sign * ny * scale, 1 - sign * offset * scale),
        resample=Image.Resampling.NEAREST)
    return Image.composite(image, mirrored, mask)


def random_symmetry_frame(image, axes):
    result = image
    for axis in axes:
        result = reflect_axis(result, axis)
    return result


def symmetry_frame(image, mode):
    image = image.convert('RGBA')
    w, h = image.size
    canvas = Image.new('RGBA', image.size, (0, 0, 0, 0))
    if mode in {'left_right', 'right_left'}:
        half = w // 2
        start = half + w % 2
        if mode == 'left_right':
            side = image.crop((0, 0, half, h))
            canvas.paste(side, (0, 0))
            canvas.paste(side.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (start, 0))
        else:
            side = image.crop((start, 0, w, h))
            canvas.paste(side, (start, 0))
            canvas.paste(side.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, 0))
        if w % 2:
            canvas.paste(image.crop((half, 0, half + 1, h)), (half, 0))
    elif mode in {'up_down', 'down_up'}:
        half = h // 2
        start = half + h % 2
        if mode == 'up_down':
            side = image.crop((0, 0, w, half))
            canvas.paste(side, (0, 0))
            canvas.paste(side.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, start))
        else:
            side = image.crop((0, start, w, h))
            canvas.paste(side, (0, start))
            canvas.paste(side.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, 0))
        if h % 2:
            canvas.paste(image.crop((0, half, w, half + 1)), (0, half))
    else:
        raise ValueError('不支持的对称方向')
    return canvas


def _gif_palette(frame):
    # GIF supports binary transparency. Index 0 is reserved even when opaque
    # black is present, and every full composited frame gets its own palette.
    quantized = frame.convert('RGB').quantize(colors=255, dither=Image.Dither.NONE)
    palette = quantized.getpalette()[:765]
    indexed = quantized.point(lambda value: value + 1)
    indexed.putpalette(([0, 0, 0] + palette + [0] * 768)[:768])
    indexed.paste(0, mask=frame.getchannel('A').point(lambda value: 255 if value < 128 else 0))
    indexed.info['transparency'] = 0
    return indexed


def save_symmetry(data, mode, output_dir, *, iterations=1, rng=None):
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError('图片或表情包为空，或超过 20 MB')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            pixels = source.width * source.height
            if pixels > MAX_FRAME_PIXELS or pixels > MAX_TOTAL_PIXELS:
                raise ValueError('图片尺寸或动画帧数过大，请发送较小的表情包')
            axes = random_axes(source.size, iterations, rng) if mode == 'random' else ()
            passes = len(axes) or 1
            pixel_budget = min(MAX_TOTAL_PIXELS, MAX_TRANSFORM_PIXELS // passes)
            if pixels > pixel_budget:
                raise ValueError('图片尺寸与随机次数的组合过大，请减少随机次数或缩小图片')
            def transform(frame):
                return random_symmetry_frame(frame, axes) if mode == 'random' else symmetry_frame(frame, mode)
            animated = bool(getattr(source, 'is_animated', False))
            output = BytesIO()
            if animated:
                loop = source.info.get('loop')
                duration_default = source.info.get('duration', 100)
                images, durations = [], []
                frame_limit = min(MAX_FRAMES, pixel_budget // max(1, pixels))
                for index in range(frame_limit + 1):
                    # seek() decodes the previous frame's disposal and composes
                    # local frame rectangles; mirroring raw patches leaves trails.
                    try:
                        source.seek(index)
                    except EOFError:
                        break
                    if index == frame_limit:
                        raise ValueError('图片尺寸或动画帧数过大，请发送较小的表情包')
                    durations.append(int(source.info.get('duration', duration_default)))
                    images.append(_gif_palette(transform(source)))
                options = {'loop': int(loop)} if loop is not None else {}
                images[0].save(output, 'GIF', save_all=True, append_images=images[1:],
                    duration=durations, disposal=2, transparency=0, background=0,
                    optimize=False, **options)
                suffix = '.gif'
            else:
                transform(source).save(output, 'PNG')
                suffix = '.png'
            if output.tell() > MAX_INPUT_BYTES:
                raise ValueError('对称结果超过 20 MB，请发送较小的表情包')
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('symmetry_' + uuid.uuid4().hex + suffix)
    try:
        path.write_bytes(output.getvalue())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path)
