
# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1,
 'fields': {'followup_ttl_seconds': {'type': 'int',
                                     'default': 60,
                                     'min': 15,
                                     'max': 300,
                                     'label': '等待下一张图片或表情包的时间（秒）'}}}

import os
import re

from core.followup import register as register_followup
from core.shell_plugin_config import settings
from core.followup import consume as consume_followup
from core.followup import peek as peek_followup
from core.sender import send
from core.weflow_media import load_visual_bytes
from utils.symmetry_image import save_symmetry, symmetry_frame, MAX_RANDOM_PASSES

SAVE_DIR = os.path.join("data", "pics")
INTERCEPT_LLM = True
# Command-like entry handled by this auto plugin before normal command lookup.
AUTO_COMMANDS = {"/对称"}


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
    # Command messages are split by Router into command + args; reconstruct the
    # original text so this auto plugin can recognize /?? even though
    # context["content"] only contains the arguments.
    command = str(context.get("command") or "").strip()
    if command.startswith("/"):
        args = str(context.get("args") or "").strip()
        return f"{command} {args}".strip()
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
    if context.get("is_picture") or context.get('is_emoji'):
        return True
    content = context.get("content", "").strip()

    if content in {"[图片]", "[表情包]", "[动画表情]", "[表情]"}:
        return True

    raw = _get_raw(context)

    if raw.get('is_picture') or raw.get('is_emoji') or raw.get("content") in {"[图片]", "[表情包]", "[动画表情]", "[表情]"}:
        return True

    from llm.core.wechat_translation import wire_fields
    fields = wire_fields(context)
    try:
        return (int(fields.get('localType') or fields.get('local_type') or 0) & 0xffffffff) in {3, 47}
    except (TypeError, ValueError):
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

    if not re.match(r'^/对称(?:\s|$)', text):
        return None

    cmd = text[len("/对称"):].strip()

    parts = cmd.split()
    if parts and parts[0] == '随机':
        if len(parts) == 1:
            return {'mode': 'random', 'iterations': 1}
        if len(parts) == 2 and re.fullmatch(r'[0-9]{1,3}', parts[1]):
            count = int(parts[1])
            if 1 <= count <= MAX_RANDOM_PASSES:
                return {'mode': 'random', 'iterations': count}
        return {'error': f'用法：/对称 随机 [次数]，次数为 1–{MAX_RANDOM_PASSES} 的整数，省略时为 1。随后发送图片或表情包。'}

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
def _make_left_right(img):
    return symmetry_frame(img, 'left_right')


def _make_right_left(img):
    return symmetry_frame(img, 'right_left')


def _make_up_down(img):
    return symmetry_frame(img, 'up_down')


def _make_down_up(img):
    return symmetry_frame(img, 'down_up')


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
        if isinstance(mode, dict) and mode.get('error'):
            return mode['error']
        wxid = context.get("wxid") or _get_raw(context).get("wxid")
        
        register_followup(
            session_id=session_id,
            target="picture",
            ttl=settings('gsmps.automation.picture')['followup_ttl_seconds'],
            payload=mode if isinstance(mode, dict) else {"mode": mode},
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
    owner = context.get("wxid") or _get_raw(context).get("wxid")
    state = peek_followup(session_id, wxid=owner)
    if not state or state.get('target') != 'picture':
        return None
    state = consume_followup(
        session_id,
        wxid=owner
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
        "down_up",
        "random"
    ):
        return None

    # 不是图片，直接结束
    if not _is_image(context):
        print(f"[symmetry] 下一条消息不是图片，已释放等待状态 session={session_id}")
        return None

    save_path = None
    try:
        binary = load_visual_bytes(context)
        save_path = save_symmetry(binary, mode, SAVE_DIR, iterations=payload.get('iterations', 1))

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
        return '这张图片或表情包未能完成对称，请换一张较小的原图重试。'


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
