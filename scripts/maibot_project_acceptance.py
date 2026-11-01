"""Project kickoff/permission acceptance; isolated data and intercepted sends only."""
import asyncio
from datetime import timedelta
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.maibot_autonomous_life_acceptance import load, msg, NOW

CHECKS = []


def check(name, result):
    CHECKS.append(dict(name=name, passed=bool(result)))
    print(name, bool(result), flush=True)


async def run(root):
    mod = load('autonomous_life'); p = mod.create_plugin()
    flow = importlib.import_module(mod.__package__ + '.project')
    cfg = tomllib.loads((ROOT / 'plugins/autonomous_life/config.toml').read_text(encoding='utf8'))
    cfg['life']['groups'] = ['test@chatroom']
    cfg['project']['owner_user_id'] = 'wxid_owner'
    p.set_plugin_config(cfg); p.now = lambda: NOW
    rows = [msg('owner', user='wxid_owner', text='继续自主项目'), msg('human')]
    ctx = NS(paths=NS(data_dir=root, runtime_dir=root / 'runtime'), logger=logging.getLogger('project-test'),
        chat=NS(get_stream_by_group_id=AsyncMock(return_value=dict(platform='wechat', group_id='test@chatroom', session_id='sid'))),
        config=NS(get=AsyncMock(return_value='GSMPS server bot')),
        call_capability=AsyncMock(return_value=rows), llm=NS(generate=AsyncMock()),
        api=NS(call=AsyncMock()), message=NS(get_by_id=AsyncMock(return_value=rows[0])),
        send=NS(hybrid=AsyncMock(return_value=dict(sent=True))))
    p._set_context(ctx)
    def sandbox_calls():
        return sum(c.args and c.args[0]=='python_sandbox.execute' for c in ctx.api.call.await_args_list)
    await p.on_load()
    try:
        check('startup_assignment_queued', p.projects.get('test@chatroom')['state'] == 'queued')
        check('explicit_kickoff_within_thirty_seconds', p.store.get('next_check') == NOW.timestamp() + 30)
        check('same_request_never_reseeded', not p.projects.seed())
        configured_owner = p.config.project.owner_user_id
        p.config.project.owner_user_id = 'all'
        check('invalid_owner_cannot_seed_or_receive_at', not p.projects.seed() and not await p.projects.notify('test@chatroom', 'sid'))
        p.config.project.owner_user_id = configured_owner
        ctx.llm.generate.side_effect = [
            dict(success=True, response=json.dumps(dict(action='python', code='print(49)',
                project=dict(title='自选小项目', status='done'), share=True))),
            dict(success=True, response=json.dumps(dict(project=dict(title='自选小项目', status='active', note='写原型'), share=False)))]
        with patch.object(mod, 'explore', return_value=dict(query='fresh research', results=[dict(title='actual fixture', url='https://example.com')])) as research:
            await p.consider()
        check('first_step_forced_real_research', research.call_count == 1 and not sandbox_calls())
        check('research_updates_persistent_assignment', p.projects.get('test@chatroom')['researched'] and p.projects.get('test@chatroom')['state'] == 'active')
        check('research_cannot_mark_program_finished', p.store.snapshot('sid')['projects'][0]['status'] == 'active')
        check('kickoff_prompt_contains_actual_capability_limits', 'project_assignment' in ctx.llm.generate.await_args_list[0].args[0] and 'python_standard_library' in ctx.llm.generate.await_args_list[0].args[0])
        sandbox = importlib.import_module(load('restricted_python').__package__ + '.appcontainer')
        async def execute(name, **kwargs):
            if name=='community_context.reference':
                return {'success':True,'reference':''}
            assert name == 'python_sandbox.execute'
            return await sandbox.run_program_async(kwargs['code'], root / 'sandbox-runtime')
        ctx.api.call.side_effect = execute
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='python',
            code="import json\nprint(49)\nopen('project.json','w').write(json.dumps({'answer':49}))",
            project=dict(title='自选小项目', status='active', note='验证结果'), share=False))),
            dict(success=True, response=json.dumps(dict(project=dict(title='自选小项目', status='active', note='继续改进'), share=False)))]
        await p.consider()
        snap = p.store.snapshot('sid')
        second_prompt = ctx.llm.generate.await_args_list[2].args[0]
        check('initial_guide_not_reinjected_after_first_step', '这是下一次启动的一次性初始引导' not in second_prompt and '"project_assignment"' not in second_prompt)
        check('project_real_appcontainer_program_saved', any('windows-appcontainer' in e['data'] and '49' in e['data'] for e in snap['experiences']) and list((root/'artifacts').rglob('result.json')))
        p.store.update_project('sid', dict(title='自选小项目', note='研究下一步'), NOW.timestamp())
        check('later_research_does_not_erase_source', 'import json' in p.store.snapshot('sid')['projects'][0]['code'])
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='python',
            code='import missing_package_for_acceptance', project=dict(title='自选小项目', status='active'), share=False)))]
        ctx.call_capability.return_value = dict(success=True, text='这个项目需要额外依赖，已暂挂，能否帮我安排一个受限环境？')
        # context() needs real messages; Replyer receives an independent capability result.
        async def capability(name, **kwargs):
            return rows if name == 'message.get_recent' else dict(success=True, text='这个项目需要额外依赖，已暂挂，能否帮我安排一个受限环境？')
        ctx.call_capability.side_effect = capability
        before = sandbox_calls()
        await p.consider()
        job = p.projects.get('test@chatroom')
        check('actual_missing_dependency_blocks_without_useless_repair', job['state'] == 'blocked' and sandbox_calls() == before + 1)
        check('permission_request_native_real_at', ctx.send.hybrid.await_count == 1 and ctx.send.hybrid.await_args.kwargs['segments'][0] == dict(type='at', data=dict(target_user_id='wxid_owner')))
        check('permission_request_native_replyer_style', ctx.call_capability.await_args.kwargs.get('reply_mode') == 'chat')
        before = sandbox_calls()
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='python', code='print(1)', project=dict(title='自选小项目'))))]
        await p.consider()
        check('blocked_project_does_not_run_or_repeat_at', sandbox_calls() == before and ctx.send.hybrid.await_count == 1)
        before = p.store.count(NOW.date().isoformat(), 'thought')
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='think', thought='想研究另一件事', share=False)))]
        await p.consider()
        check('blocked_project_does_not_stop_other_life', p.store.count(NOW.date().isoformat(), 'thought') == before + 1 and p.projects.get('test@chatroom')['state'] == 'blocked')
        await p.on_unload()
        await p.on_load()
        check('restart_preserves_block_and_notification_dedupe', p.projects.get('test@chatroom')['state'] == 'blocked' and not p.projects.seed() and not await p.projects.notify('test@chatroom', 'sid'))
        p.config.life.groups.append('added-later@chatroom')
        check('later_group_does_not_rearm_one_boot_guide', not p.projects.seed() and not p.projects.get('added-later@chatroom'))
        p.config.life.groups.remove('added-later@chatroom')
        request = dict(platform='wechat', group_id='test@chatroom', stream_id='sid', user_id='wxid_owner', message_id='owner')
        for name, override in [('forged_owner', dict(user_id='wxid_other')), ('cross_group', dict(group_id='other@chatroom')),
                               ('notify_not_user', dict(is_notify=True)), ('missing_source', dict(message_id=''))]:
            result = await p.project_control(action='resume', _request_context={**request, **override})
            check(name + '_cannot_resume', result.get('success') is False)
        ctx.message.get_by_id.return_value = msg('owner', user='wxid_owner', text='hello')
        check('innocent_owner_message_is_not_consent', (await p.project_control(action='resume', _request_context=request)).get('success') is False)
        ctx.message.get_by_id.return_value = msg('owner', user='wxid_owner', text='继续自主项目')
        check('owner_explicit_resume_accepted', (await p.project_control(action='resume', _request_context=request)).get('success') is True)
        check('resume_never_expands_sandbox_permissions', cfg['life']['python_enabled'] and not hasattr(p.config.project, 'grant_permissions'))
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='python',
            code='import missing_package_for_acceptance', project=dict(title='自选小项目'), share=False)))]
        await p.consider()
        check('same_denial_reblocks_but_does_not_spam', p.projects.get('test@chatroom')['state'] == 'blocked' and ctx.send.hybrid.await_count == 1)
        # Two distinct capability requests remain pending when the global budget/cooldown prevents delivery.
        p.projects.block('test@chatroom', {}, dict(capability='deployment', reason='needs persistent service', alternative='save prototype'))
        check('request_respects_existing_global_cooldown', not await p.projects.notify('test@chatroom', 'sid') and p.projects.get('test@chatroom')['permissions'][-1]['state'] == 'pending')
        p.now = lambda: NOW + timedelta(days=4)
        ctx.send.hybrid.side_effect = TimeoutError('uncertain transport')
        try:
            await p.projects.notify('test@chatroom', 'sid')
        except TimeoutError:
            pass
        before = ctx.send.hybrid.await_count
        check('unknown_transport_delivery_never_retried', not await p.projects.notify('test@chatroom', 'sid') and ctx.send.hybrid.await_count == before)
        p.now = lambda: NOW
        check('ordinary_429_not_permission_request', flow.permission_from_error(dict(success=False, error='429 quota exceeded')) is None)
        check('syntax_error_not_permission_request', flow.permission_from_error(dict(success=False, error='SyntaxError')) is None)
        check('network_denial_classified', flow.permission_from_error(dict(success=False, error='PermissionError [WinError 10013] socket'))['capability'] == 'sandbox_network')
        check('unknown_capability_rejected', not p.projects.block('test@chatroom', {}, dict(capability='administrator')))
        # A read-only status call exposes progress, not private reasoning or full request data.
        status = await p.status(stream_id='sid')
        check('status_has_assignment_without_private_request', status['project_assignment']['state'] == 'blocked' and 'permissions' not in status['project_assignment'])
        p.projects.record('test@chatroom', dict(action='think', project=dict(status='done')), dict(success=True))
        check('blocked_assignment_cannot_be_declared_done', p.projects.get('test@chatroom')['state'] == 'blocked')
        request['is_notify'] = False
        ctx.message.get_by_id.return_value = msg('owner', user='wxid_owner', text='暂停自主项目')
        check('owner_pause_accepted', (await p.project_control(action='pause', _request_context=request)).get('state') == 'paused')
        before = sandbox_calls()
        ctx.llm.generate.side_effect = [dict(success=True, response=json.dumps(dict(action='python', code='print(1)', project=dict(title='自选小项目'))))]
        await p.consider()
        check('paused_project_remains_idle', sandbox_calls() == before and p.projects.get('test@chatroom')['state'] == 'paused')
        components = p.get_components()
        check('native_control_tool_host_bound', next(c for c in components if c['name'] == 'autonomous_project_control')['metadata']['invoke_method'] == 'plugin.invoke_action')
        # Exercise the actual Host payload builder: a model cannot forge the reserved requester.
        from src.plugin_runtime.component_query import ComponentQueryService
        entry = NS(plugin_id='wechatbot.autonomous-life', invoke_method='plugin.invoke_action')
        anchor = NS(message_id='host-real-message', is_notify=False, account_id='bot', scope='wx',
            message_info=NS(user_info=NS(user_id='wxid_owner', user_nickname='owner'), group_info=NS(group_id='test@chatroom')))
        context = NS(stream_id='sid', session_id='sid', platform='wechat', group_id='test@chatroom', user_id='wxid_owner',
            reasoning='', metadata=dict(anchor_message=anchor))
        invocation = NS(arguments=dict(action='resume', _request_context=dict(user_id='forged')), stream_id='sid', reasoning='')
        payload = ComponentQueryService._build_tool_invocation_payload(entry, invocation, context)
        check('actual_host_overwrites_model_supplied_identity', payload['_request_context']['user_id'] == 'wxid_owner' and payload['_request_context']['message_id'] == 'host-real-message')
        job = p.projects.get('test@chatroom'); job['state'] = 'active'; p.store.set(flow.key('test@chatroom'), job)
        p.projects.record('test@chatroom', dict(action='think', project=dict(status='done')), dict(success=True))
        check('thinking_alone_cannot_complete_assignment', p.projects.get('test@chatroom')['state'] == 'active')
        p.projects.record('test@chatroom', dict(action='python', project=dict(status='done')), dict(success=True))
        check('successful_program_can_complete_assignment', p.projects.get('test@chatroom')['state'] == 'done')
        check('completed_assignment_not_reseeded_or_reblocked', not p.projects.seed() and not p.projects.block('test@chatroom', {}, dict(capability='packages')))
    finally:
        await p.on_unload()
    await progress_tests(root/'progress')
    await initial_failure_test(root/'initial-failure')


async def initial_failure_test(root):
    mod = load('autonomous_life'); p = mod.create_plugin()
    cfg = tomllib.loads((ROOT/'plugins/autonomous_life/config.toml').read_text(encoding='utf8'))
    cfg['life']['groups'] = ['test@chatroom']
    p.set_plugin_config(cfg); p.now = lambda: NOW; p.store = mod.Store(root/'life.sqlite3')
    ctx = NS(logger=logging.getLogger('initial-failure'),
        chat=NS(get_stream_by_group_id=AsyncMock(return_value=dict(platform='wechat', group_id='test@chatroom', session_id='sid'))),
        call_capability=AsyncMock(return_value=[msg('one')]), config=NS(get=AsyncMock(return_value='test persona')),
        llm=NS(generate=AsyncMock(return_value=dict(success=True, response=json.dumps(dict(action='explore', query='project research',
            project=dict(title='研究兴趣项目', status='done', note='先读一篇资料'), share=False))))))
    p._set_context(ctx); p.projects.seed()
    with patch.object(mod, 'explore', side_effect=TimeoutError('test network outage')):
        await p.consider()
    job = p.projects.get('test@chatroom'); snap = p.store.snapshot('sid')
    check('first_network_failure_keeps_intent_without_rearming_guide', not job['guidance_pending'] and snap['projects'][0]['title'] == '研究兴趣项目' and snap['projects'][0]['status'] == 'active' and not snap['projects'][0]['code'])
    p.store.set('backoff_until', 0)
    ctx.llm.generate.return_value = dict(success=True, response='{"action":"rest","reason":"rest"}')
    with patch.object(mod, 'explore') as research:
        await p.consider()
    check('guide_not_repeated_after_initial_failure', not research.called and '这是下一次启动的一次性初始引导' not in ctx.llm.generate.await_args.args[0] and '研究兴趣项目' in ctx.llm.generate.await_args.args[0])


async def progress_tests(root):
    mod = load('autonomous_life'); p = mod.create_plugin()
    cfg = tomllib.loads((ROOT/'plugins/autonomous_life/config.toml').read_text(encoding='utf8'))
    cfg['project']['enabled'] = False
    p.set_plugin_config(cfg); p.now = lambda: NOW
    p.store = mod.Store(root/'life.sqlite3')
    ctx = NS(logger=logging.getLogger('progress-test'), call_capability=AsyncMock(return_value=dict(success=True, text='原型已经跑通，接下来想补一个小功能。')),
        send=NS(hybrid=AsyncMock(return_value=dict(sent=True))))
    p._set_context(ctx)
    def result(code='hash-one', title='自选小项目', output='49'):
        return dict(success=True, code='private source', thought='private reflection', stdout=output,
            project_progress=dict(title=title, status='active', step='python', code_sha256=code, next_step='下一步计划'))
    original = result()
    p.queue_progress('sid', {}, original)
    check('real_progress_offered_to_shared_life_outbox', len(p.store.pending_progress('sid', NOW.timestamp())) == 1)
    check('private_source_and_thought_not_in_outbox', all(x not in p.store.pending_progress('sid', NOW.timestamp())[0]['payload'] for x in ['private source','private reflection']))
    sent = await p.flush_progress('sid', [])
    reference = ctx.call_capability.await_args.kwargs['reference']
    check('progress_sent_through_native_style_and_actual_results', sent and 'project_progress' in reference and '49' in reference and 'next_step是计划' in reference and ctx.call_capability.await_args.kwargs['reply_mode'] == 'chat')
    check('progress_consumed_after_send', not p.store.pending_progress('sid', NOW.timestamp()))
    rewritten = result(); rewritten['project_progress']['next_step'] = '改写了计划措辞'
    p.queue_progress('sid', {}, rewritten)
    check('same_work_new_wording_does_not_repeat', not p.store.pending_progress('sid', NOW.timestamp()))
    p.queue_progress('sid', {}, result(code='hash-two', output='50'))
    before = ctx.send.hybrid.await_count
    check('cooldown_retains_progress_without_sending', not await p.flush_progress('sid', []) and ctx.send.hybrid.await_count == before and p.store.pending_progress('sid', NOW.timestamp()))
    p.queue_progress('sid', {}, result(code='hash-three', output='51'))
    pending = p.store.pending_progress('sid', NOW.timestamp())
    check('only_latest_progress_per_project_survives_cooldown', len(pending) == 1 and 'hash-three' in pending[0]['payload'])
    p.store = mod.Store(root/'life.sqlite3')
    p.now = lambda: NOW + timedelta(hours=2)
    check('progress_survives_restart_and_sends_after_cooldown', await p.flush_progress('sid', []))
    for n in range(2):
        p.now = lambda n=n: NOW + timedelta(hours=4+n*2)
        p.queue_progress('sid', {}, result(code='next-'+str(n), title='project-'+str(n)))
        assert await p.flush_progress('sid', [])
    p.now = lambda: NOW + timedelta(hours=8)
    p.queue_progress('sid', {}, result(code='fifth', title='project-five'))
    check('progress_and_other_shares_keep_global_four_cap', not await p.flush_progress('sid', []) and ctx.send.hybrid.await_count == 4)
    p.now = lambda: NOW + timedelta(days=1)
    check('pending_latest_progress_can_share_next_day', await p.flush_progress('sid', []))
    p.now = lambda: NOW + timedelta(days=1, hours=2)
    p.queue_progress('sid', {}, result(code='timeout', title='uncertain-project'))
    ctx.send.hybrid.side_effect = TimeoutError('unknown receipt')
    try:
        await p.flush_progress('sid', [])
    except TimeoutError:
        pass
    before = ctx.send.hybrid.await_count
    p.store = mod.Store(root/'life.sqlite3')
    check('unknown_progress_send_dedupes_across_restart', not await p.flush_progress('sid', []) and ctx.send.hybrid.await_count == before)
    p.queue_progress('sid', {}, result(code='blocked-work', title='blocked-project'))
    check('paused_project_does_not_publish_stale_progress', not await p.flush_progress('sid', [], dict(state='paused', title='blocked-project')))
    check('progress_stays_in_original_stream', not p.store.pending_progress('different-sid', p.now().timestamp()))
    p.now = lambda: NOW + timedelta(days=5)
    p.store.prune(p.now().timestamp())
    check('stale_progress_expires_without_backlog_spam', not p.store.pending_progress('sid', p.now().timestamp()))


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='maibot-project-') as tmp:
        asyncio.run(run(Path(tmp)))
    report = dict(passed=all(c['passed'] for c in CHECKS), checks=CHECKS,
        production_restart=False, physical_messages=False, live_llm=False)
    path = ROOT / 'docs/maibot-wechat-acceptance/project-kickoff-20261029.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('REPORT', path, report['passed'])
    raise SystemExit(0 if report['passed'] else 1)
