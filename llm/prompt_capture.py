"""Thread-safe snapshot of the last prompt sent to the LLM provider."""

from __future__ import annotations

import json
import threading


_LOCK = threading.RLock()
_LAST_PROMPT = ""


def _render_message(message, index):
    if isinstance(message, dict):
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        lines = [f"[{index}] role={role}"]
        if content not in (None, ""):
            lines.append(str(content))
        for key in ("name", "tool_call_id"):
            if message.get(key) not in (None, ""):
                lines.append(f"{key}={message[key]}")
        if message.get("tool_calls"):
            lines.append(
                "tool_calls="
                + json.dumps(message["tool_calls"], ensure_ascii=False, default=str)
            )
        return "\n".join(lines)

    return f"[{index}] role=unknown\n{str(message)}"


def capture_prompt(messages) -> str:
    """Capture the exact message contents of the next provider request."""
    global _LAST_PROMPT
    rendered = "\n\n".join(
        _render_message(message, index)
        for index, message in enumerate(messages or [], 1)
    ).strip()
    with _LOCK:
        _LAST_PROMPT = rendered
    return rendered


def get_last_prompt() -> str:
    with _LOCK:
        return _LAST_PROMPT
