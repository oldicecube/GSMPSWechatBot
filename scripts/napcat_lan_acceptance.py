"""Passwordless LAN HTTP/WebSocket proxy contracts against a local fake upstream."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer,TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.napcat_lan_webui import create_app,permitted


class LAN(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen=[]
        async def upstream(request):
            self.seen.append((request.path,request.headers.get('Authorization'),request.headers.get('X-Forwarded-For')))
            if request.path=='/ws':
                self.seen.append(('ws_token',request.query.get('token')))
                socket=web.WebSocketResponse();await socket.prepare(request)
                async for message in socket:
                    await socket.send_str(message.data)
                return socket
            if request.path=='/webui/':return web.Response(text='<html><head></head><body>NapCat</body></html>',content_type='text/html')
            if request.path=='/upload':return web.Response(body=await request.read())
            return web.json_response({'code':0,'data':{'isLogin':True}})
        app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
        self.upstream=TestServer(app);await self.upstream.start_server()
        class Login:
            credential=''
            def status(inner):inner.credential='fixture-session';return {'isLogin':True}
        login=Login();login.base=str(self.upstream.make_url('')).rstrip('/')
        self.client=TestClient(TestServer(create_app(login)));await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();await self.upstream.close()

    async def test_no_auth_header_needed_for_api(self):
        response=await self.client.post('/api/QQLogin/CheckLoginStatus')
        self.assertEqual(response.status,200)
        self.assertEqual(self.seen[-1][1],'Bearer fixture-session')

    async def test_html_bootstraps_native_session_without_password_prompt(self):
        response=await self.client.get('/webui/')
        text=await response.text()
        self.assertIn('localStorage.setItem("token",JSON.stringify("fixture-session"))',text)
        self.assertEqual(response.headers.get('Cache-Control'),'no-store')

    async def test_websocket_no_client_auth_and_replaces_stale_token(self):
        socket=await self.client.ws_connect('/ws?token=stale')
        await socket.send_str('echo');message=await socket.receive(timeout=2)
        self.assertEqual(message.data,'echo')
        self.assertIn(('ws_token','fixture-session'),self.seen)
        await socket.close()

    async def test_upload_stream_preserved(self):
        response=await self.client.post('/upload',data=b'fixture\x00bytes')
        self.assertEqual(await response.read(),b'fixture\x00bytes')

    async def test_cross_origin_rejected(self):
        response=await self.client.post('/api/test',headers={'Origin':'https://external.invalid'})
        self.assertEqual(response.status,403);self.assertFalse(self.seen)

    async def test_remote_check_ignores_forged_forwarded_headers(self):
        with patch('scripts.napcat_lan_webui.permitted',return_value=False):
            response=await self.client.get('/api/test',headers={'X-Forwarded-For':'192.168.31.100'})
        self.assertEqual(response.status,403);self.assertFalse(self.seen)

    async def test_client_auth_and_forwarded_headers_do_not_reach_backend(self):
        response=await self.client.get('/api/test',headers={'Authorization':'Bearer wrong','X-Forwarded-For':'1.2.3.4'})
        self.assertEqual(response.status,200)
        self.assertEqual(self.seen[-1][1:3],('Bearer fixture-session',None))

    async def test_allowed_networks(self):
        for ip in ('192.168.31.1','192.168.31.254','127.0.0.1','::ffff:192.168.31.12'):self.assertTrue(permitted(ip))
        for ip in ('192.168.32.1','8.8.8.8','bad'):self.assertFalse(permitted(ip))

    async def test_native_logout_still_allows_passwordless_login(self):
        response=await self.client.post('/api/auth/logout');self.assertEqual(response.status,200)
        response=await self.client.post('/api/auth/login');self.assertEqual((await response.json())['data']['Credential'],'fixture-session')


if __name__=='__main__':unittest.main(verbosity=2)
