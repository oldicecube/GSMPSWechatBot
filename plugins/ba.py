import os
from PIL import Image, ImageDraw, ImageFont
from core.sender import send

COMMAND = "/ba"

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
HALO_PATH = os.path.join(BASE_DIR, "assets/images/halo.png")
CROSS_PATH = os.path.join(BASE_DIR, "assets/images/cross.png")
FONT_PATH = os.path.join(BASE_DIR, "assets/fonts/RoGSanSrfStd-Bd.otf")
FALLBACK_FONT_PATH = os.path.join(BASE_DIR, "assets/fonts/GlowSansSC-Normal-Bold.otf")
OUT_DIR = os.path.join(BASE_DIR, "output")

os.makedirs(OUT_DIR, exist_ok=True)

# =========================
# 渲染参数（严格对齐蔚蓝档案标题生成器 JS — settings.ts）
# =========================
CANVAS_HEIGHT = 250            # canvasHeight (N)
CANVAS_WIDTH_DEFAULT = 900     # canvasWidth  ($)
FONT_SIZE = 84                 # fontSize     (ot)
TEXT_BASELINE = 0.68           # textBaseLine (B) — y = height * 0.68 = 170
HORIZONTAL_TILT = -0.4         # horizontalTilt (H) — setTransform(1,0,H,1,0,0)
PADDING_X = 10                 # paddingX (P)
GRAPH_OFFSET_X = -15           # graphOffset.X
GRAPH_OFFSET_Y = 0             # graphOffset.Y

LEFT_COLOR = (18, 138, 250, 255)     # #128AFA
RIGHT_COLOR = (43, 43, 43, 255)      # #2B2B2B
WHITE = (255, 255, 255, 255)

STROKE_WIDTH = 12              # lineWidth (右侧文字描边)

# 空心路径（白色多边形）— 原始坐标在 500x500 参考系，渲染时缩放到 NxN
HOLLOW_PATH = [(284, 136), (321, 153), (159, 410), (148, 403)]


def init(config):
    pass


# =========================
# 辅助函数
# =========================
def _text_width(font, text):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _font_metrics(font):
    """
    获取字体的 ascent / descent。
    对应 JS 中的 TextMetrics.fontBoundingBoxAscent / fontBoundingBoxDescent。
    """
    return font.getmetrics()  # (ascent, descent)


def _get_notdef_bbox(font):
    """获取字体的 .notdef (豆腐块) 字形 bbox, 用于判断字符是否缺失."""
    mask = font.getmask("\uffff")
    return mask.getbbox() if mask else None


def _has_glyph(font, char, notdef_bbox):
    """检测字体是否包含字符的真实字形 (非 .notdef)."""
    mask = font.getmask(char)
    if mask is None:
        return False
    bbox = mask.getbbox()
    if bbox is None:
        return False
    if notdef_bbox is None:
        return True
    # 比较宽高：真实字形与 .notdef 豆腐块差异通常 >3px
    nw = notdef_bbox[2] - notdef_bbox[0]
    nh = notdef_bbox[3] - notdef_bbox[1]
    cw = bbox[2] - bbox[0]
    ch = bbox[3] - bbox[1]
    return abs(cw - nw) > 3 or abs(ch - nh) > 3


def _split_by_font(text, primary_font, fallback_font):
    """
    如果任意一个非 ASCII 字符在主字体中缺失，则所有非 ASCII 统一走 fallback。
    ASCII 字符始终使用主字体。返回 [(text_segment, font), ...]。
    """
    if fallback_font is None:
        return [(text, primary_font)]

    notdef = _get_notdef_bbox(primary_font)

    # 检测是否有非 ASCII 字符在主字体中缺失
    cjk_fallback = False
    for ch in text:
        if ord(ch) > 127 and not _has_glyph(primary_font, ch, notdef):
            cjk_fallback = True
            break

    # 按字体分组
    runs = []
    cur_text = ""
    cur_font = None

    for ch in text:
        if ord(ch) <= 127:
            ch_font = primary_font         # ASCII → 始终主字体
        else:
            ch_font = fallback_font if cjk_fallback else primary_font

        if ch_font is cur_font:
            cur_text += ch
        else:
            if cur_text:
                runs.append((cur_text, cur_font))
            cur_text = ch
            cur_font = ch_font

    if cur_text:
        runs.append((cur_text, cur_font))
    return runs


def _run_width(text, font):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _draw_runs(draw, runs, xy, anchor, **kwargs):
    """
    按字体分组绘制文字，保持正确的字符间距。
    anchor: 'rs' (右对齐-基线) 或 'ls' (左对齐-基线)
    """
    if not runs:
        return
    x, y = xy

    if anchor.startswith("r"):
        # 从右向左排列，确保右边缘对齐 anchor_x
        for text, font in reversed(runs):
            draw.text((x, y), text, font=font, anchor=anchor, **kwargs)
            x -= _run_width(text, font)
    elif anchor.startswith("l"):
        # 从左向右排列，确保左边缘对齐 anchor_x
        for text, font in runs:
            draw.text((x, y), text, font=font, anchor=anchor, **kwargs)
            x += _run_width(text, font)
    else:
        # 其他 anchor 直接整体绘制
        total = "".join(t for t, _ in runs)
        draw.text(xy, total, font=runs[0][1], anchor=anchor, **kwargs)


def _get_target(context):
    if not context:
        return "文件传输助手"
    return context.get("group") or context.get("user") or "文件传输助手"


# =========================
# 水平倾斜（严格模拟 canvas setTransform(1, 0, H, 1, 0, 0)）
#
#   正向映射:  x_screen = x_draw + H * y_draw
#             y_screen = y_draw
#
#   Pillow AFFINE 使用逆映射:
#     x_src = a*x_dst + b*y_dst + c
#     逆:   x_src = x_dst - H*y_dst + offset
#     为使所有输出坐标 >= 0, offset = |H| * height (当 H<0)
# =========================
def _skew(img, tilt=HORIZONTAL_TILT):
    """返回倾斜后的图像。输出宽度 = 原宽 + |H| * height。"""
    w, h = img.size
    shear = abs(int(tilt * h))
    new_w = w + shear

    a, b = 1, -tilt
    c = -shear if tilt < 0 else 0
    d, e, f = 0, 1, 0

    return img.transform(
        (new_w, h),
        Image.AFFINE,
        (a, b, c, d, e, f),
        Image.BICUBIC
    )


def _skewed_x(src_x, src_y, shear, tilt=HORIZONTAL_TILT):
    """
    源图层坐标 (src_x, src_y) 在倾斜输出图中的 x 坐标。
    正向: x_out = x_src + H*y_src + shear
    """
    return int(src_x + tilt * src_y + shear)


# =========================
# 主函数
# =========================
def handle(content, context):
    if content is None:
        return None

    content = str(content).strip()
    # 框架 dispatcher 已剥离命令前缀，content 直接是参数部分
    if not content:
        return "用法: /ba <左侧文字> <右侧文字>\n示例: /ba 蔚蓝 档案"

    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        return "用法: /ba <左侧文字> <右侧文字>\n示例: /ba 蔚蓝 档案"

    textL = parts[0].strip()
    textR = parts[1].strip()

    if not textL or not textR:
        return "左右两侧文字均不能为空"

    # --- 加载字体 ---
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception:
        return "错误: 未找到字体文件 RoGSanSrfStd-Bd.otf"

    try:
        font_fb = ImageFont.truetype(FALLBACK_FONT_PATH, FONT_SIZE)
    except Exception:
        font_fb = None  # fallback 字体缺失不阻塞

    # =========================
    # 1. 画布尺寸（严格对齐 JS setWidth）
    #
    #    JS 原始公式:
    #      textWidthL = metrics.width - (B*N + descent) * H
    #      textWidthR = metrics.width + (B*N - ascent) * H
    #
    #    H=-0.4 时: 左侧变宽、右侧变窄（因为文字倾斜后投影变化）
    # =========================
    ascent, descent = _font_metrics(font)
    baseline_y = CANVAS_HEIGHT * TEXT_BASELINE  # 170.0

    twL_raw = _text_width(font, textL)
    twR_raw = _text_width(font, textR)

    # 倾斜后的屏幕投影宽度
    text_widthL = twL_raw - (baseline_y + descent) * HORIZONTAL_TILT
    text_widthR = twR_raw + (baseline_y - ascent) * HORIZONTAL_TILT

    half = CANVAS_WIDTH_DEFAULT // 2  # 450

    if text_widthL + PADDING_X <= half:
        canvas_wL = half
    else:
        canvas_wL = int(text_widthL + PADDING_X)

    if text_widthR + PADDING_X <= half:
        canvas_wR = half
    else:
        canvas_wR = int(text_widthR + PADDING_X)

    total_w = canvas_wL + canvas_wR
    center_x = canvas_wL                     # 分割点 = halo 定位基准

    text_y = int(baseline_y)                 # 170
    shear = abs(int(HORIZONTAL_TILT * CANVAS_HEIGHT))  # 100

    # skew 后文字锚点落在屏幕上的 x 坐标
    target_screen_x = int(center_x + HORIZONTAL_TILT * text_y)  # center_x - 68

    # =========================
    # 2. 白色画布
    # =========================
    canvas = Image.new("RGBA", (total_w, CANVAS_HEIGHT), WHITE)

    # =========================
    # 3. 左侧文字 — JS: fillText(textL, canvasWidthL, height*B)  textAlign="end"
    #    渲染到透明层 -> 倾斜 -> 贴到画布
    # =========================
    layerL_w = int(twL_raw) + shear + 40
    layerL = Image.new("RGBA", (layerL_w, CANVAS_HEIGHT), (0, 0, 0, 0))
    dL = ImageDraw.Draw(layerL)
    anchorL_x = layerL_w - 5                      # 右对齐锚点

    # JS canvas fillText 默认 textBaseline='alphabetic' → 基线对齐
    # Pillow 用 'rs' = right-baseline (右边缘 + 基线)
    # 逐字检测字体 → 按字体会并 → 分组绘制
    runsL = _split_by_font(textL, font, font_fb)
    _draw_runs(dL, runsL, (anchorL_x, text_y), "rs", fill=LEFT_COLOR)

    skewedL = _skew(layerL)
    anchorL_out = _skewed_x(anchorL_x, text_y, shear)
    pasteL_x = target_screen_x - anchorL_out
    canvas.paste(skewedL, (pasteL_x, 0), skewedL)

    # =========================
    # 4. Halo (光环) — JS: drawImage(halo, center_x-140, 0, 250, 250)
    # =========================
    graph_x = center_x - CANVAS_HEIGHT // 2 + GRAPH_OFFSET_X  # center_x - 140
    graph_y = GRAPH_OFFSET_Y

    try:
        halo_img = Image.open(HALO_PATH).convert("RGBA")
        halo_img = halo_img.resize((CANVAS_HEIGHT, CANVAS_HEIGHT), Image.LANCZOS)
        canvas.paste(halo_img, (graph_x, graph_y), halo_img)
    except Exception as e:
        print(f"[BA] Halo load error: {e}")

    # =========================
    # 5. 右侧文字 — JS: strokeText + fillText(textR, canvasWidthL, height*B)  textAlign="start"
    # =========================
    layerR_w = int(twR_raw) + shear + STROKE_WIDTH * 2 + 40
    layerR = Image.new("RGBA", (layerR_w, CANVAS_HEIGHT), (0, 0, 0, 0))
    dR = ImageDraw.Draw(layerR)
    anchorR_x = STROKE_WIDTH + 5                 # 左对齐锚点（留描边空间）

    # 白色描边 + 深色填充（模拟 JS strokeText -> fillText）
    # JS canvas fillText/strokeText 默认 textBaseline='alphabetic' → 基线对齐
    # Pillow 用 'ls' = left-baseline (左边缘 + 基线)
    # 逐字检测字体 → 按字体会并 → 分组绘制
    runsR = _split_by_font(textR, font, font_fb)
    _draw_runs(dR, runsR, (anchorR_x, text_y), "ls",
               fill=WHITE, stroke_width=STROKE_WIDTH, stroke_fill=WHITE)
    _draw_runs(dR, runsR, (anchorR_x, text_y), "ls", fill=RIGHT_COLOR)

    skewedR = _skew(layerR)
    anchorR_out = _skewed_x(anchorR_x, text_y, shear)
    pasteR_x = target_screen_x - anchorR_out
    canvas.paste(skewedR, (pasteR_x, 0), skewedR)

    # =========================
    # 6. 空心路径 (白色多边形) — JS 在 halo 之后、cross 之前
    #    坐标定义于 500x500 参考系，缩放到 canvasHeight x canvasHeight
    #    JS: for each (hx,hy) in hollowPath -> graph.X + hx/500*N, graph.Y + hy/500*N
    # =========================
    scale = CANVAS_HEIGHT / 500.0  # 0.5
    hollow_px = [(graph_x + int(x * scale), graph_y + int(y * scale))
                 for (x, y) in HOLLOW_PATH]
    d_hollow = ImageDraw.Draw(canvas)
    d_hollow.polygon(hollow_px, fill=WHITE)

    # =========================
    # 7. Cross (十字) — JS: drawImage(cross, graph_x, graph_y, 250, 250)
    # =========================
    try:
        cross_img = Image.open(CROSS_PATH).convert("RGBA")
        cross_img = cross_img.resize((CANVAS_HEIGHT, CANVAS_HEIGHT), Image.LANCZOS)
        canvas.paste(cross_img, (graph_x, graph_y), cross_img)
    except Exception as e:
        print(f"[BA] Cross load error: {e}")

    # =========================
    # 8. 裁剪输出（对齐 JS generateImg）
    #    若文字未超出半宽，裁掉多余空白区域
    #    JS: crop to (textWidthL + textWidthR + paddingX*2)
    # =========================
    if text_widthL + PADDING_X < half or text_widthR + PADDING_X < half:
        crop_w = int(text_widthL + text_widthR + PADDING_X * 2)
        crop_x = half - int(text_widthL) - PADDING_X
        crop_x = max(crop_x, 0)
        crop_w = min(crop_w, canvas.width - crop_x)
        canvas = canvas.crop((crop_x, 0, crop_x + crop_w, CANVAS_HEIGHT))

    # --- 保存 & 发送 ---
    out_path = os.path.join(OUT_DIR, f"ba_{textL}_{textR}.png")
    canvas.save(out_path, "PNG")

    target = _get_target(context)
    ok, err = send(target=target, file_path=out_path, mode="wechat_file")
    os.remove(out_path)
    if not ok:
        return f"图片发送失败: {err}"

    return None
