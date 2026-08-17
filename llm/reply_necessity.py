"""Cheap local reply-necessity scoring for autonomous working periods.

This module deliberately contains no model call. It is a MaiBot-inspired
candidate gate: obvious direct requests bypass it, while ordinary working
period batches need enough local conversational evidence before spending an
LLM request.
"""

from __future__ import annotations

import re


_QUESTION_MARKS = "?？"
_REQUEST_MARKERS = (
    "帮我", "帮忙", "能不能", "可以不", "有没有", "怎么", "如何", "请问",
    "求", "推荐", "解释", "看看", "查一下", "告诉我", "发一下", "说说",
)
_OPINION_MARKERS = ("觉得", "认为", "怎么看", "意见", "建议", "投票", "选哪个", "哪个好")
_CONTINUATION_MARKERS = ("然后", "所以", "但是", "不过", "确实", "对啊", "对", "是的", "哈哈")
_SHORT_REACTION_RE = re.compile(r"^[\W_\d]*[哈呵嘿笑啊哦嗯呃额？！!?~～。\.]{1,8}[\W_\d]*$", re.UNICODE)


class ReplyNecessityGate:
    """Score a batch using bounded deterministic signals."""

    def __init__(self, settings=None):
        self.settings = settings if isinstance(settings, dict) else {}

    def _int(self, name, default, minimum=0, maximum=None):
        try:
            value = int(self.settings.get(name, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        return min(value, maximum) if maximum is not None else value

    @staticmethod
    def _text(item):
        return str(item.get("content") or "").strip() if isinstance(item, dict) else ""

    @staticmethod
    def _is_bot(item):
        return bool(
            isinstance(item, dict)
            and (item.get("is_bot") or str(item.get("role") or "") == "assistant")
        )

    def score(self, messages, group_messages=None, learning=None):
        batch = [item for item in (messages or []) if isinstance(item, dict) and self._text(item)]
        user_batch = [item for item in batch if not self._is_bot(item)]
        if not user_batch:
            return 0, {"reason": "no_user_message", "message_count": 0}

        # A grouped batch is stronger evidence than an isolated short line;
        # two ordinary messages should still have a chance to reach the LLM.
        score = min(20, max(0, len(user_batch) - 1) * 12)
        reasons = []
        for item in user_batch:
            text = self._text(item)
            compact = "".join(text.split())
            chars = len(compact)
            question_or_request = (
                any(mark in text for mark in _QUESTION_MARKS)
                or any(mark in text for mark in _REQUEST_MARKERS)
            )
            opinion_request = any(mark in text for mark in _OPINION_MARKERS)
            if item.get("is_at_bot") or item.get("is_mentioned") or item.get("prefix_used"):
                score += 100
                reasons.append("direct_address")
            if question_or_request:
                score += 15
                reasons.append("question_or_request")
            if opinion_request:
                score += 20
                reasons.append("opinion_request")
            if chars >= self._int("reply_necessity_long_chars", 60, 20, 500):
                score += 10
                reasons.append("substantive_length")
            elif chars >= self._int("reply_necessity_medium_chars", 24, 8, 200):
                score += 5
            elif (chars <= 6 and not question_or_request and not opinion_request) or _SHORT_REACTION_RE.match(compact):
                score -= 25
                reasons.append("short_reaction")
            if any(mark in text for mark in _CONTINUATION_MARKERS):
                score += 5

        history = [item for item in (group_messages or []) if isinstance(item, dict)]
        recent = history[-5:]
        bot_count = sum(1 for item in recent if self._is_bot(item))
        if recent and bot_count:
            penalty = min(25, round(bot_count / len(recent) * 25))
            score -= penalty
            reasons.append("bot_recent_ratio")

        learning_penalty = 0
        learning = learning if isinstance(learning, dict) else {}
        if bool(self.settings.get("reply_timing_learning_enabled", True)):
            try:
                windows = max(0, int(learning.get("observer_windows", 0) or 0))
                replies = max(0, int(learning.get("observer_reply_windows", 0) or 0))
                minimum = max(1, int(self.settings.get("reply_timing_min_windows", 20) or 20))
                target = max(0.01, min(1.0, float(self.settings.get("reply_timing_target_reply_rate", 0.28) or 0.28)))
            except (TypeError, ValueError):
                windows, replies, minimum, target = 0, 0, 20, 0.28
            if windows >= minimum:
                observed_rate = (replies + 1.0) / (windows + 2.0)
                if observed_rate > target:
                    learning_penalty = min(15, max(1, round(
                        (observed_rate - target) / max(0.01, 1.0 - target) * 15
                    )))
                    score -= learning_penalty
                    reasons.append("learned_reply_rate")

        threshold = self._int("reply_necessity_threshold", 35, 0, 200)
        return score, {
            "reason": ",".join(dict.fromkeys(reasons)) or "ordinary_batch",
            "message_count": len(user_batch),
            "threshold": threshold,
            "bot_recent_count": bot_count,
            "learning_penalty": learning_penalty,
        }

    def allow(self, messages, group_messages=None, learning=None):
        score, detail = self.score(messages, group_messages, learning)
        threshold = int(detail.get("threshold", self._int("reply_necessity_threshold", 35, 0, 200)))
        return score >= threshold, score, detail
