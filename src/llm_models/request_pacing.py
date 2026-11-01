"""Optional shared pacing for low-rate anonymous endpoints, across event loops."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import threading
import time
from typing import Any


@dataclass
class _Gate:
    next_at: float = 0.0
    sequence: int = 0
    waiters: dict[object, tuple[int, int]] = field(default_factory=dict)


_LOCK = threading.Lock()
_GATES: dict[tuple[str, str, str], _Gate] = {}


async def wait_for_request_slot(provider: Any, model: Any, request_type: str = '') -> None:
    interval = float(getattr(provider, 'request_min_interval', 0.0))
    if interval <= 0:
        return
    key = (str(provider.base_url).rstrip('/'), hashlib.sha256(str(provider.api_key).encode()).hexdigest(),
           str(model.model_identifier))
    ticket = object()
    # 聊天请求优先于后台学习，已发出的请求不被打断。
    priority = 0 if request_type.startswith(('maisaka.', 'replyer')) else 10
    with _LOCK:
        gate = _GATES.setdefault(key, _Gate())
        gate.sequence += 1
        gate.waiters[ticket] = (priority, gate.sequence)
    try:
        while True:
            with _LOCK:
                delay = max(0.0, gate.next_at - time.monotonic())
                if min(gate.waiters, key=gate.waiters.get) is ticket and delay <= 0:
                    gate.next_at = time.monotonic() + interval
                    return
            await asyncio.sleep(min(0.25, delay) if delay > 0 else 0.05)
    finally:
        with _LOCK:
            gate.waiters.pop(ticket, None)
