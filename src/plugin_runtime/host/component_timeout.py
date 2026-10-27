"""Shared timeout helpers for plugin runtime components."""

from typing import Any


DEFAULT_COMPONENT_RPC_TIMEOUT_MS = 60000


def normalize_component_timeout_ms(raw_value: Any) -> int:
    """Normalize component timeout metadata.

    Returns ``0`` when the component did not declare a timeout, so callers can
    fall back to their own default.
    """

    try:
        timeout_ms = int(raw_value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"component timeout_ms is invalid: {raw_value}") from exc
    if timeout_ms < 0:
        raise ValueError(f"component timeout_ms cannot be negative: {raw_value}")
    return timeout_ms


def resolve_component_rpc_timeout_ms(
    raw_value: Any,
    default_ms: int = DEFAULT_COMPONENT_RPC_TIMEOUT_MS,
) -> int:
    """Resolve a component timeout into the concrete RPC timeout in milliseconds."""

    timeout_ms = normalize_component_timeout_ms(raw_value)
    return timeout_ms or default_ms
