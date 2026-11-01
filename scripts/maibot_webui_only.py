"""Temporary read-only native dashboard. Never starts a chat core or plugins.

Exits when the existing bot process exits, handing the port to the next bot start.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
from starlette.responses import JSONResponse


class ReadOnlyDashboard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1013,
                        'reason': 'Temporary read-only dashboard; restart bot for live controls'})
            return
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}:
            return await JSONResponse({'detail': '临时只读 WebUI；请手动重启 bot 后使用完整控制功能'},
                                      status_code=503)(scope, receive, send)
        if scope['path'] == '/api/webui/service-mode':
            return await JSONResponse({'mode': 'temporary-read-only', 'live_core_attached': False})(scope, receive, send)
        # Show an explicit banner on SPA pages rather than pretending this process
        # has access to the existing bot's in-memory state.
        html = not scope['path'].startswith(('/api/', '/assets/')) and '.' not in scope['path']
        if html:
            scope = dict(scope, headers=[(k, v) for k, v in scope['headers'] if k.lower() != b'accept-encoding'])
        banner = ('<div style="position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                  'background:#fff3cd;color:#664d03;padding:8px;text-align:center">'
                  '临时只读 WebUI：配置和历史数据可查看。手动重启 bot 后，自动切换为完整管理界面。'
                  '</div>').encode('utf-8')

        async def send_response(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.append((b'x-maibot-webui-mode', b'temporary-read-only'))
                if html:
                    headers = [(k, v) for k, v in headers if k.lower() != b'content-length']
                message = dict(message, headers=headers)
            elif html and message['type'] == 'http.response.body' and not message.get('more_body', False):
                message = dict(message, body=message.get('body', b'') + banner)
            await send(message)
        await self.app(scope, receive, send_response)


async def main(bot_pid: int, bot_created: float):
    from src.config.config import global_config
    from src.webui.middleware.network_access import NetworkAccessMiddleware
    from src.webui.webui_server import WebUIServer

    process = psutil.Process(bot_pid)
    if abs(process.create_time() - bot_created) > 0.01:
        raise RuntimeError('Bot PID has been reused')
    if not global_config.webui.enabled:
        raise RuntimeError('WebUI is disabled')
    server = WebUIServer(hosts=global_config.webui.host, port=global_config.webui.port,
                         register_config_reload=False)
    # Guard even the temporary status endpoint, before applying the mode wrapper.
    server.app = NetworkAccessMiddleware(ReadOnlyDashboard(server.app))
    serving = asyncio.create_task(server.start())
    try:
        while not serving.done() and process.is_running():
            await asyncio.sleep(0.5)
    finally:
        await server.shutdown()
        await serving


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--until-bot-pid', type=int, required=True)
    parser.add_argument('--bot-created', type=float, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.until_bot_pid, args.bot_created))
