"""Conservative message-boundary handling for cycle learning.

A learning unit is the smallest source-traceable utterance that can be used as
slang evidence.  The code intentionally refuses to construct a phrase from a
likely half sentence or from messages sent by different people.
"""
from __future__ import annotations

import re
from typing import Iterable

# Keep this list deliberately narrow: a short reaction without one of these
# unfinished forms remains a complete unit (for example, a two- or three-word
# reaction must not be discarded just because it is short).
_SENTENCE_END = set("\u3002\uff01\uff1f!?\u2026~\uff5e")
_TRAILING_FRAGMENT_RE = re.compile(
    r"(?:[,;:\u3001\uff0c\uff1b\uff1a\u2014\-\(\[\{\u3008\u300a\u300c\u300e\"'`]|"
    r"(?:\u4f46\u662f|\u6240\u4ee5|\u56e0\u4e3a|\u5982\u679c|\u867d\u7136|\u7136\u540e|\u800c\u4e14|\u4e0d\u8fc7|\u4ee5\u53ca|\u6211\u89c9\u5f97|\u6211\u60f3|\u6211\u5728\u60f3|\u8fd9\u4e2a|\u90a3\u4e2a|\u6709\u70b9|\u89c9\u5f97))$"
)
_LEADING_CONTINUATION_RE = re.compile(
    r"^(?:[,;:\u3001\uff0c\uff1b\uff1a\)\]\}\u3009\u300b\u300d\u300f\"'`]|"
    r"(?:\u4f46\u662f|\u6240\u4ee5|\u56e0\u4e3a|\u5982\u679c|\u7136\u540e|\u800c\u4e14|\u4e0d\u8fc7|\u4ee5\u53ca|\u800c|\u4e5f|\u5c31|\u8fd8|\u624d))"
)


def _source_id(item: dict, index: int) -> str:
    value = str(item.get("source_id") or "").strip()
    if value:
        return value
    for key in ("message_id", "local_id", "server_id"):
        value = str(item.get(key) or "").strip()
        if value and value != "0":
            return value
    return f"cycle-message-{index}"


def _speaker(item: dict) -> str:
    return str(
        item.get("wxid") or item.get("sender_wxid") or item.get("sender_id")
        or item.get("sender") or item.get("speaker") or item.get("nickname") or ""
    ).strip()[:120]


def _timestamp(item: dict) -> float | None:
    try:
        value = float(item.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _looks_incomplete(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return True
    if text[-1] in _SENTENCE_END:
        return False
    return bool(_TRAILING_FRAGMENT_RE.search(text))


def _looks_continuation(text: str) -> bool:
    return bool(_LEADING_CONTINUATION_RE.search(str(text or "").strip()))


def build_learning_units(
    messages: Iterable[dict] | None,
    *,
    fragment_gap_seconds: float = 35.0,
) -> list[dict]:
    """Build conservative, source-traceable units for cycle learning.

    Only messages from the same speaker within ``fragment_gap_seconds`` are
    joined, and only when the prior message has an unmistakable unfinished
    boundary. Unresolved fragments remain in the curation context but cannot
    become local or LLM-proposed slang evidence.
    """
    units: list[dict] = []
    for index, raw in enumerate(messages or []):
        if not isinstance(raw, dict):
            continue
        if raw.get("is_bot") or str(raw.get("role") or "") == "assistant":
            continue
        content = str(raw.get("content") or "").strip()
        if not content or content.startswith("/"):
            continue
        source_id = _source_id(raw, index)
        speaker = _speaker(raw)
        timestamp = _timestamp(raw)
        incomplete = _looks_incomplete(content)
        previous = units[-1] if units else None
        can_merge = False
        if previous and previous.get("speaker_id") and previous.get("speaker_id") == speaker:
            previous_ts = previous.get("last_timestamp")
            within_gap = (
                timestamp is None or previous_ts is None
                or 0 <= timestamp - previous_ts <= max(1.0, float(fragment_gap_seconds))
            )
            can_merge = bool(
                within_gap
                and previous.get("ends_incomplete")
                and (_looks_continuation(content) or len(content) <= 80)
            )
        if can_merge:
            previous["content"] = str(previous.get("content") or "") + content
            previous.setdefault("source_ids", []).append(source_id)
            previous["last_timestamp"] = timestamp
            previous["ends_incomplete"] = incomplete
            previous["complete"] = not incomplete
            previous["candidate_allowed"] = not incomplete
            previous["boundary_confidence"] = min(
                0.98, float(previous.get("boundary_confidence", 0.85)) + 0.08
            )
            previous["merge_reason"] = "same_speaker_incomplete_continuation"
            continue
        units.append({
            "unit_id": f"unit-{len(units)}-{source_id}",
            "source_ids": [source_id],
            "speaker_id": speaker,
            "content": content,
            "first_timestamp": timestamp,
            "last_timestamp": timestamp,
            "ends_incomplete": incomplete,
            "complete": not incomplete,
            "candidate_allowed": not incomplete,
            "boundary_confidence": 0.98 if not incomplete else 0.42,
            "merge_reason": "single_message" if not incomplete else "unresolved_fragment",
        })
    for unit in units:
        unit.pop("ends_incomplete", None)
        unit.pop("first_timestamp", None)
        unit.pop("last_timestamp", None)
    return units
