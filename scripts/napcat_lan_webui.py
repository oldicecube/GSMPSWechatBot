"""LAN-only, passwordless NapCat entry; upstream remains on loopback.

No QQ/bot process restarts or vendor bundle patches. Credentials stay managed
by the local bridge, not entered by a human. The original frontend gets a
short-lived session automatically, and API/WebSocket requests are proxied.
"""
import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.napcat_login_watch import LoginClient

NETWORK=ipaddress.ip_network('192.168.31.0/24')
HOP={'connection','keep-alive','transfer-encoding','upgrade','proxy-authenticate','proxy-authorization','te','trailer','content-length','content-encoding'}


def permitted(remote):
    try:
        address=ipaddress.ip_address(remote)
        address=getattr(address,'ipv4_mapped',None) or address
        return address.is_loopback or address in NETWORK
    except ValueError:
        return False


def lan_address():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
        client.connect(('192.168.31.1',9))
        address=client.getsockname()[0]
    if ipaddress.ip_address(address) not in NETWORK:
        raise RuntimeError('No address on configured LAN')
    return address


class Bridge:
    def __init__(self, login):
        self.login=login
        self.updated=0
        self.lock=asyncio.Lock()
        self.client=None

    async def credential(self):
        async with self.lock:
            if not self.login.credential or time.monotonic()-self.updated>2400:
                self.login.credential=''
                await asyncio.to_thread(self.login.status)
                self.updated=time.monotonic()
            return self.login.credential

    async def handle(self, request):
        if not permitted(request.remote or ''):
            raise web.HTTPForbidden(text='LAN access only')
        origin=request.headers.get('Origin')
        if origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc!=request.host:
                raise web.HTTPForbidden(text='Cross-origin access denied')
        try:
            credential=await self.credential()
        except Exception:
            raise web.HTTPServiceUnavailable(text='NapCat WebUI is not ready')
        # Logging out of a passwordless entry does not revoke the bridge session.
        if request.path in {'/api/auth/logout','/api/auth/login'}:
            return web.json_response({'code':0,'data':{'Credential':credential}})
        headers={k:v for k,v in request.headers.items() if k.lower() not in HOP|{'host','authorization','origin','x-forwarded-for','x-real-ip'}}
        headers['Authorization']='Bearer '+credential
        if origin:
            headers['Origin']=self.login.base
        target=self.login.base+str(request.rel_url)
        if request.headers.get('Upgrade','').lower()=='websocket':
            return await self.websocket(request,target,headers,credential)
        async with self.client.request(request.method,target,headers=headers,
                data=request.content.iter_chunked(65536) if request.can_read_body else None,
                allow_redirects=False) as response:
            response_headers={k:v for k,v in response.headers.items() if k.lower() not in HOP}
            if 'text/html' in response.headers.get('Content-Type',''):
                html=await response.text()
                script='<script>localStorage.setItem("token",JSON.stringify('+json.dumps(credential)+'));</script>'
                html=html.replace('<head>','<head>'+script,1)
                response_headers['Cache-Control']='no-store'
                return web.Response(body=html.encode('utf8'),status=response.status,headers=response_headers)
            downstream=web.StreamResponse(status=response.status,headers=response_headers)
            await downstream.prepare(request)
            async for chunk in response.content.iter_chunked(65536):
                await downstream.write(chunk)
            await downstream.write_eof()
            return downstream

    async def websocket(self, request, target, headers, credential):
        from yarl import URL
        url=URL(target)
        query=dict(url.query)
        if 'webui_token' in query:query['webui_token']=credential
        if 'token' in query:query['token']=credential
        async with self.client.ws_connect(url.with_query(query),headers=headers,max_msg_size=32*1024*1024) as upstream:
            downstream=web.WebSocketResponse(max_msg_size=32*1024*1024)
            await downstream.prepare(request)
            async def pump(source,destination):
                async for message in source:
                    if message.type==WSMsgType.TEXT:await destination.send_str(message.data)
                    elif message.type==WSMsgType.BINARY:await destination.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE,WSMsgType.ERROR}:break
            jobs=[asyncio.create_task(pump(upstream,downstream)),asyncio.create_task(pump(downstream,upstream))]
            done,pending=await asyncio.wait(jobs,return_when=asyncio.FIRST_COMPLETED)
            for job in pending:job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            await downstream.close()
            return downstream


def create_app(login=None):
    bridge=Bridge(login or LoginClient(ROOT))
    app=web.Application(client_max_size=80*1024*1024)
    async def lifecycle(app):
        async with ClientSession(timeout=ClientTimeout(total=None,connect=10,sock_read=None)) as client:
            bridge.client=client
            yield
    app.cleanup_ctx.append(lifecycle)
    app.router.add_route('*','/{tail:.*}',bridge.handle)
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='')
    args=parser.parse_args()
    config_path=ROOT/'depends-data/napcat/config/lan-webui.json'
    if not config_path.exists() or not json.loads(config_path.read_text(encoding='utf8')).get('enabled'):
        return 0
    host=args.host or lan_address()
    if ipaddress.ip_address(host) not in NETWORK:
        raise ValueError('Listener must be on the configured LAN')
    try:
        web.run_app(create_app(),host=host,port=6099,access_log=None,print=None)
    except OSError as exc:
        if exc.errno not in {48,98,10048}:raise
    return 0


if __name__=='__main__':raise SystemExit(main())
