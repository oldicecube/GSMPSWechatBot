"""Isolated acceptance: actual AppContainer, native SDK components, no live sends."""
import asyncio
from datetime import datetime, timedelta
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHECKS = []


def check(name, ok, detail=None):
    CHECKS.append(dict(name=name, passed=bool(ok), detail=detail))
    print(name, bool(ok), str(detail)[:200], flush=True)


def load(folder):
    name = 'life_accept_' + folder.replace('-', '_')
    p = ModuleType(name); p.__path__ = [str(ROOT / 'plugins' / folder)]; sys.modules[name] = p
    return importlib.import_module(name + '.plugin')


def msg(i, user='wxid_human', text='Minecraft experiment', stamp=None, group='test@chatroom', **extra):
    return dict(message_id=str(i), timestamp=str(stamp or NOW.timestamp()), platform='wechat',
                message_info=dict(user_info=dict(user_id=user, nickname=user), group_info=dict(group_id=group), additional_config=extra),
                raw_message=[dict(type='text', data=text)])


NOW = datetime(2026, 10, 29, 14, 0, tzinfo=ZoneInfo('Asia/Shanghai'))


async def run(root):
    mod = load('autonomous_life'); plugin = mod.create_plugin()
    cfg = tomllib.loads((ROOT / 'plugins/autonomous_life/config.toml').read_text(encoding='utf8'))
    cfg['fun'] = {'enabled': False}
    cfg['project']['enabled'] = False  # General autonomous-life regression, no operator assignment.
    cfg['life']['groups'] = ['test@chatroom']; cfg['life']['share_gap_seconds'] = 1800
    plugin.set_plugin_config(cfg)
    plugin.now = lambda: NOW
    plugin.store = mod.Store(root / 'life.sqlite3')
    rows = [msg('one'), msg('two', 'wxid_friend')]
    llm = AsyncMock()
    ctx = NS(paths=NS(data_dir=root, runtime_dir=root / 'runtime'), logger=logging.getLogger('life-test'),
        chat=NS(get_stream_by_group_id=AsyncMock(return_value=dict(platform='wechat', group_id='test@chatroom', session_id='sid'))),
        config=NS(get=AsyncMock(return_value='GSMPS server bot')), call_capability=AsyncMock(return_value=rows),
        llm=NS(generate=llm), api=NS(call=AsyncMock()),
        maisaka=NS(reply=NS(generate=AsyncMock(return_value=dict(success=True, text='这个实验跑通了')))),
        send=NS(hybrid=AsyncMock(return_value=dict(sent=True, message_id='wechat-test'))))
    plugin._set_context(ctx)
    check('native_components', {x['name'] for x in plugin.get_components()} >= {'autonomous_life_status', 'autonomous_life.repost_opportunity'})
    check('status_host_bound_action', next(x for x in plugin.get_components() if x['name']=='autonomous_life_status')['metadata']['invoke_method']=='plugin.invoke_action')
    schema = mod.Config.model_json_schema()
    check('config_native_schema', '$defs' in schema)
    for _ in range(20):
        plugin.schedule(NOW.timestamp())
        assert 600 <= plugin.store.get('next_check')-NOW.timestamp() <= 1800
    check('consider_interval_10_30_minutes', True)
    llm.return_value = dict(success=True, response='{"action":"rest","reason":"需要休息"}')
    await plugin.consider()
    check('rest_no_action_no_send', plugin.store.count('2026-10-29', 'action') == 0 and not ctx.send.hybrid.await_count)
    check('next_check_persisted', mod.Store(root / 'life.sqlite3').get('next_check') > NOW.timestamp())
    decision = dict(action='think', thought='想研究红石计时', reason='群内红石讨论', interests=['红石'],
                    project=dict(title='红石实验', status='active', note='比较两个计时方案'), share=False)
    llm.return_value = dict(success=True, response=json.dumps(decision))
    await plugin.consider()
    snapshot = mod.Store(root / 'life.sqlite3').snapshot('sid')
    check('interest_project_experience_continuity', snapshot['interests']==['红石'] and snapshot['projects'][0]['title']=='红石实验' and snapshot['experiences'])
    captured = llm.await_args.args[0]
    check('decision_has_persona_real_history', 'GSMPS server bot' in captured and 'Minecraft experiment' in captured)
    check('model_rpc_budget_includes_provider_wait', llm.await_args.kwargs['rpc_timeout_ms'] ==
          plugin.config.life.llm_timeout_seconds * 1000 + 1000 and plugin.config.life.llm_timeout_seconds >= 90)
    check('decision_has_actual_action_budget', 'activity_budget' in captured and 'actions_remaining' in captured)
    await plugin.consider()
    check('future_decision_sees_old_project', '红石实验' in llm.await_args.args[0] and '比较两个计时方案' in llm.await_args.args[0])
    p = dict(title='红石实验', status='abandoned', note='目前不想继续')
    plugin.store.update_project('sid', p, NOW.timestamp())
    check('project_can_abandon', plugin.store.snapshot('sid')['projects'][0]['status']=='abandoned')
    check('cross_stream_memory_isolated', not plugin.store.snapshot('another')['projects'])
    quoted = msg('quoted'); quoted['raw_message']=[dict(type='reply',data=dict(target_message_content='@bot')), dict(type='text',data='hi')]
    bads = [msg('one'), msg('othergroup', group='excluded@chatroom'), msg('bot', user=cfg['life']['ignored_users'][0]),
            msg('annotated', bot_interaction=dict(sender_is_bot=True)), msg('echo', wechat=dict(source_kind='bot_echo_external')),
            msg('future', stamp=NOW.timestamp()+100), msg('old', stamp=NOW.timestamp()-170*3600),
            msg('hidden', wechat=dict(prompt_visible=False))]
    filtered = mod.history(rows+bads+[quoted], 'test@chatroom', NOW.timestamp(), set(cfg['life']['ignored_users']))
    check('history_identity_scope_dedupe_bot_quote', {r['id'] for r in filtered}=={'one','two','quoted'} and filtered[-1]['text']=='hi')
    share_decision = dict(person='wxid_human', evidence_ids=['one'])
    actual_rows = mod.history(rows, 'test@chatroom', NOW.timestamp(), set())
    ctx.call_capability.return_value = dict(success=True, text='这个实验跑通了')
    sent = await plugin.share('sid', actual_rows, share_decision, dict(success=True, fact='synthetic-experiment'))
    call = ctx.send.hybrid.await_args.kwargs
    check('native_replyer_and_at_send', sent and call['segments'][0]==dict(type='at',data=dict(target_user_id='wxid_human')) and call['stream_id']=='sid')
    check('native_style_no_custom_format', ctx.call_capability.await_args.kwargs.get('reply_mode')=='chat' and 'format_instruction' not in ctx.call_capability.await_args.kwargs)
    check('dedupe_no_repeat', not await plugin.share('sid', actual_rows, share_decision, dict(success=True, fact='synthetic-experiment')))
    check('unknown_person_rejected', not await plugin.share('sid', actual_rows, dict(person='forged', evidence_ids=['one']), dict(fact='other')))
    check('at_all_rejected', not await plugin.share('sid', actual_rows, dict(person='all', evidence_ids=['one']), dict(fact='other')))
    opted = [*actual_rows, dict(id='optout',person='wxid_human',text='不要艾特我')]
    check('optout_respected', not await plugin.share('sid', opted, share_decision, dict(fact='other')))
    quota = mod.Store(root / 'quota.sqlite3')
    for n in range(4):
        assert quota.reserve_share('2026-10-29', NOW.timestamp()+n*2000, 's'+str(n), str(n), '', 4, 1800, 86400)
    check('global_daily_four_limit', not quota.reserve_share('2026-10-29', NOW.timestamp()+9000, 'different-group', 'fifth', '', 4, 1800, 86400))
    restarted = mod.Store(root / 'quota.sqlite3')
    check('restart_unknown_charged_not_retried', not restarted.reserve_share('2026-10-29', NOW.timestamp()+9000, 's', '0', '', 4, 1800, 86400))
    check('new_day_budget_resets', restarted.reserve_share('2026-10-30', NOW.timestamp()+86400, 's', 'newday', '', 4, 1800, 86400))
    state = mod.Store(root / 'limits.sqlite3')
    for n in range(12): assert state.reserve('day', 1, 's', 'action', 12)
    check('hard_daily_twelve_actions', state.reserve('day', 1, 's', 'action', 12) is None)
    event = state.reserve('day', 1, 's', 'llm', 1)
    check('hard_model_budget', event and not state.reserve('day', 1, 's', 'llm', 1))
    check('quiet_hours', not plugin.awake(NOW.replace(hour=3)) and plugin.awake(NOW.replace(hour=20)))
    ctx.call_capability.return_value = rows
    llm.return_value = dict(success=False, response='429')
    before = ctx.send.hybrid.await_count
    await plugin.consider()
    check('api_failure_backoff_no_chat', plugin.store.get('backoff_until') > NOW.timestamp() and ctx.send.hybrid.await_count == before)
    count = llm.await_count; await plugin.consider()
    check('backoff_no_retry_storm', llm.await_count == count)
    plugin.store.set('backoff_until', 0)
    plugin.store.set('repost_grant:one-time', dict(token='one-time', group='test@chatroom', expires=NOW.timestamp()+60))
    check('one_time_grant', (await plugin.consume_grant(grant='one-time',group_id='test@chatroom'))['success'] and not (await plugin.consume_grant(grant='one-time',group_id='test@chatroom'))['success'])
    check('repost_scope_rejected', not (await plugin.repost_opportunity(group_id='outside@chatroom'))['success'])
    old_store = plugin.store
    plugin.store = mod.Store(root/'repost.sqlite3')
    repost_rows = [msg(i, user='human-'+str(i%2),text='https://b23.tv/synthetic'+str(i)) for i in range(6)]
    llm.return_value = dict(success=True,response='{"repost":true,"query":"Minecraft fun"}')
    async def repost_api(name, **kwargs):
        if name == 'repost.autonomous_one':
            allowed = await plugin.consume_grant(grant=kwargs['grant'],group_id=kwargs['group_id'])
            return {'success':True,'result':{'success':allowed['success'],'sent':allowed['success']}}
        raise AssertionError(name)
    async def repost_capability(name, **kwargs):
        if name == 'api.call':
            assert kwargs['timeout_ms'] == 360000
            return await repost_api(kwargs['api_name'], **kwargs['args'])
        return repost_rows
    ctx.call_capability.side_effect = repost_capability
    outcome = await plugin.repost_opportunity(group_id='test@chatroom')
    with plugin.store.db() as db:
        sent_reposts = db.execute("SELECT count(*) FROM repost_shares WHERE state='sent'").fetchone()[0]
    check('density_decision_budget_single_use_media_grant',outcome.get('sent') and sent_reposts==1
          and plugin.store.count('2026-10-29','action')==0)
    before = sum(call.args == ('api.call',) for call in ctx.call_capability.await_args_list)
    await plugin.repost_opportunity(group_id='test@chatroom')
    check('density_duplicate_and_share_cooldown',sum(call.args == ('api.call',) for call in ctx.call_capability.await_args_list)==before)
    ctx.call_capability.side_effect = None
    ctx.call_capability.return_value=[msg(1),msg(2,'other')]
    check('no_video_signal_no_autonomous_repost',not (await plugin.repost_opportunity(group_id='test@chatroom'))['success'])
    plugin.store = old_store
    ctx.api.call.side_effect = None
    ctx.api.call.return_value = {'success':True,'result':{'success':True,'stdout':'fixed'}}
    ctx.api.call.side_effect = [dict(success=True,result=dict(success=False,error='SyntaxError: invalid syntax')),
                                dict(success=True,result=dict(success=True,stdout='fixed'))]
    llm.return_value = dict(success=True,response='{"code":"print(7)"}')
    fixed = await plugin.perform('python',{'code':'bad syntax'},'sid',[])
    check('python_one_bounded_repair_uses_real_error',fixed.get('success') and fixed.get('code')=='print(7)' and 'SyntaxError' in llm.await_args.args[0])
    check('python_source_result_saved',bool(list((root/'artifacts').rglob('program.py.txt'))))
    ctx.api.call.side_effect = None
    ctx.api.call.return_value = dict(success=True,result=dict(success=False,error='AppContainer unavailable'))
    before = ctx.api.call.await_count
    await plugin.perform('python',{'code':'print(7)'},'sid',[])
    check('unavailable_isolation_not_retried',ctx.api.call.await_count==before+1)
    plugin.store.set('backoff_until',0)
    llm.side_effect = [dict(success=True,response=json.dumps(dict(action='python',code="print('completed')",
        project=dict(title='survive reflection failure',status='active',note='continue tomorrow'),share=True))),
        dict(success=False,response='429')]
    with patch.object(plugin,'perform',AsyncMock(return_value=dict(success=True,stdout='completed',code="print('completed')"))):
        await plugin.consider()
    snap=plugin.store.snapshot('sid')
    check('reflection_failure_keeps_executed_work', any('completed' in e['data'] and e['state']=='done' for e in snap['experiences']))
    check('reflection_failure_keeps_project_and_backoff',any(p['title']=='survive reflection failure' for p in snap['projects']) and plugin.store.get('backoff_until')>NOW.timestamp())
    check('reflection_failure_preserves_requested_progress_for_recovery',bool(plugin.store.pending_progress('sid', NOW.timestamp())))
    status=await plugin.status(stream_id='sid')
    check('status_exposes_actual_work_without_private_thought',any(e['output_excerpt']=='completed' for e in status['actual_experiences']) and '想研究红石计时' not in json.dumps(status,ensure_ascii=False))
    check('status_rejects_other_stream',not (await plugin.status(stream_id='forged-other-stream'))['success'])
    llm.side_effect = None
    ctx.paths.data_dir=root/'startup'
    other=mod.create_plugin(); other.set_plugin_config(cfg); other._set_context(ctx)
    await other.on_load()
    second=mod.create_plugin(); second.set_plugin_config(cfg); second._set_context(ctx)
    try:
        await second.on_load(); locked=False
    except RuntimeError: locked=True
    check('exclusive_coordinator_file_lock',locked)
    next_check=other.store.get('next_check')
    await other.on_unload(); await second.on_load()
    check('restart_no_immediate_action_and_clean_unload',second.store.get('next_check')==next_check)
    await second.on_unload()
    web = importlib.import_module(mod.__package__+'.web')
    for url in ('http://example.com','https://127.0.0.1','https://[::1]','https://user:password@example.com','https://example.com:8080'):
        try: web.read(url); ok=False
        except ValueError: ok=True
        check('ssrf_denied_'+url.split('://')[-1],ok)
    await sandbox_tests(root)
    prompt_tests()


async def sandbox_tests(root):
    mod = load('restricted_python'); sandbox = importlib.import_module(mod.__package__+'.appcontainer')
    real_root = root / 'sandbox'
    async def execute(code): return await sandbox.run_program_async(code, real_root)
    result = await execute("import math\ndef f(x): return x*x\nprint(f(7),math.sqrt(81))\nopen('result.txt','w').write('saved artifact')")
    check('real_appcontainer_full_python_artifact', result.get('isolation')=='windows-appcontainer' and result.get('stdout')=='49 9.0\n' and result['artifacts'][0]['text']=='saved artifact', result)
    secret = root/'host-secret.txt';secret.write_text('never-export-this',encoding='utf8')
    result = await execute('print(open('+repr(str(secret))+').read())')
    check('host_file_read_denied', not result.get('success') and 'PermissionError' in result.get('error',''),result)
    result = await execute('open('+repr(str(secret))+",'w').write('changed')")
    check('host_file_write_denied', not result.get('success') and secret.read_text()=='never-export-this',result)
    result = await execute('import socket\nsocket.create_connection(("1.1.1.1",443),timeout=1)')
    check('network_denied', not result.get('success'),result)
    result = await execute('import subprocess\nsubprocess.run(["C:\\\\Windows\\\\System32\\\\cmd.exe","/c","echo unsafe"],check=True)')
    check('child_process_denied', not result.get('success'),result)
    result = await execute('while True: pass')
    check('cpu_wall_limit', not result.get('success'),result)
    result = await execute('x=bytearray(512*1024*1024)')
    check('memory_limit', not result.get('success'),result)
    result = await execute("print('x'*20000)")
    check('output_limit', not result.get('success'),result)
    result = await execute("open('big.txt','wb').write(b'x'*2000000)")
    check('disk_export_limit',not result.get('success'),result)
    # Test the real repository ACL, not only the current user's Temp directory.
    probe = ROOT/'temp/autonomy-host-probe.txt'
    probe.write_text('synthetic-workspace-secret',encoding='utf8')
    try:
        result=await execute('print(open('+repr(str(probe))+').read())')
        check('workspace_file_read_denied',not result.get('success') and 'PermissionError' in result.get('error',''),result)
    finally:
        probe.unlink()
    with patch.object(sandbox,'prepare',side_effect=OSError('isolation unavailable')):
        result = await execute("print('unsafe fallback')")
    check('isolation_failure_closed', not result.get('success') and 'unavailable' in result['error'],result)


def prompt_tests():
    from llm.core.native_prompt_policy import native_styles, profile_prompt
    styles = native_styles()
    doc = tomllib.loads((ROOT/'config/bot_config.toml').read_text(encoding='utf8'))
    llm = json.loads((ROOT/'config.json').read_text(encoding='utf8'))['llm']
    # Native WebUI settings belong to the user. Their current choices may differ
    # from the defaults or the old persona; never restore hard-coded old values.
    check('native_style_fields_user_owned', all(isinstance(doc['personality'].get(k),str) for k in ('reply_style','behavior_style')))
    check('custom_identity_present_in_native_config', isinstance(doc['personality'].get('personality'),str) and bool(doc['personality']['personality'].strip()))
    check('stale_python_instruction_removed', '尚未' not in doc['personality']['personality'] and '隔离沙箱' in doc['personality']['personality'])
    check('group_no_duplicate_style_injection', all(not profile_prompt(llm,p) for p in llm['group_profiles'].values()))
    check('config_removed_style_flags', all(not any(k in x for k in ['reply_style','behavior_style','group_prompt','private_prompt']) for x in [llm,*llm['group_profiles'].values()]))
    check('legacy_style_ignored_native_authority', profile_prompt({}, {'reply_style':'old'})=={})
    from llm.core import maibot_native_config as native_config
    import shutil
    with tempfile.TemporaryDirectory(prefix='native-style-guard-') as tmp:
        folder=Path(tmp)
        for name in ['bot_config.toml','model_config.toml']:
            shutil.copy2(ROOT/'config'/name,folder/name)
        legacy=json.loads((ROOT/'config.json').read_text(encoding='utf8'))
        legacy['llm'].setdefault('identity', {})['style']='custom-style-sentinel'
        with patch.object(native_config,'BOT_CONFIG',folder/'bot_config.toml'), patch.object(native_config,'MODEL_CONFIG',folder/'model_config.toml'), patch.object(native_config,'_read_legacy',return_value=legacy):
            native_config.sync_native_config()
        check('native_policy_blocks_future_identity_style_override','custom-style-sentinel' not in (folder/'bot_config.toml').read_text(encoding='utf8'))
        after=tomllib.loads((folder/'bot_config.toml').read_text(encoding='utf8'))
        check('current_native_identity_and_style_preserved',after['personality']==doc['personality'] and after['chat']['reply_style']==doc['chat']['reply_style'] and after['bot']['nickname']==doc['bot']['nickname'])


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='maibot-life-') as tmp:
        asyncio.run(run(Path(tmp)))
    report=dict(passed=all(r['passed'] for r in CHECKS),checks=CHECKS,production_restart=False,physical_messages=False)
    path=ROOT/'docs/maibot-wechat-acceptance/autonomous-life-20261029.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('REPORT',path,report['passed'])
    raise SystemExit(0 if report['passed'] else 1)
