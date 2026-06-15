import json
import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_llm_config():
    config = _load_config()
    llm_config = config.get("llm")

    if not isinstance(llm_config, dict):
        raise ValueError("Missing llm config")

    required_fields = [
        "enabled",
        "provider",
        "model",
        "max_history",
        "history_expire_ms",
        "group_message_limit",
    ]

    missing_fields = [field for field in required_fields if field not in llm_config]
    if missing_fields:
        raise ValueError(f"Missing llm config fields: {', '.join(missing_fields)}")

    result = {
        "enabled": llm_config["enabled"],
        "provider": llm_config["provider"],
        "model": llm_config["model"],
        "max_history": llm_config["max_history"],
        "history_expire_ms": llm_config["history_expire_ms"],
        "group_message_limit": llm_config["group_message_limit"],
    }

    intercept_auto_plugins = llm_config.get("intercept_auto_plugins", [])
    if isinstance(intercept_auto_plugins, str):
        intercept_auto_plugins = [intercept_auto_plugins]
    if not isinstance(intercept_auto_plugins, list):
        intercept_auto_plugins = []
    result["intercept_auto_plugins"] = [
        str(item).strip()
        for item in intercept_auto_plugins
        if str(item).strip()
    ]

    prefix_bypass_wxids = llm_config.get("prefix_bypass_wxids", [])
    if isinstance(prefix_bypass_wxids, str):
        prefix_bypass_wxids = [prefix_bypass_wxids]
    if not isinstance(prefix_bypass_wxids, list):
        prefix_bypass_wxids = []
    result["prefix_bypass_wxids"] = [
        str(item).strip()
        for item in prefix_bypass_wxids
        if str(item).strip()
    ]

    admin_wxids = llm_config.get("admin_wxids", [])
    if isinstance(admin_wxids, str):
        admin_wxids = [admin_wxids]
    if not isinstance(admin_wxids, list):
        admin_wxids = []
    result["admin_wxids"] = [
        str(item).strip()
        for item in admin_wxids
        if str(item).strip()
    ]

    emoji_dir = str(
        llm_config.get("emoji_dir")
        or ""
    ).strip()
    result["emoji_dir"] = emoji_dir

    assistant_nickname = str(llm_config.get("assistant_nickname") or "LLM").strip()
    result["assistant_nickname"] = assistant_nickname or "LLM"

    identity = llm_config.get("identity")
    if not isinstance(identity, dict):
        identity = {}

    result["identity"] = {
        "name": str(identity.get("name") or "LLM").strip() or "LLM",
        "role": str(identity.get("role") or "微信群聊助手").strip() or "微信群聊助手",
        "style": str(identity.get("style") or "自然、简短、像真人微信聊天").strip() or "自然、简短、像真人微信聊天",
        "rules": [
            str(item).strip()
            for item in (identity.get("rules") or [])
            if str(item).strip()
        ]
    }

    prompt = llm_config.get("prompt")
    if not isinstance(prompt, dict):
        prompt = {}

    result["prompt"] = {
        "max_messages": int(prompt.get("max_messages", 3) or 3),
        "max_history_lines": int(prompt.get("max_history_lines", 100) or 100),
        "max_group_lines": int(prompt.get("max_group_lines", 20) or 20),
        "max_emoji_items": int(prompt.get("max_emoji_items", 50) or 50),
        "allow_animation": bool(prompt.get("allow_animation", True)),
        "prefer_short_reply": bool(prompt.get("prefer_short_reply", True)),
        "forbid_markdown": bool(prompt.get("forbid_markdown", True)),
        "forbid_explanation": bool(prompt.get("forbid_explanation", True)),
        "emoji_hint_text": str(prompt.get("emoji_hint_text") or "喏").strip() or "喏",
        "fallback_message": str(prompt.get("fallback_message") or "我在").strip() or "我在",
        "special_rules": [
            str(item).strip()
            for item in (prompt.get("special_rules") or [])
            if str(item).strip()
        ],
        "topic_redirect_rules": [
            str(item).strip()
            for item in (prompt.get("topic_redirect_rules") or [])
            if str(item).strip()
        ]
    }
    return result


def get_api_key():
    config = _load_config()
    llm_config = config.get("llm")

    if not isinstance(llm_config, dict):
        raise ValueError("Missing llm config")

    api_key = str(llm_config.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Missing llm api_key")

    return api_key
