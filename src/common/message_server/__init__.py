"""Maim Message - A message handling library"""

from typing import TYPE_CHECKING

__version__ = "0.2.0"

if TYPE_CHECKING:
    from maim_message import MessageServer


def get_global_api() -> "MessageServer":
    """懒加载 maim_message，避免主程序唤醒前阻塞在消息库导入上。"""

    from .api import get_global_api as _get_global_api

    return _get_global_api()


__all__ = ["get_global_api"]
