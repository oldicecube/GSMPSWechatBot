import os
from PIL import Image, ImageDraw, ImageFont
from services.mc_api import status

# =========================
# 项目根目录
# =========================
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

IMG_PATH = os.path.join(BASE_DIR, "assets/images/serverStatus.png")
FONT_PATH = os.path.join(BASE_DIR, "assets/fonts/Minecraft AE.ttf")
OUT_DIR = os.path.join(BASE_DIR, "output")

os.makedirs(OUT_DIR, exist_ok=True)

font = ImageFont.truetype(FONT_PATH, 65)
list_font = ImageFont.truetype(FONT_PATH, 40)


# =========================
# 工具函数
# =========================
def safe_int(v):
    try:
        return int(v) if v is not None else None
    except:
        return None


def draw_text(draw, x, y, text, fill, font, anchor="ld"):
    shadow = (3, 3)

    # 阴影
    draw.text(
        (x + shadow[0], y + shadow[1]),
        text,
        font=font,
        fill=(0, 0, 0, 180),
        anchor=anchor
    )

    # 正文
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        anchor=anchor
    )


# =========================
# 主函数
# =========================
def generate_status_image():
    data = status() or {}

    # =========================
    # 基础状态（来自 mc_api 已清洗）
    # =========================
    online = bool(data.get("online"))
    latency = safe_int(data.get("latency_ms"))

    online_players = data.get("online_players", 0)
    max_players = data.get("max_players", 0)

    # ✔ 已在 mc_api 清洗过（无需再次处理）
    player_list = data.get("players", [])

    # =========================
    # 文本
    # =========================
    online_text = "在线" if online else "离线"
    latency_text = f"{latency} ms" if latency is not None else "N/A"
    player_count_text = f"{online_players}/{max_players}"

    # =========================
    # 加载图片
    # =========================
    img = Image.open(IMG_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    Y_OFFSET = 5

    # =========================
    # 在线状态
    # =========================
    draw_text(
        draw,
        1434,
        495 + Y_OFFSET,
        online_text,
        (0, 255, 0, 255) if online else (255, 0, 0, 255),
        font
    )

    # =========================
    # 延迟
    # =========================
    latency_color = (
        (150, 150, 150, 255) if latency is None else
        (0, 255, 0, 255) if latency <= 30 else
        (255, 255, 0, 255) if latency <= 100 else
        (255, 0, 0, 255)
    )

    draw_text(
        draw,
        1371,
        578 + Y_OFFSET,
        latency_text,
        latency_color,
        font
    )

    # =========================
    # 在线人数
    # =========================
    draw_text(
        draw,
        1433,
        659 + Y_OFFSET,
        player_count_text,
        (0, 255, 0, 255) if online else (255, 0, 0, 255),
        font
    )

    # =========================
    # 玩家列表
    # =========================
    RIGHT_X = 1042
    START_Y = 785
    LINE_HEIGHT = 50

    for i, name in enumerate(player_list[:12]):
        draw_text(
            draw,
            RIGHT_X,
            START_Y + i * LINE_HEIGHT,
            str(name),
            (255, 255, 255, 255),
            list_font
        )

    # =========================
    # 保存
    # =========================
    output_path = os.path.join(OUT_DIR, "mc_status.png")
    img.save(output_path)

    return output_path