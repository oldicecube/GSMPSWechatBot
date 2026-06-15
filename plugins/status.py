import os
from PIL import Image, ImageDraw, ImageFont
from services.mc_api import configure as configure_mc_api
from services.mc_api import status
from core.sender import send   # ⭐ 新增：发送接口

COMMAND = "/status"

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

IMG_PATH = os.path.join(BASE_DIR, "assets/images/serverStatus.png")
FONT_PATH = os.path.join(BASE_DIR, "assets/fonts/Minecraft AE.ttf")
OUT_DIR = os.path.join(BASE_DIR, "output")

os.makedirs(OUT_DIR, exist_ok=True)

font = ImageFont.truetype(FONT_PATH, 65)
list_font = ImageFont.truetype(FONT_PATH, 40)


def init(config):
    configure_mc_api(config)


def safe_int(v):
    try:
        return int(v) if v is not None else None
    except:
        return None


def draw_text(draw, x, y, text, fill, font, anchor="ld"):
    shadow = (3, 3)

    draw.text(
        (x + shadow[0], y + shadow[1]),
        text,
        font=font,
        fill=(0, 0, 0, 180),
        anchor=anchor
    )

    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        anchor=anchor
    )


def get_target(context):
    if not context:
        return "文件传输助手"

    return context.get("group") or context.get("user") or "文件传输助手"


def handle(content, context):

    # =========================
    # ① 触发条件
    # =========================
    if content:
        c = str(content).strip()
        if c not in ["", COMMAND, "/mc"]:
            return None

    # =========================
    # ② 获取服务器状态
    # =========================
    try:
        data = status() or {}
    except Exception as e:
        return f"获取服务器状态失败: {e}"

    online = bool(data.get("online"))
    latency = safe_int(data.get("latency_ms"))

    online_players = data.get("online_players", 0)
    max_players = data.get("max_players", 0)

    player_list = data.get("players", []) or []

    # =========================
    # ③ 文本处理
    # =========================
    online_text = "在线" if online else "离线"
    latency_text = f"{latency} ms" if latency is not None else "N/A"
    player_count_text = f"{online_players}/{max_players}"

    # =========================
    # ④ 绘制图片
    # =========================
    img = Image.open(IMG_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    Y_OFFSET = 5

    draw_text(
        draw, 1434, 495 + Y_OFFSET,
        online_text,
        (0, 255, 0, 255) if online else (255, 0, 0, 255),
        font
    )

    latency_color = (
        (150, 150, 150, 255) if latency is None else
        (0, 255, 0, 255) if latency <= 30 else
        (255, 255, 0, 255) if latency <= 100 else
        (255, 0, 0, 255)
    )

    draw_text(
        draw, 1371, 578 + Y_OFFSET,
        latency_text,
        latency_color,
        font
    )

    draw_text(
        draw, 1433, 659 + Y_OFFSET,
        player_count_text,
        (0, 255, 0, 255) if online else (255, 0, 0, 255),
        font
    )

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
    # ⑤ 保存图片
    # =========================
    output_path = os.path.join(OUT_DIR, "mc_status.png")
    img.save(output_path)

    # =========================
    # ⑥ 发送图片（核心）
    # =========================
    target = get_target(context)

    ok, err = send(
        target=target,
        file_path=output_path,
        mode="wechat_file"
    )

    if not ok:
        return f"图片发送失败: {err}"
