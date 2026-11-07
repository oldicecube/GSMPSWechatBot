"""Bounded image symmetry with composited GIF frames and a reserved transparent index."""
from io import BytesIO
from pathlib import Path
import uuid
import warnings

from PIL import Image

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_FRAMES = 300
MAX_FRAME_PIXELS = 16_000_000
MAX_TOTAL_PIXELS = 40_000_000


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


def save_symmetry(data, mode, output_dir):
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError('图片或表情包为空，或超过 20 MB')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            pixels = source.width * source.height
            if pixels > MAX_FRAME_PIXELS or pixels > MAX_TOTAL_PIXELS:
                raise ValueError('图片尺寸或动画帧数过大，请发送较小的表情包')
            animated = bool(getattr(source, 'is_animated', False))
            output = BytesIO()
            if animated:
                loop = source.info.get('loop')
                duration_default = source.info.get('duration', 100)
                images, durations = [], []
                frame_limit = min(MAX_FRAMES, MAX_TOTAL_PIXELS // max(1, pixels))
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
                    images.append(_gif_palette(symmetry_frame(source, mode)))
                options = {'loop': int(loop)} if loop is not None else {}
                images[0].save(output, 'GIF', save_all=True, append_images=images[1:],
                    duration=durations, disposal=2, transparency=0, background=0,
                    optimize=False, **options)
                suffix = '.gif'
            else:
                symmetry_frame(source, mode).save(output, 'PNG')
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
