"""LLM 内置主动回复状态机。

状态顺序通常为：idle -> attention -> working -> attention -> idle；
动态密度上界可把 idle 直接切入 attention，随机静默超时可把 idle 直接切入 working。
工作期先进入短暂缓冲窗口，再把窗口内消息集中交给 LLM；关注期按进入来源使用不同的上下文预算，
连续三次没有值得回复的内容才回到空闲期。
"""

from __future__ import annotations

import random
import math
import threading
import time
from urllib.parse import urlparse

from core.group_policy import is_allowed_group, normalize_target_groups
from llm.web_tools import AUTO_REPLY_URL_HOSTS, is_url_only


def _is_supported_url_only(content):
    if not is_url_only(content):
        return False
    hostname = (urlparse(str(content).strip()).hostname or "").rstrip(".").lower()
    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in AUTO_REPLY_URL_HOSTS
    )


class ProactiveReplyManager:
    def __init__(self, config):
        self.config = config if isinstance(config, dict) else {}
        self.settings = self.config.get("auto_reply") or {}
        self.prefix_mode = str(self.config.get("prefix_mode", "strict") or "strict").strip().lower()
        self.target_groups = normalize_target_groups(self.config.get("target_groups"))
        self.allowed_groups = normalize_target_groups(self.settings.get("allowed_groups"))
        self.enabled = bool(self.settings.get("enabled", self.prefix_mode == "mixed"))
        if self.prefix_mode == "strict":
            self.enabled = False
        self.states = {}
        # Event-level guard for duplicate SSE/queue deliveries. Keys are
        # message IDs only; identical text without an ID remains valid.
        self.seen_message_ids = {}
        self.last_llm_reply_at = {}
        # Per-group counter of consecutive attention pulls that did NOT
        # trigger a slang emotional reply. Each pull rolls n*10% (n =
        # misses + 1) and the counter resets to 0 on trigger.
        self.slang_emotional_misses = {}
        self.tieba_repost_last_at = {}
        # Message-density events are control data, not chat history.
        self.density_events = {}
        self.proactive_web_last_at = {}
        # Independent of the idle/attention state machine: one random
        # proactive-web opportunity per group every 2--3 hours by default.
        # The counter is incremented only after a real outbound Bot message.
        self.proactive_web_due_at = {}
        self.proactive_web_bot_message_count = {}
        # The learner is injected after construction so the state machine can
        # gate attention checks locally without adding another LLM request.
        self.reply_timing_reader = None
        self.reply_timing_cache = {}
        self.reply_timing_cache_ttl = 60
        self.lock = threading.Lock()
        self.callback = None
        self.cycle_end_callback = None
        self.learning_settings = self.config.get("learning") or {}
        self.bot_wxids = {
            str(item).strip()
            for item in (self.config.get("bot_wxids") or [])
            if str(item).strip()
        }
        self.assistant_nickname = str(
            self.config.get("assistant_nickname") or "LLM"
        ).strip()
        identity = self.config.get("identity") or {}
        configured_bot_names = self.config.get("bot_names") or []
        if isinstance(configured_bot_names, str):
            configured_bot_names = [configured_bot_names]
        self.bot_names = {
            name.casefold()
            for name in configured_bot_names
            if str(name).strip()
        }
        for name in (
            self.assistant_nickname,
            identity.get("name") if isinstance(identity, dict) else "",
        ):
            if str(name).strip():
                self.bot_names.add(str(name).strip().casefold())
        self.clock = time.time
        self.randint = random.randint
        self.random = random.random
        if self.enabled:
            threading.Thread(
                target=self._timer_loop,
                daemon=True,
                name="llm-proactive-reply-timer",
            ).start()

    def set_batch_callback(self, callback):
        self.callback = callback if callable(callback) else None

    def set_cycle_end_callback(self, callback):
        self.cycle_end_callback = callback if callable(callback) else None

    def set_reply_timing_reader(self, callback):
        """Set a read-only learner callback used by the local attention gate."""
        self.reply_timing_reader = callback if callable(callback) else None

    def _refresh_reply_timing(self, group_ids, now):
        reader = self.reply_timing_reader
        if not reader:
            return
        refreshed = {}
        for group_id in group_ids:
            key = str(group_id)
            cached = self.reply_timing_cache.get(key)
            if cached and now - float(cached.get("at") or 0) < self.reply_timing_cache_ttl:
                continue
            try:
                counters = reader(key)
            except Exception as exc:
                print(f"[LLM PROACTIVE] timing learning read failed group={key}: {exc}", flush=True)
                counters = {}
            refreshed[key] = {
                "at": now,
                "counters": counters if isinstance(counters, dict) else {},
            }
        if refreshed:
            with self.lock:
                self.reply_timing_cache.update(refreshed)

    def _reply_timing_probability_locked(self, group_id):
        """Return the learned probability of spending an attention pull.

        This is deliberately a small local gate. It learns from historical
        attention outcomes, starts only after enough observations, and never
        applies to direct mentions/prefixes (those bypass the state machine).
        """
        if not bool(self.settings.get("reply_timing_learning_enabled", True)):
            return 1.0
        cached = self.reply_timing_cache.get(str(group_id)) or {}
        counters = cached.get("counters") or {}
        try:
            minimum = max(1, int(self.settings.get("reply_timing_min_windows", 20)))
            target = max(0.01, min(1.0, float(self.settings.get("reply_timing_target_reply_rate", 0.28))))
            floor = max(0.0, min(1.0, float(self.settings.get("reply_timing_probability_floor", 0.15))))
            windows = max(0, int(counters.get("attention_windows", 0) or 0))
            replies = max(0, int(counters.get("attention_reply_windows", 0) or 0))
        except (TypeError, ValueError):
            return 1.0
        if windows < minimum:
            return 1.0
        # Laplace smoothing prevents one short run from collapsing the gate.
        observed_rate = (replies + 1.0) / (windows + 2.0)
        if observed_rate <= target:
            return 1.0
        return max(floor, min(1.0, target / observed_rate))

    def get_reply_timing_learning(self, group_id):
        """Return the cached timing counters without a database read.

        The timer thread refreshes this cache periodically. Working-period
        gates can therefore reuse timing learning without blocking ingress on
        SQLite or starting another model request.
        """
        with self.lock:
            cached = self.reply_timing_cache.get(str(group_id)) or {}
            counters = cached.get("counters")
            return dict(counters) if isinstance(counters, dict) else {}

    def _learning_int(self, name, default, minimum=1):
        try:
            return max(minimum, int(self.learning_settings.get(name, default)))
        except (TypeError, ValueError):
            return default

    def _int_setting(self, name, default, minimum=1):
        try:
            return max(minimum, int(self.settings.get(name, default)))
        except (TypeError, ValueError):
            return default

    def _idle_bounds(self):
        # Old names remain as a backward-compatible fallback. They now mean
        # the random quiet timeout before proactive web forwarding.
        minimum = self._int_setting(
            "idle_quiet_min_seconds",
            self.settings.get("idle_min_seconds", 20 * 60),
        )
        maximum = self._int_setting(
            "idle_quiet_max_seconds",
            self.settings.get("idle_max_seconds", 40 * 60),
        )
        return minimum, max(minimum, maximum)

    def _float_setting(self, name, default, minimum=None):
        try:
            value = float(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = float(default)
        if minimum is not None:
            value = max(float(minimum), value)
        return value

    def _density_window_seconds(self):
        return self._int_setting("density_window_seconds", 60, 10)

    def _density_attention_interval_seconds(self):
        return self._int_setting("density_attention_check_interval_seconds", 30, 5)

    def _density_attention_half_life_seconds(self):
        return self._float_setting("density_attention_half_life_seconds", 5 * 60, 1)

    def _density_attention_curve_power(self):
        return self._float_setting("density_attention_curve_power", 1.7, 0.1)

    def _proactive_web_bounds(self):
        # This is intentionally independent from the retired six-hour
        # attention repost cooldown, including for old config files.
        minimum = self._int_setting("proactive_web_min_seconds", 2 * 3600, 60)
        maximum = self._int_setting("proactive_web_max_seconds", 3 * 3600, 60)
        return minimum, max(minimum, maximum)

    def _proactive_web_reset_after_messages(self):
        return self._int_setting("proactive_web_reset_after_bot_messages", 10, 1)

    def _reset_proactive_web_timer_locked(self, group_id, now, reason):
        group_id = str(group_id)
        due_at = now + self._random_seconds(self._proactive_web_bounds())
        self.proactive_web_due_at[group_id] = due_at
        self.proactive_web_bot_message_count[group_id] = 0
        print(
            f"[LLM PROACTIVE WEB] group={group_id} timer reset reason={reason} due_at={due_at}",
            flush=True,
        )
        return due_at

    def _ensure_proactive_web_timer_locked(self, group_id, now):
        group_id = str(group_id)
        if self.proactive_web_due_at.get(group_id) is None:
            self._reset_proactive_web_timer_locked(group_id, now, "initial")

    def record_sent_bot_message(self, group_id, count=1):
        """Count actual, separately delivered Bot messages for web scheduling."""
        group_id = str(group_id or "").strip()
        if not group_id:
            return
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            return
        if not count:
            return
        now = self.clock()
        with self.lock:
            # Ignore non-group/plugin output that has never entered this
            # manager, so unrelated private sends cannot create schedules.
            if group_id not in self.states:
                return
            self._ensure_proactive_web_timer_locked(group_id, now)
            total = self.proactive_web_bot_message_count.get(group_id, 0) + count
            if total > self._proactive_web_reset_after_messages():
                self._reset_proactive_web_timer_locked(group_id, now, "bot_message_limit")
            else:
                self.proactive_web_bot_message_count[group_id] = total

    def _density_upper_locked(self, group_id=None, state=None):
        """Return the configured hard upper count for the one-minute window."""
        value = self.settings.get(
            "density_upper_messages_per_minute",
            self.settings.get("density_upper_min_per_minute", 8),
        )
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 8.0

    def _update_density_locked(self, group_id, state, now):
        group_id = str(group_id)
        window = self._density_window_seconds()
        events = self.density_events.setdefault(group_id, [])
        cutoff = now - window
        self.density_events[group_id] = [stamp for stamp in events if stamp >= cutoff]
        rate = len(self.density_events[group_id]) * 60.0 / window

        state["density_upper"] = self._density_upper_locked(group_id, state)
        state["density_rate"] = rate
        return rate, float(state.get("density_upper") or 8.0)

    def _attention_probability(self, density_rate, density_upper):
        if density_upper <= 0 or density_rate <= 0:
            return 0.0
        normalized = max(0.0, min(1.0, density_rate / density_upper))
        hazard = (
            math.log(2.0) / self._density_attention_half_life_seconds()
        ) * (normalized ** self._density_attention_curve_power())
        return 1.0 - math.exp(-hazard * self._density_attention_interval_seconds())

    def _work_bounds(self):
        minimum = self._int_setting("work_min_seconds", 2 * 60)
        maximum = self._int_setting("work_max_seconds", 5 * 60)
        return minimum, max(minimum, maximum)

    def _attention_bounds(self):
        minimum = self._int_setting("attention_min_seconds", 2 * 60)
        maximum = self._int_setting("attention_max_seconds", 5 * 60)
        return minimum, max(minimum, maximum)

    def _attention_message_limit(self):
        return self._int_setting("attention_message_limit", 10)

    def _attention_no_reply_limit(self):
        return self._int_setting("attention_no_reply_limit", 3)

    def _attention_boost_after_seconds(self):
        return self._int_setting("attention_boost_after_seconds", 2 * 60 * 60)

    def _attention_nonsense_opportunity(self):
        """Grant one attention check a bounded 30% playful-interjection chance."""
        try:
            probability = float(self.settings.get("attention_nonsense_probability", 0.12) or 0.12)
        except (TypeError, ValueError):
            probability = 0.12
        probability = max(0.0, min(1.0, probability))
        return self.randint(1, 100) <= int(round(probability * 100))

    def _attention_slang_emotional_opportunity(self, group_id):
        """Escalating n*10% chance to trigger a slang emotional reply.

        n counts consecutive attention pulls that did NOT trigger. The first
        pull after a trigger rolls 10%, the second 20%, ..., capped at 100%.
        On trigger the counter is cleared so the cycle starts over.
        """
        try:
            step = float(self.settings.get("attention_slang_emotional_step", 0.1) or 0.1)
        except (TypeError, ValueError):
            step = 0.1
        step = max(0.0, min(1.0, step))
        misses = int(self.slang_emotional_misses.get(str(group_id), 0) or 0)
        n = misses + 1
        probability = min(1.0, n * step)
        triggered = self.randint(1, 100) <= int(round(probability * 100))
        if triggered:
            self.slang_emotional_misses[str(group_id)] = 0
        else:
            self.slang_emotional_misses[str(group_id)] = n
        return triggered


    def _tieba_repost_opportunity(self, group_id):
        """Legacy hook disabled; Tieba reposts use the independent timer."""
        return False

    def seconds_since_llm_reply(self, group_id):
        now = self.clock()
        with self.lock:
            last = self.last_llm_reply_at.get(str(group_id))
            if last is None:
                self.last_llm_reply_at[str(group_id)] = now
                return 0
        return max(0, int(now - last))

    def record_llm_reply(self, group_id):
        with self.lock:
            self.last_llm_reply_at[str(group_id)] = self.clock()

    def attention_boost_active(self, group_id):
        return self.seconds_since_llm_reply(group_id) >= self._attention_boost_after_seconds()

    def _style_switch_cooldown_seconds(self):
        try:
            return max(0, int(self.learning_settings.get("style_switch_cooldown_seconds", 120) or 120))
        except (TypeError, ValueError):
            return 120

    def get_active_style_switch(self, group_id):
        """Return the LLM-chosen style selection of the current cycle, if any."""
        group_id = str(group_id or "unknown")
        with self.lock:
            state = self.states.get(group_id)
            if not state:
                return None
            switch = state.get("active_style_switch")
            if not isinstance(switch, dict):
                return None
            return dict(switch)

    def set_active_style_switch(self, group_id, cycle_id=None, selection=None):
        """Apply an LLM-chosen style switch for the current cycle.

        The selection is cycle-scoped: it lives on the per-group state and is
        dropped automatically when the cycle returns to idle. A stale write
        from a finished cycle and a switch inside the cooldown window are
        ignored; clearing the selection always applies immediately.
        """
        group_id = str(group_id or "unknown")
        now = self.clock()
        with self.lock:
            state = self.states.get(group_id)
            if not state:
                return False
            if cycle_id is not None and state.get("cycle_id") and str(cycle_id) != str(state.get("cycle_id")):
                return False
            if selection is None:
                if state.get("active_style_switch") is None:
                    return False
                state["active_style_switch"] = None
                state["active_style_switch_at"] = None
                return True
            cooldown = self._style_switch_cooldown_seconds()
            last_at = state.get("active_style_switch_at") or 0
            if cooldown and last_at and now - float(last_at) < cooldown:
                return False
            state["active_style_switch"] = dict(selection)
            state["active_style_switch_at"] = now
            return True

    def _work_extend_threshold(self):
        return self._int_setting("work_extend_threshold_seconds", 3 * 60)

    def _work_extend_seconds(self):
        return self._int_setting("work_extend_seconds", 3 * 60)

    def _batch_debounce_seconds(self):
        # Keep the ingress path cheap while giving nearby messages a chance
        # to arrive in one contextual decision window.
        value = self.settings.get(
            "batch_debounce_seconds",
            self.settings.get("batch_interval_seconds", 15),
        )
        try:
            return max(1, min(int(value), 15))
        except (TypeError, ValueError):
            return 15

    def _batch_max_messages(self):
        try:
            return max(2, min(int(self.settings.get("batch_max_messages", 20)), 100))
        except (TypeError, ValueError):
            return 20

    def _random_seconds(self, bounds):
        return self.randint(bounds[0], bounds[1])

    def _new_idle_state(self, now):
        return {
            "phase": "idle",
            "phase_end": None,
            "quiet_started_at": now,
            "quiet_timeout_at": now + self._random_seconds(self._idle_bounds()),
            "next_density_attention_at": now + self._density_attention_interval_seconds(),
            "density_rate": 0.0,
            "density_upper": None,
            "recent_messages": [],
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "next_attention_at": None,
            "pending_batch": [],
            "pending_context": None,
            "pending_force_reply": False,
            "pending_trigger_source": "buffer",
            "batch_flush_at": None,
            "batch_flushing": False,
            "cycle_id": None,
            "cycle_messages": [],
            "cycle_snapshot": [],
            "cycle_context": None,
            "cycle_curation_pending": False,
            "active_style_switch": None,
            "active_style_switch_at": None,
        }

    def _new_work_state(self, now):
        return {
            "phase": "working",
            "phase_end": now + self._random_seconds(self._work_bounds()),
            # Work-period messages use the configured debounce buffer; the
            # local necessity gate runs only when that buffer is flushed.
            "recent_messages": [],
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "next_attention_at": None,
            "pending_batch": [],
            "pending_context": None,
            "pending_force_reply": False,
            "pending_trigger_source": "buffer",
            "batch_flush_at": None,
            "batch_flushing": False,
            "memory_update_used": False,
            "cycle_id": f"cycle-{int(now)}-{self.randint(1000, 9999)}",
            "cycle_messages": [],
            "active_style_switch": None,
            "active_style_switch_at": None,
            "quiet_started_at": None,
            "quiet_timeout_at": None,
        }

    def _new_attention_state(
        self,
        now,
        recent_messages=None,
        cycle_id=None,
        cycle_messages=None,
        attention_source="idle",
        quiet_started_at=None,
        quiet_timeout_at=None,
    ):
        if quiet_started_at is None or quiet_timeout_at is None:
            quiet_started_at = now
            quiet_timeout_at = now + self._random_seconds(self._idle_bounds())
        return {
            "phase": "attention",
            "phase_end": None,
            "next_attention_at": now + self._random_seconds(self._attention_bounds()),
            "recent_messages": list(recent_messages or []),
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "pending_batch": [],
            "pending_context": None,
            "pending_force_reply": False,
            "pending_trigger_source": "buffer",
            "batch_flush_at": None,
            "batch_flushing": False,
            "cycle_id": cycle_id or f"cycle-{int(now)}-{self.randint(1000, 9999)}",
            "cycle_messages": list(cycle_messages or recent_messages or []),
            "attention_source": str(attention_source or "idle"),
            "cycle_snapshot": [],
            "cycle_context": None,
            "cycle_curation_pending": False,
            "active_style_switch": None,
            "active_style_switch_at": None,
            "quiet_started_at": quiet_started_at,
            "quiet_timeout_at": quiet_timeout_at,
        }

    def _initial_state(self, now):
        return self._new_idle_state(now)

    def _group_key(self, context):
        return str(
            context.get("group")
            or context.get("sessionId")
            or context.get("user")
            or "unknown"
        ).strip() or "unknown"

    @staticmethod
    def _message_identity(message):
        for field in ("message_id", "local_id", "server_id"):
            value = message.get(field) if isinstance(message, dict) else None
            if value not in (None, "", 0, "0"):
                return f"{field}:{value}"
        return ""

    def _seen_message_locked(self, group_id, message, now):
        identity = self._message_identity(message)
        if not identity:
            return False
        seen = self.seen_message_ids.setdefault(group_id, {})
        cutoff = now - 600
        for key, timestamp in list(seen.items()):
            if timestamp < cutoff:
                seen.pop(key, None)
        if identity in seen:
            return True
        seen[identity] = now
        if len(seen) > 500:
            oldest = sorted(seen, key=seen.get)[:len(seen) - 500]
            for key in oldest:
                seen.pop(key, None)
        return False

    def _log_phase_change(self, group_id, old_phase, state, reason):
        new_phase = state.get("phase") if isinstance(state, dict) else None
        if old_phase == new_phase:
            return
        phase_end = state.get("phase_end") if isinstance(state, dict) else None
        print(
            f"[LLM PROACTIVE] group={group_id} 状态切换: {old_phase} -> {new_phase} "
            f"reason={reason} phase_end={phase_end}",
            flush=True,
        )

    def _start_proactive_work_locked(self, state, now, group_id):
        """Move idle/attention into working for a due live-web opportunity."""

        recent = list(state.get("recent_messages") or [])
        cycle_messages = list(state.get("cycle_messages") or [])
        old_phase = state.get("phase")
        context = dict(state.get("last_context") or {})
        state.clear()
        state.update(self._new_work_state(now))
        state["recent_messages"] = recent
        state["cycle_messages"] = cycle_messages
        state["last_context"] = context
        context["_cycle_id"] = state.get("cycle_id")
        context["_proactive_web_opportunity"] = True
        state["proactive_web_opportunity"] = True
        self.proactive_web_last_at[str(group_id)] = now
        self._reset_proactive_web_timer_locked(group_id, now, "web_opportunity")
        self._log_phase_change(group_id, old_phase, state, "proactive_timeout")
        pending = (context, [], False, "quiet_proactive") if self.callback is not None else None
        return pending, True

    def _advance_state_locked(self, state, now, group_id="unknown"):
        pending_dispatch = None
        if state["phase"] == "idle":
            density_rate = float(state.get("density_rate") or 0.0)
            density_upper = float(
                state.get("density_upper") or 8.0
            )

            # The upper boundary is a hard attention trigger.  This keeps a
            # genuinely active group from waiting for the stochastic check.
            if density_rate > density_upper:
                old_phase = state.get("phase")
                recent = list(state.get("recent_messages") or [])
                cycle_messages = list(state.get("cycle_messages") or [])
                state.update(self._new_attention_state(
                    now,
                    recent,
                    state.get("cycle_id"),
                    cycle_messages,
                    attention_source="density_upper",
                    quiet_started_at=state.get("quiet_started_at"),
                    quiet_timeout_at=state.get("quiet_timeout_at"),
                ))
                self._log_phase_change(group_id, old_phase, state, "density_upper")
                return pending_dispatch

            # The idle timer now only retains the old phase bookkeeping. Live
            # web reposting is scheduled by the independent 2--3 hour timer.
            if state.get("quiet_started_at") is None:
                state["quiet_started_at"] = now
                state["quiet_timeout_at"] = now + self._random_seconds(self._idle_bounds())

            next_check = state.get("next_density_attention_at")
            if next_check is None or now >= next_check:
                state["next_density_attention_at"] = now + self._density_attention_interval_seconds()
                probability = self._attention_probability(density_rate, density_upper)
                if self.random() < probability:
                    old_phase = state.get("phase")
                    recent = list(state.get("recent_messages") or [])
                    cycle_messages = list(state.get("cycle_messages") or [])
                    state.update(self._new_attention_state(
                        now,
                        recent,
                        state.get("cycle_id"),
                        cycle_messages,
                        attention_source="density_probability",
                        quiet_started_at=state.get("quiet_started_at"),
                        quiet_timeout_at=state.get("quiet_timeout_at"),
                    ))
                    self._log_phase_change(
                        group_id,
                        old_phase,
                        state,
                        f"density_probability={probability:.4f}",
                    )
        elif state["phase"] == "attention":
            pass
        elif state["phase"] == "working" and now >= state["phase_end"]:
            pending_dispatch = self._take_pending_batch_locked(state)
            recent = state.get("recent_messages") or []
            cycle_messages = state.get("cycle_messages") or []
            cycle_id = state.get("cycle_id")
            old_phase = state.get("phase")
            state.update(self._new_attention_state(
                now,
                recent,
                cycle_id,
                cycle_messages,
                attention_source="working",
            ))
            self._log_phase_change(group_id, old_phase, state, "work_timeout_to_attention")
        return pending_dispatch

    def _allowed_group(self, context):
        groups = self.allowed_groups or self.target_groups
        return is_allowed_group(context, groups)

    def _is_mention(self, context):
        if context.get("prefix_used") or context.get("is_at") or context.get("is_mentioned"):
            return True

        raw = context.get("raw") or {}
        if isinstance(raw, dict):
            if raw.get("prefix_used") or raw.get("is_at") or raw.get("is_mentioned"):
                return True
            source = raw.get("_raw") or {}
            if isinstance(source, dict) and (
                source.get("is_at") or source.get("isAt") or source.get("is_mentioned")
                or source.get("isMentioned") or source.get("atBot")
            ):
                return True
        return False

    def _message_content(self, context):
        if context.get("content") is not None:
            return str(context.get("content") or "").strip()
        raw = context.get("raw") or {}
        if isinstance(raw, dict) and raw.get("content") is not None:
            return str(raw.get("content") or "").strip()
        return str(context.get("content") or "").strip()

    def _is_bot_message(self, context):
        if context.get("is_bot"):
            return True
        raw = context.get("raw") or {}
        if isinstance(raw, dict) and raw.get("is_bot"):
            return True
        wxid = str(context.get("wxid") or "").strip()
        if wxid and wxid in self.bot_wxids:
            return True
        nickname = str(
            context.get("user")
            or (raw.get("user") if isinstance(raw, dict) else "")
            or ""
        ).strip().casefold()
        return bool(nickname and nickname in self.bot_names)

    def _eligible(self, context):
        if not self.enabled or not context.get("is_group"):
            return False
        if self._is_bot_message(context):
            return False
        if not self._allowed_group(context):
            return False
        content = self._message_content(context)
        if not content:
            return False

        if self.settings.get("exclude_commands", False):
            routed = str(context.get("content") or "").strip()
            if content.startswith("/") or routed.startswith("/"):
                return False
        if self.settings.get("exclude_media", False) and (
            (content.startswith("[") and content.endswith("]"))
            or context.get("is_picture")
            or context.get("is_emoji")
            or context.get("is_voice")
        ):
            return False
        return True

    def _serialize_message(self, context):
        raw = context.get("raw") or {}
        source = raw.get("_raw") if isinstance(raw, dict) else {}
        if not isinstance(source, dict):
            source = {}

        return {
            "message_id": raw.get("messageKey") or source.get("messageKey") or "",
            "local_id": raw.get("localId") or source.get("localId"),
            "server_id": (
                raw.get("serverId") or raw.get("svrid") or raw.get("rawid")
                or source.get("serverId") or source.get("rawid")
            ),
            "session_id": context.get("sessionId") or raw.get("sessionId") or "",
            "timestamp": raw.get("_ts") or self.clock(),
            "group": context.get("group") or raw.get("group") or "",
            "sender_nickname": context.get("user") or raw.get("user") or "未知用户",
            "sender_wxid": context.get("wxid") or raw.get("wxid") or "",
            "content": self._message_content(context),
            "is_url_only": _is_supported_url_only(self._message_content(context)),
            "is_at_bot": self._is_mention(context),
            "prefix_used": bool(context.get("prefix_used")),
            "is_command": str(context.get("content") or "").strip().startswith("/"),
            "message_type": raw.get("type") or source.get("messageType") or "text",
        }

    def _remember_locked(self, state, context, message):
        state["last_context"] = dict(context)
        recent = state.setdefault("recent_messages", [])
        recent.append(message)
        state["recent_messages"] = recent[-self._attention_message_limit():]
        cycle_messages = state.setdefault("cycle_messages", [])
        cycle_messages.append(message)

    def _take_pending_batch_locked(self, state):
        batch = list(state.get("pending_batch") or [])
        if not batch or state.get("batch_flushing"):
            return None

        context = dict(state.get("pending_context") or state.get("last_context") or {})
        context["_cycle_id"] = state.get("cycle_id")
        allow_memory_update = bool(
            state.get("phase") == "working" and not state.get("memory_update_used")
        )
        if allow_memory_update:
            state["memory_update_used"] = True
        context["_memory_update_allowed"] = allow_memory_update
        force_reply = bool(state.get("pending_force_reply"))
        trigger_source = str(state.get("pending_trigger_source") or "buffer")
        state["pending_batch"] = []
        state["pending_context"] = None
        state["pending_force_reply"] = False
        state["pending_trigger_source"] = "buffer"
        state["batch_flush_at"] = None
        state["batch_flushing"] = True
        return context, batch, force_reply, trigger_source

    def _directive(self, batch, context, force_reply, trigger_source, attention_check=False):
        return {
            "forward_batch_to_llm": True,
            "batch_messages": batch,
            "force_reply": bool(force_reply),
            "trigger_source": trigger_source,
            "attention_check": bool(attention_check),
            "group_id": self._group_key(context),
            "cycle_id": context.get("_cycle_id"),
        }

    def handle_message(self, context):
        if not self._eligible(context):
            return None

        group_id = self._group_key(context)
        now = self.clock()
        mention = self._is_mention(context)
        message = self._serialize_message(context)
        with self.lock:
            if self._seen_message_locked(group_id, message, now):
                print(f"[LLM PROACTIVE] duplicate message ignored: {self._message_identity(message)}", flush=True)
                return None
            state = self.states.get(group_id)
            if state is None:
                state = self._initial_state(now)
                self.states[group_id] = state
                self._ensure_proactive_web_timer_locked(group_id, now)
                self.last_llm_reply_at.setdefault(group_id, now)
                self._log_phase_change(group_id, "none", state, "initial")
            self.density_events.setdefault(group_id, []).append(now)
            self._update_density_locked(group_id, state, now)
            expired_dispatch = self._advance_state_locked(state, now, group_id)
            self._remember_locked(state, context, message)

            if expired_dispatch and self.callback is not None:
                threading.Thread(
                    target=self._run_callback,
                    args=(group_id, expired_dispatch[0], expired_dispatch[1], False,
                          expired_dispatch[2], expired_dispatch[3]),
                    daemon=True,
                    name="llm-proactive-reply-expired-flush",
                ).start()

            # A mention or supported URL immediately starts a three-minute
            # work period when outside work.  It is always allowed through.
            if state["phase"] != "working" and (mention or message.get("is_url_only")):
                old_phase = state.get("phase")
                recent = list(state.get("recent_messages") or [])
                cycle_messages = list(state.get("cycle_messages") or [])
                cycle_id = state.get("cycle_id")
                state.clear()
                state.update(self._new_work_state(now))
                state["phase_end"] = now + self._work_extend_seconds()
                state["recent_messages"] = recent
                state["cycle_messages"] = cycle_messages
                state["cycle_id"] = cycle_id or state.get("cycle_id")
                self._log_phase_change(group_id, old_phase, state, "prefix_or_mention")

            # In mixed mode prefix is a direct forced reply. The state machine
            # switches/keeps working, but does not enqueue the same message.
            if context.get("prefix_used"):
                # Keep direct prefix requests associated with this cycle so
                # the LLM service can refresh the working-context cache after
                # the direct reply is persisted.
                context["_cycle_id"] = state.get("cycle_id")
                print(f"[LLM PROACTIVE] group={group_id} prefix trigger -> direct forced LLM; batch_count=0", flush=True)
                return None

            if state["phase"] != "working":
                return None

            force_reply = bool(mention or message.get("is_url_only"))
            trigger_source = (
                "mention" if mention
                else "url_only" if message.get("is_url_only")
                else "buffer"
            )
            pending = state.setdefault("pending_batch", [])
            pending.append(message)
            state["pending_batch"] = pending[-self._batch_max_messages():]
            state["pending_context"] = dict(context)
            state["pending_force_reply"] = bool(
                state.get("pending_force_reply") or force_reply
            )
            if force_reply:
                state["pending_trigger_source"] = trigger_source
                state["batch_flush_at"] = now
            elif len(state["pending_batch"]) >= self._batch_max_messages():
                state["batch_flush_at"] = now
            elif not state.get("batch_flush_at"):
                state["pending_trigger_source"] = "buffer"
                state["batch_flush_at"] = now + self._batch_debounce_seconds()

            # This path is used by small direct unit callers without the
            # Dispatcher's background callback. The normal bot path is flushed
            # by _timer_loop so the worker never blocks on an LLM request.
            if self.callback is None and (
                force_reply or len(state["pending_batch"]) >= self._batch_max_messages()
            ):
                dispatch = self._take_pending_batch_locked(state)
                if dispatch:
                    return self._directive(
                        dispatch[1], dispatch[0], dispatch[2], dispatch[3]
                    )
            return None

    def _attention_miss_locked(self, state, now, group_id="unknown"):
        state["checking"] = False
        state["attention_misses"] += 1
        if state["attention_misses"] >= self._attention_no_reply_limit():
            recent = state.get("recent_messages") or []
            cycle_messages = list(state.get("cycle_messages") or [])
            cycle_id = state.get("cycle_id")
            cycle_context = dict(state.get("last_context") or {})
            quiet_started_at = state.get("quiet_started_at")
            quiet_timeout_at = state.get("quiet_timeout_at")
            old_phase = state.get("phase")
            state.clear()
            state.update(self._new_idle_state(now))
            if quiet_started_at is not None and quiet_timeout_at is not None:
                state["quiet_started_at"] = quiet_started_at
                state["quiet_timeout_at"] = quiet_timeout_at
            state["cycle_snapshot"] = cycle_messages
            state["cycle_context"] = cycle_context
            state["cycle_id"] = cycle_id
            state["cycle_end_at"] = now
            # The idle transition is the cycle-end boundary where the
            # unified curation (memory + slang + expressions + style) runs.
            state["cycle_curation_pending"] = self.cycle_end_callback is not None
            self._log_phase_change(group_id, old_phase, state, "attention_miss_limit")
        else:
            state["next_attention_at"] = now + self._random_seconds(self._attention_bounds())

    def on_llm_result(self, context, result, attention_check=False):
        group_id = self._group_key(context)
        now = self.clock()
        with self.lock:
            state = self.states.get(group_id)
            if not state:
                return

            if attention_check:
                if state["phase"] != "attention":
                    return
                if (
                    isinstance(result, dict)
                    and result.get("_llm_ok")
                    and result.get("should_reply")
                    and result.get("messages")
                ):
                    recent = state.get("recent_messages") or []
                    cycle_messages = state.get("cycle_messages") or []
                    cycle_id = state.get("cycle_id")
                    active_style_switch = state.get("active_style_switch")
                    active_style_switch_at = state.get("active_style_switch_at")
                    old_phase = state.get("phase")
                    state.clear()
                    state.update(self._new_work_state(now))
                    state["recent_messages"] = recent
                    state["cycle_messages"] = cycle_messages
                    state["cycle_id"] = cycle_id or state.get("cycle_id")
                    state["last_context"] = dict(context)
                    # The style selection is cycle-scoped: it survives the
                    # attention -> working hop but is dropped at cycle end.
                    if active_style_switch:
                        state["active_style_switch"] = active_style_switch
                        state["active_style_switch_at"] = active_style_switch_at
                    self._log_phase_change(group_id, old_phase, state, "attention_reply")
                else:
                    self._attention_miss_locked(state, now, group_id)
                return

            if (
                state["phase"] == "working"
                and isinstance(result, dict)
                and result.get("_llm_ok")
                and result.get("should_reply")
                and result.get("messages")
                and state["phase_end"] - now < self._work_extend_threshold()
            ):
                state["phase_end"] = now + self._work_extend_seconds()
                print("[LLM PROACTIVE] reply extended work period", flush=True)

    def _timer_loop(self):
        while self.enabled:
            now = self.clock()
            pending = []
            cycle_pending = []
            with self.lock:
                timing_groups = list(self.states)
            # Read SQLite outside the state lock. A slow or locked learner DB
            # must not stop density updates or message ingress.
            self._refresh_reply_timing(timing_groups, now)
            with self.lock:
                for group_id, state in self.states.items():
                    self._update_density_locked(group_id, state, now)
                    expired_dispatch = self._advance_state_locked(state, now, group_id)
                    if expired_dispatch:
                        pending.append((
                            group_id,
                            expired_dispatch[0],
                            expired_dispatch[1],
                            False,
                            expired_dispatch[2],
                            expired_dispatch[3],
                        ))

                    # Do not interrupt an active working batch. Once it
                    # returns to attention/idle, a due timer is dispatched
                    # immediately using the latest known group context.
                    due_at = self.proactive_web_due_at.get(str(group_id))
                    if (
                        state.get("phase") != "working"
                        and not state.get("checking")
                        and due_at is not None
                        and now >= due_at
                        and state.get("last_context")
                    ):
                        dispatch, transitioned = self._start_proactive_work_locked(
                            state, now, group_id
                        )
                        if transitioned and dispatch:
                            pending.append((
                                group_id, dispatch[0], dispatch[1], False,
                                dispatch[2], dispatch[3],
                            ))
                            continue

                    if (
                        state["phase"] == "working"
                        and state.get("pending_batch")
                        and not state.get("batch_flushing")
                        and state.get("batch_flush_at") is not None
                        and now >= state["batch_flush_at"]
                    ):
                        dispatch = self._take_pending_batch_locked(state)
                        if dispatch:
                            pending.append((
                                group_id,
                                dispatch[0],
                                dispatch[1],
                                False,
                                dispatch[2],
                                dispatch[3],
                            ))

                    if (
                        state["phase"] == "idle"
                        and state.get("cycle_curation_pending")
                        and self.cycle_end_callback is not None
                    ):
                        context = dict(state.get("cycle_context") or {})
                        context["_cycle_id"] = state.get("cycle_id")
                        context["_cycle_end_at"] = state.get("cycle_end_at")
                        cycle_pending.append((
                            group_id,
                            context,
                            list(state.get("cycle_snapshot") or []),
                        ))
                        state["cycle_curation_pending"] = False

                    if (
                        state["phase"] == "attention"
                        and not state.get("checking")
                        and state.get("next_attention_at") is not None
                        and now >= state["next_attention_at"]
                    ):
                        batch = list(state.get("recent_messages") or [])[-self._attention_message_limit():]
                        context = dict(state.get("last_context") or {})
                        context["_cycle_id"] = state.get("cycle_id")
                        context["_attention_source"] = state.get("attention_source") or "idle"
                        timing_probability = self._reply_timing_probability_locked(group_id)
                        if self.random() >= timing_probability:
                            print(
                                f"[LLM PROACTIVE] attention gated group={group_id} "
                                f"probability={timing_probability:.3f}",
                                flush=True,
                            )
                            self._attention_miss_locked(state, now, group_id)
                            continue
                        context["_nonsense_opportunity"] = self._attention_nonsense_opportunity()
                        # Tieba forwarding is exclusively controlled by the
                        # independent 2--3 hour timer. Attention may still
                        # generate an ordinary interjection.
                        context["_tieba_opportunity"] = False
                        if batch and self.callback is not None and context:
                            context["_slang_emotional_opportunity"] = self._attention_slang_emotional_opportunity(group_id)
                            state["checking"] = True
                            pending.append((group_id, context, batch, True, False, "attention"))
                        else:
                            self._attention_miss_locked(state, now, group_id)

            for group_id, context, batch, attention_check, force_reply, trigger_source in pending:
                threading.Thread(
                    target=self._run_callback,
                    args=(group_id, context, batch, attention_check, force_reply, trigger_source),
                    daemon=True,
                    name="llm-proactive-reply-flush",
                ).start()
            for group_id, context, messages in cycle_pending:
                threading.Thread(
                    target=self._run_cycle_curation,
                    args=(group_id, context, messages),
                    daemon=True,
                    name="llm-cycle-curation",
                ).start()
            time.sleep(1)

    def _run_callback(self, group_id, context, batch, attention_check,
                      force_reply=False, trigger_source="interval"):
        try:
            self.callback(
                context,
                batch,
                force_reply if not attention_check else False,
                "attention" if attention_check else trigger_source,
                attention_check,
            )
        finally:
            with self.lock:
                state = self.states.get(group_id)
                if state and not attention_check:
                    state["batch_flushing"] = False
                    if state.get("pending_batch") and state.get("phase") == "working":
                        if state.get("pending_force_reply") or len(state["pending_batch"]) >= self._batch_max_messages():
                            state["batch_flush_at"] = self.clock()
                        elif not state.get("batch_flush_at"):
                            state["batch_flush_at"] = self.clock() + self._batch_debounce_seconds()

                if attention_check:
                    if state and state.get("checking"):
                        self._attention_miss_locked(state, self.clock(), group_id)

    def _run_cycle_curation(self, group_id, context, messages):
        try:
            self.cycle_end_callback(group_id, context, messages)
        except Exception as exc:
            print(f"[LLM CYCLE CURATION ERROR] {exc}", flush=True)
