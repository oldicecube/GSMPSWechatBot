"""Bounded, local conversation scheduling for autonomous group replies.

This module deliberately has no model, embedding, or database dependency.
It turns the transcript already kept for the group into a small ``skip`` /
``defer`` / ``plan`` decision. ``plan`` means one normal proactive LLM call;
the model still decides whether and how to speak.
"""

from __future__ import annotations

import random
import re
import threading
import time


_QUESTION_MARKS = ("?", "\uFF1F")
_REQUEST_MARKERS = (
    "\u5E2E\u6211", "\u5E2E\u5FD9", "\u80FD\u4E0D\u80FD", "\u53EF\u4EE5\u5417", "\u6709\u6CA1\u6709", "\u600E\u4E48", "\u5982\u4F55",
    "\u8BF7\u95EE", "\u6C42", "\u63A8\u8350", "\u89E3\u91CA", "\u770B\u770B", "\u67E5\u4E00\u4E0B", "\u544A\u8BC9\u6211",
)
_OPINION_MARKERS = ("\u89C9\u5F97", "\u8BA4\u4E3A", "\u600E\u4E48\u770B", "\u610F\u89C1", "\u5EFA\u8BAE", "\u6295\u7968", "\u9009\u54EA\u4E2A")
_SHORT_REACTION = re.compile(r"^[\W_\d]*(?:[\u54C8\u5475\u563B\u7B11\u554A\u54E6\u54E5\u5443\u989D\u55EF\u786E\u5B9E]|6){1,8}[\W_\d]*$", re.UNICODE)


class ConversationPulse:
    """Cheap group pulse used to schedule, never to author, a response."""

    def __init__(self, settings=None):
        self.settings = settings if isinstance(settings, dict) else {}
        self._last_plan_at = {}
        self._last_fragmented_plan_at = {}
        self._lock = threading.Lock()
        self.clock = time.time
        self.random = random.random

    def _int(self, name, default, minimum=0, maximum=None):
        try:
            value = int(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        return min(value, maximum) if maximum is not None else value

    def _float(self, name, default, minimum=0.0, maximum=1.0):
        try:
            value = float(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _text(item):
        return str(item.get("content") or "").strip() if isinstance(item, dict) else ""

    @staticmethod
    def _is_bot(item):
        return bool(isinstance(item, dict) and (item.get("is_bot") or str(item.get("role") or "") == "assistant"))

    @staticmethod
    def _timestamp(item, fallback):
        try:
            return float(item.get("timestamp"))
        except (AttributeError, TypeError, ValueError):
            return fallback

    @staticmethod
    def _is_direct(item):
        return bool(isinstance(item, dict) and (item.get("is_at_bot") or item.get("is_mentioned") or item.get("prefix_used")))

    @staticmethod
    def _features(text):
        compact = "".join(str(text or "").split()).lower()
        if len(compact) < 2:
            return set()
        words = {word for word in re.findall(r"[a-z0-9_]{2,}", compact) if len(word) >= 2}
        cjk = "".join(char for char in compact if "\u3400" <= char <= "\u9fff")
        grams = {cjk[index:index + 2] for index in range(len(cjk) - 1)}
        return words | grams

    def _learned_penalty(self, learning):
        if not bool(self.settings.get("reply_timing_learning_enabled", True)):
            return 0
        learning = learning if isinstance(learning, dict) else {}
        try:
            windows = max(0, int(learning.get("observer_windows", 0) or 0))
            replies = max(0, int(learning.get("observer_reply_windows", 0) or 0))
            minimum = max(1, int(self.settings.get("reply_timing_min_windows", 20) or 20))
            target = self._float("reply_timing_target_reply_rate", 0.28, 0.01, 1.0)
        except (TypeError, ValueError):
            return 0
        if windows < minimum:
            return 0
        observed = (replies + 1.0) / (windows + 2.0)
        return min(15, max(0, round((observed - target) * 30))) if observed > target else 0

    def decide(self, group_id, batch_messages, group_messages=None, learning=None, seconds_since_bot_reply=0):
        """Return ``(decision, detail)`` for one already-batched group event."""
        now = self.clock()
        batch = [item for item in (batch_messages or []) if isinstance(item, dict) and self._text(item) and not self._is_bot(item)]
        if not batch:
            return "skip", {"reason": "no_user_message", "mode": "none"}

        history = [item for item in (group_messages or []) if isinstance(item, dict) and self._text(item)][-120:]
        user_history = [item for item in history if not self._is_bot(item)]
        recent_60 = [item for item in user_history if self._timestamp(item, now) >= now - 60]
        recent_180 = [item for item in user_history if self._timestamp(item, now) >= now - 180]
        speakers = {str(item.get("nickname") or item.get("sender_wxid") or "") for item in recent_180}
        speakers.discard("")
        direct = any(self._is_direct(item) for item in batch)
        batch_text = "\n".join(self._text(item) for item in batch)
        compact = "".join(batch_text.split())
        question = any(mark in batch_text for mark in _QUESTION_MARKS) or any(mark in batch_text for mark in _REQUEST_MARKERS)
        opinion = any(mark in batch_text for mark in _OPINION_MARKERS)
        short_only = all(len("".join(self._text(item).split())) <= 8 or _SHORT_REACTION.match("".join(self._text(item).split())) for item in batch)

        batch_features = self._features(batch_text)
        preceding = [item for item in recent_180 if item not in batch][-30:]
        preceding_features = set().union(*(self._features(self._text(item)) for item in preceding)) if preceding else set()
        overlap = len(batch_features & preceding_features) / max(1, len(batch_features))
        topic_sustained = len(recent_180) >= self._int("pulse_topic_min_messages", 4, 2, 30) and len(speakers) >= 2 and overlap >= self._float("pulse_topic_overlap", 0.16, 0.01, 1.0)
        density = len(recent_60)
        learned_penalty = self._learned_penalty(learning)
        score = min(18, max(0, len(batch) - 1) * 9)
        if direct:
            score += 100
        if question:
            score += 22
        if opinion:
            score += 12
        if topic_sustained:
            score += 20
        if len(compact) >= self._int("reply_necessity_long_chars", 60, 20, 500):
            score += 10
        elif len(compact) >= self._int("reply_necessity_medium_chars", 24, 8, 200):
            score += 5
        if short_only:
            score -= 20
        score -= learned_penalty

        cooldown = self._int("pulse_plan_cooldown_seconds", 45, 0, 3600)
        fragment_cooldown = self._int("fragmented_chat_min_interval_seconds", 360, 30, 86400)
        with self._lock:
            last_plan = self._last_plan_at.get(str(group_id), 0.0)
            last_fragmented = self._last_fragmented_plan_at.get(str(group_id), 0.0)
            cooldown_active = bool(cooldown and now - last_plan < cooldown)
            mode = "none"
            decision = "skip"
            if direct:
                decision, mode = "plan", "direct"
            elif not cooldown_active and (question or opinion or topic_sustained) and score >= self._int("pulse_plan_threshold", 28, 0, 200):
                decision, mode = "plan", "topic"
            else:
                fragmented_density = self._int("fragmented_chat_min_messages_per_minute", 3, 1, 60)
                fragmented_silence = self._int("fragmented_chat_min_silence_seconds", 360, 30, 86400)
                probability = self._float("fragmented_chat_plan_probability", 0.12, 0.0, 1.0)
                heat_scale = min(1.0, density / max(1, fragmented_density * 2))
                eligible_fragmented = (
                    not cooldown_active and density >= fragmented_density and seconds_since_bot_reply >= fragmented_silence
                    and now - last_fragmented >= fragment_cooldown
                )
                if eligible_fragmented and self.random() < probability * heat_scale:
                    decision, mode = "plan", "fragmented_chat"
                    self._last_fragmented_plan_at[str(group_id)] = now
                elif topic_sustained or density >= fragmented_density:
                    decision, mode = "defer", "wait_for_context"
            if decision == "plan":
                self._last_plan_at[str(group_id)] = now

        return decision, {
            "reason": mode if mode != "none" else ("short_reaction" if short_only else "low_signal"),
            "mode": mode,
            "score": score,
            "density_60s": density,
            "topic_sustained": topic_sustained,
            "topic_overlap": round(overlap, 3),
            "active_speakers": len(speakers),
            "bot_silence_seconds": max(0, int(seconds_since_bot_reply or 0)),
            "cooldown_active": cooldown_active,
            "learned_penalty": learned_penalty,
        }
