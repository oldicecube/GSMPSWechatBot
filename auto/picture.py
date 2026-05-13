import os
import time

from PIL import Image

from core.followup import register as register_followup
from core.followup import consume as consume_followup
from core.followup import peek as peek_followup
from core.sender import send
from core.weflow_media import extract_timestamp
from core.weflow_media import find_image_url_by_timestamp
from core.weflow_media import download_image

SAVE_DIR = os.path.join("data", "pics")
INTERCEPT_LLM = True


# =========================================================
# 初始化
# =========================================================
def init(config):
    os.makedirs(SAVE_DIR, exist_ok=True)
    print("[AUTO PLUGIN] symmetry 初始化完成")


def start(sender):
    return None


# =========================================================
# 统一取文本
# =========================================================
def _get_text(context):
    return (
        context.get("text")
        or context.get("content")
        or ""
    ).strip()


# =========================================================
# 原始对象
# =========================================================
def _get_raw(context):
    return context.get("_raw") or context.get("raw") or {}


# =========================================================
# 判断图片消息
# =========================================================
def _is_image(context):
    content = context.get("content", "").strip()

    if content == "[图片]":
        return True

    raw = _get_raw(context)

    if raw.get("content") == "[图片]":
        return True

    return False


# =========================================================
# 判断命令
# /对称 左右
# /对称 右左
# /对称 上下
# /对称 下上
# =========================================================
def _parse_symmetry_command(text):
    text = text.strip()

    if not text.startswith("/对称"):
        return None

    cmd = text[len("/对称"):].strip()

    if cmd == "左右":
        return "left_right"

    if cmd == "右左":
        return "right_left"

    if cmd == "上下":
        return "up_down"

    if cmd == "下上":
        return "down_up"

    return None


# =========================================================
# 统一创建透明画布
# =========================================================
def _new_canvas(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


# =========================================================
# 左右对称（左边镜像到右边）
# =========================================================
def _make_left_right(img):
    w, h = img.size
    half = w // 2

    left = img.crop((0, 0, half, h))
    right = left.transpose(Image.FLIP_LEFT_RIGHT)

    canvas = _new_canvas((w, h))

    canvas.paste(left, (0, 0), left)
    canvas.paste(right, (half, 0), right)

    if w % 2 == 1:
        mid = img.crop((half, 0, half + 1, h))
        canvas.paste(mid, (half, 0), mid)

    return canvas


# =========================================================
# 右左对称（右边镜像到左边）
# =========================================================
def _make_right_left(img):
    w, h = img.size
    half = w // 2

    right_start = half if w % 2 == 0 else half + 1

    right = img.crop((right_start, 0, w, h))
    left = right.transpose(Image.FLIP_LEFT_RIGHT)

    canvas = _new_canvas((w, h))

    canvas.paste(left, (0, 0), left)

    if w % 2 == 1:
        mid = img.crop((half, 0, half + 1, h))
        canvas.paste(mid, (half, 0), mid)

    canvas.paste(right, (right_start, 0), right)

    return canvas


# =========================================================
# 上下对称（上边镜像到下边）
# =========================================================
def _make_up_down(img):
    w, h = img.size
    half = h // 2

    top = img.crop((0, 0, w, half))
    bottom = top.transpose(Image.FLIP_TOP_BOTTOM)

    canvas = _new_canvas((w, h))

    canvas.paste(top, (0, 0), top)
    canvas.paste(bottom, (0, half), bottom)

    if h % 2 == 1:
        mid = img.crop((0, half, w, half + 1))
        canvas.paste(mid, (0, half), mid)

    return canvas


# =========================================================
# 下上对称（下边镜像到上边）
# =========================================================
def _make_down_up(img):
    w, h = img.size
    half = h // 2

    bottom_start = half if h % 2 == 0 else half + 1

    bottom = img.crop((0, bottom_start, w, h))
    top = bottom.transpose(Image.FLIP_TOP_BOTTOM)

    canvas = _new_canvas((w, h))

    canvas.paste(top, (0, 0), top)

    if h % 2 == 1:
        mid = img.crop((0, half, w, half + 1))
        canvas.paste(mid, (0, half), mid)

    canvas.paste(bottom, (0, bottom_start), bottom)

    return canvas


# =========================================================
# 保存图片（保留透明）
# =========================================================
def _save_image(img):
    filename = f"symmetry_{int(time.time()*1000)}.png"
    path = os.path.join(SAVE_DIR, filename)

    img.save(path, "PNG")

    return path


# =========================================================
# 清理文件
# =========================================================
def _cleanup_file(path):
    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"[symmetry] 已清理临时文件 path={path}")
    except Exception as e:
        print(f"[symmetry] 清理临时文件失败 path={path} err={e}")


# =========================================================
# 主逻辑
# =========================================================
def handle_auto(context):
    session_id = context.get("sessionId")

    if not session_id:
        return None

    text = _get_text(context)

    # -----------------------------------------------------
    # 第一阶段：命令
    # -----------------------------------------------------
    mode = _parse_symmetry_command(text)

    if mode:
        wxid = context.get("wxid") or _get_raw(context).get("wxid")
        
        register_followup(
            session_id=session_id,
            target="picture",
            ttl=60,
            payload={"mode": mode},
            wxid=wxid
        )

        print(
            f"[symmetry] 已进入等待图片状态 "
            f"session={session_id} mode={mode} wxid={wxid}"
        )

        return None

    # -----------------------------------------------------
    # 第二阶段：等待图片
    # -----------------------------------------------------
    state = consume_followup(
        session_id,
        wxid=context.get("wxid") or _get_raw(context).get("wxid")
    )

    if not state:
        return None

    if state.get("target") != "picture":
        return None

    payload = state.get("payload") or {}
    mode = payload.get("mode")

    if mode not in (
        "left_right",
        "right_left",
        "up_down",
        "down_up"
    ):
        return None

    # 不是图片，直接结束
    if not _is_image(context):
        print(f"[symmetry] 下一条消息不是图片，已释放等待状态 session={session_id}")
        return None

    raw = _get_raw(context)
    target_ts = extract_timestamp(raw) or extract_timestamp(context)

    print(
        f"[symmetry] 收到图片，开始处理 "
        f"session={session_id} mode={mode} ts={target_ts}"
    )

    save_path = None

    try:
        image_url, best_diff = find_image_url_by_timestamp(
            session_id,
            target_ts,
            limit=20
        )

        if not image_url:
            print("[symmetry] 获取图片失败：未找到匹配图片")
            return None

        if best_diff is None:
            print("[symmetry] 未找到可比对时间戳，回退最近图片")
        else:
            print(f"[symmetry] 已按时间戳匹配图片 diff={best_diff}")

        # =================================================
        # 核心修复：
        # 强制转 RGBA，解决透明 PNG 黑底白图问题
        # =================================================
        img = download_image(image_url).convert("RGBA")

        if mode == "left_right":
            out = _make_left_right(img)

        elif mode == "right_left":
            out = _make_right_left(img)

        elif mode == "up_down":
            out = _make_up_down(img)

        else:
            out = _make_down_up(img)

        save_path = _save_image(out)

        target = context.get("group") or context.get("user")

        ok, err = send(
            target=target,
            file_path=save_path,
            mode="wechat_file"
        )

        if ok:
            print(f"[symmetry] 图片已发送 target={target}")
        else:
            print(f"[symmetry] 图片发送失败 target={target} err={err}")

        _cleanup_file(save_path)

        return None

    except Exception as e:
        print("[symmetry ERROR]", e)
        _cleanup_file(save_path)
        return None


def allow_llm(context):
    if _is_image(context):
        return False

    session_id = context.get("sessionId")
    if not session_id:
        return True

    text = _get_text(context)
    if _parse_symmetry_command(text):
        return False

    wxid = context.get("wxid") or _get_raw(context).get("wxid")
    state = peek_followup(session_id, wxid=wxid)
    if not state:
        return True

    return state.get("target") != "picture"
