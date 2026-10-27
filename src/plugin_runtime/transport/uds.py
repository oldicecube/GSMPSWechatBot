"""Unix Domain Socket 传输实现

适用于 Linux / macOS 平台。

注意：UDS (Unix Domain Socket) 是 Unix-like 系统特有的 IPC 机制，
在 Windows 平台上不可用。Windows 平台请使用 Named Pipe 传输。
"""

from pathlib import Path
from typing import Optional

import asyncio
import os
import sys
import tempfile

from .base import Connection, ConnectionHandler, TransportClient, TransportServer


class UDSConnection(Connection):
    """基于 UDS 的连接
    
    封装了底层 StreamReader/StreamWriter，提供分帧读写能力。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        super().__init__(reader, writer)


# Unix domain socket 路径的系统限制（sun_path 字段长度）
# Linux: 108 字节，macOS: 104 字节，其他 Unix: 通常 104 字节
if sys.platform == "linux":
    _UDS_PATH_MAX = 108
elif sys.platform == "darwin":  # macOS
    _UDS_PATH_MAX = 104
else:
    _UDS_PATH_MAX = 104  # 保守默认值


class UDSTransportServer(TransportServer):
    """UDS 传输服务端"""

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        if socket_path is None:
            # 默认放在临时目录，使用 uuid 确保同一进程多实例不碰撞
            import uuid

            socket_path = Path(tempfile.gettempdir()) / f"maibot-plugin-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"

            # 如果路径超出 UDS 限制，回退到更短的路径
            if len(str(socket_path).encode()) > _UDS_PATH_MAX:
                socket_path = Path("/tmp") / f"mb-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        if len(str(socket_path).encode()) > _UDS_PATH_MAX:
            raise OSError(f"UDS socket 路径过长 ({len(str(socket_path).encode())} > {_UDS_PATH_MAX} 字节): {socket_path}")

        self._socket_path: Path = socket_path
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self, handler: ConnectionHandler) -> None:
        """启动 UDS 服务端
        
        Args:
            handler: 新连接到来时的回调函数
            
        Raises:
            RuntimeError: 当在非 Unix 平台（如 Windows）上调用时
        """
        # 平台检查：UDS 仅在 Unix-like 系统上可用
        if sys.platform == "win32":
            raise RuntimeError("UDS 不支持 Windows 平台，请使用 Named Pipe")
        
        # 清理残留 socket 文件
        if self._socket_path.exists():
            self._socket_path.unlink()

        # 确保父目录存在
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            conn = UDSConnection(reader, writer)
            try:
                await handler(conn)
            finally:
                await conn.close()

        try:
            self._server = await asyncio.start_unix_server(_on_connect, path=str(self._socket_path))

            # 设置文件权限为仅当前用户可访问
            self._socket_path.chmod(0o600)
        except Exception:
            # 启动失败时清理可能创建的目录和 socket 文件
            if self._socket_path.exists():
                self._socket_path.unlink()
            raise

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # 清理 socket 文件
        if self._socket_path.exists():
            self._socket_path.unlink()

    def get_address(self) -> str:
        return str(self._socket_path)


class UDSTransportClient(TransportClient):
    """UDS 传输客户端
    
    用于主动连接到 UDS 服务端。
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path: Path = socket_path

    async def connect(self) -> Connection:
        """建立到 UDS 服务端的连接
        
        Returns:
            UDSConnection: 连接对象
            
        Raises:
            RuntimeError: 当在非 Unix 平台（如 Windows）上调用时
        """
        # 平台检查：UDS 仅在 Unix-like 系统上可用
        if sys.platform == "win32":
            raise RuntimeError("UDS 不支持 Windows 平台，请使用 Named Pipe")
        
        reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        return UDSConnection(reader, writer)
