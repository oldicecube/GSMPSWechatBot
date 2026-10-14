"""LLM 内置主动回复状态机。

状态顺序通常为：idle -> attention -> working -> attention -> idle；
动态密度上界可把 idle 直接切入 attention，随机静默超时可把 idle 直接切入 working。
工作期先进入短暂缓冲窗口，再把窗口内消息集中交给 LLM；关注期按进入来源使用不同的上下文预算，
连续三次没有值得回复的内容才回到空闲期。
"""

from __future__ import annotations

import random
import math
import queue
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


from llm.density_gate import DensityGate, enabled as density_gate_enabled

class ProactiveReplyManager:
    def __init__(self, config):
        self.config = config if isinstance(config, dict) else {}
        self.settings = self.config.get("auto_reply") or {}
        self.prefix_mode = str(self.config.get("prefix_mode", "strict") or "strict").strip().lower()
        self.target_groups = normalize_target_groups(self.config.get("target_groups"))
        self.allowed_groups = normalize_target_groups(self.settings.get("allowed_groups"))
        self.enabled = self._setting_bool("enabled", self.prefix_mode == "mixed")
        if self.prefix_mode == "strict":
            self.enabled = False
        self.states = {}
        self.density_gate = DensityGate(self.settings)
        # Event-level guard for duplicate SSE/queue deliveries. Keys are
        # message IDs only; identical text without an ID remains valid.
        self.seen_message_ids = {}
        self.last_llm_reply_at = {}
        # Legacy optional reply-penalty state.  It is disabled by default; the
        # ordinary model-led path must not use a local cooldown or quota to
        # decide whether the Bot should speak.
        self.llm_reply_events = {}
        self.llm_reply_reservations = {}
        self.llm_reply_penalty_until = {}
        # Per-group counter of consecutive attention pulls that did NOT
        # trigger a slang emotional reply. Each pull rolls n*10% (n =
        # misses + 1) and the counter resets to 0 on trigger.
        self.slang_emotional_misses = {}
        self.tieba_repost_last_at = {}
        # Message-density events are control data, not chat history.
        self.density_events = {}
        # Idle content is no longer driven by a global 2--3 hour timer. It is
        # offered only during a normal attention window after the group has
        # earned one from its own cycle rhythm and feedback profile.
        self.activity_profile_reader = None
        self.activity_profile_cache = {}
        self.activity_profile_cache_ttl = 60
        # The learner is injected after construction so the state machine can
        # schedule attention checks locally without adding another LLM request.
        self.reply_timing_reader = None
        self.reply_timing_cache = {}
        self.reply_timing_cache_ttl = 60
        # Context-conditioned initiative evidence is cached locally and only
        # softly modulates ordinary attention checks.
        self.behavior_prior_reader = None
        self.behavior_prior_cache = {}
        self.behavior_prior_cache_ttl = 60
        # A lease is context for the model, not a local permission or turn quota.
        self.conversation_leases = {}
        self.lock = threading.Lock()
        self.callback = None
        self.cycle_end_callback = None
        self.autonomy_callback = None
        self.autonomy_settings = self.config.get("autonomy") or {}
        self.autonomy_next_check_at = {}
        self.autonomy_last_seen_message = {}
        # maibot-inspired consecutive-idle backoff: each autonomy cycle that
        # finds no new message and executes no action doubles the next check
        # interval; fresh traffic or an executed action resets it.
        self.autonomy_idle_streaks = {}
        self.autonomy_last_checked_identity = {}
        try:
            self.autonomy_check_interval = max(
                10.0,
                float(self.autonomy_settings.get("check_interval_seconds", 60) or 60),
            )
        except (TypeError, ValueError):
            self.autonomy_check_interval = 60.0
        self.learning_settings = self.config.get("learning") or {}
        self.memory_settings = self.config.get("memory") or {}
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
        self._task_stop = threading.Event()
        self._task_queue = queue.Queue(maxsize=64)
        self._timer_thread = None
        self._task_threads = []
        if self.enabled:
            self._timer_thread = threading.Thread(
                target=self._timer_loop,
                daemon=True,
                name="llm-proactive-reply-timer",
            )
            self._timer_thread.start()
            for index in range(2):
                worker = threading.Thread(
                    target=self._task_worker,
                    daemon=True,
                    name=f"llm-proactive-task-{index}",
                )
                worker.start()
                self._task_threads.append(worker)

    def _submit_task(self, target, args=()):
        if self._task_stop.is_set():
            return False
        try:
            self._task_queue.put_nowait((target, tuple(args)))
            return True
        except queue.Full:
            return False

    def _task_worker(self):
        while not self._task_stop.is_set():
            try:
                target, args = self._task_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                target(*args)
            except Exception as exc:
                print(
                    f"[LLM PROACTIVE TASK ERROR] {getattr(target, '__name__', 'task')}: {exc}",
                    flush=True,
                )
            finally:
                self._task_queue.task_done()

    def shutdown(self):
        """Stop timers and cancel queued callbacks during process shutdown."""
        self.enabled = False
        self._task_stop.set()
        if self._timer_thread and self._timer_thread.is_alive():
            self._timer_thread.join(timeout=2.0)
        for worker in self._task_threads:
            if worker.is_alive():
                worker.join(timeout=2.0)

    def set_batch_callback(self, callback):
        self.callback = callback if callable(callback) else None

    def set_cycle_end_callback(self, callback):
        self.cycle_end_callback = callback if callable(callback) else None

    def set_autonomy_callback(self, callback):
        # Retained as a compatibility no-op. Periodic autonomy is disabled;
        # proactive replies are released only by message-density state.
        self.autonomy_callback = None

    def set_reply_timing_reader(self, callback):
        """Set a read-only learner callback used by the local attention gate."""
        self.reply_timing_reader = callback if callable(callback) else None

    def set_activity_profile_reader(self, callback):
        """Set a read-only source for per-group idle-content rhythm profiles."""
        self.activity_profile_reader = callback if callable(callback) else None

    def set_behavior_prior_reader(self, callback):
        """Set a read-only source for context-conditioned initiative evidence."""
        self.behavior_prior_reader = callback if callable(callback) else None

    def _refresh_behavior_priors(self, group_ids, now):
        reader = self.behavior_prior_reader
        if not reader:
            return
        refreshed = {}
        for group_id in group_ids:
            key = str(group_id)
            cached = self.behavior_prior_cache.get(key)
            if cached and now - float(cached.get("at") or 0) < self.behavior_prior_cache_ttl:
                continue
            try:
                prior = reader(key)
            except Exception as exc:
                print(f"[LLM PROACTIVE] behavior prior read failed group={key}: {exc}", flush=True)
                prior = {}
            refreshed[key] = {
                "at": now,
                "prior": prior if isinstance(prior, dict) else {},
            }
        if refreshed:
            with self.lock:
                self.behavior_prior_cache.update(refreshed)

    def _behavior_prior_adjustment_locked(self, group_id):
        """Return a bounded (bias, cap, sample_count) for ordinary attention.

        Explicit mentions, prefixes, direct replies and forced replies never
        call this path. With too little evidence the values are neutral.
        """
        cached = self.behavior_prior_cache.get(str(group_id)) or {}
        prior = cached.get("prior") if isinstance(cached.get("prior"), dict) else {}
        try:
            samples = max(0, int(prior.get("sample_count") or 0))
        except (TypeError, ValueError):
            samples = 0
        try:
            minimum = max(1, int(self.settings.get("behavior_prior_min_samples", 30)))
        except (TypeError, ValueError):
            minimum = 30
        if samples < minimum:
            return 0.0, 1.0, samples
        try:
            bias = float(prior.get("proactive_bias") or 0.0)
        except (TypeError, ValueError):
            bias = 0.0
        try:
            cap = float(prior.get("proactive_cap") or 1.0)
        except (TypeError, ValueError):
            cap = 1.0
        bias = max(-0.15, min(0.15, bias))
        cap = max(0.45, min(1.0, cap))
        return bias, cap, samples

    def _refresh_activity_profiles(self, group_ids, now):
        reader = self.activity_profile_reader
        if not reader:
            return
        refreshed = {}
        for group_id in group_ids:
            key = str(group_id)
            cached = self.activity_profile_cache.get(key)
            if cached and now - float(cached.get("at") or 0) < self.activity_profile_cache_ttl:
                continue
            try:
                profile = reader(key)
            except Exception as exc:
                print(f"[LLM PROACTIVE] activity profile read failed group={key}: {exc}", flush=True)
                profile = {}
            refreshed[key] = {"at": now, "profile": profile if isinstance(profile, dict) else {}}
        if refreshed:
            with self.lock:
                self.activity_profile_cache.update(refreshed)

    @staticmethod
    def _profile_float(profile, key, default=0.0):
        try:
            return float((profile or {}).get(key, default) or default)
        except (TypeError, ValueError):
            return float(default)

    def _adaptive_idle_content_opportunity_locked(self, group_id, state, now):
        """Choose an idle-content opportunity from observed group rhythm.

        This is deliberately only a gate. The model still has to decide that
        the conversation is genuinely idle and that fetched content is worth
        sharing; otherwise it must remain silent.
        """
        if not bool(self.settings.get("adaptive_idle_content_enabled", False)):
            return False, 0.0, "disabled"
        profile = (self.activity_profile_cache.get(str(group_id)) or {}).get("profile") or {}
        cycles = int(profile.get("cycles", 0) or 0)
        members = self._profile_float(profile, "ema_effective_members")
        volume = self._profile_float(profile, "ema_messages_per_member")
        p75_gap = max(15.0, self._profile_float(profile, "ema_p75_gap_seconds", 45.0))
        two_person = self._profile_float(profile, "ema_two_person_share", 1.0)
        last_sent = self._profile_float(profile, "last_idle_content_at")
        attempts = int(profile.get("idle_content_attempts", 0) or 0)
        accepted = int(profile.get("idle_content_accepted", 0) or 0)
        if cycles < 3 or members < 1.6:
            return False, 0.0, "insufficient_group_rhythm"
        cooldown = max(20 * 60.0, min(4 * 3600.0, p75_gap * max(18.0, volume * 5.0)))
        if last_sent and now - last_sent < cooldown:
            return False, 0.0, "recent_idle_content"
        quiet_started = self._profile_float(state, "quiet_started_at", now)
        silence = max(0.0, now - quiet_started)
        silence_fit = max(0.0, min(1.0, silence / max(45.0, p75_gap * 2.0)))
        member_fit = max(0.0, min(1.0, (members - 1.4) / 3.5))
        conversation_fit = max(0.0, min(1.0, volume / 7.0))
        acceptance = (accepted + 1.0) / max(2.0, attempts + 2.0)
        bot_accepted = int(profile.get("bot_reply_accepted", 0) or 0)
        bot_interrupted = int(profile.get("bot_reply_interrupted", 0) or 0)
        bot_ignored = int(profile.get("bot_reply_ignored", 0) or 0)
        bot_total = bot_accepted + bot_interrupted + bot_ignored
        if bot_total >= 4:
            bot_success = bot_accepted / bot_total
            bot_interrupt = bot_interrupted / bot_total
            reply_fit = max(0.30, min(1.0, 0.55 + 0.55 * bot_success - 0.45 * bot_interrupt))
        else:
            reply_fit = 1.0
        two_person_penalty = max(0.15, 1.0 - max(0.0, two_person - 0.78) * 2.8)
        probability = min(0.22, 0.015 + 0.18 * member_fit * conversation_fit * silence_fit * acceptance * reply_fit * two_person_penalty)
        return self.random() < probability, round(probability, 4), "adaptive_group_idle_window"

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
            self.settings.get("idle_min_seconds", 10 * 60),
        )
        maximum = self._int_setting(
            "idle_quiet_max_seconds",
            self.settings.get("idle_max_seconds", 30 * 60),
        )
        return minimum, max(minimum, maximum)

    def _setting_bool(self, name, default=False):
        """Read a boolean setting without treating the string ``"false"`` as true."""
        value = self.settings.get(name, default)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return bool(value)

    def _autonomy_setting_bool(self, name, default=False):
        """Read a boolean from the autonomy config block without string traps."""
        if not isinstance(self.autonomy_settings, dict):
            return bool(default)
        value = self.autonomy_settings.get(name, default)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return bool(value)



    def _autonomy_float_setting(self, name, default, minimum=0.0, maximum=None):
        """Read a float from the autonomy config block without string traps."""
        if isinstance(self.autonomy_settings, dict):
            try:
                value = float(self.autonomy_settings.get(name, default))
            except (TypeError, ValueError):
                value = float(default)
        else:
            value = float(default)
        value = max(float(minimum), value)
        if maximum is not None:
            value = min(float(maximum), value)
        return value

    def _autonomy_int_setting(self, name, default, minimum=1):
        """Read an int from the autonomy config block without string traps."""
        value = int(self._autonomy_float_setting(name, float(default), minimum=float(minimum)))
        return max(int(minimum), value)
    def _autonomy_has_consumed(self, group_id, message):
        """True when autonomy already produced a real reply from this context.

        Suppresses ordinary proactive replies built from the same event so one
        autonomy cycle cannot be answered twice.  Skipped when the operator
        explicitly allows follow-up after an autonomy action.
        """
        # Periodic autonomy is disabled, so no message can be consumed by it.
        return False

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

    # Deprecated configuration compatibility. The old independent web timer is
    # intentionally not scheduled anymore; adaptive idle opportunities use the
    # group activity profile above.
    def _proactive_web_bounds(self):
        return 0, 0

    def _proactive_web_reset_after_messages(self):
        return 0

    def _reset_proactive_web_timer_locked(self, group_id, now, reason):
        return None

    def _ensure_proactive_web_timer_locked(self, group_id, now):
        return None

    def record_sent_bot_message(self, group_id, count=1):
        """Compatibility hook kept for dispatcher send accounting."""
        return None

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

    def _cycle_curation_interval_seconds(self):
        """Use the earliest configured curator interval for unified curation."""
        values = []
        for settings, key in (
            (self.memory_settings, "curator_interval_seconds"),
            (self.learning_settings, "slang_curator_interval_seconds"),
        ):
            try:
                value = int(settings.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                values.append(value)
        return max(1, min(values) if values else 60)

    def _cycle_curation_due_locked(self, state, now):
        last = float(state.get("cycle_curation_last_at") or 0)
        return not last or now - last >= self._cycle_curation_interval_seconds()

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

    def _reply_penalty_float(self, name, default, minimum=0.0):
        try:
            value = float(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = float(default)
        return max(float(minimum), value)

    def _reply_penalty_int(self, name, default, minimum=1):
        try:
            value = int(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = int(default)
        return max(int(minimum), value)

    def _reply_penalty_direct_bypass(self):
        value = self.settings.get("reply_penalty_direct_bypass", True)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    def _reply_frequency_mode(self):
        value = str(self.settings.get("reply_frequency_gate_mode", "adaptive") or "adaptive").strip().lower()
        return value if value in {"adaptive", "strict", "off"} else "adaptive"

    @staticmethod
    def _batch_has_substance(batch_messages):
        """Classify only obvious low-signal bursts locally.

        This is intentionally a soft backstop, not the reply policy: questions,
        requests, longer messages and messages with a concrete topic still go
        to the LLM, which decides whether speaking is appropriate.
        """
        texts = [
            str(item.get("content") or "").strip()
            for item in (batch_messages or [])
            if isinstance(item, dict)
        ]
        text = " ".join(item for item in texts if item)
        if not text:
            return False
        if len(text) >= 18:
            return True
        if any(marker in text for marker in ("\x3f", "\uff1f", "\u600e\u4e48", "\u5982\u4f55", "\u80fd\u5426", "\u8bf7", "\u5e2e", "\u4e3a\u4ec0\u4e48", "where", "what", "how")):
            return True
        # Repeated punctuation/emoji-only acknowledgements are low-signal.
        meaningful = sum(ch.isalnum() or ("\u3400" <= ch <= "\u9fff") for ch in text)
        return meaningful >= 10

    def _adaptive_low_signal_blocked(self, now, events, reservations, batch_messages, context):
        if self._batch_has_substance(batch_messages):
            return False, "substantive"
        context = context if isinstance(context, dict) else {}
        try:
            soft_cooldown = max(0.0, float(self.settings.get("reply_soft_cooldown_seconds", 18) or 18))
        except (TypeError, ValueError):
            soft_cooldown = 18.0
        last_reply = float(self.last_llm_reply_at.get(str(context.get("group") or "")) or 0.0)
        # The latest event is also considered when the caller has not supplied
        # a group in context (the normal dispatcher path does supply it).
        last_activity = max(events + reservations, default=0.0)
        last_bot = max(last_reply, last_activity if events else 0.0)
        if last_bot and now - last_bot < soft_cooldown:
            return True, "low_signal_recent_bot_reply"
        try:
            low_signal_burst = max(2, int(self.settings.get("reply_low_signal_burst_limit", 2) or 2))
        except (TypeError, ValueError):
            low_signal_burst = 2
        if len(events) + len(reservations) >= low_signal_burst:
            return True, "low_signal_burst"
        return False, "low_signal_allowed"

    def try_reserve_llm_reply(
        self, group_id, *, force_reply=False, attention_check=False, priority="MAY_REPLY",
        context=None, batch_messages=None
    ):
        """Reserve an autonomous LLM reply slot before provider work starts.

        The reservation makes the check atomic across proactive worker threads.
        Explicit prefix/@/direct requests keep their existing priority by
        bypassing this gate; ordinary MAY_REPLY and attention replies do not.
        A failed reservation must be released by ``cancel_llm_reply_reservation``.
        In adaptive mode only obvious low-signal bursts are locally paused;
        substantive batches continue to the LLM for a context-aware decision.
        """
        group_key = str(group_id or "unknown")
        priority = str(priority or "MAY_REPLY").upper()
        direct = bool(force_reply or priority in {"MUST_REPLY", "SHOULD_REPLY"})
        if direct and self._reply_penalty_direct_bypass():
            return True

        now = self.clock()
        cooldown = self._reply_penalty_float("reply_cooldown_seconds", 30.0, 0.0)
        window = self._reply_penalty_float("reply_burst_window_seconds", 90.0, 1.0)
        burst_limit = self._reply_penalty_int("reply_burst_limit", 2, 1)
        penalty_seconds = self._reply_penalty_float("reply_penalty_seconds", 90.0, 0.0)
        with self.lock:
            events = [
                float(item) for item in (self.llm_reply_events.get(group_key) or [])
                if now - float(item) <= max(window, cooldown, penalty_seconds)
            ]
            reservations = [
                float(item) for item in (self.llm_reply_reservations.get(group_key) or [])
                if now - float(item) <= max(window, cooldown)
            ]
            self.llm_reply_events[group_key] = events
            self.llm_reply_reservations[group_key] = reservations

            penalty_until = float(self.llm_reply_penalty_until.get(group_key) or 0)
            if penalty_until > now:
                print(
                    f"[LLM REPLY PENALTY] suppress group={group_key} "
                    f"reason=penalty remaining={penalty_until - now:.1f}s",
                    flush=True,
                )
                return False

            recent_events = [item for item in events if now - item <= window]
            recent_reservations = [item for item in reservations if now - item <= window]
            last_activity = max(recent_events + recent_reservations, default=0.0)
            if self._reply_frequency_mode() == "adaptive" and batch_messages is not None:
                blocked, reason = self._adaptive_low_signal_blocked(
                    now, recent_events, recent_reservations, batch_messages, context
                )
                if blocked:
                    print(
                        f"[LLM REPLY PENALTY] suppress group={group_key} reason={reason}",
                        flush=True,
                    )
                    return False
                # A substantive batch is deliberately allowed through even if
                # the Bot spoke recently; the LLM remains the decision-maker.
                self.llm_reply_reservations.setdefault(group_key, []).append(now)
                return True

            if last_activity and now - last_activity < cooldown:
                print(
                    f"[LLM REPLY PENALTY] suppress group={group_key} "
                    f"reason=cooldown remaining={cooldown - (now - last_activity):.1f}s",
                    flush=True,
                )
                return False

            if len(recent_events) + len(recent_reservations) >= burst_limit:
                self.llm_reply_penalty_until[group_key] = now + penalty_seconds
                print(
                    f"[LLM REPLY PENALTY] suppress group={group_key} "
                    f"reason=burst count={len(recent_events) + len(recent_reservations)} "
                    f"penalty={penalty_seconds:.0f}s",
                    flush=True,
                )
                return False

            self.llm_reply_reservations.setdefault(group_key, []).append(now)
            return True

    def cancel_llm_reply_reservation(self, group_id):
        """Release a slot when provider work produced no reply or failed."""
        group_key = str(group_id or "unknown")
        with self.lock:
            reservations = self.llm_reply_reservations.get(group_key) or []
            if reservations:
                reservations.pop()
            if reservations:
                self.llm_reply_reservations[group_key] = reservations
            else:
                self.llm_reply_reservations.pop(group_key, None)

    def record_llm_reply(self, group_id):
        group_key = str(group_id or "unknown")
        now = self.clock()
        with self.lock:
            self.last_llm_reply_at[group_key] = now
            reservations = self.llm_reply_reservations.get(group_key) or []
            if reservations:
                reservations.pop()
            if reservations:
                self.llm_reply_reservations[group_key] = reservations
            else:
                self.llm_reply_reservations.pop(group_key, None)
            self.llm_reply_events.setdefault(group_key, []).append(now)

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
            "pending_conversation_priority": "MAY_REPLY",
            "batch_flush_at": None,
            "batch_flushing": False,
            "cycle_id": None,
            "cycle_messages": [],
            "cycle_snapshot": [],
            "cycle_context": None,
            "cycle_curation_pending": False,
            "cycle_curation_inflight": False,
            "cycle_curation_retry_at": None,
            "cycle_curation_attempts": 0,
            "cycle_curation_last_at": None,
            "active_style_switch": None,
            "active_style_switch_at": None,
        }

    def _new_work_state(self, now):
        return {
            "phase": "working",
            "phase_end": now + self._random_seconds(self._work_bounds()),
            # Work-period messages use the configured debounce buffer; the
            # LLM makes the ordinary reply decision when that buffer is flushed.
            "recent_messages": [],
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "next_attention_at": None,
            "pending_batch": [],
            "pending_context": None,
            "pending_force_reply": False,
            "pending_trigger_source": "buffer",
            "pending_conversation_priority": "MAY_REPLY",
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
            "pending_conversation_priority": "MAY_REPLY",
            "batch_flush_at": None,
            "batch_flushing": False,
            "cycle_id": cycle_id or f"cycle-{int(now)}-{self.randint(1000, 9999)}",
            "cycle_messages": list(cycle_messages or recent_messages or []),
            "attention_source": str(attention_source or "idle"),
            "cycle_snapshot": [],
            "cycle_context": None,
            "cycle_curation_pending": False,
            "cycle_curation_inflight": False,
            "cycle_curation_retry_at": None,
            "cycle_curation_attempts": 0,
            "cycle_curation_last_at": None,
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

    def _advance_state_locked(self, state, now, group_id="unknown"):
        pending_dispatch = None
        if density_gate_enabled(self.settings):
            phase = state.get("phase")
            burst = self.density_gate.groups.get(str(group_id), {})
            if phase == "working" and now >= float(burst.get("window_end") or state.get("phase_end") or now):
                state["phase"] = "idle"
                state["phase_end"] = None
            return None
        if state["phase"] == "idle":
            density_rate = float(state.get("density_rate") or 0.0)
            density_upper = float(
                state.get("density_upper") or 8.0
            )

            # The upper boundary is a hard attention trigger.  This keeps a
            # genuinely active group from waiting for the stochastic check.
            if density_rate > density_upper:
                old_phase = state.get("phase")
                cycle_curation_last_at = state.get("cycle_curation_last_at")
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
                state["cycle_curation_last_at"] = cycle_curation_last_at
                self._log_phase_change(group_id, old_phase, state, "density_upper")
                return pending_dispatch

            # Idle timers remain useful for attention/curation bookkeeping. They
            # do not decide whether an ordinary message may be sent to the LLM.
            if state.get("quiet_started_at") is None:
                state["quiet_started_at"] = now
                state["quiet_timeout_at"] = now + self._random_seconds(self._idle_bounds())

            next_check = state.get("next_density_attention_at")
            if next_check is None or now >= next_check:
                state["next_density_attention_at"] = now + self._density_attention_interval_seconds()
                probability = self._attention_probability(density_rate, density_upper)
                if self.random() < probability:
                    old_phase = state.get("phase")
                    cycle_curation_last_at = state.get("cycle_curation_last_at")
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
                    state["cycle_curation_last_at"] = cycle_curation_last_at
                    self._log_phase_change(
                        group_id,
                        old_phase,
                        state,
                        f"density_probability={probability:.4f}",
                    )
        elif state["phase"] == "attention":
            pass
        elif state["phase"] == "working" and now >= state["phase_end"]:
            pending_dispatch = self._take_pending_batch_locked(state, group_id)
            recent = state.get("recent_messages") or []
            cycle_messages = state.get("cycle_messages") or []
            cycle_id = state.get("cycle_id")
            old_phase = state.get("phase")
            cycle_curation_last_at = state.get("cycle_curation_last_at")
            state.update(self._new_attention_state(
                now,
                recent,
                cycle_id,
                cycle_messages,
                attention_source="working",
            ))
            state["cycle_curation_last_at"] = cycle_curation_last_at
            self._log_phase_change(group_id, old_phase, state, "work_timeout_to_attention")
        return pending_dispatch

    def _allowed_group(self, context):
        groups = self.allowed_groups or self.target_groups
        return is_allowed_group(context, groups)

    def _is_mention(self, context):
        # Router-provided direction is stronger than a generic is_at flag.
        if context.get("directed_to_bot") or context.get("reply_to_bot"):
            return True
        if context.get("prefix_used") or context.get("is_at_bot"):
            return True

        raw = context.get("raw") or {}
        if isinstance(raw, dict):
            if raw.get("directed_to_bot") or raw.get("reply_to_bot"):
                return True
            if raw.get("prefix_used") or raw.get("is_at_bot") or raw.get("isAtBot") or raw.get("atBot"):
                return True
            source = raw.get("_raw") or {}
            if isinstance(source, dict) and (
                source.get("is_at_bot") or source.get("isAtBot") or source.get("atBot")
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

    @staticmethod
    def _sender_key(context):
        raw = context.get("raw") or {}
        if not isinstance(raw, dict):
            raw = {}
        value = (context.get("wxid") or raw.get("wxid") or raw.get("sender_wxid")
                 or context.get("user") or raw.get("user") or raw.get("sender_nickname"))
        return str(value or "").strip().casefold()

    @staticmethod
    def _truthy_context_flag(context, *names):
        raw = context.get("raw") or {}
        source = raw.get("_raw") if isinstance(raw, dict) else {}
        for item in (context, raw, source):
            if not isinstance(item, dict):
                continue
            for name in names:
                value = item.get(name)
                if value is True or (isinstance(value, (int, float)) and value == 1):
                    return True
                if isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1", "bot"}:
                    return True
        return False

    def _is_reply_to_bot(self, context):
        return self._truthy_context_flag(
            context, "is_reply_to_bot", "reply_to_bot", "quoted_bot",
            "is_quote_reply_to_bot", "replyToBot", "atBot", "isAtBot",
        )

    def _active_lease_locked(self, group_id, context, now):
        key = str(group_id)
        lease = self.conversation_leases.get(key)
        if not isinstance(lease, dict):
            return False
        if float(lease.get("expires_at") or 0) <= now:
            self.conversation_leases.pop(key, None)
            return False
        sender = self._sender_key(context)
        if not sender or sender != str(lease.get("sender_key") or "").casefold():
            return False
        try:
            max_turns = max(0, int(self.settings.get("conversation_lease_max_turns", 2) or 0))
        except (TypeError, ValueError):
            max_turns = 2
        if max_turns and int(lease.get("turn_count") or 0) >= max_turns:
            self.conversation_leases.pop(key, None)
            return False
        return True

    def _conversation_priority_locked(self, group_id, context, now, mention=False):
        """Classify only high-confidence direction as an immediate reply.

        A group member asking a question is not automatically asking the Bot.
        Ambiguous turns remain MAY_REPLY so the full-context policy can choose
        silence instead of letting routing heuristics turn them into a reply.
        """
        directed_to_bot = bool(
            context.get("directed_to_bot")
            or context.get("reply_to_bot")
            or self._is_reply_to_bot(context)
        )
        directed_to_other = bool(
            context.get("directed_to_other") or context.get("reply_to_other")
        )
        if directed_to_other and not directed_to_bot and not context.get("prefix_used") and not mention:
            return "DO_NOT_REPLY"
        if context.get("prefix_used") or mention or directed_to_bot:
            return "MUST_REPLY"

        # An active lease is only context indicating that a conversation may
        # still be addressed to the Bot. It is bounded by the configured lease
        # turn cap so a stale two-party exchange cannot keep the Bot replying
        # indefinitely.
        if self._active_lease_locked(group_id, context, now):
            return "SHOULD_REPLY"
        return "MAY_REPLY"

    @staticmethod
    def _priority_rank(value):
        return {"DO_NOT_REPLY": 0, "MAY_REPLY": 1, "UNCERTAIN_DIRECT": 2,
                "SHOULD_REPLY": 3, "MUST_REPLY": 4}.get(str(value or "MAY_REPLY"), 1)

    def record_conversation_reply(self, context, result):
        """Open a short lease after a successful user-facing reply."""
        if not isinstance(result, dict) or not result.get("should_reply") or not result.get("messages"):
            return
        context = context if isinstance(context, dict) else {}
        group_id = self._group_key(context)
        sender = self._sender_key(context)
        if not sender:
            batch = context.get("batch_messages") or []
            if batch and isinstance(batch[-1], dict):
                sender = str(batch[-1].get("sender_wxid") or batch[-1].get("sender_nickname") or "").strip().casefold()
        if not sender:
            return
        now = self.clock()
        priority = str(context.get("_conversation_priority") or context.get("conversation_priority") or "MAY_REPLY")
        direct = priority in {"MUST_REPLY", "SHOULD_REPLY"}
        seconds = self._float_setting("conversation_lease_direct_seconds", 180.0, minimum=15.0) if direct else self._float_setting("conversation_lease_seconds", 90.0, minimum=15.0)
        with self.lock:
            previous = self.conversation_leases.get(group_id) or {}
            self.conversation_leases[group_id] = {
                "sender_key": sender, "last_bot_message_at": now,
                "expires_at": now + seconds,
                "turn_count": int(previous.get("turn_count", 0) or 0) + 1,
            }

    def _serialize_message(self, context):
        raw = context.get("raw") or {}
        source = raw.get("_raw") if isinstance(raw, dict) else {}
        if not isinstance(source, dict):
            source = {}
        quoted = context.get("quoted_message")
        if not isinstance(quoted, dict):
            quoted = raw.get("quoted_message") if isinstance(raw, dict) else None
        if not isinstance(quoted, dict):
            quoted = source.get("quoted_message") if isinstance(source, dict) else None
        if not isinstance(quoted, dict):
            quoted = {}

        def quote_value(nested_names, flat_names):
            # Only the nested quoted object may use generic field names.
            # Top-level fields must be explicitly quote-scoped so current
            # message content/identity can never leak into the quote record.
            if isinstance(quoted, dict):
                for name in nested_names:
                    value = quoted.get(name)
                    if value not in (None, "", [], {}):
                        return value
            for mapping in (context, raw, source):
                if not isinstance(mapping, dict):
                    continue
                for name in flat_names:
                    value = mapping.get(name)
                    if value not in (None, "", [], {}):
                        return value
            return ""

        quoted_content = quote_value(
            ("content", "text", "body"),
            ("quoted_content", "replyContent", "reply_content", "quoteText", "quote_text"),
        )
        quoted_role_raw = quote_value(
            ("role", "message_role", "messageRole"),
            ("quoted_role", "replyRole", "reply_role"),
        )
        quoted_is_bot_raw = quote_value(
            ("is_bot", "isBot", "fromBot", "from_bot", "bot"),
            ("quoted_is_bot", "replyIsBot", "reply_is_bot"),
        )
        quoted_is_bot = quoted_is_bot_raw is True or str(quoted_is_bot_raw).strip().lower() in {"1", "true", "yes", "on"}
        if quoted_role_raw not in ("assistant", "user"):
            quoted_role_raw = "assistant" if quoted_is_bot else "user"
        quoted_message = {
            "message_id": str(quote_value(
                ("message_id", "messageId", "messageKey", "local_id", "localId", "server_id", "serverId", "svrid"),
                ("quoted_message_id", "replyToMessageId", "reply_to_message_id", "quotedMessageId", "quoted_message_id", "replyMessageId", "reply_message_id"),
            ) or ""),
            "sender": str(quote_value(
                ("sender", "senderName", "sender_nickname", "nickname", "user"),
                ("quoted_sender", "replyToUser", "reply_to_user", "quotedUser", "quoted_user", "replySender"),
            ) or ""),
            "sender_wxid": str(quote_value(
                ("sender_wxid", "senderWxid", "wxid"),
                ("quoted_sender_wxid", "replyToWxid", "reply_to_wxid", "quotedWxid", "quoted_wxid", "replySenderWxid"),
            ) or ""),
            "content": str(quoted_content or ""),
            "message_type": str(quote_value(
                ("message_type", "messageType", "type"),
                ("quoted_message_type", "replyMessageType", "reply_message_type", "quotedMessageType"),
            ) or ""),
            "timestamp": quote_value(
                ("timestamp", "time", "createdAt", "createTime"),
                ("quoted_timestamp", "replyTimestamp", "reply_timestamp", "quotedTimestamp"),
            ),
            "role": str(quoted_role_raw),
            "is_bot": quoted_is_bot,
            "available": bool(str(quoted_content or "").strip()),
        }

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
            "directed_to_bot": bool(
                context.get("directed_to_bot") or context.get("reply_to_bot")
                or self._is_reply_to_bot(context)
            ),
            "directed_to_other": bool(
                context.get("directed_to_other") or context.get("reply_to_other")
            ),
            "reply_to_bot": bool(context.get("reply_to_bot")),
            "reply_to_other": bool(context.get("reply_to_other")),
            "reply_target": str(context.get("reply_target") or "")[:120],
            "address_confidence": max(
                0.0, min(1.0, float(context.get("address_confidence") or 0.0))
            ) if str(context.get("address_confidence") or "").strip() else 0.0,
            "prefix_used": bool(context.get("prefix_used")),
            "is_command": str(context.get("content") or "").strip().startswith("/"),
            "message_type": raw.get("type") or source.get("messageType") or "text",
            "quoted_message": quoted_message,
            "quoted_message_id": quoted_message["message_id"],
            "quoted_sender": quoted_message["sender"],
            "quoted_sender_wxid": quoted_message["sender_wxid"],
            "quoted_content": quoted_message["content"],
            "quoted_message_type": quoted_message["message_type"],
            "quoted_timestamp": quoted_message["timestamp"],
            "quoted_available": quoted_message["available"],
            "quoted_role": quoted_message["role"],
            "quoted_is_bot": quoted_message["is_bot"],
            "conversation_priority": context.get("_conversation_priority") or context.get("conversation_priority") or "MAY_REPLY",
        }

    def _remember_locked(self, state, context, message):
        state["last_context"] = dict(context)
        recent = state.setdefault("recent_messages", [])
        recent.append(message)
        state["recent_messages"] = recent[-self._attention_message_limit():]
        cycle_messages = state.setdefault("cycle_messages", [])
        cycle_messages.append(message)

    def _take_pending_batch_locked(self, state, group_id="unknown"):
        batch = list(state.get("pending_batch") or [])
        if not batch or state.get("batch_flushing"):
            return None
        if density_gate_enabled(self.settings) and not state.get("pending_force_reply") and state.get("pending_conversation_priority") not in {"MUST_REPLY", "SHOULD_REPLY"}:
            now = self.clock()
            ttl = self.density_gate.number("density_low_accumulate_seconds")
            def fresh(item):
                try:
                    timestamp = float(item.get("timestamp") or now)
                    return math.isfinite(timestamp) and -60 <= now-timestamp <= ttl
                except (TypeError, ValueError):return False
            batch = [item for item in batch if fresh(item)]
            state["pending_batch"] = batch
            decision = self.density_gate.admit(group_id, now, float(state.get("density_rate") or 0), batch)
            state["density_stage"] = decision["stage"]
            if not decision["admit"]:
                state["batch_flush_at"] = decision["next_at"] if batch else None
                if state.get("phase") != "working":
                    state["phase"] = "attention" if decision["stage"] != "low" else "idle"
                return None
            state["phase"] = "working"
            state["phase_end"] = decision["window_end"]
            if not state.get("cycle_id"):
                state["cycle_id"] = f"cycle-{int(now)}-{self.randint(1000, 9999)}"
            context = state.get("pending_context") or {}
            context["_density_admitted"] = True
            context["_density_stage"] = decision["stage"]
            state["pending_context"] = context

        if not self._autonomy_setting_bool("allow_followup_after_autonomy", False):
            kept = [item for item in batch if not self._autonomy_has_consumed(group_id, item)]
            if len(kept) != len(batch):
                dropped = len(batch) - len(kept)
                print(
                    f"[LLM PROACTIVE] batch filtered_by_autonomy_consumed group={group_id} dropped={dropped}",
                    flush=True,
                )
                batch = kept
                state["pending_batch"] = batch
                if not batch:
                    state["batch_flush_at"] = None
                    return None

        context = dict(state.get("pending_context") or state.get("last_context") or {})
        context["_cycle_id"] = state.get("cycle_id")
        allow_memory_update = bool(
            state.get("phase") == "working" and not state.get("memory_update_used")
        )
        if allow_memory_update:
            state["memory_update_used"] = True
        context["_memory_update_allowed"] = allow_memory_update
        context["_conversation_priority"] = str(state.get("pending_conversation_priority") or "MAY_REPLY")
        force_reply = bool(state.get("pending_force_reply"))
        trigger_source = str(state.get("pending_trigger_source") or "buffer")
        state["pending_batch"] = []
        state["pending_context"] = None
        state["pending_force_reply"] = False
        state["pending_trigger_source"] = "buffer"
        state["pending_conversation_priority"] = "MAY_REPLY"
        state["batch_flush_at"] = None
        state["batch_flushing"] = True
        return context, batch, force_reply, trigger_source

    def _directive(self, batch, context, force_reply, trigger_source, attention_check=False):
        return {
            "forward_batch_to_llm": True,
            "batch_messages": batch,
            "force_reply": bool(force_reply),
            "trigger_source": trigger_source,
            "conversation_priority": context.get("_conversation_priority") or context.get("conversation_priority") or "MAY_REPLY",
            "attention_check": bool(attention_check),
            "group_id": self._group_key(context),
            "cycle_id": context.get("_cycle_id"),
            "density_admitted": bool(context.get("_density_admitted")),
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
            if self._autonomy_has_consumed(group_id, message):
                print(
                    f"[LLM PROACTIVE] suppressed_by_autonomy_consumed group={group_id} "
                    f"identity={self._message_identity(message) or 'content-fallback'}",
                    flush=True,
                )
                return None
            state = self.states.get(group_id)
            if state is None:
                state = self._initial_state(now)
                self.states[group_id] = state
                self.last_llm_reply_at.setdefault(group_id, now)
                self._log_phase_change(group_id, "none", state, "initial")
            priority = self._conversation_priority_locked(group_id, context, now, mention=mention)
            context["_conversation_priority"] = priority
            message["conversation_priority"] = priority
            if priority != "DO_NOT_REPLY":
                self.density_events.setdefault(group_id, []).append(now)
            self._update_density_locked(group_id, state, now)
            expired_dispatch = self._advance_state_locked(state, now, group_id)
            self._remember_locked(state, context, message)

            # Keep an explicit message addressed to another participant in the
            # cycle transcript, but do not let it wake/extend a Bot response.
            if priority == "DO_NOT_REPLY":
                return None

            if expired_dispatch and self.callback is not None:
                self._submit_task(
                    self._run_callback,
                    (group_id, expired_dispatch[0], expired_dispatch[1], False,
                     expired_dispatch[2], expired_dispatch[3]),
                )

            # Ordinary input is buffered in all three phases. Local density
            # controls provider admission; the model still decides reply content
            # or silence. Only explicit addressing/short reply leases bypass it.
            direct_priority = priority in {"MUST_REPLY", "SHOULD_REPLY"}
            model_led = self._setting_bool("proactive_decision_by_llm", True)
            gated = density_gate_enabled(self.settings)
            admit_to_model = bool((model_led and not gated) or direct_priority)
            if state["phase"] != "working" and admit_to_model:
                old_phase = state.get("phase")
                recent = list(state.get("recent_messages") or [])
                cycle_messages = list(state.get("cycle_messages") or [])
                cycle_id = state.get("cycle_id")
                if not gated:
                    state.clear()
                    state.update(self._new_work_state(now))
                else:
                    state["phase"] = "working"
                    state.setdefault("cycle_id", None)
                    if not state["cycle_id"]:
                        state["cycle_id"] = f"cycle-{int(now)}-{self.randint(1000, 9999)}"
                state["phase_end"] = now + self._work_extend_seconds()
                state["recent_messages"] = recent
                state["cycle_messages"] = cycle_messages
                state["cycle_id"] = cycle_id or state.get("cycle_id")
                self._log_phase_change(group_id, old_phase, state, "direct_conversation")

            # In mixed mode prefix starts the direct model-led reply path. The state
            # machine switches/keeps working, but does not enqueue the same message.
            if context.get("prefix_used"):
                # Keep direct prefix requests associated with this cycle so
                # the LLM service can refresh the working-context cache after
                # the direct reply is persisted.
                context["_cycle_id"] = state.get("cycle_id")
                print(f"[LLM PROACTIVE] group={group_id} prefix trigger -> direct model request; batch_count=0", flush=True)
                return None

            if state["phase"] != "working" and not gated:
                return None

            force_reply = bool(priority == "MUST_REPLY" or (message.get("is_url_only") and not gated))
            trigger_source = (
                "mention" if mention
                else "reply_to_bot" if self._is_reply_to_bot(context)
                else "conversation_lease" if priority == "SHOULD_REPLY"
                else "url_only" if message.get("is_url_only")
                else "buffer"
            )
            pending = state.setdefault("pending_batch", [])
            pending.append(message)
            state["pending_batch"] = pending[-self._batch_max_messages():]
            state["pending_context"] = dict(context)
            if self._priority_rank(priority) > self._priority_rank(state.get("pending_conversation_priority")):
                state["pending_conversation_priority"] = priority
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
            # by _timer_loop so the worker never blocks on an LLM request. In
            # model-led mode ordinary messages wait for the normal batch flush;
            # they are not discarded merely because they arrived during idle.
            if self.callback is None and (
                force_reply or priority == "SHOULD_REPLY"
                or len(state["pending_batch"]) >= self._batch_max_messages()
            ):
                dispatch = self._take_pending_batch_locked(state, group_id)
                if dispatch:
                    return self._directive(
                        dispatch[1], dispatch[0], dispatch[2], dispatch[3]
                    )
            return None

    def _attention_miss_locked(self, state, now, group_id="unknown"):
        state["checking"] = False
        state["attention_misses"] += 1
        last_features = state.pop("last_attention_features", None)
        if self.style_learner and isinstance(last_features, dict) and last_features.get("messages"):
            try:
                from .learning.initiative import build_initiative_profile
                self.style_learner.store.get_or_update_timing_profile(
                    group_id,
                    build_initiative_profile(last_features.get("messages") or []),
                )
            except Exception:
                pass
        if state["attention_misses"] >= self._attention_no_reply_limit():
            recent = state.get("recent_messages") or []
            cycle_messages = list(state.get("cycle_messages") or [])
            cycle_id = state.get("cycle_id")
            cycle_context = dict(state.get("last_context") or {})
            cycle_curation_last_at = state.get("cycle_curation_last_at")
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
            state["cycle_curation_last_at"] = cycle_curation_last_at
            # The idle transition is the cycle-end boundary where the
            # unified curation (memory + slang + expressions + style) runs.
            state["cycle_curation_pending"] = bool(
                self.cycle_end_callback is not None
                and self._cycle_curation_due_locked(state, now)
            )
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
                not density_gate_enabled(self.settings)
                and state["phase"] == "working"
                and isinstance(result, dict)
                and result.get("_llm_ok")
                and result.get("should_reply")
                and result.get("messages")
                and state["phase_end"] - now < self._work_extend_threshold()
            ):
                state["phase_end"] = now + self._work_extend_seconds()
                print("[LLM PROACTIVE] reply extended work period", flush=True)

    def _autonomy_signals_locked(self, group_id, state, now):
        """Build gate inputs only. These signals never choose content."""
        try:
            heat_score = float(state.get("density_rate") or 0.0)
        except (TypeError, ValueError):
            heat_score = 0.0
        if heat_score <= 0.25:
            heat_level = "low"
        elif heat_score <= 0.65:
            heat_level = "medium"
        else:
            heat_level = "high"
        group_key = str(group_id)
        recent_events = list(self.llm_reply_events.get(group_key) or [])
        recent_messages = list(state.get("recent_messages") or [])
        last_message = recent_messages[-1] if recent_messages else {}
        last_identity = ""
        if isinstance(last_message, dict):
            for key in ("message_id", "local_id", "server_id", "content"):
                value = last_message.get(key)
                if value not in (None, "", 0, "0"):
                    last_identity = f"{key}:{value}"
                    break
        new_message = bool(last_identity and self.autonomy_last_seen_message.get(group_key) != last_identity)
        if last_identity:
            self.autonomy_last_seen_message[group_key] = last_identity
        return {
            "now": now,
            "new_message": new_message,
            "heat_score": heat_score,
            "heat_level": heat_level,
            "pending_messages": len(state.get("recent_messages") or []),
            "last_bot_action_at": self.last_llm_reply_at.get(group_key),
            "recent_action_count": len(recent_events),
            "budget": 1,
        }



    def _autonomy_rhythm_locked(self, state, now, group_id=""):
        """Compute the dynamic autonomy check interval for one group.

        Reference: maibot's ``FrequencyThresholdTurnGate`` (pending + idle
        compensation) and ``IdleBackoffController`` (consecutive idle backoff),
        with one deliberate difference: a fully idle group keeps a slow
        baseline check instead of stopping completely, so autonomy can still
        notice long-quiet groups without burning provider time.

        - Backlog at/above ``backlog_urgent_threshold`` or very recent traffic
          (``backlog_idle_grace_seconds``) -> fastest interval
          (``check_interval_seconds``); the idle backoff does not apply while
          traffic is fresh (maibot's pending bypass).
        - Longer idle grows the interval by ``backlog_idle_scale`` per idle
          second, capped at ``idle_check_interval_seconds``.
        - After each autonomy cycle that finds no new message and executes no
          action, the interval is multiplied by ``idle_backoff_factor``
          (exponential, limited to ``idle_backoff_max_steps``), so a silent
          group sinks to the slow cold-group baseline instead of being probed
          every few minutes forever (maibot's IdleBackoffController).
        - The result is always a finite positive number (>= 10s).
        """
        base = self._autonomy_float_setting("check_interval_seconds", 60, minimum=10)
        idle_check_interval = self._autonomy_float_setting(
            "idle_check_interval_seconds", 1800, minimum=base
        )
        urgent_threshold = self._autonomy_int_setting("backlog_urgent_threshold", 3, minimum=1)
        idle_grace = self._autonomy_float_setting("backlog_idle_grace_seconds", 45, minimum=0)
        idle_scale = self._autonomy_float_setting("backlog_idle_scale", 0.5, minimum=0)

        recent = list((state or {}).get("recent_messages") or [])
        pending_count = len(recent)
        last_ts = None
        for item in reversed(recent):
            if not isinstance(item, dict):
                continue
            try:
                last_ts = float(item.get("timestamp"))
            except (TypeError, ValueError):
                continue
            if last_ts:
                break
        quiet_started = (state or {}).get("quiet_started_at")
        if last_ts is None and quiet_started is not None:
            try:
                last_ts = float(quiet_started)
            except (TypeError, ValueError):
                last_ts = None
        last_message_age = max(0.0, now - last_ts) if last_ts is not None else None
        if quiet_started is not None:
            try:
                idle_seconds = max(0.0, now - float(quiet_started))
            except (TypeError, ValueError):
                idle_seconds = last_message_age
        else:
            idle_seconds = last_message_age

        fresh_traffic = bool(
            pending_count >= urgent_threshold
            or (last_message_age is not None and last_message_age <= idle_grace)
        )
        group_key = str(group_id or "")
        try:
            streak = max(0, int(self.autonomy_idle_streaks.get(group_key) or 0))
        except (TypeError, ValueError):
            streak = 0
        if fresh_traffic:
            interval = base
        else:
            idle_extra = max(0.0, (idle_seconds if idle_seconds is not None else 0.0) - idle_grace)
            interval = min(idle_check_interval, base + idle_extra * idle_scale)
            max_steps = self._autonomy_int_setting("idle_backoff_max_steps", 6, minimum=0)
            factor = self._autonomy_float_setting("idle_backoff_factor", 2.0, minimum=1.0)
            multiplier = factor ** min(streak, max_steps)
            if multiplier > 1.0:
                interval = min(idle_check_interval, interval * multiplier)
        interval = max(self._autonomy_float_setting("min_interval_seconds", 10, minimum=10), interval)
        interval = max(10.0, min(idle_check_interval, interval))
        return {
            "interval": interval,
            "base_interval": base,
            "idle_backoff_streak": streak,
            "signals_extra": {
                "pending_messages": pending_count,
                "backlog_pending": pending_count,
                "last_message_age": last_message_age,
                "idle_seconds": idle_seconds,
                "dynamic_interval": interval,
                "idle_backoff_streak": streak,
            },
        }

    def autonomy_thinking_rhythm(self, group_id, now=None):
        """Thread-safe public view of the per-group autonomy thinking rhythm."""
        now = float(now if now is not None else self.clock())
        group_key = str(group_id or "")
        with self.lock:
            return self._autonomy_rhythm_locked(self.states.get(group_key), now, group_id=group_key)

    def record_autonomy_cycle(self, group_id, result=None, now=None):
        """maibot-inspired idle backoff bookkeeping for one autonomy cycle.

        Call this after an autonomy opportunity finishes.  If the cycle both
        found no new message (compared with the previous check) and produced no
        speak/tool/mixed decision, count it as an idle cycle and grow the
        consecutive-idle streak; otherwise reset the streak so fresh traffic
        or a real action restores the fast cadence.
        """
        now = float(now if now is not None else self.clock())
        group_key = str(group_id or "")
        with self.lock:
            state = self.states.get(group_key)
            if state is None:
                return
            last_identity = ""
            recent = list(state.get("recent_messages") or [])
            for item in reversed(recent):
                if not isinstance(item, dict):
                    continue
                identity = self._message_identity(item)
                if not identity:
                    identity = str(item.get("content") or "").strip()
                if identity:
                    last_identity = identity
                    break
            seen_before = self.autonomy_last_checked_identity.get(group_key)
            new_message = bool(
                last_identity
                and seen_before not in (None, "")
                and last_identity != seen_before
            )
            self.autonomy_last_checked_identity[group_key] = last_identity or seen_before or ""
            acted = False
            if result is not None:
                decision = getattr(result, "decision", None)
                if decision is not None:
                    acted = str(getattr(decision, "type", "") or "").strip().lower() in {
                        "speak",
                        "tool",
                        "mixed",
                    }
            if new_message or acted:
                self.autonomy_idle_streaks[group_key] = 0
            else:
                current = max(0, int(self.autonomy_idle_streaks.get(group_key) or 0))
                self.autonomy_idle_streaks[group_key] = current + 1

    def _run_autonomy_callback(self, group_id, context, batch, signals):
        try:
            self.autonomy_callback(group_id, context, batch, signals)
        except Exception as exc:
            print(
                f"[LLM AUTONOMY] callback failed group={group_id}: {exc}",
                flush=True,
            )

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
            self._refresh_activity_profiles(timing_groups, now)
            self._refresh_behavior_priors(timing_groups, now)
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

                    if (
                        (state["phase"] == "working" or density_gate_enabled(self.settings))
                        and state.get("pending_batch")
                        and not state.get("batch_flushing")
                        and state.get("batch_flush_at") is not None
                        and now >= state["batch_flush_at"]
                    ):
                        dispatch = self._take_pending_batch_locked(state, group_id)
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
                        not density_gate_enabled(self.settings)
                        and state["phase"] == "idle"
                        and state.get("cycle_snapshot")
                        and not state.get("cycle_curation_pending")
                        and not state.get("cycle_curation_inflight")
                        and self.cycle_end_callback is not None
                        and self._cycle_curation_due_locked(state, now)
                    ):
                        state["cycle_curation_pending"] = True

                    if (
                        not density_gate_enabled(self.settings)
                        and state["phase"] == "idle"
                        and state.get("cycle_curation_pending")
                        and not state.get("cycle_curation_inflight")
                        and (
                            state.get("cycle_curation_retry_at") is None
                            or now >= float(state.get("cycle_curation_retry_at") or 0)
                        )
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
                        state["cycle_curation_inflight"] = True

            for group_id, context, batch, attention_check, force_reply, trigger_source in pending:
                self._submit_task(
                    self._run_callback,
                    (group_id, context, batch, attention_check, force_reply, trigger_source),
                )
            for group_id, context, messages in cycle_pending:
                self._submit_task(self._run_cycle_curation, (group_id, context, messages))
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
                    if state.get("pending_batch") and (state.get("phase") == "working" or density_gate_enabled(self.settings)):
                        if state.get("pending_force_reply") or len(state["pending_batch"]) >= self._batch_max_messages():
                            state["batch_flush_at"] = self.clock()
                        elif not state.get("batch_flush_at"):
                            state["batch_flush_at"] = self.clock() + self._batch_debounce_seconds()

                if attention_check:
                    if state and state.get("checking"):
                        self._attention_miss_locked(state, self.clock(), group_id)

    def _run_cycle_curation(self, group_id, context, messages):
        success = False
        error = None
        try:
            success = bool(self.cycle_end_callback(group_id, context, messages))
        except Exception as exc:
            error = exc
            print(f"[LLM CYCLE CURATION ERROR] {exc}", flush=True)
        with self.lock:
            state = self.states.get(group_id)
            cycle_id = str((context or {}).get("_cycle_id") or (context or {}).get("cycle_id") or "")
            same_cycle = bool(
                state
                and str(state.get("cycle_id") or "") == cycle_id
                and state.get("phase") == "idle"
            )
            if not same_cycle:
                return
            state["cycle_curation_inflight"] = False
            if success:
                state["cycle_curation_pending"] = False
                state["cycle_curation_retry_at"] = None
                state["cycle_curation_attempts"] = 0
                state["cycle_curation_last_at"] = self.clock()
                return
            attempts = int(state.get("cycle_curation_attempts") or 0) + 1
            state["cycle_curation_attempts"] = attempts
            delay = min(300, max(5, 5 * (2 ** min(attempts - 1, 5))))
            state["cycle_curation_retry_at"] = self.clock() + delay
            state["cycle_curation_pending"] = True
            print(
                f"[LLM CYCLE CURATION RETRY] group={group_id} "
                f"attempt={attempts} retry_in={delay}s"
                + (f" error={error}" if error else " result=false"),
                flush=True,
            )
