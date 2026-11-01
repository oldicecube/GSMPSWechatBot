"""Prepare the official native QQ adapter; never starts/restarts a bot or logs in."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import importlib
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from types import ModuleType

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PLUGIN_ID = 'maibot-team.snowluma-adapter'
ADAPTER = Path('plugins/MaiBot-SnowLuma-Adapter')
NAPCAT = Path('depends-data/napcat')
WS_PORT = 13001  # 3001 already belongs to the existing WeFlow service.


async def resolve_group_names(names, *, root=ROOT, expected_account=''):
    """Read logged-in identity and exact group names. Never sends chat messages."""
    from aiohttp import ClientSession, ClientTimeout
    import tomllib
    client = tomllib.loads((root / ADAPTER / 'config.toml').read_text(encoding='utf8'))['client']
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.ws_connect(f"ws://{client['server']}:{client['port']}",
                headers={'Authorization':'Bearer '+client['token']}) as ws:
            async def call(action):
                await ws.send_json({'action':action,'params':{},'echo':action})
                async with asyncio.timeout(10):
                    async for message in ws:
                        value = json.loads(message.data)
                        if value.get('echo') == action:
                            if value.get('retcode') != 0:
                                raise ValueError('NapCat read-only query failed: '+action)
                            return value.get('data')
                raise ValueError('No response from NapCat: '+action)
            account = str((await call('get_login_info') or {}).get('user_id') or '')
            if not account.isascii() or not account.isdecimal() or int(account)<=0 or (expected_account and account!=expected_account):
                raise ValueError('NapCat is not logged into the expected QQ account')
            rows = await call('get_group_list')
            groups=[]
            for name in names:
                matches={str(r.get('group_id') or '') for r in rows or [] if r.get('group_name')==name}
                if len(matches)!=1:
                    raise ValueError('Group name is missing or ambiguous; supply --groups with the actual ID: '+name)
                groups.append(matches.pop())
            return account, groups



def load_settings(path):
    name = '_napcat_setup_adapter'
    pkg = ModuleType(name)
    pkg.__path__ = [str(path.resolve())]
    sys.modules[name] = pkg
    return importlib.import_module(name + '.config').QQPluginSettings


def prepare(root=ROOT, *, source=None, account='', groups=(), owner='', enable=False):
    if any(not value.isdigit() or not 5 <= len(value) <= 12 for value in [account, owner, *groups] if value):
        raise ValueError('QQ account, owner and group IDs must be 5..12 decimal digits')
    if enable and (not account or not groups):
        raise ValueError('Enabling requires an actual bot QQ account and explicit target groups')
    dest = root / ADAPTER
    napcat = root / NAPCAT
    cfg_path = dest / 'config.toml'
    if not dest.exists() and (not source or not (source / '_manifest.json').is_file()):
        raise ValueError('Provide the extracted official v1.1.9 adapter with --source')
    backup = root / 'temp' / ('napcat-config-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for rel in [ADAPTER / 'config.toml', Path('config/adapter_policy.toml'), Path('config/bot_config.toml'),
                NAPCAT / 'config/onebot11.json', NAPCAT / 'config/webui.json', NAPCAT / 'config/maibot-launch.json']:
        if (root / rel).exists():
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, target)
    # Write disabled configuration before publishing the plugin into a watched directory.
    settings_type = load_settings(dest if dest.exists() else source)
    raw = tomlkit.parse(cfg_path.read_text(encoding='utf8')) if cfg_path.exists() else settings_type().model_dump()
    raw['plugin']['enabled'] = False
    raw['debug']['ignore_self_message'] = True
    raw['client'].update(client_type='napcat', server='127.0.0.1', port=WS_PORT,
                         connection_id='napcat-local', token=raw['client'].get('token') or secrets.token_urlsafe(32))
    settings_type.model_validate(dict(raw))
    # Validate existing account wiring before publishing any changed state.
    if account and enable:
        existing_path = napcat / ('config/onebot11_' + account + '.json')
        if existing_path.exists():
            existing = json.loads(existing_path.read_text(encoding='utf8'))
            if not any(x.get('enable') and x.get('host')=='127.0.0.1' and x.get('port')==WS_PORT
                       and x.get('token')==raw['client']['token'] for x in existing.get('network', {}).get('websocketServers', [])):
                raise ValueError('Existing per-account OneBot config differs; reconcile its WS server/token first')
    if not dest.exists():
        (root / 'temp').mkdir(parents=True, exist_ok=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='qq-plugin-stage-', dir=root / 'temp') as temp:
            staged = Path(temp) / 'adapter'
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'config.toml'))
            (staged / 'config.toml').write_text(tomlkit.dumps(raw), encoding='utf8')
            staged.replace(dest)
    else:
        cfg_path.write_text(tomlkit.dumps(raw), encoding='utf8')
    token = raw['client']['token']
    (napcat / 'config').mkdir(parents=True, exist_ok=True)
    onebot = dict(network=dict(httpServers=[], httpSseServers=[], httpClients=[], websocketClients=[], plugins=[],
        websocketServers=[dict(name='maibot-native', enable=True, host='127.0.0.1', port=WS_PORT,
            messagePostFormat='array', reportSelfMessage=False, token=token, enableForcePushEvent=True,
            debug=False, heartInterval=30000)]), musicSignUrl='', enableLocalFile2Url=False, parseMultMsg=False)
    # The generic file is the supported default for the first login. Do not replace
    # existing per-account OneBot connections without an explicit operator edit.
    (napcat / 'config/onebot11.json').write_text(json.dumps(onebot, indent=2) + '\n', encoding='utf8')
    if account:
        (napcat / 'config/maibot-launch.json').write_text(json.dumps({'account':account, 'autostart':enable}) + '\n', encoding='utf8')
        account_path = napcat / ('config/onebot11_' + account + '.json')
        if not account_path.exists():
            account_path.write_text(json.dumps(onebot, indent=2) + '\n', encoding='utf8')
        else:
            existing = json.loads(account_path.read_text(encoding='utf8'))
            matches = [x for x in existing.get('network', {}).get('websocketServers', [])
                       if x.get('enable') and x.get('host') == '127.0.0.1' and x.get('port') == WS_PORT and x.get('token') == token]
            if enable and not matches:
                raise ValueError('Existing per-account OneBot config differs; reconcile its WS server/token first')
    webui_path = napcat / 'config/webui.json'
    webui = json.loads(webui_path.read_text(encoding='utf8')) if webui_path.exists() else {}
    webui.update(host='127.0.0.1', port=6099, token=webui.get('token') or secrets.token_urlsafe(32), loginRate=3)
    webui_path.write_text(json.dumps(webui, indent=2) + '\n', encoding='utf8')
    from src.platform_io.adapter_policy import AdapterIdentity, AdapterPolicyManager
    manager = AdapterPolicyManager(root / 'config/adapter_policy.toml')
    # Apply to this QQ plugin only: existing WeChat policies keep their precedence.
    manager.set_adapter_policy(AdapterIdentity(plugin_id=PLUGIN_ID, platform='qq'), {
        'group': dict(default_action='block', allow_ids=list(groups), deny_ids=[]),
        'private': dict(default_action='block', allow_ids=[owner] if owner else [], deny_ids=[]),
    })
    if account:
        bot_path = root / 'config/bot_config.toml'
        bot = tomlkit.parse(bot_path.read_text(encoding='utf8'))
        accounts = list(bot['bot'].get('platforms', []))
        if 'qq:' + account not in accounts:
            bot['bot']['platforms'] = accounts + ['qq:' + account]
            bot_path.write_text(tomlkit.dumps(bot), encoding='utf8')
    raw['plugin']['enabled'] = enable
    cfg_path.write_text(tomlkit.dumps(raw), encoding='utf8')
    return dict(adapter=str(dest), napcat=str(napcat), enabled=enable, account_configured=bool(account),
                group_count=len(groups), private_owner_configured=bool(owner), ws=f'ws://127.0.0.1:{WS_PORT}',
                webui='http://127.0.0.1:6099/webui', backup=str(backup), restarted=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path)
    p.add_argument('--account', default='')
    p.add_argument('--groups', nargs='*', default=[])
    p.add_argument('--group-name', action='append', default=[], help='Resolve an exact group name through the logged-in local NapCat')
    p.add_argument('--owner', default='')
    p.add_argument('--enable', action='store_true')
    args = p.parse_args()
    if args.group_name:
        try:
            args.account, resolved = asyncio.run(resolve_group_names(args.group_name, expected_account=args.account))
            args.groups = list(dict.fromkeys([*args.groups, *resolved]))
        except Exception as exc:
            p.exit(1, 'QQ target was not changed: '+str(exc)+'\nComplete local QR login first.\n')
    print(json.dumps(prepare(source=args.source, account=args.account, groups=args.groups, owner=args.owner, enable=args.enable)))


if __name__ == '__main__':
    main()
