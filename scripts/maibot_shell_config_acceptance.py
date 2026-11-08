"""Isolated acceptance of external plugin config, persistence and execution."""
from pathlib import Path
import importlib
import json
import os
import queue
import shutil
import sys
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import maibot_shell_registry as registry
from core import shell_plugin_config as cfg
from src.webui.services.gsmps_management import shell_bundle, save_shell_config, reset_shell_config
from fastapi import HTTPException
import tomlkit

checks = []


def check(name, result):
    checks.append({'name': name, 'passed': bool(result)})
    if not result:
        raise AssertionError(name)


def reject(name, plugin_id, payload, expected_status=400):
    before = registry.state_path().read_bytes() if registry.state_path().exists() else None
    try:
        save_shell_config(plugin_id, payload)
    except HTTPException as exc:
        check(name, exc.status_code == expected_status)
    else:
        check(name, False)
    after = registry.state_path().read_bytes() if registry.state_path().exists() else None
    check(name + ' atomic', before == after)


def run():
    with tempfile.TemporaryDirectory(prefix='gsmps-config-') as directory:
        target = Path(directory)
        for folder in ('plugins', 'auto'):
            (target / folder).mkdir()
            for source in (ROOT / folder).glob('*.py'):
                shutil.copy2(source, target / folder / source.name)
        (target / 'config.json').write_text(json.dumps({
            'bilibili': {'queue_limit': 5, 'max_size_mb': 40},
            'rcon': {'password': 'fixture-secret', 'port': 25575},
            'tournament': {'enabled': True}, 'mc': {'host': 'fixture.invalid'},
        }), encoding='utf8')
        with patch.dict(os.environ, {'MAIBOT_INSTANCE_DIR': directory}):
            rows = registry.catalog()
            check('36 external modules + Weflow after retirement', len(rows) == 37)
            for row in rows:
                bundle = shell_bundle(row['id'])
                check('schema ' + row['id'], all(f['name'] and f['ui_type'] for s in bundle['schema']['sections'].values() for f in s['fields'].values()))
                check('TOML roundtrip ' + row['id'], tomlkit.loads(bundle['raw_config']).unwrap() == bundle['config'])
            p = 'gsmps.automation.patpat'
            save_shell_config(p, {'settings': {'reply_text': '测试回复', 'cooldown_seconds': 12}})
            check('persist reread', shell_bundle(p)['config']['settings']['reply_text'] == '测试回复')
            patpat = importlib.import_module('auto.patpat')
            with patch.object(patpat, '_is_bot_pat', return_value=True):
                check('patpat consumes saved value', patpat.handle_auto({'group': 'fixture', 'user': 'u'}) == '测试回复')
            ctx = {'group': 'fixture', 'sessionId': 'id', 'wxid': 'u'}
            check('cooldown initially allowed', cfg.can_invoke(p, ctx))
            cfg.mark_invoked(p, ctx)
            check('cooldown enforced', not cfg.can_invoke(p, ctx))
            save_shell_config(p, {'settings': {'cooldown_seconds': 0, 'allow_groups': ['other']}})
            check('group restriction', not cfg.can_invoke(p, ctx))
            check('background destination restriction', not cfg.can_invoke(p, {**ctx, '_background': True}))
            save_shell_config(p, {'settings': {'allow_groups': [], 'allow_users': ['other']}})
            check('user restriction', not cfg.can_invoke(p, ctx))
            check('background has no requesting user', cfg.can_invoke(p, {**ctx, '_background': True}))
            save_shell_config(p, {'settings': {'allow_users': [], 'agent_enabled': False}})
            check('bot invocation restriction', not cfg.can_invoke(p, {**ctx, '_agent_invocation': True}))
            check('ordinary user still allowed', cfg.can_invoke(p, ctx))
            reject('unknown field', p, {'settings': {'arbitrary_admin': True}})
            reject('strict bool', p, {'plugin': {'enabled': 'false'}})
            reject('strict int', p, {'settings': {'cooldown_seconds': True}})
            reject('bounds', p, {'settings': {'cooldown_seconds': -1}})
            reject('oversize text', p, {'settings': {'reply_text': 'x' * 201}})
            reject('list item type', p, {'settings': {'allow_users': [123]}})
            reject('partial invalid patch', p, {'plugin': {'enabled': False}, 'settings': {'cooldown_seconds': -1}})
            reject('removed lottery cannot be configured', 'gsmps.command.lottery', {'settings': {'min_stake': 101}}, expected_status=404)
            reject('NaN', 'gsmps.command.bv', {'settings': {'max_size_mb': float('nan')}})
            result = save_shell_config('gsmps.command.bv', {'settings': {'queue_limit': 7}})
            effective = cfg.apply_overrides(json.loads((target / 'config.json').read_text(encoding='utf8')))
            check('startup warning', result['restart_required'])
            check('startup overlay', effective['bilibili']['queue_limit'] == 7)
            check('original deployment config preserved', json.loads((target / 'config.json').read_text(encoding='utf8'))['bilibili']['queue_limit'] == 5)
            bv = importlib.import_module('plugins.bv')
            with patch.object(bv, '_WORKER', Mock(is_alive=lambda: True)), patch.object(bv, '_QUEUE', queue.Queue()), patch.object(bv.os, 'makedirs'), patch.object(bv.bilibili, 'configure'):
                bv.init(effective)
                check('queue executes configured limit', bv.QUEUE_LIMIT == 7 and bv._QUEUE.maxsize == 7)
            save_shell_config('gsmps.command.theroom', {'settings': {'enabled': False}})
            check('shared binding synchronized', shell_bundle('gsmps.automation.tournament_monitor')['config']['settings']['enabled'] is False)
            secret = shell_bundle('gsmps.command.rcon')
            check('secret absent from entire bundle', 'fixture-secret' not in json.dumps(secret))
            save_shell_config('gsmps.command.rcon', secret['config'])
            check('masked save preserves secret', cfg.settings('gsmps.command.rcon')['password'] == 'fixture-secret')
            save_shell_config('gsmps.command.rcon', {'settings': {'password': 'new-secret'}})
            check('secret replaced in effective config', cfg.apply_overrides({})['rcon']['password'] == 'new-secret')
            check('new secret stays redacted', 'new-secret' not in json.dumps(shell_bundle('gsmps.command.rcon')))
            save_shell_config('gsmps.command.rcon', {'plugin': {'enabled': False}})
            check('toggle preserves settings', cfg.settings('gsmps.command.rcon')['password'] == 'new-secret')
            reset = reset_shell_config('gsmps.command.rcon')
            check('reset backup exists', Path(reset['backup']).is_file())
            check('reset restores deployment secret', cfg.settings('gsmps.command.rcon')['password'] == 'fixture-secret')
            repeat = importlib.import_module('auto.repeat_echo')
            save_shell_config('gsmps.automation.repeat_echo', {'settings': {'repeat_count': 3, 'special_reply_enabled': False}})
            repeat.SESSION_STATES.clear()
            check('repeat one waits', repeat.handle_auto({**ctx, 'content': 'hello'}) is None)
            check('repeat two waits', repeat.handle_auto({**ctx, 'content': 'hello'}) is None)
            check('repeat third responds', repeat.handle_auto({**ctx, 'content': 'hello'}) == 'hello')
            check('retired plugins absent from catalog', not any(registry.find('gsmps.command.'+name)
                  for name in ('lottery','guide','home','notice','prompt','redeem')))
            check('JSON validation', cfg.validate('object', '{"x": [1]}', {'type': 'json'}) == {'x': [1]})
            # Exercise the actual HTTP route contracts used by the WebUI editor.
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from src.webui.routers.plugin import config_routes
            app = FastAPI()
            app.include_router(config_routes.router, prefix='/api/plugins')
            with patch.object(config_routes, 'require_plugin_token'), TestClient(app) as client:
                base_url = '/api/plugins/config/' + p
                bundle = client.get(base_url + '/bundle')
                check('HTTP bundle discoverable', bundle.status_code == 200 and 'reply_text' in bundle.json()['schema']['sections']['settings']['fields'])
                check('HTTP schema endpoint', client.get(base_url + '/schema').status_code == 200)
                saved = client.put(base_url, json={'config': {'settings': {'reply_text': 'HTTP测试回复'}}})
                check('HTTP save accepted', saved.status_code == 200 and saved.json()['success'])
                check('HTTP reread saved value', client.get(base_url).json()['config']['settings']['reply_text'] == 'HTTP测试回复')
                with patch.object(patpat, '_is_bot_pat', return_value=True):
                    check('HTTP saved value executed', patpat.handle_auto({'group': 'fixture'}) == 'HTTP测试回复')
                check('HTTP invalid TOML rejected', client.put(base_url + '/raw', json={'config': '[broken'}).status_code == 400)
                check('HTTP invalid type rejected', client.put(base_url, json={'config': {'settings': {'cooldown_seconds': '2'}}}).status_code == 400)
                raw = client.get(base_url + '/raw').json()['config']
                check('HTTP raw form roundtrip', client.put(base_url + '/raw', json={'config': raw}).status_code == 200)
                secret = client.get('/api/plugins/config/gsmps.command.rcon/bundle')
                check('HTTP secret remains masked', 'fixture-secret' not in secret.text and '********' in secret.text)
                startup = client.put('/api/plugins/config/gsmps.command.bv', json={'config': {'settings': {'queue_limit': 8}}})
                check('HTTP restart required feedback', startup.status_code == 200 and startup.json()['restart_required'])
            from src.common.database.database import engine
            engine.dispose()
    output = ROOT / 'temp' / 'maibot-shell-config-acceptance.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'passed': len(checks), 'report': str(output)}))


if __name__ == '__main__':
    run()
