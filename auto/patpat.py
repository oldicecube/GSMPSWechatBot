import re

CONFIG = None
MATCH_RAW_MESSAGE = True
INTERCEPT_LLM = True


def init(config):
    global CONFIG
    CONFIG = config
    print("[AUTO PLUGIN] patpat 初始化完成")


# =========================================================
# 🧠 统一 content 提取（适配 dispatcher context）
# =========================================================
def _get_content(context):

    if not isinstance(context, dict):
        return ""

    # 直接标准字段（新 dispatcher 保证有）
    content = context.get("content")

    if content:
        return str(content).strip()

    # fallback：兼容旧 raw
    raw = context.get("raw")
    if isinstance(raw, dict):
        return str(
            raw.get("content")
            or raw.get("text")
            or ""
        ).strip()

    return ""


# =========================================================
# 🔥 dispatcher 统一入口（必须叫 handle_auto）
# =========================================================
def handle_auto(context):

    content = _get_content(context)

    user = context.get("user", "未知用户")
    group = context.get("group")

    # =========================
    # 空内容直接跳过
    # =========================
    if not content:
        return None

    # =========================
    # 匹配规则
    # =========================
    pattern = r"拍了拍.*服务器状态@?我"

    if not re.search(pattern, content):
        return None

    target = group or user
    if not target:
        return None

    # =========================
    # 命中输出
    # =========================
    return f"拍你吗"


def allow_llm(context):
    content = _get_content(context)
    if not content:
        return True

    pattern = r"拍了拍.*服务器状态@?我"
    return re.search(pattern, content) is None
