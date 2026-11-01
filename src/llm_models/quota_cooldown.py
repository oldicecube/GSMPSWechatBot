"""Share explicit quota exhaustion across tasks without blocking other providers."""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_BLOCKED: dict[tuple[str, str, str], tuple[float, str]] = {}
QUOTA_COOLDOWN_SECONDS = 600.0


def _key(provider: Any, model: Any) -> tuple[str, str, str]:
    credential = hashlib.sha256(str(provider.api_key or "").encode()).hexdigest()
    return str(provider.base_url).rstrip("/"), credential, str(model.model_identifier)


def explicit_quota_exhaustion(error: Exception) -> bool:
    if getattr(error, "status_code", None) != 429:
        return False
    seen = set()
    current: BaseException | None = error
    parts = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.extend([str(current), str(getattr(current, "message", "")), str(getattr(current, "body", ""))])
        current = current.__cause__ or current.__context__
    text = " ".join(parts).lower()
    return any(code in text for code in (
        "freeusagelimiterror", "free usage limit reached", "promotion_model_limit_reached",
        "insufficient_quota", "usage_limit_reached",
    ))


def mark_exhausted(provider: Any, model: Any) -> None:
    with _LOCK:
        _BLOCKED[_key(provider, model)] = (time.monotonic() + QUOTA_COOLDOWN_SECONDS,
                                         "上游免费额度已耗尽，冷却后重新探测；可配置有效认证凭据")


def mark_rate_limited(provider: Any, model: Any, error: Exception) -> None:
    """普通 429 也跨任务退避，尊重上游 Retry-After，避免并发任务继续打满。"""
    delay = 30.0
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, 'response', None)
        headers = getattr(response, 'headers', {}) or {}
        try:
            delay = max(delay, min(3600.0, float(headers.get('retry-after', '0'))))
        except (TypeError, ValueError):
            pass
        current = current.__cause__ or current.__context__
    with _LOCK:
        key = _key(provider, model)
        until = max(time.monotonic()+delay, _BLOCKED.get(key, (0.0,''))[0])
        _BLOCKED[key] = (until, '上游请求限流，等待退避后恢复')


def blocked_reason(provider: Any, model: Any) -> str:
    key = _key(provider, model)
    with _LOCK:
        entry = _BLOCKED.get(key)
        if entry is None:
            return ""
        if entry[0] <= time.monotonic():
            del _BLOCKED[key]
            return ""
        return entry[1]


def remaining_seconds(provider: Any, model: Any) -> float:
    with _LOCK:
        entry = _BLOCKED.get(_key(provider, model))
        return max(0.0, entry[0] - time.monotonic()) if entry else 0.0


def task_retry_after(model_config: Any, task_name: str) -> float:
    """读取当前配置；换凭据、增加可用线路后立即结束等待。"""
    task = getattr(model_config.model_task_config, task_name, None)
    names = getattr(task, 'model_list', [])
    models = {m.name: m for m in model_config.models}
    providers = {p.name: p for p in model_config.api_providers}
    delays = []
    for name in names:
        model = models.get(name)
        provider = providers.get(model.api_provider) if model else None
        if provider is None:
            return 0.0
        delay = remaining_seconds(provider, model)
        if delay <= 0:
            return 0.0
        delays.append(delay)
    return min(delays) if delays else 0.0
