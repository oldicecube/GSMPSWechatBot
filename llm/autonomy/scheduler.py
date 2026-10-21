from __future__ import annotations

import threading
import time
from typing import Any, Callable


class AutonomyScheduler:
    """Lightweight independent opportunity loop for the autonomy sidecar.

    It never chooses content or actions. It only asks the autonomy engine for
    an opportunity, and the engine's gate decides whether to spend resources.

    The check interval can be dynamic per group: ``interval_fn(group_id, now)``
    returns the number of seconds until the next opportunity for that group,
    so groups with a message backlog can be probed often while long-idle groups
    fall back to a slower baseline (miaobit-style pending + idle compensation,
    except that a fully idle group keeps a slow baseline instead of stopping).
    """

    def __init__(
        self,
        interval_seconds: float,
        group_provider: Callable[[], list[str]],
        opportunity_callback: Callable[[str, dict[str, Any]], Any],
        stop_event: threading.Event | None = None,
        interval_fn: Callable[[str, float], float] | None = None,
        min_loop_wait_seconds: float = 10.0,
        max_wait_seconds: float = 3600.0,
    ):
        self.interval_seconds = max(10.0, float(interval_seconds or 60))
        self.group_provider = group_provider
        self.opportunity_callback = opportunity_callback
        self.stop_event = stop_event or threading.Event()
        self.interval_fn = interval_fn if callable(interval_fn) else None
        self.min_loop_wait_seconds = max(1.0, float(min_loop_wait_seconds or 10.0))
        self.max_wait_seconds = max(self.min_loop_wait_seconds, float(max_wait_seconds or 3600.0))
        # next due timestamp per group id.
        self._next_due: dict[str, float] = {}
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return False
        self.thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="llm-autonomy-scheduler",
        )
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)

    def _interval_for(self, group_id: str, now: float) -> float:
        """Resolve the next-check interval for a group, clamped to safe bounds."""
        if self.interval_fn is None:
            value = self.interval_seconds
        else:
            try:
                value = float(self.interval_fn(group_id, now) or self.interval_seconds)
            except (TypeError, ValueError):
                value = self.interval_seconds
        if not value or value != value:  # NaN guard
            value = self.interval_seconds
        return max(self.min_loop_wait_seconds, min(value, self.max_wait_seconds))

    def _run_opportunity(self, group_id: str, now: float) -> None:
        try:
            self.opportunity_callback(group_id, {
                "source": "background_scheduler",
                "summary": "",
                "budget": 1,
                "passive_priority": False,
                "hard_safety": "pass",
                "now": now,
            })
        except Exception as exc:
            print(f"[LLM AUTONOMY SCHEDULER] group={group_id} error={exc}", flush=True)
        finally:
            # Anchor the next due time to when this check actually finished so
            # a slow LLM call cannot immediately re-trigger the same group.
            completed_at = time.time()
            self._next_due[group_id] = completed_at + self._interval_for(group_id, completed_at)

    def _process_once(self) -> float:
        """Run one scheduler round and return the wait seconds for the loop.

        Exposed separately so unit tests can drive scheduling without threads.
        """
        now = time.time()
        groups = [str(item).strip() for item in (self.group_provider() or [])]
        groups = list(dict.fromkeys(item for item in groups if item))
        for group_id in groups:
            if now < float(self._next_due.get(group_id) or 0.0):
                continue
            self._run_opportunity(group_id, now)
        known = set(groups)
        for key in [key for key in list(self._next_due) if key not in known]:
            self._next_due.pop(key, None)
        if not groups:
            return self.interval_seconds
        return max(0.0, min((float(self._next_due[g]) - time.time() for g in groups), default=0.0))

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                wait = self._process_once()
            except Exception as exc:
                print(f"[LLM AUTONOMY SCHEDULER] error={exc}", flush=True)
                wait = self.interval_seconds
            if self.stop_event.wait(wait):
                break
