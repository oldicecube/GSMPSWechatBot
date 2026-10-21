"""Thread-safe TTL store tracking which autonomy events already produced a reply.

One autonomy cycle may react to a real message and successfully send an action.
The ordinary proactive and direct reply pipelines would otherwise keep working
on the same context and generate a second, uncoordinated reply (the observed
``autonomy 决定 A，普通 LLM 又发送 B/C/D`` behaviour).  Those pipelines consult
this store before generating their own reply, so one autonomy event/cycle can
produce at most one final reply unless follow-up is explicitly allowed.

Design constraints:
- In-memory only, TTL-bounded, thread-safe.  No new persistence layer.
- Identity matching is tolerant of different layers collapsing the WeFlow
  message id fields (``messageKey``/``localId``/``serverId``) into different
  field names, so records match on the raw identity *values*.
- A content fingerprint is used only as a fallback for messages that carry no
  usable id at all, so duplicate suppression stays conservative.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

_IDENTITY_FIELDS = ("message_id", "local_id", "server_id")


def message_identity_values(message: Any) -> list[str]:
    """Raw identity values present on a message dict, any field order."""
    values: list[str] = []
    if isinstance(message, dict):
        for field in _IDENTITY_FIELDS:
            value = message.get(field)
            if value not in (None, "", 0, "0"):
                values.append(str(value).strip())
    return values


def _fingerprint(group_id: str, message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = str(message.get("content") or "").strip()
    sender = str(
        message.get("sender_wxid") or message.get("wxid")
        or message.get("sender_nickname") or message.get("nickname")
        or message.get("speaker") or ""
    ).strip()
    if not content:
        return ""
    raw = "{0}|{1}|{2}".format(str(group_id or ""), sender, content)
    return "fp:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def probe_from_message(message: Any) -> dict[str, Any]:
    """Build a normalized probe dict from any layer's message payload."""
    if not isinstance(message, dict):
        message = {}
    return {
        "message_id": message.get("message_id") or message.get("messageKey") or message.get("messageId") or "",
        "local_id": message.get("local_id") or message.get("localId") or "",
        "server_id": (
            message.get("server_id") or message.get("serverId")
            or message.get("svrid") or message.get("rawid") or ""
        ),
        "content": message.get("content") or "",
        "sender_wxid": message.get("sender_wxid") or message.get("wxid") or "",
        "sender_nickname": message.get("sender_nickname") or message.get("nickname") or message.get("user") or "",
        "wxid": message.get("wxid") or "",
        "nickname": message.get("nickname") or message.get("user") or "",
        "speaker": message.get("speaker") or "",
    }


class AutonomyConsumedStore:
    """TTL-bounded in-memory store of consumed autonomy events."""

    def __init__(self, ttl_seconds: float = 600.0, max_records: int = 2000):
        self.ttl_seconds = max(30.0, float(ttl_seconds or 600.0))
        self.max_records = max(100, int(max_records or 2000))
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def mark_consumed(
        self,
        group_id: str,
        message_ids: Any = None,
        messages: Any = None,
        event_id: str = "",
        content: str = "",
        sender: str = "",
        ttl_seconds: float | None = None,
    ) -> None:
        now = time.time()
        ttl = self.ttl_seconds if ttl_seconds is None else max(30.0, float(ttl_seconds or self.ttl_seconds))
        record: dict[str, Any] = {
            "event_id": str(event_id or ""),
            "group_id": str(group_id or ""),
            "content": str(content or "")[:240],
            "sender": str(sender or "")[:80],
            "ts": now,
            "expires_at": now + ttl,
        }
        keys: set[str] = set()
        if str(event_id or "").strip():
            keys.add("event:{0}".format(str(event_id).strip()))
        if isinstance(message_ids, (list, tuple, set)):
            for value in message_ids:
                if str(value or "").strip():
                    keys.add(str(value).strip())
        elif str(message_ids or "").strip():
            keys.add(str(message_ids).strip())
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                has_identity = False
                for value in message_identity_values(item):
                    if value:
                        keys.add(value)
                        has_identity = True
                if not has_identity:
                    fingerprint = _fingerprint(str(group_id or ""), item)
                    if fingerprint:
                        keys.add(fingerprint)
                if not record["sender"]:
                    record["sender"] = str(
                        item.get("sender_wxid") or item.get("wxid")
                        or item.get("sender_nickname") or item.get("nickname")
                        or item.get("speaker") or ""
                    )[:80]
        group_prefix = "{0}|".format(str(group_id or ""))
        with self._lock:
            self._prune_locked(now)
            for value in keys:
                key = group_prefix + str(value)
                stored = dict(record)
                stored["identity"] = str(value)[:120]
                self._records[key] = stored

    def is_consumed(
        self,
        group_id: str,
        message: Any = None,
        message_ids: Any = None,
        event_id: str = "",
    ) -> bool:
        candidates: set[str] = set()
        if isinstance(message, dict):
            has_identity = False
            for value in message_identity_values(message):
                if value:
                    candidates.add(value)
                    has_identity = True
            if not has_identity:
                fingerprint = _fingerprint(str(group_id or ""), message)
                if fingerprint:
                    candidates.add(fingerprint)
        if isinstance(message_ids, (list, tuple, set)):
            for value in message_ids:
                if str(value or "").strip():
                    candidates.add(str(value).strip())
        elif str(message_ids or "").strip():
            candidates.add(str(message_ids).strip())
        if str(event_id or "").strip():
            candidates.add("event:{0}".format(str(event_id).strip()))
        if not candidates:
            return False
        now = time.time()
        group_prefix = "{0}|".format(str(group_id or ""))
        with self._lock:
            self._prune_locked(now)
            for candidate in candidates:
                if (group_prefix + str(candidate)) in self._records:
                    return True
            event_value = str(event_id or "").strip()
            if event_value:
                for key, stored in self._records.items():
                    if key.startswith(group_prefix) and stored.get("event_id") == event_value:
                        return True
        return False

    def latest_consumed(self, group_id: str) -> dict[str, Any] | None:
        now = time.time()
        group_prefix = "{0}|".format(str(group_id or ""))
        with self._lock:
            self._prune_locked(now)
            results = [
                dict(stored)
                for key, stored in self._records.items()
                if key.startswith(group_prefix)
            ]
        if not results:
            return None
        results.sort(key=lambda item: float(item.get("ts") or 0.0), reverse=True)
        return results[0]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        expired = [key for key, stored in self._records.items() if float(stored.get("expires_at") or 0) < cutoff]
        for key in expired:
            self._records.pop(key, None)
        if len(self._records) > self.max_records:
            ordered = sorted(self._records, key=lambda key: float(self._records[key].get("ts") or 0))
            for key in ordered[: len(self._records) - self.max_records]:
                self._records.pop(key, None)


_default_store: AutonomyConsumedStore | None = None
_STORE_LOCK = threading.Lock()


def get_consumed_store() -> AutonomyConsumedStore:
    global _default_store
    with _STORE_LOCK:
        if _default_store is None:
            _default_store = AutonomyConsumedStore()
        return _default_store


def reset_consumed_store() -> None:
    """Replace the process-wide store (used by tests and shutdown)."""
    global _default_store
    with _STORE_LOCK:
        _default_store = AutonomyConsumedStore()
