"""Windows Named Pipe 传输实现。

适用于 Windows 平台，使用 asyncio ProactorEventLoop 的 named pipe 支持。

注意：Named Pipe 是 Windows 特有的 IPC 机制，
在 Linux/macOS 平台上不可用。Unix-like 平台请使用 UDS 传输。
"""

from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, cast

import asyncio
import os
import re
import sys
import uuid

from .base import Connection, ConnectionHandler, TransportClient, TransportServer

_PIPE_PREFIX = "\\\\.\\pipe\\"
_DEFAULT_PIPE_PREFIX = "maibot-plugin"


class _NamedPipeServerHandle(Protocol):
    """Named Pipe 服务端句柄的协议定义。"""
    def close(self) -> None: ...


class _NamedPipeEventLoop(Protocol):
    """ProactorEventLoop 的协议定义，提供 named pipe 相关方法。"""
    async def start_serving_pipe(
        self,
        protocol_factory: Callable[[], asyncio.BaseProtocol],
        address: str,
    ) -> List[_NamedPipeServerHandle]: ...

    async def create_pipe_connection(
        self,
        protocol_factory: Callable[[], asyncio.BaseProtocol],
        address: str,
    ) -> Tuple[asyncio.BaseTransport, asyncio.BaseProtocol]: ...

    def call_exception_handler(self, context: Dict[str, Any]) -> None: ...

    def create_task(self, coro: Any) -> asyncio.Task[None]: ...


def _normalize_pipe_address(pipe_name: Optional[str] = None) -> str:
    """规范化 Named Pipe 地址。
    
    Args:
        pipe_name: 管道名称。如果以 '\\\\.\\pipe\\' 开头则直接使用，
                   否则会自动添加前缀。如果为 None 则生成随机名称。
    
    Returns:
        规范化的管道地址（格式：\\\\.\\pipe\\name）
    """
    if pipe_name and pipe_name.startswith(_PIPE_PREFIX):
        return pipe_name

    if pipe_name:
        sanitized_name = re.sub(r"[^0-9A-Za-z._-]+", "-", pipe_name).strip("-.")
    else:
        sanitized_name = f"{_DEFAULT_PIPE_PREFIX}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    if not sanitized_name:
        sanitized_name = f"{_DEFAULT_PIPE_PREFIX}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    return f"{_PIPE_PREFIX}{sanitized_name}"


class NamedPipeConnection(Connection):
    """基于 Windows Named Pipe 的连接。
    
    封装了底层 StreamReader/StreamWriter，提供分帧读写能力。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        super().__init__(reader, writer)


class _NamedPipeServerProtocol(asyncio.StreamReaderProtocol):
    """Named Pipe 服务端协议实现。
    
    处理客户端连接的生命周期，包括连接建立、数据处理和连接关闭。
    """
    
    def __init__(self, handler: ConnectionHandler, loop: asyncio.AbstractEventLoop) -> None:
        self._reader: asyncio.StreamReader = asyncio.StreamReader()
        super().__init__(self._reader)
        self._handler: ConnectionHandler = handler
        self._loop: asyncio.AbstractEventLoop = loop
        self._handler_task: Optional[asyncio.Task[None]] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """连接建立时的回调。"""
        super().connection_made(transport)
        writer = asyncio.StreamWriter(cast(asyncio.WriteTransport, transport), self, self._reader, self._loop)
        connection = NamedPipeConnection(self._reader, writer)
        # 使用 asyncio.create_task 确保任务正确调度
        self._handler_task = asyncio.create_task(self._run_handler(connection))
        self._handler_task.add_done_callback(self._on_handler_done)

    async def _run_handler(self, connection: NamedPipeConnection) -> None:
        """运行连接处理器。"""
        try:
            await self._handler(connection)
        finally:
            await connection.close()

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        """连接处理器完成时的回调。"""
        if task.cancelled():
            return
        if exc := task.exception():
            try:
                self._loop.call_exception_handler(
                    {
                        "message": "Named pipe 连接处理失败",
                        "exception": exc,
                        "protocol": self,
                    }
                )
            except Exception:
                # 如果 loop 已经关闭，忽略异常
                pass


class NamedPipeTransportServer(TransportServer):
    """Windows Named Pipe 传输服务端。
    
    使用 ProactorEventLoop 的 start_serving_pipe 方法监听客户端连接。
    """

    def __init__(self, pipe_name: Optional[str] = None) -> None:
        self._address: str = _normalize_pipe_address(pipe_name)
        self._servers: List[_NamedPipeServerHandle] = []

    async def start(self, handler: ConnectionHandler) -> None:
        """启动 Named Pipe 服务端。
        
        Args:
            handler: 新连接到来时的回调函数
            
        Raises:
            RuntimeError: 当在非 Windows 平台或事件循环不支持时
        """
        if sys.platform != "win32":
            raise RuntimeError("Named pipe 仅支持 Windows")

        loop = asyncio.get_running_loop()
        if not hasattr(loop, "start_serving_pipe"):
            raise RuntimeError("当前事件循环不支持 Windows named pipe")
        pipe_loop = cast(_NamedPipeEventLoop, loop)

        self._servers = await pipe_loop.start_serving_pipe(
            lambda: _NamedPipeServerProtocol(handler, loop),
            self._address,
        )

    async def stop(self) -> None:
        """停止 Named Pipe 服务端并清理资源。"""
        for server in self._servers:
            server.close()
        # 等待所有服务器句柄完全关闭
        await asyncio.gather(
            *[asyncio.sleep(0.1) for _ in self._servers],
            return_exceptions=True
        )
        self._servers.clear()

    def get_address(self) -> str:
        return self._address


class NamedPipeTransportClient(TransportClient):
    """Windows Named Pipe 传输客户端。
    
    用于主动连接到 Named Pipe 服务端。
    """

    def __init__(self, address: str) -> None:
        self._address: str = _normalize_pipe_address(address)

    async def connect(self) -> Connection:
        """建立到 Named Pipe 服务端的连接。
        
        Returns:
            NamedPipeConnection: 连接对象
            
        Raises:
            NotImplementedError: 当在非 Windows 平台或事件循环不支持时
        """
        if sys.platform != "win32":
            raise NotImplementedError("Named pipe 仅支持 Windows")

        loop = asyncio.get_running_loop()
        if not hasattr(loop, "create_pipe_connection"):
            raise NotImplementedError("当前事件循环不支持 Windows named pipe")
        pipe_loop = cast(_NamedPipeEventLoop, loop)

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _protocol = await pipe_loop.create_pipe_connection(lambda: protocol, self._address)
        # 使用返回的 protocol 创建 StreamWriter
        writer = asyncio.StreamWriter(cast(asyncio.WriteTransport, transport), _protocol, reader, loop)
        return NamedPipeConnection(reader, writer)