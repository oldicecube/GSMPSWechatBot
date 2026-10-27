"""MCP 宿主回调声明。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class MCPHostCallbacks:
    """MCP 宿主回调集合。

    该对象用于向 `MCPConnection` 注入宿主侧可选能力，
    例如 Sampling、Elicitation、日志消费和自定义消息处理。
    """

    sampling_callback: Callable[..., Awaitable[Any]] | None = None
    elicitation_callback: Callable[..., Awaitable[Any]] | None = None
    logging_callback: Callable[..., Awaitable[None]] | None = None
    message_handler: Callable[..., Awaitable[None]] | None = None
