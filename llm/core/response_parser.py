import json


FALLBACK_RESPONSE = {
    "messages": ["？"],
    "animation": None
}


def parse_llm_response(text: str, emoji_list: list) -> dict:
    try:
        data = json.loads(text)

        if not isinstance(data, dict):
            return dict(FALLBACK_RESPONSE)

        messages = data.get("messages")
        animation = data.get("animation")

        if not isinstance(messages, list):
            return dict(FALLBACK_RESPONSE)

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
            normalized_messages = ["喏"]

        if not normalized_messages:
            return dict(FALLBACK_RESPONSE)

        return {
            "messages": normalized_messages,
            "animation": normalized_animation
        }
    except Exception:
        return dict(FALLBACK_RESPONSE)
