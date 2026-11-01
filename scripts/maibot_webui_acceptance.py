"""Isolated HTTP/WS/auth regression checks; no production messages or writes."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
instance = Path(tempfile.mkdtemp(prefix='webui-acceptance-', dir=ROOT / 'temp'))
(instance / 'config').mkdir()
for name in ['bot_config.toml', 'model_config.toml']:
    shutil.copy2(ROOT / 'config' / name, instance / 'config' / name)
os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from src.config.config import global_config
from src.webui.app import create_app
from src.webui.core.auth import get_current_token, is_token_valid
from src.webui.middleware.network_access import NetworkAccessMiddleware
from src.webui.routers.plugin.support import require_plugin_token
from src.webui.routers.websocket.auth import generate_ws_token, verify_ws_token
from src.webui.routers.websocket.unified import authenticate_websocket_connection

results = []


def check(name, value):
    results.append({'name': name, 'passed': bool(value)})
    if not value:
        raise AssertionError(name)


app = create_app()
check('native API routes registered', len(app.openapi()['paths']) > 100)
with TestClient(app, client=('192.168.31.42', 12345)) as client:
    for path in ['/', '/api/webui/health', '/api/webui/auth/check', '/api/webui/setup/status',
                 '/api/webui/config/schema/bot']:
        response = client.get(path)
        check('LAN no-cookie GET ' + path, response.status_code == 200)
    check('frontend authenticated', client.get('/api/webui/auth/check').json()['authenticated'])
    check('no forced password setup', not client.get('/api/webui/setup/status').json()['requires_custom_token'])
    check('no forced first setup', not client.get('/api/webui/setup/status').json()['is_first_setup'])
    token = client.get('/api/webui/ws-token').json()
    check('no-cookie WS ticket', token['success'] and verify_ws_token(token['token']))
    check('WS ticket consumed once', not verify_ws_token(token['token']))
    check('thumbnail passes auth', client.get('/api/webui/emoji/999999/thumbnail').status_code == 404)
    with client.websocket_connect('/api/webui/ws') as websocket:
        check('native unified WS no-cookie handshake', websocket.receive_json()['event'] == 'ready')
check('plugin special auth bypass', require_plugin_token(None) == 'intranet-no-auth')
check('central auth bypass', is_token_valid(None) and get_current_token(None) == 'intranet-no-auth')
check('unified WS no-cookie auth', asyncio.run(authenticate_websocket_connection(type('WS', (), {'cookies': {}})(), None)))

for peer in ['127.0.0.1', '::1', '::ffff:192.168.31.42', '192.168.31.254']:
    with TestClient(app, client=(peer, 12345)) as client:
        check('allow peer ' + peer, client.get('/api/webui/health').status_code == 200)
for peer in ['192.168.32.1', '10.0.0.5', '203.0.113.5']:
    with TestClient(app, client=(peer, 12345)) as client:
        check('deny peer ' + peer, client.get('/api/webui/health').status_code == 403)
        check('ignore forged XFF ' + peer, client.get('/api/webui/health', headers={
            'x-forwarded-for': '192.168.31.42', 'forwarded': 'for=127.0.0.1'}).status_code == 403)
        try:
            with client.websocket_connect('/api/webui/ws'):
                check('deny foreign WS', False)
        except WebSocketDisconnect as exc:
            check('deny foreign WS ' + peer, exc.code == 1008)


async def echo_ws(scope, receive, send):
    if scope['type'] == 'lifespan':
        await receive()
        await send({'type': 'lifespan.startup.complete'})
        await receive()
        await send({'type': 'lifespan.shutdown.complete'})
        return
    await receive()
    await send({'type': 'websocket.accept'})
    await send({'type': 'websocket.close', 'code': 1000})


with TestClient(NetworkAccessMiddleware(echo_ws), client=('192.168.31.42', 12345)) as client:
    with client.websocket_connect('/test'):
        check('LAN WS admitted', True)

global_config.webui.allowed_networks = []
with TestClient(app, client=('192.168.31.42', 12345)) as client:
    check('no-auth empty ACL fails closed', client.get('/api/webui/health').status_code == 403)
global_config.webui.allowed_networks = ['not-a-network']
with TestClient(app, client=('192.168.31.42', 12345)) as client:
    check('invalid ACL fails closed', client.get('/api/webui/health').status_code == 403)
global_config.webui.allowed_networks = ['127.0.0.0/8', '192.168.31.0/24']
global_config.webui.auth_enabled = True
with TestClient(app, client=('192.168.31.42', 12345)) as client:
    check('password mode denies missing cookie', client.get('/api/webui/setup/status').status_code == 401)
    check('password mode reports logged out', not client.get('/api/webui/auth/check').json()['authenticated'])
    check('password mode refuses WS ticket', not client.get('/api/webui/ws-token').json()['success'])
check('password mode refuses WS auth', not asyncio.run(authenticate_websocket_connection(type('WS', (), {'cookies': {}})(), None)))

from scripts.maibot_webui_only import ReadOnlyDashboard
global_config.webui.auth_enabled = False
with TestClient(NetworkAccessMiddleware(ReadOnlyDashboard(app)), client=('192.168.31.42', 12345)) as client:
    check('temporary mode banner', '临时只读 WebUI' in client.get('/').text)
    check('temporary mode marks detached core', not client.get('/api/webui/service-mode').json()['live_core_attached'])
    check('temporary mode blocks writes', client.put('/api/webui/config/bot', json={}).status_code == 503)
    check('temporary mode blocks restart', client.post('/api/webui/system/restart').status_code == 503)

# Verify the temporary process releases its port, then start the real threaded
# WebUI on that same port. The watched process is an isolated dummy, never bot.
import socket
import subprocess
import time
import httpx
import psutil
import tomlkit
from src.webui.webui_server import ThreadedWebUIServer

with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
path = instance / 'config' / 'bot_config.toml'
document = tomlkit.parse(path.read_text(encoding='utf-8'))
document['webui']['host'] = ['127.0.0.1']
document['webui']['port'] = port
path.write_text(tomlkit.dumps(document), encoding='utf-8')
dummy = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
helper = None
server = None
with (instance / 'helper.log').open('w', encoding='utf-8') as log:
    try:
        helper = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/maibot_webui_only.py'),
            '--until-bot-pid', str(dummy.pid), '--bot-created', str(psutil.Process(dummy.pid).create_time())],
            stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with httpx.Client(trust_env=False, timeout=1) as client:
            def wait_http():
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        response = client.get(f'http://127.0.0.1:{port}/api/webui/health')
                        if response.status_code == 200:
                            return response
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                raise AssertionError('WebUI did not bind isolated port')
            check('temporary helper starts independently', wait_http().headers.get('x-maibot-webui-mode') == 'temporary-read-only')
            dummy.terminate()
            dummy.wait(timeout=5)
            check('temporary helper exits with watched process', helper.wait(timeout=10) == 0)
            server = ThreadedWebUIServer(hosts=['127.0.0.1'], port=port)
            server.start()
            check('native threaded UI takes over same port', 'x-maibot-webui-mode' not in wait_http().headers)
            check('native UI can issue WS tickets after handoff', client.get(f'http://127.0.0.1:{port}/api/webui/ws-token').json()['success'])
    finally:
        if server:
            asyncio.run(server.shutdown())
        for process in [helper, dummy]:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
report = ROOT / 'docs' / 'maibot-wechat-acceptance' / 'webui-lan-20261030.json'
report.write_text(json.dumps({'passed': len(results), 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results), 'report': str(report)}, ensure_ascii=False))
