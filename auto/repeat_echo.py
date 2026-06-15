import threading

INTERCEPT_LLM = False

STATE_LOCK = threading.Lock()
SESSION_STATES = {}
SPECIAL_MESSAGE = "拍你吗"


def init(config):
    print("[AUTO PLUGIN] repeat_echo initialized")


def handle_auto(context):
    content = _get_content(context)
    if not content:
        return None

    if _is_ineligible(context, content):
        return None

    if content == SPECIAL_MESSAGE:
        return SPECIAL_MESSAGE

    session_key = _get_session_key(context)
    if not session_key:
        return None

    with STATE_LOCK:
        state = SESSION_STATES.setdefault(
            session_key,
            {
                "last_message": None,
                "cooldown": False,
            }
        )

        last_message = state.get("last_message")
        cooldown = bool(state.get("cooldown"))

        if cooldown:
            if content != last_message:
                state["last_message"] = content
                state["cooldown"] = False
            return None

        if content == last_message:
            state["cooldown"] = True
            return content

        state["last_message"] = content
        return None


def allow_llm(context):
    return True


def _get_content(context):
    if not isinstance(context, dict):
        return ""

    return str(context.get("content") or "").strip()


def _is_ineligible(context, content):
    if context.get("prefix_used"):
        return True

    if content.startswith("/"):
        return True

    # 非纯文本消息（如 [表情]、[图片] 等），不作处理
    if content.startswith("[") and content.endswith("]"):
        return True

    return False


def _get_session_key(context):
    return (
        context.get("group")
        or context.get("sessionId")
        or context.get("user")
        or context.get("wxid")
    )
