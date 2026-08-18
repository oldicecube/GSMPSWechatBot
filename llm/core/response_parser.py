import json
import re


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


def _strip_fences(text):
    """Strip one optional ```json ... ``` wrapper from model output."""
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        str(text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
    return str(text or "").strip()


def _json_substring(text):
    """Return the substring from the first '{' to the last '}' if present."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start:end + 1]


def _repair_truncated_json(text):
    """Best-effort repair for JSON truncated mid-structure (e.g. cut off by
    the model at max output length). Appends the missing closers for any
    unclosed arrays/objects that started before the truncation point."""
    stack = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    stack.pop()
        index += 1
    if in_string:
        # Unterminated string; do not try to guess the value.
        return None
    if not stack:
        return None
    closers = "".join("]" if opener == "[" else "}" for opener in reversed(stack))
    candidate = text.rstrip()
    while candidate.endswith(","):
        candidate = candidate[:-1].rstrip()
    try:
        return json.loads(candidate + closers)
    except Exception:
        return None


def _strip_trailing_commas(text):
    """Remove commas that directly precede a closing '}' or ']' (a common
    model mistake that strict json.loads rejects)."""
    result = []
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == '"':
            result.append(ch)
            index += 1
            while index < length:
                ch2 = text[index]
                result.append(ch2)
                if ch2 == "\\" and index + 1 < length:
                    result.append(text[index + 1])
                    index += 2
                    continue
                if ch2 == '"':
                    index += 1
                    break
                index += 1
            continue
        if ch == ",":
            probe = index + 1
            while probe < length and text[probe] in " \t\r\n":
                probe += 1
            if probe < length and text[probe] in "}]":
                index += 1
                continue
        result.append(ch)
        index += 1
    return "".join(result)


def load_json_lenient(text):
    """Parse model JSON tolerating fences, surrounding prose, trailing
    commas, and truncation.

    Order of attempts:
      1. exact json.loads
      2. after stripping a ```json``` fence
      3. after removing trailing commas before closers
      4. the substring between the first '{' and the last '}'
      5. truncated-JSON repair on that substring
    Raises ValueError when no attempt succeeds.
    """
    raw = _strip_fences(text)
    if not raw:
        raise ValueError("empty JSON response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    raw = _strip_trailing_commas(raw)
    try:
        return json.loads(raw)
    except Exception:
        pass
    candidate = _json_substring(raw)
    if candidate is None:
        candidate = raw
    try:
        return json.loads(candidate)
    except Exception:
        pass
    repaired = _repair_truncated_json(candidate)
    if repaired is not None:
        return repaired
    raise ValueError(f"invalid JSON response: {_shorten_raw_text(text)}")


def _load_model_json(text):
    raw = _strip_fences(text)
    return json.loads(raw)


_CHAT_SPLIT_RE = re.compile(r"[,，;；。！？!?]+")


def _split_chat_message(text):
    """Turn a short multi-clause chat reply into independently sent turns.

    This is deliberately conservative. Long factual or command-like replies,
    URLs, and code remain one message; ordinary short acknowledgements joined
    by commas become separate WeChat messages.
    """
    value = str(text or "").strip()
    if len(value) <= 8 or "http://" in value or "https://" in value:
        return [value] if value else []
    if value.startswith("/") or "```" in value or re.search(r"(?<!\w)/[A-Za-z][\w-]*|\s--?[A-Za-z]", value):
        return [value]
    parts = [item.strip() for item in _CHAT_SPLIT_RE.split(value) if item.strip()]
    if len(parts) < 2 or len(parts) > 4:
        return [value]
    if len(value) > 120 or max(len(item) for item in parts) > 36:
        return [value]
    return parts


def _normalize_chat_messages(items):
    result = []
    for item in items:
        result.extend(_split_chat_message(item))
    return result


_STYLE_SWITCH_FIELD_LIMITS = (
    ("scene", 40),
    ("situation", 160),
    ("slang_type", 40),
    ("emotion", 40),
    ("pattern", 240),
    ("reason", 200),
)


def _normalize_style_switch(value):
    """Normalize the optional style_switch reply field.

    Returns None when absent/invalid/keep. "clear" requests an explicit reset,
    otherwise a "set" carries the scene/situation/type/emotion the model wants
    to hold for the rest of the cycle. Unknown fields are ignored so a bad
    switch never breaks the reply contract.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    if bool(value.get("clear")):
        return {"action": "clear"}
    if bool(value.get("keep")):
        return None
    fields = {}
    for key, limit in _STYLE_SWITCH_FIELD_LIMITS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            fields[key] = raw.strip()[:limit]
    if not fields:
        return None
    fields["action"] = "set"
    return fields

def _normalize_idle_content_plan(value):
    """Validate the optional planner action without trusting free-form data."""
    if not isinstance(value, dict):
        return None
    action = str(value.get("action") or "skip").strip().lower()
    if action not in {"share", "skip"}:
        return None
    result = {"action": action}
    content_hash = str(value.get("content_hash") or "").strip()
    if content_hash:
        result["content_hash"] = content_hash[:80]
    return result


def parse_llm_response(text: str, emoji_list: list) -> dict:
    """Parse the public response contract.

    Known keys are optional. Unknown keys are intentionally ignored. A known
    key with the wrong JSON type is rejected instead of being silently coerced.
    """
    if not str(text or "").strip():
        return {"messages": [], "animation": None}

    try:
        data = _load_model_json(text)
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
            normalized_messages.extend(_split_chat_message(content))

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

    # Both known fields may be absent. This is a valid, empty response; the
    # worker will simply have nothing to send. Unknown keys were discarded by
    # constructing the return object below.
    result = {"messages": normalized_messages, "animation": animation}
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result


def parse_proactive_response(text: str, emoji_list: list, force_reply=False) -> dict:
    """Parse the batch decision returned by the proactive-reply prompt."""
    def _proactive_error(reason):
        # An empty final tool/assistant content is not useful group text.
        # Let forced requests use their normal fallback instead of sending
        # this parser diagnostic to the group.
        if not str(text or "").strip():
            return {
                "messages": [],
                "animation": None,
                "should_reply": bool(force_reply),
                "reply_to": [],
                "_valid": False,
            }
        result = build_raw_response(text, reason)
        result["_valid"] = False
        return result

    try:
        data = _load_model_json(text)
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
    should_reply = data.get(
        "should_reply",
        bool(normalized.get("messages") or normalized.get("animation")),
    )
    if force_reply:
        should_reply = True
    if not should_reply:
        normalized["messages"] = []
        normalized["animation"] = None

    result = {
        "messages": normalized.get("messages") or [],
        "animation": normalized.get("animation"),
        "should_reply": bool(should_reply),
        "reply_to": reply_to,
    }
    idle_content_plan = _normalize_idle_content_plan(data.get("share_idle_content"))
    if idle_content_plan:
        result["share_idle_content"] = idle_content_plan
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result
