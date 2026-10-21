import json
import re


DEFAULT_ERROR_MESSAGE = "LLM转发失败：返回内容格式不符合预期"
BALANCE_ERROR_MESSAGE = "余额不足，请检查llm余额是否充足"


# Model output sometimes echoes the batch context marker in front of chat
# content (for example "batch_index=6: [抽象]").  It is a transcript artifact,
# not user-facing text.
_BATCH_INDEX_PREFIX_RE = re.compile(
    r"^\s*batch_index\s*=\s*\d+\s*[:：\-]\s*"
)
_EMOJI_SUFFIX_RE = re.compile(r"\[\s*([^\[\]]+)\s*\]\s*$")


def _strip_batch_index_prefix(text):
    """Return the text without a leading batch_index= marker, if present."""
    return _BATCH_INDEX_PREFIX_RE.sub("", str(text or "")).strip()


def _strip_bracket_wrap(text):
    """Strip one surrounding bracket pair if the whole item is [emoji]."""
    value = str(text or "").strip()
    match = re.fullmatch(r"\[\s*([^\[\]]+)\s*\]", value)
    if match:
        return match.group(1).strip()
    return value


def _extract_emoji_suffix(text, valid_set):
    """Split a trailing emoji identifier off chat text when present.

    The model is told to put the identifier in animation, but it frequently
    echoes "[抽象]" or "别急 [我在]" inside messages.  Recognise both forms
    without mangling ordinary text.  Returns (head_text, emoji_or_None).
    """
    value = str(text or "").strip()
    if not value:
        return value, None
    bare = _strip_bracket_wrap(value)
    if bare in valid_set:
        return "", bare
    suffix = _EMOJI_SUFFIX_RE.search(value)
    if suffix and suffix.group(1).strip() in valid_set:
        head = value[: suffix.start()].strip()
        return head, suffix.group(1).strip()
    return value, None


def _dedupe_messages(items, limit=8):
    """Collapse exact duplicates within one model response (sticker-spam guard).

    Returns (unique_messages, dropped_count).  First occurrence wins.
    """
    unique = []
    seen = set()
    for item in items or []:
        text = str(item or "").strip()
        if not text:
            continue
        signature = text.casefold()
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(text)
    return unique[:limit], max(0, len([i for i in (items or []) if str(i or "").strip()]) - len(unique[:limit]))
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

    return {"messages": [], "animation": None, "_llm_ok": False, "_llm_error": True, "_internal_error": message}


def build_raw_response(text, reason=DEFAULT_ERROR_MESSAGE):
    """Return a silent response when the model JSON contract is invalid.

    The model response is untrusted protocol data. It may contain a reasoning
    trace, tool scratch work, or prompt text before/after the JSON object. It
    must never be copied into a user-facing WeChat message. Keep the helper
    name for compatibility with older callers, but deliberately discard the
    raw text and only expose a private validity marker.
    """
    return {"messages": [], "animation": None, "_valid": False}


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


def _strip_reasoning_blocks(text):
    """Remove explicitly tagged private reasoning before protocol parsing."""
    raw = str(text or "")
    return re.sub(
        r"<(?:think|analysis|reasoning)\b[^>]*>.*?</(?:think|analysis|reasoning)>",
        "",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _strip_fences(text):
    """Strip one optional ```json ... ``` wrapper from model output."""
    text = _strip_reasoning_blocks(text)
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        str(text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
    return str(text or "").strip()


def _json_substrings(text):
    """Yield balanced JSON object/array candidates from arbitrary text.

    A simple first-``{``/last-``}`` slice is unsafe when a reasoning trace has
    braces or when a JSON string itself contains braces. This scanner tracks
    strings, escapes, nesting and mismatched closers, allowing us to select a
    real JSON object without ever sending surrounding prose.
    """
    raw = str(text or "")
    length = len(raw)
    for start, opener in enumerate(raw):
        if opener not in "[{":
            continue
        stack = [opener]
        in_string = False
        escaped = False
        for index in range(start + 1, length):
            ch = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch in "[{":
                stack.append(ch)
                continue
            if ch in "]}":
                if not stack or (ch == "]" and stack[-1] != "[") or (ch == "}" and stack[-1] != "{"):
                    break
                stack.pop()
                if not stack:
                    yield raw[start:index + 1]
                    break


def _json_substring(text):
    """Return the first balanced JSON object/array candidate, if present."""
    return next(iter(_json_substrings(text)), None)


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


def _iter_json_values(text):
    """Yield JSON values found in model output without exposing raw text.

    A response may contain private reasoning, a markdown fence, or harmless
    prose around the final protocol object.  Every candidate is parsed before
    it is yielded; callers must still validate the expected schema.
    """
    raw = _strip_fences(text)
    if not raw:
        return

    seen = set()

    def emit(candidate):
        try:
            value = json.loads(candidate)
        except Exception:
            return None
        try:
            fingerprint = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except Exception:
            fingerprint = repr(value)
        if fingerprint in seen:
            return None
        seen.add(fingerprint)
        return value

    parsed = emit(raw)
    if parsed is not None:
        yield parsed

    normalized = _strip_trailing_commas(raw)
    if normalized != raw:
        parsed = emit(normalized)
        if parsed is not None:
            yield parsed

    candidates = list(_json_substrings(raw))
    # Keep the old outer-object preference for generic callers.  Schema-aware
    # callers below can choose a better candidate by its known keys.
    candidates.sort(key=len, reverse=True)
    for candidate in candidates:
        candidate = _strip_trailing_commas(candidate)
        parsed = emit(candidate)
        if parsed is not None:
            yield parsed
            continue
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            try:
                fingerprint = json.dumps(repaired, sort_keys=True, ensure_ascii=False)
            except Exception:
                fingerprint = repr(repaired)
            if fingerprint not in seen:
                seen.add(fingerprint)
                yield repaired

    # If the model was cut off before closing the object, try each plausible
    # opening point.  This remains parse-only and never leaks incomplete text.
    for start, opener in enumerate(raw):
        if opener not in "[{":
            continue
        repaired = _repair_truncated_json(_strip_trailing_commas(raw[start:]))
        if repaired is None:
            continue
        try:
            fingerprint = json.dumps(repaired, sort_keys=True, ensure_ascii=False)
        except Exception:
            fingerprint = repr(repaired)
        if fingerprint not in seen:
            seen.add(fingerprint)
            yield repaired


def load_json_lenient(text, preferred_keys=None):
    """Parse model JSON while tolerating wrappers and common model mistakes.

    ``preferred_keys`` is useful when a reasoning trace contains another valid
    JSON object.  The candidate containing the most protocol keys wins, rather
    than blindly selecting the longest object from the private scratch area.
    """
    values = list(_iter_json_values(text))
    if not values:
        raise ValueError("invalid JSON response")

    keys = tuple(str(key) for key in (preferred_keys or ()) if str(key))
    if not keys:
        return values[0]

    matching = [
        value for value in values
        if isinstance(value, dict) and any(key in value for key in keys)
    ]
    if matching:
        return max(
            matching,
            key=lambda value: sum(1 for key in keys if key in value),
        )
    return values[0]


def _load_model_json(text, preferred_keys=None):
    return load_json_lenient(text, preferred_keys=preferred_keys)


_CHAT_SPLIT_RE = re.compile(r"[,，;；。！？!?]+")


def _split_chat_message(text):
    """Turn a short multi-clause chat reply into independently sent turns.

    This is deliberately conservative. Long factual or command-like replies,
    URLs, and code remain one message; ordinary short acknowledgements joined
    by commas become separate WeChat messages.
    """
    value = str(text or "").strip()
    # Preserve one mention-bearing message as a single WeChat composition; the
    # sendhook splits it into text/@-selection/text segments without splitting
    # the final sent message into separate chat bubbles.
    if "[[WX_AT:" in value:
        return [value] if value else []
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

def _normalize_reply_confidence(value):
    """Normalize optional model confidence without letting it affect delivery."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


_REPLY_REASONS = {
    "not_directed_to_bot",
    "uncertain_target",
    "topic_over",
    "no_new_value",
    "already_answered",
    "needs_web_verification",
    "factual_help",
    "natural_continuation",
}


def _normalize_reply_reason(value):
    reason = str(value or "").strip().lower()
    return reason[:64] if reason in _REPLY_REASONS else None


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
        data = _load_model_json(text, preferred_keys=("messages", "animation"))
    except Exception:
        return build_raw_response(text, "LLM转发失败：返回内容不是合法JSON")

    if not isinstance(data, dict):
        return build_raw_response(text, "LLM转发失败：返回JSON不是对象")

    # Some compatible gateways/models use {"reply": "..."} even when the
    # requested contract says messages[]. Normalize that harmless alias before
    # validation so a valid model answer is not silently dropped.
    if "messages" not in data and isinstance(data.get("reply"), str):
        data = dict(data)
        data["messages"] = [data.get("reply")]

    if "messages" in data and not isinstance(data["messages"], list):
        return build_raw_response(text, "LLM转发失败：messages字段格式错误")

    if "animation" in data and data["animation"] is not None and not isinstance(data["animation"], str):
        return build_raw_response(text, "LLM转发失败：animation字段格式错误")

    normalized_messages = []
    for item in data.get("messages", []):
        if not isinstance(item, str):
            return build_raw_response(text, "LLM转发失败：messages必须是字符串数组")
        content = _strip_batch_index_prefix(item)
        if content:
            normalized_messages.extend(_split_chat_message(content))
    normalized_messages, dropped_count = _dedupe_messages(normalized_messages)
    if dropped_count:
        print(
            f"[LLM PARSER] dropped {dropped_count} exact-duplicate message(s) from model response",
            flush=True,
        )

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
            # The model often echoes the transcript marker ("batch_index=6:")
            # or wraps the identifier in brackets ("[抽象]", "别急 [我在]")
            # instead of using the animation field.  Recognise all three
            # forms so the sticker is delivered as a picture, never as text.
            head, matched = _extract_emoji_suffix(_strip_batch_index_prefix(item), valid_emoji_set)
            if transferred_animation is None and matched is not None:
                transferred_animation = matched
                if head:
                    cleaned_messages.append(head)
                continue
            cleaned_messages.append(item)

        if transferred_animation is not None:
            animation = transferred_animation
            normalized_messages = cleaned_messages

    # Both known fields may be absent. This is a valid, empty response; the
    # worker will simply have nothing to send. Unknown keys were discarded by
    # constructing the return object below.
    result = {
        "messages": normalized_messages,
        "animation": animation,
        "should_reply": bool(normalized_messages or animation),
    }
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result


def parse_proactive_response(text: str, emoji_list: list, force_reply=False) -> dict:
    """Parse the batch decision returned by the proactive-reply prompt."""
    def _proactive_error(reason):
        # An empty final tool/assistant content is not useful group text.
        # Invalid output stays silent even when routing released the request
        # directly, so parser diagnostics can never become group messages.
        if not str(text or "").strip():
            return {
                "messages": [],
                "animation": None,
                "should_reply": False,
                "reply_to": [],
                "_valid": False,
            }
        print(f"[LLM PARSER] silent invalid proactive response: {reason}", flush=True)
        return {
            "messages": [],
            "animation": None,
            "should_reply": False,
            "reply_to": [],
            "_valid": False,
        }

    try:
        data = _load_model_json(
            text,
            preferred_keys=("should_reply", "messages", "reply_to", "animation"),
        )
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

    if "messages" not in data and isinstance(data.get("reply"), str):
        data = dict(data)
        data["messages"] = [data.get("reply")]

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
    if not should_reply:
        normalized["messages"] = []
        normalized["animation"] = None

    result = {
        "messages": normalized.get("messages") or [],
        "animation": normalized.get("animation"),
        "should_reply": bool(should_reply),
        "reply_to": reply_to,
    }
    reply_confidence = _normalize_reply_confidence(data.get("reply_confidence"))
    if reply_confidence is not None:
        result["reply_confidence"] = reply_confidence
    reply_reason = _normalize_reply_reason(data.get("reply_reason"))
    if reply_reason:
        result["reply_reason"] = reply_reason
    idle_content_plan = _normalize_idle_content_plan(data.get("share_idle_content"))
    if idle_content_plan:
        result["share_idle_content"] = idle_content_plan
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result