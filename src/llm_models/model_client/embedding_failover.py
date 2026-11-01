"""Transport failover for explicitly verified, identical embedding endpoints.

The model identity stays unchanged. Different embedding models must never be
put in this list, even when they happen to return the same vector dimension.
"""
from __future__ import annotations

import threading
import time

import httpx


class EmbeddingFailoverTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url, fallback_urls, *, transport=None, verify=True):
        self._base = httpx.URL(str(base_url).rstrip('/'))
        self._routes = [self._base, *(httpx.URL(str(url).rstrip('/')) for url in fallback_urls)]
        self._transport = transport or httpx.AsyncHTTPTransport(verify=verify)
        self._lock = threading.Lock()
        self._unavailable = {}

    async def handle_async_request(self, request):
        prefix = self._base.path.rstrip('/')
        if (request.url.scheme, request.url.host, request.url.port) != (self._base.scheme, self._base.host, self._base.port) or request.url.path != prefix + '/embeddings':
            return await self._transport.handle_async_request(request)
        body = await request.aread()
        with self._lock:
            routes = [route for route in self._routes if self._unavailable.get(str(route), 0) <= time.monotonic()]
        # Always permit recovery when every route was temporarily unavailable.
        if not routes:
            routes = self._routes
        for index, route in enumerate(routes):
            headers = request.headers.copy()
            headers.pop('host', None)
            outgoing = httpx.Request(request.method,
                route.copy_with(path=route.path.rstrip('/') + '/embeddings', query=request.url.query),
                headers=headers, content=body, extensions=request.extensions)
            try:
                response = await self._transport.handle_async_request(outgoing)
            except (httpx.TransportError, OSError):
                with self._lock:
                    self._unavailable[str(route)] = time.monotonic() + 60
                if index + 1 == len(routes):
                    raise
                continue
            if response.status_code >= 500 and index + 1 < len(routes):
                await response.aclose()
                with self._lock:
                    self._unavailable[str(route)] = time.monotonic() + 60
                continue
            # A 400/401/403/429 is authoritative, not a reason to bypass limits.
            return response
        raise RuntimeError('No embedding route configured')

    async def aclose(self):
        await self._transport.aclose()
