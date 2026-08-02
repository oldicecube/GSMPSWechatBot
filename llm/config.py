import json
import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        "history_expire_ms",
    ]

    missing_fields = [field for field in required_fields if field not in llm_config]
    if missing_fields:
        raise ValueError(f"Missing llm config fields: {', '.join(missing_fields)}")

    max_history_chars = _safe_int(
        llm_config.get("max_history_chars", llm_config.get("max_history", 5000)),
        5000,
    )
    group_message_limit_chars = _safe_int(
        llm_config.get(
            "group_message_limit_chars",
            llm_config.get("group_message_limit", 2000),
        ),
        2000,
    )

    result = {
        "enabled": llm_config["enabled"],
        "provider": llm_config["provider"],
        "model": llm_config["model"],
        "max_history_chars": max(0, max_history_chars),
        "group_message_limit_chars": max(0, group_message_limit_chars),
        # Backward-compatible aliases for modules that still read the old names.
        "max_history": max(0, max_history_chars),
        "history_expire_ms": llm_config["history_expire_ms"],
        "group_message_limit": max(0, group_message_limit_chars),
        "web_fetch_enabled": bool(llm_config.get("web_fetch_enabled", True)),
        "web_fetch_max_calls": _safe_int(llm_config.get("web_fetch_max_calls", 3), 3),
        "web_fetch_timeout_seconds": _safe_float(
            llm_config.get("web_fetch_timeout_seconds", 15), 15.0
        ),
        "web_fetch_max_chars": _safe_int(llm_config.get("web_fetch_max_chars", 24000), 24000),
        "original_message_enabled": bool(llm_config.get("original_message_enabled", True)),
        "original_message_max_calls": max(1, min(_safe_int(llm_config.get("original_message_max_calls", 2), 2), 4)),
        "original_message_timeout_seconds": _safe_float(
            llm_config.get("original_message_timeout_seconds", 8), 8.0
        ),
        "original_message_max_chars": _safe_int(
            llm_config.get("original_message_max_chars", 16000), 16000
        ),
    }

    learning = llm_config.get("learning")
    if not isinstance(learning, dict):
        learning = {}
    result["learning"] = {
        "enabled": bool(learning.get("enabled", True)),
        "db_path": str(learning.get("db_path") or "data/llm_learning.sqlite3").strip(),
        "queue_max": max(100, _safe_int(learning.get("queue_max", 2000), 2000)),
        "min_term_count": max(2, _safe_int(learning.get("min_term_count", 3), 3)),
        "prompt_max_chars": max(400, _safe_int(learning.get("prompt_max_chars", 1800), 1800)),
        "bot_names": [
            str(item).strip()
            for item in (([learning.get("bot_names")] if isinstance(learning.get("bot_names"), str) else learning.get("bot_names")) or [])
            if str(item).strip()
        ],
    }

    weflow_config = config.get("weflow")
    if not isinstance(weflow_config, dict):
        weflow_config = {}
    api_base = str(weflow_config.get("apiBase") or "").strip()
    if not api_base:
        api_host = str(weflow_config.get("apiHost") or "127.0.0.1").strip() or "127.0.0.1"
        api_port = _safe_int(weflow_config.get("apiPort", 5031), 5031)
        api_base = f"http://{api_host}:{api_port}"
    result["weflow_api_base"] = api_base.rstrip("/")
    result["weflow_api_token"] = str(
        config.get("token") or weflow_config.get("apiToken") or ""
    ).strip()
    bot_wxids = llm_config.get("bot_wxids") or []
    if isinstance(bot_wxids, str):
        bot_wxids = [bot_wxids]
    if weflow_config.get("myWxid"):
        bot_wxids = list(bot_wxids) + [weflow_config.get("myWxid")]
    result["bot_wxids"] = sorted({str(item).strip() for item in bot_wxids if str(item).strip()})

    target_groups = config.get("target_group", [])
    if isinstance(target_groups, str):
        target_groups = [target_groups]
    result["target_groups"] = [
        str(item).strip()
        for item in (target_groups if isinstance(target_groups, list) else [])
        if str(item).strip()
    ]

    auto_reply = llm_config.get("auto_reply")
    if not isinstance(auto_reply, dict):
        auto_reply = llm_config.get("random_reply")
    result["auto_reply"] = dict(auto_reply) if isinstance(auto_reply, dict) else {}
    # Backward-compatible alias for existing deployments.
    result["random_reply"] = dict(result["auto_reply"])

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
