"""主运行时事件循环桥接工具。"""

from typing import Awaitable, Optional, TypeVar

import asyncio

T = TypeVar("T")

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """记录主运行时事件循环，供其他线程安全投递协程。"""
    global _main_loop
    _main_loop = loop


def get_main_loop() -> Optional[asyncio.AbstractEventLoop]:
    """获取当前主运行时事件循环。"""
    return _main_loop


async def run_on_main_loop(coro: Awaitable[T]) -> T:
    """在主运行时事件循环执行协程；已在主循环时直接 await。"""
    loop = get_main_loop()
    current_loop = asyncio.get_running_loop()
    if loop is None or loop.is_closed() or loop is current_loop:
        return await coro

    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return await asyncio.wrap_future(future)
