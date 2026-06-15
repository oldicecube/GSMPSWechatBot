import random
import threading
import time

FALLBACK_ONLY = True
INTERCEPT_LLM = False

CONFIG = None
LISTENING = False
NEXT_TIME = 0
STATE_LOCK = threading.Lock()


def init(config):
    global CONFIG, NEXT_TIME

    CONFIG = config
    NEXT_TIME = _next_trigger_time()

    print("[AUTO RANDOM_REPLY] initialized")
    print("[AUTO RANDOM_REPLY] cooldown started")

    threading.Thread(target=_timer_loop, daemon=True).start()


def _next_trigger_time():
    return time.time() + random.randint(600, 1800)


def _timer_loop():
    global LISTENING

    while True:
        with STATE_LOCK:
            listening = LISTENING
            next_time = NEXT_TIME

        if listening:
            time.sleep(1)
            continue

        remain = next_time - time.time()
        if remain > 0:
            time.sleep(min(remain, 1))
            continue

        with STATE_LOCK:
            if not LISTENING and time.time() >= NEXT_TIME:
                LISTENING = True
                print("[AUTO RANDOM_REPLY] waiting for next unmatched message")


def handle_auto(context):
    global LISTENING, NEXT_TIME

    if not _is_eligible_message(context):
        return None

    with STATE_LOCK:
        if not LISTENING:
            remain = int(max(NEXT_TIME - time.time(), 0))
            print(f"[AUTO RANDOM_REPLY] cooldown: {remain}s")
            return None

        LISTENING = False
        NEXT_TIME = _next_trigger_time()

    user = context.get("user", "unknown")
    text = context.get("content", "")
    print(f"[AUTO RANDOM_REPLY] forwarding unmatched message to llm -> {user}: {text}")
    return {"forward_to_llm": True}


def allow_llm(context):
    return True


def _is_eligible_message(context):
    content = str(context.get("content") or "").strip()
    if not content:
        return False

    if context.get("prefix_used"):
        return False

    if content.startswith("/"):
        return False

    if content.startswith("[") and content.endswith("]"):
        return False

    return True
