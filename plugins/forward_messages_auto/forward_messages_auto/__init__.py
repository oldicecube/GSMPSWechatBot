"""麦麦自主跨群转发插件的内部实现。"""

from .config import (
    CONFIG_VERSION,
    PLUGIN_VERSION,
    ForwardMessagesAutoConfig,
)
from .runtime import ForwardingRuntime

__all__ = [
    "CONFIG_VERSION",
    "PLUGIN_VERSION",
    "ForwardMessagesAutoConfig",
    "ForwardingRuntime",
]
