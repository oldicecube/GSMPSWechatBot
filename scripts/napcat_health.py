"""Read-only NapCat connection check. Does not read chat history or send messages."""
import asyncio
import json
from pathlib import Path
import sys
import tomllib

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parents[1]


async def main():
    cfg = tomllib.loads((ROOT / 'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_text(encoding='utf8'))
    client = cfg['client']
    report = dict(enabled=cfg['plugin']['enabled'], ws=f"ws://{client['server']}:{client['port']}",
                  logged_in=False, physical_messages=False)
    try:
        async with ClientSession(timeout=ClientTimeout(total=12)) as session:
            async with session.ws_connect(report['ws'], headers={'Authorization': 'Bearer ' + client['token']}) as ws:
                async def call(action):
                    await ws.send_json(dict(action=action, params={}, echo=action))
                    async with asyncio.timeout(10):
                        async for message in ws:
                            data = json.loads(message.data)
                            if data.get('echo') == action:
                                if data.get('retcode') != 0:
                                    raise RuntimeError('OneBot returned retcode ' + str(data.get('retcode')))
                                return data.get('data') or {}
                    raise RuntimeError('No matching OneBot response')
                version = await call('get_version_info')
                login = await call('get_login_info')
                status = await call('get_status')
                report.update(connected=True, client=version.get('app_name'), version=version.get('app_version'),
                              account=str(login.get('user_id') or ''), logged_in=bool(login.get('user_id')),
                              online=status.get('online'))
    except Exception as exc:
        report.update(connected=False, error=type(exc).__name__, hint='Start NapCat and complete QR login first.')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('connected') and report['logged_in'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
