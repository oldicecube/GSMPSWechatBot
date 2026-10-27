"""传输层工厂

根据运行平台自动选择最优传输实现。
"""

from pathlib import Path
from typing import Optional

import sys

from .base import TransportClient, TransportServer


def create_transport_server(socket_path: Optional[str] = None) -> TransportServer:
    """创建传输服务端

    Linux/macOS 使用 UDS，Windows 使用 Named Pipe。

    Args:
        socket_path: UDS socket 路径或 Windows pipe 名称
    """
    if sys.platform != "win32":
        from .uds import UDSTransportServer

        return UDSTransportServer(socket_path=Path(socket_path) if socket_path is not None else None)
    else:
        from .named_pipe import NamedPipeTransportServer

        return NamedPipeTransportServer(pipe_name=socket_path)


def create_transport_client(address: str) -> TransportClient:
    """创建传输客户端

    根据地址格式自动判断传输类型：
    - 以 '\\\\.\\pipe\\' 开头 -> Windows Named Pipe
    - 包含 '/' 或 '.sock' -> UDS
    - 包含 ':' -> TCP

    Args:
        address: Host 端监听地址
    """
    if address.startswith("\\\\.\\pipe\\"):
        from .named_pipe import NamedPipeTransportClient

        return NamedPipeTransportClient(address)
    if "/" in address or address.endswith(".sock"):
        from .uds import UDSTransportClient

        return UDSTransportClient(socket_path=Path(address))
    elif ":" in address:
        from .tcp import TCPTransportClient

        host, port_str = address.rsplit(":", 1)
        return TCPTransportClient(host=host, port=int(port_str))
    else:
        raise ValueError(f"无法识别的传输地址格式: {address}")
