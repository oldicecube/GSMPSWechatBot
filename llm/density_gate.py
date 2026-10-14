"""Three-stage local admission for ordinary proactive provider work.

No model calls, sleeps, cache warming or reply authoring. Direct requests are
handled separately. A budget is spent on request admission, including silence.
"""
from __future__ import annotations

import math


DEFAULTS = {
    "density_gate_enabled": True,
    "density_low_messages_per_minute": 3,
    "density_high_messages_per_minute": 8,
    "density_low_accumulate_messages": 4,
    "density_low_accumulate_seconds": 600,
    "density_low_cooldown_seconds": 600,
    "density_medium_batch_messages": 3,
    "density_high_batch_messages": 2,
    "density_burst_seconds": 120,
    "density_burst_idle_seconds": 60,
    "density_request_interval_seconds": 20,
    "density_burst_max_requests": 4,
    "density_burst_cooldown_seconds": 180,
}


def enabled(settings):
    value = (settings or {}).get("density_gate_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class DensityGate:
    def __init__(self, settings=None):
        self.settings = dict(DEFAULTS, **(settings or {}))
        # Owned under ProactiveReplyManager.lock, not shared across groups.
        self.groups = {}

    def number(self, name, minimum=1):
        try:
            value = float(self.settings[name])
            if not math.isfinite(value):raise ValueError
        except (TypeError, ValueError):
            value = float(DEFAULTS[name])
        return max(minimum, value)

    def admit(self, group_id, now, rate, pending):
        """Return stage/admission/wakeup without removing an unadmitted batch."""
        state = self.groups.setdefault(str(group_id), {
            "window_start": None, "window_end": 0, "last_admitted": None,
            "last_message_at": None, "requests": 0, "cooldown_until": 0,
        })
        low = self.number("density_low_messages_per_minute")
        high = max(low + 1, self.number("density_high_messages_per_minute"))
        stage = "low" if rate < low else "medium" if rate < high else "high"
        times = []
        for message in pending:
            try:
                timestamp = float(message.get("timestamp", now))
                if not math.isfinite(timestamp):raise ValueError
            except (TypeError, ValueError):timestamp = now
            times.append(min(now, timestamp))
        newest = max(times) if times else None
        old_newest = state["last_message_at"]
        gap = newest-old_newest if newest is not None and old_newest is not None else 0
        if state["window_start"] is not None and (
            now >= state["window_end"]
            or (old_newest is not None and now-old_newest >= self.number("density_burst_idle_seconds"))
            or gap >= self.number("density_burst_idle_seconds")
        ):
            state["window_start"] = None
            state["cooldown_until"] = max(state["cooldown_until"], now+self.number("density_burst_cooldown_seconds"))
        if newest is not None:
            state["last_message_at"] = max(newest, old_newest or newest)
        decision = {"stage": stage, "admit": False, "reason": "accumulate", "next_at": now+5}
        if not pending:
            decision["reason"] = "no_new_messages"
            return decision
        if now < state["cooldown_until"]:
            decision.update(reason="window_cooldown", next_at=state["cooldown_until"])
            return decision
        active = state["window_start"] is not None
        if not active:
            required = int(self.number(
                "density_low_accumulate_messages" if stage == "low"
                else "density_medium_batch_messages" if stage == "medium" else "density_high_batch_messages"
            ))
            if len(pending) < required:
                return decision
            last = state["last_admitted"]
            if stage == "low" and last is not None and now-last < self.number("density_low_cooldown_seconds"):
                decision.update(reason="low_density_cooldown", next_at=last+self.number("density_low_cooldown_seconds"))
                return decision
            state.update(window_start=now, window_end=now+self.number("density_burst_seconds"), requests=0)
        last = state["last_admitted"]
        if last is not None and now-last < self.number("density_request_interval_seconds"):
            decision.update(reason="batch_interval", next_at=last+self.number("density_request_interval_seconds"))
            return decision
        if active and len(pending) < int(self.number("density_high_batch_messages")):
            decision.update(reason="collect_new_batch")
            return decision
        if state["requests"] >= int(self.number("density_burst_max_requests")):
            state["window_start"] = None
            state["cooldown_until"] = now+self.number("density_burst_cooldown_seconds")
            decision.update(reason="window_budget", next_at=state["cooldown_until"])
            return decision
        # Spend budget even if a model chooses silence. No empty keepalive calls.
        state["last_admitted"] = now
        state["requests"] += 1
        decision.update(admit=True, reason="activity_window", next_at=now+self.number("density_request_interval_seconds"),
                        window_end=state["window_end"], request_number=state["requests"])
        return decision
