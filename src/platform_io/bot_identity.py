"""Short-lived adapter-resolved bot identity for prompt/history metadata.

The account ID is authoritative; nickname and group card are display aliases
scoped to platform, account, connection and group. This must never become a
global persona or be used as a permission identity.
"""
from __future__ import annotations

from threading import RLock
import time

_lock = RLock()
_values: dict[tuple[str, str, str, str], tuple[float, dict[str, str]]] = {}
_TTL = 300.0


def remember(*, platform: str, account_id: str, scope: str = '', group_id: str,
             nickname: str = '', cardname: str = '') -> None:
    key = (str(platform or '').lower(), str(account_id or ''), str(scope or ''), str(group_id or ''))
    if not all(key):
        return
    with _lock:
        _values[key] = (time.monotonic() + _TTL, {
            'platform': key[0], 'user_id': key[1], 'account_id': key[1],
            'scope': key[2], 'group_id': key[3], 'nickname': str(nickname or ''),
            'cardname': str(cardname or ''),
        })
        now = time.monotonic()
        for stale, (expiry, _) in list(_values.items()):
            if expiry <= now:
                _values.pop(stale, None)


def get(*, platform: str, account_id: str, scope: str = '', group_id: str) -> dict[str, str] | None:
    key = (str(platform or '').lower(), str(account_id or ''), str(scope or ''), str(group_id or ''))
    with _lock:
        item = _values.get(key)
        if item is None and not scope:
            candidates = [value for candidate, value in _values.items()
                          if candidate[0] == key[0] and candidate[1] == key[1] and candidate[3] == key[3]]
            item = candidates[-1] if candidates else None
        if not item:
            return None
        if item[0] <= time.monotonic():
            _values.pop(key, None)
            return None
        return dict(item[1])


def clear() -> None:
    with _lock:
        _values.clear()
