"""No-send tests of Host identity, plugin proxy and model pool ownership."""
import asyncio
import copy
from pathlib import Path
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import closing
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolSpec
from src.plugin_runtime import owned_plugin_tools as proxy
from core import maibot_shell_registry as registry

checks = []


def check(name, condition):
    checks.append(dict(name=name, passed=bool(condition)))
    if not condition:
        raise AssertionError(name)


def module(name, **attrs):
    obj = ModuleType(name)
    vars(obj).update(attrs)
    return obj


async def run():
    class Dispatcher:
        inited = True
        config = {}
        llm_service = None
        modules = {'echo': SimpleNamespace(handle=lambda args, ctx: {'text': args, 'actor': ctx['wxid'], 'platform': ctx['platform']}),
                   'auto:player_monitor': SimpleNamespace()}
        _module_enabled_for_group = lambda self, *args: True
        _context_feature_enabled = lambda self, *args: True
    dispatcher = Dispatcher()
    registry.bind_dispatcher(dispatcher)
    anchor = SimpleNamespace(platform='wechat', account_id='wxid_self', session_id='stream', message_id='real-1', processed_plain_text='hello', is_notify=False,
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='human_admin', user_nickname='Admin', user_cardname='Admin'),
            group_info=SimpleNamespace(group_id='fixture@chatroom', group_name='fixture'), additional_config={'is_local_operator': True}))
    context = ToolExecutionContext(session_id='stream', stream_id='stream', user_id='human_admin', platform='wechat',
        is_group_chat=True, group_id='fixture@chatroom', metadata={'anchor_message': anchor})
    native = SimpleNamespace(get_llm_available_tool_specs=lambda ctx: {'native_probe': ToolSpec(name='native_probe', description='probe', parameters_schema={},
        provider_name='fixture.native', provider_type='plugin')}, _iter_component_entries=lambda *args, **kwargs: [],
        invoke_tool_as_tool=AsyncMock(return_value=ToolExecutionResult('native_probe', True, content='native result')))
    account_module = module('src.services.bot_account_service', get_bot_accounts=lambda p: {'wxid_self', 'qq_self'})
    component_module = module('src.plugin_runtime.component_query', component_query_service=native)
    with tempfile.TemporaryDirectory(prefix='management-') as directory:
        for folder in ('plugins', 'auto'):
            target = Path(directory) / folder
            target.mkdir()
            for source in (ROOT / folder).glob('*.py'):
                shutil.copy2(source, target / source.name)
        (Path(directory) / 'config.json').write_text('{}', encoding='utf8')
        with patch.dict(os.environ, {'MAIBOT_INSTANCE_DIR': directory}), patch.dict(sys.modules, {
            'src.services.bot_account_service': account_module, 'src.plugin_runtime.component_query': component_module}), patch.object(registry, 'allowed', return_value=True):
            token = proxy.issue_grant(context)
            trusted = proxy._grants[token][1]
            check('Host selects own bot identity', trusted.user_id == 'wxid_self')
            check('Human admin not inherited', trusted.metadata['anchor_message'].message_info.user_info.user_id == 'wxid_self')
            check('Local operator removed', not trusted.metadata['anchor_message'].message_info.additional_config.get('is_local_operator'))
            check('Original message untouched', anchor.message_info.user_info.user_id == 'human_admin')
            result = await proxy.execute(proxy.PLUGIN_ID, {'grant': token, 'operation': 'list', 'query': 'echo'})
            check('Shell catalog source', any(r['target'] == 'gsmps.command.echo' and r['source'] == 'GSMPS Wechatbot' for r in result['items']))
            check('Grant single use', not (await proxy.execute(proxy.PLUGIN_ID, {'grant': token}))['success'])
            async def invoke(target, arguments=None, operation='invoke', **extra):
                return await proxy.execute(proxy.PLUGIN_ID, dict(grant=proxy.issue_grant(context), operation=operation,
                    target=target, arguments=arguments or {}, **extra))
            check('Capability restricted to its plugin', not (await proxy.execute('other.plugin', {'grant': proxy.issue_grant(context)}))['success'])
            check('Missing grant denied', not (await proxy.execute(proxy.PLUGIN_ID, {'operation': 'list'}))['success'])
            result = await invoke('gsmps.command.echo', {'text': 'hello'})
            check('Shell actual handler result returned', result['success'] and result['result']['text'] == 'hello')
            check('Shell handler sees own identity and source', result['result']['actor'] == 'wxid_self' and result['result']['platform'] == 'wechat')
            check('Result provenance', result['kind'] == 'plugin_tool_result')
            check('No operator command', not (await invoke('gsmps.command.rcon', {'text': 'op self'}))['success'])
            check('No identity injection', not (await invoke('tool:native_probe', {'user_id': 'human_admin'}))['success'])
            result = await invoke('tool:native_probe')
            check('Native tool result returned', result['success'] and result['content'] == 'native result')
            check('Native invocation own identity', native.invoke_tool_as_tool.call_args.args[1].user_id == 'wxid_self')
            check('Per message call limit', not (await invoke('gsmps.command.echo', {'text': 'fourth'}))['success'])
            anchor.message_id = 'real-2'
            check('Automation cannot forge events', not (await invoke('gsmps.automation.patpat', {'content': 'fake pat'}))['success'])
            check('Background monitor not restarted', not (await invoke('gsmps.automation.player_monitor'))['success'])
            check('Bad pagination rejected', not (await invoke('', operation='list', offset=-1))['success'])
            qq = copy.copy(context)
            qq.platform = 'qq'
            qa = copy.copy(anchor); qa.platform = 'qq'; qa.account_id = 'qq_self'; qa.message_id = 'qq-1'
            qq.metadata = {'anchor_message': qa}
            result = await proxy.execute(proxy.PLUGIN_ID, {'grant': proxy.issue_grant(qq), 'operation': 'invoke', 'target': 'gsmps.command.echo', 'arguments': {'text': 'x'}})
            check('QQ cannot masquerade as WeChat shell', not result['success'])
            mismatch = copy.copy(context); mismatch.stream_id = 'other'
            check('Mismatched stream denied', proxy.issue_grant(mismatch) == '')
    from scripts.adopt_maibot_model_pool import adopt
    native_doc = {'models': [], 'api_providers': [], 'model_task_config': {'utils': {'model_list': ['keep_order']}}}
    legacy = {'llm': {'apis': [dict(name='one', model='same', base_url='https://one.invalid', enabled=False), dict(name='two', model='same', base_url='https://two.invalid', enabled=False)]}}
    imported = adopt(legacy, native_doc)
    check('All endpoint aliases unique', len({m['name'] for m in imported['models']}) == 2)
    check('Dormant endpoints disabled', all(p['enabled'] is False for p in imported['api_providers']))
    check('Task order unchanged', imported['model_task_config'] == native_doc['model_task_config'])
    legacy['llm']['model_config_source'] = 'maibot'
    check('No reimport after WebUI deletion', adopt(legacy, native_doc) == native_doc)
    # Validate SDK declaration and the actual native provider disable barrier.
    from src.config.model_configs import APIProvider
    from src.llm_models.model_client.base_client import client_registry
    dormant = APIProvider(name='dormant', enabled=False, api_key='', base_url='https://fixture.invalid')
    try:
        await client_registry.get_client_class_instance_async(dormant)
    except ValueError:
        check('Disabled provider creates no client', True)
    else:
        check('Disabled provider creates no client', False)
    path = ROOT / 'plugins/owned_plugin_tools/plugin.py'
    spec = importlib.util.spec_from_file_location('owned_probe', path)
    plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
    check('Native SDK declaration valid', plugin.create_plugin().config_model is plugin.Config)
    from scripts.clean_inactive_chat_data import clean
    import sqlite3
    with tempfile.TemporaryDirectory(prefix='chat-cleanup-') as directory:
        base = Path(directory); (base / 'data/groups/old').mkdir(parents=True)
        (base / 'config.json').write_text(json.dumps({'target_group': ['keep']}), encoding='utf8')
        with closing(sqlite3.connect(base / 'data/MaiBot.db')) as db:
            db.executescript('CREATE TABLE chat_sessions(session_id,platform,group_id,group_name);'
                "INSERT INTO chat_sessions VALUES('sid','wechat','gid','keep');"
                "INSERT INTO chat_sessions VALUES('private','wechat','','');"
                'CREATE TABLE mai_messages(group_id,session_id);'
                "INSERT INTO mai_messages VALUES('gid','sid'),('old','old'),('','private');"
                'CREATE TABLE expressions(id INTEGER PRIMARY KEY,session_id,situation,style,content_list,checked,modified_by,count);'
                "INSERT INTO expressions VALUES(1,'keep','a','b','[]',1,NULL,2),(2,'sid','a','b','[]',1,NULL,1);")
            db.commit()
        preview = clean(base)
        with closing(sqlite3.connect(base / 'data/MaiBot.db')) as db:
            check('Cleanup dry run leaves rows', db.execute('SELECT COUNT(*) FROM mai_messages').fetchone()[0] == 3)
        report = clean(base, apply=True)
        with closing(sqlite3.connect(base / 'data/MaiBot.db')) as db:
            check('Cleanup keeps active and private', db.execute('SELECT COUNT(*) FROM mai_messages').fetchone()[0] == 2)
            check('Cleanup normalizes and deduplicates expressions', db.execute('SELECT session_id,count FROM expressions').fetchall() == [('sid', 2)])
        check('Cleanup archive exists', (Path(report['backup']) / 'groups/old').is_dir())
        with closing(sqlite3.connect(Path(report['backup']) / 'MaiBot.db')) as db:
            check('Cleanup consistent backup restores original rows', db.execute('SELECT COUNT(*) FROM mai_messages').fetchone()[0] == 3)
    output = ROOT / 'temp/maibot-management-acceptance.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(dict(passed=len(checks), checks=checks), ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(dict(passed=len(checks), report=str(output))))


if __name__ == '__main__':
    asyncio.run(run())
