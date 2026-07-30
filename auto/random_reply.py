import random
import threading
import time
from core.group_policy import is_allowed_group, normalize_target_groups

FALLBACK_ONLY = True
INTERCEPT_LLM = False
DISABLED = True

CONFIG = {}
LISTENING = False
NEXT_TIME = 0.0
STATE_LOCK = threading.Lock()
_CLOCK = time.time
_RANDINT = random.randint


def _settings():
    raw = (CONFIG.get("llm") or {}).get("random_reply")
    return raw if isinstance(raw, dict) else {}


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default if value is None else bool(value)


def init(config):
    global CONFIG, NEXT_TIME, DISABLED, LISTENING
    CONFIG = config if isinstance(config, dict) else {}
    DISABLED = not _as_bool(_settings().get("enabled", False), False)
    with STATE_LOCK:
        LISTENING = False
        NEXT_TIME = _next_trigger_time()
    if DISABLED:
        print("[AUTO RANDOM_REPLY] disabled", flush=True)
        return
    print("[AUTO RANDOM_REPLY] initialized", flush=True)
    print("[AUTO RANDOM_REPLY] cooldown started", flush=True)
    threading.Thread(target=_timer_loop, daemon=True, name="random-reply-timer").start()


def _cooldown_bounds():
    settings = _settings()
    try:
        minimum = max(1, int(settings.get("min_cooldown_seconds", 600)))
    except (TypeError, ValueError):
        minimum = 600
    try:
        maximum = max(1, int(settings.get("max_cooldown_seconds", 1800)))
    except (TypeError, ValueError):
        maximum = 1800
    maximum = max(minimum, maximum)
    return minimum, maximum


def _next_trigger_time():
    minimum, maximum = _cooldown_bounds()
    return _CLOCK() + _RANDINT(minimum, maximum)


def _timer_loop():
    global LISTENING
    while not DISABLED:
        with STATE_LOCK:
            if not LISTENING and _CLOCK() >= NEXT_TIME:
                LISTENING = True
                print("[AUTO RANDOM_REPLY] waiting for next unmatched message", flush=True)
        time.sleep(1)


def handle_auto(context):
    global LISTENING, NEXT_TIME
    if DISABLED or not _is_eligible_message(context):
        return None
    with STATE_LOCK:
        if not LISTENING:
            return None
        LISTENING = False
        NEXT_TIME = _next_trigger_time()
    print("[AUTO RANDOM_REPLY] forwarding unmatched message to llm", flush=True)
    return {
        "forward_to_llm": True,
        "trigger_source": "random",
        "reply_allowed": True,
        "learn_only": False,
    }


def allow_llm(context):
    return _is_eligible_message(context)


def _allowed_groups():
    groups = _settings().get("allowed_groups")
    if isinstance(groups, str):
        groups = [groups]
    configured = normalize_target_groups(groups)
    target_groups = normalize_target_groups(CONFIG.get("target_group"))
    if configured:
        return configured.intersection(target_groups)
    return target_groups


def _is_eligible_message(context):
    settings = _settings()
    if not context.get("is_group"):
        return False
    if not is_allowed_group(context, _allowed_groups()):
        return False
    content = str(context.get("content") or "").strip()
    if not content:
        return False
    if _as_bool(settings.get("exclude_commands", True), True) and content.startswith("/"):
        return False
    if _as_bool(settings.get("exclude_mentions", True), True) and (
        context.get("prefix_used") or context.get("is_at") or context.get("is_mentioned")
    ):
        return False
    if _as_bool(settings.get("exclude_media", True), True) and (
        (content.startswith("[") and content.endswith("]"))
        or context.get("is_picture")
        or context.get("is_emoji")
        or context.get("is_voice")
    ):
        return False
    return True
