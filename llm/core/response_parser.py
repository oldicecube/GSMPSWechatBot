import json


DEFAULT_ERROR_MESSAGE = "LLM转发失败：返回内容格式不符合预期"


def _shorten_raw_text(text, limit=120):
    raw = str(text or "").strip()
    if not raw:
        return ""

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return raw if len(raw) <= limit else f"{raw[:limit]}..."


def build_error_response(reason=None, raw_text=None):
    message = str(reason or DEFAULT_ERROR_MESSAGE).strip() or DEFAULT_ERROR_MESSAGE
    raw_preview = _shorten_raw_text(raw_text)

    if raw_preview:
        message = f"{message}\n原始返回：{raw_preview}"

    return {
        "messages": [message],
        "animation": None
    }


FALLBACK_RESPONSE = build_error_response()


def parse_llm_response(text: str, emoji_list: list) -> dict:
    try:
        data = json.loads(text)
    except Exception:
        return build_error_response("LLM转发失败：返回内容不是合法JSON", raw_text=text)

    if not isinstance(data, dict):
        return build_error_response("LLM转发失败：返回JSON不是对象", raw_text=text)

    messages = data.get("messages")
    animation = data.get("animation")

    if not isinstance(messages, list):
        return build_error_response("LLM转发失败：messages 字段不是数组", raw_text=text)

    normalized_messages = []
    for item in messages:
        if isinstance(item, str):
            content = item.strip()
            if content:
                normalized_messages.append(content)

    valid_emoji_set = {
        str(item).strip()
        for item in (emoji_list or [])
        if str(item).strip()
    }

    if animation is None:
        normalized_animation = None
    elif isinstance(animation, str) and animation.strip() in valid_emoji_set:
        normalized_animation = animation.strip()
    else:
        normalized_animation = None

    if normalized_animation is None and normalized_messages:
        transferred_animation = None
        cleaned_messages = []

        for item in normalized_messages:
            if transferred_animation is None and item in valid_emoji_set:
                transferred_animation = item
                continue
            cleaned_messages.append(item)

        if transferred_animation is not None:
            normalized_animation = transferred_animation
            normalized_messages = cleaned_messages

    if normalized_animation is not None and not normalized_messages:
        normalized_messages = ["[仅发送表情]"]

    if not normalized_messages:
        return build_error_response("LLM转发失败：messages 中没有可发送的文本", raw_text=text)

    return {
        "messages": normalized_messages,
        "animation": normalized_animation
    }
