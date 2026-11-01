"""Restrict HTTP and WebSocket peers independently of crawler exemptions."""
from ipaddress import ip_address, ip_network

from starlette.responses import JSONResponse

from src.config.config import global_config


def auth_enabled() -> bool:
    return getattr(global_config.webui, "auth_enabled", True)


class NetworkAccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        networks = getattr(global_config.webui, "allowed_networks", [])
        allowed = not networks and auth_enabled()
        if networks:
            try:
                peer = ip_address((scope.get("client") or ("", 0))[0])
                if getattr(peer, "ipv4_mapped", None):
                    peer = peer.ipv4_mapped
                allowed = any(peer in ip_network(value, strict=False) for value in networks)
            except (ValueError, TypeError):
                allowed = False
        if not allowed:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008, "reason": "Network not allowed"})
            return await JSONResponse({"detail": "Network not allowed"}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
