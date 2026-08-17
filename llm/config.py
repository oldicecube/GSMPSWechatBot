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


def _normalize_api_entries(llm_config, default_timeout):
    """Normalize the multi-LLM API pool list from ``llm.apis``.

    Each entry: {name, protocol, model, api_key, base_url, timeout_seconds}.
    Falls back to the legacy single ``model``/``api_key``/``api_base`` fields
    when ``apis`` is absent or empty.
    """
    entries = []
    raw_apis = llm_config.get("apis")
    if isinstance(raw_apis, list):
        for item in raw_apis:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip() or f"api{len(entries) + 1}"
            protocol = str(item.get("protocol") or "openai").strip().lower()
            if protocol not in {"openai", "responses", "anthropic"}:
                protocol = "openai"
            model = str(item.get("model") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            base_url = str(item.get("base_url") or item.get("api_base") or "").strip().rstrip("/")
            if not model or not api_key or not base_url:
                continue
            timeout = _safe_float(item.get("timeout_seconds", default_timeout), default_timeout)
            timeout = max(10.0, min(timeout, 120.0))
            max_tokens = _safe_int(item.get("max_tokens", 0), 0)
            # 显式调用优先级：越小越先调用；缺省按数组顺序（位置+1）。
            priority = _safe_int(item.get("priority", 0), 0)
            if priority <= 0:
                priority = len(entries) + 1
            entries.append({
                "name": name,
                "protocol": protocol,
                "model": model,
                "api_key": api_key,
                "base_url": base_url,
                "timeout_seconds": timeout,
                "max_tokens": max(1, max_tokens) if max_tokens > 0 else 0,
                "priority": priority,
                # 协议缓存开关：anthropic 走 cache_control；responses 走 prompt_cache_key。
                "cache": bool(item.get("cache", True)),
                "prompt_cache_key": str(item.get("prompt_cache_key") or "").strip(),
                # 缓存范围：full=完整前缀缓存；system=仅系统提示词可缓存；
                # none=不可缓存。缺省按协议在 provider 内决定。
                "cache_scope": str(item.get("cache_scope") or "").strip().lower(),
                # Anthropic cache_control 的 TTL：空=默认5分钟；"1h"=1小时。
                "cache_ttl": str(item.get("cache_ttl") or "").strip().lower(),
                # Anthropic cache_mode：auto=网关自动缓存（仅声明 TTL，请求顶层 auto_cached=true）；
                # manual=消息内注入 cache_control 断点；off=关闭缓存。
                "cache_mode": str(item.get("cache_mode") or "").strip().lower(),
            })
    if not entries:
        model = str(llm_config.get("model") or "").strip()
        api_key = str(llm_config.get("api_key") or "").strip()
        base_url = str(llm_config.get("api_base") or llm_config.get("base_url") or "").strip().rstrip("/")
        if model and api_key:
            timeout = _safe_float(llm_config.get("request_timeout_seconds", default_timeout), default_timeout)
            timeout = max(10.0, min(timeout, 120.0))
            entries.append({
                "name": "default",
                "protocol": "openai",
                "model": model,
                "api_key": api_key,
                "base_url": base_url or "https://api.deepseek.com",
                "timeout_seconds": timeout,
                "max_tokens": 0,
                "cache": True,
                "prompt_cache_key": "",
                "cache_scope": "full",
                "cache_mode": "auto",
                "cache_ttl": "",
                "priority": 1,
            })
    return entries


def _normalize_time_slots(config):
    """Expose the top-level ``time_slots`` list to LLM modules (e.g. the pool's
    daily reset uses the start of the no-response window)."""
    slots = config.get("time_slots") or []
    if isinstance(slots, dict):
        slots = [slots]
    if not isinstance(slots, list):
        return []
    return [dict(item) for item in slots if isinstance(item, dict)]


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

    prefix_mode = str(config.get("prefix_mode", "strict") or "strict").strip().lower()
    if prefix_mode == "only":
        prefix_mode = "strict"
    if prefix_mode not in {"strict", "mixed"}:
        prefix_mode = "strict"

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
        "prefix_mode": prefix_mode,
        "max_history_chars": max(0, max_history_chars),
        "group_message_limit_chars": max(0, group_message_limit_chars),
        "context_window_tokens": max(
            10000, _safe_int(llm_config.get("context_window_tokens", 200000), 200000)
        ),
        "context_compression_target_tokens": max(
            5000, _safe_int(llm_config.get("context_compression_target_tokens", 160000), 160000)
        ),
        # 聊天上下文缓存前缀预算（token）：把历史拆成「稳定前缀(缓存断点) + 动态尾部」。
        "cache_prefix_tokens": max(
            0, _safe_int(llm_config.get("cache_prefix_tokens", 24000), 24000)
        ),
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
        # Keep provider work bounded across the Worker pool and proactive
        # timer threads. The dispatcher applies the actual semaphore.
        "max_concurrent_requests": max(
            1, min(_safe_int(llm_config.get("max_concurrent_requests", 1), 1), 4)
        ),
        "direct_request_wait_seconds": max(
            0.0, min(_safe_float(llm_config.get("direct_request_wait_seconds", 3), 3.0), 10.0)
        ),
        "tool_loop_timeout_seconds": max(
            10.0, min(_safe_float(llm_config.get("tool_loop_timeout_seconds", 45), 45.0), 120.0)
        ),
        "request_timeout_seconds": max(
            10.0, min(_safe_float(llm_config.get("request_timeout_seconds", 45), 45.0), 120.0)
        ),
        # 缓存成本模型：命中:未命中价格比（如 DeepSeek 约 1:5.428）。
        "cache_cost_ratio": max(1.0, _safe_float(llm_config.get("cache_cost_ratio", 5.428), 5.428)),
        # 缓存命中率估计（用于压缩成本决策）。
        "cache_hit_rate": min(0.99, max(0.0, _safe_float(llm_config.get("cache_hit_rate", 0.85), 0.85))),
        # 上下文压缩回收成本的最长请求数（回本周期）。
        "cache_break_even_horizon": max(1, _safe_int(llm_config.get("cache_break_even_horizon", 40), 40)),
    }


    # ---- 多 LLM API 池 ----
    request_timeout = result["request_timeout_seconds"]
    result["apis"] = _normalize_api_entries(llm_config, request_timeout)
    if result["apis"]:
        # 向后兼容：主入口指向优先级最高的 API（priority 越小越先）。
        _primary = sorted(result["apis"], key=lambda e: int(e.get("priority") or 0))[0]
        result["api_key"] = _primary["api_key"]
        result["api_base"] = _primary["base_url"]
        result["model"] = _primary["model"]
    else:
        result["api_key"] = ""
        result["api_base"] = ""
    # 每日重置时刻依赖顶部 time_slots（bot 不响应时间段起点）。
    result["time_slots"] = _normalize_time_slots(config)

    learning = llm_config.get("learning")
    if not isinstance(learning, dict):
        learning = {}
    result["learning"] = {
        "enabled": bool(learning.get("enabled", True)),
        "db_path": str(learning.get("db_path") or "data/llm_learning.sqlite3").strip(),
        "queue_max": max(100, _safe_int(learning.get("queue_max", 2000), 2000)),
        "min_term_count": max(2, _safe_int(learning.get("min_term_count", 2), 2)),
        "prompt_max_chars": max(400, _safe_int(learning.get("prompt_max_chars", 1800), 1800)),
        "style_card_max_chars": max(600, _safe_int(learning.get("style_card_max_chars", 1800), 1800)),
        "slang_scene_enabled": bool(learning.get("slang_scene_enabled", True)),
        "scene_cache_ttl_seconds": max(30, _safe_int(learning.get("scene_cache_ttl_seconds", 900), 900)),
        "scene_cache_max_items": max(1, min(_safe_int(learning.get("scene_cache_max_items", 8), 8), 24)),
        "scene_prompt_max_chars": max(240, min(_safe_int(learning.get("scene_prompt_max_chars", 900), 900), 3000)),
        "slang_min_occurrences": max(2, _safe_int(learning.get("slang_min_occurrences", 2), 2)),
        "expression_max_items": max(1, min(_safe_int(learning.get("expression_max_items", 6), 6), 12)),
        "expression_max_chars": max(300, min(_safe_int(learning.get("expression_max_chars", 900), 900), 2400)),
        "expression_recall_scan_limit": max(200, _safe_int(learning.get("expression_recall_scan_limit", 2000), 2000)),
        "expression_pool_size": max(4, min(_safe_int(learning.get("expression_pool_size", 12), 12), 24)),
        "expression_selector_enabled": bool(learning.get("expression_selector_enabled", True)),
        "expression_selector_max_items": max(1, min(_safe_int(learning.get("expression_selector_max_items", 4), 4), 8)),
        "expression_selector_max_chars": max(400, min(_safe_int(learning.get("expression_selector_max_chars", 1400), 1400), 4000)),
        "expression_eval_enabled": bool(learning.get("expression_eval_enabled", False)),
        "expression_eval_max_items": max(1, min(_safe_int(learning.get("expression_eval_max_items", 6), 6), 12)),
        "slang_emotional_pool_rotation": bool(learning.get("slang_emotional_pool_rotation", True)),
        "style_switch_enabled": bool(learning.get("style_switch_enabled", True)),
        "style_switch_cooldown_seconds": max(0, _safe_int(learning.get("style_switch_cooldown_seconds", 120), 120)),
        "bot_names": [
            str(item).strip()
            for item in (([learning.get("bot_names")] if isinstance(learning.get("bot_names"), str) else learning.get("bot_names")) or [])
            if str(item).strip()
        ],
    }

    memory = llm_config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    result["memory"] = {
        "enabled": bool(memory.get("enabled", True)),
        "db_path": str(memory.get("db_path") or "data/memory.sqlite3").strip(),
        "context_max_chars": max(0, _safe_int(memory.get("context_max_chars", 0), 0)),
        "person_fact_limit": max(1, _safe_int(memory.get("person_fact_limit", 8), 8)),
        "group_knowledge_limit": max(1, _safe_int(memory.get("group_knowledge_limit", 10), 10)),
        "candidate_batch_size": max(10, _safe_int(memory.get("candidate_batch_size", 30), 30)),
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

    prefixes = config.get("prefix", [])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    result["prefixes"] = [
        str(item).strip()
        for item in (prefixes if isinstance(prefixes, list) else [])
        if str(item).strip()
    ]

    auto_reply = llm_config.get("auto_reply")
    if not isinstance(auto_reply, dict):
        auto_reply = {"enabled": prefix_mode == "mixed"}
    result["auto_reply"] = dict(auto_reply)
    result["auto_reply"]["reply_trigger_mode"] = str(
        auto_reply.get("reply_trigger_mode", "conversation_pulse") or "conversation_pulse"
    ).strip().lower()
    if result["auto_reply"]["reply_trigger_mode"] not in {"frequency", "reply_necessity", "conversation_pulse"}:
        result["auto_reply"]["reply_trigger_mode"] = "conversation_pulse"
    try:
        result["auto_reply"]["reply_necessity_threshold"] = max(
            0, min(200, int(auto_reply.get("reply_necessity_threshold", 35)))
        )
    except (TypeError, ValueError):
        result["auto_reply"]["reply_necessity_threshold"] = 35

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
    """Return the highest-priority LLM API entry's key (backward compatible)."""
    llm_config = get_llm_config()
    apis = llm_config.get("apis") or []
    if not apis:
        raise ValueError("Missing llm api entries")
    primary = sorted(apis, key=lambda e: int(e.get("priority") or 0))[0]
    api_key = str(primary.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Missing llm api_key")
    return api_key
