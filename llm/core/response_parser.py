import json


DEFAULT_ERROR_MESSAGE = "LLM转发失败：返回内容格式不符合预期"
BALANCE_ERROR_MESSAGE = "余额不足，请检查llm余额是否充足"


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

    return {"messages": [message], "animation": None}


def build_raw_response(text, reason=DEFAULT_ERROR_MESSAGE):
    """Send the complete model text when its JSON contract is invalid."""
    raw = str(text or "")
    if raw.strip():
        return {"messages": [raw], "animation": None}
    return build_error_response(reason)


def is_insufficient_balance_error(error) -> bool:
    text = str(error or "").lower()
    return "402" in text


def build_balance_error_response():
    return {
        "messages": [BALANCE_ERROR_MESSAGE],
        "animation": None,
        "_balance_error": True,
        "_llm_ok": False,
    }


FALLBACK_RESPONSE = build_error_response()


def parse_llm_response(text: str, emoji_list: list) -> dict:
    """Parse the public response contract.

    Known keys are optional. Unknown keys are intentionally ignored. A known
    key with the wrong JSON type is rejected instead of being silently coerced.
    """
    try:
        data = json.loads(text)
    except Exception:
        return build_raw_response(text, "LLM转发失败：返回内容不是合法JSON")

    if not isinstance(data, dict):
        return build_raw_response(text, "LLM转发失败：返回JSON不是对象")

    if "messages" in data and not isinstance(data["messages"], list):
        return build_raw_response(text, "LLM转发失败：messages字段格式错误")

    if "animation" in data and data["animation"] is not None and not isinstance(data["animation"], str):
        return build_raw_response(text, "LLM转发失败：animation字段格式错误")

    normalized_messages = []
    for item in data.get("messages", []):
        if not isinstance(item, str):
            return build_raw_response(text, "LLM转发失败：messages必须是字符串数组")
        content = item.strip()
        if content:
            normalized_messages.append(content)

    valid_emoji_set = {
        str(item).strip()
        for item in (emoji_list or [])
        if str(item).strip()
    }

    animation = data.get("animation")
    if isinstance(animation, str):
        animation = animation.strip() or None
    if animation is not None and animation not in valid_emoji_set:
        animation = None

    if animation is None and normalized_messages:
        transferred_animation = None
        cleaned_messages = []

        for item in normalized_messages:
            if transferred_animation is None and item in valid_emoji_set:
                transferred_animation = item
                continue
            cleaned_messages.append(item)

        if transferred_animation is not None:
            animation = transferred_animation
            normalized_messages = cleaned_messages

    if animation is not None and not normalized_messages:
        normalized_messages = ["[仅发送表情]"]

    # Both known fields may be absent. This is a valid, empty response; the
    # worker will simply have nothing to send. Unknown keys were discarded by
    # constructing the return object below.
    return {"messages": normalized_messages, "animation": animation}


def parse_proactive_response(text: str, emoji_list: list, force_reply=False) -> dict:
    """Parse the batch decision returned by the proactive-reply prompt."""
    def _proactive_error(reason):
        result = build_raw_response(text, reason)
        result["_valid"] = False
        return result

    try:
        data = json.loads(text)
    except Exception:
        return _proactive_error("LLM主动回复失败：返回内容不是合法JSON")

    if not isinstance(data, dict):
        return _proactive_error("LLM主动回复失败：返回JSON不是对象")

    if "should_reply" in data and not isinstance(data["should_reply"], bool):
        return _proactive_error("LLM主动回复失败：should_reply字段格式错误")

    if "reply_to" in data:
        reply_to = data["reply_to"]
        if (
            not isinstance(reply_to, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in reply_to)
        ):
            return _proactive_error("LLM主动回复失败：reply_to字段格式错误")
    else:
        reply_to = []

    if "messages" in data and not isinstance(data["messages"], list):
        return _proactive_error("LLM主动回复失败：messages字段格式错误")
    if "messages" in data and any(not isinstance(item, str) for item in data["messages"]):
        return _proactive_error("LLM主动回复失败：messages必须是字符串数组")
    if "animation" in data and data["animation"] is not None and not isinstance(data["animation"], str):
        return _proactive_error("LLM主动回复失败：animation字段格式错误")

    core_data = {
        key: data[key]
        for key in ("messages", "animation")
        if key in data
    }
    normalized = parse_llm_response(json.dumps(core_data, ensure_ascii=False), emoji_list)
    should_reply = data.get("should_reply", bool(normalized.get("messages")))
    if force_reply:
        should_reply = True
    if not should_reply:
        normalized["messages"] = []
        normalized["animation"] = None

    return {
        "messages": normalized.get("messages") or [],
        "animation": normalized.get("animation"),
        "should_reply": bool(should_reply),
        "reply_to": reply_to,
    }
